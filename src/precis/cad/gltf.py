"""CAD IR → glTF 2.0 (``.glb``) — the web viewer's *and* download artifact.

The one mesh pipeline the browser sees. Everything derives from the analytic
IR (:mod:`precis.cad.scene`); the viewer derives too, through the same
:func:`precis.cad.tessellate.node_meshes` front-half the STL/3MF exporter uses,
emitted as a self-contained binary glTF that three.js renders and the user
downloads (same bytes — no drift). Pure numpy + stdlib; **no heavy kernel** for
the default ``features`` mode. ``solid`` mode rents ``manifold3d`` (the
``[cad-export]`` extra) to fold the boolean DAG into the true welded solid.

Two things make it read well:

- **Per-part colours** — each ``component`` gets a distinct hue (:func:`component_colors`),
  so an assembly's parts are visually separable. ``cut``/``intersect`` nodes are
  translucent "tool volumes" (``alphaMode: BLEND``) in ``features`` mode.
- **Crease-angle vertex normals** (:func:`_smooth_normals`) — normals are averaged
  across an edge only when the dihedral angle is below the crease threshold, so a
  64-facet cylinder barrel shades glass-smooth while a box keeps crisp edges.

Coordinate frame: the IR is +Z-up (mm); glTF is +Y-up. Positions and normals are
rotated −90° about X on the way out (``(x, y, z) → (x, z, −y)``) so the ``.glb``
looks right in any glTF viewer (Blender, Windows 3D Viewer, three.js), not just
ours.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.scene import SceneSpec
from precis.cad.tessellate import Mesh, TessellationError, node_meshes

#: Dihedral angle (degrees) below which an edge is smooth-shaded. 64-facet
#: barrels vary ~5.6° edge-to-edge (smoothed); cap/box edges are 90° (kept sharp).
_CREASE_DEG = 40.0

#: Per-component hue palette (hex, no ``#``). Cycles if a design has more parts.
_PALETTE: tuple[str, ...] = (
    "4f8ef7",  # blue
    "f2704f",  # orange
    "4fbf7b",  # green
    "b07ff2",  # purple
    "f2c14f",  # amber
    "4fd0d6",  # teal
    "e75fa3",  # pink
    "8a9bb0",  # slate
)


def component_colors(components: list[str]) -> dict[str, str]:
    """Deterministic ``component → #rrggbb`` map (parts = different colours).

    The web legend and the glTF materials both call this, so the swatch beside a
    part name matches the solid on screen."""
    return {c: "#" + _PALETTE[i % len(_PALETTE)] for i, c in enumerate(components)}


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


# ---------------------------------------------------------------------------
# geometry → smooth-normalled, y-up triangle soup
# ---------------------------------------------------------------------------

#: z-up (IR) → y-up (glTF) basis: (x, y, z) → (x, z, -y). A pure −90° X rotation.
_ZUP_TO_YUP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])


def _smooth_normals(
    verts: NDArray[np.float64], tris: NDArray[np.int64], crease_deg: float = _CREASE_DEG
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.int64]]:
    """Unweld to per-face corners and average normals across an edge only when
    the two faces meet below the crease angle — smooth curves, sharp edges."""
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(fn, axis=1, keepdims=True)
    fn = np.divide(fn, ln, out=np.zeros_like(fn), where=ln > 0)

    incident: dict[int, list[int]] = {}
    for f, tri in enumerate(tris):
        for vi in tri:
            incident.setdefault(int(vi), []).append(f)

    cos_thr = math.cos(math.radians(crease_deg))
    n_corners = len(tris) * 3
    out_pos = np.empty((n_corners, 3), dtype=np.float64)
    out_nrm = np.empty((n_corners, 3), dtype=np.float64)
    out_idx = np.arange(n_corners, dtype=np.int64).reshape(-1, 3)
    k = 0
    for f, tri in enumerate(tris):
        face_n = fn[f]
        for vi in tri:
            acc = face_n.copy()
            for g in incident[int(vi)]:
                if g != f and float(fn[g] @ face_n) >= cos_thr:
                    acc = acc + fn[g]
            gl = float(np.linalg.norm(acc))
            out_nrm[k] = acc / gl if gl > 0 else face_n
            out_pos[k] = verts[vi]
            k += 1
    return out_pos, out_nrm, out_idx


def _to_yup(a: NDArray[np.float64]) -> NDArray[np.float32]:
    return (a @ _ZUP_TO_YUP.T).astype(np.float32)


# ---------------------------------------------------------------------------
# glTF assembly
# ---------------------------------------------------------------------------


class _GlbBuilder:
    """Accumulate meshes into one binary buffer + the referencing glTF JSON."""

    def __init__(self) -> None:
        self.bin = bytearray()
        self.bufferViews: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []
        self.materials: list[dict[str, Any]] = []
        self.meshes: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self._mat_cache: dict[tuple[float, float, float, float], int] = {}

    def _view(self, data: bytes, target: int) -> int:
        # 4-byte align (all our components are 4-byte, so offsets stay aligned).
        while len(self.bin) % 4:
            self.bin.append(0)
        offset = len(self.bin)
        self.bin.extend(data)
        self.bufferViews.append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(data),
                "target": target,
            }
        )
        return len(self.bufferViews) - 1

    def _material(self, rgba: tuple[float, float, float, float]) -> int:
        key = tuple(round(c, 4) for c in rgba)  # type: ignore[assignment]
        if key in self._mat_cache:
            return self._mat_cache[key]
        mat: dict[str, Any] = {
            "pbrMetallicRoughness": {
                "baseColorFactor": list(rgba),
                "metallicFactor": 0.1,
                "roughnessFactor": 0.65,
            },
            "doubleSided": True,
        }
        if rgba[3] < 1.0:
            mat["alphaMode"] = "BLEND"
        self.materials.append(mat)
        idx = len(self.materials) - 1
        self._mat_cache[key] = idx  # type: ignore[index]
        return idx

    def add_mesh(
        self, mesh: Mesh, *, name: str, rgba: tuple[float, float, float, float]
    ) -> None:
        verts, tris = mesh
        if tris.size == 0:
            return
        pos, nrm, idx = _smooth_normals(np.asarray(verts, float), np.asarray(tris))
        pos_y, nrm_y = _to_yup(pos), _to_yup(nrm)
        idx32 = idx.reshape(-1).astype(np.uint32)

        ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
        pv = self._view(pos_y.tobytes(), ARRAY_BUFFER)
        nv = self._view(nrm_y.tobytes(), ARRAY_BUFFER)
        iv = self._view(idx32.tobytes(), ELEMENT_ARRAY_BUFFER)

        pmin = [float(x) for x in pos_y.min(axis=0)]
        pmax = [float(x) for x in pos_y.max(axis=0)]
        a_pos = self._accessor(pv, 5126, len(pos_y), "VEC3", pmin, pmax)
        a_nrm = self._accessor(nv, 5126, len(nrm_y), "VEC3")
        a_idx = self._accessor(iv, 5125, len(idx32), "SCALAR")

        mat = self._material(rgba)
        self.meshes.append(
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": a_pos, "NORMAL": a_nrm},
                        "indices": a_idx,
                        "material": mat,
                    }
                ]
            }
        )
        self.nodes.append({"mesh": len(self.meshes) - 1, "name": name})

    def _accessor(
        self,
        view: int,
        comp_type: int,
        count: int,
        type_: str,
        vmin: list[float] | None = None,
        vmax: list[float] | None = None,
    ) -> int:
        acc: dict[str, Any] = {
            "bufferView": view,
            "componentType": comp_type,
            "count": count,
            "type": type_,
        }
        if vmin is not None:
            acc["min"], acc["max"] = vmin, vmax
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def to_glb(self) -> bytes:
        gltf: dict[str, Any] = {
            "asset": {"version": "2.0", "generator": "precis cad (ADR 0041)"},
            "scene": 0,
            "scenes": [{"nodes": list(range(len(self.nodes)))}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": self.materials,
            "accessors": self.accessors,
            "bufferViews": self.bufferViews,
            "buffers": [{"byteLength": len(self.bin)}],
        }
        json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)  # pad to 4 with spaces
        bin_bytes = bytes(self.bin) + b"\x00" * ((4 - len(self.bin) % 4) % 4)

        total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
        out = bytearray()
        out += struct.pack("<III", 0x46546C67, 2, total)  # magic 'glTF', version, len
        out += struct.pack("<II", len(json_bytes), 0x4E4F534A)  # 'JSON'
        out += json_bytes
        out += struct.pack("<II", len(bin_bytes), 0x004E4942)  # 'BIN\0'
        out += bin_bytes
        return bytes(out)


def _features_glb(spec: SceneSpec, colors: dict[str, str]) -> bytes:
    """Per-feature meshes: one clickable glTF node per design node, coloured by
    component; ``cut``/``intersect`` nodes translucent so removals read as ghosts."""
    b = _GlbBuilder()
    for node in spec.nodes:
        try:
            instances = node_meshes(node)
        except TessellationError:
            continue  # unbounded half-space (chamfer) — nothing to draw
        # merge a pattern's instances into one clickable object
        merged = _merge(instances)
        if merged is None:
            continue
        r, g, bl = _hex_to_rgb(colors.get(node.component, "#8a9bb0"))
        alpha = 0.32 if node.op in ("cut", "intersect") else 1.0
        b.add_mesh(merged, name=node.name, rgba=(r, g, bl, alpha))
    return b.to_glb()


def _solid_glb(spec: SceneSpec, colors: dict[str, str]) -> bytes:
    """The true CSG-folded solid (one welded mesh per component) — matches STL
    export. Rents ``manifold3d``; the caller gates on :func:`solid_available`."""
    from precis.cad.export import _component_meshes  # local: heavy extra

    b = _GlbBuilder()
    for name, verts, tris in _component_meshes(spec):
        r, g, bl = _hex_to_rgb(colors.get(name, "#8a9bb0"))
        b.add_mesh((verts, tris), name=name, rgba=(r, g, bl, 1.0))
    return b.to_glb()


def _merge(meshes: list[Mesh]) -> Mesh | None:
    """Concatenate meshes into one ``(verts, tris)`` with offset indices."""
    if not meshes:
        return None
    if len(meshes) == 1:
        return meshes[0]
    all_v: list[NDArray[np.float64]] = []
    all_t: list[NDArray[np.int64]] = []
    base = 0
    for v, t in meshes:
        all_v.append(np.asarray(v, float))
        all_t.append(np.asarray(t) + base)
        base += len(v)
    return np.concatenate(all_v), np.concatenate(all_t)


def solid_available() -> bool:
    """True iff ``mode='solid'`` can fold (the ``[cad-export]`` extra)."""
    from precis.cad.export import manifold_available

    return manifold_available()


def to_glb(
    spec: SceneSpec, *, mode: str = "features", colors: dict[str, str] | None = None
) -> bytes:
    """Render ``spec`` to a binary glTF (``.glb``).

    ``mode='features'`` (default) draws each design node as its own clickable,
    per-part-coloured mesh (cuts translucent) — always available, no heavy kernel.
    ``mode='solid'`` folds the boolean DAG into the true welded solid via
    ``manifold3d`` (raises ``ImportError`` from the backend if the extra is
    absent — gate with :func:`solid_available`)."""
    cols = colors or component_colors(spec.components)
    if mode == "solid":
        return _solid_glb(spec, cols)
    return _features_glb(spec, cols)
