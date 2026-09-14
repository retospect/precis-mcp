"""``bind_structure`` / ``unbind_structure`` — the L5 binding ops, the two
store-aware halves of the atomic mode's chemistry link.

Transferred from ``precis_nm.handler`` by the nm→se merge
(docs/backlog/nm-se-merge.md), semantics untouched. They live here rather
than in :mod:`precis_se.ops` because they *read the store* (the source
``structure`` design has to exist, and its atom labels/elements have to
check out) — ``ops.py`` stays pure by design, so the handler intercepts
these before ``apply_ops`` ever sees them, the ``import_fragment``
precedent (``precis.handlers.structure::_apply_ops_with_imports``).
Neither writes: both only mutate the in-memory tree, so the caller's own
``persist.save_tree`` stays the single commit (see
:func:`precis_se.atomic.apply.apply_ops_with_atomic`'s docstring for why
``generate`` needs a different shape).

**Where the binding is stored.** On the block it is the *existing* se L3
binding pair — ``bound_kind='structure'`` + ``bound=<structure slug>``
(:data:`precis_se.ops._BINDING_KINDS`, migration
``0007_se_atomic.sql``'s widened CHECK), never a second block-level
column: "this block's realization is that design" is one fact, and se
already had the slot for it. Per *port* it is
``se_ports.bound_design``/``bound_atom`` — the atom-side projection of
one port fact, which the block-level pair cannot carry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.blocktree.types import OpError
from precis.errors import BadInput, NotFound
from precis_se.atomic.validate import envelope_fit
from precis_se.ops import SeTree, effective_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


def _block_not_found(tree: SeTree, name: str) -> str:
    """The ``no such block`` message with the live roster — the handler's
    own :func:`precis_se.handler._block_not_found` restated here so this
    module doesn't import the handler it is called from."""
    roster = ", ".join(sorted(tree.blocks)) or "(none)"
    return f"no such block: {name!r}. Blocks in this design: {roster}"


def _block_named(tree: SeTree, op: dict[str, Any], *, opname: str) -> tuple[str, Any]:
    """``op['block']`` → ``(label, node)``. These two ops are dispatched by
    the HANDLER (they need the store), not by ``apply_ops``, so they
    resolve the block token themselves rather than through the ops layer —
    but by the same rule, uid included (:meth:`precis_se.ops.SeTree.
    resolve_key`), and reporting in this layer's error vocabulary."""
    block = op.get("block")
    if not block or not str(block).strip():
        raise BadInput(f"{opname} needs 'block'")
    token = str(block).strip()
    try:
        key = tree.resolve_key(token)
    except OpError as exc:  # an ambiguous label, with its uid list
        raise BadInput(str(exc)) from exc
    if key is None:
        raise NotFound(_block_not_found(tree, token))
    return key, tree.blocks[key]


def bind_structure(store: Store, tree: SeTree, op: dict[str, Any]) -> str:
    """``{"op": "bind_structure", "block": <name>, "design": <structure
    slug>, "ports": {<port name>: <atom label>, ...} (optional)}`` — the L5
    binding (``se_blocks.bound_kind``/``bound`` + per-port
    ``se_ports.bound_design``/``bound_atom``, module docstring). Loads the
    source structure design via the same ``get_ref``/``structure_load``
    path ``handlers/structure.py::_import_fragment`` uses, then checks
    every mapped port exists on the block, every mapped atom label exists
    in the structure, and (the capability-gate philosophy: a loud failure
    at bind time, not a silent drift) each port's declared
    ``expected_element`` — when set — matches the bound atom's actual
    element. ``expected_hybridization`` is NOT gated here — it is
    display-only today (echoed in views and propose output), carried
    over unchanged from the pre-merge behavior; gating it is an open
    item in ``docs/backlog/se-atomic-round2.md``. Nothing is written
    until every mapped port passes.

    **Rebind semantics**: binding to a *different* design than the block's
    current ``bound`` first clears every existing port binding on the block
    (a full re-target — the old design's atom labels mean nothing in the
    new design's scene, so leaving them would strand stale ``bound_atom``
    values that validate would then look up in the wrong scene: a false
    ``dangling_binding``, or worse, a silently wrong element check on a
    label that happens to collide). Binding again to the *same* design is
    incremental — an earlier call's port map survives a later call that
    only maps additional (or different) ports, so a design can be filled
    in across several ``bind_structure`` calls.

    **``envelope_fit`` preflight** (:func:`precis_se.atomic.validate.
    envelope_fit`, the L1↔L5 agreement check): once the binding above
    succeeds, every atom in the just-bound ``scene`` is checked against the
    block's own declared envelope plus a vdW margin. This never blocks the
    bind (a hand-authored envelope is often a rough first guess) — a
    protrusion only appends a warning line to the returned echo, and the
    same check runs again, every future read, as ``view='validate'``'s
    ``envelope_fit`` warn-tier finding."""
    block_name, node = _block_named(tree, op, opname="bind_structure")
    if node.template is not None:
        raise BadInput(
            f"block {block_name!r} is an instance (of {node.template!r}) "
            "— an instance binds via its template; bind_structure on "
            f"{node.template!r} instead"
        )
    design = op.get("design")
    if not design or not str(design).strip():
        raise BadInput("bind_structure needs 'design' (the structure slug)")
    design_slug = str(design).strip()
    src_ref = store.get_ref(kind="structure", id=design_slug)
    if src_ref is None:
        roster = ", ".join(
            r.slug
            for r in store.list_refs(kind="structure", order_by="id_desc", limit=8)
            if r.slug
        )
        raise NotFound(
            f"no structure design {design_slug!r}",
            next=(
                f"known designs: {roster}"
                if roster
                else "put(kind='structure', id=..., ...) to create one first"
            ),
        )
    scene, _handles = store.structure_load(src_ref.id)
    ports_raw = op.get("ports")
    if ports_raw is None:
        ports_raw = {}
    if not isinstance(ports_raw, dict):
        raise BadInput(
            "bind_structure 'ports' must be a JSON object {port name: atom label}"
        )
    resolved: dict[str, str] = {}
    for port_name_raw, atom_label_raw in ports_raw.items():
        port_name = str(port_name_raw).strip()
        if port_name not in node.ports:
            roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
            raise NotFound(
                f"no such port on block {block_name!r}: {port_name!r}. "
                f"Available ports: {roster}"
            )
        atom_label = str(atom_label_raw).strip()
        atom = scene.atoms.get(atom_label)
        if atom is None:
            labels = sorted(scene.atoms)
            roster = ", ".join(labels[:8]) if labels else "(none)"
            more = "" if len(labels) <= 8 else f", … ({len(labels)} atoms total)"
            raise NotFound(
                f"no such atom in structure {design_slug!r}: "
                f"{atom_label!r}. Available atoms: {roster}{more}"
            )
        port = node.ports[port_name]
        if port.expected_element and port.expected_element != atom.element:
            raise BadInput(
                f"bind_structure: port {block_name}.{port_name} expects "
                f"element {port.expected_element!r}, but atom "
                f"{atom_label!r} in {design_slug!r} is {atom.element!r}"
            )
        resolved[port_name] = atom_label
    if node.bound_kind == "structure" and node.bound not in (None, design_slug):
        # Full re-target (docstring above): the old design's atom labels are
        # meaningless against the new scene, so every existing port binding
        # on this block is cleared before this call's map is applied — never
        # left stranded against a scene they were never bound to.
        for p in node.ports.values():
            p.bound_design = None
            p.bound_atom = None
    node.bound_kind = "structure"
    node.bound = design_slug
    for port_name, atom_label in resolved.items():
        p = node.ports[port_name]
        p.bound_design = design_slug
        p.bound_atom = atom_label
    mapped = ", ".join(f"{p}→{a}" for p, a in resolved.items())
    msg = f"bound block {block_name!r} to structure {design_slug!r}" + (
        f" (ports: {mapped})" if mapped else ""
    )
    # envelope_fit bind preflight: advisory, never blocks the bind — a
    # hand-authored envelope is often a rough first guess, and
    # view='validate' re-checks this every time the design is read
    # afterward anyway (the same "preflight note now, standing warn-tier
    # finding forever after" split ops.py's op-time gates and the read-time
    # re-check already use for everything else).
    env = effective_envelope(tree, node)
    if env:
        worst = envelope_fit(env, scene)
        if worst is not None:
            atom_label, protrusion = worst
            msg += (
                f"\n⚠ envelope_fit: atom {atom_label!r} protrudes "
                f"{protrusion:.3g} Å beyond block {block_name!r}'s "
                f"declared envelope {env!r} — widen the envelope, or "
                "this will keep surfacing as a validate finding"
            )
    return msg


def unbind_structure(tree: SeTree, op: dict[str, Any]) -> str:
    """``{"op": "unbind_structure", "block": <name>}`` — clears the block's
    ``structure`` binding (``bound_kind``/``bound``) and every one of its
    ports' ``bound_design``/``bound_atom``. Refuses a block bound to
    something that isn't chemistry: ``set_binding`` is the op for those,
    and silently clearing a ``component`` binding through the atomic verb
    would be a surprising write."""
    block_name, node = _block_named(tree, op, opname="unbind_structure")
    if node.bound_kind is not None and node.bound_kind != "structure":
        raise BadInput(
            f"block {block_name!r} is bound to a {node.bound_kind!r}, not a "
            "structure — unbind_structure only clears an atomic-mode "
            f"chemistry binding; use set_binding(block={block_name!r}) to "
            "change a realization binding of another kind"
        )
    node.bound_kind = None
    node.bound = None
    cleared = 0
    for p in node.ports.values():
        if p.bound_design is not None or p.bound_atom is not None:
            p.bound_design = None
            p.bound_atom = None
            cleared += 1
    return f"unbound block {block_name!r} ({cleared} port binding(s) cleared)"
