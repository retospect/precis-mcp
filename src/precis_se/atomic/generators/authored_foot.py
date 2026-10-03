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

This module sits in ``precis_se`` because it is the layer that already
imports both hexfold and precis_surface; hexfold itself never imports
precis_surface (the tether is a callable).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import numpy as np

from hexfold.build import build
from hexfold.lattice import tube_radius
from hexfold.stick import stick
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


def angle_stats(pos: np.ndarray, bonds: Any, rings: Any) -> tuple[float, float, float]:
    """(rms, max) of |angle - ring ideal| over 3-coordinated corners, and
    the max pyramidalisation (360 - angle sum)."""
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
        if len(ns_set) != 3:
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

    cur = net
    hist: list[float] = []
    pos = seed
    for _ in range(_PASSES):
        pos = np.asarray(stick(cur, tether=tether, k_tether=k_tether), dtype=float)
        cur = dataclasses.replace(net, seed3=tuple(map(tuple, pos)))
        hist.append(float(surface_distance(pos * _FLIP, feat, ds=_DS)[0].mean()))
        if len(hist) > 6 and abs(hist[-1] - hist[-7]) < _SETTLED_A:
            break
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
        passes=len(hist),
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
