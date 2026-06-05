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
