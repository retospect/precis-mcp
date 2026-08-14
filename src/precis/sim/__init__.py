"""``precis.sim`` — slice 1 of the sim-harness (``docs/backlog/sim-harness.md``).

Plain-CLI machinery (no job/worker/dispatch) that lets ``precis sim …``
drive external Pareto-sim repos (``lighterthanair``, ``flowsim``,
``flyinghose``, …):

- :mod:`precis.sim.manifest` — loads + validates each sim's own
  ``precis.sim.yaml`` (``run``/``outputs``/``verify``/``writeup``).
- :mod:`precis.sim.registry` — loads the precis-side registry mapping
  ``slug -> {path, git_remote, manifest, quest}``.
- :mod:`precis.sim.ingest` — projects a manifest's prose/CSV outputs into
  ``PRECIS_ROOT`` and drives the existing prose-ingest walker
  (``handler.ensure_ingested``, not the create-only ``put()``).
- :mod:`precis.sim.verify` — lit-searches precis (read-only) for each
  low-confidence ``verify:`` YAML entry, an LLM judge clears it, then
  writes back ``verified: true`` + ``source:`` (git-committed on a
  ``precis-verify/<date>`` branch), mints a ``material`` + ``citation``,
  and appends a quest deed. ``--dry-run`` renders the diff and writes
  nothing.

See ``src/precis/cli/sim.py`` for the verb wiring.
"""

from __future__ import annotations
