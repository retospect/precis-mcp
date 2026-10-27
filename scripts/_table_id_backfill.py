"""table-id-backfill — mint stable row/column ids on pre-schema table chunks.

Usage (via the ``table-id-backfill`` shell wrapper):

    table-id-backfill              # dry run: report what would change
    table-id-backfill --apply      # write it

Spreadsheet-tables slice 1a (docs/backlog/spreadsheet-tables.md §13): every
canonical ``meta.table`` gains ``columns`` + ``rowmeta`` carrying random
4-char ids and fractional ``pos`` keys. The pass walks live
``chunk_kind='table'`` chunks on **draft refs only** whose ``meta`` carries
``table`` but whose ``meta.table`` has no ``columns`` — ingested paper
tables are not migrated (spec §13: the address form is reserved for them),
and a table the pass misses mints ids on its first write anyway, since
``normalize_table`` runs on every table write. Ids are random, so this is a
Python pass, not SQL. Re-running is a no-op (the ``columns`` guard makes it
idempotent).

Writes go through :meth:`DraftOps.patch_chunk_meta` — a ``meta``-only
UPDATE that keeps ``chunk_id`` (and ``dc<N>``) stable and logs an
``edited`` chunk_event; the append-only rule guards ``text``/``body``,
neither of which is touched. Reads ``PRECIS_DATABASE_URL`` (dev DB by
default; prod through the gitignored overlay, never a literal). NEVER run
``--apply`` against prod without Reto's go.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

if TYPE_CHECKING:
    from precis.store import Store

_BATCH = 500

#: Live table chunks on draft refs whose ``meta.table`` lacks ``columns``.
#: ``meta->'table'` is a jsonb object for canonical tables; the ``?``
#: existence check on ``columns`` is the idempotency guard.
_CANDIDATES_SQL = """
SELECT c.chunk_id, c.handle, c.meta
FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
WHERE c.chunk_id > %s
  AND c.retired_at IS NULL
  AND c.chunk_kind = 'table'
  AND r.kind = 'draft'
  AND c.handle IS NOT NULL
  AND c.meta ? 'table'
  AND jsonb_typeof(c.meta -> 'table') = 'object'
  AND NOT (c.meta -> 'table' ? 'columns')
ORDER BY c.chunk_id
LIMIT %s
"""


def backfill_table_ids(
    store: Store, *, apply: bool, report: bool = True
) -> dict[str, int]:
    """Mint ids + ``pos`` keys on every candidate chunk (see module
    docstring). ``apply=False`` only counts. A chunk whose legacy
    ``meta.table`` fails :func:`normalize_table` validation is counted as
    ``errors`` and skipped — it will refuse honestly on its next write,
    which is better than forcing a malformed grid through. Returns
    ``{scanned, changed, errors}``."""
    from precis.utils.table_data import normalize_table

    scanned = changed = errors = 0
    after = 0
    while True:
        with store.pool.connection() as conn:
            rows = conn.execute(_CANDIDATES_SQL, (after, _BATCH)).fetchall()
        if not rows:
            break
        after = int(rows[-1][0])
        for cid, handle, _meta in rows:
            scanned += 1

            # Compute INSIDE the write tx (transform re-reads the live
            # meta under FOR UPDATE): the batch snapshot's meta is stale
            # the moment it is read — an agent edit landing between scan
            # and patch would otherwise be reverted wholesale by the
            # overwrite. A chunk that gained ``columns`` (or lost
            # ``table``) in the meantime is skipped, not clobbered.
            def _patch(m: dict) -> dict | None:
                tbl = m.get("table")
                if not isinstance(tbl, dict) or "columns" in tbl:
                    return None
                return {"table": normalize_table(tbl)}

            try:
                if apply:
                    wrote = store.drafts.patch_chunk_meta(handle, transform=_patch)
                    if not wrote:
                        continue
                else:
                    normalize_table((_meta or {})["table"])
            except Exception as e:  # report and continue
                errors += 1
                if report:
                    print(f"  chunk {cid} (¶{handle}): normalize refused — {e}")
                continue
            changed += 1
    if report:
        mode = "applied" if apply else "dry run"
        print(f"{mode}: scanned={scanned} changed={changed} errors={errors}")
    return {"scanned": scanned, "changed": changed, "errors": errors}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = p.parse_args()

    store, _cfg = open_store()
    try:
        backfill_table_ids(store, apply=args.apply)
    finally:
        store.close()


if __name__ == "__main__":
    main()
