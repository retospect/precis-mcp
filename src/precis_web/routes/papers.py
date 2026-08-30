"""Papers tab — read PDFs in-browser (list/triage folded into Drive, WS1b).

``/papers`` and ``/papers/triage`` redirect to a Drive kind/tag preset
(``/drive?k=paper…`` / ``/drive?tag=needs-triage&k=paper…``), keeping old
bookmarks working. The **reader** stays here: detail page embeds the
browser's native PDF viewer at ``/papers/{id}/pdf``, streaming from
``corpus_dir`` (cluster NFS mount) via ref cite_key (``Ref.slug``) and
the ``precis watch`` shard layout ``<corpus_dir>/<letter>/<cite_key>.pdf``
— plus metadata edit/triage-lookup/tag/delete, unmoved.

Chunk anchoring is phrase-first with page fallback
(``paper-viewer.js::findInPdf``): jump to chunk text on PDF-find match,
else to its page, marked ``~p.N``. Every chunk selector (``?chunk=``,
Jump box, TOC clicks) funnels through the ONE resolver
:func:`_cited_chunk` (bare ord, ``lo..hi`` range, or compound
``pa<ref_id>~N[..M]`` handle).

Sources/Cited tabs render S2's citation graph off ``s2_neighbors``
(migration 0106; :func:`ensure_s2_neighbors` backfills on-demand at first
view), merged against ``paper_bib_entries`` (``_BibEntryIndex``:
held_ref_id → doi → s2_id); a non-held row's Fetch button mints the ref
and queues it via ``Store.requeue_stubs_for_fetch``. Meta tab: reviewed
toggle (first writer of ``refs.human_verified_at``), client-side S2
triage prefill, backlinks panel (incoming ``links`` edges by
``dst_ref_id``).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from precis.backfill.citation_recall import ensure_s2_neighbors
from precis.corpus_layout import corpus_pdf_dest
from precis.errors import BadInput, NotFound
from precis.handlers._paper_format import ENTRY_TYPE_CHOICES, ENTRY_TYPE_LABELS
from precis.identity import normalize_doi
from precis.store.types import BibEntry
from precis.utils.authors import author_names
from precis.utils.embed_query import embed_query
from precis.utils.handle_registry import format_handle
from precis.utils.toc_db import build_toc_segments
from precis_web.corpus import (
    pdf_candidates,
    ref_pdf_keys,
    resolve_pdf,
    resolve_pdf_for_ref,
)
from precis_web.deps import (
    await_dispatch,
    get_runtime,
    get_store,
    get_web_config,
    redirect_or_error,
    templates,
)
from precis_web.item_view import _OPEN_URL_OVERRIDES
from precis_web.paper_ident import paper_abstract
from precis_web.paper_links import doi_url, scholar_title_url

if TYPE_CHECKING:
    from precis.store.protocols import LinksStore, PoolStore
    from precis.store.store import Store

router = APIRouter(prefix="/papers", tags=["papers"])

#: Open tag marking a paper whose metadata automation couldn't recover —
#: the triage queue works this set (set by ``precis fix-metadata``).
_TRIAGE_TAG = "needs-triage"

#: Drive kind-facet preset the retired ``/papers/triage`` list bounces to
#: (WS1b) — narrows to the ``paper`` kind + the ``needs-triage`` tag chip.
#: ``submitted=1`` makes ``k=`` authoritative (see ``routes/drive.py``).
_TRIAGE_PRESET = f"/drive?tag={_TRIAGE_TAG}&k=paper&submitted=1"

#: Cap on the abstract length shown in the hover card (chars).
_ABSTRACT_PREVIEW = 900

_WS_RE = re.compile(r"\s+")

#: Matches the cross-ref identifier-uniqueness error raised by
#: ``Store.set_ref_identifier`` (``_identifiers_ops.py``) so a failed
#: metadata edit can render the duplicate resolver — links to the owner
#: + a delete button — instead of a raw 400.
_ID_CONFLICT_RE = re.compile(
    r"(?P<field>\w+)=(?P<value>'[^']*'|\S+) already belongs to ref id=(?P<owner>\d+)"
)


def _parse_identifier_conflict(body: str) -> dict[str, Any] | None:
    """Pull ``(field, value, owner_id)`` out of the duplicate-identifier 400.

    Scoped to ``doi`` / ``arxiv`` — those are the "same paper held twice"
    case the delete-resolver is for. A ``cite_key`` clash is a different
    problem (pick another handle), handled inline by the rename path, so
    it falls through to the generic error page here. Returns ``None`` for
    any other error too.
    """
    m = _ID_CONFLICT_RE.search(body or "")
    if m is None or m.group("field") not in ("doi", "arxiv"):
        return None
    return {
        "field": m.group("field"),
        "value": m.group("value").strip("'"),
        "owner_id": int(m.group("owner")),
    }


_SLUG_RE = re.compile(r"[a-z0-9]+")


def _suggest_slug(store: Store, ref: Any, prefill: dict[str, Any] | None) -> str:
    """A free ``cite_key`` suggestion from the paper's author + year.

    Uses the S2 ``prefill`` (author/year the operator is about to save)
    when present, else the ref's stored values. Returns ``""`` when the
    inputs are too thin to beat the ``anon`` placeholder, or on any error
    (a suggestion must never 500 the detail page).
    """
    if prefill:
        raw = str(prefill.get("authors") or "")
        authors: Any = [
            ln.strip() for ln in raw.replace(";", "\n").splitlines() if ln.strip()
        ]
        yr = str(prefill.get("year") or "").strip()
        year: int | None = int(yr) if yr.isdigit() else None
    else:
        authors = ref.authors or []
        year = ref.year
    if not authors:
        return ""
    try:
        return store.suggest_cite_key(authors, year, exclude_ref_id=ref.id)
    except Exception:
        return ""


# Corpus PDF path resolution (alias-aware) now lives in ``precis_web.corpus`` so
# the draft reader can share it (to flag a cited paper whose file is missing).
# Kept under the historical private names every call site below already uses.
_pdf_candidates = pdf_candidates
_resolve_pdf = resolve_pdf
_resolve_pdf_for_ref = resolve_pdf_for_ref
_ref_pdf_keys = ref_pdf_keys


def _authors_str(ref: Any) -> str:
    """Authors joined for a single-line display (natural reading order).

    Shape-tolerance lives in :mod:`precis.utils.authors` — this is just
    the inline-join wrapper.
    """
    return ", ".join(author_names(getattr(ref, "authors", None)))


def _author_edit_lines(ref: Any) -> list[str]:
    """Editor prefill: one author per line in natural ("Given Family")
    order — the display convention everywhere (see
    :mod:`precis.utils.authors`) — round-tripped back to the stored
    shape by the edit handler's :func:`~precis.utils.authors.normalize_authors`
    call."""
    return author_names(getattr(ref, "authors", None))


def _abstract_str(ref: Any) -> str:
    """Plain-text abstract preview for the hover card, capped to a preview
    length so the card stays bounded. Tag-stripping is single-sourced in
    :func:`precis_web.paper_ident.paper_abstract` — the same strip the
    unified preview hover uses."""
    return paper_abstract(ref, max_chars=_ABSTRACT_PREVIEW)


def _abstract_full(ref: Any) -> str:
    """Full publisher abstract (tag-stripped, NOT truncated) for the
    editable form. Distinct from :func:`_abstract_str` — feeding the editor
    the preview would persist the truncation on save."""
    return paper_abstract(ref)


def _links_from_ids(ids: dict[str, str]) -> dict[str, str]:
    """Build verification links from a ref's external identifiers.

    ``ids`` is the ``{scheme: value}`` map from
    ``store.identifiers_for_refs``. We surface DOI and arXiv as
    clickable URLs (the two an operator uses to verify a paper at a
    glance); other schemes are left for the detail page.
    """
    doi = ids.get("doi", "")
    arxiv = ids.get("arxiv", "")
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "arxiv": arxiv,
        "arxiv_url": f"https://arxiv.org/abs/{arxiv}" if arxiv else "",
    }


#: Schemes already surfaced by :func:`_links_from_ids` (doi, arxiv) or
#: internal/non-display (the local slug + dedup hashes) — never repeated
#: in the Meta tab's "other identifiers" list.
_EXTRA_ID_HIDDEN_SCHEMES = frozenset(
    {"doi", "arxiv", "cite_key", "paper_id", "pdf_sha256", "content_hash"}
)

#: Display label per extra identifier scheme; falls back to the raw
#: scheme string for anything not listed.
_EXTRA_ID_LABELS: dict[str, str] = {
    "pubmed": "PubMed",
    "openalex": "OpenAlex",
    "s2": "S2",
    "mag": "MAG",
    "dblp": "DBLP",
}

#: Canonical verify-link builder per scheme, for the schemes that have
#: one. MAG / DBLP have no single canonical landing page, so they render
#: as plain (unlinked) text per the backlog decision.
_EXTRA_ID_URL_BUILDERS: dict[str, Callable[[str], str]] = {
    "pubmed": lambda v: f"https://pubmed.ncbi.nlm.nih.gov/{v}/",
    "openalex": lambda v: f"https://openalex.org/{v}",
    "s2": lambda v: f"https://www.semanticscholar.org/paper/{v}",
}


def _extra_identifiers(ids: dict[str, str]) -> list[dict[str, str]]:
    """Identifiers beyond DOI/arXiv — PubMed, OpenAlex, S2, MAG, DBLP —
    from the same ``{scheme: value}`` map :func:`_links_from_ids` reads,
    each linked where a canonical URL exists (see
    :data:`_EXTRA_ID_URL_BUILDERS`). Sorted by scheme for stable
    rendering; empty when the paper carries none."""
    out: list[dict[str, str]] = []
    for scheme in sorted(ids):
        if scheme in _EXTRA_ID_HIDDEN_SCHEMES:
            continue
        value = (ids.get(scheme) or "").strip()
        if not value:
            continue
        builder = _EXTRA_ID_URL_BUILDERS.get(scheme)
        out.append(
            {
                "scheme": scheme,
                "label": _EXTRA_ID_LABELS.get(scheme, scheme),
                "value": value,
                "url": builder(value) if builder else "",
            }
        )
    return out


def _paper_row(ref: Any) -> dict[str, Any]:
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": ref.title,
        "year": ref.year,
        "has_pdf": bool(ref.pdf_sha256),
        # Filled in by the index route from a batched existence query.
        "has_chunks": False,
        "authors": _authors_str(ref),
        "abstract": _abstract_str(ref),
        "links": {"doi": "", "doi_url": "", "arxiv": "", "arxiv_url": ""},
    }


#: Max sources shown per (kind, relation) group in the backlinks panel — a
#: heavily-cited paper can be linked from hundreds of sources; the tail is
#: noted rather than dumped inline (the deep ``/backlinks`` page is the
#: expansion, per OPEN-ITEMS).
_BACKLINKS_GROUP_CAP = 40


def _src_url(ref: Any) -> str:
    """Canonical in-app URL for a *linking source* ref, kind-agnostic. Mirrors
    ``item_view.open_url`` (the shared ``_OPEN_URL_OVERRIDES`` map + the
    ``/refs/<kind>/<id>`` fallback), plus two overrides that map can't
    express: a ``finding`` opens its claim page ``/claim/fi<id>`` (the
    ``/claim`` route renders a friendly stub for a non-hub finding, so it's
    safe for lifecycle findings too), and a ``paper`` links by ``slug or id``
    to match this module's own idiom (``_refs_row``), preferring the stable
    cite_key over the bare ref id."""
    if ref.kind == "finding":
        return f"/claim/{format_handle('finding', ref.id)}"
    if ref.kind == "paper":
        return f"/papers/{ref.slug or ref.id}"
    tmpl = _OPEN_URL_OVERRIDES.get(ref.kind)
    if tmpl:
        return tmpl.format(id=ref.id, slug=ref.slug or ref.id)
    return f"/refs/{ref.kind}/{ref.id}"


def _backlinks(store: LinksStore, ref_id: int) -> list[dict[str, Any]]:
    """ "Who links here" — every held incoming edge into this ref, grouped by
    ``(source kind, relation)`` and clickable to the source's canonical page.
    Kind-agnostic: drafts (``cites``/``related-to``), findings
    (``derived-from`` / hub evidence roles), dreams, quests, or other papers
    all surface for free.

    Reads the materialised ``links`` reverse index (``dst_ref_id`` is indexed)
    — one cheap SQL read, no S2/network (unlike the Sources/Cited tabs).
    *Coverage caveat:* this shows only *materialised* edges. Some inline draft
    ``[pc]/[pa]/[fi]`` prose cites are scanned from chunk text on demand
    (``handlers/_citations_view``) and may not have a ``links`` row until the
    draft-save autolinker materialises them; a text-scan union is the
    documented expansion (OPEN-ITEMS "Paper-page product gaps")."""
    links = store.links_for(ref_id, direction="in")
    if not links:
        return []
    src_ids = {lk.src_ref_id for lk in links if lk.src_ref_id != ref_id}
    refs = store.fetch_refs_by_ids(list(src_ids)) if src_ids else {}
    # Group by (kind, relation); dedupe on source ref (a source citing at N
    # chunks yields N link rows but is one backlink) and count the edges.
    groups: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for lk in links:
        src = refs.get(lk.src_ref_id)
        if src is None or getattr(src, "deleted_at", None) is not None:
            continue  # missing / soft-deleted source
        bucket = groups.setdefault((src.kind, lk.relation), {})
        row = bucket.get(src.id)
        if row is None:
            handle = format_handle(src.kind, src.id)
            bucket[src.id] = {
                "url": _src_url(src),
                "title": _nav_snippet(src.title or "") or handle,
                "handle": handle,
                "edges": 1,
            }
        else:
            row["edges"] += 1
    out: list[dict[str, Any]] = []
    for (kind, relation), bucket in sorted(groups.items()):
        rows = sorted(bucket.values(), key=lambda r: r["handle"])
        overflow = max(0, len(rows) - _BACKLINKS_GROUP_CAP)
        out.append(
            {
                "kind": kind,
                "relation": relation,
                "count": len(rows),
                "rows": rows[:_BACKLINKS_GROUP_CAP],
                "overflow": overflow,
            }
        )
    return out


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(q: str | None = None) -> Response:
    """Retired into the unified Drive surface (WS1b) — redirects to the
    ``kind=paper`` facet preset, carrying a live query through so a bare
    ``?q=`` bookmark keeps searching. The keyword/semantic mode toggle and
    the ``has_pdf``/``has_chunks`` list filters this route used to own
    don't have a Drive equivalent and are dropped with the list (Drive's
    own cross-kind search box replaces them); the reader (``/papers/{ident}``
    and everything under it) is unaffected.
    """
    params: list[tuple[str, str]] = [("k", "paper"), ("submitted", "1")]
    if q and q.strip():
        params.append(("q", q.strip()))
    return RedirectResponse(url="/drive?" + urlencode(params))


@router.get("/triage", response_class=HTMLResponse)
async def triage_queue() -> Response:
    """Retired into the unified Drive surface (WS1b) — redirects to the
    ``needs-triage`` tag preset (:data:`_TRIAGE_PRESET`), narrowed to
    ``kind=paper``. Registered before ``/{ident}`` so the literal
    ``triage`` segment isn't swallowed by the path param. Reached from
    Needs-you's "view all" / "+N more" links (Risk R5) — both keep working
    through this redirect.
    """
    return RedirectResponse(url=_TRIAGE_PRESET)


#: Display label per ``refs.retraction_status`` value (the CHECK
#: constraint vocabulary — see ``store._refs_ops.py::set_retraction_status``).
_RETRACTION_LABELS: dict[str, str] = {
    "retracted": "Retracted",
    "corrected": "Correction issued",
    "expression_of_concern": "Expression of concern",
}


def _retraction_notice(ref: Any) -> dict[str, str] | None:
    """The Meta tab's retraction banner data, or ``None`` when clean.

    Display-only (Crossref ``update-to`` is the source of record — see
    ``ingest/paper_meta_enrich.py``); the web UI never writes these
    columns. ``getattr`` defaults tolerate a duck-typed ``Ref`` (route
    tests) that doesn't set the retraction columns at all.
    """
    status = getattr(ref, "retraction_status", None)
    if not status:
        return None
    return {
        "status": status,
        "label": _RETRACTION_LABELS.get(status, status),
        "reason": getattr(ref, "retraction_reason", None) or "",
        "url": getattr(ref, "retraction_url", None) or "",
    }


def _detail_tags(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """Tag chips for the Meta tab. OPEN (free) tags are ``deletable`` so
    the template offers a × that removes them (the ``needs-triage`` review
    flag included); closed-vocab tags render as inert pills, matching the
    Refs detail strip."""
    out: list[dict[str, Any]] = []
    for t in store.tags_for(ref_id):
        ns = getattr(t, "namespace", "OPEN")
        val = getattr(t, "value", "")
        out.append(
            {
                "namespace": ns,
                "value": val,
                "label": f"{ns}:{val}" if ns not in ("", "OPEN") else val,
                "deletable": ns == "OPEN",
            }
        )
    return out


def _render_detail(
    request: Request,
    ref: Any,
    *,
    triage: bool = False,
    prefill: dict[str, Any] | None = None,
    triage_msg: str = "",
    cited: dict[str, Any] | None = None,
    initial_tab: str = "",
    meta_panel: str | None = None,
    list_url: str | None = None,
    list_label: str | None = None,
    extra: dict[str, Any] | None = None,
    template: str = "papers/detail.html.j2",
    in_pane: bool = False,
) -> HTMLResponse:
    """Render the paper detail page. Shared by ``detail`` and the triage
    lookup so an S2 result can re-render the page with the edit form
    pre-filled (``prefill``) without duplicating the context build.

    ``initial_tab`` (``Navigate`` / ``Jump`` / ``Meta``) seeds the sidebar
    tab; a triaged paper defaults to ``Meta`` (where the triage panel +
    edit form live) so the queue opens straight onto the metadata it needs.

    A paper-family sibling (``cfp`` / ``datasheet``) can override the Meta tab
    with its own panel: ``meta_panel`` swaps ``doc.meta_panel`` and ``extra``
    merges kind-specific vars into the template context, while everything else
    (the shared reader shell, the ``/papers/{id}/…`` sidebar endpoints) is
    reused. ``list_url`` / ``list_label`` retarget the "back to list" link.
    """
    store = get_store(request)
    cfg = get_web_config(request)
    ref_id = ref.id
    cite_key = ref.slug or ""
    pdf_keys = _ref_pdf_keys(store, ref)
    found = _resolve_pdf_for_ref(store, cfg.corpus_dirs, ref)
    paper = _paper_row(ref)
    ref_idents = store.identifiers_for_refs([ref_id]).get(ref_id, {})
    paper["links"] = _links_from_ids(ref_idents)
    meta = ref.meta or {}
    entry_type = str(meta.get("entry_type") or "").strip()
    has_triage = triage or store.has_tag(ref_id, "OPEN", _TRIAGE_TAG)
    verified_at = getattr(ref, "human_verified_at", None)
    stamps = store.ingest_timestamps(ref_id)
    n_chunks = store.chunks.count_chunks(ref_id)
    tags = _detail_tags(store, ref_id)
    initial_tab = initial_tab or ("Meta" if has_triage else "Navigate")
    # Suggest a real cite_key from the (fixed) author + year. Pre-fill the
    # field with it only when the current handle is the anon placeholder —
    # otherwise default to the existing handle so a save is a no-op unless
    # the operator opts in. The suggestion is always shown as a hint.
    suggested_slug = _suggest_slug(store, ref, prefill)
    if suggested_slug and (not cite_key or cite_key.startswith("anon")):
        slug_default = suggested_slug
    else:
        slug_default = cite_key
    context: dict[str, Any] = {
        "active_tab": "papers",
        "paper": paper,
        # Universal handle (``pa<ref_id>`` / ``cf<ref_id>``) —
        # the address that works regardless of whether a slug is minted.
        "pa_handle": format_handle(ref.kind, ref_id),
        "handle": cite_key or str(ref_id),
        "n_chunks": n_chunks,
        "tags": tags,
        "authors_display": _authors_str(ref),
        "author_lines": _author_edit_lines(ref),
        "abstract": _abstract_full(ref),
        "ingested": stamps,
        "pdf_on_disk": found is not None,
        # Diagnostics for the "file expected but missing" case (a
        # held paper whose corpus roots / mount are misconfigured,
        # or a paper with no cite_key to address the file by): list
        # every path we tried so it's self-diagnosing.
        "cite_key": cite_key,
        "pdf_lookup_paths": [
            str(p) for p in _pdf_candidates(cfg.corpus_dirs, pdf_keys)
        ],
        "corpus_dirs": [str(p) for p in cfg.corpus_dirs],
        # Triage panel state (paste-title -> S2 lookup -> pre-filled edit).
        "is_triage": has_triage,
        "prefill": prefill,
        "triage_msg": triage_msg,
        # "Reviewed" sign-off (refs.human_verified_at/by) — the Meta tab's
        # mark-reviewed / undo row.
        "is_reviewed": bool(verified_at),
        "reviewed_at": verified_at.strftime("%Y-%m-%d") if verified_at else "",
        "reviewed_by": getattr(ref, "human_verified_by", None) or "",
        # "Can't get it" — the acquirability FACT declared on this paper
        # (meta.unacquirable_override {note,by,at}, no mode). Set from the
        # Meta tab; read through by taproot.trust to harden a clean claim
        # hub grounded only on unacquirable papers, and to enrich an
        # unverified lifecycle finding's note — never to soften a claim to
        # Ⓐ/✍ (that's a separate, claim-level declaration). ``None`` when
        # unset.
        "unacquirable": meta.get("unacquirable_override"),
        # Journal + document type (paper_meta_enrich / manual edit —
        # meta.journal, meta.issn, meta.entry_type, Crossref vocabulary
        # verbatim). ``entry_type_label`` renders "Journal article" for a
        # known select value, else the raw string for an out-of-vocabulary
        # Crossref type. Both editable via the Meta tab form.
        "journal": meta.get("journal") or "",
        "issn": meta.get("issn") or "",
        "entry_type": entry_type,
        "entry_type_label": ENTRY_TYPE_LABELS.get(entry_type, entry_type),
        "entry_type_choices": ENTRY_TYPE_CHOICES,
        # Whether the current entry_type is one of the form's <select>
        # options — drives which control (select vs free-text escape) the
        # edit form's Alpine state opens on.
        "entry_type_is_known": (not entry_type) or entry_type in ENTRY_TYPE_LABELS,
        # Retraction banner (display-only — see _retraction_notice).
        "retraction": _retraction_notice(ref),
        # Identifiers beyond DOI/arXiv (PubMed/OpenAlex/S2/MAG/DBLP),
        # linked where a canonical URL exists.
        "extra_identifiers": _extra_identifiers(ref_idents),
        # S2 enrich leftovers (stub_rank mint-time pass / paper_meta_enrich):
        # citation count + field-of-study classification, plus the
        # discovery-layer per-chunk keyword rollup.
        "s2_citation_count": meta.get("s2_citation_count"),
        "s2_fields": (
            meta.get("s2_fields") if isinstance(meta.get("s2_fields"), list) else []
        ),
        # paper_rank pass (workers/paper_rank.py, console-gated default-OFF):
        # query-independent 0-100 reading-priority composite; absent until
        # the pass has scored this paper.
        "read_first": (
            meta["paper_rank"].get("read_first")
            if isinstance(meta.get("paper_rank"), dict)
            else None
        ),
        "keywords": (
            meta.get("keywords") if isinstance(meta.get("keywords"), list) else []
        ),
        # "Who links here" — held incoming edges (drafts / findings / dreams /
        # papers) into this ref, grouped by (kind, relation), each clickable to
        # the source's canonical page. Read-only over the ``links`` reverse
        # index; rendered on the Meta tab.
        "backlinks": _backlinks(store, ref_id),
        # Editable short handle (cite_key) + a free suggestion.
        "slug_default": slug_default,
        "suggested_slug": suggested_slug,
        # Cited passage (from a ``?chunk=N`` citation click) — rendered
        # as a highlighted card so the reader lands on "the relevant
        # thing", with a PDF-page link for the full context.
        "cited": cited,
        # Sidebar tab to open on (Navigate / Jump / Meta); a ?chunk
        # citation still wins in the client (forces Jump).
        "initial_tab": initial_tab,
        # Neutral shell context consumed by the shared reader
        # (_reader/reader.html.j2); the paper-specific Meta tab is
        # plugged via ``doc.meta_panel`` and still reads the vars above.
        "doc": {
            "id": ref_id,
            "title": paper["title"],
            "handle": format_handle(ref.kind, ref_id),
            "slug": cite_key,
            # Straight into Drive scoped to papers (skips the retired
            # ``/papers`` list's redirect hop).
            "list_url": "/drive?k=paper&folder=*&sort=recency",
            "list_label": "papers",
            "n_chunks": n_chunks,
            "pdf_on_disk": found is not None,
            "has_pdf": bool(paper.get("has_pdf")),
            "cited_ord": cited["ord"] if cited else -1,
            "initial_tab": initial_tab,
            "pdf_url": f"/papers/{ref_id}/pdf",
            "meta_panel": "papers/_meta_panel.html.j2",
            # Sources/Cited tabs (S2 bibliography + citing papers) are
            # paper-only — cfp/pres/datasheet siblings share this reader
            # shell but have no S2 neighbour graph (``refs_panel``/
            # ``fetch_ref`` 404 a non-paper ref_id too).
            "show_refs_tabs": ref.kind == "paper",
            "cite_key": cite_key,
            "pdf_lookup_paths": [
                str(p) for p in _pdf_candidates(cfg.corpus_dirs, pdf_keys)
            ],
            "corpus_dirs": [str(p) for p in cfg.corpus_dirs],
        },
    }
    # Paper-family override hook: a cfp/datasheet reader swaps the Meta panel
    # + retargets the list link, and merges its own kind-specific vars, while
    # reusing the whole reader shell + sidebar endpoints.
    doc = context["doc"]
    if meta_panel is not None:
        doc["meta_panel"] = meta_panel
    if list_url is not None:
        doc["list_url"] = list_url
    if list_label is not None:
        doc["list_label"] = list_label
    if extra:
        context.update(extra)
    # In a workbench pane the reader is not a page: it has no site header to
    # subtract from the viewport, and it fills its container instead.
    doc["in_pane"] = in_pane
    return templates.TemplateResponse(request, template, context)


#: The skills plugin group compound-handle chunk selector: an optional ``pa<ref_id>~``
#: prefix (the TOC's own display form, e.g. ``pa1483~13..15``) followed by
#: a bare ord or an inclusive ``lo..hi`` range. Shared by ``?chunk=``, the
#: Jump box, and TOC clicks — all funnel through :func:`_cited_chunk`.
_SEL_RE = re.compile(r"^(?:pa(\d+)~)?(\d+)(?:\.\.\d+)?$")


def _cited_chunk(
    store: PoolStore, ref_id: int, chunk: str | None
) -> dict[str, Any] | None:
    """Resolve a chunk selector — ``N``, ``N..M``, or the compound
    ``pa<ref_id>~N[..M]`` handle the TOC displays — to the cited chunk's
    verbatim text + PDF page, for the highlighted "cited passage" card. A
    range uses its low end. ``pN`` page-jumps, missing chunks, and a
    compound handle naming a *different* paper (never resolve into
    another ref's chunk under this ``ref_id``'s URL) all return ``None``."""
    if not chunk:
        return None
    m = _SEL_RE.match(chunk)
    if m is None:  # e.g. ``p23`` — a page jump, no chunk text
        return None
    pa_id, lo = m.group(1), m.group(2)
    if pa_id is not None and int(pa_id) != ref_id:
        return None
    ord_ = int(lo)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id, text, page_first FROM chunks WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_),
        ).fetchone()
    if row is None or not row[1]:
        return None
    # ``pc<chunk_id>`` — the universal chunk handle, so the Jump
    # card can show the durable pointer alongside the per-paper ``ord`` (a
    # citation / short-code the reader can copy, not just "chunk N").
    return {
        "ord": ord_,
        "text": row[1],
        "page": row[2],
        "handle": format_handle("paper", row[0], chunk=True),
    }


#: The document family that shares the two-pane reader (proposal
#: writing). The ``ref_id``-scoped sidebar endpoints (search / toc /
#: chunk / pdf) are kind-agnostic, so they accept any family member; the
#: ``cfp``, ``pres`` and ``datasheet`` readers reuse them. The slug-detail
#: routes pass their own kind. (``pres`` joins so the /pres slide-deck editor
#: gets the same in-doc search / TOC / chunk-summary / jump-to-page sidebar;
#: ``datasheet`` joins so /datasheets reuses the paper reader verbatim.)
_DOC_FAMILY: tuple[str, ...] = ("paper", "cfp", "pres", "datasheet")


def _resolve_paper(
    store: Store, ident: str, *, kinds: tuple[str, ...] = ("paper",)
) -> Any | None:
    """Resolve a path ``ident`` (numeric id *or* cite_key slug) to a ref
    in the document family ``kinds``.

    All-digit idents are treated as the numeric ref id (back-compat with
    the old ``/papers/<id>`` URLs + ``?chunk=`` citation deep links); any
    other ident is a slug, resolved through ``ref_identifiers`` under
    ``kinds[0]``. Returns ``None`` when nothing live with a kind in
    ``kinds`` matches. ``kinds`` defaults to paper-only; the ref_id
    sidebar endpoints pass ``_DOC_FAMILY`` so a ``cfp`` reader reuses
    them, and the ``/cfp`` detail route passes ``("cfp",)``.
    """
    if ident.isdigit():
        ref = store.fetch_refs_by_ids([int(ident)], include_deleted=False).get(
            int(ident)
        )
    else:
        ids = store.fetch_ref_ids_by_slugs([ident], kind=kinds[0])
        ref = (
            store.fetch_refs_by_ids(ids, include_deleted=False).get(ids[0])
            if ids
            else None
        )
    if ref is None or ref.kind not in kinds:
        return None
    return ref


#: Cap on sidebar-nav result rows (semantic / keyword) — enough to scan,
#: bounded so a broad query doesn't return the whole paper.
_NAV_LIMIT = 80

#: Per-result snippet length shown in the sidebar (chars).
_NAV_SNIPPET = 320


def _nav_snippet(text: str) -> str:
    """Collapse a chunk's text to a bounded single-paragraph preview."""
    s = _WS_RE.sub(" ", text or "").strip()
    if len(s) > _NAV_SNIPPET:
        s = s[:_NAV_SNIPPET].rstrip() + "…"
    return s


@router.get("/{ident}", response_class=HTMLResponse, response_model=None)
async def detail(
    request: Request,
    ident: str,
    triage: int = 0,
    chunk: str | None = None,
    tab: str = "",
) -> HTMLResponse | RedirectResponse:
    """Paper detail: metadata sidebar + PDF.js reader. ``?chunk=N`` (a
    citation click) surfaces that chunk's text as a highlighted card.
    ``?tab=Meta`` (or Navigate / Jump) opens that sidebar tab.

    Addressable by cite_key slug (canonical) or numeric id; a numeric id
    that owns a slug 301-redirects to the slug URL so links settle on the
    stable handle while old ``/papers/<id>`` deep links keep working.
    """
    store = get_store(request)
    ref = _resolve_paper(store, ident)
    if ref is None:
        raise NotFound(f"paper {ident!r} not found")
    if ident.isdigit() and ref.slug:
        target = f"/papers/{ref.slug}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=301)
    # A human opened this document — heat its body chunks so it rises for the
    # summarize hot tier (summarise it first) and the dreamer. Best-effort:
    # a bump failure must never 500 the reader. Skipped on the id→slug
    # redirect above (the follow-up slug request does the bump).
    try:
        store.chunks.bump_salience_for_ref(ref.id)
        # Also stamp refs.last_viewed_at — a clean, search-hit-free open signal
        # (chunks.last_seen is bumped by search too) that the reading-brief's
        # "reading" lane can migrate onto once enough history has accumulated.
        store.touch_viewed(ref.id)
    except Exception:
        pass
    # htmx-aware (the ``flags.py`` pattern) — see the twin branch in
    # ``claim.py::claim_view`` for why this keys off the header and not a
    # separate /fragment URL: /c/<handle> and /r/paper/<id> 303 into here,
    # and HX-Request survives a same-origin redirect.
    in_pane = request.headers.get("HX-Request") == "true"
    return _render_detail(
        request,
        ref,
        triage=bool(triage),
        cited=_cited_chunk(store, ref.id, chunk),
        initial_tab=tab.strip().capitalize(),
        template="_reader/reader.html.j2" if in_pane else "papers/detail.html.j2",
        in_pane=in_pane,
    )


@router.get("/{ref_id}/search")
async def search_in_paper(
    request: Request, ref_id: int, q: str = "", mode: str = "semantic"
) -> JSONResponse:
    """Sidebar nav search, scoped to this paper's body chunks.

    Returns ranked ``{ord, page, text, keywords, score}`` rows (plain
    text for display; ``page`` drives the PDF jump):

    * ``mode='semantic'`` — cosine-ranked over the paper's chunk
      embeddings. Degrades to ``keyword`` when the embedder is down or
      the query won't embed (the search-embed guard).
    * ``mode='keyword'`` — lexical (``chunks.tsv`` / ``ts_rank_cd``).
    """
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    q = q.strip()
    if ref is None or not q:
        return JSONResponse({"results": [], "mode": mode})

    m = (mode or "semantic").strip().lower()
    hits: list[Any] = []
    if m == "semantic":
        hub = getattr(get_runtime(request), "hub", None)
        embedder = getattr(hub, "embedder", None)
        vec = embed_query(embedder, q)
        if vec is not None:
            hits = store.chunks.search_chunks_semantic(
                query_vec=vec, scope_ref_id=ref.id, limit=_NAV_LIMIT, max_distance=None
            )
        else:
            m = "keyword"  # embedder down → degrade to a lexical find
    if m != "semantic":
        m = "keyword"
        hits = store.chunks.search_chunks_lexical(
            q=q, scope_ref_id=ref.id, limit=_NAV_LIMIT
        )

    ords = [b.ord for b, _r, _s in hits]
    pages = store.chunks.chunk_pages(ref.id, ords)
    # llm-v1 summary per hit for the Semantic-mode row (falls back to the
    # keyword chips client-side when a chunk hasn't been summarised yet).
    summaries = store.chunks.chunk_summaries_for(ref.id, ords)
    is_sem = m == "semantic"
    results = [
        {
            "ord": b.ord,
            "page": pages.get(b.ord),
            "text": _nav_snippet(b.text),
            "summary": summaries.get(b.ord, ""),
            "keywords": b.keywords or [],
            # Semantic: report cosine *similarity* (1 - distance, higher =
            # better) so the best-first ordering reads naturally; keyword:
            # the ts_rank. Either way the list is already returned best-first.
            "score": round(1.0 - float(score), 3) if is_sem else round(float(score), 4),
        }
        for b, _r, score in hits
    ]
    return JSONResponse({"results": results, "mode": m})


def _parse_scope(lo: int | None, hi: int | None) -> tuple[int, int] | None:
    """Coerce optional ``?lo=&hi=`` drill-down bounds into a scope tuple.

    Both must be present and ordered for a scope; anything else (one
    side missing, inverted) means "no scope — cluster the whole body".
    """
    if lo is None or hi is None or lo > hi:
        return None
    return (lo, hi)


@router.get("/{ref_id}/toc")
async def toc_in_paper(
    request: Request, ref_id: int, lo: int | None = None, hi: int | None = None
) -> JSONResponse:
    """Clickable TOC: keyword-clustered segments of this paper's body.

    Each segment carries its ``handle``, chunk range, label keywords,
    and the PDF ``page`` of its first chunk (for the viewer jump).

    ``?lo=&hi=`` restricts the clustering to an ord sub-range — the
    drill-down path: double-clicking a fat cluster re-clusters just that
    range (papers have no heading tree, so hierarchy comes from recursive
    keyword clustering). Without both bounds the full body
    is clustered.
    """
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    if ref is None:
        return JSONResponse({"segments": []})
    # prefix each segment with the universal record handle
    # (``pa<id>`` / ``cf<id>``) so the web row mirrors the agent get id.
    handle = format_handle(ref.kind, ref.id)
    scope = _parse_scope(lo, hi)
    segments = build_toc_segments(
        store=store, ref_id=ref.id, handle=handle, scope=scope
    )
    pages = store.chunks.chunk_pages(ref.id, [seg["lo"] for seg in segments])
    for seg in segments:
        seg["page"] = pages.get(seg["lo"])
    return JSONResponse({"segments": segments})


@router.get("/{ref_id}/chunks")
async def chunks_in_paper(request: Request, ref_id: int) -> JSONResponse:
    """Per-chunk summary list for the sidebar's rapid-nav (Semantic/Keyword).

    Returns every body chunk in reading order as
    ``{ord, page, summary, keywords}`` — the empty-query state of the
    Semantic and Keyword modes, a scannable outline the operator clicks
    to jump the viewer. ``summary`` is the ``llm-v1`` summary (often empty —
    the summariser is a trickle), ``keywords`` the KeyBERT terms.
    """
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    if ref is None:
        return JSONResponse({"chunks": []})
    return JSONResponse({"chunks": store.chunks.chunk_llm_summaries_for_ref(ref.id)})


@router.get("/{ref_id}/rawchunks")
async def raw_chunks_in_paper(request: Request, ref_id: int) -> JSONResponse:
    """Verbatim chunk-text listing for the sidebar's "Raw" tab.

    Returns every body chunk in reading order as
    ``{ord, page, chunk_kind, text}``. For a chunks-only ingest with no
    PDF on disk (e.g. ``heminamino26``) this is the only way to read the
    source text in the UI — Semantic/Keyword/TOC all key off a summary or
    cluster, never the verbatim body. Reuses ``list_chunks_for_ref`` (the
    same store helper the MCP chunk-range reader calls) rather than a new
    chunk-listing query.
    """
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    if ref is None:
        return JSONResponse({"chunks": []})
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    pages = store.chunks.chunk_pages(ref.id, [b.ord for b in blocks])
    return JSONResponse(
        {
            "chunks": [
                {
                    "ord": b.ord,
                    "page": pages.get(b.ord),
                    "chunk_kind": b.chunk_kind,
                    "text": b.text,
                }
                for b in blocks
            ]
        }
    )


@router.get("/{ref_id}/chunk/{sel}")
async def chunk_in_paper(request: Request, ref_id: int, sel: str) -> JSONResponse:
    """Resolve a chunk selector → ``{ord, page, text}`` for the sidebar
    "jump to chunk" affordance. ``sel`` accepts a bare ord, an ``lo..hi``
    range (low end wins), or the compound span handle the TOC itself
    displays (``pa<ref_id>~lo..hi``) — anything else, including a compound
    handle naming a different paper, 404s-as-empty (see ``_cited_chunk``,
    which every one of these forms funnels through)."""
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    if ref is None:
        return JSONResponse({"chunk": None})
    cited = _cited_chunk(store, ref.id, sel)
    return JSONResponse({"chunk": cited})


# ── Sources / Cited tabs (S2 neighbour graph, per paper) ───────────────
#
# The bibliography a paper cites ("sources") and the papers that cite it
# ("cited") — S2's citation graph, persisted per-paper into
# ``s2_neighbors`` (migration 0106) so the reader can render a full tab
# without minting a ref per reference. A held neighbour (``held_ref_id``
# set) links straight into the corpus; a non-held one shows title/year +
# off-site links + a Fetch button when it carries an identifier to fetch
# by.

#: The two valid ``{ref_id}/refs/{direction}`` path values — web-facing
#: names, distinct from the ``s2_neighbors.direction`` DB values
#: (``cites`` / ``cited_by``) :func:`_sources_rows` / :func:`_cited_rows`
#: query directly.
_REFS_DIRECTIONS: frozenset[str] = frozenset({"sources", "cited"})


def _refs_row(
    *,
    ref_id: int,
    direction: str,
    n: int | None,
    held_ref: Any | None,
    s2_id: str | None,
    doi: str | None,
    title: str | None,
    year: int | None,
    marker: int | None = None,
    raw_text: str | None = None,
) -> dict[str, Any]:
    """One Sources/Cited row. ``ref_id``/``direction`` identify the *owning*
    paper (this reader's own ref, not the neighbour) — carried on the row
    itself so the fetch-ref endpoint's single-row htmx response is
    self-contained and needs no outer template context.

    ``marker``/``raw_text`` are citation-sources-tab additions (Sources
    tab only — ``_cited_rows`` never passes them, so a Cited row's
    rendering is untouched): ``marker`` is the real bibliography number
    from a matched ``paper_bib_entries`` row, rendered bracket-styled
    (``[N]``) in place of the positional ``n.`` badge; ``raw_text`` is the
    verbatim parsed citation line for a union row (a bib entry with no
    matching S2/held row) that has no ``title`` to fall back on."""
    base: dict[str, Any] = {
        "ref_id": ref_id,
        "direction": direction,
        "n": n,
        "marker": marker,
        "raw_text": raw_text,
        "s2_id": s2_id or None,
        "doi": doi or None,
        "year": year,
    }
    if held_ref is not None:
        base.update(
            {
                "held": True,
                "url": f"/papers/{held_ref.slug or held_ref.id}",
                "title": held_ref.title or "(untitled)",
                "year": held_ref.year,
            }
        )
        return base
    base.update(
        {
            "held": False,
            # A union row (raw_text set, no title field on paper_bib_entries)
            # has nothing to fall back to — leave title unset rather than
            # stamping a spurious "(untitled)" the raw_text branch never
            # displays anyway (it'd otherwise leak into the row's own
            # hidden Fetch-form ``title`` field).
            "title": title or (None if raw_text else "(untitled)"),
            "s2_url": (
                f"https://www.semanticscholar.org/paper/{s2_id}" if s2_id else ""
            ),
            "doi_url": doi_url(doi) if doi else "",
            "scholar_url": scholar_title_url(title) if title else "",
            # The row's own Fetch button — only when there's something to
            # fetch by (a bare title-only neighbour is links-only).
            "can_fetch": bool(s2_id or doi),
        }
    )
    return base


class _BibEntryIndex:
    """Lookup structure over a paper's ``paper_bib_entries`` rows
    (citation-sources-tab) for matching them onto ``s2_neighbors`` / held
    ``cites`` rows — first match wins, in the order ``held_ref_id`` →
    ``doi`` → ``s2_id`` (the proposal's join order). Built once per
    :func:`_sources_rows` call; first-occurrence-wins on a duplicate key
    (deterministic, mirrors the ``bib_parse`` worker's own dedup
    discipline) since a real bibliography shouldn't produce one anyway.

    ``match()`` is first-match-wins **per entry, across calls**: once an
    entry has been attached to one row it's consumed and every later call
    skips it (even via a different key) — without this, stale/duplicate S2
    data (two neighbour rows sharing a DOI/held paper) or one row matching
    via ``held_ref_id`` and another via ``doi`` to the *same* bib entry
    would attach one marker to two display rows, showing the same
    ``[N]`` badge twice."""

    def __init__(self, entries: list[BibEntry]) -> None:
        self._by_held: dict[int, BibEntry] = {}
        self._by_doi: dict[str, BibEntry] = {}
        self._by_s2: dict[str, BibEntry] = {}
        self._consumed: set[int] = set()
        for be in entries:
            if be.held_ref_id is not None:
                self._by_held.setdefault(be.held_ref_id, be)
            nd = normalize_doi(be.doi)
            if nd is not None:
                self._by_doi.setdefault(nd, be)
            if be.s2_id:
                self._by_s2.setdefault(be.s2_id, be)

    def _available(self, be: BibEntry | None) -> BibEntry | None:
        if be is None or be.marker in self._consumed:
            return None
        return be

    def match(
        self,
        *,
        held_ref_id: int | None,
        doi: str | None = None,
        s2_id: str | None = None,
    ) -> BibEntry | None:
        be = None
        if held_ref_id is not None:
            be = self._available(self._by_held.get(held_ref_id))
        if be is None:
            nd = normalize_doi(doi)
            if nd is not None:
                be = self._available(self._by_doi.get(nd))
        if be is None and s2_id:
            be = self._available(self._by_s2.get(s2_id))
        if be is not None:
            self._consumed.add(be.marker)
        return be


def _sources_rows(store: Store, ref: Any) -> list[dict[str, Any]]:
    """This paper's outgoing bibliography — our merged view (citation-
    sources-tab): the real bibliography marker from ``paper_bib_entries``
    replaces the positional index on any S2/held row it matches (bracket-
    styled, ``[N]``), and an unmatched parsed entry is unioned in as a
    first-class row showing its ``raw_text`` verbatim. Three ordering
    buckets, concatenated: rows with a real marker (sorted by marker) →
    unmatched S2 rows (S2 order, today's positional ``n.`` badge) →
    unmatched held-but-not-in-S2 rows (today's placement, appended last).
    A paper with no ``paper_bib_entries`` rows renders byte-identically to
    the pre-citation-sources-tab behaviour (empty index → nothing ever
    matches → same two buckets, same order, same badges)."""
    held_links = store.links_for(ref.id, direction="out", relation="cites")
    cited_ids = {lk.dst_ref_id for lk in held_links}
    neighbors = store.list_s2_neighbors(ref.id, "cites")
    bib_entries = store.list_bib_entries(ref.id)
    bib_index = _BibEntryIndex(bib_entries)
    want_ids = (
        cited_ids
        | {nb.held_ref_id for nb in neighbors if nb.held_ref_id}
        | {be.held_ref_id for be in bib_entries if be.held_ref_id}
    )
    refs = store.fetch_refs_by_ids(list(want_ids)) if want_ids else {}
    remaining = {
        rid
        for rid in cited_ids
        if refs.get(rid) is not None and refs[rid].kind == "paper"
    }

    marker_bucket: list[tuple[int, dict[str, Any]]] = []
    s2_bucket: list[dict[str, Any]] = []
    held_bucket: list[dict[str, Any]] = []
    matched_markers: set[int] = set()

    for i, nb in enumerate(neighbors, start=1):
        held_ref = refs.get(nb.held_ref_id) if nb.held_ref_id else None
        if held_ref is not None:
            remaining.discard(nb.held_ref_id)
        be = bib_index.match(held_ref_id=nb.held_ref_id, doi=nb.doi, s2_id=nb.s2_id)
        row = _refs_row(
            ref_id=ref.id,
            direction="sources",
            n=i,
            held_ref=held_ref,
            s2_id=nb.s2_id,
            doi=nb.doi,
            title=nb.title,
            year=nb.year,
            marker=be.marker if be is not None else None,
        )
        if be is not None:
            matched_markers.add(be.marker)
            marker_bucket.append((be.marker, row))
        else:
            s2_bucket.append(row)

    # Held cited papers the S2 list doesn't carry (elided or stale) — a
    # held row matched via held_ref_id joins the marker bucket; otherwise
    # it keeps today's unnumbered, appended-last placement.
    for rid in sorted(remaining):
        be = bib_index.match(held_ref_id=rid)
        row = _refs_row(
            ref_id=ref.id,
            direction="sources",
            n=None,
            held_ref=refs[rid],
            s2_id=None,
            doi=None,
            title=None,
            year=None,
            marker=be.marker if be is not None else None,
        )
        if be is not None:
            matched_markers.add(be.marker)
            marker_bucket.append((be.marker, row))
        else:
            held_bucket.append(row)

    # Parsed entries with no S2/held row at all — unioned in as first-class
    # rows (marker + verbatim raw_text line); never also shown twice (a
    # marker consumed by a match above is skipped here).
    for be in bib_entries:
        if be.marker in matched_markers:
            continue
        held_ref = refs.get(be.held_ref_id) if be.held_ref_id else None
        row = _refs_row(
            ref_id=ref.id,
            direction="sources",
            n=None,
            held_ref=held_ref,
            s2_id=be.s2_id,
            doi=be.doi,
            title=None,
            year=be.year,
            marker=be.marker,
            raw_text=be.raw_text,
        )
        marker_bucket.append((be.marker, row))

    marker_bucket.sort(key=lambda pair: pair[0])
    return [row for _, row in marker_bucket] + s2_bucket + held_bucket


def _cited_rows(store: Store, ref: Any) -> list[dict[str, Any]]:
    """Papers citing this one: held incoming ``cites`` links unioned with
    the S2 ``cited_by`` neighbour list, deduped on the held ref — a held
    citer appearing in both shows once, as held."""
    held_links = store.links_for(ref.id, direction="in", relation="cites")
    citer_ids = {lk.src_ref_id for lk in held_links}
    neighbors = store.list_s2_neighbors(ref.id, "cited_by")
    want_ids = citer_ids | {nb.held_ref_id for nb in neighbors if nb.held_ref_id}
    refs = store.fetch_refs_by_ids(list(want_ids)) if want_ids else {}
    # Paper-only, live-only — links_for can in principle surface a
    # non-paper or soft-deleted citer; the tab is a paper bibliography view.
    remaining = {
        rid
        for rid in citer_ids
        if refs.get(rid) is not None and refs[rid].kind == "paper"
    }
    rows: list[dict[str, Any]] = []
    for nb in neighbors:
        held_ref = refs.get(nb.held_ref_id) if nb.held_ref_id else None
        if held_ref is not None:
            remaining.discard(nb.held_ref_id)
        rows.append(
            _refs_row(
                ref_id=ref.id,
                direction="cited",
                n=None,
                held_ref=held_ref,
                s2_id=nb.s2_id,
                doi=nb.doi,
                title=nb.title,
                year=nb.year,
            )
        )
    # Held citers S2's cited_by list doesn't (yet) carry — still real
    # citations, so they're shown too, just without off-site metadata
    # (a held row needs none — it links straight into the corpus).
    for rid in sorted(remaining):
        rows.append(
            _refs_row(
                ref_id=ref.id,
                direction="cited",
                n=None,
                held_ref=refs[rid],
                s2_id=None,
                doi=None,
                title=None,
                year=None,
            )
        )
    return rows


@router.get(
    "/{ref_id}/refs/{direction}", response_class=HTMLResponse, response_model=None
)
async def refs_panel(request: Request, ref_id: int, direction: str) -> HTMLResponse:
    """Sources/Cited tab fragment — lazily loaded on first tab open
    (``paper-viewer.js``'s ``setTab``). ``direction`` is ``sources`` (this
    paper's outgoing bibliography) or ``cited`` (incoming citations).

    :func:`ensure_s2_neighbors` backfills an old paper's S2 neighbour list
    inline on first view — this may hit the network the very first time a
    reader opens either tab; every call inside the 30-day TTL after that is
    a cheap presence check + a pure-SQL read. Run off the event loop
    (worker thread) since the network call, when it fires, must not freeze
    other requests.

    Paper-only: the Sources/Cited tabs read the S2 neighbour graph, which
    only a ``paper`` carries — a ``cfp``/``pres``/``datasheet`` sibling
    (also in ``_DOC_FAMILY``, for the shared reader shell's other sidebar
    endpoints) 404s here *before* the S2 backfill, same as ``reviewed``'s
    owning-ref guard.
    """
    if direction not in _REFS_DIRECTIONS:
        raise NotFound(f"unknown refs direction {direction!r}")
    store = get_store(request)
    ref = _resolve_paper(store, str(ref_id), kinds=_DOC_FAMILY)
    if ref is None:
        raise NotFound(f"paper id={ref_id} not found")
    if ref.kind != "paper":
        return _paper_error(request, "Refs error", f"paper id={ref_id} not found", 404)
    await asyncio.to_thread(ensure_s2_neighbors, store, ref.id)
    rows = (
        _sources_rows(store, ref) if direction == "sources" else _cited_rows(store, ref)
    )
    return templates.TemplateResponse(
        request,
        "papers/_refs_panel.html.j2",
        {"ref": ref, "direction": direction, "rows": rows},
    )


@router.post("/{ref_id}/fetch-ref", response_model=None)
async def fetch_ref(
    request: Request,
    ref_id: int,
    doi: str = Form(""),
    s2_id: str = Form(""),
    title: str = Form(""),
    year: str = Form(""),
    n: str = Form(""),
    marker: str = Form(""),
    raw_text: str = Form(""),
    direction: str = Form("sources"),
    return_to: str = Form(""),
) -> Response:
    """Mint-or-reuse a fetchable stub for one Sources/Cited row (the
    single-paper sibling of batch ``/drive/requeue-stubs``). ``doi=``/
    ``s2_id=`` are whatever the row carries (:func:`_refs_row`'s
    ``can_fetch`` gates rendering the button); at least one required.

    Dispatches ``put(kind='paper', identifier=…)`` — same door as MCP's
    ``put(kind='paper', doi=…)`` — so vocabulary/tree-guard validation and
    S2-enrich → ``upsert_stub_paper`` idempotency stay single-sourced
    (``PaperHandler.acquire``). ``Response.ref_id`` is in-process-only;
    a dispatched web write re-reads the minted/reused id via
    :meth:`Store.find_ref_by_identifier`. ``verify=False``: identifier
    already came from a live S2 fetch, so acquire's hallucination guard
    would be a redundant round-trip.

    On success: :meth:`Store.requeue_stubs_for_fetch` (scoped to this
    ref) jumps the ``fetch_oa`` queue; :meth:`Store.update_s2_neighbor_held`
    stamps this row (+ its mirror direction) held/queued immediately,
    without waiting for the next ``citation_recall`` refresh.

    htmx-aware (``flags.py`` pattern): ``HX-Request`` → re-rendered row
    (``hx-target="closest .refs-row"``); plain form POST → 303 to the tab.

    ``marker=``/``raw_text=`` round-trip a matched/union row's bracket
    marker + verbatim citation line through the swap — a union row has no
    ``title`` and would otherwise render "(untitled)"; a matched row would
    downgrade its ``[N]`` badge to positional."""
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return _paper_error(request, "Fetch error", f"paper id={ref_id} not found", 404)
    doi = doi.strip().lower()
    s2_id = s2_id.strip()
    title = title.strip()
    raw_text = raw_text.strip()
    yr: int | None = int(year) if year.strip().lstrip("-").isdigit() else None
    n_val: int | None = int(n) if n.strip().isdigit() else None
    marker_val: int | None = int(marker) if marker.strip().isdigit() else None
    if direction not in _REFS_DIRECTIONS:
        direction = "sources"
    if not doi and not s2_id:
        return _paper_error(
            request,
            "Fetch error",
            "fetch-ref requires a doi or s2_id — this row carries neither",
            400,
        )
    payload: dict[str, Any] = {
        "kind": "paper",
        "identifier": f"doi:{doi}" if doi else f"s2:{s2_id}",
        "verify": False,
    }
    if title:
        payload["title"] = title
    if yr is not None:
        payload["year"] = yr
    body, is_error = await await_dispatch(request, "put", payload)
    if is_error:
        return _paper_error(request, "Fetch error", body, 400)

    scheme, value = ("doi", doi) if doi else ("s2", s2_id)
    new_ref_id = store.find_ref_by_identifier(scheme, value, kind="paper")
    held_ref = None
    if new_ref_id is not None:
        await asyncio.to_thread(
            store.requeue_stubs_for_fetch,
            ref_ids=[new_ref_id],
            id_kinds=("doi", "arxiv", "s2"),
        )
        await asyncio.to_thread(
            store.update_s2_neighbor_held,
            ref_id,
            new_ref_id,
            s2_id=s2_id or None,
            doi=doi or None,
        )
        held_ref = store.fetch_refs_by_ids([new_ref_id]).get(new_ref_id)

    if request.headers.get("HX-Request") == "true":
        row = _refs_row(
            ref_id=ref_id,
            direction=direction,
            n=n_val,
            held_ref=held_ref,
            s2_id=s2_id or None,
            doi=doi or None,
            title=title or None,
            year=yr,
            marker=marker_val,
            raw_text=raw_text or None,
        )
        return templates.TemplateResponse(
            request, "papers/_refs_row.html.j2", {"row": row}
        )
    tab = "Sources" if direction == "sources" else "Cited"
    fallback = f"/papers/{ref_id}?tab={tab}"
    return RedirectResponse(
        url=_safe_papers_redirect(return_to or fallback), status_code=303
    )


@router.post("/{ref_id}/triage-lookup", response_model=None)
async def triage_lookup(
    request: Request, ref_id: int, title: str = Form("")
) -> HTMLResponse:
    """Look the operator-supplied title up on Semantic Scholar and re-render
    the detail page with the edit form pre-filled from the best match.

    Read-only: it never writes. The operator reviews the candidate and
    commits via the normal Save (the ``edit`` POST), which also clears the
    ``needs-triage`` tag. A miss just re-opens the panel with a message.
    """
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "paper":
        raise NotFound(f"paper id={ref_id} not found")

    query = title.strip()
    if not query:
        return _render_detail(
            request, ref, triage=True, triage_msg="Enter a title to look up."
        )

    from precis.ingest.lookup import lookup_title
    from precis.secrets import get_secret

    result = lookup_title(query, s2_key=get_secret("SEMANTIC_SCHOLAR_API_KEY") or "")
    if not result or not result.get("title"):
        return _render_detail(
            request,
            ref,
            triage=True,
            triage_msg=f"No Semantic Scholar match for {query!r}. "
            "Edit the fields by hand below.",
        )

    # Same Crossref authority overlay as ``s2_prefill`` — the S2 title
    # search just discovered a DOI; the publisher record has the byline
    # S2 mangles (jammed initials, dropped middle names).
    cr_doi = (result.get("doi") or "").strip()
    overlay = _crossref_overlay(cr_doi) if cr_doi else None
    if overlay:
        result = {**result, **overlay}

    names = author_names(result.get("authors") or [])
    prefill = {
        "title": result.get("title", ""),
        "year": result.get("year") or "",
        "doi": result.get("doi") or "",
        "arxiv": result.get("arxiv_id") or "",
        "abstract": result.get("abstract") or "",
        "authors": "\n".join(names),
    }
    return _render_detail(
        request,
        ref,
        triage=True,
        prefill=prefill,
        triage_msg=f"Found on Semantic Scholar: {result['title']!r} — "
        "review and Save to apply (clears needs-triage).",
    )


def _crossref_overlay(doi: str) -> dict[str, Any] | None:
    """Crossref record for *doi*, shaped for the prefill payload —
    ``None`` on any miss/failure so the caller falls back to S2 alone.

    Only truthy fields are returned (the caller dict-merges this **over**
    the S2 result), and the abstract is JATS-stripped — Crossref stores
    abstracts as ``<jats:p>…`` XML, which must not land in a form field.
    Lazy imports: ``habanero`` lives in the ``[paper]`` extra, and a
    venv without it should degrade to the S2-only behaviour, not 500.
    """
    try:
        from precis.handlers._paper_format import _strip_jats
        from precis.ingest.crossref import lookup_crossref
    except ImportError:
        return None
    from precis import settings

    mailto = (settings.get_str("contact.crossref_mailto") or "").strip()
    try:
        result = lookup_crossref(doi, mailto=mailto)
    except Exception:
        return None
    if not result or not result.get("title"):
        return None
    overlay = {k: result.get(k) for k in ("title", "authors", "year", "doi", "journal")}
    abstract = (result.get("abstract") or "").strip()
    if abstract:
        overlay["abstract"] = _strip_jats(abstract)
    return {k: v for k, v in overlay.items() if v}


@router.get("/{ref_id}/s2-prefill", response_model=None)
async def s2_prefill(request: Request, ref_id: int) -> JSONResponse:
    """Fetch bibliographic metadata for this paper so the Meta edit form can
    **fill only its empty fields** (client-side; nothing is persisted until
    the operator Saves). Read-only — never writes.

    Two sources, layered by authority. The S2 cascade runs first — a held
    DOI or arXiv id resolves an exact record, else a title search (same
    path as ``triage-lookup``) — because S2 *discovers* identifiers and
    contributes fields Crossref lacks (abstract as plain text, arXiv id).
    Then, when a DOI is known (held, or just discovered by S2), the
    Crossref record is **overlaid on top**: authors/title/year/journal
    come from the publisher record, which carries the full, consistently
    formatted byline where S2's is jammed initials with dropped middle
    names ("A.K. Geim" / "S. Morozov" for `10.1126/science.1102896`,
    where Crossref has "A. K. Geim" / "S. V. Morozov"). A Crossref miss
    or failure degrades to the S2-only result. Authors come back in
    natural order to match the editor's one-per-line convention
    (``_author_edit_lines``).

    ``journal`` prefills from Crossref's ``container-title`` (or S2's
    ``venue`` when only S2 answered). ``entry_type`` is deliberately NOT
    returned — the client fills a fixed key set and doesn't consume it,
    and S2's value is hardcoded ``"article"`` (docs/backlog/
    paper-meta-surfacing.md)."""
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind not in _DOC_FAMILY:
        raise NotFound(f"document id={ref_id} not found")

    from precis.ingest.lookup import lookup_title
    from precis.ingest.semantic_scholar import get_paper_by_id
    from precis.secrets import get_secret

    s2_key = get_secret("SEMANTIC_SCHOLAR_API_KEY") or ""
    ids = store.identifiers_for_refs([ref_id]).get(ref_id, {})
    doi = (ids.get("doi") or "").strip()
    arxiv = (ids.get("arxiv") or "").strip()
    title = (ref.title or "").strip()

    result: dict[str, Any] | None = None
    s2_failed = False
    try:
        if doi:
            result = get_paper_by_id(f"DOI:{doi}", api_key=s2_key)
        if not result and arxiv:
            result = get_paper_by_id(f"ARXIV:{arxiv}", api_key=s2_key)
        if not result and title:
            # ``lookup_title`` (unlike ``get_paper_by_id``) re-raises after its
            # retry budget, so a down / rate-limited S2 would 500 this endpoint
            # rather than degrade — catch it and fall through: a held DOI can
            # still resolve via the Crossref overlay below.
            result = lookup_title(title, s2_key=s2_key)
    except Exception:
        s2_failed = True
        result = None

    cr_doi = (doi or (result.get("doi") if result else "") or "").strip()
    overlay = _crossref_overlay(cr_doi) if cr_doi else None
    if overlay:
        result = {**(result or {}), **overlay}

    if not result or not result.get("title"):
        if s2_failed:
            return JSONResponse(
                {"ok": False, "message": "Semantic Scholar lookup failed — try again."}
            )
        hint = title or doi or arxiv
        return JSONResponse(
            {
                "ok": False,
                "message": (
                    f"No Semantic Scholar match for {hint!r}."
                    if hint
                    else "Nothing to look up — add a title, DOI, or arXiv id first."
                ),
            }
        )

    names = author_names(result.get("authors") or [])
    return JSONResponse(
        {
            "ok": True,
            "title": result.get("title") or "",
            "year": str(result.get("year") or ""),
            "doi": result.get("doi") or "",
            "arxiv": result.get("arxiv_id") or "",
            "abstract": result.get("abstract") or "",
            "authors": "\n".join(names),
            "journal": result.get("journal") or "",
        }
    )


@router.get("/{ref_id}/pdf")
async def pdf(request: Request, ref_id: int) -> FileResponse:
    """Stream the paper's PDF from ``corpus_dir`` (inline, for the viewer)."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    # Accept any document-family ref (paper or cfp) — both store their
    # PDF in the corpus addressed by cite_key, so the reuse is direct.
    if ref is None or ref.kind not in _DOC_FAMILY:
        raise NotFound(f"document id={ref_id} not found")
    cite_key = ref.slug or ""
    pdf_keys = _ref_pdf_keys(store, ref)
    cfg = get_web_config(request)
    path = _resolve_pdf_for_ref(store, cfg.corpus_dirs, ref)
    if path is None:
        tried = [str(p) for p in _pdf_candidates(cfg.corpus_dirs, pdf_keys)]
        raise NotFound(
            f"no PDF on disk for paper id={ref_id} (cite_key={cite_key!r}, "
            f"aliases={pdf_keys!r}); "
            f"looked at {tried or '(no cite_key to address a file)'}. "
            "If the file exists elsewhere, add its root to PRECIS_CORPUS_DIR "
            "(os.pathsep-separated) for the web process and restart."
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{cite_key}.pdf"'},
    )


@router.post("/{ref_id}/replace-pdf", response_model=None)
async def replace_pdf(
    request: Request,
    ref_id: int,
    file: UploadFile = File(...),
) -> Response:
    """Overwrite a paper's on-disk PDF with an operator-uploaded copy —
    **without** re-ingesting or re-OCRing.

    Some stored PDFs are corrupted (the bytes won't render) even though the
    text chunks + metadata extracted at ingest are fine. This is the repair:
    a straight file swap so the viewer (:func:`pdf`) streams good bytes,
    leaving chunks/embeddings/metadata untouched. Safe because the viewer
    and the ``corpus_reconcile`` worker both resolve a PDF by *path /
    existence*, never by re-hashing the file against the stored
    ``pdf_sha256`` — so a swapped file is served immediately and still
    counts as "held".

    Writes to the file's currently-resolved location when it has one (so a
    PDF filed off-convention stays put), else to the canonical
    ``corpus_pdf_dest`` under the primary corpus root. Requires a cite_key
    to name the file; rejects a non-PDF upload (``%PDF`` magic). The stored
    ``pdf_sha256`` is intentionally left as-is (the swap is a bypass), but
    the authoritative ``storage_path`` pointer is refreshed so the resolver
    prefers the path we just wrote."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind not in _DOC_FAMILY:
        raise NotFound(f"document id={ref_id} not found")
    cite_key = ref.slug or ""
    if not cite_key:
        raise BadInput(
            f"paper id={ref_id} has no cite_key to name a PDF file — "
            "mint a short handle (Edit metadata) before replacing the PDF."
        )

    data = await file.read()
    if not data:
        raise BadInput("no file uploaded — choose a PDF to replace with.")
    if not data.startswith(b"%PDF"):
        raise BadInput(
            "uploaded file is not a PDF (missing %PDF header) — "
            "this replaces the stored file verbatim, so it must be a PDF."
        )

    cfg = get_web_config(request)
    dest = _resolve_pdf_for_ref(store, cfg.corpus_dirs, ref)
    if dest is None:
        dest = corpus_pdf_dest(cite_key, cfg.corpus_dirs[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Atomic swap: write a sibling temp then os.replace, so a viewer never
    # sees a half-written file (and a failed write leaves the old bytes).
    tmp = dest.with_name(dest.name + ".upload.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)

    # Keep pdf_sha256 as-is (bypass), but point the authoritative resolver
    # at the path we just wrote so it never re-guesses a stale shard.
    sha = getattr(ref, "pdf_sha256", None)
    if sha:
        store.set_pdf_storage_path(sha, str(dest))

    return RedirectResponse(url=f"/papers/{ref_id}?tab=Meta", status_code=303)


# ---- Edit + delete ----------------------------------------------------
#
# ``edit`` flows through ``runtime.dispatch(edit)`` so the handler's
# validation, ref-events log, and tree guards stay single-sourced (web +
# MCP behave the same). ``delete`` deliberately does NOT: it calls the
# store directly so paper deletion stays a web-only affordance and is not
# exposed on the agent MCP surface (``PaperHandler`` keeps
# ``supports_delete=False``).


def _build_paper_payload(
    ref_id: int,
    *,
    title: str,
    year: str,
    doi: str,
    arxiv: str,
    abstract: str,
    authors: str,
    journal: str = "",
    entry_type: str = "",
) -> dict[str, Any]:
    """Build the ``edit`` verb payload from the metadata form fields.

    Empty fields are dropped (an unset value must not overwrite a stored
    one). Authors split on newline / ``;`` and forward as cleaned lines —
    the paper edit handler canonicalises them to the stored shape. The
    result always carries ``kind`` + ``id``; ``len(...) > 2`` means real
    metadata is present. Shared by the ``edit`` route and the duplicate
    resolver's re-apply step.

    ``journal`` / ``entry_type`` default to ``""`` (not required
    keyword-only, unlike the rest) so a call site that predates the two
    new Meta-tab fields still compiles — ``resolve_duplicate``'s re-apply
    passes them through explicitly.
    """
    payload: dict[str, Any] = {"kind": "paper", "id": ref_id}
    if title.strip():
        payload["title"] = title.strip()
    if year.strip():
        try:
            payload["year"] = int(year.strip())
        except ValueError:
            pass
    if doi.strip():
        payload["doi"] = doi.strip()
    if arxiv.strip():
        payload["arxiv"] = arxiv.strip()
    if abstract.strip():
        payload["abstract"] = abstract.strip()
    if journal.strip():
        payload["journal"] = journal.strip()
    if entry_type.strip():
        payload["entry_type"] = entry_type.strip()
    if authors.strip():
        lines = [a.strip() for a in authors.replace(";", "\n").splitlines()]
        cleaned = [a for a in lines if a]
        if cleaned:
            payload["authors"] = cleaned
    return payload


def _render_edit_conflict(
    request: Request,
    ref_id: int,
    conflict: dict[str, Any],
    form: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render the duplicate-identifier resolver for a failed paper edit.

    Loads the conflicting owner so the template can link to its detail +
    PDF; degrades gracefully (``owner=None``) if it can't be fetched.
    ``form`` carries the operator's pending metadata edit so the resolver
    can re-apply it onto the survivor after the duplicate is absorbed.
    """
    store = get_store(request)
    owner_id = conflict["owner_id"]
    owner_ref = store.fetch_refs_by_ids([owner_id], include_deleted=False).get(owner_id)
    owner: dict[str, Any] | None = None
    owner_pdf = False
    if owner_ref is not None and owner_ref.kind == "paper":
        owner = _paper_row(owner_ref)
        owner["links"] = _links_from_ids(
            store.identifiers_for_refs([owner_id]).get(owner_id, {})
        )
        cfg = get_web_config(request)
        owner_pdf = _resolve_pdf_for_ref(store, cfg.corpus_dirs, owner_ref) is not None
    return templates.TemplateResponse(
        request,
        "papers/edit_conflict.html.j2",
        {
            "active_tab": "papers",
            "ref_id": ref_id,
            "field": conflict["field"],
            "value": conflict["value"],
            "owner_id": owner_id,
            "owner": owner,
            "owner_pdf": owner_pdf,
            "form": form or {},
        },
        status_code=409,
    )


def _safe_papers_redirect(return_to: str) -> str:
    """Constrain a ``return_to`` to a local ``/papers`` or ``/drive`` path
    (no open redirect). ``/drive`` joined the allow-list in WS1b — the
    list/triage callers that used to default here now bounce to a Drive
    preset, so a caller-supplied ``return_to`` pointing back at Drive must
    survive too."""
    if return_to.startswith("/papers") or return_to.startswith("/drive"):
        return return_to
    return "/drive"


@router.post("/{ref_id}/edit", response_model=None)
async def edit(
    request: Request,
    ref_id: int,
    title: str = Form(""),
    year: str = Form(""),
    doi: str = Form(""),
    arxiv: str = Form(""),
    abstract: str = Form(""),
    authors: str = Form(""),
    cite_key: str = Form(""),
    journal: str = Form(""),
    entry_type: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Update editable paper metadata.

    Empty fields are NOT sent (so an unset value doesn't overwrite the
    existing one). Authors come in as a newline- or semicolon-separated
    string; the paper edit handler canonicalises each line to
    ``{given, family}`` (unambiguous single-comma split) or ``{name}``
    (see :func:`precis.utils.authors.normalize_authors`). ``journal`` /
    ``entry_type`` merge into ``meta`` (``entry_type`` is Crossref
    vocabulary — a known value off the form's ``<select>``, or free text
    via its "Other…" escape). ``cite_key`` is the short handle: when it
    differs from the current slug the paper is re-slugged (and its PDF
    moved on disk) — see :func:`_rename_slug`.
    """
    payload = _build_paper_payload(
        ref_id,
        title=title,
        year=year,
        doi=doi,
        arxiv=arxiv,
        abstract=abstract,
        authors=authors,
        journal=journal,
        entry_type=entry_type,
    )

    store = get_store(request)
    current_slug = ""
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is not None:
        current_slug = ref.slug or ""
    new_slug = cite_key.strip().lower()
    slug_changed = bool(new_slug) and new_slug != current_slug
    has_meta = len(payload) > 2  # anything beyond kind + id

    if not has_meta and not slug_changed:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Edit error",
                "detail": "Nothing to change — edit a field or the handle first.",
                "status": 400,
            },
            status_code=400,
        )

    if has_meta:
        body, is_error = await await_dispatch(request, "edit", payload)
        if is_error:
            conflict = _parse_identifier_conflict(body)
            if conflict is not None:
                # Duplicate identifier: the DOI / arXiv id being assigned
                # already belongs to another paper (the two are almost
                # always the same paper held twice). Render the resolver —
                # it links to the owner's detail + PDF (open in a new tab to
                # inspect) and offers to merge either way (carrying the
                # pending edit so "keep this" can re-apply it post-merge).
                return _render_edit_conflict(
                    request,
                    ref_id,
                    conflict,
                    form={
                        "title": title,
                        "year": year,
                        "doi": doi,
                        "arxiv": arxiv,
                        "abstract": abstract,
                        "authors": authors,
                        "cite_key": cite_key,
                        "journal": journal,
                        "entry_type": entry_type,
                    },
                )
            # Any other error: render inline rather than redirect — the
            # operator needs to see why the edit didn't take.
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {"title": "Edit error", "detail": body, "status": 400},
                status_code=400,
            )

    if slug_changed:
        err = await asyncio.to_thread(
            _rename_slug, request, ref_id, current_slug, new_slug
        )
        if err is not None:
            return templates.TemplateResponse(
                request,
                "error.html.j2",
                {"title": "Rename error", "detail": err, "status": 400},
                status_code=400,
            )

    # A successful edit that lands a real title resolves a triaged paper —
    # clear the needs-triage tag so it leaves the queue. Idempotent: the
    # tag verb's remove is a no-op when the tag isn't present.
    if store.has_tag(ref_id, "OPEN", _TRIAGE_TAG):
        await await_dispatch(
            request,
            "tag",
            {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]},
        )
    # A review can't cover metadata that has since changed — clear any
    # existing sign-off. Idempotent: clearing an unset stamp is a no-op.
    store.clear_human_verified(ref_id)
    # Land back on the Meta tab, not the default Navigate — the user was
    # just editing metadata (bug report: /papers/helical91, 2026-08-28).
    # `?tab=Meta` is the SAME deep-link mechanism the `reviewed` /
    # `unacquirable` / `tags` handlers already redirect through (the
    # `detail` route's `tab` query param, `_render_detail`'s
    # `initial_tab`) — reused here rather than inventing a second one; a
    # plain `/papers/<id>` load (no param) still gets the usual
    # triage-or-Navigate default.
    return RedirectResponse(url=f"/papers/{ref_id}?tab=Meta", status_code=303)


def _rename_slug(
    request: Request, ref_id: int, old_slug: str, new_slug: str
) -> str | None:
    """Re-slug a paper: replace its ``cite_key`` and move the PDF on disk.

    Web-only (a direct store + filesystem op, not dispatched). Returns an
    error string for the caller to surface, or ``None`` on success.

    The on-disk PDF is named ``<cite_key>.pdf`` under a sharded corpus
    root, so the rename must move it too or the in-browser viewer 404s.
    The old path is resolved *before* the DB change (so we still have the
    handle to it), then the identifier is swapped (which raises on a
    cross-ref clash), then the file is moved best-effort.
    """
    if not _SLUG_RE.fullmatch(new_slug):
        return (
            f"handle {new_slug!r} is invalid — use lowercase letters and "
            "digits only (e.g. piela07)."
        )
    store = get_store(request)
    cfg = get_web_config(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    old_pdf = _resolve_pdf(cfg.corpus_dirs, old_slug) if old_slug else None
    try:
        store.set_ref_identifier(ref_id, "cite_key", new_slug, source="web-edit")
    except BadInput as exc:
        return str(exc)
    if old_pdf is not None:
        # Same shard math as the fetcher/resolver — one definition.
        new_pdf = corpus_pdf_dest(new_slug, old_pdf.parent.parent)
        try:
            new_pdf.parent.mkdir(parents=True, exist_ok=True)
            if not new_pdf.exists():
                old_pdf.rename(new_pdf)
            # Keep the authoritative path honest: the resolver prefers
            # ``pdfs.storage_path``, so a move that didn't update it would
            # leave the resolver pointing at the vanished old shard (a
            # false "held but missing"). ``pdf_sha256`` keys the pdfs row.
            sha = getattr(ref, "pdf_sha256", None) if ref is not None else None
            if sha and new_pdf.is_file():
                store.set_pdf_storage_path(sha, str(new_pdf))
        except OSError:
            # DB is updated; the file move is best-effort. The detail page's
            # "file expected but missing" panel will self-diagnose if the
            # move didn't land.
            pass
    return None


@router.post("/{ref_id}/delete", response_model=None)
async def delete(
    request: Request,
    ref_id: int,
    return_to: str = Form("/drive"),
) -> RedirectResponse | HTMLResponse:
    """Soft-delete this paper (sets ``refs.deleted_at = now()``).

    Web-only by policy: the call goes straight to the store rather than
    through ``runtime.dispatch`` so paper deletion is NOT exposed on the
    agent MCP surface. Soft delete is reversible at the DB level (toggle
    ``deleted_at`` back to NULL); the UX presents it as a one-way removal.
    ``return_to`` lands the operator back where they were (triage queue /
    duplicate resolver), constrained to ``/papers*``/``/drive*`` to avoid
    an open redirect; it defaults to Drive (WS1b — the papers list this
    used to default to is now a redirect, not a real page).
    """
    store = get_store(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Delete error",
                "detail": f"paper id={ref_id} not found",
                "status": 404,
            },
            status_code=404,
        )
    try:
        await asyncio.to_thread(store.soft_delete_ref, ref_id)
    except NotFound as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Delete error", "detail": str(exc), "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=_safe_papers_redirect(return_to), status_code=303)


def _paper_error(
    request: Request, title: str, detail: str, status: int
) -> HTMLResponse:
    """Render the shared error page for a paper mutation failure."""
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )


@router.post("/{ref_id}/resolve-duplicate", response_model=None)
async def resolve_duplicate(
    request: Request,
    ref_id: int,
    owner_id: int = Form(...),
    keep: str = Form("this"),
    title: str = Form(""),
    year: str = Form(""),
    doi: str = Form(""),
    arxiv: str = Form(""),
    abstract: str = Form(""),
    authors: str = Form(""),
    cite_key: str = Form(""),
    journal: str = Form(""),
    entry_type: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Resolve a same-identifier duplicate by merging one paper into the other.

    The duplicate resolver offers two directions:

    * ``keep='this'`` — keep #``ref_id``, absorb #``owner_id``. The owner's
      links migrate onto this paper and its DOI / arXiv / cite_key free up
      (``store.merge_refs``), then the operator's pending metadata edit is
      re-applied here (the identifier it clashed on is now available) and
      ``needs-triage`` is cleared. This is the "the other is an empty stub,
      keep my PDF and redirect its links here" case.
    * ``keep='other'`` — keep #``owner_id``, absorb this paper (#``ref_id``).
      This paper's links migrate onto the owner and this copy is retired.

    Both directions go through one atomic ``merge_refs`` (migrate links →
    drop the victim's identifiers → soft-delete the victim), so no edge is
    orphaned. ``delete`` (web-only, no MCP surface) underlies the retire.
    """
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id, owner_id], include_deleted=False)
    this_ref = refs.get(ref_id)
    owner_ref = refs.get(owner_id)
    if this_ref is None or this_ref.kind != "paper":
        return _paper_error(request, "Merge error", f"paper id={ref_id} not found", 404)
    if owner_ref is None or owner_ref.kind != "paper":
        return _paper_error(
            request,
            "Merge error",
            f"paper id={owner_id} not found (it may already be deleted)",
            404,
        )

    if keep == "other":
        # Keep the owner, absorb this paper into it.
        try:
            await asyncio.to_thread(store.merge_refs, ref_id, owner_id)
        except (NotFound, BadInput) as exc:
            return _paper_error(request, "Merge error", str(exc), 400)
        return RedirectResponse(url=f"/papers/{owner_id}?tab=Meta", status_code=303)

    # keep == 'this': absorb the owner here, then re-apply the pending edit.
    try:
        await asyncio.to_thread(store.merge_refs, owner_id, ref_id)
    except (NotFound, BadInput) as exc:
        return _paper_error(request, "Merge error", str(exc), 400)

    payload = _build_paper_payload(
        ref_id,
        title=title,
        year=year,
        doi=doi,
        arxiv=arxiv,
        abstract=abstract,
        authors=authors,
        journal=journal,
        entry_type=entry_type,
    )
    if len(payload) > 2:  # real metadata beyond kind + id
        body, is_error = await await_dispatch(request, "edit", payload)
        if is_error:
            return _paper_error(request, "Edit error", body, 400)

    new_slug = cite_key.strip().lower()
    current_slug = this_ref.slug or ""
    if new_slug and new_slug != current_slug:
        err = await asyncio.to_thread(
            _rename_slug, request, ref_id, current_slug, new_slug
        )
        if err is not None:
            return _paper_error(request, "Rename error", err, 400)

    # The duplicate is gone and the metadata took — leave the triage queue.
    if store.has_tag(ref_id, "OPEN", _TRIAGE_TAG):
        await await_dispatch(
            request, "tag", {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]}
        )
    return RedirectResponse(url=f"/papers/{ref_id}?tab=Meta", status_code=303)


@router.post("/{ref_id}/untriage", response_model=None)
async def untriage(
    request: Request,
    ref_id: int,
    return_to: str = Form(_TRIAGE_PRESET),
) -> Response:
    """Manually clear the ``needs-triage`` tag (dismiss from the queue).

    A successful metadata edit clears the tag automatically, but a paper
    that's actually fine, one fixed by hand outside the S2 flow, or one
    whose fix failed on a duplicate identifier stays tagged. This is the
    explicit operator control. Idempotent: the tag remove is a no-op when
    the tag isn't present.

    A thin named preset over the generic ``tag`` verb (the same dispatch
    the ``/refs/{kind}/{ref_id}/tags`` endpoint uses) routed through the
    shared :func:`redirect_or_error` so a failed dispatch renders the
    handler's message instead of silently redirecting — the original bug
    here was a swallowed ``NotFound`` that made the button look like it
    worked while the tag survived. ``return_to`` defaults to the Drive
    triage preset (WS1b — the ``/papers/triage`` queue this used to
    default to is now a redirect, not a real page).
    """
    return await redirect_or_error(
        request,
        "tag",
        {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]},
        redirect=_safe_papers_redirect(return_to),
        error_title="Untriage error",
    )


@router.post("/{ref_id}/retriage", response_model=None)
async def retriage(
    request: Request,
    ref_id: int,
    return_to: str = Form(""),
) -> Response:
    """Re-flag an already-accepted paper for review — the inverse of
    :func:`untriage`. Re-adds the ``needs-triage`` tag so the paper rejoins
    the Drive triage queue even after it left it (a metadata slip spotted
    later, a bad OCR body, a corrupted PDF the operator is about to
    replace). Idempotent: adding an already-present tag is a no-op.

    Mirrors ``untriage``'s named-preset-over-``tag``-verb shape so vocabulary
    validation and error surfacing stay single-sourced. ``return_to``
    defaults to the paper's own Meta tab (where the button lives)."""
    return await redirect_or_error(
        request,
        "tag",
        {"kind": "paper", "id": ref_id, "add": [_TRIAGE_TAG]},
        redirect=_safe_papers_redirect(return_to or f"/papers/{ref_id}?tab=Meta"),
        error_title="Mark-for-review error",
    )


@router.post("/{ref_id}/reviewed", response_model=None)
async def reviewed(
    request: Request,
    ref_id: int,
    return_to: str = Form(""),
) -> Response:
    """Toggle the ``human_verified_at`` sign-off stamp (Meta tab "Mark
    reviewed" / undo).

    ``refs.human_verified_at/by/note`` are plumbed on every ref
    (:meth:`Store.set_human_verified` / ``clear_human_verified``,
    ``_refs_ops.py:1382``) but sit outside the tag vocabulary a dispatched
    verb validates — this is a direct store op, like ``/drive``'s other
    operational stamps, not a named preset over ``tag``. Marking reviewed
    also clears a lingering ``needs-triage`` flag (a paper can't be both
    unreviewed metadata and signed off). ``by`` is the web process's
    configured write identity (``web:owner`` by default).
    """
    store = get_store(request)
    cfg = get_web_config(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return _paper_error(
            request, "Review error", f"paper id={ref_id} not found", 404
        )
    try:
        if getattr(ref, "human_verified_at", None):
            store.clear_human_verified(ref_id)
        else:
            store.set_human_verified(ref_id, by=cfg.source)
            if store.has_tag(ref_id, "OPEN", _TRIAGE_TAG):
                await await_dispatch(
                    request,
                    "tag",
                    {"kind": "paper", "id": ref_id, "remove": [_TRIAGE_TAG]},
                )
    except NotFound as exc:
        return _paper_error(request, "Review error", str(exc), 400)
    return RedirectResponse(
        url=_safe_papers_redirect(return_to or f"/papers/{ref_id}?tab=Meta"),
        status_code=303,
    )


@router.post("/{ref_id}/unacquirable", response_model=None)
async def unacquirable(
    request: Request,
    ref_id: int,
    mode: str = Form(""),
    note: str = Form(""),
    return_to: str = Form(""),
) -> Response:
    """Set / clear this paper's unacquirable-source declaration (Meta tab
    "Can't get it") — a pure **acquirability fact**, not a claim-backing
    assertion: "I tried hard to obtain this and could not; the metadata is
    correct."

    Writes ``meta.unacquirable_override = {note, by, at}`` (no ``mode`` —
    it never softens any claim's trust label). :mod:`precis.taproot.trust`
    reads it two ways: it *hardens* a clean claim hub whose every
    print-visible grounding paper carries one down to ``unverified``, and
    it *enriches* the note on an unverified lifecycle finding blocked on
    this paper — never the calm ``Ⓐ``/``✍`` marks (that door lives on the
    claim itself: the paper Meta tab's Ⓐ/✍ buttons were removed —
    declare a claim-level override via ``edit(kind='finding',
    unacquirable_note=…)`` or ``POST /claim/<head>/unacquirable``).
    ``mode=''``/``'clear'`` drops the override (set to JSON null;
    ``trust`` treats a non-dict value as unset); any other value is a 400.

    A direct store op like ``/reviewed`` — a ref-meta stamp, not a
    handler-owned kind; ``by`` is the web write identity (``web:owner``).
    ``note`` is required when setting: a silent override defeats the audit
    purpose (mirrors the finding handler's own guard)."""
    store = get_store(request)
    cfg = get_web_config(request)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind != "paper":
        return _paper_error(
            request, "Unacquirable error", f"paper id={ref_id} not found", 404
        )
    redirect = _safe_papers_redirect(return_to or f"/papers/{ref_id}?tab=Meta")
    mode = (mode or "").strip().lower()
    if mode in ("", "clear"):
        store.update_ref(ref_id, meta_patch={"unacquirable_override": None})
        return RedirectResponse(url=redirect, status_code=303)
    if mode != "set":
        return _paper_error(
            request, "Unacquirable error", f"unknown mode {mode!r}", 400
        )
    if not note.strip():
        return _paper_error(
            request,
            "Unacquirable error",
            "a note is required — say why the source can't be obtained",
            400,
        )
    override = {
        "note": note.strip(),
        "by": cfg.source,
        "at": datetime.now(UTC).isoformat(),
    }
    store.update_ref(ref_id, meta_patch={"unacquirable_override": override})
    return RedirectResponse(url=redirect, status_code=303)


def _split_tags(raw: str) -> list[str]:
    """Split a comma/space-separated tag input into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


@router.post("/{ref_id}/tags", response_model=None)
async def edit_tags(
    request: Request,
    ref_id: int,
    add: str = Form(""),
    remove: str = Form(""),
) -> Response:
    """Add / remove tags on a paper from the Meta tab.

    ``add`` is a comma/space-separated string the operator typed; ``remove``
    is a single OPEN tag value from a chip's × (the ``needs-triage`` review
    flag included). Flows through the ``tag`` verb so vocabulary validation
    stays single-sourced, then lands back on the Meta tab. A no-op submit
    just returns to the page.
    """
    add_list = _split_tags(add)
    remove_list = _split_tags(remove)
    redirect_url = f"/papers/{ref_id}?tab=Meta"
    if not add_list and not remove_list:
        return RedirectResponse(url=redirect_url, status_code=303)
    args: dict[str, Any] = {"kind": "paper", "id": ref_id}
    if add_list:
        args["add"] = add_list
    if remove_list:
        args["remove"] = remove_list
    return await redirect_or_error(
        request, "tag", args, redirect=redirect_url, error_title="Tag error"
    )
