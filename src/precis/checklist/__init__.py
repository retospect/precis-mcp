"""The ``checklist`` kind's status engine and per-kind bridges.

Design-of-record: ``docs/backlog/checklist-kind.md`` (slices 1-3). The
tables, verbs and sync live in ``handlers/checklist.py``,
``store/_checklist_ops.py`` and ``jobs/checklist_sync.py``; this package
holds the one piece two callers need identically — **how a target's
per-item status is derived** — plus the per-kind code that turns a
checklist from a generic ledger into a gate on a real design.

Why a separate package and not more handler code: the handler renders
status, and the ``checklist_clean`` ``auto_check`` evaluator
(``workers/auto_check_evaluators/checklist_clean.py``) decides a todo on
exactly the same status. Evaluators import the store, never a handler
(a handler needs a ``Hub``), so the derivation has to live below both.

Two layers, deliberately distinct (Reto, design session):

* **Tool items** are bridges to a domain's own encoded rules — DRC,
  route status, netlist exceptions. The checklist never decomposes them;
  each checker is ONE coarse item whose status is read LIVE off the
  checker's durable record (:mod:`precis.checklist.pcb`), run id as the
  evidence. A checker that cannot fire (no board, no run yet) says so
  — ``not run (<reason>)`` — never "clean". A ``waived`` ledger verdict
  with rationale is the only thing that overrides a live ``fail``.
* **Judgment items** are LLM eval tasks the agent works and argues; their
  status is the ledger's latest live verdict, judged against the item's
  current rev (``item_rev < current`` → ``stale (item revised)``) and the
  target's current fingerprint (``stale (target changed)``).

Fingerprints are computed by the target kind's bridge (pcb: a sha256
over the name-anchored design graph — refdes, net names, members, poses;
never row ids, because ``pcb_apply`` rebuilds rows on every put). A
verdict stores the fingerprint AND the ``anchors`` it was narrowed to, so
a read recomputes the current digest over the same scope: a verdict
anchored to ``U3`` goes stale when U3 or its nets change, not when an
unrelated resistor moves. Unanchored verdicts cover the whole target and
stale on any edit — honest, and why the skill tells agents to anchor.
Kinds without a bridge fall back to the caller-supplied opaque
``fingerprint=`` of slice 1 (stored, compared, never computed).

Judgment is never automated here. The ``route()`` LLM path is not
called: the agent reading the status view IS the evaluator, and the
argument thread (``checklist_notes``) is where its reasoning lands.
"""

from __future__ import annotations

from precis.checklist.status import (
    ItemStatus,
    bridge_for,
    compute_statuses,
    current_fingerprint,
)

__all__ = ["ItemStatus", "bridge_for", "compute_statuses", "current_fingerprint"]
