"""Ingest pipeline: PDF → refs → chunks → derived queue.

Public surface mirrors the pipeline stages:

* :func:`precis.ingest.add.precis_add` — the v2 ingest entry point
  (PDF / DOI / arXiv). See :mod:`precis.ingest.add` for the
  three-way dispatch.
* :class:`precis.ingest.add.IngestResult` — outcome of a single
  ``precis_add`` call (success or idempotent skip).
* :func:`precis.ingest.blocks.classify_density` and
  :func:`precis.ingest.blocks.fill_embeddings` — reusable block
  helpers shared by paper and patent ingest pipelines.

After every successful paper result, including a duplicate retry,
``precis_add`` attempts staged (URL-backed draft) finding promotion against
the committed
body. A matching source and quote enter ordinary citation chase; retained
verifier metadata never establishes trust. Bodyless papers safely leave staged
findings untouched (``finding_drafts.py``).

Short held article previews can take a longer agent-supplied PDF through
``paper_pull``. Hash-pinned ingest replaces the body only on growth, keeping
the old PDF alias and journaling both hashes and the source URL. Failed
downloads or extractions leave the old body available.

Identifier trust (td730803): a prefixed ``arxiv:``/``doi:`` S2 single-id
lookup must echo the requested id, so its record always carries it. An
S2-attributed DOI on a stub put is kept only if the DOI registries
corroborate it; an ``10.48550/arxiv.<id>`` mint self-corroborates only
when ``<id>`` is the record's own arXiv id (another preprint's mint drops
outright). Otherwise an ordinary DOI survives an inconclusive registry check
(miss, outage, tokenless or missing titles, unusable bylines); only a clear
title/author disagreement drops it. A ``doi:`` put whose S2 cluster would add
arXiv/S2 aliases is registry-checked too, and a clear mismatch discards the
cluster.

The legacy ``.acatome`` bundle parser that this package re-exported
through B6 was deleted in B7. Callers that still need bundle
parsing should pin to ``precis<0.7``; otherwise migrate to the
direct ingest path via :func:`precis_add`.

Misfiled paper → presentation conversion (``pres_convert``) re-extracts the
held PDF into one chunk per slide and retires the paper in one transaction.
Compatible ref links carry over; chunk anchors and evidence pins refuse
because Marker's page estimates cannot identify PDF slides. Evidence
relations excluding ``pres`` also refuse. The original chunks remain on
the tombstone, and the new presentation retains a stable cite key even
when the paper had none.

Cite-key and notice-slug allocation count aliases as occupied to preserve
existing citations. The partial trigram index on ``ref_identifiers.id_value``
covers both identifier kinds so these prefix probes are eligible to use it.

Discovery layer (F20)
---------------------

Per-chunk KeyBERT keywords supersede the dropped ``ref_segments`` /
``ref_segment_sentences`` tables (migration
``0003_drop_legacy_segments``): ``chunks.keywords TEXT[]`` (canonical lower-case,
GIN-indexed) + ``chunks.keywords_meta JSONB`` (versioned envelope of
short/long pairs + KeyBERT scores), filled by the ``chunk_keywords`` worker
(:mod:`precis.workers.chunk_keywords`). The claim query re-claims any chunk
whose ``keywords_meta`` version or ``content_sha`` no longer matches, so
bumping ``KEYWORDS_VERSION`` lazily re-derives the whole corpus.
``view='toc'`` DP-clusters the keyword arrays at request time — papers via
:func:`precis.utils.toc_db.render_from_store` (no precomputed segment
rows), skills via :mod:`precis.utils.toc` (LRU-memoised; skill files are
static for the process lifetime). Policy:
``docs/conventions/discovery-layer-policy.md``.

Hygiene: pysbd sentence splitting in the chunker fallback chain
(:mod:`precis.ingest.text_chunker`); dehyphenation across line breaks in
``marker._clean_text``.
Marker's pagination markers preserve physical page boundaries. Before
merging blocks, paper extraction drops recognised leading advert pages
when the target title and abstract confirm the next page is the article
opening. Exact OCR sentence loops are removed before splitting so
fragments of the loop cannot become searchable chunks.
arXiv HTML can display citation numbers that disagree with their linked
bibliography entries. Before flattening the HTML, ``markup.parse_arxiv_html``
uses the destination labels for fully linked numeric citation groups and
emits each structured bibliography item separately for ``bib_parse``.
Repeated numeric labels trigger the markup fallback because citation
storage has one number namespace per paper.

Figure alt text and captions are authored inside the dependency stack,
not in this package: marker-pdf's surya-ocr VLM emits them from its
bundled prompt set (``PROMPT_MAPPING`` inside the ``surya-ocr``
distribution). ``marker`` only parses whatever alt/caption text the
returned markdown carries, so a caption-prompt change is a dependency
bump, not an edit here (gr514334).

Marker table cleanup collapses malformed exact numeric repeats inside their
cells before splitting rows into chunks. A valid integer under a Count header
is shortened only when at least two malformed repeats and no ordinary
numeric cells corroborate duplication in its row. Single-digit pairs such
as 22 are always kept because their intended value is ambiguous.

Multi-article page spreads (gr463414): before Marker blocks become chunks,
:mod:`precis.ingest.article_spread` splits a spread into articles on
end/start evidence and keeps the one matching the target title or DOI. The
target is the fold stub's identity, else the extracted metadata. Stored
chunks are never trimmed: they are append-only, and the 2026-10-11 ruling
tags them instead. ``precis_add`` journals a ``trimmed`` / ``ambiguous``
``ingest:spread`` ref_event. Every cut needs a boundary plus independent
confirmation, so ordinary papers stay whole. An ambiguous spread keeps the
whole body and gets the ``multi-article`` tag, which finding links read as
an advisory. So does a trim that left an unconfirmed boundary inside the
kept span.
The bodiless heal (:mod:`precis.ingest.paper_hygiene`) re-extracts with the
ref's own title/DOI as the target and journals the same ``ingest:spread``
verdict.

Every ingest/edit author writer funnels through
:func:`precis.utils.authors.normalize_authors` (the canonical
``{given, family}`` shape) rather than an ad hoc ``{"name"}`` wrap, and
for ``kind='paper'`` the store projects the byline onto the
``paper_authors`` table (the truth; ``refs.authors`` is regenerated from
it — :mod:`precis.utils.authors` docstring; ``db_writer``'s raw insert
calls ``project_paper_authors`` itself, tagging the row ``source`` with
the lookup provider).
:mod:`precis.ingest.paper_meta_enrich` re-resolves a paper's authors plus
``meta.entry_type``/``journal``/``issn``, extra ``ref_identifiers``,
per-author ORCID links, and retraction status from one Crossref
(+conditional OpenAlex) fetch, on the :mod:`precis.workers.paper_meta_enrich`
cadence. Journal spelling comes from the shared Crossref journal record keyed
by ISSN (``journal_record``), with a 30-day background refresh even for
already-enriched papers; ambiguous source identities create review todos.
``meta.authors_resolved_at`` is its idempotency stamp and
``refs.human_verified_at`` its skip guard. It also fills
``refs.title``/``refs.year`` when — and only when — the ref has none of
its own (:func:`precis.identity.is_placeholder_title`), which is how a
DOI-only acquire's title-less stub ever gets a name.

Supplementary information (SI)
------------------------------

Reto's ruling (2026-10-03): found SI is always fetched and ingested, never a
placeholder; discovery runs where attention is (web paper open, MCP
``get(kind='paper')``, a fisheye ring walk), never as a corpus sweep; each
SI file is its own ``paper`` ref with ``pdf_role='supplement'``, linked to
the parent and cited as the parent. :mod:`precis.ingest.si_discovery` is
the pure discovery layer (Figshare, Crossref relation, component-DOI probe,
publisher landing-page patterns, ``safe_get`` only; a Cloudflare challenge
is a recorded miss). Publisher-specific supplement paths (including Springer
MOESM, MDPI s1, AIP and APS files) suffice without a supplement word in the
anchor: labels often contain only filenames. Non-PDF candidates remain in
discovery so ``si_fetch`` can record ``non_pdf`` with URL/source in metadata
and ``ref_events``; paper overviews display these skipped supplements without
sending ZIP/XLSX/CIF files through PDF ingestion.
:mod:`precis.workers.si_fetch` claims flagged parents
ahead of the stub backlog inside the fetch pass, downloads into the inbox
with a ``role: supplement`` sidecar, and records ``meta.si_checked_at`` /
``si_found`` so a paper is checked once. :mod:`precis.store.si_links` holds
the edge vocabulary: the link is ``part-of`` with ``links.meta.role =
'supplement'`` (the skill ``precis-relations`` defines that use; a
dedicated ``supplements`` pair would need a relations seed migration and a
backfill) and ``cite_target_for`` / ``ResolvedHandle.cite_public_id``
redirect a cite of the SI to its parent.
"""

from precis.ingest.add import IngestResult
from precis.ingest.blocks import (
    ParsedBlock,
    classify_density,
    fill_embeddings,
)

__all__ = [
    "IngestResult",
    "ParsedBlock",
    "classify_density",
    "fill_embeddings",
]
