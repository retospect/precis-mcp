"""Ingest pipeline: PDF → refs → chunks → derived queue.

Vendored from ``acatome-extract`` (B3 of the v2 storage rewrite).
See ``docs/design/pip-merge.md`` for the file-by-file mapping.

Public surface is built up incrementally:

- B3a: leaf modules (citations, crossref, semantic_scholar,
  text_chunker, figures, annotations)
- B3b: coordination modules (literature, pdf_sidecar,
  verify_metadata, lookup, marker, pdf_metadata)
- B4: pipeline (rewritten for direct DB writes)
"""
