"""The ``graph_garden`` service gate and the per-run cooling check.

Gate shape mirrors :func:`precis.workers.conflict_search.conflict_search_enabled`:
a ``service_config`` row with ``prio > 0`` on any host lights the
service — dark (no row) means a minted run skips its pass. Never raises:
a missing table or a connection blip reads as dark.

Both gates are evaluated **per run**, inside the job dispatcher
(:func:`precis.workers.job_types.graph_garden._dispatch`) — not at mint
time. The cadence is an operator-seeded recurring todo
(``meta.schedule`` + ``meta.executor='job_inproc'`` +
``meta.job_type='graph_garden'``, see ``precis-recurring-help``): each
due tick spawns a child the dispatch pass mints a typed job from, and
the generic spawner knows nothing about garden gates. Its per-tick
idempotency (``meta.spawned_for_tick``) and collision-skip (a still-open
previous child skips the new tick) are what used to be the mint's
``idem_key`` and once-daily bound.

**Cooling** (the spec's "a pass that proposes nothing for N runs cools
itself"): a trailing streak of ``DRY_RUNS_ESCALATE`` succeeded
``graph_garden`` runs that each wrote zero proposals cools the pass for
the next ``COOL_RUNS`` runs — a cooled run records
``meta.garden.skipped='cooled'`` and exits instead of scanning. A
productive run (proposals > 0) or a failure resets the streak; an
in-flight (queued/running/waiting) or gate-skipped run is skipped over,
not counted either way. The job history *is* the streak counter — no
extra state row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

SERVICE_NAME = "graph_garden"
JOB_TYPE = "graph_garden"
JOB_EXECUTOR = "job_inproc"

#: Consecutive zero-proposal succeeded runs that trip the cooled state.
#: 36 = the old 3-day rest, re-denominated at the 2-hour recurring tick
#: (12 runs/day).
DRY_RUNS_ESCALATE = 36
#: Runs the cooled state holds for once tripped — 84 = the old weekly
#: mint interval at the same 12-runs-a-day cadence.
COOL_RUNS = 84

#: STATUS values that settle a run as evidence for the streak — anything
#: else (queued/running/waiting_*) is in-flight and counts neither way.
_FINISHED_STATUSES = frozenset({"succeeded", "failed"})


def graph_garden_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``graph_garden`` anywhere."""

    def _q(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (SERVICE_NAME,),
        ).fetchone()
        return row is not None

    try:
        if conn is not None:
            return _q(conn)
        with store.pool.connection() as c:
            return _q(c)
    except Exception:
        log.warning("garden: service_config lookup failed", exc_info=True)
        return False


def _recent_runs(
    conn: Any, *, limit: int = DRY_RUNS_ESCALATE + COOL_RUNS + 8
) -> list[tuple[Any, str | None, int, str | None]]:
    """Newest-first ``(created_at, status, proposals, skipped)`` of
    graph_garden jobs. ``skipped`` is the gate marker
    (:data:`precis.workers.job_types.graph_garden` stamps
    ``meta.garden.skipped`` on a dark/cooled run); ``None`` on a real
    run."""
    rows = conn.execute(
        "SELECT r.created_at, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) "
        "    AS status, "
        "  COALESCE((r.meta->'garden'->>'proposals')::int, 0) AS proposals, "
        "  r.meta->'garden'->>'skipped' AS skipped "
        "FROM refs r "
        "WHERE r.kind = 'job' AND r.retired_at IS NULL "
        "  AND r.meta->>'job_type' = %s "
        "ORDER BY r.ref_id DESC LIMIT %s",
        (JOB_TYPE, limit),
    ).fetchall()
    return [(r[0], r[1], int(r[2]), None if r[3] is None else str(r[3])) for r in rows]


def cooling_active(
    store: Store,
    *,
    escalate_after: int = DRY_RUNS_ESCALATE,
    cool_runs: int = COOL_RUNS,
) -> bool:
    """True while the cooled run-count hasn't elapsed: the trailing streak
    of succeeded zero-proposal runs is at least ``escalate_after`` AND
    fewer than ``cool_runs`` gate-skipped runs have landed on top of it.

    ``skipped`` counts only runs newer than the streak's newest finished
    run (``dry == 0`` still): a skipped run older than a finished dry run
    belongs to an earlier cooling cycle and must not count — the window
    re-arms from the last real run, matching the retired time-based
    cooldown. An in-flight status (queued/running/waiting_*) is skipped
    over — it is evidence neither of dryness nor of a finished run.
    """
    try:
        with store.pool.connection() as conn:
            runs = _recent_runs(conn)
    except Exception:
        log.warning("garden: cooling check failed", exc_info=True)
        return False
    dry = 0
    skipped_head = 0
    for _at, status, proposals, skipped in runs:
        if skipped is not None:
            if dry == 0:
                skipped_head += 1
            continue
        if status not in _FINISHED_STATUSES:
            continue
        if status == "succeeded" and proposals == 0:
            dry += 1
        else:
            break
    return dry >= escalate_after and skipped_head < cool_runs


__all__ = [
    "COOL_RUNS",
    "DRY_RUNS_ESCALATE",
    "JOB_EXECUTOR",
    "JOB_TYPE",
    "SERVICE_NAME",
    "cooling_active",
    "graph_garden_enabled",
]
