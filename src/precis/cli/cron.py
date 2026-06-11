"""``precis cron tick`` — fire due cron entries.

Launchd timer on melchior runs this every 60s. Each invocation:

1. Atomically claims every cron ref with ``meta.next_fire_at <= now()``
   and ``meta.status='scheduled'`` (``SKIP LOCKED`` so two concurrent
   ticks don't double-fire).
2. For each claimed row, decides one of three actions:
     - **fire**: emit pg_notify('precis.cron', {ref_id, payload, target}),
       advance ``next_fire_at`` (per recurrence) or mark ``status='fired'``
       (one-shot).
     - **skip**: recurring + catch_up=false + missed by > grace window —
       silently advance ``next_fire_at`` without firing.
     - **expire**: one-shot + catch_up=false + missed by > grace window —
       mark ``status='expired'`` without firing.
3. Updates ``meta.last_fired_at``, ``meta.fire_count``, and the new
   ``meta.next_fire_at``.

The scheduler grammar parsed here mirrors what CronHandler validates
on put. Only shape is checked at put-time; this function does the
actual advance.

Exit code 0 always (failures log but don't break launchd; the next
tick recovers).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)


# How far past next_fire_at counts as "the slot was missed" for
# catch_up=false policy. 90s gives 60s tick + 30s grace.
_MISS_GRACE = timedelta(seconds=90)


_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(minute|minutes|min|m|hour|hours|hr|h|day|days|d)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_DELTA = {
    "minute": timedelta(minutes=1),
    "minutes": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "m": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hours": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "h": timedelta(hours=1),
    "day": timedelta(days=1),
    "days": timedelta(days=1),
    "d": timedelta(days=1),
}

_DAILY_AT_RE = re.compile(r"^daily@(\d{1,2}):(\d{2})$")
_WEEKLY_AT_RE = re.compile(
    r"^weekly@(mon|tue|wed|thu|fri|sat|sun)@(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)

_WEEKDAY_TO_INT = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


def _parse_duration(s: str) -> timedelta:
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"unparseable duration {s!r}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * _UNIT_TO_DELTA[unit]


def compute_next(recurring: str, after: datetime) -> datetime:
    """Advance a recurrence to the next fire time strictly after ``after``.

    Crucial property: ``MAX(prev_next + interval, now() + interval)`` —
    a recurring task that was missed for hours fires *once* on the
    catch-up tick, then next_fire_at jumps ahead to the next future
    occurrence. Without this, a 5-min recurring task missed for 3
    hours would re-fire 36 times to "catch up." Caps at one fire.
    """
    s = recurring.strip().lower()
    if s == "hourly":
        # Next hour boundary > after.
        nxt = after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return nxt
    if s == "daily":
        nxt = after.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return nxt
    if s == "weekly":
        nxt = after.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)
        return nxt
    if s.startswith("every "):
        rest = s[len("every "):]
        return after + _parse_duration(rest)
    m = _DAILY_AT_RE.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    m = _WEEKLY_AT_RE.match(s)
    if m:
        day_name = m.group(1).lower()
        hh, mm = int(m.group(2)), int(m.group(3))
        target_dow = _WEEKDAY_TO_INT[day_name]
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # Days until target weekday (1..7; never zero — always strictly future).
        days_ahead = (target_dow - after.weekday()) % 7
        if days_ahead == 0 and candidate <= after:
            days_ahead = 7
        candidate += timedelta(days=days_ahead)
        return candidate
    raise ValueError(f"unsupported recurring expression {recurring!r}")


def add_parser(subparsers: Any) -> None:
    """Register the ``cron`` subparser tree on the top-level parser."""
    cron = subparsers.add_parser(
        "cron",
        help="Cron operations — tick the scheduled-task scanner.",
    )
    cron_sub = cron.add_subparsers(dest="cron_cmd", required=True)
    tick = cron_sub.add_parser(
        "tick",
        help=(
            "Scan for due cron entries, fire NOTIFY, advance schedules. "
            "Launchd timer runs this every 60s on melchior."
        ),
    )
    tick.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would fire without committing changes.",
    )


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis cron <subcmd>``."""
    if args.cron_cmd == "tick":
        _tick(dry_run=args.dry_run)
        return
    print(f"cron: unknown subcommand {args.cron_cmd!r}", file=sys.stderr)
    sys.exit(2)


def _tick(*, dry_run: bool) -> None:
    """Atomically scan + fire due cron entries.

    Single transaction:
      - SELECT ... FOR UPDATE SKIP LOCKED claims the due rows.
      - For each: decide action, compute new meta, UPDATE.
      - For ``fire`` actions: emit pg_notify inside the same tx so
        notification ships only on commit.

    Idempotency: SKIP LOCKED + the meta.status='scheduled' filter
    means a re-entrant call sees zero work.

    Logs a one-line summary per tick.
    """
    dsn = resolve_dsn(None)

    fired = 0
    skipped = 0
    expired = 0

    now = datetime.now(tz=UTC)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ref_id, meta
                FROM refs
                WHERE kind = 'cron'
                  AND deleted_at IS NULL
                  AND (meta->>'status') = 'scheduled'
                  AND (meta->>'next_fire_at')::timestamptz <= %s
                ORDER BY (meta->>'next_fire_at')::timestamptz ASC
                FOR UPDATE SKIP LOCKED
                """,
                (now,),
            )
            due = cur.fetchall()
            log.info("cron tick: %d due entr%s", len(due), "y" if len(due) == 1 else "ies")

            for ref_id, meta in due:
                if not isinstance(meta, dict):
                    log.warning("cron %d: meta is not a dict (%r); skipping", ref_id, type(meta))
                    continue
                try:
                    new_meta, action = _decide(meta, now)
                except Exception:
                    log.exception("cron %d: decision failed; leaving as-is", ref_id)
                    continue

                if dry_run:
                    log.info("cron %d: would %s (dry-run)", ref_id, action)
                    continue

                cur.execute(
                    "UPDATE refs SET meta = %s, updated_at = now() WHERE ref_id = %s",
                    (Jsonb(new_meta), ref_id),
                )

                if action == "fire":
                    payload = {
                        "cron_id": ref_id,
                        "payload": meta.get("title") or new_meta.get("title") or "",
                        "target": meta.get("target"),
                    }
                    # Prefer the body chunk text when available; the
                    # title is a copy of the first put-time text, so
                    # use it as fallback to avoid an extra query.
                    cur.execute(
                        "SELECT text FROM chunks WHERE ref_id = %s "
                        "AND chunk_kind = 'cron_payload' ORDER BY ord LIMIT 1",
                        (ref_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        payload["payload"] = row[0]
                    cur.execute(
                        "SELECT pg_notify('precis.cron', %s)",
                        (json.dumps(payload),),
                    )
                    fired += 1
                elif action == "skip":
                    skipped += 1
                elif action == "expire":
                    expired += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    log.info(
        "cron tick: fired=%d, skipped=%d, expired=%d%s",
        fired,
        skipped,
        expired,
        " (dry-run)" if dry_run else "",
    )


def _decide(meta: dict[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
    """Compute the new meta + action for one due cron entry.

    Returns ``(new_meta, action)`` where action is
    ``'fire'`` / ``'skip'`` / ``'expire'``.
    """
    new_meta = dict(meta)
    recurring = meta.get("recurring")
    catch_up = bool(meta.get("catch_up", False))
    raw_next = meta.get("next_fire_at")
    next_at = _parse_iso(raw_next) if raw_next else now

    overdue_by = now - next_at if now > next_at else timedelta(0)

    if recurring:
        if catch_up or overdue_by <= _MISS_GRACE:
            # Fire and advance.
            action = "fire"
        else:
            # Recurring + catch_up=false + missed: silently skip.
            action = "skip"
        try:
            new_next = compute_next(recurring, max(now, next_at))
        except ValueError:
            log.exception("cron: bad recurring %r; staying scheduled", recurring)
            new_next = now + timedelta(minutes=1)
        new_meta["next_fire_at"] = new_next.isoformat()
        if action == "fire":
            new_meta["last_fired_at"] = now.isoformat()
            new_meta["fire_count"] = int(meta.get("fire_count", 0)) + 1
        return new_meta, action

    # One-shot.
    if not catch_up and overdue_by > _MISS_GRACE:
        new_meta["status"] = "expired"
        return new_meta, "expire"
    new_meta["status"] = "fired"
    new_meta["last_fired_at"] = now.isoformat()
    new_meta["fire_count"] = int(meta.get("fire_count", 0)) + 1
    return new_meta, "fire"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
