"""Sync postgres-backed store (psycopg 3). One instance per server.

:class:`Store` is a facade over :class:`~precis.store.core.StoreCore`
(the stateful pool/tx/hint-bus lifecycle — see that module) composed
from domain mixins, each owning one slice of the persistence surface:

* :class:`precis.store._refs_ops.RefsMixin`               — ref CRUD + title search
* :class:`precis.store._tags_ops.TagsMixin`               — three tag tables
* :class:`precis.store._links_ops.LinksMixin`             — link graph
* :class:`precis.store._cache_ops.CacheMixin`             — paid-tool cache state
* :class:`precis.store._identifiers_ops.IdentifiersMixin` — ``ref_identifiers`` alias lookup

``drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the first
domain carved *out* of the mixin stack into a composed sub-store —
reached **only** as ``store.drafts.*``; the flat delegations are gone
(see ``docs/backlog/codereview-store-decomposition.md``). ``blocks``
(:class:`precis.store._blocks_ops.BlockStore`) is the second — its
flat names survive as transitional delegations below until call
sites migrate to ``store.blocks.*``.

The public API is unchanged: callers that previously imported
``Store`` and called ``store.get_ref(...)`` / ``store.add_tag(...)``
still do exactly that. Splitting into mixins (and, for drafts, a
composed sub-store) is purely an implementation concern — no import
path breaks, no method signatures change. See ``_mappers.py`` for the
shared row-to-dataclass helpers and position sentinels used across
mixins.

Lifecycle + a handful of small cross-cutting ops (``system``
settings, ``corpus`` lookup/create, the slug-for-kind rule enforcer)
live in this module because they're either single-method domains
or pre-conditions used by multiple mixins.

All methods remain sync. Each method acquires a connection from the
pool for its work; callers needing multi-statement atomicity use
:meth:`Store.tx`.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.hints import Hint, HintBus
from precis.store._argument_ops import ArgumentGraphMixin
from precis.store._blocks_ops import BlockStore
from precis.store._cache_ops import CacheMixin
from precis.store._cad_ops import CadMixin
from precis.store._claude_quota_ops import ClaudeQuotaMixin
from precis.store._component_ops import ComponentMixin
from precis.store._draft_ops import DraftStore
from precis.store._email_ops import EmailAccountMixin
from precis.store._events_ops import EventsMixin
from precis.store._heartbeat_ops import HeartbeatMixin
from precis.store._identifiers_ops import IdentifiersMixin
from precis.store._integration_ops import IntegrationLedgerMixin
from precis.store._kinds_ops import KindsMixin
from precis.store._links_ops import LinksMixin
from precis.store._mappers import (
    _AGENT_WRITABLE_PREFIXES,
    _MARKUP_ONLY_BLOCK,
    _MIN_BLOCK_CHARS,
    _REF_LEVEL_POS,
    _SYSTEM_WRITABLE_PREFIXES,
    SEMANTIC_DISTANCE_FLOOR,
    _block_noise_clauses,
    _pos_to_db,
    _row_to_block,
    _row_to_cache_entry,
    _row_to_link,
    _row_to_ref,
)
from precis.store._material_ops import MaterialMixin
from precis.store._pcb_ops import PcbMixin
from precis.store._pdf_ops import PdfMixin
from precis.store._refs_ops import RefsMixin
from precis.store._resource_slots_ops import ResourceSlotsMixin
from precis.store._scheduler_ops import SchedulerLeasesMixin
from precis.store._structure_ops import StructureMixin
from precis.store._tags_ops import TagsMixin
from precis.store.core import StoreCore
from precis.store.pool import create_pool
from precis.store.types import Block, BlockInsert, Density, Ref

if TYPE_CHECKING:
    pass  # type-only imports for downstream mixins live in their own files

log = logging.getLogger(__name__)


class Store(
    RefsMixin,
    ArgumentGraphMixin,
    CadMixin,
    StructureMixin,
    PcbMixin,
    MaterialMixin,
    ComponentMixin,
    TagsMixin,
    LinksMixin,
    CacheMixin,
    IdentifiersMixin,
    IntegrationLedgerMixin,
    EventsMixin,
    HeartbeatMixin,
    ResourceSlotsMixin,
    SchedulerLeasesMixin,
    ClaudeQuotaMixin,
    EmailAccountMixin,
    KindsMixin,
    PdfMixin,
):
    """High-level handle. Owns the psycopg connection pool (via
    :class:`~precis.store.core.StoreCore`, see ``self.core``).

    Composed from domain mixins — see module docstring. Mixin order
    is alphabetical by domain; Python's MRO resolves cleanly because
    none of them collide on method names. ``drafts`` and ``blocks``
    are composed rather than mixed in — see :attr:`drafts` /
    :attr:`blocks`.
    """

    def __init__(self, pool: ConnectionPool, *, dsn: str | None = None) -> None:
        self.core = StoreCore(pool, dsn=dsn)

    @property
    def pool(self) -> ConnectionPool:
        return self.core.pool

    @pool.setter
    def pool(self, value: ConnectionPool) -> None:
        self.core.pool = value

    @property
    def dsn(self) -> str | None:
        return self.core.dsn

    @dsn.setter
    def dsn(self, value: str | None) -> None:
        self.core.dsn = value

    @property
    def hint_bus(self) -> HintBus | None:
        return self.core.hint_bus

    @hint_bus.setter
    def hint_bus(self, value: HintBus | None) -> None:
        self.core.hint_bus = value

    @cached_property
    def drafts(self) -> DraftStore:
        """The drafts domain, composed rather than mixed in — see the
        module docstring and
        ``docs/backlog/codereview-store-decomposition.md``. Cached so
        every access returns the same :class:`DraftStore` instance
        (it's stateless beyond the shared ``core``/``host``
        references, but a fresh instance per call would be wasteful)."""
        return DraftStore(self.core, host=self)

    @cached_property
    def blocks(self) -> BlockStore:
        """The blocks (chunks) domain, composed rather than mixed in —
        same carve pattern as :attr:`drafts`."""
        return BlockStore(self.core, host=self)

    def emit_hint(self, hint: Hint) -> None:
        """Emit a non-breaking agent hint if a bus is wired and we're inside a
        request scope; a no-op otherwise (worker paths, no bus)."""
        self.core.emit_hint(hint)

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def connect(
        cls,
        dsn: str,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> Self:
        """Create a Store from a DSN, using the shared pool factory.

        Defaults fall through to :mod:`precis.store.pool` so
        ``Store.connect`` and direct ``create_pool`` calls agree on
        one source of truth (previously they diverged at 8 vs 10).
        """
        from precis.store.pool import (
            DEFAULT_POOL_MAX_SIZE,
            DEFAULT_POOL_MIN_SIZE,
        )

        pool = create_pool(
            dsn,
            min_size=min_size if min_size is not None else DEFAULT_POOL_MIN_SIZE,
            max_size=max_size if max_size is not None else DEFAULT_POOL_MAX_SIZE,
        )
        return cls(pool, dsn=dsn)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.core.close()

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """Acquire a connection inside an explicit transaction.

        Auto-commits on clean exit; rolls back on exception. Used
        by handler ``put`` paths that bundle multiple writes so a
        downstream constraint violation rolls back the whole unit
        rather than leaving half-written state.
        """
        with self.core.tx() as conn:
            yield conn

    # -- app_state table -----------------------------------------------------
    #
    # Small key/value surface for cross-boot bookkeeping rows that don't
    # belong on a ref. Today's only caller is :mod:`precis.jobs.oracle_sync`
    # caching the bundled oracle YAML version so we don't re-embed the
    # whole oracle corpus on every boot. See ``app_state`` in
    # ``0001_initial.sql`` for the table definition and scoping rationale.

    def get_setting(self, key: str) -> str | None:
        """Return the value for ``key`` from ``app_state``, or ``None``.

        ``None`` means "no row" — not "row exists with empty value"; the
        ``value`` column is NOT NULL so the distinction is meaningful for
        callers that gate on first-boot vs. subsequent-boot.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = %s",
                (key,),
            ).fetchone()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        """Upsert ``(key, value)`` into ``app_state``.

        ``updated_at`` defaults to ``now()`` on insert and is bumped on
        every update so operators can see when a setting last changed
        without a separate audit table.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO app_state (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET "
                    "value = EXCLUDED.value, updated_at = now()",
                    (key, value),
                )

    def embedding_dim(self) -> int:
        """Return the configured embedding dimension as an ``int``.

        Source of truth: ``embedders.dim`` for the row with
        ``is_default = TRUE``. The migration seeds exactly one such
        row (``bge-m3, 1024``); when a second default-flagged row
        is added we will need a unique partial index, but until
        then ``LIMIT 1`` plus a stable ``ORDER BY name`` makes the
        query deterministic without a schema change.

        Raises :class:`RuntimeError` when no default embedder is
        registered — that indicates a botched migration, not a
        runtime condition the caller can recover from.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT dim FROM embedders WHERE is_default = TRUE "
                "ORDER BY name LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("no default embedder registered - did migrations run?")
        return int(row[0])

    # -- helpers -------------------------------------------------------------

    def _validate_slug_for_kind(
        self,
        kind: str,
        slug: str | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Enforce the per-kind slug rule (numeric kinds: slug=None, slug kinds: slug!=None).

        Called from :meth:`RefsMixin.insert_ref` before the INSERT
        so the agent gets a ``BadInput`` with a recovery hint instead
        of a FK/CHECK violation out of psycopg.
        """
        sql = "SELECT is_numeric FROM kinds WHERE slug = %s"
        if conn is not None:
            row = conn.execute(sql, (kind,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (kind,)).fetchone()

        if row is None:
            raise BadInput(
                f"unknown kind: {kind!r}",
                next="check kinds: SELECT slug FROM kinds",
            )
        is_numeric = row[0]
        if is_numeric and slug is not None:
            raise BadInput(
                f"kind={kind!r} is numeric - slug must be None",
                next=f"insert_ref(kind={kind!r}, slug=None, ...)",
            )
        if not is_numeric and slug is None:
            raise BadInput(
                f"kind={kind!r} is slug-addressed - slug is required",
                next=f"insert_ref(kind={kind!r}, slug='...', ...)",
            )

    # ---------------------------------------------------------------------------
    # Backwards-compatible re-exports.
    #
    # Before the mixin split, ``_row_to_ref`` / ``_pos_to_db`` / etc.
    # lived at module level in ``precis.store.store``. Tests and
    # sibling modules imported them via ``from precis.store.store import
    # _row_to_ref``. Rather than rewriting those imports, we re-export
    # the same names here. New code should import from
    # :mod:`precis.store._mappers` directly; these aliases stay to avoid
    # a churny diff on the test suite.
    # ---------------------------------------------------------------------------

    # -- blocks facade (transitional delegations — deleted per-domain as
    #    call sites migrate to ``store.blocks.*``; see
    #    ``docs/backlog/codereview-store-decomposition.md``) ---------------

    def count_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        kinds: list[str] | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
        distinct_refs: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return self.blocks.count_blocks_lexical(
            q=q,
            kind=kind,
            kinds=kinds,
            scope_ref_id=scope_ref_id,
            tags=tags,
            exclude_ref_ids=exclude_ref_ids,
            card_kinds=card_kinds,
            distinct_refs=distinct_refs,
            since=since,
            until=until,
        )

    def count_paper_yearless_matches(
        self,
        *,
        q: str,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> int:
        return self.blocks.count_paper_yearless_matches(
            q=q, scope_ref_id=scope_ref_id, tags=tags, exclude_ref_ids=exclude_ref_ids
        )

    def search_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        kinds: list[str] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks_lexical(
            q=q,
            kind=kind,
            kinds=kinds,
            created_from=created_from,
            created_to=created_to,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    def search_blocks_keywords(
        self,
        *,
        terms: list[str],
        kind: str | None = None,
        kinds: list[str] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks_keywords(
            terms=terms,
            kind=kind,
            kinds=kinds,
            created_from=created_from,
            created_to=created_to,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    def search_blocks_semantic(
        self,
        *,
        query_vec: list[float],
        kind: str | None = None,
        kinds: list[str] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks_semantic(
            query_vec=query_vec,
            kind=kind,
            kinds=kinds,
            created_from=created_from,
            created_to=created_to,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            max_distance=max_distance,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    def search_blocks_fused(
        self,
        *,
        q: str,
        query_vec: list[float] | None = None,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=kind,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            k=k,
            max_distance=max_distance,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    def search_blocks_multi(
        self,
        *,
        q_texts: list[str],
        query_vecs: list[list[float]],
        mode: str | None = None,
        kind: str | None = None,
        kinds: list[str] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
        per_paper: int | None = None,
        pool_per_leg: int = 80,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks_multi(
            q_texts=q_texts,
            query_vecs=query_vecs,
            mode=mode,
            kind=kind,
            kinds=kinds,
            created_from=created_from,
            created_to=created_to,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            k=k,
            max_distance=max_distance,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
            per_paper=per_paper,
            pool_per_leg=pool_per_leg,
        )

    def search_chunks_across_kinds(
        self,
        *,
        kinds: list[str],
        q: str,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        sort: str = "relevance",
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_chunks_across_kinds(
            kinds=kinds,
            q=q,
            query_vec=query_vec,
            mode=mode,
            tags=tags,
            since=since,
            until=until,
            sort=sort,
            limit=limit,
            offset=offset,
            k=k,
            max_distance=max_distance,
        )

    def search_blocks(
        self,
        *,
        q: str,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind=kind,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            k=k,
            max_distance=max_distance,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    def resolve_relative(self, handle: str) -> tuple[str, str] | None:
        return self.blocks.resolve_relative(handle=handle)

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        return self.blocks.insert_blocks(
            ref_id=ref_id, blocks=blocks, replace=replace, conn=conn
        )

    def replace_body_chunk(
        self,
        ref_id: int,
        new_text: str,
        *,
        chunk_kind: str,
        source: str = "agent",
        conn: Connection | None = None,
    ) -> str | None:
        return self.blocks.replace_body_chunk(
            ref_id=ref_id,
            new_text=new_text,
            chunk_kind=chunk_kind,
            source=source,
            conn=conn,
        )

    def set_ref_title(
        self,
        ref_id: int,
        new_title: str,
        *,
        source: str = "agent",
        conn: Connection | None = None,
    ) -> str | None:
        return self.blocks.set_ref_title(
            ref_id=ref_id, new_title=new_title, source=source, conn=conn
        )

    def upsert_card_combined(
        self, ref_id: int, text: str, *, conn: Connection | None = None
    ) -> int:
        return self.blocks.upsert_card_combined(ref_id=ref_id, text=text, conn=conn)

    def bump_salience(self, chunk_ids: list[int]) -> int:
        return self.blocks.bump_salience(chunk_ids=chunk_ids)

    def bump_salience_for_ref(self, ref_id: int) -> int:
        return self.blocks.bump_salience_for_ref(ref_id=ref_id)

    def touch_attended(
        self, actor: str, chunk_ids: list[int], *, conn: Connection | None = None
    ) -> int:
        return self.blocks.touch_attended(actor=actor, chunk_ids=chunk_ids, conn=conn)

    def touch_last_dreamt(
        self, chunk_ids: list[int], *, conn: Connection | None = None
    ) -> int:
        return self.blocks.touch_last_dreamt(chunk_ids=chunk_ids, conn=conn)

    def card_chunk_ids(self, ref_ids: list[int]) -> list[int]:
        return self.blocks.card_chunk_ids(ref_ids=ref_ids)

    def chunk_word_counts(
        self, ref_ids: list[int], *, chunk_kind: str
    ) -> dict[int, int]:
        return self.blocks.chunk_word_counts(ref_ids=ref_ids, chunk_kind=chunk_kind)

    def select_salient(
        self,
        actor: str,
        *,
        kinds: tuple[str, ...] = ("paper", "memory"),
        limit: int = 1,
        boost_kind: str | None = None,
        boost_seconds: float = 0.0,
        embedded_only: bool = False,
    ) -> list[int]:
        return self.blocks.select_salient(
            actor=actor,
            kinds=kinds,
            limit=limit,
            boost_kind=boost_kind,
            boost_seconds=boost_seconds,
            embedded_only=embedded_only,
        )

    def select_dream_seed(
        self, *, kinds: tuple[str, ...] = ("paper", "memory")
    ) -> int | None:
        return self.blocks.select_dream_seed(kinds=kinds)

    def dreamable_region(
        self, *, kinds: tuple[str, ...] = ("paper", "memory"), n: int = 12
    ) -> tuple[int | None, list[tuple[Block, Ref, float]]]:
        return self.blocks.dreamable_region(kinds=kinds, n=n)

    def chunk_text_by_id(self, chunk_id: int) -> str | None:
        return self.blocks.chunk_text_by_id(chunk_id=chunk_id)

    def get_chunk_vector(self, chunk_id: int) -> list[float] | None:
        return self.blocks.get_chunk_vector(chunk_id=chunk_id)

    def seed_chunk_for_ref(self, ref_id: int) -> int | None:
        return self.blocks.seed_chunk_for_ref(ref_id=ref_id)

    def angle_neighbours(
        self,
        seed_vec: list[float],
        *,
        angle: float = 1.0,
        n: int = 8,
        kinds: tuple[str, ...] = ("paper", "memory"),
        exclude_chunk_ids: list[int] | None = None,
        rng: random.Random | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        return self.blocks.angle_neighbours(
            seed_vec=seed_vec,
            angle=angle,
            n=n,
            kinds=kinds,
            exclude_chunk_ids=exclude_chunk_ids,
            rng=rng,
        )

    def get_block(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
        slug: str | None = None,
        with_embedding: bool = False,
    ) -> Block | None:
        return self.blocks.get_block(
            ref_id=ref_id, pos=pos, slug=slug, with_embedding=with_embedding
        )

    def list_blocks_for_ref(
        self,
        ref_id: int,
        *,
        pos_range: tuple[int, int] | None = None,
        with_embedding: bool = False,
    ) -> list[Block]:
        return self.blocks.list_blocks_for_ref(
            ref_id=ref_id, pos_range=pos_range, with_embedding=with_embedding
        )

    def chunk_pages(self, ref_id: int, ords: list[int]) -> dict[int, int]:
        return self.blocks.chunk_pages(ref_id=ref_id, ords=ords)

    def chunk_glosses_for_ref(
        self, ref_id: int, *, pos_range: tuple[int, int] | None = None
    ) -> list[dict[str, Any]]:
        return self.blocks.chunk_glosses_for_ref(ref_id=ref_id, pos_range=pos_range)

    def chunk_summaries_for(self, ref_id: int, ords: list[int]) -> dict[int, str]:
        return self.blocks.chunk_summaries_for(ref_id=ref_id, ords=ords)

    def chunk_summaries_bulk(
        self, pairs: list[tuple[int, int]]
    ) -> dict[tuple[int, int], str]:
        return self.blocks.chunk_summaries_bulk(pairs=pairs)

    def ref_ids_with_chunks(self, ref_ids: list[int]) -> set[int]:
        return self.blocks.ref_ids_with_chunks(ref_ids=ref_ids)

    def count_blocks(self, ref_id: int) -> int:
        return self.blocks.count_blocks(ref_id=ref_id)

    def count_chunks_for_kind(self, kind: str) -> int:
        return self.blocks.count_chunks_for_kind(kind=kind)

    def abstract_previews(
        self, ref_ids: list[int], *, max_chars: int = 900
    ) -> dict[int, str]:
        return self.blocks.abstract_previews(ref_ids=ref_ids, max_chars=max_chars)

    def random_embedded_block(self) -> tuple[Block, Ref] | None:
        return self.blocks.random_embedded_block()

    def update_block_density(self, block_id: int, density: Density) -> None:
        return self.blocks.update_block_density(block_id=block_id, density=density)

    def update_block_embedding(self, block_id: int, embedding: list[float]) -> None:
        return self.blocks.update_block_embedding(
            block_id=block_id, embedding=embedding
        )

    def blocks_missing_embeddings(
        self, *, kind: str | None = None, limit: int = 100
    ) -> list[Block]:
        return self.blocks.blocks_missing_embeddings(kind=kind, limit=limit)


__all__ = [
    "SEMANTIC_DISTANCE_FLOOR",
    "_AGENT_WRITABLE_PREFIXES",
    "_MARKUP_ONLY_BLOCK",
    "_MIN_BLOCK_CHARS",
    "_REF_LEVEL_POS",
    "_SYSTEM_WRITABLE_PREFIXES",
    # Type re-export — a few older tests import Any from here.
    "Any",
    # Psycopg re-export used by some tests that patch Jsonb coercion.
    "Jsonb",
    "Store",
    "_block_noise_clauses",
    "_pos_to_db",
    "_row_to_block",
    "_row_to_cache_entry",
    "_row_to_link",
    "_row_to_ref",
]
