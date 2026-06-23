"""ADR 0036 chunk-handle backfill pass.

Mints a universal handle (``handle_registry.mint(kind, chunk=True)``) for
any chunk lacking one whose owning ref is an addressable chunk kind. A
lazy backfill, so it needs **no per-insert-site wiring** across the ~5
chunk writers (ingest db_writer, blocks_ops, pres, chase, draft) — it
just drains ``chunks.handle IS NULL``.

``draft`` is excluded: draft chunks already carry their own ADR-0033
base-58 handle (``chunks.handle`` non-NULL), so ``handle IS NULL`` skips
them and the kind filter keeps a stray NULL-handle draft chunk from being
minted a ``dc`` handle here. Unifying drafts onto this scheme is a later
slice (the wipe).

Idiomatic derived-queue pass (ADR 0007/0017): claim ``FOR UPDATE SKIP
LOCKED``, mint, ``UPDATE``, all in one transaction so the row lock is held
only for the batch and is multi-host safe.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation

from precis.utils import handle_registry

#: Kinds whose chunks get a handle — every chunk-coded kind except draft.
#: Derived from the registry so a new chunk-bearing kind is covered the
#: moment it gets a chunk code (no second list to keep in sync).
_KINDS: list[str] = sorted(k for k in handle_registry.CHUNK_CODES if k != "draft")


def _claim(conn: Connection, *, limit: int) -> list[tuple[int, str]]:
    """Lock up to ``limit`` handle-less chunks of addressable kinds."""
    rows = conn.execute(
        "SELECT c.chunk_id, r.kind "
        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        # ord >= 0: body chunks only — derived/card chunks (ord < 0) are
        # regenerable (DELETE+INSERT) and not addressable, so no handle.
        "WHERE c.handle IS NULL AND c.ord >= 0 AND r.kind = ANY(%s) "
        "ORDER BY c.chunk_id "
        "LIMIT %s "
        "FOR UPDATE OF c SKIP LOCKED",
        (_KINDS, limit),
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _mint_one(conn: Connection, chunk_id: int, kind: str) -> bool:
    """Mint + write a unique chunk handle; retry under a savepoint on the
    (cosmically rare) clash. Returns False if it never lands."""
    for _ in range(8):
        candidate = handle_registry.mint(kind, chunk=True)
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE chunks SET handle = %s WHERE chunk_id = %s",
                    (candidate, chunk_id),
                )
            return True
        except UniqueViolation:
            continue
    return False


def run_chunk_handles_pass(store: Any, *, batch_size: int = 100) -> dict[str, int]:
    """Mint handles for one batch of handle-less chunks. Returns counts."""
    ok = 0
    failed = 0
    with store.pool.connection() as conn:
        batch = _claim(conn, limit=batch_size)
        for chunk_id, kind in batch:
            if _mint_one(conn, chunk_id, kind):
                ok += 1
            else:
                failed += 1
    return {"claimed": len(batch), "ok": ok, "failed": failed}
