"""Drafts — shared library + backend endpoint host for the ``draft`` kind. The classic per-block virtual-scroll reader page this module
used to serve is retired: ``/smartdraft/{ident}`` (``routes/smartdraft.py``)
is the sole draft reader now, and imports several of this module's helpers
(``_doc_state``, ``_review_status_by_chunk``, ``_ref_chips``,
``_paper_pdf_missing``, ``_work_items``, …) plus reuses the hand-driven
working-set + human-review endpoints below unchanged. ``GET /drafts/{ident}``
and ``GET /draft/{ident}`` are kept as 307 redirects into smartdraft so
every bookmark, quest link, and ``/c/<handle>`` deep-link still lands on the
draft.

Tier-A surface (the document is *steered*, not hand-typed). Routes still
served from here:

* ``GET /drafts`` — retired into Drive (nav restructure): redirects to the
  ``kind=draft`` facet preset (``/drive?k=draft&submitted=1``), mirroring
  ``routes/papers.py``'s WS1b retirement.
* ``GET /drafts/{ident}`` / ``GET /draft/{ident}`` — 307 redirects into
  ``/smartdraft/{ident}``.
* ``POST /drafts/{ident}/marks`` / ``/request-ws`` — the hand-driven working
  set (see ``precis_web.draft_eyes``): toggle pen/eye markers on
  paragraphs and file a change request carrying the whole set
  (``meta.working_set``) so the planner tick edits the pens grounded in the
  eyes instead of a single anchor. (The classic reader's ``/around``
  bulk-"expand around here into eyes" affordance retired with the page and is
  not yet ported to smartdraft — see ``OPEN-ITEMS.md``.)
* ``POST /drafts/{ident}/human-review`` — the ✓ gutter checkbox: records the
  human reviewer's sign-off on one block (``edit(kind='draft',
  review='human')``, migration 0086's ``chunk_review`` ledger).
  ``POST /drafts/{ident}/review/retract`` un-reviews (deletes the ledger
  row for a checker, default ``human``). Distinct from the per-heading
  ``POST /drafts/{ident}/review`` "review ▾" menu (now driving
  ``mint_review_fanout``, the incremental review fanout), which
  files review-todos, not ledger rows. ``POST /drafts/{ident}/cites/convert``
  dry-runs/applies the taproot living-cite backfill (item 5b).
* ``POST /drafts/{ident}/title`` — header rename: converges ``refs.title``
  AND the title heading chunk in one transaction (``store.set_draft_title``)
  so the name in search results can't drift from the one in the document.
* ``POST /drafts/{ident}/delete`` — soft-delete the whole draft, gated on
  typing its name (atomic: ref ``deleted_at`` + chunks retired; recoverable).
* ``GET /c/{handle}`` — resolve a chunk handle → redirect to where it
  lives: a draft chunk (``dc``/``¶``) into the smartdraft reader focused at
  the chunk, a paper/other chunk (``pc``/``mc``/…) through the ``/r``
  resolver at that chunk. The click target of every ``¶``/``§`` anchor.
* ``GET /preview/chunk/{handle}`` — hover-popover quote for any chunk
  handle (draft or paper/other), so a ``§`` paper-chunk citation hovers.
* Direct-edit / structural routes (``/text``, ``/table``, ``/block…``,
  ``/validate-refs``, ``/ref-search``, ``/figure…``, ``/authors``,
  ``/workspace``, ``/authoring``, ``/fork``, exports) — shared by
  smartdraft's ported editor UI.
* ``GET /drafts/{ident}/export.docx`` / ``POST /drafts/{ident}/export.pdf``
  — both gate on ``precis.export.retraction``: a ``retracted`` cite
  hard-blocks (override: ``?ignore_retractions=1`` / form field), reading
  stored state only (never a live Crossref check — see
  ``_retraction_blocked_response``). ``GET
  /drafts/{ident}/retraction-status`` (no-network read) and ``POST
  /drafts/{ident}/retraction-check`` (the watch button — live re-check,
  TTL-gated, ``force=1`` bypasses the TTL) back the export pane's
  retraction UI; see ``docs/backlog/retraction-status-downstream.md``.

Rendering is **raw source** (Tier A); the resolution pass that computes
§-numbers / resolves cross-refs is the export engine (Tier B), shared
across HTML/LaTeX/Word targets. KaTeX renders ``$…$`` client-side.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from markupsafe import Markup

from precis.draft.scaffolds import DOC_TYPE_BRIEF as _DOC_TYPE_BRIEF

# Unused in this module itself, but re-exported: routes/drive.py and
# routes/smartdraft.py both import _DOC_TYPES from here.
from precis.draft.scaffolds import DOC_TYPES as _DOC_TYPES  # noqa: F401
from precis.draft.scaffolds import SCAFFOLDS as _SCAFFOLDS
from precis.draft.scaffolds import SECTION_STYLES as _SECTION_STYLES
from precis.errors import BadInput, NotFound
from precis.quest.review_fanout import ALL_LENSES, DOC_LENSES, mint_review_fanout
from precis.store._draft_ops import content_sha

# The taproot backfill cascade fns (item 5b, "convert to living cites"),
# referenced through THIS module's own names (not ``backfill.``/``canon.``
# attribute lookups) so a test can inject deterministic fakes by
# monkeypatching them here — mirroring ``tests/test_taproot_backfill.py``'s
# injected-cascade-fns pattern, just at the web-route call site instead of a
# direct ``plan_chunk``/``apply_chunk`` call. Each is looked up fresh off
# this module's globals at call time (a bare name reference in a function
# body), so ``monkeypatch.setattr(drafts, "_backfill_extract_claim", fake)``
# takes effect on the next request with no re-import needed.
from precis.taproot.backfill import ChunkBackfill, apply_chunk, plan_chunk
from precis.taproot.canon import block as _backfill_block
from precis.taproot.canon import dedup_judge as _backfill_dedup_judge
from precis.taproot.canon import extract_claim as _backfill_extract_claim
from precis.taproot.canon import merge_confirm as _backfill_merge_confirm
from precis.utils import draft_markup, handle_registry, mentions
from precis.utils.authors import (
    author_display,
    to_author_dicts,
)

# Planner tiers a change-request / review can run on, via ``meta.llm_tier``.
# Single-sourced from the router's planner alias map so the
# accepted set — the cloud triad plus the cluster's ``local`` qwen tier — never
# drifts from ``Tag.parse_strict`` or the ``planner_models()`` dropdown.
from precis.utils.llm.router import PLANNER_MODEL_ALIASES as _PLANNER_MODELS
from precis.utils.llm.router import llm_select_from_payload
from precis.utils.table_data import Scalar
from precis_web import draft_eyes
from precis_web.deps import (
    await_dispatch,
    get_runtime,
    get_store,
    redirect_or_error,
    templates,
)
from precis_web.linkify import popover_chip
from precis_web.paper_ident import PAPER_IDENT_KINDS, paper_head

router = APIRouter(tags=["drafts"])

log = logging.getLogger(__name__)

#: Bounded ``(ref_id, version) → terms`` cache. ``defined_terms`` is a
#: whole-draft ``string_agg`` + Schwartz-Hearst scan plus the registry
#: ``term`` leaves; on the on-demand row path it would otherwise re-run for
#: every block hydrated. Keyed by the draft's version token, so any chunk
#: edit invalidates it. Tiny LRU. The value is the rich the structured term registry map
#: (``{surface: TermEntry|str}``) that :func:`linkify._highlight_abbrevs`
#: renders as a hover — a bare definition for a glossary/patent term, a rich
#: card (MPN / manufacturer / datasheet) for a manufacturing part.
_ABBREV_CACHE: OrderedDict[tuple[int, int], dict[str, Any]] = OrderedDict()
_ABBREV_CACHE_MAX = 64


def _abbrevs_cached(store: Any, ref_id: int, version: int) -> dict[str, Any]:
    """Whole-draft term/abbreviation map (rich records), memoised
    per (draft, version) so the per-row hydrate path doesn't re-scan the whole
    draft each time. Falls back to the plain ``{short: str}`` map for a store
    that predates :meth:`defined_terms` (older FakeStore in tests)."""
    key = (ref_id, version)
    hit = _ABBREV_CACHE.get(key)
    if hit is not None:
        _ABBREV_CACHE.move_to_end(key)
        return hit
    fn = getattr(store, "defined_terms", None) or store.defined_abbrevs
    val = fn(ref_id)
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


def _job_parent(store: Any, ref: Any) -> int:
    """The ref a draft-scoped job (export / reMarkable send) should anchor
    under: the owning project todo when linked, else the draft ref itself.

    ``draft`` is a valid :data:`JOB_PARENT_KINDS` member, so a project-less
    draft still parents its job cleanly (progress/result land under the draft
    on the task page) instead of hard-erroring. Mirrors ``_owner_workspace``'s
    ``pid if pid is not None else ref.id`` fallback."""
    pid = _project_id(store, ref.id)
    return pid if pid is not None else int(ref.id)


#: Tooltip on the red ▲ a cited paper carries when its PDF is held but
#: not on disk where the web process looked (see ``_paper_pdf_missing``).
_MISSING_PDF_WARN = "PDF missing — this cited paper is held but its file isn't on disk"


@dataclass(frozen=True, slots=True)
class RefChip:
    """A rendered reference chip plus the structured discriminant a caller
    can filter on (gr171761) — e.g. smartdraft's ``_cited_sources`` wants
    just the paper-citation chips, and used to sniff for that by testing
    ``'href="/r/paper/'`` against the rendered HTML, which silently breaks
    if the href shape ever changes. ``kind`` is the cited ref's precis kind
    (``"paper"``, ``"memory"``, ``"web"`` for an external URL, …); ``is_chunk``
    is True when the chip navigates into a chunk (``/c/<handle>``) rather
    than a whole-ref view (``/r/<kind>/<id>``) — a distinction ``kind`` alone
    can't carry since a chunk-form handle (``pc10``) still has ``kind ==
    "paper"``.

    Implements ``__html__`` so it renders exactly like the ``Markup`` it
    wraps wherever a chip is printed directly (Jinja templates, ``str()``
    call sites, existing tests)."""

    kind: str
    is_chunk: bool
    html: Markup

    def __html__(self) -> str:
        return str(self.html)

    def __str__(self) -> str:
        return str(self.html)


def _ref_chips(
    text: str, is_missing: Callable[[str, str], bool] | None = None
) -> list[RefChip]:
    """The references a block makes, as terse hover-preview chips — the
    superset grammar (bracket/sigil forms ∪ bare ``kind:ref``), deduped
    by their navigate target so ``§kong24~2`` and ``paper:kong24~2`` (the
    same chunk) collapse to one chip. Each chip carries the cited quote
    on hover (``popover_chip``), tagged with its structured ``(kind,
    is_chunk)`` (:class:`RefChip`) so a caller can filter without parsing
    the rendered HTML. Reuses the shared parser/grammar (DRY).

    ``is_missing(kind, ident)`` — when supplied — flags a cited paper whose
    PDF is missing on disk; its chip then carries a red ▲ marker."""
    seen: set[str] = set()
    chips: list[RefChip] = []

    def _warn(kind: str, ident: str) -> str | None:
        return _MISSING_PDF_WARN if is_missing and is_missing(kind, ident) else None

    def add(
        label: str,
        href: str,
        preview: str | None,
        *,
        kind: str,
        is_chunk: bool,
        warn: str | None = None,
    ) -> None:
        if href in seen:
            return
        seen.add(href)
        chips.append(
            RefChip(kind, is_chunk, popover_chip(label, href, preview, warn=warn))
        )

    def paper(slug: str, chunk: str | None, label: str) -> None:
        # chunk here is the regex group incl. leading ``~`` (or None).
        suffix = f"?chunk={chunk[1:]}" if chunk else ""
        add(
            label,
            f"/r/paper/{slug}{suffix}",
            f"/preview/paper/{slug}{suffix}",
            kind="paper",
            is_chunk=False,
            warn=_warn("paper", slug),
        )

    for ref in draft_markup.parse_references(text):
        if ref.cls == draft_markup.XREF:
            h = ref.target.lstrip("¶")
            # A ``¶handle`` may itself be an universal handle (e.g.
            # a paper chunk ``pc10``) — parse it lexically (no DB) so the
            # chip carries its real kind, not a generic placeholder.
            parsed = handle_registry.parse(h)
            xref_kind = parsed[0] if parsed is not None else "chunk"
            add(
                ref.surface or ref.target,
                f"/c/{h}",
                f"/preview/chunk/{h}",
                kind=xref_kind,
                is_chunk=True,
            )
        elif ref.cls == draft_markup.CITE:
            m = mentions.DRAFT_CITE_PATTERN.fullmatch(ref.target)
            if m:
                paper(m.group("slug"), m.group("chunk"), ref.surface or ref.target)
        elif ref.cls == draft_markup.WEB:
            add(ref.surface or ref.target, ref.target, None, kind="web", is_chunk=False)
        else:  # AUTHORING — a bare universal handle [me6184] or [[kind:id]]
            parsed = handle_registry.parse(ref.target)
            if parsed is not None:  # a universal handle → chunk or record
                kind, is_chunk, pk = parsed
                if is_chunk:
                    h = handle_registry.normalize(ref.target)
                    add(
                        ref.surface or ref.target,
                        f"/c/{h}",
                        f"/preview/chunk/{h}",
                        kind=kind,
                        is_chunk=True,
                    )
                else:
                    add(
                        ref.surface or ref.target,
                        f"/r/{kind}/{pk}",
                        f"/preview/{kind}/{pk}",
                        kind=kind,
                        is_chunk=False,
                        warn=_warn(kind, str(pk)),
                    )
                continue
            m = mentions.REF_PATTERN.fullmatch(ref.target)
            if m and m.group("kind") in mentions.LINKIFY_KINDS:
                k, i = m.group("kind"), m.group("id").lstrip("#")
                add(
                    ref.surface or ref.target,
                    f"/r/{k}/{i}",
                    f"/preview/{k}/{i}",
                    kind=k,
                    is_chunk=False,
                )
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
            kind=kind,
            is_chunk=False,
        )
    return chips


def _cite_candidates(text: str) -> tuple[set[str], set[str]]:
    """Paper-citation tokens a block makes, split for the local-vs-external
    existence check: ``handles`` (normalised ``pc``/``pa`` paper handles) and
    ``slugs`` (``§slug`` / ``paper:slug`` cite_keys). Mirrors ``_ref_chips``
    parsing so the colouring keys on exactly the tokens the linkifier
    renders as compact ``§`` markers."""
    handles: set[str] = set()
    slugs: set[str] = set()
    for ref in draft_markup.parse_references(text):
        if ref.cls == draft_markup.CITE:
            m = mentions.DRAFT_CITE_PATTERN.fullmatch(ref.target)
            if m:
                slugs.add(m.group("slug"))
        elif ref.cls == draft_markup.AUTHORING:
            parsed = handle_registry.parse(ref.target)
            if parsed is not None and parsed[0] == "paper":
                handles.add(handle_registry.normalize(ref.target))
    for kind, ident, _chunk in mentions.extract_handles(text):
        if kind == "paper":
            slugs.add(ident.lstrip("#"))
    return handles, slugs


def provenance_state(text: str) -> str:
    """A paragraph's grounding provenance, for the smartdraft reader's
    per-paragraph colour marker: ``"sourced"`` (cites a corpus paper/patent),
    ``"pending"`` (cites a ``[fi<id>]`` finding — a source still being
    chased), or ``"unsourced"`` (no citation at all). Keys off each chip's
    ``kind`` from :func:`_ref_chips` rather than re-parsing the citation
    grammar (DRY — same parser the "Cited sources" rail keys off). Unlike
    that rail, a chunk-form cite (``[pc10]``, ``is_chunk=True``) still counts
    as ``"sourced"`` — a chunk citation is real grounding evidence."""
    chips = _ref_chips(text)
    kinds = {c.kind for c in chips}
    if kinds & {"paper", "patent"}:
        return "sourced"
    if "finding" in kinds:
        return "pending"
    return "unsourced"


#: Request lifecycle ordering for the per-block list: active first, then
#: done/abandoned (which now *persist* so you can click in and debug the
#: LLM run, rather than vanishing on completion).
_REQUEST_ORDER = {"open": 0, "scheduled": 1, "doing": 2, "paused": 3}


def _requests_by_handle(
    store: Any, handles: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """ALL change-request todos anchored at each chunk, grouped by handle —
    the per-block change-request cards. Thin wrapper over
    :meth:`Store.anchored_todos` (moved there so
    :func:`precis_web.draft_links.chunk_links`'s ``flags`` reads the SAME
    query, not a second copy — gripe 178766)."""
    return store.anchored_todos(handles)


def _list_markers(
    chunk_objs: list[Any],
) -> tuple[dict[str, str], dict[str, bool]]:
    """Bullet/number marker per ``item`` handle (migration 0037).

    A ``ulist``/``olist`` container owns ``item`` children. Returns
    ``(marker, ordered)`` maps keyed by item handle: an ``olist`` child is
    numbered ``1.``, ``2.``, … (1-based among its siblings, honouring the
    container's optional ``meta.start``); a ``ulist`` child — or an orphan
    ``item`` whose container isn't live — gets a ``•`` bullet. Counters key
    on ``parent_chunk_id`` so a nested list restarts independently."""
    kind_by_id = {c.chunk_id: c.chunk_kind for c in chunk_objs}
    counters: dict[Any, int] = {}
    marker: dict[str, str] = {}
    ordered: dict[str, bool] = {}
    for c in chunk_objs:
        if c.chunk_kind != "item":
            continue
        if kind_by_id.get(c.parent_chunk_id) == "olist":
            if c.parent_chunk_id in counters:
                counters[c.parent_chunk_id] += 1
            else:
                parent = next(
                    (p for p in chunk_objs if p.chunk_id == c.parent_chunk_id),
                    None,
                )
                start = int(
                    ((parent.meta if parent else {}) or {}).get("start", 1) or 1
                )
                counters[c.parent_chunk_id] = start
            marker[c.handle] = f"{counters[c.parent_chunk_id]}."
            ordered[c.handle] = True
        else:
            marker[c.handle] = "•"
            ordered[c.handle] = False
    return marker, ordered


def _paper_pdf_missing(store: Any, ident: str) -> bool:
    """A cited paper whose PDF is *held but absent* — the anomaly the reader
    flags. True only when the ref exists, claims a PDF (``pdf_sha256`` set),
    and the corpus-presence ledger says no node holds a fresh copy
    (``Store.pdf_missing``) — a corpus-wide DB read, so the marker no longer
    depends on which corpus roots *this* web process happens to mount
    (the ``corpus_reconcile`` worker keeps the ledger current).

    A stub (no ``pdf_sha256``, queued for fetch) is a known state, not an
    anomaly, so it stays unflagged; so does a paper the ledger has never
    checked (``pdf_missing`` is False for an unswept sha), so the marker can't
    false-fire before the first reconcile sweep. A missing ref returns False
    too. ``ident`` is the chip target — a numeric ref_id (from a ``pa…``
    handle) or a slug (from a ``paper:`` mention); ``get_ref`` resolves both."""
    key: int | str = ident
    stripped = str(ident).lstrip("#")
    if stripped.isdigit():
        key = int(stripped)
    try:
        paper = store.get_ref(kind="paper", id=key)
    except Exception:  # pragma: no cover - defensive (bad ident never 500s the row)
        return False
    sha = getattr(paper, "pdf_sha256", None) if paper is not None else None
    if not sha:
        return False
    return store.pdf_missing(sha)


#: Chunk kinds the inline editor may edit as raw text (slice 2a,
#: docs/backlog/draft-inline-editor.md). Prose kinds + the verbatim-text kinds
#: (code / listing — you edit their source). Math is not a kind: display math
#: is a `paragraph` carrying `$$…$$`, edited like any other paragraph. Excludes
#: figure (bytes), table (derived from meta.table, in DERIVED_KINDS), and the
#: ulist/olist containers, which keep their own affordances.
#: NB: keep the client editable-kinds set (smartdraft/view.html.j2) in sync
#: with this.
_EDITABLE_KINDS = frozenset(
    {
        "paragraph",
        "heading",
        "item",
        "aside",
        "box",
        "callout",
        "term",
        "code",
        "listing",
    }
)
#: Kinds whose text a backspace-merge may append onto — never a heading (would
#: corrupt the title) or a derived/structural block.
_MERGE_KINDS = frozenset({"paragraph", "item", "aside", "box", "callout"})


def _review_status_by_chunk(store: Any, ref_id: int) -> dict[int, dict[str, Any]]:
    """Whole-draft human/checker review-ledger status (migration 0086),
    indexed by ``chunk_id``: ``{chunk_id: {checker: {approved_sha, verdict,
    at, dirty}, ...}}``. One call to ``Store.review_status_for_draft`` per
    request — the read-side counterpart to the ``/human-review`` route's
    write-through-the-``edit``-verb. Imported by ``routes/smartdraft.py``
    to look up the focus block's review status.

    A chunk **absent** from the map is one ``review_status_for_draft``
    doesn't surface (retired / no ``content_sha`` / unordered) — not
    reviewable, so the reader hides the ✓ gutter for it. A chunk **present**
    with an empty per-checker dict is reviewable but never reviewed.
    Threads every checker the ledger carries (not just ``'human'``) so a
    future column (the other paper-writing-pipeline rung-3 checkers) can
    render off the same payload; the reader template only reads ``'human'``
    for now.

    Each per-chunk dict also carries two RESERVED, non-checker keys
    (the via-section rollup) — ``_section_chunk_id`` (the
    nearest enclosing HEADING chunk id, ``None`` if none — the id a
    paragraph's rollup uses to pull in its section's ``structure``/
    ``adversarial`` state "via section") and ``_chunk_kind``. Leading
    underscore so neither can ever collide with a real checker name
    (``flow``/``cites``/``structure``/``adversarial``/``human``/``toc``)
    and existing ``review.human``-style dotted template access is
    unaffected.

    Records are JSON-safe (``at`` ISO-stringified via :func:`_review_entry`)
    because this map can be serialized (``tojson`` / ``JSONResponse``) — a
    raw ``datetime`` would 500 the page."""
    out: dict[int, dict[str, Any]] = {}
    for row in store.review_status_for_draft(ref_id):
        entry = out.setdefault(
            row["chunk_id"],
            {
                "_section_chunk_id": row.get("section_chunk_id"),
                "_chunk_kind": row.get("chunk_kind"),
            },
        )
        checker = row.get("checker")
        if checker:
            entry[checker] = _review_entry(row)
    return out


def _review_entry(row: dict[str, Any]) -> dict[str, Any]:
    """One JSON-safe per-checker review record ``{approved_sha, verdict, at,
    dirty}`` — ``at`` ISO-stringified (a raw ``datetime`` isn't JSON
    serializable, and this rides into the skeleton JSON). The single shape
    both :func:`_review_status_by_chunk` and :func:`_review_json` build, so
    the two paths can't drift on serializability."""
    at = row.get("at")
    return {
        "approved_sha": row.get("approved_sha"),
        "verdict": row.get("verdict"),
        "at": at.isoformat() if at is not None and hasattr(at, "isoformat") else at,
        "dirty": row.get("dirty"),
    }


def _review_json(status: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON-safe ``{checker: {approved_sha, verdict, at, dirty}}`` for the
    ``/human-review`` POST response — same per-checker record
    (:func:`_review_entry`) ``_review_status_by_chunk`` attaches to a row."""
    out: dict[str, Any] = {}
    for r in status:
        checker = r.get("checker")
        if not checker:
            continue
        out[checker] = _review_entry(r)
    return out


def _rollup_json(store: Any, ref_id: int) -> dict[str, int]:
    """``{"done": N, "total": M}`` — the toolbar rollup badge data (item 8),
    a thin JSON wrapper over ``Store.review_rollup_for_draft`` (prose-only
    denominator; ``done`` = approved by ``human`` at the current sha).
    Attached to every ledger-mutating JSON response (``/human-review``,
    ``/review/retract``) so the client can refresh the toolbar badge without
    a full page reload."""
    return store.review_rollup_for_draft(ref_id)


def _connection_chips(conns: list[dict[str, Any]]) -> list[Any]:
    """Render chunk-connection rows (linked refs + dreams) as terse
    hover-preview chips: ``kind:ident — title``, click → the ref. Shared with
    the smartdraft reader's focus "Connections" rail (gripe 178766,
    ``precis_web.draft_links.chunk_links``)."""
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


def _parse_author_lines(text: str) -> list[dict[str, str]]:
    """Parse the draft author textarea into ``{name, affiliation?, ror?}``
    entries — one author per line, fields split on ``|`` as
    ``Name | Affiliation | ROR`` (affiliation + ROR optional; extra fields
    ignored). Blank lines and name-less lines dropped. The result is fed
    through :func:`to_author_dicts` (canonical name, keys preserved)."""
    out: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if len(parts) > 1 and parts[1]:
            entry["affiliation"] = parts[1]
        if len(parts) > 2 and parts[2]:
            entry["ror"] = parts[2]
        out.append(entry)
    return out


def _draft_author_lines(ref: Any) -> str:
    """Existing byline rendered back into the textarea's
    ``Name | Affiliation | ROR`` line format for round-trip editing."""
    lines: list[str] = []
    for a in getattr(ref, "authors", None) or []:
        name = author_display(a, order="sortable")
        if not name:
            continue
        aff = ror = ""
        if isinstance(a, dict):
            aff = (a.get("affiliation") or "").strip()
            ror = (a.get("ror") or "").strip()
        parts = [name]
        if ror:
            parts += [aff, ror]  # keep the position even if aff is blank
        elif aff:
            parts += [aff]
        lines.append(" | ".join(parts))
    return "\n".join(lines)


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
    out: list[dict[str, Any]] = []
    for it in items:
        # Parse each ``ask-user[:question]`` tag into its question text (or
        # "" for the bare any-human marker), keeping the raw tag so the
        # inline answer form can strip it on submit. ``resolve_ask_question``
        # de-references a ``see-chunk-N`` redirect to the real overflow prose
        # so the operator sees the actual question, not the slug.
        asks = [
            {
                "tag": t,
                "question": store.resolve_ask_question(
                    it.todo_id, t[len("ask-user:") :]
                )
                if t.startswith("ask-user:")
                else "",
            }
            for t in it.asks
        ]
        # Attach the failure reason (job_summary) to each failed child job so
        # the operator sees *why* it died right here, not just "failed".
        jobs = [
            {
                "id": jid,
                "status": st,
                "reason": store.job_fail_reason(jid) if st == "failed" else None,
            }
            for jid, st in it.jobs
        ]
        out.append(
            {
                "todo_id": it.todo_id,
                "title": it.title,
                "blocked": it.blocked,
                "jobs": jobs,
                "asks": asks,
                "ask_tags": list(it.asks),
            }
        )
    return out


#: Document classes + section styles + per-genre scaffolds are shared with
#: the MCP surface — see :mod:`precis.draft.scaffolds` (imported above as
#: ``_DOC_TYPES`` / ``_DOC_TYPE_BRIEF`` / ``_SECTION_STYLES`` / ``_SCAFFOLDS``).


def _owner_workspace(store: Any, ref: Any) -> tuple[int, dict[str, Any]]:
    """The ``(ref_id, meta.workspace)`` that owns this draft's genre/brief:
    the owning project todo if linked, else the draft itself. Returns an
    empty workspace dict when unset or unreadable (defensive — a stub store
    just yields no genre rather than erroring)."""
    pid = _project_id(store, ref.id)
    rid = pid if pid is not None else ref.id
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        meta = (row[0] if row else None) or {}
        return rid, dict(meta.get("workspace") or {})
    except Exception:  # pragma: no cover - defensive (stub store / no pool)
        return rid, {}


def _doc_type(store: Any, ref: Any) -> str:
    """The draft's ``doc_type`` (genre), read from the owning project's
    ``meta.workspace`` (falling back to the draft's own meta). ``""`` when
    unset."""
    return str(_owner_workspace(store, ref)[1].get("doc_type") or "")


def _workspace_targets(store: Any, ref: Any) -> list[int]:
    """Refs whose ``meta.workspace`` should carry the genre/brief: the draft
    AND its owning project todo (if any). Writing both keeps ``_doc_type``
    (reads the project), the planner-prompt brief cascade (the project), and
    the section-prompt preview (reads the draft's own meta) in agreement."""
    targets = [ref.id]
    pid = _project_id(store, ref.id)
    if pid is not None and pid != ref.id:
        targets.append(pid)
    return targets


def _section_styles_for(store: Any, ref: Any) -> list[tuple[str, str]]:
    """The section styles to offer for this draft's genre (empty → no
    dropdown)."""
    return _SECTION_STYLES.get(_doc_type(store, ref), [])


def _chunk_addr(store: Any, handle: str) -> str | None:
    """Canonical ``dc<chunk_id>`` address for a posted draft-chunk handle.

    The reader posts the bare ``chunks.handle`` (the draft editable-document model base-58
    anchor, e.g. ``u9QG86``) — which ``get_draft_chunk`` resolves but
    ``edit(kind='draft')`` rejects (its guard only accepts the universal handles
    ``dc<chunk_id>`` / legacy ``¶<base58>`` form). Resolve the chunk and
    hand back the ``dc`` address so the per-heading style / list-kind
    forms reach the handler. ``None`` when the handle resolves to no
    chunk."""
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return None
    return handle_registry.format_handle("draft", chunk.chunk_id, chunk=True)


@router.get("/drafts", response_class=HTMLResponse)
@router.get("/drafts/", response_class=HTMLResponse)
async def index(q: str | None = None) -> Response:
    """Retired into the unified Drive surface (nav restructure) — redirects
    to the ``kind=draft`` facet preset, carrying a live query through so a
    bare ``?q=`` bookmark keeps searching. Mirrors ``routes/papers.py``'s
    WS1b retirement exactly. The reader (``/drafts/{ident}`` and
    everything under it) and the "+ New draft" creation flow
    (``POST /drafts/new``, still fed by :data:`_DOC_TYPES` via
    ``drive.py::_doctypes``) are unaffected.
    """
    params: list[tuple[str, str]] = [("k", "draft"), ("submitted", "1")]
    if q and q.strip():
        params.append(("q", q.strip()))
    return RedirectResponse(url="/drive?" + urlencode(params))


def _slugify(title: str) -> str:
    """A short kebab slug from a title (the draft's address)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "draft"


def _parse_id(body: str) -> int | None:
    m = re.search(r"id=(\d+)", body or "")
    return int(m.group(1)) if m else None


def _parse_tags(raw: str) -> list[str]:
    """Split a free-text tag field (comma / newline separated) into a
    clean, de-duplicated, order-preserving list of display tags. The
    user types loose labels ("CO2 reduction, ORCID 0000-…"); we keep the
    surface text for the planner's seed block and derive ``topic:`` axis
    tags separately (:func:`_topic_tags`)."""
    parts = re.split(r"[,\n]+", raw or "")
    out: list[str] = []
    for p in parts:
        t = p.strip()
        if t and t not in out:
            out.append(t)
    return out


def _topic_tags(labels: list[str]) -> list[str]:
    """Map free-text seed labels to ``topic:<slug>`` axis tags so the
    project is queryable (``search(tags=['topic:…'])``). Slugged like a
    draft slug; blanks dropped."""
    out: list[str] = []
    for label in labels:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
        tag = f"topic:{slug}" if slug else ""
        if tag and tag not in out:
            out.append(tag)
    return out


@router.post("/drafts/new")
async def new_draft(
    request: Request,
    title: str = Form(...),
    slug: str = Form(""),
    summary: str = Form(""),
    doctype: str = Form("paper"),
    cfp: str = Form(""),
    seeds: str = Form(""),
    tags: str = Form(""),
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
    the planner's **initial prompt**, not just standing context.
    ``meta.llm_tier='opus'`` is the dispatcher's auto-run signal, so the
    planner starts on the description as soon as the next ``dispatch``
    pass runs.

    ``cfp`` (proposal doctype only) is the slug/id of a call-for-proposal
    to attach via the ``has-requirement`` link — the planner then mirrors
    the call's required sections + word limits (no fixed proposal
    template). ``seeds`` (free text) + ``tags`` are the author's "things
    to read" starting material: stored at
    ``meta.workspace.extra['seeds']`` (cascades to the planner's
    ``## Seed material`` block) and the tags also become ``topic:`` axis
    tags on the project for later querying."""
    title = title.strip()
    if not title:
        return RedirectResponse(url="/drafts", status_code=303)
    # A title alone is not enough to write a document from: the description
    # IS the planner's initial prompt, and ``meta.llm_tier='opus'`` arms the
    # auto-writer the moment the draft is created. Require it (the client also marks the
    # field ``required``, but that is bypassable) rather than silently
    # falling back to a "Write a <doctype> titled …" instruction that sets
    # the planner writing from just the title.
    summary = summary.strip()
    if not summary:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "New draft error",
                "detail": (
                    "A description is required — a title alone isn't enough to "
                    "write a document from. Describe what to write; it becomes "
                    "the writer's initial prompt."
                ),
                "status": 400,
            },
            status_code=400,
        )
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

    # Seed "things to read": free text + loose tag labels. Stored under
    # the workspace's forward-compat ``extra`` so it cascades to every
    # planner tick (rendered as the ``## Seed material`` block). The tag
    # labels also map to ``topic:`` axis tags on the project root so the
    # whole project surfaces under ``search(tags=['topic:…'])``.
    seed_text = seeds.strip()
    tag_labels = _parse_tags(tags)
    if seed_text or tag_labels:
        workspace["seeds"] = {"text": seed_text, "tags": tag_labels}

    # The description IS the planner's initial prompt: it becomes the
    # project todo's body (``refs.title`` → the ``## Body`` block read by
    # ``plan_tick``). It is required (guarded above), so there is no
    # title-only fallback. ``meta.llm_tier='opus'`` is the closed-vocab
    # auto-run field the dispatcher keys on to mint the first ``plan_tick``
    # job (no ``meta.executor``).
    task_text = summary

    # 1) project root that owns the workspace + drives the planner. No
    # explicit rotation_root= needed — a root todo with meta.workspace
    # set is auto-stamped as a strategic root by TodoHandler.put (§M
    # facet normalization); llm_tier is explicit here because the
    # put()-time auto-default only fires for a *parented* child, not a
    # fresh root.
    project_tags = [*_topic_tags(tag_labels)]
    body, is_error = await await_dispatch(
        request,
        "put",
        {
            "kind": "todo",
            "text": task_text,
            "tags": project_tags,
            "meta": {"workspace": workspace, "llm_tier": "opus"},
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

    # 2a) proposal: attach the call-for-proposal via ``has-requirement`` so
    #     the planner mirrors its required sections + word limits (there is
    #     no fixed proposal scaffold; the call dictates the structure — see
    #     precis-proposal-help). Best-effort: a bad/stale cfp slug must not
    #     lose the already-created draft, so we log and carry on (the user
    #     can re-link from the proposal reader / MCP).
    cfp_ident = cfp.strip()
    if doctype == "proposal" and cfp_ident:
        _, link_err = await await_dispatch(
            request,
            "link",
            {
                "kind": "todo",
                "id": project_id,
                "target": f"cfp:{cfp_ident}",
                "rel": "has-requirement",
            },
        )
        if link_err:
            log.warning(
                "drafts: has-requirement link project=%s → cfp:%s failed",
                project_id,
                cfp_ident,
            )

    # 2b) scaffold the genre's standard sections: append
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

#: Hard cap on how many cites the retraction-watch button re-checks in one
#: request (``retraction_check_route``) — a big draft is one Crossref
#: round-trip per uncached cite, and this is a synchronous request a human
#: is deliberately waiting on (docs/backlog/retraction-status-downstream.md
#: item 3 accepts sync-with-a-cap for v1 rather than promoting to a job).
#: Chosen so a cold-cache walk stays well under a minute; a draft with more
#: cites than this gets a truncated, clearly-labelled check rather than an
#: open-ended wait.
_RETRACTION_CHECK_CAP = 40

#: Overall wall-clock budget for one retraction-watch walk. habanero's
#: per-call Crossref timeout bounds a single request, not the whole loop
#: over ``_RETRACTION_CHECK_CAP`` cites — this is the backstop so a slow or
#: half-dead Crossref can never pin the request (or its worker thread)
#: open indefinitely.
_RETRACTION_CHECK_BUDGET_S = 90.0


def _crossref_mailto() -> str | None:
    """Crossref polite-pool contact for a retraction re-check — the same
    env var the metadata-enrichment sweep reads
    (``workers/paper_meta_enrich.py``). Read fresh per call (not cached at
    import time) so rotating it in ops doesn't need a process restart."""
    return os.environ.get("PRECIS_CROSSREF_MAILTO") or None


def _retraction_paper_json(paper: Any) -> dict[str, Any]:
    """One ``CitedPaper`` (``precis.export.retraction``) as JSON — the
    shape both the read-only status route and the check-now route hand
    the export pane, so the client renders off one shape regardless of
    which one answered."""
    checked_at = paper.checked_at
    return {
        "ref_id": paper.ref_id,
        "slug": paper.slug,
        "title": paper.title,
        "status": paper.status,
        "label": paper.label,
        "blocks": paper.blocks,
        "never_checked": paper.never_checked,
        "checked_at": checked_at.isoformat()
        if hasattr(checked_at, "isoformat")
        else checked_at,
    }


def _retraction_report_json(report: Any) -> dict[str, Any]:
    """JSON body for ``DraftRetractionReport`` — shared by
    ``retraction_status_route`` (read, ``check=False``) and
    ``retraction_check_route`` (``check=True``)."""
    return {
        "ok": True,
        "checked": report.checked,
        "summary": report.summary(),
        "blocks_export": report.blocks_export,
        "total": len(report.papers),
        "retracted": [_retraction_paper_json(p) for p in report.retracted],
        "soft": [_retraction_paper_json(p) for p in report.soft],
        "unchecked": [_retraction_paper_json(p) for p in report.unchecked],
        "unresolved": report.unresolved,
    }


def _retraction_blocked_response(request: Request, report: Any) -> Response:
    """The shared "export blocked — retracted citation" page for both
    export routes (the docx GET and the pdf-job POST) — one wording, so
    the two surfaces can't drift on what they tell the user or how to get
    past it.

    Renders ``error.html.j2`` directly rather than a bare redirect: it is
    the idiom this same file already uses for a blocking, explain-why
    validation failure on a GET download (see the ``/pdf`` route's
    no-latexmk / compile-error branches), and it is the only place that
    can carry the retracted slugs and the override instructions where a
    plain 303 to the reader cannot.

    The override (``ignore_retractions=1`` / form field) is deliberate by
    design — a retracted citation making it into a published artifact is
    the exact failure mode this whole feature exists to catch, so the
    default is a hard stop; bypassing it always requires an explicit,
    separately-typed act, never a header or a default-on flag."""
    slugs = ", ".join(p.slug for p in report.retracted)
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "title": "Export blocked — retracted citation",
            "status": 409,
            "detail": (
                f"This draft cites {len(report.retracted)} retracted "
                f"paper(s): {slugs}. Exporting a retracted citation into a "
                "finished document is the exact failure mode this check "
                "exists to catch, so it hard-stops by default.\n\n"
                "To export anyway: use the override checkbox in the "
                "export panel, or add ignore_retractions=1 to this "
                "request (docx: query param; PDF: form field)."
            ),
        },
        status_code=409,
    )


def _safe_retraction_report(store: Any, ref: Any, **kw: Any) -> Any | None:
    """``draft_retraction_report`` wrapped defensively — a failure inside
    the walk must never take down the export it's gating or the reader
    the watch button lives on. Returns ``None`` on any exception; callers
    treat that as "no report available" and fail OPEN (never block an
    export on a check that itself couldn't run) — this is a best-effort
    safety net, not a compliance gate, and the worse outcome is "every
    export 500s", not "a rare retracted cite doesn't get flagged".

    The fail-open direction is a deliberate trade and the one thing to
    re-argue if this gate ever becomes a compliance requirement rather
    than an integrity nudge: a walk that cannot run produces no verdict,
    and refusing every export on an unavailable checker would wedge the
    user for a reason they cannot act on. The failure is logged loudly
    instead."""
    from precis.export.retraction import draft_retraction_report

    try:
        return draft_retraction_report(store, ref, **kw)
    except Exception:
        log.error(
            "drafts: retraction report failed for draft=%s — treating as "
            "unavailable (see _safe_retraction_report's KNOWN BUG note)",
            getattr(ref, "id", None),
            exc_info=True,
        )
        return None


@router.get("/drafts/{ident}/export.docx")
async def export_docx_route(request: Request, ident: str) -> Response:
    """Synchronous .docx export — renders the draft and streams it back as
    a download. Toolchain-free (python-docx), so this "just works"; the
    rendering runs off the event loop.

    ``?citations=endnote`` emits native EndNote *Cite While You Write*
    fields (``ADDIN EN.CITE`` + ``EN.REFLIST``) so EndNote recognizes and can
    reformat the citations; the default (``plain``) is a numbered ``[n]`` +
    References section that needs no add-in.

    ``?sources=1`` returns a ``.zip`` instead — the ``.docx`` plus a
    ``sources/`` folder of every cited paper/datasheet PDF the host holds
    (Word can't embed PDF pages inline the way the compiled-PDF path can),
    with a ``manifest.txt`` listing anything that couldn't be located.

    ``?ignore_retractions=1`` overrides a retraction block (see
    ``_retraction_blocked_response``). The gate itself only ever *reads*
    stored retraction state (``check=False``, no network) — a download
    click must never turn into a minute of Crossref latency; the
    deliberate live re-check is the watch button
    (``retraction_check_route``) the user presses and waits on on
    purpose. See ``precis.export.retraction``'s module docstring for the
    read/check split this mirrors."""
    from precis.export.docx import export_docx

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)

    ignore_retractions = request.query_params.get("ignore_retractions") in (
        "1",
        "true",
        "yes",
    )
    report = _safe_retraction_report(store, ref)
    if report is not None and report.blocks_export:
        if not ignore_retractions:
            return _retraction_blocked_response(request, report)
        # The override is deliberate and meant to be rare — leave a trace.
        # This ideally lands in the export's sources appendix
        # (precis.export.sources), which is outside this route's file
        # ownership; a server log line is the interim trace — see
        # docs/backlog/retraction-status-downstream.md item 2.
        log.warning(
            "drafts: export override — draft=%s (%s) retracted cites=%s",
            ref.id,
            ref.slug,
            [p.slug for p in report.retracted],
        )

    citations = (
        "endnote" if request.query_params.get("citations") == "endnote" else "plain"
    )
    with_sources = request.query_params.get("sources") in ("1", "true", "yes")
    name = str(ref.slug or ref.id)
    work = Path(tempfile.mkdtemp(prefix="precis-docx-"))
    out = work / f"{name}.docx"
    docx_result = await asyncio.to_thread(
        export_docx, store, ref, target_path=out, citations=citations
    )
    if not with_sources:
        return FileResponse(out, filename=f"{name}.docx", media_type=_DOCX_MEDIA)

    from precis.export.sources import build_sources_zip

    zip_path = work / f"{name}-bundle.zip"
    await asyncio.to_thread(
        build_sources_zip,
        store,
        ref,
        zip_path,
        cited_slugs=docx_result.cited_slugs,
        report_path=out,
    )
    return FileResponse(
        zip_path, filename=f"{name}-bundle.zip", media_type="application/zip"
    )


@router.get("/drafts/{ident}/papers.zip")
async def papers_zip_route(request: Request, ident: str) -> Response:
    """Zip up the draft's cited paper/datasheet PDFs + a manifest.

    Resolves the draft's cited sources (the exact bibliography set) to the
    PDFs *this host* holds and streams them as a ``.zip`` with a
    ``manifest.txt`` bibliography. Sources the host can't locate are listed
    in the manifest (the corpus is a per-host mount, so a bundle can be
    legitimately incomplete)."""
    from precis.export.sources import build_sources_zip

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    name = str(ref.slug or ref.id)
    zip_path = Path(tempfile.mkdtemp(prefix="precis-papers-")) / f"{name}-papers.zip"
    await asyncio.to_thread(build_sources_zip, store, ref, zip_path)
    return FileResponse(
        zip_path, filename=f"{name}-papers.zip", media_type="application/zip"
    )


@router.post("/drafts/{ident}/export.pdf")
async def export_pdf_route(request: Request, ident: str) -> Response:
    """Start a ``draft_export`` job (LaTeX → PDF). The job runs on a
    worker; its progress logs + result land under the draft's project on
    the task page. Redirects back to the reader.

    A ``sources=1`` form field additionally bundles every cited
    paper/datasheet PDF the worker holds as a ``pdfpages`` appendix.

    A ``ignore_retractions=1`` form field overrides a retraction block —
    see ``_retraction_blocked_response``. The gate here is the same
    stored-state-only read (``check=False``, no network) as the docx
    route: enqueuing a job must not itself wait on Crossref."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    form = await request.form()
    with_sources = str(form.get("sources") or "") in ("1", "true", "yes", "on")
    ignore_retractions = str(form.get("ignore_retractions") or "") in (
        "1",
        "true",
        "yes",
        "on",
    )

    report = _safe_retraction_report(store, ref)
    if report is not None and report.blocks_export:
        if not ignore_retractions:
            return _retraction_blocked_response(request, report)
        log.warning(
            "drafts: export override — draft=%s (%s) retracted cites=%s",
            ref.id,
            ref.slug,
            [p.slug for p in report.retracted],
        )

    slug = str(ref.slug or ref.id)
    params: dict[str, Any] = {"draft": slug}
    idem = f"draft_export:{slug}"
    if with_sources:
        params["include_sources"] = True
        idem = f"draft_export:{slug}:sources"
    return await redirect_or_error(
        request,
        "put",
        {
            "kind": "job",
            "job_type": "draft_export",
            "parent_id": _job_parent(store, ref),
            "params": params,
            "idem_key": idem,
        },
        redirect=f"/drafts/{ident}",
        error_title="PDF export error",
    )


@router.get("/drafts/{ident}/retraction-status")
async def retraction_status_route(request: Request, ident: str) -> Response:
    """Read-only retraction state of everything this draft cites — the
    exact same no-network read the export gate uses
    (``draft_retraction_report(check=False)``). Backs the export pane's
    passive "N of M cited papers have never been checked" warning: the
    pane fetches this on load, so the warning (and the override checkbox,
    when a cite is already known-retracted) is visible before the user
    ever presses "check retractions"."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    report = _safe_retraction_report(store, ref)
    if report is None:
        return JSONResponse(
            {"ok": False, "error": "retraction status unavailable"}, status_code=502
        )
    return JSONResponse(_retraction_report_json(report))


@router.post("/drafts/{ident}/retraction-check")
async def retraction_check_route(request: Request, ident: str) -> Response:
    """The retraction-watch button — trigger 2 of
    ``docs/backlog/retraction-check-triggers.md``: re-checks the draft's
    cited papers through Crossref (TTL-gated, so a same-day re-press is
    nearly free) and reports per-paper status. ``force=1`` ignores the
    TTL — without it, pressing the button twice in one day is a silent
    no-op and reads as broken.

    Synchronous-with-a-cap for v1
    (``docs/backlog/retraction-status-downstream.md`` item 3): the user
    is deliberately waiting on this button, but a large draft is one
    Crossref round-trip per uncached cite. The walk is capped at
    ``_RETRACTION_CHECK_CAP`` cites and wrapped in an overall wall-clock
    budget (``_RETRACTION_CHECK_BUDGET_S``) so neither the request nor
    its worker thread can hang indefinitely on a slow/unreachable
    Crossref — a truncated walk says so rather than silently
    under-reporting."""
    from precis.export.retraction import cited_paper_refs, draft_retraction_report

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    form = await request.form()
    force = str(form.get("force") or "") in ("1", "true", "yes", "on")

    refs, unresolved = cited_paper_refs(store, ref)
    total = len(refs)
    truncated = total > _RETRACTION_CHECK_CAP
    selected = refs[:_RETRACTION_CHECK_CAP] if truncated else refs
    cited_slugs = [r.slug for r in selected]

    def _run() -> Any:
        return draft_retraction_report(
            store,
            ref,
            cited_slugs=cited_slugs,
            check=True,
            force=force,
            mailto=_crossref_mailto(),
        )

    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=_RETRACTION_CHECK_BUDGET_S
        )
    except TimeoutError:
        # asyncio.wait_for cancels our await, but the underlying thread
        # keeps running to completion in the background — it just finishes
        # unobserved. What matters here is that this request (and the
        # event loop) doesn't wait on it.
        log.warning("drafts: retraction-check timed out for %s", ident)
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"timed out after {_RETRACTION_CHECK_BUDGET_S:.0f}s "
                    "waiting on Crossref — try again shortly"
                ),
            },
            status_code=504,
        )
    except Exception:  # pragma: no cover - defensive, see _safe_retraction_report
        log.error("drafts: retraction-check failed for %s", ident, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "retraction check failed — see server log"},
            status_code=502,
        )

    payload = _retraction_report_json(report)
    payload["unresolved"] = unresolved
    payload["truncated"] = truncated
    if truncated:
        payload["truncated_total"] = total
        payload["summary"] = (
            f"checked {len(cited_slugs)} of {total} cited papers (capped) — "
            f"{report.summary()}"
        )
    return JSONResponse(payload)


@router.post("/drafts/{ident}/remarkable")
async def send_remarkable_route(request: Request, ident: str) -> Response:
    """Start a ``remarkable_send`` job — export the draft in reMarkable mode
    (RM2 geometry + citations as self-contained footnotes), compile the PDF,
    and upload it to the tablet. Runs on a worker; progress + result land
    under the draft's project on the task page. Redirects back to the reader.

    Only meaningful when a reMarkable credential is configured — the button
    is hidden otherwise — but we re-check here so a stale page can't enqueue
    a job that would just fail."""
    from precis.export.remarkable import remarkable_configured

    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    if not remarkable_configured(store):
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "reMarkable not configured",
                "detail": "no reMarkable device credential is set — add "
                "REMARKABLE_RMAPI_CONFIG (or REMARKABLE_TOKEN) at /secrets.",
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
            "job_type": "remarkable_send",
            "parent_id": _job_parent(store, ref),
            "params": {"draft": slug},
            "idem_key": f"remarkable_send:{slug}",
        },
        redirect=f"/drafts/{ident}",
        error_title="reMarkable send error",
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
    """Singular ``/draft/<id>`` → the smartdraft reader (the sole draft reader
    since the classic reader was retired)."""
    return RedirectResponse(url=f"/smartdraft/{ident}", status_code=307)


@router.get("/drafts/{ident}")
async def reader(ident: str) -> RedirectResponse:
    """The classic virtual-scroll reader is retired — ``/smartdraft/{ident}`` is
    the sole draft reader now. Kept as a 307 redirect so every bookmark, quest
    link, and ``/c/<handle>`` deep-link still lands on the draft (307 preserves
    any ``?focus=`` query the anchor path appends)."""
    return RedirectResponse(url=f"/smartdraft/{ident}", status_code=307)


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


def _pdf_cache_token(store: Any, ref: Any) -> str:
    """Cache key for a compiled PDF — the chunk-level ``_draft_version``
    token *plus* the ref's ``updated_at`` epoch.

    ``_draft_version`` only counts ``chunk_events``, so it misses ref-level
    metadata the export actually renders — the author byline, title, year —
    because editing those columns emits no chunk event. Without the
    ``updated_at`` component, setting the author via the Authors panel left
    the token unchanged and the stale pre-edit PDF (with the ``meta.author``
    / ``precis`` fallback byline) was served from cache. ``mark_viewed``
    touches only ``last_viewed_at`` (not ``updated_at``), so a plain page
    view does not bump this token and force a needless recompile."""
    version = _draft_version(store, ref.id)
    updated = getattr(ref, "updated_at", None)
    rev = int(updated.timestamp()) if updated is not None else 0
    return f"{version}.{rev}"


def _pdf_cache_dir(ref_id: int, version: int | str, *, sources: bool = False) -> Path:
    """Per-(draft, version) build dir for the compiled PDF. Lives under
    the system temp so it survives within a deploy and is cheap to
    discard; a new version compiles into a fresh dir, so a stale PDF is
    never served. ``version`` is the composite token from
    :func:`_pdf_cache_token` (chunk version + ref ``updated_at``).

    ``sources=True`` uses a distinct ``<version>-src`` dir so the
    self-contained (pdfpages-appendix) PDF caches separately from the plain
    one — both variants can coexist for the same version."""
    import tempfile

    tag = f"{version}-src" if sources else str(version)
    return Path(tempfile.gettempdir()) / "precis-draft-pdf" / str(ref_id) / tag


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
    with_sources = request.query_params.get("sources") in ("1", "true", "yes")
    cache_token = _pdf_cache_token(store, ref)
    cache_dir = _pdf_cache_dir(ref.id, cache_token, sources=with_sources)
    pdf_path = cache_dir / "main.pdf"
    suffix = "-with-sources" if with_sources else ""
    # A cast draft downloads as its human stem (``morning_brief_<date>.pdf``),
    # matching the mp3 on the feed; other drafts keep their slug.
    from precis.reading.cast_common import export_basename_for_meta

    base = export_basename_for_meta(getattr(ref, "meta", None)) or (ref.slug or ref.id)
    filename = f"{base}{suffix}.pdf"

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
        export_draft(store, ref, target_dir=cache_dir, include_sources=with_sources)
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


@router.get("/drafts/blob/{handle}")
async def chunk_blob(request: Request, handle: str) -> Response:
    """Raw bytes for a figure chunk's image — the ``<img>``
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


def _is_dc(handle: str) -> bool:
    """True for a universal draft-chunk handle (``dc<id>``)."""
    p = handle_registry.parse(handle)
    return bool(p and p[0] == "draft" and p[1])


def _marks_view(store: Any, marks: dict[str, Any]) -> dict[str, Any]:
    """A client-friendly render of the sticky set: ``pens`` (dc handles) + an
    ``eyes`` list carrying each eye's kind/title/is-draft-chunk so the tray can
    label a promoted ring target (``pa721 — Rigidity percolation``)."""
    eyes_map: dict[str, str] = marks.get("eyes") or {}
    parsed = {h: handle_registry.parse(h) for h in eyes_map}
    ref_ids = [p[2] for p in parsed.values() if p and not p[1]]
    titles = store.fetch_refs_by_ids(ref_ids) if ref_ids else {}
    eyes: list[dict[str, Any]] = []
    for h, ext in eyes_map.items():
        p = parsed[h]
        is_chunk = bool(p and p[1])
        title = ""
        if p and not is_chunk:
            r = titles.get(p[2])
            title = (getattr(r, "title", None) or "") if r else ""
        eyes.append(
            {
                "handle": h,
                "extent": ext,
                "kind": p[0] if p else "?",
                "title": title,
                "dc": is_chunk,
            }
        )
    return {"pens": list(marks.get("pens") or []), "eyes": eyes}


@router.post("/drafts/{ident}/marks")
async def edit_marks(request: Request, ident: str) -> JSONResponse:
    """Toggle a pen/eye on draft chunks in the reader's sticky working set
    (hand-driven). Body ``{op:'pen'|'eye'|'clear', handles:[dc…],
    on?:bool, extent?:str}``. Penning auto-opens an eye; ``clear`` wipes. Returns
    the stored marks so the client re-syncs its glyph sets."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    op = str(payload.get("op") or "")
    handles = [str(h) for h in (payload.get("handles") or []) if str(h).strip()]
    on = payload.get("on")
    marks = draft_eyes.load_marks(store, ref.id)
    if op == "clear":
        marks = {"pens": [], "eyes": {}, "updated_at": None}
    elif op == "pen":
        for h in handles:
            draft_eyes.toggle_pen(marks, h, on=on)
    elif op == "eye":
        ext = payload.get("extent")
        for h in handles:
            draft_eyes.toggle_eye(marks, h, on=on, extent=ext)
    else:
        return JSONResponse({"ok": False, "error": f"bad op {op!r}"}, status_code=400)
    stored = draft_eyes.save_marks(store, ref.id, marks)
    return JSONResponse({"ok": True, "marks": _marks_view(store, stored)})


@router.post("/drafts/{ident}/human-review")
async def edit_human_review(request: Request, ident: str) -> JSONResponse:
    """Record the human reviewer's sign-off on one draft block — the ✓
    gutter checkbox (mirrors ``edit_marks``'s pen/eye toggle; distinct from
    the automated per-heading ``POST /drafts/{ident}/review`` "review ▾"
    menu, which files a *reviewer todo*, not a ledger row). Body ``{dc}``
    (either the reader's base-58 handle or the ``dc<id>`` address —
    resolved/validated via :func:`_chunk_addr`, like the table/text
    editors). Writes through ``edit(kind='draft', review='human')`` so the
    review ledger stays single-sourced with the MCP/CLI verb — this route
    never calls ``Store.record_review`` directly — then returns the
    chunk's fresh per-checker status (``Store.review_status_for_chunk``)
    so the client re-syncs the button. Also carries a fresh ``rollup``
    (``Store.review_rollup_for_draft``) so the toolbar badge can refresh
    without a page reload.

    This route only *sets* the human checkmark — un-review (retract) is
    ``POST /drafts/{ident}/review/retract``,
    a separate endpoint over ``Store.retract_review``."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    handle = str(payload.get("dc") or payload.get("handle") or "")
    addr = _chunk_addr(store, handle) if handle else None
    if addr is None:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    args: dict[str, Any] = {
        "kind": "draft",
        "id": addr,
        "review": "human",
        "verdict": str(payload.get("verdict") or "approved"),
    }
    body, is_error = await await_dispatch(request, "edit", args)
    if is_error:
        return JSONResponse({"ok": False, "error": body}, status_code=400)
    chunk = store.get_draft_chunk(handle)
    status = store.review_status_for_chunk(chunk.chunk_id) if chunk is not None else []
    return JSONResponse(
        {
            "ok": True,
            "review": _review_json(status),
            "rollup": _rollup_json(store, ref.id),
        }
    )


@router.post("/drafts/{ident}/request-ws")
async def request_change_ws(request: Request, ident: str) -> JSONResponse:
    """File a change request. With a hand-curated working set, the todo gets
    ``meta.working_set = {eyes, edit_hint}`` (+ ``meta.anchor`` = first pen). With
    **nothing pinned**, it falls back to anchoring on the caller's current focus
    (``anchor``) so the ask still works on the current para + its fisheye. Body
    ``{text, model, anchor?, placement?, reasoning?, temperature?}`` — the last
    three are the optional structured selection, threaded onto
    ``meta.llm_select`` alongside the ``model`` alias's ``meta.llm_tier``."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty request"}, status_code=422)
    marks = draft_eyes.load_marks(store, ref.id)
    has_marks = bool(marks["pens"] or marks["eyes"])
    model = str(payload.get("model") or "big")
    tier = model if model in _PLANNER_MODELS else "big"
    # Anchor at the first pen (else the first draft-chunk eye, else the caller's
    # current focus) — in the base-58 form the classic anchor modules expect.
    anchor_dc = (
        marks["pens"][0]
        if marks["pens"]
        else next((h for h in marks["eyes"] if _is_dc(h)), None)
    ) or (str(payload.get("anchor") or "").strip() or None)
    anchor = None
    if anchor_dc:
        chunk = store.get_draft_chunk(anchor_dc, kind="draft")
        anchor = chunk.handle if chunk is not None else None
    if not has_marks and not anchor:
        return JSONResponse(
            {
                "ok": False,
                "error": "nothing to work on — focus a paragraph or pin some",
            },
            status_code=422,
        )
    meta: dict[str, Any] = {"llm_tier": tier}
    if has_marks:
        meta["working_set"] = draft_eyes.to_working_set_meta(marks)
    if anchor:
        meta["anchor"] = anchor
    llm_select = llm_select_from_payload(
        placement=payload.get("placement"),
        reasoning=payload.get("reasoning"),
        temperature=payload.get("temperature"),
    )
    if llm_select:
        meta["llm_select"] = llm_select
    args: dict[str, Any] = {
        "kind": "todo",
        "text": text,
        "meta": meta,
    }
    project = _project_id(store, ref.id)
    if project is not None:
        args["parent_id"] = project
    body, is_error = await await_dispatch(request, "put", args)
    if is_error:
        return JSONResponse({"ok": False, "error": body}, status_code=400)
    return JSONResponse({"ok": True, "anchor": anchor})


@router.post("/drafts/{ident}/text")
async def edit_text_inline(
    request: Request,
    ident: str,
    handle: str = Form(...),
    text: str = Form(...),
    base_sha: str = Form(""),
) -> JSONResponse:
    """Direct (non-LLM) in-place text edit of one draft block — the inline
    editor's save (docs/backlog/draft-inline-editor.md, slice 2a).

    HARD-blocks (422, no write) a reference *this edit* newly breaks, so the
    editor can bounce the author back with the offending tokens ("comes back
    at you if you broke something serious"). Otherwise it writes through the
    ``edit`` verb — so link-sync + the advisory hints stay single-sourced with
    the MCP/CLI path — and returns any soft ``warnings`` (the ⚠ hint lines) for
    a non-blocking note. Optimistic concurrency via ``base_sha``: a stale token
    yields 409 so a concurrent agent edit isn't clobbered."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    chunk = store.get_draft_chunk(handle)
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    old_text = chunk.text or ""
    if text == old_text:  # no-op: don't churn content_sha / re-embed
        return JSONResponse({"ok": True, "warnings": [], "noop": True})
    # Hard gate: refuse to save a reference this edit newly breaks. Pre-existing
    # dead refs stay soft (see DraftHandler._newly_dangling).
    handler = get_runtime(request).hub.handler_for("draft")
    bad_chunk, bad_find = handler._newly_dangling(text, old_text)
    if bad_chunk or bad_find:
        toks = [f"[{h}]" for h in bad_chunk] + [f"finding #{s}" for s in bad_find]
        return JSONResponse(
            {
                "ok": False,
                "hard": toks,
                "error": "unresolved reference(s) introduced: "
                + ", ".join(toks)
                + " — fix or remove before saving",
            },
            status_code=422,
        )
    # Write through the edit verb (validation + link-sync + hints single-sourced).
    args: dict[str, Any] = {"kind": "draft", "id": f"¶{handle}", "text": text}
    if base_sha.strip():
        args["base_sha"] = base_sha.strip()
    body, is_error = await await_dispatch(request, "edit", args)
    if is_error:
        stale = "changed since you read" in body
        return JSONResponse(
            {"ok": False, "error": body}, status_code=409 if stale else 400
        )
    warnings = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("⚠")]
    return JSONResponse({"ok": True, "warnings": warnings})


def _coerce_cell(value: Any) -> Scalar:
    """Coerce one grid-editor cell to the JSON scalar stored in ``meta.table``.

    The browser grid posts every cell as a string. Recover typed numbers —
    so they land in the ``numerics`` index and export as numbers, not quoted
    text — behind a round-trip guard: only coerce when ``str(n)`` reproduces
    the trimmed input exactly, so ``"007"`` / ``"1e3"`` / ``"1.20"`` stay
    verbatim strings rather than being silently renormalised. A blank cell is
    a genuinely empty value → ``None`` (which ``normalize_table`` accepts and
    the markdown renders as an empty cell)."""
    if not isinstance(value, str):
        return value  # client already sent a typed scalar — trust it
    s = value.strip()
    if s == "":
        return None
    try:
        if str(int(s)) == s:
            return int(s)
    except ValueError:
        pass
    try:
        if str(float(s)) == s:
            return float(s)
    except ValueError:
        pass
    return value


@router.post("/drafts/{ident}/table")
async def edit_table_inline(request: Request, ident: str) -> JSONResponse:
    """Direct (non-LLM) structured edit of a data-table chunk — the grid
    editor's save. The browser posts JSON
    ``{handle, base_sha, header:[…], rows:[[…]], caption}``; cells arrive as
    strings and are coerced back to JSON scalars (numbers stay numbers). The
    write goes through the ``edit`` verb so the strict ``normalize_table``
    gate + link-sync stay single-sourced with the MCP/CLI path — a
    ragged/empty-header table bounces 422 with the linter's own message so the
    author fixes it in place, and a stale ``base_sha`` yields 409.

    Structured-only by design: the table's markdown text is *derived*, so
    there is no free-text edit path (the inline text editor excludes tables).
    Editing a table that predates ``meta.table`` (a Marker/LaTeX import)
    through this route rewrites it into the canonical structured form."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    handle = str(payload.get("handle") or "")
    addr = _chunk_addr(store, handle)
    if addr is None:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    header = payload.get("header") or []
    rows_in = payload.get("rows") or []
    if not isinstance(header, list) or not isinstance(rows_in, list):
        return JSONResponse(
            {"ok": False, "error": "header and rows must be lists"}, status_code=422
        )
    rows = [
        [_coerce_cell(c) for c in row] if isinstance(row, list) else row
        for row in rows_in
    ]
    args: dict[str, Any] = {
        "kind": "draft",
        "id": addr,
        "table": {"header": [str(h) for h in header], "rows": rows},
        "caption": str(payload.get("caption") or ""),
    }
    base_sha = str(payload.get("base_sha") or "").strip()
    if base_sha:
        args["base_sha"] = base_sha
    body, is_error = await await_dispatch(request, "edit", args)
    if is_error:
        stale = "changed since you read" in body
        return JSONResponse(
            {"ok": False, "error": body}, status_code=409 if stale else 422
        )
    warnings = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("⚠")]
    return JSONResponse({"ok": True, "warnings": warnings})


@router.post("/drafts/{ident}/validate-refs")
async def validate_refs(
    request: Request,
    ident: str,
    text: str = Form(""),
) -> JSONResponse:
    """Live ref validation for the inline editor's squiggle (slice 2b-ii):
    return the on-screen forms of every reference in ``text`` that resolves to
    nothing, so the editor can underline them as you type. Reuses the same
    dangling-token detection as the save-time gate — the squiggle is the live
    face of the hard gate."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"dangling": []})
    handler = get_runtime(request).hub.handler_for("draft")
    chunk = handler._dangling_chunk_tokens(text)
    find = handler._dangling_finding_tokens(text)
    dangling = [f"[{h}]" for h in chunk] + [f"#{s}" for s in find]
    return JSONResponse({"dangling": dangling})


@router.get("/drafts/{ident}/ref-search")
async def ref_search(request: Request, ident: str, q: str = "") -> JSONResponse:
    """Autocomplete for the inline editor's ``[`` picker: title-search held
    papers, returning the insertable citation token + a display label. Keeps it
    to papers for now (the common authoring need); other kinds can join later."""
    store = get_store(request)
    q = q.strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    ids = store.find_papers_by_title(kind="paper", q=q, limit=8)
    refs = store.fetch_refs_by_ids(ids)
    results = []
    for rid in ids:
        r = refs.get(rid)
        slug = getattr(r, "slug", None) if r is not None else None
        if not slug:
            continue
        results.append(
            {
                "token": f"[§{slug}]",
                "label": getattr(r, "title", None) or slug,
                "sub": slug,
            }
        )
    return JSONResponse({"results": results})


@router.post("/drafts/{ident}/block")
async def add_block(
    request: Request,
    ident: str,
    after: str = Form(...),
    chunk_kind: str = Form("paragraph"),
) -> JSONResponse:
    """Insert a new empty prose block right after ``after`` (same parent, a
    fractional ``pos`` between it and its next sibling) — the inline editor's
    ``+`` affordance (docs/backlog/draft-inline-editor.md, slice 2b). Returns
    the new block's handle so the client can hydrate and open it for editing.

    Goes straight to ``store.add_chunks`` rather than the ``put`` verb: the
    verb rejects empty ``text=`` (an agent-ergonomics guard against blank
    chunks), but "add a blank paragraph then type into it" is exactly the
    human flow here."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    anchor = store.get_draft_chunk(after)
    if anchor is None or anchor.ref_id != ref.id:
        return JSONResponse(
            {"ok": False, "error": "anchor block not found"}, status_code=404
        )
    kind = chunk_kind if chunk_kind in _EDITABLE_KINDS else "paragraph"
    chunks = store.add_chunks(
        ref_id=ref.id, chunk_kind=kind, text="", at={"after": f"¶{after}"}
    )
    new = chunks[0]
    return JSONResponse({"ok": True, "handle": new.handle, "dc": new.dc})


@router.post("/drafts/{ident}/block/{handle}/split")
async def split_block(
    request: Request,
    ident: str,
    handle: str,
    before: str = Form(""),
    after: str = Form(""),
) -> JSONResponse:
    """Split one block at the caret (inline editor's Enter): the current chunk
    keeps ``before`` (and its handle, so cross-refs to it survive), a new chunk
    carrying ``after`` lands right after it. Returns the new block's handle so
    the client can open it with the caret at its start."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    chunk = store.get_draft_chunk(handle)
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    # Splitting a heading/term yields a paragraph tail; a paragraph/item keeps
    # its kind so a list stays a list.
    tail_kind = (
        chunk.chunk_kind
        if chunk.chunk_kind
        in (
            "paragraph",
            "item",
            "aside",
            "box",
            "callout",
        )
        else "paragraph"
    )
    # base_sha = the sha of the pre-split text this handler read above — a
    # concurrent edit of this chunk between that read and this write raises
    # BadInput instead of silently clobbering it (gr176088). Mirror the
    # edit_text_inline/edit_table_inline conflict shape so the client's XHR
    # handler gets a consistent, machine-readable 409 — any other BadInput
    # (a genuinely bad request) still surfaces via the global handler.
    try:
        store.edit_text(handle, before, base_sha=content_sha(chunk.text or ""))
    except BadInput as exc:
        if "changed since you read" in str(exc):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        raise
    new = store.add_chunks(
        ref_id=ref.id,
        chunk_kind=tail_kind,
        text=after,
        at={"after": f"¶{handle}"},
        split=False,
    )[0]
    handler = get_runtime(request).hub.handler_for("draft")
    handler._sync_draft_links(ref.id)
    return JSONResponse({"ok": True, "handle": new.handle, "dc": new.dc})


@router.post("/drafts/{ident}/block/{handle}/merge-prev")
async def merge_prev_block(
    request: Request,
    ident: str,
    handle: str,
    text: str = Form(""),
) -> JSONResponse:
    """Backspace at the start of a block: append its (current, client-supplied)
    text onto the previous block and retire this one. Covers both cases — an
    empty block just gets deleted (caret lands at the end of the previous), a
    non-empty one merges. ``text`` is the live editor text so unsaved keystrokes
    aren't lost. Returns the previous block's handle + the caret offset (the
    join point). No-ops (rather than corrupting structure) when there's no
    previous block, the previous isn't mergeable prose (a heading), or this
    block has children."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    chunk = store.get_draft_chunk(handle)
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    order = store.reading_order(ref.id)
    idx = next((i for i, c in enumerate(order) if c.handle == handle), None)
    if idx is None or idx == 0:
        return JSONResponse({"ok": True, "noop": True})
    prev = order[idx - 1]
    # Only join text into mergeable prose; an empty block may still fold into a
    # non-prose previous (it's just a delete-and-go-to-end).
    if text != "" and prev.chunk_kind not in _MERGE_KINDS:
        return JSONResponse({"ok": True, "noop": True})
    caret = len(prev.text or "")
    # NB: no base_sha guard here yet (unlike split_block / _substitute). This
    # path does a retire_chunk + an edit_text that must be atomic — retire
    # first (its own noop-on-children guard is the "is this mergeable" check),
    # then append. Adding an optimistic base_sha to the edit alone can't be
    # made loss-free without wrapping both ops in one transaction: retire-first
    # orphans the retire on a conflict; edit-first defeats the childless guard.
    # Deferred to gr176088 Part 2 (structural-op locking / transactional draft
    # mutations), where retire+edit can be guarded as a unit.
    try:
        store.retire_chunk(handle)  # childless prose; refuses (→ noop) if it has kids
    except BadInput:
        return JSONResponse({"ok": True, "noop": True})
    if text:
        store.edit_text(prev.handle, (prev.text or "") + text)
    handler = get_runtime(request).hub.handler_for("draft")
    handler._sync_draft_links(ref.id)
    return JSONResponse({"ok": True, "handle": prev.handle, "caret": caret})


@router.post("/drafts/{ident}/block/{handle}/delete")
async def delete_block(
    request: Request,
    ident: str,
    handle: str,
    cascade: str = Form(""),
) -> JSONResponse:
    """Retire (soft-delete) a block. ``cascade=1`` removes a heading's whole
    subtree (the client sends it after confirming the section delete); a plain
    block retires on its own. Routed through the ``delete`` verb so retire +
    link-sync stay single-sourced with the MCP/CLI path."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    chunk = store.get_draft_chunk(handle)
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    args: dict[str, Any] = {"kind": "draft", "id": f"¶{handle}"}
    if cascade.strip():
        args["mode"] = "cascade"
    body, is_error = await await_dispatch(request, "delete", args)
    if is_error:
        return JSONResponse({"ok": False, "error": body}, status_code=400)
    return JSONResponse({"ok": True})


#: Old reviewer-menu vocabulary → the unified ledger's lens names (decided
#: fallback): one checker namespace —
#: ``structural``/``deep_review`` are kept as accepted ``lens=`` ALIASES so
#: an old caller (bookmark, script) still works, but every mint now lands
#: under ``structure``/``adversarial`` in ``chunk_review``, never the old
#: names.
_LENS_ALIASES: dict[str, str] = {
    "structural": "structure",
    "deep_review": "adversarial",
}


@router.post("/drafts/{ident}/review")
async def review_block(request: Request, ident: str) -> JSONResponse:
    """Run the incremental review fanout (``quest.review_fanout.
    mint_review_fanout``) over a draft —
    replacing this route's old ``structural``/``deep_review`` reviewer
    vocabulary and its own per-heading todo-minting.

    Body ``{lens, dc?, only_dirty?}``:

    - ``lens`` — one of ``flow`` | ``cites`` | ``structure`` | ``adversarial``
      | ``toc`` | ``all`` (plus the accepted aliases ``structural`` →
      ``structure``, ``deep_review`` → ``adversarial``). ``all`` mints
      :data:`ALL_LENSES`, plus :data:`DOC_LENSES` when the scope is the
      whole draft (``mint_review_fanout`` itself gates ``doc_lenses`` to
      ``scope is None``, so passing them for a ``dc``-scoped call is a
      harmless no-op). ``toc`` is document-altitude-only: mints ``doc_lenses
      = ('toc',)`` and is rejected (400) when ``dc`` scopes it to a
      chunk/subtree.
    - ``dc`` — a block handle (chunk or heading) narrowing the scope to that
      chunk or its subtree (``mint_review_fanout``'s own ``scope=``);
      omitted → whole draft.
    - ``only_dirty`` — pass-through to ``mint_review_fanout``. Defaults to
      ``True`` for a whole-draft call (the cheap "run outstanding checks"
      re-check loop) and ``False`` for a ``dc``-scoped call (an explicit
      "run this paragraph/section" click always re-runs, rather than
      silently no-op'ing on an already-approved pair); either default can be
      overridden explicitly.

    Returns the fanout's summary dict as JSON (``parent_id``, ``minted``,
    ``skipped``, ``unsettled_skipped``, ``author_minted``, ``chunks_seen``)
    plus ``ok``."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    lens_raw = str(payload.get("lens") or "").strip()
    lens = _LENS_ALIASES.get(lens_raw, lens_raw)
    dc = str(payload.get("dc") or "").strip()
    scope_chunk_id: int | None = None
    if dc:
        chunk = store.get_draft_chunk(dc)
        if chunk is None or chunk.ref_id != ref.id:
            return JSONResponse(
                {"ok": False, "error": "block not found"}, status_code=404
            )
        scope_chunk_id = chunk.chunk_id
    lenses: tuple[str, ...]
    doc_lenses: tuple[str, ...]
    if lens == "toc":
        if scope_chunk_id is not None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "the toc lens is whole-draft only — omit dc",
                },
                status_code=400,
            )
        lenses, doc_lenses = (), ("toc",)
    elif lens == "all":
        lenses, doc_lenses = ALL_LENSES, DOC_LENSES
    elif lens in ALL_LENSES:
        lenses, doc_lenses = (lens,), ()
    else:
        return JSONResponse(
            {"ok": False, "error": f"unknown lens {lens_raw!r}"}, status_code=400
        )
    only_dirty_raw = payload.get("only_dirty")
    only_dirty = (
        (scope_chunk_id is None) if only_dirty_raw is None else bool(only_dirty_raw)
    )
    try:
        summary = mint_review_fanout(
            store,
            ref.id,
            lenses=lenses,
            doc_lenses=doc_lenses,
            only_dirty=only_dirty,
            scope=scope_chunk_id,
        )
    except BadInput as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **summary})


@router.post("/drafts/{ident}/review/retract")
async def retract_review_route(request: Request, ident: str) -> JSONResponse:
    """Un-review one block — deletes
    the ``chunk_review`` row for ``(chunk, checker)`` via
    ``Store.retract_review``, reverting the chunk to "requires review".

    Body ``{dc, checker?}`` — ``checker`` defaults to ``'human'`` (the ✓
    gutter's un-check), but any ledger checker name works (a machine lens's
    row can be retracted the same way). Returns the chunk's fresh
    per-checker status (mirrors ``/human-review``'s response shape) plus
    the whole-draft rollup; a 404 when no such row existed to retract
    (matches this file's not-found convention elsewhere)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    handle = str(payload.get("dc") or payload.get("handle") or "")
    chunk = store.get_draft_chunk(handle) if handle else None
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    checker = str(payload.get("checker") or "human")
    existed = store.retract_review(chunk.chunk_id, checker)
    if not existed:
        return JSONResponse(
            {"ok": False, "error": f"no {checker!r} review to retract on {handle}"},
            status_code=404,
        )
    status = store.review_status_for_chunk(chunk.chunk_id)
    return JSONResponse(
        {
            "ok": True,
            "review": _review_json(status),
            "rollup": _rollup_json(store, ref.id),
        }
    )


def _backfill_chunk_json(result: ChunkBackfill) -> dict[str, Any]:
    """JSON-safe per-chunk preview/result for the "convert to living cites"
    route (dry-run ``plan_chunk`` or applied ``apply_chunk``) — one entry
    per cite-group plan plus the chunk-level rollup counts."""
    return {
        "chunk_id": result.chunk_id,
        "n_claims": result.n_claims,
        "n_ungrounded": result.n_ungrounded,
        "rewritten_text": result.rewritten_text,
        "groups": [
            {
                "handles": p.group.handles,
                "action": p.action,
                "hub_ref_id": p.hub_ref_id,
                "note": p.note,
            }
            for p in result.plans
        ],
    }


@router.post("/drafts/{ident}/cites/convert")
async def convert_cites_route(request: Request, ident: str) -> JSONResponse:
    """Convert to living cites — a
    web wrapper over ``taproot/backfill.py``'s ``plan_chunk``/``apply_chunk``:
    rewrites legacy ``[pc<id>]``/``[pa<id>]`` paper cites onto claim-hub
    cites (``[fi<hub>]``).

    Body ``{dc, dry_run?}``: ``dc`` names either a single body chunk, or a
    heading whose whole subtree converts (``Store.review_subtree_chunk_ids``
    — item 1's subtree walk, reused here). ``dry_run`` defaults ``True``
    (preview only, nothing written); ``dry_run=False`` applies. A chunk with
    no pc/pa cite groups (headings, most chunks in a subtree walk) reports
    an empty ``groups`` list, not an error.

    Apply rewrites through ``apply_chunk``'s own ``draft_handler.edit`` (the
    normal edit door — never a raw ``UPDATE``), so each rewritten chunk's
    ``content_sha`` bumps and every checker's approval on it goes stale by
    construction (the acceptance criterion) — the same guarantee any other
    draft edit gives the review ledger."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad request body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    dc = str(payload.get("dc") or "").strip()
    chunk = store.get_draft_chunk(dc) if dc else None
    if chunk is None or chunk.ref_id != ref.id:
        return JSONResponse({"ok": False, "error": "block not found"}, status_code=404)
    dry_run_raw = payload.get("dry_run")
    dry_run = True if dry_run_raw is None else bool(dry_run_raw)
    targets = (
        store.review_subtree_chunk_ids(ref.id, chunk.chunk_id)
        if chunk.chunk_kind == "heading"
        else [chunk.chunk_id]
    )
    runtime = get_runtime(request)
    embedder = getattr(getattr(runtime, "hub", None), "embedder", None)
    results: list[dict[str, Any]] = []
    if dry_run:
        for cid in targets:
            try:
                plan = plan_chunk(
                    store,
                    embedder,
                    cid,
                    extract_fn=_backfill_extract_claim,
                    block_fn=_backfill_block,
                    judge_fn=_backfill_dedup_judge,
                    merge_confirm_fn=_backfill_merge_confirm,
                )
                results.append(_backfill_chunk_json(plan))
            except BadInput:
                continue  # not a live draft body chunk (e.g. a table/figure)
    else:
        handler = runtime.hub.handler_for("draft")
        for cid in targets:
            try:
                applied = apply_chunk(
                    store,
                    embedder,
                    handler,
                    cid,
                    extract_fn=_backfill_extract_claim,
                    block_fn=_backfill_block,
                    judge_fn=_backfill_dedup_judge,
                    merge_confirm_fn=_backfill_merge_confirm,
                )
                results.append(_backfill_chunk_json(applied))
            except BadInput:
                continue
    return JSONResponse({"ok": True, "dry_run": dry_run, "chunks": results})


@router.post("/drafts/{ident}/authoring")
async def set_authoring(
    request: Request, ident: str, enabled: str = Form("0")
) -> Response:
    """Per-document auto-author toggle (3e): when ON, the grounded review
    lenses (cites/structure) EDIT the draft instead of only filing findings.
    Writes draft.meta.authoring_enabled. Default OFF."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}"
    if ref is None:
        return RedirectResponse(url=back, status_code=303)
    on = enabled.strip().lower() in ("1", "true", "on", "yes")
    store.stamp_ref_meta(ref.id, {"authoring_enabled": on})
    return RedirectResponse(url=back, status_code=303)


@router.post("/drafts/{ident}/fork")
async def fork_draft_route(
    request: Request,
    ident: str,
    project: str = Form(...),
    title: str = Form(""),
) -> Response:
    """Duplicate this draft (Phase-1 fork) — deep-copy every chunk + its
    links into a NEW draft bound to ``project`` (a fresh project's name, or a
    ``todo:N`` for an existing draft-less one). The web twin of
    ``put(kind='draft', copy_of=<slug>, project=<name>)``; the source is
    untouched and the copy starts fully unreviewed. Lands on the new copy's
    reader (or the /drafts list if the new slug can't be parsed back)."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url="/drafts", status_code=303)
    project = project.strip()
    if not project:
        return RedirectResponse(url=f"/drafts/{ident}", status_code=303)
    payload: dict[str, Any] = {
        "kind": "draft",
        "copy_of": ref.slug or ref.id,
        "project": project,
    }
    if title.strip():
        payload["title"] = title.strip()
    body, is_error = await await_dispatch(request, "put", payload)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Duplicate draft error",
                "detail": body,
                "status": 400,
            },
            status_code=400,
        )
    # _fork's ack: "forked draft '<src>' → '<new_slug>' (ref N); …".
    m = re.search(r"→ '([^']+)'", body)
    dest = m.group(1) if m else None
    return RedirectResponse(
        url=f"/drafts/{dest}" if dest else "/drafts", status_code=303
    )


@router.post("/drafts/{ident}/workspace")
async def set_workspace(
    request: Request,
    ident: str,
    doctype: str | None = Form(None),
    brief: str | None = Form(None),
    voice: str | None = Form(None),
) -> Response:
    """Set the draft's genre (``doc_type``), project context (``brief``), and/or
    standing voice/style (``voice``) after creation — the gap for *imported*
    drafts, which never went through the ``/drafts/new`` genre picker (so the
    per-heading ``style ▾`` picker stays empty and the planner has no
    ``## Project context`` / ``## Voice & style``). Writes
    ``meta.workspace.{doc_type,brief,voice}`` on both the draft and its owning
    project (see :func:`_workspace_targets`).

    **Partial update**: a field whose param is ``None`` (not present in the
    posted form) is left UNCHANGED — this lets the smartdraft reader's
    separate "genre ▾" (doctype+brief) and "style ▾" (voice) popovers each
    post just their own field without clobbering the other. A field posted
    as the empty string clears that key. Unknown genres are rejected."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}"
    if ref is None:
        return RedirectResponse(url=back, status_code=303)
    doctype = doctype.strip() if doctype is not None else None
    brief = brief.strip() if brief is not None else None
    voice = voice.strip() if voice is not None else None
    if doctype and doctype not in _DOC_TYPE_BRIEF:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Genre error",
                "status": 400,
                "detail": f"unknown genre {doctype!r}",
            },
            status_code=400,
        )
    for rid in _workspace_targets(store, ref):
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        meta = (row[0] if row else None) or {}
        ws = dict(meta.get("workspace") or {})
        if doctype is not None:
            if doctype:
                ws["doc_type"] = doctype
            else:
                ws.pop("doc_type", None)
        if brief is not None:
            if brief:
                ws["brief"] = brief
            else:
                ws.pop("brief", None)
        if voice is not None:
            if voice:
                ws["voice"] = voice
            else:
                ws.pop("voice", None)
        store.stamp_ref_meta(rid, {"workspace": ws})
    return RedirectResponse(url=back, status_code=303)


@router.post("/drafts/{ident}/authors")
async def set_authors(
    request: Request,
    ident: str,
    authors: str = Form(""),
) -> Response:
    """Set the draft's byline from the reader — the web twin of the
    ``edit(kind='draft', authors=…)`` MCP verb. One author per line,
    ``Name | Affiliation | ROR`` (affiliation + ROR optional). **Replaces**
    the whole byline; an empty box clears it. Stores to the draft's
    first-class ``authors`` column (affiliation/ROR preserved via
    :func:`to_author_dicts`)."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}"
    if ref is None:
        return RedirectResponse(url=back, status_code=303)
    entries = to_author_dicts(_parse_author_lines(authors))
    store.update_paper_fields(ref.id, authors=entries, source="web-edit")
    return RedirectResponse(url=back, status_code=303)


@router.post("/drafts/{ident}/title")
async def set_title(
    request: Request,
    ident: str,
    title: str = Form(...),
) -> JSONResponse:
    """Rename the draft from the reader header — the web twin of
    ``edit(kind='draft', title=…)``. Writes ``refs.title`` AND the title
    heading chunk in one transaction (``store.set_draft_title``) so the name
    in search results can't drift from the one in the document.

    Speaks JSON (not the 303-redirect the older meta forms use): the header
    lives OUTSIDE ``#sd-content``, so smartdraft's in-place refresh doesn't
    repaint it — the caller patches the ``<h1>`` from this response instead.
    A blank title is a 422 (``BadInput``), not a silent no-op."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    try:
        old, synced = store.set_draft_title(ref.id, title, source={"actor": "web-edit"})
    except BadInput as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except NotFound as exc:
        # The lookup above is not in the write transaction, so a concurrent
        # delete between the two lands here — 404 like the miss above, not 500.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    # Belt-and-braces, matching the other smartdraft write route: the heading is
    # edited IN PLACE, which `_cache_version`'s content digest now covers, so
    # this only saves the losing side of a race with a concurrent render.
    # Lazy import: `precis_web.smartdraft` reaches back into this module.
    from precis_web import smartdraft as _smartdraft

    _smartdraft.invalidate(ref.id)
    return JSONResponse(
        {"ok": True, "title": title.strip(), "old": old, "heading_synced": synced}
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
    """Upload an image as a figure chunk inserted after ``handle``. Bytes
    are base64'd and routed through the ``put`` verb so the
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
    """Edit an existing figure's provenance — the click-to-edit
    behind the clearance badge. Routes through the ``edit`` verb so figure
    validation stays single-sourced; only ``meta.figure`` changes (caption
    and image bytes are untouched)."""
    back = f"/drafts/{ident}#c-{handle}"
    store = get_store(request)
    addr = _chunk_addr(store, handle)
    if addr is None:
        return RedirectResponse(url=back, status_code=303)
    args: dict[str, Any] = {"kind": "draft", "id": addr, "origin": origin}
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


@router.post("/drafts/{ident}/figure/{handle}/draw")
async def create_figure_drawing(request: Request, ident: str, handle: str) -> Response:
    """Turn an asset-less figure into an editable SVG canvas (the
    ``canvas`` medium): mint a ``kind='figure'`` seeded from the caption,
    parented on the draft's project, wire the ``has-figure`` link (chunk→ref),
    and drop the user into the ``/figure`` editor. Idempotent — a figure that
    already has a linked canvas just redirects into it."""
    store = get_store(request)
    runtime = get_runtime(request)
    back = f"/drafts/{ident}#c-{handle}"
    ref = _draft_ref(store, ident)
    if ref is None:
        return RedirectResponse(url=back, status_code=303)
    chunk = store.get_draft_chunk(handle)
    if chunk is None or chunk.chunk_kind != "figure":
        return RedirectResponse(url=back, status_code=303)

    # Already drawn-with: jump straight into the existing canvas.
    existing = store.figure_canvas_ref(chunk.chunk_id)
    if existing is not None:
        cref = store.get_ref(kind="figure", id=existing)
        if cref is not None and cref.slug:
            return RedirectResponse(url=f"/figure/{cref.slug}", status_code=303)

    caption = ((chunk.text or "").splitlines() or ["Figure"])[0].strip() or "Figure"
    # Deterministic, unique, slug-safe: the draft ident + the chunk's dc handle.
    slug = f"{ident}-{chunk.dc}".lower()
    args: dict[str, Any] = {"kind": "figure", "id": slug, "title": caption[:120]}
    project = _project_id(store, ref.id)
    if project is not None:
        args["project"] = project

    # Mint the canvas. If the slug already exists (a prior half-done attempt),
    # adopt it rather than failing — the link below is what actually matters.
    runtime.dispatch_with_status("put", args)
    canvas = store.get_ref(kind="figure", id=slug)
    if canvas is None:
        # Genuine failure (not a pre-existing slug) — surface it as the reader
        # error banner would for any other verb.
        return await redirect_or_error(
            request, "put", args, redirect=back, error_title="Create drawing error"
        )
    store.link_figure_canvas(chunk.chunk_id, canvas.id)
    return RedirectResponse(url=f"/figure/{slug}", status_code=303)


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
        # The sole draft reader is /smartdraft; focus by the chunk's dc<id>
        # handle (its query-param anchor scheme, not a #c-<base58> hash).
        return RedirectResponse(
            url=f"/smartdraft/{ident}?focus={chunk.dc}", status_code=303
        )
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
    if chunk is not None:
        src_kind, text, ref_id = "draft", chunk.text, chunk.ref_id
    else:
        uc = store.universal_chunk(handle)
        if uc is None:
            return templates.TemplateResponse(
                request,
                "preview/popover.html.j2",
                {"kind": "chunk", "label": handle, "missing": True},
            )
        src_kind, text, ref_id = uc["kind"], uc["text"], uc["ref_id"]

    # For a paper-family chunk ([pc…]), lead with the shared identity header
    # of the source it points at (year · title / venue · first … last) — the
    # same ``_paper_head`` block every other preview surface uses — instead of
    # the machine handle. The chunk text follows as the quote. A chunk we can
    # quote is by definition held, so the header reads sky. Non-paper chunks
    # (a draft ¶) carry no ``head`` and fall back to the plain quote card.
    head = None
    if src_kind in PAPER_IDENT_KINDS:
        ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
        if ref is not None:
            head = paper_head(ref, held=True)

    # Show the chunk's verbatim text (≤ ~20 lines) as the quote — the
    # "what does <handle> actually say?" a hover should answer.
    text = text or ""
    lines = text.splitlines()
    quote = "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
    if len(quote) > 1600:
        quote = quote[:1600].rstrip() + "…"
    # A manufacturing part hovers its attribute bag — MPN /
    # manufacturer / datasheet / callout — from its ``term`` leaf's meta, so a
    # ``[[dc…]]`` part reference in prose shows the rich card. Absent for a
    # patent part / glossary term (empty bag) — the plain quote renders.
    part_meta = chunk.meta if chunk is not None else {}
    return templates.TemplateResponse(
        request,
        "preview/popover.html.j2",
        {
            # Friendly source-kind label ("paper"/"draft"), not the raw
            # chunk_kind ("paragraph") the maintainer flagged as noise.
            "kind": src_kind,
            "label": handle,
            "ref_id": "",  # drop the "#pc…" machine line for chunk hovers
            "head": head,
            "quote": quote.strip() or "(empty)",
            "chunk_label": "",
            "body_preview": "",
            "deleted": False,
            "missing": False,
            "manufacturer": (part_meta or {}).get("manufacturer"),
            "mpn": (part_meta or {}).get("mpn"),
            "datasheet_url": (part_meta or {}).get("url"),
            "callout": (part_meta or {}).get("callout"),
        },
    )
