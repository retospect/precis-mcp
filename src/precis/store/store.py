"""Sync postgres-backed store (psycopg 3). One instance per server.

:class:`Store` is composed from six domain mixins, each owning one
slice of the persistence surface:

* :class:`precis.store._refs_ops.RefsMixin`               — ref CRUD + title search
* :class:`precis.store._blocks_ops.BlocksMixin`           — block CRUD + hybrid search
* :class:`precis.store._tags_ops.TagsMixin`               — three tag tables
* :class:`precis.store._links_ops.LinksMixin`             — link graph
* :class:`precis.store._cache_ops.CacheMixin`             — paid-tool cache state
* :class:`precis.store._identifiers_ops.IdentifiersMixin` — ``ref_identifiers`` alias lookup

The public API is unchanged: callers that previously imported
``Store`` and called ``store.get_ref(...)`` / ``store.add_tag(...)``
still do exactly that. Splitting into mixins is purely an
implementation concern — no import path breaks, no method
signatures change. See ``_mappers.py`` for the shared row-to-
dataclass helpers and position sentinels used across mixins.

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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.hints import Hint, HintBus
from precis.store._blocks_ops import BlocksMixin
from precis.store._cache_ops import CacheMixin
from precis.store._cad_ops import CadMixin
from precis.store._claude_quota_ops import ClaudeQuotaMixin
from precis.store._draft_ops import DraftMixin, _AbbrevMixin
from precis.store._events_ops import EventsMixin
from precis.store._heartbeat_ops import HeartbeatMixin
from precis.store._identifiers_ops import IdentifiersMixin
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
from precis.store._pcb_ops import PcbMixin
from precis.store._pdf_ops import PdfMixin
from precis.store._refs_ops import RefsMixin
from precis.store._resource_slots_ops import ResourceSlotsMixin
from precis.store._scheduler_ops import SchedulerLeasesMixin
from precis.store._structure_ops import StructureMixin
from precis.store._tags_ops import TagsMixin
from precis.store.pool import create_pool

if TYPE_CHECKING:
    pass  # type-only imports for downstream mixins live in their own files

log = logging.getLogger(__name__)


class Store(
    RefsMixin,
    BlocksMixin,
    CadMixin,
    StructureMixin,
    PcbMixin,
    DraftMixin,
    _AbbrevMixin,
    TagsMixin,
    LinksMixin,
    CacheMixin,
    IdentifiersMixin,
    EventsMixin,
    HeartbeatMixin,
    ResourceSlotsMixin,
    SchedulerLeasesMixin,
    ClaudeQuotaMixin,
    KindsMixin,
    PdfMixin,
):
    """High-level handle. Owns the psycopg connection pool.

    Composed from domain mixins — see module docstring. Mixin order
    is alphabetical by domain; Python's MRO resolves cleanly because
    none of them collide on method names.
    """

    def __init__(self, pool: ConnectionPool, *, dsn: str | None = None) -> None:
        self.pool = pool
        # Original DSN string — used by callers that need to open a
        # dedicated (non-pooled) connection, e.g. for session-scoped
        # advisory locks in ``precis.ingest.claim`` where pool-based
        # connections aren't usable. ``None`` when the Store was
        # constructed without going through :meth:`connect` (tests
        # using a pre-built pool); claim acquisition falls back to a
        # no-op in that case.
        self.dsn = dsn
        # Optional back-reference to the per-request hint bus, wired by
        # ``Hub.__post_init__``. Lets low-level store ops (e.g. the
        # merged-handle redirect in ``resolve_handle``) emit a non-breaking
        # agent hint from deep in the call tree without every caller
        # threading a ``hub``. ``None`` on a store built outside a Hub (most
        # worker paths) — :meth:`emit_hint` is then a no-op, as it is outside
        # any request scope.
        self.hint_bus: HintBus | None = None

    def emit_hint(self, hint: Hint) -> None:
        """Emit a non-breaking agent hint if a bus is wired and we're inside a
        request scope; a no-op otherwise (worker paths, no bus)."""
        if self.hint_bus is not None:
            self.hint_bus.emit(hint)

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
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """Acquire a connection inside an explicit transaction.

        Auto-commits on clean exit; rolls back on exception. Used
        by handler ``put`` paths that bundle multiple writes so a
        downstream constraint violation rolls back the whole unit
        rather than leaving half-written state.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
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
