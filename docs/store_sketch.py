"""Precis V2 — `precis.store` interface sketch.

Implementation notes:
    - `asyncpg` directly. No ORM. Raw SQL where it matters; helpers where it
      doesn't.
    - All public methods are `async`. Sync calls into the store are not
      supported.
    - Row types are frozen dataclasses with slots. Conversion from
      `asyncpg.Record` is via small `_row_to_X` helpers, not magic.
    - `pgvector.asyncpg.register_vector` runs once per connection in the
      pool's `init=` hook so `vector(1024)` columns round-trip as
      `numpy.ndarray` (or `list[float]` if numpy isn't installed).
    - JSONB round-trips via a type codec set on connection init.
    - Transaction boundaries are explicit (`async with store.tx() as conn`).
      Each MCP verb call wraps its work in one transaction.
    - Tag API is unified: callers see one `Tag` type; the three-table split
      lives behind `tags.add` / `tags.remove` / `tags.for_ref`.
    - Hybrid search composes lexical (tsvector) + semantic (hnsw) results
      via reciprocal rank fusion in a single CTE query — see `search.hybrid`.

This is a sketch — signatures + docstrings, not implementations. Used as a
design checkpoint before phase-1 walking-skeleton code lands.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Literal, Self

import asyncpg


# =============================================================================
# Row types
# =============================================================================

Density = Literal["sparse", "medium", "dense"]
CacheFreshness = Literal["pinned", "fresh", "stale", "expired"]
Namespace = Literal["closed", "flag", "open"]
Relation = Literal[
    "related-to", "blocks", "blocked-by", "contradicts", "contradicted-by"
]
ActorSlug = Literal["agent", "user", "system"]


@dataclass(frozen=True, slots=True)
class Ref:
    id: int
    corpus_id: int
    kind: str                       # FK to kinds.slug
    slug: str | None                # NULL for numeric kinds
    title: str
    provider: str | None            # FK to providers.slug
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @property
    def public_id(self) -> str:
        """Agent-facing identifier: slug for slug kinds, str(id) for numeric."""
        return self.slug if self.slug is not None else str(self.id)


@dataclass(frozen=True, slots=True)
class Block:
    id: int
    ref_id: int
    pos: int                        # 0-based, renumberable
    slug: str | None                # stable citation handle
    text: str
    token_count: int | None
    embedding: list[float] | None   # populated only when fetched explicitly
    density: Density | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Link:
    id: int
    src_ref_id: int
    src_pos: int | None
    dst_ref_id: int
    dst_pos: int | None
    relation: Relation
    set_by: ActorSlug
    meta: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Tag:
    """Unified tag. The three-table split (closed/flag/open) is hidden."""
    namespace: Namespace
    prefix: str | None              # closed only; None for flag/open
    value: str                      # closed value, flag name, or open value

    @classmethod
    def closed(cls, prefix: str, value: str) -> Tag:
        return cls(namespace="closed", prefix=prefix, value=value)

    @classmethod
    def flag(cls, name: str) -> Tag:
        return cls(namespace="flag", prefix=None, value=name)

    @classmethod
    def open(cls, value: str) -> Tag:
        return cls(namespace="open", prefix=None, value=value.lower())

    @classmethod
    def parse(cls, s: str, *, known_flags: set[str] | None = None) -> Tag:
        """Parse 'STATUS:done' / 'pinned' / 'nitrate-reduction' into a Tag.

        Disambiguation rule:
          - contains ':' AND prefix is all-uppercase  -> closed
          - matches a known flag name                  -> flag
          - else                                       -> open (lowercased)
        """
        if ":" in s:
            prefix, _, value = s.partition(":")
            if prefix and prefix.isupper():
                return cls.closed(prefix, value)
        if known_flags and s in known_flags:
            return cls.flag(s)
        return cls.open(s)

    def __str__(self) -> str:
        if self.namespace == "closed":
            return f"{self.prefix}:{self.value}"
        return self.value


@dataclass(frozen=True, slots=True)
class CacheEntry:
    ref_id: int
    provider: str
    request_hash: str
    model: str | None
    fetched_at: datetime
    fresh_until: datetime | None    # NULL = pinned
    cost_usd: float | None
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchHit:
    block_id: int
    ref_id: int
    score: float                    # RRF fused score
    kind: str
    slug: str | None
    ref_id_public: str              # slug or str(id)
    title: str
    text_excerpt: str               # first ~280 chars of block.text
    pos: int
    block_slug: str | None


# =============================================================================
# Store
# =============================================================================

class Store:
    """Async postgres-backed storage layer. Single instance per server."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
    ) -> Self:
        """Build the pool with vector + jsonb codecs registered per-conn."""
        ...

    async def close(self) -> None: ...

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection and run inside a transaction. Auto-commit on
        clean exit, rollback on exception."""
        ...

    # -- system --------------------------------------------------------------

    async def get_setting(self, key: str) -> str | None: ...
    async def set_setting(self, key: str, value: str) -> None: ...
    async def embedding_dim(self) -> int:
        """Returns int(system['embedding_dim']). Cached after first call."""
        ...

    # -- corpuses ------------------------------------------------------------

    async def get_corpus(self, slug: str) -> int | None: ...
    async def ensure_corpus(self, slug: str, *, title: str | None = None) -> int: ...

    # -- refs ----------------------------------------------------------------

    async def get_ref(
        self,
        *,
        kind: str,
        id: int | str,                       # int for numeric kinds, str for slug
        corpus_id: int | None = None,
        include_deleted: bool = False,
    ) -> Ref | None: ...

    async def insert_ref(
        self,
        *,
        corpus_id: int,
        kind: str,
        slug: str | None,
        title: str,
        provider: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Ref:
        """Slug kinds require slug; numeric kinds require slug=None.
        Enforced via Store-level assertion (DB CHECK can't subquery)."""
        ...

    async def update_ref(
        self,
        ref_id: int,
        *,
        title: str | None = None,
        meta_patch: dict[str, Any] | None = None,
    ) -> Ref: ...

    async def soft_delete_ref(self, ref_id: int) -> None:
        """Sets deleted_at. Cascading effects (links/tags/cache) handled by
        FK ON DELETE CASCADE, but soft-delete keeps the row alive for
        undelete; cascade only on hard delete."""
        ...

    async def hard_delete_ref(self, ref_id: int) -> None:
        """DELETE FROM refs WHERE id = ... — cascades to blocks/links/tags/
        cache_state via FK."""
        ...

    async def list_refs(
        self,
        *,
        corpus_id: int | None = None,
        kinds: list[str] | None = None,
        provider: str | None = None,
        updated_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ref]: ...

    # -- blocks --------------------------------------------------------------

    async def get_block(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
        slug: str | None = None,
        with_embedding: bool = False,
    ) -> Block | None:
        """Fetch by (ref_id, pos) OR (ref_id, slug). Embedding excluded by
        default to avoid shipping vectors when callers don't need them."""
        ...

    async def list_blocks(
        self,
        ref_id: int,
        *,
        pos_range: tuple[int, int] | None = None,
        with_embedding: bool = False,
    ) -> list[Block]: ...

    async def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
    ) -> list[Block]:
        """Bulk-insert blocks. If replace=True, delete existing blocks for
        ref_id first (re-ingest path). Renumbering pos is the caller's job."""
        ...

    async def update_block_density(
        self,
        block_id: int,
        density: Density,
    ) -> None: ...

    async def update_block_embedding(
        self,
        block_id: int,
        embedding: list[float],
    ) -> None: ...

    async def blocks_missing_embeddings(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Block]:
        """For background re-embed jobs."""
        ...

    # -- links ---------------------------------------------------------------

    async def add_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: Relation,
        src_pos: int | None = None,
        dst_pos: int | None = None,
        set_by: ActorSlug = "agent",
        meta: dict[str, Any] | None = None,
    ) -> Link:
        """Symmetric relations canonicalize src/dst by lowest id internally."""
        ...

    async def remove_link(self, link_id: int) -> None: ...

    async def links_touching(
        self,
        ref_id: int,
        *,
        relation: Relation | None = None,
    ) -> list[Link]:
        """All links where ref_id is src OR dst."""
        ...

    # -- tags ----------------------------------------------------------------

    async def add_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        set_by: ActorSlug = "agent",
    ) -> None:
        """Routes to ref_closed_tags / ref_flags / ref_open_tags by namespace."""
        ...

    async def remove_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
    ) -> None: ...

    async def tags_for(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
    ) -> list[Tag]:
        """Reads from the `ref_tags` view; merges all three tables."""
        ...

    async def has_flag(self, ref_id: int, name: str) -> bool: ...

    async def refs_with_tag(
        self,
        tag: Tag,
        *,
        kind: str | None = None,
        corpus_id: int | None = None,
        limit: int = 50,
    ) -> list[Ref]: ...

    # -- cache ---------------------------------------------------------------

    async def get_cached_ref(
        self,
        provider: str,
        request_hash: str,
    ) -> Ref | None:
        """Idempotent lookup by (provider, request_hash). Returns the live ref
        if cached, None otherwise."""
        ...

    async def upsert_cache(
        self,
        *,
        ref_id: int,
        provider: str,
        request_hash: str,
        model: str | None,
        fetched_at: datetime,
        ttl: timedelta | None,
        cost_usd: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> CacheEntry:
        """Sets fresh_until = fetched_at + ttl (or NULL if ttl is None,
        meaning pinned)."""
        ...

    async def cache_freshness(self, ref_id: int) -> CacheFreshness | None:
        """Reads from `cache_freshness` view."""
        ...

    async def pin(self, ref_id: int) -> None:
        """Sets cache_state.fresh_until = NULL."""
        ...

    async def unpin(self, ref_id: int, ttl: timedelta) -> None: ...

    async def stale_cache_count(self, *, provider: str | None = None) -> int:
        """Used by notification channel to surface 'N stale caches'."""
        ...

    # -- search --------------------------------------------------------------

    async def search(
        self,
        *,
        q: str,
        qvec: list[float] | None = None,
        kinds: list[str] | None = None,
        corpus_id: int | None = None,
        scope_ref_id: int | None = None,
        cache: bool | None = None,        # True/False/None = include cache refs
        top_k: int = 10,
    ) -> list[SearchHit]:
        """Hybrid search via reciprocal rank fusion.

        Behaviour:
          - If qvec is None, lexical only (tsvector).
          - If qvec is provided, lexical + semantic fused (RRF, k=60).
          - kinds=None searches across all kinds (cross-corpus default).
          - scope_ref_id restricts to one ref's blocks.
          - cache=False excludes web/youtube/math kinds.
          - cache=True includes ONLY cache kinds.
          - cache=None (default) includes everything.
        """
        ...


# =============================================================================
# Insert payload types (mutable; not stored)
# =============================================================================

@dataclass
class BlockInsert:
    pos: int
    text: str
    slug: str | None = None
    token_count: int | None = None
    embedding: list[float] | None = None
    density: Density | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Migration runner (separate concern)
# =============================================================================

class Migrator:
    """Forward-only migration runner. Reads numbered .sql files from
    `migrations_dir`, applies any whose version is not in `_migrations`, and
    records the version + checksum on success."""

    def __init__(self, store: Store, migrations_dir: str) -> None: ...

    async def applied_versions(self) -> list[str]: ...

    async def pending(self) -> list[str]: ...

    async def apply_all(self) -> list[str]:
        """Apply all pending migrations in version order. Each migration runs
        in its own transaction. Returns list of newly-applied versions.

        Refuses to run if a previously-applied migration's checksum no longer
        matches its file (someone edited a sealed migration)."""
        ...
