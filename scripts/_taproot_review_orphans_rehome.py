"""Re-home the June ``TAPROOT:review`` orphan findings (fi34814–35098).

The 2026-06 norr-her TAPROOT:review batch left 35 review findings
(``kind='finding'``, ref_id 34814..35098, live, carrying the
``TAPROOT:review`` tag) with no inbound link — orphaned when the batch's
parent was pruned. This script re-homes each under a caller-named parent
with a ``part-of`` edge (``orphan --part-of--> parent``), which is the
containment relation the review queue reads.

Selection is deliberately narrow: ``kind='finding'`` AND live AND
``ref_id`` in the recorded range AND the ``TAPROOT:review`` tag AND no
inbound link of any relation. Anything already linked is reported but
untouched. ``--parent <ref_id>`` is required with ``--apply`` — the script
never guesses the new home. One transaction per finding so a bad row
leaves the rest committable.

Dry run by default (read-only report); ``--apply --parent <ref_id>``
writes. Database URL from ``PRECIS_DATABASE_URL`` (see
``scripts/prod-precis``).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

_LO, _HI = 34814, 35098

_SELECT = """
SELECT r.ref_id, r.title
  FROM refs r
 WHERE r.kind = 'finding'
   AND r.retired_at IS NULL
   AND r.ref_id BETWEEN %s AND %s
   AND EXISTS (
       SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
        WHERE rt.ref_id = r.ref_id
          AND t.namespace = 'TAPROOT' AND t.value = 'review')
   AND NOT EXISTS (
       SELECT 1 FROM links l WHERE l.dst_ref_id = r.ref_id)
 ORDER BY r.ref_id
"""


def find_orphans(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(_SELECT, (_LO, _HI)).fetchall()
    return [{"ref_id": int(r[0]), "title": str(r[1] or "")} for r in rows]


def run(store: Any, *, parent_ref_id: int | None, apply: bool) -> list[str]:
    out: list[str] = []
    with store.tx() as conn:
        orphans = find_orphans(conn)
        if apply:
            parent = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (parent_ref_id,),
            ).fetchone()
            if parent is None:
                raise SystemExit(
                    f"--parent ref_id={parent_ref_id} does not resolve to a live ref"
                )
    out.append(f"{len(orphans)} TAPROOT:review orphan finding(s) in fi{_LO}–fi{_HI}")
    linked = 0
    for o in orphans:
        line = f"fi{o['ref_id']}: {o['title'][:70]}"
        if not apply:
            out.append(f"  would re-home {line}")
            continue
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by, meta) "
                "SELECT %s, %s, 'part-of', 'agent', '{}'::jsonb "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM links WHERE src_ref_id = %s "
                "  AND dst_ref_id = %s AND relation = 'part-of')",
                (o["ref_id"], parent_ref_id, o["ref_id"], parent_ref_id),
            )
        linked += 1
        out.append(f"  re-homed {line} under ref:{parent_ref_id}")
    out.append(
        f"{linked} finding(s) re-homed" if apply else "dry run — nothing written"
    )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write the part-of edges")
    p.add_argument(
        "--parent",
        type=int,
        default=None,
        metavar="REF_ID",
        help="the live ref the orphans re-home under (required with --apply)",
    )
    args = p.parse_args()
    if args.apply and args.parent is None:
        p.error("--apply requires --parent <ref_id>")

    dsn = os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        sys.exit("set PRECIS_DATABASE_URL (scripts/prod-precis exports it)")
    from precis.store.store import Store

    store = Store.connect(dsn)
    try:
        for line in run(store, parent_ref_id=args.parent, apply=args.apply):
            print(line)
    finally:
        store.close()
    if not args.apply:
        print("dry run — pass --apply --parent <ref_id> to re-home")


if __name__ == "__main__":
    main()
