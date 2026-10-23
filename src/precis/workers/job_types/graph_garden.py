"""``graph_garden`` job_type — one bounded proposals-only upkeep tick.

The job runs :func:`precis.graph.garden.runner.run_garden_tick` —
deterministic detectors, then one routed LLM verdict per candidate
(``graph_garden:propose`` op, steerable via ``llm.op.*``), then
``verdict='proposed'`` rows into the ``reviews`` ledger (plus a
``rejected`` row per candidate the proposer turned down, so the next run
does not re-ask it). It writes NOTHING else: no merges, splits, prunes,
relinks or tags. After the tick the proposal consumer
(:mod:`precis.graph.garden.consumer`, gr506044) prunes dangling edges and
has Opus approve (merge applied, undoable) or reject (with a reason
code) the pending merge/relink proposals; ``params.consume=false`` turns
it off. A consumer exception lands on ``meta.garden.consume_error``
rather than failing the tick that already wrote its proposals.

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
llm_errors, llm_truncated, skipped_detectors, proposals_by_detector}``
— ``llm_truncated`` is the share of ``llm_errors`` cut on the length
cap (gr504896) — plus
``skipped`` on a gate-skipped
run; a tick that proposes nothing writes no chunks — "zero proposals
and zero log entries" for a clean graph. A tick that skipped a detector
on its deadline writes one summary naming it.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PASS_CHOICES = (
    "merge",
    "split",
    "relink",
    "prune",
    "tag-sweep",
    "siblings",
    "paper-identity",
    "all",
)

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
        # Review-window skip: a candidate whose target was considered and
        # is unchanged (the consider act's expected_sha is its sha now)
        # inside this many days is dropped before the proposer
        # (candidates.CONSIDERED_DAYS; 0 disables).
        "considered_days": {"type": "integer", "minimum": 0},
        "kinds": {"type": "array", "items": {"type": "string"}},
        "tag_namespaces": {"type": "array", "items": {"type": "string"}},
        "tag_kinds": {"type": "array", "items": {"type": "string"}},
        # Declared-exclusive taxon axes for the sibling_confusion
        # detector — provisional input until term-taxonomy v1.5 lands
        # axis declarations on the graph.
        "exclusive_axes": {"type": "array", "items": {"type": "string"}},
        # The proposal consumer after the tick (gr506044): off with
        # false; review_limit bounds the Opus verdicts per run.
        "consume": {"type": "boolean"},
        "review_limit": {"type": "integer", "minimum": 1},
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
            "skipped_rejected": 0,
            "skipped_considered": 0,
            "rejected": 0,
            "rule_rejected": 0,
            "llm_errors": 0,
            "llm_truncated": 0,
            "skipped_detectors": [],
            "proposals_by_detector": {},
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

    from precis.graph.garden.candidates import CONSIDERED_DAYS, DETECTOR_DEADLINE_S
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

    def _nonneg(key: str, default: int) -> int:
        try:
            return max(0, int(params.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _strs(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = params.get(key)
        if not isinstance(raw, list):
            return default
        return tuple(str(x) for x in raw)

    from precis.graph.garden.proposer import propose_llm
    from precis.graph.garden.service import saved_cursors

    prev_cursors = saved_cursors(ctx.store)
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
        cursors=prev_cursors,
        considered_days=_nonneg("considered_days", CONSIDERED_DAYS),
        propose=propose_llm,
    )
    garden: dict[str, Any] = {
        "candidates": outcome.candidates,
        "proposals": outcome.proposals,
        "skipped_pending": outcome.skipped_pending,
        "skipped_rejected": outcome.skipped_rejected,
        "skipped_considered": outcome.skipped_considered,
        "rejected": outcome.rejected,
        "rule_rejected": outcome.rule_rejected,
        "llm_errors": outcome.llm_errors,
        "llm_truncated": outcome.llm_truncated,
        "skipped_detectors": list(outcome.skipped_detectors),
        "proposals_by_detector": dict(outcome.proposals_by_detector),
        # Carried forward: a run over a subset of kinds/passes keeps
        # the other windows where the last run left them.
        "cursors": {**prev_cursors, **outcome.cursors},
    }
    ctx.set_meta(garden=garden)
    consumed = None
    if not outcome.paused and params.get("consume", True) is not False:
        # The proposal consumer (gr506044, Reto 2026-10-23): auto-prune
        # dangling edges, then the Opus review of merge/relink proposals.
        # A consume failure is recorded on meta, not raised — the tick's
        # already-written proposals must not fail with it.
        from precis.graph.garden import consumer

        try:
            consumed = consumer.consume_proposals(
                ctx.store,
                review=consumer.consume_llm,
                review_limit=_ints("review_limit", consumer.DEFAULT_REVIEW_LIMIT),
            )
        except Exception as exc:
            log.warning("graph_garden: consumer raised: %s", exc)
            garden["consume_error"] = f"{type(exc).__name__}: {exc}"
        else:
            garden["consumed"] = consumed.as_meta()
        ctx.set_meta(garden=garden)
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
        if outcome.llm_truncated:
            summary += f" ({outcome.llm_truncated} cut on the length cap)"
        if outcome.skipped_detectors:
            summary += "; skipped detectors (timed out): " + ", ".join(
                outcome.skipped_detectors
            )
        ctx.append_chunk("job_summary", summary)
    if consumed is not None and (
        consumed.pruned or consumed.applied or consumed.rejected or consumed.errors
    ):
        ctx.append_chunk(
            "job_summary",
            f"graph_garden consumer — {consumed.pruned} dangling edge(s) "
            f"pruned, {consumed.applied} merge(s) applied, {consumed.rejected} "
            f"proposal(s) rejected {consumed.reasons}, {consumed.errors} "
            "review error(s)",
        )


#: Bounded retry backoff (gr503581): a failed run mints ONE sibling job,
#: parked ``STATUS:waiting_time`` for this long before the wake_runner
#: re-queues it — a statement-timeout / transient death costs ~this much
#: time, not a full cadence tick. The retry row carries ``meta.retry_of``
#: so its own failure never re-mints: a second failure stays failed,
#: where the cadence watchdog (``watchdog:cadence/graph_garden``) reads
#: stale and surfaces it.
RETRY_BACKOFF_S = 30 * 60


def _on_failed(ctx: Any) -> None:
    """Mint the ONE bounded retry for a failed run (gr503581).

    Called by ``job_inproc`` when this job ended ``STATUS:failed`` via
    its own dispatch. No-op when this job IS already a retry
    (``meta.retry_of`` set — the bound) or when another graph_garden job
    is still pending (a queued tick already covers the next run). The
    retry is a fresh row parented like the failed one, parked
    ``waiting_time`` for :data:`RETRY_BACKOFF_S` — the failed run stays
    failed and visible either way; only the *work* retries.
    """
    if ctx.meta.get("retry_of") is not None:
        return
    from datetime import UTC, datetime, timedelta

    from precis.workers._mint import mint_background_job
    from precis.workers.dispatch import _pending_job_of_type_exists
    from precis.workers.executors._common import WAITING_TIME

    wake = datetime.now(UTC) + timedelta(seconds=RETRY_BACKOFF_S)
    with ctx.store.tx() as conn:
        if _pending_job_of_type_exists(conn, SPEC.name):
            return
        row = conn.execute(
            "SELECT parent_id, prio FROM refs WHERE ref_id = %s", (ctx.ref_id,)
        ).fetchone()
        parent_id = int(row[0]) if row and row[0] is not None else None
        prio = row[1] if row else None
        meta_extra: dict[str, Any] = {
            "retry_of": ctx.ref_id,
            "wake_when": {"kind": "at_time", "payload": {"ts": int(wake.timestamp())}},
        }
        if ctx.meta.get("dispatched_from_todo") is not None:
            meta_extra["dispatched_from_todo"] = ctx.meta["dispatched_from_todo"]
        ref = mint_background_job(
            ctx.store,
            conn=conn,
            job_type=SPEC.name,
            executor="job_inproc",
            title=f"graph_garden (retry of job:{ctx.ref_id})",
            params=dict(ctx.meta.get("params") or {}),
            parent_id=parent_id,
            prio=prio,
            status=WAITING_TIME,
            # A retry is the failed run's own work re-queued: keep its
            # class (a foreground-put run retries as foreground).
            origin=ctx.meta.get("origin"),
            meta_extra=meta_extra,
        )
        ctx.store.append_event(
            ctx.ref_id,
            source="job_inproc",
            event="retry-minted",
            payload={"retry_job_id": int(ref.id), "backoff_s": RETRY_BACKOFF_S},
            conn=conn,
        )
    log.info(
        "graph_garden: job %d failed — minted bounded retry job %d in %ds",
        ctx.ref_id,
        ref.id,
        RETRY_BACKOFF_S,
    )


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
    # Reto ruling (gr506045, 2026-10-23): a host-scoped enabled
    # ``service_config`` row for ``graph_garden`` is a placement pin, not
    # only the activation gate — claims resolve to that host, and while
    # it is down the job waits ``STATUS:queued`` rather than failing.
    service_pin="graph_garden",
    # gr506771: a tick must not enqueue a second run while an earlier
    # graph_garden job is still queued/running — every run proposes
    # over the same graph state, so the backlog only grows.
    dedupe_pending=True,
    # gr503581: a failed run gets one bounded retry after a short
    # backoff; a second failure stays failed and visible.
    on_failed=_on_failed,
    # Reto ruling 2026-10-23, gr508975 option (a): stateless live-scan
    # job types are exempt from the queue TTL. run_garden_tick reads
    # live graph state at claim time, so a long STATUS:queued wait
    # leaves no stale input to reap — the TTL only starved the cadence
    # behind derived_drain, and dedupe_pending already bounds the
    # backlog to one.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
