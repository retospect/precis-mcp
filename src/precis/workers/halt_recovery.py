"""Release audited halt parks and probe a shared infrastructure outage.

The alert is the durable breaker state; job halt receipts are its event
history. Maintenance and dispatch admission share a transaction advisory
lock, so multiple dispatch workers cannot admit simultaneous probes.
Unknown receipts never become automatic retries. Existing exclusion tags
remain authoritative after this module removes its own recovery park.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.alerts import _flip_resolved, raise_alert
from precis.store.types import Tag
from precis.workers.executors._common import TERMINAL

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

SOURCE = "breaker:infra-outage"
FINGERPRINT = "infra"
COOLDOWN_S = 15 * 60
MAX_COOLDOWN_S = 24 * 60 * 60
_LOCK_NAME = "precis:halt-recovery"
_UNCERTAIN = "ask-user:uncertain-halt-outcome"
#: Probe tick exhaustions that still prove the lane healthy: the tick ran a
#: full audited budget (max-turns, budget cap, wall-clock timeout) instead
#: of stopping on a still-broken environment or a quota window.
_CLEAN_EXHAUSTION = frozenset({"max_turns", "budget", "timeout"})


def _lock(conn: Connection) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (_LOCK_NAME,)
    ).fetchone()
    return bool(row and row[0])


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _save_halt(conn: Connection, ref_id: int, halt: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE refs SET meta = jsonb_set(meta, '{halt}', %s), updated_at = now() "
        "WHERE ref_id = %s",
        (Jsonb(halt), ref_id),
    )


def _save_breaker(conn: Connection, ref_id: int, state: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s",
        (Jsonb(state), ref_id),
    )


def _receipt_safe(conn: Connection, parent_id: int, halt: dict[str, Any]) -> bool:
    job_ref = halt.get("job_ref")
    if halt.get("retry_safe") is not True or not isinstance(job_ref, int):
        return False
    row = conn.execute(
        "SELECT meta->'halt' FROM refs WHERE ref_id = %s AND parent_id = %s "
        "AND kind = 'job' AND retired_at IS NULL",
        (job_ref, parent_id),
    ).fetchone()
    return bool(
        row
        and isinstance(row[0], dict)
        and row[0].get("retry_safe") is True
        and row[0].get("job_ref") == job_ref
    )


def _park_rows(conn: Connection, *, probe: bool = False) -> list[Any]:
    """Lock only recovery parks; skip an in-flight tick's locked parent."""
    probe_filter = ""
    if probe:
        probe_filter = """
           AND NOT EXISTS (
               SELECT 1 FROM ref_tags ht JOIN tags h ON h.tag_id = ht.tag_id
                WHERE ht.ref_id = r.ref_id AND h.namespace = 'OPEN'
                  AND h.value <> t.value
                  AND (h.value = 'halt' OR h.value LIKE 'halt:%%'
                       OR h.value = 'ask-user' OR h.value LIKE 'ask-user:%%'
                       OR h.value LIKE 'waiting-for:%%' OR h.value LIKE 'child-failed:%%')
           )
        """
    return conn.execute(
        """
        SELECT r.ref_id, r.meta->'halt', t.value
          FROM refs r JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'todo' AND r.retired_at IS NULL
           AND t.namespace = 'OPEN'
           AND ((t.value = 'waiting-for:infra' AND r.meta->'halt'->>'class' = 'infra')
                OR (t.value = 'waiting-for:plan-repair'
                    AND r.meta->'halt'->>'class' = 'misconceived')
                OR (t.value LIKE 'waiting-for:outage-%%'
                    AND r.meta->'halt'->>'class' = 'infra'))
           AND NOT EXISTS (
               SELECT 1 FROM ref_tags st JOIN tags s ON s.tag_id = st.tag_id
                WHERE st.ref_id = r.ref_id AND s.namespace = 'STATUS'
                  AND s.value NOT IN ('open', 'doing')
           )
        """
        + probe_filter
        + """
         ORDER BY CASE WHEN t.value = 'waiting-for:infra' THEN 0
                       WHEN t.value = 'waiting-for:plan-repair' THEN 1 ELSE 2 END,
                  r.meta->'halt'->>'retry_at' NULLS LAST, r.ref_id
         LIMIT 200
         FOR UPDATE OF r SKIP LOCKED
        """
    ).fetchall()


def _release(
    store: Store, conn: Connection, parent_id: int, halt: dict[str, Any], tag: str
) -> bool:
    if not _receipt_safe(conn, parent_id, halt):
        store.add_tag(parent_id, Tag.open(_UNCERTAIN), set_by="system", conn=conn)
        store.remove_tag(parent_id, Tag.open(tag), conn=conn)
        return False
    store.remove_tag(parent_id, Tag.open(tag), conn=conn)
    halt = {**halt, "retry_released": True}
    halt.pop("retry_at", None)
    _save_halt(conn, parent_id, halt)
    return True


def _open_breaker(conn: Connection) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute(
        "SELECT ref_id, meta FROM refs WHERE kind = 'alert' AND retired_at IS NULL "
        "AND alert_source = %s AND fingerprint = %s AND resolved_at IS NULL "
        "ORDER BY ref_id DESC LIMIT 1",
        (SOURCE, FINGERPRINT),
    ).fetchone()
    return (int(row[0]), dict(row[1])) if row else None


def _trip(
    store: Store, conn: Connection, now: datetime
) -> tuple[int, dict[str, Any]] | None:
    # A resolved episode's receipts must not immediately reopen it. The
    # watermark includes every halt seen before close, including probes.
    previous = conn.execute(
        "SELECT meta->>'halt_watermark', resolved_at FROM refs WHERE kind = 'alert' "
        "AND alert_source = %s AND fingerprint = %s AND resolved_at IS NOT NULL "
        "ORDER BY ref_id DESC LIMIT 1",
        (SOURCE, FINGERPRINT),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT ref_id, parent_id, meta->'halt'->>'at'
          FROM refs WHERE kind = 'job' AND retired_at IS NULL
           AND meta->'halt'->>'class' = 'infra'
           AND updated_at >= %s
        """,
        (now - timedelta(minutes=30),),
    ).fetchall()
    # Metadata edits must not turn historical receipts into fresh events.
    events = [
        row
        for row in rows
        if (at := _time(row[2])) is not None
        and now - timedelta(minutes=30) <= at <= now
        and (not previous or at > previous[1])
    ]
    if len(events) < 3 or len({row[1] for row in events}) < 2:
        return None
    state = {
        "breaker_state": "open",
        "cooldown_s": COOLDOWN_S,
        "next_probe_at": (now + timedelta(seconds=COOLDOWN_S)).isoformat(),
        "halt_watermark": max(int(row[0]) for row in events),
    }
    alert_id, _ = raise_alert(
        store,
        source=SOURCE,
        fingerprint=FINGERPRINT,
        title="Agent todo lane paused: repeated infrastructure halts",
        detail="Three infrastructure halts across at least two todos within 30 minutes. "
        "One audited cohort member will probe after cooldown; resolving this alert "
        "releases audited cohort parks on the next dispatch pass.",
        severity="critical",
        extra_meta=state,
    )
    return alert_id, state


def _watermark(conn: Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(max(ref_id), 0) FROM refs WHERE kind = 'job' "
        "AND meta->'halt'->>'class' = 'infra'"
    ).fetchone()
    return int(row[0]) if row else 0


def _probe_result(
    conn: Connection, state: dict[str, Any], now: datetime
) -> bool | None:
    parent = state.get("probe_parent")
    after = state.get("probe_after_job", 0)
    if not isinstance(parent, int) or not isinstance(after, int):
        return False
    row = conn.execute(
        """
        SELECT r.meta, COALESCE((SELECT t.value FROM ref_tags rt
               JOIN tags t ON t.tag_id = rt.tag_id
               WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1), 'queued')
          FROM refs r WHERE r.parent_id = %s AND r.kind = 'job'
           AND r.retired_at IS NULL AND r.ref_id > %s
           AND r.meta->>'job_type' = 'plan_tick'
         ORDER BY r.ref_id LIMIT 1
        """,
        (parent, after),
    ).fetchone()
    if row is not None:
        meta, status = row
        if status not in TERMINAL:
            return None
        if (
            status != "succeeded"
            or meta.get("halt")
            or meta.get("recovery_retry_blocked")
        ):
            return False
        # A routine resumable exhaustion is a healthy tick cut off mid-work
        # — common on this path, not a probe failure. An environment or
        # window stop (``no-precis-tools``, ``paused``, an unknown reason)
        # proves nothing either way and stays inconclusive.
        reason = meta.get("resumed_reason")
        return reason is None or reason in _CLEAN_EXHAUSTION
    started = _time(state.get("probe_started_at"))
    return False if started and now >= started + timedelta(minutes=15) else None


def _probe_available(conn: Connection, parent_id: int, cohort_tag: str) -> bool:
    """A separate hold or live child must not monopolize cohort sampling."""
    return (
        conn.execute(
            """
        SELECT 1 WHERE NOT EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value <> %s
               AND (t.value = 'halt' OR t.value LIKE 'halt:%%'
                    OR t.value = 'ask-user' OR t.value LIKE 'ask-user:%%'
                    OR t.value LIKE 'waiting-for:%%' OR t.value LIKE 'child-failed:%%')
        ) AND NOT EXISTS (
            SELECT 1 FROM refs c JOIN ref_tags rt ON rt.ref_id = c.ref_id
             JOIN tags t ON t.tag_id = rt.tag_id
             WHERE c.parent_id = %s AND c.retired_at IS NULL
               AND t.namespace = 'STATUS'
               AND ((c.kind = 'job' AND t.value <> ALL(%s))
                    OR (c.kind = 'todo' AND t.value NOT IN ('done', 'won''t-do', 'auto-timeout')))
        )
        """,
            (parent_id, cohort_tag, parent_id, list(TERMINAL)),
        ).fetchone()
        is not None
    )


def advance_halt_recovery(store: Store) -> None:
    """One bounded maintenance pass, before dispatch candidate enumeration."""
    now = datetime.now(UTC)
    with store.tx() as conn:
        if not _lock(conn):
            return
        active = _open_breaker(conn) or _trip(store, conn, now)
        rows = _park_rows(conn)
        if active is None:
            for parent_id, raw_halt, tag in rows:
                halt = dict(raw_halt) if isinstance(raw_halt, dict) else {}
                due = _time(halt.get("retry_at"))
                if tag.startswith("waiting-for:outage-"):
                    alert_id = tag.removeprefix("waiting-for:outage-")
                    resolved = conn.execute(
                        "SELECT 1 FROM refs WHERE kind = 'alert' AND ref_id::text = %s "
                        "AND alert_source = %s AND resolved_at IS NOT NULL",
                        (alert_id, SOURCE),
                    ).fetchone()
                    if resolved:
                        _release(store, conn, int(parent_id), halt, tag)
                elif due is not None and due <= now:
                    _release(store, conn, int(parent_id), halt, tag)
            return

        alert_id, state = active
        cohort_tag = f"waiting-for:outage-{alert_id}"
        # Enroll all waiting infra victims, including those that failed
        # before the trip; unrelated blocked/repair parks stay independent.
        for parent_id, raw_halt, tag in rows:
            halt = dict(raw_halt) if isinstance(raw_halt, dict) else {}
            if tag == "waiting-for:infra" and halt.get("class") == "infra":
                store.remove_tag(int(parent_id), Tag.open(tag), conn=conn)
                store.add_tag(
                    int(parent_id), Tag.open(cohort_tag), set_by="system", conn=conn
                )
                halt["outage_alert"] = alert_id
                halt["tag"] = cohort_tag
                _save_halt(conn, int(parent_id), halt)
            elif tag == "waiting-for:plan-repair":
                due = _time(halt.get("retry_at"))
                if due is not None and due <= now:
                    _release(store, conn, int(parent_id), halt, tag)

        if state.get("breaker_state") == "half-open":
            result = _probe_result(conn, state, now)
            if result is None:
                return
            if result:
                # Cohort tags and alert resolution commit together. Rows
                # locked by an in-flight tick are swept after resolution.
                for parent_id, raw_halt, tag in _park_rows(conn):
                    if tag == cohort_tag:
                        _release(store, conn, int(parent_id), dict(raw_halt or {}), tag)
                state["halt_watermark"] = _watermark(conn)
                state["breaker_state"] = "closed"
                _save_breaker(conn, alert_id, state)
                _flip_resolved(store, conn, alert_id, resolved_by="infra-probe")
                return
            cooldown = min(int(state.get("cooldown_s", COOLDOWN_S)) * 2, MAX_COOLDOWN_S)
            probe = state.get("probe_parent")
            # The failed probe may have changed class or acquired an
            # uncertain-outcome hold; never replace that decision.
            if isinstance(probe, int):
                row = conn.execute(
                    "SELECT meta->'halt' FROM refs WHERE ref_id = %s "
                    "FOR UPDATE SKIP LOCKED",
                    (probe,),
                ).fetchone()
                if row and isinstance(row[0], dict) and row[0].get("class") == "infra":
                    halt = dict(row[0])
                    after = state.get("probe_after_job", 0)
                    probe_job = conn.execute(
                        "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
                        "AND ref_id > %s AND meta->>'job_type' = 'plan_tick' "
                        "ORDER BY ref_id LIMIT 1",
                        (probe, after),
                    ).fetchone()
                    fresh_receipt = (
                        isinstance(halt.get("job_ref"), int) and halt["job_ref"] > after
                    )
                    probe_meta = probe_job[0] if probe_job else None
                    receipt_ok = _receipt_safe(conn, probe, halt)
                    uncertain = probe_job is not None and (
                        not receipt_ok
                        or (
                            not fresh_receipt
                            and isinstance(probe_meta, dict)
                            and bool(probe_meta.get("recovery_retry_blocked"))
                        )
                    )
                    if uncertain:
                        # A probe whose own stream audit failed, or a receipt
                        # that can no longer be verified, still needs review.
                        store.add_tag(
                            probe, Tag.open(_UNCERTAIN), set_by="system", conn=conn
                        )
                    elif receipt_ok:
                        # An inconclusive probe (env-broken resume, window
                        # pause, or a job that never minted) rejoins the
                        # cohort on its still-valid receipt instead of
                        # parking clean work for review.
                        store.remove_tag(
                            probe, Tag.open("waiting-for:infra"), conn=conn
                        )
                        store.add_tag(
                            probe, Tag.open(cohort_tag), set_by="system", conn=conn
                        )
            state.update(
                breaker_state="open",
                cooldown_s=cooldown,
                next_probe_at=(now + timedelta(seconds=cooldown)).isoformat(),
                probe_parent=None,
            )
            _save_breaker(conn, alert_id, state)
            return

        due = _time(state.get("next_probe_at"))
        if due is None or due > now:
            return
        for parent_id, raw_halt, tag in _park_rows(conn, probe=True):
            if tag != cohort_tag:
                continue
            if not _probe_available(conn, int(parent_id), cohort_tag):
                continue
            halt = dict(raw_halt or {})
            if not _release(store, conn, int(parent_id), halt, tag):
                continue
            latest = conn.execute(
                "SELECT COALESCE(max(ref_id), 0) FROM refs WHERE parent_id = %s AND kind = 'job'",
                (parent_id,),
            ).fetchone()
            state.update(
                breaker_state="half-open",
                probe_parent=int(parent_id),
                probe_after_job=int(latest[0]) if latest else 0,
                probe_started_at=now.isoformat(),
            )
            _save_breaker(conn, alert_id, state)
            return


def infra_dispatch_allowed(conn: Connection, parent_id: int) -> bool:
    """Recheck global admission inside the locked parent's mint transaction."""
    if not _lock(conn):
        return False
    active = _open_breaker(conn)
    if active is None:
        return True
    _, state = active
    if (
        state.get("breaker_state") != "half-open"
        or state.get("probe_parent") != parent_id
    ):
        return False
    after = state.get("probe_after_job")
    if not isinstance(after, int):
        return False
    # Even if the first probe finishes between maintenance and claim,
    # never mint a second tick before that probe's result closes/reopens
    # the breaker. Admission and mint hold the same advisory lock.
    return (
        conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND ref_id > %s AND meta->>'job_type' = 'plan_tick' LIMIT 1",
            (parent_id, after),
        ).fetchone()
        is None
    )
