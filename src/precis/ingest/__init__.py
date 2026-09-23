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

The legacy ``.acatome`` bundle parser that this package re-exported
through B6 was deleted in B7. Callers that still need bundle
parsing should pin to ``precis<0.7``; otherwise migrate to the
direct ingest path via :func:`precis_add`.

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
cadence; ``meta.authors_resolved_at`` is its idempotency stamp and
``refs.human_verified_at`` its skip guard. It also fills
``refs.title``/``refs.year`` when — and only when — the ref has none of
its own (:func:`precis.identity.is_placeholder_title`), which is how a
DOI-only acquire's title-less stub ever gets a name.
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
