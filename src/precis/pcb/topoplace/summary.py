"""topo-place 1c: the Decision-19 one-screen summary and its ranked edits.

The summary is what the LLM outer loop reads between rounds (Decision 17):
a verdict, best-vs-baseline numbers, run statistics, plateau and elite
agreement, then **ranked edits** — each computed by trying it, never
predicted. Slice-1 fields that only a later rung can price (via bracket,
corridor overflow, certificates) print ``n/a (slice N)`` rather than a
made-up number.

Ranked edits in slice 1 (spec §1c): unlock a fixed part, rotate / flip a
crossing-involved part, and widen a flex slot's ``any_of`` to the pins
its ``pin_functions`` table names equivalent. Pose edits re-run
``topo_place`` at 10 % of the run's effort, seeded from the best elite
(``dataclasses.replace`` on the frozen ``TopoDesign.pose0``); a widen is
priced by re-solving ``assign_pins`` on the best pose — ``net_pins`` is
frozen, so the placer has no wire to re-score anyway (1c spec-call).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np

from precis.pcb.topoplace import proxy as _proxy
from precis.pcb.topoplace.design import (
    FREEDOM_BACK_SIDE,
    FREEDOM_ROTATE,
    ObjectiveShares,
    TopoDesign,
)
from precis.pcb.topoplace.pairmap import PairMap
from precis.pcb.topoplace.pins import PinAssignment, assign_pins
from precis.pcb.topoplace.placer import TopoPlaceResult, topo_place
from precis.pcb.topoplace.proxy import all_pin_world, evaluate
from precis.pcb.topoplace.writeback import summary_table

#: Elite-agreement tolerance — the archive's own ``d_min`` (a part whose
#: elite poses all sit inside it is "stable"; anything else needs hints).
_AGREE_MM = 2.0


@dataclass(frozen=True)
class RankedEdit:
    """One tried edit (Decision 19): the one-line action and its
    measured crossing delta — negative is an improvement."""

    text: str
    delta: int


def _wire_model(
    d: TopoDesign, pose: np.ndarray
) -> tuple[np.ndarray, list[tuple[int, int]], np.ndarray]:
    """Flat MST airwires at ``pose`` plus each wire's two endpoint parts
    — the pair a crossing attribute needs once wires lose pin identity."""
    pw = all_pin_world(d, pose)
    blocks = []
    ends: list[tuple[int, int]] = []
    for np_net in d.net_pins:
        pts = pw[np_net]
        for i, j in _proxy._mst_edges(pts):
            ends.append((int(d.pin_part[np_net[i]]), int(d.pin_part[np_net[j]])))
        blocks.append(_proxy._net_wires(d, np_net, pw))
    wires, nets = _proxy._concat(blocks, list(range(len(d.net_pins))))
    return wires, ends, nets


def _intersect(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The proper-straddle intersection point of two (4,) segments."""
    x1, y1, x2, y2 = a
    x3, y3, x4, y4 = b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return (a[:2] + a[2:] + b[:2] + b[2:]) / 4.0
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return np.asarray([x1 + t * (x2 - x1), y1 + t * (y2 - y1)])


def crossing_info(d: TopoDesign, pose: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(points (K,2), per-part counts (N,))`` of the pose's airwire
    crossings — the placement.svg marks and the ranked-edit candidate
    ranking share this one count."""
    wires, ends, nets = _wire_model(d, pose)
    if len(wires) == 0:
        return np.empty((0, 2)), np.zeros(len(d.parts), dtype=np.int64)
    hit = np.argwhere(np.triu(_proxy._cross_matrix(wires, wires, nets, nets), 1))
    counts = np.zeros(len(d.parts), dtype=np.int64)
    pts = []
    for i, j in hit:
        pts.append(_intersect(wires[i], wires[j]))
        for part in (*ends[int(i)], *ends[int(j)]):
            counts[part] += 1
    return (
        np.asarray(pts).reshape(-1, 2) if pts else np.empty((0, 2)),
        counts,
    )


def _elite_pose(result: TopoPlaceResult) -> np.ndarray:
    """The "best elite" the ranked edits re-run from (spec §1c): the
    archive's cheapest, falling back to the incumbent pose when the
    diversity rule crowded it out of the archive."""
    return result.elites[0].pose if result.elites else result.best_pose


def _widen_designs(d: TopoDesign) -> list[tuple[str, TopoDesign]]:
    """Widen-a-slot candidates: a slot whose candidates all share
    ``pin_functions`` entries may admit every pin carrying one of those
    functions (``pins_with`` inverted — 1c spec-call). Skipped when the
    wider set adds nothing."""
    out: list[tuple[str, TopoDesign]] = []
    for i, s in enumerate(d.flex):
        pf = d.pin_functions[s.part] if s.part < len(d.pin_functions) else {}
        if not pf:
            continue
        funcs = {f for p in s.pins for f in pf.get(p, ())}
        if not funcs:
            continue
        widened = sorted(p for p, fs in pf.items() if set(fs) & funcs)
        if set(widened) <= set(s.pins):
            continue
        name = str(s.name) if s.name else f"{d.parts[s.part]}#{i}"
        flex = tuple(
            dataclasses.replace(slot, pins=tuple(widened)) if k == i else slot
            for k, slot in enumerate(d.flex)
        )
        text = (
            f"widen {name} to any {'/'.join(sorted(funcs))} pin "
            f"({len(s.pins)}→{len(widened)})"
        )
        out.append((text, dataclasses.replace(d, flex=flex)))
    return out


def ranked_edits(
    d: TopoDesign,
    result: TopoPlaceResult,
    assignment: PinAssignment,
    pm: PairMap,
    *,
    objective: ObjectiveShares,
    effort: int,
    seed: int,
    limit: int = 8,
    max_tries: int = 12,
) -> list[RankedEdit]:
    """The slice-1 ranked edits, each measured by trying it.

    Candidates, in the order the spec's examples go: unlock fixed parts;
    rotate and flip the parts most involved in the best pose's
    remaining crossings (``_AGREE``-ranked by crossing count); widen
    flex slots. Pose edits seed the re-run at the best elite so the
    delta prices the edit, not a fresh search."""
    base = _elite_pose(result)
    edit_effort = max(1, effort // 10)
    tries: list[tuple[str, TopoDesign | None, TopoDesign | None]] = []

    for part in np.flatnonzero(d.fixed):
        fixed = d.fixed.copy()
        fixed[part] = False
        tries.append(
            (
                f"unlock {d.parts[part]} (lock cost)",
                dataclasses.replace(d, fixed=fixed, pose0=base),
                None,
            )
        )

    _, counts = crossing_info(d, result.best_pose)
    for tried_parts, part in enumerate(np.argsort(-counts)):
        counts_part = int(counts[part])
        if counts_part == 0 or tried_parts >= 5:
            break
        if int(d.freedoms[part]) & int(FREEDOM_ROTATE):
            pose_e = base.copy()
            pose_e[part, 2] = (pose_e[part, 2] + 1.0) % 4.0
            tries.append(
                (
                    f"rotate {d.parts[part]} 90°",
                    dataclasses.replace(d, pose0=pose_e),
                    None,
                )
            )
        if int(d.freedoms[part]) & int(FREEDOM_BACK_SIDE):
            pose_e = base.copy()
            pose_e[part, 3] = 1.0 - pose_e[part, 3]
            side = "bottom" if pose_e[part, 3] >= 0.5 else "top"
            tries.append(
                (
                    f"flip {d.parts[part]} to {side}",
                    dataclasses.replace(d, pose0=pose_e),
                    None,
                )
            )

    for text, d_wide in _widen_designs(d):
        tries.append((text, None, d_wide))

    edits: list[RankedEdit] = []
    for text, d_place, d_assign in tries[:max_tries]:
        if d_place is not None:
            r2 = topo_place(d_place, objective=objective, effort=edit_effort, seed=seed)
            delta = r2.crossings - result.crossings
        else:
            assert d_assign is not None
            a2 = assign_pins(d_assign, result.best_pose)
            delta = a2.crossings - assignment.crossings
        edits.append(RankedEdit(text=text, delta=int(delta)))
    edits.sort(key=lambda e: e.delta)
    return edits[:limit]


def _agreement(d: TopoDesign, result: TopoPlaceResult) -> tuple[int, list[str]]:
    """Elite agreement (Decision 19): a part is STABLE when every elite
    puts it inside ``_AGREE_MM`` of every other with the same rot/side;
    else it is undetermined and wants an intent hint. Returns
    ``(n_stable, undetermined refdes)``."""
    if len(result.elites) < 2:
        return len(d.parts), []
    poses = np.stack([e.pose for e in result.elites])
    undetermined: list[str] = []
    for part in range(len(d.parts)):
        p = poses[:, part, :]
        same_orient = (p[:, 2] == p[0, 2]).all() and (p[:, 3] == p[0, 3]).all()
        spread = float(np.hypot(*(p[:, :2].max(axis=0) - p[:, :2].min(axis=0))))
        if not (same_orient and spread <= _AGREE_MM and np.isfinite(p).all()):
            undetermined.append(str(d.parts[part]))
    return len(d.parts) - len(undetermined), undetermined


def render_summary(
    d: TopoDesign,
    result: TopoPlaceResult,
    assignment: PinAssignment,
    pm: PairMap,
    *,
    objective: ObjectiveShares,
    effort: int,
    seed: int,
    slug: str = "",
    edits: list[RankedEdit] | None = None,
) -> str:
    """The Decision-19 one-screen text (≤ ~40 lines)."""
    from precis.pcb.topoplace.placer import _domain, _legal_xy, _scatter

    pose0 = d.pose0
    if not np.isfinite(pose0[:, :2]).all():
        # Same scatter the placer's first restart draws (fresh
        # ``default_rng(seed)``, same legal centre boxes), so the
        # baseline is the pose the run actually started from, not
        # NaN-ful zeros.
        lo, hi = _domain(d)
        xy_lo, xy_hi = _legal_xy(d, lo, hi)
        pose0 = _scatter(d, pose0, np.random.default_rng(seed), xy_lo, xy_hi)
    base_cross, base_len = evaluate(d, pose0)
    xy = result.best_pose[:, :2]
    finite = np.isfinite(xy).all(axis=1)
    if finite.any():
        span = xy[finite].max(axis=0) - xy[finite].min(axis=0)
    else:
        span = np.zeros(2)

    lines = [f"topo-place — {slug or 'board'}"]
    if result.crossings == 0:
        lines.append("verdict: routable on 2 layers (proxy)")
    else:
        lines.append(f"verdict: {result.crossings} proxy crossing(s) remain")
    lines.append(
        f"crossings {base_cross} → {result.crossings}, airwire "
        f"{base_len:.0f} → {result.length:.0f} mm, "
        f"area {span[0]:.0f}×{span[1]:.0f} mm²"
    )
    lines.append(
        "via bracket: n/a (slice 2); corridor overflow: n/a (slice 4); "
        "certificates: n/a (slice 2)"
    )
    lines.append(
        f"run: {result.evals} evals, {result.restarts} restart(s), seed "
        f"{result.seed}, effort {effort}, objective crossings "
        f"{objective.crossings:.2f}/ratsnest {objective.ratsnest:.2f}"
    )
    if result.elites:
        costs = np.asarray([e.cost for e in result.elites])
        lines.append(
            f"elites: {len(result.elites)} (cost median "
            f"{float(np.median(costs)):.4f}, p90 "
            f"{float(np.percentile(costs, 90)):.4f}); best cost "
            f"{result.best_cost:.4f}"
        )
    frac = result.last_improve_eval / max(result.evals, 1)
    verdict = (
        "more effort won't help"
        if frac < 0.75
        else "still improving at the cutoff — more effort may help"
    )
    lines.append(f"plateau: last improvement {frac:.0%} of evals — {verdict}")
    stable, undetermined = _agreement(d, result)
    tail = ", ".join(undetermined[:8])
    if len(undetermined) > 8:
        tail += f" (+{len(undetermined) - 8} more)"
    lines.append(
        f"elite agreement: {stable} stable, {len(undetermined)} undetermined"
        + (f": {tail}" if tail else "")
    )
    st = pm.stats()
    worst = "; ".join(
        f"{d.parts[s.a]}–{d.parts[s.b]} ×{s.best_crossings}" for s in st.worst[:3]
    )
    lines.append(
        f"pair map: {len(pm.pairs)} pairs, best-crossings mean {st.mean:.2f} "
        f"p95 {st.p95:.0f}" + (f"; worst: {worst}" if worst else "")
    )
    if edits is None:
        edits = ranked_edits(
            d,
            result,
            assignment,
            pm,
            objective=objective,
            effort=effort,
            seed=seed,
        )
    if edits:
        lines.append("ranked edits:")
        lines += [f"  {e.text}: {e.delta:+d} crossings" for e in edits]
    if d.flex or d.claims or assignment.swaps:
        lines.append(summary_table(d, assignment).rstrip("\n"))
    return "\n".join(lines) + "\n"


__all__ = [
    "RankedEdit",
    "crossing_info",
    "ranked_edits",
    "render_summary",
]
