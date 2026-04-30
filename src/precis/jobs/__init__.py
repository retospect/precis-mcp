"""``precis.jobs`` — single-shot maintenance entry points.

Phase 2 introduces this package to host the patent-watch runner. The
older bundled commands (``ingest-bundle``, ``ingest-bundles``,
``ingest-md``, ``import-perplexity``) still live inline in
``precis/cli.py`` and call into the relevant handler modules
directly; only ``patent_watch`` needs enough internals to warrant a
unit-testable top-level surface.

Modules:
    patent_watch     — saved-CQL watch runner.
    ingest_oracles   — bulk-load wisdom YAMLs into the ``oracle`` kind.
"""

from __future__ import annotations
