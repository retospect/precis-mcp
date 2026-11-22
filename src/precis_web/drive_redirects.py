"""Retired list surfaces folded into Drive — one redirect table.

WS1a/WS1b, gr636493 and the nav restructure retired the thin per-kind
list pages; each left a handler that redirects to the Drive facet preset
that replaced it. gr664412 collects those targets here —
:data:`FOLDED_LIST_TARGETS` for the static presets, the ``*_url``
builders for folds that carry live query params through — so the route
modules stop restating the same URL spelling in eight docstrings.

Spellings shared by the presets: ``folder=*`` ("Anywhere") drops Drive's
default top-level filter so a whole-kind pivot lists filed rows beside
unfiled ones — matching the retired lists, which had no folder notion;
``sort=recency`` is their newest-first default; ``submitted=1`` pins the
``k=`` scope explicitly so it can't resolve to the ``items_kinds``
cookie instead (``routes/drive.py``'s kind-selection precedence).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse


def _drive_url(*pairs: tuple[str, str | None], safe: str = "") -> str:
    """``/drive?`` plus the non-``None`` pairs, order preserved; ``safe``
    characters stay literal instead of percent-encoded."""
    return "/drive?" + urlencode([(k, v) for k, v in pairs if v], safe=safe)


def kind_list_url(kind: str) -> str:
    """``/drive?k=<kind>&folder=*&sort=recency`` — the preset the retired
    newest-first kind lists fold into (the detail pages' own
    ``_drive_back.html.j2`` up-link spells the same target, ``*`` literal
    rather than ``%2A``, so the two stay byte-identical)."""
    return _drive_url(("k", kind), ("folder", "*"), ("sort", "recency"), safe="*")


#: Retired index path to its Drive preset.
FOLDED_LIST_TARGETS: dict[str, str] = {
    "/structure": kind_list_url("structure"),
    "/cad": kind_list_url("cad"),
    "/pcb": kind_list_url("pcb"),
    "/figure": kind_list_url("figure"),
    "/mermaid": kind_list_url("mermaid"),
    "/papers-needed": _drive_url(("state", "stub")),
    "/papers": _drive_url(("k", "paper"), ("submitted", "1")),
    "/drafts": _drive_url(("k", "draft"), ("submitted", "1")),
    "/items": "/drive",
}


def folded_redirect(path: str, *, query: str = "") -> RedirectResponse:
    """Append the caller's encoded query to the retired index preset."""
    target = FOLDED_LIST_TARGETS[path]
    if query:
        separator = "&" if "?" in target else "?"
        target += separator + query
    return RedirectResponse(url=target)


def search_index_redirect(path: str, q: str | None) -> RedirectResponse:
    query = (q or "").strip()
    return folded_redirect(path, query=urlencode({"q": query}) if query else "")


def kinds_url(kinds: list[str], *, q: str | None = None) -> str:
    """Pin the consolidated refs selection as Drive kind chips."""
    pairs: list[tuple[str, str | None]] = [("k", k) for k in kinds]
    pairs.extend((("folder", "*"), ("submitted", "1")))
    pairs.append(("q", (q or "").strip() or None))
    return _drive_url(*pairs)


def serving_url(kind: str, qid: int) -> str:
    """The quest retinue count and its destination share direct edges."""
    return refs_list_url(kind, serves=qid)


#: The retired ``/refs/{kind}`` list's sort select → the nearest Drive
#: ``sort=`` facet. The old default ``updated_desc`` IS Drive's default
#: browse ordering. Drive has no ``title_asc`` or ``id_desc`` facet.
_REFS_SORT_TO_DRIVE: dict[str, str] = {
    "updated_asc": "oldest",
    "created_desc": "created",
}

#: The retired ``/refs/{kind}`` list's date-window presets → the lookback
#: delta a Drive ``updated_since=`` timestamp bound encodes. ``any`` is no bound.
_REFS_SINCE_DELTAS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def refs_list_url(
    kind: str,
    *,
    q: str | None = None,
    tags: list[str] | None = None,
    since: str = "",
    sort: str = "",
    serves: int | None = None,
    page: int = 1,
) -> str:
    """The ``/refs/{kind}`` list's Drive fold
    with the retired page's filters mapped onto the matching facets:
    ``serves`` selects direct links and date presets filter by modification
    time in browse mode."""
    pairs: list[tuple[str, str | None]] = [
        ("k", kind),
        ("folder", "*"),
        ("submitted", "1"),
    ]
    query = (q or "").strip()
    if query:
        pairs.append(("q", query))
    pairs.extend(("tag", t) for t in tags or [])
    if serves is not None:
        pairs.append(("directly_serves", str(serves)))
    delta = _REFS_SINCE_DELTAS.get(since or "")
    if delta is not None and not query:
        bound = datetime.now(UTC) - delta
        pairs.append(("updated_since", bound.isoformat()))
    drive_sort = _REFS_SORT_TO_DRIVE.get(sort or "")
    if drive_sort and not query:
        pairs.append(("sort", drive_sort))
    if page > 1:
        pairs.append(("page", str(page)))
    return _drive_url(*pairs)
