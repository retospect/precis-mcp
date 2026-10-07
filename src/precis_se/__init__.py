"""precis-se — the ``se`` (structural envelope) kind.

A first-party **plugin** on the precis substrate (Route B: entry points, own
migration namespace ``src/precis_se/migrations/``), so core dispatch stays
untouched. Always on wherever the plugin is installed — no per-kind flag
(pinned by ``tests/test_se_plugin.py::test_kind_is_available_without_any_flag``);
``PRECIS_KINDS_DISABLED`` is the one general off-switch. Design:
``docs/backlog/se-kind.md``; agent-facing docs: the ``precis-se-*-help`` skills
(``precis-se-help`` is the entry).

**One scale-agnostic design kind** (the ``nm`` kind merged in as its atomic
mode, docs/backlog/nm-se-merge.md): **se : cad :: atomic mode : structure**.
Non-atomic ``se`` is intent-over-solids renting the cad kernel as **metres**
(float64: within ±10⁶ m of origin it resolves below 10⁻⁴ Å, atoms-to-buildings
in one unit); atomic mode is intent-over-atoms binding ``structure`` designs.
``structure`` stays the permanent Å-native crystallography enclave
(docs/backlog/structure-unit-enclave.md); the one declared unit conversion is
the Å↔m multiply where an atomic block binds a ``structure`` design. A design is
a deliberately *suggestive* space plan ("a fork about this size, connected to a
hub that goes through a wheel so the wheel can rotate") that hardens
monotonically as answers arrive — every field beyond a block's name is
optional; validation reports absence (filled-fraction honesty) but never fails
on it.

**Stored build reports**: `view='report'` lists generated/join findings from
bound structure metadata for direct design blocks, optionally addressed by
label or uid (`args={'block': ...}`). It shares the block view's finding
renderer: a build record is historical evidence, not recomputed validation.
Missing records remain explicitly unavailable rather than implying a pass;
template occurrences and catalogue/dry-run surfaces are separate work.

**The IR — six levels** (same invariant as ``pcb``: dropping everything above
level *k* leaves a valid level-*k* object):

- **L0 — block graph.** Blocks + ports + intent connections, hierarchical
  (module trees, template refs, array nodes). No geometry.
- **L1 — envelopes + pose.** Per-block analytic envelope (the cad mini-DSL,
  reused verbatim, metres) + rough pose.
- **L2 — declared invariants.** Joints (kinematic class × mechanism),
  tolerances as relations between named measures, loads as objective vectors
  — stored explicitly, never derived from L3 geometry.
- **L3 — realized solids.** Per block: cad node sets, instanced templates,
  ``component``/``part`` bindings (``set_binding``), or (atomic mode) a bound
  ``structure`` design via ``bind_structure``/``generate``.
- **L4 — metrics/agreement.** ``envelope_fit``, interface fit, stack-up,
  design DRC — the realized solid checked against the spec, never stored twice.
- **L5 — fabrication plan.** Manufacturing mode, build frame, process DRC,
  export.

**Shape — each module owns its own design notes** in its docstring; this one
only maps them.

*Spine.* :mod:`precis_se.handler` (``SeHandler``: tree CRUD; the
tree/block/ports/measures/datums/pockets/validate/clearance/sweep/drc/bom/
order/fab/print/fret/chain/kinematics/stability/freedom/interview views;
``search`` by intent, ``wants=`` library search, ``compose=`` proposer),
:mod:`precis_se.ops` (pure typed-op application over an in-memory tree — no
store access; instancing cycle guards from the shared :mod:`precis.blocktree`
spine; first-class **arrays**, members derived at read time),
:mod:`precis_se.persist` (retire-all/reinsert-all write-back, uid identity,
``tree_mutation`` lock), :mod:`precis_se.state_arg`, :mod:`precis_se.handles`,
:mod:`precis_se.identity`.

*L2 invariants.* :mod:`precis_se.joints` (kinematic class × mechanism
registry, loads/objectives keys), :mod:`precis_se.measures` (named measures,
tolerance relations, stack-up), :mod:`precis_se.datums` (the datum a measure is
declared against; measured-from-geometry; region selectors),
:mod:`precis_se.pockets`, :mod:`precis_se.properties` (measurands + the
computer registry), :mod:`precis_se.freedom` / :mod:`precis_se.notes`
(design-freedom vocabulary, interrogation ledger).

*Checks (L4).* :mod:`precis_se.validate` (read-time L0/L1 findings, including
undeclared interpenetration), :mod:`precis_se.drc` (graph-tier DRC, store-free
by contract), :mod:`precis_se.geometry_plausibility`,
:mod:`precis_se.kinematics` / :mod:`precis_se.kinematics_drc`,
:mod:`precis_se.stability` (Maxwell/Calladine counting over the axial
subgraph; the ``axial`` class and ``cable`` mechanism) with
:mod:`precis_se.formfind` as its generator, :mod:`precis_se.precedent`
(joining-chemistry evidence against ``rxn`` yield rows).

*Bought parts, fastening, fabrication (L3/L5).* :mod:`precis_se.bom` /
:mod:`precis_se.order` / :mod:`precis_se.modes` (the manufacturing-mode
families; ``purchase`` is the one with an implementer for bought items),
:mod:`precis_se.catalog` (component spec → envelope + ports), :mod:`precis_se.fasten`
+ :mod:`precis_se.toolaccess` + :mod:`precis_se.capabilities` (screws, tool
access, process figures), :mod:`precis_se.realize` / :mod:`precis_se.simp_bridge`
/ :mod:`precis_se.simp_job` (analytic and SIMP realization),
:mod:`precis_se.printing` / :mod:`precis_se.printsolid` /
:mod:`precis_se.printgroup` / :mod:`precis_se.manufacture` /
:mod:`precis_se.manufacture_job` (the print implementer: per-block, ``model``
groups, ``manufacture`` print-in-place), :mod:`precis_se.ops_export`.

*Search.* :mod:`precis_se.library` (``wants=``), :mod:`precis_se.compose`
(``compose=``).

*Domains.* :mod:`precis_se.atomic` (chemistry-bound blocks — the merged ``nm``
kind), :mod:`precis_se.chain` (DNA/RNA over the chemistry-free
:mod:`precis_chain` kernel), :mod:`precis_se.fret` (optical FRET links),
:mod:`precis_se.properties` (region properties).

**Lifecycle.** ``put``/``edit`` build a tree through the ops table — the
store-aware ops (``bind_structure``/``unbind_structure``/``generate``/
``realize``/``join``/``relax_chain``) are intercepted first by
:func:`precis_se.atomic.apply.apply_ops_with_atomic` — and save through
``persist.save_tree`` under ``tree_mutation``; reads load a tree, derive
(catalog envelopes, array members, ``realized-by`` links) and render under the
filled-fraction honesty header. Heavy work (SIMP solves, print-in-place
fusion past ``SYNC_CELL_CAP``, ``se_propose_atomic``) is a job enqueued after
the tree saves; the op only validates and enqueues. **Apply policy by op
class** is the web chat turn's (:mod:`precis_web.design_turn`): non-destructive
pure ops auto-apply after a dry run; destructive ops
(``DESTRUCTIVE_SE_OPS``) and the store-aware ops
(:data:`precis_se.atomic.apply.HANDLER_LEVEL_OPS`) are proposals until a human
applies them.

**Seams.**

- Geometry is rented from ``precis.cad`` (``clearance``, ``probe``,
  ``printability``, ``fieldops``); numbers that are core data live in core
  (:mod:`precis.fit_classes`, :mod:`precis.component_series`,
  :mod:`precis.thread_forming`, :mod:`precis.design.states`), never in se.
- Bought things are ``component``/``part`` links with a quantity, never blocks;
  price and mass come from the ``component`` kind's own spec values, never a
  second copy.
- Identity is the block ``uid`` (names are display labels); dangling
  references are read-time DRC findings, never write-time rejections.
- A finding that needs the store is appended by the handler's ``_render_drc``
  after the store-free :func:`precis_se.drc.drc`.

**Open past what shipped** (docs/backlog/se-kind.md): the rotational DOF probe
(``translational_dof``'s missing twin), ``se_propose``, couplings
(gear/rack/belt ratios), process DRC and the capability rows behind it,
compliance advisories, the profile tier, assembly-order existence, edge
distance, sheet/tube stamping instances (finger joints, cope/fishmouth, press
seats), ranking a composition search by ``quest`` rubric weights, and running
DRC over a composition's instanced tree once realised.
"""

from __future__ import annotations

from precis_se.handler import SeHandler

__all__ = ["SeHandler"]
