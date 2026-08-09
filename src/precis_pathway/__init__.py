"""precis-pathway — the reaction-pathway tool-pack (bundle-pathway-in-tree
proposal, docs/backlog/bundle-pathway-in-tree-plugin.md).

The catalyst sibling of ``precis_bio``/``precis_chem``: a first-party
**plugin** on the precis substrate. It snaps in through the three plugin
entry-point groups (``precis.handlers`` / ``precis.job_types`` /
``precis.migrations``) declared in the precis-mcp ``pyproject.toml``, so
``dispatch.py`` and the core kind catalogue stay untouched.

This package is the **glue** — the ``pathway`` kind handler, the
``autocatpath_explore`` job, the TOON/text views, persist, and the native
structure-ingest bridge. The tool surface is **TOON-first**: the LLM reads
and argues with the reaction network *as data* (``format.toon`` tables, same
shape as ``search``), never a picture — ``put(mode='preview')`` frames a
network with no compute, ``view='analysis'`` is the objective the optimiser
reads (rate-limiting Eₐ + selectivity + confidence), ``view='compare'``
ranks candidates (rows) along the reaction coordinate (columns; ``RATE`` /
``SPAN`` precomputed). Loop + levers: skill ``precis-pathway-help``. It imports the **pure** ``autocatpath`` engine
(``autocatpath.structures``/``.neb``/``.network``/``.uncertainty``/
``.provenance``/…, installed via the ``precis-mcp[catalyst]``/
``[catalyst-gpu]`` extras) and precis's own types directly — same tree, no
cross-repo seam.

The artifact's ``results_json``/``graph_json`` come from
``autocatpath.pipeline.analyze`` (>= 0.5.2) — catpath's own analysis,
verbatim (traps / poisons / selectivity / CHE included), not a local
mirror; ``_dispatch_common`` then reduces it to the scalar summary the
quest harvests (barrier/span + ``side_span_margin``/``trap_depth``/
``poison_margin`` and their naming context).

It ships **dark** behind ``PRECIS_AUTOCATPATH_ENABLED`` (mirrors
``PRECIS_BIO_ENABLED`` / ``PRECIS_SANDBOX_ENABLED``), so the merge — and an
``autocatpath``-less venv with neither ``[catalyst]`` extra — is inert: the
``pathway`` kind simply doesn't appear, no ``ImportError`` at boot.
"""

from __future__ import annotations

from precis_pathway.handler import PathwayHandler

__all__ = ["PathwayHandler"]
