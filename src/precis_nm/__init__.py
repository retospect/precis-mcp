"""precis-nm — the ``nm`` (nanomachine) kind.

A first-party **plugin** on the precis substrate (Route B: entry points,
own migration namespace, dark behind a ``requires_setting`` flag — the
``precis_chem`` skeleton verbatim), so core dispatch stays untouched.

``nm`` is the **fourth keystone kind** (glossary: "owns a legible IR and
rents the heavy kernel only at export; the LLM traverses a graph, never
pixels"), sibling to ``cad`` (ADR 0041) / ``pcb`` (0042) / ``structure``
(0043). Where those own solids / copper+silicon / atoms, ``nm`` owns
**hierarchical building blocks with spatial envelopes** — an LLM-guided
design surface for molecular machines (rotaxanes, molecular motors,
length-changing structures): describe the machine as nested blocks first
("disc, 2 Å high, 10 Å diameter; fork; axle joins them"), *then* fill each
envelope with real chemistry (lit-searched fragments attached at named
ports, ``structure`` designs), then validate, then read property/mechanism
views. Units are Ångström, float64, everywhere — no fixed-point (see
``docs/backlog/nm-kind.md`` "Decisions": rotations produce irrational
coordinates regardless, and the one thing fixed point buys — stable
equality for caching — is solved at the hash boundary only, never in the
representation).

**The IR — six levels, molecular content** (same invariant as ``pcb``:
dropping everything above level *k* leaves a valid level-*k* object; a move
at level *k* dirties only levels above):

- **L0 — block hypergraph.** Blocks + ports + intent connections. No
  geometry.
- **L1 — envelopes.** Per-block analytic envelope (the ``cad`` mini-DSL,
  Å) + rough pose; clearance/enclosure via ``cad/relate.py::component_sdf``.
- **L2 — topology & stereochemistry, stored explicitly.** Mechanical
  interlocking (a macrocycle threaded on an axle), declared DOF, chirality
  — never re-derived from L3 coordinates.
- **L3 — fragment placement.** Chosen chemical fragments posed inside
  envelopes, ports mapped to real attachment atoms.
- **L4 — metric annotations.** Measure values, charge estimates, strain.
- **L5 — realized atoms.** A full ``structure`` Scene: validated, relaxed.
  Fill state lives in ``structure``; ``nm`` owns L0–L4 and the binding.

This package (slice 3 round 1, ``docs/backlog/nm-kind.md`` "Slice 3
design") ships the **plugin skeleton + the block tree only** (L0–L1 minus
envelope clearance): :mod:`precis_nm.handler` (``NmHandler``, the ``nm``
kind), :mod:`precis_nm.ops` (pure typed-op application over an in-memory
block tree — no store access), :mod:`precis_nm.persist` (the store
write-back: load/save a design's tree, retire-all-reinsert-all on every
save, the ``card_combined`` search chunk), and the migration
(``migrations/0001_nm_kind.sql``) — which also creates the ``nm_ports`` /
``nm_topology`` tables now (forward-only discipline: no later ALTER) even
though ports/topology/clearance/``bind_structure`` are unwired until
rounds 2/3.

Ships **dark** behind the ``nm.enabled`` setting (the ``nm`` kind's
``requires_setting``; DB row → ``PRECIS_NM_ENABLED`` env fallback) so the
merge is inert until the flag is set. See ``docs/backlog/nm-kind.md`` for
the full design (unshipped — no ``nm`` skill exists yet; write one only
once the slice ships, per that doc's closing note).
"""

from __future__ import annotations

from precis_nm.handler import NmHandler

__all__ = ["NmHandler"]
