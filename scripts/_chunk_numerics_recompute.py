"""chunk-numerics-recompute — rewrite ``chunks.numerics`` with the wide extractor.

Usage (via the ``chunk-numerics-recompute`` shell wrapper):

    chunk-numerics-recompute              # dry run: how many rows would change
    chunk-numerics-recompute --apply      # write them

``numerics`` is derived metadata, not body: the append-only trigger guards
``text`` only (``docs/conventions/invariants.md``), so this UPDATE leaves
embeddings and summaries alone. Only live chunks whose recomputed array
differs are written; ``extract_numerics_wide`` is a superset of the old
extractor, so nothing already stored is lost. Reads ``PRECIS_DATABASE_URL``
(dev DB by default; prod through the gitignored overlay, never a literal).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

_BATCH = 2000


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = p.parse_args()

    from precis.utils.numerics import extract_numerics_wide

    store, _cfg = open_store()
    after = 0
    scanned = changed = 0
    while True:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, text, numerics FROM chunks "
                "WHERE chunk_id > %s AND retired_at IS NULL AND ord >= 0 "
                "AND text IS NOT NULL ORDER BY chunk_id LIMIT %s",
                (after, _BATCH),
            ).fetchall()
            if not rows:
                break
            after = int(rows[-1][0])
            updates = []
            for cid, text, old in rows:
                scanned += 1
                new = extract_numerics_wide(text)
                if new != list(old or []):
                    updates.append((new, int(cid)))
            changed += len(updates)
            if args.apply and updates:
                with conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE chunks SET numerics = %s WHERE chunk_id = %s", updates
                    )
    mode = "applied" if args.apply else "dry run"
    print(f"{mode}: scanned={scanned} changed={changed}")


if __name__ == "__main__":
    main()
