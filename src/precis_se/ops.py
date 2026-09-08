"""Pure ops over an in-memory se block tree — no store access.

Built on the shared block-tree spine (:mod:`precis.blocktree`, extracted
from an earlier copy of this module — docs/backlog/
nm-se-shared-blocktree-core.md, phase 1): the core owns the recursive tree
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
  ``'block.port'`` syntax reserves it. Unmodified core op.
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance/array
  of this block. Unmodified core op.
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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.blocktree import ops as blocktree
from precis.blocktree.types import BlockNode, Connect, OpError, Port, Tree
from precis_se import joints as se_joints
from precis_se.bom import BomError, BomLine, vet_bom_fields
from precis_se.measures import (
    ORIGINS,
    UNITS,
    MeasureError,
    MeasureSpec,
    validate_relation,
)
from precis_se.modes import ModeError, parse_mode
from precis_se.notes import NOTE_KINDS, NoteError, NoteSpec, validate_about

#: What an L3 realization binding may point at — the two *designed*
#: realizations (a cad node set, an nm design) and the two *bought* ones
#: (an engineering-store component, a catalog part). Mirrors migration
#: 0003's ``se_blocks_bound_kind_check``.
_BINDING_KINDS: tuple[str, ...] = ("cad", "nm", "component", "part")

#: Re-exported from the core — ``PortSpec`` has no se-specific fields
#: (``annotations`` is already the open dict on the shared :class:`Port`),
#: so this is a plain alias, not a subclass.
PortSpec = Port

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
    at write time; stored strays are DRC findings."""

    joint: dict[str, Any] | None = None


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
    template (:func:`effective_ports`). Pose is metres; rot degrees. The
    fields below this line are se's own extension over the shared
    ``Block``."""

    array: dict[str, Any] | None = None
    #: loads on the block — the registered objectives vocabulary
    #: (:func:`precis_se.joints.validate_objectives`), real units.
    objectives: dict[str, Any] = field(default_factory=dict)
    #: L5 manufacturing mode (:mod:`precis_se.modes`) — ``None`` is
    #: honest: unassigned, not "assume it's printed".
    mode: str | None = None
    #: L3 realization binding: ``('cad'|'nm'|'component'|'part', slug)``,
    #: both ``None`` when the block's solid is still just its envelope.
    bound_kind: str | None = None
    bound: str | None = None
    #: Catalog-**derived** envelope/ports for a `component` binding
    #: (:mod:`precis_se.catalog`), filled at load time by
    #: :func:`precis_se.persist.load_tree`. DERIVED, never stored: it is
    #: recomputed from the component's spec rows on every read, the same
    #: sketch-canonical / copper-derived rule the rest of the tree
    #: follows, so ``save_tree`` must never write it back. A block that
    #: authors its own envelope always wins over this.
    derived: Any = None
    #: ``user | proposed`` stamps for authored facets, keyed by facet name
    #: (``'envelope'``, ``'pose'``) — slice 4's freedom vocabulary. An
    #: absent key means ``user`` (the default is never stored); a propose
    #: job stamps ``proposed`` on its own choices and treats user facets
    #: as contract.
    origins: dict[str, str] = field(default_factory=dict)


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

    def make_block(self, **kwargs: Any) -> SeBlock:
        return SeBlock(**kwargs)


def apply_ops(tree: SeTree, ops: list[dict[str, Any]]) -> SeTree:
    """Apply a list of typed ops to ``tree`` in order, mutating it —
    dispatches through :data:`_OPS` (the core's 8 shared ops plus se's own
    13), via the core's generic :func:`~precis.blocktree.ops.apply_ops`."""
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
    losing the rest."""
    if node.template is not None:
        return blocktree.effective_ports(tree, node)
    derived = getattr(node.derived, "ports", None)
    if derived:
        merged = dict(derived)
        merged.update(node.ports)
        return merged
    return node.ports


def _resolve_connect_port(
    tree: SeTree, block_name: str, port_name: str, *, what: str
) -> PortSpec:
    """se's own :func:`effective_ports` (the catalog-aware one) plugged
    into the core's endpoint resolver."""
    return blocktree._resolve_connect_port(
        tree, block_name, port_name, what=what, ports_fn=effective_ports
    )


# ── op implementations ───────────────────────────────────────────────────


def _op_instance_block(tree: SeTree, op: dict[str, Any]) -> None:
    for key in ("linear", "polar"):
        if op.get(key) is not None:
            raise OpError(
                f"instance_block does not take {key!r} — use array_block "
                "for a patterned instance"
            )
    blocktree.op_instance_block(tree, op)


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
    spec = _parse_array_spec(op)
    _commit_instance(
        tree, op, name=name, template=template, parent=parent, extra={"array": spec}
    )


def _op_set_envelope(tree: SeTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "set_envelope")
    node = tree.blocks.get(name)
    if node is None:
        raise OpError(_no_block_msg(tree, name, what="block"))
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
    node = tree.blocks[str(op["block"]).strip()]
    _stamp_origin(node, op, facet="pose", opname="set_pose")


def _op_remove_block(tree: SeTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "remove_block")
    # Computed before delegating to the core op (which raises if ``name``
    # doesn't exist or is used as a template — atomically, before any
    # mutation) so the measures/BOM cascade below acts on exactly the
    # subtree the core just removed.
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


def _op_connect(tree: SeTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("connect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _split_endpoint(a_raw, "connect 'a'")
    b_block, b_port = _split_endpoint(b_raw, "connect 'b'")
    if (a_block, a_port) == (b_block, b_port):
        raise OpError(f"connect: cannot connect {a_raw!r} to itself")
    joint = _vet_joint(op.get("joint"), opname="connect")
    objectives = _vet_objectives(op.get("objectives"), opname="connect")
    _resolve_connect_port(tree, a_block, a_port, what="connect")
    _resolve_connect_port(tree, b_block, b_port, what="connect")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for c in tree.connects:
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            raise OpError(
                f"connect: {a_block}.{a_port}—{b_block}.{b_port} already exists"
            )
    tree.connects.append(
        ConnectSpec(
            a_block=a_block,
            a_port=a_port,
            b_block=b_block,
            b_port=b_port,
            joint=joint,
            objectives=objectives or {},
        )
    )


def _op_disconnect(tree: SeTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("disconnect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _split_endpoint(a_raw, "disconnect 'a'")
    b_block, b_port = _split_endpoint(b_raw, "disconnect 'b'")
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


def _vet_objectives(raw: Any, *, opname: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpError(f"{opname} 'objectives' must be a JSON object, got {raw!r}")
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
    a_block, a_port = _split_endpoint(a_raw, f"{opname} 'a'")
    b_block, b_port = _split_endpoint(b_raw, f"{opname} 'b'")
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
    c.joint = _vet_joint(op.get("joint"), opname="set_joint")


def _op_set_load(tree: SeTree, op: dict[str, Any]) -> None:
    """Set/replace the loads (objective vectors, real units) on a block
    (``block=``) or an existing connect (``a=``/``b=``). Replace
    semantics — the op states the whole load picture for its target;
    ``clear=true`` removes it."""
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
    if op.get("clear"):
        if given:
            raise OpError("set_load: 'clear' and load keys are mutually exclusive")
        objectives: dict[str, Any] = {}
    else:
        if not given:
            known = ", ".join(sorted(se_joints.OBJECTIVE_KEYS))
            raise OpError(
                f"set_load needs at least one of {known} (or clear=true "
                "to remove loads)"
            )
        vetted = _vet_objectives(given, opname="set_load")
        objectives = vetted or {}
    if has_block:
        name = _require_name(op, "block", "set_load")
        node = tree.blocks.get(name)
        if node is None:
            raise OpError(_no_block_msg(tree, name, what="block"))
        node.objectives = objectives
    else:
        c = _find_connect(tree, op, opname="set_load")
        c.objectives = objectives


_STRENGTHS = ("hard", "soft", "gauge")


def _measure_shared(
    tree: SeTree, op: dict[str, Any], *, opname: str
) -> tuple[str, str]:
    """Resolve + vet the ``block``/``name`` pair every measure op takes:
    the block must exist and be ordinary (a measure, like a port, lives on
    the template — an instance's measures resolve through it); the measure
    name may not contain ``'.'`` (the ``'block.measure'`` relation-source
    syntax reserves it)."""
    block = _require_name(op, "block", opname)
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
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
    )
    if not any(k in op for k in field_keys):
        raise OpError(
            "set_measure needs at least one of value/relation/strength/"
            "reason/min/max/origin/unit"
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
    """Resolve ``name`` to an *ordinary* block, rejecting an instance/array
    node — realization facets (envelope, mode, binding) live on the
    template and resolve from it at read time, so setting one on an
    instance would be a silently ignored write."""
    node = tree.blocks.get(name)
    if node is None:
        raise OpError(_no_block_msg(tree, name, what="block"))
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
    ``kind`` ∈ ``cad|nm|component|part`` + ``design`` (slug / C-number),
    or ``clear=true``. Slug-keyed text resolved at read time — binding a
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
        name = _require_name(op, "block", opname)
        if name not in tree.blocks:
            raise OpError(_no_block_msg(tree, name, what="block"))
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


_OPS = {
    **blocktree.CORE_OPS,
    "set_pose": _op_set_pose,
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
    "add_bom": _op_add_bom,
    "remove_bom": _op_remove_bom,
    "add_note": _op_add_note,
    "remove_note": _op_remove_note,
}
