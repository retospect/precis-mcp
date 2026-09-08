"""Pure ops over an in-memory nm block tree — no store access.

Built on the shared block-tree spine (:mod:`precis.blocktree`, extracted
from an earlier copy of this module — docs/backlog/
nm-se-shared-blocktree-core.md, phase 1): the core owns the recursive tree
(``parent``/``template``), instancing with cycle guards, ports, connects,
and envelope validation over the ``precis.cad`` SDF kernel; this module adds
nm's own invariants on top — units are **Ångström** (float64, nm-kind.md
"Decisions"), chemistry-flavoured port expectations
(``expected_element``/``expected_hybridization``, kept as their own typed
fields rather than folded into the core's open ``annotations`` dict — see
that backlog doc's "What this is NOT" — this is a deliberately deferred
generalisation, not an oversight), a per-connect ``kind``
(``'bond'``/``'interaction'``) with a capability gate, and the L2 mechanical
vocabulary (declared threading, declared DOF) the core knows nothing about.

**Identity is the block ``name``, not a row id** — same rule as
``structure``'s atom labels. ``precis_nm.persist`` loads a design's live
rows into a fresh :class:`BlockTree` keyed by name, ``apply_ops`` grows/
mutates that tree, and ``persist.save_tree`` retires every live row and
reinserts the whole tree (row ids are rebuilt every save; names carry
across). So a block never needs to be looked up by id here — only by name.

Op catalog (slice 3 round 1 blocks; round 2 adds ports + connects; topology
lands round 3). The first 8 (``add_block``/``instance_block``/
``set_pose``/``remove_block``/``add_port``/``remove_port``/``connect``/
``disconnect``) are the shared core ops (:mod:`precis.blocktree.ops`), used
here as-is (``set_pose``/``disconnect``) or extended with nm's own fields:

- ``add_block``      — mint a new block, optionally nested under an
  existing ``parent``, with an optional envelope (validated through the
  real ``precis.cad.dsl`` parser, never re-implemented here) and an
  optional initial ``dof`` — the core op mints the block; this module then
  vets/assigns ``dof`` on it (rolling the block back out if ``dof`` isn't a
  JSON object, so a bad ``dof`` never leaves a partial block behind).
- ``instance_block``  — mint a new block that **reuses** an existing
  block's subtree by reference (``template``), resolved at *read* time —
  the ``cad`` ``Design.instance`` pattern. Only ``template``/``name``/
  ``parent``/``pose``/``rot`` are accepted — an instance resolves
  ``envelope``/``desc``/``use``/``dof`` from its template, so those keys are
  rejected rather than silently dropped; the core's
  :func:`~precis.blocktree.ops._instance_shared` already rejects
  ``envelope``/``desc``/``use`` (and does the template/name/parent
  validation, including the instance-of-instance and nest-under-own-
  template checks), so this module's own wrapper adds only the ``dof``
  rejection before delegating to :func:`~precis.blocktree.ops.
  _commit_instance` — which also runs the real indirect-cycle search
  (:func:`~precis.blocktree.ops._find_instance_cycle`) over the
  "expands-to" relation, exactly the infinite-recursion predictor the
  read-time tree walk (``precis_nm.handler._render_tree``) needs guarded
  against; that walk also carries its own expansion-stack guard as defense
  in depth, in case a row bypasses this validation (e.g. hand-corrupted
  data, or a future bug elsewhere).
- ``set_pose``        — rewrite an existing block's pose and/or rotation.
  Unmodified core op.
- ``remove_block``    — remove a block and its whole subtree; refused
  while any live block elsewhere in the tree instances it (or one of its
  descendants) — the core's template-in-use guard. Once past that guard,
  any live ``connect`` touching the removed subtree (either endpoint's
  block in it — including an *instance* of a removed block, since the
  instance's name, not the template's, is what a connect actually stores)
  is dropped in the same op — the ``structure`` vacancy precedent; the
  core's cascade. Threading is name-keyed the same way (module docstring
  above), so this module adds the same vacancy rule for
  ``tree.threading`` on top — ``validate``'s ``dangling_threading``/
  ``dangling_connect`` exist precisely to catch cases where this *doesn't*
  run (hand-corrupted data).
- ``add_port``        — mint a named attachment point on a block. Only an
  ordinary (non-instance) block owns ports — an instance resolves its
  ports from its template at read time (:func:`effective_ports`), the same
  rule ``instance_block`` already applies to envelope/desc/use/dof, so
  ``add_port`` on an instance is rejected with that explanation rather than
  silently attaching to the wrong row. ``direction``, when given, is
  normalized to unit length; a zero vector is a retryable :class:`OpError`.
  The port ``name`` may not contain ``'.'`` — the ``connect``/
  ``disconnect`` ``'block.port'`` syntax reserves it (see
  ``_split_endpoint``'s last-dot rule). A full override of the core op —
  nm's :class:`PortSpec` carries ``expected_element``/
  ``expected_hybridization`` where the core's open ``annotations`` dict
  would go, so the final construction can't be shared.
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance of this
  block (the instance's connect names the instance's block, not the
  template's, but the port it resolves to is this one — see
  :func:`effective_ports`) — the connect is named in the error either way.
  This module also refuses removing a port named in the block's own
  declared ``dof`` (``axis_ports``) — a check the core has no concept of —
  so it is a full override rather than an extension of the core op.
- ``connect``         — a port↔port intent edge (``a``/``b`` as
  ``'block.port'``, split on the *last* dot so a block name may itself
  contain one — port names may not, see ``add_port`` above). Each
  endpoint's port is looked up on the block itself, or — when the block is
  an instance — on its template (:func:`effective_ports`, "instances
  resolve ports from their template" applied at bind time too).
  Self-connects and duplicate live connects (same unordered endpoint pair)
  are rejected. **Capability gate** (transferred from
  pcb-component-model.md, "nothing attaches unless the capability affords
  it"): a ``kind='bond'`` connect requires *both* ports' ``roles`` to
  include ``'covalent'`` — or, when ``objectives={'role': ...}`` names a
  different role, both ports must afford *that* role instead. The
  rejection names the port's actual roles, never just "no". This is a
  **declared-intent check, not a chemistry validation** (the
  pcb-component-model trust model: capability *labelling*, not proof) —
  ``roles`` are whatever the caller asserted via ``add_port`` and are never
  independently checked against real chemistry, so the gate catches an
  *inconsistent* declaration (a connect the caller's own labels don't
  support), not an *implausible* one. nm's per-connect ``kind`` slot is its
  own extension over the core's :class:`~precis.blocktree.types.Connect`
  (``se``'s equivalent slot is named ``joint`` — a different, unrelated
  concept, not unified with this one), so this op is a full override, not
  an extension, of the core's ``connect``.
- ``disconnect``      — remove a live connect by its unordered endpoint
  pair; a missing pair is a retryable :class:`OpError` listing what *is*
  live. Unmodified core op.
- ``declare_threading`` — record an L2 topology invariant: ``a`` is
  threaded through ``b`` (the rotaxane macrocycle-on-axle relation),
  **stored explicitly, never re-derived from geometry** (nm-kind.md's L2
  rule). Directional (``a``/``b`` are not interchangeable) and per-pair —
  ``a == b``, a duplicate live ``(a, b)`` pair, and a live opposite-
  direction ``(b, a)`` pair are all rejected (mutual threading — each
  block inside the other — is physically impossible; the rejection names
  ``remove_threading`` for a genuinely wrong-direction declaration).
  ``bind_structure``/``declare_dof`` are handler-level (they need the
  store or are set directly on a block field); this module only owns the
  pure threading/dof-shape checks.
- ``remove_threading``  — drop a live threading pair; a missing pair is a
  retryable :class:`OpError` listing what *is* live.
- ``declare_dof``       — set a block's declared degree of freedom
  (``kind='rotational'|'translational'``, ``axis_ports`` = exactly two
  port names on the block). Only an ordinary (non-instance) block owns a
  dof — same rule ``add_port`` already applies to ports, rejected with
  that explanation rather than silently landing on the wrong row — and
  both ``axis_ports`` must resolve on the block's *own* ports (not through
  a template: an instance never reaches this op at all). Persists on the
  existing ``nm_blocks.dof`` jsonb column (no new storage).
- ``clear_dof``         — clear a block's declared dof (same instance
  rejection as ``declare_dof``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from precis.blocktree import ops as blocktree
from precis.blocktree.types import BlockNode, Connect, OpError, Port, Tree

#: What an ``axis_ports``-bearing DOF's ``kind`` may be — nm-kind.md's L2
#: vocabulary.
_DOF_KINDS = ("rotational", "translational")


@dataclass
class PortSpec(Port):
    """A named attachment point on a block — the capability-set half of the
    "one fact, two projections" port (pcb-component-model.md): the
    scaffold-side stub lives here (``roles``/``direction``/expected
    element·hybridization, this module's own extension over the shared
    :class:`~precis.blocktree.types.Port`); the atom-side attachment (once
    filled) is ``bound_design``/``bound_atom``. ``roles`` is a capability
    *set*, never an equivalence relation — legal attachments are derived at
    ``connect`` time from these roles, never stored as a second relation
    (see this module's docstring, "Capability gate"). ``expected_element``/
    ``expected_hybridization`` deliberately stay their own typed fields
    rather than moving into the inherited ``annotations`` open dict — see
    docs/backlog/nm-se-shared-blocktree-core.md, "What this is NOT"; this
    module never populates ``annotations``."""

    expected_element: str | None = None
    expected_hybridization: str | None = None
    #: The atom-side projection of this one port fact (structure design
    #: slug + atom label within it), set by the handler-level
    #: ``bind_structure`` op (needs the store — see ``precis_nm.handler``).
    #: NULL until filled. Always set together (both or neither).
    bound_design: str | None = None
    bound_atom: str | None = None


@dataclass
class ConnectSpec(Connect):
    """A port↔port intent edge — bond or non-bonded interaction — between
    two ``'block.port'`` endpoints, name-keyed like everything else in this
    module (see the module docstring's ``connect`` entry). ``kind`` is
    nm's own extension over the shared :class:`~precis.blocktree.types.
    Connect` (``se``'s ``joint`` is an unrelated slot on the same base
    class — not unified with this one); ``objectives`` is the shared free
    objective-vector slot (e.g. target bond length/angle, or the
    ``{'role': ...}`` override the capability gate reads)."""

    kind: str = "bond"


@dataclass
class ThreadingSpec:
    """One L2 threading invariant: ``a`` is threaded through ``b`` (e.g. a
    macrocycle ``a`` on an axle ``b``) — directional, name-keyed (see the
    module docstring's ``declare_threading`` entry). Stored explicitly,
    never re-derived from geometry. Has no shared-core analogue — threading
    is entirely nm's own L2 vocabulary."""

    a: str
    b: str


@dataclass
class NmBlock(BlockNode):
    """One block, addressed by ``name`` (the stable identity — see the
    module docstring). ``parent``/``template`` are block *names*, resolved
    to fresh row ids only at persist time. ``ports`` (shared with
    :class:`~precis.blocktree.types.BlockNode`) is keyed by port name; only
    an ordinary (non-instance) block ever has entries here — an instance's
    ports resolve from its template (:func:`effective_ports`). The fields
    below this line are nm's own extension over the shared ``BlockNode``.

    Named ``NmBlock``, not the bare ``BlockNode`` this class used to be —
    the core spine now owns that name (see
    :class:`~precis.blocktree.types.BlockNode`'s own docstring for why it
    isn't just ``Block``); ``NmBlock`` disambiguates the two the same way
    ``precis_se.ops.SeBlock`` does for its own subclass."""

    #: Re-declared (not new) — narrows the inherited ``dict[str, Port]``
    #: to nm's own :class:`PortSpec`; every port this module's ``add_port``
    #: ever stores is one. Same slot, same default, just a precise type.
    # mypy flags this as an unsafe narrowing (dict is invariant — a caller
    # holding this as a plain BlockNode could in principle assign a bare
    # Port in). ``BlockNode`` isn't generic over its port type the way
    # ``Tree`` is over block/connect (docs/backlog/
    # nm-se-shared-blocktree-core.md's phase 2 note: a real gap, not
    # papered over — worth a ``BlockNode[TPort: Port]`` if a third domain
    # ever needs its own port fields too), so this is the narrowest fix
    # available without widening that core class for a single caller.
    ports: dict[str, PortSpec] = field(default_factory=dict)  # type: ignore[assignment]
    dof: dict[str, Any] | None = None
    #: The L5 block-level binding (structure design slug), set by the
    #: handler-level ``bind_structure`` op. Always ``None`` on an instance —
    #: ``bind_structure`` rejects an instance block the same way
    #: ``add_port``/``declare_dof`` do (bind via the template instead).
    bound_design: str | None = None


@dataclass
class BlockTree(Tree[NmBlock, ConnectSpec]):
    """A design's live blocks, keyed by name, plus its live ``connects``
    and ``threading`` invariants. Insertion order is not significant —
    renderers/persisters compute their own (tree / topological) order from
    ``parent``/``template``; ``connects``/``threading`` are unordered lists
    (pair identity, not position). The fields below this line are nm's own
    extension over the shared :class:`~precis.blocktree.types.Tree`."""

    #: L2 threading invariants (:class:`ThreadingSpec`), unordered —
    #: identity is the ``(a, b)`` pair.
    threading: list[ThreadingSpec] = field(default_factory=list)

    def make_block(self, **kwargs: Any) -> NmBlock:
        return NmBlock(**kwargs)


def apply_ops(tree: BlockTree, ops: list[dict[str, Any]]) -> BlockTree:
    """Apply a list of typed ops to ``tree`` in order, mutating it —
    dispatches through :data:`_OPS` (the core's 8 shared ops plus nm's own
    6), via the core's generic :func:`~precis.blocktree.ops.apply_ops`."""
    return blocktree.apply_ops(tree, ops, _OPS)


# ── helpers (imported from the core; nm calls these directly for its own
# op implementations below) ─────────────────────────────────────────────

_require_name = blocktree._require_name
_opt_str = blocktree._opt_str
_as_vec3 = blocktree._as_vec3
_unit_vec = blocktree._unit_vec
_no_block_msg = blocktree._no_block_msg
_descendants = blocktree._descendants
_split_endpoint = blocktree._split_endpoint
_connects_endpoint_pair = blocktree._connects_endpoint_pair


def effective_envelope(tree: BlockTree, node: NmBlock) -> str | None:
    """The envelope "seen" at ``node`` for render purposes — its own, or —
    when ``node`` is an instance — its template's (an instance's own
    ``envelope`` field is always ``None``, see the core's
    :func:`~precis.blocktree.ops._instance_shared`'s rejection of that
    key). nm has no third envelope source (unlike ``se``'s catalog
    fallback), so this is the shared core's
    :func:`~precis.blocktree.ops.effective_envelope` directly."""
    return blocktree.effective_envelope(tree, node)


def effective_ports(tree: BlockTree, node: NmBlock) -> dict[str, PortSpec]:
    """The ports "seen" at ``node`` for connect/render purposes: its own
    ports, or — when ``node`` is an instance — its template's (an instance
    never owns ports itself, see :func:`_op_add_port`'s rejection) — the
    shared core's :func:`~precis.blocktree.ops.effective_ports`. Retyped
    (not just re-exported) for nm's own :class:`PortSpec`: every port this
    module's ``add_port`` ever stores is one, so the dict the core returns
    always is too — mypy's dict-is-invariant check just can't see that."""
    return cast("dict[str, PortSpec]", blocktree.effective_ports(tree, node))


def effective_dof(tree: BlockTree, node: NmBlock) -> dict[str, Any] | None:
    """The dof "seen" at ``node`` for render purposes — its own, or — when
    ``node`` is an instance — its template's, LOCAL or cross-design
    (:func:`~precis.blocktree.ops.resolve_template` — an instance's own
    ``dof`` field is always ``None``; see :func:`_op_instance_block`'s
    rejection of that key; ``declare_dof`` also only ever writes to an
    ordinary block). A physically real degree of freedom on a template
    block genuinely applies to every instance of it, so this mirrors
    :func:`effective_envelope`'s instance→template resolution. Has no
    shared-core analogue — the core knows nothing about dof, so this stays
    nm's own function, just built on the core's cross-design-aware
    resolver rather than a bare local ``tree.blocks.get``."""
    if node.template is not None:
        template_node = blocktree.resolve_template(tree, node.template)
        return getattr(template_node, "dof", None)
    return node.dof


def _resolve_connect_port(
    tree: BlockTree, block_name: str, port_name: str, *, what: str
) -> PortSpec:
    """Resolve one ``connect``/``disconnect``-style endpoint to its
    :class:`PortSpec`, raising a legible :class:`OpError` naming what *is*
    available when the block or port doesn't resolve. Mirrors the core's
    :func:`~precis.blocktree.ops._resolve_connect_port`, kept as its own
    small function here (rather than plugged in via that function's
    ``ports_fn`` hook) purely for nm's ``PortSpec`` return type — used at op
    time (``_op_connect``) and, over freshly-loaded/persisted data, by
    ``precis_nm.validate``'s ``dangling_connect``/``port_capability``
    checks (the render-cycle-guard shape: op-time validation plus a
    defense-in-depth re-check that never trusts stored data)."""
    node = tree.blocks.get(block_name)
    if node is None:
        raise OpError(_no_block_msg(tree, block_name, what=f"{what} block"))
    ports = effective_ports(tree, node)
    port = ports.get(port_name)
    if port is None:
        via = f" (resolved via template {node.template!r})" if node.template else ""
        roster = ", ".join(sorted(ports)) if ports else "(none)"
        raise OpError(
            f"no such port on block {block_name!r}{via}: {port_name!r}. "
            f"Available ports: {roster}"
        )
    return port


def connect_role(kind: str, objectives: dict[str, Any]) -> str | None:
    """The role a ``kind='bond'`` connect's endpoints must both afford —
    ``objectives={'role': ...}`` overrides the default ``'covalent'``.
    ``None`` for ``kind='interaction'``: non-bonded interactions aren't
    capability-gated this round."""
    if kind != "bond":
        return None
    role = objectives.get("role")
    return str(role).strip() if role else "covalent"


def _check_bond_capability(
    a_block: str,
    a_port: str,
    a_spec: PortSpec,
    b_block: str,
    b_port: str,
    b_spec: PortSpec,
    role: str,
) -> None:
    role_label = "bond" if role == "covalent" else repr(role)
    for blk, prt, spec in ((a_block, a_port, a_spec), (b_block, b_port, b_spec)):
        if role not in spec.roles:
            raise OpError(
                f"connect: {blk}.{prt} does not afford {role!r} "
                f"(its roles: {spec.roles or ['(none)']}) — a {role_label} "
                "connect needs both ports to afford the role; add it via "
                "add_port, or pass objectives={'role': '<a role both ports "
                "have>'} to gate on a different one"
            )


# ── op implementations ───────────────────────────────────────────────────


def _op_add_block(tree: BlockTree, op: dict[str, Any]) -> None:
    blocktree.op_add_block(tree, op)
    name = str(op["name"]).strip()
    dof = op.get("dof")
    if dof is not None:
        if not isinstance(dof, dict):
            del tree.blocks[name]
            raise OpError(f"add_block 'dof' must be a JSON object, got {dof!r}")
        tree.blocks[name].dof = dof


def _op_instance_block(tree: BlockTree, op: dict[str, Any]) -> None:
    template, name, parent = blocktree._instance_shared(
        tree, op, opname="instance_block"
    )
    if op.get("dof") is not None:
        raise OpError(
            "instance_block does not take 'dof' — an instance resolves "
            f"dof from its template ({template!r}) at read time; set it "
            "on the template block instead"
        )
    blocktree._commit_instance(tree, op, name=name, template=template, parent=parent)


def _op_remove_block(tree: BlockTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "remove_block")
    # Computed before delegating to the core op (which raises if ``name``
    # doesn't exist or is used as a template — atomically, before any
    # mutation) so the threading cascade below acts on exactly the subtree
    # the core just removed.
    subtree = (_descendants(tree, name) | {name}) if name in tree.blocks else set()
    blocktree.op_remove_block(tree, op)
    # Threading is name-keyed the same way connects are (module docstring),
    # so the same vacancy rule (structure precedent: removing an atom drops
    # its bonds too) drops any threading pair touching the removed subtree
    # — ``validate``'s ``dangling_threading`` exists precisely to catch
    # cases where this *doesn't* run (hand-corrupted data).
    tree.threading = [
        t for t in tree.threading if t.a not in subtree and t.b not in subtree
    ]


def _op_add_port(tree: BlockTree, op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "add_port")
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
    if node.template is not None:
        raise OpError(
            f"block {block!r} is an instance (of {node.template!r}) — an "
            "instance resolves its ports from its template at read time "
            f"(same rule as envelope/desc/use/dof); add_port on "
            f"{node.template!r} instead"
        )
    name = _require_name(op, "name", "add_port")
    if "." in name:
        raise OpError(
            f"add_port 'name' must not contain '.': {name!r} — the "
            "connect/disconnect endpoint syntax ('block.port', split on the "
            "last dot) reserves it; a dotted port name would make an "
            "endpoint ambiguous"
        )
    if name in node.ports:
        raise OpError(
            f"duplicate port name on block {block!r}: {name!r} (port "
            "names are unique per block)"
        )
    roles_raw = op.get("roles")
    if roles_raw is None:
        roles: list[str] = []
    elif not isinstance(roles_raw, list) or not all(
        isinstance(r, str) for r in roles_raw
    ):
        raise OpError(f"add_port 'roles' must be a list of strings, got {roles_raw!r}")
    else:
        roles = [r.strip() for r in roles_raw if r.strip()]
    direction: list[float] | None = None
    if op.get("direction") is not None:
        raw_vec = _as_vec3(op.get("direction"), "add_port direction")
        direction = _unit_vec(raw_vec, what=f"add_port direction for {block}.{name}")
    node.ports[name] = PortSpec(
        name=name,
        roles=roles,
        direction=direction,
        expected_element=_opt_str(op.get("expected_element")),
        expected_hybridization=_opt_str(op.get("expected_hybridization")),
    )


def _op_remove_port(tree: BlockTree, op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "remove_port")
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
    name = _require_name(op, "name", "remove_port")
    if name not in node.ports:
        roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
        raise OpError(
            f"no such port on block {block!r}: {name!r}. Available ports: {roster}"
        )
    # A connect referencing this port through an INSTANCE of ``block`` (its
    # a_block/b_block is the instance's own name, resolved to this port via
    # effective_ports at connect time — see the module docstring) must
    # block removal just as directly as one naming ``block`` itself; the
    # reviewer's bug report is exactly this case going unguarded.
    instances = {n for n, b in tree.blocks.items() if b.template == block}
    blockers = [
        c
        for c in tree.connects
        if (c.a_block, c.a_port) == (block, name)
        or (c.b_block, c.b_port) == (block, name)
        or (c.a_block in instances and c.a_port == name)
        or (c.b_block in instances and c.b_port == name)
    ]
    if blockers:
        names = ", ".join(
            f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}" for c in blockers
        )
        raise OpError(
            f"port {block}.{name} is used by live connect(s) {names} — disconnect first"
        )
    # A port named as one of the block's own declared dof axis_ports would
    # otherwise leave dof pointing at a vanished port name (a dangling
    # reference no validator currently checks for, since dof — unlike
    # connects — has no dedicated defense-in-depth re-check yet) — refuse
    # up front instead, the same "block first" discipline as the connect
    # guard above. The core has no concept of dof, so this check has no
    # shared-core analogue.
    if node.dof and name in (node.dof.get("axis_ports") or ()):
        raise OpError(
            f"port {block}.{name} is used by declared dof (axis_ports) — "
            "clear_dof first"
        )
    del node.ports[name]


def _op_connect(tree: BlockTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("connect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _split_endpoint(a_raw, "connect 'a'")
    b_block, b_port = _split_endpoint(b_raw, "connect 'b'")
    if (a_block, a_port) == (b_block, b_port):
        raise OpError(f"connect: cannot connect {a_raw!r} to itself")
    kind = str(op.get("kind") or "bond").strip().lower()
    if kind not in ("bond", "interaction"):
        raise OpError(f"connect 'kind' must be 'bond' or 'interaction', got {kind!r}")
    objectives_raw = op.get("objectives")
    if objectives_raw is not None and not isinstance(objectives_raw, dict):
        raise OpError(
            f"connect 'objectives' must be a JSON object, got {objectives_raw!r}"
        )
    objectives = dict(objectives_raw) if objectives_raw else {}

    a_spec = _resolve_connect_port(tree, a_block, a_port, what="connect")
    b_spec = _resolve_connect_port(tree, b_block, b_port, what="connect")

    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for c in tree.connects:
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            raise OpError(
                f"connect: {a_block}.{a_port}—{b_block}.{b_port} already "
                f"exists (kind={c.kind!r})"
            )

    role = connect_role(kind, objectives)
    if role is not None:
        _check_bond_capability(a_block, a_port, a_spec, b_block, b_port, b_spec, role)

    tree.connects.append(
        ConnectSpec(
            a_block=a_block,
            a_port=a_port,
            b_block=b_block,
            b_port=b_port,
            kind=kind,
            objectives=objectives,
        )
    )


def _op_declare_threading(tree: BlockTree, op: dict[str, Any]) -> None:
    a = _require_name(op, "a", "declare_threading")
    b = _require_name(op, "b", "declare_threading")
    if a == b:
        raise OpError(f"declare_threading: 'a' and 'b' must differ, got {a!r} twice")
    if a not in tree.blocks:
        raise OpError(_no_block_msg(tree, a, what="a"))
    if b not in tree.blocks:
        raise OpError(_no_block_msg(tree, b, what="b"))
    for t in tree.threading:
        if t.a == a and t.b == b:
            raise OpError(
                f"declare_threading: {a!r} is already declared threaded through {b!r}"
            )
        # Mutual threading is physically impossible (reviewer decision,
        # nm-kind.md round 3): a threaded through b and b threaded through
        # a at once would mean each is inside the other. Reject the
        # opposite-direction row too, naming remove_threading as the fix
        # for a genuinely wrong-direction declaration.
        if t.a == b and t.b == a:
            raise OpError(
                f"declare_threading: {b!r} is already threaded through {a!r} "
                "— mutual threading is physically impossible; "
                "remove_threading first if the direction was wrong"
            )
    tree.threading.append(ThreadingSpec(a=a, b=b))


def _op_remove_threading(tree: BlockTree, op: dict[str, Any]) -> None:
    a = _require_name(op, "a", "remove_threading")
    b = _require_name(op, "b", "remove_threading")
    for i, t in enumerate(tree.threading):
        if t.a == a and t.b == b:
            del tree.threading[i]
            return
    live = ", ".join(f"{t.a}→{t.b}" for t in tree.threading) or "(none)"
    raise OpError(f"no such threading {a!r} through {b!r}. Live threading: {live}")


def _op_declare_dof(tree: BlockTree, op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "declare_dof")
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
    if node.template is not None:
        raise OpError(
            f"block {block!r} is an instance (of {node.template!r}) — an "
            "instance resolves dof from its template at read time (same "
            f"rule as envelope/desc/use/ports); declare_dof on "
            f"{node.template!r} instead"
        )
    kind = _require_name(op, "kind", "declare_dof")
    if kind not in _DOF_KINDS:
        raise OpError(f"declare_dof 'kind' must be one of {_DOF_KINDS}, got {kind!r}")
    axis_raw = op.get("axis_ports")
    if (
        not isinstance(axis_raw, list)
        or len(axis_raw) != 2
        or not all(isinstance(p, str) and p.strip() for p in axis_raw)
    ):
        raise OpError(
            "declare_dof needs 'axis_ports' as a list of exactly 2 port "
            f"names, got {axis_raw!r}"
        )
    axis_ports = [p.strip() for p in axis_raw]
    for p in axis_ports:
        if p not in node.ports:
            roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
            raise OpError(
                f"declare_dof: no such port on block {block!r}: {p!r}. "
                f"Available ports: {roster}"
            )
    node.dof = {"kind": kind, "axis_ports": axis_ports}


def _op_clear_dof(tree: BlockTree, op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "clear_dof")
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
    if node.template is not None:
        raise OpError(
            f"block {block!r} is an instance (of {node.template!r}) — dof "
            f"lives on the template; clear_dof on {node.template!r} instead"
        )
    node.dof = None


_OPS = {
    **blocktree.CORE_OPS,
    "add_block": _op_add_block,
    "instance_block": _op_instance_block,
    "remove_block": _op_remove_block,
    "add_port": _op_add_port,
    "remove_port": _op_remove_port,
    "connect": _op_connect,
    "declare_threading": _op_declare_threading,
    "remove_threading": _op_remove_threading,
    "declare_dof": _op_declare_dof,
    "clear_dof": _op_clear_dof,
}
