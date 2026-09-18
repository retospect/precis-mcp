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

A bind also *measures*: the atom a port resolves to has block-local
coordinates, which is exactly what the port pose slot holds, so
:func:`bind_structure` fills ``pose``/``pose_source='bound'`` on the ports
it maps (:func:`_measure_port_poses` for what it will and will not
overwrite) and :func:`unbind_structure` drops what it wrote.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from precis.blocktree.types import OpError
from precis.errors import BadInput, NotFound
from precis.structure import Scene as StructScene
from precis_se.atomic.validate import (
    PORT_POSE_MISMATCH_FRACTION,
    FrameMismatch,
    bound_port_origin,
    envelope_diag_m,
    envelope_fit,
    m_to_A,
)
from precis_se.ops import SeBlock, SeTree, effective_envelope

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


def _measure_port_poses(
    node: SeBlock,
    scene: StructScene,
    resolved: dict[str, str],
    *,
    block_name: str,
    design_slug: str,
    env: str | None,
    frame_mismatch: bool,
) -> str:
    """Fill each freshly-bound port's pose slot from its atom — the
    ``'bound'`` half of the port pose slot (gr342026,
    docs/backlog/port-pose-and-composition-search.md §Decision 1). A bound
    scene's atoms are in the block's own local frame (the identity-pose
    contract in :func:`precis_se.atomic.validate.envelope_fit`), so the
    resolved atom's Cartesian position *is* that port's origin there; the
    only work is the Å→m crossing (:func:`~precis_se.atomic.validate.
    bound_port_origin`).

    **A measurement never overwrites a declaration.** A port whose pose an
    agent stated (``pose_source='declared'``) keeps it: that is the target
    the realization gets checked *against*, and replacing it with what the
    realization did would destroy the only record of what was asked for —
    Decision 2's requirement/fact split, applied to the port slot. Where
    the two disagree by more than
    :data:`~precis_se.atomic.validate.PORT_POSE_MISMATCH_FRACTION` of the
    block's own envelope, the gap goes on this echo and, on every later
    read, into the ``port_pose_mismatch`` finding. An empty slot — or one
    already holding a ``'bound'`` pose from an earlier bind, which this
    call's atom supersedes — simply takes the measurement: free
    information with nothing to conflict with.

    Nothing is measured when the scene doesn't share the block's frame
    (``envelope_fit``'s :class:`~precis_se.atomic.validate.FrameMismatch`):
    those coordinates are in some other frame, and reading them as
    block-local poses would mint numbers that look measured and mean
    nothing. ``rot`` is never touched either way — an atom has a position,
    not an orientation."""
    if not resolved:
        return ""
    if frame_mismatch:
        return (
            f"\n⚠ port pose: not measured for {len(resolved)} port(s) — "
            f"{design_slug!r}'s atoms do not share block {block_name!r}'s "
            "frame (see below), so a pose read off them would be a number "
            "with no meaning"
        )
    diag_m = envelope_diag_m(env) if env else None
    measured: list[str] = []
    notes: list[str] = []
    for port_name, atom_label in sorted(resolved.items()):
        origin = bound_port_origin(scene, atom_label)
        if origin is None:  # pragma: no cover - the atom resolved above
            continue
        port = node.ports[port_name]
        if port.pose is not None and port.pose_source == "declared":
            gap_m = math.dist(origin, [float(c) for c in port.pose])
            if diag_m is not None and gap_m > PORT_POSE_MISMATCH_FRACTION * diag_m:
                notes.append(
                    f"\n⚠ port_pose_mismatch: {block_name}.{port_name} "
                    f"declares an origin {m_to_A(gap_m):.3g} Å away from "
                    f"bound atom {atom_label!r} — the declared target is "
                    "kept (a bind measures, it does not re-state intent); "
                    "fix whichever is wrong with set_port_pose, or move "
                    "the atom"
                )
            continue
        port.pose = origin
        port.pose_source = "bound"
        measured.append(f"{port_name}→{atom_label}")
    if measured:
        notes.insert(
            0, "\n· port pose measured (source='bound'): " + ", ".join(measured)
        )
    return "".join(notes)


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

    **Port poses are measured, declarations are not overwritten**
    (:func:`_measure_port_poses`): every mapped port whose slot is empty
    (or holds an earlier bind's measurement) takes the atom's block-local
    origin as ``pose``/``pose_source='bound'``; a ``'declared'`` target is
    kept, and a disagreement bigger than the block's own scale is reported
    here and by the standing ``port_pose_mismatch`` finding.

    **``envelope_fit`` preflight** (:func:`precis_se.atomic.validate.
    envelope_fit`, the L1↔L5 agreement check): once the binding above
    succeeds, every atom in the just-bound ``scene`` is checked against the
    block's own declared envelope plus a vdW margin. This never blocks the
    bind (a hand-authored envelope is often a rough first guess) — a
    protrusion only appends a warning line to the returned echo, and the
    same check runs again, every future read, as ``view='validate'``'s
    ``envelope_fit`` warn-tier finding. When the whole scene sits an
    envelope-width away (an imported structure with no local-frame
    alignment — gripe 334764,
    :class:`precis_se.atomic.validate.FrameMismatch`), the line says
    "cannot check — frames do not correspond" instead of advising a
    destructive envelope widen."""
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
            if p.pose_source == "bound":
                # A measured pose is a fact about the OLD scene; the new
                # design has said nothing about this port yet, and a
                # stale measurement reads exactly like a fresh one.
                p.pose = p.rot = p.pose_source = None
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
    # re-check already use for everything else). Its frame verdict is also
    # what decides whether a port pose can be measured at all, so it runs
    # before the measurement and both read the one answer.
    env = effective_envelope(tree, node)
    worst = envelope_fit(env, scene) if env else None
    msg += _measure_port_poses(
        node,
        scene,
        resolved,
        block_name=block_name,
        design_slug=design_slug,
        env=env,
        frame_mismatch=isinstance(worst, FrameMismatch),
    )
    if env:
        if isinstance(worst, FrameMismatch):
            # gripe 334764: an imported (from_smiles) scene carries no
            # alignment to the block's local frame — refuse loudly instead
            # of advising a destructive envelope widen.
            msg += (
                "\n⚠ envelope_fit: cannot check — frames do not "
                f"correspond: every atom of {design_slug!r} sits far "
                f"outside block {block_name!r}'s declared envelope "
                f"{env!r} (nearest atom {worst.nearest_label!r} is "
                f"{worst.clearance_A:.3g} Å out; the envelope is only "
                f"{worst.envelope_diag_A:.3g} Å across). Re-author the "
                "structure's atoms near the envelope's own origin "
                "(e.g. from_smiles offset=); do NOT widen the envelope"
            )
        elif worst is not None:
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
    ports' ``bound_design``/``bound_atom`` — including any port pose the
    bind itself measured (``pose_source='bound'``): that number was a
    reading of a binding that no longer exists. A ``'declared'`` pose is
    an agent's own design intent and survives untouched. Refuses a block
    bound to
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
    unmeasured = 0
    for p in node.ports.values():
        if p.bound_design is not None or p.bound_atom is not None:
            p.bound_design = None
            p.bound_atom = None
            cleared += 1
        if p.pose_source == "bound":
            p.pose = p.rot = p.pose_source = None
            unmeasured += 1
    msg = f"unbound block {block_name!r} ({cleared} port binding(s) cleared)"
    if unmeasured:
        msg += f", {unmeasured} measured port pose(s) dropped"
    return msg
