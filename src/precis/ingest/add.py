"""``precis_add()`` — single ingest entry point for the v2 schema.

Public API for the ingest pipeline. Wires the three pipeline
producers (``extract_paper`` / ``fetch_paper_by_doi`` /
``fetch_paper_by_arxiv``) to the v2 INSERT cascade
(:func:`precis.ingest.db_writer.write_paper`) with idempotency
checks via :func:`precis.ingest.db_writer.probe_existing`.

Atomic: every successful ingest commits exactly one transaction.
If the writer raises, the transaction rolls back and no rows
land. Caller (CLI / watch / future MCP tool) just sees the
exception.

Idempotent on two layers:

1. **Fast path** — for :class:`PdfInput`, the cheap ``pdf_sha256``
   is computed from bytes and probed against ``ref_identifiers``
   *before* Marker runs. A hit short-circuits without invoking
   the pipeline at all (saves ~30–60 s/PDF on duplicates). See
   ``docs/design/extract-once.md``.
2. **Slow path** — every identifier the pipeline assembled
   (paper_id, DOI, arXiv, S2, content_hash, …) is probed again
   after extraction. Catches "same paper, different bytes" cases
   that the fast path misses.

In either case a hit yields ``IngestResult(inserted=False,
ref_id=...)`` with identifiers re-fetched from ``ref_identifiers``
(so the result reflects what's actually stored, not what the
pipeline freshly computed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from precis.identity import make_pdf_sha256
from precis.ingest.claim import Claim
from precis.ingest.db_writer import (
    PaperToWrite,
    probe_existing,
    register_aliases_and_maybe_upgrade,
    write_paper,
)
from precis.ingest.pdf_writer import PatchInfo, patch_pdf_metadata
from precis.ingest.pres import (
    PresWriteResult,
    extract_pres,
    write_pres,
)
from precis.store import Store

log = logging.getLogger(__name__)

# NOTE: ``precis.ingest.pipeline`` imports are deferred into
# :func:`_build_paper` because that module pulls in the paper-extra
# deps (habanero, semanticscholar, rapidfuzz, pymupdf, marker-pdf).
# Keeping the import lazy here means ``precis serve`` /
# ``precis migrate`` / ``precis worker`` keep working on a bare
# install without the ``[paper]`` extra; only ``precis add`` /
# ``precis watch`` ever actually call into the pipeline and they
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


PrecisAddInput = PdfInput | DoiInput | ArxivInput | PresInput


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
) -> IngestResult | None:
    """Ingest one paper into the v2 schema.

    Dispatches on the input type:

    * :class:`PdfInput` — runs Marker + the metadata cascade via
      :func:`precis.ingest.pipeline.extract_paper`. The cheap
      ``pdf_sha256`` is probed against ``ref_identifiers`` *before*
      Marker so re-ingesting a known file short-circuits without
      paying for extraction (see ``docs/design/extract-once.md``).
    * :class:`DoiInput` — CrossRef-only fetch via
      :func:`precis.ingest.pipeline.fetch_paper_by_doi`.
    * :class:`ArxivInput` — Semantic Scholar via
      :func:`precis.ingest.pipeline.fetch_paper_by_arxiv`.
    * :class:`PresInput` — local slide PDF → ``kind='pres'`` via
      :func:`precis.ingest.pres.extract_pres`. Same ``pdf_sha256``
      probe as :class:`PdfInput`; on hit, idempotent. ``subtype:slides``
      and ``extra_tags`` applied post-commit.

    Returns ``None`` if another host already holds a
    :class:`precis.ingest.claim.Claim` on this PDF's ``pdf_sha256``
    — the caller should leave the file in place so the owning host
    can complete the work. ``None`` is *never* returned for
    metadata-only inputs (``DoiInput`` / ``ArxivInput``) since those
    are cheap and don't need cross-host claim coordination.
    """
    # Fast path: PDF inputs get a pre-Marker probe on pdf_sha256.
    # The hash is bytes-cheap (~1 ms/PDF); the probe is one round
    # trip. A hit here skips both extraction and the slow-path
    # probe entirely — that's the whole point of the optimisation.
    if isinstance(input, PresInput):
        return _precis_add_pres(input, store=store)

    if isinstance(input, PdfInput):
        pdf_sha256 = _compute_pdf_sha256(input.pdf_path)
        if pdf_sha256 is None:
            # File disappeared between watcher enqueue and now;
            # treat as a no-op so the caller doesn't fail loudly.
            return None
        with store.pool.connection() as conn:
            existing_ref_id = probe_existing(pdf_sha256=pdf_sha256, conn=conn)
        if existing_ref_id is not None:
            # Fast-path hit: re-applying ``extra_tags`` is the watcher's
            # signal that re-dropping a known PDF under a different
            # ``tagging/`` dir should merge tags additively (rather than
            # silently no-op'ing because the sha is known).
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
        # See ADR 0016. If ``store.dsn`` is None (unit-test path that
        # builds Store from a pre-made pool), we degrade to no-claim
        # — single-host correctness is preserved by the file-system
        # mutex used by ``_PdfHandler._enqueue``.
        if store.dsn is None:
            return _ingest_pdf(
                input,
                store=store,
                pdf_sha256=pdf_sha256,
                use_pdf2doi=use_pdf2doi,
                crossref_mailto=crossref_mailto,
                s2_api_key=s2_api_key,
            )

        with Claim(store.dsn, pdf_sha256) as claim:
            if not claim.acquired:
                log.info(
                    "precis_add: skipping %s — claimed by another host",
                    input.pdf_path.name,
                )
                return None
            return _ingest_pdf(
                input,
                store=store,
                pdf_sha256=pdf_sha256,
                use_pdf2doi=use_pdf2doi,
                crossref_mailto=crossref_mailto,
                s2_api_key=s2_api_key,
            )

    # Metadata-only paths (DOI / arXiv) — no claim needed.
    return _ingest_metadata(
        input,
        store=store,
        use_pdf2doi=use_pdf2doi,
        crossref_mailto=crossref_mailto,
        s2_api_key=s2_api_key,
    )


def _ingest_pdf(
    input: PdfInput,
    *,
    store: Store,
    pdf_sha256: str,
    use_pdf2doi: bool,
    crossref_mailto: str,
    s2_api_key: str,
) -> IngestResult:
    """Run the slow path for a PdfInput, assuming the claim is held."""
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
        if existing is None and input.fold_ref_id is not None:
            # No identifier-based dedup hit, but the OA-fetch sidecar named
            # the stub this PDF was fetched *for*. Fold into it directly
            # (promote in place) rather than minting a duplicate — the
            # deterministic, filename-independent path that survives the
            # multi-host inbox race. Guarded to a *live metadata-only stub*
            # of the same kind, so a stale / already-upgraded / soft-deleted
            # target falls through to a normal insert.
            existing = _valid_fold_stub(input.fold_ref_id, kind=paper.kind, conn=conn)
            if existing is not None:
                log.info(
                    "precis_add: folding %s into sidecar stub ref_id=%s "
                    "(no id dedup hit; keeping stub metadata)",
                    input.pdf_path.name,
                    existing,
                )
        if existing is not None:
            # Existing ref hit — never re-insert, but always (a) register
            # this ingest's pdf/content hashes as aliases and (b) when
            # the row is a stub (``pdf_sha256 IS NULL``), upgrade it
            # by promoting this PDF to canonical and inserting the
            # extracted chunks. See db_writer.register_aliases_and_maybe_upgrade
            # for the contract and docs/design/finding-chase.md
            # §"Stub upgrade" for the rationale (chase findings
            # waiting on this stub resume on the next chase pass
            # without any extra plumbing).
            chunks_written = register_aliases_and_maybe_upgrade(
                existing, paper, conn=conn
            )
            # If this PDF de-duped against a DIFFERENT ref than the stub
            # it was fetched for, fold that orphan stub into the survivor
            # now so it stops re-qualifying for OA fetch forever (the
            # zombie-stub spin-loop). No-op on a plain re-drop.
            _reconcile_orphan_stub(
                store,
                survivor_ref_id=existing,
                file_stem=input.pdf_path.stem,
                conn=conn,
            )
            conn.commit()
            stored_kind = _lookup_kind(existing, conn=conn)
            _apply_extra_tags(store, stored_kind, existing, input.extra_tags)
            return _with_kind(
                _hit_result_from_db(
                    existing,
                    conn=conn,
                    fallback=paper,
                    chunks_written=chunks_written,
                ),
                kind=stored_kind,
            )

        # Write-back: patch the on-disk file with the resolved canonical
        # metadata so a re-ingest from a clean DB still finds the right
        # DOI via embedded metadata (see ADR 0014). Honours
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
    )


def _valid_fold_stub(ref_id: int, *, kind: str, conn: Any) -> int | None:
    """Return ``ref_id`` iff it's a live metadata-only stub of ``kind``.

    The OA-fetch sidecar names a fold target, but by the time a watcher
    ingests the PDF that target may have moved on — already upgraded
    (``pdf_sha256`` now set), soft-deleted (merged away), or of a
    different kind (a mis-pointed sidecar). Any of those disqualify the
    direct fold; the caller falls back to a normal insert. Only a live
    ``pdf_sha256 IS NULL`` stub of the expected kind is a safe promote-
    in-place target.
    """
    row = conn.execute(
        """
        SELECT ref_id FROM refs
         WHERE ref_id = %s
           AND kind = %s
           AND pdf_sha256 IS NULL
           AND deleted_at IS NULL
        """,
        (ref_id, kind),
    ).fetchone()
    return int(row[0]) if row is not None else None


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
    the stub carries DOI/arXiv/S2 (from chase) while the survivor was
    ingested from bytes and carries only content/pdf hashes.

    The filename's ``cite_key`` is the one reliable link back to the
    stub. When it resolves to a *separate* live stub, fold it into the
    survivor — migrate external identifiers + graph edges, record
    provenance, soft-delete — mirroring
    :meth:`precis.handlers.memory.MemoryHandler.supersede`'s merge.

    Returns the merged stub's ref_id, or ``None`` when there's nothing
    to reconcile (the common case: a plain re-drop of an existing PDF,
    or the survivor *is* the stub that just got upgraded in place).
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
           AND r.pdf_sha256 IS NULL
           AND r.deleted_at IS NULL
           AND r.ref_id <> %s
         LIMIT 1
        """,
        (stem, survivor_ref_id),
    ).fetchone()
    if row is None:
        return None
    stub_id = int(row[0])

    # Move external bibliographic identifiers onto the survivor so a
    # future probe_existing() dedups by DOI/arXiv/S2 directly — the very
    # gap that let this stub slip past. The PK is (id_kind, id_value),
    # so a straight UPDATE can't conflict: the survivor by definition
    # doesn't already own these exact values. Content-derived ids
    # (cite_key/paper_id) stay on the retired row; probe_existing now
    # filters soft-deleted refs so they can't resurrect it.
    conn.execute(
        """
        UPDATE ref_identifiers SET ref_id = %s
         WHERE ref_id = %s
           AND id_kind IN ('doi', 'arxiv', 's2', 'pubmed', 'openalex')
        """,
        (survivor_ref_id, stub_id),
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
    store.soft_delete_ref(stub_id, conn=conn)
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
) -> PaperToWrite:
    """Dispatch on the input variant and run the matching pipeline producer.

    The pipeline producers live in :mod:`precis.ingest.pipeline` and
    pull in the paper-extra dep tree (marker-pdf, pymupdf, habanero,
    semanticscholar, rapidfuzz). Import is deferred to here so the
    rest of the CLI keeps loading on a bare install — see the
    module-level note.
    """
    from precis.ingest.pipeline import (
        extract_paper,
        fetch_paper_by_arxiv,
        fetch_paper_by_doi,
    )

    if isinstance(input, PdfInput):
        paper = extract_paper(input.pdf_path, use_pdf2doi=use_pdf2doi)
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
    from precis.handlers._link_tag_ops import apply_tag_ops

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
    )


__all__ = [
    "ArxivInput",
    "DoiInput",
    "IngestResult",
    "PdfInput",
    "PrecisAddInput",
    "PresInput",
    "precis_add",
]
