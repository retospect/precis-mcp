"""precis-se — the ``se`` (structural envelope) kind.

A first-party **plugin** on the precis substrate (Route B: entry points,
own migration namespace), so core dispatch stays untouched.

``se`` is the one scale-agnostic design kind, with a mode split standing
in for what used to be a kind split: **se : cad :: atomic mode :
structure** (docs/backlog/se-kind.md; the ``nm`` kind used to be the
right-hand side of that symmetry as a sibling kind — the nm→se merge,
docs/backlog/nm-se-merge.md, folded it in as ``se``'s **atomic mode**
rather than promoting the symmetry across two kinds). Atomic mode is
intent-over-atoms renting the cad kernel as Å; non-atomic ``se`` is
intent-over-solids renting the same kernel as **metres** (float64
everywhere — see se-kind.md "Decisions": within ±10⁶ m of origin
float64 metres resolves below 10⁻⁴ Å, atoms-to-buildings in one unit;
the single declared *unit* conversion anywhere is the Å↔m multiply
where an atomic-mode block binds a ``structure`` design). ``structure``
itself stays the permanent Å-native crystallography enclave regardless
(docs/backlog/structure-unit-enclave.md) — the merge changed which kind
crosses into it, never the enclave rule itself. One caveat the metres
decision earned before the units-policy-cutover relative-tolerance
audit: the cad kernel's
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

**The IR — six levels** (same invariant as ``pcb`` and the merged ``nm``
kind before it: dropping everything above level *k* leaves a valid
level-*k* object):

- **L0 — block graph.** Blocks + ports + intent connections, hierarchical
  (module trees, template refs, array nodes). No geometry.
- **L1 — envelopes + pose.** Per-block analytic envelope (the cad
  mini-DSL, reused verbatim, metres) + rough pose.
- **L2 — declared invariants.** Joints (kinematic class × mechanism),
  tolerances as relations between named measures, loads as objective
  vectors — stored explicitly, never derived from L3 geometry.
- **L3 — realized solids.** Per block: cad node sets, instanced
  templates, ``component``/``part`` bindings (``set_binding``), or
  (atomic mode) a bound ``structure`` design — real atoms, via
  ``bind_structure``/``generate``.
- **L4 — metrics/agreement.** ``envelope_fit``, interface fit, stack-up,
  design DRC — the realized solid checked against the spec, never stored
  twice.
- **L5 — fabrication plan.** Manufacturing mode, build frame, process
  DRC, export.

**Apply policy by op class** (the web workbench's chat turn,
:mod:`precis_web.design_turn`, the design-workbench build (2026-09-18): the
non-destructive pure ops in :func:`precis_se.ops.known_ops` (L0–L2 — cheap
to redo, claim nothing physical) auto-apply after a dry run as one
revision; the destructive slice of that same roster
(:data:`precis_web.design_turn.DESTRUCTIVE_SE_OPS` — ``remove_block``,
``remove_port``, ``remove_measure``, ``remove_bom``, ``remove_note``,
``remove_threading``, ``disconnect``, by prefix rule off the live
roster) and the store-aware ops in
:data:`precis_se.atomic.apply.HANDLER_LEVEL_OPS`
(``bind_structure``/``unbind_structure``/``generate``/``realize`` — L3,
they spend compute or assert chemistry) are proposals until a human
applies them — undoing a decision, not just redoing one, earns the same
human-Apply gate as spending compute. ``SeHandler.edit(turn=)`` stamps the
originating chat turn onto the revision row.

This package (slices 1–3, se-kind.md "Ship order") covers the scaffold,
the L0/L1 core, and the L2 invariant tier: :mod:`precis_se.handler`
(``SeHandler``, the ``se`` kind — tree CRUD; tree/block/ports/measures/
datums/validate/clearance/drc views, clearance renting the cad kernel's
exact-sign SDF at metres), :mod:`precis_se.ops` (pure typed-op
application over an in-memory tree — no store access: ``add_block``/
``instance_block``/``array_block``/``set_pose``/``set_envelope``/
``remove_block``/``add_port``/``remove_port``/``connect``/
``disconnect``/``set_joint``/``set_load``/``add_measure``/
``set_measure``/``remove_measure``, with instancing cycle guards (the
shared :mod:`precis.blocktree` spine) and se's first-class **arrays**:
an array node carries template name +
``linear`` (count/pitch/axis) or ``polar`` (count/radius/axis), members
derived at read time — realization never flattens the tree),
:mod:`precis_se.joints` (the joint vocabulary: kinematic class ×
mechanism registry with implied demands, + the registered loads/
objectives keys), :mod:`precis_se.measures` (named measures, tolerance
relations, worst-case stack-up evaluation), :mod:`precis_se.datums`
(the datum a measure is declared against — see below),
:mod:`precis_se.validate`
(read-time L0/L1 feasibility findings incl. the
undeclared-interpenetration geometry check; rendered under the
filled-fraction honesty header), :mod:`precis_se.drc` (graph-tier DRC:
joint contradictions, mechanism-implied demands, unresolvable relations,
the declared-vs-derived axis-travel probe renting
``relate.translational_dof``, and :mod:`precis_se.geometry_plausibility`'s
connect geometric plausibility pass — a declared connect between
geometrically disjoint envelopes, and mechanism/kinematic-class implied
envelope shape: press/snap interference, captive containment,
bearing/revolute/cylindrical coaxiality + radial nesting, screw-class
axial overlap), :mod:`precis_se.persist`
(retire-all/reinsert-all store write-back, name-keyed identity, ports in
lockstep with fresh block ids), and migrations ``0001_se_kind.sql`` +
``0002_se_l2.sql`` (…+ ``0012_se_measure_datum.sql`` for ``datum``).

**Datums + measured-from-geometry** (the se plumbing
multiscale-optimisation §4's preferred-number wells attach through;
history in multiscaledesign.README.md): a measure may declare
``datum:`` — ``frame`` (the default; prismatic → the three pose-frame
faces through the frame origin, rotational → axis + base face),
``port:<name>``, ``face:<block>.<tag>``, ``axis:<block>`` — resolved
through the block's cad primitive by :mod:`precis_se.datums`
(``parse_selector``/``resolve``/``rank_datums``/``evaluate_measure``/
``d_measure``). Three settled decisions: the datum is a **column, not
relation JSON**, because every reader must understand it — evaluator,
stack-up, migration — and nothing may silently skip it; the
``measure`` predicate *declares the name and pins the feature* (the
method doc's predicate shape); and the datum is **never an optimiser
DOF** — if it could slide, the well term would round a dimension by
moving the datum instead of the geometry. ``rank_datums`` is
deterministic (largest flat face, port faces free, process-setup
candidates, accessible); ``evaluate_measure`` reads the number from
geometry (ray exits for plain extents, the param for envelope
dimensions, ``feature`` relations for sub-envelope anchors), stamps
``source: derived``, and reports ``datum_resolved`` plus a
``datum moved`` note when the caller passes the previous resolution.
Its ``mismatch`` note is band-first: a declared ``[min,max]`` flags the
derived value falling outside it, else ``relation.tol`` around the
declared value, else exact. ``d_measure`` central-differences over the
envelope params.

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

**Rungs 3b + 3c** (2026-09-15) finish what a screw has to answer for.
*3c, fastening a printed part*: rung 3 stamped ``d − P`` into whatever
the stack ended in, which is right in aluminium and wrong in an FDM boss,
so the terminal member's **mode** now decides. Metal keeps the cut
thread; a printed member takes the ``joint.params.thread_strategy`` it
declares — ``nut`` · ``nut-trap`` · ``insert`` · ``thread-forming`` —
and, undeclared, gets **nothing stamped** plus a finding naming the four
(:mod:`precis.thread_forming` holds the numbers: core-hole factors,
engagement multiples, insert pockets, nut-trap fits, each marked as
transcribed or as a shop rule). Head form finally stamps a feature too, which
migration 0163's ``head_form`` made expressible — and splits the same way:
a countersunk head's 90° cone is stamped because the screw does not seat
without it, while burying a cap head is a choice
(``params.counterbore``) and is reported rather than done. *3b, tool access*: :mod:`precis_se.toolaccess` stands
each candidate driver's swept envelope on the drive face and asks whether
it clears the assembly, answering **which** tool rather than whether
(ISO 2936 key geometry + bench tools, in
``precis/data/driver_envelopes.json``). Both need a *process* number the
tree had nowhere to keep — a printed hole comes out undersize — so
:mod:`precis_se.capabilities` seeds se-kind.md's
``se_capabilities.json`` with exactly the three fields this consumes and
leaves the rest to slice 5.

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

**Atomic mode** (docs/backlog/nm-se-merge.md) is the merged ``nm`` kind:
a block whose realization is *chemistry* rather than solids, carried by
the :mod:`precis_se.atomic` subpackage (migration ``0007_se_atomic.sql``).
Its L2 is stated explicitly, never derived from coordinates
(:mod:`precis_se.atomic.vocab` — declared dof, threading, and the
``kind='bond'`` capability gate, applied by the ops in
:mod:`precis_se.ops`); its L3 is a bound ``structure`` design, reached by
the three store-aware ops :mod:`precis_se.atomic.bind` and
:mod:`precis_se.atomic.generate` implement (``bind_structure``,
``unbind_structure``, and ``generate``, which runs a parametric block
factory from :mod:`precis_se.atomic.generators` and mints the structure
design itself — deterministic geometry, no LLM guessing);
:mod:`precis_se.atomic.apply` intercepts those three before the pure op
table. A bind also *measures*: each mapped port takes the block-local
position of the atom it resolves to as its own pose
(``pose_source='bound'``, the measured half of the port pose slot) — into
an empty slot or over an earlier bind's reading, never over a
``'declared'`` target, which is the requirement that realization is
checked against. Mapped with ``axis_atom``/``phase_atom`` (the object form of
``bind_structure``'s ``ports=``: axle bond ``atom → axis_atom`` is the
frame's z, ``atom → phase_atom`` projected off it fixes the roll), the
same bind independently measures the port's ``rot`` off that atom triple
(``rot_source='bound'`` — its OWN provenance, never coupled to
``pose_source``; se-plugin migration 0014 mirrors both as CHECKs). There
is no direction-only measurement: ``direction`` stays declared, and a
measured frame's z is checked against it under the same
``PORT_ROT_MISMATCH_RAD`` (10°) as a declared ``rot``. Its L4 is :mod:`precis_se.atomic.validate` (the bond
capability re-check, the binding checks, bond-geometry sanity, the
``port_pose_mismatch``/``port_rot_mismatch`` declared-vs-measured checks,
and ``envelope_fit`` — the design(m)↔atomistic(Å) agreement check, whose
conversion is the one permanent unit crossing, test-pinned) plus
:mod:`precis_se.atomic.mechanics`'s advisory ceilings, rendered as
``view='mechanics'``/``view='literature'``
(:mod:`precis_se.atomic.render`). A mode and a binding that contradict
each other are a ``view='drc'`` finding (``mode_binding_mismatch``),
never a rejected write. Its one job type is
``se_propose_atomic`` (:mod:`precis_se.atomic.propose`, nm's
``nm_propose`` renamed with the merge): a tool-less LLM call proposing —
never applying — one block's chemistry, dry-run validated. The retired
kind's storage is dropped by migration ``0008_se_drop_nm_tables.sql``;
``nm`` itself now answers with a retired-kind pointer at this one
(``precis.runtime.dispatch``'s ``_RETIRED_KINDS``).

**Discrete block states + stimulus-labelled transitions**
(docs/backlog/blocktree-library-build-plan.md §Slice 2) rent the shared
design core (:mod:`precis.design.states`, not an se-local table — the
same mechanism serves macro bistables and photoswitches/conformers alike,
per that module's A9 hysteresis warning: a state-carrying block's state
is not a function of its parameter vector, so nothing here memoizes by
configuration alone). ``declare_states``/``declare_transitions`` write a
block's `{name, envelope?, port_pose_overrides?}` states (an override is
`{port: {'direction'?, 'pose'?, 'rot'?}}` — direction outright, pose/rot
a rigid delta in the block frame, applied only to a port carrying a pose
of its own) and directed,
`driver_kind`-labelled edges between them, materialized once
``persist.save_tree`` has minted every block's uid
(:func:`precis_se.handler._materialize_states`); ``set_current_state``
persists a pose. ``get(..., args={'state': {block: state_name}})`` poses
transiently, for one read, on ``view='tree'|'block'|'clearance'``
(:func:`precis_se.handler._apply_state_arg`) — a block with no declared
states is unchanged in shape or render. ``view='sweep'`` answers "does
anything collide in ANY declared state" over the cross product of every
state-carrying block's states (:func:`precis.design.states.
state_carrying_uids` decides which blocks enter the product at all),
reusing :func:`precis_se.validate.envelope_overlaps` per combination
rather than a second geometry engine, with a hard combination-count
budget it names rather than silently truncates.

**The optical domain** (:mod:`precis_se.fret`, migration
``0010_se_fret.sql``) is se's first non-mechanical one: FRET links, where
a donor chromophore hands its excitation to a nearby acceptor by
near-field dipole-dipole coupling. It earns a place in a *space planner*
because the coupling has no waveguide — the channel IS the geometry, and
the rate runs as ``r⁻⁶`` times an orientation factor ``κ²`` computed from
the two transition dipoles and the vector between them. Both inputs are
things a space plan already decides, so the same six levels carry it with
no new tier: L0 is a port↔port connect like any other; L1's per-block
pose, with the dipole stored in the **block** frame, is what rotates each
card into world space; L2 is the declared ``optical`` invariant on the
connect (``min_efficiency`` — what the design *needs*, stored, never
derived); L4 is ``view='fret'``, the realized geometry checked against
that declaration. Ops: ``set_chromophore`` (the per-block property card,
block-owned beside ``dof``/``objectives`` — label, dipole, quantum yield,
lifetime, emission and absorption spectra), ``set_optical_link``, and
``set_optics`` (the design's medium index and pump wavelength — se's one
tree-level scalar record, earned by being a fact about the *space*: every
Förster radius in a design divides by the same ``n⁴`` under a sixth
root).

Two decisions there are worth not re-deriving. The ``optical`` slot is
deliberately **compatible** with ``joint`` and ``kind`` on the same
connect, unlike those two with each other: a kinematic joint and a
covalent bond are competing claims about one physics, while an optical
link is a different physics on the same pair. And a donor is a
**broadcast, not a wire** — every acceptor in range competes for one
excitation, so the branching ratios share a denominator
(:func:`precis_se.fret.solve_donor`) and a per-pair efficiency quoted in a
dense network overstates every link. That is why the view is an all-pairs
budget rather than a list. The module declines to quote a number outside
Förster's range of validity (below ~1 nm, Dexter exchange competes and
the point-dipole approximation fails; ``κ²`` near zero is a dead link at
any distance, and the actionable fix is rotating a block, not moving it).

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

**se-print-implementer.md rung 1** (2026-09-16) widens every ``fdm``
``se_capabilities.json`` row with the full process-figure set (layer
height, line width, overhang, bridge, bed contact, min feature/hole,
strength-vs-layer ratio, build volume) plus a family-level
``orientation`` weights block for the build-frame search (rung 4, below),
and builds the one resolver se-kind.md's L5 promised:
:func:`precis_se.capabilities.resolve` chains a block's own
``process_overrides`` (migration ``0011_se_process_overrides.sql``, ops
``set_process_override``/``clear_process_override``) over the
unimplemented load-derived slot over :func:`precis_se.capabilities.
capability`'s house tier, always clamped to the physical floor. Process
DRC, the printed solid, the orientation search itself and ``view='print'``
are the rungs after this one.

**se-print-implementer.md rung 4** (2026-09-17) lands the implementer:
:mod:`precis_se.printing` composes Engine 1's printed solid
(:mod:`precis_se.printsolid`) with Engine 2's cad-level orientation search
(:mod:`precis.cad.printability`) into one report per fdm-family block,
adding the se-only rules a mesh alone can't know (``unrealized``,
``abstract_joint``, ``hole_undersize``/``hole_shrink_absorbed``,
``min_feature``, ``layer_vs_load``). ``view='print'`` renders it — no args
for one section per fdm block, ``args={'block': ...}`` for the full
candidate table, ``+{'fmt': 'stl'|'3mf'}`` to write the file in the build
frame; ``set_build_frame``/``clear_build_frame`` pin/unpin the direction
(``se_blocks.build_frame``, dark since migration ``0001``, first written
here). ``view='fab'`` is the new top-level index — one row per
implementation-bearing block, any source (purchase/fdm/atomic/
unimplemented), pointing at each row's own handle; it never exports
itself. ``MODE_FAMILIES['fdm'].implemented`` flips to ``True``.

**``realize(strategy='simp')``** (docs/backlog/structural-solution-space.md
"Slice 4 bridge", round A, 2026-09-18) is the second realize strategy:
instead of seeding the cad design from the envelope,
:mod:`precis_se.simp_bridge` voxelises the block's effective envelope in
its local frame at ``pitch=`` (metres — demanded while the house
``simp_pitch`` capability is null), turns ``objectives.force``/``fixed``
into nodal loads/supports at ``load_at=``/``fixed_at=`` (an envelope
face ``x+..z-`` or a posed port; the elements under them are passive
solid), runs :func:`precis.structsolve.simp.simp_optimize` at
``volfrac=`` with the AM filter for ``build_dir=`` (default: the
envelope box's largest face down, echoed), and binds the block to a NEW
cad design rooted at the density's ``field:<sha>`` leaf (optional
``round=``/``open=``/``close=`` morphology on the grid first). The op
only validates and enqueues an ``se_simp`` job (:mod:`precis_se.
simp_job`, ``job_inproc``) — the solve is minutes; the block reads
unrealized until it lands. The job pins ``build_frame`` with
``origin='simp'``, so ``view='print'`` verifies that frame (the 45°
voxel rule on the stored field) and skips the orientation search,
saying so. The run summary sits on the se ref's ``meta.simp``
(``last`` + ``runs``); a re-realize mints a sibling cad design and
switches the one binding a block holds — the previous design stays,
named in ``runs`` and linked ``derived-from`` the se design. Advisory
tier: the compliance is a voxel estimate, never a DRC verdict.

**Print groups — ``intent='model'``** (the same spec's print ``intent``
table, round B1, 2026-09-19): :mod:`precis_se.printgroup`. A print group
is an ancestor block in an fdm mode carrying an intent — ``set_mode(block,
mode='fdm/<m>', intent='model')``, stored as the ``intent`` key of the
block's ``build_frame`` record beside a pin (``ops.print_intent``/
``ops.pinned_down`` are the two reads; an intent-only record is not a pin)
— and its members are the blocks below it by ``parent`` edges, derived at
read time, no schema; **a group ends where the next group root begins**
(a nested root owns its own subtree, the outer group lists it as one
``nested group … printed separately`` line, and every block maps to its
nearest root). ``model`` prints every fdm member's solid and every
purchase member as a **stand-in**: the cad catalog's analytic solid
(:func:`precis.cad.catalog.family_for_series` maps the component's minted
series to ``part <family>:<size>``, threads dropped) or a solid from its
spec dims, else a ``no_stand_in`` finding naming what it needs. One build
frame per group, the orientation search run on the union of the member
meshes in world pose; a SIMP member pins it to its baked ``build_dir``
(search skipped, said so; two disagreeing → ``simp_frame_conflict``);
members' frame findings are judged at that frame. ``view='print'`` on the
root renders frame + per-member findings, ``fmt='3mf'`` writes one 3MF
with an object per member in world pose (one shared bed offset,
:func:`precis.cad.printability.rotate_all_to_frame`), ``view='fab'``
collapses the group to one row. A root with no intent is not a group.

**Print groups — ``intent='manufacture'``** (round B2, 2026-09-19):
:mod:`precis_se.manufacture`, the real part, print-in-place.
``realize(block=<group root>, strategy='manufacture', gap=, fit=,
blend=, pitch=)`` builds ONE sampled-field solid per group in the root's
frame by field/CSG ops only — no mesh is ever operated on. Every connect
between two members is classified *rigid* (no DOF — ``rigid``/
``captive``/``axial``, or an undeclared joint, said so) or *DOF*
(:data:`precis_se.drc._MOVING_CLASSES`); rigid pairs of printed members
**fuse** (min-union, :func:`precis.cad.fold.smooth_min_np` of width
``blend`` at the seam — the DSL's ``blend:`` semantics, ``0`` = plain
min); a DOF pair gets its gap **seam-locally by construction** — ``A' =
A \\ dilate(B, gap/2)``, ``B' = B \\ dilate(A, gap/2)``, the dilation
:func:`precis.cad.fieldops.offset` on the PARTNER's re-distanced sampled
field, so a rigid seam elsewhere on ``A`` stays as sampled (face-to-face
and pin-in-bore come out at ``gap``; a re-entrant corner's worst case is
``gap/2``), plus half a pitch because the kernel's re-distance binarises,
so ``gap`` and ``fit`` are floors and the report quotes the **measured**
separation per joint (the other side's re-distanced SDF over this side's
mesh vertices); a DOF pair a rigid path joins anyway is ``dof_bridged``
(error, export refused); a bought member is a **cavity** — its B1
stand-in re-distanced, dilated by ``fit`` (+ half a pitch), subtracted —
with its top layer in the chosen frame, read off the sampled cavity
field (exact to the grid in any frame), reported as the mid-print
**pause** (no insertion-path search; the ``bambuuzle`` rung); an elided
fastener's holes are excluded from the fused members
(``printed_solid(exclude=)`` → ``fasten.features_for(exclude=)``, this
path only); a bought fastener
whose grip stack (:func:`precis_se.fasten.fasten`) is two or more printed
members of one fused component is **elided** (``joint fused, <block> not
needed``; the ``screw`` mechanism demand in
:func:`precis_se.fasten.abstract_joints` is satisfied by fusion for this
intent only — ``model`` still prints it). ``gap`` is required unless the
house ``min_clearance`` capability resolves (new, null in every fdm row;
a ``set_process_override`` on the root supplies it) — below the floor is
an ``in_place_clearance`` error, a null floor an info finding saying
"uncalibrated, taken as declared"; ``fit`` is required whenever a cavity
is to be cut; ``pitch`` defaults to the house ``layer_height``. The op
fuses inline when the group has no SIMP member and the grid is under
``SYNC_CELL_CAP`` (500k cells), else enqueues ``se_manufacture``
(:mod:`precis_se.manufacture_job`, its own job type — ``se_simp``'s
schema is closed and SIMP-shaped); above ``MAX_CELLS`` (8M) it refuses.
The result is a new cad design ``<design>-<root>-mfg[-N]`` rooted at
``field:<sha>``, bound to the ROOT (which must hold no solid of its own)
the way ``realize_simp`` binds — per-ref lock, inputs hash re-checked,
``meta.manufacture`` ``last``/``runs``, the summary also in the field's
provenance header, ``derived-from`` link; a re-run mints a sibling. The
root is one ``field:`` leaf: every member is re-sampled at the pitch and
sub-pitch features (hole compensation, seats) are not preserved — the
mixed root (fused ``field:`` leaf + the members' analytic ``add``/``cut``
/``blend:`` nodes, which the DSL already expresses) is the next item,
and every render says so.
``view='print'`` on the root reports the frame (root pin > SIMP member >
search on the fused mesh), one object per **connected component** of the
field (:func:`precis.cad.fieldops.label_components` — a DOF pair is two
objects, a fused pair one), the measured gaps, the cavities with pause
heights, the elisions, and the **teardrop rule**: a DOF axis-class joint
whose axis lies within 45° of the build plate is an ``overhang`` finding
naming the bore (no new primitive; an undeclared axis says the check
could not run). ``fmt='3mf'`` writes one object per component;
``view='fab'`` says ``intent manufacture, N objects, K cavities, E
elided``. ``realize(strategy='simp')`` on a manufacture root solves the
**fused group as one body**: :func:`precis_se.simp_bridge.solve_simp`'s
new ``domain_builder`` hook takes :func:`precis_se.manufacture.
simp_domain` — the union of the member envelopes in the root frame minus
the stand-in cavities (the simp op's optional ``fit=``) — with the ROOT's
loads/supports at ``load_at``/``fixed_at``; a DOF joint between printed
members is refused there (one solve is one body).

**Blocktree slice 4 — ranked library search** (docs/backlog/
blocktree-library-build-plan.md §Slice 4, port-pose-and-composition-
search.md Decision 2) lands ``search(kind='se', wants={...})``:
:mod:`precis_se.library` walks every non-instance block in the whole
library, scores it against a per-attribute wishlist (three built-in
structural keys read off the block/tree — ``stimulus``/``bistable``/
``joining`` — plus any ``component``/``material`` star-schema key
reached through a binding or a ``made-of`` link), and ranks with
:mod:`precis.quest.frontier`'s Pareto tie-break rather than a second
dominance rule. Never a strict filter: every row shows its per-attribute
match/miss with the actual value, and the result set is empty only when
the library itself is.

**Composition proposer** (port-pose-and-composition-search.md "New item
— composition proposer") lands ``search(kind='se', compose={...})``:
:mod:`precis_se.compose` enumerates n switches + m spacers over the same
library rows against a requirement box (``delta`` Å / ``span`` nm
intervals), scores each composition like a slice 4 row (per-unit facts
are the star-schema properties ``delta_length``/``unit_length``/
``pss_short_fraction``/``thermal_half_life``/``persistence_length``)
and ranks with the same order — a deterministic enumerator on the read
path, not the LLM ``se_propose_atomic`` job. Every row surfaces the
PSS-scaled stroke, the T-type verdict with τ½, a ``floppy``/``stiffness
unknown`` mark against the persistence length, and the switch↔spacer
port complementarity; the Next line is the ops script that realises it.

**Kinematics and levers** (the port-rotation item, shipped whole
2026-09-19). Nothing declares an angle: a transition's swing on a port is
*derived*, ``R_to · R_fromᵀ`` of the port frame composed through each
state's ``port_pose_overrides`` (:mod:`precis_se.kinematics`, one
``compose_port_rot`` shared with the display path; axis-angle via
``precis.cad.vec.axis_angle_from_matrix``, both degenerate cases
handled). ``view='kinematics'`` tabulates axis (block frame), angle,
``arm (envelope)`` — the port-origin-to-envelope extent in the plane
normal to the axis, a geometric UPPER BOUND on the block's own lever arm,
labelled so — and the tip displacement ``2·arm·sin(angle/2)``; sourced
``step_angle``/``rotation_rate``/``rotation_barrier`` rows sit beside the
derived angle and a >10 % disagreement is flagged, never averaged.
Precondition, surfaced twice: an override on a pose-less port is a no-op
(``_apply_port_delta``), so validate raises ``port_override_unapplied``
and the kinematics row says ``no pose`` rather than reading as
"no change". Frames: a joint names its axis in the WORLD frame
(:mod:`precis_se.joints`), a port carries its rotation in the BLOCK
frame — :mod:`precis_se.kinematics_drc` transforms through the block's
own placement before comparing, and ``revolute_axis_mismatch`` (warn) is
the disagreement; the joint owns the axis and class, the port owns the
rotation, never a second slot on joints. The proposer's second family:
a **rotary unit** (a block with a derived swing, else a sourced
``step_angle`` with arm₀ = half the envelope diagonal) plus k arm units
is a lever whose tip stroke ``2·(arm₀ + k·unit_length)·sin(angle/2)``
ranks beside linear chains under ``delta``; a ``swing`` box key (degrees,
exclusive with ``delta`` — one stroke measure) ranks rotary series by
summed angle; a lever's ``span`` is its arm reach, scored as such; a
rotating port with no role complementary to the arm's says ``joining:
none`` and emits no connect, never a placeholder.
"""

from __future__ import annotations

from precis_se.handler import SeHandler

__all__ = ["SeHandler"]
