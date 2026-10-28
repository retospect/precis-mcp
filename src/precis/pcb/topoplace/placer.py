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
    qrot,
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

#: Shove↔legalize post-pass ceiling — a CAP, not the mechanism the loop
#: relies on: ``_settle_pose`` exits on a no-movement pass, on a pass
#: whose residual conflict count did not improve (a shove↔legalize
#: fight, not progress), or on a pose hash already seen (oscillation).
#: The bound only stops a runaway between the two movers.
_POST_PASSES = 50

#: Legality-gate slack: a staged move whose courtyard/containment/edge
#: total exceeds the committed one by more than this is refused. Float
#: delta arithmetic drifts ~1e-12; violations arrive in discrete
#: mm²/mm chunks, so anything above noise is a real increase.
_OV_EPS = 1e-9


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
    # Rigid groups keep their authored shape when seeded: unplaced
    # members of a group take their pose off the group's anchor — from a
    # placed member's pose when one exists, else from a uniformly
    # scattered anchor inside the members' shared legal box (gr520546).
    for members in d.group_members:
        unpl = [m for m in members if unplaced[m]]
        if not unpl:
            continue
        placed = [m for m in members if not unplaced[m]]
        if placed:
            m0 = placed[0]
            arot_q = round(out[m0, 2] - d.group_off[m0, 2]) % 4
            anchor = out[m0, :2] - qrot(d.group_off[m0, :2], arot_q)
        else:
            arot_q = int(rng.integers(0, 4))
            lo = np.maximum.reduce(
                [xy_lo[m] - qrot(d.group_off[m, :2], arot_q) for m in members]
            )
            hi = np.minimum.reduce(
                [xy_hi[m] - qrot(d.group_off[m, :2], arot_q) for m in members]
            )
            if (hi <= lo).any():
                m0 = members[0]
                anchor = (xy_lo[m0] + xy_hi[m0]) / 2.0 - qrot(
                    d.group_off[m0, :2], arot_q
                )
            else:
                anchor = rng.uniform(lo, hi)
        for m in unpl:
            out[m, :2] = anchor + qrot(d.group_off[m, :2], arot_q)
            out[m, 2] = float((arot_q + int(d.group_off[m, 2])) % 4)
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
    can_flip = np.flatnonzero((d.freedoms & FREEDOM_BACK_SIDE).astype(bool) & ~d.fixed)
    # Rigid units (gr520546): an authored group's members move as ONE
    # body — a translate or rotate proposal stages every member, and
    # groups with a fixed member are already all-fixed by
    # ``design_from_ir`` so they never reach a mover. Flips and swaps
    # stay solo: a side flip would need mirroring the authored offsets,
    # and a rigid pair can't trade places member-for-member.
    grouped = {m for members in d.group_members for m in members}
    if len(d.group_of):
        can_flip = can_flip[d.group_of[can_flip] < 0]
    units: list[tuple[int, ...]] = []
    seen: set[int] = set()
    for p in movable.tolist():
        if p in seen:
            continue
        gid = int(d.group_of[p]) if len(d.group_of) else -1
        mates = d.group_members[gid] if gid >= 0 else ()
        unit = tuple(m for m in mates if not d.fixed[m]) or (p,)
        units.append(unit)
        seen.update(unit)
    # a unit may rotate only when every member may rotate
    rot_units = [u for u in units if all(d.freedoms[m] & FREEDOM_ROTATE for m in u)]
    if d.sides in ("top", "bottom"):
        # placement_sides (gr520370): only a part already on the
        # disallowed side is offered the move (its flip comes OFF it) —
        # the per-proposal check below keeps a flipped part from being
        # sent back.
        disallowed = 1.0 if d.sides == "top" else 0.0
        can_flip = can_flip[d.pose0[can_flip, 3] == disallowed]
    sigs = _signatures(d)
    by_sig: dict[tuple[Any, ...], list[int]] = {}
    for p in movable.tolist():
        if p in grouped:
            continue  # a group member can only trade with its own mates
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

    if not units:
        # Nothing can move (every part fixed, no live group): the anneal
        # has no proposal to make — the restart loop below would rebuild
        # the proxy state per restart and report 0 evals anyway
        # (gr524239: ~26 s on taz-leaf's all-fixed board). Settle the
        # authored pose once, score it, and return the honest 0-eval
        # result; ranked_edits still probes unlocks at a lighter effort
        # (summary.py) since that is the only lever left on such a board.
        settled_pose, shoved, legal = _settle_pose(d, d.pose0.copy())
        final = ProxyState(d, settled_pose, objective)
        archive.admit(settled_pose, final.cost)
        return TopoPlaceResult(
            best_pose=settled_pose,
            best_cost=float(final.cost),
            crossings=int(final.crossings),
            length=float(final.length),
            evals=0,
            restarts=0,
            seed=seed,
            elites=archive.elites,
            ladder_scores=tuple(ladder_scores),
            last_improve_eval=0,
            legalize=legal,
            shove=shoved,
        )

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
            # one shared delta per rigid unit — member-wise jitter would
            # tear the authored shape (gr520546)
            for members in d.group_members:
                if len(members) > 1:
                    jit[np.asarray(members, dtype=np.int64)] = jit[int(members[0])]
            pose[:, 0] += jit[:, 0]
            pose[:, 1] += jit[:, 1]
            pose[:, :2] = np.clip(pose[:, :2], xy_lo, xy_hi)
            # the generic clip is per-member and can re-tear a unit at
            # an edge — re-clip the shared delta to the members' common
            # box instead
            for members in d.group_members:
                live = [m for m in members if not d.fixed[m]]
                if len(live) < 2:
                    continue
                mm = np.asarray(live, dtype=np.int64)
                delta = jit[int(members[0])]
                delta = np.clip(
                    delta,
                    (xy_lo[mm] - anchor[mm, :2]).max(axis=0),
                    (xy_hi[mm] - anchor[mm, :2]).min(axis=0),
                )
                pose[mm, :2] = anchor[mm, :2] + delta
            pose[d.fixed] = anchor[d.fixed]

        proxy = ProxyState(d, pose, objective)
        anchor_xy = anchor[:, :2] if anchor is not None else None
        budget = per_restart if restart < n_restarts - 1 else max(1, effort - evals)
        restart_best_pose = proxy.pose.copy()
        restart_best = (proxy.crossings, proxy.cost)
        #: the committed violation total — the legality floor for every
        #: proposal below (a move may reduce debt, never raise it).
        ov_now = float(proxy.overlap)

        # move weights over the options this design actually has —
        # picking a valid move every eval keeps the effort honest.
        opts = [(0.70, "t")]
        if len(rot_units):
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
        if len(units):
            for _ in range(min(30, budget)):
                u = units[rng.integers(len(units))]
                step = max(0.5, step0 * (step_cool**evals))
                delta = np.asarray([rng.normal(0.0, step), rng.normal(0.0, step)])
                if len(u) > 1:
                    midx = np.asarray(u, dtype=np.int64)
                    delta = np.clip(
                        delta,
                        (xy_lo[midx] - proxy.pose[midx, :2]).max(axis=0),
                        (xy_hi[midx] - proxy.pose[midx, :2]).min(axis=0),
                    )
                dd = 0.0
                for m in u:
                    row = proxy.pose[m].copy()
                    row[:2] = np.clip(row[:2] + delta, xy_lo[m], xy_hi[m])
                    dd += proxy.try_move(m, row)
                evals += 1
                dd = abs(dd)
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
                u = units[rng.integers(len(units))]
                step = max(0.5, step0 * (step_cool**evals))
                delta = np.asarray([rng.normal(0.0, step), rng.normal(0.0, step)])
                # a multi-member unit's shared delta is clipped to the
                # members' common box so EVERY member stays in its own
                # legal box — a unit never tears. A solo unit skips it:
                # the per-member clip below is clip(pose+delta),
                # main's move semantics bytewise (gr520546 review).
                if len(u) > 1:
                    midx = np.asarray(u, dtype=np.int64)
                    delta = np.clip(
                        delta,
                        (xy_lo[midx] - proxy.pose[midx, :2]).max(axis=0),
                        (xy_hi[midx] - proxy.pose[midx, :2]).min(axis=0),
                    )
                staged_rows = {}
                for m in u:
                    row = proxy.pose[m].copy()
                    row[:2] = np.clip(row[:2] + delta, xy_lo[m], xy_hi[m])
                    staged_rows[m] = row
            elif mv == "r":
                u = rot_units[rng.integers(len(rot_units))]
                dq = float(rng.choice([1.0, 3.0]))
                if len(u) == 1:
                    p = u[0]
                    row = proxy.pose[p].copy()
                    row[2] = (row[2] + dq) % 4
                    staged_rows = {p: row}
                else:
                    # group rotate: turn the whole rigid body about its
                    # anchor — every member's offset swings by dq and its
                    # own rot advances by the same quarter count.
                    m0 = u[0]
                    arot_q = round(proxy.pose[m0, 2] - d.group_off[m0, 2]) % 4
                    anchor = proxy.pose[m0, :2] - qrot(d.group_off[m0, :2], arot_q)
                    staged_rows = {}
                    legal = True
                    for m in u:
                        new_xy = anchor + qrot(proxy.pose[m, :2] - anchor, int(dq))
                        if not (
                            xy_lo[m, 0] <= new_xy[0] <= xy_hi[m, 0]
                            and xy_lo[m, 1] <= new_xy[1] <= xy_hi[m, 1]
                        ):
                            legal = False
                            break
                        row = proxy.pose[m].copy()
                        row[:2] = new_xy
                        row[2] = (row[2] + dq) % 4
                        staged_rows[m] = row
                    if not legal:
                        continue
            elif mv == "f":
                p = int(can_flip[rng.integers(len(can_flip))])
                row = proxy.pose[p].copy()
                row[3] = 1.0 - row[3]
                # sides='top'/'bottom': only the flip that lands on the
                # allowed side is legal (gr520370).
                if (d.sides == "top" and row[3] >= 0.5) or (
                    d.sides == "bottom" and row[3] < 0.5
                ):
                    continue
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
            if proxy.overlap > ov_now + _OV_EPS:
                # Legality is not a trade. The commit gate refuses any
                # placement with more DRC violations than the stored
                # board, so a move that raises courtyard/containment/
                # edge debt — however good its crossings — strands the
                # whole run (taz-leaf flow runs: elites at cost 0.30 the
                # gate refused). Moves that raise overlap debt are
                # refused outright, temperature or not; overlap
                # reductions and everything else still face Metropolis
                # on the full Δ.
                proxy.revert()
            elif delta < 0 or rng.random() < math.exp(-delta / temp):
                proxy.commit()
                ov_now = float(proxy.overlap)
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
    # A shoved pose re-enters the archive as an elite (spec §4).
    best_pose, shoved, legal = _settle_pose(d, best_pose)
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


def _settle_pose(d: TopoDesign, pose: np.ndarray) -> tuple[np.ndarray, Any, Any]:
    """The shove↔legalize post-pass loop, factored out of
    :func:`topo_place` so its exit conditions are testable.

    Why the loop exists: the overlap share pulls the anneal toward legal
    poses, but the commit gate counts hard collisions as pre-route DRC
    errors — on a dense real board the best pose is still illegal
    (taz15: 147 errors, pcb-loop handoff). A raw anneal pose can be too
    overlapped for the CDT (``build_mesh`` raises ``MeshConflictError``
    and the LP exits ``reason="mesh"`` with 0 rounds — taz15/heater r4),
    so each pass lets legalize's ring-level MTV separate the worst
    first; once the mesh builds, the LP separates every pair at once at
    min |Δ| under the no-inversion guard and legalize mops up what the
    linearisation can't reach.

    Exits on the FIRST of: a pass that moved nothing (fixed point); a
    pass that moved but whose combined residual count did not shrink —
    movement without progress is a fight, not work; a pose hash already
    seen (a 2+-cycle the conflict count can't see); or the
    ``_POST_PASSES`` ceiling — a cap, not a runaway guard.

    Returns ``(pose, merged ShoveResult, merged LegalizeResult)`` where
    ``pose`` is the BEST pose seen — the end-of-pass pose with the
    fewest residual conflicts, not the last one visited (a regressing
    pass does not get to keep the board). ``moved_mm``/``rounds`` sum
    over every pass, ``lp_optimum_mm`` is the FIRST pass's objective
    (the spec's displacement-bound reference), ``reason`` the first
    non-empty stop reason any shove pass reported, ``mesh_errors`` the
    summed mesh-build failure count and ``mesh_error`` the first text
    any pass kept, ``min_tri_area_mm2`` the worst audit any pass saw, and
    ``resolved``/``remaining`` read off the pass that produced the
    returned pose — the state the pose is actually in."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.shove import shove_pose

    shove_mm = legalize_mm = 0.0
    shove_rounds = legalize_rounds = 0
    min_tri = math.inf
    lp_optimum = 0.0
    reason = ""
    mesh_errors = 0
    mesh_error = ""
    seen: set[bytes] = set()
    prev_conflicts: int | None = None
    best_conflicts: int | None = None
    best: tuple[np.ndarray, Any, Any] | None = None
    for _pass in range(_POST_PASSES):
        key = pose.tobytes()
        if key in seen:
            break
        seen.add(key)
        s = shove_pose(d, pose)
        if _pass == 0:
            lp_optimum = s.lp_optimum_mm
        reason = reason or s.reason
        mesh_errors += s.mesh_errors
        mesh_error = mesh_error or s.mesh_error
        if s.moved_mm > 0:
            pose = s.pose
        l = legalize_pose(d, pose)
        pose = l.pose
        shove_mm += s.moved_mm
        legalize_mm += l.moved_mm
        shove_rounds += s.rounds
        legalize_rounds += l.rounds
        min_tri = min(min_tri, s.min_tri_area_mm2)
        if best_conflicts is None or len(l.remaining) < best_conflicts:
            best, best_conflicts = (pose.copy(), s, l), len(l.remaining)
        if s.moved_mm == 0.0 and l.moved_mm == 0.0:
            break
        conflicts = len(s.remaining) + len(l.remaining)
        if prev_conflicts is not None and conflicts >= prev_conflicts:
            break
        prev_conflicts = conflicts
    assert best is not None
    pose, shoved, legal = best
    shoved = dataclasses.replace(
        shoved,
        rounds=shove_rounds,
        moved_mm=shove_mm,
        lp_optimum_mm=lp_optimum,
        min_tri_area_mm2=min_tri,
        reason=reason,
        mesh_errors=mesh_errors,
        mesh_error=mesh_error,
    )
    legal = dataclasses.replace(legal, rounds=legalize_rounds, moved_mm=legalize_mm)
    return pose, shoved, legal


__all__ = ["Ladder", "TopoPlaceResult", "topo_place"]
