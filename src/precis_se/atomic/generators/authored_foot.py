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

**Authored tops (hexfold-toolkit cycle 1).** ``top: "sphere"`` and
``top: "lid"`` with ``top_fillet`` are authored surfaces too: the meridian
carries on past the tube as a top fillet and a sphere arc or a hemisphere
(:func:`plan_top`), and every atom of the top is tethered to it.

This module sits in ``precis_se`` because it is the layer that already
imports both hexfold and precis_surface; hexfold itself never imports
precis_surface (the tether is a callable).
"""

from __future__ import annotations

import dataclasses
import functools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hexfold.build import build
from hexfold.check import Relaxed, _clash_pairs, geometry_findings
from hexfold.lattice import tube_radius
from hexfold.report import HexfoldError
from hexfold.stick import stick_info
from precis_surface import revolution as rv
from precis_surface.deviation import Feature, surface_distance, surface_foot
from precis_surface.relax import theta_p_by_atom

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
    # ERROR finding codes of hexfold's geometry tier on the tethered
    # coordinates (``geom.seed_overlap``, ``geom.clash``); a foot row only.
    # Scene rows leave it empty: the scene's errors live in ``ScenePlan``.
    errors: tuple[str, ...] = ()

    @property
    def meets(self) -> bool:
        return not self.misses and not self.errors

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

    pos, force, passes = _tethered_relax(
        net,
        tether,
        k_tether,
        lambda q: float(surface_distance(q * _FLIP, feat, ds=_DS)[0].mean()),
    )
    errors = tuple(
        sorted(
            {
                f.code
                for f in geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
                if f.severity.name == "ERROR"
            }
        )
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
        errors=errors,
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
    angle max, else the smallest bar excess, a row without ERROR findings
    ahead of one with (``meets`` is then False).  A row meets only with no
    bar missed and no ERROR finding on its tethered coordinates."""
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
        else min(measured, key=lambda m: (bool(m[0].errors), m[0].excess))
    )
    row, pos, centre, text = best
    return FootPlan(
        n=n, radius=radius, k=row.k, text=text, positions=pos, centre=centre, rows=rows
    )


# --- scenes: several authored feet on one sheet (S4) ----------------------

_SCENE_FLAT_A = 1.5  # a scene feature's own flat run; the sheet beyond is |z|
_BALL_FREE_BONDS = 4  # tube atoms this close (bonds) to a ball top relax free
_TOPS = ("open", "lid", "ball", "sphere")


@dataclass(frozen=True)
class SceneFeature:
    """One authored foot on the scene sheet: sheet → fillet ``radius`` →
    an ``(n, 0)`` tube of ``tube_len`` periods → ``top``.

    ``name`` is the tube's hexfold instance; the frustum is ``<name>f`` and
    the top ``<name>c``.  ``at`` is the sheet cell of the hole centre.
    ``top``: ``open``, ``lid`` (a flat ``cap(n,0)``), ``ball`` (C60 minus
    a hexagon, fused k=3; (6,0) only) or ``sphere`` (a washer, bulge and lid
    on a ``(6k,0)`` frustum; n a multiple of 6, n >= 12).  ``ball`` and a
    flat ``lid`` are not authored surfaces: the tether does not act on them,
    nor on the last few tube bonds below a ball, and their joint is reported
    in ``ScenePlan.tops``.  ``top_R`` (sphere only) and ``top_fillet`` (sphere
    or lid) author the top surface; see :func:`plan_top`.  A ``lid`` with
    ``top_fillet`` is a rounded lid and, like ``sphere``, an authored top."""

    name: str
    at: tuple[int, int]
    n: int
    radius: float
    tube_len: int
    top: str = "open"
    top_R: float | None = None
    top_fillet: float | None = None


def _authored_top(f: SceneFeature) -> bool:
    """True for a top the tether holds to an authored surface: a sphere, or
    a lid with ``top_fillet``."""
    return f.top == "sphere" or (f.top == "lid" and f.top_fillet is not None)


def _top_names(f_name: str, top: str) -> tuple[str, ...]:
    """Hexfold instances of an authored top, tube end first: a sphere is
    washer ``w``, bulge ``b``, lid ``c`` (suffixes on the tube's name); a
    rounded lid is ``c`` alone."""
    if top == "sphere":
        return (f"{f_name}w", f"{f_name}b", f"{f_name}c")
    return (f"{f_name}c",)


def _check_top(f: SceneFeature) -> None:
    """Refuse a top the build cannot seat, by name and with the reason."""
    if f.top not in _TOPS:
        raise ValueError(f"{f.name}: top must be one of {_TOPS}; got {f.top!r}")
    if f.top == "ball" and f.n != 6:
        raise ValueError(f"{f.name}: a ball top fuses onto (6,0) only; got n={f.n}")
    if f.top == "lid" and f.n % 6:
        raise ValueError(f"{f.name}: a flat lid needs n a multiple of 6; got {f.n}")
    if f.top == "sphere" and (f.n % 6 or f.n < 12):
        raise ValueError(
            f"{f.name}: a sphere top needs n a multiple of 6 and n >= 12 (a "
            f"hex(n/6-1) washer hole fused to a cap(6k,0) frustum); got n={f.n}"
        )
    if f.top_R is not None and f.top != "sphere":
        raise ValueError(f"{f.name}: top_R belongs to top 'sphere'; top is {f.top!r}")
    if f.top_fillet is not None and f.top not in ("sphere", "lid"):
        raise ValueError(
            f"{f.name}: top_fillet belongs to top 'sphere' or 'lid'; top is {f.top!r}"
        )
    for key, val in (("top_R", f.top_R), ("top_fillet", f.top_fillet)):
        if val is not None and not val > 0.0:
            raise ValueError(f"{f.name}: {key} must be positive; got {val}")
    if f.top == "lid" and f.top_fillet is not None:
        rt = tube_radius(f.n, 0)
        if f.top_fillet > rt + 1e-9:
            raise ValueError(
                f"{f.name}: top_fillet {f.top_fillet:g} A is more than the tube "
                f"radius {rt:.2f} A; a lid rounds toward a hemisphere of radius r "
                "at most"
            )


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
    # per authored top (sphere / rounded lid): the chosen candidate's plan,
    # and its columns re-measured on this scene's coordinates
    top_plans: dict[str, TopPlan] = field(default_factory=dict)
    top_rows: dict[str, TopRow] = field(default_factory=dict)

    @property
    def errors(self) -> tuple[str, ...]:
        """ERROR finding codes (``geom.clash`` overlap, ``geom.seed_overlap``)."""
        return tuple(
            sorted({f.code for f in self.findings if f.severity.name == "ERROR"})
        )

    @property
    def meets(self) -> bool:
        return not self.errors and all(r.meets for r in self.rows.values())


def _top_text(
    name: str, n: int, top: str, k: int, length: int
) -> tuple[list[str], list[str]]:
    """Body and fuse lines of a lid or sphere top on the tube ``name``.  The
    sphere is the washer route measured in ``plan_top``: tube(n,0) -> a
    ``cap(6k,0) - hex(n/6-1)`` washer (six heptagons) -> a ``(6k,0)`` bulge
    of ``length`` periods (six pentagons) -> a ``cap(6k,0)`` lid (six
    pentagons)."""
    c = f"{name}c"
    if top == "lid":
        return [f"{c}: cap({n},0)"], [f"{name}.out --fuse k=0--> {c}.in"]
    w, b = f"{name}w", f"{name}b"
    return (
        [
            f"{w}: cap({6 * k},0) - hex({n // 6 - 1})@(0,0,A):0",
            f"{b}: tube({6 * k},0, len={length})",
            f"{c}: cap({6 * k},0)",
        ],
        [
            f"{name}.out --fuse k=0--> {w}.hole",
            f"{w}.in --fuse k=0--> {b}.in",
            f"{b}.out --fuse k=0--> {c}.in",
        ],
    )


def scene_text(
    sheet: tuple[int, int],
    features: tuple[SceneFeature, ...],
    ks: dict[str, int],
    extra: str = "",
    top_plans: Mapping[str, TopPlan] | None = None,
) -> str:
    """One sheet with one ``hex(k/2-1)`` hole per feature, each fused to its
    3+3 frustum, tube and top (:func:`foot_text`'s lines, per feature).
    ``extra`` is appended verbatim: buds and their attachments.  A
    ``sphere`` feature needs its :class:`TopPlan` in ``top_plans`` (the
    washer ``k`` and bulge length)."""
    holes: list[str] = []
    body: list[str] = []
    fuses: list[str] = []
    for idx, f in enumerate(features):
        _check_top(f)
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
        if f.top == "sphere":
            if top_plans is None or f.name not in top_plans:
                raise ValueError(f"{f.name}: a sphere top needs its plan_top plan")
            tp = top_plans[f.name]
            tb, tf = _top_text(f.name, f.n, "sphere", tp.k, tp.length)
            body.extend(tb)
            fuses.extend(tf)
        elif f.top == "lid":
            tb, tf = _top_text(f.name, f.n, "lid", 0, 0)
            body.extend(tb)
            fuses.extend(tf)
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


def _scene_masks(
    bonds: Any, inst: np.ndarray, features: tuple[SceneFeature, ...]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(authored, tethered, near_top) per atom.

    Authored: on the surface the author wrote (sheet, frustum, tube), and
    judged.  Tethered: authored, less a band of ``_BALL_FREE_BONDS`` bonds
    below a ball top -- holding the (6,0) tube to its cylinder right up to
    the C60 neck folds the neck (0.83 A clash with no band, 1.05 A with 4-6
    bonds free; s4_topfree probe).  A lid needs the tether up to its seam:
    freeing the same band crumpled a one-period bump (fillet max 4.6 A) and
    lifted a pill lid seam's pyramidalisation from 3 to 23 degrees.
    Near-top: one bond from a top or a bud, that top's seam, not the foot's.
    """
    authored = inst == "s"
    for f in features:
        authored |= (inst == f.name) | (inst == f"{f.name}f")
        if _authored_top(f):  # a sphere or rounded lid is held to its surface
            authored |= np.isin(inst, _top_names(f.name, f.top))
    balls = np.isin(inst, [f"{f.name}c" for f in features if f.top == "ball"])
    tethered = authored & (_hops_from(bonds, balls) > _BALL_FREE_BONDS)
    near_top = _hops_from(bonds, ~authored) <= 1
    return authored, tethered, near_top


def _joint_mask(
    inst: np.ndarray, f: SceneFeature, near_top: np.ndarray, tethered: np.ndarray
) -> np.ndarray:
    """A top and its joint: the top's atoms plus the feature's own atoms
    the foot row leaves out (its seam and the free band), so every atom of
    the feature is judged in one of the two."""
    mine = (inst == f.name) | (inst == f"{f.name}f")
    return (inst == f"{f.name}c") | (mine & (near_top | ~tethered))


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


# --- authored tops: a sphere or a rounded lid (hexfold-toolkit, cycle 1) ---

THETA_P_MAX_DEG = 12.0  # planner bar: POAV1 pyramidalisation on the top
TOP_DEV_P95_A = 0.3  # planner bar: tethered deviation p95 over the top region
TOP_BOND_MAX_A = 1.7  # planner bar: every top bond shorter than this
TOP_PAIR_MIN_A = 1.34  # planner bar: no non-bonded pair closer than this
RELAXED_P95_A = 0.5  # band: relaxed (tether off) deviation p95, every top
FILLET_FLOOR_A = 2.0  # smallest top fillet that held in the probe
_C60_THETA_P_DEG = 11.6  # POAV1 pyramidalisation of C60 ...
_C60_R_A = 3.55  # ... whose radius is this
_A_ATOM = 3 * math.sqrt(3) / 4 * 1.42**2  # graphene area per atom, A^2
_TRIAL_TUBE_LEN = 2  # periods of tube under the top in a candidate's trial
_TRIAL_RHO = 1.0  # trial meridian's own foot arc; the tube stands on it
_TOP_HOPS = 2  # tube atoms this close (bonds) to the top are judged with it
_P95_TIE_A = 0.01  # relaxed p95 gap under which two candidates are tied
_LID_DOME_ROWS = (2, 3, 4, 5)  # tube atom rows a rounded lid's dome takes in
_SPHERE_LENGTHS = (1, 2, 3)  # bulge periods; 0 never builds (probe: torn net)

_Piece = tuple[str, float] | tuple[str, float, float]


@dataclass(frozen=True)
class TopRow:
    """One top candidate (or the chosen one re-measured in a scene).  Lengths
    in Å, angles in degrees.  ``k``/``length`` are 0 for a rounded lid (it
    uses today's flat ``cap(n,0)``, and ``dome_rows`` counts the tube atom
    rows its dome takes in); ``drop`` is how far below the tube/top seam the
    dome starts; ``R`` is the radius of the sphere (area-matched to
    the atoms above the dome start) or, for a rounded lid, of the hemisphere
    those atoms would cover."""

    k: int
    length: int
    dome_rows: int  # tube atom rows inside a rounded lid's dome (0: a sphere)
    drop: float
    R: float
    fillet: float
    atoms: int  # atoms above the dome start
    dev_p95: float  # tethered, over the top region
    dev_max: float
    theta_p_max: float
    bond_max: float
    pairs: int  # non-bonded pairs under TOP_PAIR_MIN_A touching the region
    errors: tuple[str, ...]  # ERROR finding codes of the tethered coordinates
    relaxed_p95: float  # tether off, deviation from the authored surface
    relaxed_dz: float  # top z_max drop when the tether is off
    passes: int
    misses: tuple[str, ...]  # failing bars; empty = meets all five
    # a candidate that could not be built or seated: the reason (``misses`` is
    # then ``("build",)`` and every measured column is 0.0, not a measurement)
    error: str = ""

    @property
    def meets(self) -> bool:
        return not self.misses


@dataclass(frozen=True)
class TopPlan:
    """The chosen top, with every candidate measured (``rows``)."""

    kind: str  # "sphere" | "lid"
    n: int
    k: int
    length: int
    dome_rows: int
    drop: float
    R: float
    fillet: float
    authored_R: float | None
    authored_fillet: float | None
    chosen: TopRow
    rows: tuple[TopRow, ...]

    @property
    def meets(self) -> bool:
        return self.chosen.meets

    @property
    def relaxed_ok(self) -> bool:
        return self.chosen.relaxed_p95 <= RELAXED_P95_A


def r_min_fillet(r_tube: float, theta_p_max: float = THETA_P_MAX_DEG) -> float:
    """Smallest top-fillet radius whose shoulder stays under ``theta_p_max``.

    Derivation.  POAV1 pyramidalisation of a sp2 sheet bent to mean curvature
    ``kappa_mean = (k1 + k2) / 2`` is ``theta_p ~ c * kappa_mean``; the C60
    anchor (theta_p 11.6 deg at R = 3.55 A, ``kappa_mean = 1/R`` for a sphere)
    fixes ``c = 11.6 deg * 3.55 A``.  So ``theta_p <= theta_p_max`` means
    ``kappa_mean <= theta_p_max / (11.6 deg * 3.55 A)``, 0.291 /A at 12 deg.
    At the shoulder where the tube turns into the fillet the surface curves
    two ways: around the hoop (``1/r_tube``) and along the meridian (``1/R_t``
    for a fillet of radius ``R_t``).  Taking both magnitudes as one sign is
    the conservative reading (a concave fillet's meridian curvature opposes
    the hoop's, so the true mean is smaller; this bound cannot understate
    the strain): ``(1/r_tube + 1/R_t) / 2 <= kappa_max`` gives
    ``R_t >= 1 / (2 kappa_max - 1/r_tube)``.  A singly curved fillet
    (``r_tube -> inf``) gives ``R_t >= 1 / (2 kappa_max)`` = 1.72 A.  When
    the hoop curvature alone passes ``2 kappa_max`` no fillet radius helps
    and the result is ``inf``.
    """
    kappa_max = theta_p_max / (_C60_THETA_P_DEG * _C60_R_A)
    spare = 2.0 * kappa_max - 1.0 / r_tube
    return math.inf if spare <= 0.0 else 1.0 / spare


def default_fillet(
    r_tube: float, big_r: float, theta_p_max: float = THETA_P_MAX_DEG
) -> float:
    """The sphere top's default fillet radius: ``1.5 * r_min_fillet`` capped
    by the room between tube and sphere (the radial step ``R - r_tube`` the
    fillet has to make), and never below ``FILLET_FLOOR_A``."""
    room = big_r - r_tube
    return max(FILLET_FLOOR_A, min(1.5 * r_min_fillet(r_tube, theta_p_max), room))


def _sphere_pieces(rt: float, r_t: float, big_r: float) -> list[_Piece]:
    """Meridian pieces after the cylinder: a concave fillet ``r_t`` tangent to
    a sphere of radius ``big_r`` centred on the axis, then the sphere up to
    its pole.  The fillet circle's centre is ``r_t`` out from the cylinder,
    and ``big_r + r_t`` from the sphere's centre."""
    if big_r < rt:
        raise ValueError(
            f"sphere radius {big_r:.2f} A is under the tube radius {rt:.2f} A"
        )
    dz = math.sqrt((big_r + r_t) ** 2 - (rt + r_t) ** 2)
    phi = math.atan2(dz, -(rt + r_t))
    t1 = -math.degrees(math.pi - phi)
    t2 = math.degrees(1.5 * math.pi - phi) - 1e-4  # stop short of the axis
    out: list[_Piece] = []
    if abs(t1) > 1e-6:
        out.append(("arc", r_t, t1))
    out.append(("arc", big_r, t2))
    return out


def _top_meridian(
    kind: str,
    rt: float,
    rho: float,
    z1: float,
    big_r: float,
    fillet: float,
) -> rv.Meridian:
    """The authored meridian of a feature with an authored top: sheet, foot
    fillet ``rho``, cylinder up to ``z1`` (measured from the sheet), then the
    top: a sphere top's concave fillet and sphere arc, or a rounded lid's
    convex fillet (a hemisphere when ``fillet`` is the tube radius)."""
    lc = z1 - rho
    if lc <= 0.0:
        raise ValueError(
            f"the tube is too short for the top: the dome starts {z1:.2f} A above "
            f"the sheet, under the {rho:g} A foot fillet; lengthen tube_len"
        )
    flat = _SCENE_FLAT_A
    if kind == "sphere":
        tail = _sphere_pieces(rt, fillet, big_r)
        flat = max(flat, big_r + 3.0 - rt - rho)  # the sheet reaches past the bulge
    else:
        tail = [("arc", fillet, 90.0)]
        if rt - fillet > 1e-6:
            tail.append(("line", rt - fillet))
    return rv.authored_meridian(
        rt + rho + flat,
        [("line", flat), ("arc", rho, -90.0), ("line", lc), *tail],
    )


def _area_above(m: rv.Meridian, z1: float) -> float:
    """Revolved area of the meridian at or above height ``z1``."""
    tot = 0.0
    for seg in m.segments:
        p = seg.at(np.linspace(0.0, 1.0, 801))
        p = p[p[:, 1] >= z1 - 1e-9]
        if len(p) < 2:
            continue
        ds = np.linalg.norm(np.diff(p, axis=0), axis=1)
        tot += float((math.tau * 0.5 * (p[1:, 0] + p[:-1, 0]) * ds).sum())
    return tot


def _solve_sphere_r(rt: float, z1: float, r_t: float, target: float) -> float:
    """Sphere radius whose top surface above ``z1`` has area ``target``."""
    lo, hi = rt + 1e-3, 60.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        m = _top_meridian("sphere", rt, _TRIAL_RHO, z1, mid, r_t)
        if _area_above(m, z1) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _resolve_top(
    kind: str,
    rt: float,
    z_junc: float,
    z_plane: float,
    n_top: int,
    tube_z: np.ndarray,
    *,
    dome_rows: int,
    fillet_req: float | None,
    theta_p_max: float,
) -> tuple[float, float, float, float, int]:
    """(z1, R, fillet, drop, atoms above z1) of a candidate, z in the +z
    frame.  A sphere's dome starts ``fillet / 2`` below the tube/top seam
    (the probe's choice), its R is area-matched to the atoms above it, and
    its fillet is ``fillet_req`` or the default, found by a short fixed point
    (R depends on the fillet through z1, the default on R).  A rounded lid's
    dome starts between the ``dome_rows``-th tube atom row below the top and
    the next one, its fillet is the author's, and its ``R`` the hemisphere
    those atoms' area would cover."""
    if kind == "lid":
        assert fillet_req is not None
        levels = sorted({round(float(z), 2) for z in tube_z}, reverse=True)
        z1 = 0.5 * (levels[dome_rows - 1] + levels[dome_rows])
        atoms = n_top + int((tube_z > z1).sum())
        return (
            z1,
            math.sqrt(atoms * _A_ATOM / math.tau),
            fillet_req,
            z_junc - z1,
            atoms,
        )
    r_t = (
        fillet_req
        if fillet_req is not None
        else max(FILLET_FLOOR_A, 1.5 * r_min_fillet(rt, theta_p_max))
    )
    for it in range(4):
        z1 = z_junc - 0.5 * r_t
        atoms = n_top + int((tube_z > z1).sum())
        big_r = _solve_sphere_r(rt, z1 - z_plane, r_t, atoms * _A_ATOM)
        if fillet_req is not None or it == 3:
            break
        new = default_fillet(rt, big_r, theta_p_max)
        if abs(new - r_t) < 0.02:
            break
        r_t = new
    return z1, big_r, r_t, 0.5 * r_t, atoms


def _top_region(
    bonds: Any, inst: np.ndarray, name: str, top: str, zplus: np.ndarray, z1: float
) -> np.ndarray:
    """A top's judged atoms: its own instances, and the tube atoms within
    ``_TOP_HOPS`` bonds of it or above the dome start."""
    istop = np.isin(inst, _top_names(name, top))
    hops = _hops_from(bonds, istop)
    return istop | ((inst == name) & ((hops <= _TOP_HOPS) | (zplus > z1)))


def _kabsch(src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rotation and translation taking ``src`` onto ``dst`` (least squares)."""
    cs, cd = src.mean(axis=0), dst.mean(axis=0)
    u, _s, vt = np.linalg.svd((src - cs).T @ (dst - cd))
    d = np.sign(np.linalg.det(vt.T @ u.T))
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return rot, cd - rot @ cs


def _top_misses(cols: Mapping[str, Any], theta_p_max: float) -> tuple[str, ...]:
    """The failing bars among the five."""
    return tuple(
        name
        for name, bad in (
            ("errors", bool(cols["errors"])),
            (f"pairs<{TOP_PAIR_MIN_A}", cols["pairs"] > 0),
            ("dev p95", cols["dev_p95"] > TOP_DEV_P95_A),
            (f"bonds<{TOP_BOND_MAX_A}", cols["bond_max"] >= TOP_BOND_MAX_A),
            ("theta_p", cols["theta_p_max"] > theta_p_max),
        )
        if bad
    )


def _top_columns(
    pos: np.ndarray, net_bonds: Any, region: np.ndarray, dist: np.ndarray
) -> dict[str, Any]:
    """The bars' measured columns on ``pos`` over the top ``region``; ``dist``
    is every atom's distance to the authored surface."""
    d = dist[region]
    bonds = np.array([(i, j) for i, j, *_ in net_bonds])
    tp = theta_p_by_atom(pos, bonds)[region]
    inreg = region[bonds[:, 0]] & region[bonds[:, 1]]
    bl = np.linalg.norm(pos[bonds[inreg, 0]] - pos[bonds[inreg, 1]], axis=1)
    close = [
        p
        for p in _clash_pairs(pos, net_bonds, TOP_PAIR_MIN_A)
        if region[p[1]] or region[p[2]]
    ]
    return {
        "dev_p95": float(np.percentile(d, 95)),
        "dev_max": float(d.max()),
        "theta_p_max": float(np.nanmax(tp)),
        "bond_max": float(bl.max()),
        "pairs": len(close),
    }


def _measure_top(
    n: int,
    kind: str,
    k: int,
    length: int,
    *,
    dome_rows: int,
    fillet_req: float | None,
    k_tether: float,
    theta_p_max: float,
) -> TopRow:
    """Build one candidate on a bare tube (``_TRIAL_TUBE_LEN`` periods; no
    sheet, so a trial costs a few hundred atoms), hold every atom to the
    candidate's authored meridian, measure the five bars, then relax once
    with the tether off and measure how far the top settles from the surface.

    The relaxed number is rigid-aligned first (a Kabsch fit on the tube atoms
    four or more bonds from the top, which the tether-off relax moves least)
    so a drifting tube does not read as a flat top."""
    body, fuses = _top_text("t", n, kind, k, length)
    text = "\n".join(
        [
            "hexfold 0.2",
            "origin t",
            f"t: tube({n},0, len={_TRIAL_TUBE_LEN})",
            *body,
            *fuses,
        ]
    )
    net = build(text + "\n", strict=False)
    errs = sorted({f.code for f in net.report.errors()})
    if errs:
        raise ValueError(f"({n},0) {kind} top k={k} L={length}: build errors {errs}")
    seed = np.asarray(net.seed3, dtype=float)
    inst = np.array([a.instance for a in net.atoms])
    zplus = seed[:, 2]  # the bare tube grows +z
    tube = inst == "t"
    names = _top_names("t", kind)
    istop = np.isin(inst, names)
    junc = sorted(
        {
            a
            for i, j, *_ in net.bonds
            for a, o in ((i, j), (j, i))
            if tube[a] and inst[o] == names[0]
        }
    )
    rt = tube_radius(n, 0)
    z_plane = float(zplus[tube].min()) - _TRIAL_RHO - 0.5
    z1, big_r, fillet, drop_a, atoms = _resolve_top(
        kind,
        rt,
        float(zplus[junc].mean()),
        z_plane,
        int(istop.sum()),
        zplus[tube],
        dome_rows=dome_rows,
        fillet_req=fillet_req,
        theta_p_max=theta_p_max,
    )
    if kind == "sphere" and fillet_req is not None and fillet > big_r - rt:
        # judged against this candidate's realised (area-matched) R
        raise ValueError(
            f"top_fillet {fillet:g} A is more than the room R - r_tube = "
            f"{big_r - rt:.2f} A (R {big_r:.2f} A at k={k}, L={length})"
        )
    meridian = _top_meridian(kind, rt, _TRIAL_RHO, z1 - z_plane, big_r, fillet)
    centre = (float(seed[tube, 0].mean()), float(seed[tube, 1].mean()))
    feats = [Feature("t", centre, meridian)]

    # The scene's foot holds the tube's lower end in place; a bare tube would
    # slide down its cylinder under the dome's pull, so the bottom two atom
    # rows are held at their seed height (a z-only spring, free to breathe
    # radially) and the dome starts where the scene's would.
    pin = tube & (zplus <= sorted({round(float(z), 2) for z in zplus[tube]})[1] + 1e-6)

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot, nrm = surface_foot(pos, feats, ds=_DS, z_offset=z_plane)
        foot[pin] = np.column_stack([pos[pin, :2], zplus[pin]])
        nrm[pin] = (0.0, 0.0, 1.0)
        return foot, nrm

    def dist(pos: np.ndarray) -> np.ndarray:
        return surface_distance(pos, feats, ds=_DS, z_offset=z_plane)[0]

    pos, force, passes = _tethered_relax(
        net, tether, k_tether, lambda q: float(dist(q).mean())
    )
    errors = tuple(
        sorted(
            {
                f.code
                for f in geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
                if f.severity.name == "ERROR"
            }
        )
    )
    region = _top_region(net.bonds, inst, "t", kind, zplus, z1)
    cols = _top_columns(pos, net.bonds, region, dist(pos))
    cols["errors"] = errors

    free, _ = stick_info(dataclasses.replace(net, seed3=tuple(map(tuple, pos))))
    anchor = tube & (_hops_from(net.bonds, istop) >= 4)
    rot, shift = _kabsch(free[anchor], pos[anchor])
    free = free @ rot.T + shift
    return TopRow(
        k=k,
        length=length,
        dome_rows=dome_rows,
        drop=drop_a,
        R=big_r,
        fillet=fillet,
        atoms=atoms,
        relaxed_p95=float(np.percentile(dist(free)[region], 95)),
        relaxed_dz=float(pos[istop, 2].max() - free[istop, 2].max()),
        passes=passes,
        misses=_top_misses(cols, theta_p_max),
        **cols,
    )


def pick_top(rows: Sequence[TopRow], authored_R: float | None = None) -> TopRow:
    """The chosen candidate.  Among rows that meet all five bars: take the
    smallest relaxed deviation p95, call every row within ``_P95_TIE_A``
    (0.01 A) of it tied, and choose the narrowest ``k``, then the shortest
    ``length``, then the fewest ``dome_rows``.  An authored ``top_R`` comes
    first: the row whose area-matched ``R`` is nearest it (ties by the same
    rule among the rows equally near).  When no row meets the bars, the one
    that built with the fewest misses (an ERROR finding last), then the
    smallest tethered p95; ``meets`` is then False, and :func:`plan_top`
    refuses."""
    meeting = [r for r in rows if r.meets]
    if meeting:
        pool = meeting
        if authored_R is not None:
            near = min(abs(r.R - authored_R) for r in meeting)
            pool = [r for r in meeting if abs(r.R - authored_R) <= near + 1e-9]
        best = min(r.relaxed_p95 for r in pool)
        tied = [r for r in pool if r.relaxed_p95 <= best + _P95_TIE_A]
        return min(tied, key=lambda r: (r.k, r.length, r.dome_rows))
    return min(
        rows,
        key=lambda r: (bool(r.error), bool(r.errors), len(r.misses), r.dev_p95),
    )


def _candidates(n: int, kind: str) -> list[tuple[int, int, int]]:
    """(k, L, dome_rows) per candidate: a sphere's washer-route grid, a
    rounded lid's dome depths (k and L are 0: it reuses the flat lid)."""
    if kind == "lid":
        return [(0, 0, m) for m in _LID_DOME_ROWS]
    r = n // 6 - 1
    return [(k, length, 0) for k in range(r + 3, r + 6) for length in _SPHERE_LENGTHS]


@functools.lru_cache(maxsize=16)
def _top_grid(
    n: int,
    kind: str,
    top_fillet: float | None,
    k_tether: float,
    theta_p_max: float,
) -> tuple[TopRow, ...]:
    """Every candidate, measured.  Cached without ``top_R``: a request only
    changes which row is picked, never what any row measures."""
    rows: list[TopRow] = []
    for k, length, rows_in in _candidates(n, kind):
        try:
            rows.append(
                _measure_top(
                    n,
                    kind,
                    k,
                    length,
                    dome_rows=rows_in,
                    fillet_req=top_fillet,
                    k_tether=k_tether,
                    theta_p_max=theta_p_max,
                )
            )
        except (ValueError, HexfoldError) as exc:
            # one candidate that will not build (a cut.overlap, a fillet with
            # no room) is a miss in the grid, not the end of the plan
            rows.append(_failed_row(k, length, rows_in, top_fillet, str(exc)))
    return tuple(rows)


def _failed_row(
    k: int, length: int, dome_rows: int, fillet: float | None, error: str
) -> TopRow:
    """A candidate that did not build: a miss carrying the reason; its
    numbers are 0.0 placeholders, not measurements."""
    return TopRow(
        k=k,
        length=length,
        dome_rows=dome_rows,
        drop=0.0,
        R=0.0,
        fillet=fillet if fillet is not None else 0.0,
        atoms=0,
        dev_p95=0.0,
        dev_max=0.0,
        theta_p_max=0.0,
        bond_max=0.0,
        pairs=0,
        errors=(),
        relaxed_p95=0.0,
        relaxed_dz=0.0,
        passes=0,
        misses=("build",),
        error=error,
    )


def plan_top(
    n: int,
    kind: str,
    *,
    top_R: float | None = None,
    top_fillet: float | None = None,
    k_tether: float = 1.0,
    theta_p_max: float = THETA_P_MAX_DEG,
) -> TopPlan:
    """Tile an authored top on an ``(n, 0)`` tube, like :func:`plan_foot`: every
    candidate is built, relaxed under the tether and measured, and the plan
    keeps the best one that meets five bars on the top region (the top's
    atoms, and the tube atoms within two bonds or above the dome start):
    0 ERROR findings, 0 non-bonded pairs under 1.34 A, tethered deviation
    p95 <= 0.3 A, every bond under 1.7 A, POAV1 pyramidalisation
    <= ``theta_p_max`` (12 deg).  The choice order is :func:`pick_top`:
    smallest relaxed deviation, then narrowest ``k``, then shortest ``L``.

    ``kind="sphere"``: a washer route (tube -> ``cap(6k,0) - hex(n/6-1)``
    washer -> ``(6k,0)`` bulge of ``L`` periods -> ``cap(6k,0)`` lid) held to
    cylinder -> concave top fillet -> sphere.  ``n`` a multiple of 6, ``n >=
    12``; candidates ``k`` in ``[r+3, r+5]`` (``r = n/6 - 1``; ``k = r+2``
    refuses ``cut.overlap``) and ``L`` in 1..3 (``L = 0`` tore the net at
    every setting in the probe and is never a candidate).  ``R`` is
    area-matched to the candidate's atoms above the dome start; an authored
    ``top_R`` picks the candidate whose ``R`` is nearest and the plan says so
    (``authored_R`` beside ``R``).  The fillet is ``top_fillet`` or
    :func:`default_fillet`.

    ``kind="lid"`` (needs ``top_fillet`` <= the tube radius): today's flat
    ``cap(n,0)`` lid, its edge rounded toward a hemisphere of the tube's
    radius by the same machinery, not a second code path.  Candidates are
    how many tube atom rows the dome takes in (2 to 5; rows sit 0.71 and
    1.42 A apart), around ``r/2`` of tube by the area argument ``2*pi*r*(r/2) + pi*r**2 = 2*pi*r**2``; the build
    measures which holds.  ``R`` is the hemisphere radius those atoms'
    area covers (``sqrt(atoms * 2.62 A^2 / 2 pi)``): the area argument's
    prediction, checked by the build.

    **Relaxed shape.**  After the tethered build each candidate relaxes once
    with the tether off (the scene's own stick relax, minus the surface
    spring); ``TopRow.relaxed_p95`` is the top's deviation from the authored
    surface after that, and ``relaxed_dz`` how far its z_max drops.  The band
    is ``RELAXED_P95_A`` (0.5 A) on every top; a miss is a WARN on the block
    (``scene.top.relaxed_shape``), and the stored scene stays the tethered
    one.

    **Grammar mapping** (the CAD-style spec layer compiles 1:1 onto these
    keywords; names are fixed so the compiler does not rename):
    ``ball_on(R)`` <-> ``top: "sphere", top_R: R``; ``round(r)`` <->
    ``top_fillet: r``; ``lid_on`` + ``round(r)`` <-> ``top: "lid",
    top_fillet: r``.
    """
    if kind not in ("sphere", "lid"):
        raise ValueError(f"plan_top: kind must be 'sphere' or 'lid'; got {kind!r}")
    if kind == "sphere" and (n % 6 or n < 12):
        raise ValueError(f"a sphere top needs n a multiple of 6 and n >= 12; got n={n}")
    if kind == "lid" and top_fillet is None:
        raise ValueError("a rounded lid needs top_fillet")
    for key, val in (("top_R", top_R), ("top_fillet", top_fillet)):
        if val is not None and not val > 0.0:
            raise ValueError(f"{key} must be positive; got {val}")
    rows = _top_grid(n, kind, top_fillet, k_tether, theta_p_max)
    best = pick_top(rows, top_R)
    if not best.meets:
        why = "; ".join(
            f"k={r.k} L={r.length} rows={r.dome_rows}: "
            + (r.error or ", ".join(r.misses))
            for r in rows
        )
        raise ValueError(
            f"no {kind} top candidate on ({n},0) meets the five bars "
            f"(top_fillet={top_fillet}, top_R={top_R}): {why}"
        )
    return TopPlan(
        kind=kind,
        n=n,
        k=best.k,
        length=best.length,
        dome_rows=best.dome_rows,
        drop=best.drop,
        R=best.R,
        fillet=best.fillet,
        authored_R=top_R,
        authored_fillet=top_fillet,
        chosen=best,
        rows=rows,
    )


def _errors_on(
    findings: Sequence[Any], inst: np.ndarray, names: tuple[str, ...]
) -> tuple[str, ...]:
    """ERROR finding codes with an atom in the instances ``names``: one
    feature's own errors, not every ERROR in the scene.  A finding names its
    atoms in ``data["atoms"]`` (a pair) or, failing that, ``where``."""
    mine = set(names)
    out: set[str] = set()
    for f in findings:
        if f.severity.name != "ERROR":
            continue
        atoms = dict(f.data).get("atoms")
        if atoms is None and f.where is not None:
            atoms = [int(f.where)]
        if atoms and any(inst[a] in mine for a in atoms):
            out.add(f.code)
    return tuple(sorted(out))


def _relax_scene(
    sheet: tuple[int, int],
    features: tuple[SceneFeature, ...],
    ks: dict[str, int],
    extra: str,
    k_tether: float,
    top_plans: Mapping[str, TopPlan] | None = None,
) -> ScenePlan:
    tplans = dict(top_plans or {})
    text = scene_text(sheet, features, ks, extra, tplans)
    net = build(text, strict=False)
    errs = sorted({f.code for f in net.report.errors()})
    if errs:
        raise ValueError(f"scene build errors {errs}")
    seed = np.asarray(net.seed3, dtype=float)
    inst = np.array([a.instance for a in net.atoms])
    _check_seams(net.rings, inst, features)
    authored, tethered, near_top = _scene_masks(net.bonds, inst, features)
    feats = []
    z1s: dict[str, float] = {}  # dome start per authored top, +z frame
    for f in features:
        tub = seed[inst == f.name]
        if float(tub[:, 2].mean()) > 0.0:
            raise ValueError(f"{f.name}: the build grew the feature along +z")
        rt = tube_radius(f.n, 0)
        if _authored_top(f):
            tp = tplans[f.name]
            first = _top_names(f.name, f.top)[0]
            junc = sorted(
                {
                    a
                    for i, j, *_ in net.bonds
                    for a, o in ((i, j), (j, i))
                    if inst[a] == f.name and inst[o] == first
                }
            )
            z1 = float(-seed[junc, 2].mean()) - tp.drop  # +z frame, sheet at 0
            z1s[f.name] = z1
            meridian = _top_meridian(tp.kind, rt, f.radius, z1, tp.R, tp.fillet)
        else:
            meridian = rv.authored_meridian(
                rt + f.radius + _SCENE_FLAT_A,
                [
                    ("line", _SCENE_FLAT_A),
                    ("arc", f.radius, -90.0),
                    ("line", _WALL_A),
                ],
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

    d_all, _ = surface_distance(pos * _FLIP, feats, ds=_DS)
    rows: dict[str, FootRow] = {}
    tops: dict[str, tuple[float, float]] = {}
    top_rows: dict[str, TopRow] = {}
    for f, ft in zip(features, feats, strict=True):
        rt = tube_radius(f.n, 0)
        rad = np.hypot(pos[:, 0] - ft.centre[0], pos[:, 1] - ft.centre[1])
        mine = (inst == f.name) | (inst == f"{f.name}f")
        zone = authored & (rad <= rt + f.radius + 1.0) & (-pos[:, 2] <= f.radius + 1.0)
        dz = d_all[zone]
        # the foot's corners: not a top's seam, not the free band below a ball
        corner = (zone | mine) & ~near_top & tethered
        region = np.zeros(len(inst), dtype=bool)
        if _authored_top(f):
            # the top and its shoulder are judged by the top's five bars,
            # not the foot's bond window
            region = _top_region(
                net.bonds, inst, f.name, f.top, -seed[:, 2], z1s[f.name]
            )
            corner &= ~region
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
        if _authored_top(f):
            _rms, tmax, tpyr = angle_stats(pos, net.bonds, net.rings, atoms=region)
            tops[f.name] = (tmax, tpyr)
            tp = tplans[f.name]
            cols = _top_columns(pos, net.bonds, region, d_all)
            cols["errors"] = _errors_on(findings, inst, _top_names(f.name, f.top))
            top_rows[f.name] = dataclasses.replace(
                tp.chosen,
                atoms=int(region.sum()),
                passes=passes,
                misses=_top_misses(cols, THETA_P_MAX_DEG),
                **cols,
            )
        elif f.top != "open":
            joint = _joint_mask(inst, f, near_top, tethered)
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
        top_plans=tplans,
        top_rows=top_rows,
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
    for f in features:
        _check_top(f)
    ks = {f.name: k_min(f.n) for f in features}
    # an authored top is planned on its own bare tube, once, before the scene
    top_plans = {
        f.name: plan_top(
            f.n,
            f.top,
            top_R=f.top_R,
            top_fillet=f.top_fillet,
            k_tether=k_tether,
        )
        for f in features
        if _authored_top(f)
    }
    tkw: dict[str, Any] = {"top_plans": top_plans} if top_plans else {}
    plan = _relax_scene(sheet, features, ks, extra, k_tether, **tkw)
    redo = {
        f.name: _planned_k(f.n, f.radius, k_tether)
        for f in features
        if not plan.rows[f.name].meets
    }
    if any(ks[name] != k for name, k in redo.items()):
        plan = _relax_scene(sheet, features, {**ks, **redo}, extra, k_tether, **tkw)
    return plan
