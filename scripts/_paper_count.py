"""paper-count — show paper / kind / provider counts in the precis store.

Usage (via the `paper-count` shell wrapper):

    paper-count                  # just the paper total
    paper-count --by-kind        # all kinds, sorted by count
    paper-count --by-provider    # paper rows grouped by provider
    paper-count --recent         # most recently ingested papers

Reads `PRECIS_DATABASE_URL` from the environment (the wrapper sets a
default pointing at the local `precis` database).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(
        description="Count paper refs in the precis-mcp store.",
    )
    p.add_argument(
        "--by-kind",
        action="store_true",
        help="Show counts broken down by ref kind, not just papers.",
    )
    p.add_argument(
        "--by-provider",
        action="store_true",
        help="Show paper counts grouped by provider (crossref / arxiv / …).",
    )
    p.add_argument(
        "--recent",
        type=int,
        nargs="?",
        const=10,
        default=None,
        metavar="N",
        help="List the N most-recently-ingested papers (default 10).",
    )
    args = p.parse_args()

    store, cfg = open_store()
    try:
        n_papers = store.count_refs(kind="paper")
        print(f"papers: {n_papers}")

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM blocks b "
                "JOIN refs r ON r.id = b.ref_id "
                "WHERE r.kind = 'paper' AND r.deleted_at IS NULL"
            ).fetchone()
        if row is not None:
            print(f"paper blocks: {row[0]}")

        if args.by_kind:
            with store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT kind, count(*) FROM refs "
                    "WHERE deleted_at IS NULL "
                    "GROUP BY kind ORDER BY count(*) DESC"
                ).fetchall()
            print("\nby kind:")
            for kind, n in rows:
                print(f"  {kind:<14} {n}")

        if args.by_provider:
            with store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT provider, count(*) FROM refs "
                    "WHERE kind = 'paper' AND deleted_at IS NULL "
                    "GROUP BY provider ORDER BY count(*) DESC"
                ).fetchall()
            print("\nby provider:")
            for provider, n in rows:
                print(f"  {(provider or '<null>'):<14} {n}")

        if args.recent is not None:
            with store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT slug, title, created_at "
                    "FROM refs "
                    "WHERE kind = 'paper' AND deleted_at IS NULL "
                    "ORDER BY created_at DESC NULLS LAST "
                    "LIMIT %s",
                    (args.recent,),
                ).fetchall()
            print(f"\nmost recent {args.recent}:")
            for slug, title, created in rows:
                stamp = created.strftime("%Y-%m-%d %H:%M") if created else "?"
                short = (title or "")[:80]
                print(f"  {stamp}  {slug:<40}  {short}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
