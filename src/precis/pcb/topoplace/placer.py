"""The phase-0 placer (spec §1a): annealed moves over the proxy, a
decaying rubber band toward a restart's anchor elite, and a diverse
elite archive feeding the next restart.

Search shape, all spec-driven:
- moves — translate (Gaussian step), quarter-turn rotate (ROTATE free),
  side flip (BACK_SIDE free; mirrors pin x through the pose convention),
  swap of two parts whose footprint signature matches (the sorted pin
  offsets — TopoDesign carries no footprint id, and equal geometry is
  what "equal footprint" means for a legal swap);
- rubber band — ``E_rb = λ_t Σ_i |p_i − p_anchor,i|²`` normalised by the
  board span² so λ is pose-scale-free; ``λ_t = λ_0 γ^t`` with γ set so
  λ hits 1 % of λ_0 at mid-restart;
- restarts — the eval budget splits over a few restarts, each seeded
  from an archive elite round-robin (first restart: ``pose0``);
- acceptance — Metropolis on Δ(proxy + rubber band); the incumbent is
  ranked by (crossings, cost), crossings primary, because the summary
  and the autoplace comparison both read the crossing count.

``Ladder`` is the hook type only (Decision 10's rung 1 ships here):
each ``(every_n, callable)`` rung fires on the incumbent pose, its
return keys that snapshot's archive admission. Slice 2 registers the
topo-route rung; nothing else interprets the score yet.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.pcb.topoplace.archive import Elite, EliteArchive
from precis.pcb.topoplace.design import (
    FREEDOM_BACK_SIDE,
    FREEDOM_ROTATE,
    ObjectiveShares,
    TopoDesign,
)
from precis.pcb.topoplace.proxy import ProxyState


@dataclass(frozen=True, slots=True)
class Ladder:
    """``rungs=[(every_n, callable)]`` — the cost ladder's hook type.
    ``callable(d, pose) -> float`` is a richer evaluator than the proxy
    (slice 2's topo route); rung 1 is the proxy itself and needs no
    entry — the search already prices every move with it."""

    rungs: tuple[tuple[int, Callable[[TopoDesign, np.ndarray], float]], ...] = ()


@dataclass(frozen=True, slots=True)
class TopoPlaceResult:
    best_pose: np.ndarray  # (N, 4)
    best_cost: float
    crossings: int
    length: float
    evals: int
    restarts: int
    seed: int
    elites: tuple[Elite, ...]
    ladder_scores: tuple[float, ...]  # last score per registered rung
    #: Eval index of the last incumbent improvement — the summary's
    #: plateau signal (Decision 19's "more effort won't help"): a run
    #: whose best stopped moving well before the budget ended has
    #: plateaued (1c spec-call).
    last_improve_eval: int = 0
    #: The legalize post-pass report (``LegalizeResult`` or ``None``
    #: before legalization existed — always populated now). The
    #: summary prints its remaining-pairs count.
    legalize: Any = None
    #: The phase-3 shove-LP report (``ShoveResult``) — runs before
    #: legalize; ``remaining`` in the same (a, b, area) encoding.
    shove: Any = None

    def elites_json(self) -> bytes:
        """Canonical ``elites.json`` bytes — the determinism contract:
        same design + same seed ⇒ identical bytes."""
        rows = [{"cost": e.cost, "pose": e.pose.tolist()} for e in self.elites]
        return json.dumps(
            {"elites": rows}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


def _domain(d: TopoDesign) -> tuple[np.ndarray, np.ndarray]:
    """The mover's board domain as ``(lo, hi)`` corners: the outline
    bbox when there is one, else a part-count square like
    ``autoplace``'s. ``_scatter`` seeds inside it; the translate mover
    is also CLAMPED to it — the proxy prices crossings + length only,
    so without the cap a move that parks a part off the board scores
    well and the placement-DRC gate then refuses the whole run (the
    anneal caps its translate domain the same way:
    ``optimize._derive_placement_bounds``)."""
    if len(d.outline) >= 3:
        return d.outline.min(axis=0), d.outline.max(axis=0)
    side = max(20.0, 5.0 * math.sqrt(max(len(d.parts), 1)))
    return np.zeros(2), np.full(2, side)


#: Extra inset past each part's pin radius — a part whose pins all sit
#: at its centroid still has pads around them (``_EDGE_MARGIN_MM``'s
#: role in ``optimize``; no courtyard extent exists on ``TopoDesign``).
_EDGE_MM = 0.5


def _legal_xy(
    d: TopoDesign, lo: np.ndarray, hi: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-part legal centre box ``(lo_i, hi_i)`` — the domain inset by
    the part's own pin radius + ``_EDGE_MM``, so a clamped centre keeps
    every pin (and roughly its pads) inside the board. A part wider
    than the board collapses its box to the domain centre on the
    degenerate axis rather than inverting it."""
    n = len(d.parts)
    r = np.zeros(n)
    for p in range(n):
        pins = d.pin_xy[d.pin_part == p]
        if len(pins):
            r[p] = float(np.hypot(pins[:, 0], pins[:, 1]).max())
    lo_i = lo + (r + _EDGE_MM)[:, None]
    hi_i = hi - (r + _EDGE_MM)[:, None]
    centre = np.broadcast_to((lo + hi) / 2.0, (n, 2))
    ok = hi_i > lo_i
    return np.where(ok, lo_i, centre), np.where(ok, hi_i, centre)


def _scatter(
    d: TopoDesign,
    pose: np.ndarray,
    rng: np.random.Generator,
    xy_lo: np.ndarray,
    xy_hi: np.ndarray,
) -> np.ndarray:
    """Unplaced (NaN) movable parts get a uniform random position inside
    their legal centre box. Fixed parts keep their coordinates."""
    out = pose.copy()
    unplaced = ~np.isfinite(out[:, 0]) | ~np.isfinite(out[:, 1])
    if not unplaced.any():
        return out
    out[unplaced, :2] = rng.uniform(xy_lo[unplaced], xy_hi[unplaced])
    return out


def _signatures(d: TopoDesign) -> dict[int, tuple[Any, ...]]:
    """part id -> footprint signature for the equal-footprint swap:
    the part's sorted pin offsets. Two parts with identical pin
    geometry can trade places; two without cannot (the swap would move
    their pins differently)."""
    out: dict[int, tuple[Any, ...]] = {}
    for part in range(len(d.parts)):
        pins = d.pin_xy[d.pin_part == part]
        out[part] = tuple(sorted((float(x), float(y)) for x, y in pins))
    return out


def topo_place(
    d: TopoDesign,
    *,
    objective: ObjectiveShares,
    effort: int,
    seed: int,
    archive_size: int = 8,
    ladder: Ladder | None = None,
) -> TopoPlaceResult:
    """Spec §1a signature — phase-0 placer over the proxy metric."""
    rng = np.random.default_rng(seed)
    n_parts = len(d.parts)
    movable = np.flatnonzero(~d.fixed)
    can_rotate = np.flatnonzero((d.freedoms & FREEDOM_ROTATE).astype(bool) & ~d.fixed)
    can_flip = np.flatnonzero((d.freedoms & FREEDOM_BACK_SIDE).astype(bool) & ~d.fixed)
    sigs = _signatures(d)
    by_sig: dict[tuple[Any, ...], list[int]] = {}
    for p in movable.tolist():
        by_sig.setdefault(sigs[p], []).append(p)
    swap_pairs = [
        (g[i], g[j])
        for g in by_sig.values()
        for i in range(len(g))
        for j in range(i + 1, len(g))
    ]

    # board span for rubber-band normalisation + move step scale; the
    # same domain the translate mover is clamped to (per-part legal
    # centre boxes keep each part's pins inside the board — see
    # ``_domain``/``_legal_xy``).
    lo, hi = _domain(d)
    span = float(np.hypot(*(hi - lo)))
    span2 = max(span * span, 1.0)
    xy_lo, xy_hi = _legal_xy(d, lo, hi)

    n_restarts = max(1, min(4, effort // 2500))
    per_restart = max(1, effort // n_restarts)
    lam0 = 0.5
    gamma = 0.01 ** (2.0 / per_restart)  # λ < 1% λ0 at mid-restart
    # step size on its own geometric schedule (autoplace's shape): a
    # third of the span at the start, 0.5 mm at the end of the run.
    step0 = max(0.5, span * 0.35)
    step_cool = (0.5 / step0) ** (1.0 / max(effort, 1))

    archive = EliteArchive(archive_size)
    ladder_rungs = (ladder.rungs if ladder else ()) or ()
    ladder_scores: list[float] = [math.nan] * len(ladder_rungs)

    best_pose: np.ndarray | None = None
    best_rank: tuple[float, float] = (math.inf, math.inf)
    last_improve = 0
    evals = 0

    for restart in range(n_restarts):
        elites = archive.elites
        if restart == 0 or not elites:
            pose = _scatter(d, d.pose0, rng, xy_lo, xy_hi)
            anchor = None
        else:
            anchor = elites[(restart - 1) % len(elites)].pose
            # restart near the anchor: keep the elite pose, jittered
            # (clamped back inside the legal boxes — a jitter may push
            # an edge-sitting centre out)
            pose = anchor.copy()
            jit = rng.normal(0.0, span * 0.02, (n_parts, 2))
            pose[:, 0] += jit[:, 0]
            pose[:, 1] += jit[:, 1]
            pose[:, :2] = np.clip(pose[:, :2], xy_lo, xy_hi)
            pose[d.fixed] = anchor[d.fixed]

        proxy = ProxyState(d, pose, objective)
        anchor_xy = anchor[:, :2] if anchor is not None else None
        budget = per_restart if restart < n_restarts - 1 else max(1, effort - evals)
        restart_best_pose = proxy.pose.copy()
        restart_best = (proxy.crossings, proxy.cost)

        # move weights over the options this design actually has —
        # picking a valid move every eval keeps the effort honest.
        opts = [(0.70, "t")]
        if len(can_rotate):
            opts.append((0.10, "r"))
        if len(can_flip):
            opts.append((0.10, "f"))
        if swap_pairs:
            opts.append((0.10, "s"))
        total_w = sum(w for w, _ in opts)

        # temperature is self-calibrating per restart: the median |Δ| of
        # ~30 probe moves, so T is in the same units as a real move's
        # cost delta whatever the board's crossing scale is (the share
        # normalisation makes a raw constant meaningless).
        probes: list[float] = []
        if len(movable):
            for _ in range(min(30, budget)):
                p = int(movable[rng.integers(len(movable))])
                row = proxy.pose[p].copy()
                step = max(0.5, step0 * (step_cool**evals))
                row[0] += rng.normal(0.0, step)
                row[1] += rng.normal(0.0, step)
                row[0] = np.clip(row[0], xy_lo[p, 0], xy_hi[p, 0])
                row[1] = np.clip(row[1], xy_lo[p, 1], xy_hi[p, 1])
                evals += 1
                dd = abs(proxy.try_move(p, row))
                if dd > 0:
                    probes.append(dd)
                proxy.revert()
        temp0 = float(np.median(probes)) if probes else 1e-4
        temp_cool = (1e-3) ** (1.0 / max(budget, 1))

        for t in range(budget):
            if not len(movable):
                break
            evals += 1
            lam = lam0 * (gamma**t)
            temp = max(temp0 * (temp_cool**t), 1e-9)
            x = rng.random() * total_w
            mv = "t"
            for w, name in opts:
                x -= w
                if x <= 0:
                    mv = name
                    break
            staged_rows: dict[int, np.ndarray] = {}
            if mv == "t":
                p = int(movable[rng.integers(len(movable))])
                row = proxy.pose[p].copy()
                step = max(0.5, step0 * (step_cool**evals))
                row[0] += rng.normal(0.0, step)
                row[1] += rng.normal(0.0, step)
                row[0] = np.clip(row[0], xy_lo[p, 0], xy_hi[p, 0])
                row[1] = np.clip(row[1], xy_lo[p, 1], xy_hi[p, 1])
                staged_rows = {p: row}
            elif mv == "r":
                p = int(can_rotate[rng.integers(len(can_rotate))])
                row = proxy.pose[p].copy()
                row[2] = (row[2] + rng.choice([1.0, 3.0])) % 4
                staged_rows = {p: row}
            elif mv == "f":
                p = int(can_flip[rng.integers(len(can_flip))])
                row = proxy.pose[p].copy()
                row[3] = 1.0 - row[3]
                staged_rows = {p: row}
            else:
                i, j = swap_pairs[rng.integers(len(swap_pairs))]
                row_i = proxy.pose[i].copy()
                row_j = proxy.pose[j].copy()
                row_i[:2], row_j[:2] = row_j[:2].copy(), row_i[:2].copy()
                staged_rows = {i: row_i, j: row_j}

            d_proxy = 0.0
            for p, row in staged_rows.items():
                d_proxy += proxy.try_move(p, row)

            # rubber-band delta over the moved parts' xy
            d_rb = 0.0
            if anchor_xy is not None:
                for p, row in staged_rows.items():
                    d_rb += float(
                        np.sum((row[:2] - anchor_xy[p]) ** 2)
                        - np.sum((proxy.pose[p][:2] - anchor_xy[p]) ** 2)
                    )
                d_rb *= lam / span2
            delta = d_proxy + d_rb
            if delta < 0 or rng.random() < math.exp(-delta / temp):
                proxy.commit()
            else:
                proxy.revert()

            rank = (proxy.crossings, proxy.cost)
            if rank < restart_best:
                restart_best = rank
                restart_best_pose = proxy.pose.copy()
            if rank < best_rank:
                best_rank = rank
                best_pose = proxy.pose.copy()
                last_improve = evals
                # every new incumbent is an archive candidate — the
                # diversity rule decides whether it earns a slot.
                archive.admit(best_pose, best_rank[1])

            for ri, (every_n, fn) in enumerate(ladder_rungs):
                if every_n > 0 and evals % every_n == 0:
                    score = float(fn(d, proxy.pose))
                    ladder_scores[ri] = score
                    archive.admit(proxy.pose, score)

        archive.admit(restart_best_pose, restart_best[1])

    if best_pose is None:
        best_pose = _scatter(d, d.pose0, rng, xy_lo, xy_hi)
        final = ProxyState(d, best_pose, objective)
        best_rank = (final.crossings, final.cost)

    assert best_pose is not None
    # Shove then legalize: the overlap share pulls the anneal toward
    # legal poses, but the commit gate counts hard collisions as
    # pre-route DRC errors — on a dense real board the best pose is
    # still illegal (taz15: 147 errors, pcb-loop handoff). The phase-3
    # LP separates every pair at once at min |Δ| under the mesh's
    # no-inversion guard; legalize's exact MTV + pull-back mops up what
    # the linearisation can't reach. A shoved pose re-enters the
    # archive as an elite (spec §4).
    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.shove import shove_pose

    shoved = shove_pose(d, best_pose)
    if shoved.moved_mm > 0:
        best_pose = shoved.pose
    legal = legalize_pose(d, best_pose)
    if legal.moved_mm > 0:
        best_pose = legal.pose
    final = ProxyState(d, best_pose, objective)
    if shoved.moved_mm > 0:
        archive.admit(best_pose, final.cost)
    best_rank = (final.crossings, final.cost)
    return TopoPlaceResult(
        best_pose=best_pose,
        best_cost=float(best_rank[1]),
        crossings=int(best_rank[0]),
        length=float(final.length),
        evals=evals,
        restarts=n_restarts,
        seed=seed,
        elites=archive.elites,
        ladder_scores=tuple(ladder_scores),
        last_improve_eval=last_improve,
        legalize=legal,
        shove=shoved,
    )


__all__ = ["Ladder", "TopoPlaceResult", "topo_place"]
