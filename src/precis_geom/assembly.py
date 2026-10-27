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
spec's immutable-products ruling). Construction calls are ``@traced``;
atoms carry their creator or placement frame as ``maker``. Transforms
preserve those stamps and record the affected atoms in ``touched``:

- ``assembly`` mints an ``assembly`` frame plus one ``instance`` frame
  per direct child; a leaf child's atoms are stamped with its
  ``instance`` frame id, a nested assembly's atoms keep their own
  inner stamps — so ``frame:<key>`` on the assembly selects the whole
  subtree's atoms through the maker→frame join.
- ``tile(..., fused=False)`` mints one ``tile`` frame per copy
  (``tile#0..#n-1``) — packed, the ``array_block`` shape — or, from a
  bare template *name*, a symbolic array the manifest writes and the
  importer replays through the ``array_block`` op.
- ``tile(..., fused=True)`` mints one ``tile`` frame whose ``made`` counts
  the merged output; surviving atoms/bonds keep their source makers
  (the earlier copy wins coincident sites). Copies at the period step
  identify coincident atoms and union bonds/rings (the guard-column
  overlap of a periodic sheet, k2 across each boundary).
- ``translate``/``rotate``/``transform`` mint a ``pose`` frame,
  ``mirror`` a ``mirror`` frame (improper transform — atomic emits the
  ``chirality.flipped`` INFO and flips ``meta['chiral_index']``),
  ``place`` a ``place`` frame (the port-to-port solve ``connect``
  runs). ``junction`` (two rim atom sets by minted bonds) is a later
  spec row, not this slice.

- ``ray`` (:class:`Ray`; ``ray.along``/``ray.axis``) declares an
  observation ray. Rays are observation only — they never touch
  geometry; a ``ray`` frame is minted so the manifest row joins the
  trace.

Units: an atomic :class:`Geom` is Å-native (the ``precis.structure``
enclave); a macro one rides the cad design's own unit. Vector
arguments accept a bare 3-vector (native) or a unit string the shared
``precis.utils.units`` grammar resolves (``"[0 0 1.2] nm"``).
"""

from __future__ import annotations

import math
import sys
import types
import warnings
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from precis.cad import vec as _vec
from precis.cad.vec import Transform
from precis.errors import BadInput
from precis.utils.units import parse_quantity, parse_vector_quantity
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


def _mint_labels(elements: list[str]) -> list[str]:
    """The ``a<El><n>`` atom labels minted in input order — factored out
    of :func:`_scene_from_parts` so a Net census can speak in the
    exported label space (gr514059)."""
    counters: dict[str, int] = {}
    labels: list[str] = []
    for el in elements:
        counters[el] = counters.get(el, 0) + 1
        labels.append(f"a{el}{counters[el]}")
    return labels


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
    labels = _mint_labels(list(elements))
    atoms: dict[str, Atom] = {
        la: Atom(label=la, element=el, frac=fracs[i], maker=maker)
        for i, (la, el) in enumerate(zip(labels, elements))
    }
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


def _is_net(obj: Any) -> bool:
    """A hexfold ``Net`` — the only input ``geom``'s ``coords=`` can
    re-seat (its exported geometry lives in the replaceable ``seed3``;
    a ``Block``/``Strip``/``Scene``'s ``coords`` is already final)."""
    return hasattr(obj, "atoms") and hasattr(obj, "seed3")


def _net_census(net: Any, ordered: list[Any], elements: list[str]) -> dict[str, Any]:
    """A hexfold ``Net``'s structural census in the exported ``a<El><n>``
    label space (gr514059): junction/seam/port atom sets and a
    degree/valence summary — the numbers a script otherwise recomputes
    by hand joining the extxyz labels to bonds.json. Valence semantics
    mirror ``hexfold.check._findings_from_net``: only lattice elements
    carry a lattice valence (termination atoms like H are skipped),
    ``over`` is degree > 4 for sp3 else > 3, ``open`` is degree < 3 off
    a rim, ``stubs`` is degree ≤ 1 (rim or not)."""
    labels = _mint_labels(list(elements))
    la = {a.ord: labels[k] for k, a in enumerate(ordered)}
    deg = {a.ord: 0 for a in ordered}
    for i, j, _o in net.bonds:
        deg[i] += 1
        deg[j] += 1
    rim_ords = (
        {o for _, p in net.ports for o in p.atoms}
        | {o for rim, _b, _be in net.term_rims for o in rim}
        | {o for rim, _b, _be in net.seam_rims for o in rim}
    )
    lat_el = set(getattr(net.lattice, "elements", ()) or ())
    hist: dict[str, int] = {}
    stubs: list[str] = []
    open_: list[str] = []
    over: list[str] = []
    for a in ordered:
        if a.element not in lat_el:
            continue
        d = deg[a.ord]
        hist[str(d)] = hist.get(str(d), 0) + 1
        if d > (4 if a.hyb == "sp3" else 3):
            over.append(la[a.ord])
        elif d <= 1:
            stubs.append(la[a.ord])
        if d < 3 and a.ord not in rim_ords:
            open_.append(la[a.ord])
    return {
        "atoms": len(ordered),
        "bonds": len(net.bonds),
        "junctions": [
            {
                "name": j.name,
                "atoms": [la[o] for o in j.atoms],
                "fused": [[la[i_], la[j_]] for i_, j_ in j.fused],
                "faces": [[la[o] for o in f] for f in j.faces],
            }
            for j in net.junctions
        ],
        "seams": [
            {
                "name": s.name,
                "k": s.k,
                "rims": list(s.rims),
                "atoms": [la[o] for o in s.atoms],
            }
            for s in net.seams
        ],
        "ports": {name: [la[o] for o in p.atoms] for name, p in net.ports},
        "rim": [la[o] for o in sorted(rim_ords)],
        "valence": {
            "histogram": hist,
            "stubs": stubs,
            "open": open_,
            "over": over,
        },
    }


def _caller_stacklevel() -> int:
    """The ``warnings.warn`` level that lands on the caller outside the
    wrapper chain — ``translate``/``mirror``/``tile``/``place`` reach
    ``geom()`` internally (some through a ``@traced`` wrapper, whose
    frame is ``precis_geom.trace`` plumbing), so a fixed ``stacklevel``
    attributes the warning to a library line (gr514206)."""
    level = 1
    f: types.FrameType | None = sys._getframe(1)
    while f is not None and f.f_globals.get("__name__") in {
        __name__,
        _trace.__name__,
    }:
        level += 1
        f = f.f_back
    return level


def geom(
    obj: Any,
    *,
    ports: Any = None,
    meta: dict[str, Any] | None = None,
    coords: Any = None,
) -> Geom:
    """Wrap a built object — a ``Scene``, a hexfold ``Net`` / ``Block`` /
    ``Strip``, or a cad ``Design`` — as a :class:`Geom`. ``ports`` is a
    mapping name → :class:`Port` or kwargs dict (``pos``/``direction``/
    ``roles``/``atom``/``annotations``); ``meta`` rides through (e.g.
    ``{'chiral_index': [n, m]}``, flipped by :func:`mirror`).

    A ``Net`` exports ``net.seed3`` — the *unrelaxed* seed (gr514053).
    ``coords`` overrides it with an ``(n, 3)`` array in ``Net.atoms``
    ordinal order, e.g. ``pga.geom(net, coords=stick(net))``; ``meta``
    records ``coords="relaxed"`` or ``"seed"`` either way, and carries
    ``census`` — the junction/seam/port/valence summary ``out.block``
    stamps on the manifest row (gr514059)."""
    ports_d: dict[str, Port] = {}
    for name, p in (ports or {}).items():
        if isinstance(p, Port):
            ports_d[name] = p
        else:
            ports_d[name] = Port(name=name, **{k: v for k, v in dict(p).items()})
    if coords is not None and not _is_net(obj):
        raise BadInput(
            "pga.geom: coords= applies only to a hexfold Net — a "
            "Scene/Block/Strip already carries its own coordinates",
            next="drop coords=, or relax the Net first: coords=stick(net)",
        )
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
    xyz = getattr(block, "coords", None)
    bonds = getattr(block, "bonds", None)
    coord_kind = "seed"
    census: dict[str, Any] | None = None
    if elements is None:
        atoms = getattr(obj, "atoms", None)
        if atoms is not None and hasattr(obj, "seed3"):  # a Net
            ordered = sorted(atoms, key=lambda a: a.ord)
            elements = [a.element for a in ordered]
            bonds = list(getattr(obj, "bonds", ()))
            census = _net_census(obj, ordered, elements)
            if coords is not None:
                try:
                    xyz = np.asarray(coords, dtype=np.float64)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"pga.geom: coords must be an ({len(ordered)},3) "
                        "array in Net.atoms ord order — stick(net) returns "
                        "exactly that array"
                    ) from exc
                if xyz.shape != (len(ordered), 3):
                    raise ValueError(
                        f"pga.geom: coords must be an ({len(ordered)},3) "
                        "array in Net.atoms ord order, got "
                        f"{tuple(xyz.shape)} — stick(net) returns exactly "
                        "that array"
                    )
                coord_kind = "relaxed"
            else:
                if obj.seed3 is None:
                    raise BadInput(
                        "pga.geom: this Net has no seed3 coordinates to export",
                        next="pass them explicitly: pga.geom(net, coords=stick(net))",
                    )
                seed_kind = getattr(obj, "seed_kind", "spectral")
                coord_kind = "relaxed" if seed_kind == "relaxed" else "seed"
                if seed_kind != "relaxed":
                    warnings.warn(
                        "pga.geom: exporting the Net's unrelaxed seed3 "
                        f"coordinates (seed_kind={seed_kind!r}) — "
                        "relax first: pga.geom(net, coords=stick(net))",
                        UserWarning,
                        stacklevel=_caller_stacklevel(),
                    )
                xyz = np.asarray(obj.seed3, dtype=np.float64)
    if elements is not None and xyz is not None:
        scene = _scene_from_parts(
            list(elements),
            np.asarray(xyz, dtype=np.float64),
            # (i, j, order) or (i, j, order, kind) — GeneratedBlock and
            # hexfold Block/Net bond rows alike
            [(int(b_[0]), int(b_[1]), float(b_[2])) for b_ in (bonds or ())],
        )
        m = {"units": "A", "coords": coord_kind, **dict(meta or {})}
        if census is not None:
            m["census"] = census
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


def _transform_scene(scene: Any, T: Transform) -> Any:
    """A Scene under ``T`` — atoms remapped with source makers intact, cell
    and bonds carried (orthonormal ``R`` preserves cartesian
    distances)."""
    from precis.structure.scene import Atom, Scene

    atoms: dict[str, Atom] = {}
    for la, a in scene.atoms.items():
        cart = T.apply(scene.cell.frac_to_cart(a.frac))
        atoms[la] = replace(a, frac=scene.cell.cart_to_frac(cart))
    return Scene(
        cell=scene.cell,
        atoms=atoms,
        bonds=[replace(b) for b in scene.bonds],
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


def _apply_T(g: Geom, T: Transform) -> Geom:
    """``T`` applied to ``g`` as a whole object — the invariant is
    ``result.flatten() == T(g.flatten())``: a **posed** object is
    repositioned in its parent frame (``pose = T ∘ g.pose``, local
    geometry untouched); an unposed object has ``T`` baked into its
    geometry eagerly (atoms, placements, ports, array axis). The two
    paths agree because ``pose=None`` is the identity placement."""
    _trace.record(touched={"atoms": _geom_atom_count(g)})
    if g.pose is not None:
        return replace(g, pose=T.compose(g.pose))
    scene = _transform_scene(g.scene, T) if g.scene is not None else None
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
    return _apply_T(geom(obj), T)


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
    return replace(_apply_T(g, T), meta=meta, findings=tuple(findings))


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
    # fused: eager merge, one frame counts the merged output
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
    source_bond_makers = [
        b.maker for b in flat.bonds if b.i in flat.atoms and b.j in flat.atoms
    ]

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
    new_bonds: dict[tuple[int, int, float], int | None] = {}
    for k in range(n):
        for (i, j, o), maker in zip(bonds, source_bond_makers):
            a, b = remap[(k, i)], remap[(k, j)]
            if a == b:
                continue
            new_bonds.setdefault((min(a, b), max(a, b), o), maker)
    atoms_el = [elements[i] for i in order]
    pos = np.asarray(merged_pos, dtype=np.float64)
    new_scene = _scene_from_parts(
        atoms_el,
        pos,
        sorted(new_bonds),
    )
    for atom, source in zip(new_scene.atoms.values(), order):
        atom.maker = flat.atoms[labels[source]].maker
    for bond, key in zip(new_scene.bonds, sorted(new_bonds)):
        bond.maker = new_bonds[key]
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
    (a nested assembly's atoms keep their own inner stamps). Without
    a placement, preserve each source atom and bond's creator stamp."""
    from precis.structure.scene import Atom, Bond, Scene

    labels_out: list[str] = []
    elements_out: list[str] = []
    carts_out: list[np.ndarray] = []
    bonds_out: list[tuple[int, int, float]] = []
    bond_makers: list[int | None] = []
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
                    bond_makers.append(maker if maker is not None else b.maker)
            labels_out.extend(labels)
            makers.extend(
                maker if maker is not None else sub.scene.atoms[la].maker
                for la in labels
            )
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
                maker=maker,
            )
            for (a, b, o), maker in zip(bonds_out, bond_makers)
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


# ── observation rays ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ray:
    """An observation ray — a line the eye samples (structure-workspaces
    §Observation rays). SI metres; ``frame_in`` is the reference frame
    (``None`` = the block, ``"world"``, ``"port:<name>"``); ``frame_id``
    is the trace frame of the call that made it. Observation only: a
    ray never changes geometry."""

    name: str
    block: str | None
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]
    length: float
    kind: str = "probe"
    fields: tuple[str, ...] | None = None
    step: float | None = None
    reach: float | None = None
    frame_in: str | None = None
    anchor: tuple[str, ...] | None = None
    frame_id: int | None = None
    #: ``(radius m, count)`` — a ray bundle: ``count`` parallel rays on a
    #: circle of ``radius`` around this one, one probe (``tool_access``).
    bundle: tuple[float, int] | None = None


def _ray_origin_m(value: Any) -> tuple[float, float, float]:
    """Bare 3 numbers = metres; strings / quantity lists via the shared
    SI grammar."""
    if isinstance(value, str) or (
        isinstance(value, (list, tuple)) and any(isinstance(c, str) for c in value)
    ):
        si = parse_vector_quantity(value, "length", arg_name="ray origin")
        return (float(si[0]), float(si[1]), float(si[2]))
    v = _dir_vec(value, "ray origin (metres or a unit string)")
    return (float(v[0]), float(v[1]), float(v[2]))


def _ray_length_m(value: Any) -> float:
    if isinstance(value, str):
        n = float(parse_quantity(value, "length", arg_name="ray length"))
    else:
        try:
            n = float(value)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"ray length must be a number (metres) or a unit string, got {value!r}",
                next="give a positive length, e.g. '12 nm' or 1.2e-8 (metres)",
            ) from exc
    if not math.isfinite(n) or n <= 0.0:
        raise BadInput(
            f"ray length must be > 0, got {value!r}",
            next="give a positive length, e.g. '12 nm' or 1.2e-8 (metres)",
        )
    return n


def _make_ray(
    name: str,
    origin: Any,
    direction: Any,
    length: Any,
    *,
    block: str | None,
    fields: Any,
    step: float | None,
    reach: float | None,
    frame_in: str | None,
    kind: str,
    anchor: tuple[str, ...] | None = None,
) -> Ray:
    d = _dir_vec(direction, "ray direction")
    n = math.sqrt(sum(float(x) * float(x) for x in d))
    if n == 0.0 or not math.isfinite(n):
        raise BadInput(
            "ray direction must be a nonzero vector",
            next="give a direction like [0, 0, 1]",
        )
    return Ray(
        name=str(name),
        block=block,
        origin=_ray_origin_m(origin),
        direction=(float(d[0]) / n, float(d[1]) / n, float(d[2]) / n),
        length=_ray_length_m(length),
        kind=str(kind),
        fields=tuple(str(f) for f in fields) if fields is not None else None,
        step=float(step) if step is not None else None,
        reach=float(reach) if reach is not None else None,
        frame_in=frame_in,
        anchor=anchor,
        frame_id=_leaf(),
    )


@_trace.traced(name="ray")
def _ray(
    name: str,
    origin: Any,
    direction: Any,
    length: Any,
    *,
    block: str | None = None,
    fields: Any = None,
    step: float | None = None,
    reach: float | None = None,
    frame: str | None = None,
    kind: str = "probe",
) -> Ray:
    return _make_ray(
        name,
        origin,
        direction,
        length,
        block=block,
        fields=fields,
        step=step,
        reach=reach,
        frame_in=frame,
        kind=kind,
    )


@_trace.traced(name="ray.along")
def _ray_along(
    port: Any,
    length: Any,
    *,
    name: str,
    block: str | None = None,
    fields: Any = None,
    step: float | None = None,
    reach: float | None = None,
    kind: str = "bolt_axis",
) -> Ray:
    if isinstance(port, tuple):
        owner, p = port
        if block is None:
            block = owner.name
        pname = p.name
    elif isinstance(port, Port):
        pname = port.name
    else:
        pname = str(port)
    anchor = (f"{block}.{pname}",) if block else None
    return _make_ray(
        name,
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        length,
        block=block,
        fields=fields,
        step=step,
        reach=reach,
        frame_in=f"port:{pname}",
        kind=kind,
        anchor=anchor,
    )


@_trace.traced(name="ray.axis")
def _ray_axis(
    obj: Any,
    *,
    name: str,
    length: Any = None,
    reach: float | None = None,
    block: str | None = None,
) -> Ray:
    g = geom(obj)
    if not g.atomic:
        raise BadInput(
            "ray.axis needs atoms — axis() runs a PCA of atom positions; "
            "a macro/symbolic object has none",
            next="pass an atomic Geom, or give ray(...) an explicit direction",
        )
    _labels, _els, carts = _scene_carts(g.flatten())
    if len(carts) < 2:
        raise BadInput(
            "ray.axis needs at least 2 atoms to define an axis",
            next="pass a block with 2+ atoms, or give ray(...) an explicit direction",
        )
    c = carts.mean(axis=0)
    # principal axis = top eigenvector of the covariance
    w, vecs = np.linalg.eigh(np.cov((carts - c).T, bias=True))
    ax = vecs[:, int(np.argmax(w))]
    if ax[int(np.argmax(np.abs(ax)))] < 0:
        ax = -ax
    proj = (carts - c) @ ax
    extent = float(proj.max() - proj.min())
    if length is None:
        length_a = extent + 4.0
    elif isinstance(length, str):
        length_a = _ray_length_m(length) * _A_PER_M
    else:
        length_a = float(length) * _A_PER_M
    start = c - (extent / 2.0 + 2.0) * ax
    return _make_ray(
        name,
        tuple(float(x) * 1e-10 for x in start),
        ax,
        length_a * 1e-10,
        block=block or g.name,
        fields=None,
        step=None,
        reach=reach,
        frame_in=None,
        kind="probe",
    )


class _RayApi:
    """``pga.ray(...)`` plus ``pga.ray.along`` / ``pga.ray.axis``."""

    def __call__(
        self,
        name: str,
        origin: Any,
        direction: Any,
        length: Any,
        *,
        block: str | None = None,
        fields: Any = None,
        step: float | None = None,
        reach: float | None = None,
        frame: str | None = None,
        kind: str = "probe",
    ) -> Ray:
        """A ray from ``origin`` along ``direction`` for ``length``
        (bare numbers = metres; strings via the shared unit grammar).
        ``frame`` is the *reference* frame (``None``/``"world"``/
        ``"port:<name>"``)."""
        return _ray(
            name,
            origin,
            direction,
            length,
            block=block,
            fields=fields,
            step=step,
            reach=reach,
            frame=frame,
            kind=kind,
        )

    def along(
        self,
        port: Any,
        length: Any,
        *,
        name: str,
        block: str | None = None,
        fields: Any = None,
        step: float | None = None,
        reach: float | None = None,
        kind: str = "bolt_axis",
    ) -> Ray:
        """A ray down a port's +z axis, in the port's frame."""
        return _ray_along(
            port,
            length,
            name=name,
            block=block,
            fields=fields,
            step=step,
            reach=reach,
            kind=kind,
        )

    def axis(
        self,
        obj: Any,
        *,
        name: str,
        length: Any = None,
        reach: float | None = None,
        block: str | None = None,
    ) -> Ray:
        """A ray along an atomic object's principal axis (PCA), through
        the centroid, padded 2 Å beyond each end."""
        return _ray_axis(obj, name=name, length=length, reach=reach, block=block)

    def bundle(
        self,
        r: Ray,
        radius: Any,
        count: int = 8,
        *,
        name: str | None = None,
        kind: str | None = "tool_access",
    ) -> Ray:
        """``r`` as a bundle: ``count`` parallel rays on a circle of
        ``radius`` (metres or a unit string) — a wrench or socket needs a
        tube, not a line; the report is the union of their hits."""
        rad = _ray_length_m(radius)
        if not 3 <= int(count) <= 64:
            raise BadInput(
                f"ray.bundle count must be 3..64, got {count!r}",
                next="8 rays is the usual tube",
            )
        return replace(
            r,
            name=str(name) if name is not None else r.name,
            kind=str(kind) if kind is not None else r.kind,
            bundle=(rad, int(count)),
        )


ray = _RayApi()


# ── find_holes ───────────────────────────────────────────────────────────

#: ISO 273 clearance holes, mm: ``size → (fine, coarse)``. A cut from the
#: fine to the coarse diameter is that size's clearance hole; the ranges
#: do not overlap, so a diameter names at most one size (a 4.2 mm M5 tap
#: drill is below M4's fine 4.3 and reads as no clearance hole).
_ISO273_MM = {
    "M2": (2.2, 2.6),
    "M2.5": (2.7, 3.1),
    "M3": (3.2, 3.6),
    "M4": (4.3, 4.8),
    "M5": (5.3, 5.8),
    "M6": (6.4, 7.0),
    "M8": (8.4, 10.0),
    "M10": (10.5, 12.0),
    "M12": (13.0, 14.5),
    "M16": (17.0, 18.5),
    "M20": (21.0, 24.0),
}


def _metric_sizes() -> list[tuple[str, float, float]]:
    """``(key, nominal mm, pitch mm)`` for the ISO 4762 screw sizes."""
    from precis import component_series as cs

    series = cs.find_series("iso-4762")
    out = []
    for size in series.sizes if series is not None else ():
        d = size.specs.get("outer_diameter")
        pitch = size.specs.get("thread_pitch")
        if d is not None and pitch is not None:
            out.append((size.key, float(d), float(pitch)))
    return out


def _design_of(obj: Any) -> Any:
    from precis.cad.scene import SceneSpec, build_design, parse_source

    if isinstance(obj, Geom):
        obj = obj.design
    if isinstance(obj, str):
        obj = parse_source(obj)
    if isinstance(obj, SceneSpec):
        obj = build_design(obj)
    if obj is None or not hasattr(obj, "components"):
        raise BadInput(
            "find_holes needs a cad design (Design, SceneSpec, DSL text or a "
            "macro Geom)",
            next="pga.find_holes(build_design(spec))",
        )
    return obj


def find_holes(obj: Any, *, prefix: str = "h") -> list[Port]:
    """Propose fastener ports for the cylindrical cuts in a cad design —
    the reverse of placing ports by hand (structure-workspaces 2r.2). A cut
    at an ISO metric nominal diameter is a threaded hole (``thread:
    'M5x0.8'``, ``depth`` = its length); one within ISO 273's fine–coarse
    clearance range is that size's clearance hole (``clearance_for:
    'M5'``). Each port sits at the
    cut's base centre with ``direction`` its axis, design units; named
    ``h1``… in x, y, z order. Proposals only — nothing is added to the
    design; other cuts are not fasteners and are skipped."""
    from precis.cad.fold import Diff, Inter, Leaf, Union

    design = _design_of(obj)
    cutters: list[Any] = []

    def leaves(e: Any) -> None:
        """Every leaf of a cutter subtree — a pattern is a Union of cuts."""
        if isinstance(e, Leaf):
            cutters.append(design.instances[e.iid])
        elif isinstance(e, (Union, Inter)):
            for part in e.parts:
                leaves(part)
        elif isinstance(e, Diff):
            leaves(e.base)

    def walk(e: Any) -> None:
        if isinstance(e, Diff):
            walk(e.base)
            for c in e.cutters:
                leaves(c)
        elif isinstance(e, (Union, Inter)):
            for part in e.parts:
                walk(part)

    for expr in design.components.values():
        walk(expr)
    sizes = _metric_sizes()
    found: list[tuple[tuple[float, float, float], Port]] = []
    for inst in cutters:
        prim = inst.placed.prim
        rb, rt, h = (getattr(prim, k, None) for k in ("rb", "rt", "h"))
        if (
            rb is None
            or rt is None
            or h is None
            or abs(rb - rt) > 1e-9 * max(rb, 1e-12)
        ):
            continue
        d_mm = 2.0 * float(rb) * 1e3
        ann: dict[str, Any] | None = None
        exact = [(k, pitch) for k, nom, pitch in sizes if abs(d_mm - nom) <= 1e-6 * nom]
        clear = [
            k
            for k, (fine, coarse) in _ISO273_MM.items()
            if fine - 1e-9 <= d_mm <= coarse + 1e-9
        ]
        if exact:
            ann = {"thread": f"{exact[0][0]}x{exact[0][1]:g}", "depth": float(h)}
        elif clear:
            ann = {"clearance_for": clear[0], "diameter": 2.0 * float(rb)}
        if ann is None:
            continue
        xf = inst.placed.xform
        pos = np.asarray(xf.apply(_vec.as_vec3([0.0, 0.0, 0.0])), dtype=float)
        axis = np.asarray(xf.apply_dir(_vec.as_vec3([0.0, 0.0, 1.0])), dtype=float)
        axis = axis / np.linalg.norm(axis)
        key3 = (round(pos[0], 12), round(pos[1], 12), round(pos[2], 12))
        found.append(
            (
                key3,
                Port(
                    name="",
                    pos=(float(pos[0]), float(pos[1]), float(pos[2])),
                    direction=(float(axis[0]), float(axis[1]), float(axis[2])),
                    roles=("fastener",),
                    annotations={**ann, "feature": inst.label},
                ),
            )
        )
    found.sort(key=lambda t: t[0])
    return [
        replace(p_, name=f"{prefix}{i}") for i, (_k, p_) in enumerate(found, start=1)
    ]


__all__ = [
    "Geom",
    "Port",
    "Ray",
    "array_spec",
    "assembly",
    "find_holes",
    "geom",
    "instance",
    "mirror",
    "place",
    "ray",
    "rotate",
    "tile",
    "transform",
    "translate",
]
