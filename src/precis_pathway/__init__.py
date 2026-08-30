"""precis-pathway — the reaction-pathway tool-pack.

Catalyst sibling of ``precis_bio``/``precis_chem``: a first-party
**plugin** snapping into the three plugin entry-point groups
(``precis.handlers`` / ``precis.job_types`` / ``precis.migrations``,
declared in precis-mcp's ``pyproject.toml``) — ``dispatch.py`` and the
core kind catalogue stay untouched.

**Glue only**: the ``pathway`` kind handler, the ``autocatpath_explore``
job, TOON/text views, persist, the native structure-ingest bridge. It
imports the **pure** ``autocatpath`` engine
(``autocatpath.structures``/``.neb``/``.network``/``.uncertainty``/
``.provenance``/…; extras ``precis-mcp[catalyst]``/``[catalyst-gpu]``) and
precis's own types directly — no cross-repo seam.

Tool surface is **TOON-first**: the LLM reads/argues the reaction network
as data (``format.toon`` tables, ``search``-shaped), never a picture.
``put(mode='preview')`` frames a network with no compute; ``view='analysis'``
is the objective the optimiser reads (rate-limiting Eₐ + selectivity +
confidence); ``view='compare'`` ranks candidates (rows) along the
reaction coordinate (columns; ``RATE``/``SPAN`` precomputed). Loop +
levers: skill ``precis-pathway-help``.

``results_json``/``graph_json`` are ``autocatpath.pipeline.analyze``'s
output (>= 0.5.2) verbatim — traps/poisons/selectivity/CHE/``score``
(>= 0.6.0) — not a local mirror. The aggregate additionally runs the
engine's **microkinetics in-process** post-combine (``runner.run_kinetics``,
mirrors the ``autocatpath kinetics`` CLI; feature-detected, engine >= 0.15;
failure → ``results_json.kinetics_error``, never fails the run).
``_dispatch_common`` reduces this to the scalar summary quest harvests:
barrier/span, ``selectivity_margin``/``trap_margin``/``poison_margin``
(from ``results_json.score``), trust-gated kinetics scalars
``tof``/``log_tof``/band, ``kinetics_trusted``/``kinetics_note``/``drc_top``.

Ships **dark** behind ``PRECIS_AUTOCATPATH_ENABLED`` (mirrors
``PRECIS_BIO_ENABLED``/``PRECIS_SANDBOX_ENABLED``): with the switch off or
no ``autocatpath``/``[catalyst]`` extra installed, the ``pathway`` kind
just doesn't appear — no ``ImportError`` at boot.
"""

from __future__ import annotations

from precis_pathway.handler import PathwayHandler

__all__ = ["PathwayHandler"]
