"""``factory_rescue_tick`` job_type — the dark-factory stall watchdog.

Dark-factory td574946, slice S2: every factory failure ends in done or
rescue. One pass over every plan subtree finds the nodes that went
silent — never dispatched, a cancelled newest job, queued / build /
land / review overrunning its stage deadline
(:data:`precis.workers.factory_rescue.STAGE_DEADLINES`; a queued job
only once its queue stops draining — same-type claims inside the
window are a wait, not a stall, gr625325), a ``sha-in-prod``
gate that never opens, a root closed under an open node, a land's
post-land write that never landed — and sends each to RESCUE through
:func:`precis.workers.factory_rescue.enter_rescue` (one ``rescue:<class>``
tag, one gripe per failure signature). The ``tick-silent`` heartbeat
rescues the recurring ``plan_dispatch`` todo once when the tick ran no
succeeded pass in 30 min. A hold is not a stall: ``waiting-for:``,
``halt``, paused, an open ask, ``rescue:`` or a live ``blocked-by``
target exempt a node; release plans belong to
``release_poll::watch_stuck`` and are skipped. DB-only but for one
host read — the mtime of a live job's session log, the liveness a
``run_slice`` writes progress to (gr626270). The job's target host
persists it as ``meta.log_fresh_at`` each ssh_node poll, so this
unpinned job judges it cluster-wide; a local stat adds it only when
the job's pin says this host may hold the file (td639132). No
checkout, no host capabilities.

Read-only while the kill switch holds (``PRECIS_FACTORY=off`` or a
``service_config`` ``plan_dispatch`` row with ``prio = 0``) or under
``params.dry_run``: the summary lists ``would_rescue`` and nothing is
written. Arming is an operator write — the recurring todo that mints
this job every 5 minutes (recipe:
``docs/runbooks/dark-factory-arming.md`` § Stall watchdog)::

    put(kind='todo', text='factory_rescue_tick (dark factory stall watchdog)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'factory_rescue_tick'})

After the stall pass it runs the rescue pass
(:func:`precis.workers.factory_rescue.run_rescue_pass`): a node carrying
``rescue:<class>`` gets ONE read-only diagnose task on the ``claude-sub``
lane — a ``rescue: REARM`` verdict re-arms it once with a corrected
brief; anything else files one ``ask-user`` todo blocking the node —
except a node under a closed ancestor (``terminal-root``, or any class
with a ``won't-do`` up its parent chain), which the pass closes
``STATUS:won't-do`` with one note instead, a bounded batch of
:data:`precis.workers.factory_rescue.ORPHAN_BATCH` closes a pass
(gr576258).

``dedupe_pending`` coalesces a pass whose previous run is still pending.
Workers never import handlers: every write goes through ``enter_rescue``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from precis.workers.factory_rescue import run_rescue_pass, run_stall_pass
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "factory_rescue_tick"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Run the pass read-only: list what it would rescue, write nothing.
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark-factory stall watchdog: one DB-only pass that sends plan nodes "
    "silent past their stage deadline to rescue (one tag, one gripe per "
    "failure signature), including the tick-silent heartbeat on "
    "plan_dispatch itself — then the diagnose pass: a node in rescue gets "
    "one claude-sub diagnose task, a REARM verdict re-arms it once, else "
    "one ask-user todo — except an orphan under a closed ancestor, "
    "which closes won't-do with no ask. Read-only while the factory "
    "kill switch is on."
)


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: the stall pass, then the rescue
    pass — each in its own try/except so one raising still lets the other
    run (the first failure is recorded, both reported)."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    dry = bool(params.get("dry_run"))
    now = datetime.now(UTC)
    results: dict[str, Any] = {}
    failures: list[str] = []
    for label, fn in (
        ("stall", run_stall_pass),
        ("rescue", run_rescue_pass),
    ):
        try:
            results[label] = fn(ctx.store, now, dry_run=dry)
        except Exception as exc:  # a failed pass must not wedge the lane
            log.warning(
                "factory_rescue_tick: %s pass raised (%s)",
                label,
                type(exc).__name__,
                exc_info=True,
            )
            failures.append(
                f"factory_rescue_tick: {label} pass raised: {type(exc).__name__}: {exc}"
            )
    stall = results.get("stall")
    rescue = results.get("rescue")
    ctx.set_meta(
        factory_rescue=(stall.counts() if stall is not None else {"raised": True}),
        factory_rescue_diagnose=(
            rescue.counts() if rescue is not None else {"raised": True}
        ),
    )
    rendered = [r.render() for r in (stall, rescue) if r is not None]
    rendered += [f"- {f}" for f in failures]
    ctx.append_chunk("job_summary", "\n\n".join(rendered))
    for r in (stall, rescue):
        if r is not None:
            log.info("%s", r.summary())
    if failures:
        ctx.record_failure(failures[0])


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A whole-graph pass: a second run while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live plan state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "load",
]
