"""Pure ops over an in-memory se block tree — no store access.

Built on the shared block-tree spine (:mod:`precis.blocktree`, extracted
from an earlier copy of this module — docs/backlog/
blocktree-library-build-plan.md §Settled): the core owns the recursive tree
(``parent``/``template``), instancing with cycle guards, ports, connects,
and envelope validation over the ``precis.cad`` SDF kernel; this module
adds se's own invariants on top — units are **metres** (float64,
se-kind.md "Decisions"), **arrays are first-class block-level structure**
(an array node is an instance node — ``template`` set, resolved at read
time, never copied — that additionally carries a multiplicity spec), and
the L2/L3/off-the-shelf vocabulary (joints, measures, loads, manufacturing
mode, realization binding, BOM) that the core knows nothing about.

**Identity is the block ``name``, not a row id** — ``precis_se.persist``
loads a design's live rows into a fresh :class:`SeTree` keyed by name and
reinserts the whole tree on every save (row ids are rebuilt; names carry
across), so a block is only ever looked up by name here.

Op catalog (slices 1-3 — blocks/instancing/arrays + ports/connects +
joints/measures/loads). The first 8 (``add_block``/``instance_block``/
``set_pose``/``remove_block``/``add_port``/``remove_port``/``connect``/
``disconnect``) are the shared core ops (:mod:`precis.blocktree.ops`),
used here as-is or extended with se's own cascades:

- ``add_block``      — mint a new block, optionally nested under an
  existing ``parent``, with an optional envelope (validated through the
  real ``precis.cad.dsl`` parser, never re-implemented here — the DSL is
  unit-agnostic; se declares its numbers to be metres). Unmodified core op.
- ``instance_block``  — mint a block that **reuses** an existing block's
  subtree by reference (``template``). Only ``template``/``name``/
  ``parent``/``pose``/``rot`` are accepted — an instance resolves
  ``envelope``/``desc``/``use`` from its template, so those keys are
  rejected rather than silently dropped. The instance-of-instance,
  nest-under-own-template, and indirect-cycle guards are the core's
  (:func:`~precis.blocktree.ops._find_instance_cycle` over the
  "expands-to" relation — a cycle there is an infinite-recursion
  predictor for the read-time tree walk); this module adds only the
  linear/polar rejection that points callers at ``array_block``.
- ``array_block``     — mint an **array-instance** node: everything
  ``instance_block`` does (sharing the core's :func:`~precis.blocktree.
  ops._instance_shared`/:func:`~precis.blocktree.ops._commit_instance`),
  plus exactly one of ``linear`` (``count`` + ``pitch`` (m) + ``axis``) or
  ``polar`` (``count`` + ``radius`` (m) + ``axis``, axis defaulting to
  ``[0,0,1]`` — the cad node-level ``polar:nNrR`` modifier's implicit +z,
  lifted to block level per se-kind.md "Hierarchy"). ``count`` must be a
  whole number ≥ 2 — an array of one is just an instance, and the
  rejection says so. Member poses are derived at read/check time from the
  spec (the realization-as-overlay rule: the template's solid stored
  once, posed N times); per-member ``overrides``/unlink are a later
  round, so the key is rejected loudly today rather than swallowed (the
  ``**_kw`` lesson).
- ``set_pose``        — rewrite an existing block's pose and/or rotation.
  Unmodified core op.
- ``set_envelope``    — set/replace an ordinary block's envelope (or clear
  it with ``null``); rejected on an instance/array node (envelope lives on
  the template). The suggestive-by-contract loop hardens designs
  monotonically as answers arrive — envelopes must be revisable without
  re-putting the whole design.
- ``remove_block``    — remove a block and its whole subtree; refused
  while any live block elsewhere instances it (or a descendant) — array
  nodes count as instances for this guard, since ``template`` is what it
  checks. Any live connect touching the removed subtree (either
  endpoint's block in it — including an *instance* of a removed block,
  since the instance's name is what a connect actually stores) is dropped
  in the same op (the ``structure`` vacancy precedent; ``validate``'s
  ``dangling_connect`` catches the hand-corrupted cases where this didn't
  run) — the core's cascade; this module adds the measures/BOM cascade on
  top, since those side tables are se's own.
- ``add_port``        — mint a named attachment point on a block. Only an
  ordinary (non-instance) block owns ports — an instance/array resolves
  its ports from its template at read time (:func:`effective_ports`, the
  same rule as envelope/desc/use). ``roles`` is a capability set (the
  pin→roles pattern); ``direction`` normalizes to unit length (zero
  vector rejected); ``annotations`` is an open dict — at this round every
  key is treated as *descriptive* (rendered, never enforced); the one
  superset registry with contract classes (se-kind.md "Annotations")
  arrives with the first checked consumer, and *then* the three-way
  engaged/declared-but-unchecked/descriptive honesty report. The port
  ``name`` may not contain ``'.'`` — the ``connect``/``disconnect``
  ``'block.port'`` syntax reserves it. ``pose``/``rot`` optionally place
  the port ITSELF in the block's local frame (metres/radians; ``rot``
  without ``pose`` is refused — a rotation with no origin is meaningless),
  stamped ``pose_source='declared'``. The core op plus se's two
  *expected chemistry* fields (:func:`_op_add_port`).
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance/array
  of this block. Core op plus se's dof guard (:func:`_op_remove_port`).
- ``set_port_pose``   — rewrite an existing port's own ``pose``/``rot``,
  or ``clear=True`` to null them (the consumers fall back to the envelope
  approximation, and say so). ``set_pose`` one level down; ordinary
  blocks only, same instance rule as ``add_port``. The slot is nullable
  on purpose — at box level the exact origin of an attachment point is
  often genuinely unknown (:class:`~precis.blocktree.types.Port`).
  Unmodified core op: it mutates the port in place, so se's
  :class:`PortSpec` survives.
- ``connect``         — a port↔port intent edge (``a``/``b`` as
  ``'block.port'``, split on the *last* dot). Each endpoint resolves on
  the block itself or — for an instance/array — its template (via se's
  own :func:`effective_ports`, so a `component`-bound block's catalog
  ports are reachable too). Self- and duplicate connects (same unordered
  endpoint pair) are rejected. ``joint``/``objectives`` go through the
  slice-3 schemas (:mod:`precis_se.joints` — kinematic class × mechanism,
  registered load keys), same as ``set_joint``/``set_load`` below — the
  ``joint`` slot is se's own extension over the core's ``Connect``, so
  this op is a full override, not an extension, of the core's ``connect``.
- ``disconnect``      — remove a live connect by its unordered endpoint
  pair; a missing pair is a retryable :class:`OpError` listing what *is*
  live. The core's implementation; this module adds the BOM cascade for
  lines hung off the removed connect.
- ``set_joint``       — set/replace/clear an existing connect's joint
  (``a``/``b`` endpoints + ``joint`` object or ``null``) — the L2 shape:
  kinematic ``class`` (+ ``axis`` where the class has one, unit-
  normalized), optional ``mechanism`` from the registry, ``params`` for
  mechanism-specific numbers. Unknown keys/classes rejected loudly.
- ``set_load``        — set/replace the loads on a block (``block=``) or
  a connect (``a=``/``b=``): ``force``/``torque`` 3-vectors (N / N·m),
  ``duty`` prose, ``cycles`` ≥ 0 — replace semantics; ``clear=true``
  removes. The kind-neutral loads vocabulary (se-kind.md "Relation to
  nm").
- ``add_measure`` / ``set_measure`` / ``remove_measure`` — named measures
  on ordinary blocks (metres), optionally carrying a **tolerance
  relation** ``{'source': 'block.measure', 'offset', 'tol'}`` +
  hard/soft/gauge strength (:mod:`precis_se.measures`; stack-up +
  unresolvable-relation findings are :mod:`precis_se.drc`'s read-time
  job — a forward-referenced relation source is legal at write time).

Off-the-shelf rung 1 (docs/backlog/se-off-the-shelf-fabrication.md) adds
the ops for things you *don't* make:

- ``set_mode``        — assign a block's manufacturing mode
  (:mod:`precis_se.modes` — ``purchase``, ``fdm/asa``, ``laser/acrylic``,
  …), or clear it with ``null``. An unknown *family* is rejected; a known
  family with no implementer yet is accepted and reads back as recorded
  intent, never as a checked plan.
- ``set_binding``     — bind a block's L3 realization to an existing
  design or catalog row: ``kind`` ∈ ``cad|nm|component|part`` +
  ``design`` (the slug / C-number), or ``clear=true``. The binding is
  name/slug-keyed text resolved at read time — binding a component that
  doesn't exist yet is legal and reported, not rejected.
- ``add_bom`` / ``remove_bom`` — a bought ``component``/``part`` hung off
  a block (``block=``) or a connect (``a=``/``b=``), with a
  per-occurrence ``qty`` (:mod:`precis_se.bom`, which owns the
  multiplicity arithmetic). A repeat ``add_bom`` for the same
  (target, item) *replaces* that line rather than minting a second — the
  quantity is the statement, and two lines saying different numbers is
  the ambiguity this avoids. Lines whose target is removed go with it
  (the vacancy rule ``remove_block``/``disconnect`` already follow).

Slice 2 of docs/backlog/structural-solution-space.md adds the one
solver-backed op:

- ``formfind``       — force-density form-finding over the axial
  subgraph (:mod:`precis_se.formfind` bridging
  :func:`precis.structsolve.form_find`): anchors from
  ``objectives.fixed`` and unmoved nodes, tension-positive force
  densities from member roles (``q_tie``/``q_strut``/``q_rod`` defaults
  +1/−1/+1, per-member ``q=[{'a','b','q'}]`` overrides), solved poses
  written back stamped ``origin: 'proposed'``. Which nodes may move is
  explicit: by default only poses already stamped ``proposed``;
  ``move=[...]``/``move='all'`` authorizes more — a user-origin pose is
  contract and never moves silently.

Blocktree slice 2 (docs/backlog/blocktree-library-build-plan.md §Slice 2)
adds discrete block states + stimulus-labelled transitions — the SHARED
design-core home (:mod:`precis.design.states`), not a table of se's own
(the shared-states ruling: bistability is true macro AND nano):

- ``declare_states``      — replace a block's declared states:
  ``block=`` + ``states=[{'name', 'envelope'?, 'port_pose_overrides'?,
  'descr'?}, ...]`` (``[]`` clears them). Ordinary blocks only — the same
  realization-facet rule as ``set_mode``/``set_binding``/``declare_dof``
  (:func:`_template_owned`); an instance has no states of its own to
  declare. **This op only validates and stashes the payload on the node**
  (``node.pending_states``) — the actual write goes to
  :func:`precis.design.states.set_states`, run by the handler *after*
  ``persist.save_tree`` (:mod:`precis_se.persist`), because the shared
  tables key on ``block_uid`` and a just-minted block has none until that
  save runs. A duplicate state name in one call is rejected; an envelope
  override is vetted through the same cad DSL parser every block envelope
  is.
- ``declare_transitions`` — replace a block's transitions: ``block=`` +
  ``transitions=[{'from_state', 'to_state', 'driver_kind', 'driver_ref'?,
  'params'?}, ...]`` (``[]`` clears them). **Directed** — a forward and its
  reverse are two separate entries, never collapsed (a molecular ratchet's
  barriers differ by direction). ``driver_kind`` is the closed enum
  :data:`~precis.design.states.DRIVER_KINDS`; a self-edge (``from_state ==
  to_state``) is rejected — it drives nothing. Same store-aware deferral
  as ``declare_states``, and materializes *after* it in the same call, so
  a transition declared alongside new states sees them already written
  (the shared tables' composite FK checks every endpoint against a
  declared state). ``port_pose_overrides`` is vetted to
  ``{port_name: {'direction'?, 'pose'?, 'rot'?}}``
  (:func:`_vet_port_pose_overrides`) — ``direction`` unit-normalized the
  same way ``add_port``'s own is; ``pose``/``rot`` are a rigid DELTA in
  the block frame (translation added to the port's own origin, rotation
  composed on top of it), applied at read time only to a port that
  actually carries a pose. The named port need not exist yet — a
  forward reference, the same tolerance a measure's relation source gets.
- ``set_current_state``   — PERSISTENTLY pose an ordinary block into one
  of its declared states: ``block=`` + ``state=``. The counterpart to
  ``get(..., args={'state': {...}})``'s TRANSIENT override
  (:func:`precis_se.handler._apply_state_arg`), which never writes here —
  this op is what actually changes a block's recorded current state
  (:func:`precis.design.states.set_current_state`). Same store-aware
  deferral (``node.pending_current_state``), validated against the
  block's declared states — its own, or ones this same call just
  declared — by the handler once a uid exists.

**Atomic mode** (docs/backlog/nm-se-merge.md — the merged ``nm`` kind; the
vetting lives in :mod:`precis_se.atomic.vocab`) adds the L2 vocabulary a
block whose realization is *chemistry* states. It is the same op table,
not a second one: an atomic design is an se design whose blocks carry
``mode='atomic'`` and bind a ``structure`` realization, so these ops sit
beside the others and each one's *scope* is the block it names.

- ``declare_threading`` / ``remove_threading`` — block ``a`` is threaded
  through block ``b`` (a macrocycle on an axle): mechanical interlocking
  as an explicit, directional, name-keyed fact, never re-derived from L3
  coordinates. Mutual threading is refused (each would be inside the
  other); a pair whose block is removed goes with it, the same vacancy
  rule connects/measures/BOM follow.
- ``declare_dof`` / ``clear_dof`` — a block's declared degree of freedom:
  ``kind`` (rotational | translational) about the axis through two of the
  block's OWN ports (``axis_ports``). Ordinary blocks only — an instance
  resolves dof from its template at read time (:func:`effective_dof`),
  like envelope/ports. ``add_block`` accepts the same payload nested as
  ``dof={...}``; ``remove_port`` refuses a port the dof axis names.
- ``add_port`` grows ``expected_element``/``expected_hybridization`` — the
  chemistry a scaffold-side stub demands of the atom it will attach to
  (the gate the store-aware ``bind_structure`` checks).
- ``connect`` grows ``kind`` ∈ ``bond | interaction``. Omitted (the
  default) is se's ordinary structural edge, whose L2 statement is its
  ``joint`` — the two are mutually exclusive on one edge. A ``bond``
  additionally runs the **capability gate**: both ports must afford the
  role (``'covalent'`` by default, or ``objectives={'role': ...}``) — or,
  for a role with complementary halves (``'CuAAC'`` / ``'azide'`` ↔
  ``'alkyne'``, :data:`precis_se.atomic.vocab.COMPLEMENTARY_ROLES`), one
  port must afford each half — derived at connect time from the ports'
  ``roles`` sets and never stored as a second relation.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from precis.blocktree import ops as blocktree
from precis.blocktree.types import BlockNode, Connect, OpError, Port, Tree
from precis.design.states import StateError, validate_driver_kind
from precis_se import capabilities as se_caps
from precis_se import joints as se_joints
from precis_se.atomic.vocab import (
    CONNECT_KINDS,
    ThreadingSpec,
    check_bond_capability,
    check_dof_axis_ports,
    connect_role,
    vet_dof_shape,
)
from precis_se.bom import BomError, BomLine, vet_bom_fields
from precis_se.fret import (
    FretError,
    validate_chromophore,
    validate_optical_link,
    validate_optics,
)
from precis_se.identity import resolve_block
from precis_se.measures import (
    ORIGINS,
    UNITS,
    MeasureError,
    MeasureSpec,
    validate_relation,
)
from precis_se.modes import ModeError, parse_mode
from precis_se.notes import NOTE_KINDS, NoteError, NoteSpec, validate_about

#: What an L3 realization binding may point at — the *designed*
#: realizations (a cad node set, an atomistic ``structure`` scene) and the
#: two *bought* ones (an engineering-store component, a catalog part).
#: Mirrors migration 0008's ``se_blocks_bound_kind_check``. ``structure``
#: arrived with the atomic mode (docs/backlog/nm-se-merge.md): an atomic
#: block's realization IS chemistry, so the thing it binds is an atomistic
#: design. ``nm`` was legal here while that kind existed; the merge retired
#: the kind and 0008 dropped it from the CHECK constraint too.
_BINDING_KINDS: tuple[str, ...] = ("cad", "structure", "component", "part")


@dataclass
class PortSpec(Port):
    """A named attachment point on a block — the capability-set half of the
    "one fact, two projections" port (pcb-component-model.md). ``roles``/
    ``direction``/``annotations`` are the shared core's
    (:class:`~precis.blocktree.types.Port`); the fields below are se's own,
    and today only the **atomic mode** fills them (the nm→se merge): the
    chemistry a scaffold-side stub demands of the atom it will attach to,
    and — once ``bind_structure`` has run — the atom it actually got.

    ``expected_element``/``expected_hybridization`` stay their own typed
    fields rather than moving into the inherited ``annotations`` open dict
    (docs/backlog/blocktree-library-build-plan.md §Settled, "What this is
    NOT — a kind merge"): they are *checked* capabilities, and
    ``annotations`` is for what is merely descriptive."""

    expected_element: str | None = None
    expected_hybridization: str | None = None
    #: The atom-side projection of this one port fact (``structure`` design
    #: slug + atom label within it), set by the store-aware
    #: ``bind_structure`` op. NULL until filled; always both or neither.
    bound_design: str | None = None
    bound_atom: str | None = None


# ``OpError`` too is reused directly — there is nothing domain-specific
# about "the op was bad". Imported above; re-exported by being a top-level
# name in this module (``from precis_se.ops import OpError`` keeps working).


@dataclass
class ConnectSpec(Connect):
    """A port↔port intent edge between two ``'block.port'`` endpoints,
    name-keyed like everything else in this module. ``joint`` holds the
    slice-3 schema shape (:func:`precis_se.joints.validate_joint` —
    kinematic class × mechanism) — se's own extension over the shared
    :class:`~precis.blocktree.types.Connect`; ``objectives`` the
    registered loads vocabulary (:func:`precis_se.joints.
    validate_objectives`, real units), the shared field. Both are vetted
    at write time; stored strays are DRC findings.

    ``kind`` is the **atomic mode**'s own statement about the same edge
    (the nm→se merge): ``'bond'`` or ``'interaction'`` between two blocks
    whose realization is chemistry. ``None`` — the default — is se's
    ordinary structural connect, whose L2 statement is ``joint`` instead;
    it is not "unknown", and the bond capability gate
    (:func:`precis_se.atomic.vocab.connect_role`) reads ``'bond'``
    exactly. The two slots are deliberately not unified: a bolted revolute
    joint and a covalent bond are different claims about different
    physics, and one column holding either would have to be read twice."""

    joint: dict[str, Any] | None = None
    kind: str | None = None
    #: The **optical** (FRET) L2 statement about this same pair
    #: (:func:`precis_se.fret.validate_optical_link`): ``{'min_efficiency',
    #: 'channel'?, 'reason'?}`` — "this link must transfer at least this
    #: fraction of the donor's excitation". Unlike ``joint`` and ``kind``,
    #: which are competing claims about one physics and exclude each
    #: other, this slot is **compatible with both**: an optical link is a
    #: different physics on the same pair, and two blocks that are bonded
    #: and also exchange excitation are an ordinary molecule.
    optical: dict[str, Any] | None = None


@dataclass
class SeBlock(BlockNode):
    """One block, addressed by ``name`` (the stable identity — see the
    module docstring). ``parent``/``template`` are block *names*, resolved
    to fresh row ids only at persist time. ``array`` is the multiplicity
    spec when this node is an array instance (``template`` is then always
    set too); an ordinary instance has ``template`` set and ``array``
    ``None``. ``ports`` (shared with :class:`~precis.blocktree.types.
    Block`) is keyed by port name; only an ordinary (non-instance) block
    ever has entries here — an instance's/array's ports resolve from its
    template (:func:`effective_ports`). Pose is metres; rot radians (the
    units-policy-cutover angle ruling — degrees only at ingest/display,
    :mod:`precis.utils.units`; a pre-cutover stored design's degree-valued
    ``pose_rot`` was converted in place by
    ``precis_se/migrations/0006_units_se_pose_rot_rad.sql``). The
    fields below this line are se's own extension over the shared
    ``Block``."""

    #: Re-declared (not new) — narrows the inherited ``dict[str, Port]`` to
    #: se's own :class:`PortSpec`; every port this module's ``add_port``
    #: ever stores is one.
    # mypy flags this as an unsafe narrowing (dict is invariant — a caller
    # holding this as a plain BlockNode could in principle assign a bare
    # Port in). ``BlockNode`` isn't generic over its port type the way
    # ``Tree`` is over block/connect (docs/backlog/
    # blocktree-library-build-plan.md §Settled known wart: a real gap, not
    # papered over — worth a ``BlockNode[TPort: Port]`` now that the merge
    # leaves se as the one domain with its own port fields), so this is the
    # narrowest fix available without widening that core class.
    ports: dict[str, PortSpec] = field(default_factory=dict)  # type: ignore[assignment]
    #: Stable identity (``se_blocks.uid``, migration ``0009_se_block_uid``,
    #: minted from core's ``design_block_uid_seq`` —
    #: docs/backlog/design-state-core.md item 2). ``None`` on a block this
    #: session just added: :func:`precis_se.persist.save_tree` mints it (or
    #: adopts the live row's, when the label matches) and stamps it back
    #: here, so a saved tree always carries one. Ops never set it —
    #: ``name`` remains the key the in-memory tree is addressed by, now as
    #: a display *label*, and the uid is what survives the save.
    uid: int | None = None
    array: dict[str, Any] | None = None
    #: loads on the block — the registered objectives vocabulary
    #: (:func:`precis_se.joints.validate_objectives`), real units.
    objectives: dict[str, Any] = field(default_factory=dict)
    #: Atomic-mode L2 declared degree of freedom
    #: (``{'kind', 'axis_ports'}`` — :func:`precis_se.atomic.vocab.
    #: vet_dof_shape`), stored explicitly and never re-derived from L3
    #: coordinates. Always ``None`` on an instance — an instance resolves
    #: dof from its template at read time (:func:`effective_dof`), the same
    #: rule as envelope/ports/desc/use.
    dof: dict[str, Any] | None = None
    #: L5 manufacturing mode (:mod:`precis_se.modes`) — ``None`` is
    #: honest: unassigned, not "assume it's printed".
    mode: str | None = None
    #: L3 realization binding: ``('cad'|'nm'|'component'|'part', slug)``,
    #: both ``None`` when the block's solid is still just its envelope.
    bound_kind: str | None = None
    bound: str | None = None
    #: Per-block overrides of a :mod:`precis_se.capabilities` figure,
    #: keyed by field name (``{'max_overhang': 35}``) — the override tier
    #: :func:`precis_se.capabilities.resolve` reads first, migration
    #: ``0011_se_process_overrides.sql``. ``None`` = no overrides (never
    #: stored as ``{}``). A field this block's mode family does not
    #: define, or a value beyond the field's physical floor, is rejected
    #: at write time by :func:`_op_set_process_override` — this dict never
    #: holds one. Realization-facet-adjacent like ``mode``/``bound_kind``,
    #: and template-owned the same way: :func:`_op_set_process_override`
    #: goes through :func:`_template_owned` too, for the same reason
    #: ``mode`` does — an instance has no mode of its own to check a field
    #: against.
    process_overrides: dict[str, float] | None = None
    #: A **pinned** print build-down direction (se-print-implementer.md
    #: Engine 2): ``{"down": [x, y, z], "origin": "user"}``, unit vector.
    #: ``None`` = no pin — ``view='print'`` proposes the best-scoring
    #: candidate every read and stores nothing. Deliberately never carries
    #: a ``score``: a stored score would go stale the moment the block's
    #: solid changes (a stamped hole added, a cut fixed), and the pin's
    #: whole contract is that it survives exactly that (a later
    #: ``set_envelope`` never touches this field) — the view recomputes
    #: the pinned candidate's score fresh on every read instead of trusting
    #: one written once. Written/cleared by :func:`_op_set_build_frame`/
    #: ``clear_build_frame``; ``se_blocks.build_frame`` has carried this
    #: column, dark, since migration 0001 — rung 4 is the first write to
    #: it, so no new migration is needed. Realization-facet-adjacent and
    #: template-owned the same way ``mode``/``process_overrides`` are.
    build_frame: dict[str, Any] | None = None
    #: Catalog-**derived** envelope/ports for a `component` binding
    #: (:mod:`precis_se.catalog`), filled at load time by
    #: :func:`precis_se.persist.load_tree`. DERIVED, never stored: it is
    #: recomputed from the component's spec rows on every read, the same
    #: sketch-canonical / copper-derived rule the rest of the tree
    #: follows, so ``save_tree`` must never write it back. A block that
    #: authors its own envelope always wins over this.
    derived: Any = None
    #: The optical property card that makes this block a FRET node
    #: (:func:`precis_se.fret.validate_chromophore`): transition dipole in
    #: the BLOCK frame, quantum yield, excited-state lifetime, emission and
    #: absorption spectra. Block-owned like ``dof`` and ``objectives``
    #: rather than a table of its own — one per block, meaningless without
    #: it, gone when it goes. The dipole is stored in the block frame so
    #: the pose already on this node rotates it into world space: move the
    #: block and its optics follow, which is the whole reason the
    #: orientation factor κ² can be computed from realised geometry
    #: instead of assumed.
    chromophore: dict[str, Any] | None = None
    #: ``user | proposed`` stamps for authored facets, keyed by facet name
    #: (``'envelope'``, ``'pose'``) — slice 4's freedom vocabulary. An
    #: absent key means ``user`` (the default is never stored); a propose
    #: job stamps ``proposed`` on its own choices and treats user facets
    #: as contract.
    origins: dict[str, str] = field(default_factory=dict)
    #: This call's ``declare_states``/``declare_transitions`` payload
    #: (blocktree slice 2), vetted but not yet written — ``None`` means
    #: neither op ran this call. NEVER round-tripped through ``se_blocks``:
    #: :mod:`precis_se.persist` does not read or write these fields at all,
    #: because the actual store is the shared ``design_states``/
    #: ``design_transitions`` tables (:mod:`precis.design.states`), keyed
    #: by ``block_uid`` — a fact only known once ``save_tree`` mints one.
    #: :class:`~precis_se.handler.SeHandler` reads these right after
    #: ``save_tree`` and clears them by discarding the tree; a block this
    #: call didn't touch keeps whatever it already had in the shared
    #: tables, untouched.
    pending_states: list[dict[str, Any]] | None = None
    pending_transitions: list[dict[str, Any]] | None = None
    #: This call's ``set_current_state`` payload (blocktree slice 2's
    #: PERSISTENT posing op — the counterpart to get's TRANSIENT
    #: ``args={'state': ...}`` override) — the state name to pose this
    #: block into, or ``None`` when the op didn't run this call. Same
    #: deferral as ``pending_states``/``pending_transitions``: written by
    #: :func:`precis_se.handler._materialize_states` once ``save_tree`` has
    #: minted a uid, after validating the name against the block's
    #: (possibly just-declared) states.
    pending_current_state: str | None = None


@dataclass
class SeTree(Tree[SeBlock, ConnectSpec]):
    """A design's live blocks, keyed by name, plus its live ``connects``.
    Insertion order is not significant — renderers/persisters compute
    their own (tree / topological) order from ``parent``/``template``;
    ``connects`` is an unordered list (pair identity, not position). The
    fields below this line are se's own extension over the shared
    :class:`~precis.blocktree.types.Tree`."""

    #: named measures + tolerance relations (:mod:`precis_se.measures`),
    #: unordered — identity is ``(block, name)``.
    measures: list[MeasureSpec] = field(default_factory=list)
    #: bought items (:mod:`precis_se.bom`), unordered — identity is
    #: (target, item_kind, item).
    bom: list[BomLine] = field(default_factory=list)
    #: the interrogation ledger (:mod:`precis_se.notes`), created order —
    #: identity is the note ``name``.
    notes: list[NoteSpec] = field(default_factory=list)
    #: atomic-mode L2 threading invariants (:class:`~precis_se.atomic.
    #: vocab.ThreadingSpec`), unordered — identity is the ``(a, b)`` pair.
    threading: list[ThreadingSpec] = field(default_factory=list)
    #: The design's optical context (:func:`precis_se.fret.validate_optics`)
    #: — ``{'medium_index', 'excitation_nm'?}``. The one tree-level scalar
    #: record se carries, and it earns that by being a fact about the
    #: *space* rather than about any block: every Förster radius in the
    #: design divides by the same medium index, and every spectral-
    #: crosstalk figure is quoted at the same pump wavelength. ``None``
    #: means undeclared, and the FRET view says so rather than quietly
    #: using :data:`~precis_se.fret.DEFAULT_MEDIUM_INDEX`.
    optics: dict[str, Any] | None = None

    def make_block(self, **kwargs: Any) -> SeBlock:
        return SeBlock(**kwargs)

    def resolve_key(self, token: Any) -> str | None:
        """se's identity rule, wired into every op at once: a block token
        is a uid (``'#41'``, ``'uid:41'``, ``41``) or a label
        (:func:`precis_se.identity.resolve_block`), and what comes back is
        the key it lives under in :attr:`blocks`. Raises
        :class:`~precis_se.identity.AmbiguousLabel` — an :class:`OpError`
        — when two blocks answer to one label, listing both uids rather
        than picking one."""
        node = resolve_block(self, token)
        if node is None:
            return None
        label = str(node.name)
        if self.blocks.get(label) is node:
            return label
        # A tree whose keys and names diverged (a paste, a branch merge):
        # identity is the NODE, so hand back the key actually holding it.
        return next((k for k, v in self.blocks.items() if v is node), None)


def apply_ops(tree: SeTree, ops: list[dict[str, Any]]) -> SeTree:
    """Apply a list of typed ops to ``tree`` in order, mutating it —
    dispatches through :data:`_OPS` (the core's 9 shared ops, 8 of them
    overridden here, plus se's own — :func:`known_ops` is the roster), via
    the core's generic :func:`~precis.blocktree.ops.apply_ops`."""
    return blocktree.apply_ops(tree, ops, _OPS)


# ── helpers (imported from the core; se calls these directly for its own
# op implementations below) ─────────────────────────────────────────────

_require_name = blocktree._require_name
_opt_str = blocktree._opt_str
_as_vec3 = blocktree._as_vec3
_unit_vec = blocktree._unit_vec
_no_block_msg = blocktree._no_block_msg
_validate_envelope = blocktree._validate_envelope
_descendants = blocktree._descendants
_split_endpoint = blocktree._split_endpoint
#: "Which block did the caller mean" — the core helpers that run a block
#: token through :meth:`SeTree.resolve_key`, so every se op that addresses
#: an existing block takes a uid (``'#41'``) as readily as a label
#: (docs/backlog/design-state-core.md item 2, "ops accept uid always").
#: ``_require_block`` = require the op key + resolve; ``_block_key`` =
#: resolve a bare token; ``_block_key_or_raw`` = the lenient form for ops
#: that MATCH a stored row rather than address a block.
_require_block = blocktree._require_block
_block_key = blocktree._block_key
_block_key_or_raw = blocktree._block_key_or_raw
_connects_endpoint_pair = blocktree._connects_endpoint_pair
_instance_shared = blocktree._instance_shared
_commit_instance = blocktree._commit_instance
#: Re-exported so the handler's own ``mode``/``binding`` template lookups
#: (se-specific fields the core knows nothing about, same reasoning as
#: ``precis_nm.ops.effective_dof``) can resolve a LOCAL or cross-design
#: template the same way :func:`effective_envelope`/:func:`effective_ports`
#: already do, instead of a bare ``tree.blocks.get`` that would silently
#: never see a foreign template.
resolve_template = blocktree.resolve_template


def effective_envelope(tree: SeTree, node: SeBlock) -> str | None:
    """The envelope "seen" at ``node`` for render purposes — its own, or —
    when ``node`` is an instance/array — its template's (an instance's own
    ``envelope`` field is always ``None``; see the template-metadata
    rejection in :func:`~precis.blocktree.ops._instance_shared`). A
    dangling ``template`` resolves to ``None`` rather than raising, so
    defense-in-depth callers (render, validate) stay total functions —
    both cases are the shared core's :func:`~precis.blocktree.ops.
    effective_envelope`.

    Third source (rung 2b, se's own extension): a block bound to a
    `component` with no envelope of its own falls back to the
    **catalog-derived** one (:mod:`precis_se.catalog`) — a bought part's
    solid comes from its spec row, not from something a designer drew. An
    authored envelope always wins: overriding the catalog is a legitimate
    act (a part modified after purchase), and silently preferring the
    catalog would discard it."""
    base = blocktree.effective_envelope(tree, node)
    if base is not None or node.template is not None:
        return base
    return getattr(node.derived, "envelope", None)


def effective_ports(tree: SeTree, node: SeBlock) -> dict[str, PortSpec]:
    """The ports "seen" at ``node`` for connect/render purposes: its own
    ports, or — when ``node`` is an instance/array — its template's (an
    instance never owns ports itself; see ``add_port``'s rejection) — the
    shared core's :func:`~precis.blocktree.ops.effective_ports`.

    Third source (rung 2b, se's own extension): a block bound to a
    `component` gets the **catalog's** port templates when it declares
    none of its own — without them ``connect`` cannot attach to a bought
    part at all. Own ports still win, and they win *per name*, so a
    designer can rename or re-role one port of a bought part without
    losing the rest.

    Retyped (not just re-exported) for se's own :class:`PortSpec`: every
    port ``add_port`` stores is one, so the dict the core returns always is
    too — mypy's dict-is-invariant check just can't see that."""
    if node.template is not None:
        return cast("dict[str, PortSpec]", blocktree.effective_ports(tree, node))
    derived = getattr(node.derived, "ports", None)
    if derived:
        merged = dict(derived)
        merged.update(node.ports)
        return merged
    return node.ports


def effective_dof(tree: SeTree, node: SeBlock) -> dict[str, Any] | None:
    """The atomic-mode dof "seen" at ``node`` for render purposes — its
    own, or — when ``node`` is an instance/array — its template's, LOCAL or
    cross-design (:func:`resolve_template`; an instance's own ``dof`` field
    is always ``None``, and ``declare_dof`` only ever writes to an ordinary
    block). A physically real degree of freedom on a template block
    genuinely applies to every instance of it, so this mirrors
    :func:`effective_envelope`'s instance→template resolution. Has no
    shared-core analogue — the core knows nothing about dof."""
    if node.template is not None:
        template_node = resolve_template(tree, node.template)
        return getattr(template_node, "dof", None)
    return node.dof


#: se's catalog-aware :func:`effective_ports` in the shape the core's
#: endpoint resolvers take it — the dict-invariance cast
#: :func:`_resolve_connect_port` documents, named once.
_PORTS_FN = cast("Callable[[Any, Any], dict[str, Port]]", effective_ports)


def _resolve_connect_port(
    tree: SeTree, block_name: str, port_name: str, *, what: str
) -> PortSpec:
    """se's own :func:`effective_ports` (the catalog-aware one) plugged
    into the core's endpoint resolver. Both casts are the same
    dict-invariance wart :func:`effective_ports` documents: the core's
    ``ports_fn`` hook is typed over the base :class:`~precis.blocktree.
    types.Port`, and a ``dict[str, PortSpec]`` is not a ``dict[str,
    Port]`` to mypy even though every value in it is one."""
    return cast(
        "PortSpec",
        blocktree._resolve_connect_port(
            tree,
            block_name,
            port_name,
            what=what,
            ports_fn=_PORTS_FN,
        ),
    )


def _resolve_endpoint(
    tree: SeTree, raw: Any, *, what: str, side: str
) -> tuple[str, str, PortSpec]:
    """A whole ``'block.port'`` endpoint → ``(block label, port name,
    port)`` through se's own ports (the core's :func:`~precis.blocktree.
    ops._resolve_endpoint`): the block half is an ordinary block token, so
    ``'#41.bore'`` addresses uid 41's port, and what comes back — and gets
    stored on the connect — is that block's current LABEL."""
    block, port_name, port = blocktree._resolve_endpoint(
        tree, raw, what=what, side=side, ports_fn=_PORTS_FN
    )
    return block, port_name, cast("PortSpec", port)


def _match_endpoint(tree: SeTree, raw: Any, *, what: str, side: str) -> tuple[str, str]:
    """:func:`_resolve_endpoint`'s lenient sibling for the ops that look up
    an existing connect by endpoint pair — the core's
    :func:`~precis.blocktree.ops._match_endpoint`: resolve the block half
    when something answers to it, else leave the text alone so the
    caller's "no such connect; here's what IS live" message survives."""
    return blocktree._match_endpoint(tree, raw, what=what, side=side)


# ── op implementations ───────────────────────────────────────────────────


def _op_add_block(tree: SeTree, op: dict[str, Any]) -> None:
    """The core's ``add_block`` plus se's optional ``dof={...}`` — the
    atomic-mode L2 slot, vetted for shape here and for *port existence*
    only at the end of the whole ops list (:func:`~precis_se.atomic.vocab.
    check_dof_axis_ports`'s docstring: a block minted by ``add_block``
    owns no ports yet at this instant, so any ``add_port`` naming its axis
    necessarily comes later in the same call). A bad shape rolls the block
    back out rather than leaving a half-declared node behind."""
    blocktree.op_add_block(tree, op)
    dof_raw = op.get("dof")
    if dof_raw is None:
        return
    name = str(op["name"]).strip()
    try:
        dof = vet_dof_shape(dof_raw, what="add_block 'dof'")
    except OpError:
        del tree.blocks[name]
        raise
    tree.blocks[name].dof = dof


def _op_instance_block(tree: SeTree, op: dict[str, Any]) -> None:
    for key in ("linear", "polar"):
        if op.get(key) is not None:
            raise OpError(
                f"instance_block does not take {key!r} — use array_block "
                "for a patterned instance"
            )
    _reject_instance_dof(tree, op, opname="instance_block")
    blocktree.op_instance_block(tree, op)


def _reject_instance_dof(tree: SeTree, op: dict[str, Any], *, opname: str) -> None:
    """``dof`` on an instance/array node is refused, not dropped — an
    instance resolves dof from its template at read time
    (:func:`effective_dof`), exactly as it does envelope/ports/desc/use,
    so accepting the key would store a fact nothing ever reads (the
    swallowed-facet rule)."""
    if op.get("dof") is None:
        return
    template = str(op.get("template") or "").strip()
    raise OpError(
        f"{opname} does not take 'dof' — an instance resolves dof from its "
        f"template ({template!r}) at read time; set it on the template "
        "block instead"
    )


def _parse_array_spec(op: dict[str, Any]) -> dict[str, Any]:
    """Vet exactly one of ``linear``/``polar`` into the stored array spec
    (se-kind.md "Hierarchy": the cad node-level ``linear:``/``polar:``
    modifiers lifted to block level, with an explicit axis). ``overrides``
    is a later round — rejected loudly today, never swallowed."""
    if op.get("overrides") is not None:
        raise OpError(
            "array_block does not take 'overrides' yet — per-member "
            "deviation (override entries / unlink-to-concrete-copy) is a "
            "later round; model the deviating member as its own block for "
            "now"
        )
    linear, polar = op.get("linear"), op.get("polar")
    if (linear is None) == (polar is None):
        raise OpError(
            "array_block needs exactly one of 'linear' (count/pitch/axis) "
            "or 'polar' (count/radius/axis)"
        )
    raw = linear if linear is not None else polar
    kind = "linear" if linear is not None else "polar"
    if not isinstance(raw, dict):
        raise OpError(f"array_block {kind!r} must be a JSON object, got {raw!r}")
    count_raw = raw.get("count", 0)
    try:
        count = int(count_raw)
        # int() truncates a float — a fat-fingered count=2.9 must reject,
        # never silently become a 2-member array (reviewer finding).
        if float(count_raw) != count:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"array_block {kind} 'count' must be a whole number, got {count_raw!r}"
        ) from exc
    if count < 2:
        raise OpError(
            f"array_block {kind} 'count' must be ≥ 2, got {count} — an "
            "array of one is just an instance; use instance_block"
        )
    if kind == "linear":
        try:
            pitch = float(raw.get("pitch", 0.0))
        except (TypeError, ValueError) as exc:
            raise OpError("array_block linear 'pitch' must be a number (m)") from exc
        if pitch <= 0.0:
            raise OpError(f"array_block linear 'pitch' must be > 0 m, got {pitch!r}")
        axis = _unit_vec(
            _as_vec3(raw.get("axis"), "array_block linear 'axis'"),
            what="array_block linear 'axis'",
        )
        return {"kind": "linear", "count": count, "pitch": pitch, "axis": axis}
    try:
        radius = float(raw.get("radius", 0.0))
    except (TypeError, ValueError) as exc:
        raise OpError("array_block polar 'radius' must be a number (m)") from exc
    if radius < 0.0:
        raise OpError(f"array_block polar 'radius' must be ≥ 0 m, got {radius!r}")
    # axis defaults to +z — the cad `polar:nNrR` modifier's implicit spin
    # axis, made explicit and overridable at block level. radius 0 is
    # legitimate: a pure rotational pattern of a non-centred template.
    axis_raw = raw.get("axis")
    axis = (
        _unit_vec(
            _as_vec3(axis_raw, "array_block polar 'axis'"),
            what="array_block polar 'axis'",
        )
        if axis_raw is not None
        else [0.0, 0.0, 1.0]
    )
    return {"kind": "polar", "count": count, "radius": radius, "axis": axis}


def _op_array_block(tree: SeTree, op: dict[str, Any]) -> None:
    template, name, parent = _instance_shared(tree, op, opname="array_block")
    _reject_instance_dof(tree, op, opname="array_block")
    spec = _parse_array_spec(op)
    _commit_instance(
        tree, op, name=name, template=template, parent=parent, extra={"array": spec}
    )


def _op_set_envelope(tree: SeTree, op: dict[str, Any]) -> None:
    name = _require_block(tree, op, "block", "set_envelope")
    node = tree.blocks[name]
    if node.template is not None:
        raise OpError(
            f"block {name!r} is an instance (of {node.template!r}) — the "
            "envelope lives on the template; set_envelope on "
            f"{node.template!r} instead"
        )
    if "envelope" not in op:
        raise OpError("set_envelope needs 'envelope' (a cad DSL config, or null)")
    envelope = op.get("envelope")
    if envelope is not None:
        envelope = str(envelope).strip()
        _validate_envelope(envelope)
    node.envelope = envelope
    _stamp_origin(node, op, facet="envelope", opname="set_envelope")


def _op_set_desc(tree: SeTree, op: dict[str, Any]) -> None:
    """Amend a block's prose — ``desc``/``use`` (:attr:`BlockNode.descr`/
    ``.use``, the fields ``add_block`` sets at creation time only).
    Either key may be given alone; ``null`` clears it. Without this op,
    changing a block's description after the fact meant a full
    destructive re-put (gr334771) — the sole prose channel had no
    incremental path, unlike ``set_envelope``/``set_pose``/etc."""
    name = _require_block(tree, op, "block", "set_desc")
    node = tree.blocks[name]
    if node.template is not None:
        raise OpError(
            f"block {name!r} is an instance (of {node.template!r}) — "
            "desc/use live on the template, same as envelope/ports; "
            f"set_desc on {node.template!r} instead"
        )
    if "desc" not in op and "use" not in op:
        raise OpError("set_desc needs 'desc' and/or 'use' (null clears either)")
    if "desc" in op:
        node.descr = _opt_str(op.get("desc"))
    if "use" in op:
        node.use = _opt_str(op.get("use"))


def _stamp_origin(
    node: SeBlock, op: dict[str, Any], *, facet: str, opname: str
) -> None:
    """Record the op's optional ``origin`` (user | proposed) for a block
    facet — slice 4's freedom vocabulary. ``user`` (the default) is never
    stored; a re-authored facet with no ``origin`` keeps its prior stamp
    (the author who says nothing is not thereby claiming the user's
    contract tier — a propose job must be able to omit it safely only by
    stating it, so the honest default is "unchanged")."""
    raw = op.get("origin")
    if raw is None:
        return
    origin = str(raw).strip().lower()
    if origin not in ORIGINS:
        raise OpError(
            f"{opname} 'origin' must be one of {' | '.join(ORIGINS)}, got {raw!r}"
        )
    if origin == "user":
        node.origins.pop(facet, None)
    else:
        node.origins[facet] = origin


def _op_set_pose(tree: SeTree, op: dict[str, Any]) -> None:
    """The core ``set_pose`` plus the facet-origin stamp (an instance's
    pose is its own, so the stamp lands on the posed node itself)."""
    blocktree.op_set_pose(tree, op)
    node = tree.blocks[_require_block(tree, op, "block", "set_pose")]
    _stamp_origin(node, op, facet="pose", opname="set_pose")


def _op_remove_block(tree: SeTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "remove_block")
    # Resolved leniently: a token naming nothing is the core op's error to
    # raise (below), with its roster of what IS there. Computed before
    # delegating (the core op raises for an unknown block or one used as a
    # template — atomically, before any mutation) so the measures/BOM
    # cascade acts on exactly the subtree the core just removed.
    name = _block_key_or_raw(tree, name)
    subtree = (_descendants(tree, name) | {name}) if name in tree.blocks else set()
    blocktree.op_remove_block(tree, op)
    # Measures owned by a removed block go with it (same vacancy rule). A
    # surviving measure whose *relation source* lived in the subtree is
    # deliberately kept — it dangles, and DRC's unresolvable_relation
    # finding reports it, read-time honesty over silent cleanup.
    tree.measures = [m for m in tree.measures if m.block not in subtree]
    # BOM lines follow their target: a block line in the subtree, and a
    # connect line on a connect that was just dropped by the core op
    # (exactly those whose endpoint block is in the subtree).
    tree.bom = [
        line
        for line in tree.bom
        if line.block not in subtree
        and not (
            line.is_connect and (line.a_block in subtree or line.b_block in subtree)
        )
    ]
    # Threading is name-keyed the same way connects are, so the same
    # vacancy rule drops any pair touching the removed subtree — DRC's
    # ``dangling_threading`` exists to catch the cases where this *didn't*
    # run (hand-corrupted data).
    tree.threading = [
        t for t in tree.threading if t.a not in subtree and t.b not in subtree
    ]


def _op_add_port(tree: SeTree, op: dict[str, Any]) -> None:
    """The core's ``add_port`` (which owns every check and the
    roles/direction/pose/annotations vetting) plus the atomic mode's two
    *expected chemistry* fields — se's own :class:`PortSpec`. The core
    mints a bare :class:`~precis.blocktree.types.Port`; this rewrites that
    slot as the richer spec, so there is one add_port grammar for an agent
    to learn whatever mode the block is in. ``set_port_pose`` needs no
    such override — it mutates the existing port object in place, so the
    :class:`PortSpec` survives."""
    blocktree.op_add_port(tree, op)
    node = tree.blocks[_require_block(tree, op, "block", "add_port")]
    name = str(op["name"]).strip()
    base = node.ports[name]
    node.ports[name] = PortSpec(
        name=base.name,
        roles=base.roles,
        direction=base.direction,
        annotations=base.annotations,
        pose=base.pose,
        rot=base.rot,
        pose_source=base.pose_source,
        expected_element=_opt_str(op.get("expected_element")),
        expected_hybridization=_opt_str(op.get("expected_hybridization")),
    )


def _op_remove_port(tree: SeTree, op: dict[str, Any]) -> None:
    """The core's ``remove_port`` (which refuses a port a live connect
    still uses) plus se's dof guard: a port named in the block's own
    declared ``axis_ports`` would otherwise leave dof pointing at a
    vanished port name — a dangling reference, so refuse up front and name
    ``clear_dof`` as the fix. Checked *before* delegating, since the core
    op deletes the port as its last act and the dof reference has to be
    read while it still exists."""
    block = _block_key_or_raw(tree, _require_name(op, "block", "remove_port"))
    name = _require_name(op, "name", "remove_port")
    node = tree.blocks.get(block)
    if node is not None and name in node.ports and node.dof:
        if name in (node.dof.get("axis_ports") or ()):
            raise OpError(
                f"port {block}.{name} is used by declared dof (axis_ports) — "
                "clear_dof first"
            )
    blocktree.op_remove_port(tree, op)


def _op_connect(tree: SeTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("connect needs 'a' and 'b' (each 'block.port')")
    # Shape first (both endpoints are 'block.port', and not the same one
    # twice) — the token-level rejections, before anything is looked up.
    if _split_endpoint(a_raw, "connect 'a'") == _split_endpoint(b_raw, "connect 'b'"):
        raise OpError(f"connect: cannot connect {a_raw!r} to itself")
    joint = _vet_joint(op.get("joint"), opname="connect")
    kind = _vet_connect_kind(op.get("kind"), joint=joint)
    objectives = _vet_objectives(op.get("objectives"), opname="connect", edge=True)
    a_block, a_port, a_spec = _resolve_endpoint(tree, a_raw, what="connect", side="'a'")
    b_block, b_port, b_spec = _resolve_endpoint(tree, b_raw, what="connect", side="'b'")
    if (a_block, a_port) == (b_block, b_port):
        # Reachable a second way once uids resolve: '#41.bore' and
        # 'wheel.bore' are the same endpoint written two ways.
        raise OpError(f"connect: cannot connect {a_raw!r} to itself")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for c in tree.connects:
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            raise OpError(
                f"connect: {a_block}.{a_port}—{b_block}.{b_port} already exists"
            )
    # The atomic mode's capability gate — only a ``kind='bond'`` edge has a
    # role to afford (:func:`~precis_se.atomic.vocab.connect_role`).
    role = connect_role(kind, objectives or {})
    if role is not None:
        check_bond_capability(a_block, a_port, a_spec, b_block, b_port, b_spec, role)
    tree.connects.append(
        ConnectSpec(
            a_block=a_block,
            a_port=a_port,
            b_block=b_block,
            b_port=b_port,
            joint=joint,
            kind=kind,
            objectives=objectives or {},
        )
    )


def _vet_connect_kind(raw: Any, *, joint: dict[str, Any] | None) -> str | None:
    """Vet a connect's optional atomic-mode ``kind``
    (:data:`~precis_se.atomic.vocab.CONNECT_KINDS`). Absent is the ordinary
    structural edge (``None``, whose L2 statement is ``joint``); present
    means the edge is chemistry, so a ``joint`` on the same edge is refused
    rather than stored unread — a kinematic class and a bond are two
    different claims about the same pair, and nothing reads both."""
    if raw is None:
        return None
    kind = str(raw).strip().lower()
    if kind not in CONNECT_KINDS:
        known = " | ".join(CONNECT_KINDS)
        raise OpError(
            f"connect 'kind' must be {known} (or omitted, for an ordinary "
            f"structural connect whose L2 statement is its joint); got {raw!r}"
        )
    if joint is not None:
        raise OpError(
            f"connect: 'kind' ({kind}) and 'joint' are mutually exclusive — "
            "an atomic bond/interaction and a kinematic joint are different "
            "claims about the same pair; declare one"
        )
    return kind


def _op_disconnect(tree: SeTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("disconnect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _match_endpoint(tree, a_raw, what="disconnect", side="'a'")
    b_block, b_port = _match_endpoint(tree, b_raw, what="disconnect", side="'b'")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    blocktree.op_disconnect(tree, op)
    # BOM lines hung off this connect go with it (same vacancy rule as
    # remove_block) — a bearing bought *for a joint* has no meaning once
    # the joint is gone.
    tree.bom = [
        line for line in tree.bom if not (line.is_connect and _bom_pair(line) == pair)
    ]


def _vet_joint(raw: Any, *, opname: str) -> dict[str, Any] | None:
    """``joint=`` through the one schema (:mod:`precis_se.joints`) — write
    time is where a malformed joint gets rejected; DRC only *reports* what
    slipped past into storage."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpError(f"{opname} 'joint' must be a JSON object, got {raw!r}")
    try:
        return se_joints.validate_joint(raw)
    except se_joints.JointError as exc:
        raise OpError(f"{opname}: {exc}") from exc


def _vet_objectives(
    raw: Any, *, opname: str, edge: bool = False
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpError(f"{opname} 'objectives' must be a JSON object, got {raw!r}")
    if edge and raw.get("fixed") is not None:
        # reject on EVERY edge write path, not just set_load — a stored
        # 'fixed' on a connect is read by nothing (the swallowed-facet
        # lesson, reviewer finding 2026-09-09).
        raise OpError(
            f"{opname}: 'fixed' grounds a BLOCK's translations (stability "
            "supports) — it has no meaning on a connect"
        )
    if not edge and raw.get("role") is not None:
        # The mirror of the rejection above: 'role' is the atomic bond
        # capability gate's override and is read on connects only (a block
        # affords roles through its PORTS, never as a load objective).
        raise OpError(
            f"{opname}: 'role' gates a kind='bond' CONNECT's ports — it has "
            "no meaning on a block; a block's affordances are its ports' roles"
        )
    try:
        return se_joints.validate_objectives(raw)
    except se_joints.JointError as exc:
        raise OpError(f"{opname}: {exc}") from exc


def _find_connect(tree: SeTree, op: dict[str, Any], *, opname: str) -> ConnectSpec:
    """Resolve ``a=``/``b=`` to the live connect with that unordered
    endpoint pair — the ``disconnect`` lookup, shared by the joint/load
    ops that address an existing edge."""
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError(f"{opname} needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _match_endpoint(tree, a_raw, what=opname, side="'a'")
    b_block, b_port = _match_endpoint(tree, b_raw, what=opname, side="'b'")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for c in tree.connects:
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            return c
    live = (
        ", ".join(
            f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}" for c in tree.connects
        )
        or "(none)"
    )
    raise OpError(
        f"{opname}: no such connect between {a_raw!r} and {b_raw!r}. "
        f"Live connects: {live}"
    )


def _op_set_joint(tree: SeTree, op: dict[str, Any]) -> None:
    """Set/replace (or clear, with ``joint=null``) an existing connect's
    joint — the slice-3 schema: ``{'class': rigid|revolute|prismatic|
    cylindrical|planar|ball|compliant|captive, 'axis'?: [x,y,z],
    'mechanism'?: snap|screw|press|key|magnet|bearing|bond|integral,
    'params'?: {...}}``."""
    c = _find_connect(tree, op, opname="set_joint")
    if "joint" not in op:
        raise OpError("set_joint needs 'joint' (the joint object, or null to clear)")
    joint = _vet_joint(op.get("joint"), opname="set_joint")
    if joint is not None and c.kind is not None:
        # Mirrors ``connect``'s own mutual exclusion (:func:`_vet_connect_kind`)
        # — reachable the long way round otherwise.
        raise OpError(
            f"set_joint: {c.a_block}.{c.a_port}—{c.b_block}.{c.b_port} is an "
            f"atomic {c.kind} connect — a kinematic joint and a bond are "
            "different claims about the same pair; disconnect and re-connect "
            "without 'kind' if the edge is structural"
        )
    c.joint = joint


def _op_set_load(tree: SeTree, op: dict[str, Any]) -> None:
    """Merge loads (objective vectors, real units) onto a block
    (``block=``) or an existing connect (``a=``/``b=``). MERGE semantics,
    not replace — given keys overlay whatever's already stored, so
    ``set_load(block, fixed=true)`` (declare support) followed by
    ``set_load(block, force=[...])`` (declare load) — the natural
    authoring order, in separate calls — keeps both instead of the second
    call silently dropping the first (gr335190). ``clear=true`` stays the
    explicit full reset, on either target."""
    has_block = op.get("block") is not None
    has_edge = op.get("a") is not None or op.get("b") is not None
    if has_block == has_edge:
        raise OpError(
            "set_load targets exactly one of a block (block=) or a "
            "connect (a= and b=, each 'block.port')"
        )
    allowed = {"op", "block", "a", "b", "clear"} | set(se_joints.OBJECTIVE_KEYS)
    strays = sorted(set(op) - allowed)
    if strays:
        # a typo'd load key next to a valid one must reject, never be
        # silently dropped (the swallowed-facet lesson).
        known = ", ".join(sorted(se_joints.OBJECTIVE_KEYS))
        raise OpError(
            f"set_load: unknown key(s) {', '.join(strays)} — registered "
            f"load keys: {known}"
        )
    given = {k: op[k] for k in se_joints.OBJECTIVE_KEYS if op.get(k) is not None}
    clear = bool(op.get("clear"))
    if clear:
        if given:
            raise OpError("set_load: 'clear' and load keys are mutually exclusive")
        vetted: dict[str, Any] = {}
    else:
        if not given:
            known = ", ".join(sorted(se_joints.OBJECTIVE_KEYS))
            raise OpError(
                f"set_load needs at least one of {known} (or clear=true "
                "to remove loads)"
            )
        vetted = _vet_objectives(given, opname="set_load", edge=not has_block) or {}
    if has_block:
        node = tree.blocks[_require_block(tree, op, "block", "set_load")]
        node.objectives = {} if clear else {**node.objectives, **vetted}
    else:
        c = _find_connect(tree, op, opname="set_load")
        c.objectives = {} if clear else {**c.objectives, **vetted}


_STRENGTHS = ("hard", "soft", "gauge")


def _measure_shared(
    tree: SeTree, op: dict[str, Any], *, opname: str
) -> tuple[str, str]:
    """Resolve + vet the ``block``/``name`` pair every measure op takes:
    the block must exist and be ordinary (a measure, like a port, lives on
    the template — an instance's measures resolve through it); the measure
    name may not contain ``'.'`` (the ``'block.measure'`` relation-source
    syntax reserves it)."""
    block = _require_block(tree, op, "block", opname)
    node = tree.blocks[block]
    if node.template is not None:
        raise OpError(
            f"block {block!r} is an instance (of {node.template!r}) — "
            "measures live on the template (same rule as envelope/ports); "
            f"{opname} on {node.template!r} instead"
        )
    name = _require_name(op, "name", opname)
    if "." in name:
        raise OpError(
            f"{opname} 'name' must not contain '.': {name!r} — the "
            "relation-source syntax ('block.measure', split on the last "
            "dot) reserves it"
        )
    return block, name


def _vet_number(op: dict[str, Any], key: str, *, opname: str) -> float | None:
    if op.get(key) is None:
        return None
    try:
        return float(op[key])
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"{opname} {key!r} must be a number (the measure's unit), got {op[key]!r}"
        ) from exc


def _vet_vocab(
    op: dict[str, Any], key: str, vocab: tuple[str, ...], *, opname: str
) -> str | None:
    if op.get(key) is None:
        return None
    word = str(op[key]).strip().lower()
    if word not in vocab:
        raise OpError(
            f"{opname} {key!r} must be one of {' | '.join(vocab)}, got {op[key]!r}"
        )
    return word


def _vet_measure_fields(op: dict[str, Any], *, opname: str) -> dict[str, Any]:
    """The optional measure fields, vetted, keyed by :class:`MeasureSpec`
    field name — a key is absent from the result when absent from the op
    (presence-based, so ``set_measure`` can tell "unchanged" from a
    value). ``min``/``max`` (op keys) land as ``min_value``/``max_value``;
    band ordering and a declared point outside its own band are rejected
    here (write-time loud — a hand-edited stored row is stack-up's
    ``mismatch`` problem instead)."""
    out: dict[str, Any] = {}
    value = _vet_number(op, "value", opname=opname)
    if value is not None:
        out["value"] = value
    min_value = _vet_number(op, "min", opname=opname)
    if min_value is not None:
        out["min_value"] = min_value
    max_value = _vet_number(op, "max", opname=opname)
    if max_value is not None:
        out["max_value"] = max_value
    if min_value is not None and max_value is not None and min_value > max_value:
        raise OpError(
            f"{opname}: 'min' ({min_value:g}) exceeds 'max' ({max_value:g}) "
            "— an empty band declares nothing satisfiable"
        )
    origin = _vet_vocab(op, "origin", ORIGINS, opname=opname)
    if origin is not None:
        out["origin"] = origin
    unit = _vet_vocab(op, "unit", UNITS, opname=opname)
    if unit is not None:
        out["unit"] = unit
    relation: dict[str, Any] | None = None
    if op.get("relation") is not None:
        if not isinstance(op["relation"], dict):
            raise OpError(
                f"{opname} 'relation' must be a JSON object, got {op['relation']!r}"
            )
        try:
            relation = validate_relation(op["relation"])
        except MeasureError as exc:
            raise OpError(f"{opname}: {exc}") from exc
    if relation is not None:
        out["relation"] = relation
    strength = _vet_vocab(op, "strength", _STRENGTHS, opname=opname)
    if strength is not None:
        out["strength"] = strength
    reason = _opt_str(op.get("reason"))
    if reason is not None:
        out["reason"] = reason
    if op.get("datum") is not None:
        # Strict on shape at write (the selector grammar is vetted now);
        # lenient on existence — a selector naming a face that isn't
        # there yet is legal (precis_se.datums' resolve-time finding).
        if not isinstance(op["datum"], str) or not op["datum"].strip():
            raise OpError(
                f"{opname} 'datum' must be a selector string "
                f"(frame | port:<name> | face:<instance>.<tag> | …), "
                f"got {op['datum']!r}"
            )
        from precis_se.datums import parse_selector

        try:
            parse_selector(op["datum"].strip())
        except MeasureError as exc:
            raise OpError(f"{opname}: {exc}") from exc
        out["datum"] = op["datum"].strip()
    return out


def _check_band(
    value: float | None,
    min_value: float | None,
    max_value: float | None,
    *,
    opname: str,
) -> None:
    """A declared point must sit inside its own declared band (an open
    end is unbounded) — rejecting the contradiction at write time; a
    hand-edited stored row surfaces as stack-up's ``mismatch`` instead."""
    if value is None:
        return
    if min_value is not None and value < min_value:
        raise OpError(
            f"{opname}: 'value' ({value:g}) lies below the measure's own "
            f"'min' ({min_value:g}) — a chosen point must sit inside its "
            "declared band"
        )
    if max_value is not None and value > max_value:
        raise OpError(
            f"{opname}: 'value' ({value:g}) lies above the measure's own "
            f"'max' ({max_value:g}) — a chosen point must sit inside its "
            "declared band"
        )


def _find_measure(tree: SeTree, block: str, name: str) -> MeasureSpec | None:
    for m in tree.measures:
        if m.block == block and m.name == name:
            return m
    return None


def _op_add_measure(tree: SeTree, op: dict[str, Any]) -> None:
    """Mint a named measure on a block — ``value`` and/or a ``min``/``max``
    band and/or ``relation`` (``{'source': 'block.measure', 'scale': <×>,
    'offset', 'tol'}``), all optional (a measure may exist as a named
    handle first — suggestive by contract); plus ``unit`` (m | count |
    ratio | deg, default m) and ``origin`` (user | proposed, default
    user). A relation source that doesn't exist YET is accepted (a
    forward reference inside one ops batch is normal); an unresolvable
    relation is DRC's read-time finding."""
    block, name = _measure_shared(tree, op, opname="add_measure")
    if _find_measure(tree, block, name) is not None:
        raise OpError(
            f"duplicate measure on block {block!r}: {name!r} (measure "
            "names are unique per block; set_measure to change it)"
        )
    fields = _vet_measure_fields(op, opname="add_measure")
    _check_band(
        fields.get("value"),
        fields.get("min_value"),
        fields.get("max_value"),
        opname="add_measure",
    )
    tree.measures.append(MeasureSpec(block=block, name=name, **fields))


def _op_set_measure(tree: SeTree, op: dict[str, Any]) -> None:
    """Update an existing measure, presence-based: only the keys the op
    carries change. An explicit ``value=null``/``relation=null`` is
    rejected (a presence-based update can't clear a field, and silent
    inaction would misread as "cleared") — remove + re-add instead."""
    block, name = _measure_shared(tree, op, opname="set_measure")
    m = _find_measure(tree, block, name)
    if m is None:
        roster = (
            ", ".join(sorted(x.name for x in tree.measures if x.block == block))
            or "(none)"
        )
        raise OpError(
            f"no such measure on block {block!r}: {name!r}. "
            f"Measures on {block!r}: {roster}"
        )
    field_keys = (
        "value",
        "relation",
        "strength",
        "reason",
        "min",
        "max",
        "origin",
        "unit",
        "datum",
    )
    if not any(k in op for k in field_keys):
        raise OpError(
            "set_measure needs at least one of value/relation/strength/"
            "reason/min/max/origin/unit/datum"
        )
    # An explicit null must push back, not silently no-op (reviewer
    # finding): presence-based updates can't express "clear this field".
    nulled = [k for k in field_keys if k in op and op[k] is None]
    if nulled:
        raise OpError(
            f"set_measure cannot clear {', '.join(nulled)} with null — "
            "remove_measure + add_measure to drop a field"
        )
    fields = _vet_measure_fields(op, opname="set_measure")
    merged_value: float | None = fields.get("value", m.value)
    merged_min: float | None = fields.get("min_value", m.min_value)
    merged_max: float | None = fields.get("max_value", m.max_value)
    _check_band(merged_value, merged_min, merged_max, opname="set_measure")
    if merged_min is not None and merged_max is not None and merged_min > merged_max:
        raise OpError(
            "set_measure: the merged 'min' exceeds the merged 'max' — "
            "an empty band declares nothing satisfiable"
        )
    for key, val in fields.items():
        setattr(m, key, val)


def _op_remove_measure(tree: SeTree, op: dict[str, Any]) -> None:
    """Drop a measure. A surviving relation that pointed at it now
    dangles — DRC's unresolvable_relation reports it (read-time honesty,
    same posture as remove_block's measure note)."""
    block, name = _measure_shared(tree, op, opname="remove_measure")
    m = _find_measure(tree, block, name)
    if m is None:
        roster = (
            ", ".join(sorted(x.name for x in tree.measures if x.block == block))
            or "(none)"
        )
        raise OpError(
            f"no such measure on block {block!r}: {name!r}. "
            f"Measures on {block!r}: {roster}"
        )
    tree.measures.remove(m)


def _template_owned(tree: SeTree, name: str, *, opname: str, what: str) -> SeBlock:
    """Resolve ``name`` — a block token, label or uid — to an *ordinary*
    block, rejecting an instance/array node: realization facets (envelope,
    mode, binding) live on the template and resolve from it at read time,
    so setting one on an instance would be a silently ignored write."""
    name = _block_key(tree, name, what="block")
    node = tree.blocks[name]
    if node.template is not None:
        raise OpError(
            f"block {name!r} is an instance (of {node.template!r}) — the "
            f"{what} lives on the template; {opname} on {node.template!r} "
            "instead"
        )
    return node


def _op_set_mode(tree: SeTree, op: dict[str, Any]) -> None:
    """Assign (or clear, with ``mode=null``) a block's manufacturing mode
    — ``'purchase'``, ``'fdm/asa'``, ``'laser/acrylic'``, … An unknown
    family is rejected with the legal list; a known family whose
    implementer hasn't shipped is accepted, and reads back as *recorded
    intent* (se-kind.md's suggestive-by-contract posture, applied to L5:
    stating how you mean to make something is worth storing before the
    checker exists)."""
    name = _require_name(op, "block", "set_mode")
    node = _template_owned(tree, name, opname="set_mode", what="manufacturing mode")
    if "mode" not in op:
        raise OpError("set_mode needs 'mode' (a mode key, or null to clear)")
    raw = op.get("mode")
    if raw is None:
        node.mode = None
        return
    try:
        parse_mode(raw)
    except ModeError as exc:
        raise OpError(f"set_mode: {exc}") from exc
    node.mode = str(raw).strip()


def _op_set_binding(tree: SeTree, op: dict[str, Any]) -> None:
    """Bind a block's L3 realization to a design or catalog row:
    ``kind`` ∈ :data:`_BINDING_KINDS` + ``design`` (slug / C-number), or
    ``clear=true``. Slug-keyed text resolved at read time — binding a
    component that doesn't exist yet is a legal, honest state (and a DRC
    finding), never a write-time rejection: the design language must let
    you name what you intend to buy before it's in the store."""
    name = _require_name(op, "block", "set_binding")
    node = _template_owned(tree, name, opname="set_binding", what="realization binding")
    if op.get("clear"):
        if op.get("kind") is not None or op.get("design") is not None:
            raise OpError("set_binding: 'clear' and kind/design are mutually exclusive")
        node.bound_kind = None
        node.bound = None
        return
    kind = str(op.get("kind") or "").strip()
    if kind not in _BINDING_KINDS:
        known = " | ".join(_BINDING_KINDS)
        raise OpError(
            f"set_binding needs 'kind' ∈ {known} (or clear=true); got {kind!r}"
        )
    design = str(op.get("design") or "").strip()
    if not design:
        raise OpError(
            f"set_binding needs 'design' — the {kind} "
            f"{'C-number' if kind == 'part' else 'slug'} this block realizes as"
        )
    node.bound_kind = kind
    node.bound = design


def _op_set_process_override(tree: SeTree, op: dict[str, Any]) -> None:
    """Set (or replace) a per-block override of one
    :mod:`precis_se.capabilities` field — se-kind.md L5's "the model
    overrides if it wants" posture: the resolver's house-tier default may
    be tightened or loosened per block, never below the process's
    physical figure. ``block`` + ``field`` (req) + ``value`` (a number, in
    the field's own unit). Rejected at write, never stored malformed: a
    field the block's mode family doesn't define (or a block with no mode
    at all — nothing to check a field against), and a value beyond the
    field's physical floor/ceiling."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "set_process_override"),
        opname="set_process_override",
        what="process override",
    )
    field = _require_name(op, "field", "set_process_override")
    if op.get("value") is None:
        raise OpError("set_process_override needs 'value' (a number)")
    try:
        value = float(op["value"])
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"set_process_override {field!r}: 'value' must be a number, "
            f"got {op['value']!r}"
        ) from exc
    known = se_caps.known_fields(node.mode)
    if not known:
        raise OpError(
            f"set_process_override: block {node.name!r} has "
            f"{'no mode set' if not node.mode else f'mode {node.mode!r}, an unrecognized family'} "
            "— a process override needs a mode with capability fields to "
            "check against (set_mode first)"
        )
    if field not in known:
        accepted = ", ".join(sorted(known))
        raise OpError(
            f"set_process_override: {field!r} is not a field {node.mode!r}'s "
            f"family defines — accepted fields: {accepted}"
        )
    cap = se_caps.capability(node.mode, field)
    if cap is not None and cap.physical is not None:
        if field.startswith("max_") and value > cap.physical:
            raise OpError(
                f"set_process_override: {field} {value:g} exceeds the "
                f"physical ceiling {cap.physical:g} {cap.unit} — the process "
                "cannot beat it whatever the design declares"
            )
        if field.startswith("min_") and value < cap.physical:
            raise OpError(
                f"set_process_override: {field} {value:g} is below the "
                f"physical floor {cap.physical:g} {cap.unit} — the process "
                "cannot beat it whatever the design declares"
            )
    overrides = dict(node.process_overrides or {})
    overrides[field] = value
    node.process_overrides = overrides


def _op_clear_process_override(tree: SeTree, op: dict[str, Any]) -> None:
    """Remove one block's override of one capability field — the inverse
    of :func:`_op_set_process_override`. A field with no override on this
    block is a typo the ops layer names, the same posture
    ``remove_measure`` takes: naming what *is* overridden costs nothing
    and catches a caller's stale assumption."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "clear_process_override"),
        opname="clear_process_override",
        what="process override",
    )
    field = _require_name(op, "field", "clear_process_override")
    current = node.process_overrides or {}
    if field not in current:
        roster = ", ".join(sorted(current)) or "(none)"
        raise OpError(
            f"clear_process_override: block {node.name!r} has no override "
            f"for {field!r}. Overridden fields on {node.name!r}: {roster}"
        )
    overrides = dict(current)
    del overrides[field]
    node.process_overrides = overrides or None


def _op_set_build_frame(tree: SeTree, op: dict[str, Any]) -> None:
    """Pin a block's print build-down direction — se-print-implementer.md
    Engine 2's ``origin: user`` posture: ``view='print'`` searches every
    read regardless, but a pinned block reports how much worse the pin
    scores than the best candidate rather than silently switching to it.
    ``block`` + ``down`` (a non-zero 3-vector, any scale — normalized on
    write so a re-read never has to). Pure — no store, no mesh, no score:
    the pin is a direction, not a verdict; the verdict is computed fresh
    by the view every time, since the block's solid can change underneath
    a standing pin (which survives that on purpose, se-kind.md's origin
    contract)."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "set_build_frame"),
        opname="set_build_frame",
        what="build frame",
    )
    down = op.get("down")
    if down is None:
        raise OpError("set_build_frame needs 'down' (a non-zero 3-vector [x, y, z])")
    try:
        vec = [float(x) for x in down]
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"set_build_frame: 'down' must be a 3-vector [x, y, z], got {down!r}"
        ) from exc
    if len(vec) != 3:
        raise OpError(
            f"set_build_frame: 'down' must be a 3-vector [x, y, z], got {down!r}"
        )
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        raise OpError("set_build_frame: 'down' must be non-zero")
    node.build_frame = {"down": [v / norm for v in vec], "origin": "user"}


def _op_clear_build_frame(tree: SeTree, op: dict[str, Any]) -> None:
    """Remove a block's pinned build frame — the inverse of
    :func:`_op_set_build_frame`. ``view='print'`` resumes proposing the
    best-scoring candidate on the very next read; nothing else changes."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "clear_build_frame"),
        opname="clear_build_frame",
        what="build frame",
    )
    if node.build_frame is None:
        raise OpError(
            f"clear_build_frame: block {node.name!r} has no pinned build frame"
        )
    node.build_frame = None


def _bom_pair(line: BomLine) -> frozenset[tuple[str, str]]:
    """A connect-targeted line's endpoint pair, for identity comparison."""
    assert line.a_block is not None and line.a_port is not None
    assert line.b_block is not None and line.b_port is not None
    return _connects_endpoint_pair(line.a_block, line.a_port, line.b_block, line.b_port)


def _same_bom_target(a: BomLine, b: BomLine) -> bool:
    if a.is_connect != b.is_connect:
        return False
    if not a.is_connect:
        return a.block == b.block
    return _bom_pair(a) == _bom_pair(b)


def _bom_target(tree: SeTree, op: dict[str, Any], *, opname: str) -> BomLine:
    """Resolve the ``block=`` / ``a=``+``b=`` half of a BOM op into a
    target-only line (the item half is the caller's). Both forms must name
    something live — a BOM line against a block that isn't there is a typo,
    and the ops layer is where a typo still costs nothing."""
    has_block = op.get("block") is not None
    has_edge = op.get("a") is not None or op.get("b") is not None
    if has_block == has_edge:
        raise OpError(
            f"{opname} targets exactly one of a block (block=) or a connect "
            "(a= and b=, each 'block.port')"
        )
    if has_block:
        name = _require_block(tree, op, "block", opname)
        return BomLine(item_kind="component", item="", block=name)
    c = _find_connect(tree, op, opname=opname)
    return BomLine(
        item_kind="component",
        item="",
        a_block=c.a_block,
        a_port=c.a_port,
        b_block=c.b_block,
        b_port=c.b_port,
    )


def _op_add_bom(tree: SeTree, op: dict[str, Any]) -> None:
    """Hang a bought ``component``/``part`` off a block or a connect, with
    a **per-occurrence** quantity — the tree's arrays multiply it
    (:mod:`precis_se.bom`). Re-adding the same item to the same target
    replaces that line: the quantity is a statement about the target, and
    two lines disagreeing about it is exactly the ambiguity a BOM must not
    have."""
    target = _bom_target(tree, op, opname="add_bom")
    try:
        kind, item, qty, uom, why = vet_bom_fields(
            item_kind=op.get("item_kind"),
            item=op.get("item"),
            qty=op.get("qty"),
            uom=op.get("uom"),
            reason=op.get("reason"),
            opname="add_bom",
        )
    except BomError as exc:
        raise OpError(str(exc)) from exc
    target.item_kind = kind
    target.item = item
    target.qty = qty
    target.uom = uom
    target.reason = why
    for i, existing in enumerate(tree.bom):
        if (
            _same_bom_target(existing, target)
            and existing.item_kind == kind
            and existing.item == item
        ):
            tree.bom[i] = target
            return
    tree.bom.append(target)


def _op_remove_bom(tree: SeTree, op: dict[str, Any]) -> None:
    """Drop one BOM line, addressed by its target + item."""
    target = _bom_target(tree, op, opname="remove_bom")
    try:
        kind, item, _qty, _uom, _why = vet_bom_fields(
            item_kind=op.get("item_kind"),
            item=op.get("item"),
            qty=None,
            opname="remove_bom",
        )
    except BomError as exc:
        raise OpError(str(exc)) from exc
    for i, existing in enumerate(tree.bom):
        if (
            _same_bom_target(existing, target)
            and existing.item_kind == kind
            and existing.item == item
        ):
            del tree.bom[i]
            return
    live = (
        ", ".join(
            f"{line.item_kind}:{line.item}"
            for line in tree.bom
            if _same_bom_target(line, target)
        )
        or "(none)"
    )
    raise OpError(
        f"remove_bom: no {kind} {item!r} on {target.target!r}. Live items there: {live}"
    )


def _op_formfind(tree: SeTree, op: dict[str, Any]) -> None:
    """Force-density form-finding over the axial subgraph — solve for
    the equilibrium geometry and write the solved poses back stamped
    ``origin: 'proposed'`` (:mod:`precis_se.formfind`, renting
    :func:`precis.structsolve.form_find`)."""
    # Function-level import: the op registry lives here, and the bridge
    # imports stability, which imports this module.
    from precis_se import formfind as se_formfind

    se_formfind.op_formfind(tree, op)


def _find_note(tree: SeTree, name: str) -> NoteSpec | None:
    for n in tree.notes:
        if n.name == name:
            return n
    return None


def _op_add_note(tree: SeTree, op: dict[str, Any]) -> None:
    """Append to the interrogation ledger (:mod:`precis_se.notes`) —
    ``name`` (unique), ``kind`` (question | answer | decision), ``text``
    (the body), optional ``re`` (the note this answers/decides — must
    already exist; earlier ops in the same batch count), ``about``
    (anchor names, 'block' or 'block.measure' — dangling is legal, the
    interview view annotates it), ``origin`` (user | proposed)."""
    name = _require_name(op, "name", "add_note")
    if _find_note(tree, name) is not None:
        raise OpError(
            f"duplicate note {name!r} (note names are unique per design; "
            "the ledger is append-shaped — add a NEW note to amend, or "
            "remove_note to retract)"
        )
    kind = str(op.get("kind") or "").strip().lower()
    if kind not in NOTE_KINDS:
        raise OpError(
            f"add_note 'kind' must be one of {' | '.join(NOTE_KINDS)}, "
            f"got {op.get('kind')!r}"
        )
    body = _opt_str(op.get("text"))
    if not body:
        raise OpError("add_note needs 'text' (the note body)")
    re_name = _opt_str(op.get("re"))
    if re_name is not None:
        if kind == "question":
            raise OpError(
                "add_note: a question takes no 're' — only answers/"
                "decisions respond to another note"
            )
        target = _find_note(tree, re_name)
        if target is None:
            roster = ", ".join(sorted(n.name for n in tree.notes)) or "(none)"
            raise OpError(
                f"add_note 're' names no live note: {re_name!r}. Notes: {roster}"
            )
        if target.kind != "question":
            raise OpError(
                f"add_note 're' must name a question, but {re_name!r} is "
                f"a {target.kind} — chain answers to the question itself, "
                "not to each other"
            )
    origin = str(op.get("origin") or "user").strip().lower()
    if origin not in ORIGINS:
        raise OpError(
            f"add_note 'origin' must be one of {' | '.join(ORIGINS)}, "
            f"got {op.get('origin')!r}"
        )
    try:
        about = validate_about(op.get("about"))
    except NoteError as exc:
        raise OpError(f"add_note: {exc}") from exc
    tree.notes.append(
        NoteSpec(
            name=name,
            kind=kind,
            body=body,
            re=re_name,
            about=about,
            origin=origin,
        )
    )


def _op_remove_note(tree: SeTree, op: dict[str, Any]) -> None:
    """Retract a note. An answer/decision whose ``re`` named it now
    dangles — kept, and the interview view reports the orphan (read-time
    honesty, the remove_measure posture)."""
    name = _require_name(op, "name", "remove_note")
    n = _find_note(tree, name)
    if n is None:
        roster = ", ".join(sorted(x.name for x in tree.notes)) or "(none)"
        raise OpError(f"no such note: {name!r}. Notes: {roster}")
    tree.notes.remove(n)


# ── atomic mode (docs/backlog/nm-se-merge.md) ───────────────────────────
# The L2 vocabulary only a chemistry-realized block states. Structurally
# identical to the ops above — vet, then mutate the in-memory tree — with
# the vetting itself in :mod:`precis_se.atomic.vocab`.


def _op_declare_threading(tree: SeTree, op: dict[str, Any]) -> None:
    """Declare that block ``a`` is threaded through block ``b`` (a
    macrocycle on an axle) — the L2 mechanical-interlocking fact, stored,
    never re-derived from coordinates."""
    a = _require_block(tree, op, "a", "declare_threading", what="a")
    b = _require_block(tree, op, "b", "declare_threading", what="b")
    if a == b:
        raise OpError(f"declare_threading: 'a' and 'b' must differ, got {a!r} twice")
    for t in tree.threading:
        if t.a == a and t.b == b:
            raise OpError(
                f"declare_threading: {a!r} is already declared threaded through {b!r}"
            )
        # Mutual threading is physically impossible: a threaded through b
        # and b threaded through a at once would mean each is inside the
        # other. Reject the opposite-direction row too, naming
        # remove_threading as the fix for a wrong-direction declaration.
        if t.a == b and t.b == a:
            raise OpError(
                f"declare_threading: {b!r} is already threaded through {a!r} "
                "— mutual threading is physically impossible; "
                "remove_threading first if the direction was wrong"
            )
    tree.threading.append(ThreadingSpec(a=a, b=b))


def _op_remove_threading(tree: SeTree, op: dict[str, Any]) -> None:
    # Lenient: threading may legitimately dangle (its block was removed by
    # hand-corrupted data — DRC's ``dangling_threading``), so a token that
    # resolves to nothing still gets to match a stored row by its text.
    a = _block_key_or_raw(tree, _require_name(op, "a", "remove_threading"))
    b = _block_key_or_raw(tree, _require_name(op, "b", "remove_threading"))
    for i, t in enumerate(tree.threading):
        if t.a == a and t.b == b:
            del tree.threading[i]
            return
    live = ", ".join(f"{t.a}→{t.b}" for t in tree.threading) or "(none)"
    raise OpError(f"no such threading {a!r} through {b!r}. Live threading: {live}")


def _op_declare_dof(tree: SeTree, op: dict[str, Any]) -> None:
    """Declare a block's degree of freedom — ``kind`` (rotational |
    translational) about the axis through its own two ``axis_ports``."""
    block = _require_name(op, "block", "declare_dof")
    node = _template_owned(tree, block, opname="declare_dof", what="dof")
    # Every op-dict key besides 'op'/'block' is dof payload — declare_dof's
    # own kind=/axis_ports= live as direct op fields (unlike add_block's
    # nested dof={...}), so this is the equivalent dict to hand
    # vet_dof_shape. Anything beyond kind/axis_ports is a loud reject
    # (gripe 334765's reported states=/driver=), never a silent drop.
    payload = {k: v for k, v in op.items() if k not in ("op", "block")}
    dof = vet_dof_shape(payload, what="declare_dof")
    check_dof_axis_ports(node, dof, node.name, what="declare_dof")
    node.dof = dof


def _op_clear_dof(tree: SeTree, op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "clear_dof")
    node = _template_owned(tree, block, opname="clear_dof", what="dof")
    node.dof = None


def effective_chromophore(tree: SeTree, node: SeBlock) -> dict[str, Any] | None:
    """The chromophore card "seen" at ``node`` — its own, or, when ``node``
    is an instance/array, its template's.

    Mirrors :func:`effective_dof` and :func:`effective_envelope` for the
    same reason: a dye attached to a template block is genuinely present on
    every instance of it, and the dipole is stored in the **block frame**,
    so each instance's own pose rotates the same card into a different
    world-space orientation. That is what makes an array of emitters a
    useful thing to state once — the geometry varies, the chemistry does
    not.
    """
    if node.template is not None:
        template_node = resolve_template(tree, node.template)
        return getattr(template_node, "chromophore", None)
    return node.chromophore


def _op_set_chromophore(tree: SeTree, op: dict[str, Any]) -> None:
    """Attach (or replace) a block's optical property card — the op that
    makes a block a FRET node.

    ``block=`` plus ``label``, ``dipole`` ([x, y, z] in the **block**
    frame), ``quantum_yield``, ``lifetime_s``, ``emission`` and
    ``absorption`` (each ``[[wavelength_nm, value], ...]``; emission in
    arbitrary units, absorption in M⁻¹cm⁻¹). ``clear=true`` removes the
    card instead.

    Replace semantics, and every field required: the card states a whole
    chromophore or none at all. A partial card would still compute — it
    would just compute a rate from a spectrum that isn't there — and a
    plausible wrong number is worse here than a refusal.
    """
    name = _require_name(op, "block", "set_chromophore")
    node = _template_owned(tree, name, opname="set_chromophore", what="chromophore")
    if op.get("clear"):
        node.chromophore = None
        return
    payload = {k: v for k, v in op.items() if k not in ("op", "block", "clear")}
    try:
        node.chromophore = validate_chromophore(payload)
    except FretError as exc:
        raise OpError(f"set_chromophore: {exc}") from exc


def _op_set_optical_link(tree: SeTree, op: dict[str, Any]) -> None:
    """Declare (or clear) an existing connect's required transfer
    efficiency — the L2 invariant of the optical domain.

    ``a=``/``b=`` address the connect the way ``set_joint`` does;
    ``min_efficiency`` (strictly between 0 and 1) is the requirement, with
    optional ``channel`` and ``reason``. ``min_efficiency=null`` clears it.

    Declared, never derived — the number states what the design *needs*,
    and the ``fret`` view is what checks the realised geometry against it.
    Both endpoints must already carry a chromophore card: a required
    efficiency between blocks with no optics is not an unmet requirement,
    it is a typo, and catching it at write time beats surfacing it as a
    view finding much later.
    """
    c = _find_connect(tree, op, opname="set_optical_link")
    if "min_efficiency" not in op:
        raise OpError(
            "set_optical_link needs 'min_efficiency' (a fraction in (0, 1), "
            "or null to clear)"
        )
    if op.get("min_efficiency") is None:
        c.optical = None
        return
    for side, block_name in (("a", c.a_block), ("b", c.b_block)):
        node = tree.blocks.get(block_name)
        if node is None or effective_chromophore(tree, node) is None:
            raise OpError(
                f"set_optical_link: endpoint {side}={block_name!r} has no "
                "chromophore — set_chromophore on both endpoints first; a "
                "transfer requirement between blocks with no optics can "
                "never be evaluated"
            )
    payload = {k: v for k, v in op.items() if k not in ("op", "a", "b")}
    try:
        c.optical = validate_optical_link(payload)
    except FretError as exc:
        raise OpError(f"set_optical_link: {exc}") from exc


def _op_set_optics(tree: SeTree, op: dict[str, Any]) -> None:
    """Declare the design's optical context — ``medium_index`` (required)
    and ``excitation_nm`` (optional pump wavelength). ``clear=true``
    removes it.

    Design-level, because both are facts about the space rather than about
    a block. Undeclared is a legitimate state: the ``fret`` view then
    assumes :data:`~precis_se.fret.DEFAULT_MEDIUM_INDEX` and **says so** on
    every number it quotes, rather than letting an assumed medium read as a
    measured one.
    """
    if op.get("clear"):
        tree.optics = None
        return
    payload = {k: v for k, v in op.items() if k not in ("op", "clear")}
    try:
        tree.optics = validate_optics(payload)
    except FretError as exc:
        raise OpError(f"set_optics: {exc}") from exc


# ── blocktree slice 2 — discrete states + transitions ───────────────────
# (docs/backlog/blocktree-library-build-plan.md §Slice 2). Both ops below
# are store-free like everything else in this module — they vet and stash
# the payload on the node; :func:`precis_se.handler._materialize_states`
# does the actual write, after ``persist.save_tree`` has minted a uid for
# every block (module docstring, and :mod:`precis.design.states`).


#: The keys one port's entry in a state's ``port_pose_overrides`` may
#: carry (:func:`_vet_port_pose_overrides`) — closed, so a typo ('xyz')
#: is a loud write-time rejection rather than an override that silently
#: does nothing at read time.
_PORT_OVERRIDE_KEYS = frozenset({"direction", "pose", "rot"})


def _vet_port_pose_overrides(raw: Any, *, state_name: str) -> dict[str, Any] | None:
    """Vet a declared state's ``port_pose_overrides`` — ``{port_name:
    {'direction'?: [x,y,z], 'pose'?: [dx,dy,dz], 'rot'?: [rx,ry,rz]}}``,
    keyed by the block's own port names (migration
    ``0162_design_core.sql``'s column comment: "the ports a state moves"),
    at least one key and no others.

    ``direction`` is unit-normalized here the same way ``add_port``'s own
    is, so the get-time poser (:func:`precis_se.handler._apply_state_arg`)
    can trust every stored vector without re-checking it. ``pose``/``rot``
    are a rigid **DELTA** in the block frame, not an absolute placement:
    the translation adds to the port's own origin, the rotation composes
    on top of the port's own. Delta because the requirement is phrased
    that way ("in the bonded state the far port moves 9 Å along x") and
    because it stays meaningful for a port whose own
    :attr:`~precis.blocktree.types.Port.pose` is null — an absolute value
    there would silently invent an origin the design never declared.

    The named port may not exist yet — a forward reference, the same
    tolerance ``add_measure``'s relation source gets: only the SHAPE is
    vetted here, never port existence (a dangling one is a read-time
    honesty finding for a later round, not a write-time rejection)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpError(
            f"declare_states: state {state_name!r} 'port_pose_overrides' "
            f"must be a JSON object keyed by port name, got {raw!r}"
        )
    out: dict[str, Any] = {}
    for port_name, override in raw.items():
        keys = set(override) if isinstance(override, dict) else set()
        if not isinstance(override, dict) or not keys or keys - _PORT_OVERRIDE_KEYS:
            raise OpError(
                f"declare_states: state {state_name!r} "
                f"port_pose_overrides[{port_name!r}] takes at least one of "
                f"'direction' [x,y,z], 'pose' [dx,dy,dz], 'rot' [rx,ry,rz] "
                f"(pose/rot are a rigid delta in the block frame) and "
                f"nothing else, got {override!r}"
            )
        what = f"declare_states: state {state_name!r} port {port_name!r}"
        entry: dict[str, Any] = {}
        if "direction" in keys:
            d_what = f"{what} direction"
            entry["direction"] = _unit_vec(
                _as_vec3(override["direction"], d_what), what=d_what
            )
        if "pose" in keys:
            entry["pose"] = _as_vec3(override["pose"], f"{what} pose")
        if "rot" in keys:
            entry["rot"] = _as_vec3(override["rot"], f"{what} rot")
        out[str(port_name)] = entry
    return out


def _op_declare_states(tree: SeTree, op: dict[str, Any]) -> None:
    """Replace a block's declared states — ``states=[{'name',
    'envelope'?, 'port_pose_overrides'?, 'descr'?}, ...]`` (``[]`` clears
    them). Ordinary blocks only (:func:`_template_owned`) — an instance
    resolves its facets from its template, the same rule as envelope/mode/
    dof, and a state declared on the template genuinely applies to every
    instance of it once a later round adds instance-side resolution."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "declare_states"),
        opname="declare_states",
        what="states",
    )
    raw = op.get("states")
    if not isinstance(raw, list):
        raise OpError(
            "declare_states needs 'states' — a list of state objects ([] clears them)"
        )
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise OpError(
                f"declare_states: every state must be a JSON object, got {entry!r}"
            )
        state_name = str(entry.get("name") or "").strip()
        if not state_name:
            raise OpError("declare_states: every state needs a non-empty 'name'")
        if state_name in seen:
            raise OpError(f"declare_states: state name {state_name!r} declared twice")
        seen.add(state_name)
        envelope = entry.get("envelope")
        if envelope is not None:
            envelope = str(envelope).strip()
            _validate_envelope(envelope)
        overrides = _vet_port_pose_overrides(
            entry.get("port_pose_overrides"), state_name=state_name
        )
        parsed.append(
            {
                "name": state_name,
                "envelope": envelope,
                "port_pose_overrides": overrides,
                "descr": _opt_str(entry.get("descr")),
            }
        )
    node.pending_states = parsed


def _op_declare_transitions(tree: SeTree, op: dict[str, Any]) -> None:
    """Replace a block's transitions — ``transitions=[{'from_state',
    'to_state', 'driver_kind', 'driver_ref'?, 'params'?}, ...]`` (``[]``
    clears them). Directed: a ratchet's forward and reverse edges are two
    separate entries here, never collapsed into one unordered pair —
    declare both when both exist. ``driver_kind`` is the closed enum
    (:func:`~precis.design.states.validate_driver_kind`); a self-edge
    (``from_state == to_state``) is rejected outright, the same as the
    shared table's own CHECK constraint."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "declare_transitions"),
        opname="declare_transitions",
        what="transitions",
    )
    raw = op.get("transitions")
    if not isinstance(raw, list):
        raise OpError(
            "declare_transitions needs 'transitions' — a list of transition "
            "objects ([] clears them)"
        )
    parsed: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise OpError(
                "declare_transitions: every transition must be a JSON "
                f"object, got {entry!r}"
            )
        from_state = str(entry.get("from_state") or "").strip()
        to_state = str(entry.get("to_state") or "").strip()
        if not from_state or not to_state:
            raise OpError(
                "declare_transitions: every transition needs 'from_state' "
                "and 'to_state'"
            )
        if from_state == to_state:
            raise OpError(
                f"declare_transitions: transition from {from_state!r} to "
                "itself — a self-edge drives nothing"
            )
        try:
            driver_kind = validate_driver_kind(entry.get("driver_kind"))
        except StateError as exc:
            raise OpError(f"declare_transitions: {exc}") from exc
        params = entry.get("params")
        if params is not None and not isinstance(params, dict):
            raise OpError(
                f"declare_transitions: transition {from_state!r} -> "
                f"{to_state!r} 'params' must be a JSON object, got {params!r}"
            )
        parsed.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "driver_kind": driver_kind,
                "driver_ref": _opt_str(entry.get("driver_ref")),
                "params": params or {},
            }
        )
    node.pending_transitions = parsed


def _op_set_current_state(tree: SeTree, op: dict[str, Any]) -> None:
    """Persistently pose an ordinary block into one of its declared states
    — ``block=`` + ``state=`` (a state name). The PERSISTENT counterpart to
    get's TRANSIENT ``args={'state': ...}`` override
    (:func:`precis_se.handler._apply_state_arg`): this op changes what the
    block's CURRENT state records; the get-time override never does.

    Store-free like ``declare_states``/``declare_transitions`` — stashes
    the request on the node (``node.pending_current_state``);
    :func:`precis_se.handler._materialize_states` validates the name
    against the block's declared states (its own, or the ones this same
    call just declared) and writes it via
    :func:`precis.design.states.set_current_state`, after ``save_tree`` has
    minted a uid for every block. Ordinary blocks only — the same
    realization-facet rule ``declare_states`` itself follows."""
    node = _template_owned(
        tree,
        _require_name(op, "block", "set_current_state"),
        opname="set_current_state",
        what="current state",
    )
    node.pending_current_state = _require_name(op, "state", "set_current_state")


_OPS = {
    **blocktree.CORE_OPS,
    "add_block": _op_add_block,
    "add_port": _op_add_port,
    "remove_port": _op_remove_port,
    "set_pose": _op_set_pose,
    "set_desc": _op_set_desc,
    "instance_block": _op_instance_block,
    "array_block": _op_array_block,
    "set_envelope": _op_set_envelope,
    "remove_block": _op_remove_block,
    "connect": _op_connect,
    "disconnect": _op_disconnect,
    "set_joint": _op_set_joint,
    "set_load": _op_set_load,
    "add_measure": _op_add_measure,
    "set_measure": _op_set_measure,
    "remove_measure": _op_remove_measure,
    "set_mode": _op_set_mode,
    "set_binding": _op_set_binding,
    "set_process_override": _op_set_process_override,
    "clear_process_override": _op_clear_process_override,
    "set_build_frame": _op_set_build_frame,
    "clear_build_frame": _op_clear_build_frame,
    "add_bom": _op_add_bom,
    "remove_bom": _op_remove_bom,
    "add_note": _op_add_note,
    "remove_note": _op_remove_note,
    "formfind": _op_formfind,
    "declare_threading": _op_declare_threading,
    "remove_threading": _op_remove_threading,
    "declare_dof": _op_declare_dof,
    "clear_dof": _op_clear_dof,
    "set_chromophore": _op_set_chromophore,
    "set_optical_link": _op_set_optical_link,
    "set_optics": _op_set_optics,
    "declare_states": _op_declare_states,
    "declare_transitions": _op_declare_transitions,
    "set_current_state": _op_set_current_state,
}


def known_ops() -> frozenset[str]:
    """Every op name :func:`apply_ops` recognizes on its own
    (:data:`_OPS`'s keys) — the single source the handler's unknown-op
    error reads for its roster, so the message and the dispatch can't
    drift apart. Does NOT include the store-aware ops the handler
    intercepts before ``apply_ops`` ever sees them."""
    return frozenset(_OPS)
