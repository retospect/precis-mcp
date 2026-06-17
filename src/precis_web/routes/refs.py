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

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape

from precis.errors import NotFound
from precis_web.deps import await_dispatch, get_store, templates

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
    # ``_REF_KIND_SET`` is the old per-kind nav list (memory / conv /
    # oracle / gripe / patent / pres). After T12.6 the detail + list
    # routes serve every kind in ``_REFS_BROWSABLE_KINDS`` (web,
    # youtube, perplexity-research, etc. — anything search lists),
    # so the gate has to use that set or live refs like /refs/youtube/N
    # 400 with "no browse tab" even though their detail page renders
    # fine.
    if kind not in _REFS_BROWSABLE_KINDS:
        raise NotFound(
            f"no browse tab for kind={kind!r}",
            next=f"browsable kinds: {sorted(_REFS_BROWSABLE_KINDS)}",
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


#: Meta keys rendered as dedicated fields on a turn. Everything else
#: in ``Block.meta`` falls into ``extra_meta`` and is shown as a
#: key/value strip so the operator sees the full per-turn record
#: (stop_reason, token counts, msg_id, source flags, …).
_TURN_SPECIAL_META: frozenset[str] = frozenset({"author", "ts", "chunk_kind"})


def _conv_turns(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Structured turns for the conversation transcript view.

    Reads body chunks (one per turn) straight off the store so the web
    renders a human-readable chat transcript — the handler's ``get``
    overview is the agent-facing card (with ``Next:`` call
    affordances), which is noise for a person reading a thread.

    Each turn carries ``chunk_kind`` (paragraph / conv_message / …)
    and ``extra_meta`` — every ``meta`` key not consumed by a
    dedicated field. The strip surfaces stop_reason / input_tokens /
    output_tokens / msg_id and any other bridge-stamped fields so a
    reader sees the complete per-turn record without needing to drop
    into the MCP get(view='last-meta').
    """
    turns: list[dict[str, Any]] = []
    for b in store.list_blocks_for_ref(ref_id):
        meta = getattr(b, "meta", None) or {}
        author = meta.get("author") or "?"
        extra = [
            (k, v)
            for k, v in sorted(meta.items())
            if k not in _TURN_SPECIAL_META and v is not None and v != ""
        ]
        turns.append(
            {
                "pos": b.pos,
                "author": author,
                "dot": _author_dot(author),
                "ts": _fmt_turn_ts(meta.get("ts")),
                "text": b.text or "",
                "chunk_kind": (
                    meta.get("chunk_kind") or getattr(b, "chunk_kind", "") or ""
                ),
                "extra_meta": extra,
            }
        )
    return turns


#: The kinds the Refs tab pre-checks by default — note-like, browsable,
#: low-friction. The other checkbox-eligible kinds stay unchecked
#: unless the operator opts in (via ``?all=1`` or by tickering them
#: manually). Order pinned for stable rendering.
_DEFAULT_REFS_KINDS: tuple[str, ...] = ("memory", "conv", "gripe", "pres")

#: Every kind the consolidated Refs page knows how to render. Kept as
#: a static list — extending it is a one-liner when a new browsable
#: kind ships. We don't trust the hub's full ``kinds`` set here because
#: it includes non-browsable kinds (calc / random / math) whose
#: ``list_refs`` would either error or render meaningless.
_REFS_BROWSABLE_KINDS: tuple[str, ...] = (
    "memory",
    "conv",
    "gripe",
    "pres",
    "oracle",
    "paper",
    "patent",
    "todo",
    "job",
    "finding",
    "citation",
    "flashcard",
    "perplexity-research",
    "perplexity-reasoning",
    "web",
    "youtube",
    "websearch",
    "cron",
    "message",
    # Cached generators / utility kinds — they still store refs in the
    # DB so detail pages work; list pages render whatever the kind's
    # ``list_refs`` returns (empty for the on-demand kinds when the
    # cache is cold). Added 2026-06-16 after live 400s on
    # /refs/math/* and /refs/finding/* from hover-preview links.
    "math",
    "calc",
    "skill",
    "tag",
    "provenance",
    "random",
)

_PER_KIND_LIMIT = 20  # cap rows per kind so 19-kind search stays readable


# ---- References extraction (MVP for #188) ---------------------------
#
# Scan a body for the same kind:ref shapes the linkifier picks up
# (prefixed ``kind:slug``, bare paper cite_keys, bare discord conv
# handles). Resolve each in a single batched query and shape an
# expansion for inline rendering below the body.

#: Match prefixed ``kind:ref(~chunk)?`` exactly the same way the
#: linkifier does. Imported lazily from the linkify module so the
#: detection grammar stays single-sourced.
_REF_HANDLE_RE = __import__(
    "precis_web.linkify", fromlist=["_REF_PATTERN"]
)._REF_PATTERN
_BARE_PAPER_RE = __import__(
    "precis_web.linkify", fromlist=["_BARE_PAPER_PATTERN"]
)._BARE_PAPER_PATTERN
_BARE_CONV_RE = __import__(
    "precis_web.linkify", fromlist=["_BARE_CONV_PATTERN"]
)._BARE_CONV_PATTERN

#: Same kind allowlist as the linkifier — only resolve handles that
#: would have rendered as anchors. Avoids surfacing prose tokens like
#: ``user:asa`` as "broken reference: no such user".
_LINKIFY_KINDS = __import__(
    "precis_web.linkify", fromlist=["_LINKIFY_KINDS"]
)._LINKIFY_KINDS


def _extract_handles(body: str) -> list[tuple[str, str, str | None]]:
    """Walk ``body`` for every kind:ref handle. Returns ``(kind, id,
    chunk)`` triples in appearance order, deduplicated by (kind, id,
    chunk). Bare paper cite_keys map to ``paper``; bare discord handles
    map to ``conv``."""
    if not body:
        return []
    seen: set[tuple[str, str, str | None]] = set()
    out: list[tuple[str, str, str | None]] = []

    def _push(kind: str, ref_id: str, chunk: str | None) -> None:
        ref_id = ref_id.lstrip("#")
        key = (kind, ref_id, chunk)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for m in _REF_HANDLE_RE.finditer(body):
        kind = m.group("kind")
        if kind not in _LINKIFY_KINDS:
            continue
        _push(kind, m.group("id"), m.group("chunk"))
    for m in _BARE_CONV_RE.finditer(body):
        whole = m.group(0)
        slug, _, suffix = whole.partition("~")
        _push("conv", slug, ("~" + suffix) if suffix else None)
    for m in _BARE_PAPER_RE.finditer(body):
        whole = m.group(0)
        slug, _, suffix = whole.partition("~")
        _push("paper", slug, ("~" + suffix) if suffix else None)
    return out


def _expand_handle(
    store: Any, kind: str, ref_id: str, chunk: str | None
) -> dict[str, Any]:
    """Resolve one ``(kind, id, chunk)`` triple to a display row.

    Returns a row carrying:
      ``handle`` — what to print as the cite handle
      ``url`` — click-through URL (the resolver path)
      ``title`` — best-effort title (paper cite, memory id, etc.)
      ``preview`` — short body preview when available
      ``status`` — ``"resolved"`` / ``"missing"`` / ``"deleted"``
    """
    raw_handle = f"{kind}:{ref_id}" + (chunk or "")
    url = f"/r/{kind}/{ref_id}" + (f"?chunk={chunk[1:]}" if chunk else "")
    # Numeric ids vs slugs: try int first, fall back to slug lookup
    # via the ref_identifiers table (same path the preview route uses).
    numeric_id: int | None = None
    try:
        numeric_id = int(ref_id)
    except ValueError:
        pass
    ref = None
    if numeric_id is not None:
        ref = store.fetch_refs_by_ids([numeric_id], include_deleted=True).get(
            numeric_id
        )
    else:
        try:
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT ref_id FROM ref_identifiers "
                    "WHERE id_kind = 'cite_key' AND id_value = %s",
                    (ref_id,),
                ).fetchone()
        except Exception:
            row = None
        if row is not None:
            ref = store.fetch_refs_by_ids(
                [int(row[0])], include_deleted=True
            ).get(int(row[0]))
    if ref is None:
        return {
            "handle": raw_handle,
            "url": url,
            "title": "(not found)",
            "preview": "",
            "status": "missing",
            "kind": kind,
        }
    if getattr(ref, "deleted_at", None) is not None:
        return {
            "handle": raw_handle,
            "url": url,
            "title": (getattr(ref, "title", "") or "(untitled)").split("\n", 1)[0][:120],
            "preview": "(deleted)",
            "status": "deleted",
            "kind": kind,
        }
    title = (getattr(ref, "title", "") or "(untitled)").split("\n", 1)[0][:160]
    preview = ""
    # For chunk-addressed handles, fetch the actual chunk text.
    if chunk and chunk.startswith("~") and chunk[1:].isdigit():
        ord_pos = int(chunk[1:])
        try:
            blocks = store.list_blocks_for_ref(ref.id)
            for b in blocks:
                if getattr(b, "pos", -1) == ord_pos:
                    preview = (b.text or "")[:400].rstrip()
                    if len(b.text or "") > 400:
                        preview += "…"
                    break
        except Exception:
            pass
    if not preview:
        # Fall back to the first block (or the title-derived hint).
        try:
            blocks = store.list_blocks_for_ref(ref.id)
            if blocks:
                preview = (blocks[0].text or "")[:400].rstrip()
                if len(blocks[0].text or "") > 400:
                    preview += "…"
        except Exception:
            pass
    # Citation metadata for BibTeX / Markdown export — only meaningful
    # for paper kind, but the dict shape is uniform so the template
    # doesn't have to branch.
    citation: dict[str, Any] = {}
    if kind == "paper":
        slug = getattr(ref, "slug", None) or ""
        authors = getattr(ref, "authors", None) or []
        year = getattr(ref, "year", None)
        # Authors come in as ``[{"family":..., "given":...}, ...]``.
        author_names = []
        for a in authors:
            if isinstance(a, dict):
                fam = (a.get("family") or "").strip()
                given = (a.get("given") or "").strip()
                if fam and given:
                    author_names.append(f"{fam}, {given}")
                elif fam:
                    author_names.append(fam)
        # Try to pull DOI off ref.meta if the handler stored it there
        # (papers ingested from Crossref do).
        meta = getattr(ref, "meta", None) or {}
        doi = meta.get("doi") if isinstance(meta, dict) else None
        citation = {
            "cite_key": slug,
            "authors": author_names,
            "year": year,
            "doi": doi,
            "url": (f"https://doi.org/{doi}" if doi else None),
        }

    return {
        "handle": raw_handle,
        "url": url,
        "title": title,
        "preview": preview,
        "status": "resolved",
        "kind": kind,
        "slug": getattr(ref, "slug", None) or "",
        "citation": citation,
    }


@router.get("", response_class=HTMLResponse)
async def consolidated(
    request: Request,
    q: str | None = None,
    kinds: str | None = None,
    all: int = 0,
) -> HTMLResponse:
    """Consolidated cross-kind ref browser with kind checkboxes.

    Replaces the old per-kind nav tabs for memory / conv / gripe / pres
    — see ``T12.6`` in the session notes. Each kind has a checkbox;
    when ``?all=1`` is set, every browsable kind lights regardless of
    the ``kinds`` query param. The 🔍 loupe in the nav posts here with
    ``?all=1`` so a global query hits everything we have local.

    Per-kind detail (``/refs/{kind}/{ref_id}``) and the per-kind list
    pages (``/refs/{kind}``) keep working — they're the long-form
    affordances for pagination, date filters, sort. The consolidated
    view is the casual "I half-remember something" surface.
    """
    if all:
        selected: list[str] = list(_REFS_BROWSABLE_KINDS)
    elif kinds:
        # Tolerate trailing commas / whitespace / unknown kinds.
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        selected = [k for k in _REFS_BROWSABLE_KINDS if k in requested]
        # Preserve the operator's ordering for kinds we didn't recognise
        # so a future-added kind shows up when its checkbox is added.
        for k in requested:
            if k not in selected and k not in _REFS_BROWSABLE_KINDS:
                selected.append(k)
    else:
        selected = list(_DEFAULT_REFS_KINDS)

    store = get_store(request)
    query = (q or "").strip()
    by_kind: dict[str, list[dict[str, object]]] = {}
    for kind in selected:
        try:
            if query:
                hits = store.search_refs_lexical(
                    q=query, kind=kind, limit=_PER_KIND_LIMIT
                )
                refs = [ref for ref, _ in hits]
            else:
                refs = store.list_refs(kind=kind, limit=_PER_KIND_LIMIT)
        except Exception:
            # Unsupported / unregistered kind on this process — skip the
            # whole bucket rather than 500 the page.
            continue
        if not refs:
            continue
        rows: list[dict[str, object]] = []
        for r in refs:
            title = (getattr(r, "title", "") or "").split("\n", 1)[0]
            if len(title) > 80:
                title = title[:80].rstrip() + "…"
            rows.append(
                {
                    "id": r.id,
                    "title": title or "(untitled)",
                    "url": _consolidated_ref_url(kind, r.id),
                }
            )
        by_kind[kind] = rows

    return templates.TemplateResponse(
        request,
        "refs/consolidated.html.j2",
        {
            "active_tab": "refs",
            "q": query,
            "selected": set(selected),
            "all_browsable": list(_REFS_BROWSABLE_KINDS),
            "default_kinds": list(_DEFAULT_REFS_KINDS),
            "by_kind": by_kind,
            "all_lit": bool(all),
            "total": sum(len(v) for v in by_kind.values()),
        },
    )


#: Per-kind URL shape for the native detail viewer in consolidated view.
_CONSOLIDATED_KIND_URLS: dict[str, str] = {
    "paper": "/papers/{id}",
    "todo": "/tasks?focus={id}",
    "job": "/tasks?focus={id}",
}


def _consolidated_ref_url(kind: str, ref_id: int) -> str:
    template = _CONSOLIDATED_KIND_URLS.get(kind, "/refs/{kind}/{id}")
    return template.format(kind=kind, id=ref_id)


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
            "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
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
                "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
                "ref": _row(ref),
                "turns": _conv_turns(store, ref.id),
            },
        )

    # Slug kinds (oracle/patent/pres) address get() by slug; numeric
    # kinds (memory/gripe) by id. Prefer the slug when present.
    addr: str | int = ref.slug if ref.slug else ref.id
    body, is_error = await await_dispatch(request, "get", {"kind": kind, "id": addr})

    # Disabled-but-cached fallback: when the handler is currently
    # registered-but-disabled (math without WOLFRAM_APP_ID, web without
    # outbound HTTP, etc.) but the ref already exists with cached body
    # chunks, render the cached body directly rather than showing the
    # operator a wall of "[error:Unsupported]". The cache is still
    # valuable even when fresh fetches can't run — that's why we keep
    # it. Tag the response so the template can show a quiet banner.
    body_disabled_notice: str | None = None
    if is_error and "disabled in this build" in (body or ""):
        cached_chunks = list(store.list_blocks_for_ref(ref.id))
        if cached_chunks:
            cached_text = "\n\n".join(
                (b.text or "").strip() for b in cached_chunks if b.text
            )
            if cached_text:
                body = cached_text
                is_error = False
                body_disabled_notice = (
                    f"kind {kind!r} is currently disabled in this build; "
                    "showing the cached body. Fresh fetches will resume "
                    "once the required env (e.g. WOLFRAM_APP_ID) is set."
                )

    # Patent body text lives in body chunks; the handler's overview
    # only renders the bibliographic header + abstract excerpt. Pull
    # the chunks so the detail view can show the full text (description
    # + claims) as one row per chunk — what's actually in the corpus.
    chunks: list[dict[str, Any]] = []
    if kind == "patent":
        for b in store.list_blocks_for_ref(ref.id):
            chunks.append(
                {
                    "pos": b.pos,
                    "chunk_kind": getattr(b, "chunk_kind", "paragraph"),
                    "slug": b.slug or "",
                    "text": b.text or "",
                }
            )

    # Tag editor — every browsable kind gets the same chip strip.
    # Closed-vocab tags (STATUS:*, LLM:*, DREAM:*) appear but the
    # template doesn't offer a × on them; per-ref removal of a
    # structural tag goes through the standard tag() verb explicitly.
    raw_tags = store.tags_for(ref.id)
    tags = [
        {
            "namespace": getattr(t, "namespace", "OPEN"),
            "value": getattr(t, "value", ""),
            "label": (
                f"{getattr(t, 'namespace', 'OPEN')}:{getattr(t, 'value', '')}"
                if getattr(t, "namespace", "") not in ("", "OPEN")
                else getattr(t, "value", "")
            ),
            "deletable": getattr(t, "namespace", "OPEN") == "OPEN",
        }
        for t in raw_tags
    ]

    # References panel (MVP — memory views only, where dreams live).
    # Walk the body for ref handles, resolve each, build a list to
    # render below the body. Cheap reads — at most ~20 handles per
    # memory typical, batched into ``fetch_refs_by_ids``.
    references: list[dict[str, Any]] = []
    if kind == "memory" and not is_error and body:
        handles = _extract_handles(body)
        for ref_kind, ref_ident, chunk in handles:
            references.append(_expand_handle(store, ref_kind, ref_ident, chunk))

    return templates.TemplateResponse(
        request,
        "refs/detail.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
            "ref": _row(ref),
            "body": body,
            "is_error": is_error,
            "chunks": chunks,
            "tags": tags,
            "body_disabled_notice": body_disabled_notice,
            "references": references,
        },
    )


def _split_tag_input(raw: str) -> list[str]:
    """Split a comma/space-separated tag input into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


@router.post("/{kind}/{ref_id}/tags")
async def edit_tags(
    request: Request,
    kind: str,
    ref_id: int,
    add: str = Form(""),
    remove: str = Form(""),
) -> Response:
    """Add or remove tags on a browsable ref via the ``tag`` verb.

    Same shape as ``/tasks/{id}/tags`` — ``add`` is a comma/space-
    separated string the operator typed; ``remove`` is a single
    ``namespace:value`` from a chip's × button. Both flow through
    the handler so tag-vocabulary validation stays single-sourced.
    """
    _require_kind(kind)
    add_list = _split_tag_input(add)
    remove_list = _split_tag_input(remove)
    redirect_url = f"/refs/{kind}/{ref_id}"
    if not add_list and not remove_list:
        return RedirectResponse(url=redirect_url, status_code=303)
    args: dict[str, Any] = {"kind": kind, "id": ref_id}
    if add_list:
        args["add"] = add_list
    if remove_list:
        args["remove"] = remove_list
    body, is_error = await await_dispatch(request, "tag", args)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Tag error", "detail": body, "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=redirect_url, status_code=303)
