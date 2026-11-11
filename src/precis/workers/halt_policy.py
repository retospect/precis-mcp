"""Runner-owned halt classes and the context needed for safe recovery.

An infrastructure failure must remain retryable without relying on the
agent's broken MCP connection to write its own tags. The dispatch recovery
pass releases only parks whose full tick transcript proves retry safety;
unknown outcomes require human review. Permanent task-design failures get
one repair opportunity, retained across later ticks rather than reset by a
successful intermediate tick.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.store.types import Tag
from precis.workers.executors._common import (
    SUCCEEDED,
    append_chunk,
    set_meta,
    set_status,
)
from precis.workers.halt_retry import assess_halt_retry

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store
    from precis.utils.tick_conclusion import TickConclusion

INFRA_RETRY_DELAY = timedelta(minutes=15)
UNCERTAIN_HALT_TAG = "ask-user:uncertain-halt-outcome"
#: ``waiting-for:`` targets the recovery pass owns (``infra``,
#: ``plan-repair``, and the ``outage-<alert>`` cohort prefix). Every
#: release path matches them with a halt-class check a ``blocked`` park
#: never satisfies, and no detector scans them — an agent-named dependency
#: colliding with one would park invisibly forever, so ``handle_halt``
#: mints it outside the managed space instead.
_MANAGED_WAIT_TARGETS = frozenset({"infra", "plan-repair"})


def block_reclaimed_recovery(store: Store, parent_id: int, job_ref_id: int) -> bool:
    """A dead worker supplies no receipt for repeating its recovery attempt.

    Claim bookkeeping increments attempts only when stealing a running
    job, not on the first claim or an ordinary waiting-time wake. Check
    before invoking the planner: auditing its next output is too late to
    prevent an uncertain write from being repeated after a worker crash.
    """
    with store.tx() as conn:
        row = conn.execute(
            "SELECT p.meta->'halt', j.meta FROM refs p JOIN refs j ON j.parent_id = p.ref_id "
            "WHERE p.ref_id = %s AND j.ref_id = %s FOR UPDATE OF p, j",
            (parent_id, job_ref_id),
        ).fetchone()
        if (
            not row
            or not isinstance(row[0], dict)
            or row[0].get("retry_released") is not True
        ):
            return False
        meta = row[1]
        attempts = meta.get("attempts", 0)
        reclaimed = bool(meta.get("reclaims")) or (
            isinstance(attempts, int) and attempts > 0
        )
        if not reclaimed:
            return False
        reason = "reclaimed recovery attempt has no complete prior-attempt audit"
        store.add_tag(
            parent_id, Tag.open(UNCERTAIN_HALT_TAG), set_by="system", conn=conn
        )
        set_meta(conn, job_ref_id, recovery_retry_blocked=reason)
        append_chunk(
            store,
            job_ref_id,
            "job_event",
            f"runner: {reason}; review before retrying",
            conn=conn,
        )
        set_status(store, job_ref_id, SUCCEEDED, conn=conn)
        return True


def snapshot_halt_tags(store: Store, parent_id: int) -> frozenset[str]:
    """Remember pre-existing holds so a tick cannot clear an owner's halt."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' "
            "AND (t.value = 'halt' OR t.value LIKE 'halt:%%')",
            (parent_id,),
        ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _remove_tick_halts(
    store: Store,
    conn: Connection,
    parent_id: int,
    job_ref_id: int,
    previous_halt_tags: frozenset[str],
) -> None:
    """Clear only fresh plan-tick-owned holds, never user/unknown writers.

    Legacy agents may still self-tag before emitting their conclusion. A
    class-based park alongside that hard halt would never recover. Shared
    install principals and NULL legacy attribution cannot identify an owner,
    so their holds stay visible for manual review.
    """
    rows = conn.execute(
        "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
        "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' "
        "AND (t.value = 'halt' OR t.value LIKE 'halt:%%') "
        "AND rt.set_by = 'agent' AND rt.principal = 'worker:plan_tick' "
        "AND rt.created_at >= (SELECT created_at FROM refs WHERE ref_id = %s)",
        (parent_id, job_ref_id),
    ).fetchall()
    for row in rows:
        value = str(row[0])
        if value not in previous_halt_tags:
            store.remove_tag(parent_id, Tag.open(value), conn=conn)


def handle_halt(
    store: Store,
    conn: Connection,
    parent_id: int,
    job_ref_id: int,
    conclusion: TickConclusion,
    raw_stream: str,
    *,
    previous_halt_tags: frozenset[str] = frozenset(),
) -> str:
    """Persist one declared halt; return the park/halt tag that was applied.

    Both parent and job retain the receipt. The parent is the current state;
    the immutable-per-job receipt lets the outage breaker count distinct
    victims even after the parent has resumed or halted for another reason.
    The caller finalizes the job without failure bubbling, and raises the
    terminal-design alert after committing to avoid nested subject locks.
    """
    row = conn.execute(
        "SELECT meta FROM refs WHERE ref_id = %s FOR UPDATE", (parent_id,)
    ).fetchone()
    previous = (row[0] or {}).get("halt", {}) if row else {}
    if not isinstance(previous, dict):
        previous = {}
    repair_attempts = previous.get("repair_attempts", 0)
    if not isinstance(repair_attempts, int) or isinstance(repair_attempts, bool):
        repair_attempts = 0
    halt_class = conclusion.halt_class or "infra"
    if halt_class not in {"infra", "blocked", "misconceived", "cost-cap"}:
        halt_class = "infra"
    now = datetime.now(UTC)
    retry_safe, audit_reason = assess_halt_retry(raw_stream)
    halt: dict[str, Any] = {
        "class": halt_class,
        "job_ref": job_ref_id,
        "at": now.isoformat(),
        "summary_first_line": (
            conclusion.summary or "No halt summary supplied"
        ).splitlines()[0][:500],
        "retry_safe": retry_safe,
        "audit_reason": audit_reason,
        "repair_attempts": repair_attempts,
        "repair_pending": False,
    }
    if halt_class == "infra":
        tag = "waiting-for:infra"
        if retry_safe:
            halt["retry_at"] = (now + INFRA_RETRY_DELAY).isoformat()
    elif halt_class == "blocked":
        target = re.sub(
            r"[^a-z0-9_.-]+", "-", (conclusion.blocked_on or "external").lower()
        ).strip("-")[:100]
        if target in _MANAGED_WAIT_TARGETS or target.startswith("outage-"):
            target = f"dep-{target}"
        tag = f"waiting-for:{target or 'external'}"
    elif halt_class == "misconceived":
        if repair_attempts < 1:
            halt["repair_attempts"] = 1
            halt["repair_pending"] = True
            tag = "waiting-for:plan-repair"
            if retry_safe:
                halt["retry_at"] = now.isoformat()
        else:
            tag = "halt:misconceived"
    else:
        tag = "halt:cost-cap"
    if (halt_class == "infra" or halt["repair_pending"]) and not retry_safe:
        tag = UNCERTAIN_HALT_TAG
    halt["tag"] = tag
    _remove_tick_halts(store, conn, parent_id, job_ref_id, previous_halt_tags)
    store.add_tag(parent_id, Tag.open(tag), set_by="system", conn=conn)
    set_meta(conn, parent_id, halt=halt)
    set_meta(conn, job_ref_id, halt=halt)
    return tag


def alert_misconceived_halt(store: Store, parent_id: int, job_ref_id: int) -> None:
    """Surface an exhausted repair round after its state has committed."""
    from precis.alerts import raise_alert

    raise_alert(
        store,
        source="planner:misconceived",
        fingerprint=f"misconceived:{parent_id}",
        title=f"Todo #{parent_id} halted after one plan repair",
        detail=f"Tick job #{job_ref_id} still found the task misconceived; review its halt summary.",
        severity="info",
        subject_ref_id=parent_id,
    )


def clear_repair_pending(conn: Connection, parent_id: int) -> None:
    """Consume the repair prompt after a clean tick, retaining its allowance."""
    conn.execute(
        "UPDATE refs SET meta = jsonb_set(meta, '{halt,repair_pending}', 'false'::jsonb) "
        "WHERE ref_id = %s AND meta->'halt'->>'repair_pending' = 'true'",
        (parent_id,),
    )
