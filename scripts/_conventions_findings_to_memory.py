"""Move the conventions-import findings to ``kind='memory'`` (Q12 (d)).

``precis conventions import`` minted ~160 rule/enforcer refs as
``kind='finding'`` with ``meta.source='conventions-import'`` — but a
convention is operational memory, not evidence: the ruling sends them to
md/memory, and the importer now needs a one-time move.

The move is a re-kind, not a copy: ``UPDATE refs.kind`` finding ->
memory plus ``finding_body`` -> ``memory_body`` on each ref's body
chunks. The ``ref_id`` (and every tag, link, and logbook row) survives;
only the agent-facing handle prefix changes ``fi<N>`` -> ``me<N>``.
Anything still pointing at ``fi<N>`` keeps resolving (handles are
kind-prefix + ref_id, and the ref itself is not retired).

Selection is deliberately narrow: ``kind='finding'`` AND
``meta->>'source' = 'conventions-import'`` AND live. A hand-made finding
carrying the same key is included (it is the same import convention);
anything else is untouched. One transaction per ref so a bad row leaves
the rest committable.

Dry run by default (read-only report); ``--apply`` writes.
Database URL from ``PRECIS_DATABASE_URL`` (see ``scripts/prod-precis``).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

_SELECT = """
SELECT r.ref_id, r.title
  FROM refs r
 WHERE r.kind = 'finding'
   AND r.retired_at IS NULL
   AND r.meta->>'source' = 'conventions-import'
 ORDER BY r.ref_id
"""


def find_refs(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(_SELECT).fetchall()
    return [{"ref_id": int(r[0]), "title": str(r[1] or "")} for r in rows]


def run(store: Any, *, apply: bool) -> list[str]:
    out: list[str] = []
    with store.tx() as conn:
        refs = find_refs(conn)
    out.append(f"{len(refs)} conventions-import finding(s) to move to kind='memory'")
    moved = 0
    for r in refs:
        line = f"fi{r['ref_id']}: {r['title'][:70]}"
        if not apply:
            out.append(f"  would move {line}")
            continue
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET kind = 'memory' WHERE ref_id = %s "
                "AND kind = 'finding' AND retired_at IS NULL",
                (r["ref_id"],),
            )
            conn.execute(
                "UPDATE chunks SET chunk_kind = 'memory_body' "
                "WHERE ref_id = %s AND chunk_kind = 'finding_body'",
                (r["ref_id"],),
            )
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (
                    '{"moved_from_kind": "finding", "moved_by": "conventions_findings_to_memory"}',
                    r["ref_id"],
                ),
            )
        moved += 1
        out.append(f"  moved {line} -> me{r['ref_id']}")
    out.append(f"{moved} ref(s) moved" if apply else "dry run — nothing written")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="perform the move")
    args = p.parse_args()

    dsn = os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        sys.exit("set PRECIS_DATABASE_URL (scripts/prod-precis exports it)")
    from precis.store.store import Store

    store = Store.connect(dsn)
    try:
        for line in run(store, apply=args.apply):
            print(line)
    finally:
        store.close()
    if not args.apply:
        print("dry run — pass --apply to move")


if __name__ == "__main__":
    main()
