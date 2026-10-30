"""Surface-Pourbaix slices 4+5 — per-rung staircase masks, the honest
operating window, and Shapley attribution of the lost window as a field.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(the index of Reto's transfer prompt — §7.4–7.5 for masks/toggles,
§8.1–8.4 for Shapley and its reductions). Pure numpy + dataclasses: no
engine import, no DB, no views — the layer between slice 2's resting map
and whatever renders or optimises on top.

**Input contract** (what the parallel engine export will emit):

- :class:`Rung` — one staircase step: ``delta_g0`` in eV at U = 0 V_RHE,
  ``n_e`` proton-electron pairs **consumed** by the step (positive for a
  reduction step, negative for an oxidation, ``0`` for a chemical or
  desorption step; *every* rung is pH-flat on the RHE scale under CHE), a
  ``kind`` label, an optional viability tolerance ``eps_eV`` (a step may
  be slightly endergonic and still run) and an optional propagated error
  bar ``sigma`` in eV.
- :class:`Route` — a name plus the ordered rungs of one catalytic cycle.
- :class:`RestingMap` — the ``(U_RHE, pH)`` grid slice 2 produces: two
  strictly-increasing axes and a boolean "wanted termination present"
  mask, ``wanted[i_ph, i_u]``.
- optional HER competitor — itself a :class:`Route`; the window
  condition is that the main route's limiting step at U beats HER's
  limiting step by a margin ``m``.

**Semantics.** A rung is *viable* at ``(U, pH)`` when
``delta_g(U) = delta_g0 + n_e * U_RHE <= eps`` (``eU`` in eV per pair —
the engine's convention, ``G_node(U) = G_node(0) + n_H_rel * eU``: a
reduction gets easier at more negative U).
Every rung projects a *forbidden region* (the complement). The honest
operating window (§7.4) is ``resting ∩ all active rung masks ∩ HER
mask``; toggles are the set of active rung names, so all-off = the
resting map (plus HER, if supplied — it is a constraint, not a rung) and
all-on = full catalytic reality. The HER comparison uses *all* rungs of
each route, not the active subset: a toggled-off rung still exists on
the physical staircase — the toggle only lifts its constraint shading.

**Shapley (§8).** Players are the active rungs; the characteristic
function is the *lost window*: at a point ``p`` in the baseline
(resting ∩ HER), ``L_p(S) = 1`` iff some rung in ``S`` forbids ``p``;
globally ``L(S)`` is its grid-measure. Leave-one-out undercounts
overlapping constraints, so credit is split the Shapley way. At one
point the game is a pure OR over the rungs forbidding it, so the exact
value is ``1/|F_p|`` each — :func:`shapley_field` returns that closed
form (identical to enumerating all ``2^N`` subsets pointwise), which is
why the field stays exact at any ``N``. The *global* (area) game is a
weighted sum of OR games, not itself an OR game: it is enumerated over
all ``2^N`` subsets for ``N <= 16`` and permutation-sampled (seeded,
with a warning) above that; sampling an ordering's marginals keeps the
values summing exactly to the total lost area. Per linearity of Shapley
in the game, the exact global values equal the area-weighted field sum —
a built-in cross-check. **Never sum the fields across rungs** (§8.4):
the sum just returns the lost-window map. No helper here does it; use
:func:`reduce_field`'s reductions instead.

Uncertainty bookkeeping follows the same module's rule as
``surface_pourbaix``: ``sigma`` marks where a rung's boundary is
uncertain (the band ``|delta_g - eps| <= sigma``), it never moves the
mask — remedy: add an anchor.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .surface_pourbaix import PREFAC_V, u_she

#: Default viability tolerance (eV) for a chemical (``n_e = 0``) rung: a
#: U-flat step this far uphill is still thermally surmountable at room
#: temperature, so it never masks the whole grid on its own (gr489164,
#: Reto 2026-10-15: "I like the 0.3 eV thing"). A quest's
#: ``window_objectives.eps_eV`` or a rung's own ``eps_eV`` overrides it.
DEFAULT_EPS_EV = 0.3

#: Default viability tolerance (eV) for an electrochemical rung: it must
#: be downhill at U, ``delta_g <= 0`` — U itself is the lever.
DEFAULT_EPS_ELECTRO_EV = 0.0

#: Largest rung count for exact ``2^N`` Shapley enumeration; above it the
#: global values come from seeded permutation sampling with a warning.
EXACT_SHAPLEY_MAX_RUNGS = 16

#: Orderings sampled for the global Shapley values when N is too large.
DEFAULT_SHAPLEY_SAMPLES = 512

#: Rung labels. All kinds mask identically; the label is provenance for
#: renderers ("her" marks steps of the competitor route).
RUNG_KINDS = ("electrochemical", "chemical", "desorption", "her")


@dataclass(frozen=True)
class Rung:
    """One staircase step. ``delta_g0`` eV at U = 0 V_RHE; ``n_e``
    proton-electron pairs consumed (positive for a reduction step,
    negative for an oxidation, 0 = chemical/desorption, U-flat). ``sigma``
    is the propagated error bar on ``delta_g0`` — it marks an uncertain
    band, never moves the mask.

    Slice-7 input slots (``reset_contour``'s estimator ladder reads
    them): ``bep`` is the edge's Brønsted–Evans–Polanyi fit
    ``(alpha, beta)`` for ``E_a = alpha * dG + beta``, ``barrier_ea`` a
    measured forward barrier in eV. Both optional, both ``None`` when the
    pathway recorded no kinetics — the edge then stays on the flat
    overdrive proxy."""

    name: str
    delta_g0: float
    n_e: int
    kind: str = "electrochemical"
    eps_eV: float | None = None
    sigma: float | None = None
    bep: tuple[float, float] | None = None
    barrier_ea: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in RUNG_KINDS:
            raise ValueError(
                f"rung {self.name!r}: kind must be one of {RUNG_KINDS}, "
                f"got {self.kind!r}"
            )

    @property
    def eps(self) -> float:
        if self.eps_eV is not None:
            return float(self.eps_eV)
        return DEFAULT_EPS_EV if self.n_e == 0 else DEFAULT_EPS_ELECTRO_EV

    def delta_g(self, u_rhe: Any) -> np.ndarray:
        """``delta_g0 + n_e * U_RHE`` in eV; pH-flat on the RHE scale.
        Positive ``n_e`` (a reduction) goes downhill at negative U."""
        return self.delta_g0 + self.n_e * np.asarray(u_rhe, dtype=float)

    def viable(self, u_rhe: Any) -> np.ndarray:
        return self.delta_g(u_rhe) <= self.eps

    @property
    def u_star(self) -> float | None:
        """U_RHE where ``delta_g = eps`` — the rung's boundary line:
        viable for ``U <= u_star`` when ``n_e > 0``, ``U >= u_star`` when
        ``n_e < 0``; ``None`` for a chemical step, which is viable
        everywhere or nowhere."""
        if self.n_e == 0:
            return None
        return (self.eps - self.delta_g0) / self.n_e

    def u_star_she(self, ph: float) -> float | None:
        """The boundary on the SHE scale at the given pH (display only —
        the map lives on RHE)."""
        return None if self.u_star is None else u_she(self.u_star, ph)


@dataclass(frozen=True)
class Route:
    """An ordered list of rungs forming one catalytic cycle (or the HER
    competitor). Rung names are unique within a route."""

    name: str
    rungs: tuple[Rung, ...] = ()

    def __post_init__(self) -> None:
        names = [r.name for r in self.rungs]
        if len(set(names)) != len(names):
            raise ValueError(f"route {self.name!r}: duplicate rung names")

    def rung(self, name: str) -> Rung:
        for r in self.rungs:
            if r.name == name:
                return r
        raise KeyError(f"route {self.name!r} has no rung {name!r}")

    def limiting_g(self, u_rhe: Any) -> np.ndarray:
        """``max`` over rungs of ``delta_g(U)`` — the limiting step's free
        energy; ``-inf`` for an empty route (nothing limits it)."""
        u = np.asarray(u_rhe, dtype=float)
        if not self.rungs:
            return np.full(u.shape, -math.inf)
        return np.maximum.reduce([r.delta_g(u) for r in self.rungs])


def _ends(name: str) -> tuple[str, str] | None:
    """``(reactant, product)`` of an ``"a->b"`` rung name, else ``None``."""
    a, sep, b = name.partition("->")
    return (a, b) if sep and a and b else None


def _n_h_star(state: str) -> int:
    """Adsorbed H atoms in a state name — the ``H`` terms of ``"X+H+H"``."""
    return state.split("+").count("H")


def pcet_route(route: Route) -> Route:
    """``route`` in the one-rung CHE convention: each electrochemical H*
    adsorption rung (``X -> X+H``, the engine's Volmer half) merged with
    the chemical rung right after it that consumes that H*
    (``X+H -> XH``) into one PCET rung ``X -> XH`` — ``delta_g0`` and
    ``n_e`` summed (gr489164; Nørskov CHE, 10.1021/jp047349j).

    The engine splits every proton-coupled step so the hydrogenation can
    carry a thermal barrier; for viability that split puts all the U
    dependence into H* binding and leaves the chemistry U-flat, so one
    uphill hydrogenation masks the whole grid. Masks, the operating
    window and ``U_L`` read this route; the reset contour and the
    rendered staircase keep the split (the barrier lives on the chemical
    half). A merged rung's ``sigma`` is the hypot of its halves' — an
    upper bound, since they share the ``X+H`` state; it carries no
    ``bep``/``barrier_ea``. A route with nothing to merge comes back
    unchanged."""
    out: list[Rung] = []
    rungs = route.rungs
    i = 0
    while i < len(rungs):
        r = rungs[i]
        nxt = rungs[i + 1] if i + 1 < len(rungs) else None
        e1 = _ends(r.name)
        e2 = None if nxt is None else _ends(nxt.name)
        if (
            nxt is not None
            and e1 is not None
            and e2 is not None
            and r.n_e > 0
            and nxt.n_e == 0
            and nxt.kind == "chemical"
            # A step into a ``~`` landing is never a PCET follow-on, even
            # mid-route where its label is "chemical" (Reto 2026-10-16:
            # the desorption label is terminal-only, masks unchanged).
            and "~" not in e2[1]
            and e1[1] == e2[0]
            and _n_h_star(e1[1]) > _n_h_star(e1[0])
            and _n_h_star(e2[1]) < _n_h_star(e2[0])
        ):
            sig = [x.sigma for x in (r, nxt) if x.sigma is not None]
            out.append(
                Rung(
                    name=f"{e1[0]}->{e2[1]}",
                    delta_g0=r.delta_g0 + nxt.delta_g0,
                    n_e=r.n_e,
                    kind=r.kind,
                    eps_eV=r.eps_eV,
                    sigma=math.hypot(*sig) if sig else None,
                )
            )
            i += 2
            continue
        out.append(r)
        i += 1
    if len(out) == len(rungs):
        return route
    return Route(name=route.name, rungs=tuple(out))


def u_limiting(route: Route) -> float | None:
    """``U_L = -max(delta_g0 / n_e)`` over the route's reduction rungs —
    the most positive U at which every one of them is downhill. ``None``
    without a reduction rung."""
    vals = [r.delta_g0 / r.n_e for r in route.rungs if r.n_e > 0]
    return -max(vals) if vals else None


def _axis_weights(axis: np.ndarray) -> np.ndarray:
    """Per-point cell measure along one strictly-increasing axis (midpoint
    edges; the outer edges extend half a step past the ends)."""
    n = axis.size
    if n == 1:
        return np.ones(1)
    edges = np.empty(n + 1)
    edges[1:n] = 0.5 * (axis[:-1] + axis[1:])
    edges[0] = axis[0] - 0.5 * (axis[1] - axis[0])
    edges[n] = axis[-1] + 0.5 * (axis[-1] - axis[-2])
    return np.diff(edges)


@dataclass(frozen=True, eq=False)
class RestingMap:
    """The ``(U_RHE, pH)`` grid with the wanted-termination mask slice 2
    emits. Axes strictly increasing; ``wanted[i_ph, i_u]`` boolean."""

    u_rhe: Any
    ph: Any
    wanted: Any

    def __post_init__(self) -> None:
        u = np.asarray(self.u_rhe, dtype=float)
        p = np.asarray(self.ph, dtype=float)
        w = np.asarray(self.wanted, dtype=bool)
        for name, a in (("u_rhe", u), ("ph", p)):
            if a.ndim != 1 or a.size == 0:
                raise ValueError(f"{name} must be a non-empty 1-D axis")
            if a.size > 1 and not np.all(np.diff(a) > 0):
                raise ValueError(f"{name} must be strictly increasing")
        if w.shape != (p.size, u.size):
            raise ValueError(
                f"wanted must have shape (n_ph, n_u) = {(p.size, u.size)}, "
                f"got {w.shape}"
            )
        object.__setattr__(self, "u_rhe", u)
        object.__setattr__(self, "ph", p)
        object.__setattr__(self, "wanted", w)

    @classmethod
    def full(cls, u_rhe: Any, ph: Any) -> RestingMap:
        """A resting map wanted everywhere (the rung masks do all the
        carving) — the common test and default case."""
        u = np.asarray(u_rhe, dtype=float)
        p = np.asarray(ph, dtype=float)
        return cls(u, p, np.ones((p.size, u.size), dtype=bool))

    @property
    def shape(self) -> tuple[int, int]:
        return self.wanted.shape

    def u_grid(self) -> np.ndarray:
        """``U_RHE`` broadcast to the grid, ``(n_ph, n_u)``."""
        return np.broadcast_to(self.u_rhe, self.shape)

    def cell_areas(self) -> np.ndarray:
        """Per-point measure in V·pH, ``(n_ph, n_u)``: each grid point owns
        a cell bounded by the axis midpoints, extending half a step beyond
        the ends — so on a uniform grid every point weighs ``dU * dpH``
        and the total measure is ``n_u*dU × n_ph*dpH`` (fractions are cell
        fractions). Supports nonuniform axes."""
        return np.outer(_axis_weights(self.ph), _axis_weights(self.u_rhe))

    @property
    def total_area(self) -> float:
        return float(self.cell_areas().sum())

    def area(self, mask: np.ndarray) -> float:
        """Grid measure of ``mask`` in V·pH."""
        return float((np.asarray(mask, dtype=bool) * self.cell_areas()).sum())

    def fraction(self, mask: np.ndarray) -> float:
        """``area(mask)`` as a fraction of the whole grid."""
        return self.area(mask) / self.total_area


@dataclass(frozen=True, eq=False)
class RungMask:
    """One rung evaluated on a grid: where it is viable, where it is
    forbidden (the complement — the region the toggle projects), and the
    ``sigma`` band where the boundary could move (uncertainty source 2;
    never moves the mask)."""

    rung: Rung
    viable: np.ndarray
    uncertain: np.ndarray

    @property
    def forbidden(self) -> np.ndarray:
        return ~self.viable


def rung_mask(rung: Rung, resting: RestingMap) -> RungMask:
    dg = rung.delta_g(resting.u_grid())
    viable = dg <= rung.eps
    if rung.sigma is None:
        uncertain = np.zeros(viable.shape, dtype=bool)
    else:
        uncertain = np.abs(dg - rung.eps) <= float(rung.sigma)
    return RungMask(rung=rung, viable=viable, uncertain=uncertain)


def her_mask(
    resting: RestingMap, route: Route, her: Route, margin: float = 0.0
) -> np.ndarray:
    """Viable where the main route's limiting step beats HER's by at
    least ``margin`` eV: ``limiting_main + margin <= limiting_her``.

    The limiting step is the max ``delta_g`` over **all** rungs of each
    route — toggles lift constraint shading, they don't change the
    staircase's physics (module docstring). An empty main route is never
    HER-limited; an empty HER route is a caller error."""
    if not her.rungs:
        raise ValueError("the HER competitor route has no rungs")
    u = resting.u_grid()
    return route.limiting_g(u) + float(margin) <= her.limiting_g(u)


def _active_rungs(route: Route, active: Iterable[str] | None) -> tuple[Rung, ...]:
    """Resolve the toggle set to rungs in route order; ``None`` = all on."""
    if active is None:
        return tuple(route.rungs)
    names = set(active)
    known = {r.name for r in route.rungs}
    unknown = sorted(names - known)
    if unknown:
        raise ValueError(f"route {route.name!r}: unknown active rung(s) {unknown}")
    return tuple(r for r in route.rungs if r.name in names)


@dataclass(frozen=True, eq=False)
class Window:
    """The operating window for one toggle set, with its provenance.
    ``baseline`` is resting ∩ HER — the constraint floor the rungs carve
    from; ``lost`` is the baseline area the active rungs exclude."""

    mask: np.ndarray
    baseline: np.ndarray
    lost: np.ndarray
    active_rungs: tuple[str, ...]
    rung_masks: tuple[RungMask, ...]
    her: str | None
    her_margin: float | None
    area: float
    fraction: float
    baseline_area: float
    resting_area: float


def operating_window(
    resting: RestingMap,
    route: Route,
    active: Iterable[str] | None = None,
    *,
    her: Route | None = None,
    her_margin: float = 0.0,
) -> Window:
    """``resting.wanted ∩ HER mask ∩ every active rung's viable mask``.

    ``active`` is the toggle set of rung names (route order is kept);
    ``None`` activates all rungs, ``()`` none — the two extremes of
    §7.5."""
    rungs = _active_rungs(route, active)
    baseline = resting.wanted.copy()
    if her is not None:
        baseline &= her_mask(resting, route, her, her_margin)
    masks = tuple(rung_mask(r, resting) for r in rungs)
    mask = baseline.copy()
    for m in masks:
        mask &= m.viable
    return Window(
        mask=mask,
        baseline=baseline,
        lost=baseline & ~mask,
        active_rungs=tuple(r.name for r in rungs),
        rung_masks=masks,
        her=None if her is None else her.name,
        her_margin=None if her is None else float(her_margin),
        area=resting.area(mask),
        fraction=resting.fraction(mask),
        baseline_area=resting.area(baseline),
        resting_area=resting.area(resting.wanted),
    )


@dataclass(frozen=True, eq=False)
class ShapleyField:
    """Per-rung Shapley values at every grid point plus the global
    (lost-area) values and the inputs' provenance.

    ``field[i]`` is rung ``rungs[i]``'s share of the lost window at each
    point — the shares sum to ``lost`` there (the test is in
    ``tests/test_staircase_masks.py``). ``global_values[i]`` is the same
    attribution over the whole map in V·pH: exact for ``N <= 16``, seeded
    permutation sampling above (``global_exact`` False, ``n_samples``
    set)."""

    route: str
    rungs: tuple[str, ...]
    field: np.ndarray
    lost: np.ndarray
    window: np.ndarray
    baseline: np.ndarray
    global_values: np.ndarray
    global_exact: bool
    n_samples: int | None
    seed: int | None
    u_rhe: np.ndarray
    ph: np.ndarray
    her: str | None
    her_margin: float | None
    lost_area: float
    lost_fraction: float
    window_area: float
    window_fraction: float
    baseline_area: float
    grid_area: float

    def at(self, u_rhe: float, ph: float) -> dict[str, float]:
        """Shapley at one condition — the nearest grid cell's per-rung
        values (they sum to the lost window there, 0 or 1)."""
        j = int(np.argmin(np.abs(self.u_rhe - float(u_rhe))))
        i = int(np.argmin(np.abs(self.ph - float(ph))))
        return {name: float(self.field[k, i, j]) for k, name in enumerate(self.rungs)}


def _shapley_exact(lost_area_by_subset: np.ndarray, n: int) -> np.ndarray:
    """Exact Shapley values for the scalar game ``L(S)`` given for all
    ``2^n`` subsets (index = bitmask over the players)."""
    fact = math.factorial
    coef = np.array(
        [fact(k) * fact(n - k - 1) / fact(n) if 0 <= k < n else 0.0 for k in range(n)]
    )
    masks = np.arange(1 << n)
    popcount = np.array([int(m).bit_count() for m in masks])
    phi = np.zeros(n)
    for i in range(n):
        without = masks[(masks & (1 << i)) == 0]
        phi[i] = float(
            (
                coef[popcount[without]]
                * (
                    lost_area_by_subset[without | (1 << i)]
                    - lost_area_by_subset[without]
                )
            ).sum()
        )
    return phi


def _subset_lost_areas(
    killer_bits: np.ndarray, weights: np.ndarray, n: int
) -> np.ndarray:
    """``L(S)`` for every subset ``S`` (bitmask index): ``killer_bits[p]``
    is the bitmask of rungs forbidding baseline point ``p``, ``weights``
    its cell area. Compressed on the unique killer patterns — usually a
    handful even on a large grid."""
    uniq, inv = np.unique(killer_bits, return_inverse=True)
    w = np.bincount(inv, weights=weights, minlength=uniq.size)
    out = np.zeros(1 << n)
    for m in range(1, 1 << n):
        out[m] = float(w[(uniq & m) != 0].sum())
    return out


def _shapley_sampled(
    pattern_weights: dict[int, float], n: int, n_samples: int, seed: int | None
) -> np.ndarray:
    """Permutation sampling of the Shapley sum: each ordering contributes
    the players' marginals along it, which sum to the total lost window —
    so the estimate stays efficient at any sample count."""
    rng = np.random.default_rng(seed)
    phi = np.zeros(n)
    for _ in range(n_samples):
        s = 0
        prev = 0.0
        for i in rng.permutation(n):
            s |= 1 << int(i)
            cur = sum(w for k, w in pattern_weights.items() if k & s)
            phi[int(i)] += cur - prev
            prev = cur
    return phi / n_samples


def shapley_field(
    resting: RestingMap,
    route: Route,
    active: Iterable[str] | None = None,
    *,
    her: Route | None = None,
    her_margin: float = 0.0,
    n_samples: int = DEFAULT_SHAPLEY_SAMPLES,
    seed: int | None = None,
) -> ShapleyField:
    """The Shapley field over the grid plus the global lost-area values.

    At each baseline point the game is an OR over the rungs forbidding
    it, so ``field[i, p] = 1/|F_p|`` for ``i`` in the forbidding set —
    exact for any ``N`` (module docstring). The global game is enumerated
    over ``2^N`` subsets for ``N <= 16`` and permutation-sampled above
    that (``seed``, warning)."""
    win = operating_window(resting, route, active, her=her, her_margin=her_margin)
    rungs = win.rung_masks
    n = len(rungs)
    baseline = win.baseline
    if n:
        forbidden = np.stack([m.forbidden for m in rungs])  # (N, n_ph, n_u)
    else:
        forbidden = np.zeros((0,) + resting.shape, dtype=bool)
    n_forbid = forbidden.sum(axis=0)
    share = np.divide(
        baseline.astype(float),
        n_forbid,
        out=np.zeros(resting.shape),
        where=n_forbid > 0,
    )
    field = forbidden * share[None]
    lost = win.lost

    sel = baseline.ravel()
    if n == 0 or not sel.any():
        global_values = np.zeros(n)
        exact = True
        used_samples: int | None = None
    elif n <= EXACT_SHAPLEY_MAX_RUNGS:
        kill = np.zeros(resting.shape, dtype=np.int64).ravel()
        for i in range(n):
            kill |= forbidden[i].ravel().astype(np.int64) << i
        lost_by_subset = _subset_lost_areas(
            kill[sel], resting.cell_areas().ravel()[sel], n
        )
        global_values = _shapley_exact(lost_by_subset, n)
        exact = True
        used_samples = None
    else:
        warnings.warn(
            f"shapley_field: {n} active rungs > {EXACT_SHAPLEY_MAX_RUNGS} — "
            f"global Shapley values estimated from {n_samples} sampled "
            f"orderings (seed={seed}); the per-point field stays exact.",
            stacklevel=2,
        )
        kill = np.zeros(resting.shape, dtype=object).ravel()
        for i in range(n):
            bit = 1 << i
            for p in np.flatnonzero(forbidden[i].ravel()):
                kill[p] = (kill[p] or 0) | bit
        weights = resting.cell_areas().ravel()
        patterns: dict[int, float] = {}
        for p in np.flatnonzero(sel):
            k = kill[p] or 0
            patterns[k] = patterns.get(k, 0.0) + float(weights[p])
        global_values = _shapley_sampled(patterns, n, n_samples, seed)
        exact = False
        used_samples = int(n_samples)

    return ShapleyField(
        route=route.name,
        rungs=win.active_rungs,
        field=field,
        lost=lost,
        window=win.mask,
        baseline=baseline,
        global_values=global_values,
        global_exact=exact,
        n_samples=used_samples,
        seed=seed,
        u_rhe=resting.u_rhe,
        ph=resting.ph,
        her=win.her,
        her_margin=win.her_margin,
        lost_area=resting.area(lost),
        lost_fraction=resting.fraction(lost),
        window_area=win.area,
        window_fraction=win.fraction,
        baseline_area=win.baseline_area,
        grid_area=resting.total_area,
    )


@dataclass(frozen=True)
class RungFieldStats:
    """One rung's Shapley field collapsed over the grid (§8.4: collapse
    over space, keep the steps). Mean/variance are per grid cell;
    ``area`` is the global Shapley value in V·pH."""

    rung: str
    max: float
    mean: float
    var: float
    area: float


@dataclass(frozen=True, eq=False)
class Reductions:
    """The Shapley field's reductions (§8.4), with provenance.

    ``sumsq`` = per-point ``sum_i phi_i^2`` (raw — "big and concentrated
    is the worst of all"). ``ipr`` = ``(sum_i phi_i)^2 / sumsq`` per
    point — the inverse participation ratio read as an **effective
    number of bottlenecks**: 1 when one rung owns the loss, ~N when blame
    is shared; 0 where nothing is lost. ``dominant`` is the argmax-rung
    identity map: a rung index, ``-2`` where the max is tied between
    rungs (per-point shares are ``1/|F_p|``, so every shared cell ties
    exactly — picking one by index would make the map and the churn a
    rung-order artefact), ``-1`` where nothing is lost. ``churn`` is the
    fraction of grid cells whose dominant rung differs from a
    dominant-holding neighbour — tie and no-loss cells are excluded as
    both cell and neighbour — the ridge-density fragility scalar.
    ``smooth_max`` is the optional log-sum-exp at ``temperature`` — the
    differentiable worst-step proxy (tau → 0 = max), masked to 0 where
    nothing is lost."""

    sumsq: np.ndarray
    ipr: np.ndarray
    ipr_mean: float
    dominant: np.ndarray
    dominant_names: tuple[str, ...]
    churn: float
    per_rung: tuple[RungFieldStats, ...]
    smooth_max: np.ndarray | None
    temperature: float | None
    route: str
    rungs: tuple[str, ...]
    global_exact: bool
    n_samples: int | None
    seed: int | None
    shape: tuple[int, int]
    u_window: tuple[float, float]
    ph_window: tuple[float, float]
    her: str | None
    her_margin: float | None
    lost_area: float
    window_area: float
    window_fraction: float


def _churn(dominant: np.ndarray) -> float:
    """Fraction of cells holding a single dominant rung that have a
    4-neighbour holding a *different* single dominant rung. Tie cells
    (-2) and no-loss cells (-1) are excluded as both cell and neighbour —
    a one-rung field has churn 0 wherever its boundary runs."""
    d = dominant
    differs = np.zeros(d.shape, dtype=bool)
    has = d >= 0
    for sl_a, sl_b in (
        ((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
        ((slice(1, None), slice(None)), (slice(None, -1), slice(None))),
    ):
        a, b = d[sl_a], d[sl_b]
        diff = has[sl_a] & has[sl_b] & (a != b)
        differs[sl_a] |= diff
        differs[sl_b] |= diff
    return float(differs.sum()) / float(d.size)


def reduce_field(sf: ShapleyField, temperature: float | None = None) -> Reductions:
    """Slice-5 reductions of a :class:`ShapleyField` (§8.4)."""
    f = sf.field
    total = f.sum(axis=0)  # = sf.lost, computed inside the reduction
    sumsq = (f * f).sum(axis=0)
    ipr = np.divide(total * total, sumsq, out=np.zeros(sumsq.shape), where=sumsq > 0)
    w = np.outer(_axis_weights(sf.ph), _axis_weights(sf.u_rhe))
    lost_w = float(w[sumsq > 0].sum())
    ipr_mean = float((ipr * w)[sumsq > 0].sum() / lost_w) if lost_w > 0 else 0.0
    if f.shape[0]:
        max_share = f.max(axis=0)
        tied = (f == max_share[None]).sum(axis=0) > 1
        dominant = np.where(
            sumsq > 0, np.where(tied, -2, np.argmax(f, axis=0)), -1
        ).astype(np.int64)
    else:
        dominant = np.full(sumsq.shape, -1, dtype=np.int64)
    per_rung = tuple(
        RungFieldStats(
            rung=name,
            max=float(f[k].max()),
            mean=float(f[k].mean()),
            var=float(f[k].var()),
            area=float(sf.global_values[k]),
        )
        for k, name in enumerate(sf.rungs)
    )
    smooth = None
    if temperature is not None:
        if temperature <= 0:
            raise ValueError("temperature must be > 0 (tau -> 0 is the max)")
        if f.shape[0]:
            m = f.max(axis=0)
            smooth = temperature * (
                np.log(np.exp((f - m[None]) / temperature).sum(axis=0))
                + m / temperature
            )
            smooth = np.where(total > 0, smooth, 0.0)
        else:
            smooth = np.zeros(sf.lost.shape)
    return Reductions(
        sumsq=sumsq,
        ipr=ipr,
        ipr_mean=ipr_mean,
        dominant=dominant,
        dominant_names=sf.rungs,
        churn=_churn(dominant),
        per_rung=per_rung,
        smooth_max=smooth,
        temperature=temperature,
        route=sf.route,
        rungs=sf.rungs,
        global_exact=sf.global_exact,
        n_samples=sf.n_samples,
        seed=sf.seed,
        shape=sf.lost.shape,
        u_window=(float(sf.u_rhe[0]), float(sf.u_rhe[-1])),
        ph_window=(float(sf.ph[0]), float(sf.ph[-1])),
        her=sf.her,
        her_margin=sf.her_margin,
        lost_area=sf.lost_area,
        window_area=sf.window_area,
        window_fraction=sf.window_fraction,
    )


def render_text(sf: ShapleyField, red: Reductions) -> str:
    """One block of prose for later views: the window, the per-rung
    Shapley ranking, the effective-bottleneck count and the ridge churn."""
    lines: list[str] = []
    lo_u, hi_u = float(sf.u_rhe[0]), float(sf.u_rhe[-1])
    lo_p, hi_p = float(sf.ph[0]), float(sf.ph[-1])
    est = (
        ""
        if sf.global_exact
        else f" (sampled, {sf.n_samples} orderings, seed={sf.seed})"
    )
    lines.append(
        f"operating window of route {sf.route!r} over {lo_u:+.2f}…{hi_u:+.2f} V_RHE "
        f"× pH {lo_p:g}…{hi_p:g}: {sf.window_area:.3f} V·pH "
        f"({sf.window_fraction:.1%} of the grid, baseline {sf.baseline_area:.3f} V·pH"
        + (
            f", HER competitor {sf.her!r} at margin {sf.her_margin:g} eV"
            if sf.her
            else ""
        )
        + f"); lost {sf.lost_area:.3f} V·pH{est}."
    )
    order = np.argsort(-sf.global_values)
    for k in order:
        share = sf.global_values[k] / sf.lost_area if sf.lost_area > 0 else 0.0
        lines.append(
            f"  {sf.rungs[k]}: {sf.global_values[k]:.3f} V·pH of lost window "
            f"({share:.0%})"
        )
    lines.append(
        f"effective bottlenecks {red.ipr_mean:.2f} (1 = one rung owns the loss, "
        f"~{len(sf.rungs)} = blame shared); ridge churn {red.churn:.1%} of grid cells."
    )
    return "\n".join(lines)


#: The corners of the attribution sensitivity probe: ``0`` the stored sweep,
#: ``-1``/``+1`` the opposed anchor shifts (slice 8 contract, stage 4).
PROBE_CORNERS = (-1, 0, 1)

#: The verdict strings :func:`attribution_verdict` can return — exactly these.
VERDICT_AMBIGUOUS = "ambiguous"
VERDICT_NOT_AMBIGUOUS = "not ambiguous under this probe"
VERDICT_NO_RUNNER_UP = "no runner-up"
VERDICT_NO_LOST_AREA = "no lost area: attribution undefined"

#: Two Shapley values this close are the same value (float noise from the
#: subset sums), for the corner-0 tie and all-zero tests.
_PHI_RTOL = 1e-9
_PHI_ATOL = 1e-12


@dataclass(frozen=True)
class AttributionVerdict:
    """Outcome of :func:`attribution_verdict`.

    ``verdict`` is one of :data:`VERDICT_AMBIGUOUS`,
    :data:`VERDICT_NOT_AMBIGUOUS`, :data:`VERDICT_NO_RUNNER_UP`,
    :data:`VERDICT_NO_LOST_AREA`. ``top`` / ``runner_up`` are rung indices
    at corner 0 (``None`` where undefined). ``ranges[r]`` is rung ``r``'s
    ``(min, max)`` φ across the corners that had a wanted region (``()``
    when corner 0 is unusable). ``order_by_corner[c]`` is whether
    ``φ_c[top] > φ_c[runner_up]`` (``None`` for a corner with an empty
    wanted region): all True with ``ambiguous`` means the ranges overlap
    without the order ever flipping; a False is a real top-rung flip."""

    verdict: str
    reason: str
    top: int | None
    runner_up: int | None
    ranges: tuple[tuple[float, float], ...]
    order_by_corner: dict[int, bool | None]


def attribution_verdict(
    phi_by_corner: Mapping[int, Sequence[float] | None],
    rung_names: Sequence[str] | None = None,
) -> AttributionVerdict:
    """Is the top Shapley rung's attribution ambiguous under the opposed-
    corner anchor probe (slice 8 contract, stage 4 "Verdict rule")?

    ``phi_by_corner`` maps the corners ``-1``, ``0``, ``+1`` to the per-rung
    Shapley lost-area vector (one value per rung, same rung order at every
    corner); ``None`` means the wanted region was empty at that corner.
    ``rung_names`` only words the ``reason``.

    Rule, with ``R_r = [min_c φ_c[r], max_c φ_c[r]]``, ``r*`` the top rung at
    corner 0 and ``r2`` the runner-up: **ambiguous** if the wanted region is
    empty at corner ``-1`` or ``+1`` (checked first, before any other
    outcome), or if ``min R_{r*} <= max R_{r2}``; otherwise "not ambiguous
    under this probe". Degenerate inputs, in this order after the empty
    check: a single rung gives "no runner-up" (no verdict claimed); corner-0
    φ all zero gives "no lost area: attribution undefined"; a tie for top at
    corner 0 gives ambiguous. Ties (to float noise) for runner-up take the
    lowest index; ``top`` on a tie is also the lowest tied index. The
    overlap rule is conservative: ``order_by_corner`` says whether ``r*``
    really lost to ``r2`` anywhere.

    A sensitivity probe over the propagated anchor bars, not a confidence
    interval. Raises ``ValueError`` when a corner is missing, corner 0 is
    ``None`` or empty, or vector lengths / ``rung_names`` disagree."""
    missing = [c for c in PROBE_CORNERS if c not in phi_by_corner]
    if missing:
        raise ValueError(
            f"attribution_verdict: corner(s) {missing} missing from "
            f"phi_by_corner (need {list(PROBE_CORNERS)})"
        )
    if phi_by_corner[0] is None:
        raise ValueError(
            "attribution_verdict: corner 0 is None — the unshifted wanted "
            "region is empty, so there is no attribution to probe"
        )
    phi: dict[int, np.ndarray | None] = {
        c: (None if phi_by_corner[c] is None else np.asarray(phi_by_corner[c], float))
        for c in PROBE_CORNERS
    }
    p0 = phi[0]
    assert p0 is not None
    n = int(p0.size)
    if p0.ndim != 1 or n == 0:
        raise ValueError("attribution_verdict: corner 0 needs a non-empty 1-D vector")
    for c, p in phi.items():
        if p is not None and (p.ndim != 1 or p.size != n):
            raise ValueError(
                f"attribution_verdict: corner {c} has shape {p.shape}, "
                f"corner 0 has {n} rungs"
            )
    if rung_names is not None and len(rung_names) != n:
        raise ValueError(
            f"attribution_verdict: {len(rung_names)} rung_names for {n} rungs"
        )

    def name(i: int | None) -> str:
        if i is None:
            return "none"
        return f"{rung_names[i]!r}" if rung_names is not None else f"rung {i}"

    stack = np.stack([p for p in (phi[c] for c in PROBE_CORNERS) if p is not None])
    ranges = tuple(
        (float(stack[:, r].min()), float(stack[:, r].max())) for r in range(n)
    )

    top: int | None = None
    runner_up: int | None = None
    order: dict[int, bool | None] = {c: None for c in PROBE_CORNERS}

    def done(verdict: str, reason: str) -> AttributionVerdict:
        return AttributionVerdict(verdict, reason, top, runner_up, ranges, order)

    def lowest_at(v: float, among: list[int]) -> int:
        return next(
            i for i in among if np.isclose(p0[i], v, rtol=_PHI_RTOL, atol=_PHI_ATOL)
        )

    if not np.allclose(p0, 0.0, rtol=0.0, atol=_PHI_ATOL):
        everyone = list(range(n))
        top = lowest_at(float(p0.max()), everyone)
        if n > 1:
            rest = [i for i in everyone if i != top]
            v2 = float(max(p0[i] for i in rest))
            # A tie for top leaves a tied rung in ``rest``, so v2 == p0[top].
            runner_up = lowest_at(v2, rest)
            for c in PROBE_CORNERS:
                p = phi[c]
                if p is not None:
                    order[c] = bool(p[top] > p[runner_up])

    empty = [c for c in (-1, 1) if phi[c] is None]
    if empty:
        return done(
            VERDICT_AMBIGUOUS,
            f"the wanted region is empty at corner {', '.join(f'{c:+d}' for c in empty)}",
        )
    if n == 1:
        return done(VERDICT_NO_RUNNER_UP, "a single rung: nothing to rank it against")
    if top is None:
        return done(
            VERDICT_NO_LOST_AREA,
            "corner-0 φ is zero for every rung: the window loses no area",
        )
    assert runner_up is not None
    if np.isclose(p0[top], p0[runner_up], rtol=_PHI_RTOL, atol=_PHI_ATOL):
        return done(
            VERDICT_AMBIGUOUS,
            f"{name(top)} and {name(runner_up)} tie for top at corner 0",
        )
    lo_top = ranges[top][0]
    hi_run = ranges[runner_up][1]
    flipped = [c for c in PROBE_CORNERS if order[c] is False]
    if lo_top <= hi_run:
        how = (
            f"the order flips at corner {', '.join(f'{c:+d}' for c in flipped)}"
            if flipped
            else "the order holds at every corner but the ranges overlap"
        )
        return done(
            VERDICT_AMBIGUOUS,
            f"min φ({name(top)}) = {lo_top:.4g} <= max φ({name(runner_up)}) = "
            f"{hi_run:.4g}; {how}",
        )
    return done(
        VERDICT_NOT_AMBIGUOUS,
        f"min φ({name(top)}) = {lo_top:.4g} > max φ({name(runner_up)}) = {hi_run:.4g}",
    )


__all__ = [
    "DEFAULT_EPS_ELECTRO_EV",
    "DEFAULT_EPS_EV",
    "DEFAULT_SHAPLEY_SAMPLES",
    "EXACT_SHAPLEY_MAX_RUNGS",
    "PREFAC_V",
    "PROBE_CORNERS",
    "RUNG_KINDS",
    "VERDICT_AMBIGUOUS",
    "VERDICT_NOT_AMBIGUOUS",
    "VERDICT_NO_LOST_AREA",
    "VERDICT_NO_RUNNER_UP",
    "AttributionVerdict",
    "Reductions",
    "RestingMap",
    "Route",
    "Rung",
    "RungFieldStats",
    "RungMask",
    "ShapleyField",
    "Window",
    "attribution_verdict",
    "her_mask",
    "operating_window",
    "pcet_route",
    "reduce_field",
    "render_text",
    "rung_mask",
    "shapley_field",
    "u_limiting",
    "u_she",
]
