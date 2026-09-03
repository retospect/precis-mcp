"""precis-se — the ``se`` (structural envelope) kind.

A first-party **plugin** on the precis substrate (Route B: entry points,
own migration namespace, dark behind a ``requires_setting`` flag — the
``precis_nm`` scaffold verbatim), so core dispatch stays untouched.

``se`` is the scale-agnostic sibling of ``nm`` — the symmetry that locates
it: **se : cad :: nm : structure** (docs/backlog/se-kind.md). nm is
intent-over-atoms renting the cad kernel as Å; se is intent-over-solids
renting the same kernel as **metres** (float64 everywhere — see
se-kind.md "Decisions": within ±10⁶ m of origin float64 metres resolves
below 10⁻⁴ Å, atoms-to-buildings in one unit; the single declared
conversion anywhere is the Å↔m multiply where an atomic-mode block binds
an nm design). A design is a deliberately *suggestive* space plan
("a fork about this size, connected to a hub that goes through a wheel so
the wheel can rotate") that hardens monotonically as answers arrive —
every field beyond a block's name is optional; validation reports absence
(filled-fraction honesty) but never fails on it.

**The IR — six levels** (same invariant as pcb/nm: dropping everything
above level *k* leaves a valid level-*k* object):

- **L0 — block graph.** Blocks + ports + intent connections, hierarchical
  (module trees, template refs, array nodes). No geometry.
- **L1 — envelopes + pose.** Per-block analytic envelope (the cad
  mini-DSL, reused verbatim, metres) + rough pose.
- **L2 — declared invariants.** Joints (kinematic class × mechanism),
  tolerances as relations between named measures, loads as objective
  vectors — stored explicitly, never derived from L3 geometry.
- **L3 — realized solids.** Per block: cad node sets, instanced
  templates, ``component`` bindings, or (atomic mode) a bound ``nm``
  design.
- **L4 — metrics/agreement.** ``envelope_fit``, interface fit, stack-up,
  design DRC — the realized solid checked against the spec, never stored
  twice.
- **L5 — fabrication plan.** Manufacturing mode, build frame, process
  DRC, export.

This package (slices 1–2, se-kind.md "Ship order") covers the scaffold
plus the L0/L1 core: :mod:`precis_se.handler` (``SeHandler``, the ``se``
kind — tree CRUD; tree/block/ports/validate/clearance views, clearance
renting the cad kernel's exact-sign SDF at metres),
:mod:`precis_se.ops` (pure typed-op application over an in-memory tree —
no store access: ``add_block``/``instance_block``/``array_block``/
``set_pose``/``set_envelope``/``remove_block``/``add_port``/
``remove_port``/``connect``/``disconnect``, with nm's instancing cycle
guards and se's first-class **arrays**: an array node carries template
name + ``linear`` (count/pitch/axis) or ``polar`` (count/radius/axis),
members derived at read time — realization never flattens the tree),
:mod:`precis_se.validate` (read-time feasibility findings incl. the
undeclared-interpenetration geometry check; rendered under the
filled-fraction honesty header), :mod:`precis_se.persist`
(retire-all/reinsert-all store write-back, name-keyed identity, ports in
lockstep with fresh block ids), and migration ``0001_se_kind.sql``.

Ships **dark** behind the ``se.enabled`` setting (the ``se`` kind's
``requires_setting``; DB row → ``PRECIS_SE_ENABLED`` env fallback) — the
kind is hidden from the catalogue/dispatcher until the flag is set. See
``docs/backlog/se-kind.md`` for the full design (annotations superset
registry, joint split, manufacturing modes, the propose/interrogate
loop); the agent-facing skill lands last (ship order step 7). Unshipped
past this round: joint kinematic-class/mechanism schema, measures/
tolerances, loads, graph-tier DRC, notes ledger, ``se_propose``,
modes/process DRC, realization bindings, the profile tier.
"""

from __future__ import annotations

from precis_se.handler import SeHandler

__all__ = ["SeHandler"]
