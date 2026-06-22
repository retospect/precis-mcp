"""Schedule worker — Slice 4 of ``docs/design/todo-tree-plan.md``.

Walks every ``level:recurring`` ref whose ``meta.schedule`` is
non-null, computes ticks since the last spawn event, and mints one
``level:subtask`` child per due tick. The Watches umbrella is
seeded on first run via :func:`ensure_watches_root` so the second
panel of ``view='roots'`` has somewhere to anchor.

Guards (in order, per the plan):

1. **Folder skip** — refs with ``meta.schedule is None`` (the Watches
   umbrella, and any future "folder" recurring) are walked but
   never spawn anything.
2. **Paused skip** — recurrings with ``STATUS:paused`` are skipped.
3. **Idempotency** — each candidate tick stamps the spawned child
   with ``meta.spawned_for_tick='YYYY-MM-DDTHH:MM'``; if a child
   with the same stamp already exists, the tick is a no-op.
4. **Collision-skip** — when the previous spawned child is still
   open (no ``STATUS:done``-class tag), the new tick is skipped.
   A stalled queue doesn't pile up; the nursery sweep surfaces the
   stuck leaf.
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
    parse_schedule,
    ticks_since,
)
from precis.workers.schedule.seed import (
    WATCHES_BUILTIN,
    ensure_watches_root,
)

log = logging.getLogger(__name__)


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
    * not paused (``STATUS:paused`` excluded)
    * not the umbrella folder (``meta.builtin = 'watches-root'``)
    * has a ``meta.schedule`` block (not a folder ref by other means)
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
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
                   ) <> 'paused'
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
            """
            SELECT r.ref_id,
                   r.title,
                   r.meta->'schedule' AS schedule,
                   r.meta->>'executor' AS executor,
                   r.meta->>'job_type' AS job_type,
                   r.meta->'params' AS params,
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
                   ) <> 'paused'
             FOR UPDATE OF r SKIP LOCKED
            """,
            (rec_id, WATCHES_BUILTIN),
        ).fetchone()
        if row is None:
            # Either another worker holds the row, or the row was
            # paused / deleted / un-marked between the enumeration
            # pass and now. Both cases: do nothing, return clean.
            return (0, 0, 0)

        rec: dict[str, Any] = {
            "id": int(row[0]),
            "title": row[1],
            "schedule": dict(row[2] or {}),
            "executor": row[3],
            "job_type": row[4],
            "params": row[5],
            "last_tick": row[6],
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
    """Compute due ticks and mint a child per tick inside the locked tx.

    Called only from :func:`_claim_and_process`, which holds the row
    lock. ``conn`` is the transaction-bound connection so the spawn
    inserts + event appends share the tx; commit happens when the
    outer ``with store.tx()`` exits.
    """
    spec = rec["schedule"]
    if not spec:
        return (0, 0)
    schedule: Schedule = parse_schedule(spec)
    last_tick = rec["last_tick"]
    if last_tick is not None and last_tick.tzinfo is None:
        last_tick = last_tick.replace(tzinfo=UTC)
    ticks = ticks_since(last_tick, schedule, now=now)
    if not ticks:
        return (0, 0)
    spawned = 0
    skipped = 0
    for tick in ticks:
        stamp = tick.isoformat(timespec="minutes")
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
    """True when any spawned child is still open (non-done STATUS).

    Plan's collision policy: skip the new tick if the previous one
    is still on the queue. We check *all* spawned children for open
    status — if any prior spawn is still open, the queue is already
    stalled and minting more would compound the problem.
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
               ) NOT IN ('done', 'won''t-do', 'auto-timeout')
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
