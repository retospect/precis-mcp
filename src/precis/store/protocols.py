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

from typing import Any, Protocol


class ReadingOrderStore(Protocol):
    """Read a document's chunk sequence (draft/plan reading order)."""

    def reading_order(self, ref_id: int, *, kind: str = ...) -> list[Any]: ...


class DiagramBindingStore(Protocol):
    """Read a diagram's element↔chunk bindings and resolve bound chunks."""

    def element_bindings(self, node_chunk_id: int) -> list[dict[str, Any]]: ...
    def universal_chunk(self, handle: str) -> dict[str, Any] | None: ...


class DiagramTurnStore(ReadingOrderStore, DiagramBindingStore, Protocol):
    """Everything a diagram edit turn touches: read the document + bindings,
    write the node's source/bindings, stamp turn bookkeeping meta."""

    def edit_text(self, handle: str, text: str, *, kind: str = ...) -> Any: ...
    def add_chunks(self, **kw: Any) -> list[Any]: ...
    def stamp_ref_meta(self, ref_id: int, patch: dict[str, Any]) -> Any: ...
    def set_element_bindings(
        self, *, node_chunk_id: int, desired: list[dict[str, Any]], set_by: str = ...
    ) -> dict[str, int]: ...


class OpenTagMetaStore(Protocol):
    """Look up the first ref meta carrying a given open tag (patent CQL lift)."""

    def find_first_meta_for_open_tag(self, *, kind: str, tag: str) -> dict | None: ...
