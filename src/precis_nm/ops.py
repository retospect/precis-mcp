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

Op catalog (slice 3 round 1 — ports/topology ops land in rounds 2/3):

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
  descendants) — the template-in-use guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.cad import dsl as cad_dsl


class OpError(ValueError):
    """A rejected nm op (bad reference, unknown op, malformed payload)."""


@dataclass
class BlockNode:
    """One block, addressed by ``name`` (the stable identity — see the
    module docstring). ``parent``/``template`` are block *names*, resolved
    to fresh row ids only at persist time."""

    name: str
    parent: str | None = None
    template: str | None = None
    pose: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rot: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    envelope: str | None = None
    descr: str | None = None
    use: str | None = None
    dof: dict[str, Any] | None = None


@dataclass
class BlockTree:
    """A design's live blocks, keyed by name. Insertion order is not
    significant — renderers/persisters compute their own (tree / topological)
    order from ``parent``/``template``."""

    blocks: dict[str, BlockNode] = field(default_factory=dict)


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
    for n in subtree:
        del tree.blocks[n]


_OPS = {
    "add_block": _op_add_block,
    "instance_block": _op_instance_block,
    "set_pose": _op_set_pose,
    "remove_block": _op_remove_block,
}
