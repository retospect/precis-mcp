"""precis-chem — the chemistry / protein tool-pack (ADR 0056).

A first-party **plugin** on the precis substrate: it snaps in through the
three plugin entry-point groups (``precis.handlers`` /
``precis.job_types`` / ``precis.migrations``) declared in the
precis-mcp ``pyproject.toml``, so ``dispatch.py`` and the core kind
catalogue stay untouched. It rides the two seams shipped for exactly
this (``KindSpec.can_own_jobs`` + the open relation vocabulary). Each
external tool = a **kind** (the legible IR the LLM reads) + a
**job_type** (the heavy engine, off the request path on the ADR 0044
compute lane) — never a broker MCP server. Engines run on Linux compute
nodes only (Macs orchestrate); wrapper images build on-demand per node
(``docker/``, ``podman build``, no registry), model weights mount from
the NAS, never baked into the image.

Slice 1 (this package) is the **retrosynthesis `route` kind** + a
``retrosynth`` job that plans a synthetic route to a target molecule.
It ships **dark** behind ``PRECIS_CHEM_ENABLED`` (the ``route`` kind's
``requires_env``) so the merge is inert until the flag is set. The
heavy engines (AiZynthFinder, ASKCOS, …) live behind the ``[chem]``
extra and are lazy-imported only on the compute node that runs the
job; the always-on request path needs none of them — a deterministic
in-process ``stub`` engine proves the compute-lane round-trip + the
content-addressed cache without a cluster or a built image.

One canonical ``route`` IR, every engine normalizes to it: LinChemIn
runs *inside* the engine container (or a standalone normalizer container
for service engines) and emits a precis-canonical ``route.json`` —
``normalize.parse_syngraph`` is the single, dependency-free precis-side
reader shared by all engines. Transports (inprocess / container /
service) + engine adapters: :mod:`precis_chem.engine`. Known gap: the
read-time inverse-relation rewrite doesn't know plugin relations
(gripe 160213) — keep plugin relations symmetric or query the stored
direction. Rationale + rejected alternatives: ADR 0056.
"""

from __future__ import annotations

from precis_chem.route import RouteHandler

__all__ = ["RouteHandler"]
