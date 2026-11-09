"""Quest replay — the optimisation-benchmark read of a quest's history.

gr608711: quest history was never replayed as an optimisation benchmark, so
the LLM-vs-surrogate proposer debate had no numbers. GOLLuM (pa57193)
reports top-5% coverage and a regret curve per optimisation task; this
module is that same read over the serves graph. Read-only — it never
writes to the store.

The benchmark, on one quest's recorded history:

* **Pool** — every `structure` candidate serving the quest, with its
  *final* measures (:func:`precis.quest.frontier._candidate_from_structure`:
  best converged run + meta stamps). A candidate counts as *measured* when
  any evidence timestamp exists: the first ``result`` logbook entry citing
  it, the first converged energy-bearing ``struct_runs`` row, or — a
  measures-carrying candidate with neither (e.g. a creation-time
  ``atom_cost`` stamp) — its own mint time.
* **Picks** — the LLM proposer's materialised proposals, in mint order:
  the logbook's ``minted [st<N>] from proposal`` lineage entries
  (``meta.section == 'lineage'``, falling back to the entry text). A
  candidate with no lineage entry (a human seed, a struct_search-minted
  design, a pre-lineage import) is pool-but-not-pick, so coverage can
  honestly read < 1. A quest with no lineage entries at all (pre-metadata
  history) falls back to ref-creation order, flagged on the report.
* **Measurement order** — picks sorted by their first-evidence timestamp
  (then evidence id, then pick index). The replay walks that order: at
  each step the candidate's final measured values are revealed — the
  standard replay simplification; it answers "did the proposer front-load
  the eventual winners", not "was a later re-measure predictable".
* **Per rubric axis** (``meta.rubric_objectives``, same resolution as the
  frontier): the **top-5% set** is the best ``top_frac`` share of the
  pool's values on that axis (at least one member). Reported for the LLM:
  **coverage** (|picks ∩ T| / |T|), **hit rate** (|picks ∩ T| / measured
  picks), and the index of the first pick reaching T. For the **random**
  baseline: the same numbers averaged over ``trials`` seeded orderings
  that draw the same number of evaluated picks uniformly without
  replacement from the same pool.
* **Best-so-far / regret** on the primary axis (first objective):
  best-so-far at decile checkpoints of the LLM's measured sequence, the
  final simple regret against the pool's best, and the random baseline's
  mean curve.
* **Duplicate rate** — logbook ``duplicate proposal:`` (content-addressed
  slug hit), ``symmetry-duplicate proposal:`` (canonical-geometry twin)
  and ``rejected proposal:`` (unbuildable spec) observations over named
  proposals (hypothesis ``candidate:`` entries) — the GOLLuM
  "invalid-or-duplicate" number.
* **Frontier coverage** — how much of the current confirmed frontier
  (:func:`precis.quest.frontier.quest_frontier`) sits inside the LLM's
  picks, and the measurement index at which the last such member landed.

Extension point: a baseline proposer is just an ordering over the pool —
a GP-over-text or Optuna comparator slots in beside :func:`_random_trials`
with no schema or data changes. ``random.Random(seed)`` makes the report
deterministic; ``trials`` trades runtime for tighter means (200 draws of
a few hundred candidates is milliseconds).

CLI: ``precis quest replay <id|qu<id>>``; ``scripts/quest-replay``.
"""

from __future__ import annotations

import logging
import math
import random
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.quest.frontier import (
    _candidate_from_structure,
    _objectives_for,
    axis_label_for,
)
from precis.quest.gaps import _live_servers

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``[st336035]`` / bare ``st336035`` mentions inside a logbook entry — the
#: same handle shape :func:`precis.quest.logbook.link_entry_structures`
#: cites, re-derived from text so entries written before link-extraction
#: (or whose link append failed) still count as measurement evidence.
_ST_HANDLE_RE = re.compile(r"\bst(\d+)\b")

#: The logbook text prefixes compute.py writes for proposals that produced
#: no fresh candidate (:mod:`precis.quest.compute`).
_DUP_PREFIX = "duplicate proposal:"
_SYMDUP_PREFIX = "symmetry-duplicate proposal:"
_REJECTED_PREFIX = "rejected proposal:"
_MINTED_RE = re.compile(r"minted \[st(\d+)\]")

#: A hypothesis logbook entry is a proposal exactly when it opens with the
#: ``candidate:`` marker both write paths stamp (the tick's
#: ``candidate: <name>[buildable] — <rationale>`` and
#: :func:`precis.quest.compute._note_minted_candidate`'s fallback entry).
_PROPOSAL_PREFIX = "candidate:"

#: Best-so-far checkpoints (fractions of the measured sequence).
_CHECKPOINTS: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 1.0)


@dataclass(frozen=True)
class AxisReport:
    """One rubric axis's replay numbers (LLM vs the random baseline)."""

    key: str
    sense: str
    pool: int
    top_n: int
    threshold: float
    llm_evaluated: int
    llm_hits: int
    llm_hit_rate: float
    llm_coverage: float
    llm_first_hit: int | None
    llm_curve: list[float | None]
    final_best: float
    llm_final_best: float | None
    random_hits_mean: float
    random_coverage_mean: float
    random_curve_mean: list[float | None]
    random_first_hit_mean: float | None


@dataclass(frozen=True)
class ReplayReport:
    """The full replay receipt — what :func:`render_replay` prints."""

    quest_id: int
    handle: str
    title: str
    objectives: list[tuple[str, str]]
    n_proposals: int
    n_duplicates: int
    n_symmetry_duplicates: int
    n_rejected: int
    n_picks: int
    n_measured_picks: int
    n_pool: int
    n_unattributed: int
    pick_order: str  # "lineage" | "creation"
    axes: list[AxisReport] = field(default_factory=list)
    frontier_total: int = 0
    frontier_captured: int = 0
    frontier_last_pick: int | None = None
    random_frontier_coverage_mean: float | None = None
    top_frac: float = 0.05
    trials: int = 0
    seed: int = 0


def _top_set(
    values: dict[int, float], sense: str, n_top: int
) -> tuple[set[int], float]:
    """The ``n_top`` best-valued candidate ids + the boundary value."""
    ordered = sorted(
        values.items(), key=lambda kv: (kv[1] if sense == "min" else -kv[1], kv[0])
    )
    top = ordered[:n_top]
    return {cid for cid, _ in top}, top[-1][1]


def _checkpoints(seq: list[float], sense: str) -> list[float | None]:
    """Best-so-far at each fraction of ``seq`` (None for an empty seq)."""
    out: list[float | None] = []
    for frac in _CHECKPOINTS:
        upto = max(1, math.ceil(frac * len(seq))) if seq else 0
        window = seq[:upto]
        out.append((min if sense == "min" else max)(window) if window else None)
    return out


def _random_trials(
    rng: random.Random,
    pool: list[int],
    values: dict[int, float],
    sense: str,
    top: set[int],
    draws: int,
    trials: int,
) -> tuple[float, float, list[float | None], float | None]:
    """``trials`` uniform orderings of ``pool``, ``draws`` picks each.

    Returns (mean hits, mean coverage, mean best-so-far curve, mean
    first-hit index). Each trial's curve is checkpointed the same way as
    the LLM's so the means line up against :func:`_checkpoints`.
    """
    if not pool or draws <= 0 or trials <= 0:
        return 0.0, 0.0, [None] * len(_CHECKPOINTS), None
    draws = min(draws, len(pool))
    hits = 0.0
    coverages = 0.0
    first_hits: list[int] = []
    curve_acc = [0.0] * len(_CHECKPOINTS)
    for _ in range(trials):
        sample = rng.sample(pool, draws)
        n_hits = sum(1 for cid in sample if cid in top)
        hits += n_hits
        coverages += n_hits / len(top)
        for hit_idx, cid in enumerate(sample):
            if cid in top:
                first_hits.append(hit_idx + 1)
                break
        vals = [values[cid] for cid in sample]
        for i, frac in enumerate(_CHECKPOINTS):
            upto = max(1, math.ceil(frac * draws))
            curve_acc[i] += (min if sense == "min" else max)(vals[:upto])
    n = float(trials)
    return (
        hits / n,
        coverages / n,
        [v / n for v in curve_acc],
        (sum(first_hits) / len(first_hits)) if first_hits else None,
    )


def quest_replay(
    store: Store,
    quest_id: int,
    *,
    top_frac: float = 0.05,
    trials: int = 200,
    seed: int = 1,
) -> ReplayReport | None:
    """Replay one quest's measured history as an optimisation benchmark.

    ``None`` when the quest does not exist (the CLI prints ``not found``
    like ``precis quest status``). Read-only: a few batched SELECTs, no
    writes.
    """
    quest = store.get_ref(kind="quest", id=quest_id)
    if quest is None:
        return None
    from precis.utils import handle_registry

    objectives = _objectives_for(store, quest_id)
    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    runs_by = store.structure_runs_many([int(s.id) for s in structures])
    cands = {
        int(s.id): _candidate_from_structure(store, s, runs=runs_by[int(s.id)])
        for s in structures
    }
    ref_by_id = {int(s.id): s for s in structures}

    entries = [
        c
        for c in store.chunks.list_chunks_for_ref(quest_id)
        if (c.meta or {}).get("entry_type")
    ]

    # ── proposals / duplicates / lineage / measurement citations ────────
    n_proposals = 0
    n_dup = n_symdup = n_rejected = 0
    lineage: list[tuple[int, int]] = []  # (entry pos, candidate id)
    cited_at: dict[int, tuple[Any, int]] = {}  # cid -> (ts, chunk ord)

    def _note_cite(cid: int, ts: Any, pos: int) -> None:
        prev = cited_at.get(cid)
        if prev is None or (ts, pos) < prev:
            cited_at[cid] = (ts, pos)

    for c in entries:
        meta = c.meta or {}
        etype = meta.get("entry_type")
        text = c.text or ""
        if etype == "hypothesis" and text.startswith(_PROPOSAL_PREFIX):
            n_proposals += 1
        elif etype == "observation":
            if text.startswith(_DUP_PREFIX):
                n_dup += 1
            elif text.startswith(_SYMDUP_PREFIX):
                n_symdup += 1
            elif text.startswith(_REJECTED_PREFIX):
                n_rejected += 1
            if meta.get("section") == "lineage":
                cid = meta.get("candidate_id")
                if isinstance(cid, int) and not isinstance(cid, bool):
                    lineage.append((int(c.ord), cid))
                    continue
            m = _MINTED_RE.search(text)
            if m is not None:
                lineage.append((int(c.ord), int(m.group(1))))
        elif etype == "result":
            for m in _ST_HANDLE_RE.finditer(text):
                cid = int(m.group(1))
                if cid in cands:
                    _note_cite(cid, c.created_at, int(c.ord))

    # ── first-evidence timestamp per candidate ──────────────────────────
    # (ts, seq) — seq is the run id or chunk ord that supplied the ts, so a
    # same-timestamp pair still orders by insertion order, not at random.
    first_evidence: dict[int, tuple[Any, float]] = {}
    for cid, (ts, pos) in cited_at.items():
        first_evidence[cid] = (ts, float(pos))
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, MIN(created_at), MIN(id) FROM struct_runs "
            "WHERE ref_id = ANY(%s) AND converged AND energy IS NOT NULL "
            "GROUP BY ref_id",
            (list(cands) or [-1],),
        ).fetchall()
    for ref_id, ts, run_id in rows:
        cid = int(ref_id)
        cur = first_evidence.get(cid)
        ev = (ts, float(run_id))
        if cur is None or ev < cur:
            first_evidence[cid] = ev
    # A measured candidate with no timestamped evidence (meta-only
    # measures, e.g. a creation-time atom_cost stamp) counts as measured at
    # mint time — the value existed from the proposal onward.
    for cid, candidate in cands.items():
        if cid not in first_evidence and candidate.measures:
            first_evidence[cid] = (ref_by_id[cid].created_at, float("inf"))

    measured_pool = [cid for cid in cands if cid in first_evidence]

    # ── the LLM's pick sequence ─────────────────────────────────────────
    picks: list[int] = []
    pick_order = "lineage"
    seen: set[int] = set()
    for _pos, cid in sorted(lineage):
        if cid in cands and cid not in seen:
            seen.add(cid)
            picks.append(cid)
    unattributed = [cid for cid in cands if cid not in seen]
    if not picks and cands:
        # Pre-lineage history: no minted-receipt metadata — every serving
        # candidate was somebody's pick; creation order is the only order.
        pick_order = "creation"
        picks = sorted(cands, key=lambda cid: (ref_by_id[cid].created_at, cid))
        unattributed = []

    pick_index = {cid: i for i, cid in enumerate(picks)}
    measured_picks = [cid for cid in picks if cid in first_evidence]
    measured_picks.sort(key=lambda cid: (*first_evidence[cid], pick_index[cid]))
    seq_index = {cid: i for i, cid in enumerate(measured_picks)}  # 0-based

    # ── per-axis top-5% coverage / hit rate / best-so-far ───────────────
    rng = random.Random(seed)
    axes: list[AxisReport] = []
    for key, sense in objectives:
        values = {cid: c.measures[key] for cid, c in cands.items() if key in c.measures}
        if not values:
            continue
        n_top = max(1, math.ceil(top_frac * len(values)))
        top, threshold = _top_set(values, sense, n_top)
        llm_seq = [cid for cid in measured_picks if cid in values]
        hits = [cid for cid in llm_seq if cid in top]
        first_hit = (llm_seq.index(hits[0]) + 1) if hits else None
        llm_curve = _checkpoints([values[cid] for cid in llm_seq], sense)
        best = (min if sense == "min" else max)(values.values())
        r_hits, r_cov, r_curve, r_first = _random_trials(
            rng, sorted(values), values, sense, top, len(llm_seq), trials
        )
        axes.append(
            AxisReport(
                key=key,
                sense=sense,
                pool=len(values),
                top_n=n_top,
                threshold=threshold,
                llm_evaluated=len(llm_seq),
                llm_hits=len(hits),
                llm_hit_rate=(len(hits) / len(llm_seq)) if llm_seq else 0.0,
                llm_coverage=(len(hits) / n_top) if n_top else 0.0,
                llm_first_hit=first_hit,
                llm_curve=llm_curve,
                final_best=best,
                llm_final_best=llm_curve[-1] if llm_curve else None,
                random_hits_mean=r_hits,
                random_coverage_mean=r_cov,
                random_curve_mean=r_curve,
                random_first_hit_mean=r_first,
            )
        )

    # ── frontier coverage ───────────────────────────────────────────────
    frontier_total = frontier_captured = 0
    frontier_last: int | None = None
    random_fc: float | None = None
    try:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)
        fset = {c.ref_id for c in fr.frontier}
        frontier_total = len(fset)
        captured = fset & set(picks)
        frontier_captured = len(captured)
        idxs = [seq_index[cid] + 1 for cid in captured if cid in seq_index]
        frontier_last = max(idxs) if idxs else None
        if fset and measured_pool:
            draws = min(len(measured_picks), len(measured_pool))
            cov = 0.0
            for _ in range(trials):
                sample = rng.sample(measured_pool, draws)
                cov += len(set(sample) & fset) / len(fset)
            random_fc = cov / trials if trials else None
    except Exception:
        # Frontier assembly is additive — a degrading read must not sink
        # the replay (it surfaces as frontier_total=0, no section printed).
        log.warning("quest_replay: frontier read failed", exc_info=True)

    return ReplayReport(
        quest_id=quest_id,
        handle=handle_registry.try_format("quest", quest_id) or f"qu{quest_id}",
        title=(quest.title or "").splitlines()[0] if quest.title else "",
        objectives=objectives,
        n_proposals=n_proposals,
        n_duplicates=n_dup,
        n_symmetry_duplicates=n_symdup,
        n_rejected=n_rejected,
        n_picks=len(picks),
        n_measured_picks=len(measured_picks),
        n_pool=len(measured_pool),
        n_unattributed=len(unattributed),
        pick_order=pick_order,
        axes=axes,
        frontier_total=frontier_total,
        frontier_captured=frontier_captured,
        frontier_last_pick=frontier_last,
        random_frontier_coverage_mean=random_fc,
        top_frac=top_frac,
        trials=trials,
        seed=seed,
    )


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_v(v: float | None) -> str:
    return "—" if v is None else f"{v:.4g}"


def render_replay(report: ReplayReport) -> str:
    """The text report — one line per axis per proposer, a curve per axis."""
    r = report
    lines = [
        f"{r.handle} replay — {r.title}",
        "objectives: "
        + " · ".join(f"{axis_label_for(k)} {s}" for k, s in r.objectives),
        (
            f"proposals: {r.n_proposals} named — {r.n_duplicates} duplicate + "
            f"{r.n_symmetry_duplicates} symmetry-duplicate + {r.n_rejected} "
            f"rejected = {_pct((r.n_duplicates + r.n_symmetry_duplicates + r.n_rejected) / r.n_proposals) if r.n_proposals else '—'} "
            "invalid-or-duplicate"
        ),
        (
            f"picks: {r.n_picks} materialised ({r.pick_order} order), "
            f"{r.n_measured_picks} measured; pool {r.n_pool} measured "
            f"candidates"
            + (f" ({r.n_unattributed} not LLM-proposed)" if r.n_unattributed else "")
        ),
    ]
    if not r.axes:
        lines.append("no measured values on any rubric axis — nothing to replay")
        return "\n".join(lines)
    fracs = " / ".join(f"{f:g}" for f in _CHECKPOINTS)
    for ax in r.axes:
        arrow = "▼" if ax.sense == "min" else "▲"
        cmp_op = "<=" if ax.sense == "min" else ">="
        lines.append(
            f"axis {axis_label_for(ax.key)} ({arrow} {ax.sense}) — "
            f"top {r.top_frac * 100:g}% = {ax.top_n} of {ax.pool} "
            f"({ax.key} {cmp_op} {ax.threshold:g})"
        )
        lines.append(
            f"  llm    picks={ax.llm_evaluated} hits={ax.llm_hits} "
            f"hit-rate={_pct(ax.llm_hit_rate)} "
            f"coverage={ax.llm_hits}/{ax.top_n}={_pct(ax.llm_coverage)}"
            + (
                f" first-hit pick #{ax.llm_first_hit}"
                if ax.llm_first_hit is not None
                else " first-hit —"
            )
        )
        lines.append(
            f"  random draws={ax.llm_evaluated} hits={ax.random_hits_mean:.1f} "
            f"coverage={ax.random_coverage_mean * ax.top_n:.1f}/{ax.top_n}="
            f"{_pct(ax.random_coverage_mean)}"
            + (
                f" first-hit ≈#{ax.random_first_hit_mean:.0f}"
                if ax.random_first_hit_mean is not None
                else ""
            )
            + f" (mean of {r.trials} seeded orderings)"
        )
        llm_pts = " → ".join(_fmt_v(v) for v in ax.llm_curve)
        rnd_pts = " → ".join(_fmt_v(v) for v in ax.random_curve_mean)
        regret: float | None = None
        if ax.llm_final_best is not None:
            gap = ax.llm_final_best - ax.final_best
            regret = gap if ax.sense == "min" else -gap
        lines.append(
            f"  best-so-far ({fracs}): llm {llm_pts} | random {rnd_pts} | "
            f"final best {_fmt_v(ax.final_best)}"
            + (f" | llm regret {_fmt_v(regret)}" if regret is not None else "")
        )
    if r.frontier_total:
        lines.append(
            f"frontier (current confirmed): {r.frontier_total} candidates — "
            f"llm captured {r.frontier_captured} "
            f"({_pct(r.frontier_captured / r.frontier_total)})"
            + (
                f", last at pick #{r.frontier_last_pick}"
                if r.frontier_last_pick
                else ""
            )
            + (
                f"; random mean {_pct(r.random_frontier_coverage_mean)}"
                if r.random_frontier_coverage_mean is not None
                else ""
            )
        )
    return "\n".join(lines)


__all__ = [
    "AxisReport",
    "ReplayReport",
    "quest_replay",
    "render_replay",
]
