"""``precis_add()`` — single ingest entry point for the v2 schema.

Public API for the ingest pipeline. Wires the three producers
(``extract_paper``/``fetch_paper_by_doi``/``fetch_paper_by_arxiv``) to
the v2 INSERT cascade (:func:`precis.ingest.db_writer.write_paper`)
with idempotency checks via
:func:`precis.ingest.db_writer.probe_existing`.

Atomic: one successful ingest commits exactly one transaction; a
writer raise rolls it back with no rows landed.

Idempotent on two layers: **fast path** — for :class:`PdfInput`, the
cheap ``pdf_sha256`` is probed against ``ref_identifiers`` *before*
Marker runs (a hit short-circuits, saving ~30-60s/PDF; the probe's
short-lived connection is dropped before Marker, since extraction is
minutes of inference and the pool is sized for short transactions);
**slow path** — every identifier the pipeline assembled (paper_id,
DOI, arXiv, S2, content_hash, …) is probed again post-extraction,
catching "same paper, different bytes" the fast path misses.

Either hit yields ``IngestResult(inserted=False, ref_id=...)`` with
identifiers re-fetched from ``ref_identifiers`` (reflecting what's
actually stored, not what the pipeline freshly computed).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from precis.identity import make_pdf_sha256, normalize_doi
from precis.ingest.article_spread import mark_article_start
from precis.ingest.claim import Claim
from precis.ingest.db_writer import (
    PaperToWrite,
    probe_existing,
    register_aliases_and_maybe_upgrade,
    write_paper,
)
from precis.ingest.dedup import (
    _drop_cleared_loser_rows,
    _drop_dropped_loser_rows,
    _merge_cleared_markers,
    _merge_dropped_markers,
    inherit_missing_identity,
)
from precis.ingest.pdf_writer import PatchInfo, patch_pdf_metadata
from precis.ingest.pres import (
    PresWriteResult,
    extract_pres,
    write_pres,
)
from precis.store import Store
from precis.store.revision_context import trust_door
from precis.store.types import Tag
from precis.utils.title_merge import _first_family

log = logging.getLogger(__name__)

# NOTE: ``precis.ingest.pipeline`` imports are deferred into
# :func:`_build_paper` because that module pulls in the paper-extra
# deps (habanero, semanticscholar, rapidfuzz, pymupdf, marker-pdf).
# Keeping the import lazy here means ``precis serve`` /
# ``precis migrate`` / ``precis worker`` keep working on a bare
# install without the ``[paper]`` extra; only ``precis add`` /
# ``precis ingest --watch`` ever actually call into the pipeline and they
# fail with a clean ``ModuleNotFoundError`` at runtime if the
# extra is missing.

# ---------------------------------------------------------------------------
# Tagged-union input + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfInput:
    pdf_path: Path
    #: Open tags applied to the ref after ingest. Used by the watcher
    #: to forward ``inbox/.../tagging/<slug>/`` directory tokens as
    #: ``topic:<slug>`` and (for ``books/``) the synthetic
    #: ``subtype:book`` / ``topic:book`` pair. Applied on both the
    #: fresh-insert and the sha256-hit branches so re-dropping a PDF
    #: under a new tagging dir merges tags additively instead of
    #: silently no-op'ing.
    extra_tags: tuple[str, ...] = ()
    #: Stored kind for this ingest. Defaults to ``"paper"``. The CFP /
    #: requirements flow passes ``as_kind="cfp"`` so the *identical*
    #: Marker → chunks pipeline lands the document under the spec-role
    #: ``cfp`` kind instead of the citable ``paper`` kind (DRY — no
    #: separate extractor; only the ``refs.kind`` differs). Threaded
    #: into :class:`~precis.ingest.db_writer.PaperToWrite.kind` via
    #: :func:`_build_paper`.
    as_kind: str = "paper"
    #: Authoritative fold target from the OA-fetch sidecar
    #: (:mod:`precis.ingest.fetch_sidecar`). When set and still a live
    #: metadata-only stub (``pdf_sha256 IS NULL``), a PDF whose extracted
    #: identifiers *don't* dedup against any existing ref is folded into
    #: this stub in place — promoting it and keeping its good title/DOI —
    #: instead of minting a duplicate ref. ``None`` for manual drops (no
    #: sidecar), which fall back to identity re-derivation. Immune to the
    #: filename munging that breaks the ``cite_key``-stem match.
    fold_ref_id: int | None = None
    #: True when this PDF was fetched *alongside* a markup trigger for the
    #: same stub (gr161905) — set from the OA-fetch sidecar's
    #: ``printable_only`` flag. Skips Marker entirely: this PDF is only
    #: ever an attach-only printable, never a second, order-dependent body
    #: candidate. See :func:`precis.ingest.pipeline.extract_paper`.
    printable_only: bool = False
    #: Parent paper ``ref_id`` when this PDF is a supplementary-information
    #: file (sidecar ``role='supplement'``). Ingest then mints the PDF as its
    #: OWN ref (``pdf_role='supplement'``) linked to that parent — never folds
    #: into it, never registers the parent's DOI on it. See
    #: :func:`_ingest_supplement`.
    supplement_of: int | None = None
    #: Whole-slug request for a ``as_kind='datasheet'`` mint (gr520126) —
    #: e.g. ``datasheet_pull``'s ``<mfr> <mpn>``. Kebabed downstream; the
    #: writer's ``resolve_slug`` suffixes ``-2``/``-3`` on collision.
    #: Ignored for other kinds.
    slug_hint: str | None = None
    #: ``source`` / ``url`` / ``component_doi`` provenance from the sidecar,
    #: recorded on the minted ref's ``meta.si_parent``.
    supplement_info: dict[str, str] | None = None


@dataclass(frozen=True)
class DoiInput:
    doi: str


@dataclass(frozen=True)
class ArxivInput:
    arxiv_id: str


@dataclass(frozen=True)
class PresInput:
    """Local slide PDF → ``kind='pres'`` (one chunk per slide).

    ``slug_hint`` and ``title_hint`` override the defaults derived
    from the filename. Both are advisory — :func:`extract_pres`
    accepts them as starting points and the writer applies the
    ``-2``/``-3``/… suffix policy if the slug is already taken.

    ``extra_tags`` follows the same shape as :class:`PdfInput`: open
    tags pushed onto the ref after commit. The watcher uses this to
    forward ``inbox/.../tagging/<slug>/`` directory tokens.
    """

    pdf_path: Path
    slug_hint: str | None = None
    title_hint: str | None = None
    extra_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarkupInput:
    """Structured full text (JATS / Elsevier XML / arXiv HTML / LaTeX).

    Markup-first ingest (docs/backlog/dark-features-activation.md): the body
    chunks come from ``markup_path`` via
    :func:`precis.ingest.pipeline.extract_paper_from_markup` — **Marker
    is never run**.

    On a :class:`precis.ingest.markup.MarkupParseError` the markup ref is
    left claimable and returns ``None`` — but first
    :func:`_recover_markup_parse_failure` (gr161905) deterministically
    recovers via the companion PDF the OA fetcher dropped alongside this
    markup trigger: that PDF is tagged ``printable_only`` at fetch time
    (see :func:`precis.workers.fetch_oa._run_cascade`) so it never
    independently races Marker against this markup regardless of which
    host's watcher reaches it first — a parse failure here either clears
    that flag (if the PDF is still unprocessed) or OCRs the already-
    attached stored copy directly (if it already landed as a printable).
    """

    markup_path: Path
    #: One of :data:`precis.ingest.markup.MARKUP_FORMATS`.
    fmt: str
    #: Provenance URL of the markup, stamped into ``refs.meta``.
    source_url: str | None = None
    #: Same semantics as :class:`PdfInput.extra_tags`.
    extra_tags: tuple[str, ...] = ()
    #: Same semantics as :class:`PdfInput.as_kind`.
    as_kind: str = "paper"
    #: Same semantics as :class:`PdfInput.fold_ref_id` — the OA-fetch
    #: sidecar's authoritative stub target.
    fold_ref_id: int | None = None
    #: The OA-fetch sidecar's own ``id_kind → id_value`` map (gr453913),
    #: e.g. ``{"doi": ..., "arxiv": ...}`` — carried through from
    #: :class:`precis.ingest.fetch_sidecar.FetchSidecar.identifiers`.
    #: ``jats``/``elsevier_xml`` pull their DOI out of the document's own
    #: front matter, so they never needed this; ``arxiv_html``/``latex``
    #: have no in-document identifier and previously derived their arXiv
    #: id *exclusively* from ``source_url`` — which the watcher never
    #: sets — so every real ingest of those two formats raised
    #: ``MarkupParseError``. This is the fallback identity source
    #: :func:`precis.ingest.pipeline.extract_paper_from_markup` consults
    #: when the parser's own extraction came up empty. ``None`` for a
    #: manual drop with no sidecar.
    identifiers: dict[str, str] | None = None


PrecisAddInput = PdfInput | DoiInput | ArxivInput | PresInput | MarkupInput


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a :func:`precis_add` call.

    ``inserted=False`` means an idempotency hit — the paper (or one
    of its identifiers) was already in the DB and we returned the
    existing ``ref_id`` unchanged. ``inserted=True`` means the writer
    produced new rows in this call.

    For pres ingests, ``cite_key`` carries the pres slug (slug
    kinds uniformly store under ``id_kind='cite_key'`` in
    ``ref_identifiers``), ``paper_id`` is empty, and ``content_hash``
    is None. The ``kind`` field disambiguates so callers (notably
    the watcher) can route the on-disk move and the ingest.log
    status correctly.
    """

    ref_id: int
    inserted: bool
    paper_id: str
    pub_id: str | None
    cite_key: str
    pdf_sha256: str | None
    content_hash: str | None
    chunks_written: int
    identifiers: dict[str, str]
    kind: str = "paper"
    #: gr236139 — ``True`` when :func:`precis.ingest.pipeline.extract_paper`
    #: fell back from Marker to fitz page-level extraction for this PDF
    #: (Marker raised or timed out). Only ever set on a fresh insert — the
    #: dedup-hit branches never re-extract, so they default ``False``. The
    #: watcher uses this to raise a rate-limited ops alert (see
    #: :func:`precis.cli.watch._check_marker_fallback`).
    used_marker_fallback: bool = False
    #: gr236139 — ``True`` when the fitz fallback above produced *zero*
    #: body chunks on a PDF with at least one page — the silent-data-loss
    #: signature (an image-only/scanned PDF the fallback can't OCR).
    #: ``False`` whenever ``used_marker_fallback`` is ``False``. The
    #: watcher routes this case to ``errors/`` instead of reporting
    #: success and leaves the fetch sidecar unconsumed.
    fallback_empty_body: bool = False
    #: gr510690 — body-chunk presence on the target ref *after* this call:
    #: ``EXISTS chunks ord >= 0`` on the dedup-hit paths, the extraction's
    #: own ``ord >= 0`` count on a fresh insert. ``None`` on paths where
    #: body presence was not evaluated — the watcher's zero-body guard
    #: fires only on an explicit ``False``.
    ref_has_body: bool | None = None
    #: gr510690 — how many ``ord >= 0`` chunks this call wrote. Lets the
    #: watcher tell "this ingest populated a bodiless ref" (the file is
    #: the body source and belongs in the corpus) from a genuine
    #: duplicate re-drop on an already-bodied ref (attach-only → 0).
    body_chunks_written: int = 0
    #: gr519746 — ``True`` when this call promoted ``refs.pdf_sha256`` to
    #: this ingest's file (``register_aliases_and_maybe_upgrade``'s
    #: promotion leg: the ref carried no ``pdf_sha256``, or a bodiless
    #: re-point). Lets the watcher tell an *attach-only* drop — the file
    #: just became the ref's canonical printable and must take the
    #: cite_key corpus path — from a byte-different re-drop on a ref that
    #: already holds a PDF (a genuine duplicate → ``errors/duplicates``).
    pdf_promoted: bool = False


class MarkupTriggerSpent(Exception):
    """A markup trigger failed to parse and never will — the trigger
    file is *spent*, must be retired from the inbox, not retried.

    Raised by :func:`_ingest_markup` after a
    :class:`~precis.ingest.markup.MarkupParseError`, once the
    companion-PDF OCR recovery (:func:`_recover_markup_parse_failure`,
    gr161905) has run. Distinct from ``precis_add`` returning ``None``
    (**claim contention**, transient — the watcher leaves the file for
    the owning host to finish): this is a **terminal parse failure** —
    the same bytes fail identically every pass, so "leave it for retry"
    would strand the ``.xml`` trigger + sidecar in the inbox forever and
    re-scan on every backfill. The watcher catches this and moves the
    spent trigger out instead; the paper's body, if recoverable, was
    already folded from the companion PDF by the recovery step.

    Carries the source format for the watcher's log/error record.
    """

    def __init__(self, message: str, *, fmt: str = "") -> None:
        super().__init__(message)
        self.fmt = fmt


# ---------------------------------------------------------------------------
# precis_add — the public entry point
# ---------------------------------------------------------------------------


def precis_add(
    input: PrecisAddInput,
    *,
    store: Store,
    use_pdf2doi: bool = False,
    crossref_mailto: str = "",
    s2_api_key: str = "",
    marker_timeout_s: float | None = None,
) -> IngestResult | None:
    """Ingest one paper into the v2 schema.

    ``marker_timeout_s`` (P2-3) only affects :class:`PdfInput` (see
    :func:`precis.ingest.pipeline.extract_paper`); ``None`` (default)
    preserves the original in-process, unguarded Marker call.

    Dispatches on input type: :class:`PdfInput` runs Marker + the
    metadata cascade via :func:`precis.ingest.pipeline.extract_paper`
    (``pdf_sha256`` probed against ``ref_identifiers`` *before* Marker
    so a known file short-circuits); :class:`DoiInput` is a
    CrossRef-only fetch (:func:`~precis.ingest.pipeline.fetch_paper_by_doi`);
    :class:`ArxivInput` uses Semantic Scholar
    (:func:`~precis.ingest.pipeline.fetch_paper_by_arxiv`);
    :class:`PresInput` is a local slide PDF → ``kind='pres'``
    (:func:`precis.ingest.pres.extract_pres`, same ``pdf_sha256`` probe,
    ``subtype:slides``+``extra_tags`` applied post-commit).

    Returns ``None`` if another host already holds a
    :class:`precis.ingest.claim.Claim` on this PDF's ``pdf_sha256`` —
    the caller should leave the file in place. Never ``None`` for
    metadata-only inputs (``DoiInput``/``ArxivInput``), which are cheap
    and need no cross-host claim coordination.
    """
    # Fast path: PDF inputs get a pre-Marker probe on pdf_sha256.
    # The hash is bytes-cheap (~1 ms/PDF); the probe is one round
    # trip. A hit here skips both extraction and the slow-path
    # probe entirely — that's the whole point of the optimisation.
    if isinstance(input, PresInput):
        return _precis_add_pres(input, store=store)

    if isinstance(input, MarkupInput):
        return _precis_add_markup(input, store=store)

    if isinstance(input, PdfInput):
        pdf_sha256 = _compute_pdf_sha256(input.pdf_path)
        if pdf_sha256 is None:
            # File disappeared between watcher enqueue and now;
            # treat as a no-op so the caller doesn't fail loudly.
            return None
        with store.pool.connection() as conn:
            existing_ref_id = probe_existing(pdf_sha256=pdf_sha256, conn=conn)
        if existing_ref_id is not None and input.supplement_of is not None:
            # Same SI bytes seen before: reuse that ref (ensure its link to
            # the parent), never mint a second one and never reconcile it
            # against a stub (the SI's filename stem is not a cite_key).
            return _reuse_supplement(input, existing_ref_id, store=store)
        if existing_ref_id is not None:
            # gr510690 — a ``pdf_sha256`` hit is a genuine duplicate only
            # when the matched ref already carries body chunks. A bodiless
            # ref holding these bytes means the first ingest produced no
            # text; short-circuiting here would keep it bodiless forever
            # and send every re-upload to ``errors/duplicates/``. Fall
            # through to the claim + extraction instead: the slow-path
            # probe re-matches this ref and
            # ``register_aliases_and_maybe_upgrade`` fills its body.
            with store.pool.connection() as conn:
                existing_has_body = _ref_has_body(existing_ref_id, conn=conn)
            if not existing_has_body:
                log.info(
                    "precis_add: %s pdf_sha256 matches ref_id=%s which has "
                    "no body chunks — re-extracting to fill it rather than "
                    "short-circuiting as duplicate",
                    input.pdf_path.name,
                    existing_ref_id,
                )
        if existing_ref_id is not None and existing_has_body:
            # Fast-path hit: re-applying ``extra_tags`` is the watcher's
            # signal that re-dropping a known PDF under a different
            # ``tagging/`` dir should merge tags additively (rather than
            # silently no-op'ing because the sha is known).
            # A file named ``pa<id>`` whose bytes match a different live ref:
            # the named stub wins (the twin merges into it), gr479362.
            with store.pool.connection() as conn:
                existing_ref_id = _fold_into_named_stub(
                    store,
                    matched_ref_id=existing_ref_id,
                    file_stem=input.pdf_path.stem,
                    pdf_path=input.pdf_path,
                    conn=conn,
                )
                conn.commit()
            _apply_extra_tags(store, "paper", existing_ref_id, input.extra_tags)
            # A byte-identical re-fetch hits here before Marker runs; still
            # fold any orphan stub the fetch was for so it stops
            # re-qualifying (the slow path does the same post-extraction).
            with store.pool.connection() as conn:
                _reconcile_orphan_stub(
                    store,
                    survivor_ref_id=existing_ref_id,
                    file_stem=input.pdf_path.stem,
                    conn=conn,
                )
                conn.commit()
            with store.pool.connection() as conn:
                hit = _hit_result_from_db(existing_ref_id, conn=conn)
                stored_kind = _lookup_kind(existing_ref_id, conn=conn)
            return _with_kind(hit, kind=stored_kind)

        # Claim before the expensive work. Advisory lock auto-releases
        # if this host dies, so no stale-claim cleanup is needed.
        # If ``store.dsn`` is None (unit-test path that
        # builds Store from a pre-made pool), we degrade to no-claim
        # — single-host correctness is preserved by the file-system
        # mutex used by ``_PdfHandler._enqueue``.
        ingest = _ingest_supplement if input.supplement_of is not None else _ingest_pdf
        if store.dsn is None:
            return ingest(
                input,
                store=store,
                pdf_sha256=pdf_sha256,
                use_pdf2doi=use_pdf2doi,
                crossref_mailto=crossref_mailto,
                s2_api_key=s2_api_key,
                marker_timeout_s=marker_timeout_s,
            )

        with Claim(store.dsn, pdf_sha256) as claim:
            if not claim.acquired:
                log.info(
                    "precis_add: skipping %s — claimed by another host",
                    input.pdf_path.name,
                )
                return None
            return ingest(
                input,
                store=store,
                pdf_sha256=pdf_sha256,
                use_pdf2doi=use_pdf2doi,
                crossref_mailto=crossref_mailto,
                s2_api_key=s2_api_key,
                marker_timeout_s=marker_timeout_s,
            )

    # Metadata-only paths (DOI / arXiv) — no claim needed.
    return _ingest_metadata(
        input,
        store=store,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
    )


def _ref_has_body(ref_id: int, *, conn: Any) -> bool:
    """``EXISTS chunks ord >= 0`` — does the ref carry extracted body text?"""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0)",
        (ref_id,),
    ).fetchone()
    return bool(row and row[0])


#: ``ref_events.source`` of the PDF leg's "finished without a body" journal row.
PDF_NO_BODY_SOURCE = "ingest:pdf"


def _note_pdf_no_body(
    store: Store,
    ref_id: int | None,
    input: PdfInput,
    paper: Any,
    *,
    conn: Any,
) -> None:
    """Journal why a PDF ingest left the ref without body text (gr453860).

    The markup leg journals its outcome (:func:`_note_markup_outcome`); the
    PDF leg used to promote a stub (``pdf_sha256`` set) and stop, so a
    bodiless paper showed ``fetch_ok`` and then nothing. When the ref has no
    ``ord >= 0`` chunk after this ingest, append one ``no_body`` event with
    a reason: ``deferred_printable_only`` (Marker skipped by design for a
    companion PDF; transient, the markup leg should land the body),
    ``extraction_empty_fallback`` (Marker failed and the fitz fallback found
    no text) or ``extraction_empty`` (extractor returned no body blocks).
    ``heal:bodiless`` (:mod:`precis.ingest.paper_hygiene`) later judges the
    terminal cases. Best-effort: never fails an otherwise good ingest.
    """
    if ref_id is None:
        return
    try:
        from precis.store._body_predicate import has_body_sql

        row = conn.execute(
            f"SELECT {has_body_sql('r')} FROM refs r WHERE r.ref_id = %s",
            (int(ref_id),),
        ).fetchone()
        if row is None or row[0]:
            return
        if input.printable_only:
            reason = "deferred_printable_only"
        elif paper.meta.get("extract_fallback_empty"):
            reason = "extraction_empty_fallback"
        else:
            reason = "extraction_empty"
        store.append_event(
            int(ref_id),
            source=PDF_NO_BODY_SOURCE,
            event="no_body",
            payload={
                "reason": reason,
                "file": input.pdf_path.name,
                "pdf_sha256": paper.pdf_sha256,
            },
            conn=conn,
        )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (pdf): could not journal no_body for ref_id=%s: %s",
            ref_id,
            exc,
        )


#: ``ref_events.source`` of the multi-article page-spread journal row.
ARTICLE_SPREAD_SOURCE = "ingest:spread"
#: Open tag on a paper whose stored body still holds several articles.
MULTI_ARTICLE_TAG = "multi-article"


def _fold_target_identity(input: PdfInput, *, store: Store) -> tuple[str, str | None]:
    """Title and DOI of the stub this PDF was fetched for, else ``("", None)``.

    The sidecar ``fold_ref_id`` or a ``pa<id>`` filename names the article
    the file is for; :func:`precis.ingest.pipeline.extract_body` keeps that
    article from a multi-article page spread (gr463414). A placeholder title
    is no evidence, so it falls back to the extracted metadata.
    """
    from precis.utils.title_body import is_placeholder_title

    ref_id = input.fold_ref_id or _handle_stem_ref_id(input.pdf_path.stem)
    if ref_id is None:
        return "", None
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT r.title,
                       (SELECT min(id_value) FROM ref_identifiers
                         WHERE ref_id = r.ref_id AND id_kind = 'doi')
                  FROM refs r
                 WHERE r.ref_id = %s AND r.retired_at IS NULL
                """,
                (int(ref_id),),
            ).fetchone()
    except Exception as exc:  # pragma: no cover - a hint, never fatal
        log.warning("precis_add (pdf): fold target ref_id=%s unread: %s", ref_id, exc)
        return "", None
    if row is None:
        return "", None
    title = str(row[0] or "")
    return ("" if is_placeholder_title(title) else title), row[1]


def _note_article_spread(
    store: Store,
    ref_id: int | None,
    spread: dict[str, Any] | None,
    input: PdfInput,
    *,
    conn: Any,
) -> None:
    """Journal a multi-article page spread found at extraction (gr463414).

    ``trimmed``: the body kept one article; the payload names the kept and
    cut block/page ranges with their heads so the cut is auditable.
    ``ambiguous``: several articles and no confident title/DOI match, so the
    whole body was kept and the paper is tagged ``multi-article`` (finding
    links onto it then carry the advisory). A ``trimmed`` result with
    ``residual`` (an unconfirmed boundary stayed in the kept span) is tagged
    too. Best-effort, like
    :func:`_note_pdf_no_body`.
    """
    if ref_id is None or not spread:
        return
    try:
        verdict = str(spread.get("verdict") or "")
        store.append_event(
            int(ref_id),
            source=ARTICLE_SPREAD_SOURCE,
            event=verdict,
            payload={**spread, "file": input.pdf_path.name},
            conn=conn,
        )
        if verdict == "ambiguous" or spread.get("residual"):
            store.add_tag(
                int(ref_id), Tag.open(MULTI_ARTICLE_TAG), set_by="system", conn=conn
            )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (pdf): could not journal article spread for ref_id=%s: %s",
            ref_id,
            exc,
        )


def _ingest_pdf(
    input: PdfInput,
    *,
    store: Store,
    pdf_sha256: str,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
    marker_timeout_s: float | None = None,
) -> IngestResult:
    """Run the slow path for a PdfInput, assuming the claim is held."""
    target_title, target_doi = _fold_target_identity(input, store=store)
    paper = _build_paper(
        input,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
        marker_timeout_s=marker_timeout_s,
        target_title=target_title,
        target_doi=target_doi,
    )
    # Journalled as a ref_event below, not stored on refs.meta.
    spread = paper.meta.get("article_spread")
    if spread is not None:
        paper = replace(
            paper, meta={k: v for k, v in paper.meta.items() if k != "article_spread"}
        )

    with store.pool.connection() as conn:
        existing = probe_existing(
            # A printable_only companion's identity is its fetch provenance
            # (the sidecar ref_id), never its embedded metadata — gr506757:
            # Elsevier stamps some companion PDFs with a *predecessor*
            # paper's DocInfo DOI/title, so probing bibliographic ids can
            # attach the file to a ref that legitimately owns that other
            # DOI. Only the content hash probe is meaningful here.
            paper_id=None if input.printable_only else paper.paper_id,
            doi=None if input.printable_only else paper.doi,
            arxiv_id=None if input.printable_only else paper.arxiv_id,
            s2_id=None if input.printable_only else paper.s2_id,
            pubmed_id=None if input.printable_only else paper.pubmed_id,
            openalex_id=None if input.printable_only else paper.openalex_id,
            pdf_sha256=paper.pdf_sha256,
            content_hash=paper.content_hash,
            conn=conn,
        )
        fold_ref_id = input.fold_ref_id
        if existing is None and fold_ref_id is None:
            # Manual drop named by handle (``pa123.pdf``): same fold, no sidecar.
            fold_ref_id = _handle_stem_ref_id(input.pdf_path.stem)
        if existing is None and fold_ref_id is not None:
            # No identifier-based dedup hit, but the OA-fetch sidecar (or a
            # ``pa<id>`` filename) named the stub this PDF was fetched *for*. Fold into it directly
            # (promote in place) rather than minting a duplicate — the
            # deterministic, filename-independent path that survives the
            # multi-host inbox race. Guarded to a *live metadata-only stub*
            # of the same kind, so a stale / already-upgraded / soft-deleted
            # target falls through to a normal insert.
            existing = _valid_fold_stub(
                fold_ref_id, kind=paper.kind, conn=conn, doi=paper.doi
            )
            if (
                existing is None
                and input.printable_only
                and input.fold_ref_id is not None
            ):
                # gr506757: a printable_only companion writes no body chunks
                # anywhere, so the embedded-DOI conflict that rightly refuses
                # a body-bearing fold is the wrong gate — that DOI can be the
                # publisher's own stale DocInfo. Retry the fold gated on the
                # rendered-text identity check instead.
                existing = _printable_fold_target(
                    fold_ref_id, input.pdf_path, kind=paper.kind, conn=conn
                )
                if existing is not None:
                    _note_printable_doi_override(
                        store, fold_ref_id, input, paper, conn=conn
                    )
            if existing is None and not input.printable_only:
                # gr513878: a related-article DOI typeset ahead of the
                # article's own can trip the conflict gate while the file
                # still IS the stub's paper — arbitrate on the rendered
                # text / cite_key stem before minting a duplicate.
                existing = _fold_past_doi_conflict(
                    store, fold_ref_id, input, paper, conn=conn
                )
            if existing is not None and not input.printable_only:
                # Identity (gr512169): a *no-DOI* fetched file passes the
                # DOI-conflict gate on nothing, so a series sibling
                # fetched for this stub would take the stub's body in
                # place. Check part markers + first author against the
                # stub before handing it a body.
                s_title, s_authors = _ref_identity(conn, existing)
                refusal = _stub_identity_refusal(
                    s_title, s_authors, paper.title or "", paper.authors
                )
                if refusal is not None:
                    log.warning(
                        "precis_add: refusing fold of %s into stub "
                        "ref_id=%s: %s (stub %r vs fetched %r) — ingesting "
                        "as a new ref, stub left live",
                        input.pdf_path.name,
                        existing,
                        refusal,
                        s_title[:60],
                        (paper.title or "")[:60],
                    )
                    _note_stub_fold_refused(
                        store,
                        existing,
                        input=input,
                        paper=paper,
                        reason=refusal,
                        conn=conn,
                    )
                    existing = None
            if (
                existing is not None
                and input.fold_ref_id is None
                and not _named_stub_identity_ok(existing, input.pdf_path, conn=conn)
            ):
                # Handle-named but a different paper (e.g. one that only
                # cites the stub's): ingest as a fresh ref, never fold.
                existing = None
            elif existing is not None:
                log.info(
                    "precis_add: folding %s into sidecar stub ref_id=%s "
                    "(no id dedup hit; keeping stub metadata)",
                    input.pdf_path.name,
                    existing,
                )
            elif input.printable_only and input.fold_ref_id is not None:
                # Never mint: a printable_only file is only ever the named
                # ref's attachable printable — not a body candidate — so a
                # refused fold minting a fresh ref from embedded metadata
                # mints a phantom paper AND strips the real ref's printable
                # (gr506757). Journal the refusal on the target instead.
                _note_printable_attach_refused(
                    store, fold_ref_id, input, paper, conn=conn
                )
                conn.commit()
                refused = _with_kind(
                    _hit_result_from_db(fold_ref_id, conn=conn, fallback=paper),
                    kind=_lookup_kind(fold_ref_id, conn=conn),
                )
                # No attach happened, so the file is not the target's
                # canonical copy — park it in duplicates/, never "recover"
                # it into the corpus under the target's cite_key.
                return replace(refused, cite_key="")
            elif input.fold_ref_id is None:
                log.warning(
                    "precis_add: %s names handle ref_id=%s but it is not a "
                    "valid fold stub (retired, wrong kind, has a body, or DOI "
                    "mismatch); ingesting as a new ref",
                    input.pdf_path.name,
                    fold_ref_id,
                )
            else:
                log.warning(
                    "precis_add: %s sidecar named ref_id=%s but it is not a "
                    "valid fold stub (retired, wrong kind, has a body, or DOI "
                    "mismatch); ingesting as a new ref",
                    input.pdf_path.name,
                    fold_ref_id,
                )
        if existing is not None:
            # Existing ref hit — never re-insert, but always (a) register
            # this ingest's pdf/content hashes as aliases and (b) when
            # the row is a stub (``pdf_sha256 IS NULL``), upgrade it
            # by promoting this PDF to canonical and inserting the
            # extracted chunks. See db_writer.register_aliases_and_maybe_upgrade
            # for the contract and docs/backlog/finding-chase.md
            # §"Stub upgrade" for the rationale (chase findings
            # waiting on this stub resume on the next chase pass
            # without any extra plumbing).
            # gr519746: capture the ref's stored ``pdf_sha256`` around the
            # upgrade so the result can tell the watcher whether this call
            # *promoted* the canonical sha to this ingest's file
            # (attach-only onto a bodied/sha-less ref, or the bodiless
            # re-point) — as opposed to merely registering an alias on a
            # ref that already held a PDF (a real duplicate re-drop).
            pre_sha_row = conn.execute(
                "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (existing,),
            ).fetchone()
            pre_sha = pre_sha_row[0] if pre_sha_row else None
            chunks_written = register_aliases_and_maybe_upgrade(
                existing, paper, conn=conn
            )
            post_sha_row = conn.execute(
                "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (existing,),
            ).fetchone()
            pdf_promoted = bool(
                paper.pdf_sha256
                and post_sha_row
                and post_sha_row[0] == paper.pdf_sha256
                and pre_sha != paper.pdf_sha256
            )
            # A ``pa<id>``-named file that de-duped against a different live
            # ref: the named stub wins; the twin merges into it (gr479362).
            # Only when the stub wasn't already the dedup target above.
            if input.fold_ref_id is None:
                existing = _fold_into_named_stub(
                    store,
                    matched_ref_id=existing,
                    file_stem=input.pdf_path.stem,
                    pdf_path=input.pdf_path,
                    conn=conn,
                )
            # If this PDF de-duped against a DIFFERENT ref than the stub
            # it was fetched for, fold that orphan stub into the survivor
            # now so it stops re-qualifying for OA fetch forever (the
            # zombie-stub spin loop). No-op on a plain re-drop.
            _reconcile_orphan_stub(
                store,
                survivor_ref_id=existing,
                file_stem=input.pdf_path.stem,
                conn=conn,
            )
            _note_pdf_no_body(store, existing, input, paper, conn=conn)
            if chunks_written:
                _note_article_spread(store, existing, spread, input, conn=conn)
                # gr515439: mark a leading foreign prefix (previous
                # article's tail) so title/front-matter readers skip it.
                mark_article_start(store, existing, conn=conn)
            conn.commit()
            stored_kind = _lookup_kind(existing, conn=conn)
            _apply_extra_tags(store, stored_kind, existing, input.extra_tags)
            return _with_kind(
                _hit_result_from_db(
                    existing,
                    conn=conn,
                    fallback=paper,
                    chunks_written=chunks_written,
                    # ``chunks_written`` counts submitted rows (cards
                    # included, conflict-skips counted anyway); the body
                    # count is what tells the watcher a bodiless ref just
                    # got filled (gr510690).
                    body_chunks_written=(
                        sum(1 for c in paper.chunks if c.ord >= 0)
                        if chunks_written
                        else 0
                    ),
                    pdf_promoted=pdf_promoted,
                ),
                kind=stored_kind,
            )

        # Write-back: patch the on-disk file with the resolved canonical
        # metadata so a re-ingest from a clean DB still finds the right
        # DOI via embedded metadata. Honours
        # ``PRECIS_PATCH_PDFS=0`` off-switch.
        paper = _maybe_patch_pdf(input.pdf_path, paper)

        result = write_paper(paper, conn=conn)
        # Root-cause fold: this is the *first* ingest of this content, so
        # it dedup-missed and minted a fresh ref. If a live stub was fetched
        # for this same paper but its identity didn't intersect (Marker
        # truncated/dropped the DOI, or extracted none), fold that orphan
        # stub into the new ref now — by ``cite_key == filename stem`` — so
        # it doesn't linger ``pdf_sha256 IS NULL`` and re-qualify for OA
        # fetch forever. Previously this reconcile ran only on the dedup-hit
        # branches (a *re*-drop), so every metadata-mismatched paper stayed
        # split until a second ingest. The sidecar ``fold_ref_id`` above is
        # the deterministic version of the same fold; this filename fallback
        # covers manual drops and legacy PDFs that carry no sidecar.
        _reconcile_orphan_stub(
            store,
            survivor_ref_id=result.ref_id,
            file_stem=input.pdf_path.stem,
            conn=conn,
        )
        _note_pdf_no_body(store, result.ref_id, input, paper, conn=conn)
        if result.chunks_written:
            _note_article_spread(store, result.ref_id, spread, input, conn=conn)
            mark_article_start(store, result.ref_id, conn=conn)
        conn.commit()

    _apply_extra_tags(store, paper.kind, result.ref_id, input.extra_tags)
    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id=paper.paper_id,
        pub_id=paper.pub_id,
        cite_key=result.cite_key,
        pdf_sha256=paper.pdf_sha256,
        content_hash=paper.content_hash,
        chunks_written=result.chunks_written,
        identifiers=result.identifiers_written,
        kind=paper.kind,
        # gr236139 — surfaced by extract_paper via paper.meta so the
        # watcher can raise a fallback alert / route an empty-body
        # degraded extraction to errors/ without re-deriving it here.
        used_marker_fallback=bool(paper.meta.get("extract_used_fallback")),
        fallback_empty_body=bool(paper.meta.get("extract_fallback_empty")),
        ref_has_body=any(c.ord >= 0 for c in paper.chunks),
        body_chunks_written=sum(1 for c in paper.chunks if c.ord >= 0),
    )


def _load_si_parent(ref_id: int, *, conn: Any) -> dict[str, Any] | None:
    """The live paper a supplement attaches to, or ``None``."""
    row = conn.execute(
        """
        SELECT r.title, r.authors, r.year, r.meta,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'),
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi')
          FROM refs r
         WHERE r.ref_id = %s AND r.kind = 'paper' AND r.retired_at IS NULL
        """,
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "ref_id": ref_id,
        "title": str(row[0] or ""),
        "authors": row[1] or [],
        "year": row[2],
        "meta": row[3] or {},
        "cite_key": row[4],
        "doi": row[5],
    }


def _link_supplement(
    si_ref_id: int, parent_ref_id: int, *, store: Store, conn: Any
) -> None:
    """Idempotently write the SI -> parent edge (``si_links.SI_RELATION``)."""
    from precis.store.si_links import link_supplement

    link_supplement(si_ref_id, parent_ref_id, store=store, conn=conn)


def _reuse_supplement(
    input: PdfInput, existing_ref_id: int, *, store: Store
) -> IngestResult:
    """A ref already holds this SI PDF's sha: ensure the link, no new mint."""
    parent_id = input.supplement_of
    assert parent_id is not None
    with store.pool.connection() as conn:
        role_row = conn.execute(
            "SELECT pdf_role FROM refs WHERE ref_id = %s", (existing_ref_id,)
        ).fetchone()
        existing_role = role_row[0] if role_row is not None else None
        if parent_id == existing_ref_id or existing_role != "supplement":
            # The sha is held by an ordinary paper (e.g. the SI bytes equal a
            # main PDF): linking it would turn a real paper into someone's SI.
            log.warning(
                "precis_add: supplement of ref_id=%s matches existing ref_id=%s "
                "(pdf_role=%r) — not linking",
                parent_id,
                existing_ref_id,
                existing_role,
            )
        elif _load_si_parent(parent_id, conn=conn) is not None:
            _link_supplement(existing_ref_id, parent_id, store=store, conn=conn)
        conn.commit()
        hit = _hit_result_from_db(existing_ref_id, conn=conn)
        stored_kind = _lookup_kind(existing_ref_id, conn=conn)
    return _with_kind(hit, kind=stored_kind)


def _ingest_supplement(
    input: PdfInput,
    *,
    store: Store,
    pdf_sha256: str,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
    marker_timeout_s: float | None = None,
) -> IngestResult:
    """Mint an SI PDF as its OWN paper ref linked to its parent.

    The normal extraction runs (chunks searchable like any body text) with
    these guards, because an SI PDF usually prints the PARENT's DOI/title:

    * ``pdf_role='supplement'`` (the stub-upgrade path's COALESCE and the
      pipeline's hardcoded ``'main'`` never get a say — no probe by extracted
      identifiers, so no fold into the parent);
    * no DOI/arXiv/S2/PubMed/OpenAlex identifier from the PDF is registered —
      only the SI's own component DOI (``supplement_info['component_doi']``)
      when the discovery source knew it; ``paper_id`` is sha-derived;
    * the title is minted ``Supporting Information: <parent title>`` (with
      `` (n)`` once the parent has other SI refs), authors/year copied from
      the parent, and the PDF is not patched with the resolved metadata;
    * ``meta.si_parent`` records parent/source/url/component DOI.

    Falls back to the ordinary path when the parent is gone (nothing to link).
    """
    from precis.identity import make_paper_id
    from precis.ingest.pipeline import _build_cards
    from precis.store.si_links import SI_PDF_ROLE, supplement_children

    parent_id = input.supplement_of
    assert parent_id is not None
    with store.pool.connection() as conn:
        parent = _load_si_parent(parent_id, conn=conn)
    if parent is None:
        log.warning(
            "precis_add: supplement parent ref_id=%s is gone — ingesting %s "
            "as an ordinary paper",
            parent_id,
            input.pdf_path.name,
        )
        return _ingest_pdf(
            input,
            store=store,
            pdf_sha256=pdf_sha256,
            use_pdf2doi=use_pdf2doi,
            crossref_mailto=crossref_mailto,
            s2_api_key=s2_api_key,
            marker_timeout_s=marker_timeout_s,
        )

    paper = _build_paper(
        input,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
        marker_timeout_s=marker_timeout_s,
    )
    info = dict(input.supplement_info or {})
    component_doi = info.get("component_doi") or None
    parent_cite = parent["cite_key"] or f"ref{parent_id}"

    with store.pool.connection() as conn:
        n_existing = len(supplement_children(conn, parent_id))
        title = f"Supporting Information: {parent['title']}".strip()
        if n_existing >= 1:
            title += f" ({n_existing + 1})"

        # Cards are rebuilt from the minted title/authors: the extracted ones
        # name whatever the SI PDF printed (usually the parent's own title).
        cards = _build_cards(
            title=title, authors=parent["authors"], abstract="", keywords=[]
        )
        body = [c for c in paper.chunks if c.ord >= 0]
        meta = {
            k: v
            for k, v in paper.meta.items()
            if k not in ("abstract", "keywords", "verify_warnings")
        }
        parent_journal = (parent["meta"] or {}).get("journal")
        if parent_journal:
            meta["journal"] = parent_journal
        meta["si_parent"] = {
            "ref_id": parent_id,
            "cite_key": parent_cite,
            "doi": parent["doi"],
            "source": info.get("source"),
            "url": info.get("url"),
            "component_doi": component_doi,
        }
        paper = replace(
            paper,
            title=title,
            authors=parent["authors"],
            year=parent["year"],
            paper_id=make_paper_id(pdf_sha256=pdf_sha256),
            pub_id=None,
            cite_key_prefix=f"{parent_cite}si",
            doi=component_doi,
            arxiv_id=None,
            s2_id=None,
            pubmed_id=None,
            openalex_id=None,
            pdf_role=SI_PDF_ROLE,
            meta=meta,
            chunks=cards + body,
        )
        # Only the sha identity may already exist (same file processed in a
        # race); identifier-based dedup is exactly what must NOT run here.
        existing = probe_existing(
            pdf_sha256=paper.pdf_sha256, content_hash=paper.content_hash, conn=conn
        )
        if existing is not None:
            conn.commit()
            return _reuse_supplement(input, existing, store=store)
        result = write_paper(paper, conn=conn)
        _link_supplement(result.ref_id, parent_id, store=store, conn=conn)
        conn.commit()

    _apply_extra_tags(store, paper.kind, result.ref_id, input.extra_tags)
    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id=paper.paper_id,
        pub_id=paper.pub_id,
        cite_key=result.cite_key,
        pdf_sha256=paper.pdf_sha256,
        content_hash=paper.content_hash,
        chunks_written=result.chunks_written,
        identifiers=result.identifiers_written,
        kind=paper.kind,
        used_marker_fallback=bool(paper.meta.get("extract_used_fallback")),
        fallback_empty_body=bool(paper.meta.get("extract_fallback_empty")),
        ref_has_body=any(c.ord >= 0 for c in paper.chunks),
        body_chunks_written=sum(1 for c in paper.chunks if c.ord >= 0),
    )


#: DOI prefixes of preprint servers. A preprint copy fetched for a journal
#: ref legitimately carries a different DOI, so these never count as a
#: conflict in :func:`_valid_fold_stub`: arXiv, bioRxiv/medRxiv,
#: ChemRxiv, Research Square, Preprints.org.
_PREPRINT_DOI_PREFIXES = (
    "10.48550/",
    "10.1101/",
    "10.26434/",
    "10.21203/",
    "10.20944/",
)


def _doi_conflicts(fetched_doi: str | None, ref_id: int, *, conn: Any) -> str | None:
    """The target's DOI when it differs from ``fetched_doi``, else ``None``.

    Only a conflict between two journal DOIs counts: no DOI on either side,
    or a preprint-server DOI on either side, is not evidence of a wrong
    target."""
    fetched = normalize_doi(fetched_doi)
    if not fetched or fetched.startswith(_PREPRINT_DOI_PREFIXES):
        return None
    rows = conn.execute(
        "SELECT id_value FROM ref_identifiers WHERE ref_id = %s AND id_kind = 'doi'",
        (ref_id,),
    ).fetchall()
    target_dois = [str(r[0]) for r in rows]
    journal = [d for d in target_dois if not d.startswith(_PREPRINT_DOI_PREFIXES)]
    if not journal or fetched in journal:
        return None
    return journal[0]


_HANDLE_STEM_RE = re.compile(r"pa(\d+)(?:-.*)?", re.IGNORECASE | re.DOTALL)


def _handle_stem_ref_id(stem: str) -> int | None:
    """``pa<digits>`` / ``pa<digits>-<anything>`` filename stem -> ref_id.

    The ``pa<id>`` handle is what every surface shows, so a human drops a
    PDF for a wanted stub under that name. Pure parse; callers still gate
    the result through :func:`_valid_fold_stub`.
    """
    m = _HANDLE_STEM_RE.fullmatch(stem.strip())
    return int(m.group(1)) if m else None


def _valid_fold_stub(
    ref_id: int, *, kind: str, conn: Any, doi: str | None = None
) -> int | None:
    """Return ``ref_id`` iff it's a live, same-kind ref that still needs a body.

    The OA-fetch sidecar names a fold target, but by the time a watcher
    ingests the file that target may have moved on — soft-deleted (merged
    away), of a different kind (a mis-pointed sidecar), or already
    carrying a body. Retired / wrong-kind targets disqualify the direct
    fold; the caller falls back to a normal insert.

    A valid target is either a ``pdf_sha256 IS NULL`` metadata stub, or a
    *bodiless* ref (no ``chunks`` row with ``ord >= 0``) even when it holds
    a stored PDF sha — the bodiless re-fetch case, where the fetched file
    differs from the stored one. ``register_aliases_and_maybe_upgrade``
    then writes chunks only if the ref has no body, attaches only
    otherwise, and honours the ``markup_refetch`` pin.

    Identity check: when the fetched file carries a journal DOI and the
    target holds a different journal DOI, the sidecar is pointing at the
    wrong paper (or the source served the wrong file), so the fold is
    refused and logged; the caller inserts a separate ref instead of
    writing another paper's chunks onto this one. Concurrent folds into
    one ref are serialised by the row lock in
    ``register_aliases_and_maybe_upgrade``.
    """
    row = conn.execute(
        """
        SELECT ref_id FROM refs r
         WHERE r.ref_id = %s
           AND r.kind = %s
           AND r.retired_at IS NULL
           AND (
                r.pdf_sha256 IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM chunks c
                     WHERE c.ref_id = r.ref_id AND c.ord >= 0
                )
           )
        """,
        (ref_id, kind),
    ).fetchone()
    if row is None:
        return None
    conflict = _doi_conflicts(doi, ref_id, conn=conn)
    if conflict is not None:
        log.warning(
            "precis_add: refusing sidecar fold into ref_id=%s: fetched DOI %s "
            "!= target DOI %s",
            ref_id,
            normalize_doi(doi),
            conflict,
        )
        return None
    return int(row[0])


def _warn_skipped_cite_key_match(conn: Any, *, stem: str, exclude_ref_id: int) -> None:
    """Log a near-miss: a live, ``pdf_sha256 IS NULL`` ref shares ``stem``
    as its ``cite_key`` but :func:`_reconcile_orphan_stub`'s kind/body
    guard disqualified it (not ``kind='paper'``, or it carries live body
    chunks). This is exactly the shape of the 2026-09-27 incident (a
    ``draft`` ref matched by filename) minus the guard that now stops it
    — surfacing it at WARNING so the next near-miss is visible instead of
    silent until it becomes a second incident.
    """
    row = conn.execute(
        """
        SELECT r.ref_id, r.kind
          FROM ref_identifiers ri
          JOIN refs r ON r.ref_id = ri.ref_id
         WHERE ri.id_kind = 'cite_key'
           AND lower(ri.id_value) = %s
           AND r.pdf_sha256 IS NULL
           AND r.retired_at IS NULL
           AND r.ref_id <> %s
         LIMIT 1
        """,
        (stem, exclude_ref_id),
    ).fetchone()
    if row is not None:
        log.warning(
            "precis_add: cite_key match ref_id=%s kind=%s for stem=%r skipped "
            "by _reconcile_orphan_stub's paper/no-body-chunks guard "
            "(survivor ref_id=%s) — not reconciled",
            row[0],
            row[1],
            stem,
            exclude_ref_id,
        )


#: Leading body chunks of the survivor searched for the stub's title tokens.
_FOLD_EVIDENCE_CHUNKS = 5


def _stub_title_corroborated(conn: Any, stub_id: int, survivor_ref_id: int) -> bool:
    """True when the stub's title overlaps the survivor's title/leading body.

    Needs ``min(2, len(stub tokens))`` shared distinctive tokens
    (:func:`~precis.ingest.verify_metadata.title_overlap`); a stub with no
    usable title tokens cannot be corroborated and is not folded.
    """
    from precis.ingest.verify_metadata import content_tokens, title_overlap

    srow = conn.execute(
        "SELECT title FROM refs WHERE ref_id = %s", (stub_id,)
    ).fetchone()
    stub_title = (srow[0] if srow else None) or ""
    need = min(2, len(content_tokens(stub_title)))
    if need == 0:
        return False
    vrow = conn.execute(
        "SELECT title FROM refs WHERE ref_id = %s", (survivor_ref_id,)
    ).fetchone()
    parts = [(vrow[0] if vrow else None) or ""]
    parts += [
        r[0] or ""
        for r in conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord LIMIT %s",
            (survivor_ref_id, _FOLD_EVIDENCE_CHUNKS),
        ).fetchall()
    ]
    return title_overlap(stub_title, "\n".join(parts)) >= need


#: Series-part markers recognised in a title (gr512169): ``…: I.``,
#: ``Part 2``, ``Vol. III`` and a trailing `` II.`` — roman and arabic
#: numerals both compare as ints so ``I.`` and ``Part 1`` agree. Roman
#: numerals are matched uppercase-only so words like ``id``/``vitro``
#: can't read as markers; a separator followed by whitespace is required
#: so ``COVID-19.`` can't read as part 19.
_SERIES_MARKER_RES = (
    re.compile(r"(?i:\bpart)\s+(?:no\.?\s*)?(\d{1,3}|[IVXLCDM]{1,7})\b"),
    re.compile(r"(?i:\bvol(?:ume)?)\.?\s*(?:no\.?\s*)?(\d{1,3}|[IVXLCDM]{1,7})\b"),
    re.compile(r"[:;.–—-]\s+(?:part\s+)?(\d{1,3}|[IVXLCDM]{1,7})\s*\."),
    re.compile(r"[\s:;.,–—-]([IVX]{1,4})\.\s*$"),
)

_ROMAN_VALS = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}


def _marker_int(token: str) -> int:
    """A series numeral as an int: arabic digits, else roman (``IV`` → 4)."""
    if token.isdigit():
        return int(token)
    total, prev = 0, 0
    for ch in reversed(token):
        v = _ROMAN_VALS.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def _series_markers(title: str) -> frozenset[int]:
    """The series-part numbers a title claims (``{1}`` for ``'…: I.'``)."""
    return frozenset(
        n
        for rx in _SERIES_MARKER_RES
        for m in rx.finditer(title)
        if (n := _marker_int(m.group(1)))
    )


def _ref_identity(conn: Any, ref_id: int) -> tuple[str, Any]:
    """``(title, authors)`` for a ref — the identity a fold is checked against."""
    row = conn.execute(
        "SELECT coalesce(title, ''), authors FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    return (str(row[0]), row[1]) if row else ("", None)


def _stub_identity_refusal(
    stub_title: str,
    stub_authors: Any,
    held_title: str,
    held_authors: Any,
) -> str | None:
    """Why ``held`` must not take the stub's identity (``None`` = OK).

    gr512169: the cite_key reconcile folded a *Part II* PDF into a Part I
    DOI stub — series siblings share every distinctive title token, so
    the token-overlap corroboration could not tell them apart. On top of
    that gate require (a) series-part markers to agree exactly — the part
    number is the reliable tell between siblings, and a marker on only
    one side cannot be corroborated — and (b) first-author family names
    to match whenever both sides carry one. Missing *author* evidence is
    not a refusal: extraction gaps would strand legitimate stubs in the
    re-fetch loop the reconcile exists to stop.
    """
    if _series_markers(stub_title) != _series_markers(held_title):
        return "series-marker-mismatch"
    stub_first, held_first = (
        _first_family(stub_authors),
        _first_family(held_authors),
    )
    if stub_first and held_first and stub_first != held_first:
        return "author-mismatch"
    return None


_STUB_REVIEW_SOURCE = "ingest:stub-reconcile-review"
_STUB_IDENTITY_REVIEW_TAG = "paper-stub-identity-review"


def _note_stub_reconcile_refused(
    store: Store,
    ref_id: int,
    *,
    stub_id: int,
    file_stem: str,
    reason: str,
    conn: Any,
) -> None:
    """Journal + quarantine-tag a refused stub fold on ``ref_id`` (gr512169).

    ``ref_id`` is whichever ref holds the fetched content — the minted
    survivor in :func:`_reconcile_orphan_stub`, the dedup-matched twin in
    :func:`_fold_into_named_stub`. It keeps its own identity (it may be a
    perfectly good *other* paper — the incident's Part II was) but is
    tagged ``paper-stub-identity-review`` so the suspect copy is
    findable, and the refusal is journalled so the ref's log shows why
    the stub's identifiers did NOT move. Best-effort, like
    :func:`_note_pdf_no_body`.
    """
    try:
        store.append_event(
            ref_id,
            source="ingest:dedup",
            event="stub_reconcile_refused",
            payload={
                "stub_ref_id": stub_id,
                "file_stem": file_stem,
                "reason": reason,
            },
            conn=conn,
        )
        store.add_tag(
            ref_id,
            Tag.parse_strict(_STUB_IDENTITY_REVIEW_TAG),
            set_by="system",
            conn=conn,
        )
    except Exception as exc:  # journalling is best-effort
        log.warning(
            "precis_add: could not journal stub_reconcile_refused for ref_id=%s: %s",
            ref_id,
            exc,
        )


def _note_stub_fold_refused(
    store: Store,
    stub_id: int,
    *,
    input: PdfInput,
    paper: Any,
    reason: str,
    conn: Any,
) -> None:
    """Journal + quarantine-tag a stub whose fetched file failed identity.

    The in-place-fold counterpart of :func:`_note_stub_reconcile_refused`:
    the fetched file has no ref of its own yet (it goes on to mint one),
    so the refusal is journalled on the *stub* — the ref whose body the
    file nearly became — and the stub is tagged
    ``paper-stub-identity-review`` so the contested target is findable.
    Best-effort, like :func:`_note_pdf_no_body`.
    """
    try:
        store.append_event(
            stub_id,
            source="ingest:dedup",
            event="stub_fold_refused",
            payload={
                "file": input.pdf_path.name,
                "pdf_sha256": paper.pdf_sha256,
                "reason": reason,
            },
            conn=conn,
        )
        store.add_tag(
            stub_id,
            Tag.parse_strict(_STUB_IDENTITY_REVIEW_TAG),
            set_by="system",
            conn=conn,
        )
    except Exception as exc:  # journalling is best-effort
        log.warning(
            "precis_add: could not journal stub_fold_refused for ref_id=%s: %s",
            stub_id,
            exc,
        )


def _raise_stub_identity_review(
    store: Store,
    *,
    stub_id: int,
    survivor_ref_id: int,
    file_stem: str,
    reason: str,
    conn: Any,
) -> None:
    """File a ``STATUS:open`` review todo for a refused stub reconcile.

    Deduped on ``(source, stub, survivor)`` like
    ``dedup._raise_doi_case_review``: a standing todo already covering
    the pair is not re-filed, so a re-fetch loop can't re-raise it each
    pass. Runs on the caller's connection so the todo commits or rolls
    back with the rest of the ingest.
    """
    from precis.config import load_config
    from precis.utils.handle_registry import format_handle

    standing = conn.execute(
        "SELECT 1 FROM refs t "
        "WHERE t.kind = 'todo' AND t.retired_at IS NULL "
        "  AND t.meta->>'source' = %s AND t.meta->>'stub_ref_id' = %s "
        "  AND t.meta->>'survivor_ref_id' = %s "
        "  AND NOT EXISTS (SELECT 1 FROM ref_tags rt "
        "    JOIN tags tg USING (tag_id) "
        "    WHERE rt.ref_id = t.ref_id "
        "      AND tg.namespace = 'STATUS' AND tg.value = 'done')",
        (_STUB_REVIEW_SOURCE, str(stub_id), str(survivor_ref_id)),
    ).fetchone()
    if standing is not None:
        return
    todo_ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=(
            f"Review refused stub reconcile "
            f"{format_handle('paper', stub_id)} vs "
            f"{format_handle('paper', survivor_ref_id)}: the PDF dropped "
            f"as {file_stem!r} failed the stub identity check ({reason}). "
            "The stub keeps its identifiers and stays live; merge by "
            "hand only if they really are the same paper (gr512169)."
        ),
        meta={
            "source": _STUB_REVIEW_SOURCE,
            "stub_ref_id": stub_id,
            "survivor_ref_id": survivor_ref_id,
            "file_stem": file_stem,
            "reason": reason,
        },
        conn=conn,
    )
    store.add_tag(
        todo_ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    for tag in (_STUB_IDENTITY_REVIEW_TAG, f"waiting-for:{load_config().owner}"):
        store.add_tag(todo_ref.id, Tag.parse_strict(tag), set_by="system", conn=conn)


def _stub_identity_verdict(
    stub_id: int, pdf_path: Path, *, conn: Any
) -> tuple[str, str | None]:
    """``(verdict, detail)`` — does the PDF look like the paper ``stub_id`` is?

    Runs the pdf_url door's gate, ``paper_pull.check_pdf`` (stub DOI in
    the first pages, or enough title words). An unreadable file yields
    ``("unchecked", None)`` — nothing to compare.
    """
    from precis.ingest.pdf_identity import identity_verdict, read_leading_pages

    row = conn.execute(
        "SELECT coalesce(title, ''), coalesce(meta->>'entry_type', '') "
        "FROM refs WHERE ref_id = %s",
        (stub_id,),
    ).fetchone()
    doi_row = conn.execute(
        "SELECT id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind = 'doi' ORDER BY id_value LIMIT 1",
        (stub_id,),
    ).fetchone()
    try:
        _, texts = read_leading_pages(pdf_path)
    except Exception:  # unreadable: nothing to compare
        return "unchecked", None
    return identity_verdict(
        texts,
        title=str(row[0]) if row else "",
        doi=str(doi_row[0]) if doi_row else None,
    )


def _named_stub_identity_ok(stub_id: int, pdf_path: Path, *, conn: Any) -> bool:
    """Does the PDF look like the paper ``stub_id`` is? (gr479094 wrong fold.)

    A ``pa<id>`` filename is a human claim, not proof: a PDF that merely
    cites the stub's paper (Megaro 2017 vs Howell-Midha 1994) was folded into
    it. Only an ``identity_mismatch`` refuses; unreadable / short files and
    stubs with nothing to compare (``unchecked``) pass, since this gate is
    identity, not preview detection.
    """
    verdict, detail = _stub_identity_verdict(stub_id, pdf_path, conn=conn)
    if verdict == "mismatch":
        log.warning(
            "precis_add: %s is named for stub ref_id=%s but its text does "
            "not match that paper (%s) — not folding, stub left live",
            pdf_path.name,
            stub_id,
            detail,
        )
        return False
    return True


def _stem_matches_cite_key(stem: str, ref_id: int, *, conn: Any) -> bool:
    """True when the filename stem equals ``ref_id``'s cite_key.

    The OA fetcher names its downloads ``<cite_key>.pdf``
    (:func:`precis.workers.fetch_oa._stub_filename`), so an equal stem is
    the fetcher's own identity claim — gr513878's ``a26aqd.pdf`` was
    fetched for the stub whose cite_key is ``a26aqd``.
    """
    row = conn.execute(
        "SELECT 1 FROM ref_identifiers WHERE ref_id = %s "
        "AND id_kind = 'cite_key' AND lower(id_value) = %s LIMIT 1",
        (ref_id, stem.strip().lower()),
    ).fetchone()
    return row is not None


def _fold_past_doi_conflict(
    store: Store,
    fold_ref_id: int,
    input: PdfInput,
    paper: Any,
    *,
    conn: Any,
) -> int | None:
    """``fold_ref_id`` when a DOI-refused fold target is textually this paper.

    gr513878: a related-article DOI typeset ahead of the article's own
    (the MethodsX / Data in Brief companion shape) can win extraction, so
    the embedded DOI conflicts with the fetch target's while the file
    still IS that paper — refusing the fold then mints a duplicate ref
    holding the foreign DOI's metadata. The stub must still be a valid
    fold target and the conflict real; then the fold proceeds when the
    target's DOI or title appears in the leading pages, or the filename
    stem equals its cite_key — UNLESS the title actively disagrees (a
    citing or misserved file), which keeps the refusal. A granted
    arbitration journals ``attach_doi_overridden`` so the override stays
    auditable.
    """
    valid = _valid_fold_stub(fold_ref_id, kind=paper.kind, conn=conn)
    if valid is None:
        return None
    if _doi_conflicts(paper.doi, fold_ref_id, conn=conn) is None:
        return None
    verdict, detail = _stub_identity_verdict(fold_ref_id, input.pdf_path, conn=conn)
    if verdict == "mismatch":
        log.warning(
            "precis_add: not arbitrating %s into stub ref_id=%s despite the "
            "DOI conflict — the title disagrees too (%s)",
            input.pdf_path.name,
            fold_ref_id,
            detail,
        )
        return None
    stem_hit = _stem_matches_cite_key(input.pdf_path.stem, fold_ref_id, conn=conn)
    if verdict == "unchecked" and not stem_hit:
        return None
    log.warning(
        "precis_add: folding %s into stub ref_id=%s overriding extracted DOI "
        "%s (verdict=%s, stem_cite_key=%s)",
        input.pdf_path.name,
        fold_ref_id,
        normalize_doi(paper.doi),
        verdict,
        stem_hit,
    )
    _note_doi_overridden_fold(store, fold_ref_id, input, paper, conn=conn)
    return valid


def _printable_fold_target(
    ref_id: int, pdf_path: Path, *, kind: str, conn: Any
) -> int | None:
    """``ref_id`` iff a ``printable_only`` companion may attach to it (gr506757).

    Runs after :func:`_valid_fold_stub` refused the sidecar's fold target —
    which, for a still-live same-kind ref that still needs a PDF, can only
    be the embedded-DOI conflict gate. That gate is the *wrong* evidence
    here: a companion is attach-only (it never writes body chunks, the
    hazard the DOI guard exists for), and its embedded DocInfo can name a
    different paper outright — Elsevier stamps some companion PDFs with a
    predecessor paper's DOI/title while the rendered pages are the fetched
    article. Re-check liveness/kind/body-need, then gate on
    :func:`_named_stub_identity_ok`'s rendered-text verdict instead of the
    embedded metadata.
    """
    valid = _valid_fold_stub(ref_id, kind=kind, conn=conn)
    if valid is None:
        return None
    if not _named_stub_identity_ok(ref_id, pdf_path, conn=conn):
        return None
    return valid


def _printable_event_dois(paper: Any, fold_ref_id: int, *, conn: Any) -> dict[str, str]:
    """``{extracted_doi, target_doi}`` for the printable-attach journals."""
    out: dict[str, str] = {}
    if paper.doi:
        out["extracted_doi"] = str(paper.doi)
    target = _doi_conflicts(paper.doi, fold_ref_id, conn=conn)
    if target is None:
        row = conn.execute(
            "SELECT min(id_value) FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'doi'",
            (fold_ref_id,),
        ).fetchone()
        target = str(row[0]) if row and row[0] else ""
    if target:
        out["target_doi"] = target
    return out


def _note_printable_doi_override(
    store: Store,
    ref_id: int,
    input: PdfInput,
    paper: Any,
    *,
    conn: Any,
) -> None:
    """Journal a printable attach that overrode an embedded-DOI conflict.

    The rendered-text gate passed where the embedded DocInfo DOI disagreed
    with the fetch target — the signature of a publisher-stale DocInfo
    (gr506757). Worth a row: if a genuinely-misserved file ever slips past
    the text check, this event is what tells an operator which DOI lied.
    Best-effort, like :func:`_note_pdf_no_body`.
    """
    try:
        store.append_event(
            int(ref_id),
            source=PDF_NO_BODY_SOURCE,
            event="attach_doi_overridden",
            payload={
                "file": input.pdf_path.name,
                "pdf_sha256": paper.pdf_sha256,
                **_printable_event_dois(paper, ref_id, conn=conn),
            },
            conn=conn,
        )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (pdf): could not journal attach_doi_overridden for "
            "ref_id=%s: %s",
            ref_id,
            exc,
        )


def _note_doi_overridden_fold(
    store: Store,
    ref_id: int,
    input: PdfInput,
    paper: Any,
    *,
    conn: Any,
) -> None:
    """Journal a body fold that overrode an extracted-DOI conflict (gr513878).

    The fetched file carried a related/companion article's DOI, but the
    rendered text (or the cite_key filename) showed it IS the fold
    target's paper. Shares the printable path's
    ``attach_doi_overridden`` event — the row names which DOI lied.
    Best-effort, like :func:`_note_pdf_no_body`.
    """
    try:
        store.append_event(
            int(ref_id),
            source=PDF_NO_BODY_SOURCE,
            event="attach_doi_overridden",
            payload={
                "file": input.pdf_path.name,
                "pdf_sha256": paper.pdf_sha256,
                "reason": "fold_conflict_arbitration",
                **_printable_event_dois(paper, ref_id, conn=conn),
            },
            conn=conn,
        )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (pdf): could not journal attach_doi_overridden for "
            "ref_id=%s: %s",
            ref_id,
            exc,
        )


def _note_printable_attach_refused(
    store: Store,
    ref_id: int,
    input: PdfInput,
    paper: Any,
    *,
    conn: Any,
) -> None:
    """Journal a ``printable_only`` companion that could not attach (gr506757).

    The sidecar's fold target is gone, retired, resolved, or its rendered
    text mismatches the file. The ingest does NOT mint a fresh ref — the
    file was only ever this ref's printable — so this row is the loud
    record of where the printable went. Best-effort, like
    :func:`_note_pdf_no_body`.
    """
    try:
        store.append_event(
            int(ref_id),
            source=PDF_NO_BODY_SOURCE,
            event="attach_refused",
            payload={
                "file": input.pdf_path.name,
                "pdf_sha256": paper.pdf_sha256,
                **_printable_event_dois(paper, ref_id, conn=conn),
            },
            conn=conn,
        )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (pdf): could not journal attach_refused for ref_id=%s: %s",
            ref_id,
            exc,
        )


def _fold_into_named_stub(
    store: Store,
    *,
    matched_ref_id: int,
    file_stem: str,
    pdf_path: Path,
    conn: Any,
) -> int:
    """Make the ``pa<id>`` stub a filename names win over a dedup-matched twin.

    The PDF's hash/identity matched ``matched_ref_id`` (a live twin), but the
    human named the file after a wanted stub: that handle must not be
    retired (gr479362). When the stem names a live, same-kind, bodiless stub
    other than the twin and the stub's title corroborates the twin
    (:func:`_stub_title_corroborated`, the gr264748 guard), merge the twin
    INTO the stub with :func:`precis.ingest.dedup.merge_duplicate`, which
    hands the twin's body, PDF and content ids to the stub. Returns the ref
    the caller should treat as the ingest's result: the stub on a merge,
    else ``matched_ref_id`` (no handle in the stem, an invalid stub, or a
    refusal, which is logged naming the twin; both refs stay live).
    """
    from precis.ingest.dedup import merge_duplicate

    handle_ref = _handle_stem_ref_id(file_stem.strip().lower())
    if handle_ref is None or handle_ref == matched_ref_id:
        return matched_ref_id
    if _lookup_kind(matched_ref_id, conn=conn) != "paper":
        return matched_ref_id
    stub_id = _valid_fold_stub(handle_ref, kind="paper", conn=conn)
    if stub_id is None:
        log.warning(
            "precis_add: stem=%r names ref_id=%s but it is not a valid fold "
            "stub (retired, wrong kind or has a body) — not merged (twin "
            "ref_id=%s)",
            file_stem,
            handle_ref,
            matched_ref_id,
        )
        return matched_ref_id
    if not _named_stub_identity_ok(stub_id, pdf_path, conn=conn):
        return matched_ref_id
    if not _stub_title_corroborated(conn, stub_id, matched_ref_id):
        log.warning(
            "precis_add: not folding twin ref_id=%s into named stub ref_id=%s "
            "(stem=%r): stub title shares no distinctive tokens with the "
            "twin's title/leading body — both left live",
            matched_ref_id,
            stub_id,
            file_stem,
        )
        return matched_ref_id
    # Identity (gr512169): a series sibling passes both the rendered-text
    # and token gates — it shares the stub's tokens. Require part markers
    # + first author to agree before the twin's body lands on the stub.
    stub_title, stub_authors = _ref_identity(conn, stub_id)
    twin_title, twin_authors = _ref_identity(conn, matched_ref_id)
    refusal = _stub_identity_refusal(stub_title, stub_authors, twin_title, twin_authors)
    if refusal is not None:
        log.warning(
            "precis_add: not folding twin ref_id=%s into named stub ref_id=%s "
            "(stem=%r): %s (stub %r vs twin %r) — both left live",
            matched_ref_id,
            stub_id,
            file_stem,
            refusal,
            stub_title[:60],
            twin_title[:60],
        )
        _note_stub_reconcile_refused(
            store,
            matched_ref_id,
            stub_id=stub_id,
            file_stem=file_stem,
            reason=refusal,
            conn=conn,
        )
        _raise_stub_identity_review(
            store,
            stub_id=stub_id,
            survivor_ref_id=matched_ref_id,
            file_stem=file_stem,
            reason=refusal,
            conn=conn,
        )
        return matched_ref_id
    merge_duplicate(
        store,
        survivor_ref_id=stub_id,
        duplicate_ref_id=matched_ref_id,
        source="ingest:dedup",
        reason="named-stub-wins",
        conn=conn,
    )
    log.info(
        "precis_add: %r names stub ref_id=%s; merged dedup-matched twin "
        "ref_id=%s into it (stub keeps its handle)",
        file_stem,
        stub_id,
        matched_ref_id,
    )
    return stub_id


@trust_door("computed")
def _reconcile_orphan_stub(
    store: Store,
    *,
    survivor_ref_id: int,
    file_stem: str,
    conn: Any,
) -> int | None:
    """Fold an orphan fetch stub into ``survivor_ref_id`` on a dup hit.

    The OA fetcher names each downloaded PDF after the stub's
    ``cite_key`` (``fetch_oa._stub_filename``). When that PDF
    re-extracts to content already owned by a *different* ref, the
    slow-path :func:`probe_existing` matches the survivor by
    ``content_hash`` and the stub the fetch was *for* is never touched
    — it keeps ``pdf_sha256 IS NULL`` and re-qualifies for fetching
    forever (a zombie). The two refs can't be deduped by identifier:
    the stub carries DOI/arXiv/S2 (from chase), the survivor only
    content/pdf hashes (ingested from bytes).

    The filename's ``cite_key`` is the one reliable link back to the
    stub — when it resolves to a *separate* live stub, folds it into
    the survivor (migrate external identifiers + graph edges, record
    provenance, soft-delete), mirroring
    :meth:`precis.handlers.memory.MemoryHandler.supersede`.

    INVARIANT (prod incident 2026-09-27, ref 348633): a cite_key match
    is only ever a candidate here when it is (a) ``kind = 'paper'`` —
    the OA fetcher only ever mints ``paper`` stubs (see
    :func:`precis.ingest.paper_hygiene.requeue_stranded_fetches` for the
    same ``kind='paper'`` + ``pdf_sha256 IS NULL`` stub signature) — and
    (b) carries no live body chunks (``ord >= 0``), the definition of a
    metadata-only stub (see ``db_writer._has_body_chunks``). Draft slugs
    are also stored as ``id_kind='cite_key'`` identifiers, and a draft
    never has a ``pdf_sha256``; without this guard, ingesting a draft's
    own exported PDF (``<slug>.pdf``) matched the draft's cite_key here
    and retired it as a "content-duplicate-stub" of the new paper,
    destroying its (non-empty) body. Never relax either half of the
    guard to catch a same-kind duplicate — that's a different, identifier-
    based dedup path, not this filename fallback.

    Returns the merged stub's ref_id, or ``None`` when there's nothing
    to reconcile (a plain re-drop of an existing PDF, or the survivor
    *is* the stub just upgraded in place).
    """
    stem = file_stem.strip().lower()
    if not stem:
        return None
    row = conn.execute(
        """
        SELECT r.ref_id
          FROM ref_identifiers ri
          JOIN refs r ON r.ref_id = ri.ref_id
         WHERE ri.id_kind = 'cite_key'
           AND lower(ri.id_value) = %s
           AND r.kind = 'paper'
           AND r.pdf_sha256 IS NULL
           AND r.retired_at IS NULL
           AND r.ref_id <> %s
           AND NOT EXISTS (
                 SELECT 1 FROM chunks c
                  WHERE c.ref_id = r.ref_id
                    AND c.ord >= 0
                    AND c.retired_at IS NULL
               )
         LIMIT 1
        """,
        (stem, survivor_ref_id),
    ).fetchone()
    # A ``pa<id>`` stem is NOT handled here: the human named that ref, so it
    # must survive — see :func:`_fold_into_named_stub` (gr479362).
    if row is None:
        _warn_skipped_cite_key_match(conn, stem=stem, exclude_ref_id=survivor_ref_id)
        return None
    stub_id = int(row[0])

    # Lock both refs rows first, in id order: set/clear_ref_identifier
    # take refs before ref_identifiers, and this fold writes
    # ref_identifiers before the marker union writes refs — without a
    # common first lock the two orders deadlock (gr515246).
    conn.execute(
        "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
        "ORDER BY ref_id FOR NO KEY UPDATE",
        ([survivor_ref_id, stub_id],),
    )

    # Corroboration (gr264748): the filename/handle link alone folded
    # unrelated papers (pa42497 -> pa42939). Require the stub's title to
    # share distinctive tokens with the survivor's title or leading body.
    if not _stub_title_corroborated(conn, stub_id, survivor_ref_id):
        log.warning(
            "precis_add: not folding stub ref_id=%s into survivor ref_id=%s "
            "(stem=%r): stub title shares no distinctive tokens with the "
            "survivor's title/leading body — stub left live",
            stub_id,
            survivor_ref_id,
            stem,
        )
        return None

    # Identity (gr512169): token corroboration can't tell series
    # siblings apart — Part II shares Part I's distinctive tokens — so a
    # sibling's PDF minted under the stub's cite_key name would take the
    # stub's DOI. Require part markers + first author to agree before
    # the stub's identifiers move; on a mismatch quarantine the fetched
    # ref for review instead of corrupting the stub's identity.
    stub_title, stub_authors = _ref_identity(conn, stub_id)
    surv_title, surv_authors = _ref_identity(conn, survivor_ref_id)
    refusal = _stub_identity_refusal(stub_title, stub_authors, surv_title, surv_authors)
    if refusal is not None:
        log.warning(
            "precis_add: not folding stub ref_id=%s into survivor ref_id=%s "
            "(stem=%r): %s (stub %r vs fetched %r) — stub left live, "
            "survivor quarantined for review",
            stub_id,
            survivor_ref_id,
            stem,
            refusal,
            stub_title[:60],
            surv_title[:60],
        )
        _note_stub_reconcile_refused(
            store,
            survivor_ref_id,
            stub_id=stub_id,
            file_stem=stem,
            reason=refusal,
            conn=conn,
        )
        _raise_stub_identity_review(
            store,
            stub_id=stub_id,
            survivor_ref_id=survivor_ref_id,
            file_stem=stem,
            reason=refusal,
            conn=conn,
        )
        return None

    # Move external bibliographic identifiers onto the survivor so a
    # future probe_existing() dedups by DOI/arXiv/S2 directly — the very
    # gap that let this stub slip past. The PK is (id_kind, id_value),
    # so a straight UPDATE can't conflict: the survivor by definition
    # doesn't already own these exact values. Content-derived ids
    # (cite_key/paper_id) stay on the retired row; probe_existing now
    # filters soft-deleted refs so they can't resurrect it.
    # A value the survivor's operator deliberately cleared must not ride
    # across on the stub's rows — the fold would resurrect the very
    # identifier the human removed (gr515246). Same per-value for a pair
    # dropped at the drop_identifier door (gr517591).
    _drop_cleared_loser_rows(
        conn, survivor_ref_id=survivor_ref_id, loser_ref_id=stub_id
    )
    _drop_dropped_loser_rows(
        conn, survivor_ref_id=survivor_ref_id, loser_ref_id=stub_id
    )
    conn.execute(
        """
        UPDATE ref_identifiers SET ref_id = %s
         WHERE ref_id = %s
           AND id_kind IN ('doi', 'arxiv', 's2', 'pubmed', 'openalex')
        """,
        (survivor_ref_id, stub_id),
    )
    _merge_cleared_markers(conn, survivor_ref_id=survivor_ref_id, loser_ref_id=stub_id)
    _merge_dropped_markers(conn, survivor_ref_id=survivor_ref_id, loser_ref_id=stub_id)

    inherit_missing_identity(
        store,
        survivor_ref_id=survivor_ref_id,
        loser_ref_id=stub_id,
        source="ingest:dedup",
        conn=conn,
    )
    store.migrate_links(stub_id, survivor_ref_id, conn=conn)
    store.add_link(
        src_ref_id=survivor_ref_id,
        dst_ref_id=stub_id,
        relation="supersedes",
        set_by="agent",
        conn=conn,
    )
    store.stamp_ref_meta(
        stub_id,
        {"superseded_by": survivor_ref_id, "dedup": "content-duplicate-stub"},
        conn=conn,
    )
    store.retire_ref(stub_id, conn=conn)
    store.append_event(
        survivor_ref_id,
        source="ingest:dedup",
        event="stub_reconciled",
        payload={"stub_ref_id": stub_id, "cite_key": stem},
        conn=conn,
    )
    log.info(
        "precis_add: reconciled orphan stub ref_id=%s into survivor "
        "ref_id=%s (content-duplicate; cite_key=%s)",
        stub_id,
        survivor_ref_id,
        stem,
    )
    return stub_id


def _precis_add_pres(
    input: PresInput,
    *,
    store: Store,
) -> IngestResult | None:
    """Top-level dispatch for :class:`PresInput`.

    Mirrors the :class:`PdfInput` arm of :func:`precis_add`:
    compute sha → fast-path probe → on miss, acquire cross-host
    claim → run extract_pres → write_pres. The probe is the same
    ``probe_existing(pdf_sha256=...)`` query papers use; it returns
    whichever ref already owns these bytes regardless of kind. So
    a deck whose bytes already landed as a paper (extremely
    unlikely but possible if the operator dropped the same file in
    both ``papers/`` and ``presentations/``) returns an
    ``inserted=False`` for the existing paper ref. Tags still apply
    additively to whatever ref won.

    Returns ``None`` for the cross-host-claim-already-held case.
    """
    pdf_sha256 = _compute_pdf_sha256(input.pdf_path)
    if pdf_sha256 is None:
        return None

    with store.pool.connection() as conn:
        existing_ref_id = probe_existing(pdf_sha256=pdf_sha256, conn=conn)
    if existing_ref_id is not None:
        # Same bytes already ingested under some kind. Merge the
        # caller's extra_tags onto whichever ref owns them; do *not*
        # try to retag with ``subtype:slides`` because the existing
        # ref might be a paper that legitimately doesn't carry that
        # axis. Operator can manually relabel if they really intended
        # a re-ingest as pres.
        _apply_extra_tags(store, "pres", existing_ref_id, input.extra_tags)
        with store.pool.connection() as conn:
            result = _hit_result_from_db(existing_ref_id, conn=conn)
            stored_kind = _lookup_kind(existing_ref_id, conn=conn)
        return _with_kind(result, kind=stored_kind)

    if store.dsn is None:
        return _ingest_pres_pdf(input, store=store, pdf_sha256=pdf_sha256)

    with Claim(store.dsn, pdf_sha256) as claim:
        if not claim.acquired:
            log.info(
                "precis_add (pres): skipping %s — claimed by another host",
                input.pdf_path.name,
            )
            return None
        return _ingest_pres_pdf(input, store=store, pdf_sha256=pdf_sha256)


def _ingest_pres_pdf(
    input: PresInput,
    *,
    store: Store,
    pdf_sha256: str,
) -> IngestResult:
    """Slow path for a :class:`PresInput`, assuming the claim is held."""
    pres = extract_pres(
        input.pdf_path,
        slug_hint=input.slug_hint,
        title_hint=input.title_hint,
    )
    # Marker's own per-file sha can drift from our pre-claim hash if
    # someone touched the file mid-flight (unlikely but possible on
    # NFS retries). Trust the value we hashed for the claim — that's
    # what idempotency keys off.
    if pres.pdf_sha256 != pdf_sha256:
        log.warning(
            "precis_add (pres): sha drift for %s (claim=%s, post-marker=%s); "
            "trusting claim hash",
            input.pdf_path.name,
            pdf_sha256,
            pres.pdf_sha256,
        )
        from dataclasses import replace as _replace

        pres = _replace(pres, pdf_sha256=pdf_sha256)

    with store.pool.connection() as conn:
        result: PresWriteResult = write_pres(pres, conn=conn)
        conn.commit()

    # subtype:slides + caller tags, in one apply so the validation
    # batch is one round-trip.
    tags_to_apply: tuple[str, ...] = ("subtype:slides", *input.extra_tags)
    _apply_extra_tags(store, "pres", result.ref_id, tags_to_apply)

    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id="",
        pub_id=None,
        cite_key=result.slug,
        pdf_sha256=pres.pdf_sha256,
        content_hash=None,
        chunks_written=result.n_slides,
        identifiers={"cite_key": result.slug, "pdf_sha256": pres.pdf_sha256 or ""},
        kind="pres",
    )


def _compute_markup_sha256(markup_path: Path) -> str | None:
    """Content hash of the markup bytes — the cross-host claim key.

    Markup ingests have no PDF (the body comes from the markup), so the
    claim keys on the markup source instead of ``pdf_sha256``. Reuses
    :func:`make_pdf_sha256` (a plain sha256 over bytes) so ``Claim``'s
    hex-string key derivation applies unchanged. ``None`` if the bytes
    can't be read.
    """
    try:
        raw = Path(markup_path).read_bytes()
    except OSError:
        return None
    return make_pdf_sha256(raw)


def _precis_add_markup(
    input: MarkupInput,
    *,
    store: Store,
) -> IngestResult | None:
    """Top-level dispatch for :class:`MarkupInput`.

    Mirrors the :class:`PdfInput` arm but claims on the **markup**
    content hash (the chunk source) instead of a PDF hash, then runs the
    markup producer. Returns ``None`` when another host holds the claim.
    """
    markup_sha256 = _compute_markup_sha256(input.markup_path)
    if markup_sha256 is None:
        log.warning(
            "precis_add (markup): %s vanished before hash; skipping",
            input.markup_path.name,
        )
        return None

    if store.dsn is None:
        return _ingest_markup(input, store=store)

    with Claim(store.dsn, markup_sha256) as claim:
        if not claim.acquired:
            log.info(
                "precis_add (markup): skipping %s — claimed by another host",
                input.markup_path.name,
            )
            return None
        return _ingest_markup(input, store=store)


def _note_markup_outcome(
    store: Store,
    ref_id: int | None,
    *,
    event: str,
    fmt: str,
    path: Path,
    **payload: Any,
) -> None:
    """Record how a markup trigger resolved, on the paper's own event log.

    The markup leg's whole claim over the PDF leg is that a non-entitled
    source fails *loudly* (``MarkupParseError``) instead of silently
    leaving an entitlement-preview body in place. That loudness used to
    exist only in this worker's log: ``get(kind='paper', view='log')``
    showed a ``fetch_ok`` for the XML and nothing about whether the XML
    ever became a body, so a one-page paper was indistinguishable from a
    full-text one without reading its chunks (gripe 372781).

    Best-effort by construction — a failure to journal must never fail an
    ingest that otherwise succeeded, and ``ref_id`` is ``None`` for a
    manual drop with no sidecar stub to attach to.
    """
    if ref_id is None:
        return
    try:
        store.append_event(
            int(ref_id),
            source=f"markup:{fmt}",
            event=event,
            payload={"fmt": fmt, "file": path.name, **payload},
        )
    except Exception as exc:  # pragma: no cover - journalling is best-effort
        log.warning(
            "precis_add (markup): could not journal %s for ref_id=%s: %s",
            event,
            ref_id,
            exc,
        )


def _ingest_markup(
    input: MarkupInput,
    *,
    store: Store,
) -> IngestResult | None:
    """Run the markup producer and write / fold the resulting paper.

    On a :class:`~precis.ingest.markup.MarkupParseError`, logs, attempts
    the companion-PDF OCR recovery (see
    :func:`_recover_markup_parse_failure`, gr161905), and returns ``None``
    so the ref stays claimable.
    """
    from precis.ingest.markup import MarkupParseError
    from precis.ingest.pipeline import extract_paper_from_markup

    try:
        paper = extract_paper_from_markup(
            input.markup_path,
            fmt=input.fmt,
            source_url=input.source_url,
            identifiers=input.identifiers,
        )
    except MarkupParseError as exc:
        log.error(
            "precis_add (markup): %s parse failed (%s) for %s; attempting "
            "companion-PDF OCR recovery",
            input.fmt,
            exc,
            input.markup_path.name,
        )
        _note_markup_outcome(
            store,
            input.fold_ref_id,
            event="markup_parse_failed",
            fmt=input.fmt,
            path=input.markup_path,
            error=str(exc)[:300],
        )
        _recover_markup_parse_failure(input, store=store)
        # The markup itself is unparseable and always will be — the paper's
        # body (if any) now comes from the companion PDF's OCR above, not this
        # trigger. Signal the watcher to retire the spent trigger + sidecar
        # rather than return a bare ``None`` it would mistake for claim
        # contention and leave in the inbox to be re-scanned forever.
        raise MarkupTriggerSpent(str(exc), fmt=input.fmt) from exc

    if input.as_kind != paper.kind:
        paper = replace(paper, kind=input.as_kind)

    with store.pool.connection() as conn:
        existing = probe_existing(
            paper_id=paper.paper_id,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            s2_id=paper.s2_id,
            pubmed_id=paper.pubmed_id,
            openalex_id=paper.openalex_id,
            pdf_sha256=paper.pdf_sha256,
            content_hash=paper.content_hash,
            conn=conn,
        )
        if existing is None and input.fold_ref_id is not None:
            existing = _valid_fold_stub(
                input.fold_ref_id, kind=paper.kind, conn=conn, doi=paper.doi
            )
            if existing is not None:
                log.info(
                    "precis_add (markup): folding %s into sidecar stub ref_id=%s",
                    input.markup_path.name,
                    existing,
                )
        if existing is not None:
            chunks_written = register_aliases_and_maybe_upgrade(
                existing, paper, conn=conn
            )
            _reconcile_orphan_stub(
                store,
                survivor_ref_id=existing,
                file_stem=input.markup_path.stem,
                conn=conn,
            )
            conn.commit()
            stored_kind = _lookup_kind(existing, conn=conn)
            _apply_extra_tags(store, stored_kind, existing, input.extra_tags)
            log.info(
                "precis_add (markup): %s hit existing ref_id=%s "
                "(fmt=%s, chunks_written=%d)",
                input.markup_path.name,
                existing,
                input.fmt,
                chunks_written,
            )
            # chunks_written == 0 here is the attach-only path (the ref
            # already had a body) — worth journalling precisely because
            # it looks identical to a successful body ingest otherwise.
            _note_markup_outcome(
                store,
                existing,
                event="markup_ingested",
                fmt=input.fmt,
                path=input.markup_path,
                chunks_written=chunks_written,
                ref_state="folded",
            )
            return _with_kind(
                _hit_result_from_db(
                    existing,
                    conn=conn,
                    fallback=paper,
                    chunks_written=chunks_written,
                    # ``chunks_written`` counts submitted rows (cards
                    # included, conflict-skips counted anyway); the body
                    # count is what tells the watcher a bodiless ref just
                    # got filled (gr510690).
                    body_chunks_written=(
                        sum(1 for c in paper.chunks if c.ord >= 0)
                        if chunks_written
                        else 0
                    ),
                ),
                kind=stored_kind,
            )

        result = write_paper(paper, conn=conn)
        conn.commit()

    _apply_extra_tags(store, paper.kind, result.ref_id, input.extra_tags)
    _note_markup_outcome(
        store,
        result.ref_id,
        event="markup_ingested",
        fmt=input.fmt,
        path=input.markup_path,
        chunks_written=result.chunks_written,
        ref_state="new",
    )
    log.info(
        "precis_add (markup): wrote new ref_id=%s from %s "
        "(fmt=%s, chunks=%d, printable=%s)",
        result.ref_id,
        input.markup_path.name,
        input.fmt,
        result.chunks_written,
        bool(paper.pdf_sha256),
    )
    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id=paper.paper_id,
        pub_id=paper.pub_id,
        cite_key=result.cite_key,
        pdf_sha256=paper.pdf_sha256,
        content_hash=paper.content_hash,
        chunks_written=result.chunks_written,
        identifiers=result.identifiers_written,
        kind=paper.kind,
        ref_has_body=any(c.ord >= 0 for c in paper.chunks),
        body_chunks_written=sum(1 for c in paper.chunks if c.ord >= 0),
    )


def _recover_markup_parse_failure(input: MarkupInput, *, store: Store) -> None:
    """gr161905: markup parsing just failed — recover deterministically
    instead of racing the companion PDF against Marker across hosts.

    The companion PDF (fetched alongside this trigger, tagged
    ``printable_only`` so it never independently races to write a body
    — :func:`precis.workers.fetch_oa._run_cascade`) is the intended OCR
    fallback. Two cases, resolved right now in this process, since we've
    just learned markup won't produce a body: **still in the inbox** (no
    watcher reached it) — clear ``printable_only`` so the next watcher
    runs Marker normally (unraced, since ``has_body`` is still
    ``False``); **already ingested** (attach-only printable,
    ``pdf_sha256`` set, no body) — re-run the full PDF ingest on the
    stored corpus copy synchronously.

    Best-effort throughout: any lookup failure means the pre-fix
    behavior (parse failure logs, ref stays claimable) — never raises.
    """
    if input.fold_ref_id is None:
        return  # manual drop, no sidecar — nothing to recover

    from precis.ingest.fetch_sidecar import read_sidecar, write_sidecar

    sidecar = read_sidecar(input.markup_path)
    if sidecar is None or not sidecar.companion_pdf:
        log.info(
            "precis_add (markup): no known companion PDF for %s — no "
            "automatic OCR recovery available",
            input.markup_path.name,
        )
        return

    companion = input.markup_path.parent / sidecar.companion_pdf
    if companion.is_file():
        # Read the companion's OWN sidecar and only flip printable_only —
        # its source/identifiers were recorded independently by whichever
        # PDF-cascade leg actually fetched it (e.g. fetcher:elsevier), NOT
        # by this markup trigger's leg (e.g. fetcher:europepmc_jats).
        # Reusing the markup sidecar's fields here would silently corrupt
        # the companion's `source`, defeating source-gated checks downstream
        # (e.g. watch.py's gr162364 Elsevier-truncation guard, which bails
        # out unless sidecar.source is the Elsevier fetcher).
        companion_sc = read_sidecar(companion)
        if companion_sc is None:
            log.warning(
                "precis_add (markup): companion %s has no sidecar of its "
                "own — cannot clear printable_only safely",
                companion.name,
            )
            return
        write_sidecar(
            companion,
            ref_id=companion_sc.ref_id,
            identifiers=companion_sc.identifiers,
            source=companion_sc.source,
            source_format=companion_sc.source_format,
            printable_only=False,
        )
        log.info(
            "precis_add (markup): cleared printable_only on companion %s "
            "for ref_id=%s — will OCR when a watcher next reaches it",
            companion.name,
            input.fold_ref_id,
        )
        return

    # Already left the inbox: it landed as an attach-only printable.
    # Locate the stored copy and OCR it now.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (input.fold_ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        log.warning(
            "precis_add (markup): companion %s already left the inbox but "
            "ref_id=%s has no pdf_sha256 — cannot locate the stored copy "
            "for OCR recovery",
            sidecar.companion_pdf,
            input.fold_ref_id,
        )
        return
    storage_path = store.pdf_storage_path(str(row[0]))
    if not storage_path or not Path(storage_path).is_file():
        log.warning(
            "precis_add (markup): no on-disk stored copy for ref_id=%s "
            "(storage_path=%r) — cannot recover",
            input.fold_ref_id,
            storage_path,
        )
        return
    stored = Path(storage_path)
    stored_sha256 = str(row[0])  # already the ref's recorded pdf_sha256
    log.info(
        "precis_add (markup): %s already attached printable-only with no "
        "body — running OCR fallback on the stored copy for ref_id=%s",
        stored.name,
        input.fold_ref_id,
    )
    # Call _ingest_pdf directly, NOT precis_add: precis_add's dispatch has
    # a fast-path pdf_sha256 probe that short-circuits (no Marker) exactly
    # because this hash is *already* registered (from the earlier
    # printable-only attach) — that optimization exists to skip re-OCRing
    # a byte-identical re-drop of an already body-bearing file, which is
    # the opposite of what we need here: this ref deliberately has no body
    # yet, and this is the one deliberate re-run of Marker to give it one.
    # Still claim on the hash, same as every other _ingest_pdf caller: a
    # second, independent fetch pass for the same stub (e.g. a later pass
    # landing a different markup leg) could race this same recovery
    # concurrently on another host.
    pdf_input = PdfInput(
        pdf_path=stored,
        extra_tags=input.extra_tags,
        as_kind=input.as_kind,
        fold_ref_id=input.fold_ref_id,
    )
    if store.dsn is None:
        _ingest_pdf(
            pdf_input,
            store=store,
            pdf_sha256=stored_sha256,
            use_pdf2doi=False,
            crossref_mailto="",
            s2_api_key="",
        )
        return
    with Claim(store.dsn, stored_sha256) as claim:
        if not claim.acquired:
            log.info(
                "precis_add (markup): OCR recovery for ref_id=%s already "
                "claimed by another host — skipping",
                input.fold_ref_id,
            )
            return
        _ingest_pdf(
            pdf_input,
            store=store,
            pdf_sha256=stored_sha256,
            use_pdf2doi=False,
            crossref_mailto="",
            s2_api_key="",
        )


def _with_kind(result: IngestResult, *, kind: str) -> IngestResult:
    """Return ``result`` with ``kind`` overridden so the caller
    sees what's actually stored. ``_hit_result_from_db`` defaults
    ``kind='paper'``; on the pres dispatch hit branch we look up
    the stored kind and apply it here."""
    from dataclasses import replace as _replace

    return _replace(result, kind=kind)


def _lookup_kind(ref_id: int, *, conn: Any) -> str:
    """Look up ``refs.kind`` for ``ref_id``. Used on the pres-dispatch
    hit branch so the returned :class:`IngestResult` reflects what
    actually got stored — the same bytes might already live as a
    paper if the operator cross-dropped."""
    row = conn.execute(
        "SELECT kind FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    if row is None:
        return "paper"
    return str(row[0])


def _ingest_metadata(
    input: PrecisAddInput,
    *,
    store: Store,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
) -> IngestResult:
    """Slow path for DOI / arXiv inputs (no PDF, no Marker, no claim)."""
    paper = _build_paper(
        input,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
    )

    with store.pool.connection() as conn:
        existing = probe_existing(
            paper_id=paper.paper_id,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            s2_id=paper.s2_id,
            pubmed_id=paper.pubmed_id,
            openalex_id=paper.openalex_id,
            pdf_sha256=paper.pdf_sha256,
            content_hash=paper.content_hash,
            conn=conn,
        )
        if existing is not None:
            return _hit_result_from_db(existing, conn=conn, fallback=paper)

        result = write_paper(paper, conn=conn)
        conn.commit()

    return IngestResult(
        ref_id=result.ref_id,
        inserted=True,
        paper_id=paper.paper_id,
        pub_id=paper.pub_id,
        cite_key=result.cite_key,
        pdf_sha256=paper.pdf_sha256,
        content_hash=paper.content_hash,
        chunks_written=result.chunks_written,
        identifiers=result.identifiers_written,
        ref_has_body=any(c.ord >= 0 for c in paper.chunks),
        body_chunks_written=sum(1 for c in paper.chunks if c.ord >= 0),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_paper(
    input: PrecisAddInput,
    *,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
    marker_timeout_s: float | None = None,
    target_title: str = "",
    target_doi: str | None = None,
) -> PaperToWrite:
    """Dispatch on the input variant and run the matching pipeline producer.

    The pipeline producers live in :mod:`precis.ingest.pipeline` and
    pull in the paper-extra dep tree (marker-pdf, pymupdf, habanero,
    semanticscholar, rapidfuzz). Import is deferred to here so the
    rest of the CLI keeps loading on a bare install — see the
    module-level note.

    ``marker_timeout_s`` (P2-3) only matters for the :class:`PdfInput`
    branch — forwarded to :func:`extract_paper`.
    """
    from precis.ingest.pipeline import (
        extract_paper,
        fetch_paper_by_arxiv,
        fetch_paper_by_doi,
    )

    if isinstance(input, PdfInput):
        paper = extract_paper(
            input.pdf_path,
            use_pdf2doi=use_pdf2doi,
            printable_only=input.printable_only,
            marker_timeout_s=marker_timeout_s,
            kind=input.as_kind,
            target_title=target_title,
            target_doi=target_doi,
            slug_hint=input.slug_hint,
        )
        # DRY: the identical pipeline lands the doc under whichever kind
        # the caller asked for (``paper`` default, ``cfp`` for the
        # requirements flow). Only ``refs.kind`` differs downstream.
        if input.as_kind != paper.kind:
            paper = replace(paper, kind=input.as_kind)
        return paper
    if isinstance(input, DoiInput):
        return fetch_paper_by_doi(input.doi, crossref_mailto=crossref_mailto)
    if isinstance(input, ArxivInput):
        return fetch_paper_by_arxiv(input.arxiv_id, s2_api_key=s2_api_key)
    raise TypeError(f"Unsupported input type: {type(input).__name__}")


def _maybe_patch_pdf(pdf_path: Path, paper: PaperToWrite) -> PaperToWrite:
    """Run the PDF metadata write-back and return an updated
    ``PaperToWrite`` reflecting whichever hash is now canonical.

    If the patch ran and produced new bytes, the file on disk now
    carries the resolved canonical identifiers (title / authors /
    DOI), the post-patch hash becomes the canonical
    ``pdf_sha256``, and the pre-patch hash is prepended to
    ``pdf_sha256_aliases`` so re-ingest of either byte sequence
    still hits the fast-path probe.

    If the patch was skipped (signed PDF, encrypted, no-op, env
    off-switch, error), ``paper`` is returned unchanged — the
    pre-existing ``paper.pdf_sha256`` stays canonical and there's
    nothing to alias.
    """
    info = PatchInfo(
        title=paper.title or None,
        authors=_format_authors_for_pdf(paper.authors),
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
    )
    outcome = patch_pdf_metadata(pdf_path, info, pre_hash=paper.pdf_sha256)
    if outcome.post_hash is None:
        return paper
    return replace(
        paper,
        pdf_sha256=outcome.post_hash,
        pdf_size_bytes=outcome.post_size,
        pdf_sha256_aliases=[outcome.pre_hash, *paper.pdf_sha256_aliases],
    )


def _format_authors_for_pdf(authors: list[dict[str, Any]] | None) -> list[str]:
    """Pull a flat list of surname strings out of the ref's authors
    JSON for the PDF ``Author`` field. Different pipelines (CrossRef,
    S2, Marker) use different key names, so we probe a small set.
    """
    if not authors:
        return []
    out: list[str] = []
    for entry in authors:
        if not isinstance(entry, dict):
            continue
        name = (
            entry.get("family")
            or entry.get("last")
            or entry.get("name")
            or entry.get("full")
            or ""
        ).strip()
        if name:
            out.append(name)
    return out


def _apply_extra_tags(
    store: Store,
    kind: str,
    ref_id: int,
    tags: tuple[str, ...],
) -> None:
    """Apply open tags to ``ref_id`` in a post-commit transaction.

    Used by both the paper and pres ingest paths to push the
    watcher's ``tagging/<slug>/`` (and ``books/`` sentinel) tags
    onto the ref. Runs outside the writer's transaction so a
    constraint violation here doesn't roll back the just-written
    ingest — by the time we get here the ref is durable and
    re-runs apply the same tags idempotently via
    ``ON CONFLICT DO UPDATE`` inside :meth:`Store.add_tag`.

    Failures are logged but not raised: a tagged ingest where the
    tag step fails is still better than a rolled-back ingest, and
    the next watcher pass over the same PDF (sha256 hit path) will
    re-attempt.
    """
    if not tags:
        return
    # Deferred import — ``precis.handlers`` pulls in the dispatch
    # graph; we keep the ingest module importable without it for
    # the bare-install path (``precis migrate`` / ``precis serve``).
    from precis.utils.link_tag_ops import apply_tag_ops

    try:
        apply_tag_ops(
            store,
            kind,
            ref_id,
            tags=list(tags),
            untags=None,
        )
    except Exception:
        log.exception(
            "precis_add: failed to apply extra_tags %r to ref_id=%d (kind=%s)",
            tags,
            ref_id,
            kind,
        )


def _compute_pdf_sha256(pdf_path: Path) -> str | None:
    """Compute ``pdf_sha256`` for ``pdf_path``.

    Returns the hex digest, or ``None`` if the bytes can't be read
    (missing file / permission denied). The fast-path probe and the
    cross-host claim both need this value early — keeping the read
    in one place means we hash the file at most once per ingest.
    """
    try:
        pdf_bytes = Path(pdf_path).read_bytes()
    except OSError:
        return None
    return make_pdf_sha256(pdf_bytes)


def _hit_result_from_db(
    ref_id: int,
    *,
    conn: Any,
    fallback: PaperToWrite | None = None,
    chunks_written: int = 0,
    body_chunks_written: int = 0,
    pdf_promoted: bool = False,
) -> IngestResult:
    """Build an ``inserted=False`` result by re-fetching the existing
    ref's identifiers from the DB.

    The values returned reflect what's already stored, not what the
    pipeline freshly computed (the latter might disagree on
    cite_key suffix, etc.). ``fallback`` is consulted only for the
    rare case where ``ref_identifiers`` is missing a row we expect
    (e.g. ``paper_id`` was never written) — defensive belt-and-
    braces for the slow path. The fast path passes ``fallback=None``
    because no pipeline has run yet.

    ``chunks_written`` is non-zero only on the stub-upgrade path
    (see ``register_aliases_and_maybe_upgrade``); the
    ``inserted=False`` flag still holds — the *ref* existed, we
    just enriched it.
    """
    rows = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
        (ref_id,),
    ).fetchall()
    identifiers: dict[str, str] = {kind: value for kind, value in rows}

    if fallback is not None:
        paper_id = identifiers.get("paper_id", fallback.paper_id)
        pub_id = identifiers.get("pub_id", fallback.pub_id)
        cite_key = identifiers.get("cite_key", fallback.cite_key_prefix)
        pdf_sha256 = identifiers.get("pdf_sha256", fallback.pdf_sha256)
        content_hash = identifiers.get("content_hash", fallback.content_hash)
    else:
        paper_id = identifiers.get("paper_id", "")
        pub_id = identifiers.get("pub_id")
        cite_key = identifiers.get("cite_key", "")
        pdf_sha256 = identifiers.get("pdf_sha256")
        content_hash = identifiers.get("content_hash")

    return IngestResult(
        ref_id=ref_id,
        inserted=False,
        paper_id=paper_id,
        pub_id=pub_id,
        cite_key=cite_key,
        pdf_sha256=pdf_sha256,
        content_hash=content_hash,
        chunks_written=chunks_written,
        identifiers=identifiers,
        # gr510690 — propagate the fallback flags through hit results too:
        # a stub-fold whose Marker run fell back to fitz and still found
        # nothing must not read as a plain "duplicate" to the watcher.
        used_marker_fallback=bool(
            fallback and fallback.meta.get("extract_used_fallback")
        ),
        fallback_empty_body=bool(
            fallback and fallback.meta.get("extract_fallback_empty")
        ),
        ref_has_body=_ref_has_body(ref_id, conn=conn),
        body_chunks_written=body_chunks_written,
        pdf_promoted=pdf_promoted,
    )


__all__ = [
    "ArxivInput",
    "DoiInput",
    "IngestResult",
    "MarkupInput",
    "PdfInput",
    "PrecisAddInput",
    "PresInput",
    "precis_add",
]
