"""Pure ops over an in-memory block tree — no store access, no unit.

Extracted whole from ``precis_se.ops`` (docs/backlog/
blocktree-library-build-plan.md §Settled): the LLM edits the *graph* via
typed ops; :func:`apply_ops` mutates a :class:`~precis.blocktree.types.Tree`
in place; an unknown op or a bad reference raises :class:`~precis.
blocktree.types.OpError`. A domain plugin (``precis_se``) registers its
own extra ops alongside :data:`CORE_OPS` in the table it passes to
:func:`apply_ops`, and may override any of the 8 core entries outright —
``apply_ops`` never hardcodes which ops exist, the caller's table does.

The 8 shared ops:

- ``add_block``      — mint a new block, optionally nested under an
  existing ``parent``, with an optional envelope (validated through the
  real ``precis.cad.dsl`` parser, never re-implemented here).
- ``instance_block``  — mint a block that **reuses** an existing block's
  subtree by reference (``template``). Only ``template``/``name``/
  ``parent``/``pose``/``rot`` are accepted — an instance resolves
  ``envelope``/``desc``/``use`` from its template, so those keys are
  rejected rather than silently dropped. The instance-of-instance,
  nest-under-own-template, and indirect-cycle guards run over the
  "expands-to" relation (:func:`_find_instance_cycle`) — a cycle there is
  an infinite-recursion predictor for the read-time tree walk. A domain
  that also has a *patterned* instance op (``se``'s ``array_block``) calls
  :func:`_instance_shared`/:func:`_commit_instance` directly to share this
  validation without going through the plain ``instance_block`` op.
  ``template`` also accepts a **cross-design** reference
  (``'design-slug#block-name'``, :func:`~precis.blocktree.types.
  parse_template_ref` — docs/backlog/blocktree-library-build-plan.md slice
  1, the library-part-by-reference feature): resolved through
  :attr:`~precis.blocktree.types.Tree.foreign`, a store-backed lookup the
  HANDLER wires in (this package still touches no store directly), so a
  design can instance a block from another design without copying it — an
  edit to the library part's own design keeps showing up everywhere it's
  instanced, exactly like a local instance already does.
  :func:`_find_instance_cycle` walks into a foreign design too, so "A
  instances B, B instances A" is refused across designs the same way it
  already was within one.
- ``set_pose``        — rewrite an existing block's pose and/or rotation.
- ``remove_block``    — remove a block and its whole subtree; refused
  while any live block elsewhere instances it (or a descendant). Any live
  connect touching the removed subtree (either endpoint's block in it —
  including an *instance* of a removed block, since the instance's name is
  what a connect actually stores) is dropped in the same op. A domain with
  its own per-block/per-connect side tables (``se``'s measures/BOM)
  overrides this op to cascade them too, typically by computing the
  subtree with :func:`_descendants` *before* delegating to this
  implementation.
- ``add_port``        — mint a named attachment point on a block. Only an
  ordinary (non-instance) block owns ports — an instance resolves its
  ports from its template at read time (:func:`effective_ports`, the same
  rule as envelope/desc/use). ``roles`` is a capability set; ``direction``
  normalizes to unit length (zero vector rejected); ``annotations`` is an
  open dict. The port ``name`` may not contain ``'.'`` — the ``connect``/
  ``disconnect`` ``'block.port'`` syntax reserves it.
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance of this
  block.
- ``connect``         — a port↔port intent edge (``a``/``b`` as
  ``'block.port'``, split on the *last* dot). Each endpoint resolves on
  the block itself or — for an instance — its template. Self- and
  duplicate connects (same unordered endpoint pair) are rejected. A
  domain with a per-connect mechanism/joint slot (``se``'s ``joint``,
  ``nm``'s ``kind``) overrides this op outright rather than extending it —
  that slot is domain vocabulary, not shared shape.
- ``disconnect``      — remove a live connect by its unordered endpoint
  pair; a missing pair is a retryable :class:`~precis.blocktree.types.
  OpError` listing what *is* live.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from precis.blocktree.types import (
    TEMPLATE_SEP,
    BlockNode,
    OpError,
    Port,
    Tree,
    parse_template_ref,
)
from precis.cad import dsl as cad_dsl

#: An op implementation: ``(tree, op) -> None``, mutating ``tree`` in
#: place. Typed against ``Any`` for the tree parameter rather than
#: ``Tree[Any, Any]`` — a domain's own op functions are typed against its
#: own concrete ``Tree`` subclass (e.g. ``(tree: SeTree, op) -> None``),
#: and Callable parameter types are contravariant, so a table entry typed
#: any more specifically than ``Any`` would reject the domain's own ops.
OpFn = Callable[[Any, dict[str, Any]], None]
OpsTable = Mapping[str, OpFn]


def apply_ops[TTree: Tree[Any, Any]](
    tree: TTree, ops: list[dict[str, Any]], ops_table: OpsTable
) -> TTree:
    """Apply a list of typed ops to ``tree`` in order, mutating it, via
    ``ops_table`` — the domain's own (possibly core-plus-extras, possibly
    core-with-overrides) op dispatch table. Never hardcodes which ops
    exist; that is entirely the caller's table."""
    for op in ops:
        if "op" not in op:
            raise OpError(f"op missing 'op' key: {op!r}")
        name = op["op"]
        handler = ops_table.get(name)
        if handler is None:
            known = ", ".join(sorted(ops_table))
            raise OpError(f"unknown op: {name!r}; known: {known}")
        handler(tree, op)
    return tree


# ── helpers ──────────────────────────────────────────────────────────────


def _require_name(op: dict[str, Any], key: str, opname: str) -> str:
    raw = op.get(key)
    if raw is None or not str(raw).strip():
        raise OpError(f"{opname} needs {key!r}")
    return str(raw).strip()


def _reject_hash(name: str, *, opname: str, what: str) -> None:
    """A block ``name`` may not contain :data:`~precis.blocktree.types.
    TEMPLATE_SEP` (``'#'``) — reserved by the cross-design ``template``
    syntax (``'design-slug#block-name'``, :func:`~precis.blocktree.types.
    parse_template_ref`), the same reservation ``add_port`` already makes
    for ``'.'`` in a PORT name so the ``'block.port'`` connect syntax stays
    unambiguous. Applied at both places a block name is minted
    (``op_add_block``'s ``name``, an instance's own ``name`` in
    :func:`_instance_shared`) — splitting a qualified template on the FIRST
    ``'#'`` only stays unambiguous if a block name can never contain one."""
    if TEMPLATE_SEP in name:
        raise OpError(
            f"{opname} {what} must not contain {TEMPLATE_SEP!r}: {name!r} "
            "— the cross-design template syntax ('design-slug#block-name') "
            "reserves it"
        )


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


def _unit_vec(vec: list[float], *, what: str) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise OpError(f"{what} must be a nonzero vector, got {vec!r}")
    return [x / norm for x in vec]


def _no_block_msg(tree: Tree[Any, Any], name: str, *, what: str) -> str:
    base = f"no such block ({what}): {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"


def _validate_envelope(config: str) -> None:
    """Parse-only validation, reusing the real cad mini-DSL parser (never
    re-implemented here) — ``DslError`` already names the valid shapes.
    The DSL is unit-agnostic float64; a domain declares its own unit once,
    in its own docs — this function never states one."""
    try:
        cad_dsl.parse(config)
    except cad_dsl.DslError as exc:
        raise OpError(f"bad envelope: {exc}") from exc


def _descendants(tree: Tree[Any, Any], name: str) -> set[str]:
    """Names of every block whose parent chain passes through ``name``
    (not including ``name`` itself) — fixed-point pass over the small
    tree."""
    out: set[str] = set()
    frontier = {name}
    while frontier:
        nxt = {n for n, b in tree.blocks.items() if b.parent in frontier} - out
        out |= nxt
        frontier = nxt
    return out


def _foreign_tree(
    tree: Tree[Any, Any], design_slug: str | None
) -> Tree[Any, Any] | None:
    """The design tree for ``design_slug`` as seen from ``tree`` — ``tree``
    itself when ``design_slug`` names THIS design (``== tree.own_slug``,
    including the ``None is None`` case for a caller that never set
    ``own_slug`` and is only ever walking a purely local graph), else
    whatever :attr:`~precis.blocktree.types.Tree.foreign` resolves. Total:
    ``None`` both when no resolver is wired and when the resolver itself
    reports nothing at that slug (not found, or retired) — callers decide
    whether that is loud (write time, ``instance_block``) or silent (read
    time, :func:`resolve_template`); this is just the lookup, memoized by
    neither (the resolver itself is the caller's cache — see
    :data:`~precis.blocktree.types.ForeignResolver`'s docstring)."""
    if design_slug == tree.own_slug:
        return tree
    if design_slug is None or tree.foreign is None:
        return None
    return tree.foreign(design_slug)


def resolve_template(tree: Tree[Any, Any], template: str) -> BlockNode | None:
    """Resolve a ``template`` reference — bare local, or cross-design
    (``'design-slug#block-name'``) — to its :class:`BlockNode`. Total, the
    same rule :func:`effective_envelope`/:func:`effective_ports` already
    apply to a dangling LOCAL template: ``None`` for every unresolvable case
    (design not found or retired, block not found in it, or — the bare
    local case — no such block in ``tree``), never raises. A cross-design
    dangling reference is a real, ordinary reachable state (there is no
    cross-design analogue of ``remove_block``'s "refused while a live
    instance elsewhere uses this as a template" guard — the owning design
    can be edited or retired with no cross-reference to stop it), so this
    stays total for exactly the reason a local dangling reference already
    did: a read path (render, validate) must degrade gracefully, not crash
    the whole design over one stale reference. The LOUD counterpart lives
    at *write* time instead — ``instance_block``'s own validation
    (:func:`_instance_shared`) rejects a template that doesn't resolve
    when the instance is FIRST minted, the same "loud at bind time, not a
    silent drift" split ``precis_nm.handler``'s ``bind_structure`` already
    uses for its own store-backed reference."""
    design_slug, block_name = parse_template_ref(template)
    if design_slug is None:
        return tree.blocks.get(block_name)
    owner = _foreign_tree(tree, design_slug)
    return owner.blocks.get(block_name) if owner is not None else None


def _find_instance_cycle(tree: Tree[Any, Any]) -> list[str] | None:
    """DFS cycle search over the "expands-to" relation — the indirect-cycle
    guard the two local checks in ``instance_block``/a domain's patterned
    variant can't provide. Spans into other designs when a template is
    cross-design-qualified: node identity is ``(design slug, block name)``,
    with ``None`` standing for "whatever ``tree.own_slug`` is" so a purely
    local graph (every caller before this slice, and every caller that
    never sets ``own_slug``) is identical to what this function computed
    before cross-design instancing existed — same nodes, same edges, same
    messages. A foreign design is fetched at most once per call
    (``design_tree``'s local cache below), however many nodes within it get
    visited, so a design instanced by many blocks costs one fetch here, not
    one per descendant (the request-level cache lives in the resolver
    itself — :data:`~precis.blocktree.types.ForeignResolver`'s docstring —
    this is just DFS-local memoization on top). Returns the cycle as a name
    path: bare block names when every hop stays in one design (byte-
    identical to the pre-cross-design format, so every existing local-cycle
    message is unchanged), or, the moment more than one design is involved,
    EVERY hop qualified (``'design-slug#block-name'``) — so the message
    always names every design in the cycle, not just the ones that happen
    to differ from wherever the walk started."""
    cache: dict[str | None, Tree[Any, Any] | None] = {}

    def design_tree(slug: str | None) -> Tree[Any, Any] | None:
        if slug not in cache:
            cache[slug] = _foreign_tree(tree, slug)
        return cache[slug]

    def expands_to(slug: str | None, name: str) -> set[tuple[str | None, str]]:
        """Every *other* (design, template) referenced by an instance
        anywhere in ``(slug, name)``'s subtree — exactly the extra edge the
        read-time tree walk introduces when it resolves an instance's
        subtree, now spanning a foreign design when a descendant's own
        ``template`` is qualified."""
        dtree = design_tree(slug)
        if dtree is None:
            return set()
        out: set[tuple[str | None, str]] = set()
        for d in _descendants(dtree, name):
            t = dtree.blocks[d].template
            if t is None:
                continue
            t_slug, t_name = parse_template_ref(t)
            out.add((t_slug if t_slug is not None else slug, t_name))
        return out

    on_stack: set[tuple[str | None, str]] = set()
    visited: set[tuple[str | None, str]] = set()
    stack: list[tuple[str | None, str]] = []

    def visit(node: tuple[str | None, str]) -> list[tuple[str | None, str]] | None:
        visited.add(node)
        on_stack.add(node)
        stack.append(node)
        slug, name = node
        for m in sorted(expands_to(slug, name), key=lambda x: (x[0] or "", x[1])):
            if m in on_stack:
                i = stack.index(m)
                return [*stack[i:], m]
            if m not in visited:
                found = visit(m)
                if found is not None:
                    return found
        stack.pop()
        on_stack.discard(node)
        return None

    own = tree.own_slug
    for name in sorted(tree.blocks):
        start = (own, name)
        if start not in visited:
            found = visit(start)
            if found is not None:
                slugs = {s for s, _ in found}
                if len(slugs) <= 1:
                    return [n for _, n in found]
                return [f"{s}{TEMPLATE_SEP}{n}" for s, n in found]
    return None


def effective_envelope(tree: Tree[Any, Any], node: BlockNode) -> str | None:
    """The envelope "seen" at ``node`` for render purposes — its own, or —
    when ``node`` is an instance — its template's, LOCAL or cross-design
    (:func:`resolve_template` — an instance's own ``envelope`` field is
    always ``None``; see the template-metadata rejection in
    :func:`_instance_shared`). A dangling ``template`` resolves to ``None``
    rather than raising, so defense-in-depth callers (render, validate)
    stay total functions — :func:`resolve_template`'s docstring on why that
    stays true across a cross-design reference too."""
    if node.template is not None:
        template_node = resolve_template(tree, node.template)
        return template_node.envelope if template_node is not None else None
    return node.envelope


def effective_ports(tree: Tree[Any, Any], node: BlockNode) -> dict[str, Port]:
    """The ports "seen" at ``node`` for connect/render purposes: its own
    ports, or — when ``node`` is an instance — its template's, LOCAL or
    cross-design (:func:`resolve_template`; an instance never owns ports
    itself — see ``add_port``'s rejection). A dangling ``template``
    resolves to no ports rather than raising — same total-function
    tolerance as :func:`effective_envelope`."""
    if node.template is not None:
        template_node = resolve_template(tree, node.template)
        return template_node.ports if template_node is not None else {}
    return node.ports


def _split_endpoint(raw: Any, what: str) -> tuple[str, str]:
    """``'block.port'`` → ``(block, port)``, splitting on the *last* dot so
    a block name may itself contain one (port names may not — ``add_port``
    rejects a dotted name)."""
    s = str(raw or "").strip()
    block, sep, port = s.rpartition(".")
    block, port = block.strip(), port.strip()
    if not sep or not block or not port:
        raise OpError(f"{what} must be 'block.port', got {raw!r}")
    return block, port


def _resolve_connect_port(
    tree: Tree[Any, Any],
    block_name: str,
    port_name: str,
    *,
    what: str,
    ports_fn: Callable[[Any, Any], dict[str, Port]] = effective_ports,
) -> Port:
    """Resolve one ``connect``/``disconnect``-style endpoint to its
    :class:`Port`, raising a legible :class:`OpError` naming what *is*
    available — used at op time and, over freshly-loaded data, by a
    handler's validate re-check (never trusts stored data). ``ports_fn``
    defaults to the core :func:`effective_ports`; a domain whose own
    ``effective_ports`` adds another source (e.g. ``se``'s catalog
    fallback) passes its own in."""
    node = tree.blocks.get(block_name)
    if node is None:
        raise OpError(_no_block_msg(tree, block_name, what=f"{what} block"))
    ports = ports_fn(tree, node)
    port = ports.get(port_name)
    if port is None:
        via = f" (resolved via template {node.template!r})" if node.template else ""
        roster = ", ".join(sorted(ports)) if ports else "(none)"
        raise OpError(
            f"no such port on block {block_name!r}{via}: {port_name!r}. "
            f"Available ports: {roster}"
        )
    return port


def _connects_endpoint_pair(
    a_block: str, a_port: str, b_block: str, b_port: str
) -> frozenset[tuple[str, str]]:
    return frozenset({(a_block, a_port), (b_block, b_port)})


# ── instancing (shared validation, no patterning) ───────────────────────


def _foreign_template_node(
    tree: Tree[Any, Any], design_slug: str, block_name: str, *, opname: str
) -> BlockNode:
    """The write-time (loud) counterpart of :func:`resolve_template`'s
    read-time (total) foreign lookup — used only when MINTING a brand-new
    instance (:func:`_instance_shared`). A qualified ``template`` that
    can't be resolved is a named :class:`OpError` here, never a silent
    empty/None: the "loud at bind time, not a silent drift" split
    ``precis_nm.handler``'s ``bind_structure`` already applies to its own
    store-backed reference, applied here to the design half (does
    ``design_slug`` even resolve — not found, or soft-retired, both read
    the same way through :attr:`~precis.blocktree.types.Tree.foreign`,
    mirroring ``store.get_ref``'s "retired reads as absent" default) and
    the block half (does ``block_name`` exist within it) separately, each
    naming exactly what's missing."""
    owner = _foreign_tree(tree, design_slug)
    if owner is None:
        qualified = f"{design_slug}{TEMPLATE_SEP}{block_name}"
        raise OpError(
            f"{opname}: no such design {design_slug!r} to take template "
            f"{qualified!r} from — it may not exist, or has been retired"
        )
    node = owner.blocks.get(block_name)
    if node is None:
        raise OpError(
            _no_block_msg(owner, block_name, what=f"template in design {design_slug!r}")
        )
    return node


def _instance_shared(
    tree: Tree[Any, Any], op: dict[str, Any], *, opname: str
) -> tuple[str, str, str | None]:
    """The validation every instance-minting op shares: resolve + vet
    ``template`` (must exist, must be an ordinary block — LOCAL, or
    cross-design via ``'design-slug#block-name'``, docs/backlog/
    blocktree-library-build-plan.md slice 1), ``name`` (unique, and may not
    itself contain the cross-design separator), the template-metadata
    rejection (envelope/desc/use resolve from the template at read time —
    rejected, never silently dropped), and ``parent`` (must exist; must not
    nest the template inside its own instance — LOCAL only, since a
    cross-design template's subtree lives in a different design's
    namespace and can never collide with a local ``parent`` chain). Returns
    ``(template, name, parent)`` — ``template`` is the raw string as given
    (bare or qualified), stored on the node as-is so every existing reader
    of ``node.template`` (render, ``effective_*``) keeps working unchanged.
    A domain with a *patterned* instance op (``se``'s ``array_block``)
    calls this directly rather than going through the plain
    :func:`op_instance_block`."""
    template = _require_name(op, "template", opname)
    design_slug, block_name = parse_template_ref(template)
    if design_slug is None:
        if template not in tree.blocks:
            raise OpError(_no_block_msg(tree, template, what="template"))
        if tree.blocks[template].template is not None:
            raise OpError(
                f"block {template!r} is itself an instance — {opname} the "
                "original template block, not another instance"
            )
    else:
        foreign_node = _foreign_template_node(
            tree, design_slug, block_name, opname=opname
        )
        if foreign_node.template is not None:
            raise OpError(
                f"block {block_name!r} in design {design_slug!r} is itself "
                f"an instance — {opname} the original template block there, "
                "not another instance"
            )
    name = _require_name(op, "name", opname)
    _reject_hash(name, opname=opname, what="'name'")
    if name in tree.blocks:
        raise OpError(f"duplicate block name: {name!r} (names are unique per design)")
    for key in ("envelope", "desc", "use"):
        if op.get(key) is not None:
            raise OpError(
                f"{opname} does not take {key!r} — an instance resolves "
                f"{key} from its template ({template!r}) at read time; set "
                "it on the template block instead"
            )
    parent = op.get("parent")
    if parent is not None:
        parent = str(parent).strip()
        if parent not in tree.blocks:
            raise OpError(_no_block_msg(tree, parent, what="parent"))
        if parent == template or parent in _descendants(tree, template):
            raise OpError(
                f"{opname}: parent {parent!r} is {template!r} or one of "
                "its descendants — that would nest the template inside its "
                "own instance (infinite recursion at read time)"
            )
    return template, name, parent


def _commit_instance(
    tree: Tree[Any, Any],
    op: dict[str, Any],
    *,
    name: str,
    template: str,
    parent: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Tentatively add the instance node (via :meth:`Tree.make_block`, so
    the domain's own block subclass gets minted, not the bare core one),
    then run the real cycle search (the local checks in
    :func:`_instance_shared` only catch a direct cycle) — rolling back on
    rejection so a failed op never mutates the tree. ``extra`` carries any
    domain-specific fields the new block needs beyond ``name``/``parent``/
    ``template``/``pose``/``rot`` (e.g. ``se``'s ``array`` spec)."""
    tree.blocks[name] = tree.make_block(
        name=name,
        parent=parent,
        template=template,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        **(extra or {}),
    )
    cycle = _find_instance_cycle(tree)
    if cycle is not None:
        del tree.blocks[name]
        raise OpError(f"instance cycle: {' → '.join(cycle)}")


# ── the 8 shared op implementations ─────────────────────────────────────


def op_add_block(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
    name = _require_name(op, "name", "add_block")
    _reject_hash(name, opname="add_block", what="'name'")
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
    tree.blocks[name] = tree.make_block(
        name=name,
        parent=parent,
        template=None,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        envelope=envelope,
        descr=_opt_str(op.get("desc")),
        use=_opt_str(op.get("use")),
    )


def op_instance_block(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
    template, name, parent = _instance_shared(tree, op, opname="instance_block")
    _commit_instance(tree, op, name=name, template=template, parent=parent)


def op_set_pose(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
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


def op_remove_block(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
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
    # Vacancy precedent: removing a block drops its connects too. A
    # connect stores the literal block name at each endpoint — the
    # *instance's* name when the endpoint sits on an instance — so
    # "touching the removed subtree" means either endpoint's stored name
    # is in ``subtree``, no template resolution needed here.
    tree.connects = [
        c
        for c in tree.connects
        if c.a_block not in subtree and c.b_block not in subtree
    ]
    for n in subtree:
        del tree.blocks[n]


def op_add_port(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
    block = _require_name(op, "block", "add_port")
    node = tree.blocks.get(block)
    if node is None:
        raise OpError(_no_block_msg(tree, block, what="block"))
    if node.template is not None:
        raise OpError(
            f"block {block!r} is an instance (of {node.template!r}) — an "
            "instance resolves its ports from its template at read time "
            "(same rule as envelope/desc/use); add_port on "
            f"{node.template!r} instead"
        )
    name = _require_name(op, "name", "add_port")
    if "." in name:
        raise OpError(
            f"add_port 'name' must not contain '.': {name!r} — the "
            "connect/disconnect endpoint syntax ('block.port', split on "
            "the last dot) reserves it; a dotted port name would make an "
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
    annotations_raw = op.get("annotations")
    if annotations_raw is not None and not isinstance(annotations_raw, dict):
        raise OpError(
            f"add_port 'annotations' must be a JSON object, got {annotations_raw!r}"
        )
    node.ports[name] = Port(
        name=name,
        roles=roles,
        direction=direction,
        annotations=dict(annotations_raw) if annotations_raw else {},
    )


def op_remove_port(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
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
    # A connect referencing this port through an INSTANCE of ``block``
    # (its endpoint names the instance, resolved to this port via
    # effective_ports) blocks removal as directly as one naming ``block``
    # itself.
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


def op_connect(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise OpError("connect needs 'a' and 'b' (each 'block.port')")
    a_block, a_port = _split_endpoint(a_raw, "connect 'a'")
    b_block, b_port = _split_endpoint(b_raw, "connect 'b'")
    if (a_block, a_port) == (b_block, b_port):
        raise OpError(f"connect: cannot connect {a_raw!r} to itself")
    _resolve_connect_port(tree, a_block, a_port, what="connect")
    _resolve_connect_port(tree, b_block, b_port, what="connect")
    pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
    for c in tree.connects:
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            raise OpError(
                f"connect: {a_block}.{a_port}—{b_block}.{b_port} already exists"
            )
    tree.connects.append(
        tree.make_connect(
            a_block=a_block,
            a_port=a_port,
            b_block=b_block,
            b_port=b_port,
            objectives=op.get("objectives") or {},
        )
    )


def op_disconnect(tree: Tree[Any, Any], op: dict[str, Any]) -> None:
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


#: The 8 shared ops, keyed by op name — a domain merges this with its own
#: extra ops (and may override any entry) to build the table it passes to
#: :func:`apply_ops`.
CORE_OPS: OpsTable = {
    "add_block": op_add_block,
    "instance_block": op_instance_block,
    "set_pose": op_set_pose,
    "remove_block": op_remove_block,
    "add_port": op_add_port,
    "remove_port": op_remove_port,
    "connect": op_connect,
    "disconnect": op_disconnect,
}
