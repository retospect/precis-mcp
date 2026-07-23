"""One-off backfill: convert existing `kind='cron'` refs to `level:recurring`
todos (ADR 0061, superseding ADR 0030's cron ruling).

`kind='cron'` is retired — this script is the data-migration half of that
retirement (the code half needs no schema migration; `kind` is a plain
string column). For every non-deleted `cron` ref it:

* builds a `level:recurring` todo carrying the translated
  ``meta.schedule`` (one-shot ``at``/``catch_up``, or recurring
  ``cron``/``backfill_missed``) + ``meta.deliver = {'target': ...}``;
* carries over the body text, the ``automation``-family open tags (not
  ``STATUS:*`` — those get re-derived), and a best-effort ``STATUS``
  (already-resolved crons land ``STATUS:done``; paused ones land
  ``STATUS:paused``);
* soft-deletes the original ``cron`` ref (``deleted_at``) so the row stays
  for audit but stops rendering live.

**Not run against prod by this change** — see `OPEN-ITEMS.md`. Dry-run by
default (`--commit` writes). The old cron recurrence vocabulary
(`hourly`/`daily`/`weekly`/`every <N> <unit>`/`daily@HH:MM`/
`weekly@<dow>@HH:MM`) doesn't map 1:1 onto the new 5-field cron grammar for
every case — `every <N> day`/`hour`/`minute` outside the new grammar's
supported ranges has no clean translation and is **skipped with a warning**
(left as a `cron` ref for manual handling) rather than guessed at. `weekly`
(day-agnostic in the old engine) becomes Monday in the new grammar — noted
per-row in the dry-run report so a human can adjust if the original intent
was a different day.

Links on the original cron ref are **not** migrated (rare in practice;
re-link manually if needed).

Env: ``PRECIS_DATABASE_URL`` (falls back to the local dev DSN).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("migrate-cron-to-recurring")

_DEFAULT_DB = "postgresql://precis:precis@127.0.0.1:5432/precis"

_WEEKDAY_TO_CRON_DOW: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


@dataclass
class Translation:
    """Result of translating one cron ref's schedule."""

    schedule: dict[str, Any] | None = None
    note: str | None = None  # informational — e.g. "weekly defaulted to Monday"
    skip_reason: str | None = None  # non-None ⇒ leave this row alone


def translate_schedule(meta: dict[str, Any]) -> Translation:
    """Translate a cron ref's ``meta`` (recurring/next_fire_at/catch_up) into
    the new ``meta.schedule`` shape. Returns a :class:`Translation`;
    ``skip_reason`` set means "don't touch this row automatically."
    """
    recurring = meta.get("recurring")
    catch_up = bool(meta.get("catch_up", False))

    if not recurring:
        # One-shot.
        next_fire_at = meta.get("next_fire_at")
        if not next_fire_at:
            return Translation(skip_reason="no next_fire_at on a one-shot cron")
        return Translation(schedule={"at": next_fire_at, "catch_up": catch_up})

    s = str(recurring).strip().lower()
    if s == "hourly":
        return Translation(schedule={"cron": "0 * * * *", "backfill_missed": catch_up})
    if s == "daily":
        return Translation(schedule={"cron": "0 0 * * *", "backfill_missed": catch_up})
    if s == "weekly":
        return Translation(
            schedule={"cron": "0 0 * * 1", "backfill_missed": catch_up},
            note="old 'weekly' was day-agnostic; defaulted to Monday 00:00",
        )
    if s.startswith("every "):
        rest = s[len("every ") :]
        parts = rest.split()
        if len(parts) != 2:
            return Translation(skip_reason=f"unparseable 'every' form: {recurring!r}")
        try:
            n = int(parts[0])
        except ValueError:
            return Translation(skip_reason=f"unparseable 'every' count: {recurring!r}")
        unit = parts[1]
        if unit in ("minute", "minutes", "min", "m"):
            if not (1 <= n <= 59):
                return Translation(
                    skip_reason=f"every {n} minute(s) outside 1..59 — no clean cron form"
                )
            cron = f"*/{n} * * * *" if n != 1 else "* * * * *"
            return Translation(schedule={"cron": cron, "backfill_missed": catch_up})
        if unit in ("hour", "hours", "hr", "h"):
            if not (1 <= n <= 23):
                return Translation(
                    skip_reason=f"every {n} hour(s) outside 1..23 — no clean cron form"
                )
            cron = f"0 */{n} * * *" if n != 1 else "0 * * * *"
            return Translation(schedule={"cron": cron, "backfill_missed": catch_up})
        if unit in ("day", "days", "d"):
            if n != 1:
                return Translation(
                    skip_reason=f"every {n} day(s) — only every-1-day has a clean cron form"
                )
            return Translation(
                schedule={"cron": "0 0 * * *", "backfill_missed": catch_up}
            )
        return Translation(skip_reason=f"unknown 'every' unit: {recurring!r}")

    if s.startswith("daily@"):
        hhmm = s[len("daily@") :]
        try:
            hh, mm = hhmm.split(":")
            hh_i, mm_i = int(hh), int(mm)
        except ValueError:
            return Translation(skip_reason=f"unparseable daily@ time: {recurring!r}")
        return Translation(
            schedule={"cron": f"{mm_i} {hh_i} * * *", "backfill_missed": catch_up}
        )

    if s.startswith("weekly@"):
        rest = s[len("weekly@") :]
        try:
            dow_name, hhmm = rest.split("@")
            hh, mm = hhmm.split(":")
            hh_i, mm_i = int(hh), int(mm)
        except ValueError:
            return Translation(skip_reason=f"unparseable weekly@ form: {recurring!r}")
        dow = _WEEKDAY_TO_CRON_DOW.get(dow_name)
        if dow is None:
            return Translation(skip_reason=f"unknown weekday in {recurring!r}")
        return Translation(
            schedule={
                "cron": f"{mm_i} {hh_i} * * {dow}",
                "backfill_missed": catch_up,
            }
        )

    return Translation(skip_reason=f"unrecognised recurring expression: {recurring!r}")


@dataclass
class Row:
    ref_id: int
    title: str
    meta: dict[str, Any]
    body: str
    tags: list[str] = field(default_factory=list)


def _fetch_cron_rows(conn: Any) -> list[Row]:
    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, r.meta
          FROM refs r
         WHERE r.kind = 'cron' AND r.deleted_at IS NULL
         ORDER BY r.ref_id
        """
    ).fetchall()
    out: list[Row] = []
    for ref_id, title, meta in rows:
        body_row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'cron_payload' ORDER BY ord LIMIT 1",
            (ref_id,),
        ).fetchone()
        body = body_row[0] if body_row else (title or "")
        tag_rows = conn.execute(
            "SELECT t.namespace, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
        tags = [
            f"{ns}:{val}" if ns not in ("open", "OPEN") else val
            for ns, val in tag_rows
        ]
        out.append(Row(ref_id=int(ref_id), title=title or "", meta=meta or {}, body=body, tags=tags))
    return out


def _migrate_one(store: Any, row: Row, *, commit: bool, watches_root: int) -> str:
    """Migrate one cron row. Returns a one-line report string."""
    translation = translate_schedule(row.meta)
    if translation.skip_reason:
        return f"cron {row.ref_id}: SKIPPED — {translation.skip_reason}"

    old_status = str(row.meta.get("status") or "scheduled")
    new_status = {
        "scheduled": "open",
        "paused": "paused",
        "fired": "done",
        "expired": "done",
        "cancelled": "done",
    }.get(old_status, "open")

    target = row.meta.get("target")
    carried_tags = [
        t
        for t in row.tags
        if not t.startswith("STATUS:") and t not in ("level:recurring",)
    ]
    tags = ["level:recurring", *carried_tags]

    note = f" ({translation.note})" if translation.note else ""
    report = (
        f"cron {row.ref_id} -> todo: schedule={translation.schedule}{note}, "
        f"deliver.target={target!r}, STATUS:{new_status}, tags={tags}"
    )
    if not commit:
        return "[dry-run] " + report

    from precis.store.types import Tag

    meta: dict[str, Any] = {"schedule": translation.schedule}
    if target:
        meta["deliver"] = {"target": str(target)}
    # Carry the old fire history as informational-only fields (not read by
    # the new engine, but useful audit trail on the new row).
    if row.meta.get("fire_count"):
        meta["migrated_fire_count"] = row.meta["fire_count"]
    if row.meta.get("last_fired_at"):
        meta["migrated_last_fired_at"] = row.meta["last_fired_at"]

    with store.tx() as conn:
        # Mirrors TodoHandler._create: the full payload text is the task
        # line (refs.title), same as how a normal put(text=...) stores it —
        # no separate body chunk needed (unlike the old cron ref, which
        # split payload into a searchable chunk because the ref title alone
        # wasn't embedded/keyworded on that kind). Parented under Watches,
        # same default TodoHandler.put() applies to a level:recurring root.
        new_ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=row.body or row.title,
            meta=meta,
            parent_id=watches_root,
            conn=conn,
        )
        for t in tags:
            store.add_tag(
                new_ref.id, Tag.parse_strict(t, kind="todo"), set_by="system", conn=conn
            )
        store.add_tag(
            new_ref.id,
            Tag.closed("STATUS", new_status),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.execute(
            "UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (row.ref_id,)
        )

    return report + f" [committed as todo id={new_ref.id}]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--database-url", default=os.environ.get("PRECIS_DATABASE_URL", _DEFAULT_DB)
    )
    ap.add_argument(
        "--commit",
        action="store_true",
        help="Actually write + soft-delete (default: dry-run report only).",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from precis.store import Store

    store = Store.connect(args.database_url, min_size=1, max_size=2)
    try:
        with store.pool.connection() as conn:
            rows = _fetch_cron_rows(conn)
        if not rows:
            log.info("no live kind='cron' refs found — nothing to migrate")
            return 0
        log.info(
            "%d live cron ref(s) found%s",
            len(rows),
            "" if args.commit else " (dry-run — pass --commit to write)",
        )
        watches_root = -1
        if args.commit:
            from precis.workers.schedule.seed import ensure_watches_root

            watches_root = ensure_watches_root(store)
        n_migrated = n_skipped = 0
        for row in rows:
            report = _migrate_one(store, row, commit=args.commit, watches_root=watches_root)
            log.info(report)
            if "SKIPPED" in report:
                n_skipped += 1
            else:
                n_migrated += 1
        log.info(
            "done: %d migrated, %d skipped (needs manual handling)",
            n_migrated,
            n_skipped,
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
