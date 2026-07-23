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

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape

from precis.errors import NotFound
from precis.utils import mentions
from precis.utils.authors import author_names
from precis.utils.claude_agent import ClaudeAgentError
from precis_web import ask
from precis_web.deps import (
    await_dispatch,
    get_store,
    get_web_config,
    redirect_or_error,
    templates,
)

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
    created = getattr(ref, "created_at", None)
    refreshed = getattr(ref, "refreshed_at", None)
    auto_refresh_days = getattr(ref, "auto_refresh_days", None)
    title = ref.title or "(untitled)"
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": title,
        "title_preview": _title_preview(title),
        "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
        # Extra meta surfaced on the detail page's header strip. The
        # list templates ignore the keys they don't use, so widening
        # the row here is safe for index / consolidated callers too.
        "created": created.strftime("%Y-%m-%d %H:%M") if created else "",
        "set_by": getattr(ref, "set_by", None) or "",
        "prio": getattr(ref, "prio", None),
        # Relevance-decay window (null auto_refresh_days = permanent).
        # Surfaced together so the operator can see "permanent" vs.
        # "decays over N days since <refreshed>".
        "auto_refresh_days": auto_refresh_days,
        "refreshed": refreshed.strftime("%Y-%m-%d %H:%M") if refreshed else "",
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


def _followup_discussions(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Conv threads spawned from this ref via the "ask a follow-up" box.

    Each follow-up conv is linked ``conv --derived-from--> source``
    (chunk-scoped via ``dst_pos`` when the question was asked on a
    chunk). We surface them on the source's detail page so the
    discussion is reachable from the thought it grew out of.
    """
    try:
        links = store.links_for(ref_id, direction="in", relation="derived-from")
    except Exception:
        return []
    src_ids = [lnk.src_ref_id for lnk in links]
    if not src_ids:
        return []
    refs = store.fetch_refs_by_ids(src_ids, include_deleted=False)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for lnk in links:
        conv = refs.get(lnk.src_ref_id)
        if conv is None or conv.kind != "conv" or conv.id in seen:
            continue
        seen.add(conv.id)
        rows.append(
            {
                "id": conv.id,
                "title": (conv.title or "(untitled)").split("\n", 1)[0][:120],
                "url": f"/refs/conv/{conv.id}",
                "turns": store.count_blocks(conv.id),
                "chunk": lnk.dst_pos,
            }
        )
    return rows


def _job_actions(store: Any, ref: Any, tags: list[Any]) -> dict[str, Any]:
    """Context for the job detail actions strip — retry, transcript, parent.

    The ``/refs/job/{id}`` page is where an operator lands on a
    ``closed:failed`` job (e.g. a swept ``claim-orphaned`` plan_tick).
    They want to *unstick* it, not interrogate it. This gathers what the
    template needs to offer the same affordances the ``/tasks`` dashboard
    already has:

    * **retry** — POST ``/tasks/{id}/retry`` clears the parent's
      ``child-failed:`` bubble so ``dispatch`` re-mints a fresh attempt.
      Only a ``failed`` / ``cancelled`` job is retryable (the handler
      enforces this too; we gate the button to avoid a guaranteed error).
    * **model swap** — offered only when the parent todo is an
      ``LLM:*`` planner, so the re-minted tick can run on a different tier.
    * **transcript** — a link to the readable ``stream-json`` turns, when
      the job captured one.
    * **parent** — the owning todo, so the intent is one click away.
    """
    status: str | None = None
    for t in tags:
        s = str(t)
        if s.startswith("STATUS:"):
            status = s[len("STATUS:") :]
            break

    # A job hangs off an owner ref via ``refs.parent_id`` (ADR 0044). Retry
    # only re-dispatches through the *intent* lane (a ``kind='todo'``
    # parent); a compute-lane job owned by a build subject, or a legacy
    # orphan with no parent, can't be re-minted this way.
    parent_id = getattr(ref, "parent_id", None)
    parent_kind: str | None = None
    is_llm_planner = False
    if parent_id is not None:
        try:
            parent = store.fetch_refs_by_ids([parent_id]).get(parent_id)
        except Exception:
            parent = None
        if parent is not None:
            parent_kind = parent.kind
            if parent_kind == "todo":
                try:
                    is_llm_planner = any(
                        str(t).startswith("LLM:") for t in store.tags_for(parent_id)
                    )
                except Exception:
                    is_llm_planner = False

    meta = ref.meta or {}
    return {
        "job_id": ref.id,
        "status": status,
        "retryable": status in ("failed", "cancelled"),
        # A retry re-dispatches through the parent todo; a legacy orphan
        # (no todo parent) can't be re-minted, so don't offer the button.
        "can_retry": (
            status in ("failed", "cancelled")
            and parent_id is not None
            and parent_kind == "todo"
        ),
        "parent_id": parent_id if parent_kind == "todo" else None,
        "is_llm_planner": is_llm_planner,
        "has_transcript": bool(meta.get("transcript")),
        "job_type": meta.get("job_type"),
    }


def _youtube_meta(store: Any, ref: Any) -> dict[str, Any] | None:
    """Header context for a ``kind='youtube'`` detail page.

    The watch-page scrape (channel / thumbnail / duration) lands in
    ``cache_state.meta`` — not ``refs.meta`` — so pull the cache row to
    surface a clickable **Watch on YouTube** link and the video's
    thumbnail (a "screenshot") above the transcript body. Returns
    ``None`` only when the video id can't be recovered (so the template
    just renders the plain body).

    The thumbnail falls back to the deterministic ``i.ytimg.com`` URL
    when the og:image scrape didn't populate one, so a thumbnail shows
    even for a transcript fetched before the scrape existed.
    """
    slug = getattr(ref, "slug", None) or ""
    meta: dict[str, Any] = {}
    if slug:
        try:
            cached = store.get_cache_entry_by_slug(kind="youtube", slug=slug)
        except Exception:
            cached = None
        if cached is not None:
            meta = cached[1].meta or {}
    video_id = meta.get("video_id") or slug
    if not video_id:
        return None

    duration = ""
    if meta.get("duration_s"):
        sec = int(meta["duration_s"])
        mins, s = divmod(sec, 60)
        duration = f"{mins}m{s:02d}s"
    elif meta.get("duration_iso"):
        duration = str(meta["duration_iso"])

    return {
        "video_id": video_id,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        # Prefer the scraped og:image; fall back to YouTube's stable
        # per-video thumbnail endpoint so a screenshot always renders.
        "thumbnail_url": (
            meta.get("thumbnail_url")
            or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        ),
        "channel_name": meta.get("channel_name") or "",
        "channel_url": meta.get("channel_url") or "",
        "duration": duration,
        "published_at": meta.get("published_at") or "",
    }


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
    "anki",
    "perplexity-research",
    "perplexity-reasoning",
    "web",
    "youtube",
    "websearch",
    "message",
    # Quest (the striving/lab-notebook kind) + its candidate `structure`s.
    # QuestHandler.get(id=N) renders the append-only logbook (the lab
    # notebook) and StructureHandler.get renders the candidate scene through
    # the generic detail template; without these the quest page + every
    # candidate link 400 with "no browse tab" even though they render fine.
    "quest",
    "structure",
    # Machine-detected ops/health rows (non-embedded). The /alerts list
    # links each row to /refs/alert/<id>; without this the detail page
    # 400s ("no browse tab for kind='alert'"). AlertHandler.get(id=N)
    # renders fine through the generic detail template.
    "alert",
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

#: Cache-backed kinds (``CacheBackedHandler`` subclasses) whose ``get``
#: verb *fetches on a miss* — a paid, slow upstream call for the paid
#: tiers. The read-only detail page must pass ``no_fetch=True`` so
#: rendering an existing ref serves the stored body instead of silently
#: re-running the fetch (a ~$0.50 / 2–10 min perplexity-research call, a
#: billed Sonar query) on every page view. Keep in sync with the
#: ``CacheBackedHandler`` subclasses.
_CACHE_BACKED_KINDS: frozenset[str] = frozenset(
    {
        "math",
        "news",
        "orcid",
        "perplexity-reasoning",
        "perplexity-research",
        "semanticscholar",
        "web",
        "websearch",
        "wikipedia",
        "youtube",
    }
)

#: Kinds whose refs do **not** live in the ``refs`` table, so the
#: consolidated browser can't reach them through ``list_refs`` /
#: ``search_refs_lexical`` (both return nothing — confirmed in prod:
#: zero rows for either kind). They ARE searchable through their own
#: ``search`` verb — ``skill`` over the on-disk skill files, ``tag``
#: over the tag vocabulary — so the consolidated view dispatches that
#: verb and renders its markdown result instead of a row grid.
_HANDLER_SEARCHED_KINDS: frozenset[str] = frozenset({"skill", "tag"})

#: Browsable kinds that can only ever render empty in the consolidated
#: view: ``random`` mints on demand and ``provenance`` is a report over
#: other refs — neither has ``refs`` rows *or* a ``search`` verb. They
#: stay in ``_REFS_BROWSABLE_KINDS`` (detail routes may still target
#: them) but are dropped from the browser's checkboxes so the page never
#: offers a control that returns nothing by construction.
_CONSOLIDATED_HIDDEN_KINDS: frozenset[str] = frozenset({"random", "provenance"})

#: The kinds the consolidated browser offers as checkboxes / searches:
#: every browsable kind minus the always-empty ones.
_CONSOLIDATED_KINDS: tuple[str, ...] = tuple(
    k for k in _REFS_BROWSABLE_KINDS if k not in _CONSOLIDATED_HIDDEN_KINDS
)


# ---- References extraction (MVP for #188) ---------------------------
#
# Scan a body for the same kind:ref shapes the linkifier picks up
# (prefixed ``kind:slug``, bare paper cite_keys, bare discord conv
# handles). Resolve each in a single batched query and shape an
# expansion for inline rendering below the body.


def _extract_handles(body: str) -> list[tuple[str, str, str | None]]:
    """Every kind:ref handle in ``body`` as ``(kind, id, chunk)`` triples.

    Thin wrapper over the shared ``mentions.extract_handles`` — the
    grammar + dedup live there so the read-time References panel and the
    write-time autolinker can't drift apart.
    """
    return mentions.extract_handles(body)


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
    # Numeric-id-or-slug resolution is single-sourced in the shared
    # mentions module (same two-step the write-time autolinker uses).
    ref = mentions.resolve_handle_ref(store, ref_id, include_deleted=True)
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
            "title": (getattr(ref, "title", "") or "(untitled)").split("\n", 1)[0][
                :120
            ],
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
    has_chunks = False
    if not preview:
        # Fall back to the first block (or the title-derived hint).
        try:
            blocks = store.list_blocks_for_ref(ref.id)
            if blocks:
                has_chunks = True
                preview = (blocks[0].text or "")[:400].rstrip()
                if len(blocks[0].text or "") > 400:
                    preview += "…"
        except Exception:
            pass
    else:
        # We hit the chunk-addressed path above which means chunks exist.
        has_chunks = True
    # Status taxonomy for verification badges (#191):
    #   resolved → ref exists and has chunks (the typical successful case)
    #   stub     → ref exists, no chunks yet (paper awaiting fetcher)
    #   missing  → ref id doesn't resolve
    #   deleted  → ref exists but soft-deleted
    status = "resolved" if has_chunks else "stub"
    # Citation metadata for BibTeX / Markdown export — only meaningful
    # for paper kind, but the dict shape is uniform so the template
    # doesn't have to branch.
    citation: dict[str, Any] = {}
    if kind == "paper":
        slug = getattr(ref, "slug", None) or ""
        year = getattr(ref, "year", None)
        # Citation-form names, tolerant of every stored author shape
        # (``{name}`` from ingest, ``{family, given}`` from the editor).
        author_list = author_names(getattr(ref, "authors", None), order="sortable")
        # Try to pull DOI off ref.meta if the handler stored it there
        # (papers ingested from Crossref do).
        meta = getattr(ref, "meta", None) or {}
        doi = meta.get("doi") if isinstance(meta, dict) else None
        citation = {
            "cite_key": slug,
            "authors": author_list,
            "year": year,
            "doi": doi,
            "url": (f"https://doi.org/{doi}" if doi else None),
        }

    return {
        "handle": raw_handle,
        "url": url,
        "title": title,
        "preview": preview,
        "status": status,
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
        selected: list[str] = list(_CONSOLIDATED_KINDS)
    elif kinds:
        # Tolerate trailing commas / whitespace / unknown kinds.
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        selected = [k for k in _CONSOLIDATED_KINDS if k in requested]
        # Preserve the operator's ordering for kinds we didn't recognise
        # so a future-added kind shows up when its checkbox is added.
        for k in requested:
            if k not in selected and k not in _CONSOLIDATED_KINDS:
                selected.append(k)
    else:
        selected = list(_DEFAULT_REFS_KINDS)

    store = get_store(request)
    query = (q or "").strip()
    by_kind: dict[str, list[dict[str, object]]] = {}
    #: Handler-searched kinds (skill / tag) have no ``refs`` rows, so
    #: they contribute a rendered-markdown block from their own ``search``
    #: verb rather than a row grid.
    by_kind_md: dict[str, str] = {}
    for kind in selected:
        if kind in _HANDLER_SEARCHED_KINDS:
            # No refs rows to list — dispatch the kind's own search verb
            # (skill files / tag vocabulary) and render its markdown.
            # An empty query lists where the verb supports it (the skill
            # index); a verb that requires q= on empty input just yields
            # an error we skip, so the section drops out cleanly.
            args: dict[str, Any] = {"kind": kind, "page_size": _PER_KIND_LIMIT}
            if query:
                args["q"] = query
            try:
                body, is_error = await await_dispatch(request, "search", args)
            except Exception:
                continue
            if is_error or not (body or "").strip():
                continue
            by_kind_md[kind] = body
            continue
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
            "all_browsable": list(_CONSOLIDATED_KINDS),
            "default_kinds": list(_DEFAULT_REFS_KINDS),
            "by_kind": by_kind,
            "by_kind_md": by_kind_md,
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
        # Follow-up threads stamp the source handle in ref.meta so the
        # transcript can offer a "continue this discussion" box that
        # routes the next question back to the same source.
        conv_meta = ref.meta or {}
        followup_source = conv_meta.get("followup_source")
        return templates.TemplateResponse(
            request,
            "refs/conv_detail.html.j2",
            {
                "active_tab": f"refs:{kind}",
                "kind": kind,
                "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
                "ref": _row(ref),
                "turns": _conv_turns(store, ref.id),
                "followup_source": followup_source,
                "followup_source_url": (
                    _source_detail_url(
                        str(conv_meta.get("followup_kind") or ""),
                        conv_meta.get("followup_ref_id"),
                    )
                    if followup_source
                    else None
                ),
            },
        )

    # Slug kinds (oracle/patent/pres) address get() by slug; numeric
    # kinds (memory/gripe) by id. Prefer the slug when present.
    addr: str | int = ref.slug if ref.slug else ref.id
    get_args: dict[str, Any] = {"kind": kind, "id": addr}
    # This detail page is a read-only view. For cache-backed kinds a plain
    # get() re-fetches on a cache miss — and addressing by slug reliably
    # misses for query-keyed kinds (perplexity/websearch), so a page view
    # would re-run the paid upstream call. no_fetch=True serves the stored
    # body and never spends.
    if kind in _CACHE_BACKED_KINDS:
        get_args["no_fetch"] = True
    body, is_error = await await_dispatch(request, "get", get_args)

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
    footnotes: dict[tuple[str, str, str | None], int] | None = None
    if kind == "memory" and not is_error and body:
        handles = _extract_handles(body)
        handle_to_num: dict[tuple[str, str, str | None], int] = {}
        for n, (ref_kind, ref_ident, chunk) in enumerate(handles, 1):
            handle_to_num[(ref_kind, ref_ident, chunk)] = n
            row = _expand_handle(store, ref_kind, ref_ident, chunk)
            row["number"] = n
            references.append(row)
        # Hand the numbering to ``linkify_refs`` (via the template) so the
        # inline ``[N]`` footnote markers are emitted *inside* its escaping
        # pass — appended right after each handle's hover anchor. The body
        # stays plain text; we no longer splice raw ``<a>`` HTML into it
        # (which autoescape would now neutralise into visible markup).
        footnotes = handle_to_num

    # Job detail gets an actions strip instead of the dream-memory
    # "Ask & think" box: a failed/cancelled job is a thing you *unstick*
    # (retry → re-mint) or *read* (transcript / parent intent), not a
    # thought you interrogate. The generic Discussion affordance ran a
    # slow agentic pass whose answer landed in a side conv thread — no
    # help for "what went wrong / how do I fix it". See
    # ``templates/refs/detail.html.j2``.
    job_actions: dict[str, Any] | None = None
    if kind == "job":
        job_actions = _job_actions(store, ref, raw_tags)

    # YouTube detail pages get a header card with a clickable watch link
    # and the video thumbnail (a "screenshot") above the transcript.
    youtube_meta = _youtube_meta(store, ref) if kind == "youtube" else None

    return templates.TemplateResponse(
        request,
        "refs/detail.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
            "ref": _row(ref),
            "body": body,
            "footnotes": footnotes,
            "is_error": is_error,
            "chunks": chunks,
            "tags": tags,
            "body_disabled_notice": body_disabled_notice,
            "references": references,
            "job_actions": job_actions,
            "youtube_meta": youtube_meta,
            # The generic "Ask & think" Discussion box is a dream-memory
            # affordance; a job wants the actions strip, not an agentic
            # side-thread. Suppress it for jobs.
            "discussions": (
                None if kind == "job" else _followup_discussions(store, ref.id)
            ),
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
    return await redirect_or_error(
        request, "tag", args, redirect=redirect_url, error_title="Tag error"
    )


# ---- Ask a follow-up question about a thought -----------------------
#
# A textbox + button on each detail page. The question is captured as
# a turn in a ``conv`` thread (one per source[, chunk]); an agentic
# ``claude -p`` pass (the dreaming dispatch — SOUL prompt + MCP precis
# tools) answers, and the answer is appended as the next turn. The conv
# is linked ``derived-from`` the source so the discussion is reachable
# from the thought. All DB writes go through the put / link verbs.


def _source_detail_url(kind: str, ref_id: Any) -> str:
    """Best detail URL for a source ref (papers have their own viewer)."""
    if ref_id is None:
        return "/refs"
    if kind == "paper":
        return f"/papers/{ref_id}"
    return f"/refs/{kind}/{ref_id}"


async def _run_followup(
    request: Request,
    *,
    source_kind: str,
    source_ref_id: int,
    chunk_pos: int | None,
    question: str,
) -> Response:
    """Capture a question, think about it, append the answer to a conv.

    Shared by the source-page ``/ask`` route and the conv-page
    ``/continue`` route — both resolve to the same conv slug, so a
    discussion accumulates turns in one thread.
    """
    question = (question or "").strip()
    store = get_store(request)
    refs = store.fetch_refs_by_ids([source_ref_id], include_deleted=False)
    source = refs.get(source_ref_id)
    if source is None or source.kind != source_kind:
        raise NotFound(f"{source_kind} id={source_ref_id} not found")

    back_url = _source_detail_url(source_kind, source_ref_id)
    if not question:
        return RedirectResponse(url=back_url, status_code=303)

    slug = ask.followup_slug(source_kind, source_ref_id, chunk_pos)
    handle = ask.source_handle(source_kind, source_ref_id, chunk_pos)
    source_title = (source.title or "").split("\n", 1)[0][:120] or handle
    asker = get_web_config(request).owner

    def _err(title: str, detail: str) -> Response:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": title, "detail": detail, "status": 400},
            status_code=400,
        )

    # 1. Append the human question (mints the thread on first ask,
    #    stamping the source handle in ref_meta for the continue box).
    _, is_error = await await_dispatch(
        request,
        "put",
        {
            "kind": "conv",
            "id": slug,
            "text": question,
            "author": asker,
            "title": f"Follow-up · {source_title}",
            "ref_meta": {
                "followup_source": handle,
                "followup_kind": source_kind,
                "followup_ref_id": source_ref_id,
                "followup_chunk": chunk_pos,
            },
        },
    )
    if is_error:
        return _err("Follow-up error", "could not record the question")

    conv = store.get_ref(kind="conv", id=slug)
    if conv is None:
        return _err("Follow-up error", "conv thread missing after put")
    conv_url = f"/refs/conv/{conv.id}"

    # 2. Link the discussion back to its source (idempotent — re-running
    #    is a no-op via the links unique tuple). Chunk-scoped via ~N.
    await await_dispatch(
        request,
        "link",
        {"kind": "conv", "id": slug, "target": handle, "rel": "derived-from"},
    )

    # 3. Build the prompt: source body + chunk-in-focus + the discussion
    #    so far (every turn except the question we just appended).
    addr: str | int = source.slug if source.slug else source.id
    src_body, src_err = await await_dispatch(
        request, "get", {"kind": source_kind, "id": addr}
    )
    if src_err:
        src_body = source.title or ""
    focus_text: str | None = None
    if chunk_pos is not None:
        for b in store.list_blocks_for_ref(
            source_ref_id, pos_range=(chunk_pos, chunk_pos)
        ):
            focus_text = b.text or ""
            break
    all_turns = store.list_blocks_for_ref(conv.id)
    prior = [
        ((b.meta or {}).get("author") or "?", b.text or "") for b in all_turns[:-1]
    ]
    prompt = ask.build_prompt(
        source_kind=source_kind,
        source_handle_str=handle,
        source_title=source_title,
        source_body=src_body,
        focus_text=focus_text,
        prior_turns=prior,
        question=question,
    )

    # 4. Think. The subprocess can take tens of seconds — run it off the
    #    event loop so concurrent tabs / healthz stay responsive.
    answer_author = ask.ANSWERER
    answer_meta: dict[str, Any] = {}
    try:
        result = await asyncio.to_thread(
            ask.generate_answer, prompt, store=store, conv_ref_id=conv.id
        )
        answer = (result.final_text or "").strip() or "(the model returned no text)"
        answer_meta = {
            k: v
            for k, v in {
                "model": getattr(result, "model", None),
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration_s, 1),
                "turns": result.turns_used,
            }.items()
            if v is not None
        }
    except ClaudeAgentError as exc:
        answer = f"⚠️ thinking failed: {exc}"
        answer_author = "system"

    # 5. Append the answer as the next turn, then land on the transcript.
    await await_dispatch(
        request,
        "put",
        {
            "kind": "conv",
            "id": slug,
            "text": answer,
            "author": answer_author,
            "meta": answer_meta,
        },
    )
    return RedirectResponse(url=conv_url, status_code=303)


@router.post("/{kind}/{ref_id}/ask")
async def ask_followup(
    request: Request,
    kind: str,
    ref_id: int,
    question: str = Form(""),
    chunk: str = Form(""),
) -> Response:
    """Ask a follow-up about a ref (or a specific chunk via ``chunk=N``)."""
    _require_kind(kind)
    chunk_pos: int | None = None
    if chunk.strip():
        try:
            chunk_pos = int(chunk.strip())
        except ValueError:
            chunk_pos = None
    return await _run_followup(
        request,
        source_kind=kind,
        source_ref_id=ref_id,
        chunk_pos=chunk_pos,
        question=question,
    )


@router.post("/conv/{conv_ref_id}/continue")
async def continue_followup(
    request: Request,
    conv_ref_id: int,
    question: str = Form(""),
) -> Response:
    """Continue a follow-up thread — resolve its source from ref.meta."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([conv_ref_id], include_deleted=False)
    conv = refs.get(conv_ref_id)
    if conv is None or conv.kind != "conv":
        raise NotFound(f"conv id={conv_ref_id} not found")
    meta = conv.meta or {}
    source_kind = meta.get("followup_kind")
    source_ref_id = meta.get("followup_ref_id")
    if not source_kind or source_ref_id is None:
        raise NotFound(
            f"conv id={conv_ref_id} is not a follow-up thread (no source in meta)"
        )
    chunk_raw = meta.get("followup_chunk")
    chunk_pos = int(chunk_raw) if isinstance(chunk_raw, int) else None
    return await _run_followup(
        request,
        source_kind=str(source_kind),
        source_ref_id=int(source_ref_id),
        chunk_pos=chunk_pos,
        question=question,
    )
