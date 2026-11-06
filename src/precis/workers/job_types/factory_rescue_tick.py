"""``factory_rescue_tick`` job_type — the dark-factory stall watchdog.

Dark-factory td574946, slice S2: every factory failure ends in done or
rescue. One pass over every plan subtree finds the nodes that went
silent — never dispatched, a cancelled newest job, queued / build /
land / review overrunning its stage deadline
(:data:`precis.workers.factory_rescue.STAGE_DEADLINES`), a ``sha-in-prod``
gate that never opens, a root closed under an open node, a land's
post-land write that never landed — and sends each to RESCUE through
:func:`precis.workers.factory_rescue.enter_rescue` (one ``rescue:<class>``
tag, one gripe per failure signature). The ``tick-silent`` heartbeat
rescues the recurring ``plan_dispatch`` todo once when the tick ran no
succeeded pass in 30 min. A hold is not a stall: ``waiting-for:``,
``halt``, paused, an open ask, ``rescue:`` or a live ``blocked-by``
target exempt a node; release plans belong to
``release_poll::watch_stuck`` and are skipped. DB-only — no checkout,
no host capabilities.

Read-only while the kill switch holds (``PRECIS_FACTORY=off`` or a
``service_config`` ``plan_dispatch`` row with ``prio = 0``) or under
``params.dry_run``: the summary lists ``would_rescue`` and nothing is
written. Arming is an operator write — the recurring todo that mints
this job every 5 minutes (recipe:
``docs/runbooks/dark-factory-arming.md`` § Stall watchdog)::

    put(kind='todo', text='factory_rescue_tick (dark factory stall watchdog)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'factory_rescue_tick'})

``dedupe_pending`` coalesces a pass whose previous run is still pending.
Workers never import handlers: every write goes through ``enter_rescue``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from precis.workers.factory_rescue import run_stall_pass
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
    "plan_dispatch itself. Read-only while the factory kill switch is on."
)


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one stall pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    now = datetime.now(UTC)
    try:
        res = run_stall_pass(ctx.store, now, dry_run=bool(params.get("dry_run")))
    except Exception as exc:  # a failed pass must not wedge the lane
        log.warning(
            "factory_rescue_tick: pass raised (%s)", type(exc).__name__, exc_info=True
        )
        ctx.record_failure(
            f"factory_rescue_tick: pass raised: {type(exc).__name__}: {exc}"
        )
        return
    ctx.set_meta(factory_rescue=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


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
