"""Schedule worker — Slice 4 of ``docs/design/todo-tree-plan.md``, extended
by ADR 0061 to also fire **push delivery** (the retired ``kind='cron'``
mechanism, folded onto ``level:recurring``).

Walks every ``level:recurring`` ref whose ``meta.schedule`` is
non-null, computes ticks since the last spawn event, and mints one
``level:subtask`` child per due tick. The Watches umbrella is
seeded on first run via :func:`ensure_watches_root` so the second
panel of ``view='roots'`` has somewhere to anchor.

Two schedule shapes, two tick actions
======================================

* ``meta.schedule.cron`` (recurring) — the existing per-tick spawn loop
  below. When the recurring ALSO carries ``meta.deliver = {'target': ...}``,
  a due tick fires a push notify (:func:`_fire_delivery_conn`) instead of
  minting a subtask — the tick's action *is* the delivery, matching how
  the retired cron kind never touched the todo queue either.
* ``meta.schedule.at`` (one-shot) — a single absolute fire time (ADR 0061's
  "remind me in/at" case). :func:`_process_one_shot` decides fire / wait /
  expire and, once resolved, tags the recurring root ``STATUS:done`` so it
  never re-fires — a one-shot is a ``level:recurring`` node that retires
  itself after its one tick.

Guards (in order, per the plan):

1. **Folder skip** — refs with ``meta.schedule is None`` (the Watches
   umbrella, and any future "folder" recurring) are walked but
   never spawn anything.
2. **Paused / resolved skip** — recurrings tagged ``STATUS:paused`` (or,
   for a one-shot, already resolved to ``STATUS:done``) are skipped.
3. **Idempotency** — queue-mode: each candidate tick stamps the spawned
   child with ``meta.spawned_for_tick='YYYY-MM-DDTHH:MM'``. Delivery-mode
   (``meta.deliver`` set): the same stamp lives on a ``schedule:deliver``
   ``ref_events`` row instead (no child to stamp). Either way, a second
   pass on the same tick is a no-op.
4. **Collision-skip** (queue-mode only) — when the previous spawned
   child is still open (no ``STATUS:done``-class tag), the new tick is
   skipped. A stalled queue doesn't pile up; the nursery sweep surfaces
   the stuck leaf. Delivery-mode has no queue item to collide with, so
   this guard doesn't apply there.
5. **Backfill policy** — when the schedule's ``backfill_missed`` is
   ``False`` (the default), only the most recent tick is considered;
   missed ticks for weather / news are dropped. ``True`` walks every
   missed tick since the last spawn, so birthdays catch up.

The worker is sql + python only — no LLM calls — and stays in the
default ``precis worker`` rotation alongside ``auto_check``.

Multi-host concurrency
======================

Each recurring is processed under a per-row exclusive lock
(``SELECT … FROM refs … FOR UPDATE SKIP LOCKED``) opened inside a
``store.tx()`` block. The lock spans the full claim-check-spawn
sequence including the ``meta.spawned_for_tick`` write — so two
workers (same host or different hosts) racing on the same recurring
serialise cleanly. The loser's ``SELECT`` returns no row and it
moves on to the next candidate.

Crash safety: the lock is bound to the connection's transaction.
If the process dies hard (OOM, network partition, ``kill -9``),
Postgres rolls the tx back and releases the lock at the next
deadlock-check / connection-cleanup cycle. No heartbeat, no TTL
reaper, no stale-row sweeper. Same property the ingest claim
(``ingest/claim.py``, ADR 0016) relies on, scaled down to per-row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from precis.store import Store
from precis.workers.runner import BatchResult
from precis.workers.schedule.parse import (
    Schedule,
    one_shot_action,
    parse_schedule,
    ticks_since,
)
from precis.workers.schedule.seed import (
    WATCHES_BUILTIN,
    ensure_watches_root,
)

log = logging.getLogger(__name__)

#: STATUS values that make a recurring root ineligible for the next sweep:
#: paused (operator opt-out) or one of the done-class terminals a resolved
#: one-shot self-tags after firing/expiring.
_INELIGIBLE_STATUSES_SQL = "('paused', 'done', 'won''t-do', 'auto-timeout')"


def run_schedule_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """One sweep of the recurring queue.

    Returns a :class:`BatchResult` where:

    * ``claimed`` = number of recurring refs inspected this pass
    * ``ok`` = number of subtasks minted (one per due tick)
    * ``failed`` = number of ticks **skipped** (collision-skip, or
      stamps that already existed)

    The naming follows the shared ``BatchResult`` shape; ``failed``
    here is not a software failure but the "didn't spawn this tick"
    counter that the operator may want to see in metrics.
    """
    # Seed the umbrella first so the panel renders even before the
    # operator creates any explicit recurrings.
    try:
        ensure_watches_root(store)
    except Exception:
        # Don't let a transient seed failure (race, transient DB
        # error) kill the pass — the umbrella shows up on the next
        # tick. Log loudly so a persistent failure gets noticed.
        log.exception("schedule: ensure_watches_root failed")

    candidate_ids = _candidate_recurring_ids(store, limit=limit)
    if not candidate_ids:
        return BatchResult(handler="schedule", claimed=0, ok=0, failed=0)

    now = datetime.now(UTC)
    n_claimed = 0
    n_spawned = 0
    n_skipped = 0
    for rec_id in candidate_ids:
        try:
            claimed, spawned, skipped = _claim_and_process(store, rec_id, now=now)
        except Exception:
            # Bad schedule shape, missing index, etc. — log and
            # continue. The handler boundary validates at write time,
            # so a bad ``meta.schedule`` here is a pre-existing row
            # or a system-modified meta.
            log.exception("schedule: failed to process recurring id=%d", rec_id)
            n_skipped += 1
            continue
        n_claimed += claimed
        n_spawned += spawned
        n_skipped += skipped

    return BatchResult(
        handler="schedule",
        claimed=n_claimed,
        ok=n_spawned,
        failed=n_skipped,
    )


# ── candidate enumeration (unlocked) ──────────────────────────────


def _candidate_recurring_ids(store: Store, *, limit: int) -> list[int]:
    """Return ref ids that *might* need ticking this pass.

    Pure SELECT — no locks held. The per-row claim happens later
    inside :func:`_claim_and_process` under ``FOR UPDATE SKIP LOCKED``.
    A candidate that turns out to be claimed by another worker
    silently disappears at the lock step.

    Eligibility:

    * ``kind='todo'`` and not deleted
    * carries the ``level:recurring`` open tag
    * not paused, and not a resolved one-shot (``STATUS`` in
      :data:`_INELIGIBLE_STATUSES_SQL` excluded)
    * not the umbrella folder (``meta.builtin = 'watches-root'``)
    * has a ``meta.schedule`` block (not a folder ref by other means)
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN {_INELIGIBLE_STATUSES_SQL}
               AND COALESCE(r.meta->>'builtin', '') <> %s
               AND r.meta ? 'schedule'
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (WATCHES_BUILTIN, limit),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── per-recurring locked spawn ────────────────────────────────────


def _claim_and_process(
    store: Store,
    rec_id: int,
    *,
    now: datetime,
) -> tuple[int, int, int]:
    """Lock one recurring's refs row, compute ticks, mint children.

    Returns ``(claimed, spawned, skipped)``:

    * ``claimed`` = 1 if we successfully acquired the row lock, 0 if
      another worker already holds it.
    * ``spawned`` = number of subtasks minted under this recurring.
    * ``skipped`` = number of ticks the spawner refused to mint
      (stamp-collision idempotency, or collision-skip when previous
      tick still open).

    The lock spans the entire claim-check-spawn sequence including
    the spawned-child insert + the ``schedule:spawn`` event append,
    so two workers racing on the same recurring serialise on the
    refs row's tx lock. ``SKIP LOCKED`` means the loser walks past
    rather than queueing — the next pass picks it up if there's
    still work, otherwise it's a no-op.
    """
    with store.tx() as conn:
        row = conn.execute(
            f"""
            SELECT r.ref_id,
                   r.title,
                   r.meta->'schedule' AS schedule,
                   r.meta->>'executor' AS executor,
                   r.meta->>'job_type' AS job_type,
                   r.meta->'params' AS params,
                   r.meta->'deliver' AS deliver,
                   (SELECT max(e.ts) FROM ref_events e
                     WHERE e.ref_id = r.ref_id
                       AND e.source = 'schedule'
                       AND e.event = 'spawn') AS last_tick
              FROM refs r
             WHERE r.ref_id = %s
               AND r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.meta ? 'schedule'
               AND COALESCE(r.meta->>'builtin', '') <> %s
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN {_INELIGIBLE_STATUSES_SQL}
             FOR UPDATE OF r SKIP LOCKED
            """,
            (rec_id, WATCHES_BUILTIN),
        ).fetchone()
        if row is None:
            # Either another worker holds the row, or the row was
            # paused / deleted / resolved between the enumeration
            # pass and now. Both cases: do nothing, return clean.
            return (0, 0, 0)

        rec: dict[str, Any] = {
            "id": int(row[0]),
            "title": row[1],
            "schedule": dict(row[2] or {}),
            "executor": row[3],
            "job_type": row[4],
            "params": row[5],
            "deliver": dict(row[6]) if row[6] else None,
            "last_tick": row[7],
        }
        spawned, skipped = _spawn_due_ticks(store, rec, now=now, conn=conn)
        return (1, spawned, skipped)


def _spawn_due_ticks(
    store: Store,
    rec: dict[str, Any],
    *,
    now: datetime,
    conn: Any,
) -> tuple[int, int]:
    """Compute due ticks and act on each, inside the locked tx.

    Called only from :func:`_claim_and_process`, which holds the row
    lock. ``conn`` is the transaction-bound connection so every write
    (child insert, delivery notify, event append, STATUS tag) shares
    the tx; commit happens when the outer ``with store.tx()`` exits.

    Branches on the schedule shape (ADR 0061):

    * ``schedule.at`` set — one-shot; delegates to
      :func:`_process_one_shot`.
    * ``schedule.cron`` set — recurring; the existing per-tick loop.
      When ``rec['deliver']`` is set, a due tick fires a push
      delivery (:func:`_fire_delivery_conn`) instead of minting a
      subtask — no collision-skip guard applies (there's no queue
      item to collide with, matching the retired cron kind).
    """
    spec = rec["schedule"]
    if not spec:
        return (0, 0)
    schedule: Schedule = parse_schedule(spec)
    if schedule.at is not None:
        return _process_one_shot(store, conn, rec, schedule, now=now)
    last_tick = rec["last_tick"]
    if last_tick is not None and last_tick.tzinfo is None:
        last_tick = last_tick.replace(tzinfo=UTC)
    ticks = ticks_since(last_tick, schedule, now=now)
    if not ticks:
        return (0, 0)
    deliver = rec.get("deliver")
    spawned = 0
    skipped = 0
    for tick in ticks:
        stamp = tick.isoformat(timespec="minutes")
        if deliver:
            if _deliver_stamp_exists_conn(conn, rec["id"], stamp):
                skipped += 1
                continue
            _fire_delivery_conn(
                conn, rec, stamp=stamp, target=deliver["target"], store=store
            )
            spawned += 1
            continue
        if _child_with_stamp_exists_conn(conn, rec["id"], stamp):
            skipped += 1
            continue
        if _has_open_previous_tick_conn(conn, rec["id"]):
            log.info(
                "schedule: rec id=%d skipping tick %s (previous still open)",
                rec["id"],
                stamp,
            )
            skipped += 1
            continue
        _mint_child_conn(store, conn, rec, tick=tick, stamp=stamp)
        spawned += 1
    return (spawned, skipped)


def _process_one_shot(
    store: Store,
    conn: Any,
    rec: dict[str, Any],
    schedule: Schedule,
    *,
    now: datetime,
) -> tuple[int, int]:
    """Resolve a one-shot ``meta.schedule.at`` recurring (ADR 0061).

    A one-shot is a ``level:recurring`` node whose schedule fires exactly
    once: this decides fire / wait / expire via
    :func:`~precis.workers.schedule.parse.one_shot_action`, fires the
    delivery notify on a fire (when ``rec['deliver']`` is set), and — on
    either a fire or an expire — tags the recurring root ``STATUS:done``
    so it drops out of :func:`_candidate_recurring_ids` for good (no
    self-inflicted re-fire next pass).

    Returns ``(spawned, skipped)`` where ``spawned`` counts a fire and
    ``skipped`` counts a not-yet-due wait or an expiry.
    """
    from precis.store.types import Tag

    assert schedule.at is not None
    at = _parse_iso(schedule.at)
    action = one_shot_action(at, catch_up=schedule.catch_up, now=now)
    if action == "wait":
        return (0, 0)
    stamp = at.isoformat(timespec="minutes")
    if _deliver_stamp_exists_conn(conn, rec["id"], stamp):
        # Already resolved this exact tick (shouldn't happen — the
        # STATUS:done tag below should have excluded it from the next
        # candidate scan — but stay idempotent regardless).
        return (0, 0)
    deliver = rec.get("deliver")
    if action == "fire":
        if deliver:
            _fire_delivery_conn(
                conn, rec, stamp=stamp, target=deliver["target"], store=store
            )
        else:
            store.append_event(
                rec["id"],
                source="schedule",
                event="deliver",
                payload={"tick": stamp, "action": "fire"},
                conn=conn,
            )
        result = (1, 0)
    else:  # "expire"
        store.append_event(
            rec["id"],
            source="schedule",
            event="deliver",
            payload={"tick": stamp, "action": "expire"},
            conn=conn,
        )
        log.info(
            "schedule: rec id=%d one-shot expired (missed, catch_up=False)", rec["id"]
        )
        result = (0, 1)
    store.add_tag(
        rec["id"],
        Tag.closed("STATUS", "done"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    return result


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _deliver_stamp_exists_conn(conn: Any, rec_id: int, stamp: str) -> bool:
    """True when a ``schedule:deliver`` event already carries this tick stamp.

    Delivery-mode ticks mint no child row (nothing to check
    ``spawned_for_tick`` against), so idempotency lives directly on
    ``ref_events`` — the same table :func:`_mint_child_conn` appends
    ``schedule:spawn`` to for queue-mode ticks.
    """
    row = conn.execute(
        """
        SELECT 1 FROM ref_events
         WHERE ref_id = %s AND source = 'schedule' AND event = 'deliver'
           AND payload->>'tick' = %s
         LIMIT 1
        """,
        (rec_id, stamp),
    ).fetchone()
    return row is not None


def _fire_delivery_conn(
    conn: Any, rec: dict[str, Any], *, stamp: str, target: str, store: Store
) -> None:
    """Fire the push-delivery notify + stamp the tick (ADR 0061).

    Emits the *exact* wire payload the retired ``kind='cron'`` fired —
    ``pg_notify('precis.cron', {cron_id, payload, target})`` — so asa_bot's
    listener (``asa_bot/pg_listen.py`` + ``bot.py::_handle_cron``) needs no
    change: it never read back a ``kind='cron'`` ref, only the notify
    payload. ``payload`` is the recurring's own title/text — recurring
    reminders don't carry a fresh per-tick body, same as the retired cron.
    """
    import json

    payload_text = rec.get("title") or ""
    conn.execute(
        "SELECT pg_notify('precis.cron', %s)",
        (
            json.dumps(
                {"cron_id": rec["id"], "payload": payload_text, "target": target}
            ),
        ),
    )
    store.append_event(
        rec["id"],
        source="schedule",
        event="deliver",
        payload={"tick": stamp, "target": target},
        conn=conn,
    )


def _child_with_stamp_exists_conn(conn: Any, parent_id: int, stamp: str) -> bool:
    """True when a child already carries this ``spawned_for_tick`` stamp."""
    row = conn.execute(
        """
        SELECT 1 FROM refs
         WHERE parent_id = %s
           AND deleted_at IS NULL
           AND meta->>'spawned_for_tick' = %s
         LIMIT 1
        """,
        (parent_id, stamp),
    ).fetchone()
    return row is not None


def _has_open_previous_tick_conn(conn: Any, parent_id: int) -> bool:
    """True when a prior spawned tick is still genuinely *in flight*.

    Plan's collision policy: skip the new tick if the previous one
    is still on the queue. We check *all* spawned children — if any
    prior spawn is still in flight, the queue is already stalled and
    minting more would compound the problem.

    "In flight" excludes both the done-class terminals (``done`` /
    ``won't-do`` / ``auto-timeout``) **and** a tick that has terminally
    *failed*. A recurring tick whose job fails bubbles a
    ``child-failed:<job_id>`` open tag onto the spawned child but leaves
    its ``STATUS:open`` (awaiting an owner retry/give-up decision — the
    failure-bubble convention). That child is terminal-for-scheduling,
    not in-flight: counting it as "still open" wedged the recurring
    *forever* (one failed ``news_poll`` / ``briefing`` tick silently
    stopped the whole cadence — every subsequent pass logged "skipping
    tick … (previous still open)" with no way out short of a manual
    close). A recurring must be resilient to a single bad tick: skip the
    failed one, fire the next. The failure still surfaces (the
    ``child-failed`` bubble + the nursery ``stalled-recurring`` alert),
    so it isn't lost — it just no longer halts the schedule.
    """
    row = conn.execute(
        """
        SELECT 1 FROM refs c
         WHERE c.parent_id = %s
           AND c.deleted_at IS NULL
           AND c.meta ? 'spawned_for_tick'
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) NOT IN ('done', 'won''t-do', 'auto-timeout', 'failed')
           -- A tick whose child job failed bubbles a ``child-failed:*``
           -- open tag and stays STATUS:open; that is terminal-for-
           -- scheduling, not in-flight. Don't let it block the cadence.
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = c.ref_id
                    AND t.namespace = 'OPEN'
                    AND t.value LIKE 'child-failed:%%'
               )
         LIMIT 1
        """,
        (parent_id,),
    ).fetchone()
    return row is not None


def _mint_child_conn(
    store: Store,
    conn: Any,
    rec: dict[str, Any],
    *,
    tick: datetime,
    stamp: str,
) -> None:
    """Insert one spawned child + the ``spawn`` event on the held tx.

    The child carries:

    * ``parent_id = rec.id``
    * ``meta.spawned_for_tick = stamp``
    * ``meta.executor = rec.executor`` (if present on the recurring)
    * ``meta.job_type`` / ``meta.params`` = rec's (if present) — lets a
      recurring drive a deterministic in-process job_type, not just an
      agentic claude task
    * ``prio = 2`` (the cron-spawn default, see plan)
    * ``STATUS:open`` open tag (handled by ``add_tag``)
    * ``level:subtask`` open tag

    The ``ref_events`` row carries ``source='schedule', event='spawn',
    payload={'tick': stamp}`` so the dashboard can surface "last tick"
    without parsing meta on every read.
    """
    from precis.store.types import Tag

    title = _render_child_title(rec, tick)
    meta: dict[str, Any] = {"spawned_for_tick": stamp}
    if rec.get("executor"):
        meta["executor"] = rec["executor"]
    # Carry job_type + params so a recurring whose executor runs a
    # deterministic job_type (e.g. news_poll / briefing) spawns a child
    # the dispatch pass can mint a *typed* job from. Without these the
    # child would carry only `executor` and the executor would fail it
    # ("missing meta.job_type"), so recurrings could only ever run
    # agentic (claude-on-the-text) work, never an in-process pass.
    if rec.get("job_type"):
        meta["job_type"] = rec["job_type"]
    if rec.get("params") is not None:
        meta["params"] = rec["params"]
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta=meta,
        parent_id=rec["id"],
        prio=2,
        conn=conn,
    )
    store.add_tag(
        child.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(
        child.id,
        Tag.open("level:subtask"),
        set_by="system",
        conn=conn,
    )
    store.append_event(
        rec["id"],
        source="schedule",
        event="spawn",
        payload={"tick": stamp, "child_id": int(child.id)},
        conn=conn,
    )
    log.info(
        "schedule: rec id=%d spawned child id=%d for tick %s",
        rec["id"],
        child.id,
        stamp,
    )


def _render_child_title(rec: dict[str, Any], tick: datetime) -> str:
    """Render the spawned child's title as ``<rec_title> <YYYY-MM-DD>``.

    Short and indexable — the date suffix gives the operator an
    at-a-glance "which tick is this" without having to read meta.
    Hour-resolution recurrings still get day-stamped titles; the
    full minute is in ``meta.spawned_for_tick`` for tooling.
    """
    base = (rec.get("title") or "Untitled recurring").split("\n", 1)[0]
    return f"{base} {tick.date().isoformat()}"


__all__ = ["run_schedule_pass"]
