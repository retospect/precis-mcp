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
    from collections.abc import Iterable

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
