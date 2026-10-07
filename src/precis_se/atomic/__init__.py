"""``se`` **atomic mode** — the molecular-machine domain layer.

Read-only S1 surface deviation exposes the existing precis_surface judge over
stored structure-local Å atoms. Explicit caller targets retain rigid-z-only
alignment. With target omitted, only an exact evaluated hexfold_scene receipt
is used: analytic meridians and the applied canonicalization inverse including
reflection, never requested radius, fitting or planner reconstruction. Receipt
and geometry commit together; ref/version, cell and ordered atom-row identity
bind the receipt, so even byte-identical row replacement makes it unavailable.
Legacy, ambiguous or invalid receipts are unknown with explicit-target guidance.
SE pose is display placement and does not alter the measured target frame.
Structure identity/version, receipt, lattice and live positions come from one SQL
statement snapshot. Version brackets were refused: import saves can rewrite
at the same version, so equal labels do not prove coherent cell/atom reads.
Missing snapshot/version is unknown; missing design IDs cannot substitute a list.

The landing zone for the ``nm`` kind's domain code as it folds into ``se``
(docs/backlog/nm-se-merge.md). se and nm were built as siblings on the
symmetry ``se : cad :: nm : structure``; the units-policy cutover removed
the last substantive difference (both kinds store SI metres), leaving
duplicated scaffold and a which-kind-do-I-use decision point for every
agent. The merge keeps *one* design kind (``se``) whose ``atomic``
manufacturing mode (:mod:`precis_se.modes`) carries what ``nm`` owned:
blocks whose realized content is chemistry rather than solids, bound to
``structure`` designs at L3/L5.

This subpackage is the carve that keeps the merge from producing one
enormous file — the handlers are already ~77 KB (se) and ~85 KB (nm), so
nm's domain modules land here as submodules rather than inline:

- :mod:`precis_se.atomic.generators` — parametric block factories (the
  IC-design PCell: ``params → block``, the deterministic fill path).
  Å-native by design: generator math stays in ångström (the atomistic
  enclave convention, ``docs/backlog/structure-unit-enclave.md``), and
  the m↔Å crossing happens once, at the envelope's ingest boundary.
  The ``hexfold`` generator integrates the standalone topology-only
  ``.hx`` notation package (sheets/tubes/cones/fullerenes/defects/
  attachments as one spec text; geometry derived, never authored),
  vendored at ``src/hexfold`` (its own spec ``src/hexfold/spec.md``;
  never imports precis) and imported directly; ``params.fidelity``
  ``check|stick`` picks report-only vs stick-preview coordinates — see
  ``docs/backlog/hexfold-integration.md``.
- :mod:`precis_se.atomic.mechanics` — the L4 closed-form mechanics
  ceilings (Euler buckling, rupture force, bend stiffness). Signatures
  keep Å/nN/eV deliberately: these are cost terms *over* the geometry,
  not lengths *in* it (nm-se-merge.md "Explicitly NOT in scope").
- :mod:`precis_se.atomic.vocab` — the L2 statements only an atomic block
  makes (declared dof, threading, the bond capability gate), vetted for
  :mod:`precis_se.ops` to apply.
- :mod:`precis_se.atomic.validate` — the read-time chemistry findings
  (bond capability, structure bindings, bond geometry sanity, the
  ``envelope_fit`` L1↔L5 agreement check).
- :mod:`precis_se.atomic.bind` / :mod:`precis_se.atomic.generate` — the
  **store-aware** ops (``bind_structure``/``unbind_structure``, which
also measure a mapped port's own pose off the atom it resolves to — the
``pose_source='bound'`` half of the port pose slot — and
  ``generate``'s prepare/finish pair), and
  :mod:`precis_se.atomic.apply` — the walker that intercepts them for
  ``put``/``edit``.
- :mod:`precis_se.atomic.render` — ``view='mechanics'``/
  ``view='literature'`` plus the atomic filled-fraction line.
- :mod:`precis_se.atomic.propose` — the ``se_propose_atomic`` job type
  (nm's one ``nm_propose`` ``JobTypeSpec``, renamed for its se home):
  a tool-less LLM call that PROPOSES one block's chemistry as a
  dry-run-validated ``job_result``, and applies nothing.

Everything here is pure except :mod:`~precis_se.atomic.bind`,
:mod:`~precis_se.atomic.generate`, :mod:`~precis_se.atomic.render` and
:mod:`~precis_se.atomic.propose`, which take a store explicitly (the
last one via its job ``ctx``) — the ``ops.py`` discipline holds for the
op table itself, and the three ops that genuinely need the store are
intercepted before it.

**Storage and lifecycle.** Migration ``0007_se_atomic.sql`` adds the atomic
tables; the retired ``nm`` kind's storage is dropped by
``0008_se_drop_nm_tables.sql``, and ``nm`` answers with a retired-kind
pointer here (``precis.runtime.dispatch``'s ``_RETIRED_KINDS``). A bind also
*measures*: each mapped port takes the block-local position of the atom it
resolves to as its own pose (``pose_source='bound'`` — into an empty slot or
over an earlier bind's reading, never over a ``'declared'`` target, which is
what realization is checked against). Mapped with ``axis_atom``/
``phase_atom`` (the object form of ``bind_structure``'s ``ports=``: axle bond
``atom → axis_atom`` is the frame's z, ``atom → phase_atom`` projected off it
fixes the roll), the same bind independently measures the port's ``rot``
(``rot_source='bound'`` — its own provenance, never coupled to
``pose_source``; se migration 0014 mirrors both as CHECKs). There is no
direction-only measurement: ``direction`` stays declared, and a measured
frame's z is checked against it under the same ``PORT_ROT_MISMATCH_RAD``
(10°) as a declared ``rot``. Validation adds ``port_pose_mismatch`` /
``port_rot_mismatch`` declared-vs-measured checks, and ``envelope_fit`` — the
design(m)↔atomistic(Å) agreement check, whose conversion is the one permanent
unit crossing, test-pinned. A mode and a binding that contradict each other
are a ``view='drc'`` finding (``mode_binding_mismatch``), never a rejected
write. ``se_propose_atomic`` is the one job type: a tool-less LLM call
proposing — never applying — one block's chemistry, dry-run validated.
"""

from __future__ import annotations

__all__: list[str] = []
