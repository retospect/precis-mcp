"""Tile an authored fillet foot: sheet → fillet of radius ``R`` → an
``(n, 0)`` tube, built by hexfold and pinned to the authored surface
(docs/backlog/hexfold-ideal-surface-then-tile.md, S3).

The surface comes first and never moves: :func:`foot_meridian` writes it
from the author's radius (:func:`precis_surface.revolution.authored_meridian`),
and :func:`plan_foot` tiles it with a 3+3 foot — three heptagons where the
sheet meets a ``cap(6k,0)`` frustum, three where the frustum meets the
tube — relaxed by :func:`hexfold.stick.stick` under the opt-in normal
tether toward :func:`precis_surface.deviation.surface_foot`.

**k is chosen by measurement, not by formula.** The frustum width ``k``
is the only free integer. The S3 sweep (reviews/hexfold-toolkit.md, "S3
planner") found the narrowest frustum that builds wins on (6,0), (9,0),
(12,0) and (24,0) at every R tried, while (18,0) crumples there; so each
candidate ``k`` from :func:`k_min` upward is built, relaxed and judged,
and the plan keeps the one that meets every bar with the smallest
ring-ideal angle max. When none meets the bars the plan returns the
smallest bar excess with ``meets=False`` and the failing columns named —
the radius is never snapped (the backlog's "NOT in scope").

**The deviation column is not independent evidence** (orchestrator, S3
verdict): the tether and the judge target the same surface, so a stiff
tether makes it small by construction. Bonds, ring-ideal angles and
pyramidalisation are the columns the tether does not act on; every
:class:`FootRow` carries them.

**Scenes (S4).** :func:`plan_scene` puts several authored feet on one
sheet and relaxes them in one tethered pass, each feature judged in its
own fillet zone with the same :class:`FootRow` columns, the whole scene by
hexfold's geometry findings on the tethered coordinates
(:class:`hexfold.check.Relaxed`). Tops (a flat lid, a C60) are not
authored surfaces yet; their joints are reported in ``ScenePlan.tops``,
not barred. A hole cell whose sheet seam is not the planned three
heptagons is refused (a hexfold fuse-phase fault, gr464341).

This module sits in ``precis_se`` because it is the layer that already
imports both hexfold and precis_surface; hexfold itself never imports
precis_surface (the tether is a callable).
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from hexfold.build import build
from hexfold.check import Relaxed, geometry_findings
from hexfold.lattice import tube_radius
from hexfold.stick import stick_info
from precis_surface import revolution as rv
from precis_surface.deviation import Feature, surface_distance, surface_foot

# S3 bars (backlog acceptance): fillet-zone deviation and bond window, Å.
MEAN_MAX_A = 0.10
DEV_MAX_A = 0.3
BOND_MIN_A = 1.36
BOND_MAX_A = 1.50

_SHEET = 40  # sheet(40,40): wide enough that no tested foot reaches its edge
_FLAT_A = 20.0  # authored flat run outside the fillet
_WALL_A = 60.0  # authored tube wall, longer than the built tube
_TUBE_LEN = 8
_DS = 0.25  # judge sampling step on catenoid-free meridians is exact anyway
_PASSES = 40
_SETTLED_A = 0.002  # stop when the judge's mean moves less than this over 6 passes
_FLIP = np.array([1.0, 1.0, -1.0])  # the build grows features along -z


@dataclass(frozen=True)
class FootRow:
    """One candidate frustum, measured.  Lengths in Å, angles in degrees."""

    k: int
    k_tether: float
    fillet_mean: float
    fillet_p95: float
    fillet_max: float
    bond_min: float
    bond_max: float
    angle_rms: float  # against each corner's ring ideal (s-2)*180/s
    angle_max: float
    pyramid_max: float  # 360 - sum of the three angles; C60 is 12
    rim_r: float  # where the outer heptagon row landed
    passes: int
    misses: tuple[str, ...]  # failing bar columns; empty = meets every bar

    @property
    def meets(self) -> bool:
        return not self.misses

    @property
    def excess(self) -> float:
        """Summed overshoot past the bars, Å (0 when every bar is met)."""
        return (
            max(0.0, self.fillet_mean - MEAN_MAX_A)
            + max(0.0, self.fillet_max - DEV_MAX_A)
            + max(0.0, BOND_MIN_A - self.bond_min)
            + max(0.0, self.bond_max - BOND_MAX_A)
        )


@dataclass(frozen=True)
class FootPlan:
    n: int
    radius: float
    k: int
    text: str
    positions: np.ndarray  # relaxed, in the build's frame (features along -z)
    centre: tuple[float, float]
    rows: tuple[FootRow, ...]  # every candidate tried, in k order

    @property
    def chosen(self) -> FootRow:
        return next(r for r in self.rows if r.k == self.k)

    @property
    def meets(self) -> bool:
        return self.chosen.meets


def k_min(n: int) -> int:
    """Narrowest 3+3 frustum that builds onto an (n,0) tube: the smallest
    even k with k >= n/3 + 2 (a frustum two to three rows wide).  Measured
    for n = 6, 9, 12, 15, 18, 24; narrower k fails ``port.unknown``."""
    if n % 3 or n < 6:
        raise ValueError(
            f"a 3+3 foot needs an (n,0) tube with n a multiple of 3, n >= 6; got {n}"
        )
    k = n // 3 + 2
    return k + (k % 2)


def foot_text(n: int, k: int) -> str:
    """The 3+3 foot: a ``hex(k/2-1)`` hole in the sheet, fused to a
    ``cap(6k,0)`` frustum with three heptagons and a ``hex(n/3-1)`` hole,
    fused to the tube."""
    if k % 2 or k < k_min(n):
        raise ValueError(f"k must be even and >= {k_min(n)} for ({n},0); got {k}")
    r = n // 3 - 1
    c = _SHEET // 2
    return (
        "hexfold 0.2\norigin s\n"
        f"s: sheet({_SHEET},{_SHEET}) - hex({k // 2 - 1})@({c},{c},A):0\n"
        f"b: cap({6 * k},0) + 3@(-1,0,A):2 - hex({r})@(-1,0,A):0\n"
        f"t: tube({n},0, len={_TUBE_LEN})\n"
        "s.hole --fuse k=0--> b.in\n"
        "b.hole --fuse k=0--> t.in\n"
    )


def foot_meridian(n: int, radius: float) -> rv.Meridian:
    """The authored surface: flat sheet, a concave quarter circle of the
    author's radius, the (n,0) tube wall."""
    rt = tube_radius(n, 0)
    return rv.authored_meridian(
        rt + radius + _FLAT_A,
        [("line", _FLAT_A), ("arc", radius, -90.0), ("line", _WALL_A)],
    )


def angle_stats(
    pos: np.ndarray, bonds: Any, rings: Any, atoms: np.ndarray | None = None
) -> tuple[float, float, float]:
    """(rms, max) of |angle - ring ideal| over 3-coordinated corners, and
    the max pyramidalisation (360 - angle sum); ``atoms`` (a boolean mask)
    limits the corners to one feature's zone."""
    nb: dict[int, set[int]] = {}
    for i, j, *_ in bonds:
        nb.setdefault(i, set()).add(j)
        nb.setdefault(j, set()).add(i)
    ideal: dict[tuple[int, frozenset[int]], float] = {}
    for rg in rings:
        s = len(rg)
        for t in range(s):
            ideal[(rg[t], frozenset((rg[t - 1], rg[(t + 1) % s])))] = (
                (s - 2) * 180.0 / s
            )
    dev: list[float] = []
    pyr: list[float] = [0.0]
    for a, ns_set in nb.items():
        if len(ns_set) != 3 or (atoms is not None and not atoms[a]):
            continue
        ns = sorted(ns_set)
        u = {b: (pos[b] - pos[a]) / np.linalg.norm(pos[b] - pos[a]) for b in ns}
        total = 0.0
        for p, q in ((0, 1), (0, 2), (1, 2)):
            ang = float(np.degrees(np.arccos(np.clip(u[ns[p]] @ u[ns[q]], -1.0, 1.0))))
            total += ang
            ide = ideal.get((a, frozenset((ns[p], ns[q]))))
            if ide is not None:
                dev.append(abs(ang - ide))
        pyr.append(360.0 - total)
    d = np.asarray(dev) if dev else np.zeros(1)
    return float(np.sqrt(np.mean(d**2))), float(d.max()), float(max(pyr))


def _tethered_relax(
    net: Any,
    tether: Any,
    k_tether: float,
    judge: Callable[[np.ndarray], float],
) -> tuple[np.ndarray, float, int]:
    """Stick passes under the tether, each seeded from the last, until the
    judge's mean moves less than ``_SETTLED_A`` over 6 passes: (positions,
    final max force, passes)."""
    cur = net
    hist: list[float] = []
    pos = np.asarray(net.seed3, dtype=float)
    force = 0.0
    for _ in range(_PASSES):
        raw, force = stick_info(cur, tether=tether, k_tether=k_tether)
        pos = np.asarray(raw, dtype=float)
        cur = dataclasses.replace(net, seed3=tuple(map(tuple, pos)))
        hist.append(judge(pos))
        if len(hist) > 6 and abs(hist[-1] - hist[-7]) < _SETTLED_A:
            break
    return pos, force, len(hist)


def _measure(
    n: int, k: int, radius: float, k_tether: float
) -> tuple[FootRow, np.ndarray, tuple[float, float], str]:
    text = foot_text(n, k)
    net = build(text, strict=False)
    errs = sorted({f.code for f in net.report.errors()})
    if errs:
        raise ValueError(f"({n},0) k={k}: build errors {errs}")
    seed = np.asarray(net.seed3, dtype=float)
    inst = np.array([a.instance for a in net.atoms])
    tub = seed[inst == "t"]
    centre = (float(tub[:, 0].mean()), float(tub[:, 1].mean()))
    feat = [Feature("foot", centre, foot_meridian(n, radius))]

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot, nrm = surface_foot(pos * _FLIP, feat, ds=_DS)
        return foot * _FLIP, nrm * _FLIP

    pos, _force, passes = _tethered_relax(
        net,
        tether,
        k_tether,
        lambda q: float(surface_distance(q * _FLIP, feat, ds=_DS)[0].mean()),
    )
    d, _ = surface_distance(pos * _FLIP, feat, ds=_DS)
    b = np.array([(i, j) for i, j, *_ in net.bonds])
    bl = np.linalg.norm(pos[b[:, 0]] - pos[b[:, 1]], axis=1)
    rt = tube_radius(n, 0)
    rad = np.hypot(pos[:, 0] - centre[0], pos[:, 1] - centre[1])
    dz = d[(rad <= rt + radius + 1.0) & (-pos[:, 2] <= radius + 1.0)]
    # sheet atoms bonded to the frustum: the outer heptagon row's radius
    rim = sorted(
        {
            a
            for i, j, *_ in net.bonds
            for a, o in ((i, j), (j, i))
            if inst[a] == "s" and inst[o] == "b"
        }
    )
    arms, amax, pmax = angle_stats(pos, net.bonds, net.rings)
    misses = tuple(
        name
        for name, bad in (
            ("fillet mean", dz.mean() > MEAN_MAX_A),
            ("fillet max", dz.max() > DEV_MAX_A),
            ("bond min", bl.min() < BOND_MIN_A),
            ("bond max", bl.max() > BOND_MAX_A),
        )
        if bad
    )
    row = FootRow(
        k=k,
        k_tether=k_tether,
        fillet_mean=float(dz.mean()),
        fillet_p95=float(np.percentile(dz, 95)),
        fillet_max=float(dz.max()),
        bond_min=float(bl.min()),
        bond_max=float(bl.max()),
        angle_rms=arms,
        angle_max=amax,
        pyramid_max=pmax,
        rim_r=float(rad[rim].mean()),
        passes=passes,
        misses=misses,
    )
    return row, pos, centre, text


def plan_foot(
    n: int,
    radius: float,
    *,
    k_tether: float = 1.0,
    candidates: tuple[int, ...] | None = None,
) -> FootPlan:
    """Tile the authored foot (sheet → fillet ``radius`` → (n,0) tube).

    Tries ``candidates`` (default: even k from :func:`k_min` to k_min + 6),
    keeps the candidate that meets every bar with the smallest ring-ideal
    angle max, else the smallest bar excess (``meets`` is then False)."""
    if radius <= 0.0:
        raise ValueError(f"fillet radius must be positive; got {radius}")
    ks = (
        candidates
        if candidates is not None
        else tuple(range(k_min(n), k_min(n) + 7, 2))
    )
    measured = [_measure(n, k, radius, k_tether) for k in ks]
    rows = tuple(m[0] for m in measured)
    passing = [m for m in measured if m[0].meets]
    best = (
        min(passing, key=lambda m: m[0].angle_max)
        if passing
        else min(measured, key=lambda m: m[0].excess)
    )
    row, pos, centre, text = best
    return FootPlan(
        n=n, radius=radius, k=row.k, text=text, positions=pos, centre=centre, rows=rows
    )


# --- scenes: several authored feet on one sheet (S4) ----------------------

_SCENE_FLAT_A = 1.5  # a scene feature's own flat run; the sheet beyond is |z|
_BALL_FREE_BONDS = 4  # tube atoms this close (bonds) to a ball top relax free
_TOPS = ("open", "lid", "ball")


@dataclass(frozen=True)
class SceneFeature:
    """One authored foot on the scene sheet: sheet → fillet ``radius`` →
    an ``(n, 0)`` tube of ``tube_len`` periods → ``top``.

    ``name`` is the tube's hexfold instance; the frustum is ``<name>f`` and
    the top ``<name>c``.  ``at`` is the sheet cell of the hole centre.
    ``top``: ``open``, ``lid`` (a flat ``cap(n,0)``) or ``ball`` (C60 minus
    a hexagon, fused k=3; (6,0) only).  Tops are not authored surfaces in
    S4: the tether does not act on them, nor on the last few tube bonds
    below a ball, and their joint is reported in ``ScenePlan.tops``."""

    name: str
    at: tuple[int, int]
    n: int
    radius: float
    tube_len: int
    top: str = "open"


@dataclass(frozen=True)
class ScenePlan:
    text: str
    ks: dict[str, int]
    positions: np.ndarray  # relaxed, in the build's frame (features along -z)
    instances: tuple[str, ...]  # per atom
    rows: dict[str, FootRow]  # per feature, judged in its own fillet zone
    # per lid/ball feature: (ring-ideal angle max, pyramidalisation max) over
    # the top and its joint -- reported, not barred: tops are not authored
    # in S4, and a free (6,0) + C60 neck is ~50-60 degrees on its own
    tops: dict[str, tuple[float, float]]
    findings: tuple[Any, ...]  # hexfold geometry findings on ``positions``
    passes: int

    @property
    def errors(self) -> tuple[str, ...]:
        """ERROR finding codes (``geom.clash`` overlap, ``geom.seed_overlap``)."""
        return tuple(
            sorted({f.code for f in self.findings if f.severity.name == "ERROR"})
        )

    @property
    def meets(self) -> bool:
        return not self.errors and all(r.meets for r in self.rows.values())


def scene_text(
    sheet: tuple[int, int],
    features: tuple[SceneFeature, ...],
    ks: dict[str, int],
    extra: str = "",
) -> str:
    """One sheet with one ``hex(k/2-1)`` hole per feature, each fused to its
    3+3 frustum, tube and top (:func:`foot_text`'s lines, per feature).
    ``extra`` is appended verbatim: buds and their attachments."""
    holes: list[str] = []
    body: list[str] = []
    fuses: list[str] = []
    for idx, f in enumerate(features):
        if f.top not in _TOPS:
            raise ValueError(f"{f.name}: top must be one of {_TOPS}; got {f.top!r}")
        if f.top == "ball" and f.n != 6:
            raise ValueError(f"{f.name}: a ball top fuses onto (6,0) only; got n={f.n}")
        if f.top == "lid" and f.n % 6:
            raise ValueError(f"{f.name}: a flat lid needs n a multiple of 6; got {f.n}")
        k = ks[f.name]
        foot_text(f.n, k)  # refuses a k too narrow to build
        holes.append(f" - hex({k // 2 - 1})@({f.at[0]},{f.at[1]},A):0")
        b, c = f"{f.name}f", f"{f.name}c"
        body.append(
            f"{b}: cap({6 * k},0) + 3@(-1,0,A):2 - hex({f.n // 3 - 1})@(-1,0,A):0"
        )
        body.append(f"{f.name}: tube({f.n},0, len={f.tube_len})")
        hole = "hole" if idx == 0 else f"hole{idx}"
        fuses.append(f"s.{hole} --fuse k=0--> {b}.in")
        fuses.append(f"{b}.hole --fuse k=0--> {f.name}.in")
        if f.top == "lid":
            body.append(f"{c}: cap({f.n},0)")
            fuses.append(f"{f.name}.out --fuse k=0--> {c}.in")
        elif f.top == "ball":
            body.append(f"{c}: fullerene(C60) - hexagon@(0,0,A)")
            fuses.append(f"{f.name}.out --fuse k=3--> {c}.hole")
    lines = [
        "hexfold 0.2",
        "origin s",
        f"s: sheet({sheet[0]},{sheet[1]})" + "".join(holes),
        *body,
        *fuses,
    ]
    tail = extra.strip()
    return "\n".join(lines) + "\n" + (tail + "\n" if tail else "")


def _hops_from(bonds: Any, start: np.ndarray) -> np.ndarray:
    """Bond-graph distance of every atom from the ``start`` mask (0 on it,
    a large number where unreachable)."""
    n = len(start)
    nb: list[list[int]] = [[] for _ in range(n)]
    for i, j, *_ in bonds:
        nb[i].append(j)
        nb[j].append(i)
    hops = np.full(n, n + 1, dtype=np.int64)
    front = [int(a) for a in np.flatnonzero(start)]
    hops[front] = 0
    while front:
        nxt = []
        for a in front:
            for b in nb[a]:
                if hops[b] > hops[a] + 1:
                    hops[b] = hops[a] + 1
                    nxt.append(b)
        front = nxt
    return hops


def _check_seams(
    rings: Any, inst: np.ndarray, features: tuple[SceneFeature, ...]
) -> None:
    """Each sheet→frustum seam must be the 3+3 foot's three heptagons.  At
    some hole cells the fuse mints three extra 5-7 pairs there instead (a
    hexfold seam-phase fault, cause under investigation); a scene built
    on one would judge a different defect census than the one planned, so
    it is refused with the cell named rather than relaxed."""
    for f in features:
        seam = {"s", f"{f.name}f"}
        census = sorted(
            len(r) for r in rings if len(r) != 6 and {inst[a] for a in r} == seam
        )
        if census != [7, 7, 7]:
            raise ValueError(
                f"{f.name}: the sheet seam at cell {f.at} has rings {census}, not "
                "[7, 7, 7]; move the hole one cell (seam-phase fault)"
            )


def _relax_scene(
    sheet: tuple[int, int],
    features: tuple[SceneFeature, ...],
    ks: dict[str, int],
    extra: str,
    k_tether: float,
) -> ScenePlan:
    text = scene_text(sheet, features, ks, extra)
    net = build(text, strict=False)
    errs = sorted({f.code for f in net.report.errors()})
    if errs:
        raise ValueError(f"scene build errors {errs}")
    seed = np.asarray(net.seed3, dtype=float)
    inst = np.array([a.instance for a in net.atoms])
    _check_seams(net.rings, inst, features)
    # authored: on the surface the author wrote (sheet, frustum, tube) and
    # judged against it.  Tethered: authored, less a band of
    # _BALL_FREE_BONDS bonds below a ball top -- holding the (6,0) tube to
    # its cylinder right up to the C60 neck folds the neck (0.83 A clash
    # with no band, 1.05 A with 4-6 bonds free; s4_topfree probe).  A lid
    # needs the tether up to its seam: freeing the same band crumpled a
    # one-period bump (fillet max 4.6 A) and lifted a pill lid seam's
    # pyramidalisation from 3 to 23 degrees.
    authored = inst == "s"
    for f in features:
        authored |= (inst == f.name) | (inst == f"{f.name}f")
    hops = _hops_from(net.bonds, ~authored)
    balls = np.isin(inst, [f"{f.name}c" for f in features if f.top == "ball"])
    tethered = authored & (_hops_from(net.bonds, balls) > _BALL_FREE_BONDS)
    feats = []
    for f in features:
        tub = seed[inst == f.name]
        if float(tub[:, 2].mean()) > 0.0:
            raise ValueError(f"{f.name}: the build grew the feature along +z")
        rt = tube_radius(f.n, 0)
        meridian = rv.authored_meridian(
            rt + f.radius + _SCENE_FLAT_A,
            [("line", _SCENE_FLAT_A), ("arc", f.radius, -90.0), ("line", _WALL_A)],
        )
        centre = (float(tub[:, 0].mean()), float(tub[:, 1].mean()))
        feats.append(Feature(f.name, centre, meridian))

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot, nrm = surface_foot(pos * _FLIP, feats, ds=_DS)
        foot, nrm = foot * _FLIP, nrm * _FLIP
        foot[~tethered] = pos[~tethered]
        nrm[~tethered] = 0.0
        return foot, nrm

    def judge(pos: np.ndarray) -> float:
        d, _ = surface_distance(pos[authored] * _FLIP, feats, ds=_DS)
        return float(d.mean())

    pos, force, passes = _tethered_relax(net, tether, k_tether, judge)
    findings = tuple(geometry_findings(net, relaxed=Relaxed(pos, force, "tethered")))

    # one bond away from a top or a bud is that top's seam, not the foot's
    near_top = hops <= 1
    d_all, _ = surface_distance(pos * _FLIP, feats, ds=_DS)
    rows: dict[str, FootRow] = {}
    tops: dict[str, tuple[float, float]] = {}
    for f, ft in zip(features, feats, strict=True):
        rt = tube_radius(f.n, 0)
        rad = np.hypot(pos[:, 0] - ft.centre[0], pos[:, 1] - ft.centre[1])
        mine = (inst == f.name) | (inst == f"{f.name}f")
        zone = authored & (rad <= rt + f.radius + 1.0) & (-pos[:, 2] <= f.radius + 1.0)
        dz = d_all[zone]
        # the foot's corners: not a top's seam, not the free band below a ball
        corner = (zone | mine) & ~near_top & tethered
        bl = np.array(
            [
                float(np.linalg.norm(pos[i] - pos[j]))
                for i, j, *_ in net.bonds
                if corner[i] and corner[j]
            ]
        )
        arms, amax, pmax = angle_stats(pos, net.bonds, net.rings, atoms=corner)
        rim = sorted(
            {
                a
                for i, j, *_ in net.bonds
                for a, o in ((i, j), (j, i))
                if inst[a] == "s" and inst[o] == f"{f.name}f"
            }
        )
        misses = tuple(
            name
            for name, bad in (
                ("fillet mean", dz.mean() > MEAN_MAX_A),
                ("fillet max", dz.max() > DEV_MAX_A),
                ("bond min", bl.min() < BOND_MIN_A),
                ("bond max", bl.max() > BOND_MAX_A),
            )
            if bad
        )
        rows[f.name] = FootRow(
            k=ks[f.name],
            k_tether=k_tether,
            fillet_mean=float(dz.mean()),
            fillet_p95=float(np.percentile(dz, 95)),
            fillet_max=float(dz.max()),
            bond_min=float(bl.min()),
            bond_max=float(bl.max()),
            angle_rms=arms,
            angle_max=amax,
            pyramid_max=pmax,
            rim_r=float(rad[rim].mean()),
            passes=passes,
            misses=misses,
        )
        if f.top != "open":
            joint = (inst == f"{f.name}c") | (mine & (near_top | ~tethered))
            _rms, tmax, tpyr = angle_stats(pos, net.bonds, net.rings, atoms=joint)
            tops[f.name] = (tmax, tpyr)
    return ScenePlan(
        text=text,
        ks=dict(ks),
        positions=pos,
        instances=tuple(inst.tolist()),
        rows=rows,
        tops=tops,
        findings=findings,
        passes=passes,
    )


@functools.lru_cache(maxsize=32)
def _planned_k(n: int, radius: float, k_tether: float) -> int:
    return plan_foot(n, radius, k_tether=k_tether).k


def plan_scene(
    sheet: tuple[int, int],
    features: tuple[SceneFeature, ...],
    *,
    extra: str = "",
    k_tether: float = 1.0,
) -> ScenePlan:
    """Tile several authored feet on one sheet in one tethered relax.

    Every feature starts at :func:`k_min` (the narrowest frustum won every
    S3 anchor) and is judged in its own fillet zone.  A feature whose row
    misses a bar gets :func:`plan_foot`'s candidate loop on its own sheet;
    when that picks a different k the scene is rebuilt once with it.  The
    returned rows are the scene's, so a miss that survives is reported,
    never hidden."""
    names = [f.name for f in features]
    if len(set(names)) != len(names) or "s" in names:
        raise ValueError(f"feature names must be distinct and not 's': {names}")
    ks = {f.name: k_min(f.n) for f in features}
    plan = _relax_scene(sheet, features, ks, extra, k_tether)
    redo = {
        f.name: _planned_k(f.n, f.radius, k_tether)
        for f in features
        if not plan.rows[f.name].meets
    }
    if any(ks[name] != k for name, k in redo.items()):
        plan = _relax_scene(sheet, features, {**ks, **redo}, extra, k_tether)
    return plan
