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
reaction coordinate (columns; ``RATE``/``SPAN`` precomputed).
``args={'U': x}`` (V vs RHE) re-levers ``analysis``/``profile``/``compare``
under CHE (``analysis.at_potential``: states shift by ``n_H·eU``, barriers
don't; refused on a pre-CHE graph). Loop + levers: skill
``precis-pathway-help``.

``results_json``/``graph_json`` are ``autocatpath.pipeline.analyze``'s
output (>= 0.5.2) verbatim — traps/poisons/selectivity/CHE/``score``
(>= 0.6.0) — not a local mirror. The aggregate additionally runs the
engine's **microkinetics** post-combine (``runner.run_kinetics_subprocess``,
mirrors the ``autocatpath kinetics`` CLI; feature-detected, engine >= 0.15;
failure → ``results_json.kinetics_error``, never fails the run). It runs in a
killable child under ``PRECIS_AUTOCATPATH_KINETICS_SECONDS`` (default 900, 0 =
unbounded in-process): the solve is a diagnostic bonus, and job 449981 showed
an unbounded one can hold a worker for 11h on work that measures 33s.
``_dispatch_common`` reduces this to the scalar summary quest harvests:
barrier/span, ``selectivity_margin``/``trap_margin``/``poison_margin``
(from ``results_json.score``), the CHE route scalars ``U_L``/``U_opt``/
``span_at_UL``/``span_at_Uopt`` gated on the barrier's trust blockers
(``electro_trusted``/``electro_note``; an untrusted value is stashed as
``{key}_untrusted_value``), and trust-gated kinetics scalars
``tof``/``log_tof``/band, ``kinetics_trusted``/``kinetics_note``/``drc_top``.

``surface_pourbaix`` + ``coverage_job`` are the surface-Pourbaix inner loop
(``docs/backlog/surface-pourbaix-staircase-optimizer.md`` slice 1): the
``surface_coverage_scan`` job runs catpath's ab-initio-thermodynamics
coverage scan for one MLIP model (the anchors, on a recorded footing:
``anchor_key``/``engine_version``/``corrections``) and pools every model's
scan on the same key into the CHE sweep — resting termination along U, each
boundary with its propagated and model-form bands kept apart.

``step_retry`` + the tail of ``aggregate_job`` are the step-level retry:
when the just-aggregated ``trust_summary`` names a quantity blocker that is
a fatal ``neb_convergence``/``relax_convergence`` record, the aggregate
re-queues ONLY that step at a fresh seed (a new ``autocatpath_seed`` job,
``params.only_steps``/``step_retry``, its own idem key) under a new
aggregate todo on the candidate, capped at ``PRECIS_PATHWAY_STEP_RETRIES``
(default 2) per step and model, each dispatch logged on the pathway ref's
``meta.step_retries``; the retry round's aggregate inherits the base
partials, swaps the failed step measurement for the narrowed retry and
persists onto the same pathway. Endpoint-mismatch-class records are never
retried (basin diagnosis, not a re-roll); infra/budget failures never reach
this path. Trade-off: the pinned engine has no single-step entry point, so
the retry seed still computes the whole network and precis narrows what it
records — the evidence and replacement semantics are step-scoped, the
compute saving waits on an engine step filter.

Gated on **dependency presence**, not a feature flag: with no
``autocatpath``/``[catalyst]`` extra installed the handler raises
``InitError`` at registration and the ``pathway`` kind just doesn't
appear — no ``ImportError`` at boot.
"""

from __future__ import annotations

from precis_pathway.handler import PathwayHandler

__all__ = ["PathwayHandler"]
