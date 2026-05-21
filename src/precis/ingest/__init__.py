"""Ingest pipeline: PDF → refs → chunks → derived queue.

Vendored from ``acatome-extract`` (B3 of the v2 storage rewrite).
See ``docs/design/pip-merge.md`` for the file-by-file mapping.

Public surface is built up incrementally:

- B3a: leaf modules (citations, crossref, semantic_scholar,
  text_chunker, figures, annotations)
- B3b: coordination modules (literature, pdf_sidecar,
  verify_metadata, lookup, marker, pdf_metadata)
- B4: pipeline (rewritten for direct DB writes)

Legacy bundle parser (the pre-B3 ``src/precis/ingest.py``) lives in
``_legacy.py`` for the duration of the rewrite. Its public names
are re-exported here so existing callers keep working until B7
deletes the legacy bundle ingest path entirely.
"""

from precis.ingest._legacy import (
    IngestResult,
    ParsedBlock,
    ParsedBundle,
    author_strings,
    classify_density,
    fill_embeddings,
    mint_paper_slug,
    parse_bundle,
    read_bundle,
)

__all__ = [
    "IngestResult",
    "ParsedBlock",
    "ParsedBundle",
    "author_strings",
    "classify_density",
    "fill_embeddings",
    "mint_paper_slug",
    "parse_bundle",
    "read_bundle",
]
