"""Re-point the agent-written ``quest --supports--> finding`` edges.

The 2026-09-12..19 norr-her agent writer filed ~960 edges as
``quest --supports--> finding`` — backwards and on the wrong relation:
``supports`` is an evidence/stance edge (finding → finding/paper); a quest
is a container, and "this finding is in the quest's service" is
``finding --serves--> quest`` (the direction ``view='tree'`` and health
read). This script rewrites each such edge in place:

1. insert ``finding --serves--> quest`` (idempotent — ``add_link``
   collapses onto an existing row),
2. delete the backward ``supports`` row,

one transaction per edge so a bad edge leaves the rest committable.

Selection is deliberately narrow: ``relation='supports'`` AND src kind
``quest`` AND dst kind ``finding`` AND ``set_by='agent'`` AND
``created_at`` in [2026-09-12, 2026-09-20) UTC (the writer's window). A
``supports`` edge between two findings, or a human-authored one, is
untouched. ``--window`` overrides the date range.

Dry run by default (read-only report); ``--apply`` writes.
Database URL from ``PRECIS_DATABASE_URL`` (see ``scripts/prod-precis``).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

_WINDOW_START = "2026-09-12"
_WINDOW_END = "2026-09-20"  # exclusive — the writer ran 09-12..19

_SELECT = """
SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.set_by, l.meta,
       l.created_at
  FROM links l
  JOIN refs qs ON qs.ref_id = l.src_ref_id AND qs.kind = 'quest'
  JOIN refs fd ON fd.ref_id = l.dst_ref_id AND fd.kind = 'finding'
 WHERE l.relation = 'supports'
   AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
   AND l.set_by = 'agent'
   AND l.created_at >= %s::date
   AND l.created_at <  %s::date
 ORDER BY l.link_id
"""


def find_edges(conn: Any, start: str, end: str) -> list[dict[str, Any]]:
    """The backward quest→finding ``supports`` rows in the window."""
    rows = conn.execute(_SELECT, (start, end)).fetchall()
    return [
        {
            "link_id": int(r[0]),
            "quest_id": int(r[1]),
            "finding_id": int(r[2]),
            "set_by": str(r[3]),
            "meta": dict(r[4] or {}),
            "created_at": r[5],
        }
        for r in rows
    ]


def run(store: Any, *, start: str, end: str, apply: bool) -> list[str]:
    """Report (and with ``apply`` rewrite) every matching edge."""
    out: list[str] = []
    with store.tx() as conn:
        edges = find_edges(conn, start, end)
    out.append(
        f"{len(edges)} backward quest→finding 'supports' edge(s) in [{start}, {end})"
    )
    moved = skipped = 0
    for e in edges:
        line = (
            f"link {e['link_id']}: qu{e['quest_id']} -supports-> "
            f"fi{e['finding_id']} ({e['created_at']:%Y-%m-%d})"
        )
        if not apply:
            out.append(
                f"  would re-point → fi{e['finding_id']} -serves-> "
                f"qu{e['quest_id']}  ({line})"
            )
            continue
        with store.tx() as conn:
            # insert first, delete second: a failed insert leaves the
            # old row, a failed delete rolls the insert back.
            store.add_link(
                src_ref_id=e["finding_id"],
                dst_ref_id=e["quest_id"],
                relation="serves",
                set_by=e["set_by"],
                meta=dict(e["meta"], repointed_from=f"supports:{e['link_id']}"),
                conn=conn,
            )
            conn.execute("DELETE FROM links WHERE link_id = %s", (e["link_id"],))
            row = conn.execute(
                "SELECT 1 FROM links WHERE link_id = %s", (e["link_id"],)
            ).fetchone()
            assert row is None, f"delete of link {e['link_id']} did not land"
        out.append(
            f"  re-pointed → fi{e['finding_id']} -serves-> qu{e['quest_id']}  ({line})"
        )
        moved += 1
    if apply:
        out.append(f"applied: {moved} re-pointed, {skipped} skipped")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="re-point the edges")
    p.add_argument(
        "--window",
        nargs=2,
        metavar=("START", "END"),
        default=[_WINDOW_START, _WINDOW_END],
        help="created_at window, [START, END) ISO dates "
        f"(default {_WINDOW_START}..{_WINDOW_END})",
    )
    args = p.parse_args()
    dsn = os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        sys.exit("PRECIS_DATABASE_URL is not set")
    from precis.store import Store

    store = Store.connect(dsn)
    try:
        for line in run(
            store, start=args.window[0], end=args.window[1], apply=args.apply
        ):
            print(line)
    finally:
        store.close()
    if not args.apply:
        print("dry run — pass --apply to re-point")


if __name__ == "__main__":
    main()
