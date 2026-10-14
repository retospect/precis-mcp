"""The ``graph_garden`` service gate, cooling check and job mint.

Gate shape mirrors :func:`precis.workers.conflict_search.conflict_search_enabled`:
a ``service_config`` row with ``prio > 0`` on any host lights the
service — dark (no row) means the cadence mints nothing. This runs in
the scheduler process, which is never the host the job lands on, so
"is it lit anywhere" is the right question (per-host resolution is the
claiming lane's business). Never raises: a missing table or a
connection blip reads as dark.

**Cooling** (the spec's "a pass that proposes nothing for N runs cools
itself"): a trailing run of ``DRY_REST_ESCALATE`` succeeded ``graph_garden``
jobs that each wrote zero proposals backs the mint off to
``COOL_INTERVAL_S`` until something changes — the job history *is* the
streak counter (same trick as the quest loop's dry-rest cooldown; no
extra state row). A productive run (proposals > 0) or a failure resets
the streak by construction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

SERVICE_NAME = "graph_garden"
JOB_TYPE = "graph_garden"
JOB_EXECUTOR = "job_inproc"

#: Consecutive zero-proposal succeeded runs that trip the cooled state.
DRY_REST_ESCALATE = 3
#: Mint interval while cooled — weekly instead of daily.
COOL_INTERVAL_S = 7 * 24 * 3600
#: Minted job priority — background maintenance, same as doctor_tick.
_JOB_PRIO = 8


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


def _recent_runs(conn: Any, *, limit: int = 20) -> list[tuple[Any, str | None, int]]:
    """Newest-first ``(created_at, status, proposals)`` of graph_garden jobs."""
    rows = conn.execute(
        "SELECT r.created_at, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) "
        "    AS status, "
        "  COALESCE((r.meta->'garden'->>'proposals')::int, 0) AS proposals "
        "FROM refs r "
        "WHERE r.kind = 'job' AND r.retired_at IS NULL "
        "  AND r.meta->>'job_type' = %s "
        "ORDER BY r.ref_id DESC LIMIT %s",
        (JOB_TYPE, limit),
    ).fetchall()
    return [(r[0], r[1], int(r[2])) for r in rows]


def cooling_active(
    store: Store,
    *,
    escalate_after: int = DRY_REST_ESCALATE,
    cool_interval_s: int = COOL_INTERVAL_S,
) -> bool:
    """True while the cooled mint interval hasn't elapsed: the last
    ``escalate_after`` succeeded runs all proposed nothing AND the newest
    is younger than ``cool_interval_s``."""
    try:
        with store.pool.connection() as conn:
            runs = _recent_runs(conn)
    except Exception:
        log.warning("garden: cooling check failed", exc_info=True)
        return False
    if not runs:
        return False
    streak = 0
    for _at, status, proposals in runs:
        if status == "succeeded" and proposals == 0:
            streak += 1
        else:
            break
    if streak < escalate_after:
        return False
    newest = runs[0][0]
    if newest is None:
        return False
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - newest).total_seconds()
    return age < cool_interval_s


def mint_job(
    store: Store,
    *,
    now: datetime | None = None,
    params: dict[str, Any] | None = None,
) -> int | None:
    """Mint at most one queued ``graph_garden`` job per UTC day.

    Returns the job ref id — the *existing* one when the day already
    minted (the ``idem_key`` guard makes a re-mint a no-op) — or ``None``
    when the service is dark or the pass is cooled (a long enough run of
    empty days backs the cadence off to weekly — see the module
    docstring).
    """
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    idem_key = f"garden:{now:%Y-%m-%d}"
    with store.tx() as conn:
        if not graph_garden_enabled(store, conn=conn):
            return None
        if cooling_active(store):
            return None
        existing = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        from precis.store.types import Tag

        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"graph_garden ({idem_key})",
            meta={
                "job_type": JOB_TYPE,
                "executor": JOB_EXECUTOR,
                "params": dict(params or {}),
                "idem_key": idem_key,
            },
            prio=_JOB_PRIO,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        return int(ref.id)


__all__ = [
    "COOL_INTERVAL_S",
    "DRY_REST_ESCALATE",
    "JOB_EXECUTOR",
    "JOB_TYPE",
    "SERVICE_NAME",
    "cooling_active",
    "graph_garden_enabled",
    "mint_job",
]
