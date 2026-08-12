"""Sync postgres-backed store (psycopg 3). One instance per server.

:class:`Store` is a facade over :class:`~precis.store.core.StoreCore`
(the stateful pool/tx/hint-bus lifecycle — see that module) composed
from domain mixins, each owning one slice of the persistence surface:

* :class:`precis.store._refs_ops.RefsMixin`               — ref CRUD + title search
* :class:`precis.store._blocks_ops.BlocksMixin`           — block CRUD + hybrid search
* :class:`precis.store._tags_ops.TagsMixin`               — three tag tables
* :class:`precis.store._links_ops.LinksMixin`             — link graph
* :class:`precis.store._cache_ops.CacheMixin`             — paid-tool cache state
* :class:`precis.store._identifiers_ops.IdentifiersMixin` — ``ref_identifiers`` alias lookup

``drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the first
domain carved *out* of the mixin stack into a composed sub-store —
reached as ``store.drafts.*`` — rather than mixed directly into
``Store``. ``Store`` still exposes every ``DraftStore`` method under
its flat historical name too, via a transitional delegation block
below; those delegations are deleted one by one as call sites migrate
to ``store.drafts.*`` (see
``docs/backlog/codereview-store-decomposition.md``).

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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.hints import Hint, HintBus
from precis.store._argument_ops import ArgumentGraphMixin
from precis.store._blocks_ops import BlocksMixin
from precis.store._cache_ops import CacheMixin
from precis.store._cad_ops import CadMixin
from precis.store._claude_quota_ops import ClaudeQuotaMixin
from precis.store._component_ops import ComponentMixin
from precis.store._draft_ops import DraftChunk, DraftStore, DraftWorkItem, TocEntry
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

if TYPE_CHECKING:
    pass  # type-only imports for downstream mixins live in their own files

log = logging.getLogger(__name__)


class Store(
    RefsMixin,
    BlocksMixin,
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
    none of them collide on method names. ``drafts`` is composed
    rather than mixed in — see :attr:`drafts`.
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

    # -- drafts facade (transitional delegations — deleted per-domain as
    #    call sites migrate to ``store.drafts.*``; see
    #    ``docs/backlog/codereview-store-decomposition.md``) ----------------

    def ensure_glossary_heading(self, ref_id: int) -> str:
        return self.drafts.ensure_glossary_heading(ref_id=ref_id)

    def ensure_registry_heading(self, ref_id: int, role: str) -> str:
        return self.drafts.ensure_registry_heading(ref_id=ref_id, role=role)

    def parts_callout_map(self, ref_id: int, role: str = "parts") -> dict[str, int]:
        return self.drafts.parts_callout_map(ref_id=ref_id, role=role)

    def undefined_abbrevs(self, ref_id: int, text: str) -> list[str]:
        return self.drafts.undefined_abbrevs(ref_id=ref_id, text=text)

    def defined_abbrevs(self, ref_id: int) -> dict[str, str]:
        return self.drafts.defined_abbrevs(ref_id=ref_id)

    def defined_terms(self, ref_id: int) -> dict[str, Any]:
        return self.drafts.defined_terms(ref_id=ref_id)

    def registry_callouts(self, ref_id: int, role: str) -> list[int]:
        return self.drafts.registry_callouts(ref_id=ref_id, role=role)

    def add_abbrev_ignore(self, ref_id: int, tokens: list[str]) -> None:
        return self.drafts.add_abbrev_ignore(ref_id=ref_id, tokens=tokens)

    def draft_subtree_chunk_ids(self, handle: str) -> list[int]:
        return self.drafts.draft_subtree_chunk_ids(handle=handle)

    def draft_term_shorts(self, ref_id: int) -> set[str]:
        return self.drafts.draft_term_shorts(ref_id=ref_id)

    def draft_terms(self, ref_id: int) -> dict[str, tuple[str, str]]:
        return self.drafts.draft_terms(ref_id=ref_id)

    def draft_handles_for(self, chunk_ids: list[int]) -> dict[int, str]:
        return self.drafts.draft_handles_for(chunk_ids=chunk_ids)

    def draft_chunk_meta(self, handle: str) -> dict[str, Any]:
        return self.drafts.draft_chunk_meta(handle=handle)

    def soft_delete_draft(self, ref_id: int) -> int:
        return self.drafts.soft_delete_draft(ref_id=ref_id)

    def universal_chunk(self, handle: str) -> dict[str, Any] | None:
        return self.drafts.universal_chunk(handle=handle)

    def universal_chunks(self, handles: Iterable[str]) -> dict[str, dict[str, Any]]:
        return self.drafts.universal_chunks(handles=handles)

    def chunk_text_at(self, ref_id: int, ord: int) -> str | None:
        return self.drafts.chunk_text_at(ref_id=ref_id, ord=ord)

    def get_draft_chunk(self, handle: str, *, kind: str = "draft") -> DraftChunk | None:
        return self.drafts.get_draft_chunk(handle=handle, kind=kind)

    def draft_relative_chunk_ids(
        self, addr: str, *, kind: str = "draft"
    ) -> list[int] | None:
        return self.drafts.draft_relative_chunk_ids(addr=addr, kind=kind)

    def reading_order(self, ref_id: int, *, kind: str = "draft") -> list[DraftChunk]:
        return self.drafts.reading_order(ref_id=ref_id, kind=kind)

    def chunk_ord_map(self, ref_id: int) -> dict[int, int]:
        return self.drafts.chunk_ord_map(ref_id=ref_id)

    def chunk_connections(
        self, ref_id: int, handles: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return self.drafts.chunk_connections(ref_id=ref_id, handles=handles)

    def ref_connections(self, ref_id: int) -> list[dict[str, Any]]:
        return self.drafts.ref_connections(ref_id=ref_id)

    def anchored_todos(self, handles: list[str]) -> dict[str, list[dict[str, Any]]]:
        return self.drafts.anchored_todos(handles=handles)

    def bind_element(
        self,
        *,
        node_chunk_id: int,
        element: str,
        target: str,
        relation: str = "depicts",
        set_by: str = "agent",
        conn: psycopg.Connection | None = None,
    ) -> None:
        return self.drafts.bind_element(
            node_chunk_id=node_chunk_id,
            element=element,
            target=target,
            relation=relation,
            set_by=set_by,
            conn=conn,
        )

    def unbind_element(
        self,
        *,
        node_chunk_id: int,
        element: str,
        target: str | None = None,
        relation: str = "depicts",
        conn: psycopg.Connection | None = None,
    ) -> int:
        return self.drafts.unbind_element(
            node_chunk_id=node_chunk_id,
            element=element,
            target=target,
            relation=relation,
            conn=conn,
        )

    def element_bindings(self, node_chunk_id: int) -> list[dict[str, Any]]:
        return self.drafts.element_bindings(node_chunk_id=node_chunk_id)

    def set_element_bindings(
        self,
        *,
        node_chunk_id: int,
        desired: list[dict[str, Any]],
        set_by: str = "agent",
    ) -> dict[str, int]:
        return self.drafts.set_element_bindings(
            node_chunk_id=node_chunk_id, desired=desired, set_by=set_by
        )

    def live_paper_cites(self, handles: set[str], slugs: set[str]) -> set[str]:
        return self.drafts.live_paper_cites(handles=handles, slugs=slugs)

    def chunk_edit_stats(
        self, ref_id: int, handles: list[str]
    ) -> dict[str, dict[str, Any]]:
        return self.drafts.chunk_edit_stats(ref_id=ref_id, handles=handles)

    def block_views(
        self, ref_id: int, handles: list[str] | None = None
    ) -> dict[str, dict[str, str]]:
        return self.drafts.block_views(ref_id=ref_id, handles=handles)

    def draft_toc(
        self, ref_id: int, *, root_handle: str | None = None
    ) -> list[TocEntry]:
        return self.drafts.draft_toc(ref_id=ref_id, root_handle=root_handle)

    def draft_attached_work(
        self, draft_ref_id: int, *, limit: int = 20
    ) -> list[DraftWorkItem]:
        return self.drafts.draft_attached_work(draft_ref_id=draft_ref_id, limit=limit)

    def resolve_ask_question(self, ref_id: int, tag_value: str) -> str:
        return self.drafts.resolve_ask_question(ref_id=ref_id, tag_value=tag_value)

    def job_fail_reason(self, job_ref_id: int, *, limit: int = 240) -> str | None:
        return self.drafts.job_fail_reason(job_ref_id=job_ref_id, limit=limit)

    def create_draft(
        self,
        *,
        name: str,
        title: str,
        project_ref_id: int,
        meta: dict[str, Any] | None = None,
        kind: str = "draft",
        relation: str = "draft-of",
    ) -> tuple[Any, DraftChunk]:
        return self.drafts.create_draft(
            name=name,
            title=title,
            project_ref_id=project_ref_id,
            meta=meta,
            kind=kind,
            relation=relation,
        )

    def draft_title_chunk_id(
        self, conn: psycopg.Connection, ref_id: int
    ) -> tuple[int, str] | None:
        return self.drafts.draft_title_chunk_id(conn=conn, ref_id=ref_id)

    def set_draft_title(
        self, ref_id: int, title: str, *, source: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        return self.drafts.set_draft_title(ref_id=ref_id, title=title, source=source)

    def fork_draft(
        self,
        src_ref_id: int,
        project_id: int,
        *,
        new_slug: str,
        title: str | None = None,
    ) -> Any:
        return self.drafts.fork_draft(
            src_ref_id=src_ref_id, project_id=project_id, new_slug=new_slug, title=title
        )

    def add_chunks(
        self,
        *,
        ref_id: int,
        chunk_kind: str,
        text: str,
        at: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        split: bool = True,
        kind: str = "draft",
    ) -> list[DraftChunk]:
        return self.drafts.add_chunks(
            ref_id=ref_id,
            chunk_kind=chunk_kind,
            text=text,
            at=at,
            meta=meta,
            split=split,
            kind=kind,
        )

    def add_figure(
        self,
        *,
        ref_id: int,
        caption: str,
        origin: str,
        image: bytes,
        mime: str,
        at: dict[str, Any] | None = None,
        figure_meta: dict[str, Any] | None = None,
    ) -> DraftChunk:
        return self.drafts.add_figure(
            ref_id=ref_id,
            caption=caption,
            origin=origin,
            image=image,
            mime=mime,
            at=at,
            figure_meta=figure_meta,
        )

    def get_chunk_blob(self, handle: str) -> tuple[bytes, str] | None:
        return self.drafts.get_chunk_blob(handle=handle)

    def has_chunk_blob(self, chunk_id: int) -> bool:
        return self.drafts.has_chunk_blob(chunk_id=chunk_id)

    def upsert_chunk_blob(
        self,
        chunk_id: int,
        image: bytes,
        mime: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        return self.drafts.upsert_chunk_blob(
            chunk_id=chunk_id, image=image, mime=mime, conn=conn
        )

    def figure_render_bundle(self, figure_chunk_id: int) -> dict[str, Any] | None:
        return self.drafts.figure_render_bundle(figure_chunk_id=figure_chunk_id)

    def stamp_render_key(self, figure_chunk_id: int, cached_key: str) -> None:
        return self.drafts.stamp_render_key(
            figure_chunk_id=figure_chunk_id, cached_key=cached_key
        )

    def set_render_recipe(
        self,
        chunk_id: int,
        recipe: dict[str, Any],
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        return self.drafts.set_render_recipe(
            chunk_id=chunk_id, recipe=recipe, conn=conn
        )

    def link_figure_plots(self, figure_chunk_id: int, data_chunk_ids: list[int]) -> int:
        return self.drafts.link_figure_plots(
            figure_chunk_id=figure_chunk_id, data_chunk_ids=data_chunk_ids
        )

    def link_figure_canvas(self, figure_chunk_id: int, canvas_ref_id: int) -> None:
        return self.drafts.link_figure_canvas(
            figure_chunk_id=figure_chunk_id, canvas_ref_id=canvas_ref_id
        )

    def figure_canvas_ref(self, figure_chunk_id: int) -> int | None:
        return self.drafts.figure_canvas_ref(figure_chunk_id=figure_chunk_id)

    def figure_owning_draft(self, canvas_ref_id: int) -> tuple[int, int] | None:
        return self.drafts.figure_owning_draft(canvas_ref_id=canvas_ref_id)

    def set_figure_provenance(
        self,
        handle: str,
        *,
        permission: dict[str, Any] | None = None,
        origin: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        return self.drafts.set_figure_provenance(
            handle=handle, permission=permission, origin=origin, source=source
        )

    def set_chunk_style(self, handle: str, style: str | None) -> DraftChunk | None:
        return self.drafts.set_chunk_style(handle=handle, style=style)

    def patch_chunk_meta(self, handle: str, patch: dict[str, Any]) -> None:
        return self.drafts.patch_chunk_meta(handle=handle, patch=patch)

    def set_term_attrs(self, handle: str, attrs: dict[str, Any]) -> DraftChunk | None:
        return self.drafts.set_term_attrs(handle=handle, attrs=attrs)

    def set_list_kind(
        self, handle: str, kind: str, *, source: dict[str, Any] | None = None
    ) -> DraftChunk | None:
        return self.drafts.set_list_kind(handle=handle, kind=kind, source=source)

    def set_word_target(
        self, handle: str, target: tuple[int, int] | None
    ) -> DraftChunk | None:
        return self.drafts.set_word_target(handle=handle, target=target)

    def section_style_for(self, handle: str) -> str | None:
        return self.drafts.section_style_for(handle=handle)

    def scaffold_sections(
        self, ref_id: int, sections: list[tuple[str, str | None]]
    ) -> list[str]:
        return self.drafts.scaffold_sections(ref_id=ref_id, sections=sections)

    def edit_text(
        self,
        handle: str,
        text: str,
        *,
        base_sha: str | None = None,
        source: dict[str, Any] | None = None,
        meta_patch: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> DraftChunk | None:
        return self.drafts.edit_text(
            handle=handle,
            text=text,
            base_sha=base_sha,
            source=source,
            meta_patch=meta_patch,
            kind=kind,
        )

    def record_review(
        self, chunk_id: int, checker: str, *, verdict: str = "approved"
    ) -> str:
        return self.drafts.record_review(
            chunk_id=chunk_id, checker=checker, verdict=verdict
        )

    def retract_review(self, chunk_id: int, checker: str) -> bool:
        return self.drafts.retract_review(chunk_id=chunk_id, checker=checker)

    def approved_pairs_at_current_sha(self, ref_id: int) -> set[tuple[int, str]]:
        return self.drafts.approved_pairs_at_current_sha(ref_id=ref_id)

    def review_subtree_chunk_ids(self, ref_id: int, heading_chunk_id: int) -> list[int]:
        return self.drafts.review_subtree_chunk_ids(
            ref_id=ref_id, heading_chunk_id=heading_chunk_id
        )

    def toc_digest(self, ref_id: int) -> str:
        return self.drafts.toc_digest(ref_id=ref_id)

    def chunks_requiring_review(
        self, ref_id: int, checker: str
    ) -> list[dict[str, Any]]:
        return self.drafts.chunks_requiring_review(ref_id=ref_id, checker=checker)

    def authored_provenance(self, ref_id: int) -> dict[int, str]:
        return self.drafts.authored_provenance(ref_id=ref_id)

    def draft_authoring_enabled(self, ref_id: int) -> bool:
        return self.drafts.draft_authoring_enabled(ref_id=ref_id)

    def reviewable_chunks(self, ref_id: int) -> list[dict[str, Any]]:
        return self.drafts.reviewable_chunks(ref_id=ref_id)

    def review_status_for_chunk(self, chunk_id: int) -> list[dict[str, Any]]:
        return self.drafts.review_status_for_chunk(chunk_id=chunk_id)

    def review_root_chunk_id(self, ref_id: int) -> int | None:
        return self.drafts.review_root_chunk_id(ref_id=ref_id)

    def review_status_for_draft(self, ref_id: int) -> list[dict[str, Any]]:
        return self.drafts.review_status_for_draft(ref_id=ref_id)

    def review_rollup_for_draft(self, ref_id: int) -> dict[str, int]:
        return self.drafts.review_rollup_for_draft(ref_id=ref_id)

    def review_diff_since(self, chunk_id: int, since_sha: str) -> str:
        return self.drafts.review_diff_since(chunk_id=chunk_id, since_sha=since_sha)

    def move_chunk(
        self,
        handle: str,
        move: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> DraftChunk | None:
        return self.drafts.move_chunk(
            handle=handle, move=move, source=source, kind=kind
        )

    def retire_chunk(
        self,
        handle: str,
        *,
        mode: str | None = None,
        source: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> None:
        return self.drafts.retire_chunk(
            handle=handle, mode=mode, source=source, kind=kind
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
