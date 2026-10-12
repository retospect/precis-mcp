"""gr479094 — move twin bodies onto the wanted stubs they were merged into.

The three twins (pa478895, pa478905, pa478923) were retired into stubs
(pa343399, pa343410, pa343409) before ``merge_duplicate`` learned to hand a
body to a bodiless survivor, so the stubs stayed bodiless. For each pair this
asserts the survivor is live and bodiless and the twin is retired with body
chunks, then calls :func:`precis.ingest.dedup._move_body_to_survivor`.

Dry run by default (read-only); ``--apply`` writes, one transaction per pair.
Database URL from ``PRECIS_DATABASE_URL`` (see ``scripts/prod-precis``).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

#: (survivor stub, retired twin)
PAIRS: tuple[tuple[int, int], ...] = (
    (343399, 478895),
    (343410, 478905),
    (343409, 478923),
)
SOURCE = "gr479094"


def body_count(conn: Any, ref_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL",
        (ref_id,),
    ).fetchone()
    return int(row[0])


def check_pair(conn: Any, survivor: int, twin: int) -> tuple[int, int]:
    """Assert the pair is in the expected state; return (survivor, twin) body counts."""
    rows = {
        int(r[0]): (r[1], r[2])
        for r in conn.execute(
            "SELECT ref_id, retired_at, meta->>'superseded_by' "
            "FROM refs WHERE ref_id = ANY(%s)",
            ([survivor, twin],),
        )
    }
    assert set(rows) == {survivor, twin}, f"missing ref in pair {survivor}/{twin}"
    assert rows[survivor][0] is None, f"survivor {survivor} is retired"
    assert rows[twin][0] is not None, f"twin {twin} is not retired"
    assert rows[twin][1] == str(survivor), (
        f"twin {twin} was not superseded by {survivor} ({rows[twin][1]!r})"
    )
    sv, tw = body_count(conn, survivor), body_count(conn, twin)
    assert sv == 0, f"survivor {survivor} already has {sv} body chunks"
    assert tw > 0, f"twin {twin} has no body chunks"
    return sv, tw


def run(store: Any, pairs: tuple[tuple[int, int], ...], *, apply: bool) -> list[str]:
    """Check (and with ``apply`` move) every pair; return report lines."""
    from precis.ingest.dedup import _move_body_to_survivor

    out: list[str] = []
    # Guard every pair before writing any, so a bad pair aborts the whole run.
    with store.tx() as conn:
        for survivor, twin in pairs:
            check_pair(conn, survivor, twin)
    for survivor, twin in pairs:
        with store.tx() as conn:
            sv, tw = check_pair(conn, survivor, twin)
            if not apply:
                out.append(f"pa{survivor} <- pa{twin}: before {sv}/{tw} (dry run)")
                continue
            moved = _move_body_to_survivor(
                store,
                survivor_ref_id=survivor,
                duplicate_ref_id=twin,
                source=SOURCE,
                conn=conn,
            )
            sv2, tw2 = body_count(conn, survivor), body_count(conn, twin)
            assert sv2 == tw and tw2 == 0, (
                f"move incomplete for pa{survivor}: {sv2}/{tw2}"
            )
            out.append(
                f"pa{survivor} <- pa{twin}: moved {moved}; "
                f"chunks {sv}->{sv2}, twin {tw}->{tw2}"
            )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="move the bodies")
    args = p.parse_args()
    dsn = os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        sys.exit("PRECIS_DATABASE_URL is not set")
    from precis.store import Store

    store = Store.connect(dsn)
    try:
        for line in run(store, PAIRS, apply=args.apply):
            print(line)
    finally:
        store.close()
    if not args.apply:
        print("dry run — pass --apply to move")


if __name__ == "__main__":
    main()
