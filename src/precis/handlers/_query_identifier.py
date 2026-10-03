"""Resolve a free-text search query that *is* an identifier to a ref.

The web ``/drive`` search box short-circuits through this before the
chunk search, mirroring the bare-DOI / handle shortcut
:meth:`~precis.handlers._paper_search.FusedBlockSearch.run` gives the MCP
search tool (which keeps its own copy — it carries scope/filter gating
this lookup does not need):

- a lone record handle (``pa5``, ``td12``, ``gr462129`` …, any prefix the
  :mod:`~precis.utils.handle_registry` knows) resolves to its live ref,
  following a merge/supersede tombstone;
- a DOI, whole or a prefix (``10.1021/acscatal.3c0196``), resolves to the
  paper — exact match first, else a *unique* live prefix match.

An ambiguous DOI prefix resolves to nothing but reports its candidate
ref ids, so a caller can fall through to the ordinary search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.store._identifiers_ops import _DOI_RE
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store.store import Store

#: Candidate cap for an ambiguous prefix — only none/one/several matters.
_PREFIX_LIMIT = 5


@dataclass
class QueryIdentifierMatch:
    """Outcome of :func:`resolve_query_identifier`."""

    #: The single live ref the query names, else ``None``.
    ref: Any | None = None
    #: Ref ids of an ambiguous DOI prefix (``ref`` is ``None``).
    ambiguous_ref_ids: list[int] = field(default_factory=list)


def resolve_query_identifier(store: Store, q: str) -> QueryIdentifierMatch:
    """Resolve ``q`` as a record handle or DOI; see the module docstring."""
    text = (q or "").strip()
    if not text:
        return QueryIdentifierMatch()

    parsed = handle_registry.parse(text)
    if parsed is not None:
        kind, is_chunk, _pk = parsed
        if is_chunk:
            return QueryIdentifierMatch()
        resolved = store.resolve_handle(text)
        if resolved is None:
            return QueryIdentifierMatch()
        try:
            ref = resolve_live_slug_ref(
                store,
                kind=resolved.kind,
                id=handle_registry.format_handle(resolved.kind, resolved.ref_id),
            )
        except NotFound:
            return QueryIdentifierMatch()
        return QueryIdentifierMatch(ref=ref)

    if _DOI_RE.match(text) is None:
        return QueryIdentifierMatch()
    slug = store.find_paper_slug_by_doi(text)
    if slug is not None:
        try:
            return QueryIdentifierMatch(
                ref=resolve_live_slug_ref(store, kind="paper", id=slug)
            )
        except NotFound:
            pass
    ids = store.find_paper_ref_ids_by_doi_prefix(text, limit=_PREFIX_LIMIT)
    if len(ids) == 1:
        only = store.fetch_refs_by_ids(ids).get(ids[0])
        return QueryIdentifierMatch(ref=only)
    return QueryIdentifierMatch(ambiguous_ref_ids=ids)
