"""``mol.sites`` — the site grammar over a :class:`Geom`.

A selector is tokens joined by ``&`` (all must hold):

==================  =====================================================
``open``            atoms below their valence (:func:`_valence.open_count`)
``face:out|in|axial``  which side of the surface the bond leaves on
``ring:<class>``    the ring class (``5-ring`` … ``8-ring``, ``open-edge``,
                    ``non-ring``, ``sp3``, ``substituted``, ``hetero``)
``element:<sym>``   the host element
``frame:<key>``     atoms made by that trace frame or its subtree
                    (``frame:qual=<qual>`` for every frame of that qual)
``every:k``         every k-th selected site, starting with the first
==================  =====================================================

The enumeration, ring taxonomy and face rule are
:mod:`precis.structure.sites` — the code behind ``view='sites'`` — run on
the Geom's flattened scene. ``face`` is read against an axis: the
Geom's ``meta['axis']`` or ``axis=``, else the principal axis the atoms
single out (a tube, or a sheet's normal); without one, ``face`` reads
``n/a`` and a ``face:`` token refuses.

A hexfold-Net-built Geom keeps its Net in ``meta['_net']``: its per-atom
``sp2``/``sp3`` (invisible in the all-single-bond scene) decides which
atoms are open and which way their free bond leaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.structure import sites as _core
from precis_geom import assembly as pga
from precis_geom import trace as _trace
from precis_geom.assembly import Geom
from precis_geom.mol import _valence

#: The tokens, as the refusals list them.
TOKENS = (
    "open",
    "face:out|in|axial",
    "ring:<class>",
    "element:<sym>",
    "frame:<key>",
    "every:<k>",
)
_FACES = ("out", "in", "axial")
#: ``ring:`` names beyond the view's ring classes: carbons the view lists
#: no row for, and every non-carbon.
_EXTRA_CLASSES = ("sp3", "substituted", "hetero")
RING_CLASSES = (*_core.SITE_CLASSES, *_EXTRA_CLASSES)
#: A principal axis is claimed only when one eigenvalue stands this far
#: (ratio) from the other two.
_AXIS_SEPARATION = 1.25


@dataclass(frozen=True)
class MolSite:
    """One site: the host ``atom`` (scene label), the unit ``direction``
    a new bond would leave along, its ``face`` against the axis, the
    ring ``ring_class``, the host ``element``, ``free`` more single bonds
    it can take (``0`` for an H-capped atom a group could still replace
    — see ``has_h``) and the probe-sphere ``exposure``."""

    atom: str
    ordinal: int
    element: str
    direction: tuple[float, float, float]
    face: str
    ring_class: str
    free: int
    has_h: bool
    exposure: float
    pos: tuple[float, float, float]

    def __repr__(self) -> str:
        return (
            f"MolSite({self.atom} {self.ring_class} face={self.face} "
            f"free={self.free}{' +H' if self.has_h else ''})"
        )


@dataclass
class _Selector:
    open: bool = False
    faces: tuple[str, ...] = ()
    rings: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()
    every: int = 1


def parse_selector(text: str) -> _Selector:
    """``'open & face:out & every:3'`` → a :class:`_Selector`. Unknown
    tokens and bad values refuse with the accepted list."""
    if not isinstance(text, str) or not text.strip():
        raise BadInput(
            "mol.sites: the selector is empty",
            options=list(TOKENS),
            next="e.g. 'open & face:out & every:3'",
        )
    sel = _Selector()
    faces: list[str] = []
    rings: list[str] = []
    els: list[str] = []
    frames: list[str] = []
    for raw in text.split("&"):
        tok = raw.strip()
        if not tok:
            raise BadInput(
                f"mol.sites: empty token in {text!r}",
                options=list(TOKENS),
                next="join tokens with '&', e.g. 'open & face:out'",
            )
        name, _, val = tok.partition(":")
        name, val = name.strip().lower(), val.strip()
        if name == "open" and not val:
            sel.open = True
        elif name == "face":
            if val.lower() not in _FACES:
                raise BadInput(
                    f"mol.sites: face must be one of {list(_FACES)}, got {val!r}",
                    options=list(_FACES),
                    next="face:out, face:in or face:axial",
                )
            faces.append(val.lower())
        elif name == "ring":
            if val not in RING_CLASSES:
                raise BadInput(
                    f"mol.sites: unknown ring class {val!r}",
                    options=list(RING_CLASSES),
                    next="e.g. ring:6-ring or ring:open-edge",
                )
            rings.append(val)
        elif name == "element":
            if not val.isalpha() or len(val) > 2:
                raise BadInput(
                    f"mol.sites: element wants a chemical symbol, got {val!r}",
                    next="e.g. element:C or element:N",
                )
            els.append(val.capitalize())
        elif name == "frame":
            if not val:
                raise BadInput(
                    "mol.sites: frame: needs a trace frame key",
                    next="frame:<key> from the run trace (or frame:qual=<qual>)",
                )
            frames.append(val)
        elif name == "every":
            try:
                k = int(val)
            except ValueError:
                k = 0
            if k < 1 or str(k) != val:
                raise BadInput(
                    f"mol.sites: every wants an integer >= 1, got {val!r}",
                    next="e.g. every:3",
                )
            sel.every = k
        else:
            raise BadInput(
                f"mol.sites: unknown token {tok!r}",
                options=list(TOKENS),
                next="join the accepted tokens with '&'",
            )
    sel.faces, sel.rings = tuple(faces), tuple(rings)
    sel.elements, sel.frames = tuple(els), tuple(frames)
    return sel


# ── what a Geom knows beyond its scene ───────────────────────────────────


def bake(g: Geom) -> Geom:
    """``g`` with its pose and placements applied: a plain scene Geom in
    the frame ``attach`` works in (its labels are stable from here on)."""
    if not g.atomic:
        raise BadInput(
            "mol: this Geom has no atomic geometry",
            next="mol works on atomic Geoms (mol.frag / mol.lib / pga.geom)",
        )
    if g.pose is None and not g.placements:
        return g
    ports = (
        pga._transform_ports(g.ports, g.pose) if g.pose is not None else dict(g.ports)
    )
    return Geom(scene=g.flatten(), ports=ports, meta=dict(g.meta), findings=g.findings)


def net_ref(g: Geom) -> Any:
    """The :class:`~precis_geom.assembly.NetRef` in ``g.meta`` while it
    still describes ``g``'s atoms (a sample of labels sits where the
    Net's coordinates put them), else ``None``."""
    ref = g.meta.get("_net")
    if ref is None or g.scene is None or g.pose is not None or g.placements:
        return None
    labels = list(ref.scene.atoms)
    step = max(1, len(labels) // 12)
    for k in range(0, len(labels), step):
        la = labels[k]
        a = g.scene.atoms.get(la)
        if a is None or a.element != ref.scene.atoms[la].element:
            return None
        if float(np.linalg.norm(_valence.cart(g.scene, la) - ref.xyz[k])) > 1e-6:
            return None
    return ref


def net_pristine(g: Geom) -> Any:
    """The NetRef when ``g`` is exactly the scene :func:`pga.geom`
    exported from the Net (nothing attached, moved or removed)."""
    ref = net_ref(g)
    if ref is not None and g.scene is ref.scene:
        return ref
    return None


def hyb_map(g: Geom) -> dict[str, str]:
    """label → ``sp``/``sp2``/``sp3`` for atoms whose scene carries no
    tag but whose Net does."""
    ref = net_ref(g)
    if ref is None:
        return {}
    labels = list(ref.scene.atoms)
    return {
        labels[a.ord]: a.hyb for a in ref.net.atoms if a.hyb in ("sp", "sp2", "sp3")
    }


def hyb_of(scene: Any, label: str, nets: dict[str, str]) -> str | None:
    """The hybridization to build with: the atom's own tag, the Net's,
    else ``None`` (the helpers infer from bond orders)."""
    return scene.atoms[label].hybridization or nets.get(label)


# ── the axis ─────────────────────────────────────────────────────────────


@dataclass
class _Axis:
    point_A: np.ndarray
    dir_A: np.ndarray
    note: str = ""


def _auto_axis(pts: np.ndarray) -> _Axis | None:
    """The principal direction that stands apart from the other two — a
    tube's axis (long or squat), a sheet's normal — or ``None`` for a
    blob."""
    if len(pts) < 4:
        return None
    c = pts.mean(axis=0)
    w, v = np.linalg.eigh(np.cov((pts - c).T))
    w = np.maximum(w, 1e-12)
    best, best_gap = -1, 0.0
    for i in range(3):
        gap = min(max(w[i], w[j]) / min(w[i], w[j]) for j in range(3) if j != i)
        if gap > best_gap:
            best, best_gap = i, gap
    if best_gap < _AXIS_SEPARATION:
        return None
    return _Axis(c, v[:, best] / float(np.linalg.norm(v[:, best])), "principal axis")


def _resolve_axis(g: Geom, axis: Any, pts: np.ndarray) -> _Axis | None:
    spec = axis if axis is not None else g.meta.get("axis")
    if spec is None or (isinstance(spec, str) and spec == "auto"):
        return _auto_axis(pts)
    if isinstance(spec, str):
        if spec == "none":
            return None
        raise BadInput(
            f"mol.sites: axis must be 'auto', 'none' or (point, direction), got {spec!r}",
            options=["auto", "none"],
            next="axis=((0, 0, 0), (0, 0, 1))",
        )
    try:
        point, direction = (np.asarray(x, dtype=float).reshape(3) for x in spec)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            "mol.sites: axis must be (point, direction), two 3-vectors",
            next="axis=((0, 0, 0), (0, 0, 1))",
        ) from exc
    n = float(np.linalg.norm(direction))
    if n < 1e-9:
        raise BadInput("mol.sites: the axis direction is zero")
    return _Axis(point, direction / n, "given")


# ── enumeration ──────────────────────────────────────────────────────────


@dataclass
class _Cloud:
    name: str
    labels: list[str]
    elements: list[str]
    pts_A: np.ndarray


def _tuple3(v: np.ndarray) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


def enumerate_all(
    g: Geom, *, axis: Any = None, exposure_A: float = 3.0
) -> tuple[list[MolSite], _Axis | None]:
    """Every site on ``g`` (``g`` baked): the view's carbon rows, one row
    per open atom (direction by the hybridization-aware rule), and rows
    for the open atoms the view lists none for (heteroatoms, sp3 and
    substituted carbons). Sorted by atom ordinal, then face."""
    from scipy.spatial import cKDTree

    scene = g.scene
    labels = list(scene.atoms)
    pts = np.asarray([_valence.cart(scene, la) for la in labels], dtype=float).reshape(
        -1, 3
    )
    cloud = _Cloud("geom", labels, [scene.atoms[la].element for la in labels], pts)
    nets = hyb_map(g)
    hybs = [hyb_of(scene, la, nets) for la in labels]
    ax = _resolve_axis(g, axis, pts)
    adj, unsat = _core.bond_graph(scene, labels, pts)
    classes = _core.ring_classes(cloud, adj, unsat, hybs)
    cadj = _core.c_adjacency(adj, cloud.elements)
    tree = cKDTree(pts)
    centroid = pts.mean(axis=0)

    def face_for(i: int, d: np.ndarray) -> str:
        if ax is None:
            return "n/a"
        rel = pts[i] - ax.point_A
        radial = rel - float(np.dot(rel, ax.dir_A)) * ax.dir_A
        face = _core.face_of(d, radial)
        if face == "axial" and len(cadj[i]) >= 3:
            return "out" if float(np.dot(d, pts[i] - centroid)) > 0 else "in"
        return face

    def row(i: int, d: np.ndarray, ring: str, free: int, has_h: bool) -> MolSite:
        return MolSite(
            atom=labels[i],
            ordinal=i,
            element=cloud.elements[i],
            direction=_tuple3(d),
            face=face_for(i, d),
            ring_class=ring,
            free=free,
            has_h=has_h,
            exposure=_core.exposure(pts[i], d, tree, exposure_A),
            pos=_tuple3(pts[i]),
        )

    tables = _valence.bond_tables(scene)
    free_of = [
        _valence.open_count(scene, la, hyb=hybs[i], tables=tables)
        for i, la in enumerate(labels)
    ]
    has_h = [any(cloud.elements[j] == "H" for j in adj[i]) for i in range(len(labels))]
    rows: list[MolSite] = []
    done: set[int] = set()
    # the view's carbon rows (ring class, both faces of a wall carbon) ...
    view_rows = _core.enumerate_sites(cloud, classes, adj, ax, exposure_A)
    for s in sorted(view_rows, key=lambda s: (s.ordinal, s.face)):
        i = s.ordinal
        if free_of[i] >= 1:
            if i not in done:  # ... one row per open atom, hybridization-aware
                d = _valence.free_direction(
                    scene, labels[i], prefer_h=False, hyb=hybs[i]
                )
                rows.append(row(i, d, s.ring_class, free_of[i], has_h[i]))
        else:
            rows.append(
                MolSite(
                    atom=s.label,
                    ordinal=i,
                    element=cloud.elements[i],
                    direction=_tuple3(s.normal),
                    face=s.face,
                    ring_class=s.ring_class,
                    free=0,
                    has_h=has_h[i],
                    exposure=s.exposure,
                    pos=_tuple3(pts[i]),
                )
            )
        done.add(i)
    # ... and open atoms the view has no row for
    for i, la in enumerate(labels):
        if i in done or free_of[i] < 1:
            continue
        ring = classes[i] or "hetero"
        d = _valence.free_direction(scene, la, prefer_h=False, hyb=hybs[i])
        rows.append(row(i, d, ring, free_of[i], has_h[i]))
    rows.sort(key=lambda r: (r.ordinal, r.face))
    return rows, ax


def _ring_match(wanted: str, ring_class: str) -> bool:
    if wanted == ring_class:
        return True
    if wanted in ("open-edge", "non-ring", "sp3", "substituted", "hetero"):
        return False
    if ring_class.endswith("-ring") and ring_class != "non-ring":
        sizes = {int(p) for p in ring_class[: -len("-ring")].split("+")}
        return int(wanted[: -len("-ring")]) in sizes
    return False


def _frame_ids(spec: str) -> set[int]:
    """Frame ids of the trace subtree(s) a ``frame:`` token names."""
    tr = _trace.current()
    if tr is None:
        raise BadInput(
            f"mol.sites: frame:{spec} reads the run's construction trace, and "
            "none is active",
            next="run inside out.run(...), or select by another token",
        )
    by_qual = spec.startswith("qual=")
    want = spec[len("qual=") :] if by_qual else spec
    ids: set[int] = set()
    seen: list[str] = []
    stack = [tr.root]
    hits = []
    while stack:
        f = stack.pop()
        if f.id and (f.qual == want if by_qual else f.key == want):
            hits.append(f)
        elif f.id and len(seen) < 20:
            seen.append(f.qual if by_qual else f.key)
        stack.extend(f.children)
    if not hits:
        raise BadInput(
            f"mol.sites: no trace frame {'with qual ' if by_qual else ''}{want!r}",
            options=sorted(set(seen)),
            next="use a frame key from the run trace, or frame:qual=<qual>",
        )
    for h in hits:
        sub = [h]
        while sub:
            f = sub.pop()
            ids.add(f.id)
            sub.extend(f.children)
    return ids


def select(
    rows: list[MolSite], sel: _Selector, g: Geom, ax: _Axis | None
) -> list[MolSite]:
    """``rows`` filtered by ``sel`` (``every`` last, over the survivors in
    ordinal order)."""
    if sel.faces and ax is None:
        raise BadInput(
            "mol.sites: face: needs an axis and this Geom has no principal one "
            "(its atoms are not tube- or sheet-like)",
            next="pass axis=((x, y, z), (dx, dy, dz)), or drop the face: token",
        )
    out = rows
    if sel.open:
        out = [r for r in out if r.free >= 1]
    if sel.faces:
        out = [r for r in out if r.face in sel.faces]
    if sel.rings:
        out = [r for r in out if any(_ring_match(w, r.ring_class) for w in sel.rings)]
    if sel.elements:
        present = sorted({r.element for r in rows})
        bad = [e for e in sel.elements if e not in present]
        if bad:
            raise BadInput(
                f"mol.sites: no {bad} among the sites' elements",
                options=present,
                next="pick an element present on this Geom",
            )
        out = [r for r in out if r.element in sel.elements]
    if sel.frames:
        ids: set[int] = set()
        for f in sel.frames:
            ids |= _frame_ids(f)
        scene = g.scene
        out = [r for r in out if scene.atoms[r.atom].maker in ids]
    if sel.every > 1:
        out = out[:: sel.every]
    return out


@_trace.traced(name="sites")
def sites(
    geom: Any,
    selector: str = "open",
    *,
    axis: Any = None,
    exposure_A: float = 3.0,
) -> list[MolSite]:
    """The sites of ``geom`` that ``selector`` picks (grammar above), in
    atom order. Each is a :class:`MolSite`: host ``atom``, free
    ``direction``, ``face``, ring ``ring_class``. A wall carbon with no
    open valence lists one row per face; an open atom one row."""
    if not (isinstance(exposure_A, (int, float)) and 0.5 <= exposure_A <= 20.0):
        raise BadInput(
            f"mol.sites: exposure_A must be in [0.5, 20] Å, got {exposure_A!r}",
            next="the probe radius, e.g. exposure_A=3.0",
        )
    sel = parse_selector(selector)
    g = bake(pga.geom(geom))
    rows, ax = enumerate_all(g, axis=axis, exposure_A=float(exposure_A))
    picked = select(rows, sel, g, ax)
    _trace.record(made={"sites": len(picked)})
    return picked
