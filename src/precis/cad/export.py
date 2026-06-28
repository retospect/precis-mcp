"""CAD export (ADR 0041 §10). Export is the *only* place geometry leaves
the analytic IR. Three routes, in order of fidelity vs weight:

1. :func:`to_openscad` — pure text, **zero dependencies**. The agent- and
   human-readable form; drop it into the OpenSCAD GUI. Always available.
2. :func:`export_mesh` — **printable** STL / 3MF. Tessellates each
   primitive (:mod:`precis.cad.tessellate`), folds the boolean DAG with
   ``manifold3d`` (the same robust CSG kernel OpenSCAD uses, in-process —
   no external binary), then precis hand-writes the mesh. Gated on the
   ``[cad-export]`` extra (:func:`manifold_available`).
3. :func:`export_step` — **exact** STEP (ISO 10303) B-rep interchange for
   mechanical CAD. A mesh kernel fundamentally cannot emit STEP, so this
   delegates to the OpenCASCADE backend (:mod:`precis.cad._occt`), gated
   on the heavy ``[cad-step]`` extra (:func:`step_available`).

The design / probe loop needs none of this — meshing happens here and
nowhere else.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import numpy as np

from precis.cad.dsl import parse
from precis.cad.scene import NodeSpec, SceneSpec, _node_xform, _pattern_transforms
from precis.cad.tessellate import node_meshes
from precis.cad.vec import Transform

#: Facet resolution for curved primitives in the exported mesh.
_FN = 64


class ExportError(RuntimeError):
    """Export failed (missing backend extra, or the kernel errored)."""


# ===========================================================================
# 1. OpenSCAD source — pure, zero-dependency
# ===========================================================================


def _g(x: float) -> str:
    return f"{x:.6g}"


def _scad_primitive(config: str) -> str:
    """OpenSCAD primitive for a ``config`` string, in its local frame."""
    spec = parse(config)
    a, p = spec.alias, spec.params
    if a == "box":
        w, d, h = p["w"], p["d"], p["h"]
        return (
            f"translate([{_g(-w / 2)},{_g(-d / 2)},0]) cube([{_g(w)},{_g(d)},{_g(h)}]);"
        )
    if a == "cyl":
        return f"cylinder(h={_g(p['h'])}, r={_g(p['r'])}, $fn={_FN});"
    if a == "cone":
        return f"cylinder(h={_g(p['h'])}, r1={_g(p['r'])}, r2=0, $fn={_FN});"
    if a == "tcone":
        return (
            f"cylinder(h={_g(p['h'])}, r1={_g(p['rb'])}, r2={_g(p['rt'])}, $fn={_FN});"
        )
    if a == "sphere":
        return f"sphere(r={_g(p['r'])}, $fn={_FN});"
    if a == "hex":
        return f"cylinder(h={_g(p['h'])}, r={_g(p['r'])}, $fn=6);"
    if a == "ngon":
        return f"cylinder(h={_g(p['h'])}, r={_g(p['r'])}, $fn={int(p['n'])});"
    if a == "frustum":
        return (
            f"cylinder(h={_g(p['h'])}, r1={_g(p['rb'])}, r2={_g(p['rt'])}, "
            f"$fn={int(p['n'])});"
        )
    if a == "pyramid":
        return f"cylinder(h={_g(p['h'])}, r1={_g(p['r'])}, r2=0, $fn={int(p['n'])});"
    if a == "torus":
        return (
            f"rotate_extrude($fn={_FN}) translate([{_g(p['R'])},0,0]) "
            f"circle(r={_g(p['r'])}, $fn={_FN});"
        )
    raise ExportError(f"shape {a!r} has no OpenSCAD export")


def _multmatrix(xf: Transform) -> str:
    rows = []
    for i in range(3):
        rows.append(
            f"[{_g(float(xf.R[i, 0]))},{_g(float(xf.R[i, 1]))},"
            f"{_g(float(xf.R[i, 2]))},{_g(float(xf.t[i]))}]"
        )
    rows.append("[0,0,0,1]")
    return "multmatrix([" + ",".join(rows) + "])"


def _node_scad(node: NodeSpec) -> str:
    prim = _scad_primitive(node.config)
    if node.pattern is not None:
        placements = [f"{_multmatrix(xf)} {prim}" for xf in _pattern_transforms(node)]
        return "union() {\n  " + "\n  ".join(placements) + "\n}"
    return f"{_multmatrix(_node_xform(node.loc, node.rot))} {prim}"


def _indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


def _component_scad(nodes: list[NodeSpec]) -> str:
    cur: str | None = None
    for node in nodes:
        snippet = _node_scad(node)
        if cur is None:
            cur = snippet
        elif node.op == "add":
            cur = "union() {\n" + _indent(cur) + "\n" + _indent(snippet) + "\n}"
        elif node.op == "cut":
            cur = "difference() {\n" + _indent(cur) + "\n" + _indent(snippet) + "\n}"
        elif node.op == "intersect":
            cur = "intersection() {\n" + _indent(cur) + "\n" + _indent(snippet) + "\n}"
    return cur or ""


def _by_component(spec: SceneSpec) -> dict[str, list[NodeSpec]]:
    by_comp: dict[str, list[NodeSpec]] = {}
    for node in spec.nodes:
        by_comp.setdefault(node.component, []).append(node)
    return by_comp


def to_openscad(spec: SceneSpec, *, name: str = "design") -> str:
    """Render a :class:`SceneSpec` to OpenSCAD source (assembly = union of
    each component's folded solid)."""
    by_comp = _by_component(spec)
    blocks = []
    for comp in spec.components:
        if comp not in by_comp:
            continue
        body = _component_scad(by_comp[comp])
        if body:
            blocks.append(f"// component {comp}\n{body}")

    head = f"// {name} — generated by precis cad (ADR 0041); units = mm\n$fn = {_FN};\n"
    if not blocks:
        return head + "// (empty design)\n"
    if len(blocks) == 1:
        return head + blocks[0] + "\n"
    joined = "\n".join(_indent(b) for b in blocks)
    return head + "union() {\n" + joined + "\n}\n"


# ===========================================================================
# 2. Printable mesh — manifold3d CSG + hand-written STL / 3MF
# ===========================================================================


def manifold_available() -> bool:
    """True iff the ``manifold3d`` CSG backend (``[cad-export]``) is installed."""
    try:
        import manifold3d  # noqa: F401
    except Exception:
        return False
    return True


def _to_manifold(verts: np.ndarray, tris: np.ndarray) -> object:
    import manifold3d as m3d

    mesh = m3d.Mesh(
        vert_properties=verts.astype(np.float32),
        tri_verts=tris.astype(np.uint32),
    )
    return m3d.Manifold(mesh)


def _node_solid(node: NodeSpec) -> object:
    """A node's solid — the union of its pattern instances (or the one)."""
    parts = [_to_manifold(v, t) for v, t in node_meshes(node)]
    cur = parts[0]
    for p in parts[1:]:
        cur = cur + p  # manifold3d: + is union
    return cur


def _component_solids(spec: SceneSpec) -> list[tuple[str, object]]:
    """Fold each component into its own ``manifold3d`` solid (per-component
    boolean DAG), **without** unioning across components — so an assembly
    keeps its parts distinct for the formats that can carry them (3MF)."""
    if not manifold_available():
        raise ExportError(
            "manifold3d not installed — mesh export needs it. "
            "Install the extra:  pip install 'precis-mcp[cad-export]'"
        )
    by_comp = _by_component(spec)
    out: list[tuple[str, object]] = []
    for comp in spec.components:
        cur: object | None = None
        for node in by_comp.get(comp, []):
            man = _node_solid(node)
            if cur is None:
                cur = man
            elif node.op == "add":
                cur = cur + man
            elif node.op == "cut":
                cur = cur - man
            elif node.op == "intersect":
                cur = cur ^ man  # manifold3d: ^ is intersection
        if cur is not None:
            out.append((comp, cur))
    if not out:
        raise ExportError("design has no solid geometry to export")
    return out


def _design_solid(spec: SceneSpec) -> object:
    """The whole design as one welded ``manifold3d`` solid (union of every
    component) — used for STL, which has no concept of separate parts."""
    solids = _component_solids(spec)
    result = solids[0][1]
    for _name, c in solids[1:]:
        result = result + c
    return result


def _mesh_of(solid: object) -> tuple[np.ndarray, np.ndarray]:
    mesh = solid.to_mesh()  # type: ignore[attr-defined]
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    if tris.size == 0:
        raise ExportError("the folded design produced an empty mesh")
    return verts, tris


def _solid_mesh(spec: SceneSpec) -> tuple[np.ndarray, np.ndarray]:
    """Fold the design and return its final welded ``(verts, tris)`` mesh."""
    return _mesh_of(_design_solid(spec))


def _component_meshes(spec: SceneSpec) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """One ``(name, verts, tris)`` per component (parts kept separate)."""
    return [(name, *_mesh_of(solid)) for name, solid in _component_solids(spec)]


def _write_binary_stl(path: Path, verts: np.ndarray, tris: np.ndarray) -> None:
    tv = verts[tris]  # (M, 3, 3)
    normals = np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)
    with open(path, "wb") as fh:
        fh.write(b"precis cad STL (ADR 0041)".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        for i in range(len(tris)):
            fh.write(struct.pack("<3f", *normals[i]))
            fh.write(struct.pack("<3f", *tv[i, 0]))
            fh.write(struct.pack("<3f", *tv[i, 1]))
            fh.write(struct.pack("<3f", *tv[i, 2]))
            fh.write(struct.pack("<H", 0))


_3MF_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_3MF_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Relationships "
    'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


def _3mf_object(obj_id: int, name: str, verts: np.ndarray, tris: np.ndarray) -> str:
    vs = "".join(f'<vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in verts)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (
        f'<object id="{obj_id}" name="{name}" type="model"><mesh>'
        f"<vertices>{vs}</vertices><triangles>{ts}</triangles>"
        "</mesh></object>"
    )


def _write_3mf(path: Path, parts: list[tuple[str, np.ndarray, np.ndarray]]) -> None:
    """Write a 3MF package. Each part is its **own** ``<object>`` referenced
    by the ``<build>`` — so a multi-component assembly stays separable in
    the slicer (3MF natively carries multiple objects)."""
    objects = "".join(
        _3mf_object(i, name, v, t) for i, (name, v, t) in enumerate(parts, start=1)
    )
    items = "".join(f'<item objectid="{i}"/>' for i in range(1, len(parts) + 1))
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{objects}</resources>"
        f"<build>{items}</build></model>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _3MF_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _3MF_RELS)
        zf.writestr("3D/3dmodel.model", model)


_MESH_FORMATS = ("stl", "3mf")


def export_mesh(
    spec: SceneSpec, out_path: str | Path, *, fmt: str | None = None
) -> Path:
    """Fold ``spec`` to a watertight mesh and write it as STL or 3MF.

    ``fmt`` defaults to the ``out_path`` suffix. **STL** has no notion of
    parts, so a multi-component assembly is welded into one body. **3MF**
    carries each component as its own object, so the assembly stays
    separable in the slicer. Raises :class:`ExportError` if ``manifold3d``
    is missing or the format is unknown."""
    out = Path(out_path)
    f = (fmt or out.suffix.lstrip(".")).lower()
    if f == "stl":
        verts, tris = _solid_mesh(spec)
        _write_binary_stl(out, verts, tris)
    elif f == "3mf":
        _write_3mf(out, _component_meshes(spec))
    else:
        raise ExportError(
            f"unknown mesh format {f!r}; supported: {list(_MESH_FORMATS)}"
        )
    return out


# ===========================================================================
# 3. Exact STEP B-rep — OpenCASCADE (delegated, heavy extra)
# ===========================================================================


def step_available() -> bool:
    """True iff the OpenCASCADE STEP backend (``[cad-step]``) is installed."""
    from precis.cad import _occt

    return _occt.available()


def export_step(spec: SceneSpec, out_path: str | Path) -> Path:
    """Export ``spec`` to an exact STEP (ISO 10303) B-rep via OpenCASCADE.

    Raises :class:`ExportError` if the ``[cad-step]`` extra is not
    installed (a mesh kernel cannot produce STEP — this path is the only
    one that can)."""
    from precis.cad import _occt

    if not _occt.available():
        raise ExportError(
            "OpenCASCADE not installed — exact STEP export needs it. "
            "Install the extra:  pip install 'precis-mcp[cad-step]'"
        )
    return _occt.export_step(spec, Path(out_path))


#: Format → which backend extra is required (for handler error messages).
EXPORT_FORMATS = {
    "scad": "always",
    "stl": "cad-export",
    "3mf": "cad-export",
    "step": "cad-step",
}
