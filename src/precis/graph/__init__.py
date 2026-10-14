"""Graph layer — computation over the refs/links/tags graph, above store.

Packages under ``precis.graph`` read the graph the store persists and
produce derived judgments (candidates, proposals, plans) — they sit
between :mod:`precis.store` (rows in, rows out) and :mod:`precis.workers`
(scheduled claim/drain loops). A graph package may import ``precis.store``
and ``precis.utils`` (and sibling domain packages like
``precis.taxonomy``); it must not import ``precis.handlers`` or
``precis.workers`` — callers reach in, never the other way.

Members:

- :mod:`precis.graph.garden` — the proposals-only graph gardener.
"""

from __future__ import annotations
