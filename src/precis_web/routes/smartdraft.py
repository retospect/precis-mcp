"""Smartdraft — the (sole, live) fisheye-rail draft reader.

Three panes — left (fisheye TOC nav), middle (the focus + its neighbourhood),
right (collaboration: the working set + a request box). The classic
virtual-scroll `/drafts` reader was **retired**; this is now the only draft
reader and every draft deep link redirects in here. It reuses ~20
`/drafts/{ident}/…` backend endpoints (editing / export / figure / lifecycle,
plus `/marks`, `/request-ws`, `/human-review`, `/review`).

The focus is a query param (`?focus=dc<id>`). Navigation is client-side and
no-reload: a TOC/result click swaps only `#sd-content` (see `view.html.j2`).
Two view modes — **fisheye** (relevance-collapse of quiet runs, budget-bounded)
and **full document** (📄, `?relevance=0`, the default) which renders a window
around the focus and lazily hydrates distant chunks on scroll.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from precis.store._tags_ops import escape_like
from precis.store.types import Tag
from precis.utils.embed_query import embed_query
from precis.utils.mentions import COMPUTED_EVIDENCE_KINDS
from precis_web import draft_eyes, smartdraft
from precis_web.claim_render import (
    cite_heads_in,
    claim_cite_head_sets,
    render_claims_evidence,
)
from precis_web.deps import get_runtime, get_store, templates
from precis_web.draft_links import chunk_links
from precis_web.linkify import popover_chip
from precis_web.routes.drafts import (
    _DOC_TYPES,
    RefChip,
    _abbrevs_cached,
    _connection_chips,
    _draft_author_lines,
    _draft_ref,
    _draft_version,
    _owner_workspace,
    _review_status_by_chunk,
)

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["smartdraft"])


@router.get("/smartdraft", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """List drafts, linking each into the smartdraft reader."""
    store = get_store(request)
    refs = store.list_refs(kind="draft", order_by="viewed_desc", limit=200)
    drafts = [
        {
            "id": r.id,
            "slug": r.slug,
            "title": (r.title or r.slug or "untitled").split("\n", 1)[0],
        }
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "smartdraft/index.html.j2",
        {"active_tab": "smartdraft", "drafts": drafts},
    )


#: Search signal letters, in display order.
_SIGNALS = "vkts"


@router.get("/smartdraft/{ident}", response_class=HTMLResponse)
async def reader(
    request: Request,
    ident: str,
    focus: str = "",
    relevance: str = "0",
    q: str = "",
    sig: str = _SIGNALS,
    sview: str = "list",
    debug: str = "",
) -> Response:
    """The three-pane fisheye reader. ``q`` runs multi-signal search (RRF); ``sig``
    is the active-signal letter set (e.g. ``vkts``); ``sview`` is ``list``/``toc``."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "smartdraft",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    marks = draft_eyes.load_marks(store, ref.id)
    rel_on = relevance.strip().lower() not in ("0", "false", "off", "no")
    sview_mode = "toc" if sview == "toc" else "list"

    # Build the nodes once; search + view share them.
    nodes = smartdraft.build_nodes(store, ref.id, marks=marks)

    # Defined-term/abbreviation hover highlighting (linkify._highlight_abbrevs)
    # — reuses the classic reader's whole-draft term map + its
    # ``(ref_id, version)`` cache (``_abbrevs_cached``), keyed off
    # ``_draft_version`` (one ``MAX(chunk_events.event_id)`` aggregate) rather
    # than smartdraft's own ``_cache_version`` (a full string_agg over every
    # live chunk, already paid once per request inside ``build_nodes`` above):
    # cheaper, and correct — abbrevs depend only on chunk CONTENT
    # (``defined_terms``), which ``_draft_version`` already bumps on any
    # create/edit/move/retire; it just skips the chunk-tag count
    # ``_cache_version`` folds in for its own (tag-search-signal) purposes,
    # which has no bearing on term definitions.
    abbrevs = _abbrevs_cached(store, ref.id, _draft_version(store, ref.id))

    # Search (RRF over the active signals). Embed the query once, degrading to
    # lexical-only if the embedder is down; surface that so it isn't silent.
    query = q.strip()
    active = {c for c in sig.lower() if c in _SIGNALS}
    hits: list[smartdraft.SearchHit] = []
    sem_degraded = False
    if query:
        sranks: dict[int, int] = {}
        if "s" in active:
            embedder = getattr(
                getattr(get_runtime(request), "hub", None), "embedder", None
            )
            qvec = embed_query(embedder, query)  # None if no embedder / failure
            sem_degraded = qvec is None
            # top-N nearest via the HNSW index — no full-vector scan.
            sranks = smartdraft.semantic_ranks(store, ref.id, qvec)
        hits = smartdraft.search_chunks(
            nodes, query, active=active, semantic_ranks=sranks
        )

    # The in-TOC search view keeps every hit visible (uncollapsed).
    keep_dcs = {h.node.dc for h in hits} if (query and sview_mode == "toc") else None
    view = smartdraft.assemble_view(
        nodes,
        ref_id=ref.id,
        focus_dc=focus or None,
        relevance=rel_on,
        keep_dcs=keep_dcs,
    )

    # "occurs in N places" backlinks for a focused registry/glossary term
    # (gripe 56690) — computed from the already-loaded node set, no DB scan.
    term_occurrences = (
        smartdraft.term_occurrences(nodes, view.focus)
        if view.focus is not None and view.focus.is_term
        else []
    )

    # "Cited sources" (gripe 56635) — the focus block's paper citations,
    # each a new-tab hover-preview chip. Scoped to the focus only (cheap —
    # its text is already in hand), not the whole draft.
    cited_sources = (
        _cited_sources(store, view.focus.text) if view.focus is not None else []
    )

    # In/out link-edges + anchored flags for the FOCUS chunk (gripe 178766) —
    # the SAME data path the classic reader assembles from
    # (precis_web.draft_links.chunk_links), rendered here rather than baked
    # into ChunkNode/build_nodes: it's a per-request, focus-only lookup
    # (one small query), mirroring how cited_sources/term_occurrences above
    # are computed for just the focus, not cached into every node.
    focus_links = (
        chunk_links(store, ref.id, view.focus.base58)
        if view.focus is not None
        else {"links_out": [], "links_in": [], "flags": []}
    )
    links_out = _connection_chips(focus_links["links_out"])
    links_in = _connection_chips(focus_links["links_in"])
    flags = _flag_chips(focus_links["flags"])

    # Nodes that ACTUALLY render in the middle pane — excludes the `skel`
    # placeholder rows full-document mode uses as inert scroll spacers for
    # everything outside the ±40 window (see `assemble_view`). Every
    # per-request derivation below that scans middle-pane text (claims,
    # review payloads, the hub/citation-lifecycle scoreboard) scopes to
    # THIS list, not `view.middle`/`nodes` — a `skel` row's `.node.text` is
    # the full original chunk text even though nothing renders it, so
    # scanning `view.middle` unfiltered silently re-widens every one of
    # these back to O(whole draft) in full-document mode (the
    # "/smartdraft reader O(all-hubs) TTFB" defect — OPEN-ITEMS.md).
    rendered_nodes = [m.node for m in view.middle if m.mode != "skel"]

    # Taproot claim-hub cites (violet anchors): the hub heads cited anywhere
    # in the RENDERED middle pane, resolved once and shared by every
    # linkify call in the template; the right-rail "Claims" panel lists
    # their evidence via the batch entry point (`render_claims_evidence`)
    # so N distinct hubs cost a handful of bulk queries, not N x ~16.
    claims, pending_claims, refuted_claims, hypothesis_claims = claim_cite_head_sets(
        store, [n.text or "" for n in rendered_nodes]
    )
    # The rail's Claims panel lists hubs in reading order — first appearance
    # in the middle pane, not frozenset iteration order
    # (render_claims_evidence preserves its input order).
    ordered_heads: list[str] = []
    for n in rendered_nodes:
        for h in cite_heads_in(n.text or ""):
            if h in claims and h not in ordered_heads:
                ordered_heads.append(h)
    claims_evidence = render_claims_evidence(store, ordered_heads)

    # Tools pane (right-rail bottom): the export/metadata/lifecycle controls
    # ported from the classic reader. All reuse the classic /drafts/{ident}/…
    # endpoints, so this only needs the same context the classic route computes
    # (drafts.detail): reMarkable readiness, the byline editor's prefilled
    # lines, the genre picker's options + current genre/brief, and the
    # auto-author toggle state.
    from precis.export.remarkable import remarkable_configured
    from precis_web.auth import current_user

    _remarkable_user = current_user(request)
    _, owner_ws = _owner_workspace(store, ref)

    # Whole-draft review-ledger status (migration 0086) — ONE query
    # shared by the per-block indicator,
    # its dropdown, and the toolbar rollup below. Recomputed each render
    # (incl. the __sdRefresh after any review/human-review/retract click) —
    # never baked into the cached ChunkNodes, since a review action changes
    # the ledger without minting a new chunk_id (would go stale).
    review_status = _review_status_by_chunk(store, ref.id)
    rollup = store.drafts.review_rollup_for_draft(ref.id)

    # Per-block indicator payloads (item 6) — scoped to what's actually
    # RENDERED (the middle pane), not the whole draft, so the citation-
    # integrity flag's store lookups (item 5c) stay O(rendered blocks), not
    # O(draft). In full-document mode most of ``view.middle`` is ``skel``
    # placeholder rows (inert scroll spacers, see ``assemble_view`` — they
    # never reach ``sd_review_widget``), so those are excluded here too —
    # otherwise a huge draft's per-cited-paper integrity check
    # (``cite_integrity_ok``'s ``resolve_handle``/``count_chunks`` calls)
    # would run over EVERY node, not just the ones a template actually
    # renders. ``/blocks`` hydration threads the same helper over its own
    # hydrated window (see ``blocks`` below), which never carries ``skel``
    # rows to begin with.
    review_by_dc = smartdraft.review_payloads_for(rendered_nodes, review_status, store)
    # ``focus_review`` — kept for back-compat with any other call site that
    # still expects the OLD raw-ledger shape (``{checker: {...}}``, not the
    # derived indicator); the widget itself reads ``review_by_dc``.
    focus_review = (
        review_status.get(view.focus.chunk_id) if view.focus is not None else None
    )

    checker_counts = smartdraft.checker_rollup(review_status)
    hub_stats = _hub_and_citation_stats(request, store, rendered_nodes)
    shape_stats = _document_shape_stats(store, ref.id)

    # Document-level edges for the header panel — one query, whole-ref
    # (chunk-anchored edges are the right rail's job, per focus block).
    ref_conns = _ref_connection_groups(store, ref.id)

    return templates.TemplateResponse(
        request,
        "smartdraft/view.html.j2",
        {
            "active_tab": "smartdraft",
            "ident": ident,
            "ref": _ref_view(ref),
            "view": view,
            "relevance": rel_on,
            "focus_dc": view.focus.dc if view.focus else "",
            "focus_pinned": bool(view.focus and view.focus.pinned),
            "eye_count": len(marks.get("eyes") or {}),
            "q": query,
            "active_sig": "".join(c for c in _SIGNALS if c in active),
            "sview": sview_mode,
            "hits": hits,
            "sem_degraded": sem_degraded,
            "needs": _needs_items(store, ref.id),
            "term_occurrences": term_occurrences,
            "cited_sources": cited_sources,
            "links_out": links_out,
            "links_in": links_in,
            "flags": flags,
            "claims": claims,
            "pending_claims": pending_claims,
            "refuted_claims": refuted_claims,
            "hypothesis_claims": hypothesis_claims,
            "claims_evidence": claims_evidence,
            "abbrevs": abbrevs,
            "debug": debug.strip().lower() in ("1", "true", "on", "yes"),
            # ── Tools pane (right-rail bottom) — classic-reader parity ──
            "remarkable_ready": remarkable_configured(
                store, login=_remarkable_user.login if _remarkable_user else None
            ),
            "author_lines": _draft_author_lines(ref),
            "doctypes": _DOC_TYPES,
            "cur_doctype": str(owner_ws.get("doc_type") or ""),
            "cur_brief": str(owner_ws.get("brief") or ""),
            "cur_voice": str(owner_ws.get("voice") or ""),
            "authoring_enabled": store.drafts.draft_authoring_enabled(ref.id),
            "focus_review": focus_review,
            # ── Review status (indicator + rollup) ──
            "review_by_dc": review_by_dc,
            "rollup": rollup,
            "checker_counts": checker_counts,
            "hub_stats": hub_stats,
            "shape_stats": shape_stats,
            # ── Header meta panel (▸ collapsed / ▾ expanded) ──
            "doc_meta": _doc_meta(ref),
            "ref_conns": ref_conns,
            "ref_tags": _ref_tag_labels(store, ref.id),
            "concern_count": sum(
                g["total"] for g in ref_conns if g["relation"] == "raises-concern-about"
            ),
        },
    )


@router.get("/smartdraft/{ident}/blocks", response_class=HTMLResponse)
async def blocks(
    request: Request, ident: str, dcs: str = "", debug: str = ""
) -> Response:
    """Lazy-hydrate fragment for full-document (📄) mode: the real reading
    blocks for a window of ``dcs`` (``dc<id>`` handles) scrolling into view.
    The client's IntersectionObserver batches nearby placeholders into one
    request and swaps the returned blocks in place, so a huge draft's full-doc
    view loads a screenful of nodes, not all N. Renders the SAME
    ``sd_doc_block`` macro the initial page uses (``smartdraft/_block.html.j2``)
    so a hydrated block is byte-identical to a server-rendered one."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return HTMLResponse("", status_code=404)
    marks = draft_eyes.load_marks(store, ref.id)
    nodes = smartdraft.build_nodes(store, ref.id, marks=marks)
    by_dc = {n.dc: n for n in nodes}
    wanted = [d.strip() for d in dcs.split(",") if d.strip()]
    sel = [by_dc[d] for d in wanted if d in by_dc]
    # Violet claim-hub cites, resolved once for just this window (the reader
    # does the same for its middle pane) so a hydrated block's [fi…]/pub_id
    # cites render as claim anchors identically to the initial render.
    claims, pending_claims, refuted_claims, hypothesis_claims = claim_cite_head_sets(
        store, [n.text or "" for n in sel]
    )
    # Review-status payload for just this hydrated window (item 4 — the
    # /blocks payload must carry the same per-chunk review dict as the
    # initial render, or a scrolled-to indicator would silently stay blank).
    review_by_dc = smartdraft.review_payloads_for(
        sel, _review_status_by_chunk(store, ref.id), store
    )
    # Same abbrev-highlighting map the initial render uses (see reader()'s
    # note) — a scrolled-to hydrated block must highlight defined terms
    # identically to a server-rendered one.
    abbrevs = _abbrevs_cached(store, ref.id, _draft_version(store, ref.id))
    return templates.TemplateResponse(
        request,
        "smartdraft/_blocks.html.j2",
        {
            "nodes": sel,
            "claims": claims,
            "pending_claims": pending_claims,
            "refuted_claims": refuted_claims,
            "hypothesis_claims": hypothesis_claims,
            "abbrevs": abbrevs,
            "review_by_dc": review_by_dc,
            "ident": ident,
            "debug": debug.strip().lower() in ("1", "true", "on", "yes"),
        },
    )


def _hub_and_citation_stats(
    request: Request, store: Store, nodes: list[Any]
) -> dict[str, Any]:
    """Item 5(a)'s rollup-dropdown numbers — the taproot hub-coverage
    scoreboard (``DraftHandler._taproot_hub_scoreboard``, the SAME
    computation the outline's ``## Hygiene`` "N of M cited passages have a
    hub" line uses — see ``_hygiene_lines``) plus the citation lifecycle
    counts (``_citations_view``'s to-fetch/to-re-ground/to-promote/done
    partition). Both reused, not reimplemented; both derived from
    ``nodes`` (no extra query — they carry the ``.dc``/``.text`` either
    helper needs). Degrades to all-zero when no handler is wired (a bare
    test ``FakeRuntime`` has no ``.hub`` — this dropdown entry is
    advisory, not load-bearing, so it fails soft rather than 500ing the
    whole reader).

    ``nodes`` is deliberately the caller's scoped choice, not the whole
    draft — the ``reader()`` call site passes only what full-document mode
    actually RENDERS (excludes ``skel`` placeholders), so this dropdown's
    numbers are viewport-scoped and will differ from the MCP
    ``get(kind='draft')`` hygiene line's whole-draft count for a large
    draft (same underlying computation, deliberately different input —
    OPEN-ITEMS.md's "/smartdraft reader" scope fix; the MCP hygiene
    surface itself is untouched, see ``_hygiene_lines``)."""
    from precis.handlers._citations_view import _build_rows, _collect_raw_cites

    grounded, total = 0, 0
    try:
        handler = get_runtime(request).hub.handler_for("draft")
        # ``_taproot_hub_scoreboard`` takes resolved cited-paper ref_ids
        # (docs/backlog/draft-doi-completeness-check.md's Hygiene-footer
        # refactor shares the chunk-token mining with the DOI line) —
        # mine once via the same helper the outline's own scoreboard uses.
        cited_ref_ids = handler._cited_paper_ref_ids(nodes)
        grounded, total = handler._taproot_hub_scoreboard(cited_ref_ids)
    except Exception:
        pass
    try:
        raw = _collect_raw_cites(store, nodes)
        buckets, _doi_line = _build_rows(store, raw)
        lifecycle = {p: len(rows) for p, rows in buckets.items()}
    except Exception:
        lifecycle = {}
    return {"hub_grounded": grounded, "hub_total": total, "lifecycle": lifecycle}


def _document_shape_stats(store: Store, ref_id: int) -> dict[str, Any]:
    """Item 10's deterministic half, for the toolbar rollup dropdown:
    per-section word-count balance, reusing ``aggregate_word_counts`` — the
    SAME computation ``view='wordcount'`` renders, not reimplemented.

    Scaffold-completeness (current headings vs the draft's expected
    sections) is deliberately NOT computed: there is no STORED "expected
    section list" to diff against — ``Store.scaffold_sections`` lays down
    headings once, at draft-creation time, and persists nothing else about
    what it laid down. Rather than invent a scaffold store (out of remit —
    a store/schema decision), this surfaces the absence plainly via
    ``scaffold_note`` and shows only the word-count balance."""
    from precis.utils.wordcount import aggregate_word_counts

    try:
        chunks = store.drafts.reading_order(ref_id)
        report = aggregate_word_counts(chunks)
    except Exception:
        return {
            "total_words": 0,
            "n_sections": 0,
            "flagged": [],
            "scaffold_note": (
                "scaffold-completeness unavailable — no stored expected-"
                "section list for this draft"
            ),
        }
    flagged = [s for s in report.sections if s.verdict in ("under", "over")]
    return {
        "total_words": report.total,
        "n_sections": len(report.sections),
        "flagged": [
            {"title": s.title or "(untitled)", "verdict": s.verdict, "words": s.words}
            for s in flagged
        ],
        "scaffold_note": (
            "scaffold-completeness unavailable — no stored expected-section "
            "list for this draft (scaffold_sections lays down headings at "
            "creation time but doesn't persist what it laid down)"
        ),
    }


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _cited_sources(store: Any, text: str) -> list[RefChip]:
    """The paper (``pc``/``pa``) AND computational-evidence
    (:data:`precis.utils.mentions.COMPUTED_EVIDENCE_KINDS`) sources the
    focus block cites, as hover-preview chips (gripe 56635) — reuses the classic ``/drafts``
    reader's block-scoped cite parser (:func:`precis_web.routes.drafts.
    _ref_chips`) rather than re-implementing cite parsing, then narrows its
    output to just the citation chips this rail wants (that parser also
    yields intra-draft ``¶`` xrefs / other-kind mentions). Each chip is a
    :func:`precis_web.linkify.popover_chip` — its anchor already carries
    ``target=\"_blank\" rel=\"noopener\"`` (no ``data-dc``), so it opens the
    source in a new tab and the no-reload nav interceptor leaves it alone.

    Filters on each chip's structured ``(kind, is_chunk)``
    (:class:`precis_web.routes.drafts.RefChip`) rather than string-sniffing
    the rendered href (gr171761) — ``is_chunk=False`` excludes a chunk-form
    paper handle (``pc10`` → ``/c/pc10``, not a whole-paper source chip),
    matching the historical ``/r/paper/`` filter's behaviour without
    depending on the href's literal shape."""
    from precis_web.routes.drafts import _paper_pdf_missing, _ref_chips

    def is_missing(kind: str, ident: str) -> bool:
        return kind == "paper" and _paper_pdf_missing(store, ident)

    chips = _ref_chips(text or "", is_missing=is_missing)
    return [
        c
        for c in chips
        if not c.is_chunk and (c.kind == "paper" or c.kind in COMPUTED_EVIDENCE_KINDS)
    ]


def _flag_chips(flags: list[dict[str, Any]]) -> list[Any]:
    """Render ``chunk_links``'s ``flags`` (anchored change-request todos,
    ``store.drafts.anchored_todos``) as hover chips — the standalone-anchor case
    gripe 178766 filed on: no project link, no job, so neither the fisheye
    ring nor the outline's "Work in progress" block ever surfaces it. Shares
    :func:`precis_web.linkify.popover_chip` with ``_connection_chips``
    (the links_out/links_in chips), just a different field shape (a todo
    dict, not a ``links`` row) so it isn't reused directly."""
    chips: list[Any] = []
    for f in flags:
        label = f.get("title") or f"todo:{f.get('ref_id')}"
        if f.get("audit"):
            label += f" [{f['audit']}]"
        if f.get("status"):
            label += f" · {f['status']}"
        chips.append(popover_chip(label, f"/r/todo/{f.get('ref_id')}", None))
    return chips


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _needs_items(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Open change-requests / LLM jobs on this draft for the right pane — the
    '✋ needs you' + in-flight status. Reuses the classic reader's walk; a bare
    list of ``{todo_id, title, blocked, asks, status}`` degrades to [] on error."""
    try:
        from precis_web.routes.drafts import _work_items

        items = _work_items(store, ref_id)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for w in items or []:
        # ``_work_items`` returns dicts (jobs = ``[{"id", "status", "reason"}]``,
        # asks = ``[{"tag", "question"}]``) — read keys, not attributes, or every
        # field silently defaults and the pane shows a blank row (todo → None).
        jobs = w.get("jobs") or ()
        status = (
            jobs[-1]["status"] if jobs else ("blocked" if w.get("blocked") else "open")
        )
        out.append(
            {
                "todo_id": w.get("todo_id"),
                "title": w.get("title") or "",
                "blocked": bool(w.get("blocked")),
                "asks": [
                    (a.get("question") or a.get("tag") or "")
                    for a in (w.get("asks") or [])
                ],
                "status": status,
            }
        )
    return out


def _ref_view(ref: Any) -> dict[str, Any]:
    """The ref row the header strip renders. Deliberately more than
    ``{id, title}`` (all this used to pass): the document's own metadata —
    when it was made, what it belongs to, what a worker stamped on it — had
    no surface in the reader at all, so e.g. an ``audio_failed_at`` sat
    invisible on every morning brief whose narration had failed."""
    return {
        "id": ref.id,
        "kind": getattr(ref, "kind", "draft"),
        "slug": getattr(ref, "slug", None),
        "title": getattr(ref, "title", None) or f"draft {ref.id}",
        "created_at": getattr(ref, "created_at", None),
        "updated_at": getattr(ref, "updated_at", None),
    }


#: Draft ``meta`` keys the header panel renders with a human label, in
#: display order. Nested ``workspace`` is flattened to ``workspace.<key>``.
#:
#: This is a LABEL table, not a filter: a key that isn't here still renders
#: (under its raw name, see ``_doc_meta``). That fallthrough is the point —
#: a whitelist means the next worker to stamp a new key is invisible until
#: someone remembers to edit this dict.
_META_LABELS: dict[str, str] = {
    "workspace.doc_type": "genre",
    "workspace.brief": "project brief",
    "workspace.path": "workspace",
    "workspace.format": "format",
    "workspace.seeds": "seed material",
    "authoring_enabled": "auto-author",
    "llm_tier": "llm tier",
    "imported_from": "imported from",
    "cohort": "cohort",
    "dossier_of_quest": "dossier of quest",
    "dossier_of_owner": "dossier owner",
    "cast": "audio cast",
    "date": "brief date",
    "voice": "TTS voice",
    "audio_episode_id": "audio episode",
    "audio_failed_at": "audio failed",
    "abbrev_ignore": "not-abbrevs",
    "reader_working_set": "reader working set",
}

#: Keys rendered as "N <noun>" rather than dumped — a registry whose size is
#: the signal and whose contents would swamp the panel.
_META_COUNTS: dict[str, str] = {"abbrevs": "abbreviations"}

#: Keys whose mere presence is a fault worth colouring amber.
_META_WARN: frozenset[str] = frozenset({"audio_failed_at"})

#: Relations that lead the connections list — a finding filed against the
#: document outranks its bibliography.
_REL_PRIORITY: dict[str, int] = {
    "raises-concern-about": 0,
    "draft-of": 1,
    "dossier-of": 2,
    "derived-from": 3,
}


def _flatten_meta(meta: dict[str, Any]) -> list[tuple[str, Any]]:
    """``meta`` as ``(key, value)`` pairs, ``workspace`` flattened one level
    to ``workspace.<key>``. Empty containers drop out — a `{}` abbrevs
    registry is noise, not information."""
    out: list[tuple[str, Any]] = []
    for key, value in (meta or {}).items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "workspace" and isinstance(value, dict):
            out.extend(
                (f"workspace.{k}", v)
                for k, v in value.items()
                if v is not None and v != "" and v != [] and v != {}
            )
        else:
            out.append((key, value))
    return out


def _meta_value(key: str, value: Any) -> str:
    """One meta value as display text. Counts collapse to "N noun"; lists
    join; anything structured falls back to compact JSON rather than a
    Python repr."""
    if key in _META_COUNTS:
        n = len(value) if hasattr(value, "__len__") else 0
        return f"{n} {_META_COUNTS[key]}"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
        return ", ".join(str(v) for v in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _doc_meta(ref: Any) -> list[dict[str, Any]]:
    """The draft's ``meta`` as labelled rows for the header panel — known keys
    first in ``_META_LABELS`` order, then everything else alphabetically under
    its raw name (``raw=True``, rendered monospace so it reads as "we don't
    have a nice name for this yet" rather than as a typo)."""
    pairs = _flatten_meta(getattr(ref, "meta", None) or {})
    order = list(_META_LABELS)
    known = [(k, v) for k, v in pairs if k in _META_LABELS or k in _META_COUNTS]
    known.sort(key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    unknown = sorted((kv for kv in pairs if kv not in known), key=lambda kv: kv[0])
    return [
        {
            "key": key,
            "label": _META_LABELS.get(key, key),
            "value": _meta_value(key, value),
            "warn": key in _META_WARN,
            "raw": key not in _META_LABELS and key not in _META_COUNTS,
        }
        for key, value in [*known, *unknown]
    ]


def _ref_tag_labels(store: Store, ref_id: int) -> list[str]:
    """Ref-level tags as ``ns:value`` labels (bare value in the default
    namespace). Empty for every draft today — nothing tags them — so the
    panel's tag row renders only when something starts to."""
    tags = store.ref_tags_bulk([ref_id]).get(ref_id, [])
    return [f"{ns}:{value}" if ns and ns != "tag" else value for ns, value in tags]


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _ref_connection_groups(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Whole-document edges grouped by (relation, direction), concerns first.

    **Every** edge is rendered — the group is bounded by a scrolling box in the
    template, not by a chip quota here. An earlier cap kept the first dozen and
    reduced the rest to a "+N more" count, which made a briefing's bibliography
    half-unreachable; and any cap tuned to today's corpus is tuned to a
    coincidence of what the briefing cast happens to write, not to a property
    of drafts. Bounding height in CSS holds at any N without discarding."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for conn in store.drafts.ref_connections(ref_id):
        groups.setdefault((conn["relation"], conn["direction"]), []).append(conn)
    out: list[dict[str, Any]] = []
    for (relation, direction), conns in groups.items():
        out.append(
            {
                "relation": relation,
                "direction": direction,
                "total": len(conns),
                "chips": _connection_chips(conns),
            }
        )
    out.sort(
        key=lambda g: (_REL_PRIORITY.get(str(g["relation"]), 9), str(g["relation"]))
    )
    return out


def _chunk_tags(store: Store, chunk_id: int) -> list[str]:
    """The tag values on one chunk (for the write path's echo-back)."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT t.value FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
                "WHERE ct.chunk_id = %s ORDER BY t.value",
                (chunk_id,),
            ).fetchall()
    except Exception:
        return []
    return [str(r[0]) for r in rows]


@router.post("/smartdraft/{ident}/chunk-tag")
async def chunk_tag(request: Request, ident: str) -> JSONResponse:
    """Add / remove a **chunk-level** tag (the ``T`` search signal). Body
    ``{handle: 'dc<id>', add?: str, remove?: str}``. Tags are free-form (OPEN
    namespace, lowercased). Returns the chunk's current tag values."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    rh = store.resolve_handle(str(payload.get("handle") or ""))
    if rh is None or rh.chunk_ord is None or int(rh.ref_id) != ref.id:
        return JSONResponse({"ok": False, "error": "chunk not found"}, status_code=404)
    add = str(payload.get("add") or "").strip()
    remove = str(payload.get("remove") or "").strip()
    if add:
        store.add_tag(ref.id, Tag.open(add), pos=rh.chunk_ord)
    if remove:
        store.remove_tag(ref.id, Tag.open(remove), pos=rh.chunk_ord)
    # Tag edits don't mint a new chunk_id, so bust the base-node cache directly
    # (a text edit self-invalidates via the content version) — the T signal +
    # the chip row then reflect it on the very next render.
    smartdraft.invalidate(ref.id)
    return JSONResponse({"ok": True, "tags": _chunk_tags(store, int(rh.chunk_id))})


@router.get("/smartdraft/{ident}/tag-suggest")
async def tag_suggest(request: Request, ident: str, q: str = "") -> JSONResponse:
    """Type-ahead: existing tag values on this draft matching ``q`` — so you
    reuse tags you've already applied rather than re-inventing them."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    query = q.strip()
    if ref is None or len(query) < 2:
        return JSONResponse({"tags": []})
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT t.value FROM tags t "
                "JOIN chunk_tags ct ON ct.tag_id = t.tag_id "
                "JOIN chunks c ON c.chunk_id = ct.chunk_id "
                "WHERE c.ref_id = %s AND t.value ILIKE %s ORDER BY t.value LIMIT 10",
                (ref.id, f"%{escape_like(query)}%"),
            ).fetchall()
    except Exception:
        return JSONResponse({"tags": []})
    return JSONResponse({"tags": [str(r[0]) for r in rows]})
