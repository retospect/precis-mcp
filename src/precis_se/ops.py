"""Pure ops over an in-memory se block tree — no store access.

Mirrors :mod:`precis_nm.ops`'s discipline (the LLM edits the *graph* via
typed ops; :func:`apply_ops` mutates a :class:`SeTree` in place; an unknown
op or a bad reference raises :class:`OpError`, mapped onto ``BadInput`` by
the handler), with se's own invariants: units are **metres** (float64,
se-kind.md "Decisions"), and **arrays are first-class block-level
structure** — an array node is an instance node (``template`` set, resolved
at read time, never copied) that additionally carries a multiplicity spec.

**Identity is the block ``name``, not a row id** — ``precis_se.persist``
loads a design's live rows into a fresh :class:`SeTree` keyed by name and
reinserts the whole tree on every save (row ids are rebuilt; names carry
across), so a block is only ever looked up by name here.

Op catalog (slices 1-3 — blocks/instancing/arrays + ports/connects +
joints/measures/loads):

- ``add_block``      — mint a new block, optionally nested under an
  existing ``parent``, with an optional envelope (validated through the
  real ``precis.cad.dsl`` parser, never re-implemented here — the DSL is
  unit-agnostic; se declares its numbers to be metres).
- ``instance_block``  — mint a block that **reuses** an existing block's
  subtree by reference (``template``). Only ``template``/``name``/
  ``parent``/``pose``/``rot`` are accepted — an instance resolves
  ``envelope``/``desc``/``use`` from its template, so those keys are
  rejected rather than silently dropped. The instance-of-instance,
  nest-under-own-template, and indirect-cycle guards are
  :mod:`precis_nm.ops`'s, transferred whole (:func:`_find_instance_cycle`
  over the "expands-to" relation — a cycle there is an
  infinite-recursion predictor for the read-time tree walk).
- ``array_block``     — mint an **array-instance** node: everything
  ``instance_block`` does, plus exactly one of ``linear`` (``count`` +
  ``pitch`` (m) + ``axis``) or ``polar`` (``count`` + ``radius`` (m) +
  ``axis``, axis defaulting to ``[0,0,1]`` — the cad node-level
  ``polar:nNrR`` modifier's implicit +z, lifted to block level per
  se-kind.md "Hierarchy"). ``count`` must be a whole number ≥ 2 — an
  array of one is just an instance, and the rejection says so. Member
  poses are derived at read/check time from the spec (the
  realization-as-overlay rule: the template's solid stored once, posed N
  times); per-member ``overrides``/unlink are a later round, so the key
  is rejected loudly today rather than swallowed (the ``**_kw`` lesson).
- ``set_pose``        — rewrite an existing block's pose and/or rotation.
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
  run).
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
  ``'block.port'`` syntax reserves it.
- ``remove_port``     — drop a port; refused while any live ``connect``
  still references it, *including* one stored against an instance/array
  of this block.
- ``connect``         — a port↔port intent edge (``a``/``b`` as
  ``'block.port'``, split on the *last* dot). Each endpoint resolves on
  the block itself or — for an instance/array — its template. Self- and
  duplicate connects (same unordered endpoint pair) are rejected.
  ``joint``/``objectives`` go through the slice-3 schemas
  (:mod:`precis_se.joints` — kinematic class × mechanism, registered
  load keys), same as ``set_joint``/``set_load`` below.
- ``disconnect``      — remove a live connect by its unordered endpoint
  pair; a missing pair is a retryable :class:`OpError` listing what *is*
  live.
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

import math
from dataclasses import dataclass, field
from typing import Any

from precis.cad import dsl as cad_dsl
from precis_se import joints as se_joints
from precis_se.bom import BomError, BomLine, vet_bom_fields
from precis_se.measures import MeasureError, MeasureSpec, validate_relation
from precis_se.modes import ModeError, parse_mode

#: What an L3 realization binding may point at — the two *designed*
#: realizations (a cad node set, an nm design) and the two *bought* ones
#: (an engineering-store component, a catalog part). Mirrors migration
#: 0003's ``se_blocks_bound_kind_check``.
_BINDING_KINDS: tuple[str, ...] = ("cad", "nm", "component", "part")


class OpError(ValueError):
    """A rejected se op (bad reference, unknown op, malformed payload)."""


@dataclass
class PortSpec:
    """A named attachment point on a block (the pcb pin→roles pattern:
    ``roles`` is a capability *set*; legal attachments are derived at
    connect/joint time from these roles, never stored as a second
    relation). ``annotations`` is the open dict over the future superset
    registry (se-kind.md "Annotations") — every key is *descriptive* at
    this round (rendered into views, never enforced); contract-classed
    checked keys arrive together with their first code consumer."""

    name: str
    roles: list[str] = field(default_factory=list)
    direction: list[float] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectSpec:
    """A port↔port intent edge between two ``'block.port'`` endpoints,
    name-keyed like everything else in this module. ``joint`` holds the
    slice-3 schema shape (:func:`precis_se.joints.validate_joint` —
    kinematic class × mechanism); ``objectives`` the registered loads
    vocabulary (:func:`precis_se.joints.validate_objectives`, real units).
    Both are vetted at write time; stored strays are DRC findings."""

    a_block: str
    a_port: str
    b_block: str
    b_port: str
    joint: dict[str, Any] | None = None
    objectives: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeBlock:
    """One block, addressed by ``name`` (the stable identity — see the
    module docstring). ``parent``/``template`` are block *names*, resolved
    to fresh row ids only at persist time. ``array`` is the multiplicity
    spec when this node is an array instance (``template`` is then always
    set too); an ordinary instance has ``template`` set and ``array``
    ``None``. ``ports`` is keyed by port name; only an ordinary
    (non-instance) block ever has entries here — an instance's/array's
    ports resolve from its template (:func:`effective_ports`). Pose is
    metres; rot degrees."""

    name: str
    parent: str | None = None
    template: str | None = None
    pose: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rot: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    envelope: str | None = None
    descr: str | None = None
    use: str | None = None
    array: dict[str, Any] | None = None
    ports: dict[str, PortSpec] = field(default_factory=dict)
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


@dataclass
class SeTree:
    """A design's live blocks, keyed by name, plus its live ``connects``.
    Insertion order is not significant — renderers/persisters compute
    their own (tree / topological) order from ``parent``/``template``;
    ``connects`` is an unordered list (pair identity, not position)."""

    blocks: dict[str, SeBlock] = field(default_factory=dict)
    connects: list[ConnectSpec] = field(default_factory=list)
    #: named measures + tolerance relations (:mod:`precis_se.measures`),
    #: unordered — identity is ``(block, name)``.
    measures: list[MeasureSpec] = field(default_factory=list)
    #: bought items (:mod:`precis_se.bom`), unordered — identity is
    #: (target, item_kind, item).
    bom: list[BomLine] = field(default_factory=list)


def apply_ops(tree: SeTree, ops: list[dict[str, Any]]) -> SeTree:
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


def _unit_vec(vec: list[float], *, what: str) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise OpError(f"{what} must be a nonzero vector, got {vec!r}")
    return [x / norm for x in vec]


def _no_block_msg(tree: SeTree, name: str, *, what: str) -> str:
    base = f"no such block ({what}): {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"


def _validate_envelope(config: str) -> None:
    """Parse-only validation, reusing the real cad mini-DSL parser (never
    re-implemented here) — ``DslError`` already names the valid shapes. The
    DSL is unit-agnostic float64; se declares the unit once: metres."""
    try:
        cad_dsl.parse(config)
    except cad_dsl.DslError as exc:
        raise OpError(f"bad envelope: {exc}") from exc


def _descendants(tree: SeTree, name: str) -> set[str]:
    """Names of every block whose parent chain passes through ``name``
    (not including ``name`` itself) — fixed-point pass over the small tree
    (:func:`precis_nm.ops._descendants`'s shape)."""
    out: set[str] = set()
    frontier = {name}
    while frontier:
        nxt = {n for n, b in tree.blocks.items() if b.parent in frontier} - out
        out |= nxt
        frontier = nxt
    return out


def _expands_to(tree: SeTree, template: str) -> set[str]:
    """Direct "expands-to" edges for ``template``: every *other* template
    referenced by an instance (or array node) anywhere in ``template``'s
    subtree — exactly the extra edge the read-time tree walk introduces
    when it resolves an instance's subtree (see
    :func:`precis_nm.ops._expands_to`, transferred whole; array nodes
    participate because they carry ``template`` like any instance)."""
    out: set[str] = set()
    for d in _descendants(tree, template):
        t = tree.blocks[d].template
        if t is not None:
            out.add(t)
    return out


def _find_instance_cycle(tree: SeTree) -> list[str] | None:
    """DFS cycle search over the "expands-to" relation — the indirect-cycle
    guard the two local checks in ``instance_block``/``array_block`` can't
    provide (:func:`precis_nm.ops._find_instance_cycle`'s docstring).
    Returns the cycle as a name path, or ``None`` if acyclic."""
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


def effective_envelope(tree: SeTree, node: SeBlock) -> str | None:
    """The envelope "seen" at ``node`` for render purposes — its own, or —
    when ``node`` is an instance/array — its template's (an instance's own
    ``envelope`` field is always ``None``; see the template-metadata
    rejection in :func:`_instance_shared`). A dangling ``template``
    resolves to ``None`` rather than raising, so defense-in-depth callers
    (render, validate) stay total functions."""
    if node.template is not None:
        template_node = tree.blocks.get(node.template)
        return template_node.envelope if template_node is not None else None
    return node.envelope


def effective_ports(tree: SeTree, node: SeBlock) -> dict[str, PortSpec]:
    """The ports "seen" at ``node`` for connect/render purposes: its own
    ports, or — when ``node`` is an instance/array — its template's (an
    instance never owns ports itself; see ``add_port``'s rejection). A
    dangling ``template`` resolves to no ports rather than raising —
    same total-function tolerance as :func:`effective_envelope`."""
    if node.template is not None:
        template_node = tree.blocks.get(node.template)
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
    tree: SeTree, block_name: str, port_name: str, *, what: str
) -> PortSpec:
    """Resolve one ``connect``/``disconnect``-style endpoint to its
    :class:`PortSpec`, raising a legible :class:`OpError` naming what *is*
    available — used at op time and, over freshly-loaded data, by the
    handler's validate re-check (never trusts stored data)."""
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


def _connects_endpoint_pair(
    a_block: str, a_port: str, b_block: str, b_port: str
) -> frozenset[tuple[str, str]]:
    return frozenset({(a_block, a_port), (b_block, b_port)})


# ── op implementations ───────────────────────────────────────────────────


def _op_add_block(tree: SeTree, op: dict[str, Any]) -> None:
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
    tree.blocks[name] = SeBlock(
        name=name,
        parent=parent,
        template=None,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        envelope=envelope,
        descr=_opt_str(op.get("desc")),
        use=_opt_str(op.get("use")),
    )


def _instance_shared(
    tree: SeTree, op: dict[str, Any], *, opname: str
) -> tuple[str, str, str | None]:
    """The validation both instance-minting ops share: resolve + vet
    ``template`` (must exist, must be an ordinary block), ``name``
    (unique), the template-metadata rejection (envelope/desc/use resolve
    from the template at read time — rejected, never silently dropped),
    and ``parent`` (must exist; must not nest the template inside its own
    instance). Returns ``(template, name, parent)``."""
    template = _require_name(op, "template", opname)
    if template not in tree.blocks:
        raise OpError(_no_block_msg(tree, template, what="template"))
    if tree.blocks[template].template is not None:
        raise OpError(
            f"block {template!r} is itself an instance — {opname} the "
            "original template block, not another instance"
        )
    name = _require_name(op, "name", opname)
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
    tree: SeTree,
    op: dict[str, Any],
    *,
    name: str,
    template: str,
    parent: str | None,
    array: dict[str, Any] | None,
) -> None:
    """Tentatively add the instance/array node, then run the real cycle
    search (the local checks in :func:`_instance_shared` only catch a
    direct cycle) — rolling back on rejection so a failed op never mutates
    the tree."""
    tree.blocks[name] = SeBlock(
        name=name,
        parent=parent,
        template=template,
        pose=_as_vec3(op.get("pose"), "pose"),
        rot=_as_vec3(op.get("rot"), "rot"),
        array=array,
    )
    cycle = _find_instance_cycle(tree)
    if cycle is not None:
        del tree.blocks[name]
        raise OpError(f"instance cycle: {' → '.join(cycle)}")


def _op_instance_block(tree: SeTree, op: dict[str, Any]) -> None:
    template, name, parent = _instance_shared(tree, op, opname="instance_block")
    for key in ("linear", "polar"):
        if op.get(key) is not None:
            raise OpError(
                f"instance_block does not take {key!r} — use array_block "
                "for a patterned instance"
            )
    _commit_instance(tree, op, name=name, template=template, parent=parent, array=None)


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
    _commit_instance(tree, op, name=name, template=template, parent=parent, array=spec)


def _op_set_pose(tree: SeTree, op: dict[str, Any]) -> None:
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


def _op_remove_block(tree: SeTree, op: dict[str, Any]) -> None:
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
    # Vacancy precedent (structure/nm): removing a block drops its
    # connects too. A connect stores the literal block name at each
    # endpoint — the *instance's* name when the endpoint sits on an
    # instance — so "touching the removed subtree" means either endpoint's
    # stored name is in ``subtree``, no template resolution needed here.
    tree.connects = [
        c
        for c in tree.connects
        if c.a_block not in subtree and c.b_block not in subtree
    ]
    # Measures owned by a removed block go with it (same vacancy rule). A
    # surviving measure whose *relation source* lived in the subtree is
    # deliberately kept — it dangles, and DRC's unresolvable_relation
    # finding reports it, read-time honesty over silent cleanup.
    tree.measures = [m for m in tree.measures if m.block not in subtree]
    # BOM lines follow their target: a block line in the subtree, and a
    # connect line on a connect that was just dropped above.
    tree.bom = [
        line
        for line in tree.bom
        if line.block not in subtree
        and not (
            line.is_connect and (line.a_block in subtree or line.b_block in subtree)
        )
    ]
    for n in subtree:
        del tree.blocks[n]


def _op_add_port(tree: SeTree, op: dict[str, Any]) -> None:
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
    node.ports[name] = PortSpec(
        name=name,
        roles=roles,
        direction=direction,
        annotations=dict(annotations_raw) if annotations_raw else {},
    )


def _op_remove_port(tree: SeTree, op: dict[str, Any]) -> None:
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
    # A connect referencing this port through an INSTANCE/array of
    # ``block`` (its endpoint names the instance, resolved to this port
    # via effective_ports) blocks removal as directly as one naming
    # ``block`` itself (the nm reviewer's unguarded case, guarded here
    # from day one).
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
    for i, c in enumerate(tree.connects):
        if _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port) == pair:
            del tree.connects[i]
            # BOM lines hung off this connect go with it (same vacancy
            # rule as remove_block) — a bearing bought *for a joint* has
            # no meaning once the joint is gone.
            tree.bom = [
                line
                for line in tree.bom
                if not (line.is_connect and _bom_pair(line) == pair)
            ]
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


def _vet_measure_fields(
    op: dict[str, Any], *, opname: str
) -> tuple[float | None, dict[str, Any] | None, str | None, str | None]:
    """The optional measure fields, vetted: ``(value, relation, strength,
    reason)`` — each ``None`` when absent from the op."""
    value: float | None = None
    if op.get("value") is not None:
        try:
            value = float(op["value"])
        except (TypeError, ValueError) as exc:
            raise OpError(
                f"{opname} 'value' must be a number (m), got {op['value']!r}"
            ) from exc
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
    strength: str | None = None
    if op.get("strength") is not None:
        strength = str(op["strength"]).strip().lower()
        if strength not in _STRENGTHS:
            raise OpError(
                f"{opname} 'strength' must be one of {' | '.join(_STRENGTHS)}, "
                f"got {op['strength']!r}"
            )
    return value, relation, strength, _opt_str(op.get("reason"))


def _find_measure(tree: SeTree, block: str, name: str) -> MeasureSpec | None:
    for m in tree.measures:
        if m.block == block and m.name == name:
            return m
    return None


def _op_add_measure(tree: SeTree, op: dict[str, Any]) -> None:
    """Mint a named measure on a block — ``value`` (m) and/or ``relation``
    (``{'source': 'block.measure', 'offset': <m>, 'tol': <m>}``), both
    optional (a measure may exist as a named handle first — suggestive by
    contract). A relation source that doesn't exist YET is accepted (a
    forward reference inside one ops batch is normal); an unresolvable
    relation is DRC's read-time finding."""
    block, name = _measure_shared(tree, op, opname="add_measure")
    if _find_measure(tree, block, name) is not None:
        raise OpError(
            f"duplicate measure on block {block!r}: {name!r} (measure "
            "names are unique per block; set_measure to change it)"
        )
    value, relation, strength, reason = _vet_measure_fields(op, opname="add_measure")
    tree.measures.append(
        MeasureSpec(
            block=block,
            name=name,
            value=value,
            relation=relation,
            strength=strength or "gauge",
            reason=reason,
        )
    )


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
    field_keys = ("value", "relation", "strength", "reason")
    if not any(k in op for k in field_keys):
        raise OpError(
            "set_measure needs at least one of value/relation/strength/reason"
        )
    # An explicit null must push back, not silently no-op (reviewer
    # finding): presence-based updates can't express "clear this field".
    nulled = [k for k in field_keys if k in op and op[k] is None]
    if nulled:
        raise OpError(
            f"set_measure cannot clear {', '.join(nulled)} with null — "
            "remove_measure + add_measure to drop a field"
        )
    value, relation, strength, reason = _vet_measure_fields(op, opname="set_measure")
    if value is not None:
        m.value = value
    if relation is not None:
        m.relation = relation
    if strength is not None:
        m.strength = strength
    if reason is not None:
        m.reason = reason


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


_OPS = {
    "add_block": _op_add_block,
    "instance_block": _op_instance_block,
    "array_block": _op_array_block,
    "set_pose": _op_set_pose,
    "set_envelope": _op_set_envelope,
    "remove_block": _op_remove_block,
    "add_port": _op_add_port,
    "remove_port": _op_remove_port,
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
}
