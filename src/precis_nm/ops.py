"""Pure ops over an in-memory nm block tree — no store access.

Mirrors :mod:`precis.structure.ops`'s discipline: the LLM edits the *graph*
(intent) via typed ops; :func:`apply_ops` mutates a :class:`BlockTree` in
place and returns it; an unknown op or a bad reference raises
:class:`OpError` (the handler maps that onto ``BadInput``).

**Identity is the block ``name``, not a row id** — same rule as
``structure``'s atom labels. ``precis_nm.persist`` loads a design's live
rows into a fresh :class:`BlockTree` keyed by name, ``apply_ops`` grows/
mutates that tree, and ``persist.save_tree`` retires every live row and
reinserts the whole tree (row ids are rebuilt every save; names carry
across). So a block never needs to be looked up by id here — only by name.

Op catalog (slice 3 round 1 blocks; round 2 adds ports + connects; topology
lands round 3):

- ``add_block``      — mint a new block, optionally nested under an
  existing ``parent``, with an optional envelope (validated through the
  real ``precis.cad.dsl`` parser, never re-implemented here).
- ``instance_block``  — mint a new block that **reuses** an existing
  block's subtree by reference (``template``), resolved at *read* time —
  the ``cad`` ``Design.instance`` pattern. Only ``template``/``name``/
  ``parent``/``pose``/``rot`` are accepted — an instance resolves
  ``envelope``/``desc``/``use``/``dof`` from its template, so those keys are
  rejected rather than silently dropped. An instance cannot itself be
  instanced (``template`` must be an ordinary block) and cannot be nested
  under the template's own subtree (the direct/simple cases, checked
  first for a clearer message). Neither local check is sufficient on its
  own against an *indirect* cycle — e.g. A hosts an instance of B, B hosts
  an instance of A — so every ``instance_block`` also runs a real cycle
  search (:func:`_find_instance_cycle`) over the "expands-to" relation
  (template T expands to template U when T's subtree contains an instance
  of U) after tentatively applying the op, rolling back and rejecting if
  a cycle appears. This is the exact relation the read-time tree walk
  (``precis_nm.handler._render_tree``) follows when it expands an
  instance's subtree, so a cycle here is precisely an infinite-recursion
  predictor for that walk — the walk *also* carries its own
  expansion-stack guard as defense in depth, in case a row bypasses this
  validation (e.g. hand-corrupted data, or a future bug elsewhere).
- ``set_pose``        — rewrite an existing block's pose and/or rotation.
- ``remove_block``    — soft-remove a block and its whole subtree; refused
  while any live block elsewhere in the tree instances it (or one of its
  descendants) — the template-in-use guard (checked first). Once past that
  guard, any live ``connect`` touching the removed subtree (either endpoint's
  block in it — including an *instance* of a removed block, since the
  instance's name, not the template's, is what a connect actually stores) is
  retired in the same op — the ``structure`` vacancy precedent: removing an
  atom drops its bonds too. Silent, like vacancy — ops don't return
  messages, and ``validate``'s ``dangling_connect`` exists precisely to
  catch cases where this *doesn't* run (hand-corrupted data).
- ``add_port``        — mint a named attachment point on a block. Only an
  ordinary (non-instance) block owns ports — an instance resolves its
  ports from its template at read time (:func:`effective_ports`), the same
  rule ``instance_block`` already applies to envelope/desc/use/dof, so
  ``add_port`` on an instance is rejected with that explanation rather than
  silently attaching to the wrong row. ``direction``, when given, is
  normalized to unit length; a zero vector is a retryable :class:`OpError`
  (there is no such thing as a direction-less bond vector). The port
  ``name`` may not contain ``'.'`` — the ``connect``/``disconnect``
  ``'block.port'`` syntax reserves it (see ``_split_endpoint``'s last-dot
  rule), so a dotted port name would make an endpoint string ambiguous.
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance of this
  block (the instance's connect names the instance's block, not the
  template's, but the port it resolves to is this one — see
  :func:`effective_ports`) — the connect is named in the error either way.
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
  support), not an *implausible* one.
- ``disconnect``      — remove a live connect by its unordered endpoint
  pair; a missing pair is a retryable :class:`OpError` listing what *is*
  live.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from precis.cad import dsl as cad_dsl


class OpError(ValueError):
    """A rejected nm op (bad reference, unknown op, malformed payload)."""


@dataclass
class PortSpec:
    """A named attachment point on a block — the capability-set half of the
    "one fact, two projections" port (pcb-component-model.md): the
    scaffold-side stub lives here (``roles``/``direction``/expected
    element·hybridization); the atom-side attachment (once filled) is a
    later round. ``roles`` is a capability *set*, never an equivalence
    relation — legal attachments are derived at ``connect`` time from these
    roles, never stored as a second relation (see ``ops.py``'s module
    docstring, "Capability gate")."""

    name: str
    roles: list[str] = field(default_factory=list)
    direction: list[float] | None = None
    expected_element: str | None = None
    expected_hybridization: str | None = None


@dataclass
class ConnectSpec:
    """A port↔port intent edge — bond or non-bonded interaction — between
    two ``'block.port'`` endpoints, name-keyed like everything else in this
    module (see the module docstring's ``connect`` entry). ``objectives``
    is the free objective-vector slot (e.g. target bond length/angle, or
    the ``{'role': ...}`` override the capability gate reads)."""

    a_block: str
    a_port: str
    b_block: str
    b_port: str
    kind: str = "bond"
    objectives: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockNode:
    """One block, addressed by ``name`` (the stable identity — see the
    module docstring). ``parent``/``template`` are block *names*, resolved
    to fresh row ids only at persist time. ``ports`` is keyed by port name;
    only an ordinary (non-instance) block ever has entries here — an
    instance's ports resolve from its template (:func:`effective_ports`)."""

    name: str
    parent: str | None = None
    template: str | None = None
    pose: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rot: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    envelope: str | None = None
    descr: str | None = None
    use: str | None = None
    dof: dict[str, Any] | None = None
    ports: dict[str, PortSpec] = field(default_factory=dict)


@dataclass
class BlockTree:
    """A design's live blocks, keyed by name, plus its live ``connects``.
    Insertion order is not significant — renderers/persisters compute their
    own (tree / topological) order from ``parent``/``template``; ``connects``
    is an unordered list (endpoint-pair identity, not position)."""

    blocks: dict[str, BlockNode] = field(default_factory=dict)
    connects: list[ConnectSpec] = field(default_factory=list)


def apply_ops(tree: BlockTree, ops: list[dict[str, Any]]) -> BlockTree:
    """Apply a list of typed ops to ``tree`` in order, mutating it."""
    for op in ops:
        if "op" not in op:
            raise OpError(f"op missing 'op' key: {op!r}")
        name = op["op"]
        handler = _OPS.get(name)
        if handler is None:
            known = ", ".join(sorted(_OPS))
            raise OpError(f"unknown op: {name!r}; known: {known}")
        handler(tree, op)
    return tree


# ── helpers ──────────────────────────────────────────────────────────────


def _require_name(op: dict[str, Any], key: str, opname: str) -> str:
    raw = op.get(key)
    if raw is None or not str(raw).strip():
        raise OpError(f"{opname} needs {key!r}")
    return str(raw).strip()


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_vec3(value: Any, what: str) -> list[float]:
    """``None``/absent → ``[0.0, 0.0, 0.0]``; else must coerce to exactly 3
    floats, or a retryable :class:`OpError`."""
    if value is None:
        return [0.0, 0.0, 0.0]
    try:
        vec = [float(x) for x in value]
    except (TypeError, ValueError) as exc:
        raise OpError(f"{what} must be a 3-vector [x, y, z], got {value!r}") from exc
    if len(vec) != 3:
        raise OpError(f"{what} must be a 3-vector [x, y, z], got {value!r}")
    return vec


def _no_block_msg(tree: BlockTree, name: str, *, what: str) -> str:
    base = f"no such block ({what}): {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"


def _validate_envelope(config: str) -> None:
    """Parse-only validation, reusing the real cad mini-DSL parser (never
    re-implemented here) — ``DslError`` already names the valid shapes."""
    try:
        cad_dsl.parse(config)
    except cad_dsl.DslError as exc:
        raise OpError(f"bad envelope: {exc}") from exc


def _descendants(tree: BlockTree, name: str) -> set[str]:
    """Names of every block whose parent chain passes through ``name``
    (not including ``name`` itself). Fixed-point pass over the (small) tree
    — mirrors ``component_would_cycle``'s ancestor-walk shape, in the
    descendant direction."""
    out: set[str] = set()
    frontier = {name}
    while frontier:
        nxt = {n for n, b in tree.blocks.items() if b.parent in frontier} - out
        out |= nxt
        frontier = nxt
    return out


def _expands_to(tree: BlockTree, template: str) -> set[str]:
    """Direct "expands-to" edges for ``template``: every *other* template
    referenced by an instance anywhere in ``template``'s subtree.

    This is exactly the extra edge the read-time tree walk introduces
    beyond the plain parent-forest: when the walk resolves an instance of
    ``template``, it recurses into ``template``'s structural children —
    and if one of those (at any depth) is itself an instance of ``U``, the
    walk goes on to expand ``U``'s subtree too. A cycle in this relation is
    therefore precisely an infinite-recursion predictor for that walk (see
    :func:`_find_instance_cycle`).
    """
    out: set[str] = set()
    for d in _descendants(tree, template):
        t = tree.blocks[d].template
        if t is not None:
            out.add(t)
    return out


def _find_instance_cycle(tree: BlockTree) -> list[str] | None:
    """DFS cycle search over the "expands-to" relation (:func:`_expands_to`).

    The two structural checks in ``_op_instance_block`` (instance-of-an-
    instance, nesting under one's own template) only catch a *direct*
    cycle; an indirect one — A's subtree hosts an instance of B, B's
    subtree hosts an instance of A — needs a real graph search, since
    neither local check ever sees the other template. Returns the cycle as
    a name path (e.g. ``['A', 'B', 'A']``), or ``None`` if the relation is
    acyclic.
    """
    graph = {name: _expands_to(tree, name) for name in tree.blocks}
    on_stack: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        visited.add(n)
        on_stack.add(n)
        stack.append(n)
        for m in sorted(graph.get(n, ())):
            if m in on_stack:
                i = stack.index(m)
                return [*stack[i:], m]
            if m not in visited:
                found = visit(m)
                if found is not None:
                    return found
        stack.pop()
        on_stack.discard(n)
        return None

    for n in sorted(graph):
        if n not in visited:
            found = visit(n)
            if found is not None:
                return found
    return None


def effective_ports(tree: BlockTree, node: BlockNode) -> dict[str, PortSpec]:
    """The ports "seen" at ``node`` for connect/render purposes: its own
    ports, or — when ``node`` is an instance — its template's (an instance
    never owns ports itself, see :func:`_op_add_port`'s rejection). A
    dangling ``template`` (shouldn't happen; ``ops.py`` never lets one form)
    resolves to no ports rather than raising, so callers built for defense
    in depth (validate, render) stay total functions."""
    if node.template is not None:
        template_node = tree.blocks.get(node.template)
        return template_node.ports if template_node is not None else {}
    return node.ports


def effective_envelope(tree: BlockTree, node: BlockNode) -> str | None:
    """The envelope "seen" at ``node`` for render purposes — its own, or —
    when ``node`` is an instance — its template's (an instance's own
    ``envelope`` field is always ``None``, see ``_op_instance_block``'s
    rejection of that key). Mirrors :func:`effective_ports`'s
    instance→template resolution and the same dangling-template tolerance."""
    if node.template is not None:
        template_node = tree.blocks.get(node.template)
        return template_node.envelope if template_node is not None else None
    return node.envelope


def _unit_vec(vec: list[float], *, what: str) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise OpError(f"{what} must be a nonzero vector, got {vec!r}")
    return [x / norm for x in vec]


def _split_endpoint(raw: Any, what: str) -> tuple[str, str]:
    """``'block.port'`` → ``(block, port)``, splitting on the *last* dot so
    a block name may itself contain one."""
    s = str(raw or "").strip()
    block, sep, port = s.rpartition(".")
    block, port = block.strip(), port.strip()
    if not sep or not block or not port:
        raise OpError(f"{what} must be 'block.port', got {raw!r}")
    return block, port


def _resolve_connect_port(
    tree: BlockTree, block_name: str, port_name: str, *, what: str
) -> PortSpec:
    """Resolve one ``connect``/``disconnect``-style endpoint to its
    :class:`PortSpec`, raising a legible :class:`OpError` naming what *is*
    available when the block or port doesn't resolve — used at op time
    (``_op_connect``) and, over freshly-loaded/persisted data, by
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


def _connects_endpoint_pair(
    a_block: str, a_port: str, b_block: str, b_port: str
) -> frozenset[tuple[str, str]]:
    return frozenset({(a_block, a_port), (b_block, b_port)})


# ── op implementations ───────────────────────────────────────────────────


def _op_add_block(tree: BlockTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "name", "add_block")
    if name in tree.blocks:
        raise OpError(f"duplicate block name: {name!r} (names are unique per design)")
    parent = op.get("parent")
    if parent is not None:
        parent = str(parent).strip()
        if parent not in tree.blocks:
            raise OpError(_no_block_msg(tree, parent, what="parent"))
    envelope = op.get("envelope")
    if envelope is not None:
        envelope = str(envelope).strip()
        _validate_envelope(envelope)
    dof = op.get("dof")
    if dof is not None and not isinstance(dof, dict):
        raise OpError(f"add_block 'dof' must be a JSON object, got {dof!r}")
    tree.blocks[name] = BlockNode(
        name=name,
        parent=parent,
        template=None,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        envelope=envelope,
        descr=_opt_str(op.get("desc")),
        use=_opt_str(op.get("use")),
        dof=dof,
    )


def _op_instance_block(tree: BlockTree, op: dict[str, Any]) -> None:
    template = _require_name(op, "template", "instance_block")
    if template not in tree.blocks:
        raise OpError(_no_block_msg(tree, template, what="template"))
    if tree.blocks[template].template is not None:
        raise OpError(
            f"block {template!r} is itself an instance — instance the "
            "original template block, not another instance"
        )
    name = _require_name(op, "name", "instance_block")
    if name in tree.blocks:
        raise OpError(f"duplicate block name: {name!r} (names are unique per design)")
    for key in ("envelope", "desc", "use", "dof"):
        if op.get(key) is not None:
            raise OpError(
                f"instance_block does not take {key!r} — an instance "
                f"resolves {key} from its template ({template!r}) at read "
                f"time; set it on the template block instead"
            )
    parent = op.get("parent")
    if parent is not None:
        parent = str(parent).strip()
        if parent not in tree.blocks:
            raise OpError(_no_block_msg(tree, parent, what="parent"))
        if parent == template or parent in _descendants(tree, template):
            raise OpError(
                f"instance_block: parent {parent!r} is {template!r} or one "
                "of its descendants — that would nest the template inside "
                "its own instance (infinite recursion at read time)"
            )
    tree.blocks[name] = BlockNode(
        name=name,
        parent=parent,
        template=template,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        envelope=None,
        descr=None,
        use=None,
        dof=None,
    )
    # Local checks above only catch a direct cycle — an indirect one (A
    # hosts an instance of B, B hosts an instance of A) needs the real
    # graph search. Tentatively committed above so the search sees the new
    # edge; roll back on rejection so a failed op never mutates the tree.
    cycle = _find_instance_cycle(tree)
    if cycle is not None:
        del tree.blocks[name]
        raise OpError(f"instance cycle: {' → '.join(cycle)}")


def _op_set_pose(tree: BlockTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "set_pose")
    node = tree.blocks.get(name)
    if node is None:
        raise OpError(_no_block_msg(tree, name, what="block"))
    if "pose" not in op and "rot" not in op:
        raise OpError("set_pose needs 'pose' and/or 'rot'")
    if "pose" in op:
        node.pose = _as_vec3(op.get("pose"), "pose")
    if "rot" in op:
        node.rot = _as_vec3(op.get("rot"), "rot")


def _op_remove_block(tree: BlockTree, op: dict[str, Any]) -> None:
    name = _require_name(op, "block", "remove_block")
    if name not in tree.blocks:
        raise OpError(_no_block_msg(tree, name, what="block"))
    subtree = _descendants(tree, name) | {name}
    users = sorted(
        n for n, b in tree.blocks.items() if b.template in subtree and n not in subtree
    )
    if users:
        raise OpError(
            f"block {name!r} (or a descendant) is used as a template by "
            f"instance(s) {', '.join(users)} — remove the instance(s) first"
        )
    # Vacancy precedent (structure): removing an atom drops its bonds too.
    # A connect stores the literal block name at each endpoint — which is
    # the *instance's* name, not the template's, when the endpoint sits on
    # an instance — so "touching the removed subtree" means either
    # endpoint's block name is in ``subtree`` exactly as stored, no
    # template resolution needed here.
    tree.connects = [
        c
        for c in tree.connects
        if c.a_block not in subtree and c.b_block not in subtree
    ]
    for n in subtree:
        del tree.blocks[n]


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


def _op_disconnect(tree: BlockTree, op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("disconnect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _split_endpoint(a_raw, "disconnect 'a'")
    b_block, b_port = _split_endpoint(b_raw, "disconnect 'b'")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for i, c in enumerate(tree.connects):
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            del tree.connects[i]
            return
    live = (
        ", ".join(
            f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}" for c in tree.connects
        )
        or "(none)"
    )
    raise OpError(
        f"no such connect between {a_raw!r} and {b_raw!r}. Live connects: {live}"
    )


_OPS = {
    "add_block": _op_add_block,
    "instance_block": _op_instance_block,
    "set_pose": _op_set_pose,
    "remove_block": _op_remove_block,
    "add_port": _op_add_port,
    "remove_port": _op_remove_port,
    "connect": _op_connect,
    "disconnect": _op_disconnect,
}
