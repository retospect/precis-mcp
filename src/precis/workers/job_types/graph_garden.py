"""``graph_garden`` job_type — one bounded proposals-only upkeep tick.

The job runs :func:`precis.graph.garden.runner.run_garden_tick` —
deterministic detectors, then one routed LLM verdict per candidate
(``graph_garden:propose`` op, steerable via ``llm.op.*``), then
``verdict='proposed'`` rows into the ``reviews`` ledger. It writes
NOTHING else: no merges, splits, prunes, relinks or tags — proposals
wait for a human (or a separate, explicit apply verb) to act.

Who mints it: an operator-seeded recurring todo under Watches —
``meta.schedule`` + ``meta.executor='job_inproc'`` +
``meta.job_type='graph_garden'`` (see ``precis-recurring-help``). Each
due tick spawns a subtask the dispatch pass mints this job from; the
spawner's per-tick ``spawned_for_tick`` idempotency and its
collision-skip on a still-open previous child are the run bound. The
gates that used to sit in the cadence mint are evaluated here, per run:
a dark ``graph_garden`` ``service_config`` (absent row = off) or an
active cooling streak writes ``meta.garden.skipped`` and exits — a
dark/cooled run does no scanning and counts as neither a dry run nor a
productive one. A hand ``put(kind='job', job_type='graph_garden',
params={'pass': 'merge'})`` takes the same gates — light the service to
run it.

Outcome contract (the cooling streak reads this): ``meta.garden`` on the
job row carries ``{candidates, proposals, skipped_pending, rejected,
llm_errors, skipped_detectors}`` — plus ``skipped`` on a gate-skipped
run; a tick that proposes nothing writes no chunks — "zero proposals
and zero log entries" for a clean graph. A tick that skipped a detector
on its deadline writes one summary naming it.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PASS_CHOICES = ("merge", "split", "relink", "prune", "tag-sweep", "siblings", "all")

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Which detectors run this tick. "all" (default) runs every
        # pass; a single pass name runs just that slice.
        "pass": {
            "anyOf": [
                {"type": "string", "enum": list(_PASS_CHOICES)},
                {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_PASS_CHOICES)},
                },
            ]
        },
        "limit": {"type": "integer", "minimum": 1},
        "scan_cap": {"type": "integer", "minimum": 1},
        "max_proposals": {"type": "integer", "minimum": 1},
        # Per-detector wall-clock budget in seconds (run_detectors'
        # skip-and-continue; gr503580).
        "detector_deadline_s": {"type": "integer", "minimum": 1},
        "stale_days": {"type": "integer", "minimum": 1},
        "kinds": {"type": "array", "items": {"type": "string"}},
        "tag_namespaces": {"type": "array", "items": {"type": "string"}},
        "tag_kinds": {"type": "array", "items": {"type": "string"}},
        # Declared-exclusive taxon axes for the sibling_confusion
        # detector — provisional input until term-taxonomy v1.5 lands
        # axis declarations on the graph.
        "exclusive_axes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _gate_skip(ctx: Any, reason: str) -> None:
    """Record a gate-skipped run on ``meta.garden`` and return.

    ``skipped`` is the cooling streak's marker: a gate-skipped run is
    neither a dry run nor a productive one
    (:func:`precis.graph.garden.service.cooling_active` reads this key),
    and a ``'cooled'`` value is what counts the cool window down."""
    ctx.set_meta(
        garden={
            "skipped": reason,
            "candidates": 0,
            "proposals": 0,
            "skipped_pending": 0,
            "rejected": 0,
            "llm_errors": 0,
            "skipped_detectors": [],
        }
    )
    log.info("graph_garden: run skipped (%s)", reason)


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """Plugin dispatcher invoked by ``job_inproc`` for a claimed job.

    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`
    (typed ``Any`` like the other job-type modules — test stand-ins and
    the real context both satisfy the used surface: ``.meta``,
    ``.store``, ``.set_meta``, ``.append_chunk``, ``.defer``,
    ``.record_failure``)."""
    from precis.graph.garden.service import cooling_active, graph_garden_enabled

    if not graph_garden_enabled(ctx.store):
        _gate_skip(ctx, "dark")
        return
    if cooling_active(ctx.store):
        _gate_skip(ctx, "cooled")
        return

    params = dict(ctx.meta.get("params") or {})

    raw_pass = params.get("pass", "all")
    passes: tuple[str, ...]
    if isinstance(raw_pass, str):
        passes = (raw_pass,)
    else:
        passes = tuple(str(p) for p in raw_pass)

    from precis.graph.garden.candidates import DETECTOR_DEADLINE_S
    from precis.graph.garden.runner import (
        DEFAULT_LIMIT,
        DEFAULT_MAX_PROPOSALS,
        run_garden_tick,
    )

    def _ints(key: str, default: int) -> int:
        try:
            return max(1, int(params.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _strs(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = params.get(key)
        if not isinstance(raw, list):
            return default
        return tuple(str(x) for x in raw)

    from precis.graph.garden.proposer import propose_llm

    outcome = run_garden_tick(
        ctx.store,
        passes=passes,
        kinds=_strs("kinds", ("taxon", "concept", "memory")),
        limit=_ints("limit", DEFAULT_LIMIT),
        scan_cap=_ints("scan_cap", 2000),
        stale_days=_ints("stale_days", 180),
        tag_namespaces=_strs("tag_namespaces", ("DOMAIN", "TOPIC")),
        tag_kinds=_strs("tag_kinds", ("finding", "paper", "concept", "memory")),
        exclusive_axes=_strs("exclusive_axes", ()),
        max_proposals=_ints("max_proposals", DEFAULT_MAX_PROPOSALS),
        detector_deadline_s=_ints("detector_deadline_s", int(DETECTOR_DEADLINE_S)),
        propose=propose_llm,
    )
    ctx.set_meta(
        garden={
            "candidates": outcome.candidates,
            "proposals": outcome.proposals,
            "skipped_pending": outcome.skipped_pending,
            "rejected": outcome.rejected,
            "llm_errors": outcome.llm_errors,
            "skipped_detectors": list(outcome.skipped_detectors),
        }
    )
    if outcome.paused:
        # Window-scoped LLM pause — park the job (STATUS:waiting_time)
        # so the whole tick re-runs when the window reopens; candidates
        # and the ledger dedup make that safe.
        defer = getattr(ctx, "defer", None)
        if defer is not None and defer(
            None,
            "graph_garden: proposer LLM paused (window-scoped) — "
            "re-queued for a later tick",
        ):
            return
        ctx.record_failure(
            "graph_garden: proposer LLM unavailable — nothing proposed",
            failure_class="infra",
        )
        return
    # A clean tick writes no chunks — the outcome lives in meta.garden
    # only (the "zero log entries" contract). A tick that skipped a
    # detector DOES write: a silent partial run is what gr503580 was.
    if outcome.proposals or outcome.llm_errors or outcome.skipped_detectors:
        summary = (
            "graph_garden — "
            f"{outcome.candidates} candidate(s), {outcome.proposals} proposal(s) "
            f"written to reviews, {outcome.skipped_pending} already pending, "
            f"{outcome.rejected} rejected, {outcome.llm_errors} proposer error(s)"
        )
        if outcome.skipped_detectors:
            summary += "; skipped detectors (timed out): " + ", ".join(
                outcome.skipped_detectors
            )
        ctx.append_chunk("job_summary", summary)


SPEC = JobTypeSpec(
    name="graph_garden",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=(
        "Proposals-only graph upkeep tick: deterministic detectors "
        "(dupes, orphans, stale nodes, dangling/cycle edges, under-tagged "
        "refs, polysemy and sibling-confusion signals), one routed LLM "
        "verdict each, proposals into the reviews ledger — never a graph "
        "write. Minted by the operator-seeded recurring todo; a run skips "
        "while the graph_garden service_config row is dark or a dry "
        "streak cools the pass."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
