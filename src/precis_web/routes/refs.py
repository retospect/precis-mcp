"""Refs tab(s) — browse durable ref kinds.

One generic list/detail surface served per kind via ``/refs/{kind}``.
Each browsable kind gets its own top-nav tab (see ``base.html.j2``),
so the nav doubles as the kind selector — there is exactly one route
module and one pair of templates behind every tab.

* List reads off the DB: ``search_refs_lexical`` when a query is
  present (ranked), else ``list_refs`` with the date / tag filters and
  the whitelisted sort. Pagination is offset-based.
* Detail renders the handler's own ``get`` output read-only (through
  the in-process runtime, so the rendering can't drift from MCP).

This surface is read-only by design — mutations stay on the verb-
specific tabs (Tasks) or the Console. Slug kinds (conv / oracle /
patent / pres) and numeric kinds (memory / gripe) are both addressed
in the URL by their numeric ``ref_id``; the detail view resolves the
canonical address (slug when present, else id) for the ``get`` call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup, escape

from precis.errors import NotFound
from precis_web.deps import dispatch, get_store, templates

router = APIRouter(prefix="/refs", tags=["refs"])

#: Browsable ref kinds, in nav order: ``(kind, label)``. The nav in
#: ``base.html.j2`` renders one tab per entry; adding a kind here +
#: the nav loop is all it takes to expose another browser.
REF_KINDS: tuple[tuple[str, str], ...] = (
    ("memory", "Memory"),
    ("conv", "Conversations"),
    ("oracle", "Oracle"),
    ("gripe", "Gripes"),
    ("patent", "Patents"),
    ("pres", "Presentations"),
)
_REF_KIND_SET = {k for k, _ in REF_KINDS}
_REF_KIND_LABEL = dict(REF_KINDS)

#: Date-window presets → lookback delta (``None`` = no date filter).
DATE_PRESETS: tuple[tuple[str, str, timedelta | None], ...] = (
    ("any", "Any time", None),
    ("24h", "Last 24h", timedelta(hours=24)),
    ("7d", "Last 7 days", timedelta(days=7)),
    ("30d", "Last 30 days", timedelta(days=30)),
    ("90d", "Last 90 days", timedelta(days=90)),
)
_DATE_DELTA = {key: delta for key, _, delta in DATE_PRESETS}

#: Sort options exposed in the UI → ``Store.list_refs`` order_by keys.
SORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("updated_desc", "Updated (newest)"),
    ("updated_asc", "Updated (oldest)"),
    ("created_desc", "Created (newest)"),
    ("title_asc", "Title A–Z"),
    ("id_desc", "ID (newest)"),
)
_SORT_KEYS = {k for k, _ in SORT_CHOICES}

_PAGE_SIZE = 50


def _require_kind(kind: str) -> None:
    if kind not in _REF_KIND_SET:
        raise NotFound(
            f"no browse tab for kind={kind!r}",
            next=f"browsable kinds: {sorted(_REF_KIND_SET)}",
        )


def _parse_tags(raw: str | None) -> list[str]:
    """Split a comma/space separated tag filter into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def _title_preview(title: str) -> Markup:
    """First two non-empty lines of ``title``, joined by ``<br>``.

    Memory / digest titles can be the whole document body — a row that
    bare-prints the title fills the list with one giant entry. Picking
    the first two non-empty lines is enough to recognise the entry
    (the leading ``# heading`` plus the first prose line), and the
    explicit ``<br>`` keeps both visible without paragraph spacing.

    Per-line content is HTML-escaped (XSS guard) and the ``<br>`` is
    emitted raw — returns ``Markup`` so Jinja honours the mix.
    """
    lines = [ln for ln in (title or "").splitlines() if ln.strip()]
    if not lines:
        return Markup("(untitled)")
    return Markup("<br>").join(escape(ln) for ln in lines[:2])


def _row(ref: Any) -> dict[str, Any]:
    updated = getattr(ref, "updated_at", None)
    title = ref.title or "(untitled)"
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": title,
        "title_preview": _title_preview(title),
        "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
    }


def _fmt_turn_ts(ts: Any) -> str:
    """Best-effort human timestamp for a conv turn's ``meta['ts']``.

    Turns carry ``ts`` as an ISO string (Discord bridge) or a
    datetime; tolerate both and anything else by stringifying. Empty
    when absent.
    """
    if not ts:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    s = str(ts)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return s


#: Author dot colours for the transcript, picked deterministically by
#: author name so the same speaker keeps one colour down a thread.
_AUTHOR_DOTS: tuple[str, ...] = (
    "bg-sky-500",
    "bg-emerald-500",
    "bg-violet-500",
    "bg-amber-500",
    "bg-rose-500",
    "bg-teal-500",
)


def _author_dot(author: str) -> str:
    """Stable colour class for an author (presentation only)."""
    idx = sum(ord(c) for c in author) % len(_AUTHOR_DOTS)
    return _AUTHOR_DOTS[idx]


def _conv_turns(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Structured turns for the conversation transcript view.

    Reads body chunks (one per turn) straight off the store so the web
    renders a human-readable chat transcript — the handler's ``get``
    overview is the agent-facing card (with ``Next:`` call
    affordances), which is noise for a person reading a thread.
    """
    turns: list[dict[str, Any]] = []
    for b in store.list_blocks_for_ref(ref_id):
        meta = getattr(b, "meta", None) or {}
        author = meta.get("author") or "?"
        turns.append(
            {
                "pos": b.pos,
                "author": author,
                "dot": _author_dot(author),
                "ts": _fmt_turn_ts(meta.get("ts")),
                "text": b.text or "",
            }
        )
    return turns


@router.get("/{kind}", response_class=HTMLResponse)
async def index(
    request: Request,
    kind: str,
    q: str | None = None,
    tag: str | None = None,
    since: str = "any",
    sort: str = "updated_desc",
    page: int = 1,
) -> HTMLResponse:
    """List / search one ref kind with date + tag filters and sort."""
    _require_kind(kind)
    store = get_store(request)

    tags = _parse_tags(tag)
    since = since if since in _DATE_DELTA else "any"
    sort = sort if sort in _SORT_KEYS else "updated_desc"
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    query = (q or "").strip()
    if query:
        # Ranked title search; date / sort don't apply to a relevance
        # ordering, so they're shown but inert while a query is active.
        hits = store.search_refs_lexical(
            q=query, kind=kind, tags=tags or None, limit=_PAGE_SIZE
        )
        refs = [ref for ref, _score in hits]
        has_next = False
    else:
        updated_after: datetime | None = None
        delta = _DATE_DELTA.get(since)
        if delta is not None:
            updated_after = datetime.now(UTC) - delta
        refs = store.list_refs(
            kind=kind,
            tags=tags or None,
            updated_after=updated_after,
            order_by=sort,
            limit=_PAGE_SIZE + 1,  # one extra row probes "has next page"
            offset=offset,
        )
        has_next = len(refs) > _PAGE_SIZE
        refs = refs[:_PAGE_SIZE]

    return templates.TemplateResponse(
        request,
        "refs/index.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL[kind],
            "rows": [_row(r) for r in refs],
            "q": query,
            "tag": tag or "",
            "since": since,
            "sort": sort,
            "page": page,
            "has_next": has_next,
            "date_presets": [(k, label) for k, label, _ in DATE_PRESETS],
            "sort_choices": SORT_CHOICES,
        },
    )


@router.get("/{kind}/{ref_id}", response_class=HTMLResponse)
async def detail(request: Request, kind: str, ref_id: int) -> HTMLResponse:
    """Read-only detail: the handler's own ``get`` output for this ref."""
    _require_kind(kind)
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != kind:
        raise NotFound(f"{kind} id={ref_id} not found")

    # Conversations render as a human-readable chat transcript (one
    # turn per body chunk) rather than the handler's agent-facing
    # overview card — a person clicking a thread wants the turns, not
    # the `Next:` call affordances meant for the LLM.
    if kind == "conv":
        return templates.TemplateResponse(
            request,
            "refs/conv_detail.html.j2",
            {
                "active_tab": f"refs:{kind}",
                "kind": kind,
                "kind_label": _REF_KIND_LABEL[kind],
                "ref": _row(ref),
                "turns": _conv_turns(store, ref.id),
            },
        )

    # Slug kinds (oracle/patent/pres) address get() by slug; numeric
    # kinds (memory/gripe) by id. Prefer the slug when present.
    addr: str | int = ref.slug if ref.slug else ref.id
    body, is_error = dispatch(request, "get", {"kind": kind, "id": addr})

    return templates.TemplateResponse(
        request,
        "refs/detail.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL[kind],
            "ref": _row(ref),
            "body": body,
            "is_error": is_error,
        },
    )
