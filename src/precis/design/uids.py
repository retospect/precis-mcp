"""Stable block identity — the ``uid`` mint.

A design's blocks are saved by retire-all/reinsert-all in the renting
persist layer, so a block **row id** is rebuilt on every save and can never
be an external identity: not a viewer path leaf, not a cache key, not an FK
target. The uid is that identity instead. It is minted once, carried
forward across saves as ordinary column data, and **preserved by branch
copies** — which is what makes a naive full-copy branch diffable
block-by-block (design-state-core.md items 2 and 5 depend on each other
exactly here).

One global sequence, not one per design: a uid means the same block whether
it is read through its own design, a cross-design instance, or a branch, so
there is nothing to qualify it with. bigint, matching house style.

Uids are never reused and the sequence is never reset — a stale reference to
a deleted block must fail to resolve, not silently hit a different one.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from precis.design._db import read_conn

#: The sequence, created by migration ``0162_design_core.sql``.
UID_SEQUENCE = "design_block_uid_seq"


def mint_uid(store: Any, *, conn: Connection | None = None) -> int:
    """Mint one fresh block uid."""
    return mint_uids(store, 1, conn=conn)[0]


def mint_uids(store: Any, count: int, *, conn: Connection | None = None) -> list[int]:
    """Mint ``count`` fresh block uids, in ascending order.

    One round trip for the whole batch — a save that adds twenty blocks
    should not make twenty calls. ``count`` of 0 returns an empty list
    rather than erroring, so a caller can pass ``len(new_blocks)`` blindly.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if count == 0:
        return []
    # nextval() is transaction-safe by design: it never rolls back, so two
    # concurrent saves can't be handed the same uid even when one aborts.
    # The gap a rollback leaves is deliberate and harmless.
    # The sequence name is interpolated, not bound: nextval() takes a
    # regclass, and a bound text parameter doesn't resolve to one. It is a
    # module constant, never caller input.
    with read_conn(store, conn) as c:
        rows = c.execute(
            f"SELECT nextval('{UID_SEQUENCE}') FROM generate_series(1, %s)",
            (count,),
        ).fetchall()
    return [int(r[0]) for r in rows]
