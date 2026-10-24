"""``precis_geom.assembly`` (``pga``) — the one assembly implementation
scripts and MCP ops share (structure-workspaces spec §Assembly API,
slice 2a).

The DRY rule: the geometry math — pitch expansion, pose composition,
port alignment, the fused-tile seam — lives once here;
``precis_se/ops.py`` keeps naming, validation and tree bookkeeping and
calls this library for the numbers. A script and an op on the same
inputs produce the same block.

Products are **immutable**: every call returns a new :class:`Geom`; a
move is a recipe edit plus a rebuild, never an in-place transform (the
spec's immutable-products ruling). Each call is ``@traced`` — its own
creator frame, and the object's atoms carry that frame's id as their
``maker``:

- ``assembly`` mints an ``assembly`` frame plus one ``instance`` frame
  per direct child; a leaf child's atoms are stamped with its
  ``instance`` frame id, a nested assembly's atoms keep their own
  inner stamps — so ``frame:<key>`` on the assembly selects the whole
  subtree's atoms through the maker→frame join.
- ``tile(..., fused=False)`` mints one ``tile`` frame per copy
  (``tile#0..#n-1``) — packed, the ``array_block`` shape — or, from a
  bare template *name*, a symbolic array the manifest writes and the
  importer replays through the ``array_block`` op.
- ``tile(..., fused=True)`` mints one ``tile`` frame whose ``made`` is
  the seam: translational copies at the period step, coincident atoms
  identified, bonds/rings unioned (the guard-column overlap of a
  periodic sheet, k2 across each boundary).
- ``translate``/``rotate``/``transform`` mint a ``pose`` frame,
  ``mirror`` a ``mirror`` frame (improper transform — atomic emits the
  ``chirality.flipped`` INFO and flips ``meta['chiral_index']``),
  ``place`` a ``place`` frame (the port-to-port solve ``connect``
  runs). ``junction`` (two rim atom sets by minted bonds) is a later
  spec row, not this slice.

Units: an atomic :class:`Geom` is Å-native (the ``precis.structure``
enclave); a macro one rides the cad design's own unit. Vector
arguments accept a bare 3-vector (native) or a unit string the shared
``precis.utils.units`` grammar resolves (``"[0 0 1.2] nm"``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from precis.cad import vec as _vec
from precis.cad.vec import Transform
from precis.errors import BadInput
from precis.utils.units import parse_vector_quantity
from precis_geom import trace as _trace

_A_PER_M = 1e10
#: Atom positions closer than this are the same site under a fused tile
#: (guard-column overlap) — far below any bond length (σ ≈ 1.42 Å) and
#: far above the structure_sha rounding (1e-6).
_MERGE_EPS_A = 1e-3

_FRAME_TILE = "tile"
_FRAME_INSTANCE = "instance"
_FRAME_ASSEMBLY = "assembly"
_FRAME_POSE = "pose"
_FRAME_MIRROR = "mirror"
_FRAME_PLACE = "place"


# ── objects ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Port:
    """A named attachment point on a :class:`Geom`, in the object's own
    frame (Å atomic / design units macro). ``atom`` binds it to a scene
    label — the manifest port's ``atom`` key; a coordinate port
    (``pos``) serves :func:`place` either way."""

    name: str
    pos: tuple[float, float, float] | None = None
    direction: tuple[float, float, float] | None = None
    roles: tuple[str, ...] = ()
    atom: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Geom:
    """An immutable built object: geometry (atomic ``scene`` or macro
    ``design``), ports, sub-assembly ``placements`` and provenance.

    ``placements`` are ``(child, xform, frame_id)`` — the child's pose
    in this assembly's frame and the ``instance``/``tile`` frame that
    placed it (the ``maker`` the child's atoms carry once flattened).
    ``template``+``array`` is the symbolic packed-tile form (a manifest
    instance row), ``pose`` the placement a parent assembly or the
    manifest consumes."""

    scene: Any = None
    design: Any = None
    ports: dict[str, Port] = field(default_factory=dict)
    placements: tuple[tuple[Geom, Transform, int | None], ...] = ()
    template: str | None = None
    array: dict[str, Any] | None = None
    period: tuple[float, float, float] | None = None  # packed/fused step
    pose: Transform | None = None
    fused: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    findings: tuple[str, ...] = ()

    def port(self, name: str) -> tuple[Geom, Port]:
        """``(self, port)`` — the handle :func:`place` takes."""
        try:
            return self, self.ports[name]
        except KeyError:
            raise BadInput(
                f"no such port on {self.name or 'geom'!r}: {name!r}. "
                f"Available: {sorted(self.ports) or '(none)'}"
            ) from None

    @property
    def name(self) -> str | None:
        return self.meta.get("name")

    @property
    def atomic(self) -> bool:
        return self.scene is not None or any(c.atomic for c, _x, _f in self.placements)

    def flatten(self) -> Any:
        """The merged geometry — a ``precis.structure.scene.Scene`` for
        an atomic object (placements expanded, ``pose`` applied, atoms
        re-labelled deterministically, ``maker`` = the placing frame's
        id)."""
        if self.atomic:
            if self.scene is not None and not self.placements and self.pose is None:
                return self.scene
            return _flatten_atomic(self)
        raise BadInput(
            "pga: a symbolic object (template/array, no geometry) has "
            "nothing to flatten — out.instance writes it, not out.block"
        )


def _leaf() -> int | None:
    return _trace.leaf_id()


def _record(**made: int) -> None:
    if made:
        _trace.record(made=made)


# ── units ────────────────────────────────────────────────────────────────


def _native_vec(value: Any, geom_scale: str, what: str) -> np.ndarray:
    """A 3-vector argument → native float64 (Å atomic, design units
    macro). Bare numerics are native; strings and per-component
    quantities go through the shared SI grammar then scale in."""
    if isinstance(value, str) or (
        isinstance(value, (list, tuple)) and any(isinstance(c, str) for c in value)
    ):
        si = parse_vector_quantity(value, "length", arg_name=what)
        factor = _A_PER_M if geom_scale == "atomic" else 1e3
        return np.asarray(si, dtype=np.float64) * factor
    try:
        v = np.asarray(value, dtype=np.float64).reshape(3)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"{what} must be a 3-vector (native units or a unit string), got {value!r}"
        ) from exc
    return v


def _scale_of(g: Geom) -> str:
    return "atomic" if g.atomic else "macro"


def _dir_vec(value: Any, what: str) -> np.ndarray:
    """A dimensionless 3-vector (an axis, a normal) — bare numbers only,
    no unit grammar (a direction has no dimension to fail)."""
    try:
        return np.asarray(value, dtype=np.float64).reshape(3)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{what} must be a 3-vector, got {value!r}") from exc


# ── array spec (the array_block numbers — shared by ops and scripts) ────


def _unit_axis(value: Any, what: str) -> list[float]:
    """A unit axis — normalised by the same pure-Python
    ``math.sqrt(sum(x*x))`` the blocktree's ``_unit_vec`` uses, so the
    op path and a script's ``pga.tile`` store byte-identical specs
    (numpy's ``linalg.norm`` rounds differently in the last bit)."""
    v = _dir_vec(value, f"{what}: axis")
    n = math.sqrt(sum(float(x) * float(x) for x in v))
    if n == 0.0:
        raise BadInput(
            f"{what}: axis must be a nonzero vector",
            next="give a direction the pattern steps along, e.g. [0, 0, 1]",
        )
    return [float(x) / n for x in v]


def array_spec(spec: dict[str, Any], *, what: str = "array") -> dict[str, Any]:
    """The canonical array spec ``array_block`` stores — ``{'kind':
    'linear'|'polar', 'count', ...}`` with lengths in SI metres and a
    unit axis. ``pga.tile`` emits this verbatim on a symbolic packed
    tile; the op-side normaliser delegates here (slice 2a.2) so both
    paths store byte-identical specs."""
    if spec.get("kind") == "linear":
        count = int(spec["count"])
        pitch = float(spec["pitch"])
        axis = _unit_axis(spec["axis"], what)
        return {
            "kind": "linear",
            "count": count,
            "pitch": pitch,
            "axis": axis,
        }
    if spec.get("kind") == "polar":
        return {
            "kind": "polar",
            "count": int(spec["count"]),
            "radius": float(spec["radius"]),
            "axis": _unit_axis(spec["axis"], what),
        }
    raise BadInput(
        f"{what}: kind must be 'linear' or 'polar', got {spec!r}",
        options=["linear", "polar"],
    )


def _linear_spec(count: int, pitch_native: np.ndarray, scale: str) -> dict[str, Any]:
    """The SI array spec for a packed tile — ``pitch`` metres, ``axis``
    unit. Native Å (atomic) / mm (macro) → m."""
    factor = 1e-10 if scale == "atomic" else 1e-3
    pitch = float(np.linalg.norm(pitch_native))
    if pitch <= 0.0:
        raise BadInput(
            "tile pitch must be nonzero",
            next="give the per-copy translation vector, e.g. '[0 0 1.2] nm'",
        )
    return array_spec(
        {
            "kind": "linear",
            "count": count,
            "pitch": pitch * factor,
            "axis": [float(x) for x in pitch_native / pitch],
        }
    )


def _polar_spec(
    count: int, radius_native: float, axis_native: np.ndarray, scale: str
) -> dict[str, Any]:
    factor = 1e-10 if scale == "atomic" else 1e-3
    return array_spec(
        {
            "kind": "polar",
            "count": count,
            "radius": float(radius_native) * factor,
            "axis": [float(x) for x in axis_native],
        }
    )


# ── scene plumbing ───────────────────────────────────────────────────────


def _bounds_cell(carts: np.ndarray) -> Any:
    """A deterministic orthorhombic cell around the atoms (5 Å margin):
    the same geometry always mints the same cell, so content shas
    compare across build paths."""
    from precis.structure.cell import Cell

    if len(carts) == 0:
        return Cell(np.eye(3) * 20.0, (False, False, False))
    lo = carts.min(axis=0) - 5.0
    hi = carts.max(axis=0) + 5.0
    lat = np.diag(np.maximum(hi - lo, 1.0))
    return Cell(lat, (False, False, False))


def _scene_from_parts(
    elements: list[str],
    carts: np.ndarray,
    bonds: list[tuple[int, int, float]],
    *,
    cell: Any = None,
    maker: int | None = None,
) -> Any:
    """A ``precis.structure.scene.Scene`` from cartesian parts — atoms
    labelled ``a<El><n>`` in input order."""
    from precis.structure.scene import Atom, Bond, Scene

    cell = cell if cell is not None else _bounds_cell(carts)
    inv = np.linalg.inv(np.asarray(cell.lattice, dtype=float))
    fracs = np.asarray(carts, dtype=float) @ inv if len(carts) else np.zeros((0, 3))
    atoms: dict[str, Atom] = {}
    counters: dict[str, int] = {}
    for i, el in enumerate(elements):
        counters[el] = counters.get(el, 0) + 1
        atoms[f"a{el}{counters[el]}"] = Atom(
            label=f"a{el}{counters[el]}",
            element=el,
            frac=fracs[i],
            maker=maker,
        )
    labels = list(atoms)
    return Scene(
        cell=cell,
        atoms=atoms,
        bonds=[
            Bond(i=labels[int(a)], j=labels[int(b)], order=float(o), maker=maker)
            for a, b, o in bonds
        ],
    )


def _scene_carts(scene: Any) -> tuple[list[str], list[str], np.ndarray]:
    """``(labels, elements, carts)`` of a Scene in deterministic label
    order (insertion order — ``atoms`` is a dict)."""
    labels = list(scene.atoms)
    return (
        labels,
        [scene.atoms[la].element for la in labels],
        np.asarray(
            [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels],
            dtype=np.float64,
        ).reshape(-1, 3),
    )


def _scene_bonds(scene: Any, labels: list[str]) -> list[tuple[int, int, float]]:
    """``(i, j, order)`` ordinal bonds in ``labels`` order."""
    idx = {la: k for k, la in enumerate(labels)}
    return [
        (idx[b.i], idx[b.j], float(b.order))
        for b in scene.bonds
        if b.i in idx and b.j in idx
    ]


def geom(obj: Any, *, ports: Any = None, meta: dict[str, Any] | None = None) -> Geom:
    """Wrap a built object — a ``Scene``, a hexfold ``Net`` / ``Block`` /
    ``Strip``, or a cad ``Design`` — as a :class:`Geom`. ``ports`` is a
    mapping name → :class:`Port` or kwargs dict (``pos``/``direction``/
    ``roles``/``atom``/``annotations``); ``meta`` rides through (e.g.
    ``{'chiral_index': [n, m]}``, flipped by :func:`mirror`)."""
    ports_d: dict[str, Port] = {}
    for name, p in (ports or {}).items():
        if isinstance(p, Port):
            ports_d[name] = p
        else:
            ports_d[name] = Port(name=name, **{k: v for k, v in dict(p).items()})
    if isinstance(obj, Geom):
        if ports_d or meta:
            return replace(
                obj,
                ports={**obj.ports, **ports_d},
                meta={**obj.meta, **(meta or {})},
            )
        return obj
    if hasattr(obj, "atoms") and hasattr(obj, "cell") and hasattr(obj, "bonds"):
        m = {"units": "A", **dict(meta or {})}
        return Geom(scene=obj, ports=ports_d, meta=m)
    if hasattr(obj, "design"):
        m = {"units": "mm", **dict(meta or {})}
        return Geom(design=obj, ports=ports_d, meta=m)
    # hexfold Net / Block / Strip — atomic geometry in Å
    block = getattr(obj, "block", obj)
    elements = getattr(block, "elements", None)
    coords = getattr(block, "coords", None)
    bonds = getattr(block, "bonds", None)
    if elements is None:
        atoms = getattr(obj, "atoms", None)
        seed3 = getattr(obj, "seed3", None)
        if atoms is not None and seed3 is not None:  # a Net
            ordered = sorted(atoms, key=lambda a: a.ord)
            elements = [a.element for a in ordered]
            coords = np.asarray(seed3, dtype=np.float64)
            bonds = list(getattr(obj, "bonds", ()))
    if elements is not None and coords is not None:
        scene = _scene_from_parts(
            list(elements),
            np.asarray(coords, dtype=np.float64),
            # (i, j, order) or (i, j, order, kind) — GeneratedBlock and
            # hexfold Block/Net bond rows alike
            [(int(b_[0]), int(b_[1]), float(b_[2])) for b_ in (bonds or ())],
        )
        m = {"units": "A", **dict(meta or {})}
        return Geom(scene=scene, ports=ports_d, meta=m)
    raise BadInput(
        f"pga: unrecognised geometry {type(obj).__name__} — want a "
        "structure Scene, a hexfold Net/Block/Strip, or a cad Design",
        options=[
            "precis.structure.scene.Scene",
            "hexfold Net/Block/Strip",
            "cad Design",
        ],
    )


# ── transforms ───────────────────────────────────────────────────────────


def _rot_axis(axis: np.ndarray, rad: float) -> np.ndarray:
    a = _vec.normalize(axis)
    ux = np.array(
        [[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]],
        dtype=np.float64,
    )
    return np.eye(3) + math.sin(rad) * ux + (1.0 - math.cos(rad)) * (ux @ ux)


def _transform_scene(scene: Any, T: Transform, *, maker: int | None) -> Any:
    """A Scene under ``T`` — atoms remapped (``maker`` re-stamped), cell
    and bonds carried (orthonormal ``R`` preserves cartesian
    distances)."""
    from precis.structure.scene import Atom, Scene

    atoms: dict[str, Atom] = {}
    for la, a in scene.atoms.items():
        cart = T.apply(scene.cell.frac_to_cart(a.frac))
        atoms[la] = replace(a, frac=scene.cell.cart_to_frac(cart), maker=maker)
    return Scene(
        cell=scene.cell,
        atoms=atoms,
        bonds=[replace(b, maker=maker) for b in scene.bonds],
        measures=list(scene.measures),
        label_hi=dict(scene.label_hi),
    )


def _transform_ports(ports: dict[str, Port], T: Transform) -> dict[str, Port]:
    out: dict[str, Port] = {}
    for name, p in ports.items():
        out[name] = replace(
            p,
            pos=tuple(T.apply(_vec.as_vec3(p.pos))) if p.pos is not None else None,
            direction=(
                tuple(T.apply_dir(_vec.as_vec3(p.direction)))
                if p.direction is not None
                else None
            ),
        )
    return out


def _apply_T(g: Geom, T: Transform, *, maker: int | None) -> Geom:
    """``T`` applied to ``g`` as a whole object — the invariant is
    ``result.flatten() == T(g.flatten())``: a **posed** object is
    repositioned in its parent frame (``pose = T ∘ g.pose``, local
    geometry untouched); an unposed object has ``T`` baked into its
    geometry eagerly (atoms, placements, ports, array axis). The two
    paths agree because ``pose=None`` is the identity placement."""
    if g.pose is not None:
        return replace(g, pose=T.compose(g.pose))
    scene = _transform_scene(g.scene, T, maker=maker) if g.scene is not None else None
    placements = tuple((c, T.compose(xf), fid) for c, xf, fid in g.placements)
    array = g.array
    if array is not None:
        array = dict(array)
        if array.get("axis") is not None:
            array["axis"] = [float(x) for x in T.apply_dir(_vec.as_vec3(array["axis"]))]
    return replace(
        g,
        scene=scene,
        placements=placements,
        array=array,
        period=(
            tuple(T.apply_dir(_vec.as_vec3(g.period))) if g.period is not None else None
        ),
        ports=_transform_ports(g.ports, T),
    )


@_trace.traced(name=_FRAME_POSE)
def transform(obj: Any, T: Transform) -> Geom:
    """``obj`` moved by ``T`` — a new object, never moved in place; the
    ``pose`` frame. On a posed object ``T`` composes the pose (a move
    in the parent frame); otherwise it bakes into the geometry."""
    return _apply_T(geom(obj), T, maker=_leaf())


def translate(obj: Any, v: Any) -> Geom:
    """``transform(obj, translation(v))`` — ``v`` a 3-vector (native or
    unit string)."""
    g = geom(obj)
    tv = _native_vec(v, _scale_of(g), "translate v")
    return transform(g, Transform(R=np.eye(3), t=tv))


def rotate(obj: Any, axis: Any, deg: float) -> Geom:
    """``transform(obj, rotation(axis, deg°))`` — right-hand rule about
    the given axis."""
    g = geom(obj)
    ax = _dir_vec(axis, "rotate axis")
    return transform(
        g, Transform(R=_rot_axis(ax, math.radians(float(deg))), t=np.zeros(3))
    )


@_trace.traced(name=_FRAME_MIRROR)
def mirror(obj: Any, plane: Any = "yz") -> Geom:
    """The mirror image of ``obj`` through ``plane`` — an improper
    transform. ``plane`` is ``'xy'|'yz'|'xz'``, a unit normal, or
    ``(point, normal)``. Atomic emits the ``chirality.flipped`` INFO and
    swaps ``meta['chiral_index']`` (n, m) → (m, n); macro flips face
    normals via the improper instance transform."""
    g = geom(obj)
    if isinstance(plane, str):
        normals = {"xy": (0.0, 0.0, 1.0), "yz": (1.0, 0.0, 0.0), "xz": (0.0, 1.0, 0.0)}
        if plane not in normals:
            raise BadInput(
                f"mirror plane must be 'xy', 'yz', 'xz' or a (point, normal) "
                f"pair, got {plane!r}",
                options=["xy", "yz", "xz"],
            )
        point, normal = np.zeros(3), np.asarray(normals[plane])
    elif isinstance(plane, (list, tuple)) and len(plane) == 2:
        point = np.asarray(plane[0], dtype=np.float64)
        normal = np.asarray(plane[1], dtype=np.float64)
    else:
        point = np.zeros(3)
        normal = np.asarray(plane, dtype=np.float64)
    n = _vec.normalize(_vec.as_vec3(normal))
    T = _vec.reflection(n, point)  # the one improper transform — det -1
    meta = dict(g.meta)
    findings = list(g.findings)
    if "chiral_index" in meta:
        ci = list(meta["chiral_index"])
        meta["chiral_index"] = [ci[1], ci[0]]
        findings.append(
            f"chirality.flipped: chiral index [{ci[0]}, {ci[1]}] → "
            f"[{ci[1]}, {ci[0]}] (the other hand)"
        )
    else:
        findings.append("chirality.flipped: geometry mirrored (improper transform)")
    return replace(_apply_T(g, T, maker=_leaf()), meta=meta, findings=tuple(findings))


# ── tile ─────────────────────────────────────────────────────────────────


@_trace.traced(name=_FRAME_TILE)
def _tile_copy(g: Geom, T: Transform) -> tuple[Geom, Transform, int | None]:
    """One packed-tile copy — its own ``tile`` frame, so the flattened
    atoms' ``maker`` resolves the copy that placed them (``tile#k``)."""
    _record(atoms=_geom_atom_count(g))
    return g, T, _leaf()


@_trace.traced(name=_FRAME_TILE)
def tile(
    obj: Any,
    n: int,
    *,
    pitch: Any = None,
    period: Any = None,
    fused: bool = False,
) -> Geom:
    """``n`` copies of ``obj`` in a row — packed (``fused=False``, the
    ``array_block`` shape) or fused (one body, bonded across each
    boundary).

    ``pitch``/``period`` is the per-copy translation vector (native or
    unit string); ``pitch`` packs (copies at k·pitch), ``period`` fuses
    (copies at k·period with coincident atoms identified — a periodic
    sheet's guard-column overlap, so ``tile`` of a P-period unit at
    ``n·P`` equals the nP-period unit). ``obj`` may be a manifest
    template *name* for the symbolic packed form (``out.instance``
    writes the ``array`` spec; there is no geometry to fuse)."""
    n = int(n)
    if n < 2:
        raise BadInput(
            f"tile count must be ≥ 2, got {n}",
            next="an array of one is just an instance — use pga.instance",
        )
    scale = _scale_of(obj) if isinstance(obj, Geom) else "atomic"
    step_in = period if fused else pitch if pitch is not None else period
    if step_in is None:
        raise BadInput(
            "tile needs 'pitch' (packed) or 'period' (fused), got neither",
            next="tile(b, n, pitch=<vec>) packs; tile(b, n, period=<vec>, "
            "fused=True) fuses",
        )
    step = _native_vec(step_in, scale, "tile pitch" if not fused else "tile period")
    if not fused:
        spec = _linear_spec(n, step, scale)
        if isinstance(obj, str):
            return Geom(
                template=obj,
                array=spec,
                period=tuple(step),
                meta={"name": obj, "units": "A"},
            )
        g = geom(obj)
        placements = tuple(
            _tile_copy(g, Transform(R=np.eye(3), t=k * step)) for k in range(n)
        )
        return Geom(
            placements=placements,
            array=spec,
            period=tuple(step),
            meta=dict(g.meta),
        )
    # fused: eager merge, one frame, made = seam bonds
    if isinstance(obj, str):
        raise BadInput(
            "tile fused=True needs geometry, not a template name",
            next="pass a Geom — fused tile merges real atoms; only packed "
            "tile takes a manifest template name",
        )
    g = geom(obj)
    return _fuse_copies(g, n, step)


def _geom_atom_count(g: Geom) -> int:
    if g.scene is not None:
        return len(g.scene.atoms)
    return sum(_geom_atom_count(c) for c, _x, _f in g.placements)


def _fuse_copies(g: Geom, n: int, step: np.ndarray) -> Geom:
    """``n`` copies at ``k·step`` merged at coincident atoms — the k2
    fuse across each boundary. The merge identifies atoms closer than
    ``_MERGE_EPS_A`` (one representative: the earlier copy's), unions
    bonds and drops duplicates; the result is the continuous
    ``n·periods`` object when ``step`` is the unit's own period."""
    flat = _flatten_atomic(g)
    labels, elements, carts = _scene_carts(flat)
    bonds = _scene_bonds(flat, labels)
    base_maker = _leaf()

    order: list[int] = []  # source atom index per kept merged atom
    merged_pos: list[np.ndarray] = []
    remap: dict[tuple[int, int], int] = {}
    for k in range(n):
        for i in range(len(labels)):
            p = carts[i] + k * step
            hit = None
            for j in range(len(merged_pos) - 1, -1, -1):
                if float(np.linalg.norm(p - merged_pos[j])) < _MERGE_EPS_A:
                    hit = j
                    break
            if hit is None:
                hit = len(merged_pos)
                merged_pos.append(p)
                order.append(i)
            remap[(k, i)] = hit
    new_bonds: set[tuple[int, int, float]] = set()
    for k in range(n):
        for i, j, o in bonds:
            a, b = remap[(k, i)], remap[(k, j)]
            if a == b:
                continue
            new_bonds.add((min(a, b), max(a, b), o))
    atoms_el = [elements[i] for i in order]
    pos = np.asarray(merged_pos, dtype=np.float64)
    new_scene = _scene_from_parts(
        atoms_el,
        pos,
        sorted(new_bonds),
        maker=base_maker,
    )
    _record(atoms=len(atoms_el), bonds=len(new_bonds))
    return Geom(
        scene=new_scene,
        ports=dict(g.ports),
        meta=dict(g.meta),
        fused=True,
    )


# ── assembly / instance / place ──────────────────────────────────────────


def _flatten_atomic(g: Geom) -> Any:
    """Merge ``g`` into one Scene — placements expanded in order, atoms
    re-labelled ``a<El><n>``, ``maker`` from each placement's frame id
    (a nested assembly's atoms keep their own inner stamps)."""
    from precis.structure.scene import Atom, Bond, Scene

    labels_out: list[str] = []
    elements_out: list[str] = []
    carts_out: list[np.ndarray] = []
    bonds_out: list[tuple[int, int, float]] = []
    makers: list[int | None] = []
    counters: dict[str, int] = {}

    def emit(sub: Geom, xf: Transform, maker: int | None) -> None:
        if sub.scene is not None:
            labels, elements, carts = _scene_carts(sub.scene)
            idx_map = {la: k for k, la in enumerate(labels)}
            base = len(labels_out)
            for la in labels:
                elements_out.append(sub.scene.atoms[la].element)
                carts_out.append(
                    xf.apply(sub.scene.cell.frac_to_cart(sub.scene.atoms[la].frac))
                )
            for b in sub.scene.bonds:
                if b.i in idx_map and b.j in idx_map:
                    bonds_out.append(
                        (base + idx_map[b.i], base + idx_map[b.j], float(b.order))
                    )
            labels_out.extend(labels)
            makers.extend([maker] * len(labels))
        for child, cxf, fid in sub.placements:
            if sub.meta.get("_asm"):
                # a nested assembly's atoms keep the stamps of its own
                # subtree — frame:<sub key> still selects them
                emit(child, xf.compose(cxf), fid if fid is not None else maker)
            else:
                # an instance-shaped child is a recipe, not a boundary:
                # the enclosing placement claims its atoms
                emit(child, xf.compose(cxf), maker if maker is not None else fid)

    top = g.pose or Transform(np.eye(3), np.zeros(3))
    if g.scene is not None:
        emit(Geom(scene=g.scene), top, None)
    for child, cxf, fid in g.placements:
        emit(child, top.compose(cxf), fid)

    carts = np.asarray(carts_out, dtype=np.float64).reshape(-1, 3)
    cell = _bounds_cell(carts)
    inv = np.linalg.inv(np.asarray(cell.lattice, dtype=float))
    atoms: dict[str, Atom] = {}
    for i, (old_la, el) in enumerate(zip(labels_out, elements_out)):
        # keep the source label while it is unique — port ``atom``
        # bindings and frame: selectors keep their anchors; a repeated
        # template's later copies re-mint deterministically.
        la = old_la
        if la in atoms:
            counters[el] = counters.get(el, 0) + 1
            la = f"a{el}{counters[el]}"
            while la in atoms:
                counters[el] += 1
                la = f"a{el}{counters[el]}"
        atoms[la] = Atom(label=la, element=el, frac=carts[i] @ inv, maker=makers[i])
    # ordinal → new label, for bond remap
    new_labels = list(atoms)
    return Scene(
        cell=cell,
        atoms=atoms,
        bonds=[
            Bond(
                i=new_labels[a],
                j=new_labels[b],
                order=o,
                maker=makers[a] if makers[a] == makers[b] else None,
            )
            for a, b, o in bonds_out
        ],
    )


@_trace.traced(name=_FRAME_INSTANCE)
def _place_child(child: Geom, xf: Transform) -> tuple[Geom, Transform, int | None]:
    """One placement — its own ``instance`` frame under the enclosing
    ``assembly``. Atoms stamped at flatten; a nested assembly keeps its
    own inner stamps."""
    return child, xf, _leaf()


@_trace.traced(name=_FRAME_ASSEMBLY)
def assembly(
    name: str,
    *,
    children: Any = (),
    pose: Any = None,
    ports: Any = None,
) -> Geom:
    """A named sub-assembly: ``children`` are :class:`Geom` (or
    ``(geom, Transform)`` pairs) placed by one ``instance`` frame each
    under this call's ``assembly`` frame. ``pose`` places the assembly
    in its own parent frame."""
    placements: list[tuple[Geom, Transform, int | None]] = []
    for c in children:
        if isinstance(c, tuple):
            child, xf = c
            cg = geom(child)
            xf = (
                xf if xf is not None else (cg.pose or Transform(np.eye(3), np.zeros(3)))
            )
        else:
            cg = geom(c)
            xf = cg.pose or Transform(np.eye(3), np.zeros(3))
        placements.append(_place_child(cg, xf))
    ports_d: dict[str, Port] = {}
    for pname, p in (ports or {}).items():
        ports_d[pname] = p if isinstance(p, Port) else Port(name=pname, **dict(p))
    meta: dict[str, Any] = {"name": str(name), "_asm": True}
    _record(atoms=sum(_geom_atom_count(c) for c, _x, _f in placements))
    return Geom(
        placements=tuple(placements),
        ports=ports_d,
        pose=_pose_of(pose),
        meta=meta,
    )


def _pose_of(pose: Any) -> Transform | None:
    if pose is None:
        return None
    if isinstance(pose, Transform):
        return pose
    if isinstance(pose, (list, tuple)) and len(pose) == 6:
        return _vec.pose(_vec.as_vec3(pose[:3]), _vec.as_vec3(pose[3:]))
    raise BadInput(
        f"pose must be a Transform or [x, y, z, rx, ry, rz], got {pose!r}",
        next="pass a cad Transform or a 6-list [x, y, z, rx, ry, rz]",
    )


@_trace.traced(name=_FRAME_INSTANCE)
def instance(template: Any, *, pose: Any = None) -> Geom:
    """One placement of ``template`` — a Geom (one-child assembly) or a
    manifest template *name* (the symbolic form ``out.instance``
    writes)."""
    if isinstance(template, str):
        return Geom(template=template, pose=_pose_of(pose), meta={"units": "A"})
    g = geom(template)
    xf = _pose_of(pose) or Transform(np.eye(3), np.zeros(3))
    return Geom(placements=((g, xf, _leaf()),))


@_trace.traced(name=_FRAME_PLACE)
def place(child: Any, *, at: Any, align: Any) -> Geom:
    """``child`` posed so its ``align`` port lands on ``at`` — the
    port-to-port solve ``connect`` runs: port origins coincident, port
    directions anti-parallel (mated). ``at``/``align`` are
    ``geom.port('name')`` handles ``(owner, port)``.

    The ``at`` owner's own ``pose`` resolves the port into the shared
    frame the returned ``pose`` is written in — so a *posed* parent's
    dock still receives the child. Deeper nesting needs nothing extra:
    poses compose through the parent chain at flatten. Returns the
    child with ``pose`` set; geometry is unchanged (a placement, not a
    transform)."""
    cg = geom(child)
    at_owner, at_port = at if isinstance(at, tuple) else at.port(str(at))
    _cg, align_port = align if isinstance(align, tuple) else align.port(str(align))
    if at_port.pos is None or align_port.pos is None:
        raise BadInput(
            "place needs ports with positions — declare 'pos' (or an "
            "atom-bound port) on both",
            next="set the port's 'pos' (or bind it to an atom)",
        )
    target = _vec.as_vec3(at_port.pos)
    at_dir = at_port.direction
    if at_owner.pose is not None:
        target = at_owner.pose.apply(target)
        if at_dir is not None:
            at_dir = tuple(at_owner.pose.apply_dir(_vec.as_vec3(at_dir)))
    src = _vec.as_vec3(align_port.pos)
    R = np.eye(3)
    if at_dir is not None and align_port.direction is not None:
        want = -_vec.normalize(_vec.as_vec3(at_dir))
        have = _vec.normalize(_vec.as_vec3(align_port.direction))
        c = float(np.clip(have @ want, -1.0, 1.0))
        if c > -1.0 + 1e-9:
            v = np.cross(have, want)
            s = math.sqrt(max(1.0 - c * c, 0.0))
            if s < 1e-9:
                R = np.eye(3)
            else:
                ux = np.array(
                    [[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]],
                    dtype=np.float64,
                )
                R = np.eye(3) + ux + ux @ ux * ((1.0 - c) / (s * s))
        else:  # 180°: pick any perpendicular axis
            a = _vec.normalize(have)
            perp = np.cross(a, np.array([1.0, 0.0, 0.0]))
            if float(np.linalg.norm(perp)) < 1e-9:
                perp = np.cross(a, np.array([0.0, 1.0, 0.0]))
            R = _rot_axis(_vec.normalize(perp), math.pi)
    T = Transform(R=R, t=target - R @ src)
    return replace(cg, pose=T)


__all__ = [
    "Geom",
    "Port",
    "array_spec",
    "assembly",
    "geom",
    "instance",
    "mirror",
    "place",
    "rotate",
    "tile",
    "transform",
    "translate",
]
