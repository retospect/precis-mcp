"""Atomic-mode feasibility findings over a loaded :class:`~precis_se.ops.
SeTree` — the read-time half of the atomic vocabulary, next to
:mod:`precis_se.atomic.vocab`'s write-time gates.

Transferred from ``precis_nm.validate`` by the nm→se merge
(docs/backlog/nm-se-merge.md) and retyped for se's tree/block/port
classes. Same shape as :mod:`precis_se.validate` (a rule/subject/detail
finding per row, error/warn/info tiers — its
:class:`~precis_se.validate.ValidationIssue` is reused directly, not
re-declared), and the same reason to exist: op-time validation only
protects data that went through ``apply_ops``, so a row that got there
some other way (hand correction, a future bug, direct persist-layer
manipulation) must still be caught, loudly, the next time anyone looks.
Nothing here mutates or gates a write.

**What moved and what did not.** Only the findings that are *about
chemistry* live here — the bond capability re-check, the L5
structure-binding checks, the bond-geometry sanity pair, and the
connect-graph cycle basis. Every rule nm's validator shared with se's own
(``dangling_connect``, ``unconnected_port``,
``block_without_envelope``, envelope interpenetration) stayed in
:mod:`precis_se.validate`, whose versions supersede them (the se
interpenetration check carries an AABB broad phase and a time budget nm's
never had); ``dangling_threading``/``threaded_without_envelope`` are
already :mod:`precis_se.drc`'s section 8 under the same rule names and
severities. One finding per problem, from one module — a merged kind that
reported the same overlap twice under two rule names would be worse than
either kind was alone.

Store-free, like every other checker: ``dangling_binding``/
``binding_element_mismatch`` need to know whether a bound ``structure``
design still resolves and what element its atoms are, and
:func:`envelope_fit` needs their real Cartesian positions, so the handler
hydrates both once in the view path (``bound_scenes``: slug →
``{atom_label: element}``, or ``None`` for a slug that no longer resolves;
``bound_full_scenes``: slug → the whole :class:`~precis.structure.Scene`)
and passes them into :func:`validate_atomic`.

:func:`envelope_fit` is the L1↔L5 agreement check — a block declares an
envelope (metres, design space) at L1 and holds real atoms (Å, the
structure enclave) at L5, and this models the *agreement* between the two
(the pcb-component-model precedent: "model the agreement, not the two
sides"), reporting the single worst-offending atom and how far it
protrudes. Its ``_A_TO_M``/``_M_TO_A`` pair is the **permanent**
structure-enclave crossing, explicitly not on the merge's seam kill list
(nm-se-merge.md "Explicitly NOT in scope") — it is pinned as an allowed
``1e-10`` site in ``tests/test_se_atomic_angstrom_seam.py``. Wired in two
places, both calling this one function: a bind preflight
(:func:`precis_se.atomic.bind.bind_structure`, advisory on the echo — a
hand-authored envelope is often a rough first guess, so it never blocks
the bind) and the warn-tier ``envelope_fit`` finding below, re-checked on
every read.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis.cad import dsl as cad_dsl
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Placed, Primitive
from precis.cad.relate import component_sdf
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import euler_rad_from_matrix
from precis.cad.vec import pose as cad_pose
from precis.cad.vec import rotation as cad_rotation
from precis.structure import Scene as StructScene
from precis.utils.units import format_quantity
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_se.atomic.vocab import bond_capability_offences, connect_role
from precis_se.ops import PortSpec, SeBlock, SeTree, effective_envelope, effective_ports
from precis_se.validate import ValidationIssue

#: The design↔atomistic seam conversion factor (`precis/utils/units.py`,
#: structure-unit-enclave.md): a block's ``envelope`` is design-space
#: cad-DSL text — canonical/storage mode, bare numbers in METRES (the
#: block tree's own internal unit) — while a bound ``structure`` scene's
#: atom coordinates stay Å (the enclave). Both sides of
#: :func:`envelope_fit`'s comparison are converted into the SAME frame
#: (metres, chosen here since ``envelope`` is already parsed as metres by
#: ``cad_dsl.build_config``) rather than mixing units inside one SDF call.
_A_TO_M = 1e-10
_M_TO_A = 1e10

#: :func:`envelope_fit`'s frame-correspondence gate (gripe 334764): when even
#: the *nearest* atom sits farther outside the envelope than this fraction of
#: the envelope's own bbox diagonal, the scene and the envelope do not share
#: a frame at all — generator-minted scenes emit atoms into the block's local
#: frame (the identity-pose contract in :func:`envelope_fit`'s docstring),
#: but an imported/``from_smiles`` scene's atoms land wherever its own cell
#: put them, and comparing the two answers nothing. Scale-relative per
#: docs/backlog/multiscale-design-architecture.md "Units policy" (a fixed Å
#: epsilon is nonsense across sub-nm..tens-of-nm blocks); 0.5 keeps a
#: genuine near-drift (atoms hugging the surface after an envelope shrink)
#: on the protrusion path while catching the dogfood's actual failure, where
#: the whole fragment sat an envelope-width away.
FRAME_MISMATCH_CLEARANCE_FRACTION = 0.5


@dataclass
class FrameMismatch:
    """:func:`envelope_fit`'s refusal outcome (gripe 334764): every atom sits
    grossly outside the envelope, so the two frames do not correspond and the
    fit question is unanswerable — the caller must say "cannot check", never
    "widen the envelope" (that advice would destroy a correct envelope)."""

    #: the atom closest to the envelope, and how far outside it sits (Å) —
    #: named so the refusal message stays concrete.
    nearest_label: str
    clearance_A: float
    #: the envelope's own bbox diagonal (Å) — the governing length the
    #: clearance was judged against.
    envelope_diag_A: float


def envelope_fit(
    envelope: str, scene: StructScene, *, margin_A: float = VDW_MARGIN_A
) -> tuple[str, float] | FrameMismatch | None:
    """The L1↔L5 agreement check itself (module docstring): does every atom
    of ``scene`` sit inside ``envelope`` (a ``cad`` mini-DSL config string —
    design-space canonical text, metres, see :data:`_A_TO_M`) plus
    ``margin_A`` (Å, an atomistic-scale constant — the enclave rule keeps
    this parameter self-naming its own unit) of headroom? Returns ``(worst
    atom label, protrusion_A)`` for the single worst-offending atom — the
    largest signed distance beyond the margin
    (:func:`~precis.cad.relate.component_sdf` is negative inside, so
    ``sdf - margin_m > 0`` is a genuine protrusion), reported back in Å
    (the atomistic scale this finding is about) — or ``None`` when every
    atom sits inside the margin, or when ``envelope`` fails to parse (a
    malformed envelope is a different finding's job, not this one's to
    raise on).

    **Or a :class:`FrameMismatch`** (gripe 334764) when even the nearest
    atom sits farther out than :data:`FRAME_MISMATCH_CLEARANCE_FRACTION` of
    the envelope's own bbox diagonal: the identity-pose contract below only
    holds for scenes authored in the block's local frame, and an imported
    (``from_smiles``) scene got no alignment step — a wholly-elsewhere atom
    cloud means the frames do not correspond, so the caller must refuse
    ("cannot check"), not report a protrusion whose "widen the envelope"
    advice would destroy a correct envelope.

    **Posed at identity, not the block's world pose/rot.** A block's
    envelope is declared in the block's own *local* frame — the same local
    frame every :mod:`precis_se.atomic.generators` builder emits atoms into
    (a ``cyl:r<>h<>`` envelope's ``z=0..h``, radially centered on the axis,
    matches a generated tube's own atom coordinates exactly, unshifted).
    ``node.pose``/``node.rot`` only place the block *within* the larger
    design (the frame ``view='clearance'`` poses envelopes into to check
    block-vs-block clearance) — applying them here would compare the bound
    scene's own local-frame atoms against an envelope translated/rotated
    into a different frame entirely, comparing two things that were never
    meant to line up."""
    try:
        prim = cad_dsl.build_config(envelope)  # design-space canonical: metres
    except cad_dsl.DslError:
        return None
    design = CadDesign()
    identity = cad_pose(cad_as_vec3([0.0, 0.0, 0.0]), cad_as_vec3([0.0, 0.0, 0.0]))
    design.add_component("_envelope_fit", design.prim("_envelope_fit", prim, identity))
    expr = design.components["_envelope_fit"]
    margin_m = margin_A * _A_TO_M
    worst_label: str | None = None
    worst_protrusion_m = 0.0
    nearest_label: str | None = None
    nearest_sdf_m = math.inf
    for label, atom in scene.atoms.items():
        cart_A = scene.cell.frac_to_cart(atom.frac)  # atomistic enclave: Å
        cart_m = cad_as_vec3([c * _A_TO_M for c in cart_A])
        sdf = component_sdf(design, expr, cart_m)
        protrusion_m = sdf - margin_m
        if protrusion_m > worst_protrusion_m:
            worst_protrusion_m = protrusion_m
            worst_label = label
        if sdf < nearest_sdf_m:
            nearest_sdf_m = sdf
            nearest_label = label
    if worst_label is None:
        return None
    # Frame-correspondence gate (gripe 334764, FRAME_MISMATCH_CLEARANCE_
    # FRACTION's docstring): only meaningful when EVERY atom protruded
    # (worst_label set AND the nearest atom is itself outside the margin) —
    # any atom genuinely inside proves the frames line up, and the finding
    # is then a real protrusion.
    diag_m = _envelope_diag(prim)
    if (
        nearest_label is not None
        and diag_m is not None
        and nearest_sdf_m - margin_m > FRAME_MISMATCH_CLEARANCE_FRACTION * diag_m
    ):
        return FrameMismatch(
            nearest_label=nearest_label,
            clearance_A=nearest_sdf_m * _M_TO_A,
            envelope_diag_A=diag_m * _M_TO_A,
        )
    return worst_label, worst_protrusion_m * _M_TO_A


# ── gripe 334768 — the azo-stick-5nm dogfood (2026-09-11): a design with a
# 5-block connect cycle closed head-to-tail, a 48.5 Å "covalent bond", and
# away-pointing bond-vector ports validated CLEANER than a correct design,
# because none of them were checked. The functions below add them;
# :func:`validate_atomic` folds their findings in. Every threshold here is
# a *fraction of a block's own envelope size* (the smaller of the two
# blocks involved), never an absolute figure —
# docs/backlog/multiscale-design-architecture.md "Units policy": an atomic
# design's blocks run from sub-nm to tens of nm, so a fixed epsilon tuned
# for one scale is nonsense at another; a governing-length fraction reads
# the same at every scale.
# ──────────────────────────────────────────────────────────────────────

#: :func:`_bond_length_findings`'s excess-gap threshold — a fraction of the
#: smaller connected block's own envelope bbox diagonal. A real port-to-port
#: bond sits close to both blocks' surfaces, so the pose-to-pose distance
#: minus each block's own envelope extent along that line should come out
#: near zero (or negative — a port often sits inside its nominal envelope);
#: 50% leaves generous headroom for the block-pose-not-port-position
#: approximation this check falls back to whenever a port's own pose slot
#: is null, while still catching the dogfood's 48.5 Å bond against
#: ~10s-of-Å blocks, whose residual gap is many multiples of either block's
#: own size. The SAME number bounds the exact port-to-port distance when
#: both ports do carry a pose: a real covalent bond is a small fraction of
#: a block, so half the smaller block's diagonal is as generous there.
BOND_GAP_FRACTION = 0.5

#: :func:`_bond_vector_findings`'s alignment threshold, radians off
#: perfectly anti-parallel (π). Two bonded ports both declare a
#: ``direction`` pointing outward from their own block along the bond, so a
#: real bond's two vectors are anti-parallel; 60° (radians internal per the
#: units-policy-cutover angle ruling) absorbs a genuinely bent approach
#: geometry while still catching the dogfood's actual failure (vectors
#: pointing away from each other — nowhere near anti-parallel).
BOND_VECTOR_MAX_DEVIATION_RAD = math.radians(60.0)

#: :func:`_port_pose_findings`'s drift threshold — a fraction of the
#: block's own envelope bbox diagonal, the same governing length the two
#: above are measured in. A ``'declared'`` port pose is a target an agent
#: stated at box level ("the far port sits 9 Å along x"); the bound atom
#: is where the realization actually put it, and the two are allowed to
#: differ by a bond length or so without either being wrong. A quarter of
#: the block's own diagonal is about that much on a ~10 Å block and grows
#: with the block; past it the target and the realization are describing
#: different points, which is the thing worth saying out loud.
PORT_POSE_MISMATCH_FRACTION = 0.25

#: :func:`rot_mismatch_rad`'s disagreement threshold (R1, docs/backlog/
#: port-rotation-and-lever-composition.md), radians — the axis-angle
#: between a port's declared frame (its ``rot``, or — absent one — the
#: angle between a declared ``direction`` and the measured z alone) and
#: the frame :func:`measured_port_frame` reads off ``axis_atom``/
#: ``phase_atom``. 0.175 rad (~10°) is the spec's own figure: unlike the
#: length thresholds above, an angle has no "fraction of the block" to
#: scale by, so this one constant covers every block size.
PORT_ROT_MISMATCH_RAD = 0.175


def _envelope_diag(prim: Primitive) -> float | None:
    """A primitive's own characteristic size — its local (unposed) AABB
    diagonal — the governing length every threshold above is a fraction
    of (multiscale-design-architecture.md "Units policy": bbox diagonal is
    the stated fallback governing length when there's no more specific
    feature size to hand). ``None`` for a degenerate/non-finite AABB (an
    unbounded primitive some future envelope shape might introduce) — the
    caller skips the finding rather than dividing by nonsense."""
    lo, hi = prim.aabb_local()
    diag = np.asarray(hi, dtype=float) - np.asarray(lo, dtype=float)
    if not np.all(np.isfinite(diag)):
        return None
    return float(np.linalg.norm(diag))


def envelope_diag_m(envelope: str) -> float | None:
    """An envelope's characteristic size in metres, from the DSL text —
    :func:`_envelope_diag` for the callers (:mod:`precis_se.atomic.bind`)
    that hold the string rather than the built primitive. ``None`` when
    the text doesn't parse or the AABB is degenerate: the caller skips its
    threshold rather than dividing by nonsense."""
    try:
        prim = cad_dsl.build_config(envelope)
    except cad_dsl.DslError:
        return None
    return _envelope_diag(prim)


def bound_port_origin(scene: StructScene, atom_label: str) -> list[float] | None:
    """The block-local origin, in METRES, of the atom a port is bound to —
    the measured half of the port pose slot (``pose_source='bound'``,
    :data:`~precis.blocktree.types.PORT_POSE_SOURCES`). A bound scene's
    atoms already sit in the block's own local frame by the identity-pose
    contract (:func:`envelope_fit`'s docstring), so the atom's Cartesian
    position *is* the port's origin there and this is the enclave crossing
    (Å → m, :data:`_A_TO_M`) and nothing else. ``None`` when the label is
    gone — ``dangling_binding`` is the finding for that, not this one's
    job to raise on."""
    atom = scene.atoms.get(atom_label)
    if atom is None:
        return None
    cart_A = scene.cell.frac_to_cart(atom.frac)
    return [float(c) * _A_TO_M for c in cart_A]


def m_to_A(value_m: float) -> float:
    """A design-space length said in the atomistic scale's own unit, for a
    *message*. The factor lives in this module by the seam allowlist
    (``tests/test_se_atomic_angstrom_seam.py``), so a caller that needs to
    report Å borrows this rather than spelling ``1e10`` somewhere new."""
    return value_m * _M_TO_A


#: A frame's unit x/y/z columns, each a plain 3-list — dimensionless (unit
#: vectors have no length unit to cross, unlike :func:`bound_port_origin`'s
#: origin), so this is the SAME frame whether read off Å or m coordinates.
PortFrame = tuple[list[float], list[float], list[float]]


def _frame_or_reason(
    scene: StructScene, atom_label: str, axis_atom_label: str, phase_atom_label: str
) -> tuple[PortFrame | None, str | None]:
    """The shared core of :func:`measured_port_frame` (the silent form,
    for a read-time caller that only wants the frame or ``None``) and
    :func:`frame_degeneracy_reason` (the naming form, for a bind-time
    caller that has to tell an agent WHICH atoms are the problem) — the
    geometry is computed once here so the two can never drift apart on
    what counts as degenerate.

    ``z`` is the unit vector ``atom_label -> axis_atom_label`` (the axle —
    a stator->rotor bond); ``x`` is the unit projection of ``atom_label ->
    phase_atom_label`` onto the plane normal to ``z`` (a rotor substituent
    fixes the roll); ``y`` is the cross product ``z`` × ``x``, completing
    a right-handed triad. Stacked as a matrix's COLUMNS ``(x, y, z)`` this
    is exactly what :func:`~precis.cad.vec.rotation` builds (its own
    columns are where it sends the local basis vectors), so
    :func:`~precis.cad.vec.euler_rad_from_matrix` inverts it directly.

    ``(None, reason)`` — never raises — when any label is missing from
    ``scene``, the axle is zero-length (``axis_atom`` coincides with
    ``atom_label``), ``phase_atom`` itself coincides with ``atom_label``
    (zero-length, nothing to project), or ``phase_atom`` is collinear with
    the axle (projected x-component under ``1e-3`` of the phase vector's
    own length) — the FOUR distinct ways this can fail, each its own
    ``reason`` string so a bind-time caller can name the actual problem
    rather than a generic "degenerate"."""
    atom = scene.atoms.get(atom_label)
    axis = scene.atoms.get(axis_atom_label)
    phase = scene.atoms.get(phase_atom_label)
    if atom is None or axis is None or phase is None:
        missing = next(
            label
            for label, resolved in (
                (atom_label, atom),
                (axis_atom_label, axis),
                (phase_atom_label, phase),
            )
            if resolved is None
        )
        return None, f"atom {missing!r} not found in the structure"
    p0 = scene.cell.frac_to_cart(atom.frac)
    p_axis = scene.cell.frac_to_cart(axis.frac)
    p_phase = scene.cell.frac_to_cart(phase.frac)
    v_axis = p_axis - p0
    axis_len = float(np.linalg.norm(v_axis))
    if axis_len < 1e-9:
        return None, (
            f"axis_atom {axis_atom_label!r} coincides with atom "
            f"{atom_label!r} — the axle has zero length"
        )
    z = v_axis / axis_len
    v_phase = p_phase - p0
    phase_len = float(np.linalg.norm(v_phase))
    if phase_len < 1e-9:
        return None, (
            f"phase_atom {phase_atom_label!r} coincides with atom "
            f"{atom_label!r} — there is no direction left to fix the roll"
        )
    x_raw = v_phase - np.dot(v_phase, z) * z
    if float(np.linalg.norm(x_raw)) < 1e-3 * phase_len:
        return None, (
            f"phase_atom {phase_atom_label!r} is collinear with the axle "
            f"{atom_label!r}→{axis_atom_label!r} — nothing fixes the roll"
        )
    x = x_raw / np.linalg.norm(x_raw)
    y = np.cross(z, x)
    frame: PortFrame = (
        [float(v) for v in x],
        [float(v) for v in y],
        [float(v) for v in z],
    )
    return frame, None


def measured_port_frame(
    scene: StructScene, atom_label: str, axis_atom_label: str, phase_atom_label: str
) -> PortFrame | None:
    """The port frame R1 measures (docs/backlog/
    port-rotation-and-lever-composition.md) — see :func:`_frame_or_reason`
    for the geometry. ``None`` — never raises — when any label is missing
    from ``scene`` or the frame is degenerate: the caller decides whether
    that is loud (bind time, :func:`precis_se.atomic.bind.bind_structure`,
    which uses :func:`frame_degeneracy_reason` instead to name the atoms)
    or silent (read time — a degenerate stored row contributes nothing to
    a finding rather than raising on a read)."""
    frame, _reason = _frame_or_reason(
        scene, atom_label, axis_atom_label, phase_atom_label
    )
    return frame


def frame_degeneracy_reason(
    scene: StructScene, atom_label: str, axis_atom_label: str, phase_atom_label: str
) -> str | None:
    """Why :func:`measured_port_frame` returned ``None`` for these three
    atoms, naming which of the four ways it failed (module-level
    :func:`_frame_or_reason`) — the bind-time counterpart to
    :func:`measured_port_frame`'s silence, for
    :func:`precis_se.atomic.bind.bind_structure`'s ``BadInput``. ``None``
    when the frame is not, in fact, degenerate (a caller error — should
    never happen when :func:`measured_port_frame` returned ``None`` for
    the same three labels)."""
    _frame, reason = _frame_or_reason(
        scene, atom_label, axis_atom_label, phase_atom_label
    )
    return reason


def frame_euler_rad(frame: PortFrame) -> list[float]:
    """``frame``'s ``(x, y, z)`` columns as an ``[rx, ry, rz]`` Euler
    triple, se's storage shape for ``rot`` (module docstring's ``Rz@Ry@Rx``
    convention, mirrored from :func:`~precis.cad.vec.rotation`)."""
    x, y, z = frame
    R = np.column_stack([x, y, z])
    return list(euler_rad_from_matrix(R))


def rot_mismatch_rad(port: PortSpec, frame: PortFrame) -> float | None:
    """The axis-angle (radians) between ``port``'s DECLARED frame and the
    MEASURED ``frame`` (:func:`measured_port_frame`) — the rot half of
    :func:`_port_pose_findings`'s comparison, shared by the bind-time echo
    (:func:`precis_se.atomic.bind._measure_port_poses`) and the standing
    ``port_rot_mismatch`` finding (:func:`_port_rot_findings`) so the two
    can never drift onto different arithmetic.

    A declared ``rot`` (Euler radians) is compared as a full frame:
    ``angle = arccos((trace(R_declared^T @ R_measured) - 1) / 2)`` — the
    standard rotation-matrix geodesic distance. Absent a declared ``rot``
    but present a declared ``direction`` (never itself measured — module
    docstring), only the measured z is compared against it, same formula
    specialized to two unit vectors. ``None`` when the port declares
    neither (nothing to compare) or a declared ``direction`` is the zero
    vector (a malformed row, not this function's job to raise on)."""
    x, y, z = frame
    if port.rot is not None:
        R_declared = cad_rotation(*(float(v) for v in port.rot)).R
        R_measured = np.column_stack([x, y, z])
        cos = (float(np.trace(R_declared.T @ R_measured)) - 1.0) / 2.0
        return math.acos(float(np.clip(cos, -1.0, 1.0)))
    if port.direction is not None:
        d = np.asarray(port.direction, dtype=float)
        d_norm = float(np.linalg.norm(d))
        if d_norm < 1e-12:
            return None
        cos = float(np.dot(d / d_norm, np.asarray(z, dtype=float)))
        return math.acos(float(np.clip(cos, -1.0, 1.0)))
    return None


def _extent_along(lo: object, hi: object, unit: object) -> float:
    """The support-function half-width of the world AABB ``(lo, hi)`` along
    unit direction ``unit`` — the exact projected half-extent of an
    axis-aligned box along an arbitrary direction, and thus an upper bound
    on a (possibly non-box) primitive's own extent along that line, since
    the primitive is fully contained in its AABB."""
    half = (np.asarray(hi, dtype=float) - np.asarray(lo, dtype=float)) / 2.0
    return float(np.dot(half, np.abs(np.asarray(unit, dtype=float))))


def _cycle_path(
    u: str, v: str, parent: dict[str, str | None], depth: dict[str, int]
) -> list[str]:
    """Reconstruct the cycle closed by the back-edge ``u—v`` in a BFS
    spanning tree (``parent``/``depth`` from that tree, keyed by block
    name): walk both endpoints up to their lowest common ancestor, then
    splice the two walks into one closed loop ``[u, ..., lca, ..., v, u]``."""
    path_u, path_v = [u], [v]
    while depth[u] > depth[v]:
        parent_u = parent[u]
        assert parent_u is not None
        u = parent_u
        path_u.append(u)
    while depth[v] > depth[u]:
        parent_v = parent[v]
        assert parent_v is not None
        v = parent_v
        path_v.append(v)
    while u != v:
        parent_u = parent[u]
        parent_v = parent[v]
        assert parent_u is not None and parent_v is not None
        u, v = parent_u, parent_v
        path_u.append(u)
        path_v.append(v)
    return path_u + list(reversed(path_v[:-1])) + [path_u[0]]


def _connect_cycle_findings(tree: SeTree) -> list[ValidationIssue]:
    """``connect_cycle`` (warn) — the **atomic** connect graph (nodes:
    block names; edges: every live connect carrying a ``kind``, by index so
    a genuine parallel bond between the same two blocks counts as its own
    2-cycle too) is not acyclic. The block *tree* is parent-child by
    construction, but connects between ports can close a loop across it
    (the dogfood's 5-block chain connected head-to-tail) — a macrocycle IS
    real chemistry (a crown ether, a ring polymer), so this never says
    "forbidden", only "verify this is intended" (warn, not error). One
    finding per independent cycle in a spanning-tree cycle basis (standard
    graph-theory technique: BFS each connected component, and every
    non-tree edge closes exactly one cycle against the tree already built)
    — not an exhaustive enumeration of every cycle a denser graph might
    contain, which is combinatorial; a basis names every independent loop
    at least once, which is what "verify this is intended" needs.

    **Kind-bearing edges only** (the merge's own narrowing): a closed loop
    of *structural* se connects is a truss, the most ordinary thing in the
    kind — :mod:`precis_se.stability` is what reads those rings, and a warn
    on every triangle would be noise. Only a ring of chemistry edges
    asserts the macrocycle this check asks about."""
    edges = [
        (c.a_block, c.b_block)
        for c in tree.connects
        if c.kind is not None
        and c.a_block in tree.blocks
        and c.b_block in tree.blocks
        and c.a_block != c.b_block
    ]
    if not edges:
        return []
    adj: dict[str, list[tuple[str, int]]] = {}
    for idx, (a, b) in enumerate(edges):
        adj.setdefault(a, []).append((b, idx))
        adj.setdefault(b, []).append((a, idx))
    visited: set[str] = set()
    seen_edges: set[int] = set()
    findings: list[ValidationIssue] = []
    for start in sorted(adj):
        if start in visited:
            continue
        visited.add(start)
        parent: dict[str, str | None] = {start: None}
        parent_edge: dict[str, int | None] = {start: None}
        depth: dict[str, int] = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            u = queue.popleft()
            for v, eidx in adj[u]:
                if eidx == parent_edge[u]:
                    continue  # the tree edge we arrived on — not a cycle
                if v not in visited:
                    visited.add(v)
                    parent[v] = u
                    parent_edge[v] = eidx
                    depth[v] = depth[u] + 1
                    queue.append(v)
                    continue
                if eidx in seen_edges:
                    continue
                seen_edges.add(eidx)
                path = _cycle_path(u, v, parent, depth)
                path_str = "—".join(path)
                findings.append(
                    ValidationIssue(
                        rule="connect_cycle",
                        subject=path_str,
                        detail=(
                            f"connect cycle: {path_str} — verify this is an "
                            "intended macrocycle, not an accidental closure"
                        ),
                        severity="warn",
                    )
                )
    return findings


def _port_world_origin(
    tree: SeTree, node: SeBlock, port_name: str
) -> NDArray[np.float64] | None:
    """The world-space origin of ``node``'s ``port_name``, or ``None`` when
    that port carries no stored pose of its own (the nullable slot's normal
    case, :class:`~precis.blocktree.types.Port`). The port's pose is in the
    block's LOCAL frame — the same convention ``direction``/``envelope``
    use (:func:`envelope_fit`) — so the block's own placement maps it
    out."""
    port = effective_ports(tree, node).get(port_name)
    if port is None or port.pose is None:
        return None
    placement = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    return np.asarray(placement.apply(cad_as_vec3(port.pose)), dtype=float)


def _port_pose_source(tree: SeTree, node: SeBlock, port_name: str) -> str:
    """The provenance stamp on that port's stored pose (``'declared'`` /
    ``'bound'``), for the finding to name — a target the realization is
    checked against reads differently from one measured off it."""
    port = effective_ports(tree, node).get(port_name)
    return (port.pose_source if port is not None else None) or "?"


def _bond_length_findings(tree: SeTree) -> list[ValidationIssue]:
    """``bond_length_sanity`` (warn) — a ``kind='bond'`` connect whose two
    endpoints sit wildly further apart than a plausible bond. Measured two
    ways, and the finding always says which one it used:

    * both ports carry a stored ``pose`` → the real **port-to-port** world
      distance, no approximation anywhere in it;
    * otherwise → the blocks' pose-to-pose distance minus each block's own
      envelope extent along that line, which is an approximation: those
      ports have no position of their own, only their block's.

    Same scale-relative threshold either way. Never gates (``warn``): the
    approximation can read long for a legitimate reason (a bent/off-axis
    port), so this only ever flags for a human/agent to look again, the
    same trust level ``port_capability`` extends to declared roles."""
    findings: list[ValidationIssue] = []
    for c in tree.connects:
        if c.kind != "bond":
            continue
        a_node = tree.blocks.get(c.a_block)
        b_node = tree.blocks.get(c.b_block)
        if a_node is None or b_node is None:
            continue  # dangling_connect already covers this endpoint
        a_env = effective_envelope(tree, a_node)
        b_env = effective_envelope(tree, b_node)
        if not a_env or not b_env:
            continue
        try:
            a_prim = cad_dsl.build_config(a_env)
            b_prim = cad_dsl.build_config(b_env)
        except cad_dsl.DslError:
            continue
        a_diag = _envelope_diag(a_prim)
        b_diag = _envelope_diag(b_prim)
        if a_diag is None or b_diag is None:
            continue
        threshold = BOND_GAP_FRACTION * min(a_diag, b_diag)
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        # Preferred measurement: both ports know where they are, so the
        # distance is the real port-to-port one, with no approximation in
        # it at all. The envelope-extent projection below is the fallback
        # for the (normal, by-design) case where the slot is null.
        a_origin = _port_world_origin(tree, a_node, c.a_port)
        b_origin = _port_world_origin(tree, b_node, c.b_port)
        if a_origin is not None and b_origin is not None:
            span = float(np.linalg.norm(b_origin - a_origin))
            if span <= threshold:
                continue
            a_src = _port_pose_source(tree, a_node, c.a_port)
            b_src = _port_pose_source(tree, b_node, c.b_port)
            findings.append(
                ValidationIssue(
                    rule="bond_length_sanity",
                    subject=subject,
                    detail=(
                        f"port-to-port distance {span:.3g} m (both ports "
                        f"carry a stored pose: a={a_src}, b={b_src}) "
                        f"exceeds the {threshold:.3g} m scale-relative "
                        "threshold for a plausible bond — not a chemically "
                        "real covalent bond at this distance"
                    ),
                    severity="warn",
                )
            )
            continue
        a_pos = np.asarray(a_node.pose, dtype=float)
        b_pos = np.asarray(b_node.pose, dtype=float)
        delta = b_pos - a_pos
        distance = float(np.linalg.norm(delta))
        # Scale-relative, not a fixed 1e-9 (`precis/utils/units.py`'s
        # relative-tolerance audit): a fraction of the smaller block's own
        # envelope size (this module's governing-length convention) reads
        # the same at every scale, where 1e-9 m is negligible at Å scale
        # and a whole nanometre at nm-design scale.
        if distance <= 1e-9 * min(a_diag, b_diag):
            continue  # coincident poses — nothing to project an axis onto
        unit = delta / distance
        a_lo, a_hi = Placed(
            a_prim, cad_pose(cad_as_vec3(a_node.pose), cad_as_vec3(a_node.rot))
        ).aabb()
        b_lo, b_hi = Placed(
            b_prim, cad_pose(cad_as_vec3(b_node.pose), cad_as_vec3(b_node.rot))
        ).aabb()
        gap = (
            distance - _extent_along(a_lo, a_hi, unit) - _extent_along(b_lo, b_hi, unit)
        )
        if gap <= threshold:
            continue
        findings.append(
            ValidationIssue(
                rule="bond_length_sanity",
                subject=subject,
                detail=(
                    f"block-pose gap ≈{gap:.3g} m (pose distance {distance:.3g} "
                    f"m minus each block's own envelope extent along the "
                    f"line — an approximation: these ports carry no stored "
                    f"pose of their own, only their block's pose) exceeds "
                    f"the {threshold:.3g} m scale-relative threshold for a "
                    "plausible bond — not a chemically real covalent bond "
                    "at this distance"
                ),
                severity="warn",
            )
        )
    return findings


def _bond_vector_findings(tree: SeTree) -> list[ValidationIssue]:
    """``bond_vector_alignment`` (warn) — a ``kind='bond'`` connect whose
    two ports both declare a ``direction`` (skipped when either doesn't —
    ``direction`` is optional), but those vectors are far from anti
    -parallel (:data:`BOND_VECTOR_MAX_DEVIATION_RAD` off 180°). A bonded
    port's ``direction`` is meant to point outward along the bond axis, so
    two real bond partners point at each other, not away — the dogfood's
    actual failure mode, which proved these vectors are pure decoration
    unless something reads them.

    ``direction`` is declared in the block's own LOCAL frame — the same
    convention ``envelope`` uses (:func:`envelope_fit`'s docstring, "Posed
    at identity, not the block's world pose/rot") — so each vector is
    rotated into world frame via the block's own pose/rot
    (:meth:`~precis.cad.vec.Transform.apply_dir`, rotation only — no
    translation for a direction) before the dot product; comparing the raw
    stored vectors would silently misjudge every rotated block (a spurious
    warn on a genuinely anti-parallel pair, or a miss on a genuinely
    misaligned one)."""
    findings: list[ValidationIssue] = []
    for c in tree.connects:
        if c.kind != "bond":
            continue
        a_node = tree.blocks.get(c.a_block)
        b_node = tree.blocks.get(c.b_block)
        if a_node is None or b_node is None:
            continue
        a_spec = effective_ports(tree, a_node).get(c.a_port)
        b_spec = effective_ports(tree, b_node).get(c.b_port)
        if (
            a_spec is None
            or b_spec is None
            or a_spec.direction is None
            or b_spec.direction is None
        ):
            continue
        a_xform = cad_pose(cad_as_vec3(a_node.pose), cad_as_vec3(a_node.rot))
        b_xform = cad_pose(cad_as_vec3(b_node.pose), cad_as_vec3(b_node.rot))
        a_dir = np.asarray(
            a_xform.apply_dir(cad_as_vec3(a_spec.direction)), dtype=float
        )
        b_dir = np.asarray(
            b_xform.apply_dir(cad_as_vec3(b_spec.direction)), dtype=float
        )
        cos = float(np.clip(np.dot(a_dir, b_dir), -1.0, 1.0))
        angle_rad = math.acos(cos)
        deviation = math.pi - angle_rad
        if deviation <= BOND_VECTOR_MAX_DEVIATION_RAD:
            continue
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        findings.append(
            ValidationIssue(
                rule="bond_vector_alignment",
                subject=subject,
                detail=(
                    f"port direction vectors are {format_quantity(angle_rad, 'angle')} "
                    "apart (expect ≈180°, anti-parallel, within "
                    f"{format_quantity(BOND_VECTOR_MAX_DEVIATION_RAD, 'angle')} — a "
                    "bonded port's direction points outward along the bond) — "
                    f"{c.a_block}.{c.a_port} and {c.b_block}.{c.b_port} "
                    "don't point at each other"
                ),
                severity="warn",
            )
        )
    return findings


def _port_capability_findings(tree: SeTree) -> list[ValidationIssue]:
    """``port_capability`` (error, defense in depth) — a stored
    ``kind='bond'`` connect whose endpoints don't satisfy its role
    (:func:`~precis_se.atomic.vocab.connect_role`): both affording a
    symmetric role, or one affording each half of a complementary one
    (:func:`~precis_se.atomic.vocab.bond_capability_offences` is the single
    rule, shared with the op). ``ops.py``'s connect op already gates this
    at write time; this re-checks whatever ended up stored.

    A connect with a dangling endpoint is skipped: there's no ``PortSpec``
    to read roles off, and :mod:`precis_se.validate`'s own
    ``dangling_connect`` already reports it — one finding per problem.

    This re-checks the same *declared* roles the op gated on; it never
    independently verifies a role against real chemistry (the
    pcb-component-model trust model: capability labelling, not proof), so a
    finding here means the stored connect is inconsistent with its own
    endpoints' labels, not that the bond is chemically implausible."""
    findings: list[ValidationIssue] = []
    for c in tree.connects:
        a_node = tree.blocks.get(c.a_block)
        b_node = tree.blocks.get(c.b_block)
        a_spec = (
            effective_ports(tree, a_node).get(c.a_port) if a_node is not None else None
        )
        b_spec = (
            effective_ports(tree, b_node).get(c.b_port) if b_node is not None else None
        )
        if a_spec is None or b_spec is None:
            continue  # dangling_connect's finding, not this one's
        role = connect_role(c.kind, c.objectives)
        if role is None:
            continue
        offences = bond_capability_offences(
            c.a_block, c.a_port, a_spec, c.b_block, c.b_port, b_spec, role
        )
        if not offences:
            continue
        findings.append(
            ValidationIssue(
                rule="port_capability",
                subject=f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}",
                detail="; ".join(offences),
                severity="error",
            )
        )
    return findings


def _binding_findings(
    tree: SeTree, bound_scenes: dict[str, dict[str, str] | None]
) -> list[ValidationIssue]:
    """``dangling_binding`` (error) + ``binding_element_mismatch`` (warn) —
    the bind-time capability gate (:func:`precis_se.atomic.bind.
    bind_structure`) re-checked against currently-hydrated scene data,
    defense in depth like :func:`_port_capability_findings`. An instance
    never owns a binding of its own (``bind_structure`` rejects it — bind
    via the template), so only an ordinary block's own ``bound``/ports are
    ever the subject. A slug the caller didn't hydrate is skipped rather
    than guessed at."""
    findings: list[ValidationIssue] = []
    for node in tree.blocks.values():
        if node.template is not None or node.bound_kind != "structure":
            continue
        slug = node.bound
        if slug is None or slug not in bound_scenes:
            continue  # caller didn't hydrate this slug — skip, don't guess
        atoms = bound_scenes[slug]
        if atoms is None:
            findings.append(
                ValidationIssue(
                    rule="dangling_binding",
                    subject=node.name,
                    detail=(
                        f"block {node.name!r} is bound to structure design "
                        f"{slug!r}, which no longer resolves — "
                        "bind_structure again, or unbind_structure"
                    ),
                    severity="error",
                )
            )
            continue
        for port in node.ports.values():
            if port.bound_atom is None:
                continue
            element = atoms.get(port.bound_atom)
            if element is None:
                findings.append(
                    ValidationIssue(
                        rule="dangling_binding",
                        subject=f"{node.name}.{port.name}",
                        detail=(
                            f"bound atom {port.bound_atom!r} no longer "
                            f"exists in structure design {port.bound_design!r} "
                            "— rebind, or unbind_structure"
                        ),
                        severity="error",
                    )
                )
                continue
            if port.expected_element and port.expected_element != element:
                findings.append(
                    ValidationIssue(
                        rule="binding_element_mismatch",
                        subject=f"{node.name}.{port.name}",
                        detail=(
                            f"port expects element {port.expected_element!r}, "
                            f"bound atom {port.bound_atom!r} is {element!r}"
                        ),
                        severity="warn",
                    )
                )
    return findings


def _envelope_fit_findings(
    tree: SeTree, bound_full_scenes: dict[str, StructScene]
) -> list[ValidationIssue]:
    """``envelope_fit`` (warn) — the L1↔L5 agreement check (module
    docstring): a bound block's realized atoms should sit inside its
    declared envelope plus a vdW margin. Only an ordinary, bound block with
    a resolvable effective envelope is ever the subject — a block with no
    envelope has nothing to check against (``block_without_envelope``
    covers that gap), and a slug the caller didn't hydrate is skipped
    rather than guessed at (a dangling design is already reported once, by
    :func:`_binding_findings`)."""
    findings: list[ValidationIssue] = []
    for node in tree.blocks.values():
        if node.template is not None or node.bound_kind != "structure":
            continue
        env = effective_envelope(tree, node)
        if not env or node.bound is None:
            continue
        scene = bound_full_scenes.get(node.bound)
        if scene is None:
            continue
        worst = envelope_fit(env, scene)
        if worst is None:
            continue
        if isinstance(worst, FrameMismatch):
            findings.append(
                ValidationIssue(
                    rule="envelope_fit",
                    subject=node.name,
                    detail=(
                        "cannot check — frames do not correspond: every "
                        f"atom of bound structure {node.bound!r} sits far "
                        f"outside block {node.name!r}'s declared envelope "
                        f"{env!r} (nearest atom {worst.nearest_label!r} is "
                        f"{worst.clearance_A:.3g} Å out; the envelope is "
                        f"only {worst.envelope_diag_A:.3g} Å across). An "
                        "imported structure carries no alignment to the "
                        "block's local frame — re-author its atoms near "
                        "the envelope's own origin (e.g. from_smiles "
                        "offset=); do NOT widen the envelope"
                    ),
                    severity="warn",
                )
            )
            continue
        atom_label, protrusion = worst
        findings.append(
            ValidationIssue(
                rule="envelope_fit",
                subject=node.name,
                detail=(
                    f"atom {atom_label!r} (in bound structure "
                    f"{node.bound!r}) protrudes {protrusion:.3g} Å "
                    f"beyond block {node.name!r}'s declared envelope "
                    f"{env!r} (+{VDW_MARGIN_A:g} Å vdW margin) — the L1 "
                    "envelope and the L5 realized atoms have drifted "
                    "apart; widen the envelope or rebind"
                ),
                severity="warn",
            )
        )
    return findings


def _port_pose_findings(
    tree: SeTree, bound_full_scenes: dict[str, StructScene]
) -> list[ValidationIssue]:
    """``port_pose_mismatch`` (warn) — a port carrying a ``'declared'``
    target pose whose bound atom sits somewhere else entirely.

    A bind never overwrites a declared target (:func:`precis_se.atomic.
    bind.bind_structure`: the target is a requirement, the atom is the
    fact, and the port slot holds one of them — the same split Decision 2
    makes for the star schema), so a disagreement has to be *reported*
    rather than resolved. It is, twice: once on the bind echo, and then by
    this check on every later read, because the bound structure can be
    edited under a standing binding long after the bind returned (the
    "preflight note now, standing finding forever after" split
    ``envelope_fit`` already uses). Warn, never error — a box-level target
    is a rough statement of intent, and this only ever asks a human to
    look again.

    Skipped when the scene doesn't share the block's frame at all: the
    ``envelope_fit`` finding says that once for the whole block, and one
    of these per port would be that same fact repeated in a worse unit."""
    findings: list[ValidationIssue] = []
    for node in tree.blocks.values():
        if node.template is not None or node.bound_kind != "structure":
            continue
        env = effective_envelope(tree, node)
        if not env or node.bound is None:
            continue
        candidates = [
            p
            for p in node.ports.values()
            if p.pose is not None
            and p.pose_source == "declared"
            and p.bound_atom is not None
        ]
        if not candidates:
            continue
        diag_m = envelope_diag_m(env)
        scene = bound_full_scenes.get(node.bound)
        if diag_m is None or scene is None:
            continue
        if isinstance(envelope_fit(env, scene), FrameMismatch):
            continue
        for port in candidates:
            assert port.bound_atom is not None and port.pose is not None
            origin = bound_port_origin(scene, port.bound_atom)
            if origin is None:
                continue  # dangling_binding owns the vanished label
            gap_m = math.dist(origin, [float(c) for c in port.pose])
            if gap_m <= PORT_POSE_MISMATCH_FRACTION * diag_m:
                continue
            findings.append(
                ValidationIssue(
                    rule="port_pose_mismatch",
                    subject=f"{node.name}.{port.name}",
                    detail=(
                        f"declared origin sits {m_to_A(gap_m):.3g} Å from "
                        f"bound atom {port.bound_atom!r} (in structure "
                        f"{node.bound!r}) — more than "
                        f"{PORT_POSE_MISMATCH_FRACTION:g} of block "
                        f"{node.name!r}'s own "
                        f"{m_to_A(diag_m):.3g} Å envelope. The declared "
                        "target is kept: fix whichever is wrong "
                        "(set_port_pose, or move the atom)"
                    ),
                    severity="warn",
                )
            )
    return findings


def _port_rot_findings(
    tree: SeTree, bound_full_scenes: dict[str, StructScene]
) -> list[ValidationIssue]:
    """``port_rot_mismatch`` (warn) — :func:`_port_pose_findings`'s
    counterpart for the frame half of the port pose slot (R1, docs/backlog/
    port-rotation-and-lever-composition.md): a port whose declared ``rot``
    (or, absent one, a declared ``direction``) disagrees with the frame
    :func:`measured_port_frame` reads off its ``axis_atom``/``phase_atom``
    by more than :data:`PORT_ROT_MISMATCH_RAD`. Re-derived from the
    hydrated scene on every read — the bind echo says it once, this says
    it again on every later read, because the bound structure can change
    under a standing binding long after the bind returned (the same
    "preflight note now, standing finding forever after" split
    ``envelope_fit``/``port_pose_mismatch`` already use).

    Only a port carrying the full atom/axis_atom/phase_atom triple AND a
    declared frame (``rot_source=='declared'``, or — absent a declared
    ``rot`` — a declared ``direction``, which carries no provenance of its
    own) is ever a candidate — gated on ``rot``'s OWN provenance, NOT
    ``pose_source`` (R1's bind-time bug fix, mirrored here: a port with a
    declared pose and only a measured/absent rot is not a candidate — it
    has nothing declared to check the rot against). A port with neither
    has nothing to check against, and one bound by string form alone (no
    ``axis_atom``/``phase_atom``) was never measured a frame to disagree
    with. Skipped, like ``port_pose_mismatch``, when the scene doesn't
    share the block's frame at all (``envelope_fit`` already says that
    once for the whole block)."""
    findings: list[ValidationIssue] = []
    for node in tree.blocks.values():
        if node.template is not None or node.bound_kind != "structure":
            continue
        env = effective_envelope(tree, node)
        if not env or node.bound is None:
            continue
        scene = bound_full_scenes.get(node.bound)
        if scene is None:
            continue
        if isinstance(envelope_fit(env, scene), FrameMismatch):
            continue
        for port in node.ports.values():
            if (
                port.bound_atom is None
                or port.axis_atom is None
                or port.phase_atom is None
            ):
                continue
            declared_rot = port.rot is not None and port.rot_source == "declared"
            declared_direction_only = port.rot is None and port.direction is not None
            if not (declared_rot or declared_direction_only):
                continue
            frame = measured_port_frame(
                scene, port.bound_atom, port.axis_atom, port.phase_atom
            )
            if frame is None:
                continue  # degenerate atoms — nothing to compare
            deviation = rot_mismatch_rad(port, frame)
            if deviation is None or deviation <= PORT_ROT_MISMATCH_RAD:
                continue
            against = "declared rot" if declared_rot else "declared direction"
            findings.append(
                ValidationIssue(
                    rule="port_rot_mismatch",
                    subject=f"{node.name}.{port.name}",
                    detail=(
                        f"measured frame off {port.axis_atom!r}/"
                        f"{port.phase_atom!r} (in structure {node.bound!r}) "
                        f"is {format_quantity(deviation, 'angle')} from the "
                        f"{against} — more than "
                        f"{format_quantity(PORT_ROT_MISMATCH_RAD, 'angle')}. "
                        "The declared target is kept: fix whichever is "
                        "wrong (set_port_pose, or move the atoms)"
                    ),
                    severity="warn",
                )
            )
    return findings


def validate_atomic(
    tree: SeTree,
    *,
    bound_scenes: dict[str, dict[str, str] | None] | None = None,
    bound_full_scenes: dict[str, StructScene] | None = None,
) -> list[ValidationIssue]:
    """Every atomic-mode finding (empty = clean, as far as *chemistry*
    goes — :func:`precis_se.validate.validate` owns the rest, and the
    handler concatenates the two under one filled-fraction header). Pure
    read over ``tree`` plus the optional pre-hydrated ``bound_scenes``/
    ``bound_full_scenes`` (module docstring) — omitted or missing a slug
    simply skips the checks that need it, rather than raising, so a caller
    that hasn't wired binding/envelope-fit hydration still gets every
    other finding.

    A design with no chemistry in it at all (no bond/interaction connect,
    no ``structure`` binding) produces nothing here — every check below is
    keyed off an atomic fact the design would have to have stated."""
    findings: list[ValidationIssue] = []
    findings.extend(_port_capability_findings(tree))
    findings.extend(_binding_findings(tree, bound_scenes or {}))
    findings.extend(_envelope_fit_findings(tree, bound_full_scenes or {}))
    findings.extend(_port_pose_findings(tree, bound_full_scenes or {}))
    findings.extend(_port_rot_findings(tree, bound_full_scenes or {}))
    findings.extend(_connect_cycle_findings(tree))
    findings.extend(_bond_length_findings(tree))
    findings.extend(_bond_vector_findings(tree))
    return findings
