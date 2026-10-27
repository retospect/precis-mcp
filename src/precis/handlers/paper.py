"""PaperHandler — read scientific papers from the v2 store.

Bodies are read-only via ``get`` / ``search``; ingest happens
out-of-band via :func:`precis.ingest.add.precis_add`, ``precis add FILE.pdf``,
or ``precis watch`` with a PDF in ``inbox/papers/``. ``put`` requests paper
stubs or queues supplementary-information acquisition with ``mode='fetch-si'``;
it never writes a body. ``edit`` is scoped to *bibliographic metadata*
(authors / year / title / abstract / doi / arxiv / journal / entry_type)
plus the ``args=`` lifecycle doors — ``retire`` / ``redirect_to`` /
``merge_into``, the wrong-body repair set (``drop_body`` /
``move_body_to`` / ``split_body`` / ``clear_abstract``, gr514995) and
the single-row identifier removal (``drop_identifier``, gr513878); it
never edits block text in place.

Superseded paper restoration is CLI-only (``precis garden restore``);
MCP/web handlers expose no restore-superseded write door.
Superseded-handle resolution on link/edit/put/tag emits a per-call footer
receipt through the shared hint bus, exempt from advice cooldown/caps because
it names the ref actually changed. Reads keep a cooled-down merge note in the
footer. Delete refuses superseded handles before normalization and names the
survivor's explicit delete call, so a stale address cannot retire its survivor.

Slug parsing supports the canonical slug-with-chunk syntax used across
v2:

    wang2020state              — overview
    wang2020state~38           — block at pos=38
    wang2020state~38..42       — block range pos∈[38,42]
    wang2020state/cite/bib     — view shortcut path
    wang2020state/abstract     — view shortcut path
    wang2020state/toc          — view shortcut path
    wang2020state~38..42/toc   — TOC scoped to a range (drill-down, phase 3.5)
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import json
import logging
import re
import shlex
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg

from precis import settings
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, Gone, NotFound, Unsupported, Upstream
from precis.format import render_agent_table
from precis.handlers._exclude_closure import resolve_exclude_paper_ids
from precis.handlers._eye import is_eye_view
from precis.handlers._paper_format import (
    ENTRY_TYPE_VOCAB,
    _clean_inline_text,
    _format_authors,
    _render_citation,
    _strip_jats,
)
from precis.handlers._paper_search import (
    _BROAD_LEG_CAP,
    BylineSearch,
    FusedBlockSearch,
    PaperSearchResultRenderer,
    _coerce_search_year,
    _dedup_card_hits,
)
from precis.handlers._paper_text import (
    _is_caption_only_block,
    _is_image_only_block,
    _looks_like_caption,
    _page_spans,
    _render_block_body,
)
from precis.handlers._slug_ref_shared import (
    reject_chunk_or_path_view,
    resolve_live_slug_ref,
)
from precis.ingest.cards import rewrite_cards
from precis.ingest.text_chunker import CHUNKER_VERSION as _PAPER_CHUNKER_VERSION
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Store, Tag
from precis.store._body_predicate import has_body_sql
from precis.store._chunks_ops import _year_range_clauses
from precis.store._identifiers_ops import (
    _normalise_identifier,
    detect_identifier_scheme,
)
from precis.store._review_ops import trust_refusal
from precis.store._stub_predicate import ACQUIRE_RESTAMP_MIN_HOURS
from precis.store._tag_filter import build_tag_filter
from precis.store.types import ONE_PAGE_COVERAGE_NOTE
from precis.utils import handle_registry
from precis.utils.authors import (
    author_display,
    author_links,
    entry_from_author_row,
    normalize_authors,
    paper_scholar_link,
)
from precis.utils.authors import author_names as _shared_author_names
from precis.utils.edit_resolve import normalize_dry_run
from precis.utils.embed_query import embed_query
from precis.utils.glyph_health import glyph_caveat
from precis.utils.link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.utils.next_block import render_next_section
from precis.utils.record_cite import RECORD_TYPES, is_record_cite, record_type_of
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt
from precis.utils.title_merge import title_merge_refusal
from precis.utils.title_views import title_md, title_plain
from precis.utils.toc import ChunksForToc
from precis.utils.toc_db import render_from_store

log = logging.getLogger(__name__)


def _pa(ref: Ref) -> str:
    """Universal document-record handle (e.g. ``pa123`` / ``cf123``).

    Reads the code from ``ref.kind`` so the handle follows the ref's
    actual kind — paper refs render ``pa<id>``, cfp refs render
    ``cf<id>`` — letting :class:`CfpHandler` reuse the paper rendering
    path verbatim without minting cross-kind handles."""
    return handle_registry.format_handle(ref.kind, ref.id)


def _retired_note(report: Any) -> str:
    """The body-ops ack suffix naming retired chunks the delete also
    took (``BodyReport.retired_chunks``); empty when there were none."""
    n = getattr(report, "retired_chunks", 0)
    return f" (+{n} retired)" if n else ""


def _links_removed_note(report: Any) -> str:
    """The body-ops ack suffix naming chunk-anchored links the delete's
    cascade took (``BodyReport.chunk_links_removed``) — links anchored
    on retired donor chunks have no copy to re-point onto."""
    n = getattr(report, "chunk_links_removed", 0)
    return f", {n} retired-chunk link(s) removed" if n else ""


def _dropped_content_ids(identifiers: list[str], kept_pdf: str | None) -> list[str]:
    """The content ids a drop actually removes — under ``keep_pdf`` only
    ``content_hash`` and the non-kept ``pdf_sha256`` rows go; the kept
    sha's row and ``paper_id``/``pub_id`` stay (gr513878)."""
    if kept_pdf is None:
        return list(identifiers)
    return [
        i
        for i in identifiers
        if i.startswith("content_hash:")
        or (i.startswith("pdf_sha256:") and i != f"pdf_sha256:{kept_pdf}")
    ]


@contextlib.contextmanager
def _body_op_tx(store: Store, door: str) -> Iterator[Any]:
    """``store.tx()`` with the body doors' DB-error mapping.

    ``deadlock_detected`` becomes ``Upstream`` with a retry hint — the
    op is atomic, so the victim rolls back cleanly and a retry is safe.
    A check violation becomes ``BadInput``: a ``trust:`` refusal
    (migration 0214's guards, on a link or ref the ``computed`` door
    can't restamp) carries the trigger's hint as ``next=`` via
    :func:`trust_refusal`; anything else is a constraint the caller
    tripped and gets a gripe-pointers ``next=``.

    ``SET LOCAL statement_timeout`` lifts the role's defensive bound
    for the door's statements (gr516498): the re-home is a handful of
    set-based statements, but the ``chunk_embeddings`` copy feeds one
    HNSW index insertion per chunk. A 5.7 k-chunk body cost ~6.5 s
    (~1 ms/chunk) on a quiet dev DB, but that understates prod badly —
    the reported 5,724-chunk split outran the 120 s ``agent_rw`` bound
    (deploy/roles/postgres), i.e. ≥21 ms/chunk. 600 s covers ~28
    k-chunk bodies at the prod rate while still bounding a genuinely
    wedged statement; a cancellation still maps to ``Upstream`` with a
    retry hint.
    """
    try:
        with store.tx() as conn:
            conn.execute("SET LOCAL statement_timeout = '600s'")
            yield conn
    except psycopg.errors.DeadlockDetected as exc:
        raise Upstream(
            f"{door} lost a lock deadlock — nothing was written",
            next="retry the edit; if deadlocks repeat, another session is "
            "holding a write on one of the refs — file a gripe naming them",
        ) from exc
    except psycopg.errors.QueryCanceled as exc:
        raise Upstream(
            f"{door} hit the statement timeout — nothing was written",
            next="retry the edit; if it times out again the body is too "
            "large for one transaction — file a gripe naming the refs",
        ) from exc
    except psycopg.errors.CheckViolation as exc:
        refusal = trust_refusal(exc)
        if refusal is not None:
            raise refusal from exc
        raise BadInput(
            f"{door} refused by a database check: {exc.diag.message_primary}",
            next=exc.diag.message_hint or "file a gripe naming the refs",
        ) from exc


#: The most live body chunks a door re-homes in one transaction
#: (gr516498): the lifted ``_body_op_tx`` bound (600 s) covers ~28 k
#: chunks at the worst observed prod rate — the 5,724-chunk split that
#: outran the 120 s ``agent_rw`` bound, i.e. ≥21 ms/chunk. 20 k keeps
#: ~30% headroom under that rate; past it the door refuses up front
#: rather than dying mid-transaction on the timeout.
_BODY_OP_MAX_CHUNKS = 20_000


def _refuse_oversized_body(probe: Any, door: str, handle: str) -> None:
    """Refuse a body door whose chunk count can't fit the lifted
    ``_body_op_tx`` bound — up front, with ``next=``, instead of a
    mid-transaction ``QueryCanceled`` (gr516498)."""
    if probe.chunks <= _BODY_OP_MAX_CHUNKS:
        return
    raise BadInput(
        f"{handle} holds {probe.chunks} body chunk(s) — over the "
        f"{_BODY_OP_MAX_CHUNKS}-chunk ceiling, {door} cannot finish "
        "inside one transaction's statement_timeout",
        next="a body this size needs a batched path the body doors do "
        "not have yet — file a gripe naming the ref",
    )


# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------

#: Cap on ``search(view='status', ids=[…])`` — one bulk query, one page.
_STATUS_MAX_IDS = 200

#: Cap on ``get(id=[...])`` — the same 50 the numeric-ref batch uses
#: (:data:`precis.handlers._numeric_ref.BATCH_ID_CAP`), restated here
#: because the slug-addressed handler doesn't share that base class.
_BATCH_ID_CAP = 50

_SUPPORTED_VIEWS = (
    "bibtex",
    "ris",
    "endnote",
    "abstract",
    "toc",
    "summaries",
    "health",
    "bibliography",
    "log",
    "abbrevs",
    "links",
    "claims",
    "authors",
    "chunks",
    "ops",
    "pdf_link",
    "tables",
    "measures",
    "page_image",
)

#: Chunk kinds that always get a figure pointer on read (gr520326).
_FIGURE_CHUNK_KINDS: frozenset[str] = frozenset({"figure", "caption"})

#: How far (in ords) a figure pointer may reach for a Marker ``page-N``
#: span when the chunk's own text carries none — ``page_first`` is a
#: TOC-anchor interpolation that drifts badly on image-heavy datasheets
#: (gr520326: 835 claimed vs. ~857 actual), the nearest real span
#: brackets much tighter.
_PAGE_SPAN_WINDOW = 6


# Tunable knobs for the nearest-match suggester. The cutoff (0.6 of
# difflib's SequenceMatcher ratio) is deliberately conservative — it
# accepts ``wang2020stat`` → ``wang2020state`` (one missing char,
# ratio≈0.96) but rejects ``foo`` → ``wang2020state`` (ratio≈0.05),
# avoiding spurious "did you mean?" prompts when the agent is querying
# an obviously-different slug.
_SUGGEST_TOP_N = 3
_SUGGEST_CUTOFF = 0.6
# Hard cap on how many slugs we pull into memory for the close-match
# scan. At 30 chars per slug × 5K papers, that's ~150 KB of strings —
# well within the budget for an error path. If the corpus grows past
# this, the suggester silently truncates to the most-recent N papers
# (``list_refs`` orders by ``updated_at DESC``); that's a reasonable
# locality bias and keeps the worst-case cost bounded.
_SUGGEST_CORPUS_CAP = 5000

# Title-similarity floor for the DOI-edit guard (gr180189): below this
# Jaccard score, a Crossref-resolved title for the *new* DOI is treated
# as "a different paper" rather than a formatting/normalisation drift.
# Matches ``ProvenanceResult.has_metadata_mismatch``'s title threshold
# in ``precis.ingest.provenance`` — same signal, same cutoff.
_DOI_EDIT_TITLE_MISMATCH_THRESHOLD = 0.6

#: Trigram title-similarity floor for the ``merge_into`` door (gr509659):
#: the same auto-merge cutoff ``dedup._TITLE_AUTO_SIM`` uses — an
#: agent-asserted pair still needs its titles to corroborate the fold.
_MERGE_TITLE_SIM_FLOOR = 0.85
#: A missing year on either side passes the title gate only at
#: near-identical titles — the same relaxation the mint-time stub guard
#: uses (``Store.upsert_stub_paper``). A known-year mismatch always
#: refuses.
_MERGE_UNKNOWN_YEAR_SIM = 0.95

#: The ``edit(args=…)`` door keys, in dispatch order — at most one per
#: call, and never combined with the metadata fields. Every door takes
#: ``args['reason']``. ``keep_pdf`` / ``pdf_sha256`` are *modifiers* —
#: ``keep_pdf`` of ``drop_body`` alone, ``pdf_sha256`` of ``drop_body``,
#: ``reextract`` and ``move_pdf_to`` — they ride the same args dict.
_EDIT_DOOR_ARGS = (
    "retire",
    "redirect_to",
    "merge_into",
    "drop_body",
    "reextract",
    "move_body_to",
    "move_pdf_to",
    "attach_pdf",
    "split_body",
    "clear_abstract",
    "convert_to",
    "drop_identifier",
    "cite_key",
)


# ``_coerce_search_year`` / ``_embed_query_batch`` / ``_broad_args_suffix`` used
# to live here — they're pure ``search()``-only helpers, moved alongside the
# BylineSearch / FusedBlockSearch / PaperSearchResultRenderer collaborators in
# ``_paper_search.py`` (OPEN-ITEMS "Refactor handlers/paper.py::search()").
# ``_dedup_card_hits`` / ``_BROAD_LEG_CAP`` moved there too but are
# re-imported at the top of this module: both are also used by
# ``search_hits()`` below. ``search_hits()``'s ``exclude=`` resolution
# (gr312636) reuses ``resolve_exclude_paper_ids`` from
# ``_exclude_closure.py`` — the same batched resolver ``search()``'s
# single-kind path uses via ``FusedBlockSearch`` — instead of
# ``_normalise_exclude_slug`` per entry.


def _suggest_paper_slugs(slug: str, *, store: Store, kind: str = "paper") -> list[str]:
    """Return up to ``_SUGGEST_TOP_N`` ``kind`` slugs that look like ``slug``.

    Uses :func:`difflib.get_close_matches` with a ratio cutoff that
    rejects far-off matches — see ``_SUGGEST_CUTOFF`` for the rationale.
    Returns an empty list when:

    - the corpus is empty (no papers ingested yet),
    - no slug clears the cutoff (typical when the user types a topic
      string into the slug slot, e.g. ``id='nitrate reduction'``),
    - the typed slug exists exactly (caller should have resolved
      already; defensive no-op).

    The helper does **not** raise; callers always pass the result
    straight into the ``options=`` field of a ``NotFound``. Empty list
    → no ``options:`` line in the rendered envelope, which is exactly
    what we want when there's nothing useful to suggest.

    Why a free function (not a method): keeping this off the handler
    makes it independently testable without spinning up a Hub fixture
    and means the same logic could be reused by patent/oracle/conv
    handlers if they grow nearest-match support too.
    """
    if not slug:
        return []
    refs = store.list_refs(kind=kind, limit=_SUGGEST_CORPUS_CAP)
    candidates = [r.slug for r in refs if r.slug]
    if not candidates:
        return []
    return difflib.get_close_matches(
        slug,
        candidates,
        n=_SUGGEST_TOP_N,
        cutoff=_SUGGEST_CUTOFF,
    )


# Identifier prefixes the gated ``acquire`` tool accepts in its
# ``identifier=`` slot. Bare DOIs (``10....``, including doi.org URL
# forms) and arXiv ids are inferred without a prefix.
_ACQUIRE_ID_PREFIXES = ("doi", "arxiv", "s2", "pubmed")


def _parse_acquire_identifier(raw: str) -> tuple[str, str] | None:
    """Parse an ``acquire`` identifier into an ``(id_kind, id_value)`` pair.

    Accepts prefixed forms (``doi:10.1/x``, ``arxiv:2401.00001``,
    ``s2:<id>``, ``pubmed:<id>``) and infers bare DOIs, URL-form DOIs
    (``https://doi.org/10.…``, ``doi.org/10.…``), and bare arXiv ids.
    DOI and arXiv values are canonicalised through
    :func:`precis.identity.normalize_doi` / ``normalize_arxiv`` so the
    spellings callers actually paste — ``DOI:10.1/X``, another ``doi:``
    inside the value, a doi.org URL, arXiv URLs and version suffixes —
    collapse to the same ``(kind, canonical)`` pair the
    ``ref_identifiers`` collapse probe and the S2 lookup expect
    (gr484234). Returns ``None`` when nothing recognisable is found —
    the caller turns that into a ``BadInput`` with a usage hint.
    """
    from precis.identity import normalize_arxiv, normalize_doi

    s = raw.strip()
    if not s:
        return None
    low = s.lower()
    for pfx in _ACQUIRE_ID_PREFIXES:
        if low.startswith(pfx + ":"):
            val = s[len(pfx) + 1 :].strip()
            if pfx == "doi":
                val = normalize_doi(val) or ""
            elif pfx == "arxiv":
                val = normalize_arxiv(val) or ""
            if not val:
                return None
            return (pfx, val)
    doi = normalize_doi(s)
    if doi is not None and doi.startswith("10."):
        return ("doi", doi)
    arxiv = normalize_arxiv(s)
    if arxiv is not None:
        return ("arxiv", arxiv)
    return None


def _lookup_acquire_metadata(id_kind: str, id_value: str) -> dict[str, Any] | None:
    """Best-effort Semantic Scholar enrichment for a fresh stub.

    Returns the normalised S2 metadata dict (``title`` / ``year`` /
    ``doi`` / ``arxiv_id`` / ``s2_id`` / ...) or ``None`` when S2 has no
    record for the identifier (a genuine not-found). Propagates a
    resolver-side failure — ``S2ResolverError`` from the strict lookup,
    or a plain ``Exception`` when the resolver can't even run (the
    ``semanticscholar`` lib absent/broken in this venv) — so the caller
    can tell "S2 says no such paper" apart from "S2 can't answer right
    now" (gr483930: under the old catch-all-None contract an S2 outage
    reported every real DOI as a hallucinated id). Patched out in tests
    to keep them offline.
    """
    from precis.ingest.semantic_scholar import get_paper_by_id

    if id_kind == "doi":
        return get_paper_by_id(f"doi:{id_value}", propagate=True)
    if id_kind == "arxiv":
        return get_paper_by_id(f"arxiv:{id_value}", propagate=True)
    if id_kind == "s2":
        return get_paper_by_id(id_value, propagate=True)
    return None


def _lookup_doi_registry(doi: str) -> dict[str, Any] | None:
    """DOI registration-agency fallback for a genuine S2 miss (gr486090).

    Crossref then DataCite via :mod:`precis.ingest.doi_registry` — the
    fetch goes through ``safe_get`` because the DOI is agent-supplied.
    Returns the normalized metadata dict or ``None`` when no registry
    knows the DOI. ``RegistryLookupError`` propagates so the caller can
    mint unverified rather than reject a real DOI over a registry
    outage — the same miss-vs-fault split the S2 leg draws (gr483930).
    Patched out in tests.
    """
    from precis.ingest.doi_registry import resolve_registry_doi

    return resolve_registry_doi(
        doi, mailto=settings.get_str("contact.crossref_mailto") or ""
    )


#: Display names for the ``meta.source`` tags ``resolve_registry_doi``
#: returns — surfaced in the acquire response so the caller can see
#: which registry vouched for the DOI.
_REGISTRY_LABELS = {"crossref": "Crossref", "datacite": "DataCite"}


class PaperHandler(Handler):
    """Slug-addressed paper handler (read-only bodies; metadata-editable).

    Stored data: each paper is a ``refs`` row with kind='paper' and one
    block per chunk in ``blocks`` (text + optional embedding + density).
    Bibliographic metadata (doi, authors, year, journal, ...) lives in
    ``refs.meta``.
    """

    #: ingest of third-party papers (cfp/datasheet inherit): the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True

    #: ``get(id='<slug>', q='…')`` is find-in-ref (gr486840) — ``q`` on a
    #: concrete id scans the paper's chunks; ``id=None`` keeps its older
    #: meaning (delegate to ``search``).
    supports_find_in_ref = True

    #: ``edit(id=[...])`` stays inside the handler — its own list door is
    #: the ``args={'clear_stamp': …}`` cohort re-arm; other field edits
    #: refuse a list there with a named per-id call.
    edit_id_list: ClassVar[str] = "native"
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        # ``get`` reads the ``view='ops'`` slice window only
        # (gr517423: :meth:`_render_ops_view`); the deprecated
        # ``offset``/``limit`` aliases ride along through
        # ``_slice_args.slice_window``. ``ttl_s`` is the
        # ``view='pdf_link'`` token-lifetime knob (gr517423 slice 3).
        # ``view='tables'`` (gr506762): ``args={'table': n|<pc handle>}``
        # renders one table, ``args={'match': 'w1 w2'}`` keeps tables
        # whose header/caption/row labels hold any word.
        "get": ArgsSpec(
            # ``ord`` is ``view='page_image'``'s chunk selector
            # (gr520326): resolve the figure crop / page for the chunk
            # at that ord.
            {"page", "page_size", "offset", "limit", "ttl_s", "match", "table", "ord"}
        ),
        # Every acquire knob — ``args={'doi': …, 'verify': False}`` is the
        # spelling the slim schema pushes (kind-specific fields ride
        # args=), and acquire's own BadInput hint names verify=/title=.
        # Rejecting them made the advertised recovery path uncallable
        # (gr483930 / gr484234).
        "put": ArgsSpec(
            {
                "pdf_url",
                "identifier",
                "doi",
                "arxiv",
                "title",
                "year",
                "reason",
                "context_ref_id",
                "citation",
                "verify",
                "record_type",
                # restore door (gr517423, slice 2): args={'restore': <one
                # precis-paper-dump/1 slice>, 'target': 'same'|'new',
                # 'handle': <slug for a minted target>} — :meth:`_restore`;
                # args={'restore_abort': True} clears an in-flight
                # restore's meta.restore — :meth:`_restore_abort`.
                "restore",
                "restore_abort",
                "target",
                "handle",
                # upload doors (gr517423, slice 6): args={'pdf_upload_link':
                # True} mints a single-use signed POST URL
                # (:meth:`_mint_pdf_upload_link`); args={'pdf_upload':
                # '<sha>'} queues a paper_pull attach of the staged file
                # (:meth:`_queue_pdf_upload`).
                "pdf_upload_link",
                "pdf_upload",
            }
        ),
        # ``redirect_to`` re-points a retired/merged stub's supersede
        # redirect (:meth:`_repoint_supersede`); ``retire`` soft-deletes a
        # phantom stub (:meth:`_retire_stub`); ``merge_into`` folds a live
        # duplicate into a named survivor (:meth:`_merge_duplicate_into`);
        # ``clear_stamp`` removes allow-listed worker-retry meta keys
        # so the owning pass re-claims the ref
        # (:meth:`_clear_worker_stamps`); ``drop_body`` / ``move_body_to``
        # / ``split_body`` / ``clear_abstract`` are the wrong-body
        # repair doors (gr514995: :meth:`_drop_body`,
        # :meth:`_move_body_to`, :meth:`_split_body`,
        # :meth:`_clear_abstract`) — ``keep_pdf`` / ``pdf_sha256`` are
        # ``drop_body``'s modifiers (gr513878), not doors of their own;
        # ``reextract`` drops the body, keeps the pdf and queues a
        # ``paper_reextract`` job to re-extract it (gr517423,
        # :meth:`_reextract`; ``pdf_sha256`` rides it too);
        # ``drop_identifier`` removes one named identifier row
        # (:meth:`_drop_identifier`); ``reason`` is the audit string every
        # door writes into its ref_event. Subclass hooks read their own
        # keys via _edit_args.
        "edit": ArgsSpec(
            {
                "redirect_to",
                "retire",
                "merge_into",
                "clear_stamp",
                "drop_body",
                "reextract",
                "keep_pdf",
                "pdf_sha256",
                "move_body_to",
                "move_pdf_to",
                "attach_pdf",
                "split_body",
                "clear_abstract",
                "convert_to",
                "subtype",
                "drop_identifier",
                "cite_key",
                "reason",
            }
        ),
    }

    stamps_recall = True

    #: Empty-corpus message for :meth:`_render_list_papers`. Subclasses
    #: (cfp, datasheet) that don't share paper's ingest pipeline invocation
    #: override this — without it they'd inherit "no papers ingested yet -
    #: use `precis jobs ingest-bundles`", which is paper-pipeline phrasing
    #: that doesn't apply to a spec/evidence-role sibling kind (gr311347 #12).
    _empty_corpus_message: ClassVar[str] = (
        "no papers ingested yet - use `precis jobs ingest-bundles <dir>` to populate"
    )

    spec: ClassVar[KindSpec] = KindSpec(
        kind="paper",
        title="Paper",
        description=(
            "Scientific paper. Addressable by slug (e.g. 'wang2020dopamine') "
            "OR by bare DOI (e.g. '10.1038/nature10352') — `get` and "
            "`search` resolve DOIs transparently. One ref per paper, "
            "blocks per chunk. Ingest PDF bodies with `precis add FILE.pdf` "
            "or drop a PDF into `inbox/papers/` for `precis watch`. "
            "`put` requests stubs or queues `mode='fetch-si'`; it never "
            "writes a body. Use tag / link to classify and "
            "cross-cite."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Paper *bodies* arrive via PDF ingest, never authored from the
        # agent surface. ``put`` exposes stub requests and ``fetch-si``;
        # ``put(kind='paper',
        # doi=… / arxiv=… / title=…)`` requests a paper into the
        # "papers we need" backlog (the fetch_oa worker chases it); it
        # never writes a body. ``edit`` is scoped to *bibliographic
        # metadata* only (authors / year / title / abstract / doi /
        # arxiv) — the repair affordance the web metadata editor drives —
        # plus the ``args=`` doors (merge_into / retire / redirect_to /
        # clear_stamp); it never touches block bodies.
        supports_put=True,
        # ``put`` mints stubs; its one other door is ``mode='fetch-si'``
        # (queue a supplementary-information fetch for an existing paper).
        modes=("fetch-si",),
        # ``put(args={'pdf_url': ...})`` queues a ``paper_pull`` job that
        # builds into this stub, so the paper parents it.
        can_own_jobs=True,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        # A paper is citable evidence — it participates in literature
        # search and is a valid citation source (the citation handler
        # resolves ``source_handle`` against ``kind='paper'``).
        corpus_role="evidence",
        placement="corpus",
        views=_SUPPORTED_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("paper: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # -- acquire: the gated dream stub-mint tool -----------------------------

    def put(
        self,
        *,
        identifier: str | None = None,
        doi: str | None = None,
        arxiv: str | None = None,
        title: str | None = None,
        year: int | None = None,
        reason: str | None = None,
        context_ref_id: int | str | None = None,
        verify: bool = True,
        id: str | int | None = None,
        mode: str | None = None,
        citation: str | None = None,
        tags: list[str] | None = None,
        record_type: str | None = None,
        dry_run: bool | str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Mint a paper **stub** — the agent-facing "I want this paper".

        ``put(kind='paper', id=<stub>, args={'pdf_url': 'https://…'})`` is
        the door for a known-legal PDF the OA fetcher cannot find: it queues
        a ``paper_pull`` job that drops the PDF into the ingest inbox for
        that body-less stub (:meth:`_queue_pdf_pull`).

        ``put(kind='paper', id=<slug>, mode='fetch-si')`` is the one other
        door: it queues a supplementary-information fetch for an existing
        paper (:meth:`_queue_si_fetch`).

        Ingest paper *bodies* with ``precis add FILE.pdf`` or drop a PDF
        into ``inbox/papers/`` for ``precis watch``. Stub requests enter
        the "papers we need" backlog, where the ``fetch_oa`` worker chases
        an OA PDF. Shapes:

            put(kind='paper', doi='10.1038/nature10352')
            put(kind='paper', arxiv='2401.00001', title='…')
            put(kind='paper', title='…')   # title-only backlog stub
            put(kind='paper', citation='Phys. Rev. B 47, 558 (1993)')
            put(kind='paper', id='pa496283', record_type='software')

        ``record_type=`` ('software' | 'dataset', gr514384) types an
        existing *chunkless* record so its whole-ref ``[pa<id>]`` cite is
        legitimate — see :meth:`_set_record_type`.

        ``put(kind='paper', id=<pa>, args={'restore': <one dump slice>,
        'reason': '…'}, dry_run=…)`` is the slice-2 restore door
        (gr517423): it rebuilds a ref's body from the ``view='ops'``
        ``precis-paper-dump/1`` export, sent one slice per call in
        window order — see :meth:`_restore`.

        ``citation=`` resolves a classic journal/volume/page/year string via
        Crossref ``query.bibliographic``: one full-confidence hit is stubbed
        by its DOI; otherwise the scored candidates come back, nothing minted.

        This is a thin adapter over :meth:`acquire` — it folds the
        ``doi=`` / ``arxiv=`` conveniences into the canonical
        ``identifier=`` form and reuses the same S2-enrich →
        ``upsert_stub_paper`` → tag/link path (idempotent: a hit on an
        already-held or already-wanted paper is a no-op).
        """
        # ``args=`` is the declared extras channel for this handler, so
        # the acquire knobs a caller nests there (the slim-schema spelling)
        # fill any parameter they didn't also set top-level.
        extras = args or {}
        doi = doi or extras.get("doi")
        arxiv = arxiv or extras.get("arxiv")
        identifier = identifier or extras.get("identifier")
        title = title or extras.get("title")
        year = year if year is not None else extras.get("year")
        reason = reason or extras.get("reason")
        context_ref_id = (
            context_ref_id
            if context_ref_id is not None
            else extras.get("context_ref_id")
        )
        citation = citation or extras.get("citation")
        verify = extras.get("verify", verify)
        record_type = (
            record_type if record_type is not None else extras.get("record_type")
        )
        if isinstance(verify, str):
            # ``args=`` values can arrive JSON-stringified from non-slim
            # profiles; "false"/"0"/"no" must not read truthy.
            verify = verify.strip().lower() not in ("false", "0", "no")
        pdf_url = extras.get("pdf_url", _kw.get("pdf_url"))
        pdf_upload_link = extras.get("pdf_upload_link")
        if isinstance(pdf_upload_link, str):
            pdf_upload_link = pdf_upload_link.strip().lower() not in (
                "false",
                "0",
                "no",
                "",
            )
        pdf_upload_sha = extras.get("pdf_upload")
        restore_abort = extras.get("restore_abort")
        if isinstance(restore_abort, str):
            # ``args=`` values can arrive JSON-stringified; "false" must
            # not read truthy (same shape as ``verify`` above).
            restore_abort = restore_abort.strip().lower() not in (
                "false",
                "0",
                "no",
                "",
            )
        if extras.get("restore") is not None or restore_abort:
            if extras.get("restore") is not None and restore_abort:
                raise BadInput(
                    "args['restore'] and args['restore_abort'] are two "
                    "different doors — send them as separate calls",
                    next="put(kind='paper', id='<pa-or-slug>', args="
                    "{'restore': <one dump slice>, 'reason': '<why>'})",
                )
            # The restore door writes a body, so it shares nothing with
            # the stub-mint fields — a combined call is a caller error.
            combined = [
                name
                for name, val in (
                    ("doi", doi),
                    ("arxiv", arxiv),
                    ("identifier", identifier),
                    ("title", title),
                    ("year", year),
                    ("citation", citation),
                    ("record_type", record_type),
                    ("tags", tags),
                    ("mode", mode),
                    ("pdf_url", pdf_url),
                    ("text", _kw.get("text")),
                )
                if val is not None and (not isinstance(val, str) or val.strip())
            ]
            if combined:
                door = "restore_abort" if restore_abort else "restore"
                raise BadInput(
                    f"args['{door}'] is a restore door; it does "
                    f"not combine with: {', '.join(combined)}",
                    next="put(kind='paper', id='<pa-or-slug>', args="
                    "{'restore': <one dump slice>, 'reason': '<why>'})",
                )
            if restore_abort:
                return self._restore_abort(id, extras, dry_run=dry_run)
            return self._restore(id, extras, dry_run=dry_run)
        if pdf_upload_link or pdf_upload_sha is not None:
            # The upload doors queue an attach or mint a staging link —
            # a combined call reads as a stub-mint and would silently
            # drop half the intent.
            combined = [
                name
                for name, val in (
                    ("doi", doi),
                    ("arxiv", arxiv),
                    ("identifier", identifier),
                    ("title", title),
                    ("year", year),
                    ("citation", citation),
                    ("record_type", record_type),
                    ("tags", tags),
                    ("mode", mode),
                    ("pdf_url", pdf_url),
                    ("text", _kw.get("text")),
                )
                if val is not None and (not isinstance(val, str) or val.strip())
            ]
            if pdf_upload_link and pdf_upload_sha is not None:
                combined.append("pdf_upload_link+pdf_upload")
            if combined:
                raise BadInput(
                    "args['pdf_upload_link'] / args['pdf_upload'] are "
                    f"separate doors; the call does not combine with: "
                    f"{', '.join(combined)}",
                    next="put(kind='paper', id='<pa-or-slug>', args="
                    "{'pdf_upload_link': True}) then args={'pdf_upload': "
                    "'<sha256>', 'reason': '<why>'}",
                )
            if pdf_upload_link:
                return self._mint_pdf_upload_link(id)
            return self._queue_pdf_upload(id, pdf_upload_sha, reason, dry_run=dry_run)
        if tags:
            if pdf_url is not None or mode is not None:
                raise BadInput(
                    "tags= applies only to minting a stub (doi=/arxiv=/title=); "
                    "tag an existing paper with tag(kind='paper', id=…, add=[…])",
                    next="tag(kind='paper', id='<slug-or-paN>', add=['<tag>'])",
                )
            # Validate before minting so a bad tag leaves no stub behind.
            Tag.normalize_filter(tags, kind="paper")
        if pdf_url is not None:
            return self._queue_pdf_pull(id, pdf_url)
        if mode is not None:
            if mode != "fetch-si":
                raise BadInput(
                    f"put(kind={self.spec.kind!r}) only supports mode='fetch-si', "
                    f"got {mode!r}",
                    options=["fetch-si"],
                    next=f"put(kind={self.spec.kind!r}, id='<slug>', mode='fetch-si')",
                )
            return self._queue_si_fetch(id)
        if record_type is not None:
            # Whole-record cite typing (gr514384): declare an existing
            # chunkless paper a ``software``/``dataset`` record so its
            # whole-ref ``[pa<id>]`` cite is legitimate. Exclusive of the
            # stub-mint fields — a mint that also wants typing comes back
            # typed by the registry itself, or is typed after it lands.
            combined = [
                name
                for name, val in (
                    ("doi", doi),
                    ("arxiv", arxiv),
                    ("identifier", identifier),
                    ("title", title),
                    ("citation", citation),
                )
                if val is not None and str(val).strip()
            ]
            if combined:
                raise BadInput(
                    "record_type= types an existing record; it does not "
                    f"combine with stub-mint field(s): {', '.join(combined)}",
                    next="put(kind='paper', id='<slug-or-paN>', "
                    "record_type='software')",
                )
            return self._set_record_type(
                id, record_type, supporters=_kw.get("supporters"), tags=tags
            )
        # ``put`` mints stubs; it never writes a body. A caller passing
        # ``text=`` is trying to rewrite a paper body — reject loudly
        # rather than silently drop the text into ``_kw``. Bodies arrive
        # through the PDF ingest pipeline.
        if _kw.get("text") is not None:
            raise Unsupported(
                "paper does not support put with text= — ingest PDF bodies "
                "with `precis add FILE.pdf` or drop a PDF into `inbox/papers/` "
                "for `precis watch`; put(kind='paper') requests stubs or "
                "queues mode='fetch-si', never writes bodies",
                next="precis add FILE.pdf",
            )
        ident = identifier.strip() if identifier and identifier.strip() else None
        if (
            ident is None
            and not (doi and doi.strip())
            and citation
            and citation.strip()
        ):
            from precis.ingest import crossref_citation as _cc

            cands = _cc.resolve_citation(
                citation.strip(),
                settings.get_str("contact.crossref_mailto") or "",
            )
            hit = _cc.pick_unambiguous(cands)
            if hit is None:
                return Response(body=_render_citation_candidates(citation, cands))
            doi = hit["doi"]
        if ident is None:
            if doi and doi.strip():
                ident = f"doi:{doi.strip()}"
            elif arxiv and arxiv.strip():
                ident = f"arxiv:{arxiv.strip()}"
        if ident is None and not (title and title.strip()):
            raise BadInput(
                "put(kind='paper') mints a stub - pass doi=, arxiv=, "
                "identifier= (doi:/arxiv:/s2:), citation=, or title=",
                next="put(kind='paper', doi='10.1038/nature10352')",
            )
        return self.acquire(
            identifier=ident,
            title=title,
            year=year,
            reason=reason,
            context_ref_id=context_ref_id,
            verify=verify,
            tags=tags,
        )

    def _queue_pdf_pull(self, id: str | int | None, pdf_url: Any) -> Response:
        """Queue a ``paper_pull`` job feeding an agent-supplied PDF URL to a
        body-less stub. Refuses a non-paper and a bodied stub up front; the job fetches (``safe_stream``) and drops PDF + sidecar
        in the ingest inbox — the MCP server fetches nothing."""
        from urllib.parse import urlsplit

        from precis.workers.job_types import paper_pull

        example = "put(kind='paper', id='<slug>', args={'pdf_url': 'https://…/x.pdf'})"
        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput("pdf_url needs id=<paper slug>", next=example)
        url = pdf_url.strip() if isinstance(pdf_url, str) else ""
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise BadInput(
                f"pdf_url must be an http(s) URL, got {pdf_url!r}", next=example
            )
        if (bad := paper_pull.blocked_host(url)) is not None:
            raise BadInput(
                f"pdf_url host {bad} is a shadow library; not fetched",
                next="use a publisher, repository or author-hosted open copy",
            )
        slug, ref_id = self._resolve_paper_slug(id)
        problem = paper_pull.target_problem(self.store, ref_id)
        if problem is not None:
            raise BadInput(
                f"{slug} {problem}",
                next="pdf_url feeds a paper that still needs a PDF or a "
                "body (a sha-less or bodiless ref); "
                "get(kind='paper', id=...) to check its state",
            )
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        resp = hub.sibling("job").put(
            job_type=paper_pull.JOB_TYPE,
            executor="job_inproc",
            parent_id=ref_id,
            params={"ref_id": ref_id, "url": url},
            idem_key=paper_pull.idem_key(ref_id, url),
        )
        state = "already queued" if getattr(resp, "reused", False) else "queued"
        return Response(
            body=(
                f"{state}: PDF pull for {slug} from {url} - a worker fetches it "
                "(size-capped, %PDF-checked) and drops it in the ingest inbox "
                "with a sidecar naming this stub; the watcher then attaches the "
                "body. The outcome lands on the paper's meta.paper_pull."
            )
        )

    def _mint_pdf_upload_link(self, id: str | int | None) -> Response:
        """``put(args={'pdf_upload_link': True})`` — a single-use signed
        upload URL for a paper stub (gr517423 slice 6). precis_web stages
        the POSTed bytes under ``PRECIS_UPLOAD_STAGING``; nothing is
        attached until the agent comes back with
        ``args={'pdf_upload': '<sha>'}``.
        """
        from precis import file_links
        from precis import secrets as vault
        from precis.ingest import pdf_upload as _uploads
        from precis.workers.job_types import paper_pull

        example = "put(kind='paper', id='<slug>', args={'pdf_upload_link': True})"
        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput("pdf_upload_link needs id=<paper slug>", next=example)
        slug, ref_id = self._resolve_paper_slug(id)
        problem = paper_pull.target_problem(self.store, ref_id)
        if problem is not None:
            raise BadInput(
                f"{slug} {problem}",
                next="pdf_upload_link feeds a paper that still needs a PDF "
                "or a body (a sha-less or bodiless ref); "
                "get(kind='paper', id=...) to check its state",
            )
        _uploads.require_uploads_dir()
        token = file_links.mint("put", file_links.UPLOAD_SHA, ref_id, store=self.store)
        exp = file_links.parse(token).exp
        path = f"/files/upload/{ref_id}?t={token}"
        base = (
            vault.get_secret(file_links.WEB_BASE_URL_SECRET, store=self.store) or ""
        ).rstrip("/")
        url = f"{base}{path}" if base else path
        expires = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines: list[str] = []
        if not base:
            lines.append(
                "PRECIS_WEB_BASE_URL is not set — the link below is "
                "path-only; prepend the precis_web origin to POST to it "
                "(the operator sets it via the vault)."
            )
        lines += [
            f"# {slug} — PDF upload link",
            f"url: {url}",
            f"expires: {expires} (single use — one upload per link)",
            f"max bytes: {_uploads.MAX_UPLOAD_BYTES} · must start with %PDF",
            "",
            f"curl -f --data-binary @paper.pdf -H 'Content-Type: application/pdf' "
            f"'{url}'",
            "",
            "then attach the sha256 the POST answers:",
            f"put(kind='paper', id='{slug}', args="
            "{'pdf_upload': '<sha256>', 'reason': '<why>'})",
        ]
        return Response(body="\n".join(lines))

    def _queue_pdf_upload(
        self,
        id: str | int | None,
        sha_arg: Any,
        reason: Any,
        *,
        dry_run: bool | str | None,
    ) -> Response:
        """``put(args={'pdf_upload': '<sha256>'})`` — attach a staged
        upload to a body-less stub: queue ``paper_pull`` with the staged
        file as its source (same ``check_pdf`` gate + inbox drop as a
        fetched URL). The sha must name a staging the upload route
        recorded under ``PRECIS_UPLOAD_STAGING`` — anything else refuses
        toward the mint door."""
        from precis.ingest import pdf_upload as _uploads
        from precis.workers.job_types import paper_pull

        example = (
            "put(kind='paper', id='<slug>', args="
            "{'pdf_upload': '<sha256>', 'reason': '<why>'})"
        )
        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput("pdf_upload needs id=<paper slug>", next=example)
        sha = sha_arg.strip().lower() if isinstance(sha_arg, str) else ""
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise BadInput(
                f"args['pdf_upload'] must be the sha256 the upload POST "
                f"answered, got {sha_arg!r}",
                next=example,
            )
        slug, ref_id = self._resolve_paper_slug(id)
        self._require_reason(
            str(reason or ""), "pdf_upload", self.spec.kind, slug, arg="'<sha256>'"
        )
        problem = paper_pull.target_problem(self.store, ref_id)
        if problem is not None:
            raise BadInput(
                f"{slug} {problem}",
                next="pdf_upload attaches to a paper that still needs a PDF "
                "or a body (a sha-less or bodiless ref); "
                "get(kind='paper', id=...) to check its state",
            )
        _uploads.require_uploads_dir()
        staged = _uploads.staged_upload_path(sha, ref_id=ref_id)
        if staged is None:
            raise BadInput(
                f"no staged upload named {sha[:16]}… for {slug} — the sha "
                "must come from the upload POST's answer (stagings are bound "
                "to their token's ref and swept after 24 h)",
                next=f"put(kind='paper', id='{slug}', "
                "args={'pdf_upload_link': True})",
            )
        if dry_run:
            return Response(
                body=(
                    f"dry run: would queue a paper_pull job attaching staged "
                    f"upload {sha[:16]}… ({staged}) to {slug} — check_pdf "
                    "identity gate, inbox drop, pdf_uploaded ref_event"
                )
            )
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        resp = hub.sibling("job").put(
            job_type=paper_pull.JOB_TYPE,
            executor="job_inproc",
            parent_id=ref_id,
            params={"ref_id": ref_id, "upload_sha": sha, "reason": reason},
            idem_key=paper_pull.idem_key(ref_id, f"upload:{sha}"),
        )
        state = "already queued" if getattr(resp, "reused", False) else "queued"
        return Response(
            body=(
                f"{state}: attach staged upload {sha[:16]}… to {slug} — a "
                "paper_pull job re-checks the target, gates the file on the "
                "same identity checks as a fetched URL, and drops it in the "
                "ingest inbox for the watcher to fold. The outcome lands on "
                "meta.paper_pull and a pdf_uploaded ref_event."
            )
        )

    def _queue_si_fetch(self, id: str | int | None) -> Response:
        """Flag a paper so the ``fetch_oa`` lane's SI pass discovers and fetches
        its supplementary information. Writes ``meta.si_fetch`` only — the MCP
        server fetches nothing (no Marker, no inbox here)."""
        from precis.store.si_links import request_si_fetch

        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput(
                "mode='fetch-si' needs id=<paper slug>",
                next="put(kind='paper', id='<slug>', mode='fetch-si')",
            )
        slug, ref_id = self._resolve_paper_slug(id)
        with self.store.pool.connection() as conn:
            status, parent = request_si_fetch(conn, ref_id, by="agent")
            if status == "is_supplement":
                assert parent is not None
                raise BadInput(
                    f"{slug} is itself supplementary information of "
                    f"{parent[1] or parent[0]}",
                    next=f"put(kind='paper', id='{parent[1] or parent[0]}', mode='fetch-si')",
                )
            conn.commit()
        state = "already queued" if status == "pending" else "queued"
        return Response(
            body=(
                f"{state}: supplementary-information fetch for {slug} — the "
                "fetch worker discovers it on its next tick; each SI PDF found "
                "becomes its own paper ref linked to this one and cites as it. "
                "The outcome lands on the paper "
                "(get(id=..., view='log'): si_found / si_none / si_blocked / si_error)."
            )
        )

    def _set_record_type(
        self,
        id: str | int | None,
        record_type: Any,
        *,
        supporters: Any = None,
        tags: list[str] | None = None,
    ) -> Response:
        """``put(kind='paper', id=<slug-or-paN>, record_type=…)`` — declare
        an existing **chunkless** paper a whole-record cite
        (``'software'`` | ``'dataset'``, gr514384).

        Typing is what makes the ``[pa<id>]`` whole-ref cite legitimate:
        a record has no passage to quote, so there is no ``[pc<id>]`` to
        drill to. The write goes through
        :meth:`Store.update_paper_fields` (``meta`` merge + a
        ``metadata_edited`` ref_event), never a raw UPDATE. Idempotent —
        re-declaring the same type is a no-op ack; correcting a wrong
        auto-typed value (DataCite/Crossref registration) is the same
        call."""
        rt = str(record_type).strip().lower()
        if rt not in RECORD_TYPES:
            raise BadInput(
                f"record_type= takes 'software' or 'dataset', got {record_type!r}",
                options=sorted(RECORD_TYPES),
                next="put(kind='paper', id='<slug-or-paN>', record_type='software')",
            )
        if supporters:
            # A record carries no passage, so it can never ground a claim
            # hub's supporter — claims about the software's behaviour
            # still need a passage (a README, a paper) behind [pc<id>].
            raise BadInput(
                "record_type= does not take supporters= — a record has no "
                "passage to ground a claim hub on",
                next="cite the record whole as [pa<id>]; ground the claim "
                "on a passage supporter ([pc<id>] of a README or paper)",
            )
        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput(
                "record_type= types an existing paper record — pass id=<slug-or-paN>",
                next="put(kind='paper', id='pa496283', record_type='software')",
            )
        slug, ref_id = self._resolve_paper_slug(id)
        n_body = self.store.chunks.count_chunks(ref_id)
        if n_body:
            # Strict zero-body-chunks eligibility (gr514384): a paper
            # with passages stays on the passage rules — the whole-paper
            # hint is exactly what should keep firing for it.
            raise BadInput(
                f"{slug} has {n_body} passage{'s' if n_body != 1 else ''} — "
                "record_type= is only for a record with no passages",
                next="cite a finding hub [fi<id>] grounded on a passage "
                "([pc<id>]) instead",
            )
        ref = self.store.get_ref(kind=self.spec.kind, id=ref_id)
        prior = record_type_of(ref.meta if ref is not None else None)
        handle = handle_registry.format_handle(self.spec.kind, ref_id)
        if prior == rt:
            return Response(
                body=f"{slug} is already typed record_type='{rt}' — "
                f"cite whole as [{handle}]; no passages"
            )
        self.store.update_paper_fields(
            ref_id, meta_patch={"record_type": rt}, source="mcp-edit"
        )
        if tags:
            apply_tag_ops(self.store, self.spec.kind, ref_id, tags=tags, untags=None)
        return Response(
            body=(
                f"{slug} typed record_type='{rt}' — its whole-ref cite "
                f"[{handle}] is legitimate (no passages); exports emit "
                f"@{rt} and the whole-paper lint skips it"
            )
        )

    def _restore_expected_pin(self, id: str | int | None) -> int | None:
        """The optional ``id=`` pin on the restore call: the ref_id the
        caller *believes* the dump is for. ``pa<N>`` / a bare int may name
        a gone ref (``target='new'`` exists to rebuild one); any other
        spelling goes through slug resolution and must resolve."""
        if id is None or (isinstance(id, str) and not id.strip()):
            return None
        if isinstance(id, int):
            return id
        parsed = handle_registry.parse(str(id).strip())
        if parsed is not None:
            kind, is_chunk, pk = parsed
            if is_chunk or kind != self.spec.kind:
                raise BadInput(
                    f"restore id {id!r} is not a {self.spec.kind!r} ref handle",
                    next=f"put(kind='{self.spec.kind}', id='<{self.spec.kind} "
                    "handle or slug>', args={'restore': <slice>})",
                )
            return pk
        _slug, ref_id = self._resolve_paper_slug(str(id).strip())
        return ref_id

    def _restore(
        self,
        id: str | int | None,
        extras: dict[str, Any],
        *,
        dry_run: bool | str | None,
    ) -> Response:
        """``put(args={'restore': <slice>})`` — rebuild a ref from a
        ``precis-paper-dump/1`` slice (gr517423, slice 2).

        One transaction per slice inside ``_body_op_tx``; the writes live
        in :func:`precis.ingest.paper_dump.restore_slice`. The handler
        owns the ref_events: ``paper_restore_slice`` on every slice,
        ``paper_restored`` on the completing one. ``dry_run`` runs the
        whole thing under a savepoint and rolls it back.
        """
        from precis.ingest import paper_dump

        example = (
            f"put(kind='{self.spec.kind}', id='<pa-or-slug>', args="
            "{'restore': <one dump slice>, 'reason': '<why>'})"
        )
        raw: Any = extras["restore"]
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                # A pasted fenced block: take the fence's contents.
                text = (
                    text.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in text else text
                )
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise BadInput(
                    f"args['restore'] is not a parseable dump slice: {exc}",
                    next=example,
                ) from exc
        reason = extras.get("reason")
        dry = normalize_dry_run(dry_run) is not None
        if not dry and not (reason and str(reason).strip()):
            raise BadInput(
                "args['restore'] needs args['reason'] — the ref_events carry it",
                next=example,
            )
        expected = self._restore_expected_pin(id)
        target = extras.get("target") or "same"
        handle = extras.get("handle")
        with _body_op_tx(self.store, "restore") as conn:
            report = paper_dump.restore_slice(
                conn,
                raw,
                target=str(target).strip(),
                handle=str(handle).strip() if handle is not None else None,
                kind=self.spec.kind,
                expected_ref_id=expected,
                dry=dry,
            )
            if not dry:
                self.store.append_event(
                    report.ref_id,
                    source="agent",
                    event="paper_restore_slice",
                    payload={
                        "window": list(report.window),
                        "inserted": report.inserted,
                        "kept": report.kept,
                        "replaced": report.replaced,
                        "chunks_done": report.chunks_done,
                        "chunks_total": report.chunks_total,
                        "reason": reason,
                    },
                    conn=conn,
                )
                if report.completed:
                    self.store.append_event(
                        report.ref_id,
                        source="agent",
                        event="paper_restored",
                        payload={
                            "dump_body_sha": (raw.get("manifest") or {}).get("body_sha")
                            if isinstance(raw, dict)
                            else None,
                            "chunks": report.chunks_total,
                            "cards": report.cards_total,
                            "identifiers": report.identifiers_total,
                            "links": report.links_written + report.links_kept,
                            "skipped_links": report.links_skipped,
                            "skipped_identifiers": report.skipped_identifiers,
                            "not_carried": report.not_carried,
                            # gr517423 review R1: dumped trust stamps are
                            # never written raw — the door restamps; this
                            # is the record of what changed.
                            "restamped": len(report.links_restamped),
                            "restamped_links": report.links_restamped,
                            "reason": reason,
                        },
                        conn=conn,
                    )
        return Response(body=self._render_restore_ack(report))

    def _restore_abort(
        self,
        id: str | int | None,
        extras: dict[str, Any],
        *,
        dry_run: bool | str | None,
    ) -> Response:
        """``put(args={'restore_abort': True})`` — clear ``meta.restore``
        on a ref whose restore was abandoned or whose dump went stale
        (gr517423, slice 2). The chunk rows the run inserted are deleted;
        chunks that predate the restore stay, and a retired target —
        including a just-minted one — stays retired.
        """
        from precis.ingest import paper_dump

        example = (
            f"put(kind='{self.spec.kind}', id='<pa-or-slug>', "
            "args={'restore_abort': True, 'reason': '<why>'})"
        )
        reason = extras.get("reason")
        dry = normalize_dry_run(dry_run) is not None
        if not dry and not (reason and str(reason).strip()):
            raise BadInput(
                "args['restore_abort'] needs args['reason'] — the ref_events carry it",
                next=example,
            )
        ref_id = self._restore_expected_pin(id)
        if ref_id is None:
            raise BadInput(
                "restore_abort needs id= naming the ref its restore is in flight on",
                next=example,
            )
        with _body_op_tx(self.store, "restore_abort") as conn:
            info = paper_dump.restore_abort(conn, ref_id, kind=self.spec.kind, dry=dry)
            progress = info["progress"]
            if not dry:
                self.store.append_event(
                    ref_id,
                    source="agent",
                    event="paper_restore_aborted",
                    payload={
                        "dump_ref_id": progress.get("dump_ref_id"),
                        "chunks_done": progress.get("chunks_done"),
                        "chunks_total": progress.get("chunks_total"),
                        "chunks_removed": info["chunks_removed"],
                        "chunks_live": info["chunks_live"],
                        "reason": reason,
                    },
                    conn=conn,
                )
        done = progress.get("chunks_done") or 0
        total = progress.get("chunks_total") or 0
        lead = "DRY RUN (no write) — would abort" if dry else "aborted"
        verb = "would be" if dry else "were"
        stays = f"{info['chunks_live']} chunk(s) that predate the restore stay"
        if info["retired"]:
            stays += " (the ref stays retired)"
        body = (
            f"{lead} the in-flight restore on {info['handle']}: "
            f"meta.restore {'would be ' if dry else ''}cleared "
            f"(frontier was {done}/{total} chunk(s))\n"
            f"{info['chunks_removed']} chunk(s) the run inserted {verb} "
            f"removed; {stays}"
        )
        return Response(body=body)

    @staticmethod
    def _render_restore_ack(report: Any) -> str:
        """The restore ack: the window, the per-ord counts, then either the
        next window to send or the completion line with the link tally."""
        lo, hi = report.window
        lead = (
            f"DRY RUN (no write) — would restore {report.handle}"
            if report.dry
            else f"restored {report.handle}"
        )
        parts = [
            f"{lead} slice window [{lo}, {hi}): "
            f"{report.inserted} chunk(s) inserted, {report.kept} kept"
            + (f", {report.replaced} replaced" if report.replaced else "")
            + ("; ref un-retired" if report.unretired else "")
        ]
        if report.completed:
            line = (
                f"complete: body_sha verified — {report.handle}'s body "
                f"matches the dump; {report.links_written} link(s) written"
            )
            if report.links_kept:
                line += f", {report.links_kept} already present"
            if report.links_skipped:
                line += f", {len(report.links_skipped)} skipped"
            parts.append(line)
            parts += [
                f"- skipped {s.get('relation')} link "
                f"pa{s.get('src_ref_id')}→pa{s.get('dst_ref_id')}: "
                f"{s.get('reason')}"
                for s in report.links_skipped
            ]
            parts += [
                f"- trust restamped on link {r['link_id']} "
                f"(dump link {r.get('dump_link_id')}): {r['was']} → {r['now']}"
                for r in report.links_restamped
            ]
        else:
            parts.append(
                f"progress: {report.chunks_done}/{report.chunks_total} "
                "chunks restored — send the slice whose window starts "
                f"at {report.chunks_done}"
            )
        parts += [
            f"- skipped identifier {i.get('id_kind')}={i.get('id_value')}: "
            f"{i.get('reason')}"
            for i in report.skipped_identifiers
        ]
        if report.not_carried:
            parts.append(f"not carried: {', '.join(report.not_carried)}")
        return "\n".join(parts)

    def acquire(
        self,
        *,
        identifier: str | None = None,
        title: str | None = None,
        year: int | None = None,
        reason: str | None = None,
        context_ref_id: int | str | None = None,
        verify: bool = True,
        tags: list[str] | None = None,
        explicit: bool = True,
        **_kw: Any,
    ) -> Response:
        """Queue a missing paper for fetch — the shared stub-mint impl.

        Agent-facing spelling is ``put(kind='paper', …)`` (see
        :meth:`put`); this method does the work. A dream (or anyone)
        notices the corpus keeps citing a paper it doesn't hold and mints
        a **stub** so the existing fetch pipeline takes over
        (``docs/backlog/dreaming.md`` §Acquire) — never ingests inline, no
        download, no Marker, in the dream turn.

        1. Resolve ``identifier`` (``doi:``/``arxiv:``/``s2:`` or a bare
           DOI/arXiv id) and best-effort enrich via S2.
        2. Idempotently upsert a stub ``paper`` ref (identifier-collapse:
           a hit on an already-held or already-wanted paper short-circuits
           to a no-op), tagged ``DREAM:acquire`` with ``meta.set_by='dream'``.
        3. Link it from ``context_ref_id`` (provenance) when supplied.

        Downstream is automatic: an explicit acquire is a "fetch NOW"
        signal, so the stub is pinned to the head of the ``fetch_oa``
        queue (``Store.pin_stub_for_fetch``: ``prio=1``/
        ``meta.prio_by='acquire'`` + an ``oa_requeued`` stamp that lifts a
        backed-off stub for one immediate retry) and claimed on the
        worker's next pass; auto-discovered stubs instead earn their
        place via ``stub_rank``. No OA copy → the stub waits on the
        ``precis stubs`` required-papers backlog. Minting is additive and
        reversible (soft-delete) — a runaway dream can at worst enqueue
        stubs, never blow a budget on downloads. The legacy in-process
        dream loop's gated ``acquire`` tool (``PRECIS_DREAM_ACQUIRE``)
        retired when the dreamers consolidated onto ``claude -p`` + MCP
        ``dream_agent``.

        ``verify`` (default ``True``): an unrecognised identifier (S2
        returns no metadata — for a DOI, Crossref and DataCite are asked
        before refusing, since S2's index lags fresh records and skips
        whole registries; gr486090) is rejected with :class:`BadInput`
        so a hallucinated DOI/arXiv ID never lands on the "Papers we
        need" backlog. Pass ``verify=False`` for a known-real preprint
        no index carries yet, or when the resolvers are unreachable —
        resolver outages mint with ``meta.acquire_unverified=True`` for
        a later re-check.
        """
        has_identifier = bool(identifier and identifier.strip())
        has_title = bool(title and title.strip())
        if not has_identifier and not has_title:
            raise BadInput(
                "acquire requires identifier= (doi/arxiv/s2) or title=",
                next="acquire(identifier='doi:10.1/x', reason='cited 5x in cluster')",
            )

        id_pair: tuple[str, str] | None = None
        if has_identifier:
            assert identifier is not None
            id_pair = _parse_acquire_identifier(identifier)
            if id_pair is None:
                raise BadInput(
                    f"acquire: unrecognised identifier {identifier!r}",
                    next=(
                        "use 'doi:10...', 'arxiv:2401.00001', or 's2:<id>' "
                        "(or pass title= for a backlog-only stub)"
                    ),
                )

        # Validate the provenance ref up-front so a bad id fails before
        # any write (kind-agnostic — context may be a paper or a memory).
        ctx_id: int | None = None
        if context_ref_id is not None:
            try:
                ctx_id = int(context_ref_id)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"acquire: context_ref_id must be an int, got {context_ref_id!r}",
                    next="pass the numeric ref id where the paper came up",
                ) from exc
            if ctx_id not in self.store.fetch_refs_by_ids(
                [ctx_id], include_deleted=False
            ):
                raise BadInput(
                    f"acquire: context_ref_id={ctx_id} is not a live ref",
                    next="omit context_ref_id or pass a live ref id",
                )

        # Best-effort S2 enrichment → a meaningful stub. Failure is fine.
        # ``year`` arrives as a caller hint (e.g. from put(kind='paper',
        # year=…)); S2 overrides it when it has one, else the hint stands.
        stub_title = title.strip() if title and title.strip() else None
        identifiers: list[tuple[str, str]] = [id_pair] if id_pair else []
        unverified = False
        resolver_err: Exception | None = None
        s2_meta: dict[str, Any] | None = None
        registry_meta: dict[str, Any] | None = None
        s2_down = False
        if id_pair is not None:
            try:
                meta = _lookup_acquire_metadata(*id_pair)
            except Exception as exc:
                # A resolver fault (S2 unreachable, rate-limited, broken
                # client) is NOT evidence the identifier is wrong —
                # mint the stub marked unverified instead of rejecting a
                # real DOI as "hallucinated" (gr483930).
                meta = None
                resolver_err = exc
            if meta:
                # Lazy import mirrors ``_lookup_acquire_metadata`` above —
                # ``semanticscholar`` is gated behind the ``[paper]``
                # extra, and a successful ``meta`` here already proves
                # the module imported cleanly in that call.
                from precis.ingest.semantic_scholar import s2_stub_meta

                s2_meta = s2_stub_meta(meta, now=datetime.now(UTC))
                stub_title = stub_title or (meta.get("title") or None)
                raw_year = meta.get("year")
                year = int(raw_year) if isinstance(raw_year, int) else year
                for kind_key, meta_key in (
                    ("doi", "doi"),
                    ("arxiv", "arxiv_id"),
                    ("s2", "s2_id"),
                ):
                    val = meta.get(meta_key)
                    if val:
                        pair = (
                            kind_key,
                            str(val).lower() if kind_key == "doi" else str(val),
                        )
                        if pair not in identifiers:
                            identifiers.append(pair)
            elif resolver_err is not None:
                # The resolver could not answer at all (S2 outage, 429
                # storm, broken client) — mint anyway, marked unverified.
                # Rejecting here reports real identifiers as
                # "hallucinated" and loses the request entirely; the stub
                # is the "fetch NOW" the caller asked for and the fetch
                # cascade's non-S2 legs don't need this lookup
                # (gr483930). A DOI still gets the registry check: a
                # Crossref/DataCite hit verifies it without S2 (gr489237
                # — a gateway with no S2 client minted every DOI
                # unverified).
                s2_down = True
                if id_pair[0] == "doi":
                    try:
                        registry_meta = _lookup_doi_registry(id_pair[1])
                    except Exception:
                        registry_meta = None
                if registry_meta is not None:
                    resolver_err = None
                else:
                    unverified = True
            else:
                # A genuine S2 "no such paper" — not an outage. S2's
                # index lags fresh records and misses whole registries
                # (DataCite proceedings like LIPIcs), so for a DOI ask
                # the registration agencies before rejecting (gr486090).
                if id_pair[0] == "doi":
                    try:
                        registry_meta = _lookup_doi_registry(id_pair[1])
                    except Exception as exc:
                        # Same rule as the S2 leg: a registry fault is
                        # not evidence the identifier is wrong.
                        resolver_err = exc
                if resolver_err is not None:
                    unverified = True
                elif registry_meta is not None:
                    pass  # verified by a registry; fields applied below
                elif verify and not has_title:
                    # Strict path: caller gave us only an identifier and
                    # neither S2 nor (for a DOI) any registration agency
                    # knows it — a *genuine* not-found, not an outage
                    # (resolver faults mint unverified above). The safe
                    # default is to reject hallucinated identifiers, and
                    # let the caller pass ``verify=False`` for known-real
                    # niche / brand-new papers, or add a ``title=`` hint
                    # that converts this from "validate-or-reject" into
                    # "validate-best-effort" (the title path means the
                    # operator can still recognise the stub by hand).
                    kind_key, val = id_pair
                    via = (
                        "Semantic Scholar or the DOI registries"
                        if kind_key == "doi"
                        else "Semantic Scholar"
                    )
                    raise BadInput(
                        f"acquire: identifier {identifier!r} did not resolve "
                        f"via {via} — likely a hallucinated or mistyped ID",
                        # Name the spelling that actually reaches the
                        # handler: these knobs ride ``args=`` on the slim
                        # MCP schema (gr483930 — the bare ``verify=False``
                        # wording sent callers into the args= key
                        # rejection).
                        next=(
                            f"verify {kind_key}:{val} on doi.org / arxiv.org, "
                            f"OR put(kind='paper', args={{'identifier': "
                            f"{identifier!r}, 'title': '<best-known title>'}}) "
                            "to mint a stub that resolves best-effort, OR "
                            f"put(kind='paper', args={{'identifier': "
                            f"{identifier!r}, 'verify': False}}) if you know "
                            "the paper is real"
                        ),
                    )
                elif verify and has_title:
                    # We had a title hint, so the operator can still find
                    # this in the "Papers we need" tab even though we
                    # couldn't auto-confirm the identifier. Mark
                    # unverified.
                    unverified = True

        if registry_meta is not None:
            stub_title = stub_title or (registry_meta.get("title") or None)
            raw_year = registry_meta.get("year")
            year = int(raw_year) if isinstance(raw_year, int) else year
            reg_doi = registry_meta.get("doi")
            if reg_doi:
                pair = ("doi", str(reg_doi).lower())
                if pair not in identifiers:
                    identifiers.append(pair)

        with self.store.tx() as conn:
            ref_id, created = self.store.upsert_stub_paper(
                identifiers=identifiers,
                title=stub_title,
                year=year,
                set_by="dream",
                s2_meta=s2_meta,
                conn=conn,
            )
            # Tag the provenance only on a fresh stub — never slap
            # DREAM:acquire onto a paper the corpus already holds in full.
            if created:
                self.store.add_tag(
                    ref_id,
                    Tag.closed("DREAM", "acquire"),
                    set_by="agent",
                    conn=conn,
                )
                if unverified:
                    # Open tag so the operator can filter the
                    # "Papers we need" tab to un-validated stubs;
                    # the worker fetch cascade still tries them.
                    self.store.add_tag(
                        ref_id,
                        Tag.open("acquire:unverified"),
                        set_by="agent",
                        conn=conn,
                    )
                    # meta marker for the later re-check pass; carries
                    # the resolver failure's reason when the unverified
                    # mint was an outage, not an unresolvable id.
                    from psycopg.types.json import Jsonb

                    patch: dict[str, Any] = {"acquire_unverified": True}
                    if resolver_err is not None:
                        patch["acquire_unverified_reason"] = str(resolver_err)[:300]
                    conn.execute(
                        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                        (Jsonb(patch), ref_id),
                    )
                if registry_meta is not None:
                    # A registry-resolved stub is verified — carry the
                    # bibliographic fields onto it: authors column plus
                    # journal/abstract meta, and ``acquire_registry`` as
                    # provenance. No ``s2_enriched_at`` stamp — the stub
                    # stays claimable for stub_rank's real S2 enrich once
                    # S2's index catches up.
                    reg_patch: dict[str, Any] = {}
                    reg_source = str(registry_meta.get("source") or "")
                    if reg_source:
                        reg_patch["acquire_registry"] = reg_source
                    journal = registry_meta.get("journal")
                    if isinstance(journal, str) and journal.strip():
                        reg_patch["journal"] = journal.strip()
                    # Whole-record cite fields (gr514384): a DataCite
                    # Software/Dataset or a Crossref dataset/software DOI
                    # carries ``record_type`` (and the record's ``version``
                    # / ``publisher`` / typed ``entry_type``) through to
                    # the stub's meta, so it registers already citable
                    # whole — no second ``put(record_type=…)`` needed.
                    for key in ("record_type", "version", "publisher", "entry_type"):
                        val = registry_meta.get(key)
                        if isinstance(val, str) and val.strip():
                            reg_patch[key] = val.strip()
                    abstract = registry_meta.get("abstract")
                    if isinstance(abstract, str) and abstract.strip():
                        reg_patch["abstract"] = abstract.strip()
                    self.store.update_paper_fields(
                        ref_id,
                        authors=registry_meta.get("authors") or None,
                        meta_patch=reg_patch,
                        source=f"acquire:{reg_source or 'registry'}",
                        # ``paper_authors.source`` has no ``datacite``
                        # tier — the generic ``pdf`` tier is the
                        # sanctioned fallback for programmatic writers.
                        authors_source=(
                            "crossref" if reg_source == "crossref" else "pdf"
                        ),
                        conn=conn,
                    )
            if ctx_id is not None:
                self.store.add_link(
                    src_ref_id=ctx_id,
                    dst_ref_id=ref_id,
                    relation="related-to",
                    set_by="agent",
                    meta={"acquire_reason": reason} if reason else None,
                    conn=conn,
                )
            # An explicit acquire means "fetch this NOW" — pin it to the
            # head of the fetch_oa queue (fresh mint AND re-request of a
            # still-pending stub alike; the re-stamp also lifts a stub out
            # of fetch backoff for one immediate retry). Auto-discovered
            # stubs instead earn their prio via stub_rank. No-op on a
            # collapse hit onto an already-held paper.
            # ``explicit=False`` (auto-discovery callers — quest lit-fetch)
            # keeps the 24 h re-stamp guard so a periodic re-request can't
            # re-arm the bypass on a stub that was just tried (gr453859);
            # an explicit ask always re-stamps (gr510430).
            pinned = self.store.pin_stub_for_fetch(
                ref_id,
                conn=conn,
                restamp_min_hours=(None if explicit else ACQUIRE_RESTAMP_MIN_HOURS),
            )

        if tags:
            # gr492998: ``put(kind='paper', doi=…, tags=[…])`` is the
            # obvious spelling; tag the stub (minted or already tracked).
            apply_tag_ops(self.store, "paper", ref_id, tags=tags, untags=None)

        state = "minted stub" if created else "already tracked"
        parts = [f"acquire: {state} paper id={ref_id}"]
        if resolver_err is not None:
            # Say what failed and what clears the flag (gr489237): the
            # fetch runs either way, and S2 enrichment or a landed PDF
            # flips the stub to verified.
            parts.append(
                "(identifier not verified yet — "
                f"{type(resolver_err).__name__}: {str(resolver_err)[:80]}; "
                "tagged acquire:unverified until S2 enrichment or the "
                "fetch confirms it)"
            )
        if registry_meta is not None:
            reg_label = _REGISTRY_LABELS.get(
                str(registry_meta.get("source") or ""), "a DOI registry"
            )
            s2_state = "S2 unavailable" if s2_down else "S2 has no record"
            parts.append(f"(identifier verified via {reg_label} — {s2_state})")
        if pinned:
            parts.append("(pinned to front of fetch queue)")
        if identifiers:
            parts.append("(" + ", ".join(f"{k}:{v}" for k, v in identifiers) + ")")
        if ctx_id is not None:
            parts.append(f"← linked from ref {ctx_id}")
        body = " ".join(parts)

        # On a collapse hit (the identifier was already known), point the
        # caller straight at the existing paper — so re-requesting a paper
        # we already hold/want returns *the paper*, not just a bare
        # "already tracked id=N". Held papers (with a PDF) say so; stubs
        # report that the fetch is still pending.
        if not created:
            existing = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            if existing is not None:
                held = getattr(existing, "pdf_sha256", None) is not None
                title = title_md((existing.title or "").split("\n", 1)[0].strip())
                where = "held" if held else "stub (awaiting fetch)"
                line = f"\n→ {where}: {_pa(existing)}"
                if title and title.lower() != "paper":
                    line += f" — {title[:120]}"
                line += f"\n  get(id='{_pa(existing)}') to read it"
                body += line

        return Response(body=body)

    # -- get -----------------------------------------------------------------

    def find_in_ref(
        self,
        *,
        id: Any = None,
        q: Any = None,
        view: Any = None,
        context: Any = None,
    ) -> Response:
        """``get(id='<slug>', q='…')`` — matching passages inside one paper.

        A ``~A..B`` chunk-range suffix scopes the scan to that range;
        ``id=None`` keeps ``get``'s older delegation to ``search`` so the
        bare ``get(kind='paper', q='…')`` shape doesn't regress
        (gr486840). cfp/datasheet inherit this unchanged.
        """
        from precis.handlers._find_in_ref import find_preflight, find_response

        if id is None:
            # The established bare-q shape: rank papers, not find-in-ref.
            # A blank q falls back to the list view exactly as the get
            # path below does.
            if q is None or not str(q).strip():
                return self._render_list_papers()
            if view is not None:
                raise BadInput(
                    "view= doesn't combine with a bare-q get — the q is a "
                    "search query here",
                    next=f"search(kind={self.spec.kind!r}, q={str(q).strip()!r})",
                )
            return self.search(q=str(q).strip())
        if isinstance(id, list):
            # ``get(id=[...], q='…')`` maps find-in-ref over the batch —
            # the same per-id fan-out ``_get_batch`` gives a view.
            if not str(q or "").strip():
                raise BadInput(
                    "find-in-ref needs q= on a batch",
                    next=(
                        f"get(kind='{self.spec.kind}', id=[<id>, ...], q='your query')"
                    ),
                )
            if len(id) > _BATCH_ID_CAP:
                raise BadInput(
                    f"id=[...] takes at most {_BATCH_ID_CAP} ids, got {len(id)}",
                    next=f"split into calls of {_BATCH_ID_CAP} or fewer",
                )
            blocks: list[str] = []
            for elem in id:
                try:
                    blocks.append(self.find_in_ref(id=elem, view=view, q=q).body)
                except NotFound:
                    blocks.append(f"{elem}  not found")
                except Gone:
                    blocks.append(f"{elem}  deleted")
            return Response(
                body=(
                    f"# {len(id)} {self.spec.kind}s (batch find, "
                    f"q={str(q).strip()!r})\n\n" + "\n\n".join(blocks)
                ),
                result_count=len(id),
            )
        find_preflight(kind=self.spec.kind, id=id, view=view, q=q)
        raw_id = _maybe_resolve_doi(self.store, str(id))
        slug, chunk_spec, path_view = _parse_paper_id(raw_id, self.spec.kind)
        if path_view is not None:
            raise BadInput(
                f"q= (find-in-ref) doesn't combine with a /{path_view} path",
                next=f"get(kind='{self.spec.kind}', id='{slug}', q={str(q).strip()!r})",
            )
        ref = resolve_live_slug_ref(
            self.store,
            kind=self.spec.kind,
            id=slug,
            next_hint=f"search(kind='{self.spec.kind}', q='your query') to find existing",
        )
        if chunk_spec is not None and chunk_spec[0] < 0:
            raise BadInput(
                "find-in-ref scans body chunks, not the synthetic card "
                f"chunk (~{chunk_spec[0]})",
                next=f"get(kind='{self.spec.kind}', id='{slug}', q={str(q).strip()!r})",
            )
        pos_range = chunk_spec if chunk_spec is not None else None
        if pos_range is not None:
            chunks = self.store.chunks.list_chunks_for_ref(ref.id, pos_range=pos_range)
            if not chunks:
                raise NotFound(
                    f"no blocks in {ref.slug} for range ~{pos_range[0]}..{pos_range[1]}",
                    next=f"get(id='{_pa(ref)}', view='toc')",
                )
        else:
            chunks = self.store.chunks.list_chunks_for_ref(ref.id)
        return find_response(
            kind=ref.kind,
            display=_pa(ref),
            chunks=chunks,
            q=str(q).strip(),
            context=context,
            ref_id=ref.id,
        )

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        # find-in-ref (gr486840): ``q`` on a concrete get is the shared
        # find; ``id=None`` keeps its older meaning below (delegate to
        # ``search``). Eye views keep their own ``q`` (ring expansion).
        if q is not None and not is_eye_view(view):
            return self.find_in_ref(id=id, view=view, q=q)
        # Round-2 picky 2026-05-30: ``get(kind='paper', q='...')`` was
        # silently dropping ``q=`` and rendering the paper list,
        # leaving callers confused why their search returned no
        # chunks. Delegate to ``search`` when ``q=`` is present and
        # no concrete ``id=`` was given — same UX shift as the skill
        # handler's empty-q path, in the opposite direction.
        if id is None and q is not None and q.strip():
            return self.search(q=q)
        if id is None:
            return self._render_list_papers()
        if isinstance(id, list):
            return self._get_batch(id, view=view)
        raw_id = _maybe_resolve_doi(self.store, str(id))
        kind = self.spec.kind
        slug, chunk_spec, path_view = _parse_paper_id(raw_id, kind)

        ref = resolve_live_slug_ref(
            self.store,
            kind=kind,
            id=slug,
            next_hint=f"search(kind='{kind}', q='your query') to find existing",
            options=_suggest_paper_slugs(slug, store=self.store, kind=kind),
        )

        # Citation-chunk-grounding "active paper" trigger (see
        # workers/inbound_chase.py): the first
        # time a paper is actually read — any view, any chunk range —
        # flags it for the inbound-citer sweep, once, permanently. Dark
        # behind PRECIS_INBOUND_CHASE_ENABLED (no-op check + no tag write
        # while off); no-op on every later read once tagged.
        from precis.workers.inbound_chase import mark_paper_active

        mark_paper_active(self.store, ref)

        # Path view (`slug/cite/bib`) takes precedence over kwarg `view`,
        # because the agent is being explicit in the id. Whatever wins,
        # normalise it through the same alias map so view='cite/bib' and
        # view='bibtex' resolve identically — the MCP critic flagged the
        # asymmetry where the path form accepted 'cite/bib' but the kwarg
        # form rejected it.
        effective_view = _normalise_view(path_view or view)
        # Phase F 2026-05-31: validate against per-kind enum so the
        # agent gets the accepted list back in one round-trip on a
        # bogus or missing-arg view. ``None`` (no view requested)
        # always passes through to the overview / chunk-resolver
        # paths below.
        if effective_view is not None:
            accepted = self.accepted_views(id=ref)
            if effective_view not in accepted:
                # Reserved views: ``fig/<N>`` is advertised in
                # ``precis-paper-help`` as a future-reserved
                # affordance. Surface a dedicated "reserved" error so
                # a caller who has read the help skill knows the
                # shape is right but the build is early — distinct
                # from a typo against the canonical enum.
                if effective_view.startswith("fig/"):
                    from precis.errors import Unsupported

                    raise Unsupported(
                        f"paper view {effective_view!r} is a reserved "
                        f"affordance — the help advertises fig/<N> but "
                        f"the build does not yet implement it",
                        next=(
                            "get(kind='skill', id='precis-paper-help') for "
                            "the current view enum"
                        ),
                    )
                from precis.errors import Unsupported

                raise Unsupported(
                    f"unknown view {effective_view!r} for kind={kind!r}",
                    options=accepted,
                    next=(
                        f"view= for kind={kind!r} accepts: {accepted}; "
                        f"omit view= for the {kind} overview"
                    ),
                )

        # ``args={'table'|'match'}`` belong to view='tables' — anywhere
        # else (another view, a chunk range, the bare overview) they'd
        # be silently dropped; refuse and name the view they need.
        if (
            effective_view != "tables"
            and isinstance(args, dict)
            and (args.get("table") is not None or args.get("match") is not None)
        ):
            raise BadInput(
                "args={'table'|'match'} only apply to view='tables', "
                f"not view={effective_view!r}",
                next=(
                    f"get(id='{_pa(ref)}', view='tables', "
                    "args={'table': 1} or args={'match': 'word'})"
                ),
            )

        # Combined form: ``slug~A..B/toc`` → range-scoped TOC drill-down.
        # Only ``view='toc'`` is valid with a chunk_spec; other views
        # don't have a sensible "this range only" meaning yet.
        if chunk_spec is not None and effective_view is not None:
            if effective_view == "toc":
                return self._render_toc(ref, scope=chunk_spec)
            # Build the full id string first, then repr() it whole — the
            # MCP critic flagged ``id={slug!r}~{lo}..{hi}/toc`` as
            # producing ``id='slug'~38..38/toc`` (slug repr'd, suffix
            # outside the quotes) which is a SyntaxError when pasted.
            recovery_id = f"{_pa(ref)}~{chunk_spec[0]}..{chunk_spec[1]}/toc"
            raise BadInput(
                f"cannot combine chunk selector (~N..M) with view={effective_view!r}",
                # The caller almost always wants the ref-level view (a
                # ``pc<id>`` chunk handle from search results + view=
                # lands here — gr484234-era mining). Name that call, then
                # the range-scoped form that does exist.
                next=(
                    f"the view is ref-level — drop the selector: "
                    f"get(id='{_pa(ref)}', view={effective_view!r}); "
                    f"or keep the range with view='toc': "
                    f"get(id='{recovery_id}')"
                ),
            )

        if chunk_spec is not None:
            return self._render_chunks(ref, chunk_spec)

        if effective_view is None:
            from precis.store.si_links import queue_si_on_attention

            queue_si_on_attention(self.store, ref.id, "mcp_get")
            return self._render_overview(ref)

        return self._render_view(ref, effective_view, args=args)

    # -- batch get (``id=[...]``, gr501175) -----------------------------------

    def _get_batch(self, ids: list[Any], *, view: str | None = None) -> Response:
        """``get(id=[...])``: one summary block per id, gripe-style
        (``_numeric_ref._get_batch``).

        Elements take every address the single ``get`` does — slug,
        ``pa<id>`` record handle, bare ref_id, DOI/arXiv, a ``pc<id>``
        chunk handle, or an id carrying its own ``~N``/``/view``
        selector (that element renders the single-id shape, so a list
        of ``pc`` handles reads several chunks at once). The default
        renders a compact block — handle, title, byline/venue, block
        count — not the full overview, for the same reason the
        numeric-ref batch is summary-shaped; ``view=`` maps the
        single-id view over the list, and ``view='status'`` delegates
        to the search-side status table (``search(view='status',
        ids=[…])``, gr500651) — "state of these N ids" is the same
        question asked on the other verb. A missing or deleted element
        is a one-line block, not a failed call.
        """
        kind = self.spec.kind
        if not ids:
            raise BadInput(
                "id=[] is empty",
                next=f"get(kind='{kind}', id=[<handle-or-slug>, ...])",
            )
        if len(ids) > _BATCH_ID_CAP:
            raise BadInput(
                f"id=[...] takes at most {_BATCH_ID_CAP} ids, got {len(ids)}",
                next=f"split into calls of {_BATCH_ID_CAP} or fewer",
            )
        if str(view or "").strip() == "status":
            # ``search(kind='paper', view='status', ids=[…])`` is the
            # same table — the caller already had the list, just on the
            # other verb (the gr501175 arg sample).
            return self._fetch_status_view([str(i) for i in ids])
        blocks: list[str] = []
        for elem in ids:
            shown = str(elem)
            try:
                raw, slug, chunk_spec, path_view = self._resolve_batch_element(elem)
                if view is not None or chunk_spec is not None or path_view is not None:
                    blocks.append(self.get(id=raw, view=view).body)
                    continue
                ref = resolve_live_slug_ref(
                    self.store,
                    kind=kind,
                    id=slug,
                    next_hint=f"search(kind='{kind}', q='...') to find existing",
                )
                blocks.append(self._batch_summary(ref))
            except NotFound:
                blocks.append(f"{shown}  not found")
            except Gone:
                blocks.append(f"{shown}  deleted")
        body = f"# {len(ids)} {kind}s (batch)\n\n" + "\n\n".join(blocks)
        body += render_next_section(
            [
                (
                    f"get(kind='{kind}', id={ids[0]!r})",
                    f"read the first {kind} in full",
                ),
                (
                    f"search(kind='{kind}', view='status', ids=[…])",
                    "per-paper ingest/fetch state",
                ),
            ]
        )
        return Response(body=body, result_count=len(ids))

    def _resolve_batch_element(
        self, elem: Any
    ) -> tuple[str, str, tuple[int, int] | None, str | None]:
        """One ``id=[...]`` element → ``(raw, slug, chunk_spec, path_view)``.

        Universal handles self-identify: a ``pa<id>`` record handle
        rewrites to the slug, a ``pc<id>`` chunk handle to
        ``<slug>~<ord>`` — the same rewrite dispatch's single-id path
        does (``_maybe_infer_kind_from_handle``) — so a batch of ``pc``
        handles reads chunks, and a handle of another kind refuses
        naming the right one. ``raw`` is the re-parsable id string
        (selectors preserved) for the full-render path; ``slug`` is
        the bare ref address for the summary path.
        """
        kind = self.spec.kind
        raw = str(elem).strip()
        mm = re.match(r"^([a-zA-Z]{2}\d+)([~/].*)$", raw)
        base, suffix = (mm.group(1), mm.group(2)) if mm else (raw, "")
        normalized = handle_registry.normalize(base)
        if handle_registry.is_well_formed(normalized):
            resolved = self.store.resolve_handle(normalized)
            if resolved is not None:
                if resolved.kind != kind:
                    raise BadInput(
                        f"id element {raw!r} is a {resolved.kind} handle, not {kind}",
                        next=f"get(kind={resolved.kind!r}, id={raw!r})",
                    )
                if resolved.chunk_id is not None:
                    if resolved.chunk_ord is not None:
                        raw = f"{resolved.public_id}~{resolved.chunk_ord}"
                    # ``chunk_ord=None`` falls through — the raw handle
                    # parses as a slug below and NotFound's cleanly.
                else:
                    raw = resolved.public_id + suffix
        raw = _maybe_resolve_doi(self.store, raw)
        slug, chunk_spec, path_view = _parse_paper_id(raw, kind)
        return raw, slug, chunk_spec, path_view

    def _batch_summary(self, ref: Ref) -> str:
        """The compact per-ref block for ``get(id=[...])``: handle +
        title line, then byline/venue and the block count."""
        meta = ref.meta or {}
        title = title_md((ref.title or "").split("\n", 1)[0].strip())
        head = f"{_pa(ref)}  {title}".rstrip()
        sub: list[str] = []
        authors = _format_authors(ref.authors)
        if authors:
            sub.append(authors)
        venue = ", ".join(
            x
            for x in (
                _clean_inline_text(str(meta.get("journal") or "")),
                str(ref.year) if ref.year else "",
            )
            if x
        )
        if venue:
            sub.append(venue)
        n_blocks = self.store.chunks.count_chunks(ref.id)
        sub.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")
        return head + "\n" + " · ".join(sub)

    # -- search --------------------------------------------------------------

    def search(
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        tags: list[str] | None = None,
        # ``None`` picks the mode-aware default: 25 rows on the default
        # ``unique_per='paper'`` page, 10 on the per-chunk page — the MCP
        # surface forwards ``None`` when the caller didn't name a size so
        # this default can follow the resolved row shape.
        page_size: int | None = None,
        page: int = 1,
        exclude: list[str] | None = None,
        mode: str | None = None,
        after: int | str | None = None,
        before: int | str | None = None,
        queries: list[str] | None = None,
        answers: list[str] | None = None,
        per_paper: int | None = None,
        # Row granularity: ``'paper'`` (default) collapses the fused hits
        # to one row per ref — best surviving chunk's handle + a ``more``
        # count + that chunk's keywords; ``'chunk'`` is the per-hit drill
        # shape. Omitted, ``scope=`` or ``mode='verbatim'`` imply 'chunk'
        # (both are already drill doors).
        unique_per: str | None = None,
        good: bool = False,
        title: str | None = None,
        author: str | None = None,
        # dispatch-injected (see runtime.dispatch._resolve_uncited_exclude,
        # ``search(uncited=<draft>)``): additional ref_ids to drop, already
        # resolved to a set — orthogonal to ``exclude=`` (which is a mixed
        # slug/container list resolved by ``resolve_exclude_paper_ids``).
        # Declared as an explicit kwarg (not swallowed by ``**_kw``) so the
        # filter can never be silently dropped for paper/cfp/datasheet.
        exclude_ref_ids: list[int] | None = None,
        # dispatch-injected (see runtime.dispatch._resolve_cited_include /
        # ._resolve_hubbed_facet — ``search(cited=<draft>)`` /
        # ``search(hubbed=True)``): the inclusion mirror of
        # ``exclude_ref_ids``, already resolved. ``None`` means no
        # restriction; an empty list means "restricted to zero sources"
        # and must return zero hits, not fall through unfiltered.
        include_ref_ids: list[int] | None = None,
        # ``view='count'`` (gr500649): filtered total + ``group_by=``
        # facets, same contract as the numeric-ref kinds. ``view='status'``
        # (gr500651, ``ids=`` handles/slugs) and ``view='ingest'``
        # (gr500643, ``days=``) are paper-only ingest-state reads.
        view: str | None = None,
        group_by: str | list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        ids: list[str] | None = None,
        days: int | None = None,
        # ``args={'serves': 'quest:<id>'}`` (gr503059): restrict hits to
        # the refs serving that quest (inbound ``serves`` edges, live
        # only). ``args={'has_fulltext': True|False}`` restricts to refs
        # with / without body chunks — SQL-level, honest in paged totals.
        # ``args={'record_type': 'software'|'dataset'}`` (gr514384)
        # restricts to papers typed as whole-record cites.
        serves: str | None = None,
        has_fulltext: bool | None = None,
        record_type: str | None = None,
        # ``args={'chunk_kind': 'table'}`` (gr506762): restrict hits to
        # chunks of the named kind(s) — a table-only search for the
        # parsed-grid read. Validated against ``chunk_kinds.slug``;
        # implies the per-chunk drill shape like ``scope=``.
        chunk_kind: str | list[str] | None = None,
        # ``args={'stamp': '<meta key>'}`` (gr517627): the live refs
        # carrying that ``refs.meta`` worker/enrichment stamp —
        # ``context_sentence_failed``, ``s2_enriched_at``, … With no
        # ``q=`` this is a paged ids+count listing (verifying a stamp
        # cohort without SQL); with ``q=`` it restricts hits via the
        # include channel like ``record_type=``.
        stamp: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Dispatch to the right search collaborator (see ``_paper_search.py``).

        Five shapes, checked in order: ``title=``/``author=`` (byline
        record lookup), a bare ``tags=`` with no ``q=`` (recency-ordered
        ref listing, gr311340 — see :meth:`_render_tags_only_search`), ``good=True``
        (deep-search campaign submit), else the block-hit fused search
        (single-leg or broad). This method only validates + routes; the
        actual retrieval and rendering live in
        :mod:`precis.handlers._paper_search`.
        """
        kind = self.spec.kind
        if view is not None and view not in ("index", "ids"):
            if view == "count":
                return self._count_view(
                    q=q,
                    tags=tags,
                    group_by=group_by,
                    since=since,
                    until=until,
                    exclude_ref_ids=exclude_ref_ids,
                    include_ref_ids=include_ref_ids,
                )
            if view == "status":
                return self._fetch_status_view(ids)
            if view == "ingest":
                return self._ingest_rate_view(days)
            # Only name views the kind actually serves (gr520077): the
            # chase queue is the DOI-fetcher backlog — paper-only.
            options = ["count", "status", "ingest", "stubs"] + (
                ["chase-queue"] if kind == "paper" else []
            )
            raise BadInput(
                f"unknown view {view!r} for search(kind={kind!r})",
                options=options,
                next=(
                    f"search(kind={kind!r}, view='count', "
                    "args={'group_by': 'tag:studytype'}) · view='status' "
                    "(ids=[…]) · view='ingest' (days=14)"
                ),
            )
        # Field-scoped byline lookup — ``title=`` / ``author=`` return
        # paper *records* (handle + citation), the targeted alternative to
        # the block-hit path when you know the title or an author. Routed
        # first: it needs no q= and ignores the block-search filters.
        title_q = title.strip() if isinstance(title, str) and title.strip() else None
        author_q = (
            author.strip() if isinstance(author, str) and author.strip() else None
        )
        if title_q or author_q:
            if kind != "paper":
                raise BadInput(
                    f"title=/author= lookup is paper-only (kind={kind!r})",
                    next="search(kind='paper', title='…')  # or author='…'",
                )
            if title_q and author_q:
                raise BadInput(
                    "pass title= or author=, not both",
                    next="search(kind='paper', author='Vaswani')",
                )
            q_byline = title_q or author_q
            assert q_byline is not None  # exactly one is set (guarded above)
            return BylineSearch(store=self.store).run(
                field="title" if title_q else "author",
                q=q_byline,
                page=page,
                # Byline pages are already one row per paper — the
                # mode-aware block-search default (25 paper rows) doesn't
                # apply here, so an unset page_size keeps the old 10.
                page_size=page_size if page_size is not None else 10,
                kind=kind,
            )

        # ``args={'stamp': '<meta key>'}`` (gr517627): alone, a paged
        # ids+count listing of the live refs carrying that meta stamp —
        # tags=/serves=/record_type=/has_fulltext= still narrow it (the
        # cohort filter is SQL-side, not an id-array round trip); with
        # q= it restricts hits through the same include channel
        # serves=/record_type= use.
        stamp_norm = (
            str(stamp).strip() if isinstance(stamp, str) and stamp.strip() else None
        )
        if stamp is not None and stamp_norm is None:
            raise BadInput(
                f"args={{'stamp': …}} takes a non-empty meta key, got {stamp!r}",
                next="search(kind='paper', args={'stamp': 'context_sentence_failed'})",
            )
        if stamp_norm is not None:
            if q is None or not q.strip():
                return self._render_stamp_search(
                    stamp=stamp_norm,
                    tags=tags,
                    serves=serves,
                    has_fulltext=has_fulltext,
                    record_type=record_type,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    exclude=exclude,
                    after=after,
                    before=before,
                    since=since,
                    until=until,
                    page=page,
                    page_size=page_size if page_size is not None else 25,
                )
            stamped = self._resolve_stamp_scope(stamp_norm)
            include_ref_ids = sorted(
                (set(include_ref_ids) & stamped)
                if include_ref_ids is not None
                else stamped
            )

        if q is None or not q.strip():
            # ``tags=`` alone is a valid call — precis-paper-tag-axes
            # documents ``search(kind='paper', tags=['studytype:review'])``
            # as the tag-only recipe (gr311340). Without q= there's
            # nothing to rank against, so this degrades to a filter-only
            # enumeration (recency order) rather than the FTS/semantic
            # block search below — same shape as ``_numeric_ref.py``'s
            # ``tags=``-only path. ``good=``/``queries=``/``answers=`` are
            # ranking-mode flags that need a real query; if any of those
            # are set we fall through to the original "requires q=" error
            # instead of silently ignoring them.
            if tags and not good and not queries and not answers:
                return self._render_tags_only_search(
                    tags=tags,
                    page=page,
                    page_size=page_size if page_size is not None else 10,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    exclude=exclude,
                    after=after,
                    before=before,
                    since=since,
                    until=until,
                )
            raise BadInput(
                "search requires q= or tags=",
                next=(
                    f"search(kind='{kind}', q='your query') or "
                    f"search(kind='{kind}', tags=['<tag>'])"
                ),
            )

        # Broad-retrieval boundary checks — same caps + messages as the
        # MCP tool surface (``tools/core.py``). Re-checked here because
        # the agentic tier calls the handler directly, bypassing MCP
        # validation; without this the fan-out is unbounded.
        if queries is not None and len(queries) > _BROAD_LEG_CAP:
            raise BadInput(
                f"queries= has {len(queries)} entries, max {_BROAD_LEG_CAP}",
                next="pass up to 8 distinct rephrasings; merge the rest",
            )
        if answers is not None and len(answers) > _BROAD_LEG_CAP:
            raise BadInput(
                f"answers= has {len(answers)} entries, max {_BROAD_LEG_CAP}",
                next="pass up to 8 hypothetical-answer passages (HyDE)",
            )
        # NB ``isinstance(True, int)`` is True in Python — reject bools
        # explicitly so ``per_paper=True`` doesn't silently become cap 1.
        if per_paper is not None and (
            isinstance(per_paper, bool)
            or not isinstance(per_paper, int)
            or per_paper < 1
        ):
            raise BadInput(
                f"per_paper must be a positive integer, got {per_paper!r}",
                next="per_paper=2 keeps at most 2 hits per paper",
            )

        # ``unique_per`` — 'paper' (default) renders one row per ref on
        # the fused candidate list; 'chunk' is the per-hit drill shape.
        # The drill doors imply 'chunk': ``scope=`` already restricts to
        # one ref, and ``mode='verbatim'``'s keyword path applies no
        # per-paper collapse at all. An explicit ``unique_per='paper'``
        # with verbatim is a real conflict (the mode can't honour it), so
        # it errors rather than silently falling back to chunk rows.
        unique_per_norm = (
            str(unique_per).strip().lower() if unique_per is not None else None
        )
        if unique_per_norm is not None and unique_per_norm not in (
            "paper",
            "chunk",
        ):
            raise BadInput(
                f"unique_per must be 'paper' or 'chunk', got {unique_per!r}",
                next="unique_per='chunk' lists one row per chunk hit",
            )
        verbatim_mode = (mode or "").strip().lower() == "verbatim"
        if unique_per_norm == "paper" and verbatim_mode:
            raise BadInput(
                "unique_per='paper' does not apply to mode='verbatim' — "
                "the keyword-containment path has no ranked per-paper "
                "pool to collapse",
                next="drop mode='verbatim', or use unique_per='chunk'",
            )
        # unique_per='paper' is the *paper* shape — its headline counts
        # papers and its rows carry paper-drift semantics. Subclassed
        # kinds (cfp, datasheet) share this handler but keep chunk rows.
        if unique_per_norm == "paper" and kind != "paper":
            raise BadInput(
                f"unique_per='paper' is a paper-search shape, not kind='{kind}'",
                next="drop unique_per= — chunked hits are the shape here",
            )
        # ``args={'chunk_kind': 'table'}`` — validate against the
        # ``chunk_kinds`` catalogue; an unknown slug gets the body-kind
        # list back, like the enum on a bad ``view=``.
        chunk_kinds: list[str] | None = None
        if chunk_kind is not None:
            raw_kinds = (
                [chunk_kind] if isinstance(chunk_kind, str) else list(chunk_kind)
            )
            if not raw_kinds:
                raise BadInput(
                    "args={'chunk_kind': []} is empty",
                    next="args={'chunk_kind': 'table'} keeps only table chunks",
                )
            with self.store.pool.connection() as conn:
                known = {
                    str(r[0])
                    for r in conn.execute("SELECT slug FROM chunk_kinds").fetchall()
                }
            bad = [k for k in raw_kinds if str(k) not in known]
            if bad:
                raise BadInput(
                    f"unknown chunk_kind {bad[0]!r} for kind={kind!r}",
                    options=sorted(known),
                    next="args={'chunk_kind': 'table'} restricts hits to one "
                    "body chunk kind",
                )
            chunk_kinds = [str(k) for k in raw_kinds]

        resolved_unique_per = (
            unique_per_norm
            if unique_per_norm is not None
            else (
                "paper"
                if kind == "paper"
                and scope is None
                and not verbatim_mode
                and chunk_kinds is None
                else "chunk"
            )
        )
        # ``per_paper=N`` is a chunk-mode diversity cap; paper rows already
        # collapse to one row per ref, so a larger cap would silently do
        # nothing. ``per_paper=1`` is exactly what paper mode applies —
        # accept it as a no-op rather than break old call sites.
        if resolved_unique_per == "paper" and per_paper is not None and per_paper != 1:
            raise BadInput(
                f"per_paper={per_paper} is a chunk-mode cap; "
                "unique_per='paper' already returns one row per paper",
                next="drop per_paper=, or pass unique_per='chunk' for the "
                "capped per-hit view",
            )
        if page_size is None:
            page_size = 25 if resolved_unique_per == "paper" else 10

        # ``good=True`` — the deep-search campaign surface. Does NOT
        # search inline: mints a ``good_search`` coordinator job (under
        # an auto-minted ephemeral todo) and returns an async handle
        # the caller polls. Paper-only — the cfp subclass shares this
        # method but a spec-role doc is never deep-searched.
        if good:
            if kind != "paper":
                raise BadInput(
                    f"good=True is a paper-only deep search (kind={kind!r})",
                    next="search(kind='paper', q='…', good=True)",
                )
            from precis.handlers._good_search import submit_good_search

            # self.hub is set at registration; a hand-constructed handler
            # (tests) leaves it None, so fall back to a minimal hub over
            # the same store — submit_good_search only needs hub.store
            # and hub.sibling(...).
            hub = self.hub if self.hub is not None else Hub(store=self.store)
            return submit_good_search(
                hub,
                q=q.strip(),
                queries=queries,
                answers=answers,
            )

        # ``serves='quest:<id>'`` — the refs serving that quest (its
        # inbound ``serves`` edges) intersect the include channel, same
        # composition discipline as cited=/hubbed=. An empty serving set
        # means zero hits, not an unfiltered search.
        if serves is not None:
            serving = self._resolve_serves_scope(serves)
            include_ref_ids = sorted(
                (set(include_ref_ids) & serving)
                if include_ref_ids is not None
                else serving
            )
        if has_fulltext is not None and not isinstance(has_fulltext, bool):
            raise BadInput(
                f"has_fulltext must be a boolean, got {has_fulltext!r}",
                next="args={'has_fulltext': true} keeps only refs with body chunks",
            )
        if record_type is not None:
            rt = str(record_type).strip().lower()
            if rt not in RECORD_TYPES:
                raise BadInput(
                    f"record_type= takes 'software' or 'dataset', got {record_type!r}",
                    options=sorted(RECORD_TYPES),
                    next="search(kind='paper', q='…', "
                    "args={'record_type': 'software'})",
                )
            # The include-channel composition, same as serves= above:
            # an empty typed set means zero hits, never unfiltered.
            typed = self._resolve_record_type_scope(rt)
            include_ref_ids = sorted(
                (set(include_ref_ids) & typed) if include_ref_ids is not None else typed
            )

        # Everything else — validation, single-leg vs. broad (RRF-fused)
        # retrieval, title-introducer promotion, year-omitted notice — is
        # FusedBlockSearch's job; PaperSearchResultRenderer turns the
        # result into the agent-facing Response (empty-hits DOI-aware
        # guidance, or headline + TOON table + pagination trailer).
        result = FusedBlockSearch(
            store=self.store, embedder=self.embedder, kind=kind
        ).run(
            q=q,
            scope=scope,
            tags=tags,
            page_size=page_size,
            page=page,
            exclude=exclude,
            mode=mode,
            after=after,
            before=before,
            queries=queries,
            answers=answers,
            per_paper=per_paper,
            unique_per=resolved_unique_per,
            unique_per_arg=unique_per_norm,
            extra_exclude_ref_ids=exclude_ref_ids,
            extra_include_ref_ids=include_ref_ids,
            has_body=has_fulltext,
            chunk_kinds=chunk_kinds,
        )
        return PaperSearchResultRenderer(kind=kind).render(result)

    def _resolve_record_type_scope(self, record_type: str) -> set[int]:
        """``args={'record_type': 'software'}`` → the live refs of this kind
        carrying that ``meta.record_type`` (gr514384 — the whole-record
        cite discoverability filter; SQL-level, honest in paged totals via
        the ``include_ref_ids`` channel)."""
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = %s "
                "AND retired_at IS NULL AND meta->>'record_type' = %s",
                (self.spec.kind, record_type),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def _resolve_serves_scope(self, serves: Any) -> set[int]:
        """``args={'serves': 'quest:<id>'}`` → the live refs serving it.

        Accepts ``quest:<id>``, a ``qu<id>`` handle or a bare numeric id.
        The serving set is the src end of the quest's inbound ``serves``
        edges — the same listing ``get(kind='quest', view='served')``
        renders. Raises ``NotFound`` when the quest doesn't resolve
        (a named target must never degrade to an unfiltered search).
        """
        links = self.store.links_for(
            self._serves_quest_id(serves), direction="in", relation="serves"
        )
        return {int(ln.src_ref_id) for ln in links}

    def _serves_quest_id(self, serves: Any) -> int:
        """The ref_id of the quest ``serves=`` names — the parse +
        existence check :meth:`_resolve_serves_scope` shares with the
        SQL-side ``serves`` filters."""
        token = str(serves).strip()
        if token.startswith("quest:"):
            token = token[len("quest:") :].strip()
        parsed = handle_registry.parse(token)
        if parsed is not None and parsed[0] == "quest" and not parsed[1]:
            qid = parsed[2]
        elif token.isdigit():
            qid = int(token)
        else:
            raise BadInput(
                f"serves= takes a quest reference, got {serves!r}",
                next="args={'serves': 'quest:164903'}",
            )
        qref = self.store.get_ref(kind="quest", id=qid)
        if qref is None:
            raise NotFound(
                f"quest id={qid} not found",
                next="search(kind='quest', q='...') to find the quest",
            )
        return qref.id

    def _resolve_stamp_scope(self, stamp: str) -> set[int]:
        """``args={'stamp': '<meta key>'}`` → the live refs of this kind
        carrying that ``refs.meta`` key — the worker/enrichment stamps
        (``context_sentence_failed``, ``s2_enriched_at``, …; gr517627)."""
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = %s "
                "AND retired_at IS NULL AND meta ? %s",
                (self.spec.kind, stamp),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def _resolve_browse_filters(
        self,
        *,
        include_ref_ids: list[int] | None,
        exclude_ref_ids: list[int] | None,
        exclude: list[str] | None,
        after: int | str | None,
        before: int | str | None,
        since: str | None,
        until: str | None,
    ) -> dict[str, Any]:
        """Resolve the raw search kwargs a queryless listing must honour
        (r2 S2 — never drop one silently) into store/clause-ready values:
        the ``include_ref_ids`` channel passed through (``None`` = no
        restriction, an empty list = zero hits), ``uncited=`` +
        ``exclude=`` merged into one drop set, ``after=``/``before=`` as
        publish-year bounds, and ``since=``/``until=`` as a created_at
        window (until exclusive). Both no-q doors resolve here so the
        two can't drift."""
        kind = self.spec.kind
        drop_ids = set(exclude_ref_ids or ())
        if exclude:
            drop_ids |= set(
                resolve_exclude_paper_ids(
                    [str(e) for e in exclude], store=self.store, kind=kind
                )
            )
        year_from = _coerce_search_year(after, "after")
        year_to = _coerce_search_year(before, "before")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise BadInput(
                f"after={year_from} is later than before={year_to}",
                next=f"search(kind='{kind}', q='…', after=2019, before=2023)",
            )
        from precis.handlers._numeric_ref import _parse_window_bound

        return {
            "include_ref_ids": include_ref_ids,
            "exclude_ref_ids": sorted(drop_ids) or None,
            "year_from": year_from,
            "year_to": year_to,
            "since": _parse_window_bound(since, "since"),
            "until": _parse_window_bound(until, "until"),
        }

    def _render_stamp_search(
        self,
        *,
        stamp: str,
        tags: list[str] | None,
        serves: str | None,
        has_fulltext: bool | None,
        record_type: str | None,
        include_ref_ids: list[int] | None,
        exclude_ref_ids: list[int] | None,
        exclude: list[str] | None,
        after: int | str | None,
        before: int | str | None,
        since: str | None,
        until: str | None,
        page: int,
        page_size: int,
    ) -> Response:
        """The no-``q=`` ``args={'stamp': …}`` door (gr517627): a paged
        ids+count listing of the stamp cohort — the verifier's "which
        papers carry this stamp" question without SQL. Each row shows
        the handle, the (truncated) stamp value, and the title.

        The cohort filter lives in SQL (``meta ? %s``), not a Python id
        array, and the other filter args still apply — a stamp cohort
        intersected by ``tags=`` / ``serves=`` / ``record_type=`` /
        ``has_fulltext=`` / the include+exclude id channels /
        ``exclude=`` / ``after=``/``before=`` / ``since=``/``until=``
        lists the *filtered* cohort with an honest total (ruling: no
        early return that drops them)."""
        kind = self.spec.kind
        clauses = ["r.retired_at IS NULL", "r.kind = %s", "r.meta ? %s"]
        params: list[Any] = [kind, stamp]
        normalized_tags = Tag.normalize_filter(tags, kind=kind) if tags else None
        if normalized_tags:
            tag_frag, tag_params = build_tag_filter(normalized_tags, ref_alias="r")
            clauses.append(tag_frag)
            params.extend(tag_params)
        if serves is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM links l WHERE l.dst_ref_id = %s "
                "AND l.relation = 'serves' AND l.src_ref_id = r.ref_id)"
            )
            params.append(self._serves_quest_id(serves))
        if has_fulltext is not None:
            if not isinstance(has_fulltext, bool):
                raise BadInput(
                    f"has_fulltext must be a boolean, got {has_fulltext!r}",
                    next="args={'has_fulltext': true} keeps only refs with body chunks",
                )
            clauses.append(
                has_body_sql("r") if has_fulltext else f"NOT {has_body_sql('r')}"
            )
        if record_type is not None:
            rt = str(record_type).strip().lower()
            if rt not in RECORD_TYPES:
                raise BadInput(
                    f"record_type= takes 'software' or 'dataset', got {record_type!r}",
                    options=sorted(RECORD_TYPES),
                    next="search(kind='paper', args={'record_type': 'software'})",
                )
            clauses.append("r.meta->>'record_type' = %s")
            params.append(rt)
        bf = self._resolve_browse_filters(
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
            exclude=exclude,
            after=after,
            before=before,
            since=since,
            until=until,
        )
        if bf["include_ref_ids"] is not None:
            clauses.append("r.ref_id = ANY(%s)")
            params.append(list(bf["include_ref_ids"]))
        if bf["exclude_ref_ids"]:
            clauses.append("NOT (r.ref_id = ANY(%s))")
            params.append(bf["exclude_ref_ids"])
        clauses.extend(_year_range_clauses(bf["year_from"], bf["year_to"]))
        if bf["since"] is not None:
            clauses.append("r.created_at >= %s")
            params.append(bf["since"])
        if bf["until"] is not None:
            clauses.append("r.created_at < %s")
            params.append(bf["until"])
        where = " AND ".join(clauses)
        page_size = max(1, int(page_size))
        offset = max(0, (int(page) - 1) * page_size)
        with self.store.pool.connection() as conn:
            total_row = conn.execute(
                f"SELECT count(*) FROM refs r WHERE {where}", params
            ).fetchone()
            total = int((total_row or (0,))[0])
            rows = conn.execute(
                "SELECT r.ref_id, r.title, left(coalesce(r.meta->>%s, ''), 40) "
                f"FROM refs r WHERE {where} "
                "ORDER BY r.updated_at DESC, r.ref_id LIMIT %s OFFSET %s",
                [stamp, *params, page_size, offset],
            ).fetchall()
        if not rows:
            body = f"no live {kind} entries stamped {stamp!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='{kind}', view='count', "
                        "args={'group_by': 'meta.<key>'})",
                        "facet the corpus on a meta key instead",
                    ),
                ]
            )
            return Response(body=body, result_count=0)
        suffix = "" if total <= len(rows) and page == 1 else f" of {total}"
        noun = "entry" if len(rows) == 1 else "entries"
        lines = [f"# {len(rows)} {kind} {noun} stamped {stamp!r}{suffix}"]
        for ref_id, title, stamp_val in rows:
            handle = handle_registry.format_handle(kind, int(ref_id))
            preview = _excerpt(title_md(title or ""), limit=80)
            value = f"  = {stamp_val}" if stamp_val else ""
            lines.append(f"  {handle:<30}{value}  {preview}")
        body = "\n".join(lines)
        next_ops: list[tuple[str, str]] = []
        if offset + len(rows) < total:
            next_ops.append(
                (
                    f"search(kind='{kind}', args={{'stamp': {stamp!r}}}, "
                    f"page={page + 1})",
                    "next page",
                )
            )
        next_ops.append(
            (
                f"get(id='{handle_registry.format_handle(kind, int(rows[0][0]))}')",
                "open one paper from the list (paste any handle above)",
            )
        )
        body += render_next_section(next_ops)
        return Response(body=body, result_count=len(rows))

    # -- count / ingest-state views (gr500649 / gr500651 / gr500643) ---------

    def _count_view(
        self,
        *,
        q: str | None,
        tags: list[str] | None,
        group_by: Any,
        since: str | None,
        until: str | None,
        exclude_ref_ids: list[int] | None,
        include_ref_ids: list[int] | None,
    ) -> Response:
        """``search(kind='paper', view='count')`` — filtered total + facets.

        Same store query and facet grammar as the numeric-ref kinds
        (``_numeric_ref._count_view``); ``q=`` matches titles or any live
        body chunk. ``group_by='tag:<axis>'`` / ``'meta.<key>'`` /
        ``'fulltext'`` facet — ``'fulltext'`` buckets papers
        ``full-text | card-only | stub`` (the ``view='status'``
        vocabulary) and trails a ``passages:`` line counting live body
        chunks over the same filtered set (gr518168).
        """
        from precis.handlers._numeric_ref import (
            _COUNT_GROUP_LIMIT,
            _count_facet_label,
            _parse_count_facets,
            _parse_window_bound,
        )
        from precis.utils.next_block import render_next_section

        kind = self.spec.kind
        facets = _parse_count_facets(group_by)
        since_dt = _parse_window_bound(since, "since")
        until_dt = _parse_window_bound(until, "until")
        normalized = Tag.normalize_filter(tags, kind=kind) if tags else None
        total, groups = self.store.count_refs_facets(
            kind=kind,
            tags=normalized,
            q=q,
            q_in_chunks=True,
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
            facets=facets,
            group_limit=_COUNT_GROUP_LIMIT,
            since=since_dt,
            until=until_dt,
        )
        filters: list[str] = []
        if q and q.strip():
            filters.append(f"q={q!r}")
        if normalized:
            filters.append(f"tags={normalized!r}")
        if since_dt is not None or until_dt is not None:
            lo = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else "…"
            hi = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if until_dt else "…"
            filters.append(f"created [{lo}, {hi})")
        head = f"# {total} {kind} entr{'y' if total == 1 else 'ies'}"
        if filters:
            head += f" ({', '.join(filters)})"
        parts = [head]
        if facets:
            parts.append("")
            parts.append(" × ".join(_count_facet_label(f) for f in facets) + ":")
            for vals, n in groups:
                label = " | ".join(v if v is not None else "(none)" for v in vals)
                parts.append(f"  {label}: {n}")
            if len(groups) == _COUNT_GROUP_LIMIT:
                parts.append(f"  … (top {_COUNT_GROUP_LIMIT} groups by count)")
            if any(ft == "fulltext" for ft, _k in facets):
                # The facet counts *papers*; a grant-style "how many
                # full-text papers and passages" also wants the passage
                # total over the same filtered set (gr518168).
                passages = self.store.count_body_chunks(
                    kind=kind,
                    tags=normalized,
                    q=q,
                    q_in_chunks=True,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    since=since_dt,
                    until=until_dt,
                )
                parts.append(f"  passages: {passages} live body chunks")
        else:
            parts.append(
                render_next_section(
                    [
                        (
                            f"search(kind='{kind}', view='count', "
                            "args={'group_by': 'tag:studytype'})",
                            "facet by a tag axis (or 'meta.<key>')",
                        ),
                        (
                            f"search(kind='{kind}', view='ingest')",
                            "full-text vs card-only landings per day",
                        ),
                    ]
                )
            )
        return Response(body="\n".join(parts), result_count=total)

    def _require_paper_kind(self, view: str) -> None:
        if self.spec.kind != "paper":
            raise BadInput(
                f"view={view!r} is paper-only (kind={self.spec.kind!r})",
                next=f"search(kind='paper', view={view!r})",
            )

    def _fetch_status_view(self, ids: list[str] | None) -> Response:
        """``search(kind='paper', view='status', ids=[…])`` — one row per
        paper: body state (full-text / card-only / stub), has-PDF, and the
        newest fetcher outcome. Full-text = live non-card, non-references
        body chunks; ``card_*`` chunks are abstract/title cards only."""
        from precis.utils.next_block import render_next_section

        self._require_paper_kind("status")
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            raise BadInput(
                "view='status' needs ids= (paper handles, slugs or DOIs)",
                next="search(kind='paper', view='status', "
                "ids=['pa123', 'wang2020state'])",
            )
        if len(ids) > _STATUS_MAX_IDS:
            raise BadInput(
                f"ids= has {len(ids)} entries, max {_STATUS_MAX_IDS}",
                next=f"split the list into pages of up to {_STATUS_MAX_IDS}",
            )
        resolved = resolve_exclude_paper_ids([str(i) for i in ids], store=self.store)
        rows = self.store.paper_fetch_status(sorted(resolved))
        lines = [f"fetch status — {len(rows)} of {len(ids)} ids resolved:", ""]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["body"]] = counts.get(r["body"], 0) + 1
            h = handle_registry.format_handle("paper", r["ref_id"])
            last = (
                f"{r['last_event']} {r['last_ts'][:16]}Z ({r['last_source']})"
                if r["last_event"]
                else "never tried"
            )
            pdf = "pdf" if r["has_pdf"] else "no-pdf"
            lines.append(
                f"  {h}  [{r['cite_key'] or h}]  {r['body']}"
                f" ({r['full_chunks']} body chunks)  {pdf}  fetch: {last}"
            )
        unresolved = len(ids) - len(rows)
        summary = " · ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"
        if unresolved > 0:
            summary += f" · {unresolved} unresolved"
        lines.append("")
        lines.append(f"summary: {summary}")
        lines.append(
            render_next_section(
                [
                    ("search(kind='paper', view='stubs')", "the papers still to get"),
                    ("get(kind='paper', id='<handle>', view='log')", "full event log"),
                ]
            )
        )
        return Response(body="\n".join(lines), result_count=len(rows))

    def _ingest_rate_view(self, days: int | None) -> Response:
        """``search(kind='paper', view='ingest', days=N)`` — is ingestion
        landing papers? Per-UTC-day full-text vs card-only landings."""
        from datetime import timedelta

        from precis.utils.next_block import render_next_section

        self._require_paper_kind("ingest")
        n = 14 if days is None else days
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 90:
            raise BadInput(
                f"days must be an integer 1..90, got {days!r}",
                next="search(kind='paper', view='ingest', args={'days': 14})",
            )
        now = datetime.now(UTC)
        since = (now - timedelta(days=n - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = self.store.paper_ingest_rate(since=since)
        tot_full = sum(f for _d, f, _k in rows)
        tot_card = sum(k for _d, _f, k in rows)
        lines = [
            f"# paper landings, last {n} days (UTC, since {since:%Y-%m-%d}): "
            f"{tot_full} full-text · {tot_card} card-only",
            "full-text = first non-card, non-references body chunk created that "
            "day; card-only = created that day, only card_* chunks so far",
            "",
        ]
        if not rows:
            lines.append("  no landings in the window")
        for d, f, k in rows:
            lines.append(f"  {d}: {f} full-text · {k} card-only")
        lines.append(
            render_next_section(
                [
                    (
                        "search(kind='paper', view='status', ids=[…])",
                        "per-paper body state + last fetch outcome",
                    ),
                    ("search(kind='paper', view='stubs')", "the still-to-get backlog"),
                ]
            )
        )
        return Response(body="\n".join(lines), result_count=tot_full + tot_card)

    # -- search_hits: structured form for cross-kind merge -------------------

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        exclude: list[str] | None = None,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        # dispatch-injected (see runtime.dispatch._resolve_uncited_exclude,
        # ``search(uncited=<draft>)``): already-resolved ref_ids to drop,
        # merged with the ``exclude=`` slug resolution below. Declared
        # explicitly (not swallowed by ``**_kw``) so the cross-kind fan-out
        # never silently loses the filter for this kind.
        exclude_ref_ids: list[int] | None = None,
        # dispatch-injected (see runtime.dispatch._resolve_cited_include /
        # ._resolve_hubbed_facet): the inclusion mirror. ``None`` means no
        # restriction; an empty list means "restricted to zero sources".
        include_ref_ids: list[int] | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level fused search returned as ``SearchHit``s.

        Same engine as :meth:`search`, but skips the per-handler
        rendering and surfaces the structured rows so the runtime
        cross-kind dispatcher can RRF-fuse them with hits from
        other kinds.  ``scope=`` is intentionally omitted — cross-
        kind merge has no per-paper scope.

        ``exclude=`` mirrors the ``search`` shape (coarse, ref-level
        slug list, plus the ``dr…``/``dc…`` draft-container forms
        ``resolve_exclude_paper_ids`` supports). Cross-kind callers can
        pass it through so pagination works across the merged stream.
        ``exclude_ref_ids=`` is the pre-resolved numeric complement
        (``uncited=``'s closure). ``include_ref_ids=`` is the pre-resolved
        inclusion set (``cited=``'s closure, or ``hubbed=True``'s set) —
        threaded straight through (no ``include=`` slug counterpart to
        merge with).
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind=self.spec.kind)
        resolved_exclude_ref_ids: set[int] = set(exclude_ref_ids or ())
        # gr312636: batched (one ``fetch_refs_by_ids``/``fetch_ref_ids_by_slugs``
        # for the whole list, per-item fallback only for a genuine miss) —
        # same shared resolver ``search()``'s single-kind path uses via
        # ``FusedBlockSearch`` (gr311339), reused here instead of a serial
        # per-entry ``_normalise_exclude_slug`` loop.
        # ``.dead_handles`` (gr340059) is dropped here: this structured
        # cross-kind fan-out path returns bare ``SearchHit``s, not a
        # rendered ``Response``, so there's no body to attach the notice
        # to — the single-kind ``search(kind='paper', ...)`` path
        # (``FusedBlockSearch`` below) is where it's surfaced.
        resolved_exclude_ref_ids |= resolve_exclude_paper_ids(
            exclude, store=self.store, kind=self.spec.kind
        )
        # query_vec= may be pre-supplied by the runtime cross-kind
        # dispatcher (computed once for all kinds), avoiding an
        # extra embed_one(q) per fanned-out kind.
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        # Opt the title/meta card in (same as :meth:`search`) so a paper
        # is reachable by title in the cross-kind merge too, then dedup
        # so a body hit wins over its own card.
        triples = self.store.chunks.search_chunks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind=self.spec.kind,
            tags=normalized_tags,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=sorted(resolved_exclude_ref_ids) or None,
            include_ref_ids=include_ref_ids,
            card_kinds=("card_combined",),
        )
        triples = _dedup_card_hits(triples)
        # Title introducer, as in :meth:`search`: FTS strips an exact-title
        # query ("attention is all you need") to 'attent' & 'need', so the
        # paper's card loses on ts_rank. Without this the console's paper
        # view and the cross-kind merge (both built on search_hits) buried
        # a paper the single-kind search ranks first. Same gate as the
        # single-kind path (no tag filter); an inclusion restriction skips
        # it, and excluded refs are dropped so exclude= paging holds.
        promoted: set[int] = set()
        if not normalized_tags and include_ref_ids is None:
            triples, _callouts, promoted = FusedBlockSearch(
                store=self.store, embedder=self.embedder, kind=self.spec.kind
            )._inject_title_matches(
                triples, q=q, kind=self.spec.kind, page_size=page_size
            )
            if resolved_exclude_ref_ids:
                triples = [
                    t for t in triples if t[1].id not in resolved_exclude_ref_ids
                ]
            # The injector stamps promoted rows with a float('inf')
            # sentinel (a render cue for the single-kind table); a
            # SearchHit score feeds numeric tiebreaks, so cap it at the
            # best finite score — rank order already carries the promotion.
            finite = [s for _b, _r, s in triples if s != float("inf")]
            top = max(finite, default=1.0)
            triples = [(b, r, top if s == float("inf") else s) for b, r, s in triples]
        # Salience bump (block-level); no-op for dream-actor reads.
        self.store.chunks.bump_salience([block.id for block, _ref, _score in triples])
        hits = block_hits_to_search_hits(triples, kind=self.spec.kind)
        if promoted:
            # Pin the named paper so the cross-kind RRF merge keeps it on
            # top (see ``SearchHit.pinned``).
            hits = [
                dataclasses.replace(h, pinned=True) if h.ref_id in promoted else h
                for h in hits
            ]
        return hits

    # -- seven-verb surface --------------------------------------------------

    def _resolve_paper_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair.

        Accepts a numeric ``ref_id`` (the web addresses papers by id,
        e.g. the triage queue's "Clear flag" → ``tag`` and the detail
        page's link ops), a slug, or a DOI. Slugs are never all-digits,
        so the numeric branch is unambiguous — and it must come first,
        because :func:`resolve_live_slug_ref` stringifies its id and
        would otherwise look ``"5822"`` up as a *cite_key* (a guaranteed
        miss that raised ``NotFound`` and silently no-op'd the web tag
        ops). Mirrors :meth:`_resolve_paper_ref_id`'s numeric path.

        Rejects chunk selectors and path views — link/tag ops live
        at the ref level only. Raises ``BadInput`` (selector
        present) or ``NotFound`` (slug unknown) so the caller can
        let those propagate.
        """
        return resolve_paper_slug(self.store, id, kind=self.spec.kind)

    def _resolve_paper_ref_id(self, id: str | int) -> int:
        """Resolve an id to a live paper ``ref_id``.

        Accepts a numeric ``ref_id`` (the web addresses papers by id),
        a slug, or a DOI — slugs are never all-digits, so the branch is
        unambiguous. Raises ``NotFound`` if missing / soft-deleted or
        the ref isn't a paper. Thin wrapper over
        :meth:`_resolve_paper_slug` (which owns the numeric/slug/DOI
        resolution) for callers that only need the id.
        """
        _slug, ref_id = self._resolve_paper_slug(id)
        return ref_id

    def _edit_args(
        self, ref_id: int, args: dict[str, Any] | None, *, dry: bool
    ) -> list[str]:
        """Subclass hook: apply kind-specific ``edit(args=...)`` keys, return
        the change labels. Papers take none."""
        return []

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        title: str | None = None,
        year: int | None = None,
        authors: Any = None,
        abstract: str | None = None,
        doi: str | None = None,
        arxiv: str | None = None,
        journal: str | None = None,
        entry_type: str | None = None,
        volume: str | int | float | None = None,
        issue: str | int | float | None = None,
        pages: str | int | float | None = None,
        url: str | int | float | None = None,
        supplement_of: str | None = None,
        dry_run: bool | str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Repair a paper's bibliographic metadata.

        ``supplement_of=<parent slug | paN>`` instead declares this record
        the supplementary information (SI) of that paper
        (:meth:`_declare_supplement`); it takes no other field in the same
        call. ``args={'merge_into': <survivor>}`` folds this live ref into
        a confirmed duplicate's survivor (:meth:`_merge_duplicate_into`),
        taking ``args['reason']`` and no other field.
        ``args={'cite_key': '<newkey>', 'reason': '<why>'}`` re-keys a
        paper and relocates its stored PDF (:meth:`_rekey`).
        ``args={'drop_identifier': {'scheme': …, 'value': …}}`` removes
        exactly one ``ref_identifiers`` row — the surgical fix for a
        wrong id sitting next to correct ones
        (:meth:`_drop_identifier`); ``args={'drop_body': True,
        'keep_pdf': True}`` drops the body but keeps the pdf attachment
        so the bodiless-heal re-extracts it (:meth:`_drop_body`);
        ``args={'reextract': True}`` is that kept drop plus the queued
        ``paper_reextract`` job in one call — the repair for a wrong
        *extraction* of the right file (:meth:`_reextract`).
        ``args={'clear_stamp': <key|[keys]>}`` removes allow-listed
        worker-retry stamps from ``refs.meta`` so the owning pass
        re-claims the paper on its next sweep
        (:meth:`_clear_worker_stamps`); alone among the ``args`` doors
        it accepts ``id=[...]`` — a cohort re-arm is one call.

        The operator / agent affordance for fixing parse errors — wrong
        DOI, missing authors, off-by-one year. Paper *bodies* stay
        import-only; this never touches block text. Only the fields
        passed are changed; a ``None`` / blank field is left as-is
        (the web form's "leave blank to keep" contract).

        ``authors`` accepts any tolerated shape (name strings — including
        the ``get(view='authors')``/web textarea grammar ``"Family,
        Given M. [0000-0002-1825-0097]"``, its bracketed ORCID iD parsed
        off by :func:`precis.utils.authors._strip_orcid_bracket` —
        ``{family, given}`` or ``{name}`` dicts) and is canonicalised via
        :func:`precis.utils.authors.normalize_authors` — a flat "Family,
        Given" string splits to ``{given, family}``; anything ambiguous
        or junk-flagged stays ``{"name"}`` or is dropped. This always
        writes ``paper_authors.source='human'`` (the
        ``_default_author_source`` mapping for this door) and — as long
        as at least one author survived normalisation — stamps
        ``human_verified_at`` (:meth:`Store.set_human_verified`), a
        human byline fix being exactly the kind of review that stamp
        exists to record.
        ``abstract`` / ``journal`` / ``entry_type`` merge into ``meta``
        (``entry_type`` validated against the Crossref vocabulary —
        :data:`_paper_format.ENTRY_TYPE_LABELS` plus the export map's
        remaining types — and stored lowercase, matching what
        :mod:`precis.ingest.paper_meta_enrich` writes); ``volume`` /
        ``issue`` / ``pages`` / ``url`` merge the same way, under the
        meta keys the exporters already read (``volume`` / ``number``
        for ``issue=`` / ``pages`` / ``url`` — the enricher's spelling,
        gr515469); ``doi`` / ``arxiv`` replace this ref's alias
        via :meth:`Store.set_ref_identifier` — except ``doi=''`` /
        ``arxiv=''`` (an explicit *empty string*, not the omitted-field
        ``None`` default), which instead **clears** that identifier via
        :meth:`Store.clear_ref_identifier` (gr353804: a wrong DOI —
        e.g. an SI record stuck holding an unrelated paper's DOI —
        otherwise had no MCP-reachable door to remove). A manual ``journal`` /
        ``entry_type`` edit stamps no provenance key — unlike authors
        (``meta.authors_source``), nothing in the enrich pass tracks
        provenance for these two fields, so there's nothing to keep
        consistent with.

        ``dry_run=True`` (or ``'diff'`` / ``'full'``) previews the patch
        as ``field: old → new`` lines and writes nothing — inherited by
        cfp/datasheet (both ``PaperHandler`` subclasses).
        """
        dry_mode = normalize_dry_run(dry_run)
        raw_args = dict(args) if args else {}
        if "clear_stamp" in raw_args:
            if (
                any(k in raw_args for k in _EDIT_DOOR_ARGS)
                or supplement_of is not None
                or any(
                    v is not None
                    for v in (
                        title,
                        year,
                        authors,
                        abstract,
                        doi,
                        arxiv,
                        journal,
                        entry_type,
                        volume,
                        issue,
                        pages,
                        url,
                    )
                )
            ):
                raise BadInput(
                    "clear_stamp= cannot be combined with other edit fields",
                    next="make the clear_stamp call alone, then edit the rest",
                )
            return self._clear_worker_stamps(
                id, raw_args["clear_stamp"], dry=bool(dry_mode)
            )
        if isinstance(id, list):
            raise BadInput(
                f"edit(kind='{self.spec.kind}') fields take one id= at a "
                "time, not a list",
                next=(
                    "one call per ref — only args={'clear_stamp': ...} accepts id=[...]"
                ),
            )
        doors = [k for k in _EDIT_DOOR_ARGS if k in raw_args]
        if "keep_pdf" in raw_args and "drop_body" not in doors:
            # keep_pdf is a drop_body modifier — anywhere else it would
            # be silently ignored and the pdf dropped.
            raise BadInput(
                "args['keep_pdf'] only modifies the drop_body door",
                next=f"edit(kind='{self.spec.kind}', id='{id}', args="
                "{'drop_body': True, 'keep_pdf': True, 'reason': '<why>'})",
            )
        if "pdf_sha256" in raw_args and not {
            "drop_body",
            "reextract",
            "move_pdf_to",
        } & set(doors):
            # pdf_sha256 names the attachment a keep_pdf drop retains, a
            # reextract re-reads or a move_pdf_to re-homes — anywhere
            # else it is ignored.
            raise BadInput(
                "args['pdf_sha256'] only modifies the drop_body / reextract "
                "/ move_pdf_to doors",
                next=f"edit(kind='{self.spec.kind}', id='{id}', args="
                "{'drop_body': True, 'keep_pdf': True, 'pdf_sha256': "
                "'<sha>', 'reason': '<why>'})",
            )
        if doors:
            door = doors[0]
            if (
                len(doors) > 1
                or supplement_of is not None
                or any(
                    v is not None
                    for v in (
                        title,
                        year,
                        authors,
                        abstract,
                        doi,
                        arxiv,
                        journal,
                        entry_type,
                        volume,
                        issue,
                        pages,
                        url,
                    )
                )
            ):
                raise BadInput(
                    f"{door}= cannot be combined with other edit fields",
                    next=f"make the {door} call alone, then edit the rest",
                )
            reason = str(raw_args.get("reason") or _kw.get("reason") or "").strip()
            if door == "cite_key":
                return self._rekey(
                    id, raw_args["cite_key"], reason=reason, dry=bool(dry_mode)
                )
            if door == "retire":
                return self._retire_stub(
                    id, raw_args["retire"], reason=reason, dry=bool(dry_mode)
                )
            if door == "redirect_to":
                return self._repoint_supersede(
                    id,
                    raw_args["redirect_to"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "merge_into":
                return self._merge_duplicate_into(
                    id,
                    raw_args["merge_into"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "drop_body":
                keep_raw = raw_args.get("keep_pdf")
                keep_pdf = (
                    str(keep_raw).strip().lower() not in ("false", "0", "no", "")
                    if keep_raw is not None
                    else False
                )
                if raw_args.get("pdf_sha256") is not None and not keep_pdf:
                    raise BadInput(
                        "args['pdf_sha256'] names the attachment a keep_pdf "
                        "drop retains — it needs args['keep_pdf']: True",
                        next=f"edit(kind='{self.spec.kind}', id='{id}', args="
                        "{'drop_body': True, 'keep_pdf': True, 'pdf_sha256': "
                        f"'{raw_args['pdf_sha256']}', 'reason': '<why>'}})",
                    )
                return self._drop_body(
                    id,
                    reason=reason,
                    dry=bool(dry_mode),
                    keep_pdf=keep_pdf,
                    pdf_sha256=raw_args.get("pdf_sha256"),
                )
            if door == "reextract":
                return self._reextract(
                    id,
                    reason=reason,
                    dry=bool(dry_mode),
                    pdf_sha256=raw_args.get("pdf_sha256"),
                )
            if door == "move_body_to":
                return self._move_body_to(
                    id,
                    raw_args["move_body_to"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "move_pdf_to":
                return self._move_pdf_to(
                    id,
                    raw_args["move_pdf_to"],
                    reason=reason,
                    dry=bool(dry_mode),
                    pdf_sha256=raw_args.get("pdf_sha256"),
                )
            if door == "attach_pdf":
                return self._attach_pdf(
                    id,
                    raw_args["attach_pdf"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "split_body":
                return self._split_body(
                    id,
                    raw_args["split_body"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "convert_to":
                return self._convert_to(
                    id,
                    raw_args["convert_to"],
                    subtype=raw_args.get("subtype"),
                    reason=reason,
                    dry=bool(dry_mode),
                )
            if door == "drop_identifier":
                return self._drop_identifier(
                    id,
                    raw_args["drop_identifier"],
                    reason=reason,
                    dry=bool(dry_mode),
                )
            return self._clear_abstract(id, reason=reason, dry=bool(dry_mode))
        ref_id = self._resolve_paper_ref_id(id)
        # gr514995: an explicit blank abstract used to be a silent no-op
        # (a *wrong* abstract had no removal door). Blank means "keep"
        # for every other field, so rather than overload it the caller
        # gets pointed at the clear door; the web form never sends empty
        # fields, so nothing that used to work changes.
        if abstract is not None and not (
            isinstance(abstract, str) and abstract.strip()
        ):
            raise BadInput(
                "abstract=<blank> does not clear the abstract — blank "
                "fields keep the stored value",
                next=f"edit(kind='{self.spec.kind}', "
                f"id='{handle_registry.format_handle(self.spec.kind, ref_id)}', "
                "args={'clear_abstract': True, 'reason': '<why>'})",
            )
        extra_changes = self._edit_args(ref_id, args, dry=bool(dry_mode))
        if supplement_of is not None and str(supplement_of).strip():
            if any(
                v is not None
                for v in (title, year, authors, abstract, doi, arxiv, journal)
            ) or any(v is not None for v in (entry_type, volume, issue, pages, url)):
                raise BadInput(
                    "supplement_of= cannot be combined with other edit fields",
                    next="make the supplement_of= call alone, then edit the rest",
                )
            return self._declare_supplement(
                ref_id, str(supplement_of).strip(), dry=bool(dry_mode)
            )
        new_title = title.strip() if isinstance(title, str) and title.strip() else None
        new_authors = normalize_authors(authors) if authors else None
        if authors and not new_authors:
            # An all-junk list must not reach the store: ``[]`` is a real
            # write there (wipes every paper_authors row + the jsonb).
            raise BadInput(
                "authors= contains no usable author names (each entry was "
                "junk-guarded: an email, a heading, an over-long string)",
                next="pass names as 'Family, Given M. [orcid]' lines or "
                "{given, family} dicts; omit authors= to leave the byline",
            )
        meta_patch: dict[str, Any] = {}
        if isinstance(abstract, str) and abstract.strip():
            meta_patch["abstract"] = abstract.strip()
        if isinstance(journal, str) and journal.strip():
            meta_patch["journal"] = journal.strip()
        if entry_type is not None:
            et = str(entry_type).strip().lower()
            if et:
                if et not in ENTRY_TYPE_VOCAB:
                    raise BadInput(
                        f"entry_type={entry_type!r} is not a Crossref type; "
                        "accepted values are listed below",
                        options=sorted(ENTRY_TYPE_VOCAB),
                        next="edit again with a value from the list",
                    )
                meta_patch["entry_type"] = et
        # Bibliographic locators (gr515469): the meta keys the enricher
        # and the exporters read — Crossref ``issue`` lands as
        # ``meta.number`` (paper_meta_enrich's spelling). Scalars coerce
        # to str (an ``args={'volume': 12}`` int); a compound value is
        # refused rather than dropped silently.
        for kwarg_name, value, meta_key in (
            ("volume", volume, "volume"),
            ("issue", issue, "number"),
            ("pages", pages, "pages"),
            ("url", url, "url"),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise BadInput(
                    f"{kwarg_name}= takes a string or a number, not "
                    f"{type(value).__name__}",
                    next=f"edit again with {kwarg_name}='<value>'",
                )
            sval = str(value).strip()
            if sval:
                meta_patch[meta_key] = sval
        has_doi = bool(doi and str(doi).strip())
        has_arxiv = bool(arxiv and str(arxiv).strip())
        # gr353804: '' (explicit empty string) means "clear this
        # identifier"; None / omitted means "leave it alone" — the same
        # "blank = keep existing" contract the docstring already
        # promises for every other field, except here blank is
        # overloaded: the *string* '' is the one blank value that DOES
        # act (as a clear), while None stays inert. `doi.strip()`
        # deliberately reads on a non-None value only, so a caller who
        # explicitly passes doi=None (the default) never trips this.
        doi_clear = doi is not None and not str(doi).strip()
        arxiv_clear = arxiv is not None and not str(arxiv).strip()
        if (
            new_title is None
            and year is None
            and not new_authors
            and not meta_patch
            and not has_doi
            and not has_arxiv
            and not doi_clear
            and not arxiv_clear
        ):
            if extra_changes:
                return Response(
                    body=f"updated {self.spec.kind} id={ref_id}: "
                    f"{', '.join(extra_changes)}."
                )
            raise BadInput(
                f"edit(kind='{self.spec.kind}') needs at least one field to change",
                next=(
                    f"edit(kind='{self.spec.kind}', id=<slug|id>, "
                    "authors=[...], year=2024)"
                ),
            )
        if dry_mode:
            return self._render_paper_dry_run(
                ref_id,
                new_title=new_title,
                year=year,
                new_authors=new_authors,
                meta_patch=meta_patch,
                doi=doi if has_doi else None,
                arxiv=arxiv if has_arxiv else None,
                doi_clear=doi_clear,
                arxiv_clear=arxiv_clear,
            )
        changed: list[str] = []
        doi_edit_check: tuple[str, str] | None = None
        with self.store.tx() as conn:
            updated = self.store.update_paper_fields(
                ref_id,
                title=new_title,
                year=year,
                authors=new_authors,
                meta_patch=meta_patch or None,
                source="edit",
                conn=conn,
            )
            if new_authors:
                # A human byline correction (source='human', see
                # ``_default_author_source``) also signs off the paper's
                # metadata as reviewed — the same stamp the web Meta
                # tab's "Mark reviewed" toggle sets
                # (``precis_web/routes/papers.py::reviewed``), so a
                # deliberate author fix doesn't leave the paper looking
                # unreviewed. Not double-stamped: nothing else on this
                # write path touches ``human_verified_at``.
                self.store.set_human_verified(ref_id, by="agent", conn=conn)
            for scheme, value, clear in (
                ("doi", doi, doi_clear),
                ("arxiv", arxiv, arxiv_clear),
            ):
                if value and str(value).strip():
                    if self.store.set_ref_identifier(
                        ref_id, scheme, str(value), source="edit", conn=conn
                    ):
                        changed.append(scheme)
                elif clear and self.store.clear_ref_identifier(
                    ref_id, scheme, source="edit", conn=conn
                ):
                    # A no-op clear (nothing to remove) deliberately
                    # doesn't land here — "no change" is the honest
                    # response, not a false "cleared" claim (gr353804
                    # ask: "a clean no-op message" for the nothing-to-
                    # clear case).
                    changed.append(f"{scheme} cleared")
            if "doi" in changed and updated.title and updated.title.strip():
                # Just capture inputs here — the Crossref round-trip in
                # _doi_edit_warning happens after commit (below), never
                # inside the tx (see its docstring).
                doi_edit_check = (str(doi).strip(), updated.title)
            # Rewrite the derived search cards so an edit actually changes
            # what title/author/abstract search matches against — otherwise
            # the card_* chunks keep the stale (pre-edit) text. Uses the
            # *merged* post-update values (COALESCE means an unchanged field
            # still returns its current value), so e.g. a title-only edit
            # still rebuilds card_combined from the live authors + abstract.
            if new_title is not None or new_authors or meta_patch:
                meta = updated.meta or {}
                abstract_val = meta.get("abstract", "")
                kw = meta.get("keywords", [])
                rewrite_cards(
                    conn,
                    ref_id,
                    title=updated.title or "",
                    author_names=_shared_author_names(updated.authors),
                    abstract=abstract_val if isinstance(abstract_val, str) else "",
                    keywords=list(kw) if isinstance(kw, list) else [],
                )
        if new_title is not None:
            changed.append("title")
        if year is not None:
            changed.append("year")
        if new_authors:
            changed.append(f"authors({len(new_authors)})")
        changed.extend(meta_patch.keys())
        changed.extend(extra_changes)
        reminted = None
        if new_authors or year is not None:
            # A byline/year repair can retire a junk-mint cite_key
            # (gr521515): when the paper's current key is
            # anon*/user*/the*-shaped and the new metadata mints a real
            # one, re-mint it — the old key stays resolvable as a
            # cite_key_alias redirect. Runs post-commit so rekey's own
            # transaction and PDF move see the write above — under the
            # same REKEY_DEADLINE_S bound the explicit rekey door uses:
            # the corpus-mount probes inside must not hang the request.
            from precis.store.paper_rekey import (
                REKEY_DEADLINE_S,
                remint_junk_cite_key,
            )
            from precis.utils.deadline import DeadlineExceeded, call_with_deadline

            try:
                reminted = call_with_deadline(
                    lambda: remint_junk_cite_key(
                        self.store, ref_id, source="auto-remint"
                    ),
                    deadline_s=REKEY_DEADLINE_S,
                    label="remint_junk_cite_key",
                )
            except DeadlineExceeded:
                # The abandoned thread may still finish the rekey; the
                # junk-key sweep catches whatever it leaves behind.
                log.warning(
                    "edit: auto-remint of ref %d exceeded %.0fs",
                    ref_id,
                    REKEY_DEADLINE_S,
                )
        changed_str = ", ".join(changed) if changed else "no change"
        body = f"updated {self.spec.kind} id={ref_id}: {changed_str}."
        if reminted:
            body += f" cite_key re-minted — {reminted}"
        if doi_edit_check is not None:
            # Runs after the tx above has committed — see
            # _doi_edit_warning's docstring for why.
            doi_value, current_title = doi_edit_check
            doi_warning = self._doi_edit_warning(
                ref_id, doi=doi_value, current_title=current_title
            )
            if doi_warning:
                body = f"{body}\n{doi_warning}"
        return Response(body=body)

    def _clear_worker_stamps(
        self,
        id: str | int | list[str | int] | None,
        stamps: Any,
        *,
        dry: bool,
    ) -> Response:
        """``edit(args={'clear_stamp': ...})`` — remove allow-listed
        worker-retry stamps from ``refs.meta`` so the owning pass
        re-claims the ref on its next sweep (gr514347 — previously raw
        ``meta - key`` prod SQL, an op no operator agent may run).

        The allow-list lives in :mod:`precis.workers.retry_stamps`: each
        clearable key is registered beside the worker that writes it,
        and any other meta key is refused naming the allowed set —
        there is deliberately no generic meta-delete door. ``id=`` takes
        one ref (slug / ``pa<id>`` / ref_id / DOI) or a list of them, so
        a cohort re-arm is one call. ``dry`` reports what would clear
        without writing."""
        from precis.workers.retry_stamps import clearable_stamps

        if isinstance(stamps, str):
            keys = [stamps]
        elif isinstance(stamps, (list, tuple)):
            keys = list(stamps)
        else:
            keys = []
        allowed = clearable_stamps()
        bad = [k for k in keys if not isinstance(k, str) or k not in allowed]
        if bad or not keys:
            raise BadInput(
                "args['clear_stamp'] wants allow-listed worker-retry meta "
                f"keys; got {bad if bad else stamps!r}",
                options=sorted(allowed),
                next=(
                    f"edit(kind='{self.spec.kind}', id=<ref>, args="
                    "{'clear_stamp': 'context_sentence_no_abstract'})"
                ),
            )
        if id is None:
            raise BadInput(
                "clear_stamp needs id= — one ref or a list of them",
                next=(
                    f"edit(kind='{self.spec.kind}', id=[<ref>, ...], args="
                    "{'clear_stamp': 'context_sentence_no_abstract'})"
                ),
            )
        ids = list(id) if isinstance(id, list) else [id]
        if not ids:
            raise BadInput(
                "id=[] is empty — pass at least one ref",
                next=f"edit(kind='{self.spec.kind}', id=[<ref>, ...], args=...)",
            )
        ref_ids = [self._resolve_paper_ref_id(i) for i in ids]
        if dry:
            live = self.store.fetch_refs_by_ids(ref_ids, include_deleted=False)
            cleared: dict[int, list[str]] = {}
            for rid in ref_ids:
                ref = live.get(rid)
                meta = (ref.meta if ref is not None else None) or {}
                cleared[rid] = [k for k in keys if k in meta]
        else:
            cleared = {}
            with self.store.tx() as conn:
                for rid in ref_ids:
                    cleared[rid] = self.store.clear_ref_meta_keys(
                        rid, keys, source="edit", conn=conn
                    )
        verb = "would clear" if dry else "cleared"
        parts = [
            f"{key} ({allowed[key]}): "
            f"{sum(1 for rid in ref_ids if key in cleared[rid])} {verb}, "
            f"{sum(1 for rid in ref_ids if key not in cleared[rid])} already clear"
            for key in keys
        ]
        prefix = "DRY RUN (nothing written) — " if dry else ""
        return Response(
            body=(
                f"{prefix}clear_stamp on {len(ref_ids)} "
                f"{self.spec.kind} ref(s): " + "; ".join(parts) + "."
            )
        )

    def _resolve_ref_incl_retired(self, id: str | int) -> Ref | None:
        """Resolve a paper address to its ref WITHOUT the liveness filter.

        The redirect door addresses the retired tombstone itself — the
        thing every other path transparently follows to the survivor.
        Accepts a ``pa<id>`` handle, a bare ref_id / digits, or a slug
        (the loser keeps its cite_key across a merge). ``None`` for an
        unresolvable or foreign-kind id; the caller names the failure.
        """
        kind = self.spec.kind
        ref_id: int | None = None
        if isinstance(id, int):
            ref_id = id
        else:
            slug, sel, path_view = _parse_paper_id(str(id).strip(), kind)
            reject_chunk_or_path_view(
                kind=kind, slug=slug, sel=sel, path_view=path_view
            )
            parsed = handle_registry.parse(slug)
            if parsed is not None and not parsed[1]:
                if parsed[0] != kind:
                    return None
                ref_id = parsed[2]
            elif slug.isdigit():
                ref_id = int(slug)
            else:
                return self.store.get_ref(kind=kind, id=slug, include_deleted=True)
        ref = self.store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
        if ref is None or ref.kind != kind:
            return None
        return ref

    def _repoint_supersede(
        self, id: str | int, target: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'redirect_to': <live paper>})`` — re-point a
        retired/merged ref's supersede redirect at a different survivor.

        A dedup merge leaves the loser retired with
        ``meta.superseded_by=<survivor>`` and a ``survivor -supersedes->
        loser`` edge; handles and link targets follow that pointer
        transparently. When the merge picked the *wrong* survivor
        (gr501446: pa42495/42497/42498) the pointer itself was
        unreachable — every handle resolves through to the survivor
        before a handler sees it. This door moves both halves in one
        transaction and logs a ``redirect_repointed`` ref_event carrying
        old target, new target and ``reason`` — undo is the same call
        aimed back at the old target.

        Refuses (each with a ``next=``): a source that isn't a retired
        merge (live, or deleted with no pointer), a target that isn't
        live (missing, itself merged, or deleted), a target whose
        supersede chain leads back to the source (a cycle the
        reader-side follow would hang on), and a missing ``reason``.
        """
        kind = self.spec.kind
        src = self._resolve_ref_incl_retired(id)
        if src is None:
            raise NotFound(
                f"{kind} id={id!r} resolves to no ref (live or retired)",
                next=f"search(kind='{kind}', q='...') to find the ref",
            )
        src_h = _pa(src)
        if src.retired_at is None:
            raise BadInput(
                f"{src_h} is live — redirect_to re-points a retired/merged "
                "ref's supersede pointer only",
                next=f"folding a live duplicate is a different door: "
                f"edit(kind='{kind}', id='{src_h}', args="
                "{'merge_into': 'pa<M>', 'reason': '<why>'})",
            )
        try:
            old_id = int(str((src.meta or {}).get("superseded_by")))
        except (TypeError, ValueError):
            old_id = -1
        if old_id < 0:
            raise BadInput(
                f"{src_h} is retired but carries no meta.superseded_by — "
                "a plain deletion has no redirect to re-point",
                next="if it should resolve to another paper that is a merge: "
                "get(kind='skill', id='precis-paper-help')",
            )
        if not reason:
            raise BadInput(
                "args['redirect_to'] needs args['reason'] — the "
                "redirect_repointed ref_event carries it",
                next=f"edit(kind='{kind}', id='{src_h}', args="
                "{'redirect_to': 'pa<M>', 'reason': '<why>'})",
            )
        tgt_raw = "" if target is None else str(target).strip()
        tgt = self._resolve_ref_incl_retired(tgt_raw) if tgt_raw else None
        if tgt is None:
            raise BadInput(
                f"redirect_to={tgt_raw!r} resolves to no {kind} ref",
                next=f"search(kind='{kind}', q='...') for the live "
                "target's pa<id> or slug",
            )
        tgt_h = _pa(tgt)
        if tgt.id == src.id:
            raise BadInput(
                "redirect_to names the record itself",
                next="pass the live paper this stub should resolve to",
            )
        if tgt.retired_at is not None:
            chain_end = self.store.follow_supersede(tgt.id)
            if chain_end is not None:
                end_h = handle_registry.format_handle(tgt.kind, chain_end)
                raise BadInput(
                    f"{tgt_h} is itself merged into {end_h} — a redirect "
                    "must end on a live ref",
                    next=f"edit(kind='{kind}', id='{src_h}', args="
                    f"{{'redirect_to': '{end_h}', 'reason': '{reason}'}})",
                )
            raise BadInput(
                f"{tgt_h} is deleted (retired, no survivor) — a redirect "
                "must end on a live ref",
                next=f"search(kind='{kind}', q='...') for a live target",
            )
        with self.store.pool.connection() as conn:
            cycles = _supersede_chain_reaches(conn, tgt.id, src.id)
        if cycles:
            raise BadInput(
                f"redirect_to {tgt_h} would cycle: its superseded_by "
                f"chain leads back to {src_h}",
                next="pick a live target whose supersede chain does not "
                "pass through this stub",
            )
        old_h = handle_registry.format_handle(kind, old_id)
        if tgt.id == old_id:
            return Response(body=f"no change: {src_h} already redirects to {tgt_h}.")
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would re-point {src_h}: "
                    f"superseded_by {old_h} -> {tgt_h}, the supersedes "
                    "edge moved and a redirect_repointed ref_event logged."
                )
            )
        with self.store.tx() as conn:
            removed = self.store.remove_link(
                src_ref_id=old_id,
                dst_ref_id=src.id,
                relation="supersedes",
                conn=conn,
            )
            self.store.add_link(
                src_ref_id=tgt.id,
                dst_ref_id=src.id,
                relation="supersedes",
                set_by="agent",
                conn=conn,
            )
            self.store.stamp_ref_meta(src.id, {"superseded_by": tgt.id}, conn=conn)
            self.store.append_event(
                src.id,
                source="edit",
                event="redirect_repointed",
                payload={
                    "old_survivor": old_id,
                    "new_survivor": tgt.id,
                    "reason": reason,
                },
                conn=conn,
            )
        edge_note = (
            "supersedes edge moved"
            if removed
            else "no old supersedes edge found; new one added"
        )
        return Response(
            body=(
                f"re-pointed {src_h}: superseded_by {old_h} -> {tgt_h} "
                f"({edge_note}; ref_event 'redirect_repointed' logged). "
                f"Undo: edit(kind='{kind}', id='{src_h}', args="
                f"{{'redirect_to': '{old_h}', 'reason': '<why>'}})"
            )
        )

    def _merge_duplicate_into(
        self, id: str | int, target: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'merge_into': <live paper>})`` — fold this live
        ref into the named survivor (gr509659).

        The single-pair door for a duplicate the caller has already
        confirmed (a stub a user dropped a PDF for, a reconcile-missed
        twin): it wraps :func:`precis.ingest.dedup.merge_duplicate`, which
        migrates external identifiers, links and bib holds to the
        survivor, moves the body/PDF when the survivor lacks them, stamps
        ``meta.superseded_by`` + a ``supersedes`` edge on the loser,
        soft-deletes it, and logs ``duplicate_merged`` /
        ``soft_deleted_duplicate`` ref_events carrying ``reason``.

        ``id=`` is the *loser* (the ref that retires); ``merge_into`` is
        the *winner*. The call is caller-asserted but still gated on the
        shared title-compatibility check
        (:func:`precis.utils.title_merge.title_merge_refusal` plus a
        trigram-similarity floor — the same cutoffs
        ``reconcile_by_title_similarity`` and the mint-time stub guard
        use): a pair whose titles don't corroborate it refuses rather
        than retire a live ref on a maybe (gr501446 was a same-DOI pair
        that wasn't one work).

        Refuses (each with a ``next=``): a loser that isn't live
        (already merged → re-point with ``redirect_to``; retired
        otherwise → restore with ``retire: False`` first), a target that
        isn't live (missing, itself merged → aim at the chain end it
        names), a target equal to the loser, a missing ``reason``, and a
        title-gate failure naming the refusal (``short-title`` /
        ``year-unknown`` / ``year-mismatch`` / ``author-mismatch`` /
        low similarity) — fix the disagreeing field and retry.
        """
        from precis.ingest.dedup import merge_duplicate

        kind = self.spec.kind
        loser = self._resolve_ref_incl_retired(id)
        if loser is None:
            raise NotFound(
                f"{kind} id={id!r} resolves to no ref (live or retired)",
                next=f"search(kind='{kind}', q='...') to find the ref",
            )
        loser_h = _pa(loser)
        if loser.retired_at is not None:
            if (loser.meta or {}).get("superseded_by") is not None:
                end_id = self.store.follow_supersede(loser.id)
                end_h = (
                    handle_registry.format_handle(kind, end_id)
                    if end_id is not None
                    else "its survivor"
                )
                raise BadInput(
                    f"{loser_h} is already merged into {end_h} — nothing to fold",
                    next=f"to aim its redirect at a different survivor: "
                    f"edit(kind='{kind}', id='{loser_h}', args="
                    "{'redirect_to': 'pa<M>', 'reason': '<why>'})",
                )
            raise BadInput(
                f"{loser_h} is already retired — merge_into folds a live ref only",
                next=f"restore it first: edit(kind='{kind}', id='{loser_h}', "
                "args={'retire': False, 'reason': '<why>'})",
            )
        if not reason:
            raise BadInput(
                "args['merge_into'] needs args['reason'] — the dedup "
                "ref_events carry it",
                next=f"edit(kind='{kind}', id='{loser_h}', args="
                "{'merge_into': 'pa<M>', 'reason': '<why>'})",
            )
        tgt_raw = "" if target is None else str(target).strip()
        winner = self._resolve_ref_incl_retired(tgt_raw) if tgt_raw else None
        if winner is None:
            raise BadInput(
                f"merge_into={tgt_raw!r} resolves to no {kind} ref",
                next=f"search(kind='{kind}', q='...') for the live "
                "survivor's pa<id> or slug",
            )
        winner_h = _pa(winner)
        if winner.id == loser.id:
            raise BadInput(
                "merge_into names the record itself",
                next="pass the other live ref of the duplicate pair",
            )
        if winner.retired_at is not None:
            chain_end = self.store.follow_supersede(winner.id)
            if chain_end is not None:
                end_h = handle_registry.format_handle(winner.kind, chain_end)
                raise BadInput(
                    f"{winner_h} is itself merged into {end_h} — a merge "
                    "must land on a live ref",
                    next=f"edit(kind='{kind}', id='{loser_h}', args="
                    f"{{'merge_into': '{end_h}', 'reason': '{reason}'}})",
                )
            raise BadInput(
                f"{winner_h} is deleted (retired, no survivor) — a merge "
                "must land on a live ref",
                next=f"search(kind='{kind}', q='...') for a live target",
            )
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT similarity(%s, %s)",
                (loser.title or "", winner.title or ""),
            ).fetchone()
        sim = float(row[0]) if row is not None and row[0] is not None else 0.0
        refusal = title_merge_refusal(
            loser.title,
            loser.year,
            loser.authors,
            winner.title,
            winner.year,
            winner.authors,
            sim=sim,
            unknown_year_min_sim=_MERGE_UNKNOWN_YEAR_SIM,
        )
        if sim < _MERGE_TITLE_SIM_FLOOR or refusal is not None:
            raise BadInput(
                f"{loser_h} and {winner_h} fail the merge title gate "
                f"({refusal or 'low-similarity'}; sim {sim:.2f})",
                next=f"if they are the same paper, fix the disagreeing "
                f"field first (edit(kind='{kind}', id='{loser_h}', "
                "title=/year=/authors=[...])) then retry the merge_into "
                "call",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would fold {loser_h} into "
                    f"{winner_h}: identifiers, links and bib holds migrate "
                    f"to {winner_h}, its missing body/PDF move over, "
                    f"{loser_h} is soft-deleted with a supersede redirect "
                    "and ref_events 'duplicate_merged' / "
                    "'soft_deleted_duplicate' are logged."
                )
            )
        with self.store.tx() as conn:
            merge_duplicate(
                self.store,
                survivor_ref_id=winner.id,
                duplicate_ref_id=loser.id,
                source="agent",
                reason=reason,
                conn=conn,
            )
        merged_events = self.store.events_for(
            winner.id, event="duplicate_merged", limit=1
        )
        moved: list[str] = []
        if merged_events:
            moved = merged_events[0].payload.get("moved") or []
        moved_note = f"; {'/'.join(moved)} moved to {winner_h}" if moved else ""
        return Response(
            body=(
                f"merged {loser_h} into {winner_h}: external identifiers, "
                f"links and bib holds migrated{moved_note}; {loser_h} "
                "retired (supersede redirect; ref_events "
                "'duplicate_merged' / 'soft_deleted_duplicate' logged). "
                f"{loser_h} now resolves to {winner_h}."
            )
        )

    def _retire_stub(
        self, id: str | int, retire: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'retire': True, 'reason': ...})`` — retire a
        phantom stub; ``{'retire': False}`` un-retires it (gr509441).

        The door exists for refs that should never have been minted — a
        body-less stub whose title describes rather than names a paper,
        with no external identifier and no inbound cites. Anything more
        is not a phantom: the guards below refuse (each with a
        ``next=``) a ref that still carries live body chunks, an attached
        PDF, a non-``cite_key`` identifier, a live inbound cite edge
        (``cites`` / ``cited-by`` / ``awaits-evidence``), a ``[pa<id>]`` /
        ``§<cite_key>`` token in a live chunk, a ``paper_bib_entries``
        held cite, or a merge tombstone redirecting through it — the
        merge-survivor case, where each tombstone's redirect must be
        moved to another survivor via ``redirect_to`` FIRST.

        ``retire`` writes a ``deleted`` ref_event (the same tombstone
        convention ``delete`` uses, so a later ``get``'s ``Gone`` reads
        the reason); ``retire: False`` on the tombstone restores it and
        logs ``restored`` — the inverse call, so the door undoes itself.
        A merge tombstone (``meta.superseded_by``) is refused on both
        directions: re-point it with ``redirect_to``, don't resurrect it.
        """
        kind = self.spec.kind
        ref = self._resolve_ref_incl_retired(id)
        if ref is None:
            raise NotFound(
                f"{kind} id={id!r} resolves to no ref (live or retired)",
                next=f"search(kind='{kind}', q='...') to find the ref",
            )
        handle = _pa(ref)
        if not _args_truthy(retire):
            return self._unretire_stub(ref, reason=reason, dry=dry)
        if not reason:
            raise BadInput(
                "args['retire'] needs args['reason'] — the deleted "
                "ref_event carries it",
                next=f"edit(kind='{kind}', id='{handle}', args="
                "{'retire': True, 'reason': '<why>'})",
            )
        if ref.retired_at is not None:
            if (ref.meta or {}).get("superseded_by") is not None:
                raise BadInput(
                    f"{handle} is already a merge tombstone — its "
                    "superseded_by pointer is the redirect",
                    next=f"re-point it with edit(kind='{kind}', "
                    f"id='{handle}', args={{'redirect_to': '<pa<M>>', "
                    "'reason': '<why>'}})",
                )
            raise BadInput(
                f"{handle} is already retired",
                next=f"undo with edit(kind='{kind}', id='{handle}', "
                "args={'retire': False, 'reason': '<why>'})",
            )
        with self.store.pool.connection() as conn:
            blockers = _retire_blockers(conn, ref)
        if blockers:
            raise BadInput(
                f"{handle} is not a phantom stub — refusing to retire: "
                + "; ".join(blockers),
                next=f"a duplicate of a real record wants a merge, not a "
                f"retire: edit(kind='{kind}', id='{handle}', args="
                "{'merge_into': 'pa<M>', 'reason': '<why>'})",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would retire {handle}: "
                    "retired_at set and a 'deleted' ref_event logged."
                )
            )
        with self.store.tx() as conn:
            # Re-check inside the transaction: the guards above read on a
            # pooled conn, so a concurrent import/link between the two
            # could slip a blocker past them (TOCTOU).
            blockers = _retire_blockers(conn, ref)
            if blockers:
                raise BadInput(
                    f"{handle} is not a phantom stub — refusing to retire: "
                    + "; ".join(blockers),
                    next=f"a duplicate of a real record wants a merge, not "
                    f"a retire: edit(kind='{kind}', id='{handle}', args="
                    "{'merge_into': 'pa<M>', 'reason': '<why>'})",
                )
            # The event goes in BEFORE the UPDATE: migration 0229's
            # trail trigger writes its own ('db','retired') row unless a
            # ref_event for this ref already exists in this tx, so
            # appending first keeps exactly one row — ours, with reason.
            self.store.append_event(
                ref.id,
                source="agent",
                event="deleted",
                payload={"reason": reason},
                conn=conn,
            )
            self.store.retire_ref(ref.id, conn=conn)
        return Response(
            body=(
                f"retired {handle} (soft-deleted; ref_event 'deleted' "
                f"logged). Undo: edit(kind='{kind}', id='{handle}', "
                "args={'retire': False, 'reason': '<why>'})"
            )
        )

    def _unretire_stub(self, ref: Ref, *, reason: str, dry: bool) -> Response:
        """``args={'retire': False}`` — undo of :meth:`_retire_stub`.

        Restores a plain tombstone via :meth:`Store.restore_ref` (clears
        ``retired_at``; body rows were never removed) and logs a
        ``restored`` ref_event. Refuses a live ref (nothing to undo), a
        merge tombstone — restoring one would fork the supersede
        redirect, which is ``redirect_to``'s job — and a missing
        ``reason``.
        """
        kind = self.spec.kind
        handle = _pa(ref)
        if not reason:
            raise BadInput(
                "args['retire']=False needs args['reason'] — the "
                "restored ref_event carries it",
                next=f"edit(kind='{kind}', id='{handle}', args="
                "{'retire': False, 'reason': '<why>'})",
            )
        if ref.retired_at is None:
            raise BadInput(
                f"{handle} is live — retire=False only restores a retired ref",
                next=f"retire it first: edit(kind='{kind}', id='{handle}', "
                "args={'retire': True, 'reason': '<why>'})",
            )
        if (ref.meta or {}).get("superseded_by") is not None:
            raise BadInput(
                f"{handle} is a merge tombstone — restoring it would "
                "fork the supersede redirect",
                next=f"re-point the redirect instead: edit(kind='{kind}', "
                f"id='{handle}', args={{'redirect_to': '<pa<M>>', "
                "'reason': '<why>'}})",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would restore {handle}: "
                    "retired_at cleared and a 'restored' ref_event logged."
                )
            )
        with self.store.tx() as conn:
            # Same ordering as retire: the event precedes the UPDATE so
            # the 0229 trail trigger suppresses its own 'restored' row.
            self.store.append_event(
                ref.id,
                source="agent",
                event="restored",
                payload={"reason": reason},
                conn=conn,
            )
            self.store.restore_ref(ref.id, conn=conn)
        return Response(
            body=(
                f"restored {handle} (retired_at cleared; ref_event 'restored' logged)."
            )
        )

    # -- body ops (gr514995): detach / re-home / re-parent / clear abstract --

    def _require_live_body_ref(self, id: str | int, door: str) -> tuple[Ref, str]:
        """Resolve a body-op's source ref; refuse a tombstone/retired ref.

        All four doors act on a *live* ref — a merged tombstone's body is
        the survivor's to manage, and a plain-retired one restores first.
        Returns ``(ref, handle)``."""
        kind = self.spec.kind
        ref = self._resolve_ref_incl_retired(id)
        if ref is None:
            raise NotFound(
                f"{kind} id={id!r} resolves to no ref (live or retired)",
                next=f"search(kind='{kind}', q='...') to find the ref",
            )
        handle = _pa(ref)
        if ref.retired_at is not None:
            if (ref.meta or {}).get("superseded_by") is not None:
                raise BadInput(
                    f"{handle} is a merge tombstone — its leftover body is "
                    "the survivor's to manage",
                    next=f"get(kind='{kind}', id='{handle}') to see which "
                    "survivor it redirects to",
                )
            raise BadInput(
                f"{handle} is retired — {door} acts on a live ref",
                next=f"restore it first: edit(kind='{kind}', id='{handle}', "
                "args={'retire': False, 'reason': '<why>'})",
            )
        return ref, handle

    @staticmethod
    def _require_reason(
        reason: str, door: str, kind: str, handle: str, arg: str = "True"
    ) -> None:
        """Every body door carries the caller's reason into its ref_event."""
        if not reason:
            raise BadInput(
                f"args['{door}'] needs args['reason'] — the ref_event carries it",
                next=f"edit(kind='{kind}', id='{handle}', args="
                f"{{'{door}': {arg}, 'reason': '<why>'}})",
            )

    def _drop_body(
        self,
        id: str | int,
        *,
        reason: str,
        dry: bool,
        keep_pdf: bool = False,
        pdf_sha256: Any = None,
    ) -> Response:
        """``edit(args={'drop_body': True})`` — detach the body, keep the
        record (gr514995).

        The wrong-PDF repair's class-b door: DELETEs the ref's ``ord >= 0``
        chunks (the cascade clears embeddings/summaries/citations/blobs),
        NULLs ``refs.pdf_sha256``/``pdf_pages``, removes the content-derived
        identifiers (``pdf_sha256``/``content_hash``/``paper_id``/``pub_id``)
        and strips the extraction meta keys — title, year, authors,
        bibliographic identifiers, cite_key and card chunks stay, so the
        ref is a stub the fetcher can re-chase. No retire, no redirect.

        ``args['keep_pdf']: True`` keeps the attachment instead
        (gr513878): the pointer and its ``pdf_sha256`` identifier row
        survive, leaving a bodiless ref the ``paper_reconcile`` pass /
        ``precis bodiless-heal`` re-extracts from the *stored* file — the
        repair for a ref whose body came from the wrong PDF while the
        right one is already attached. ``args['pdf_sha256']: '<sha>'``
        names the file to keep when the ref is attached to several
        (required then; a named sha that is attached but not the pointer
        re-points ``refs.pdf_sha256`` at it).
        """
        from precis.ingest import body_ops, paper_dump

        kind = self.spec.kind
        ref, handle = self._require_live_body_ref(id, "drop_body")
        self._require_reason(reason, "drop_body", kind, handle)
        named = None if pdf_sha256 is None else str(pdf_sha256).strip() or None
        kept_sha: str | None = None
        switched = False
        with self.store.pool.connection() as conn:
            probe = body_ops.probe_body(conn, ref.id)
            if keep_pdf:
                kept_sha, switched = body_ops.resolve_kept_pdf(
                    conn, ref.id, named=named
                )
            undo = paper_dump.undo_line(conn, ref.id, handle)
        _refuse_oversized_body(probe, "drop_body", handle)
        # A stub carrying stale content ids / body meta is still a drop
        # target — detach_body cleans exactly those (gr515106 LOW).
        if probe.is_stub and not probe.identifiers and not probe.meta_keys:
            return Response(
                body=f"no change: {handle} already holds no body and no PDF."
            )
        if dry:
            dropped_ids = _dropped_content_ids(probe.identifiers, kept_sha)
            pdf_note = "pdf_sha256 cleared, " if probe.pdf else ""
            if kept_sha is not None:
                pdf_note = (
                    f"pdf re-pointed to {kept_sha} (pdf_pages cleared — the "
                    "span was the dropped file's), "
                    if switched
                    else f"pdf {kept_sha} kept (pointer, pages and its "
                    "pdf_sha256 identifier row retained), "
                )
            return Response(
                body=(
                    f"DRY RUN (no write) - would drop {handle}'s body: "
                    f"{probe.chunks} chunk(s) + {probe.embeddings} embedding "
                    f"row(s) deleted"
                    f"{_retired_note(probe)}, "
                    f"{probe.chunk_links_total} chunk link(s) removed, "
                    f"{pdf_note}content ids "
                    f"removed ({', '.join(dropped_ids) or 'none'}), "
                    f"meta keys stripped ({', '.join(probe.meta_keys) or 'none'}); "
                    "a 'body_dropped' ref_event logged."
                    + (
                        " The kept pdf leaves the ref a bodiless-heal "
                        "candidate — the paper_reconcile pass / "
                        "'precis bodiless-heal' re-extracts the stored file."
                        if kept_sha is not None
                        else ""
                    )
                    + f"\n\n{undo}"
                )
            )
        with _body_op_tx(self.store, "drop_body") as conn:
            # Same TOCTOU pin as _move_body_to/_split_body: the probe ran
            # outside the tx — lock the ref row (dedup's FOR NO KEY UPDATE
            # pattern) and re-verify it is still live, else a concurrent
            # merge_into could tombstone it between check and delete.
            row = conn.execute(
                "SELECT retired_at, meta->>'superseded_by' "
                "FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
                (ref.id,),
            ).fetchone()
            if row is None or row[0] is not None or row[1] is not None:
                raise BadInput(
                    f"{handle} was retired or merged since the check — "
                    "a body drop needs a live ref",
                    next=f"re-resolve and retry: edit(kind='{kind}', "
                    f"id='{handle}', args={{'drop_body': True, "
                    f"'reason': '{reason}'}})",
                )
            report = body_ops.detach_body(
                ref_id=ref.id,
                conn=conn,
                keep_pdf=keep_pdf,
                pdf_sha256=named,
            )
            self.store.append_event(
                ref.id,
                source="agent",
                event="body_dropped",
                payload={
                    "reason": reason,
                    "body_chunks": report.chunks,
                    "retired_chunks": report.retired_chunks,
                    "embeddings": report.embeddings,
                    "chunk_links_removed": report.chunk_links_removed,
                    "pdf_cleared": report.pdf and report.kept_pdf is None,
                    "pdf_kept": report.kept_pdf,
                    "pdf_repointed": report.pdf_repointed,
                    "identifiers_removed": _dropped_content_ids(
                        report.identifiers, report.kept_pdf
                    ),
                },
                conn=conn,
            )
        if report.kept_pdf is not None:
            pdf_note = (
                f"pdf re-pointed to {report.kept_pdf} (pdf_pages cleared — "
                "the span was the dropped file's)"
                if report.pdf_repointed
                else f"pdf {report.kept_pdf} kept"
            )
            return Response(
                body=(
                    f"dropped {handle}'s body: {report.chunks} chunk(s) + "
                    f"{report.embeddings} embedding row(s) deleted"
                    f"{_retired_note(report)}, "
                    f"{report.chunk_links_removed} chunk link(s) removed; "
                    f"{pdf_note} — the ref is a bodiless "
                    "stub whose stored file the paper_reconcile pass / "
                    "'precis bodiless-heal' re-extracts "
                    "(ref_event 'body_dropped' logged)."
                )
            )
        return Response(
            body=(
                f"dropped {handle}'s body: {report.chunks} chunk(s) + "
                f"{report.embeddings} embedding row(s) deleted"
                f"{_retired_note(report)}, "
                f"{report.chunk_links_removed} chunk link(s) removed"
                f"{', pdf_sha256 cleared' if report.pdf else ''}; "
                "bibliographic metadata kept — the ref is a stub again "
                "(ref_event 'body_dropped' logged). Refetch with "
                f"put(kind='{kind}', id='{handle}', args={{'pdf_url': ...}})."
            )
        )

    def _reextract(
        self,
        id: str | int,
        *,
        reason: str,
        dry: bool,
        pdf_sha256: Any = None,
    ) -> Response:
        """``edit(args={'reextract': True})`` — re-extract the body from
        the stored pdf on a worker (gr517423 slice 4).

        The repair for a ref whose *extraction* went wrong while the
        attached file is right. The call only validates and enqueues —
        per the Opus ruling it never drops the body: the ``paper_reextract``
        job (``job_inproc`` — Marker minutes per file is far too slow for
        the call) resolves the file, runs a ``pdf_identity`` gate against
        the ref's title/DOI, extracts, then swaps old body for new in one
        locked transaction, re-pointing ``refs.pdf_sha256`` when the named
        sha differs. A failed extraction leaves the ref exactly as it was.
        ``args['pdf_sha256']: '<sha>'`` names the file to re-extract when
        the ref is attached to several — the same naming rule ``keep_pdf``
        uses. The job's outcome lands on ``meta.reextract`` (a
        non-extracted verdict also logs ``reextract_failed``); a file
        held only on another node parks the job for a retry, and a file
        no node holds refuses here.
        """
        from precis.ingest import body_ops, paper_dump
        from precis.workers.job_types import paper_reextract

        kind = self.spec.kind
        ref, handle = self._require_live_body_ref(id, "reextract")
        self._require_reason(reason, "reextract", kind, handle)
        named = None if pdf_sha256 is None else str(pdf_sha256).strip() or None
        with self.store.pool.connection() as conn:
            probe = body_ops.probe_body(conn, ref.id)
            # A re-extract has nothing to read without an attached file —
            # refuse before resolve_kept_pdf, whose next= points at the
            # plain drop rather than the file the caller actually needs.
            if not probe.pdf and not any(
                i.startswith("pdf_sha256:") for i in probe.identifiers
            ):
                raise BadInput(
                    f"{handle} holds no pdf — there is no stored file to "
                    "re-extract from",
                    next=f"attach one first: put(kind='{kind}', "
                    f"id='{handle}', "
                    "args={'pdf_url': 'https://…/x.pdf'}), then re-run "
                    "the reextract",
                )
            kept_sha, switched = body_ops.resolve_kept_pdf(conn, ref.id, named=named)
            ptr_row = conn.execute(
                "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref.id,)
            ).fetchone()
            pointer = str(ptr_row[0]).strip() if ptr_row and ptr_row[0] else None
            undo = paper_dump.undo_line(conn, ref.id, handle)
        # Cheap presence refusal: refuse only a sha the ledger has
        # *swept and found absent everywhere* — a never-swept sha (the
        # reconcile pass hasn't judged it yet) is unknown, not missing,
        # and goes through to the job, which defers or fails correctly
        # (gr517423 r3 finding D).
        if self.store.pdf_missing(kept_sha):
            raise BadInput(
                f"{handle}'s pdf {kept_sha[:16]}… is held on no node — "
                "there is no file to re-extract from",
                next=f"attach a copy first: put(kind='{kind}', "
                f"id='{handle}', args={{'pdf_url': 'https://…/x.pdf'}}), "
                "or restore the file into the corpus, then re-run",
            )
        switch_note = (
            f" and re-point pdf_sha256 from {pointer} to {kept_sha}" if switched else ""
        )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would queue paper_reextract "
                    f"(job_inproc, idem_key "
                    f"'{paper_reextract.idem_key(ref.id, kept_sha)}') to "
                    f"re-extract {handle} from stored pdf {kept_sha}"
                    f"{switch_note}. The job probes the file, checks its "
                    "leading pages against this paper's title/DOI, "
                    "extracts, then swaps the current body "
                    f"({probe.chunks} chunk(s), {probe.embeddings} "
                    "embedding row(s), "
                    f"{probe.chunk_links_total} chunk link(s) removed) "
                    "for the new one in one "
                    "transaction — the old body survives any failure. "
                    f"\n\n{undo}"
                )
            )
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        resp = hub.sibling("job").put(
            job_type=paper_reextract.JOB_TYPE,
            executor="job_inproc",
            parent_id=ref.id,
            params={
                "ref_id": ref.id,
                "pdf_sha256": kept_sha,
                "reason": reason,
            },
            idem_key=paper_reextract.idem_key(ref.id, kept_sha),
        )
        state = "already queued" if getattr(resp, "reused", False) else "queued"
        return Response(
            body=(
                f"{state}: re-extract {handle} from stored pdf {kept_sha}"
                f"{switch_note} — the body swap happens inside job id="
                f"{resp.ref_id} (the current body stays until the new "
                "one lands). Watch: "
                f"get(kind='job', id={resp.ref_id}); the outcome lands on "
                "the paper's meta.reextract."
            )
        )

    def _move_body_to(
        self, id: str | int, target: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'move_body_to': <live paper>})`` — hand this ref's
        body to a bodiless live ref (gr514995).

        The b+move repair door: the body's *content* (chunks, embeddings,
        summaries, citations, blobs, pdf, content ids, extraction meta
        keys) lands on the target; the donor keeps its metadata,
        identifiers and cite_key and ends a live stub — no retire, no
        identifier migration, no supersede redirect (that is
        ``merge_into``'s job for a same-paper pair). The target must hold
        zero ``ord >= 0`` chunks and no pdf of its own — two files can't
        both be the body, and pairing a pdf-less donor's body text with a
        file it wasn't extracted from is unverifiable (the target
        re-extracts from its own pdf via ``precis bodiless-heal``).
        """
        from precis.ingest import body_ops, paper_dump

        kind = self.spec.kind
        donor, donor_h = self._require_live_body_ref(id, "move_body_to")
        self._require_reason(reason, "move_body_to", kind, donor_h, arg="'pa<M>'")
        tgt_raw = "" if target is None else str(target).strip()
        tgt = self._resolve_ref_incl_retired(tgt_raw) if tgt_raw else None
        if tgt is None:
            raise BadInput(
                f"move_body_to={tgt_raw!r} resolves to no {kind} ref",
                next=f"search(kind='{kind}', q='...') for the live "
                "target's pa<id> or slug",
            )
        tgt_h = _pa(tgt)
        if tgt.id == donor.id:
            raise BadInput(
                "move_body_to names the record itself",
                next="pass the other live ref the body belongs to",
            )
        if tgt.retired_at is not None:
            chain_end = self.store.follow_supersede(tgt.id)
            if chain_end is not None:
                end_h = handle_registry.format_handle(tgt.kind, chain_end)
                raise BadInput(
                    f"{tgt_h} is itself merged into {end_h} — a move must "
                    "land on a live ref",
                    next=f"edit(kind='{kind}', id='{donor_h}', args="
                    f"{{'move_body_to': '{end_h}', 'reason': '{reason}'}})",
                )
            raise BadInput(
                f"{tgt_h} is deleted (retired, no survivor) — a move must "
                "land on a live ref",
                next=f"search(kind='{kind}', q='...') for a live target",
            )
        with self.store.pool.connection() as conn:
            src_probe = body_ops.probe_body(conn, donor.id)
            tgt_probe = body_ops.probe_body(conn, tgt.id)
            undo = paper_dump.undo_line(conn, donor.id, donor_h)
        _refuse_oversized_body(src_probe, "move_body_to", donor_h)
        if src_probe.is_stub:
            return Response(
                body=f"no change: {donor_h} holds no body and no PDF to move."
            )
        if tgt_probe.chunks:
            raise BadInput(
                f"{tgt_h} already holds {tgt_probe.chunks} body chunk(s) — "
                "a move lands only on an empty ref",
                next=f"if the pair is the same paper, merge instead: "
                f"edit(kind='{kind}', id='{donor_h}', args="
                f"{{'merge_into': '{tgt_h}', 'reason': '<why>'}}); if {tgt_h}'s "
                f"body is the wrong one, drop it first: edit(kind='{kind}', "
                f"id='{tgt_h}', args={{'drop_body': True, 'reason': '<why>'}})",
            )
        if src_probe.pdf and tgt_probe.pdf:
            raise BadInput(
                f"{tgt_h} already holds a pdf — two files can't both be the body",
                next="decide which file is right first "
                "(drop_body on the wrong side), then retry",
            )
        if tgt_probe.pdf:
            raise BadInput(
                f"{tgt_h} holds a pdf and {donor_h} holds none — the move "
                "would weld body text extracted from a different file "
                f"onto {tgt_h}'s pdf",
                next=f"re-extract {tgt_h}'s body from its own pdf "
                "instead ('precis bodiless-heal' judges and extracts a "
                "pdf-holding bodiless ref); if this body truly is "
                f"{tgt_h}'s, drop the pdf first: edit(kind='{kind}', "
                f"id='{tgt_h}', args={{'drop_body': True, "
                "'reason': '<why>'}})",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would move {donor_h}'s body to "
                    f"{tgt_h}: {src_probe.chunks} chunk(s) + "
                    f"{src_probe.embeddings} embedding row(s) re-inserted "
                    f"under {tgt_h} (summaries/citations/blobs follow), "
                    f"{src_probe.chunk_links} chunk link(s) re-pointed"
                    f"{_links_removed_note(src_probe)}, "
                    f"{'pdf moved, ' if src_probe.pdf else ''}content ids "
                    f"({', '.join(src_probe.identifiers) or 'none'}) and "
                    "extraction meta keys handed over; "
                    f"{donor_h} keeps its metadata and ends a live stub; "
                    "'body_moved' ref_events logged on both.\n\n"
                    f"{undo}"
                )
            )
        with _body_op_tx(self.store, "move_body_to") as conn:
            # Re-check inside the tx (TOCTOU): the probe ran outside it.
            # FOR NO KEY UPDATE pins both ref rows (dedup's pattern) so a
            # concurrent retire/merge/pdf-write can't slip past; then the
            # same conditions the pre-checks proved are re-verified.
            # ORDER BY ref_id takes the two locks in one fixed order, so
            # two opposing moves can't deadlock — one would still surface
            # as Upstream via _body_op_tx.
            locked = {
                int(r[0]): r
                for r in conn.execute(
                    "SELECT ref_id, pdf_sha256, retired_at, "
                    "       meta->>'superseded_by' "
                    "FROM refs WHERE ref_id = ANY(%s) "
                    "ORDER BY ref_id FOR NO KEY UPDATE",
                    ([donor.id, tgt.id],),
                )
            }
            for ref_id, handle in ((donor.id, donor_h), (tgt.id, tgt_h)):
                row = locked.get(ref_id)
                if row is None or row[2] is not None or row[3] is not None:
                    raise BadInput(
                        f"{handle} was retired or merged since the check — "
                        "a body move needs both refs live",
                        next=f"re-resolve and retry: edit(kind='{kind}', "
                        f"id='{donor_h}', args={{'move_body_to': '<live "
                        f"target>', 'reason': '{reason}'}})",
                    )
            if conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 LIMIT 1",
                (tgt.id,),
            ).fetchone():
                raise BadInput(
                    f"{tgt_h} gained a body since the check — a move "
                    "lands only on an empty ref",
                    next=f"retry, or drop it first: edit(kind='{kind}', "
                    f"id='{tgt_h}', args={{'drop_body': True, "
                    "'reason': '<why>'}})",
                )
            if locked[tgt.id][1] is not None and locked[donor.id][1] is not None:
                raise BadInput(
                    f"{tgt_h} gained a pdf since the check — two files "
                    "can't both be the body",
                    next="decide which file is right first "
                    "(drop_body on the wrong side), then retry",
                )
            if locked[tgt.id][1] is not None:
                raise BadInput(
                    f"{tgt_h} gained a pdf since the check and {donor_h} "
                    "has none — the move would weld body text extracted "
                    "from a different file onto it",
                    next=f"re-extract {tgt_h} from its own pdf "
                    "('precis bodiless-heal'), or drop the pdf first: "
                    f"edit(kind='{kind}', id='{tgt_h}', args="
                    "{'drop_body': True, 'reason': '<why>'})",
                )
            report = body_ops.move_body(
                src_ref_id=donor.id, dst_ref_id=tgt.id, conn=conn
            )
            cards = body_ops.ensure_paper_cards(conn, tgt.id)
            payload = {
                "reason": reason,
                "body_chunks": report.chunks,
                "retired_chunks": report.retired_chunks,
                "embeddings": report.embeddings,
                "chunk_links_repointed": report.chunk_links,
                "chunk_links_removed": report.chunk_links_removed,
                "pdf_moved": report.pdf,
                "identifiers_moved": report.identifiers,
                "cards_written": cards,
            }
            self.store.append_event(
                donor.id,
                source="agent",
                event="body_moved",
                payload={**payload, "moved_to": tgt.id},
                conn=conn,
            )
            self.store.append_event(
                tgt.id,
                source="agent",
                event="body_moved",
                payload={**payload, "moved_from": donor.id},
                conn=conn,
            )
        return Response(
            body=(
                f"moved {donor_h}'s body to {tgt_h}: {report.chunks} "
                f"chunk(s) + {report.embeddings} embedding row(s) "
                f"re-inserted{_retired_note(report)}, "
                f"{report.chunk_links} chunk link(s) re-pointed"
                f"{_links_removed_note(report)}"
                f"{', pdf moved' if report.pdf else ''}; "
                f"{donor_h} kept its metadata and is now a live stub; "
                "ref_events 'body_moved' logged on both."
            )
        )

    def _pdf_target_ready(self, tgt_probe: Any, tgt_h: str, kind: str) -> None:
        """Refuse a ``move_pdf_to``/``attach_pdf`` target that isn't a
        bodiless, pdf-less live ref (gr517423 slice 5): a body present
        points at ``drop_body``, a pdf already held at ``reextract``."""
        if tgt_probe.chunks:
            raise BadInput(
                f"{tgt_h} already holds {tgt_probe.chunks} body chunk(s) — "
                "a pdf move lands only on a bodiless ref",
                next=f"drop the body first: edit(kind='{kind}', "
                f"id='{tgt_h}', args={{'drop_body': True, 'reason': '<why>'}})",
            )
        if tgt_probe.pdf:
            raise BadInput(
                f"{tgt_h} already holds a pdf — one ref, one file",
                next=f"re-extract it from that file instead: "
                f"edit(kind='{kind}', id='{tgt_h}', args="
                "{'reextract': True, 'reason': '<why>'}), or drop the "
                f"pdf first: edit(kind='{kind}', id='{tgt_h}', args="
                "{'drop_body': True, 'reason': '<why>'})",
            )

    def _rekey(
        self, id: str | int, new_key: Any, *, reason: str, dry: bool
    ) -> Response:
        """Use the same cite-key and PDF rename operation as the web editor.

        The corpus-mount probes and DB lock waits inside run under the
        same wall-clock bound the web route applies
        (:data:`~precis.store.paper_rekey.REKEY_DEADLINE_S`) — here via a
        bounded worker thread rather than ``asyncio.wait_for``; an
        expired bound abandons the thread, which may still complete the
        rename in the background.
        """
        from precis.store.paper_rekey import REKEY_DEADLINE_S, rekey_paper
        from precis.utils.deadline import DeadlineExceeded, call_with_deadline

        ref_id = self._resolve_paper_ref_id(id)
        try:
            body = call_with_deadline(
                lambda: rekey_paper(
                    self.store,
                    ref_id,
                    new_key,
                    reason=reason,
                    dry_run=dry,
                    kinds=(self.spec.kind,),
                ),
                deadline_s=REKEY_DEADLINE_S,
                label="rekey_paper",
            )
        except DeadlineExceeded as exc:
            raise BadInput(
                f"cite_key rekey of {self.spec.kind} id={ref_id} did not "
                f"finish within {REKEY_DEADLINE_S:.0f}s — the corpus mount "
                "or a DB lock is not answering (the worker thread may "
                "still complete the rename in the background)",
                next=(
                    f"get(kind='{self.spec.kind}', id='{ref_id}') to check "
                    "the handle; retry the edit if it is unchanged"
                ),
            ) from exc
        return Response(body=body)

    def _enqueue_pdf_attach(
        self,
        *,
        donor_id: int | None,
        tgt_id: int,
        sha: str,
        reason: str,
    ) -> Any:
        """Queue the ``pdf_attach`` job — the doors' deferred identity
        gate (gr517423 slice 5). Returns the put response."""
        from precis.workers.job_types import pdf_attach

        hub = self.hub if self.hub is not None else Hub(store=self.store)
        return hub.sibling("job").put(
            job_type=pdf_attach.JOB_TYPE,
            executor="job_inproc",
            parent_id=tgt_id,
            params={
                "target_ref_id": tgt_id,
                "donor_ref_id": donor_id,
                "pdf_sha256": sha,
                "reason": reason,
            },
            idem_key=pdf_attach.idem_key(tgt_id, sha),
        )

    def _enqueue_reextract(self, *, tgt_id: int, sha: str, reason: str) -> Any:
        """Queue the slice-4 ``paper_reextract`` after a pdf move —
        the moved file becomes the target's body."""
        from precis.workers.job_types import paper_reextract

        hub = self.hub if self.hub is not None else Hub(store=self.store)
        return hub.sibling("job").put(
            job_type=paper_reextract.JOB_TYPE,
            executor="job_inproc",
            parent_id=tgt_id,
            params={
                "ref_id": tgt_id,
                "pdf_sha256": sha,
                "reason": reason,
            },
            idem_key=paper_reextract.idem_key(tgt_id, sha),
        )

    def _move_pdf_to(
        self,
        id: str | int,
        target: Any,
        *,
        reason: str,
        dry: bool,
        pdf_sha256: Any = None,
    ) -> Response:
        """``edit(args={'move_pdf_to': <target>})`` — re-home a stored
        pdf from a bodiless donor to a bodiless target (gr517423 slice 5).

        The wrong-paper repair's pdf leg: a file parked on the wrong
        record whose body already went (a ``keep_pdf`` drop, a dedup
        fold) moves onto the ref it actually belongs to. A donor still
        holding a body refuses toward ``move_body_to`` — body and pdf
        move together there. The target must be live, bodiless and
        pdf-less. Before any write the file is identity-checked against
        the target's title/DOI — from the donor's stored page-1-3 chunk
        text when it exists, else deferred to a ``pdf_attach`` job that
        reads the file's leading pages on a corpus host. On a
        ``doi``/``title`` verdict the pointer, identifier row and
        ``pdf_moved`` events land in one transaction and a
        ``paper_reextract`` job is queued so the moved file becomes the
        target's body. ``args['pdf_sha256']`` names the file when the
        donor is attached to several.
        """
        from precis.ingest import body_ops, paper_dump, pdf_migrate
        from precis.ingest.paper_hygiene import _identity_title

        kind = self.spec.kind
        donor, donor_h = self._require_live_body_ref(id, "move_pdf_to")
        self._require_reason(reason, "move_pdf_to", kind, donor_h, arg="'pa<M>'")
        tgt_raw = "" if target is None else str(target).strip()
        tgt = self._resolve_ref_incl_retired(tgt_raw) if tgt_raw else None
        if tgt is None:
            raise BadInput(
                f"move_pdf_to={tgt_raw!r} resolves to no {kind} ref",
                next=f"search(kind='{kind}', q='...') for the live "
                "target's pa<id> or slug",
            )
        tgt_h = _pa(tgt)
        if tgt.id == donor.id:
            raise BadInput(
                "move_pdf_to names the record itself",
                next="pass the other live ref the pdf belongs to",
            )
        if tgt.retired_at is not None:
            chain_end = self.store.follow_supersede(tgt.id)
            if chain_end is not None:
                end_h = handle_registry.format_handle(tgt.kind, chain_end)
                raise BadInput(
                    f"{tgt_h} is itself merged into {end_h} — a move must "
                    "land on a live ref",
                    next=f"edit(kind='{kind}', id='{donor_h}', args="
                    f"{{'move_pdf_to': '{end_h}', 'reason': '{reason}'}})",
                )
            raise BadInput(
                f"{tgt_h} is deleted (retired, no survivor) — a move must "
                "land on a live ref",
                next=f"search(kind='{kind}', q='...') for a live target",
            )
        named = None if pdf_sha256 is None else str(pdf_sha256).strip() or None
        with self.store.pool.connection() as conn:
            src_probe = body_ops.probe_body(conn, donor.id)
            tgt_probe = body_ops.probe_body(conn, tgt.id)
            undo = paper_dump.undo_line(conn, donor.id, donor_h)
            pointer, id_shas = body_ops._attached_pdf_shas(conn, donor.id)
            attached = sorted({*id_shas, *([pointer] if pointer else [])})
            if src_probe.chunks:
                raise BadInput(
                    f"{donor_h} still holds {src_probe.chunks} body chunk(s) "
                    "— body and pdf move together",
                    next=f"move the whole body instead: edit(kind='{kind}', "
                    f"id='{donor_h}', args={{'move_body_to': '{tgt_h}', "
                    f"'reason': '{reason}'}})",
                )
            if attached:
                self._pdf_target_ready(tgt_probe, tgt_h, kind)
                sha, _switch = body_ops.resolve_kept_pdf(conn, donor.id, named=named)
                texts = pdf_migrate.leading_texts(conn, donor.id)
                _, tgt_doi = pdf_migrate.ref_identity(conn, tgt.id)
        if not attached:
            return Response(body=f"no change: {donor_h} holds no pdf to move.")
        verdict: str | None = None
        detail: str | None = None
        if texts:
            verdict, detail = pdf_migrate.target_gate_verdict(
                texts, title=_identity_title(tgt.title), doi=tgt_doi
            )
            if verdict == "mismatch":
                raise BadInput(
                    f"the stored pages do not look like {tgt_h}: {detail or '-'}",
                    next="judge the refs again — if the file really is "
                    f"{tgt_h}'s, its title/doi needs repair first: "
                    f"edit(kind='{kind}', id='{tgt_h}', title=…|doi=…)",
                )
            if verdict == "unchecked":
                raise BadInput(
                    f"{tgt_h} has neither title words nor a DOI to check "
                    f"{donor_h}'s pdf against",
                    next=f"repair the target's metadata first: "
                    f"edit(kind='{kind}', id='{tgt_h}', "
                    "title=…|doi=…), then re-run the move",
                )
        if dry:
            gate_note = (
                f"identity verdict {verdict} (from {donor_h}'s stored page-1-3 chunks)"
                if verdict is not None
                else "identity check DEFERRED to a pdf_attach job — no "
                "stored chunks on pages 1-3 to judge against here"
            )
            return Response(
                body=(
                    f"DRY RUN (no write) - would move pdf {sha} from "
                    f"{donor_h} to {tgt_h}: {gate_note}; the move "
                    f"clears {donor_h}'s pdf_sha256/pdf_pages/pdf_role, "
                    "re-homes the pdf_sha256 identifier row, sets "
                    f"{tgt_h}'s pointer, journals 'pdf_moved' on both "
                    "refs and queues paper_reextract for the target."
                    f"\n\n{undo}"
                )
            )
        if verdict is None:
            resp = self._enqueue_pdf_attach(
                donor_id=donor.id, tgt_id=tgt.id, sha=sha, reason=reason
            )
            state = "already queued" if getattr(resp, "reused", False) else "queued"
            return Response(
                body=(
                    f"{state}: pdf_attach job id={resp.ref_id} to "
                    f"identity-check and move pdf {sha} from {donor_h} "
                    f"to {tgt_h} — the file's leading pages are read on "
                    "a worker (no stored chunks on pages 1-3 here). "
                    f"Watch: get(kind='job', id={resp.ref_id}); the "
                    "outcome lands on the target's meta.pdf_attach."
                )
            )
        with _body_op_tx(self.store, "move_pdf_to") as conn:
            # TOCTOU, same shape as _move_body_to: lock both ref rows in
            # ref_id order, then re-verify everything the probes proved.
            locked = {
                int(r[0]): r
                for r in conn.execute(
                    "SELECT ref_id, pdf_sha256, retired_at, "
                    "       meta->>'superseded_by' "
                    "FROM refs WHERE ref_id = ANY(%s) "
                    "ORDER BY ref_id FOR NO KEY UPDATE",
                    ([donor.id, tgt.id],),
                )
            }
            for ref_id, handle in ((donor.id, donor_h), (tgt.id, tgt_h)):
                row = locked.get(ref_id)
                if row is None or row[2] is not None or row[3] is not None:
                    raise BadInput(
                        f"{handle} was retired or merged since the check — "
                        "a pdf move needs both refs live",
                        next=f"re-resolve and retry: edit(kind='{kind}', "
                        f"id='{donor_h}', args={{'move_pdf_to': '<live "
                        f"target>', 'reason': '{reason}'}})",
                    )
            if conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL LIMIT 1",
                (donor.id,),
            ).fetchone():
                raise BadInput(
                    f"{donor_h} gained a body since the check — body "
                    "and pdf move together",
                    next=f"move the whole body instead: edit(kind='{kind}', "
                    f"id='{donor_h}', args={{'move_body_to': '{tgt_h}', "
                    f"'reason': '{reason}'}})",
                )
            if (
                locked[tgt.id][1] is not None
                or conn.execute(
                    "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
                    "AND retired_at IS NULL LIMIT 1",
                    (tgt.id,),
                ).fetchone()
            ):
                raise BadInput(
                    f"{tgt_h} gained a body or pdf since the check — "
                    "a pdf move lands only on a bodiless, pdf-less ref",
                    next=f"re-extract from its own pdf: edit(kind='{kind}', "
                    f"id='{tgt_h}', args={{'reextract': True, "
                    "'reason': '<why>'}})",
                )
            pdf_migrate.apply_pdf_move(
                conn,
                store=self.store,
                sha=sha,
                target_id=tgt.id,
                donor_id=donor.id,
                verdict=verdict,
                reason=reason,
                source="agent",
            )
        job = self._enqueue_reextract(tgt_id=tgt.id, sha=sha, reason=reason)
        return Response(
            body=(
                f"moved pdf {sha} from {donor_h} to {tgt_h} (verdict "
                f"{verdict}): {donor_h}'s pointer cleared, the "
                "pdf_sha256 identifier row re-homed, 'pdf_moved' "
                f"ref_events on both refs; paper_reextract "
                f"{'reused' if getattr(job, 'reused', False) else 'queued'} "
                f"(job id={job.ref_id}) to extract the target's body "
                f"from the moved file."
            )
        )

    def _attach_pdf(
        self, id: str | int, sha_value: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'attach_pdf': '<sha>'})`` — adopt an orphan
        ``pdfs`` row (gr517423 slice 5).

        ``drop_body`` leaves exactly such orphans: the corpus holds the
        file but no live ref points at it. The target must be live,
        bodiless and pdf-less; a sha a live ref owns refuses naming the
        owner (``move_pdf_to`` is the re-home door). The identity gate
        always defers to the ``pdf_attach`` job — an orphan sha has no
        donor chunks to judge against, and the file's leading pages are
        read on a corpus host. On a ``doi``/``title`` verdict the job
        sets the pointer + identifier row, journals ``pdf_moved`` and
        queues ``paper_reextract``.
        """
        from precis.ingest import body_ops, paper_dump, pdf_migrate

        kind = self.spec.kind
        tgt, tgt_h = self._require_live_body_ref(id, "attach_pdf")
        self._require_reason(reason, "attach_pdf", kind, tgt_h, arg="'<sha>'")
        sha = "" if sha_value is None else str(sha_value).strip().lower()
        if not sha:
            raise BadInput(
                "attach_pdf needs a sha to adopt",
                next=f"edit(kind='{kind}', id='{tgt_h}', args="
                "{'attach_pdf': '<sha>', 'reason': '<why>'})",
            )
        with self.store.pool.connection() as conn:
            tgt_probe = body_ops.probe_body(conn, tgt.id)
            self._pdf_target_ready(tgt_probe, tgt_h, kind)
            owner = pdf_migrate.live_pdf_sha_owner(conn, sha)
            if owner is not None and owner != tgt.id:
                owner_h = handle_registry.format_handle(kind, owner)
                raise BadInput(
                    f"pdf {sha} already belongs to live {owner_h} — "
                    "attach_pdf only adopts orphans",
                    next=f"re-home it instead: edit(kind='{kind}', "
                    f"id='{owner_h}', args={{'move_pdf_to': '{tgt_h}', "
                    f"'reason': '{reason}'}})",
                )
            has_pdfs_row = (
                conn.execute(
                    "SELECT 1 FROM pdfs WHERE pdf_sha256 = %s", (sha,)
                ).fetchone()
                is not None
            )
            undo = paper_dump.undo_line(conn, tgt.id, tgt_h)
        if not has_pdfs_row:
            raise BadInput(
                f"the corpus holds no pdfs row for {sha} — attach_pdf "
                "adopts a stored file, it does not fetch one",
                next=f"attach by URL instead: put(kind='{kind}', "
                f"id='{tgt_h}', args={{'pdf_url': 'https://…/x.pdf'}})",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would queue pdf_attach to "
                    f"adopt pdf {sha} onto {tgt_h}: the job reads the "
                    "file's leading pages, applies only on a doi/title "
                    "verdict (sets the pointer, re-homes the identifier "
                    "row, journals 'pdf_moved') and queues "
                    "paper_reextract for the body.\n\n"
                    f"{undo}"
                )
            )
        resp = self._enqueue_pdf_attach(
            donor_id=None, tgt_id=tgt.id, sha=sha, reason=reason
        )
        state = "already queued" if getattr(resp, "reused", False) else "queued"
        return Response(
            body=(
                f"{state}: pdf_attach job id={resp.ref_id} to "
                f"identity-check and attach pdf {sha} onto {tgt_h} — "
                "the file's leading pages are read on a worker. "
                f"Watch: get(kind='job', id={resp.ref_id}); the "
                "outcome lands on the ref's meta.pdf_attach."
            )
        )

    def _split_body(
        self, id: str | int, spec: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'split_body': {…}})`` — mint a new paper ref that
        takes this ref's body (gr514995).

        The class-c repair door: the body is a different work than the
        record's metadata describes, so it gets its own ref — caller
        metadata (``title`` required; ``year``/``authors``/``journal``/
        ``doi`` optional) mints it, then the same move primitive hands
        the body over. The donor keeps its metadata and ends a live
        stub. A ``doi`` already held by a live ref refuses naming that
        ref — that is the ``move_body_to`` case, not a mint.
        """
        from precis.identity import make_cite_key
        from precis.ingest import body_ops, paper_dump

        kind = self.spec.kind
        donor, donor_h = self._require_live_body_ref(id, "split_body")
        self._require_reason(reason, "split_body", kind, donor_h, arg="{'title': …}")
        if not isinstance(spec, dict):
            raise BadInput(
                "args['split_body'] takes a metadata dict — "
                "{'title': …, 'year': …, 'authors': […], 'journal': …, 'doi': …}",
                next=f"edit(kind='{kind}', id='{donor_h}', args="
                "{'split_body': {'title': '<the body\\'s real title>'}, "
                "'reason': '<why>'})",
            )
        new_title = spec.get("title")
        if not isinstance(new_title, str) or not new_title.strip():
            raise BadInput(
                "split_body needs a 'title' — the minted ref's title",
                next=f"edit(kind='{kind}', id='{donor_h}', args="
                "{'split_body': {'title': '<the body\\'s real title>'}, "
                "'reason': '<why>'})",
            )
        new_title = new_title.strip()
        new_year = spec.get("year")
        if new_year is not None and not isinstance(new_year, int):
            raise BadInput(
                f"split_body 'year' must be an int, got {new_year!r}",
                next="pass the body's real publication year, or omit it",
            )
        new_authors = (
            normalize_authors(spec["authors"]) if spec.get("authors") else None
        )
        new_journal = spec.get("journal")
        if new_journal is not None and not str(new_journal).strip():
            new_journal = None
        new_doi = spec.get("doi")
        if new_doi is not None and not str(new_doi).strip():
            new_doi = None
        if new_doi:
            owner = self.store.find_ref_by_identifier("doi", str(new_doi), kind="paper")
            if owner is not None:
                owner_h = handle_registry.format_handle("paper", owner)
                raise BadInput(
                    f"split_body doi={str(new_doi).strip()!r} already belongs "
                    f"to {owner_h} — that is the move case, not a mint",
                    next=f"hand the body to that ref instead: "
                    f"edit(kind='{kind}', id='{donor_h}', args="
                    f"{{'move_body_to': '{owner_h}', 'reason': '{reason}'}})",
                )
        with self.store.pool.connection() as conn:
            probe = body_ops.probe_body(conn, donor.id)
            undo = paper_dump.undo_line(conn, donor.id, donor_h)
        _refuse_oversized_body(probe, "split_body", donor_h)
        if probe.is_stub:
            raise BadInput(
                f"{donor_h} holds no body and no PDF — nothing to split off",
                next=f"to mint the other work as a stub instead: "
                f"put(kind='{kind}', title=..., doi=...)",
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would mint a new {kind} ref "
                    f"{new_title!r} (year={new_year}, "
                    f"authors={len(new_authors or [])}, "
                    f"journal={new_journal!r}, doi={new_doi!r}) taking "
                    f"{donor_h}'s body: {probe.chunks} chunk(s) + "
                    f"{probe.embeddings} embedding row(s)"
                    f"{', pdf moved' if probe.pdf else ''}, "
                    f"{probe.chunk_links} chunk link(s) re-pointed"
                    f"{_links_removed_note(probe)}; "
                    f"{donor_h} keeps its metadata and ends a live stub; "
                    "'body_split' / 'body_moved' ref_events logged.\n\n"
                    f"{undo}"
                )
            )
        with _body_op_tx(self.store, "split_body") as conn:
            # The probe ran outside the tx — pin the donor row (dedup's
            # FOR NO KEY UPDATE pattern) and re-verify it is still live
            # and still carries the body before minting off it.
            row = conn.execute(
                "SELECT pdf_sha256, retired_at, meta->>'superseded_by' "
                "FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
                (donor.id,),
            ).fetchone()
            if row is None or row[1] is not None or row[2] is not None:
                raise BadInput(
                    f"{donor_h} was retired or merged since the check — "
                    "a split needs a live donor",
                    next=f"re-resolve and retry: edit(kind='{kind}', "
                    f"id='<live ref>', args={{'split_body': {{'title': "
                    f"'{new_title}'}}, 'reason': '{reason}'}})",
                )
            if (
                not conn.execute(
                    "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 LIMIT 1",
                    (donor.id,),
                ).fetchone()
                and row[0] is None
            ):
                raise BadInput(
                    f"{donor_h} lost its body since the check — nothing to split off",
                    next="re-resolve the donor and retry",
                )
            slug = self.store.suggest_cite_key(new_authors or [], new_year, conn=conn)
            if not slug:
                first = new_title.split()
                slug = make_cite_key(
                    [{"family": first[0] if first else "anon"}], new_year
                )
            meta: dict[str, Any] = {"set_by": "edit"}
            if new_journal:
                meta["journal"] = str(new_journal).strip()
            new_ref = self.store.insert_ref(
                kind="paper",
                slug=slug,
                title=new_title,
                authors=new_authors,
                year=new_year,
                provider="manual",
                meta=meta,
                authors_source="human",
                conn=conn,
            )
            if new_doi:
                self.store.set_ref_identifier(
                    new_ref.id, "doi", str(new_doi), source="edit", conn=conn
                )
            report = body_ops.move_body(
                src_ref_id=donor.id, dst_ref_id=new_ref.id, conn=conn
            )
            cards = body_ops.ensure_paper_cards(conn, new_ref.id)
            payload = {
                "reason": reason,
                "body_chunks": report.chunks,
                "retired_chunks": report.retired_chunks,
                "embeddings": report.embeddings,
                "chunk_links_repointed": report.chunk_links,
                "chunk_links_removed": report.chunk_links_removed,
                "pdf_moved": report.pdf,
                "identifiers_moved": report.identifiers,
                "cards_written": cards,
            }
            self.store.append_event(
                donor.id,
                source="agent",
                event="body_split",
                payload={**payload, "new_ref_id": new_ref.id},
                conn=conn,
            )
            self.store.append_event(
                new_ref.id,
                source="agent",
                event="body_moved",
                payload={**payload, "moved_from": donor.id, "via": "body_split"},
                conn=conn,
            )
        new_h = _pa(new_ref)
        return Response(
            body=(
                f"split {donor_h}: minted {new_h} {new_title!r} and moved "
                f"the body over ({report.chunks} chunk(s) + "
                f"{report.embeddings} embedding row(s)"
                f"{', pdf moved' if report.pdf else ''}, "
                f"{report.chunk_links} chunk link(s) re-pointed"
                f"{_links_removed_note(report)}); "
                f"{donor_h} kept its metadata and is now a live stub "
                "(ref_events 'body_split' / 'body_moved' logged)."
            )
        )

    def _clear_abstract(self, id: str | int, *, reason: str, dry: bool) -> Response:
        """``edit(args={'clear_abstract': True})`` — remove a junk
        abstract (gr514995).

        ``meta.abstract`` is deleted outright and the ``card_abstract``
        chunk (the ``ord < 0`` search card a bad abstract pollutes, e.g.
        extraction junk like ``'Nasl. z nasl. zaslona.'``) goes with it;
        ``card_combined`` is rebuilt by ``rewrite_cards`` without the
        abstract so semantic search stops matching the junk text. A
        later enrichment may re-fill the abstract — the event log is the
        audit of why it was removed.
        """
        from precis.ingest import paper_dump

        kind = self.spec.kind
        ref, handle = self._require_live_body_ref(id, "clear_abstract")
        self._require_reason(reason, "clear_abstract", kind, handle)
        with self.store.pool.connection() as conn:
            meta_abstract = (ref.meta or {}).get("abstract")
            undo = paper_dump.undo_line(conn, ref.id, handle)
            card = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'card_abstract' AND retired_at IS NULL",
                (ref.id,),
            ).fetchone()
        has_meta = isinstance(meta_abstract, str) and bool(meta_abstract.strip())
        n_card = int(card[0]) if card else 0
        if not has_meta and not n_card:
            return Response(
                body=f"no change: {handle} carries no meta.abstract and no "
                "card_abstract chunk."
            )
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would clear {handle}'s abstract: "
                    f"meta.abstract {'removed' if has_meta else 'already absent'}, "
                    f"{n_card} card_abstract chunk(s) deleted, card_combined "
                    "rebuilt without it; an 'abstract_cleared' ref_event "
                    f"logged.\n\n{undo}"
                )
            )
        with _body_op_tx(self.store, "clear_abstract") as conn:
            if has_meta:
                conn.execute(
                    "UPDATE refs SET meta = meta - 'abstract', "
                    "updated_at = now() WHERE ref_id = %s",
                    (ref.id,),
                )
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND "
                "chunk_kind = 'card_abstract' AND retired_at IS NULL",
                (ref.id,),
            )
            fresh = conn.execute(
                "SELECT title, authors, meta FROM refs WHERE ref_id = %s",
                (ref.id,),
            ).fetchone()
            assert fresh is not None
            meta = fresh[2] if isinstance(fresh[2], dict) else {}
            kw = meta.get("keywords", [])
            rewrite_cards(
                conn,
                ref.id,
                title=fresh[0] or "",
                author_names=_shared_author_names(fresh[1] or []),
                abstract="",
                keywords=list(kw) if isinstance(kw, list) else [],
            )
            self.store.append_event(
                ref.id,
                source="agent",
                event="abstract_cleared",
                payload={
                    "reason": reason,
                    "had_meta_abstract": has_meta,
                    "card_abstract_removed": n_card,
                },
                conn=conn,
            )
        return Response(
            body=(
                f"cleared {handle}'s abstract: meta.abstract "
                f"{'removed' if has_meta else 'was absent'}, {n_card} "
                "card_abstract chunk(s) deleted, card_combined rebuilt "
                "(ref_event 'abstract_cleared' logged)."
            )
        )

    def _convert_to(
        self,
        id: str | int,
        target: Any,
        *,
        subtype: Any,
        reason: str,
        dry: bool,
    ) -> Response:
        """``edit(args={'convert_to': 'datasheet'})`` — flip this ref's
        kind, keeping identity (gr515477).

        The datasheet/manual misfiled as a paper: the ``refs.kind``
        column flips and every ``ref_id``-keyed row follows untouched —
        body chunks, embeddings (the ``chunk_embeddings.kind`` denorm
        re-stamps), links incl. chunk anchors, identifiers, the pdf
        pointer, authors and trust stamps. The cite_key slug keeps
        resolving under the new kind; the computed handle re-mints
        (``pa<id>`` → ``da<id>`` — the code encodes the kind, and the old
        handle redirects through the ``converted_from_kind`` stamp).
        ``args['subtype']`` picks the datasheet sub-genre
        (:data:`precis.ingest.kind_convert.DATASHEET_SUBTYPES`,
        default ``'datasheet'``); ``meta.subtype`` + a
        ``converted_from_kind`` stamp are written. Paper-only ref tags
        (categorizer axes + ``DREAM``) are stripped in the same
        transaction (each stripped axis's ``<NS>CASCADE`` sweep marker
        goes with it); the dry run lists them. A kind flip is semantic
        under the 0214 guard: v0 trust stamps carry, a v1 ref is
        restamped ``<caller origin>/hunch`` (the dry run names this).
        One transaction.

        Refuses with ``next=`` when the target can't hold something:
        an SI record, a paper with live SI children, or a link whose
        relation's endpoint-kind constraint excludes the target.
        """
        from precis.ingest import kind_convert
        from precis.ingest.kind_convert import (
            CONVERT_TARGETS,
            DATASHEET_SUBTYPES,
        )

        kind = self.spec.kind
        if kind != "paper":
            raise BadInput(
                f"convert_to is supported only from kind='paper' "
                f"(this door is kind='{kind}')",
                next="only a paper converts to a datasheet — the reverse "
                "or other pairs need a migration decision first",
            )
        target_s = str(target or "").strip()
        if target_s not in CONVERT_TARGETS:
            raise BadInput(
                f"convert_to={target_s!r} unsupported — the only "
                "supported conversion is paper → datasheet",
                next="edit(kind='paper', id=<slug>, args="
                "{'convert_to': 'datasheet', 'reason': '<why>'})",
            )
        ref, handle = self._require_live_body_ref(id, "convert_to")
        self._require_reason(reason, "convert_to", kind, handle, arg="'datasheet'")
        sub = str(subtype or "").strip() or "datasheet"
        if sub not in DATASHEET_SUBTYPES:
            raise BadInput(
                f"args['subtype']={sub!r} unknown; expected one of "
                f"{sorted(DATASHEET_SUBTYPES)}",
                next="args={'convert_to': 'datasheet', 'subtype': "
                "'app-note'|'errata'|'reference-manual', 'reason': '<why>'}",
            )
        with self.store.pool.connection() as conn:
            probe = kind_convert.probe_convert(conn, ref.id, target_s)
        self._convert_refuse(probe, handle, target_s)
        new_handle = handle_registry.format_handle(target_s, ref.id)
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would convert {handle} "
                    f"paper → {target_s}: refs.kind flipped (1 row), "
                    f"meta.subtype='{sub}' + converted_from_kind stamped, "
                    f"{self._convert_kept(probe)}."
                    f"{self._convert_notes(probe)} a 'converted' "
                    f"ref_event logged. New handle {new_handle}; "
                    f"pa{ref.id} redirects to it (converted_from_kind "
                    f"stamp). The cite_key slug is kept as-is."
                )
            )
        with _body_op_tx(self.store, "convert_to") as conn:
            # Same TOCTOU pin as the body ops: probe ran outside the tx —
            # lock the ref FOR UPDATE (the kind column sits in
            # refs_ref_id_kind_key, so the flip upgrades the lock anyway)
            # and re-verify it is still a live paper, then re-probe:
            # an SI child or constrained link added between the outer
            # probe and the lock converts anyway otherwise.
            row = conn.execute(
                "SELECT kind, retired_at, meta->>'superseded_by' "
                "FROM refs WHERE ref_id = %s FOR UPDATE",
                (ref.id,),
            ).fetchone()
            if (
                row is None
                or row[0] != "paper"
                or row[1] is not None
                or row[2] is not None
            ):
                raise BadInput(
                    f"{handle} changed kind or was retired/merged since "
                    "the check — a convert needs a live paper",
                    next=f"re-resolve and retry: edit(kind='paper', "
                    f"id='{handle}', args={{'convert_to': 'datasheet', "
                    "'reason': '<why>'}})",
                )
            probe = kind_convert.probe_convert(conn, ref.id, target_s)
            self._convert_refuse(probe, handle, target_s)
            applied = kind_convert.apply_convert(
                conn, ref_id=ref.id, target=target_s, subtype=sub
            )
            self.store.append_event(
                ref.id,
                source="agent",
                event="converted",
                payload={
                    "reason": reason,
                    "from_kind": "paper",
                    "to_kind": target_s,
                    "subtype": sub,
                    "body_chunks": probe.chunks,
                    "retired_chunks": probe.retired_chunks,
                    "embeddings": applied["embedding_rows"],
                    "links": probe.links,
                    "chunk_links": probe.chunk_links,
                    "identifiers": probe.identifiers,
                    "authors": probe.authors,
                    "pdf_kept": probe.pdf,
                    "stripped_tags": applied["stripped_tags"],
                },
                conn=conn,
            )
        return Response(
            body=(
                f"converted {handle} → kind='{target_s}': "
                f"{self._convert_kept(probe)}.{self._convert_notes(probe)} "
                f"(ref_event 'converted' logged). Address it as "
                f"get(kind='datasheet', id='{ref.slug or new_handle}') "
                f"or {new_handle} — pa{ref.id} keeps resolving through "
                "the converted_from_kind redirect; the cite_key slug "
                "and every other identifier keep working."
            )
        )

    @staticmethod
    def _convert_refuse(probe: Any, handle: str, target: str) -> None:
        """Raise the convert refusals (with ``next=``) for a probe's
        flags — run on the outer probe AND the in-tx re-probe so a state
        change between them can't slip a refusal through."""
        if probe.is_supplement:
            raise BadInput(
                f"{handle} is an SI record — a datasheet has no supplement parent",
                next="convert the parent paper instead, or unlink the "
                "part-of edge first",
            )
        if probe.si_children:
            raise BadInput(
                f"{handle} has {probe.si_children} supplement record(s) "
                "attached — converting the parent strands them",
                next="convert or merge the SI records first "
                "(args={'merge_into': <survivor>} / {'drop_body': True})",
            )
        if probe.violated_links:
            listing = ", ".join(
                f"link:{lid} rel='{rel}' ({side})"
                for lid, rel, side in probe.violated_links
            )
            raise BadInput(
                f"{handle} carries edge(s) a {target} cannot hold: {listing}",
                next="unlink them first — the relation's endpoint-kind "
                f"constraint excludes '{target}'",
            )

    @staticmethod
    def _convert_notes(probe: Any) -> str:
        """The trust-restamp + tag-strip trailer, built from whichever
        probe is authoritative for the reply — the outer probe for the
        dry run, the in-tx re-probe for the write ack."""
        trust = (
            " Trust stamp is v1 — the 0214 guard restamps it "
            "<caller origin>/hunch on write (a 'considered' stamp does "
            "not survive)."
            if probe.trust_write_version == 1
            else ""
        )
        strip = (
            f" Paper-only tags stripped: {', '.join(probe.stripped_tags)}."
            if probe.stripped_tags
            else ""
        )
        return f"{trust}{strip}"

    @staticmethod
    def _convert_kept(probe: Any) -> str:
        """The carried-over counts line shared by the dry run, the ack
        and the ref_event."""
        return (
            f"{probe.chunks} chunk(s)"
            + (f" (+{probe.retired_chunks} retired)" if probe.retired_chunks else "")
            + f", {probe.embedding_rows} embedding row(s), {probe.links} "
            f"link(s) ({probe.chunk_links} chunk-anchored), "
            f"{probe.identifiers} identifier(s), {probe.authors} author "
            f"row(s){', pdf' if probe.pdf else ''} kept"
        )

    def _drop_identifier(
        self, id: str | int, spec: Any, *, reason: str, dry: bool
    ) -> Response:
        """``edit(args={'drop_identifier': {'scheme': …, 'value': …}})`` —
        remove exactly one ``ref_identifiers`` row (gr513878).

        The surgical door ``doi=''``/``arxiv=''`` can't express: those
        clear *every* row of the scheme and stamp the human-cleared
        marker — wrong when a ref carries a bad value next to good ones
        (a merge that left the donor's pubmed/openalex/s2 rows on the
        survivor) and the scheme should stay open for a later correct
        write. One ``(scheme, value)`` row goes; the scheme's other
        rows stay, no cleared marker is stamped, and a displayed
        ``meta.doi``/``meta.arxiv`` ghost of the dropped value follows
        it out (re-pointed at a surviving row of the scheme, else
        removed — :meth:`Store.drop_ref_identifier`).
        """
        kind = self.spec.kind
        ref, handle = self._require_live_body_ref(id, "drop_identifier")
        self._require_reason(
            reason,
            "drop_identifier",
            kind,
            handle,
            arg="{'scheme': '<scheme>', 'value': '<value>'}",
        )
        if isinstance(spec, dict):
            scheme_raw, value_raw = spec.get("scheme"), spec.get("value")
        elif isinstance(spec, str) and ":" in spec:
            scheme_raw, value_raw = spec.split(":", 1)
        else:
            scheme_raw = value_raw = None
        scheme = str(scheme_raw or "").strip().lower()
        value = _normalise_identifier(scheme, str(value_raw or ""))
        if not scheme or not value:
            raise BadInput(
                "drop_identifier needs {'scheme': '<scheme>', 'value': "
                "'<value>'} — one identifier row per call",
                next=f"edit(kind='{kind}', id='{handle}', args="
                "{'drop_identifier': {'scheme': 'pubmed', 'value': "
                "'24970080'}, 'reason': '<why>'})",
            )
        if scheme == "cite_key":
            raise BadInput(
                "drop_identifier cannot remove a cite_key — a cite_key is "
                "the ref's handle, not an external identifier",
                next="to fold this record into another ref use "
                "args={'merge_into': '<survivor>', 'reason': '<why>'}",
            )
        # Content-derived ids are the body doors' to manage; dropping the
        # pointer's own pdf_sha256 row would orphan the attachment.
        if scheme in ("content_hash", "paper_id", "pub_id"):
            raise BadInput(
                f"drop_identifier can't remove {scheme}={value!r} — it is "
                "a content-derived id the body ops manage",
                next=f"edit(kind='{kind}', id='{handle}', args="
                "{'drop_body': True, 'reason': '<why>'}) to drop the "
                "attachment too, 'keep_pdf': True to keep the file",
            )
        pointer = (ref.pdf_sha256 or "").strip() if ref.pdf_sha256 else ""
        if scheme == "pdf_sha256" and value == pointer.lower():
            raise BadInput(
                f"pdf_sha256={value!r} is {handle}'s pdf pointer — "
                "dropping its identifier row orphans the attachment",
                next=f"edit(kind='{kind}', id='{handle}', args="
                "{'drop_body': True, 'reason': '<why>'}) to drop the pdf "
                "too, or 'keep_pdf': True to keep the file",
            )
        rows = self.store.list_ref_identifiers(ref.id)
        held = [v for s, v, _src in rows if s == scheme]
        # Rows may be stored non-canonical (mixed case) — the DELETE
        # matches lower(id_value), so the held check does too.
        if value not in {v.lower() for v in held}:
            raise BadInput(
                f"{handle} has no {scheme}={value!r} identifier row",
                next=(
                    f"{handle} holds {scheme}: {', '.join(held)} — pass "
                    "one of those as 'value'"
                    if held
                    else f"get(kind='{kind}', id='{handle}') lists the "
                    "identifiers it does hold"
                ),
            )
        remaining = [v for v in held if v.lower() != value]
        if dry:
            return Response(
                body=(
                    f"DRY RUN (no write) - would remove identifier "
                    f"{scheme}:{value} from {handle}; "
                    + (
                        f"{len(remaining)} other {scheme} row(s) stay "
                        f"({', '.join(remaining)}); "
                        if remaining
                        else f"no {scheme} rows left afterwards; "
                    )
                    + "no cleared_identifiers marker — the scheme stays "
                    "open for a later correct write; the pair is "
                    "suppressed in meta.dropped_identifiers so "
                    "enrichment can't re-add it; an "
                    "'identifier_dropped' ref_event logged."
                )
            )
        with self.store.tx() as conn:
            removed = self.store.drop_ref_identifier(ref.id, scheme, value, conn=conn)
            if removed is None:
                raise BadInput(
                    f"{handle}'s {scheme}={value!r} row was removed between "
                    "the check and the write — nothing was written",
                    next="re-list the ref's identifiers and retry if the "
                    "row is really still there",
                )
            self.store.append_event(
                ref.id,
                source="agent",
                event="identifier_dropped",
                payload={"reason": reason, "scheme": scheme, "value": removed},
                conn=conn,
            )
        return Response(
            body=(
                f"removed identifier {scheme}:{removed} from {handle} "
                + (
                    f"({len(remaining)} {scheme} row(s) kept); "
                    if remaining
                    else f"(no {scheme} rows left); "
                )
                + "ref_event 'identifier_dropped' logged; the pair is "
                "suppressed in meta.dropped_identifiers (an explicit "
                "re-add lifts it)."
            )
        )

    def _declare_supplement(
        self, child_id: int, parent_handle: str, *, dry: bool = False
    ) -> Response:
        """Declare paper ``child_id`` the supplementary information of
        ``parent_handle`` (slug, ``pa<id>`` or numeric id) after the fact.

        Refuses (``BadInput``, with a ``next=`` hint) when the parent is not
        a live paper, is the child itself, is itself an SI record, or the
        child already has a supplement parent or holds a DOI of its own.
        One transaction: ``pdf_role='supplement'``, the part-of edge
        (:func:`precis.store.si_links.link_supplement`), ``meta.si_parent``,
        a ``Supporting Information: <parent title>`` retitle (unless it
        already has that prefix), the parent's year when the child has none,
        and a ``supplement_declared`` ref_event.
        """
        from precis.store.si_links import (
            SI_PDF_ROLE,
            link_supplement,
            supplement_parent,
            utc_stamp,
        )

        m = re.fullmatch(r"pa(\d+)", parent_handle, flags=re.IGNORECASE)
        try:
            _pslug, parent_id = self._resolve_paper_slug(
                int(m.group(1)) if m else parent_handle
            )
        except NotFound as exc:
            raise BadInput(
                f"supplement_of={parent_handle!r}: no such live paper",
                next="search(kind='paper', q='...') for the parent's slug or paN",
            ) from exc
        if parent_id == child_id:
            raise BadInput(
                "supplement_of= names the record itself",
                next="pass the PARENT paper's slug or paN",
            )
        refs = self.store.fetch_refs_by_ids(
            [child_id, parent_id], include_deleted=False
        )
        child, parent = refs.get(child_id), refs.get(parent_id)
        if child is None or parent is None:
            raise NotFound(
                f"{self.spec.kind} not found (child id={child_id} or "
                f"parent id={parent_id} no longer resolves)",
                next=(
                    f"get(kind='{self.spec.kind}', id={child_id}) and "
                    f"get(kind='{self.spec.kind}', id={parent_id}) to see "
                    "which is gone, then retry with the live pair"
                ),
            )
        child_h = child.slug or f"pa{child_id}"
        parent_h = parent.slug or f"pa{parent_id}"
        if parent.pdf_role == SI_PDF_ROLE:
            raise BadInput(
                f"{parent_h} is itself a supplement record; an SI cannot be the "
                "parent of another SI",
                next="pass the main paper this supplement belongs to",
            )
        with self.store.pool.connection() as conn:
            existing = supplement_parent(conn, child_id)
        if existing is not None:
            raise BadInput(
                f"{child_h} is already the supplement of "
                f"{existing[1] or 'pa' + str(existing[0])}",
                next="no change needed; to re-point it, unlink the existing "
                "part-of edge first",
            )
        if self.store.identifiers_for_refs([child_id]).get(child_id, {}).get("doi"):
            raise BadInput(
                f"{child_h} holds a DOI of its own; a supplement record carries "
                "none (it cites as its parent)",
                next=f"edit(kind='paper', id='{child_h}', mode='replace', "
                "args={'doi': ''}) to drop it first, then retry",
            )
        title = child.title or ""
        if not title.startswith("Supporting Information:"):
            title = f"Supporting Information: {parent.title or parent_h}"
        new_year = parent.year if child.year is None else None
        if dry:
            return Response(
                body=f"DRY RUN (no write) - would declare {child_h} the "
                f"supplementary information of {parent_h}; title {title!r}."
            )
        declared_at = utc_stamp()
        si_parent = {
            "ref_id": parent_id,
            "source": "manual",
            "declared_at": declared_at,
        }
        with self.store.tx() as conn:
            updated = self.store.update_paper_fields(
                child_id,
                title=title if title != child.title else None,
                year=new_year,
                meta_patch={"si_parent": si_parent},
                source="edit",
                conn=conn,
            )
            conn.execute(
                "UPDATE refs SET pdf_role = %s WHERE ref_id = %s",
                (SI_PDF_ROLE, child_id),
            )
            link_supplement(
                child_id, parent_id, store=self.store, conn=conn, set_by="agent"
            )
            self.store.append_event(
                child_id,
                source="edit",
                event="supplement_declared",
                payload={"parent_ref_id": parent_id, "parent": parent_h},
                conn=conn,
            )
            if title != child.title:
                meta = updated.meta or {}
                abstract_val = meta.get("abstract", "")
                kw = meta.get("keywords", [])
                rewrite_cards(
                    conn,
                    child_id,
                    title=updated.title or "",
                    author_names=_shared_author_names(updated.authors),
                    abstract=abstract_val if isinstance(abstract_val, str) else "",
                    keywords=list(kw) if isinstance(kw, list) else [],
                )
        return Response(
            body=f"declared {child_h} the supplementary information of "
            f"{parent_h}; cite as {parent_h}."
        )

    def _doi_edit_warning(self, ref_id: int, *, doi: str, current_title: str) -> str:
        """Warn when a DOI edit risks a future wrong-paper metadata clobber.

        gr180189: an agent edited a paper's DOI to the wrong record, and
        the next metadata re-resolve pass (which trusts any DOI on file)
        overwrote the real title/authors with the other paper's — the
        stub had no ``authors_resolved_at`` stamp yet, so nothing gated
        it. This never blocks the edit (a corrected DOI is exactly what
        the edit affordance is for) — it just makes the risk visible.

        Called **after** the caller's edit transaction has committed —
        it does a synchronous Crossref HTTP round-trip
        (:func:`precis.ingest.crossref.lookup_crossref`), and a slow or
        hung Crossref call must never hold the row lock / pooled
        connection open on ``refs`` (and risk tripping an
        idle-in-transaction timeout that rolls back the legitimate DOI
        edit). Its own ``ref_events`` write below runs on a fresh
        one-shot connection for the same reason.

        Always records a ``doi_edit_metadata_risk`` ref_event noting the
        DOI change happened on a paper that already carries an extracted
        title, so a later re-resolve may replace it. When Crossref is
        reachable, also does a **best-effort**, no-write lookup of the
        new DOI (reusing :func:`precis.ingest.crossref.lookup_crossref`
        — the same lookup :meth:`_render_health` already performs, no
        new dependency) and compares its title against *current_title*
        via :func:`precis.ingest._text_norm.best_jaccard`; a score below
        :data:`_DOI_EDIT_TITLE_MISMATCH_THRESHOLD` sharpens the warning
        into the actual gr180189 signature ("this DOI looks like a
        different paper"). A lookup failure/timeout/miss silently falls
        back to the generic note — never raises.
        """
        warning = (
            f"note: doi changed to {doi!r} on a paper that already has a "
            "title — a future metadata re-resolve may overwrite title/"
            "authors from the new DOI's record"
        )
        event_payload: dict[str, Any] = {"doi": doi, "title": current_title}
        try:
            from precis.ingest._text_norm import best_jaccard
            from precis.ingest.crossref import lookup_crossref

            mailto = settings.get_str("contact.crossref_mailto") or ""
            resolved = lookup_crossref(doi, mailto=mailto)
            resolved_title = (resolved or {}).get("title")
            if resolved_title and str(resolved_title).strip():
                score, _, _ = best_jaccard(current_title, str(resolved_title))
                event_payload["crossref_title"] = resolved_title
                event_payload["title_score"] = round(score, 3)
                if score < _DOI_EDIT_TITLE_MISMATCH_THRESHOLD:
                    warning = (
                        f"WARNING: doi {doi!r} resolves on Crossref to "
                        f"{resolved_title!r}, which looks different from "
                        f"the stored title {current_title!r} (similarity "
                        f"{score:.2f}) — this DOI may belong to a "
                        "different paper; verify before keeping this edit"
                    )
        except Exception:
            pass
        try:
            # No conn= — the edit tx has already committed by the time
            # this runs, so this writes on its own short-lived pooled
            # connection rather than piggybacking on a (closed) tx.
            self.store.append_event(
                ref_id,
                source="edit",
                event="doi_edit_metadata_risk",
                payload=event_payload,
            )
        except Exception:
            pass
        return warning

    def _render_paper_dry_run(
        self,
        ref_id: int,
        *,
        new_title: str | None,
        year: int | None,
        new_authors: list[dict[str, Any]] | None,
        meta_patch: dict[str, Any],
        doi: str | None,
        arxiv: str | None,
        doi_clear: bool = False,
        arxiv_clear: bool = False,
    ) -> Response:
        """Preview a bibliographic metadata patch without writing it.

        Mirrors the file-kind dry_run contract (real preview, no I/O)
        adapted to a field patch rather than a text buffer: reads the
        live ``Ref`` + identifiers, renders each changed field as
        ``old → new``, and touches neither ``refs`` nor
        ``ref_identifiers`` — no :meth:`Store.tx` is opened.

        gr341499: a ``doi=`` / ``arxiv=`` edit can fail atomically at
        commit time — the real write (:meth:`Store.set_ref_identifier`)
        raises :class:`BadInput` when the value is already claimed by a
        *different* **live** ref (the ``(id_kind, id_value)`` uniqueness
        that ``ref_identifiers`` enforces; migration 0049's
        BEFORE-UPDATE trigger lowercases stored DOIs, so the collision
        is case-insensitive) — but that check never ran here, so
        ``dry_run='full'`` reported success on a commit that would
        actually fail. Mirrors it with :meth:`Store.find_ref_by_identifier`
        (the same ``(id_kind, id_value) ... AND retired_at IS NULL``
        lookup the real write's own conflict check runs) for both
        fields, and raises the identical error dry-run would hit for
        real rather than silently implying the write will succeed.
        Every other field this method previews (title / year / authors
        / abstract / journal / entry_type) has no commit-time
        validation beyond "the ref is still live", which the
        ``NotFound`` above already covers — there's nothing else to
        skip.
        """
        old = self.store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
        if old is None:
            raise NotFound(
                f"{self.spec.kind} id={ref_id} not found",
                next=(
                    f"search(kind='{self.spec.kind}', q='<title words>') for the "
                    "current slug, then retry with it"
                ),
            )
        old_ids = self.store.identifiers_for_refs([ref_id]).get(ref_id, {})
        for scheme, value in (("doi", doi), ("arxiv", arxiv)):
            if not value or not str(value).strip():
                continue
            owner_id = self.store.find_ref_by_identifier(scheme, str(value))
            if owner_id is not None and owner_id != ref_id:
                raise BadInput(
                    f"{scheme}={str(value).strip()!r} already belongs to ref id={owner_id}",
                    next=(
                        "a merge_duplicate cleanup is needed — see "
                        "get(kind='skill', id='precis-paper-help') §resolving "
                        "a duplicate ref; prefer the ref with chunks/body as "
                        "the survivor"
                    ),
                )
        lines: list[str] = []
        if new_title is not None:
            lines.append(f"title: {old.title!r} → {new_title!r}")
        if year is not None:
            lines.append(f"year: {old.year!r} → {year!r}")
        if new_authors:
            old_n = len(old.authors or [])
            lines.append(f"authors: {old_n} → {len(new_authors)}")
        if "abstract" in meta_patch:
            old_abstract = (old.meta or {}).get("abstract", "")
            new_abstract = meta_patch["abstract"]
            old_len = len(old_abstract) if isinstance(old_abstract, str) else 0
            lines.append(f"abstract: {old_len} chars → {len(new_abstract)} chars")
        if "journal" in meta_patch:
            old_journal = (old.meta or {}).get("journal") or "(none)"
            lines.append(f"journal: {old_journal!r} → {meta_patch['journal']!r}")
        if "entry_type" in meta_patch:
            old_entry_type = (old.meta or {}).get("entry_type") or "(none)"
            lines.append(
                f"entry_type: {old_entry_type!r} → {meta_patch['entry_type']!r}"
            )
        for meta_key in ("volume", "number", "pages", "url"):
            if meta_key in meta_patch:
                old_val = (old.meta or {}).get(meta_key) or "(none)"
                lines.append(f"{meta_key}: {old_val!r} → {meta_patch[meta_key]!r}")
        if doi:
            lines.append(f"doi: {old_ids.get('doi', '(none)')!r} → {doi!r}")
        elif doi_clear:
            lines.append(f"doi: {old_ids.get('doi', '(none)')!r} → (cleared)")
        if arxiv:
            lines.append(f"arxiv: {old_ids.get('arxiv', '(none)')!r} → {arxiv!r}")
        elif arxiv_clear:
            lines.append(f"arxiv: {old_ids.get('arxiv', '(none)')!r} → (cleared)")
        body = "\n".join(f"  {line}" for line in lines) if lines else "  (no change)"
        return Response(
            body=(
                f"DRY RUN (no write) — would update {self.spec.kind} id={ref_id}:\n"
                f"{body}"
            )
        )

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove paper tags. Allowed axes: ``SRC``, ``CACHE`` + open."""
        require_tag_ops("paper", add, remove)
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "paper", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from this paper to another ref.

        ``meta`` (edge metadata, add-only) lands on the link row —
        a machine writer's admission annotation (``meta.evidence``
        chunk pins, ``meta.{model,prompt_version,run_id,why}``) reaches
        the gate at ``store.add_link`` through it (gr504789).
        """
        target = require_link_target("paper", target)
        validate_link_mode(mode)
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
            meta=meta,
            merge_meta=meta is not None,
            hub=getattr(self, "hub", None),
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # -- rendering helpers ---------------------------------------------------

    def _si_overview_lines(self, ref: Ref) -> list[str]:
        """One line naming the parent on an SI ref, or listing the SI refs on
        a parent (empty for a paper with neither)."""
        from precis.store.si_links import supplement_children, supplement_parent

        if ref.pdf_role == "supplement":
            with self.store.pool.connection() as conn:
                parent = supplement_parent(conn, ref.id)
            if parent is None:
                return []
            p = parent[1] or str(parent[0])
            return [f"supplementary information of {p} — cite as {p}"]
        # Parent side: an ordinary paper pays no query. Only a paper whose SI
        # was requested (``put(mode='fetch-si')``) can have SI refs from the
        # fetch pass; a hand-dropped SI of an unflagged paper is not listed here.
        meta = ref.meta or {}
        if "si_fetch" not in meta and not meta.get("si_fetched"):
            return []
        with self.store.pool.connection() as conn:
            kids = supplement_children(conn, ref.id)
        shown = ", ".join(k[1] or str(k[0]) for k in kids)
        lines = [f"supplementary information: {shown}"] if kids else []
        for item in meta.get("si_skipped") or []:
            if isinstance(item, dict) and item.get("url"):
                lines.append(
                    f"supplement exists, not ingested ({item.get('reason') or 'non_pdf'}): "
                    f"{item['url']}"
                )
        return lines

    def _overview_meta_lines(self, ref: Ref) -> list[str]:
        """Extra one-line ``meta`` fields shown on the default overview.

        Papers have none; sibling kinds with typed meta fields (cfp's
        ``deadline``/``opens_on``) override."""
        return []

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        doi = meta.get("doi")
        year = meta.get("year")
        authors_raw = meta.get("authors")
        authors = _format_authors(authors_raw)
        journal = _clean_inline_text(str(meta.get("journal") or ""))
        n_blocks = self.store.chunks.count_chunks(ref.id)

        lines: list[str] = []
        banner = _paper_banner(ref, store=self.store)
        if banner:
            lines.append(banner)
            lines.append("")
        lines.extend([f"# {_pa(ref)}", f"_{title_md(ref.title)}_"])
        if authors:
            lines.append(authors)
        venue: list[str] = []
        if journal:
            venue.append(journal)
        if year:
            venue.append(str(year))
        if venue:
            lines.append(", ".join(venue))
        if doi:
            lines.append(f"doi: {doi}")
        # Whole-record cite marker (gr514384): a typed software/dataset
        # record shows its genre + version, and — while it still carries
        # no passages (the live predicate) — the cite-whole recipe the
        # author needs. A typed record that since gained a body loses
        # the cite-whole half and reads like an ordinary paper again.
        rt = record_type_of(meta)
        if rt:
            ver = str(meta.get("version") or "").strip()
            line = f"{rt} record" + (f" v{ver}" if ver else "")
            if is_record_cite(self.store, ref):
                line += f" — cite whole: [{_pa(ref)}]; no passages"
            lines.append(line)
        lines.extend(self._si_overview_lines(ref))
        lines.extend(self._overview_meta_lines(ref))
        # gr515633: the categorizer hint line — enabled ref-level axes +
        # topic: tags, "(not yet classified)" per missing axis. Machine
        # output, marked as a filter hint, not curated truth.
        from precis.categorizers import ref_hint_line

        hint = ref_hint_line(self.store, ref)
        if hint:
            lines.append(hint)
        lines.append("")
        lines.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")
        abstract = meta.get("abstract")
        if abstract:
            lines.append("")
            # Strip JATS XML *before* excerpting — otherwise the
            # 500-char window can chop a tag mid-attribute and the
            # downstream "<jats:" sniff won't catch the dangling
            # garbage. The MCP critic flagged the default overview
            # leaking ``<jats:title>Abstract</jats:title><jats:p>…``
            # verbatim into the response body; ``_strip_jats`` is
            # the same helper view='abstract' uses, so the two
            # paths agree on cleanup.
            lines.append(_excerpt(_strip_jats(str(abstract)), limit=500))

        body = "\n".join(lines)
        next_steps = [
            (
                f"get(id='{_pa(ref)}', view='toc')",
                "see the TOC",
            ),
            (
                f"get(id='{_pa(ref)}~0..5')",
                "read the first 6 chunks",
            ),
        ]
        # BibTeX only makes sense for citable evidence; a spec-role doc
        # (a call-for-proposal) is never cited, so skip the hint.
        if self.spec.corpus_role == "evidence":
            next_steps.append(
                (
                    f"get(id='{_pa(ref)}', view='bibtex')",
                    "get the BibTeX entry",
                )
            )
        next_steps.append(
            (
                f"search(kind='{ref.kind}', q='...', scope='{_pa(ref)}')",
                f"search blocks within this {ref.kind}",
            )
        )
        body += render_next_section(next_steps)
        # Change B: surface the ref-to-ref link graph on the default
        # read too — not just ``view='links'`` — so an agent reading a
        # paper the plain way still sees its evidential edges (cites,
        # supports, contradicts, …). Capped + priority-sorted so a
        # heavily-linked paper doesn't blow the response budget; the
        # overflow line points at ``view='links'`` for the rest.
        # gr311679: shares ``DEFAULT_LINK_ROW_CAP`` with the numeric-ref
        # bare-``get`` callsite instead of a duplicated ``limit=12``
        # literal.
        from precis.handlers._links_render import (
            DEFAULT_LINK_ROW_CAP,
            render_links_section,
        )

        body += render_links_section(
            self.store, ref, limit=DEFAULT_LINK_ROW_CAP, priority=True
        )
        return Response(body=body)

    def _render_ops_view(
        self, ref: Ref, args: dict[str, Any] | None = None
    ) -> Response:
        """``view='ops'`` — the ref's whole stored state as a
        ``precis-paper-dump/1`` fenced block (gr517423 slice 1:
        :mod:`precis.ingest.paper_dump`), paged over every chunk by
        ``args={'page': N, 'page_size': K}`` exactly like the se kind's
        ops export. This is the undo the ``drop_body``/``clear_abstract``
        /``split_body``/``move_body_to`` dry runs point at; the restore
        door that consumes it is slice 2 of the same spec."""
        from precis.ingest import paper_dump

        handle = _pa(ref)
        with self.store.pool.connection() as conn:
            body = paper_dump.render_ops(
                conn,
                ref_id=ref.id,
                title=ref.title or handle,
                handle=handle,
                args=dict(args) if args is not None else None,
            )
        return Response(
            body=body,
            # Same fold-recovery hint as the se export: a folded dump's
            # middle is only reachable by slicing.
            pagination_alt_hint=(
                f"get(kind='paper', id={handle!r}, view='ops', "
                "args={'page': <n>, 'page_size': <count>}) fetches one "
                "slice of the dump — the concatenated slices' `chunks` "
                "arrays are the whole export"
            ),
        )

    def _render_pdf_link_view(
        self, ref: Ref, args: dict[str, Any] | None = None
    ) -> Response:
        """``view='pdf_link'`` — a signed, short-lived download URL for
        the ref's held PDF (gr517423 slice 3: :mod:`precis.file_links`).
        Bytes never ride an MCP result; precis_web streams them to the
        token bearer at ``GET /files/pdf/<sha>?t=<token>``. Reusable
        until expiry — ``args={'ttl_s': …}``, default 600, cap 3600.
        """
        from precis import file_links
        from precis import secrets as vault

        handle = _pa(ref)
        sha = getattr(ref, "pdf_sha256", None)
        if not sha:
            raise BadInput(
                f"{handle} holds no PDF — nothing to link",
                next=(
                    f"put(kind='paper', id='{handle}', "
                    "args={'pdf_url': 'https://…/x.pdf'})"
                ),
            )
        ttl_raw = (args or {}).get("ttl_s", file_links.DEFAULT_TTL_S)
        try:
            # ``int(float('inf'))`` raises OverflowError — JSON Infinity
            # arrives as exactly that.
            ttl_s = int(ttl_raw)
        except (TypeError, ValueError, OverflowError):
            raise BadInput(
                f"args['ttl_s'] must be seconds, got {ttl_raw!r}",
                next=f"get(kind='paper', id='{handle}', view='pdf_link')",
            ) from None
        token = file_links.mint("get", sha, ref.id, ttl_s, store=self.store)
        exp = file_links.parse(token).exp
        path = f"/files/pdf/{sha}?t={token}"
        base = (
            vault.get_secret(file_links.WEB_BASE_URL_SECRET, store=self.store) or ""
        ).rstrip("/")
        url = f"{base}{path}" if base else path
        # ``pdfs`` carries the ingest-time byte size and physical page
        # count — surfaced so the caller can size the fetch before it
        # commits to the download.
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT size_bytes, page_count FROM pdfs WHERE pdf_sha256 = %s",
                (sha,),
            ).fetchone()
        size_b = f"{int(row[0])} B" if row else "unknown"
        pages = str(int(row[1])) if row else "unknown"
        expires = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines: list[str] = []
        if not base:
            lines.append(
                "PRECIS_WEB_BASE_URL is not set — the link below is "
                "path-only; prepend the precis_web origin to fetch it "
                "(the operator sets it via the vault)."
            )
        stem = ref.slug or handle
        lines += [
            f"# {handle} — PDF download link",
            f"url: {url}",
            f"expires: {expires} (reusable until then)",
            f"sha256: {sha}",
            f"bytes: {size_b} · pages: {pages}",
            "",
            f"curl -fo {shlex.quote(stem + '.pdf')} '{url}'",
        ]
        return Response(body="\n".join(lines))

    def _render_page_image_view(
        self, ref: Ref, args: dict[str, Any] | None = None
    ) -> Response:
        """``view='page_image'`` — a signed link to a figure's pixels
        (gr520326): a stored figure crop when the named chunk carries
        one, else one page of the ref's held PDF rendered to PNG.

        Bytes never ride an MCP result — the ``file_links`` contract of
        ``pdf_link``: precis_web streams ``GET /files/blob/<sha>`` for a
        ``chunk_blobs`` crop, ``GET /files/pdf_page/<sha>/<page>`` for a
        page render (fitz). ``args={'page': N}`` is the 0-based PDF page
        — the numbering ``chunks.page_first`` and Marker ``page-N``
        spans share. ``args={'ord': N}`` names a body chunk: a stored
        crop wins, else the chunk's page resolves server-side (own
        ``page-N`` span → nearest neighbour span → ``page_first``).
        ``ttl_s`` rides like ``pdf_link``'s.
        """
        from precis import file_links
        from precis import secrets as vault

        handle = _pa(ref)
        a = args or {}
        page_raw: Any = a.get("page")
        ord_raw: Any = a.get("ord")
        if (page_raw is None) == (ord_raw is None):
            raise BadInput(
                "page_image wants exactly one of args={'page': N} "
                "(0-based PDF page) or args={'ord': N} (a body chunk)",
                next=(
                    f"get(id='{handle}', view='page_image', "
                    "args={'ord': <ord from a caption/figure chunk>})"
                ),
            )
        ttl_raw = a.get("ttl_s", file_links.DEFAULT_TTL_S)
        try:
            ttl_s = int(ttl_raw)
        except (TypeError, ValueError, OverflowError):
            raise BadInput(
                f"args['ttl_s'] must be seconds, got {ttl_raw!r}",
                next=f"get(id='{handle}', view='page_image')",
            ) from None
        ttl_s = max(1, min(ttl_s, file_links.MAX_TTL_S))

        base = (
            vault.get_secret(file_links.WEB_BASE_URL_SECRET, store=self.store) or ""
        ).rstrip("/")

        def _mint_url(sha: str, path: str) -> tuple[str, str]:
            token = file_links.mint("get", sha, ref.id, ttl_s, store=self.store)
            exp = file_links.parse(token).exp
            expires = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return f"{base}{path}?t={token}", expires

        stem = ref.slug or handle
        banner = (
            []
            if base
            else [
                "PRECIS_WEB_BASE_URL is not set — the link below is "
                "path-only; prepend the precis_web origin to fetch it "
                "(the operator sets it via the vault)."
            ]
        )

        blob_sha: str | None = None
        blob_ord: int | None = None
        if ord_raw is not None:
            try:
                ord_i = int(ord_raw)
            except (TypeError, ValueError, OverflowError):
                raise BadInput(
                    f"args['ord'] must be a chunk ord, got {ord_raw!r}",
                    next=f"get(id='{handle}', view='page_image', args={{'page': 0}})",
                ) from None
            chunk = self.store.chunks.get_chunk(ref.id, pos=ord_i)
            if chunk is None:
                raise NotFound(
                    f"no body chunk at ~{ord_i} in {handle}",
                    next=f"get(id='{handle}', view='toc')",
                )
            hit = self.store.chunks.image_blobs_for_ords(ref.id, [ord_i]).get(ord_i)
            if hit is not None:
                _cid, blob_sha, _mime = hit
                blob_ord = ord_i
            else:
                page, _exact = self._chunk_page_hint(ref.id, chunk)
                if page is None:
                    raise NotFound(
                        f"~{ord_i} in {handle} has no stored figure image "
                        "and no page can be derived for it",
                        next=f"get(id='{handle}', view='pdf_link')",
                    )
                page_raw = page

        if blob_sha is not None:
            url, expires = _mint_url(blob_sha, f"/files/blob/{blob_sha}")
            lines = [
                *banner,
                f"# {handle} — figure image link (chunk ~{blob_ord})",
                f"url: {url}",
                f"expires: {expires} (reusable until then)",
                f"sha256: {blob_sha}",
                "",
                f"curl -fo {shlex.quote(stem + f'-c{blob_ord}.img')} '{url}'",
            ]
            return Response(body="\n".join(lines))

        try:
            page = int(page_raw)
        except (TypeError, ValueError, OverflowError):
            raise BadInput(
                f"args['page'] must be a 0-based PDF page, got {page_raw!r}",
                next=f"get(id='{handle}', view='page_image', args={{'page': 0}})",
            ) from None
        if page < 0:
            raise BadInput(
                f"args['page'] must be >= 0, got {page}",
                next=f"get(id='{handle}', view='page_image', args={{'page': 0}})",
            )
        sha = getattr(ref, "pdf_sha256", None)
        if not sha:
            raise BadInput(
                f"{handle} holds no PDF — no page to render",
                next=(
                    f"put(kind='{ref.kind}', id='{handle}', "
                    "args={'pdf_url': 'https://…/x.pdf'})"
                ),
            )
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT page_count FROM pdfs WHERE pdf_sha256 = %s", (sha,)
            ).fetchone()
        if row and row[0] is not None and page >= int(row[0]):
            raise BadInput(
                f"page {page} is past the end — {handle}'s PDF has {int(row[0])} pages",
                next=f"get(id='{handle}', view='page_image', args={{'page': {int(row[0]) - 1}}})",
            )
        url, expires = _mint_url(sha, f"/files/pdf_page/{sha}/{page}")
        lines = [
            *banner,
            f"# {handle} — page {page} image link",
            f"url: {url}",
            f"expires: {expires} (reusable until then)",
            f"page: {page} (0-based PDF index)",
            "",
            f"curl -fo {shlex.quote(stem + f'-p{page}.png')} '{url}'",
        ]
        return Response(body="\n".join(lines))

    def _chunk_page_hint(
        self,
        ref_id: int,
        chunk: Any,
        *,
        window: list[Any] | None = None,
        pages: dict[int, int] | None = None,
    ) -> tuple[int | None, bool]:
        """``(page, exact)`` — the 0-based PDF page a chunk's content sits
        on. Exact when a Marker ``page-N`` span inside the chunk's own
        text says so; approximate off the nearest neighbour span (within
        ``_PAGE_SPAN_WINDOW`` ords) or the TOC-anchor ``page_first``
        interpolation, in that order. ``pages`` is the precomputed
        ord→page_first map — passing it skips the per-chunk
        ``chunk_pages`` query on the fallback leg."""
        own = _page_spans(chunk.text)
        if own:
            return own[0], True
        if window is None:
            window = self.store.chunks.list_chunks_for_ref(
                ref_id,
                pos_range=(
                    max(0, chunk.ord - _PAGE_SPAN_WINDOW),
                    chunk.ord + _PAGE_SPAN_WINDOW,
                ),
            )
        nearest: tuple[int, int] | None = None  # (ord distance, page)
        for n in window:
            if n.ord == chunk.ord:
                continue
            spans = _page_spans(n.text)
            if not spans:
                continue
            # A *preceding* neighbour's closest span to this chunk is its
            # last; a following neighbour's is its first. Tuple order
            # prefers the lower page on a distance tie.
            cand = (
                abs(n.ord - chunk.ord),
                spans[-1] if n.ord < chunk.ord else spans[0],
            )
            if nearest is None or cand < nearest:
                nearest = cand
        if nearest is not None:
            return nearest[1], False
        if pages is not None:
            page = pages.get(chunk.ord)
        else:
            page = self.store.chunks.chunk_pages(ref_id, [chunk.ord]).get(chunk.ord)
        return (page, False) if page is not None else (None, False)

    def _figure_pointer_lines(
        self, ref: Ref, blocks: list[Any]
    ) -> dict[int, list[str]]:
        """Per-ord extra render lines giving a figure-ish chunk's pixels
        a door (gr520326): a ``figure image:`` crop link when the chunk —
        or the figure block next to a caption-only one — has an image in
        ``chunk_blobs``, else a ``page N of <pa>:`` pointer naming the
        ``page_image`` call.

        Qualifying chunks are ``figure``/``caption`` kinds and
        caption-only blocks (a legend with no body — the datasheet
        failure where a figure's pixels never landed). A read with no
        qualifying block pays nothing: the blob/page probes only run
        then.
        """
        qual = [
            b
            for b in blocks
            if b.chunk_kind in _FIGURE_CHUNK_KINDS or _is_caption_only_block(b.text)
        ]
        if not qual:
            return {}
        reach = sorted({o for b in qual for o in (b.ord - 1, b.ord, b.ord + 1)})
        blobs = self.store.chunks.image_blobs_for_ords(ref.id, reach)
        pages = self.store.chunks.chunk_pages(ref.id, [b.ord for b in qual])
        handle = _pa(ref)
        window: list[Any] | None = None
        out: dict[int, list[str]] = {}
        for b in qual:
            blob_ord = next(
                (o for o in (b.ord, b.ord - 1, b.ord + 1) if o in blobs),
                None,
            )
            if blob_ord is not None:
                cid, _sha, _mime = blobs[blob_ord]
                img_handle = (
                    handle_registry.try_format(ref.kind, cid, chunk=True)
                    or f"{ref.slug or handle}~{blob_ord}"
                )
                out[b.ord] = [
                    f"figure image: {img_handle} — "
                    f"get(id='{handle}', view='page_image', "
                    f"args={{'ord': {blob_ord}}})"
                ]
                continue
            page: int | None
            approx: bool
            if _page_spans(b.text):
                page, approx = _page_spans(b.text)[0], False
            else:
                if window is None:
                    lo = max(0, min(b.ord for b in qual) - _PAGE_SPAN_WINDOW)
                    hi = max(b.ord for b in qual) + _PAGE_SPAN_WINDOW
                    # When the read's own block list already covers the
                    # window (a whole-doc read with qualifiers at both
                    # ends), reuse it — a fresh range query would just
                    # refetch what ``blocks`` holds.
                    have = {blk.ord for blk in blocks}
                    if all(o in have for o in range(lo, hi + 1)):
                        window = [blk for blk in blocks if lo <= blk.ord <= hi]
                    else:
                        window = self.store.chunks.list_chunks_for_ref(
                            ref.id, pos_range=(lo, hi)
                        )
                page, exact = self._chunk_page_hint(
                    ref.id, b, window=window, pages=pages
                )
                approx = not exact
            if page is None:
                continue
            out[b.ord] = [
                f"page {'≈' if approx else ''}{page} of {handle}: "
                f"get(id='{handle}', view='page_image', "
                f"args={{'page': {page}}})"
            ]
        return out

    def _render_view(
        self, ref: Ref, view: str, *, args: dict[str, Any] | None = None
    ) -> Response:
        if view == "ops":
            return self._render_ops_view(ref, args)
        if view == "pdf_link":
            return self._render_pdf_link_view(ref, args)
        if view == "page_image":
            return self._render_page_image_view(ref, args)
        if view == "log":
            # Per-ref chronological event log across every subsystem
            # that writes to ref_events (chase, fetcher, provenance,
            # ingest). No source filter — for papers the cross-cutting
            # view is the useful one ("what's happened to this paper?").
            from precis.handlers._event_log_render import render_event_log

            return render_event_log(self.store, ref.id)
        if view == "abstract":
            abstract = (ref.meta or {}).get("abstract")
            if not abstract:
                # Empty result still teaches the next call shape:
                # the paper's body is reachable via TOC + chunk
                # ranges even when the publisher's abstract metadata
                # is missing. Without this trailer the bare
                # "no abstract on file" was a dead end (MCP critic
                # MINOR-C 2026-05-02).
                slug = ref.slug or "???"
                body = f"no abstract on file for {slug}"
                body += render_next_section(
                    [
                        (
                            f"get(id='{_pa(ref)}', view='toc')",
                            "see the TOC and pick a section",
                        ),
                        (
                            f"get(id='{_pa(ref)}~0..5')",
                            "read the first 6 chunks (often include the abstract)",
                        ),
                        (
                            f"search(kind='{ref.kind}', q='abstract', scope='{_pa(ref)}')",
                            "search blocks within this paper",
                        ),
                    ]
                )
                return Response(body=body)
            # Strip JATS XML namespace tags (<jats:title>, <jats:p>, …)
            # that some publishers leave in the metadata, then run the
            # entity unescape pipeline so ``&amp;`` lands as ``&``.
            # The MCP critic flagged the body-only response as missing
            # any affordance — add a slug header + Next: trailer so
            # the caller knows which paper they're reading and where
            # to go next. (MCP critic NIT — abstract has no header /
            # Next:.)
            cleaned = _clean_inline_text(_strip_jats(str(abstract)))
            slug = ref.slug or "???"
            title = title_md(ref.title)
            body = f"# {slug} - abstract\n_{title}_\n\n{cleaned}"
            abstract_next = [
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "see the TOC",
                ),
            ]
            if self.spec.corpus_role == "evidence":
                abstract_next.append(
                    (
                        f"get(id='{_pa(ref)}', view='bibtex')",
                        "get the BibTeX entry",
                    )
                )
            body += render_next_section(abstract_next)
            return Response(body=body)

        if view == "toc":
            return self._render_toc(ref, scope=None)

        if view == "summaries":
            return self._render_summaries(ref)

        if view in ("bibtex", "ris", "endnote"):
            # F15: pre-fetch the DOI from ref_identifiers. The v2
            # schema moved DOI off ref.meta into its own table, but
            # _render_citation was still reading meta.get('doi') and
            # always finding None — so every bibtex looked like a
            # stub even when the data was fully populated.
            doi: str | None = None
            try:
                for scheme, value, _src in self.store.list_ref_identifiers(ref.id):
                    if scheme == "doi" and value:
                        doi = value
                        break
            except Exception:
                doi = None
            return Response(body=_render_citation(ref, style=view, doi=doi))

        if view == "health":
            return self._render_health(ref)

        if view == "abbrevs":
            return self._render_abbrevs(ref)

        if view == "bibliography":
            return self._render_bibliography(ref)

        if view == "claims":
            # Taproot claim hubs THIS paper grounds — not to be confused
            # with the patent kind's ``view='claims'`` (that one is the
            # patent's own legal claim set; different animal, same view
            # name, no collision since views are scoped per kind).
            return self._render_claims(ref)

        if view == "authors":
            return self._render_authors(ref)

        if view == "chunks":
            return self._render_chunk_stats(ref)

        if view == "tables":
            from precis.handlers._paper_tables import render_tables_view

            return render_tables_view(self.store, ref, args=args)

        if view == "measures":
            from precis.handlers._paper_measures import render_measures_view

            return render_measures_view(self.store, ref)

        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️): paper
            # had no links view at all — ``related-to`` / ``cites`` /
            # ``cited-by`` edges on a paper (214 inbound related-to on
            # some refs, checked in prod) were invisible to an agent
            # reading it. Shares the render with every numeric-ref kind
            # (``handlers/_links_render.py``) rather than forking it.
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense=self.spec.kind)

        # The MCP critic flagged ``view='figures'`` as a silent
        # failure — the agent had no signal that figure retrieval
        # is unsupported. Surface a sharper hint pointing at the
        # caption-only figure workflow documented in
        # precis-paper-help so the agent doesn't keep retrying.
        # (Critic MAJOR #4.)
        if view in ("figures", "fig", "figure"):
            raise Unsupported(
                f"figure view {view!r} not implemented for kind='paper'",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "figure binaries aren't served - figures live as legend "
                    "blocks inside the body. Find the figure number via "
                    "view='toc', then read the legend block "
                    "(e.g. 'Figure 3. …' on a ~N block). See the "
                    "'Figures' section of precis-paper-help."
                ),
            )
        # ``view='fig/<N>'`` is documented in precis-paper-help as a
        # reserved-for-future affordance.  Without a dedicated branch
        # it falls into the generic "unknown view" error below,
        # which makes a caller who *has* read the help skill assume
        # the docs are wrong rather than the build being early.
        # Surface the reservation explicitly so the caller knows to
        # use the caption-only workaround until figure-binary
        # serving is wired.  (MCP critic MINOR — fig/<N> documented
        # but unrecognised view path returns the same enum as a
        # typo.)
        if view.startswith("fig/"):
            raise Unsupported(
                f"view={view!r} is reserved for a future build",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "view='fig/<N>' is documented in precis-paper-help as "
                    "reserved - figure-binary serving isn't wired yet.  "
                    "Until then, find the figure number via view='toc' "
                    "and read the legend block on the matching ~N "
                    "(e.g. 'Figure 3. …')."
                ),
            )
        raise Unsupported(
            f"unknown view {view!r} for kind='paper'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-paper-help - try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_chunk_stats(self, ref: Ref) -> Response:
        """``view='chunks'`` — chunk counts by ``chunk_kind`` and by
        ``section_path`` tail, plus total and body (``ord >= 0``)
        totals. The per-paper sizing read that used to need prod-psql
        (gr503059). ``id=[…]`` batches it through ``_get_batch``."""
        stats = self.store.chunks.chunk_stats_for_ref(ref.id)
        lines = [
            f"# {_pa(ref)} — chunks",
            f"total: {stats['total']} · body: {stats['body']}",
        ]
        if stats["by_kind"]:
            lines += ["", "by chunk_kind:"]
            lines += [f"  {k}: {n}" for k, n in stats["by_kind"].items()]
        if stats["by_section"]:
            lines += ["", "by section tail:"]
            lines += [f"  {t}: {n}" for t, n in stats["by_section"].items()]
        return Response(body="\n".join(lines), result_count=stats["total"])

    def _render_bibliography(self, ref: Ref) -> Response:
        """``view='bibliography'`` — citations referencing this paper.

        Walks the ``links`` table for ``cites`` edges whose
        destination is this paper (``links_for(ref_id,
        relation='cited-by')`` — the inverse-rewrite returns the
        ``cites`` rows from citation→paper). For each, pulls the
        citation ref and renders its claim, source handle, verbatim
        quote, and verifier confidence.

        Returns a "no citations on file" placeholder with recovery
        hints when no citations exist for this paper.
        """
        cite_links = self.store.links_for(ref.id, direction="in", relation="cites")
        slug = ref.slug or "???"

        if not cite_links:
            body = f"# {slug} bibliography — 0 citations on file"
            body += render_next_section(
                [
                    (
                        f"get(id='{_pa(ref)}', view='toc')",
                        "browse segments to find drillable claims",
                    ),
                    (
                        "get(kind='skill', id='precis-citation-help')",
                        "how to file a verified citation",
                    ),
                ]
            )
            return Response(body=body)

        rows: list[dict[str, str]] = []
        first_citation_id: int | None = None
        for link in cite_links:
            citation = self.store.get_ref(kind="citation", id=link.src_ref_id)
            if citation is None:
                continue
            if first_citation_id is None:
                first_citation_id = citation.id
            meta = citation.meta or {}
            handle = meta.get("source_handle") or ""
            quote = _clean_inline_text(meta.get("source_quote") or "")
            quote = _excerpt(quote, limit=120)
            claim = _clean_inline_text(meta.get("claim") or citation.title or "")
            claim = _excerpt(claim, limit=80)
            confidence = meta.get("verifier_confidence")
            conf_str = f"{float(confidence):.2f}" if confidence is not None else "?"
            rows.append(
                {
                    "id": f"citation:{citation.id}",
                    "claim": claim,
                    "source": handle,
                    "conf": conf_str,
                    "quote": f'"{quote}"',
                }
            )

        head = f"# {slug} bibliography — {len(rows)} citation{'s' if len(rows) != 1 else ''}"
        body = (
            head
            + "\n\n"
            + render_agent_table(
                rows, schema=["id", "claim", "source", "conf", "quote"]
            )
        )
        # ``get(kind='citation', id=<N>)`` was an unquoted angle
        # placeholder — a SyntaxError, not even a valid template. The
        # table above always carries a real ``citation:<id>`` value
        # once ``rows`` is non-empty;
        # interpolate it as a proper literal instead.
        nav: list[tuple[str, str]] = []
        if first_citation_id is not None:
            nav.append(
                (
                    f"get(kind='citation', id={first_citation_id})",
                    "read one citation's full record (the verifier's caveats too)",
                )
            )
        nav.append(
            (
                "get(kind='skill', id='precis-citation-help')",
                "the verifier-workflow agent surface",
            )
        )
        body += render_next_section(nav)
        return Response(body=body)

    def _render_claims(self, ref: Ref) -> Response:
        """``view='claims'`` — the Taproot claim hubs THIS paper grounds
        (an inbound ``establishes``/``corroborates``/``contradicts``
        evidence edge), i.e. "what claims does this paper ground?". This
        is a *different* ``view='claims'`` than the patent kind's (a
        patent's own legal claim set) — the name collides across kinds
        only in spelling, never in dispatch, since views are looked up
        per ``self.spec.kind``.

        Reads :func:`~precis.taproot.lookup.hubs_grounded_by_paper` with
        ``require_pub_id=False`` — unlike the cite-time nudge
        (``_draft_lint.pc_cite_claim_hub_hint``, which wants only citable
        hubs), a hub this paper grounds but not yet assigned a ``pub_id``
        is exactly the mint-frontier signal a reader here wants, not
        noise to hide. ``pub_id`` itself never renders in this table
        (the read-for-question loop (skill precis-read-for-question) slice 2: skills already
        tell authors to cite ``[fi<id>]``, never the pub_id placeholder) —
        an unassigned one still surfaces the hub, just without a column
        naming the gap.

        Posture (``state``/``support``/``flags``) comes from
        ``handlers/finding.py::_posture_cells`` over one batched
        ``hub_rows(ref_ids=[...])`` call — reused rather than a second
        posture rendering, so the two surfaces can't drift apart.
        ``role`` is the evidence relation this paper's own edge carries,
        so a paper that CONTRADICTS a hub reads distinctly from one that
        establishes/corroborates it.
        """
        from precis.handlers.finding import _posture_cells
        from precis.nanopub.overview import hub_rows
        from precis.taproot.lookup import hubs_grounded_by_paper

        slug = ref.slug or "???"
        hubs = hubs_grounded_by_paper(self.store, ref.id, require_pub_id=False)
        if not hubs:
            body = f"# {slug} — no claim hubs"
            body += render_next_section(
                [
                    (
                        f"put(kind='finding', title='<claim sentence>', "
                        f"supporters=[{{'paper': '{_pa(ref)}', "
                        "'source_handle': '<pc_id>'}])",
                        "mint a claim hub grounded in this paper — "
                        f"get a pc<id> chunk handle from "
                        f"get(id='{_pa(ref)}', view='toc') or any chunk read",
                    ),
                ]
            )
            return Response(body=body)

        postures = {
            row.ref_id: row
            for row in hub_rows(self.store, ref_ids=[h["hub_ref_id"] for h in hubs])
        }
        rows: list[dict[str, str]] = []
        hub_handles: list[str] = []
        for h in hubs:
            hub_handle = handle_registry.format_handle("finding", h["hub_ref_id"])
            hub_handles.append(hub_handle)
            claim = _excerpt(_clean_inline_text(h["claim"] or ""), limit=80)
            cells = _posture_cells(postures.get(h["hub_ref_id"]))
            rows.append(
                {
                    "hub": f"[{hub_handle}]",
                    "role": h["role"],
                    "claim": claim,
                    **cells,
                }
            )

        head = f"# {slug} — {len(rows)} claim hub(s) grounded"
        table = render_agent_table(
            rows,
            schema=["hub", "role", "claim", "state", "support", "flags"],
        )
        body = f"{head}\n\n{table}"
        body += render_next_section(
            [
                (
                    f"get(kind='finding', id='{hub_handles[0]}')",
                    "read one hub's full evidence graph",
                ),
            ]
        )
        return Response(body=body)

    def _render_authors(self, ref: Ref) -> Response:
        """``view='authors'`` — the ``paper_authors`` byline table
        (precis.utils.authors module docstring): position, display name,
        source tier chip, a ``✓ verified <date>`` mark when ORCID has
        cross-checked the row, and the three per-author links (ORCID —
        iD + the ``oi<id>`` node handle when a ``kind='orcid'`` node is
        linked, OpenAlex, Google Scholar name search), plus the
        paper-level Scholar lookup (DOI-keyed when a DOI is on file).

        Tier ladder (highest wins on a subsequent write, `human` always
        wins): ``orcid`` > ``crossref``/``openalex``/``s2`` >
        ``pdf``/``legacy`` > ``human`` (human is not "highest tier" by
        provenance quality — it wins by the write-time guard in
        :func:`~precis.store._refs_ops.project_paper_authors`, not by
        rank here). Empty table (no byline on file) renders a one-line
        hint pointing at the edit affordance instead of a bare table.
        """
        slug = ref.slug or "???"
        rows = self.store.get_paper_authors(ref.id)
        doi: str | None = None
        try:
            for scheme, value, _src in self.store.list_ref_identifiers(ref.id):
                if scheme == "doi" and value:
                    doi = value
                    break
        except Exception:
            doi = None

        if not rows:
            body = f"# {slug} — no authors on file"
            body += render_next_section(
                [
                    (
                        f"edit(kind='paper', id='{_pa(ref)}', "
                        "authors=['Family, Given'])",
                        "add the byline (one 'Family, Given [ORCID]' line per author)",
                    ),
                ]
            )
            return Response(body=body)

        table_rows: list[dict[str, str]] = []
        for row in rows:
            name = author_display(entry_from_author_row(row)) or row.get("name_raw", "")
            verified = ""
            if row.get("verified_at"):
                verified = f"✓ verified {row['verified_at'].date().isoformat()}"
            links = author_links(row, doi=doi, title=title_plain(ref.title))
            node = ""
            if row.get("person_ref_id") is not None:
                node = handle_registry.format_handle("orcid", row["person_ref_id"])
            table_rows.append(
                {
                    "pos": str(row["position"]),
                    "name": name,
                    "source": row.get("source") or "",
                    "verified": verified,
                    "orcid": links.get("orcid", ""),
                    "node": node,
                    "openalex": links.get("openalex", ""),
                    "scholar": links.get("scholar", ""),
                }
            )
        head = f"# {slug} — {len(table_rows)} author(s)"
        table = render_agent_table(
            table_rows,
            schema=[
                "pos",
                "name",
                "source",
                "verified",
                "orcid",
                "node",
                "openalex",
                "scholar",
            ],
        )
        body = f"{head}\n\n{table}"
        scholar = paper_scholar_link(doi, title_plain(ref.title))
        if scholar:
            body += f"\n\nPaper-level Scholar lookup: {scholar}"
        body += render_next_section(
            [
                (
                    f"edit(kind='paper', id='{_pa(ref)}', "
                    "authors=['Family, Given M. [0000-0002-1825-0097]', ...])",
                    "correct the byline (human edits always win over a later "
                    "tier's re-resolution)",
                ),
            ]
        )
        return Response(body=body)

    def _render_abbrevs(self, ref: Ref) -> Response:
        """F20: render the per-paper abbreviation legend.

        Reads ``ref.meta['abbrevs']`` (populated lazily by the
        chunk_keywords worker via Schwartz-Hearst over the body text).
        Renders alphabetical TOON ``{short	long}``. Empty-paper case
        gets a placeholder + a pointer at the worker.
        """
        slug = ref.slug or "???"
        meta = ref.meta or {}
        abbrevs = meta.get("abbrevs") or {}
        if not abbrevs:
            body = (
                f"# {slug} — no abbreviations detected yet\n\n"
                "Detection runs on the first chunk_keywords worker pass "
                "over this paper. Either the worker hasn't run yet, or "
                "the body text contains no ``long form (SHORT)`` "
                "patterns that Schwartz-Hearst recognises."
            )
            return Response(body=body)
        # Normalise: legacy entries are plain strings, newer ones
        # may be ``{long, first_at}`` envelopes. Render to plain dict
        # for sorting + table rendering.
        flat: dict[str, str] = {}
        for short, val in abbrevs.items():
            if isinstance(val, str):
                flat[short] = val
            elif isinstance(val, dict) and "long" in val:
                flat[short] = str(val["long"])
        rows = [{"short": short, "long": flat[short]} for short in sorted(flat)]
        head = f"# {slug} — {len(rows)} abbreviation(s)"
        table = render_agent_table(rows, schema=["short", "long"])
        return Response(body=f"{head}\n\n{table}")

    def _render_health(self, ref: Ref) -> Response:
        """Phase 5 shim: ``view='health'`` on a paper ref.

        Looks up the paper's DOI from ``ref_identifiers``, then
        delegates to the provenance kind's ``check_doi`` so agents
        that already have a slug don't have to do the slug→DOI
        lookup themselves. The full markdown report comes back via
        ``render_single`` — same shape as
        ``get(kind='provenance', id='<doi>')``.

        Edge cases:
        - **Paper has no DOI on file** (preprints from venues
          Crossref doesn't index, book chapters, hand-ingested
          records). We surface a clear error rather than silently
          succeeding — provenance is meaningless without a DOI.
        """
        from precis.handlers._provenance_report import render_single
        from precis.ingest.provenance import check_doi

        # Pull all known identifiers for this ref; pick the DOI.
        try:
            aliases = self.store.list_ref_identifiers(ref.id)
        except Exception as exc:
            raise BadInput(
                f"paper view='health': cannot read identifiers for {ref.slug}: {exc}",
                next=f"get(id='{_pa(ref)}', view='bibtex')",
            ) from exc

        doi: str | None = None
        for scheme, value, _source in aliases:
            if scheme == "doi" and value:
                doi = value
                break

        if doi is None:
            slug = ref.slug or "???"
            raise BadInput(
                f"paper view='health': no DOI on file for {slug} — "
                "provenance checks require a DOI",
                next=f"get(id='{_pa(ref)}', view='abstract')",
            )

        # Inherit the mailto convention from settings, same as the
        # provenance handler.
        mailto = settings.get_str("contact.crossref_mailto")
        result = check_doi(doi, store=self.store, mailto=mailto)
        return Response(body=render_single(result))

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        if lo == hi and lo < 0:
            # Synthetic card-variant chunk (title/authors/abstract/combined,
            # ord<0) — ``list_chunks_for_ref`` excludes ``ord<0`` by design
            # (a body range/TOC scan shouldn't pick up derived search
            # chunks), so route a single exact-ord card fetch through
            # ``get_chunk`` instead, which carries no such restriction.
            # ``(ref_id, ord)`` is a genuine unique key
            # (``chunks_ref_id_ord_key``), so this isn't a lossy
            # ord round-trip: the ord in a search-emitted ``pc<id>``
            # handle always resolves back to the same row. gr334152.
            card = self.store.chunks.get_chunk(ref.id, pos=lo)
            blocks = [card] if card is not None else []
        else:
            blocks = self.store.chunks.list_chunks_for_ref(ref.id, pos_range=(lo, hi))
        if not blocks:
            raise NotFound(
                f"no blocks in {ref.slug} for range ~{lo}..{hi}",
                next=f"get(id='{_pa(ref)}', view='toc')",
            )

        # Figure-and-caption coalescing: when a single-block request
        # lands on an image-only block, fetch the next block too so the
        # caller sees the caption in the same response. Without this an
        # agent gets just ``![](_page_19_Figure_1.jpeg)`` — no number,
        # no caption, and a relative URL that nothing serves.
        # (MCP critic MAJOR — figure block returns image marker with no
        # caption.)
        if len(blocks) == 1 and lo == hi and _is_image_only_block(blocks[0].text):
            tail = self.store.chunks.list_chunks_for_ref(
                ref.id, pos_range=(hi + 1, hi + 1)
            )
            if tail and _looks_like_caption(tail[0].text):
                blocks = [*blocks, *tail]
                hi = tail[0].ord

        lines: list[str] = []
        banner = _paper_banner(ref, store=self.store)
        if banner:
            lines.append(banner)
            lines.append("")
        pointers = self._figure_pointer_lines(ref, blocks)
        for b in blocks:
            # head each chunk with its computed handle plus the ~ord it
            # hides (``pc<id> ~<ord>``, gr520328); the legacy ``slug~pos``
            # stays only for a kind with no chunk code.
            lines.append(
                f"# {handle_registry.chunk_head_label(ref.kind, b.id, b.ord, ref.slug)}"
            )
            lines.append(_render_block_body(ref.slug or "???", b.ord, b.text))
            lines.extend(pointers.get(b.ord, ()))
            # Citation-chunk-grounding Part 3 (the sidecar render): capped, expand-on-request
            # sidecars of this chunk's verified `cites` verdicts, when any
            # exist — outbound ("this chunk cites …", src_pos) and inbound
            # ("this chunk is cited by …", dst_pos) are two small sections,
            # not one table, since a chunk can carry both at once. Gated
            # behind the same dark switch as the inbound chase that produces
            # the data (nothing to show until it's on).
            from precis.workers.inbound_chase import inbound_chase_enabled

            if inbound_chase_enabled():
                from precis.handlers._citer_sidecar import (
                    render_cited_by_sidecar,
                    render_citer_sidecar,
                )

                sidecar = render_citer_sidecar(self.store, ref, b.ord)
                if sidecar:
                    lines.append(sidecar.lstrip("\n"))
                cited_by_sidecar = render_cited_by_sidecar(self.store, ref, b.ord)
                if cited_by_sidecar:
                    lines.append(cited_by_sidecar.lstrip("\n"))
            lines.append("")

        # Next: trailer — adjacent ranges + parent toc + citation.
        # Use the actual block count so the hint never points off the end.
        # Degenerate single-block ranges render as ``~N`` rather than
        # ``~N..N``: the MCP critic flagged ``~77..77`` as training the
        # wrong call shape — agents who saw a "range" hint then
        # extrapolated ``~5..5`` for unrelated singletons later. The
        # canonical single-block form is ``~N``. (Critic MINOR m6.)
        #
        # Single-block reads also widen the forward suggestion into a
        # range so a "next chunk" hint doesn't train a linear ~N → ~N+1
        # → ~N+2 sequential scan (observed pattern: agent reading
        # gerfen2011~13 through ~21 one-by-one across ~10 LLM turns
        # @ ~3min/turn when a single ~13..21 range read would have
        # finished in one).  The promoted "navigate via TOC" hint
        # comes first in single-block mode, since paging-by-block is
        # almost never the right strategy when scanning a paper.
        total = self.store.chunks.count_chunks(ref.id)
        nav: list[tuple[str, str]] = []
        single_block = lo == hi

        # In single-block mode, lead with the two structural reads
        # that are almost always more useful than paging linearly:
        #
        # 1. In-paper semantic search — when looking for a specific
        #    quote/section, scoped search beats reading sequentially.
        #    The same fused lexical+embedding index used by
        #    cross-paper search applies here, just narrowed to one
        #    paper.
        # 2. TOC — structural map of the whole paper, ~50 lines.
        #
        # In range mode these stay available but at lower priority
        # than the next/prev range hints.
        if single_block:
            nav.append(
                (
                    f"search(kind='{ref.kind}', q='your query', scope='{_pa(ref)}')",
                    "search inside this paper "
                    "(fused lexical+embedding) - usually beats paging",
                )
            )
            nav.append(
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "TOC - structural map of the paper",
                )
            )

        # Forward read is always a range — never advertise a bare
        # ``~N+1`` single-block hint. Single-block reads widen to a
        # 5-block window; range reads widen to the same size as the
        # current read. Backward navigation ("previous chunk") and
        # range-scoped TOC are dropped: the former is rarely the
        # right next move when reading forward, and a TOC scoped to
        # a small range usually has at most one section header — both
        # waste tokens in every chunk response.
        if hi + 1 < total:
            span = 5 if single_block else (hi - lo + 1)
            n_next = min(span, total - 1 - hi)
            # Absolute range off the same record handle as the trailer's
            # other hints (``pa<id>~26..28``) — a reader who just fetched
            # ``~23..25`` can verify it at a glance. The relative form this
            # replaces (``pc<id>+1..3``) was arithmetically equivalent but
            # anchored on an opaque chunk id, and read as off-by-something
            # even to a careful human; a hint must be checkable from the
            # response it rides on. ``resolve_relative`` still accepts the
            # relative shape as input — it's just no longer advertised.
            nxt_lo, nxt_hi = hi + 1, hi + n_next
            hint_id = (
                f"{_pa(ref)}~{nxt_lo}..{nxt_hi}"
                if nxt_hi > nxt_lo
                else f"{_pa(ref)}~{nxt_lo}"
            )
            nav.append(
                (
                    f"get(id='{hint_id}')",
                    f"next {n_next} chunks" if n_next > 1 else "next chunk",
                )
            )
        if not single_block:
            # Range mode: full TOC is the structural escape hatch.
            nav.append(
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "see the full TOC",
                )
            )
        if self.spec.corpus_role == "evidence":
            nav.append(
                (
                    f"get(id='{_pa(ref)}', view='bibtex')",
                    "get the BibTeX entry",
                )
            )
        body = "\n".join(lines).rstrip() + render_next_section(nav)
        return Response(body=body)

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # F3: single source of truth for paper views. Was previously
        # a hand-curated subset that contradicted ``_SUPPORTED_VIEWS``
        # — ``slug`` was advertised here but had no dispatch arm, and
        # ``ris``/``endnote``/``health`` were dispatched but not
        # advertised, so the agent couldn't discover them. Mirroring
        # ``_SUPPORTED_VIEWS`` directly removes the drift.
        return list(_SUPPORTED_VIEWS)

    def chunks_for_toc(self, ref: Any) -> ChunksForToc:
        """Adapter for the generic TOC renderer.

        Fetches every block of the paper (with embeddings) once,
        plus the H1/H2 structure detected by
        :func:`_paper_toc.detect_heading`. The TOC renderer caches
        on ref_id + chunker/embedder versions, so this method's
        cost amortises across repeated TOC views of the same paper.
        """
        from precis.handlers._paper_toc import detect_heading

        blocks = self.store.chunks.list_chunks_for_ref(ref.id, with_embedding=True)
        if not blocks:
            return ChunksForToc(
                chunks_text=(),
                embeddings=None,
                h2_boundaries=(),
            )
        # Sort by pos to guarantee reading order.
        blocks = sorted(blocks, key=lambda b: b.ord)
        chunks_text = tuple(b.text for b in blocks)
        # Canonical positions = block.ord so TOC handles (slug~N)
        # resolve via ``get(id='slug~<pos>')``. Skipping this would
        # leave handles using list indices and break search-hit
        # cluster lookups when block.ord has gaps.
        positions = tuple(b.ord for b in blocks)

        # Per-block embeddings — None when any block lacks one
        # (mixed corpus or partial reingest). The renderer falls
        # back to H2 / flat listing when embeddings is None.
        if all(b.embedding is not None for b in blocks):
            embeddings: tuple[tuple[float, ...], ...] | None = tuple(
                tuple(b.embedding) for b in blocks if b.embedding is not None
            )
        else:
            embeddings = None

        # H2 boundaries: detect headings in each block, then walk
        # to assign each H1/H2 a (start, end) span. We treat both
        # H1 and H2 as section markers for TOC purposes — the
        # generic renderer just needs "where do natural sections
        # start and end" not the H1-vs-H2 hierarchy.
        #
        # Filter out journal-template "headings" (``PAPER``, ``View
        # Article Online``, ``Broader context``, ``Article info``,
        # …). These come from the markdown ingester picking up
        # journal page chrome as H1/H2 — they're not real sections
        # and they confuse the TOC's H2-mode policy (one of them
        # often expands to cover the entire body because the real
        # body sections aren't marked with markdown headings).
        # Round-2 picky #3 / cai23 verification 2026-05-31.
        headings: list[tuple[int, str]] = []
        for b in blocks:
            h = detect_heading(b)
            if h is None or h.level not in (1, 2):
                continue
            if _is_journal_template_heading(h.title):
                continue
            headings.append((b.ord, h.title))

        h2_boundaries: list[tuple[int, int, str]] = []
        for i, (start, title) in enumerate(headings):
            end = headings[i + 1][0] - 1 if i + 1 < len(headings) else blocks[-1].ord
            h2_boundaries.append((start, end, title))

        return ChunksForToc(
            chunks_text=chunks_text,
            embeddings=embeddings,
            h2_boundaries=tuple(h2_boundaries),
            positions=positions,
            chunker_version=_PAPER_CHUNKER_VERSION,
            embedder_name=getattr(self.embedder, "model", "unknown"),
            embedder=self.embedder,
        )

    def _render_summaries(self, ref: Ref) -> Response:
        """``view='summaries'`` — per-chunk summary list for the whole body.

        One row per body chunk: its ``ord`` handle, the ``llm-v1`` summary
        (``chunk_summaries``), and the KeyBERT keyword string. This is the
        agent-surface twin of the web reader's Semantic/Keyword rapid-nav
        list (both read :meth:`Store.chunk_llm_summaries_for_ref`).

        The ``summary`` column is often empty — ``llm_summarize`` coverage
        is a deliberate trickle — so ``keywords`` is the always-present
        fallback the reader falls back to. For a clustered overview use
        ``view='toc'``; for a chunk's full text use ``get(id='pa<id>~N')``.
        """
        summaries = self.store.chunks.chunk_llm_summaries_for_ref(ref.id)
        if not summaries:
            return Response(
                body=(
                    f"# {_pa(ref)} — no body chunks to summarise\n\n"
                    "The chunker hasn't produced any body chunks for this paper."
                )
            )
        rows = [
            {
                "handle": f"{_pa(ref)}~{g['ord']}",
                "summary": g["summary"] or "—",
                "keywords": g["keywords"] or "—",
            }
            for g in summaries
        ]
        n_summ = sum(1 for g in summaries if g["summary"])
        head = (
            f"# {_pa(ref)} summaries — {len(rows)} chunks, {n_summ} with an llm summary"
        )
        table = render_agent_table(rows, schema=["handle", "summary", "keywords"])
        body = f"{head}\n\n{table}"
        banner = _paper_banner(ref, store=self.store)
        if banner:
            body = f"{banner}\n\n{body}"
        return Response(body=body)

    def _render_toc(
        self,
        ref: Ref,
        *,
        scope: tuple[int, int] | None,
    ) -> Response:
        """Render the smart TOC, optionally scoped to a block range.

        Phase B integration 2026-05-31: replaced the H1/H2-only
        ``build_toc`` renderer with the unified
        :mod:`precis.utils.toc` renderer (TextTiling-style embedding
        segmentation, H2-first fallback, per-segment RAKE, shared-
        phrases footer, abbreviation legend). The old ``build_toc``
        / ``render_toc`` path is retained for callers that need
        explicit H1-hierarchy rendering — see git log.
        """
        # Dynamic clustering at request time over per-chunk keywords
        # (``chunks.keywords``, populated by the chunk_keywords worker).
        # See :mod:`precis.utils.toc_db` for the algorithm.
        body = render_from_store(
            store=self.store,
            ref_id=ref.id,
            # emit the universal record handle (``pa<id>``) so
            # every row + drill-in hint is a copy-pasteable get id. The
            # legacy ``kind:slug~pos`` form was unparseable on input.
            handle=_pa(ref),
            kind=self.spec.kind,
            scope=scope,
            # Only the top-level (unscoped) TOC carries the readiness
            # line — a drill-down sub-TOC is the same document, so
            # re-querying the same corpus-wide embed/summarize backlog
            # on every recursive drill would be redundant DB load for
            # a number that hasn't changed.
            readiness_line=_toc_readiness_line(self.store, ref.id)
            if scope is None
            else None,
        )
        banner = _paper_banner(ref, store=self.store)
        if banner:
            body = f"{banner}\n\n{body}"
        return Response(body=body)

    def _render_list_papers(self) -> Response:
        # Cap the page at 50 — production corpora can be 1000s of papers
        # and a flat dump blows the agent's context. We expose the total
        # count and a search affordance so the agent has somewhere to go.
        limit = 50
        refs = self.store.list_refs(kind=self.spec.kind, limit=limit)
        total = self.store.count_refs(kind=self.spec.kind)
        if not refs:
            return Response(body=self._empty_corpus_message)
        suffix = "" if total <= limit else f" of {total}"
        # Surface total corpus depth so the agent doesn't have to
        # estimate chunk volume from per-paper counts (#38683).
        total_chunks = self.store.chunks.count_chunks_for_kind("paper")
        lines = [
            f"# {len(refs)} paper{'s' if len(refs) != 1 else ''}{suffix}"
            f"  ({total_chunks} chunks)"
        ]
        for r in refs:
            year = (r.meta or {}).get("year") or ""
            # Run titles through the JATS/entity cleanup before
            # excerpting — otherwise a title like
            # ``Cu/ZnO<sub>x</sub>`` lands in the list verbatim and
            # any LLM reading it copies the markup back into prose.
            # (MCP critic MINOR — list view leaks raw HTML/JATS.)
            preview = _excerpt(title_md(r.title), limit=80)
            yr = f"  ({year})" if year else ""
            lines.append(f"  {_pa(r):<30}{yr}  {preview}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"search(kind='{self.spec.kind}', q='your topic')",
                    "find a specific paper by topic",
                ),
                (
                    # ``'pa<id>'`` was an unresolvable placeholder — every
                    # row above already prints a real handle; interpolate
                    # the first one (same fix _paper_search.py's
                    # equivalent affordance already applies) so the
                    # advertised call actually executes.
                    f"get(id='{_pa(refs[0])}')",
                    "open one paper from the list (paste any handle above)",
                ),
            ]
        )
        return Response(body=body)

    #: Queryless-browse sort keys → ``Store.list_refs`` ``order_by`` and the
    #: ``meta`` key a date sort annotates each row with. ``sort`` reaches
    #: this method only from kinds that accept it (cfp); paper's search
    #: never passes one.
    _BROWSE_SORTS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "recency": ("updated_desc", None),
        "deadline": ("deadline_asc", "deadline"),
        "opens_on": ("opens_on_asc", "opens_on"),
    }

    def _render_tags_only_search(
        self,
        *,
        tags: list[str] | None,
        page: int,
        page_size: int,
        sort: str | None = None,
        include_ref_ids: list[int] | None = None,
        exclude_ref_ids: list[int] | None = None,
        exclude: list[str] | None = None,
        after: int | str | None = None,
        before: int | str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> Response:
        """Filter-only enumeration for ``search(kind=..., tags=[...])``.

        No q= means no lex/semantic ranking to fuse — this lists live
        refs matching the tag filter, most-recently-updated first, the
        same shape ``_numeric_ref.py``'s ``_list_by_tags`` gives the
        numeric-ref kinds. Kept separate from :meth:`_render_list_papers`
        (unfiltered) so the tag filter and its "N of TOTAL" count stay
        exact — this refs.count(*) doesn't share the unfiltered path's
        chunk-count callout, which isn't tag-scoped.

        The dispatch-injected id channels and the date bounds are
        honoured too (r2 S2 — a queryless listing must never drop a
        filter silently): ``include_ref_ids``/``exclude_ref_ids``
        (cited=/hubbed=/uncited=), ``exclude=``, publish-year
        ``after=``/``before=`` and the ``since=``/``until=`` created
        window all intersect the listing with an honest total.

        ``sort`` names a :attr:`_BROWSE_SORTS` key; a date sort annotates
        each row with that ``meta`` date and a missing date reads
        ``(no <key>)`` (undated rows sort last). ``tags=None`` enumerates
        the whole kind — only reachable where the kind's own ``search``
        allows a bare browse (cfp).
        """
        kind = self.spec.kind
        normalized_tags = Tag.normalize_filter(tags, kind=kind)
        bf = self._resolve_browse_filters(
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
            exclude=exclude,
            after=after,
            before=before,
            since=since,
            until=until,
        )
        order_by, date_key = self._BROWSE_SORTS.get(
            sort or "recency", self._BROWSE_SORTS["recency"]
        )
        page_size = max(1, int(page_size))
        offset = max(0, (int(page) - 1) * page_size)
        filters: dict[str, Any] = {
            "include_ref_ids": bf["include_ref_ids"],
            "exclude_ref_ids": bf["exclude_ref_ids"],
            "year_from": bf["year_from"],
            "year_to": bf["year_to"],
            "created_after": bf["since"],
            "created_before": bf["until"],
        }
        refs = self.store.list_refs(
            kind=kind,
            tags=normalized_tags,
            limit=page_size,
            offset=offset,
            order_by=order_by,
            **filters,
        )
        total = self.store.count_refs(kind=kind, tags=normalized_tags, **filters)
        if not refs:
            scope = f" tagged {normalized_tags}" if normalized_tags else ""
            body = f"no {kind} entries{scope}"
            body += render_next_section(
                [
                    (
                        f"search(kind='{kind}', q='your topic')",
                        "drop the tag filter and search by topic instead",
                    ),
                ]
            )
            return Response(body=body)
        suffix = "" if total <= len(refs) and page == 1 else f" of {total}"
        tag_scope = f" tagged {normalized_tags}" if normalized_tags else ""
        sort_scope = f" by {sort}" if sort and sort != "recency" else ""
        lines = [
            f"# {len(refs)} {kind}{'s' if len(refs) != 1 else ''}"
            f"{tag_scope}{sort_scope}{suffix}"
        ]
        for r in refs:
            year = (r.meta or {}).get("year") or ""
            preview = _excerpt(title_md(r.title), limit=80)
            if date_key:
                day = (r.meta or {}).get(date_key)
                when = f"  {date_key} {day}" if day else f"  (no {date_key})"
            else:
                when = f"  ({year})" if year else ""
            lines.append(f"  {_pa(r):<30}{when}  {preview}")
        body = "\n".join(lines)
        next_ops: list[tuple[str, str]] = []
        if offset + len(refs) < total:
            next_ops.append(
                (
                    f"search(kind='{kind}', tags={normalized_tags!r}, page={page + 1})",
                    "next page",
                )
            )
        next_ops.append(
            (
                f"get(id='{_pa(refs[0])}')",
                "open one paper from the list (paste any handle above)",
            )
        )
        body += render_next_section(next_ops)
        return Response(body=body)


# ---------------------------------------------------------------------------
# Slug + chunk parsing
# ---------------------------------------------------------------------------

# Slugs are lowercase alphanumeric + hyphens. The `~` introduces a chunk
# selector; the rest of the string is parsed as a path of `view/sub`
# segments.
_SLUG_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)(.*)$")
_RANGE_RE = re.compile(r"^(\d+)(?:\.\.|-)(\d+)$")
# Single chunk selector. A negative ord (``-1``) addresses a synthetic
# card-variant chunk (title/authors/abstract/combined — gr244679's
# title-match promotion and search's chunk-handle rendering both emit
# these), so the leading sign is accepted here even though card ords
# have no "range" grammar (``_RANGE_RE`` stays positive-only — cards
# aren't a sequence you page through). gr334152.
_CHUNK_RE = re.compile(r"^(-?\d+)$")

# The one DOI pattern lives in ``precis.store._identifiers_ops``
# (gr516333 — ``_paper_search`` imports it lazily from there). DOI-form
# ids can't be routed through ``_SLUG_RE`` — the suffix can legally
# contain slashes and dots, so the regex would try to split the DOI on
# ``/`` as a view path and fail. Chunk selectors (``~38``) still attach
# to DOI-form ids via the pattern's group 2; view paths (``/abstract``)
# do not — use the ``view=`` kwarg instead, because we can't safely
# disambiguate ``/abstract`` as "view=abstract" vs "DOI suffix
# /abstract".

_VIEW_PATH_ALIASES: dict[tuple[str, ...], str] = {
    ("cite", "bib"): "bibtex",
    ("cite", "bibtex"): "bibtex",
    ("cite", "ris"): "ris",
    ("cite", "endnote"): "endnote",
    ("abstract",): "abstract",
    ("toc",): "toc",
    ("summaries",): "summaries",
    ("bibtex",): "bibtex",
    ("ris",): "ris",
    ("endnote",): "endnote",
}


# ── Journal-template heading filter ────────────────────────────────
#
# The markdown ingester (marker / PDFs → markdown) sometimes promotes
# page chrome to H1 / H2: an article-type label like ``PAPER`` at the
# top of a journal page, ``View Article Online`` from the side nav,
# ``Broader context`` from a publisher sidebar, ``Article info``
# blocks. None of these are real paper sections. They mislead the
# TOC's H2-mode policy — typically one of them then expands to cover
# the entire body because the actual Introduction / Methods / Results
# aren't marked with markdown headings, and the agent ends up with a
# TOC labelled "Broader context" for 85 chunks of unrelated body.
#
# Filter is a small allow-deny: known template strings + an "all-caps
# short word" rule for the ``PAPER`` / ``ARTICLE`` / ``BRIEF`` class.
# Real sections are almost never single uppercase words, so the rule
# is conservative.

#: Case-insensitive exact-match strings that are journal-template
#: chrome, not real section headings. Extend as new patterns appear.
_JOURNAL_TEMPLATE_HEADINGS: frozenset[str] = frozenset(
    {
        # Article-type labels
        "paper",
        "article",
        "review",
        "review article",
        "research article",
        "editorial",
        "communication",
        "letter",
        "news",
        "perspective",
        "news & views",
        # Journal page nav / chrome
        "view article online",
        "article info",
        "article information",
        "article history",
        "article menu",
        "cite this article",
        "how to cite",
        "download pdf",
        "download citation",
        "metrics",
        "altmetric",
        "open access",
        # Date stamps that sometimes get heading-promoted
        "received",
        "accepted",
        "revised",
        "published",
        "published online",
        # Publisher sidebars
        "broader context",
        "graphical abstract",
        "highlights",
        "key points",
        # Footer chrome
        "permissions",
        "reprints",
        "supporting information",
        "supplementary information",
        "supplementary material",
        "this journal is",
    }
)


_RETRACTION_LABELS: dict[str, str] = {
    "retracted": "RETRACTED",
    "expression_of_concern": "EXPRESSION OF CONCERN",
    "corrected": "CORRECTED",
}


def _toc_readiness_line(store: Store, ref_id: int) -> str | None:
    """``readiness: embedded N/M · summarised N/M`` for ``get(kind='paper',
    view='toc')`` (read-for-question loop, slice 4 / docs/backlog/
    embed-status-hint-three-state.md) — lets a caller that just edited a
    paper's chunks poll "is my edit embedded/summarised yet" instead of
    guessing or re-searching blindly.

    ``None`` when the paper has no body chunks yet (nothing to report —
    :func:`~precis.utils.toc_db._empty_body` already says so). Body chunks
    only (``ord >= 0``). The two fractions have independent denominators —
    ``embedded``'s is :func:`~precis.workers.embed.eligible_chunk_count`
    (excludes ``EmbedHandler.skip_chunk_kinds`` + ``no_index``),
    ``summarised``'s is
    :func:`~precis.workers.llm_summarize.eligible_chunk_count` (excludes
    ``SKIP_KINDS``, the ``[MIN_CHUNK_CHARS, MAX_CHUNK_CHARS]`` window, and
    ``no_index``) — each matches the predicate its own "not done" count
    uses, so a structurally-ineligible chunk (a ``table``, or a stub below
    ``MIN_CHUNK_CHARS``) never reads as silently "done" in a denominator it
    can't actually satisfy. ``· failed N`` is appended only when the
    scoped embed count reports a genuine failure
    (:func:`~precis.workers.embed.failed_embedding_count`) — most papers
    show none. Counted entirely in SQL — no chunk rows are fetched here.
    """
    from precis.workers.embed import (
        eligible_chunk_count as eligible_embed_chunk_count,
    )
    from precis.workers.embed import failed_embedding_count, unembedded_chunk_count
    from precis.workers.llm_summarize import (
        eligible_chunk_count as eligible_summary_chunk_count,
    )
    from precis.workers.llm_summarize import unsummarized_chunk_count

    if store.chunks.count_chunks(ref_id) == 0:
        return None
    with store.pool.connection() as conn:
        eligible_embed = eligible_embed_chunk_count(conn, ref_id=ref_id)
        not_embedded = unembedded_chunk_count(conn, ref_id=ref_id)
        failed = failed_embedding_count(conn, ref_id=ref_id)
        eligible_summary = eligible_summary_chunk_count(conn, ref_id=ref_id)
        not_summarised = unsummarized_chunk_count(conn, ref_id=ref_id)
    embedded = eligible_embed - not_embedded
    summarised = eligible_summary - not_summarised
    line = (
        f"readiness: embedded {embedded}/{eligible_embed} · "
        f"summarised {summarised}/{eligible_summary}"
    )
    if failed:
        line += f" · failed {failed}"
    return line


def _retraction_banner(ref: Ref) -> str | None:
    """One-line warning banner for retracted / EoC / corrected papers.

    Returns ``None`` when ``ref.retraction_status`` is unset. Otherwise
    returns a single Markdown line including the status label, a date
    when ``retracted_at`` is populated, and a pointer to the provenance
    handler for full notice details. The banner is meant to be the
    first line of any paper view (overview / TOC / chunks).
    """
    status = (ref.retraction_status or "").strip().lower()
    if not status:
        return None
    label = _RETRACTION_LABELS.get(status, status.upper())
    parts = [f"> [!] **{label}**"]
    when = ref.retracted_at
    if when is not None:
        parts.append(f"({when.date().isoformat()})")
    reason = (ref.retraction_reason or "").strip()
    if reason:
        parts.append(f"— {_clean_inline_text(reason)}")
    doi = (ref.meta or {}).get("doi") if ref.meta else None
    if doi:
        parts.append(
            f"— see `get(kind='provenance', id={str(doi)!r})` for the full notice."
        )
    else:
        parts.append("— see the provenance handler for the full notice.")
    return " ".join(parts)


def _paper_banner(ref: Ref, *, store: Store | None = None) -> str | None:
    """Combined banner block for the top of a paper view: the retraction
    banner, then (when ``meta.glyph_health`` is suspected) a glyph-damage
    line in the same ``> [!]`` style, then a one-page-PDF coverage line
    (gr372863). ``None`` when none applies.

    The one-page line corroborates via :meth:`Store.is_one_page_preview`
    when a store is passed — the numbered-page span alone is not enough:
    the physical page count and the body volume must also say "preview"
    (gr514187). Without a store it falls back to the raw span check."""
    lines = [_retraction_banner(ref)]
    caveat = glyph_caveat(ref.meta)
    if caveat:
        lines.append(f"> [!] **GLYPH-DAMAGED SOURCE** — {caveat}")
    one_page = (
        store.is_one_page_preview(ref) if store is not None else ref.is_one_page_pdf
    )
    if one_page:
        lines.append(f"> [!] **{ONE_PAGE_COVERAGE_NOTE}**")
    out = [ln for ln in lines if ln]
    return "\n".join(out) if out else None


def _is_journal_template_heading(title: str) -> bool:
    """True when ``title`` is journal-template chrome, not a real section.

    Three rules, in order:

    1. Exact (case-insensitive) match against
       :data:`_JOURNAL_TEMPLATE_HEADINGS` — known offenders from the
       major chemistry / biology / physics publishers.
    2. Single uppercase word ≤ 8 chars (``PAPER``, ``ARTICLE``,
       ``NEWS``, ``BRIEF``). Real H1/H2 section titles are almost
       never a single short ALL-CAPS word; when they are, the cost
       of dropping one real section beats the cost of admitting
       the chrome.
    3. Empty / whitespace-only.

    Otherwise pass through.
    """
    if not title or not title.strip():
        return True
    raw = title.strip()
    if raw.lower() in _JOURNAL_TEMPLATE_HEADINGS:
        return True
    if raw.isupper() and raw.isalpha() and " " not in raw and 2 <= len(raw) <= 8:
        return True
    return False


def _render_citation_candidates(citation: str, cands: list[dict[str, Any]]) -> str:
    """Candidate list for a citation string that did not resolve to exactly
    one full-confidence DOI. Nothing was minted."""
    if not cands:
        return (
            f"No Crossref match for citation {citation!r}; nothing minted. "
            "Try the title: search(kind='semanticscholar', q='<title>')."
        )
    lines = [
        f"No single confident match for citation {citation!r}; nothing "
        "minted. Candidates (confidence 0-1):"
    ]
    for c in cands:
        lines.append(
            f"- doi:{c['doi']}  conf={c['confidence']:.2f}  "
            f"{c['first_author'] or '?'} {c['year'] or '?'} — "
            f"{c['journal']} {c['volume'] or ''} {c['page'] or ''} — "
            f"{c['title']}"
        )
    lines.append(
        f"Pick one: put(kind='paper', doi='{cands[0]['doi']}')  "
        "(substitute the right DOI)"
    )
    return "\n".join(lines)


def resolve_paper_slug(
    store: Store, id: str | int, *, kind: str = "paper"
) -> tuple[str, int]:
    """Coerce a paper id to a ``(slug, ref_id)`` pair — the module-level home
    of what ``PaperHandler._resolve_paper_slug`` used to do inline, shared
    with ``precis paper fetch-si``.

    Accepts a numeric ``ref_id`` (the web addresses papers by id, e.g. the
    triage queue's "Clear flag" → ``tag`` and the detail page's link ops),
    a slug, or a DOI/identifier form. Slugs are never all-digits, so the
    numeric branch is unambiguous — and it must come first, because
    :func:`resolve_live_slug_ref` stringifies its id and would otherwise
    look ``"5822"`` up as a *cite_key* (a guaranteed miss that raised
    ``NotFound`` and silently no-op'd the web tag ops).

    Rejects chunk selectors and path views — link/tag ops live at the ref
    level only. Raises ``BadInput`` (selector present) or ``NotFound``
    (slug unknown) so the caller can let those propagate.
    """
    if isinstance(id, int) or (isinstance(id, str) and id.strip().isdigit()):
        ref_id = int(id)
        ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
        if ref is None or ref.kind != kind:
            raise NotFound(
                f"{kind} id={ref_id} not found",
                next=f"search(kind='{kind}', q='...') to find existing",
            )
        return ref.slug or str(ref_id), ref_id
    raw_id = _maybe_resolve_doi(store, str(id))
    slug, chunk_spec, path_view = _parse_paper_id(raw_id, kind)
    reject_chunk_or_path_view(
        kind=kind,
        slug=slug,
        sel=chunk_spec,
        path_view=path_view,
    )
    ref = resolve_live_slug_ref(
        store,
        kind=kind,
        id=slug,
        next_hint=(f"search(kind='{kind}', q='...') to find existing slugs"),
        options=_suggest_paper_slugs(slug, store=store, kind=kind),
    )
    return slug, ref.id


def _maybe_resolve_doi(store: Store, raw: str) -> str:
    """Translate an identifier-form paper id to its slug form.

    When an agent hands us a paper identifier — a DOI
    (``10.1111/jnc.13915``, or the ``doi:``/``doi.org`` URL spellings
    callers actually paste), an arXiv id (``2401.00001``,
    ``cs.LG/0501001``, ``arxiv:``-prefixed), or any other scheme
    :func:`detect_identifier_scheme` recognises (S2 sha1, PubMed,
    OpenAlex…) — route it through ``ref_identifiers`` and substitute
    the slug so the rest of the pipeline (``_parse_paper_id``,
    :func:`resolve_live_slug_ref`, rendering) stays slug-addressed
    and unchanged (gr484234: only the bare ``10.`` shape used to
    resolve; every other accepted spelling fell through to "illegal
    character" or a misleading slug NotFound).

    Chunk selectors ride along: ``10.1111/jnc.13915~38`` →
    ``wang2020dopamine~38``. View paths are *not* supported on
    identifier-form ids — DOI suffixes can legally contain ``/``, so we
    can't disambiguate ``10.1000/foo/abstract`` between "DOI
    literal" and "DOI + view=abstract". The caller must use the
    ``view=`` kwarg alongside an identifier-form id.

    Non-identifier inputs are returned unchanged — this function is a
    no-op on slug-form ids. Bare digits (``_PUBMED_RE``-shaped) that
    resolve to nothing also pass through, so the slug resolver's
    digits→ref_id fallback keeps working.

    Raises :class:`NotFound` when the identifier is well-formed but no
    live paper carries it; the error carries an acquire hint rather
    than falling through to the generic ``"illegal character"``
    message the slug regex would emit.
    """
    base, sep, sel = raw.partition("~")
    if not base:
        return raw
    scheme = detect_identifier_scheme(base)
    if scheme is None:
        return raw
    slug = store.find_paper_slug_by_identifier(base)
    if slug is None and base.isdigit():
        # Pure digits are also the digits→ref_id fallback the slug
        # resolver admonishes (resolve_live_slug_ref's A1 branch) —
        # leave them there rather than report an unheld PubMed id.
        return raw
    if slug is None:
        ident = base
        if scheme == "doi":
            from precis.identity import normalize_doi

            ident = normalize_doi(base) or base
        mint = {
            "doi": f"put(kind='paper', doi='{ident}')",
            "arxiv": f"put(kind='paper', arxiv='{ident}')",
            "s2": f"put(kind='paper', identifier='s2:{ident}')",
            "pubmed": f"put(kind='paper', identifier='pubmed:{ident}')",
        }.get(
            scheme,
            "put(kind='paper', title='<title>')",
        )
        # Point at the two paths that actually work: mint the stub
        # directly, or register the claim now in acquisition mode
        # (``wants=``/``provenance=`` — a bare ``cited_in='doi:'`` is
        # rejected, the link parser has no 'doi' kind; gr311341).
        # ``get`` refuses rather than queueing the fetch itself because
        # the cascade can end on the paid OpenAlex Content leg (~$0.01 a
        # file; gr494831) — the hint names the cost so the caller makes
        # the call. Kept short: the MCP client already appends the tool
        # schema on isError, so a long hint lands twice (gr516710).
        raise NotFound(
            f"paper with {scheme.upper()} {base!r} not ingested",
            next=(
                f"{mint}  "
                "to mint a stub — get does not queue it: the fetch can "
                "end on the paid OpenAlex Content leg (~$0.01/file), so "
                "the spend is the caller's call. To register the claim "
                "meanwhile: put(kind='finding', title='<short claim>', "
                "body='<claim>', "
                f"wants=[{{{scheme!r}: '{ident}'}}], "
                "provenance='<chunk you read the claim in>')  "
                "— a bare 'doi:' cited_in is rejected. Held under "
                "another slug? search(kind='paper', q='<title>')."
            ),
        )
    return slug + ("~" + sel if sel else "")


def _supersede_chain_reaches(
    conn: Any, start_ref_id: int, needle_ref_id: int, *, cap: int = 8
) -> bool:
    """Walk raw ``meta.superseded_by`` pointers from ``start_ref_id``;
    True when the chain hits ``needle_ref_id`` within ``cap`` hops.

    Unlike :meth:`Store.follow_supersede` this ignores liveness — a
    live ref still carrying a merge stamp counts, because pointing a
    redirect at it would write a pointer cycle the reader-side follow
    would then spin on (its seen-set would return ``None``: a silent
    dead handle).
    """
    seen: set[int] = set()
    cur = start_ref_id
    for _ in range(cap):
        if cur == needle_ref_id:
            return True
        if cur in seen:
            return False
        seen.add(cur)
        row = conn.execute(
            "SELECT meta->>'superseded_by' FROM refs WHERE ref_id = %s",
            (cur,),
        ).fetchone()
        if row is None or row[0] is None:
            return False
        try:
            cur = int(row[0])
        except (TypeError, ValueError):
            return False
    return False


def _args_truthy(value: Any) -> bool:
    """``args={'retire': …}`` flag decode — ``'true'``/``'1'``/``'yes'``
    strings count as True, everything else (incl. ``'false'``) False.
    Mirrors :func:`precis.handlers._comment_timeline._truthy`."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


#: Inbound link relations that make a paper *cited-depended-on* — a live
#: ``cites``/``cited-by`` edge or a finding's ``awaits-evidence`` claim on
#: the stub. ``supersedes`` is handled separately (its src is by
#: construction retired, so a liveness filter would silence it).
_RETIRE_BLOCKING_RELATIONS = ("cites", "cited-by", "awaits-evidence")


def _retire_blockers(conn: Any, ref: Ref) -> list[str]:
    """Reasons ``ref`` may not be retired by the phantom-stub door
    (:meth:`PaperHandler._retire_stub`), as human strings; ``[]`` clears it.

    A phantom stub is title-only: no body chunks, no attached PDF, no
    external identifier (``ref_identifiers`` rows other than the
    ``cite_key`` — the slug is the internal id, not an external one), and
    nothing citing it: no live inbound cite edges, no ``[pa<id>]`` /
    ``§<cite_key>`` token in a live draft chunk (draft-scoped like the
    ``[fi<id>]`` delete guard — a token elsewhere is a mention, not a
    cite), no bib entry held on it, and no merge tombstone redirecting
    through it — the merge-survivor case, where each tombstone's
    redirect has to be moved to another survivor (``redirect_to``)
    before this ref can go.
    """
    blockers: list[str] = []
    row = conn.execute(
        "SELECT count(*) FROM chunks WHERE ref_id = %s "
        "AND retired_at IS NULL AND ord >= 0",
        (ref.id,),
    ).fetchone()
    if row and row[0]:
        blockers.append(f"{row[0]} live body chunk(s)")
    if ref.pdf_sha256:
        blockers.append("an attached PDF (pdf_sha256 is set)")
    ids = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind <> 'cite_key' ORDER BY id_kind",
        (ref.id,),
    ).fetchall()
    if ids:
        blockers.append(
            "external identifier(s): " + ", ".join(f"{k}:{v}" for k, v in ids)
        )
    citers = conn.execute(
        "SELECT s.kind, s.ref_id, x.relation "
        "FROM links x JOIN refs s ON s.ref_id = x.src_ref_id "
        "WHERE x.dst_ref_id = %s AND x.relation = ANY(%s) "
        "AND s.retired_at IS NULL",
        (ref.id, list(_RETIRE_BLOCKING_RELATIONS)),
    ).fetchall()
    # The survivor side of a merge: ``this -supersedes-> tombstone``
    # edges plus retired rows whose meta.superseded_by names this ref —
    # both spell "a redirect ends here". Retiring the survivor strands
    # each tombstone's resolution, so they must be moved (redirect_to on
    # the tombstone) first.
    victims = conn.execute(
        "SELECT d.kind, d.ref_id "
        "FROM links x JOIN refs d ON d.ref_id = x.dst_ref_id "
        "WHERE x.src_ref_id = %s AND x.relation = 'supersedes' "
        "AND d.retired_at IS NOT NULL",
        (ref.id,),
    ).fetchall()
    stamped = conn.execute(
        "SELECT kind, ref_id FROM refs WHERE retired_at IS NOT NULL "
        "AND meta->>'superseded_by' = %s",
        (str(ref.id),),
    ).fetchall()
    redirect_ends = {(k, i) for k, i in victims} | {(k, i) for k, i in stamped}
    if redirect_ends:
        blockers.append(
            "merge tombstone(s) redirect here: "
            + ", ".join(
                sorted(
                    handle_registry.try_format(k, i) or f"{k}{i}"
                    for k, i in redirect_ends
                )
            )
            + " — move each redirect to another survivor first "
            "(edit(args={'redirect_to': <other pa<M>>}) on the tombstone)"
        )
    if citers:
        blockers.append(
            "live inbound cite edge(s): "
            + ", ".join(
                f"{handle_registry.try_format(k, i) or f'{k}{i}'}:{rel}"
                for k, i, rel in citers
            )
        )
    cited_rows = conn.execute(
        "SELECT c.chunk_id, c.ref_id, r.kind "
        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE c.retired_at IS NULL AND c.ord >= 0 AND r.retired_at IS NULL "
        "AND r.kind = 'draft' AND c.ref_id <> %s AND c.text ~ %s "
        "ORDER BY c.ref_id, c.chunk_id LIMIT 10",
        (ref.id, _retire_cite_pattern(conn, ref)),
    ).fetchall()
    if cited_rows:
        docs = sorted({int(r[1]) for r in cited_rows})
        blockers.append(
            f"cited by token in {len(cited_rows)} live draft chunk(s) "
            f"across {len(docs)} draft(s)"
        )
    row = conn.execute(
        "SELECT count(*) FROM paper_bib_entries WHERE held_ref_id = %s",
        (ref.id,),
    ).fetchone()
    if row and row[0]:
        blockers.append(f"{row[0]} bib entrie(s) held on it")
    return blockers


def _retire_cite_pattern(conn: Any, ref: Ref) -> str:
    """Postgres regexp matching every chunk-text cite form that can name
    ``ref``: the ``[<kind><id>]`` handle, the handle inside a ``>``/``+``
    pin list, and the ``§<cite_key>`` sugar (cite_key read live — a stub
    without one simply gets no ``§`` alternative)."""
    row = conn.execute(
        "SELECT id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind = 'cite_key'",
        (ref.id,),
    ).fetchone()
    handle = handle_registry.format_handle(ref.kind, ref.id)
    alts = [rf"\[{handle}\]", rf"[,>+]{handle}[\],]"]
    if row is not None and row[0]:
        slug = re.escape(str(row[0]))
        alts.append(rf"§{slug}([^A-Za-z0-9_-]|$)")
    return "|".join(alts)


def _parse_paper_id(
    raw: str,
    kind: str = "paper",
) -> tuple[str, tuple[int, int] | None, str | None]:
    """Return (slug, chunk_range, view).

    ``kind`` is the caller-requested kind (``paper`` / ``cfp`` / …) so the
    error messages echo what the caller asked for rather than the internal
    ``paper`` handler name (gr48511).

    ``slug`` is mandatory. Both ``chunk_range`` and ``view`` may be set
    when the id carries both — e.g. ``slug~46..105/toc`` is the
    drill-down form (TOC scoped to that block range). For plain chunk
    selectors (``slug~38``) view is ``None``; for plain view paths
    (``slug/cite/bib``) chunk_range is ``None``.
    """
    # Friendly redirect for the cross-kind list-view shape. Numeric
    # kinds (memory, todo, ...) accept ``id='/recent'`` for their
    # listing, so a 7B caller learning the convention from one kind
    # naturally retries it on paper. The MCP critic flagged the
    # generic "invalid paper id" reply as a footgun — it sent the
    # caller down a slug-fixup detour when the actual fix is "drop
    # the id=, papers list via the bare get". (Critic MINOR #7.)
    if isinstance(raw, str) and raw.startswith("/"):
        raise BadInput(
            f"{kind} has no list view {raw!r} - list-view paths are "
            "specific to numeric kinds (memory/todo/anki/...)",
            next=(
                f"{kind} doesn't accept '/recent' - use the bare list shape: "
                f"get(kind='{kind}')"
            ),
        )
    m = _SLUG_RE.match(raw)
    if not m:
        raise BadInput(
            f"invalid paper id: {raw!r}",
            next="paper ids look like 'wang2020state' or 'wang2020state~38'",
        )
    slug, rest = m.group(1), m.group(2)
    # The slug regex is permissive at the right edge — `[a-z0-9][a-z0-9-]*`
    # matches the *prefix*, then `(.*)` swallows whatever's left. So
    # `nonexistent_paper_xyz` parses as slug='nonexistent' + rest='_paper_xyz'.
    # The rest then doesn't start with `~` or `/` and falls through to
    # the generic "unparseable" error at the bottom — which doesn't
    # name the actual rule. The MCP critic flagged this: a 7B model
    # using snake_case sees BadInput instead of NotFound and goes down
    # the wrong recovery branch. Catch the underscore case explicitly
    # before the chunk/view logic gets a chance. (Critic MINOR m3.)
    if rest and not rest.startswith(("~", "/")):
        first_bad = rest[0]
        if first_bad == "_":
            raise BadInput(
                f"paper slug contains '_' (illegal): {raw!r}",
                next=(
                    "paper slugs are lowercase a-z + digits + '-' only - "
                    "no underscores. Most slugs look like 'wang2020state'"
                ),
            )
        raise BadInput(
            f"paper slug contains illegal {first_bad!r}: {raw!r}",
            next="paper slugs match [a-z0-9-]+ (e.g. 'wang2020state')",
        )

    if not rest:
        return slug, None, None

    chunk_range: tuple[int, int] | None = None
    if rest.startswith("~"):
        # Split selector from optional view path: ``~46..105/toc``.
        sel_and_path = rest[1:]
        if "/" in sel_and_path:
            sel, _, path_part = sel_and_path.partition("/")
            rest_after_sel = "/" + path_part
        else:
            sel = sel_and_path
            rest_after_sel = ""

        rng = _RANGE_RE.match(sel)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                raise BadInput(
                    f"empty chunk range: {raw!r}",
                    next="ranges run lo..hi inclusive (e.g. '~3..7')",
                )
            chunk_range = (lo, hi)
        else:
            single = _CHUNK_RE.match(sel)
            if single:
                n = int(single.group(1))
                chunk_range = (n, n)
            else:
                raise BadInput(
                    f"unparseable chunk selector after ~: {sel!r}",
                    next="use '~N' for a single block or '~N..M' for a range",
                )

        if not rest_after_sel:
            return slug, chunk_range, None
        rest = rest_after_sel

    if rest.startswith("/"):
        parts = tuple(rest[1:].split("/"))
        view = _VIEW_PATH_ALIASES.get(parts)
        if view is None:
            # Specific hint for the figure family — the MCP critic
            # flagged ``slug/fig/N`` as failing silently with a
            # generic "unknown view" error. Surface the caption-only
            # workflow so the agent stops retrying. (Critic MAJOR #4.)
            if parts and parts[0] in ("fig", "figure", "figures"):
                raise BadInput(
                    f"figure view path {raw!r} not implemented",
                    options=list(_SUPPORTED_VIEWS),
                    next=(
                        "figure binaries aren't served - figures live as "
                        "legend blocks inside the body. Use view='toc' to "
                        "locate the figure number, then read the legend "
                        "block. See 'Figures' in precis-paper-help."
                    ),
                )
            raise BadInput(
                f"unknown view path: {raw!r}",
                options=list(_SUPPORTED_VIEWS),
                next="see precis-paper-help for the supported view paths",
            )
        return slug, chunk_range, view

    raise BadInput(
        f"unparseable paper id: {raw!r}",
        next="format: <slug> | <slug>~N | <slug>~N..M | <slug>/<view> | <slug>~N..M/<view>",
    )


# Author + citation + inline-markup helpers moved to
# ``precis.handlers._paper_format`` (2026-06-05). The symbols
# ``_author_names``, ``_format_authors``, ``_render_citation``,
# ``_clean_inline_text``, ``_latex_escape`` are imported at the top of
# this file; tests previously reaching them via
# ``precis.handlers.paper`` should import from ``_paper_format`` now.


# ---------------------------------------------------------------------------
# View aliasing + abstract sanitisation
# ---------------------------------------------------------------------------


# Kwarg ``view=`` accepts the same vocabulary as the id-path form so that
# ``view='cite/bib'`` and ``id='slug/cite/bib'`` resolve identically.
# Without this, an agent that copied an id like ``slug/cite/bib`` from a
# docstring and then split it into id+view ended up with an Unsupported
# error — the asymmetry the MCP critic flagged.
_VIEW_KWARG_ALIASES: dict[str, str] = {
    "bibtex": "bibtex",
    "ris": "ris",
    "endnote": "endnote",
    "abstract": "abstract",
    "toc": "toc",
    "summaries": "summaries",
    "cite/bib": "bibtex",
    "cite/bibtex": "bibtex",
    "cite/ris": "ris",
    "cite/endnote": "endnote",
}


_VIEW_NOOP_ALIASES: frozenset[str] = frozenset({"text", "body", "full"})


def _normalise_view(view: str | None) -> str | None:
    """Canonicalise the ``view`` argument.

    Accepts both bare names (``'bibtex'``) and slash-paths
    (``'cite/bib'``). Returns the canonical bare name. Unknown views
    pass through verbatim so the renderer can produce its own
    ``Unsupported`` error with the supported-options list.

    ``view='text'``, ``'body'``, ``'full'`` are treated as no-ops —
    they map to ``None``. Workers reach for ``view='text'`` as the
    natural way to ask for chunk bytes (``id='slug~13', view='text'``);
    rather than fight that mental model and emit ``Unsupported``,
    accept it as a synonym for "render the addressed scope using the
    default renderer". With a chunk selector this gives the chunk
    text; without one it gives the paper overview.
    """
    if view is None:
        return None
    if view in _VIEW_NOOP_ALIASES:
        return None
    return _VIEW_KWARG_ALIASES.get(view, view)


# JATS-stripping (``_strip_jats``) and figure-and-caption coalescing
# (``_is_image_only_block`` / ``_looks_like_caption`` /
# ``_render_block_body`` / ``_chunk_keywords_or_caption`` /
# ``_scrub_block_text``) moved to ``precis.handlers._paper_text``
# (2026-06-05). Imported at the top of this file; tests previously
# reaching them via ``precis.handlers.paper`` should import from
# ``_paper_text`` now.


__all__ = ["PaperHandler"]
