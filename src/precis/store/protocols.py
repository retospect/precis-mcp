"""Narrow, role-named structural views of :class:`precis.store.Store`.

Policy (codereview-store-typing-seam): a function that needs only a
sliver of the Store types its parameter with a role protocol from here
— not ``Store`` (drags the 23-mixin concrete class into the import
graph) and not ``Any`` (no checking, breeds defensive ``getattr``).
Each protocol lists exactly the methods its role calls; grow a protocol
— or add a new one — as more ``store: Any`` sites convert. The real
``Store`` satisfies all of these structurally; tests may pass a minimal
fake instead of a live pool.

This module stays import-light on purpose: typing only, no
``precis.store.store`` import, so it can be imported from anywhere
without cycles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from psycopg import Connection

    from precis.store.types import Ref, Tag


class PoolStore(Protocol):
    """The raw-SQL escape hatch: all the caller does is
    ``store.pool.connection()``. ``pool`` is ``Any`` rather than
    ``ConnectionPool`` so test fakes can hand back a stub connection
    context manager without subclassing psycopg_pool."""

    pool: Any


class ClaimTrustStore(PoolStore, Protocol):
    """What taproot's trust derivation reads: tags (lifecycle status),
    refs (kind/meta), cite-key aliases, plus raw SQL for the seniority
    queries (via :class:`PoolStore`)."""

    def tags_for(self, ref_id: int, *, pos: int | None = ...) -> list[Tag]: ...
    def fetch_refs_by_ids(
        self, ref_ids: Iterable[int], *, include_deleted: bool = ...
    ) -> dict[int, Ref]: ...
    def ref_cite_keys(
        self, ref_id: int, *, conn: Connection | None = ...
    ) -> list[str]: ...
    def ref_cite_keys_bulk(self, ref_ids: Iterable[int]) -> dict[int, list[str]]: ...


class SettingsStore(Protocol):
    """The ``app_state`` key/value surface (cursor bookkeeping)."""

    def get_setting(self, key: str) -> str | None: ...
    def set_setting(self, key: str, value: str) -> None: ...


class OpenTagMetaStore(Protocol):
    """Look up the first ref meta carrying a given open tag (patent CQL lift)."""

    def find_first_meta_for_open_tag(self, *, kind: str, tag: str) -> dict | None: ...


# ── wave 2 (codereview-store-typing-seam) ───────────────────────────────


class LinksStore(Protocol):
    """A "who links here" reader: the incoming/outgoing edge index plus a
    batch ref lookup to resolve the endpoints (``papers._backlinks``)."""

    def links_for(
        self, ref_id: int, *, direction: str = ..., relation: str | None = ...
    ) -> list[Any]: ...
    def fetch_refs_by_ids(
        self, ref_ids: Iterable[int], *, include_deleted: bool = ...
    ) -> dict[int, Ref]: ...


class RefsByIdStore(Protocol):
    """Just the batch ref lookup — callers that only need to turn ids into
    refs (e.g. resolving a dossier's own slug for a canonical URL)."""

    def fetch_refs_by_ids(
        self, ref_ids: Iterable[int], *, include_deleted: bool = ...
    ) -> dict[int, Ref]: ...


class RefMetaStore(Protocol):
    """Read-modify a ref's ``meta`` JSONB by (kind, id) — the weave-tick
    quest-body-flag write. ``get_ref`` returns ``Any`` rather than
    ``Ref | None`` so a minimal test double can hand back its own
    ref-like stand-in without subclassing the real type."""

    def get_ref(self, *, kind: str, id: int) -> Any: ...
    def stamp_ref_meta(self, ref_id: int, updates: dict[str, Any]) -> None: ...


# ── long-tail (codereview-store-typing-seam) ────────────────────────────


class _ChunksAccessor(Protocol):
    """Just the read surface :mod:`precis.utils.toc_db`'s clustering
    renderer needs off the composed ``store.chunks`` sub-store."""

    def list_chunks_for_ref(
        self, ref_id: int, *, pos_range: tuple[int, int] | None = ...
    ) -> Sequence[Any]: ...


class ChunkListingStore(Protocol):
    """A store exposing only ``chunks.list_chunks_for_ref`` — the TOC
    renderer (``render_from_store`` / ``build_toc_segments``) never
    touches anything else on ``Store``. ``chunks`` is a read-only
    ``@property`` (not a plain attribute) so the real ``Store``'s
    composed, non-settable ``chunks`` sub-store satisfies it."""

    @property
    def chunks(self) -> _ChunksAccessor: ...


# ── long-tail, export/reading/taproot/pathway/backfill/pcb batch ───────


class RefLookupStore(Protocol):
    """Resolve a paper/patent/datasheet slug to its ref, plus the batch
    DOI/arXiv alias lookup — the citation resolvers shared by the docx and
    LaTeX exporters (``export/docx.py::_resolve_source``/``_format_reference``,
    ``export/latex.py::build_bib``)."""

    def get_ref(self, *, kind: str, id: int | str) -> Any: ...
    def identifiers_for_refs(self, ref_ids: list[int]) -> dict[int, dict[str, str]]: ...


class PdfLookupStore(Protocol):
    """Resolve a cited slug to its ref, then locate its PDF by sha — the
    draft-export "which cited sources have a local copy" surface
    (``export/sources.py``'s private per-slug/per-ref resolvers)."""

    def get_ref(self, *, kind: str, id: int | str) -> Any: ...
    def pdf_storage_path(self, pdf_sha256: str) -> str | None: ...


class DraftsSubStore(Protocol):
    """Callers that only need the ``drafts`` sub-store facade (composed off
    :class:`~precis.store.Store`) — e.g. the audio narration walk
    (``export/audio.py::export_audio``, which forwards ``store`` into
    :func:`precis.draft.narrate.render_narration`). ``drafts`` is a
    read-only property typed ``Any`` (matching ``Store.drafts``, itself a
    property) rather than the concrete sub-store type, so a minimal test
    double (a ``.drafts`` property returning ``self``) doesn't need to
    subclass the real mixin."""

    @property
    def drafts(self) -> Any: ...


class ChunkSearchStore(PoolStore, Protocol):
    """Raw SQL (via :class:`PoolStore`) plus the ``chunks`` sub-store's
    semantic search — the hub-refine dry-run harness's read-only surface
    (``taproot/slice_refine_eval.py``). ``chunks`` is a read-only property
    typed ``Any`` (matching ``Store.chunks``, itself a property) rather
    than the concrete sub-store type, so a minimal fake (a ``chunks``
    property returning itself) doesn't need to subclass the real mixin."""

    @property
    def chunks(self) -> Any: ...


class PinStore(ClaimTrustStore, Protocol):
    """:class:`ClaimTrustStore` plus universal-handle resolution — the
    authorial-pin application policy shared by ``precis resolve`` and the
    draft exporters (``taproot/cite.py::resolve_pin_handle``/``apply_pin``)."""

    def resolve_handle(self, handle: str, *, conn: Connection | None = ...) -> Any: ...
