"""writer-relabel — correct the stale writer model on the AIxMAT 2026-11-18
reading-wave records (gr727770).

The reading waves of 2026-11-18 ran gpt-6.2-astra at reasoning effort xhigh,
but the reading brief hard-coded the stale id in
``args={'writer': {'model': 'gpt-6-astra'}}`` (~/.codex/config.toml had
changed after the pilot). About 630 findings, 419 setup memories, ~18
summary memories and ~1047 measure rows from those Codex sessions carry the
older model; the MCP doors cannot correct it (a measure row's meta is
frozen; ``meta.model`` is rejected on finding edits, gr715492 / gr715597).
This script is the one-off database update, keyed as tightly as the schema
allows:

* **findings and memories** (refs) — ``meta.filed_by`` carries the client
  and model, so the scope is exact: ``client = codex-mcp-client``,
  ``model = gpt-6-astra``, created on 2026-11-18. The update rewrites only
  ``meta.filed_by.model``. ``filed_by`` is bookkeeping, not a covered meta
  key for these kinds (0185), so the relabel changes no content sha,
  stales no review and logs no revision row — this script and the gripe
  are its audit trail.
* **Measures** have an actor and run key but no client or session ID.
  Dry run lists candidate IDs and provenance fields for operator verification.
  Apply requires a file of verified IDs and rechecks model and time bounds.
  The default cutoff is 17:00 UTC.
* **Reviews** pin the measure SHA, which covers model. Apply holds a lock
  on the review ledger from the pin check through commit, so a concurrent
  insert cannot slip between the check and the update.

Historical revisions retain their writer stamps.

Dry run: ``scripts/_prod-run scripts/writer-relabel``.
Apply: ``scripts/_prod-run scripts/writer-relabel --apply --measure-ids FILE``.
FILE contains whitespace-separated numeric IDs verified against reading-wave
provenance. An empty file explicitly selects zero measures. Already corrected
IDs in the window are accepted on retry.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from psycopg import Connection

#: The stale writer id the reading brief hard-coded (gr727770).
OLD_MODEL = "gpt-6-astra"
#: The model those sessions actually ran.
NEW_MODEL = "gpt-6.2-astra"
#: The day the affected waves ran.
DAY = date(2026, 11, 18)
#: Default window end: from ~17:00 UTC the drivers read the id from
#: ~/.codex/config.toml and labelled their rows correctly themselves.
DEFAULT_CUTOFF = time(17, 0)
#: The client stamp the Codex sessions filed their refs under.
CLIENT = "codex-mcp-client"
#: Kinds whose filed_by stamp this fix rewrites.
REF_KINDS = ("finding", "memory")


def _bounds(day: date, cutoff: time) -> tuple[datetime, datetime, datetime]:
    return (
        datetime.combine(day, time.min, UTC),
        datetime.combine(day + timedelta(days=1), time.min, UTC),
        datetime.combine(day, cutoff, UTC),
    )


def _candidates(
    conn: Connection, start: datetime, end: datetime, measure_end: datetime
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """List scoped refs and measures that need provenance verification."""
    refs = conn.execute(
        "SELECT r.ref_id, r.kind FROM refs r "
        " WHERE r.kind = ANY(%s) "
        "   AND r.meta -> 'filed_by' ->> 'client' = %s "
        "   AND r.meta -> 'filed_by' ->> 'model' = %s "
        "   AND r.created_at >= %s AND r.created_at < %s "
        " ORDER BY r.ref_id",
        (list(REF_KINDS), CLIENT, OLD_MODEL, start, end),
    ).fetchall()
    measures = conn.execute(
        "SELECT m.id, m.created_at, m.actor, m.run_key, m.subject_ref_id, "
        "       m.source_ref_id FROM measures m "
        " WHERE m.model = %s "
        "   AND m.created_at >= %s AND m.created_at < %s "
        " ORDER BY m.id",
        (OLD_MODEL, start, measure_end),
    ).fetchall()
    return refs, measures


def _verified_targets(
    conn: Connection, ids: list[int], start: datetime, measure_end: datetime
) -> list[int]:
    """Lock and validate the supplied IDs, returning those still mislabeled."""
    rows = conn.execute(
        "SELECT id, model FROM measures "
        " WHERE id = ANY(%s) AND model = ANY(%s) "
        "   AND created_at >= %s AND created_at < %s "
        " ORDER BY id FOR UPDATE",
        (ids, [OLD_MODEL, NEW_MODEL], start, measure_end),
    ).fetchall()
    missing = sorted(set(ids) - {row[0] for row in rows})
    if missing:
        raise ValueError(f"verified measure IDs outside the repair scope: {missing}")
    return [row[0] for row in rows if row[1] == OLD_MODEL]


def _pinned_reviews(conn: Connection, ids: list[int]) -> list[int]:
    """Find content-basis reviews pinned on the current measure SHA."""
    rows = conn.execute(
        "SELECT DISTINCT r.target_id FROM reviews r "
        " WHERE r.target_kind = 'measure' AND r.target_id = ANY(%s) "
        "   AND r.sha_basis = 'content' "
        "   AND r.content_sha = precis_target_sha('measure', r.target_id) "
        " ORDER BY r.target_id",
        (ids,),
    ).fetchall()
    return [row[0] for row in rows]


def _apply(
    conn: Connection,
    *,
    start: datetime,
    end: datetime,
    measure_end: datetime,
    ids: list[int],
) -> tuple[int, int]:
    """Validate and update the selected rows in the caller's transaction."""
    if ids:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        # INSERT takes ROW EXCLUSIVE before evaluating the review's SHA.
        # Wait for earlier writers, then check under a fresh RC snapshot.
        conn.execute("LOCK TABLE reviews IN SHARE ROW EXCLUSIVE MODE")
        ids = _verified_targets(conn, ids, start, measure_end)
        pinned = _pinned_reviews(conn, ids)
        if pinned:
            raise ValueError(f"measure reviews pin the current SHA for IDs {pinned}")

    conn.execute("SELECT set_config('precis.allow_writer_relabel', 'on', true)")
    refs_cur = conn.execute(
        "UPDATE refs SET meta = jsonb_set("
        "    meta, '{filed_by,model}', to_jsonb(%s::text)) "
        " WHERE kind = ANY(%s) "
        "   AND meta -> 'filed_by' ->> 'client' = %s "
        "   AND meta -> 'filed_by' ->> 'model' = %s "
        "   AND created_at >= %s AND created_at < %s",
        (NEW_MODEL, list(REF_KINDS), CLIENT, OLD_MODEL, start, end),
    )
    measures_cur = conn.execute(
        "UPDATE measures SET model = %s WHERE id = ANY(%s)",
        (NEW_MODEL, ids),
    )
    return refs_cur.rowcount or 0, measures_cur.rowcount or 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="dry run unless --apply; prod via scripts/_prod-run",
    )
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF.strftime("%H:%M"),
        metavar="HH:MM",
        help="window end in UTC on "
        f"{DAY.isoformat()} (default {DEFAULT_CUTOFF.strftime('%H:%M')}: "
        "later rows already carry the correct model)",
    )
    p.add_argument(
        "--measure-ids",
        type=Path,
        metavar="FILE",
        help="whitespace-separated verified measure IDs; required with --apply",
    )
    args = p.parse_args(argv)
    if args.apply != (args.measure_ids is not None):
        p.error("--apply and --measure-ids FILE must be supplied together")

    try:
        cutoff = time.fromisoformat(args.cutoff)
        if cutoff.tzinfo is not None:
            raise ValueError("specify the UTC cutoff without a time zone offset")
        ids = (
            sorted(
                {
                    int(token)
                    for token in args.measure_ids.read_text(encoding="utf-8").split()
                }
            )
            if args.apply
            else []
        )
        if any(not 0 < mid < 2**63 for mid in ids):
            raise ValueError("measure IDs must be positive bigint values")
    except (OSError, ValueError) as ex:
        p.error(str(ex))

    start, end, measure_end = _bounds(DAY, cutoff)
    from _common import open_store

    store, _cfg = open_store()
    try:
        with store.pool.connection() as conn:
            if not args.apply:
                refs, measures = _candidates(conn, start, end, measure_end)
                print(
                    f"scope: {CLIENT} / {OLD_MODEL} on {DAY.isoformat()}, "
                    f"measures before {measure_end:%H:%M}Z"
                )
                print(f"findings/memories: {len(refs)} candidate(s)")
                for ref_id, kind in refs:
                    print(f"    {ref_id} [{kind}]")
                print(f"measures: {len(measures)} candidate(s)")
                for mid, created, actor, run_key, subject, source in measures:
                    print(
                        f"    {mid} {created.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ} "
                        f"actor={actor} run={run_key} subject={subject} source={source}"
                    )
                print(
                    "dry run: verify measure provenance before using --apply --measure-ids FILE"
                )
                return 0

            try:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    n_refs, n_measures = _apply(
                        conn,
                        start=start,
                        end=end,
                        measure_end=measure_end,
                        ids=ids,
                    )
            except (
                ValueError,
                psycopg.errors.CheckViolation,
                psycopg.errors.LockNotAvailable,
            ) as ex:
                print(f"refusing: {ex}")
                return 1
            print(f"applied: {n_refs} ref(s), {n_measures} measure row(s)")
            return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
