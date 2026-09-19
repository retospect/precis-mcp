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
overwrite) and :func:`unbind_structure` drops what it wrote. A port mapped
with the object form's ``axis_atom``/``phase_atom`` (R1, docs/backlog/
port-rotation-and-lever-composition.md) additionally measures a full
frame into ``rot``, and also carries ``se_ports.axis_atom``/
``phase_atom`` — the two extra atom labels, persisted alongside
``bound_atom`` and cleared with it (both mirror ``bound_atom``'s
lifecycle exactly).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.blocktree.types import OpError
from precis.errors import BadInput, NotFound
from precis.structure import Scene as StructScene
from precis.utils.units import format_quantity
from precis_se.atomic.validate import (
    PORT_POSE_MISMATCH_FRACTION,
    PORT_ROT_MISMATCH_RAD,
    FrameMismatch,
    PortFrame,
    bound_port_origin,
    envelope_diag_m,
    envelope_fit,
    frame_degeneracy_reason,
    frame_euler_rad,
    m_to_A,
    measured_port_frame,
    rot_mismatch_rad,
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


@dataclass
class _PortBind:
    """One port's resolved ``ports=`` mapping entry — the atom
    (``bind_structure``'s string form, or the object form's ``atom``) plus
    the optional ``axis_atom``/``phase_atom`` pair that additionally asks
    for a measured ``rot`` (R1, docs/backlog/
    port-rotation-and-lever-composition.md). ``frame`` is that
    measurement, computed once in the ``ports=`` parse loop — the
    degeneracy checks there already need it to decide whether to raise, so
    :func:`_measure_port_poses` reuses the answer rather than re-deriving
    it (no second store query, module docstring)."""

    atom: str
    axis_atom: str | None = None
    phase_atom: str | None = None
    frame: PortFrame | None = None


def _measure_port_poses(
    node: SeBlock,
    scene: StructScene,
    resolved: dict[str, _PortBind],
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

    **When the port also carries an ``axis_atom``/``phase_atom`` pair**
    (R1), the SAME rule extends to ``rot`` — but ``rot`` has its OWN
    provenance, :attr:`~precis.blocktree.types.Port.rot_source`,
    independent of ``pose_source``: a port can carry a ``'declared'``
    pose and a ``'bound'`` (or never-yet-measured) rot, or vice versa,
    and this function decides what to do with each half separately. A
    port mapped by the plain string form (or the object form's ``atom``
    alone) carries no :attr:`_PortBind.frame` and this never touches its
    ``rot`` either way, exactly as before R1.

    **A measurement never overwrites a declaration — per field.** A
    ``pose_source='declared'`` pose keeps its ``pose`` as stated: it is
    the target the realization gets checked *against*, and a disagreement
    past :data:`~precis_se.atomic.validate.PORT_POSE_MISMATCH_FRACTION`
    of the block's own envelope goes on this echo and the standing
    ``port_pose_mismatch`` finding. Independently, a ``rot_source=
    'declared'`` rot (or, absent one, a declared ``direction`` — which
    carries no provenance of its own and so is ALWAYS a comparison
    target, never a write) keeps its frame, compared instead
    (:func:`~precis_se.atomic.validate.rot_mismatch_rad`) against a past
    :data:`~precis_se.atomic.validate.PORT_ROT_MISMATCH_RAD` disagreement,
    reported the same way as ``port_rot_mismatch``. Neither comparison
    depends on the OTHER field's provenance — a declared pose with an
    empty (or ``'bound'``) rot still gets that rot filled/refreshed, and
    vice versa. An empty slot — or one already holding a ``'bound'``
    reading from an earlier bind, which this call's atoms supersede —
    simply takes the measurement: free information with nothing to
    conflict with.

    Nothing is measured when the scene doesn't share the block's frame
    (``envelope_fit``'s :class:`~precis_se.atomic.validate.FrameMismatch`):
    those coordinates are in some other frame, and reading them as
    block-local poses would mint numbers that look measured and mean
    nothing."""
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
    measured_rot: list[str] = []
    notes: list[str] = []
    for port_name, bind in sorted(resolved.items()):
        atom_label = bind.atom
        origin = bound_port_origin(scene, atom_label)
        if origin is None:  # pragma: no cover - the atom resolved above
            continue
        port = node.ports[port_name]
        # The pose half — unchanged from Decision 1, gated on pose_source
        # alone.
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
        else:
            port.pose = origin
            port.pose_source = "bound"
            measured.append(f"{port_name}→{atom_label}")
        # The rot half — its OWN gate, independent of what the pose half
        # just decided (the bug this branch split fixes: a declared pose
        # with no declared rot must still get the rot measured).
        if bind.frame is None:
            continue
        declared_rot = port.rot is not None and port.rot_source == "declared"
        declared_direction_only = port.rot is None and port.direction is not None
        if declared_rot or declared_direction_only:
            deviation = rot_mismatch_rad(port, bind.frame)
            if deviation is not None and deviation > PORT_ROT_MISMATCH_RAD:
                against = "rot" if declared_rot else "direction"
                notes.append(
                    f"\n⚠ port_rot_mismatch: {block_name}.{port_name} "
                    f"declares a {against} whose frame is "
                    f"{format_quantity(deviation, 'angle')} from the "
                    f"frame measured off {bind.axis_atom!r}/"
                    f"{bind.phase_atom!r} — the declared target is "
                    "kept; fix whichever is wrong with set_port_pose, "
                    "or move the atoms"
                )
        else:
            port.rot = frame_euler_rad(bind.frame)
            port.rot_source = "bound"
            measured_rot.append(port_name)
    if measured_rot:
        notes.insert(
            0,
            "\n· port rot measured (source='bound'): " + ", ".join(measured_rot),
        )
    if measured:
        notes.insert(
            0, "\n· port pose measured (source='bound'): " + ", ".join(measured)
        )
    return "".join(notes)


def bind_structure(store: Store, tree: SeTree, op: dict[str, Any]) -> str:
    """``{"op": "bind_structure", "block": <name>, "design": <structure
    slug>, "ports": {<port name>: <atom label> | {'atom': <label>,
    'axis_atom'?: <label>, 'phase_atom'?: <label>}, ...} (optional)}`` —
    the L5 binding (``se_blocks.bound_kind``/``bound`` + per-port
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

    **The object form additionally measures a frame** (R1, docs/backlog/
    port-rotation-and-lever-composition.md): ``axis_atom``/``phase_atom``
    are both-or-neither (one alone is a ``BadInput`` naming the port),
    ``axis_atom`` may not equal ``atom`` (a degenerate axle), and the
    triple must not be geometrically degenerate (``phase_atom`` collinear
    with the axle) — all three are gated here, before anything is
    written, same as every other bind-time check. See
    :func:`_measure_port_poses` for what the measured frame does to
    ``rot``.

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

    **Port poses (and, when mapped with the frame form, rots) are
    measured, declarations are not overwritten — per field**
    (:func:`_measure_port_poses`): every mapped port whose ``pose`` slot
    is empty (or holds an earlier bind's measurement) takes the atom's
    block-local origin as ``pose``/``pose_source='bound'``; INDEPENDENTLY,
    a port mapped with ``axis_atom``/``phase_atom`` whose ``rot`` slot is
    empty (or ``rot_source='bound'``) takes its axle's frame as ``rot``/
    ``rot_source='bound'`` — a declared pose does not block a rot
    measurement, and vice versa. A ``'declared'`` target (either field) is
    kept, and a disagreement bigger than the block's own scale (pose) or
    :data:`~precis_se.atomic.validate.PORT_ROT_MISMATCH_RAD` (rot) is
    reported here and by the standing ``port_pose_mismatch``/
    ``port_rot_mismatch`` findings.

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
    resolved: dict[str, _PortBind] = {}
    for port_name_raw, spec_raw in ports_raw.items():
        port_name = str(port_name_raw).strip()
        if port_name not in node.ports:
            roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
            raise NotFound(
                f"no such port on block {block_name!r}: {port_name!r}. "
                f"Available ports: {roster}"
            )
        # The object form (R1): {'atom': ..., 'axis_atom'?: ..., 'phase_atom'?:
        # ...} — 'atom' required, the other two both-or-neither. The plain
        # string form (the bound atom alone) still works unchanged.
        if isinstance(spec_raw, dict):
            atom_raw = spec_raw.get("atom")
            if not atom_raw or not str(atom_raw).strip():
                raise BadInput(
                    f"bind_structure: port {port_name!r}'s object form needs "
                    "'atom' (the bound atom label)"
                )
            atom_label = str(atom_raw).strip()
            axis_raw = spec_raw.get("axis_atom")
            phase_raw = spec_raw.get("phase_atom")
            has_axis = bool(axis_raw) and bool(str(axis_raw).strip())
            has_phase = bool(phase_raw) and bool(str(phase_raw).strip())
            if has_axis != has_phase:
                raise BadInput(
                    f"bind_structure: port {port_name!r} needs both "
                    "'axis_atom' and 'phase_atom' together (or neither) — "
                    "one alone cannot fix a frame"
                )
            axis_atom = str(axis_raw).strip() if has_axis else None
            phase_atom = str(phase_raw).strip() if has_phase else None
            if axis_atom is not None and axis_atom == atom_label:
                raise BadInput(
                    f"bind_structure: port {port_name!r}'s axis_atom "
                    f"{axis_atom!r} is the same atom as 'atom' — the "
                    "frame's z axis needs two distinct atoms"
                )
        else:
            atom_label = str(spec_raw).strip()
            axis_atom = phase_atom = None
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
        frame: PortFrame | None = None
        if axis_atom is not None and phase_atom is not None:
            for extra_label in (axis_atom, phase_atom):
                if scene.atoms.get(extra_label) is None:
                    labels = sorted(scene.atoms)
                    roster = ", ".join(labels[:8]) if labels else "(none)"
                    more = (
                        "" if len(labels) <= 8 else f", … ({len(labels)} atoms total)"
                    )
                    raise NotFound(
                        f"no such atom in structure {design_slug!r}: "
                        f"{extra_label!r}. Available atoms: {roster}{more}"
                    )
            frame = measured_port_frame(scene, atom_label, axis_atom, phase_atom)
            if frame is None:
                reason = frame_degeneracy_reason(
                    scene, atom_label, axis_atom, phase_atom
                )
                raise BadInput(
                    f"bind_structure: port {port_name!r}'s frame is "
                    f"degenerate — {reason or 'the three atoms cannot fix a frame'}"
                )
        resolved[port_name] = _PortBind(
            atom=atom_label, axis_atom=axis_atom, phase_atom=phase_atom, frame=frame
        )
    if node.bound_kind == "structure" and node.bound not in (None, design_slug):
        # Full re-target (docstring above): the old design's atom labels are
        # meaningless against the new scene, so every existing port binding
        # on this block is cleared before this call's map is applied — never
        # left stranded against a scene they were never bound to.
        for p in node.ports.values():
            p.bound_design = None
            p.bound_atom = None
            p.axis_atom = None
            p.phase_atom = None
            # Pose and rot drop independently, by their OWN provenance
            # (R1) — a measured value is a fact about the OLD scene; the
            # new design has said nothing about this port yet, and a
            # stale measurement reads exactly like a fresh one. A
            # declared value (either one) is design intent and survives.
            if p.pose_source == "bound":
                p.pose = p.pose_source = None
            if p.rot_source == "bound":
                p.rot = p.rot_source = None
    node.bound_kind = "structure"
    node.bound = design_slug
    for port_name, bind in resolved.items():
        p = node.ports[port_name]
        p.bound_design = design_slug
        p.bound_atom = bind.atom
        p.axis_atom = bind.axis_atom
        p.phase_atom = bind.phase_atom
    mapped = ", ".join(f"{p}→{b.atom}" for p, b in resolved.items())
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
    ports' ``bound_design``/``bound_atom``/``axis_atom``/``phase_atom`` —
    including any port pose/rot the bind itself measured, each dropped by
    its OWN provenance (``pose_source=='bound'``/``rot_source=='bound'``,
    R1 — independent stamps, so a port with a declared pose and only a
    measured rot drops just the rot): those numbers were a reading of a
    binding that no longer exists. A ``'declared'`` pose/rot is an agent's
    own design intent and survives untouched. Refuses a block bound to
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
    pose_unmeasured = 0
    rot_unmeasured = 0
    for p in node.ports.values():
        if (
            p.bound_design is not None
            or p.bound_atom is not None
            or p.axis_atom is not None
            or p.phase_atom is not None
        ):
            p.bound_design = None
            p.bound_atom = None
            p.axis_atom = None
            p.phase_atom = None
            cleared += 1
        # Pose and rot drop independently, by their OWN provenance (R1) —
        # a port bound with pose declared but only rot measured drops just
        # the rot, keeping the declared pose untouched, and vice versa.
        if p.pose_source == "bound":
            p.pose = p.pose_source = None
            pose_unmeasured += 1
        if p.rot_source == "bound":
            p.rot = p.rot_source = None
            rot_unmeasured += 1
    msg = f"unbound block {block_name!r} ({cleared} port binding(s) cleared)"
    if pose_unmeasured:
        msg += f", {pose_unmeasured} measured port pose(s) dropped"
    if rot_unmeasured:
        msg += f", {rot_unmeasured} measured port rot(s) dropped"
    return msg
