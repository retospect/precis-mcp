"""Cache state CRUD against v2 schema. Mixin on
:class:`precis.store.Store`.

**Phase 4 stub** — every public method raises ``NotImplementedError``
pointing at the plan file. The v1 implementations selected ``r.id`` /
``r.corpus_id`` / ``r.slug`` (all dropped in v2) and inserted refs
with ``corpus_id`` (also dropped). The v2 rewrite needs:

- ``get_cache_entry_by_slug``: JOIN ``ref_identifiers`` (``id_kind =
  'cite_key'``) instead of ``r.slug``
- ``put_cache_entry`` / ``update_cache_entry``: drop ``corpus_id``
  from INSERT, write the slug to ``ref_identifiers``, replace
  ``DELETE FROM blocks`` with ``DELETE FROM chunks`` (and let the FK
  CASCADE clean ``chunk_embeddings`` + ``chunk_tags``)
- ``_REFS_COLS`` projection of the JOIN: now picks up the v2 slug
  via the correlated subquery in ``_mappers.py``, so the read path
  needs the new column list

Cache-freshness semantics (``fresh_until``, pinned entries via NULL
``fresh_until``, soft-deleted refs excluded) carry over unchanged
to v2 — the ``cache_state`` table itself was preserved.

Mixin assumes the concrete Store provides:

* ``self.pool``
* ``self.tx()``                 — context-manager transaction
* ``self.insert_blocks(...)``   — BlocksMixin method used for the
                                   body-on-replace path (v2: writes
                                   to ``chunks``)
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store.types import Block, BlockInsert, CacheEntry, Ref

_PHASE_4_MSG = (
    "phase 4 (cache v2 rewrite: drop corpus_id, slug→ref_identifiers, "
    "blocks→chunks) not yet implemented; see "
    "/Users/reto/.claude/plans/lively-yawning-kahn.md"
)


class CacheMixin:
    """v2 cache CRUD. All methods stubbed — Phase 4 of the store rewrite."""

    pool: ConnectionPool

    # Provided by the concrete Store / sibling mixins. These stubs
    # document the dependency for readers; MRO resolves the real
    # implementations at runtime when the mixin is composed into
    # :class:`Store`. Calling them on a bare ``CacheMixin`` raises.
    def tx(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        raise NotImplementedError  # pragma: no cover — overridden by BlocksMixin

    def get_cache_entry(
        self,
        *,
        provider: str,
        request_hash: str,
    ) -> tuple[Ref, CacheEntry] | None:
        raise NotImplementedError(f"get_cache_entry: {_PHASE_4_MSG}")

    def get_cache_entry_by_slug(
        self,
        *,
        kind: str,
        slug: str,
    ) -> tuple[Ref, CacheEntry] | None:
        raise NotImplementedError(f"get_cache_entry_by_slug: {_PHASE_4_MSG}")

    def update_cache_entry(
        self,
        *,
        ref_id: int,
        title: str,
        body_blocks: list[BlockInsert],
        ttl_seconds: int | None,
        request_hash: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
        cache_meta: dict[str, Any] | None = None,
    ) -> tuple[Ref, CacheEntry]:
        raise NotImplementedError(f"update_cache_entry: {_PHASE_4_MSG}")

    def put_cache_entry(
        self,
        *,
        kind: str,
        slug: str,
        title: str,
        body_blocks: list[BlockInsert],
        provider: str,
        request_hash: str,
        ttl_seconds: int | None,
        model: str | None = None,
        cost_usd: float | None = None,
        ref_meta: dict[str, Any] | None = None,
        cache_meta: dict[str, Any] | None = None,
    ) -> tuple[Ref, CacheEntry]:
        raise NotImplementedError(f"put_cache_entry: {_PHASE_4_MSG}")


__all__ = ["CacheMixin"]
