"""Drafts tab — a read-first viewer/editor for the ``draft`` kind (ADR 0033).

Tier-A surface (the document is *steered*, not hand-typed). The reader is
a **per-block row grid**: one row per chunk in DFS reading order, each row
three columns —

  ┌ content (raw source via linkify_refs + KaTeX, hierarchy-indented,
  │          headings collapse their subtree)
  ├ meta    (terse: the refs this block makes + in-flight change-requests)
  └ change  (a per-block "around here…" box → an anchored todo)

**On-demand loading (true virtual scrolling).** A massive draft (10k+
blocks) keeps only the on-screen *window* of rows in the DOM — not a node
per block. The reader embeds a compact **skeleton** (one tiny record per
block: handle, kind, depth, ancestors, heading title, height estimate) and
renders the first ``INITIAL_WINDOW`` rows server-side; everything else is a
pair of sized spacer ``<div>``s (``#dr-top`` / ``#dr-bot``) so the scrollbar
is right. Client JS (``draftDoc`` in ``detail.html.j2``) reconciles
``#dr-win`` to the blocks intersecting the viewport (± a margin) on scroll,
fetching them in one ``/rows?handles=…`` batch and dropping rows that scroll
away. So a 9,700-block draft costs ~a screenful of nodes, not 9,700 — which
is what fixed the "works but with a minute lag" (the browser was
styling/laying-out/Alpine-walking every block). Collapse recomputes the
visible set + spacers (no per-node binding). The whole-draft inputs (reading
order, version, abbrevs) are memoised per ``(ref, version)``. A draft with ≤
``INITIAL_WINDOW`` blocks renders entirely server-side. Find, collapse, and
deep-links scroll the target block into the window before acting.

Routes:

* ``GET /drafts`` — list drafts.
* ``GET /drafts/{ident}`` — the reader (slug or numeric id); embeds the
  skeleton + the server-rendered first window.
* ``GET /draft/{ident}`` — singular convenience alias → 303 to the reader.
* ``POST /drafts/{ident}/request`` — file a change request (anchored todo
  parented on the draft's project; flows into the todo tree → dispatch).
* ``POST /drafts/{ident}/delete`` — soft-delete the whole draft, gated on
  typing its name (atomic: ref ``deleted_at`` + chunks retired; recoverable).
* ``GET /c/{handle}`` — resolve a chunk handle → redirect to where it
  lives: a draft chunk (``dc``/``¶``) into the reader at ``#c-<handle>``, a
  paper/other chunk (``pc``/``mc``/…) through the ``/r`` resolver at that
  chunk. The click target of every ``¶``/``§`` anchor.
* ``GET /preview/chunk/{handle}`` — hover-popover quote for any chunk
  handle (draft or paper/other), so a ``§`` paper-chunk citation hovers.
* ``GET /drafts/{ident}/row/{handle}`` — one hydrated row.
* ``GET /drafts/{ident}/rows`` — ``?handles=a,b,…`` batch-renders those
  blocks (the scroller's window fetch); no param → the whole document.
* ``GET /drafts/{ident}/skeleton`` — the skeleton + version token as JSON;
  the live poll refetches it to re-window after an edit.
* ``GET /drafts/{ident}/version`` — a monotone version token (max
  ``chunk_events.event_id``) the poll compares against.

Rendering is **raw source** (Tier A); the resolution pass that computes
§-numbers / resolves cross-refs is the export engine (Tier B), shared
across HTML/LaTeX/Word targets. KaTeX renders ``$…$`` client-side.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from precis.utils import draft_markup, handle_registry, mentions
from precis.utils.embed_query import embed_query
from precis.utils.figure_clearance import draft_figure_clearance, figure_status
from precis_web.deps import (
    await_dispatch,
    get_runtime,
    get_store,
    redirect_or_error,
    templates,
)
from precis_web.linkify import popover_chip

router = APIRouter(tags=["drafts"])

log = logging.getLogger(__name__)

#: How many blocks the reader renders fully on first paint. The rest land
#: as lightweight placeholders and hydrate on demand (loaded as they
#: scroll near the viewport, unloaded when they drift far away) — so a
#: massive draft no longer renders thousands of enriched rows up front nor
#: holds them all in the DOM. A draft with ≤ this many blocks renders
#: entirely server-side and behaves exactly as before; windowing only
#: kicks in past it.
INITIAL_WINDOW = 30

#: Bounded ``(ref_id, version) → abbrevs`` cache. ``defined_abbrevs`` is a
#: whole-draft ``string_agg`` + Schwartz-Hearst scan; on the on-demand row
#: path it would otherwise re-run for every block hydrated. Keyed by the
#: draft's version token, so any chunk edit invalidates it. Tiny LRU.
_ABBREV_CACHE: OrderedDict[tuple[int, int], dict[str, str]] = OrderedDict()
_ABBREV_CACHE_MAX = 64


def _abbrevs_cached(store: Any, ref_id: int, version: int) -> dict[str, str]:
    """Whole-draft abbreviation map, memoised per (draft, version) so the
    per-row hydrate path doesn't re-scan the whole draft each time."""
    key = (ref_id, version)
    hit = _ABBREV_CACHE.get(key)
    if hit is not None:
        _ABBREV_CACHE.move_to_end(key)
        return hit
    val = store.defined_abbrevs(ref_id)
    _ABBREV_CACHE[key] = val
    _ABBREV_CACHE.move_to_end(key)
    while len(_ABBREV_CACHE) > _ABBREV_CACHE_MAX:
        _ABBREV_CACHE.popitem(last=False)
    return val


#: Bounded ``(ref_id, version) → reading_order`` cache. ``reading_order``
#: is a recursive CTE over the whole draft; without this, every on-demand
#: row hydrate (one HTTP request per block) would re-run it — O(N) per
#: block → O(N²) over a scroll of a 10k-chunk draft. The cached list is
#: immutable (frozen ``DraftChunk`` dataclasses), so it's safe to share.
#: Keyed by the version token, so any chunk create/edit/move invalidates.
_RO_CACHE: OrderedDict[tuple[int, int], list[Any]] = OrderedDict()
_RO_CACHE_MAX = 16


def _reading_order_cached(store: Any, ref_id: int, version: int) -> list[Any]:
    key = (ref_id, version)
    hit = _RO_CACHE.get(key)
    if hit is not None:
        _RO_CACHE.move_to_end(key)
        return hit
    val = store.reading_order(ref_id)
    _RO_CACHE[key] = val
    _RO_CACHE.move_to_end(key)
    while len(_RO_CACHE) > _RO_CACHE_MAX:
        _RO_CACHE.popitem(last=False)
    return val


def _doc_state(store: Any, ref: Any) -> tuple[list[Any], int, dict[str, str]]:
    """The whole-draft inputs every render path shares — reading order,
    version token, abbreviations — each memoised per (ref, version) so a
    big draft pays for them once, not once per hydrated block."""
    version = _draft_version(store, ref.id)
    chunk_objs = _reading_order_cached(store, ref.id, version)
    abbrevs = _abbrevs_cached(store, ref.id, version)
    return chunk_objs, version, abbrevs


def _draft_ref(store: Any, ident: str) -> Any:
    """Resolve a draft by slug or numeric ref_id (``get_ref`` handles
    both). Returns the live ``Ref`` or ``None``."""
    key: int | str = int(ident) if ident.lstrip("#").isdigit() else ident
    if isinstance(key, str) and key.startswith("#"):
        key = int(key[1:])
    return store.get_ref(kind="draft", id=key)


def _project_id(store: Any, ref_id: int) -> int | None:
    """The draft's owning *live* project todo (the ``draft-of`` target).

    Skips a soft-deleted target: ``links_for`` doesn't filter on the
    destination's ``deleted_at``, so a draft whose project todo was
    deleted would otherwise hand back a dead ``parent_id`` and ``put``
    rejects it (NotFound). Returning ``None`` here makes the anchored
    todo a root instead of erroring."""
    for link in store.links_for(ref_id, direction="out", relation="draft-of"):
        dst = int(link.dst_ref_id)
        if store.get_ref(kind="todo", id=dst) is not None:
            return dst
    return None


def _ancestor_headings(chunk_objs: list[Any]) -> dict[str, list[str]]:
    """Each chunk's ancestor *heading* handles (root→nearest), walking
    ``parent_chunk_id``. Drives client-side collapse: a row hides when any
    of its ancestor headings is collapsed; a heading owns exactly the
    chunks that carry it in this list."""
    by_id = {c.chunk_id: c for c in chunk_objs}
    out: dict[str, list[str]] = {}
    for c in chunk_objs:
        anc: list[str] = []
        pid = c.parent_chunk_id
        while pid is not None and pid in by_id:
            p = by_id[pid]
            if p.chunk_kind == "heading":
                anc.append(p.handle)
            pid = p.parent_chunk_id
        out[c.handle] = list(reversed(anc))
    return out


def _ref_chips(text: str) -> list[Any]:
    """The references a block makes, as terse hover-preview chips — the
    superset grammar (bracket/sigil forms ∪ bare ``kind:ref``), deduped
    by their navigate target so ``§kong24~2`` and ``paper:kong24~2`` (the
    same chunk) collapse to one chip. Each chip carries the cited quote
    on hover (``popover_chip``). Reuses the shared parser/grammar (DRY)."""
    seen: set[str] = set()
    chips: list[Any] = []

    def add(label: str, href: str, preview: str | None) -> None:
        if href in seen:
            return
        seen.add(href)
        chips.append(popover_chip(label, href, preview))

    def paper(slug: str, chunk: str | None, label: str) -> None:
        # chunk here is the regex group incl. leading ``~`` (or None).
        suffix = f"?chunk={chunk[1:]}" if chunk else ""
        add(label, f"/r/paper/{slug}{suffix}", f"/preview/paper/{slug}{suffix}")

    for ref in draft_markup.parse_references(text):
        if ref.cls == draft_markup.XREF:
            h = ref.target.lstrip("¶")
            add(ref.surface or ref.target, f"/c/{h}", f"/preview/chunk/{h}")
        elif ref.cls == draft_markup.CITE:
            m = mentions.DRAFT_CITE_PATTERN.fullmatch(ref.target)
            if m:
                paper(m.group("slug"), m.group("chunk"), ref.surface or ref.target)
        elif ref.cls == draft_markup.WEB:
            add(ref.surface or ref.target, ref.target, None)
        else:  # AUTHORING — a bare universal handle [me6184] or [[kind:id]]
            parsed = handle_registry.parse(ref.target)
            if parsed is not None:  # a universal handle → chunk or record
                kind, is_chunk, pk = parsed
                if is_chunk:
                    h = handle_registry.normalize(ref.target)
                    add(ref.surface or ref.target, f"/c/{h}", f"/preview/chunk/{h}")
                else:
                    add(
                        ref.surface or ref.target,
                        f"/r/{kind}/{pk}",
                        f"/preview/{kind}/{pk}",
                    )
                continue
            m = mentions.REF_PATTERN.fullmatch(ref.target)
            if m and m.group("kind") in mentions.LINKIFY_KINDS:
                k, i = m.group("kind"), m.group("id").lstrip("#")
                add(ref.surface or ref.target, f"/r/{k}/{i}", f"/preview/{k}/{i}")
    for kind, ident, chunk in mentions.extract_handles(text):
        i = ident.lstrip("#")
        if kind == "paper":  # collapse with the § form (same target)
            paper(i, chunk, f"{kind}:{ident}{chunk or ''}")
            continue
        suffix = f"?chunk={chunk[1:]}" if chunk else ""
        add(
            f"{kind}:{ident}{chunk or ''}",
            f"/r/{kind}/{i}{suffix}",
            f"/preview/{kind}/{i}{suffix}",
        )
    return chips


#: Request lifecycle ordering for the per-block list: active first, then
#: done/abandoned (which now *persist* so you can click in and debug the
#: LLM run, rather than vanishing on completion).
_REQUEST_ORDER = {"open": 0, "scheduled": 1, "doing": 2, "paused": 3}


def _requests_by_handle(
    store: Any, handles: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """ALL change-request todos anchored at each chunk (``meta.anchor =
    '¶<handle>'``), grouped by handle — including **done / won't-do**, so a
    finished request hangs around to click into (its ``plan_tick`` job's
    captured LLM transcript is the debugging surface). Active requests
    sort first. ``started`` (a job minted) + ``done`` + ``failed`` drive
    the close-X: it shows on not-yet-started, done, or failed requests,
    and is suppressed only while a request is actively running."""
    if not handles:
        return {}
    # Match both the new bare ``dc<id>`` anchors and any legacy ``¶<handle>``
    # ones still stored (transition); the group key below normalises to bare.
    anchors = list(handles) + [f"¶{h}" for h in handles]
    sql = (
        "SELECT r.ref_id, r.title, r.meta->>'anchor' AS anchor, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status, "
        "  EXISTS (SELECT 1 FROM refs j WHERE j.parent_id = r.ref_id "
        "          AND j.kind = 'job') AS started, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
        "      AND t.value LIKE 'ask-user:%%' LIMIT 1) AS asking, "
        "  EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
        "      AND t.value LIKE 'child-failed:%%') AS failed "
        "FROM refs r "
        "WHERE r.kind = 'todo' AND r.deleted_at IS NULL "
        "  AND r.meta->>'anchor' = ANY(%s)"
    )
    out: dict[str, list[dict[str, Any]]] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (anchors,)).fetchall()
    for ref_id, title, anchor, status, started, asking, failed in rows:
        status = status or "open"
        handle = (anchor or "").lstrip("¶")
        # ``OPEN:ask-user:<slug>`` → a human-ish question ("see-chunk-0" →
        # "see chunk 0"). The slug is terse — the full reasoning is in the
        # job transcript the chip links to.
        ask = (asking or "").split("ask-user:", 1)[-1].replace("-", " ").strip()
        out.setdefault(handle, []).append(
            {
                "ref_id": ref_id,
                "title": (title or "").split("\n", 1)[0][:60],
                "status": status,
                "done": status in ("done", "won't-do"),
                # "started" = a plan_tick (or other) job minted; the
                # X-to-cancel only shows before that.
                "started": bool(started),
                # attention: waiting on the user, or a failed child job.
                "asking": ask,
                "failed": bool(failed),
            }
        )
    for reqs in out.values():
        reqs.sort(key=lambda r: _REQUEST_ORDER.get(r["status"], 9))
    return out


def _block_views(
    store: Any, ref_id: int, handles: list[str] | None = None
) -> dict[str, dict[str, str]]:
    """Per-block keyword + llm-summary text for the view slider (body /
    summary / keywords). Thin wrapper over ``store.block_views`` (shared
    with the handler's outline render); empty for a chunk the
    chunk_keywords / llm_summarize workers haven't reached yet (→
    first-line fallback in the row). ``handles`` scopes it to a subset for
    the on-demand single-row path."""
    return store.block_views(ref_id, handles)


def _connection_chips(conns: list[dict[str, Any]]) -> list[Any]:
    """Render chunk-connection rows (linked refs + dreams) as terse
    hover-preview chips: ``kind:ident — title``, click → the ref."""
    chips: list[Any] = []
    for c in conns:
        kind, ident = c["kind"], c["ident"]
        label = f"{kind}:{ident}"
        if c.get("title"):
            label += f" — {c['title']}"
        chips.append(
            popover_chip(label, f"/r/{kind}/{ident}", f"/preview/{kind}/{ident}")
        )
    return chips


def _build_rows(
    store: Any,
    ref: Any,
    chunk_objs: list[Any],
    want_idx: Any,
    *,
    abbrevs: dict[str, str],
) -> list[dict[str, Any]]:
    """Full per-block row context for the chunks at ``want_idx`` (an
    iterable of indices into ``chunk_objs``). The expensive per-handle
    lookups (requests / views / connections / edit churn) are scoped to the
    wanted blocks plus their immediate neighbours — the neighbour-fold needs
    prev/next connections — so a single-row hydrate doesn't re-scan the
    whole draft. ``_rows_for`` builds the whole document; the on-demand row
    route builds one index. ``abbrevs`` (whole-draft) is passed in so it is
    computed/cached once, not per call."""
    want = sorted(set(want_idx))
    if not want:
        return []
    n = len(chunk_objs)
    anc = _ancestor_headings(chunk_objs)
    want_handles = [chunk_objs[i].handle for i in want]
    # Connections need the wanted blocks AND their prev/next neighbours
    # (the "nearby" fold); the rest only need their own handle.
    scope: set[str] = set(want_handles)
    for i in want:
        for j in (i - 1, i + 1):
            if 0 <= j < n:
                scope.add(chunk_objs[j].handle)
    requests = _requests_by_handle(store, want_handles)
    views = _block_views(store, ref.id, want_handles)
    conns = store.chunk_connections(ref.id, list(scope))
    edits = store.chunk_edit_stats(ref.id, want_handles)
    # The per-heading "style ▾" picker — same list for every heading in this
    # draft, so compute once (ADR 0037; scoped to the genre).
    section_styles = _section_styles_for(store, ref)
    rows: list[dict[str, Any]] = []
    for i in want:
        c = chunk_objs[i]
        # Neighbour folding: prev/next paragraph connections, deduped
        # against this block's own (so "nearby" only shows what's *extra*).
        own = {(x["kind"], x["ident"]) for x in conns.get(c.handle, [])}
        nearby: list[dict[str, Any]] = []
        nseen = set(own)
        for j in (i - 1, i + 1):
            if 0 <= j < n:
                for x in conns.get(chunk_objs[j].handle, []):
                    k = (x["kind"], x["ident"])
                    if k not in nseen:
                        nseen.add(k)
                        nearby.append(x)
        est = edits.get(c.handle, {})
        v = views.get(c.handle, {})
        first_line = ((c.text or "").splitlines() or [""])[0][:140]
        is_figure = c.chunk_kind == "figure"
        fig = (getattr(c, "meta", None) or {}).get("figure", {}) if is_figure else {}
        rows.append(
            {
                "handle": c.handle,
                # ADR 0036 universal handle (``dc<chunk_id>``) — the agent- and
                # human-facing address. ``handle`` (base-58) stays the internal
                # DOM/nav key the JS collapse/find machinery already threads.
                "dc": handle_registry.try_format("draft", c.chunk_id, chunk=True)
                or c.handle,
                "chunk_kind": c.chunk_kind,
                "text": c.text,
                "depth": c.depth,
                "is_heading": c.chunk_kind == "heading",
                # section style (ADR 0037): the current meta.style + the
                # genre's pickable styles, for the per-heading "style ▾" menu.
                "style": (getattr(c, "meta", None) or {}).get("style")
                if c.chunk_kind == "heading"
                else None,
                "section_styles": section_styles if c.chunk_kind == "heading" else None,
                "is_figure": is_figure,
                # figure provenance for the origin chip + clearance badge
                "figure_origin": fig.get("origin") if is_figure else None,
                "figure_cleared": _figure_cleared(fig) if is_figure else None,
                "figure_permission": fig.get("permission") if is_figure else None,
                "blob_url": f"/drafts/blob/{c.handle}" if is_figure else None,
                "ancestors": anc.get(c.handle, []),
                "abbrevs": abbrevs,
                "refs": _ref_chips(c.text),
                "requests": requests.get(c.handle, []),
                # view slider: summary falls back to keywords → first line;
                # keywords falls back to first line.
                "summary": v.get("summary") or v.get("keywords") or first_line,
                "keywords": v.get("keywords") or first_line,
                # Connections surface: graph links + folded neighbours + churn.
                "connections": _connection_chips(conns.get(c.handle, [])),
                "nearby": _connection_chips(nearby),
                "edits": est.get("edits", 0),
                "edited_at": est.get("last_at"),
            }
        )
    return rows


def _rows_for(store: Any, ref: Any) -> list[dict[str, Any]]:
    """Per-block row context for the **whole** draft (the no-arg ``/rows``
    fragment). The reader itself renders only an initial window fully (see
    ``reader``) and fetches the rest in windowed batches as you scroll."""
    chunk_objs, _version, abbrevs = _doc_state(store, ref)
    return _build_rows(store, ref, chunk_objs, range(len(chunk_objs)), abbrevs=abbrevs)


def _rows_for_handles(store: Any, ref: Any, handles: list[str]) -> list[dict[str, Any]]:
    """Hydrate a *batch* of blocks by handle, in document order — the
    on-demand fragment the reader swaps in for a window of placeholders.
    One request hydrates many blocks (vs one HTTP per block), and the
    shared cached reading-order means no per-block whole-draft re-scan."""
    chunk_objs, _version, abbrevs = _doc_state(store, ref)
    want = {h for h in handles}
    idx = [i for i, c in enumerate(chunk_objs) if c.handle in want]
    return _build_rows(store, ref, chunk_objs, idx, abbrevs=abbrevs)


def _one_row(store: Any, ref: Any, handle: str) -> dict[str, Any] | None:
    """Hydrate a single block by handle — O(neighbours) enrichment over the
    cached reading order, not a whole-draft re-scan per block."""
    rows = _rows_for_handles(store, ref, [handle])
    return rows[0] if rows else None


def _est_height_rem(c: Any) -> float:
    """A rough placeholder height (rem) so an un-rendered block reserves
    about the space its real row will take — keeps the scrollbar honest and
    avoids large jumps when a block renders just below the fold."""
    kind = c.chunk_kind
    if kind == "heading":
        return 2.5
    if kind == "figure":
        return 14.0
    chars = len(c.text or "")
    lines = max(1, (chars // 90) + 1)
    return round(min(40.0, 1.6 + lines * 1.5), 1)


def _est_height_px(c: Any) -> int:
    """Placeholder height in px (rem × 16) for the virtual-scroll spacers."""
    return round(_est_height_rem(c) * 16)


def _skeleton(store: Any, ref: Any) -> list[dict[str, Any]]:
    """The whole-draft **skeleton** the reader's virtual scroller runs on:
    one tiny record per block (handle, address, kind, depth, ancestors,
    heading flag/title, height estimate) — NOT a DOM node. The client keeps
    only the on-screen window of real rows in the DOM (fetched via
    ``/rows?handles=``) and a sized spacer for everything else, so a
    10k-block draft costs ~a screenful of nodes, not 10k. Cheap: derived
    from the cached reading order, no per-block enrichment."""
    chunk_objs, _version, _abbrevs = _doc_state(store, ref)
    anc = _ancestor_headings(chunk_objs)
    out: list[dict[str, Any]] = []
    for c in chunk_objs:
        is_h = c.chunk_kind == "heading"
        first = ((c.text or "").splitlines() or [""])[0]
        out.append(
            {
                "h": c.handle,
                "dc": handle_registry.try_format("draft", c.chunk_id, chunk=True)
                or c.handle,
                "kind": c.chunk_kind,
                "depth": c.depth,
                "anc": anc.get(c.handle, []),
                "heading": is_h,
                "title": first[:200] if is_h else "",
                "est": _est_height_px(c),
            }
        )
    return out


def _figure_cleared(fig: dict[str, Any]) -> bool:
    """Per-figure clearance for the reader badge — the shared ADR 0034 §4
    rule (third-party needs a granted, unexpired permission)."""
    return figure_status(fig)[0]


def _ref_view(ref: Any) -> dict[str, Any]:
    return {
        "ident": ref.slug or ref.id,
        "slug": ref.slug,
        "title": ref.title,
        "id": ref.id,
    }


def _work_items(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Stuck / in-flight work on this draft for the detail panel (Fix A):
    blocked-or-in-flight open todos walked draft→project→subtree. Mirrors
    the MCP outline's "Work in progress" block so a failed enrichment job
    is visible from the draft in the browser too."""
    try:
        items = store.draft_attached_work(ref_id)
    except Exception:  # pragma: no cover - defensive, never fail the page
        log.warning("drafts: attached-work walk failed for %s", ref_id, exc_info=True)
        return []
    return [
        {
            "todo_id": it.todo_id,
            "title": it.title,
            "blocked": it.blocked,
            "jobs": [{"id": jid, "status": st} for jid, st in it.jobs],
        }
        for it in items
    ]


#: Document types offered by the "+ New draft" form. Each maps to a
#: standing guidance line folded into the project brief (so the planner
#: writes in the right register — the brief is injected as the
#: ``## Project context`` block on every tick) and stashed structurally
#: as ``meta.workspace.doc_type`` for the future export documentclass
#: switch. ``brief`` is "" for the neutral default (adds no guidance).
_DOC_TYPES: list[dict[str, str]] = [
    {
        "value": "paper",
        "label": "Research paper",
        "brief": "This is a research paper: an abstract, motivated "
        "introduction, methods/results, and a discussion, with rigorous "
        "citations throughout.",
    },
    {
        "value": "patent",
        "label": "Patent application",
        "brief": "This is a patent application: write in patent register — "
        "a technical field and background, a summary, a detailed description "
        "of embodiments, and numbered claims. Be precise and broad in claim "
        "scope; avoid marketing language.",
    },
    {
        "value": "report",
        "label": "Technical report",
        "brief": "This is a technical report: an executive summary up front, "
        "clearly sectioned findings, and concrete recommendations.",
    },
    {
        "value": "review",
        "label": "Review / survey",
        "brief": "This is a review/survey article: synthesise and compare the "
        "literature, organise by theme, and map open problems rather than "
        "presenting new results.",
    },
    {
        "value": "article",
        "label": "General article",
        "brief": "",
    },
]
_DOC_TYPE_BRIEF: dict[str, str] = {d["value"]: d["brief"] for d in _DOC_TYPES}

#: Section styles offered in the per-heading "style ▾" dropdown, keyed by
#: ``doc_type``. Each ``(slug, label)`` sets ``meta.style`` on the heading
#: (ADR 0037; the slug is a section-style skill served by ``get(kind=
#: 'skill')``). The picker is scoped to the genre so the menu stays short;
#: the scaffold normally sets these, this is the manual override.
_SCI_SECTION = [
    ("sci-abstract", "Abstract"),
    ("sci-introduction", "Introduction"),
    ("sci-related-work", "Related work"),
    ("sci-methods", "Methods"),
    ("sci-results", "Results"),
    ("sci-discussion", "Discussion"),
    ("sci-conclusion", "Conclusion"),
]
_SECTION_STYLES: dict[str, list[tuple[str, str]]] = {
    "patent": [
        ("patent-description", "Description"),
        ("patent-claim", "Claim"),
        ("patent-image-part", "Drawings + parts"),
        ("patent-prior-art", "Prior art"),
        ("patent-abstract", "Abstract"),
    ],
    "paper": _SCI_SECTION,
    "report": _SCI_SECTION,
    "review": [
        ("sci-abstract", "Abstract"),
        ("sci-introduction", "Introduction"),
        ("sci-methods", "Scope & method"),
        ("sci-survey-section", "Synthesis section"),
        ("sci-discussion", "Discussion"),
        ("sci-conclusion", "Conclusion"),
    ],
}


#: The standard section skeleton laid down when a draft of this ``doc_type``
#: is created (ADR 0037 step 4): an ordered list of ``(heading, style)``.
#: The new-draft flow appends these as styled headings after the title, so
#: picking a genre yields a styled skeleton to fill (each section's style
#: skill then fires when editing under it). Empty/absent → no scaffold.
_SCAFFOLDS: dict[str, list[tuple[str, str]]] = {
    "patent": [
        ("Field of the Invention", "patent-description"),
        ("Background", "patent-description"),
        ("Summary", "patent-description"),
        ("Brief Description of the Drawings", "patent-image-part"),
        ("Detailed Description", "patent-description"),
        ("Claims", "patent-claim"),
        ("Abstract", "patent-abstract"),
        ("Prior Art / IDS Disclosures", "patent-prior-art"),
    ],
    "paper": [
        ("Abstract", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Related Work", "sci-related-work"),
        ("Methods", "sci-methods"),
        ("Results", "sci-results"),
        ("Discussion", "sci-discussion"),
        ("Conclusion", "sci-conclusion"),
    ],
    "report": [
        ("Executive Summary", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Findings", "sci-results"),
        ("Discussion", "sci-discussion"),
        ("Conclusion", "sci-conclusion"),
    ],
    "review": [
        ("Abstract", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Scope & Method", "sci-methods"),
        ("Themes", "sci-survey-section"),
        ("Open Problems", "sci-survey-section"),
        ("Conclusion", "sci-conclusion"),
    ],
}


def _doc_type(store: Any, ref: Any) -> str:
    """The draft's ``doc_type`` (genre), read from the owning project's
    ``meta.workspace`` (falling back to the draft's own meta). ``""`` when
    unset or unreadable (defensive, so a stub store just yields no
    section-style options rather than erroring)."""
    try:
        pid = _project_id(store, ref.id)
        rid = pid if pid is not None else ref.id
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        meta = (row[0] if row else None) or {}
        return ((meta.get("workspace") or {}).get("doc_type") or "") or ""
    except Exception:  # pragma: no cover - defensive (stub store / no pool)
        return ""


def _section_styles_for(store: Any, ref: Any) -> list[tuple[str, str]]:
    """The section styles to offer for this draft's genre (empty → no
    dropdown)."""
    return _SECTION_STYLES.get(_doc_type(store, ref), [])


@router.get("/drafts", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = get_store(request)
    # Most recently *opened* (in the reader) first — see store.touch_viewed,
    # stamped on the reader page-load below. Never-opened drafts fall to the
    # bottom, then by last write.
    refs = store.list_refs(kind="draft", order_by="viewed_desc", limit=200)
    drafts = [
        {
            "ident": r.slug or r.id,
            "title": (r.title or r.slug or "untitled").split("\n", 1)[0],
            "slug": r.slug,
        }
        for r in refs
    ]
    doctypes = [
        {"value": d["value"], "label": d["label"], "default": d["value"] == "paper"}
        for d in _DOC_TYPES
    ]
    return templates.TemplateResponse(
        request,
        "drafts/index.html.j2",
        {"active_tab": "drafts", "drafts": drafts, "doctypes": doctypes},
    )


def _slugify(title: str) -> str:
    """A short kebab slug from a title (the draft's address)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "draft"


def _parse_id(body: str) -> int | None:
    m = re.search(r"id=(\d+)", body or "")
    return int(m.group(1)) if m else None


@router.post("/drafts/new")
async def new_draft(
    request: Request,
    title: str = Form(...),
    slug: str = Form(""),
    summary: str = Form(""),
    doctype: str = Form("paper"),
) -> Response:
    """Create a draft from the /drafts page. A draft is 1:1 with a
    project, so this mints the owning strategic ``todo`` (carrying the
    workspace + optional brief), then the draft under it, and lands on the
    new draft's reader. ``slug`` is derived from the title when blank.

    ``doctype`` (paper / patent / report / …) sets the document's style:
    it is stored as ``meta.workspace.doc_type`` and its standing guidance
    line becomes the project brief (the planner's ``## Project context``),
    so the planner writes in the right register from the first tick.

    ``summary`` is the user's description of *what to write* — it becomes
    the project todo's body (the ``## Body`` of every planner tick), i.e.
    the planner's **initial prompt**, not just standing context. The
    ``LLM:opus`` tag is the dispatcher's auto-run signal, so the planner
    starts on the description as soon as the next ``dispatch`` pass runs."""
    title = title.strip()
    if not title:
        return RedirectResponse(url="/drafts", status_code=303)
    slug = _slugify(slug.strip() or title)
    workspace: dict[str, Any] = {"path": f"projects/{slug}", "format": "tex"}
    doctype = doctype.strip() or "paper"
    if doctype in _DOC_TYPE_BRIEF:
        workspace["doc_type"] = doctype
    # The brief is the planner's standing ``## Project context`` — the
    # document-type register/voice guidance only. The user's description is
    # the *task*, so it rides as the todo body below (and cascades to child
    # ticks the planner mints), not buried here as background context.
    guidance = _DOC_TYPE_BRIEF.get(doctype, "")
    if guidance:
        workspace["brief"] = guidance

    # The description IS the planner's initial prompt: it becomes the
    # project todo's body (``refs.title`` → the ``## Body`` block read by
    # ``plan_tick``). Fall back to a bare instruction when the user left it
    # blank. ``LLM:opus`` is the closed-vocab auto-run tag the dispatcher
    # keys on to mint the first ``plan_tick`` job (no ``meta.executor``).
    task_text = summary.strip() or f'Write a {doctype} titled "{title}".'

    # 1) project root that owns the workspace + drives the planner.
    body, is_error = await await_dispatch(
        request,
        "put",
        {
            "kind": "todo",
            "text": task_text,
            "tags": ["level:strategic", "LLM:opus"],
            "meta": {"workspace": workspace},
        },
    )
    project_id = None if is_error else _parse_id(body)
    if is_error or project_id is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "New draft error",
                "detail": body
                if is_error
                else f"could not resolve project id:\n{body}",
                "status": 400,
            },
            status_code=400,
        )

    # 2) the draft, bound 1:1 to that project.
    body2, is_error2 = await await_dispatch(
        request,
        "put",
        {
            "kind": "draft",
            "id": slug,
            "title": title,
            "project": project_id,
            "meta": {"workspace": workspace},
        },
    )
    if is_error2:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "New draft error", "detail": body2, "status": 400},
            status_code=400,
        )

    # 2b) scaffold the genre's standard sections (ADR 0037 step 4): append
    #     styled headings for the picked doc_type, so the author lands on a
    #     skeleton to fill and each section's style skill fires as they
    #     write. Best-effort — never fail draft creation on the scaffold.
    sections = _SCAFFOLDS.get(doctype, [])
    if sections:
        store = get_store(request)
        draft_ref = store.get_ref(kind="draft", id=slug)
        if draft_ref is not None:
            try:
                store.scaffold_sections(draft_ref.id, sections)
            except Exception:  # pragma: no cover - defensive
                log.warning("drafts: scaffold failed for %s", slug, exc_info=True)
    return RedirectResponse(url=f"/drafts/{slug}", status_code=303)


_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/drafts/{ident}/export.docx")
async def export_docx_route(request: Request, ident: str) -> Response:
    """Synchronous .docx export — renders the draft and streams it back as
    a download. Toolchain-free (python-docx), so this "just works"; the
    rendering runs off the event loop."""
    from precis.export.docx import export_docx

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    name = str(ref.slug or ref.id)
    out = Path(tempfile.mkdtemp(prefix="precis-docx-")) / f"{name}.docx"
    await asyncio.to_thread(export_docx, store, ref, target_path=out)
    return FileResponse(out, filename=f"{name}.docx", media_type=_DOCX_MEDIA)


@router.post("/drafts/{ident}/export.pdf")
async def export_pdf_route(request: Request, ident: str) -> Response:
    """Start a ``draft_export`` job (LaTeX → PDF). The job runs on a
    worker; its progress logs + result land under the draft's project on
    the task page. Redirects back to the reader."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    project = _project_id(store, ref.id)
    if project is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "PDF export error",
                "detail": "this draft has no project todo to parent the job under",
                "status": 400,
            },
            status_code=400,
        )
    slug = str(ref.slug or ref.id)
    return await redirect_or_error(
        request,
        "put",
        {
            "kind": "job",
            "job_type": "draft_export",
            "parent_id": project,
            "params": {"draft": slug},
            "idem_key": f"draft_export:{slug}",
        },
        redirect=f"/drafts/{ident}",
        error_title="PDF export error",
    )


def _delete_confirm_ok(ref: Any, confirm: str) -> bool:
    """The type-the-name guard: the typed text must match the draft's title
    or slug (trimmed, case-insensitive). Deliberately strict — a delete must
    be intentional, not a stray click."""
    typed = confirm.strip().casefold()
    if not typed:
        return False
    candidates = [
        str(ref.title or "").strip().casefold(),
        str(ref.slug or "").strip().casefold(),
    ]
    return typed in [c for c in candidates if c]


@router.post("/drafts/{ident}/delete")
async def delete_draft(
    request: Request, ident: str, confirm: str = Form("")
) -> Response:
    """Soft-delete a whole draft, gated on typing its name. Atomic
    (``store.soft_delete_draft`` marks the ref deleted + retires its chunks
    in one transaction); recoverable. The owning project todo is left
    intact — this deletes the document, not the project."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    if not _delete_confirm_ok(ref, confirm):
        # name mismatch — bounce back to the reader, nothing deleted.
        return RedirectResponse(url=f"/drafts/{ident}", status_code=303)
    try:
        store.soft_delete_draft(ref.id)
    except Exception as exc:  # pragma: no cover - defensive
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Delete draft error",
                "status": 400,
                "detail": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(url="/drafts", status_code=303)


@router.get("/draft/{ident}")
async def reader_alias(ident: str) -> RedirectResponse:
    """Singular ``/draft/<id>`` → the canonical plural reader."""
    return RedirectResponse(url=f"/drafts/{ident}", status_code=303)


@router.get("/drafts/{ident}", response_class=HTMLResponse)
async def reader(request: Request, ident: str) -> Response:
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    # Stamp the access (drives the drafts list's most-recently-opened order).
    # Only here — the full page open — not the poll/rows/doc/version endpoints,
    # so a tab left polling doesn't keep pinning this draft to the top. Never
    # fail the page on a stamp error.
    try:
        store.touch_viewed(ref.id)
    except Exception:  # pragma: no cover - defensive
        log.warning("drafts: touch_viewed failed for %s", ref.id, exc_info=True)
    chunk_objs, _version, abbrevs = _doc_state(store, ref)
    n = len(chunk_objs)
    first = min(INITIAL_WINDOW, n)
    # The first screen is rendered server-side (correct + visible even if the
    # virtual-scroll JS never runs); the rest live only in the skeleton.
    window_rows = _build_rows(store, ref, chunk_objs, range(first), abbrevs=abbrevs)
    skeleton = _skeleton(store, ref)
    botpad = sum(s["est"] for s in skeleton[first:])
    return templates.TemplateResponse(
        request,
        "drafts/detail.html.j2",
        {
            "active_tab": "drafts",
            "ref": _ref_view(ref),
            "window_rows": window_rows,
            "skeleton": skeleton,
            "botpad": botpad,
            "work": _work_items(store, ref.id),
            "clearance": draft_figure_clearance(store, ref.id),
        },
    )


@router.get("/drafts/{ident}/row/{handle}", response_class=HTMLResponse)
async def reader_row(request: Request, ident: str, handle: str) -> HTMLResponse:
    """One rendered row — the fragment a future live-refresh poll swaps in
    place (the page is composed from this same macro, so no rewrite)."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return HTMLResponse("", status_code=404)
    row = _one_row(store, ref, handle)
    if row is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request,
        "drafts/_row_fragment.html.j2",
        {"r": row, "ref": _ref_view(ref)},
    )


@router.get("/drafts/{ident}/rows", response_class=HTMLResponse)
async def reader_rows(request: Request, ident: str, handles: str = "") -> HTMLResponse:
    """Rendered rows, no page chrome.

    * ``?handles=h1,h2,…`` — **batch hydrate** just those blocks, in
      document order. This is what the reader fetches as a window of
      placeholders scrolls into view: one request for the whole window
      instead of one HTTP per block (the prior O(N) hydrate storm).
    * no ``handles`` — every block hydrated (the whole-draft render);
      kept for callers that want the full document in one shot."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return HTMLResponse("", status_code=404)
    if handles.strip():
        wanted = [h for h in handles.split(",") if h.strip()]
        rows = _rows_for_handles(store, ref, wanted)
    else:
        rows = _rows_for(store, ref)
    return templates.TemplateResponse(
        request,
        "drafts/_rows.html.j2",
        {"rows": rows, "ref": _ref_view(ref)},
    )


@router.get("/drafts/{ident}/skeleton")
async def reader_skeleton(request: Request, ident: str) -> JSONResponse:
    """The virtual-scroll skeleton as JSON (+ version token) — the reader
    fetches this when the version bumps to rebuild its block list without a
    full page reload."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"skeleton": [], "version": 0})
    return JSONResponse(
        {"skeleton": _skeleton(store, ref), "version": _draft_version(store, ref.id)}
    )


def _draft_version(store: Any, ref_id: int) -> int:
    """Monotone version token = max ``chunk_events.event_id`` over the
    draft's chunks. Bumps on every chunk create/edit/move/retire, so it
    doubles as the cache key for a compiled PDF."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ce.event_id), 0) FROM chunk_events ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id WHERE c.ref_id = %s",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row else 0


@router.get("/drafts/{ident}/version")
async def version(request: Request, ident: str) -> JSONResponse:
    """Monotone version token = max ``chunk_events.event_id`` over the
    draft's chunks. The poll refetches changed rows when it bumps."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"version": 0})
    return JSONResponse({"version": _draft_version(store, ref.id)})


def _pdf_cache_dir(ref_id: int, version: int) -> Path:
    """Per-(draft, version) build dir for the compiled PDF. Lives under
    the system temp so it survives within a deploy and is cheap to
    discard; a new version compiles into a fresh dir, so a stale PDF is
    never served."""
    import tempfile

    return Path(tempfile.gettempdir()) / "precis-draft-pdf" / str(ref_id) / str(version)


@router.get("/drafts/{ident}/pdf")
async def pdf(request: Request, ident: str) -> Response:
    """Compile the draft to PDF on demand and serve it, cached by the
    draft's version token. First request for a version exports the LaTeX
    project + runs ``latexmk``; later requests serve the cached file.

    Degrades cleanly: with no ``latexmk`` on the host (``--pdf`` is a
    no-op in such builds) it returns a friendly 503 rather than a 500;
    on a LaTeX error it returns the compile log tail so the failure is
    debuggable (and feeds the future LLM-repair loop)."""
    from precis.export.compile import compile_pdf, have_latexmk
    from precis.export.latex import export_draft

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    version_token = _draft_version(store, ref.id)
    cache_dir = _pdf_cache_dir(ref.id, version_token)
    pdf_path = cache_dir / "main.pdf"
    filename = f"{ref.slug or ref.id}.pdf"

    if not pdf_path.exists():
        if not have_latexmk():
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {
                    "active_tab": "drafts",
                    "title": "PDF unavailable",
                    "status": 503,
                    "detail": (
                        "latexmk is not installed on this host, so the draft "
                        "can't be compiled to PDF here. Run `precis draft export "
                        f"{ref.slug or ref.id} --pdf` on a host with a TeX "
                        "toolchain, or install mactex/TeX Live on the web host."
                    ),
                },
                status_code=503,
            )
        export_draft(store, ref, target_dir=cache_dir)
        result = compile_pdf(cache_dir)
        if not result.ok:
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {
                    "active_tab": "drafts",
                    "title": "PDF compile failed",
                    "status": 500,
                    "detail": (
                        "latexmk could not build this draft. Last lines of "
                        f"the log:\n\n{result.log_tail}"
                    ),
                },
                status_code=500,
            )
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/drafts/{ident}/find")
async def find(
    request: Request, ident: str, q: str = "", mode: str = "verbatim"
) -> JSONResponse:
    """In-draft find — the user's reader-side search bar.

    Returns the matching chunk handles, in the order the find bar
    cycles them with ‹ ›:

    * ``mode='verbatim'`` — case-insensitive substring over each live
      block's source text, in **document order** (a plain Ctrl-F over
      the prose, the deterministic path that needs no embedder).
    * ``mode='semantic'`` — cosine ranked (best-first) over the draft's
      chunk embeddings, scoped to this draft. Degrades to verbatim when
      the embedder is unavailable or the query won't embed.

    The client highlights/scrolls to each handle and cycles next/prev
    starting from whichever chunk is currently in view.
    """
    store = get_store(request)
    ref = _draft_ref(store, ident)
    q = q.strip()
    if ref is None or not q:
        return JSONResponse({"handles": [], "mode": mode})

    chunks = store.reading_order(ref.id)
    m = (mode or "verbatim").strip().lower()

    if m == "semantic":
        hub = getattr(get_runtime(request), "hub", None)
        embedder = getattr(hub, "embedder", None)
        vec = embed_query(embedder, q)
        if vec is not None:
            by_id = {c.chunk_id: c.handle for c in chunks}
            hits = store.search_blocks_semantic(
                query_vec=vec,
                scope_ref_id=ref.id,
                limit=200,
                max_distance=None,
            )
            handles = [by_id[b.id] for b, _ref, _d in hits if b.id in by_id]
            return JSONResponse({"handles": handles, "mode": "semantic"})
        m = "verbatim"  # no vector → degrade to a literal find

    needle = q.lower()
    handles = [c.handle for c in chunks if needle in (c.text or "").lower()]
    return JSONResponse({"handles": handles, "mode": "verbatim"})


@router.get("/drafts/blob/{handle}")
async def chunk_blob(request: Request, handle: str) -> Response:
    """Raw bytes for a figure chunk's image (ADR 0034) — the ``<img>``
    ``src`` the reader points at. 404 when the chunk carries no blob. The
    handle is globally unique, so no draft ident is needed."""
    store = get_store(request)
    blob = store.get_chunk_blob(handle)
    if blob is None:
        return Response(status_code=404)
    data, mime = blob
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post("/drafts/{ident}/request")
async def request_change(
    request: Request,
    ident: str,
    handle: str = Form(...),
    text: str = Form(...),
) -> Response:
    """File a change request anchored at a chunk: a ``todo`` parented on
    the draft's project, carrying ``meta.anchor='¶<handle>'``. Flows
    through the normal todo tree → dispatch → jobs."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    if ref is None or not text.strip():
        return RedirectResponse(url=back, status_code=303)
    project = _project_id(store, ref.id)
    args: dict[str, Any] = {
        "kind": "todo",
        "text": text.strip(),
        "meta": {"anchor": handle},
    }
    if project is not None:
        args["parent_id"] = project
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Change request error"
    )


#: Reviewer briefs for the per-heading "review ▾" dropdown. Each files an
#: anchored review-todo (→ plan_tick), scoped to the heading's subtree.
#: ``all`` files one todo that tells the planner to fan out sequentially.
_REVIEW_BRIEFS: dict[str, str] = {
    "structural": (
        "Structural review of the draft section under {h}. Check it against "
        "the project brief: drift, contradictions with sibling sections, gaps, "
        "depth/fanout problems, weak or missing topic sentences. File concrete "
        "change requests (anchored at the offending chunks) for what to fix."
    ),
    "deep_review": (
        "Deep review of the draft section under {h}. Scrutinise the rigor of "
        "every claim and citation — does each cited passage actually and "
        "strongly support its claim? Prune redundancy, rebalance, and flag "
        "anything overstated. File concrete change requests."
    ),
    "all": (
        "Review the draft section under {h} thoroughly. Do this as SEQUENTIAL "
        "subtasks: (1) a structural review (drift, contradictions, gaps, topic-"
        "sentence structure), then (2) a deep review (claim/citation rigor, "
        "redundancy, overstatement). File concrete change requests from each."
    ),
}


@router.post("/drafts/{ident}/review")
async def review_block(
    request: Request,
    ident: str,
    handle: str = Form(...),
    reviewer: str = Form(...),
) -> Response:
    """Run a standard reviewer on a heading's subtree — files an anchored
    review-todo (parented on the draft's project) that runs as a plan_tick,
    showing up as an in-flight request on the block. ``reviewer`` is
    ``structural`` | ``deep_review`` | ``all``."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    brief = _REVIEW_BRIEFS.get(reviewer)
    if ref is None or brief is None:
        return RedirectResponse(url=back, status_code=303)
    args: dict[str, Any] = {
        "kind": "todo",
        "text": brief.format(h=handle),
        "meta": {"anchor": handle, "review": reviewer},
    }
    project = _project_id(store, ref.id)
    if project is not None:
        args["parent_id"] = project
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Review error"
    )


@router.post("/drafts/{ident}/style")
async def set_section_style(
    request: Request,
    ident: str,
    handle: str = Form(...),
    style: str = Form(""),
) -> Response:
    """Set (or clear) a heading's section style from the editor (ADR 0037).
    ``style=""`` clears it. Routes through ``edit(kind='draft', style=…)``
    so the handler's heading-only guard + error surface apply."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    if ref is None:
        return RedirectResponse(url=back, status_code=303)
    return await redirect_or_error(
        request,
        "edit",
        {"kind": "draft", "id": handle, "style": style},
        redirect=back,
        error_title="Section style error",
    )


@router.post("/drafts/{ident}/figure")
async def add_figure(
    request: Request,
    ident: str,
    handle: str = Form(...),
    caption: str = Form(...),
    origin: str = Form(...),
    file: UploadFile = File(...),
    publisher: str = Form(""),
    permission_id: str = Form(""),
    status: str = Form(""),
    requested_at: str = Form(""),
    granted_at: str = Form(""),
    expires_at: str = Form(""),
    scope: str = Form(""),
    required_credit: str = Form(""),
    source_paper: str = Form(""),
) -> Response:
    """Upload an image as a figure chunk inserted after ``handle`` (ADR
    0034). Bytes are base64'd and routed through the ``put`` verb so the
    DraftHandler's figure validation (caption / origin / third-party needs
    permission) is single-sourced with the MCP surface. A ``third_party``
    figure's permission paper-trail comes from the inline form fields."""
    back = f"/drafts/{ident}#c-{handle}"
    data = await file.read()
    if not data:
        return RedirectResponse(url=back, status_code=303)
    args: dict[str, Any] = {
        "kind": "draft",
        "id": ident,
        "chunk_kind": "figure",
        "text": caption,
        "image": base64.b64encode(data).decode(),
        "origin": origin,
        "at": {"after": handle},
    }
    if file.content_type:
        args["mime"] = file.content_type
    if origin == "third_party":
        perm = {
            k: v.strip()
            for k, v in {
                "publisher": publisher,
                "permission_id": permission_id,
                "status": status,
                "requested_at": requested_at,
                "granted_at": granted_at,
                "expires_at": expires_at,
                "scope": scope,
                "required_credit": required_credit,
                "source_paper": source_paper,
            }.items()
            if v.strip()
        }
        if perm:
            args["permission"] = perm
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Add figure error"
    )


@router.post("/drafts/{ident}/figure/{handle}/permission")
async def edit_figure_permission(
    request: Request,
    ident: str,
    handle: str,
    origin: str = Form("third_party"),
    publisher: str = Form(""),
    permission_id: str = Form(""),
    status: str = Form(""),
    requested_at: str = Form(""),
    granted_at: str = Form(""),
    expires_at: str = Form(""),
    scope: str = Form(""),
    required_credit: str = Form(""),
    source_paper: str = Form(""),
) -> Response:
    """Edit an existing figure's provenance (ADR 0034) — the click-to-edit
    behind the clearance badge. Routes through the ``edit`` verb so figure
    validation stays single-sourced; only ``meta.figure`` changes (caption
    and image bytes are untouched)."""
    back = f"/drafts/{ident}#c-{handle}"
    args: dict[str, Any] = {"kind": "draft", "id": handle, "origin": origin}
    if origin == "third_party":
        args["permission"] = {
            k: v.strip()
            for k, v in {
                "publisher": publisher,
                "permission_id": permission_id,
                "status": status,
                "requested_at": requested_at,
                "granted_at": granted_at,
                "expires_at": expires_at,
                "scope": scope,
                "required_credit": required_credit,
                "source_paper": source_paper,
            }.items()
            if v.strip()
        }
    return await redirect_or_error(
        request, "edit", args, redirect=back, error_title="Edit permission error"
    )


@router.post("/drafts/{ident}/todo/{todo_id}/delete")
async def delete_change_request(request: Request, ident: str, todo_id: int) -> Response:
    """Close a change-request todo anchored in this draft (the X on a
    chip). Cancels a not-yet-started request or clears a finished one
    (done / won't-do / failed); a running request has no X. Soft-deletes
    via the todo handler."""
    back = f"/drafts/{ident}"
    return await redirect_or_error(
        request,
        "delete",
        {"kind": "todo", "id": todo_id},
        redirect=back,
        error_title="Delete change request error",
    )


@router.get("/c/{handle}")
async def goto_chunk(request: Request, handle: str) -> Response:
    """Resolve a chunk handle → redirect to where it lives. A draft chunk
    (``dc<id>`` / ``¶<base58>``) lands in the draft reader anchored at the
    chunk; any **other** chunk handle (``pc<id>`` paper, ``mc``/``lc``/…)
    redirects through the ``/r/<kind>/<id>`` resolver at that chunk (e.g. a
    paper → its PDF page). The click target of every ``¶``/``§`` anchor."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    if chunk is not None:
        ref = store.get_ref(kind="draft", id=int(chunk.ref_id))
        ident = ref.slug if ref and ref.slug else chunk.ref_id
        return RedirectResponse(url=f"/drafts/{ident}#c-{handle}", status_code=303)
    uc = store.universal_chunk(handle)
    if uc is not None:
        # paper chunks carry an ord the /r resolver maps to a PDF page;
        # other kinds just land on the record.
        suffix = (
            f"?chunk={uc['ord']}"
            if uc["kind"] == "paper" and uc["ord"] is not None
            else ""
        )
        return RedirectResponse(
            url=f"/r/{uc['kind']}/{uc['ref_id']}{suffix}", status_code=303
        )
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "active_tab": "drafts",
            "title": "Chunk not found",
            "status": 404,
            "detail": f"no chunk {handle}",
        },
        status_code=404,
    )


@router.get("/preview/chunk/{handle}", response_class=HTMLResponse)
async def preview_chunk(request: Request, handle: str) -> HTMLResponse:
    """Hover-popover fragment for a chunk anchor (``¶``/``§``) — peer of the
    ``/preview/{kind}/{id}`` route, reusing the same popover template.
    Resolves a draft chunk first, then **any** universal chunk handle
    (``pc<id>`` paper, ``mc``/``lc``/…) so a paper-chunk citation hovers to
    its quote. A dangling handle degrades to a 'missing' card."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    chunk_kind = chunk.chunk_kind if chunk is not None else None
    text = chunk.text if chunk is not None else None
    if chunk is None:
        uc = store.universal_chunk(handle)
        if uc is None:
            return templates.TemplateResponse(
                request,
                "preview/popover.html.j2",
                {"kind": "chunk", "label": handle, "missing": True},
            )
        chunk_kind, text = uc["chunk_kind"], uc["text"]
    # Show the chunk's verbatim text (≤ ~20 lines) as the quote — the
    # "what does <handle> actually say?" a hover should answer.
    text = text or ""
    lines = text.splitlines()
    quote = "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
    if len(quote) > 1600:
        quote = quote[:1600].rstrip() + "…"
    return templates.TemplateResponse(
        request,
        "preview/popover.html.j2",
        {
            "kind": chunk_kind,
            "label": handle,
            "ref_id": handle,
            "title": handle,
            "quote": quote.strip() or "(empty)",
            "chunk_label": "",
            "body_preview": "",
            "deleted": False,
            "missing": False,
        },
    )
