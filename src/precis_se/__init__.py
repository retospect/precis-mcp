"""precis-se — the ``se`` (structural envelope) kind.

A first-party **plugin** on the precis substrate (Route B: entry points,
own migration namespace — the ``precis_nm`` scaffold verbatim), so core
dispatch stays untouched.

``se`` is the scale-agnostic sibling of ``nm`` — the symmetry that locates
it: **se : cad :: nm : structure** (docs/backlog/se-kind.md). nm is
intent-over-atoms renting the cad kernel as Å; se is intent-over-solids
renting the same kernel as **metres** (float64 everywhere — see
se-kind.md "Decisions": within ±10⁶ m of origin float64 metres resolves
below 10⁻⁴ Å, atoms-to-buildings in one unit; the single declared
*unit* conversion anywhere is the Å↔m multiply where an atomic-mode
block binds an nm design). One caveat the metres decision earned before
the units-policy-cutover relative-tolerance audit: the cad kernel's
tolerances used to be absolute in whatever numbers it was handed
(``LINEAR_EPS = 1e-6``, fine for Å and mm callers, fatal for a
nanometre-scale box whose every face it culled). Its tolerances are now
scale-relative (``precis.cad.vec.LINEAR_REL_EPS``, each primitive's own
governing length), so this no longer bites directly — but geometry
queries still pass through :func:`precis_se.validate.kernel_scale`,
which normalizes out-of-band designs into O(100) kernel units and
converts results back to metres, both as belt-and-suspenders and because
``_CROSS_SCALE_RATIO`` still refuses to combine wildly different-scale
blocks in one SDF query — a numerical-conditioning problem the kernel fix
doesn't solve. In-band designs go through unscaled, bit-identical. A
design is a deliberately *suggestive* space plan
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
  templates, ``component``/``part`` bindings (``set_binding``), or
  (atomic mode) a bound ``nm`` design.
- **L4 — metrics/agreement.** ``envelope_fit``, interface fit, stack-up,
  design DRC — the realized solid checked against the spec, never stored
  twice.
- **L5 — fabrication plan.** Manufacturing mode, build frame, process
  DRC, export.

This package (slices 1–3, se-kind.md "Ship order") covers the scaffold,
the L0/L1 core, and the L2 invariant tier: :mod:`precis_se.handler`
(``SeHandler``, the ``se`` kind — tree CRUD; tree/block/ports/measures/
validate/clearance/drc views, clearance renting the cad kernel's
exact-sign SDF at metres), :mod:`precis_se.ops` (pure typed-op
application over an in-memory tree — no store access: ``add_block``/
``instance_block``/``array_block``/``set_pose``/``set_envelope``/
``remove_block``/``add_port``/``remove_port``/``connect``/
``disconnect``/``set_joint``/``set_load``/``add_measure``/
``set_measure``/``remove_measure``, with nm's instancing cycle guards
and se's first-class **arrays**: an array node carries template name +
``linear`` (count/pitch/axis) or ``polar`` (count/radius/axis), members
derived at read time — realization never flattens the tree),
:mod:`precis_se.joints` (the joint vocabulary: kinematic class ×
mechanism registry with implied demands, + the registered loads/
objectives keys), :mod:`precis_se.measures` (named measures, tolerance
relations, worst-case stack-up evaluation), :mod:`precis_se.validate`
(read-time L0/L1 feasibility findings incl. the
undeclared-interpenetration geometry check; rendered under the
filled-fraction honesty header), :mod:`precis_se.drc` (graph-tier DRC:
joint contradictions, mechanism-implied demands, unresolvable relations,
the declared-vs-derived axis-travel probe renting
``relate.translational_dof``), :mod:`precis_se.persist`
(retire-all/reinsert-all store write-back, name-keyed identity, ports in
lockstep with fresh block ids), and migrations ``0001_se_kind.sql`` +
``0002_se_l2.sql``.

**Off-the-shelf rung 1** (docs/backlog/se-off-the-shelf-fabrication.md,
migration ``0003_se_bom.sql``) adds the layer for things you *don't*
make: :mod:`precis_se.bom` (a bought ``component``/``part`` hung off a
block or a connect, and the multiplicity rollup that turns one authored
line into the number you actually order — arrays and instances multiply
through) and :mod:`precis_se.modes` (the manufacturing-mode families;
``purchase`` is the one with an implementer, the rest are recordable
intent until theirs ship). Surfaced as ``view='bom'`` — priced and massed
through the ``component`` kind's own spec values, never a second copy of
them — with ``set_mode``/``set_binding``/``add_bom``/``remove_bom`` ops
and the DRC demands they make checkable (a ``bearing``/``screw`` joint
with nothing on the BOM; a ``purchase`` block that names nothing to buy).

**Rungs 2–3** turn a bought part from a line item into geometry, and a
joint from a diagram into a change to the parts it joins:
:mod:`precis_se.catalog` (rung 2b — pure ``component`` spec values →
envelope + port templates per category, in metres; reached at load time
by ``persist.attach_catalog`` and **derived, never stored**, so
re-dimensioning a component reaches every design bound to it) and
:mod:`precis_se.fasten` (rung 3 — a `screw` joint's clearance and tapped
holes, the grip stack-up walked along the fastener's own axis with
``cad.probe.probe_ray``, the length/engagement checks, and the thread
read as a **lead with limits**: metres per turn from the catalog pitch,
bounded by the engagement the stack leaves, cross-checked against a
declared ``params.lead``). Surfaced as ``view='fasten'``, with the
findings folded into ``view='drc'``. The clearance-hole table itself is
core data, not se's — :mod:`precis.fit_classes` (ISO 273 fine/medium/
coarse plus the house ``d + 0.2`` rule), the same file-not-a-table
posture as :mod:`precis.component_series` (whose ISO fastener tables the
cad catalog also reads since 2026-09-06 — one transcription, not three).

**Tension rungs 1+4** (docs/backlog/structural-solution-space.md, its
build-order slice 1) add the unilateral
member and the whole-structure verdict: kinematic class ``axial`` — ONE
pin-ended member whose params capacity pair
(``tension_capacity``/``compression_capacity``, + ``free_length``/
``rate``/``preload``) decides tie/strut/rod, no declared axis (its line
of action is derived from the endpoint poses) — the ``cable`` mechanism
(demands a BOM line), the ``fixed`` support objective on blocks, and
:mod:`precis_se.stability` (``view='stability'``): Maxwell/Calladine
``m − s`` counting off one SVD of the equilibrium matrix, self-stress
sign-feasibility against the capacity pairs, and the Pellegrino–Calladine
second-order test → rigid / mechanism / **prestress-stabilized**, over
the axial subgraph only (pin nodes at block poses — the honesty header
says so). The DOF probe reports ``axial`` as an honest skip; capacity
findings fold into ``view='drc'``. This satisfies the two-party mobility
tripwire contract by construction (the fallback line is
``stability.TRIPWIRE_LINE``, verbatim). Structural-solution-space
slice 2 adds the generator to that checker: the ``formfind`` op
(:mod:`precis_se.formfind` bridging the pure
:func:`precis.structsolve.form_find` force-density solver) solves the
axial subgraph's equilibrium geometry — anchors from
``objectives.fixed``, role-derived tension-positive force densities —
and writes solved poses back stamped ``origin: 'proposed'``; a
user-origin pose is contract and moves only under an explicit
``move=`` authorization. Slice 3 (rung 5's null-space DRC,
:func:`precis_se.stability.prestress_report`) checks declared member
``preload``s against the self-stress space — undeclared members are
completed by least squares, implied forces vetted against role sign and
capacity pair — as a prestress section in ``view='stability'`` and the
warn-tier ``prestress_state`` DRC rule.

A `component` binding additionally **projects onto a ``realized-by``
link** on every save (``persist.sync_realized_by``, migration 0156's
realization edge, the same one cad writes for its ``part`` lines). The
plugin table stays authoritative and the link is derived and rebuilt, so
one `links` query answers "what does this artifact resolve to" — and its
inverse "who calls for this component" — across both tracks instead of
requiring a consumer to know two spellings.

**Always on wherever the plugin is installed** — the original
``se.enabled`` ``requires_setting`` gate was removed (Reto, 2026-09-11; pinned by
``tests/test_se_plugin.py::test_kind_is_available_without_any_flag``);
``PRECIS_KINDS_DISABLED`` is the one general off-switch. See
``docs/backlog/se-kind.md`` for the full design (annotations superset
registry, manufacturing modes, the propose/interrogate loop); the
agent-facing skill lands last (ship order step 8). Slice 4 round 1
(:mod:`precis_se.notes` interrogation ledger + :mod:`precis_se.freedom`
design-freedom vocabulary — interval measures, ``origin``,
``view='freedom'``; migration ``0005``) shipped 2026-09-08. Unshipped
past this round: the rotational DOF probe (translational_dof's missing
twin), ``se_propose``, couplings
(gear/rack/belt ratios — ship-order step 6), process DRC + the
capability rows behind it, compliance advisories (ship-order step 6),
the profile tier, and the rest of mechanism→geometry: tool access
(a swept driver envelope per drive type × size), assembly-order
existence, edge distance, and the sheet/tube instances of the stamping
engine (finger joints, cope/fishmouth, press seats).
"""

from __future__ import annotations

from precis_se.handler import SeHandler

__all__ = ["SeHandler"]
