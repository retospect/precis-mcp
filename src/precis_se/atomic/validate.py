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

from precis.cad import dsl as cad_dsl
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Placed, Primitive
from precis.cad.relate import component_sdf
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.structure import Scene as StructScene
from precis.utils.units import format_quantity
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_se.atomic.vocab import bond_capability_offences, connect_role
from precis_se.ops import SeTree, effective_envelope, effective_ports
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
#: approximation this check is forced into (ports have no stored position)
#: while still catching the dogfood's 48.5 Å bond against ~10s-of-Å blocks,
#: whose residual gap is many multiples of either block's own size.
BOND_GAP_FRACTION = 0.5

#: :func:`_bond_vector_findings`'s alignment threshold, radians off
#: perfectly anti-parallel (π). Two bonded ports both declare a
#: ``direction`` pointing outward from their own block along the bond, so a
#: real bond's two vectors are anti-parallel; 60° (radians internal per the
#: units-policy-cutover angle ruling) absorbs a genuinely bent approach
#: geometry while still catching the dogfood's actual failure (vectors
#: pointing away from each other — nowhere near anti-parallel).
BOND_VECTOR_MAX_DEVIATION_RAD = math.radians(60.0)


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


def _bond_length_findings(tree: SeTree) -> list[ValidationIssue]:
    """``bond_length_sanity`` (warn) — a ``kind='bond'`` connect whose two
    blocks' pose-to-pose gap (distance minus each block's own envelope
    extent along that line — an approximation: ports have no stored
    position of their own, only their owning block's pose) is wildly beyond
    a plausible bond. Never gates (``warn``): the approximation can read
    long for a legitimate reason (a bent/off-axis port), so this only ever
    flags for a human/agent to look again, the same trust level
    ``port_capability`` extends to declared roles."""
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
        threshold = BOND_GAP_FRACTION * min(a_diag, b_diag)
        if gap <= threshold:
            continue
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        findings.append(
            ValidationIssue(
                rule="bond_length_sanity",
                subject=subject,
                detail=(
                    f"block-pose gap ≈{gap:.3g} m (pose distance {distance:.3g} "
                    f"m minus each block's own envelope extent along the "
                    f"line — an approximation: ports have no stored position "
                    f"of their own, only their block's pose) exceeds the "
                    f"{threshold:.3g} m scale-relative threshold for a "
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
    findings.extend(_connect_cycle_findings(tree))
    findings.extend(_bond_length_findings(tree))
    findings.extend(_bond_vector_findings(tree))
    return findings
