"""precis-chem — the chemistry / protein tool-pack (ADR 0056).

A first-party **plugin** on the precis substrate (design-of-record
``docs/design/chem-tools-integration.md``): it snaps in through the
three plugin entry-point groups (``precis.handlers`` /
``precis.job_types`` / ``precis.migrations``) declared in the
precis-mcp ``pyproject.toml``, so ``dispatch.py`` and the core kind
catalogue stay untouched. It rides the two seams shipped for exactly
this (``KindSpec.can_own_jobs`` + the open relation vocabulary).

Slice 1 (this package) is the **retrosynthesis `route` kind** + a
``retrosynth`` job that plans a synthetic route to a target molecule.
It ships **dark** behind ``PRECIS_CHEM_ENABLED`` (the ``route`` kind's
``requires_env``) so the merge is inert until the flag is set. The
heavy engines (AiZynthFinder, ASKCOS, …) live behind the ``[chem]``
extra and are lazy-imported only on the compute node that runs the
job; the always-on request path needs none of them — a deterministic
in-process ``stub`` engine proves the compute-lane round-trip + the
content-addressed cache without a cluster or a built image.

See ADR 0056 and the design doc for the canonical-`route`-kind
decision, the two-engine-styles split, and the build order (slices).
"""

from __future__ import annotations

from precis_chem.route import RouteHandler

__all__ = ["RouteHandler"]
