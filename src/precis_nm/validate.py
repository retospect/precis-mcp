"""L0 feasibility findings over a loaded :class:`~precis_nm.ops.BlockTree` —
the ``structure.validate`` shape (error/warn tiers, a rule/subject/detail
finding per row), applied to the block/port/connect graph rather than atoms.

This is a **read-time re-check over stored data**, not the op-time gate
(``ops.py``'s ``_op_connect``) restated: the two exist for the same reason
``handler._render_tree``'s expansion-stack guard exists alongside
``ops._find_instance_cycle`` — op-time validation only protects data that
went through ``apply_ops``; a row that got there some other way (hand
correction, a future bug, direct persist-layer manipulation) must still be
caught, loudly, the next time anyone looks. Nothing here mutates or gates a
write; it only reports.

**Round 3** adds threading/binding findings. ``dangling_binding``/
``binding_element_mismatch`` need to know whether a bound structure design
still resolves and what element its bound atoms are — this module stays
store-free (module docstring above), so the handler hydrates that once (a
``bound_scenes`` mapping: design slug → ``{atom_label: element}``, or
``None`` for a slug that no longer resolves) and passes it into
:func:`validate`, the same "assemble in the view path, keep the checker
pure" split ``_render_validate`` already uses for tree data.

**Slice 4b** adds :func:`envelope_fit` — the L1↔L5 agreement check
(docs/backlog/nm-kind.md "4b — LLM fill loop"): a block declares an
envelope (the ``cad`` mini-DSL, Å) at L1; once bound, it holds real atoms
at L5. ``envelope_fit`` models the *agreement* between the two (the
pcb-component-model precedent transferred verbatim: "model the agreement,
not the two sides"), not either side alone — it reports the single
worst-offending atom and by how many Å it protrudes past the declared
envelope plus a vdW margin, via the ``cad`` kernel's exact-sign
:func:`~precis.cad.relate.component_sdf` (the same kernel ``view=
'clearance'`` already uses). Wired in two places (both call this one
function, never re-derived): a **bind preflight**
(:meth:`precis_nm.handler.NmHandler._bind_structure`, an advisory note on
the echo — a hand-authored envelope is often a rough first guess, so this
never blocks the bind) and a **warn-tier finding** here, in
:func:`validate` (rule ``envelope_fit``, checked every time the design is
read — catches drift after a bind, e.g. an envelope shrunk by a later
``add_block`` re-declaration, or a rebind to a differently-shaped
fragment). This module still needs no store: the handler hydrates the
bound *scenes themselves* (not just their atom/element map — envelope_fit
needs real Cartesian positions) into ``bound_full_scenes`` and passes them
in, the same "assemble in the view path, keep the checker pure" split.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Placed, Primitive
from precis.cad.relate import clearance as cad_clearance
from precis.cad.relate import component_sdf
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.structure import Scene as StructScene
from precis_nm.generators.sp2 import VDW_MARGIN_A
from precis_nm.ops import BlockTree, connect_role, effective_envelope, effective_ports


@dataclass
class ValidationIssue:
    """One validator finding — mirrors ``structure.validate.ValidationIssue``
    but named for the block/port/connect domain (subject, not atoms)."""

    rule: str
    subject: str
    detail: str
    #: 'error' (structurally broken — a dangling reference, or a connect
    #: that violates its own endpoints' *declared* roles — see
    #: ``port_capability``'s note on the trust model below), 'warn'
    #: (advisory — scaffolding-in-progress is normal), or 'info' (a finding
    #: that names something worth knowing but never signals a problem —
    #: today only ``unconnected_port``'s "external by design" line for a
    #: port carrying ``annotations={"external": true}``, gripe 334769).
    severity: str = "error"


def envelope_fit(
    envelope: str, scene: StructScene, *, margin_A: float = VDW_MARGIN_A
) -> tuple[str, float] | None:
    """The L1↔L5 agreement check itself (module docstring): does every atom
    of ``scene`` sit inside ``envelope`` (a ``cad`` mini-DSL config string,
    Å) plus ``margin_A`` of headroom? Returns ``(worst atom label,
    protrusion_A)`` for the single worst-offending atom — the largest
    signed distance beyond the margin (``component_sdf`` is negative
    inside, so ``sdf - margin_A > 0`` is a genuine protrusion) — or
    ``None`` when every atom sits inside the margin, or when ``envelope``
    fails to parse (a malformed envelope is a different finding —
    ``ops.py``'s ``_validate_envelope`` gate, or a stored-but-now-invalid
    envelope the way ``_render_clearance`` re-checks; not this function's
    job to raise on it).

    **Posed at identity, not the block's world pose/rot.** A block's
    envelope is declared in the block's own *local* frame — the same local
    frame every :mod:`precis_nm.generators` builder emits atoms into (a
    ``cyl:r<>h<>`` envelope's ``z=0..h``, radially centered on the axis,
    matches a generated tube's own atom coordinates exactly, unshifted).
    ``node.pose``/``node.rot`` only place the block *within* the larger
    design (the same frame :func:`precis_nm.handler._render_clearance`
    poses envelopes into to check block-vs-block clearance) — applying
    them here would compare the bound scene's own local-frame atoms
    against an envelope translated/rotated into a different frame
    entirely, comparing two things that were never meant to line up."""
    try:
        prim = cad_dsl.build_config(envelope)
    except cad_dsl.DslError:
        return None
    design = CadDesign()
    identity = cad_pose(cad_as_vec3([0.0, 0.0, 0.0]), cad_as_vec3([0.0, 0.0, 0.0]))
    design.add_component("_envelope_fit", design.prim("_envelope_fit", prim, identity))
    expr = design.components["_envelope_fit"]
    worst_label: str | None = None
    worst_protrusion = 0.0
    for label, atom in scene.atoms.items():
        cart = scene.cell.frac_to_cart(atom.frac)
        sdf = component_sdf(design, expr, cad_as_vec3(cart))
        protrusion = sdf - margin_A
        if protrusion > worst_protrusion:
            worst_protrusion = protrusion
            worst_label = label
    if worst_label is None:
        return None
    return worst_label, worst_protrusion


# ── gripe 334768 — the azo-stick-5nm dogfood (2026-09-11): a design with a
# deeply-interpenetrating unconnected pair, a 5-block connect cycle closed
# head-to-tail, a 48.5 Å "covalent bond", and away-pointing bond-vector
# ports validated CLEANER than a correct design, because none of the four
# were checked. The four functions below add them; :func:`validate` folds
# their findings in below. Every threshold here is a *fraction of a block's
# own envelope size* (the smaller of the two blocks involved), never an
# absolute Å figure — docs/backlog/multiscale-design-architecture.md
# "Units policy": an nm design's blocks can be anywhere from sub-nm to
# tens of nm, so a fixed epsilon tuned for one scale is nonsense at
# another; a governing-length fraction reads the same at every scale.
# ──────────────────────────────────────────────────────────────────────

#: :func:`_envelope_overlap_findings`'s interpenetration-depth threshold —
#: a fraction of the smaller of the two blocks' own envelope bbox diagonal.
#: Two genuinely unrelated blocks should never share material at all; 10%
#: leaves headroom for ``cad.relate.clearance``'s own coarse-grid +
#: gradient-descent numerical slack (its docstring) without missing the
#: dogfood's actual failure, where the interpenetration depth was on the
#: order of the blocks' own size, not a sliver.
OVERLAP_DEPTH_FRACTION = 0.10

#: :func:`_bond_length_findings`'s excess-gap threshold — a fraction of the
#: smaller connected block's own envelope bbox diagonal. A real port-to-port
#: bond sits close to both blocks' surfaces, so the pose-to-pose distance
#: minus each block's own envelope extent along that line should come out
#: near zero (or negative — a port often sits inside its nominal envelope);
#: 50% leaves generous headroom for the block-pose-not-port-position
#: approximation this check is forced into (ports have no stored position —
#: this module's docstring, "Round 3", and nm-kind.md's port row) while
#: still catching the dogfood's 48.5 Å bond against ~10s-of-Å blocks, whose
#: residual gap is many multiples of either block's own size.
BOND_GAP_FRACTION = 0.5

#: :func:`_bond_vector_findings`'s alignment threshold, in degrees off
#: perfectly anti-parallel (180°). Two bonded ports both declare a
#: ``direction`` pointing outward from their own block along the bond, so a
#: real bond's two vectors are anti-parallel; 60° absorbs a genuinely bent
#: approach geometry while still catching the dogfood's actual failure
#: (vectors pointing away from each other — nowhere near anti-parallel).
BOND_VECTOR_MAX_DEVIATION_DEG = 60.0


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


def _connected(tree: BlockTree, a_name: str, b_name: str) -> bool:
    """Whether a live connect names ``a_name``/``b_name`` at its two
    endpoints (either order) — the domain's own notion of "declared
    contact" between two blocks, port identity aside."""
    return any({c.a_block, c.b_block} == {a_name, b_name} for c in tree.connects)


def _is_nested(tree: BlockTree, a_name: str, b_name: str) -> bool:
    """Whether ``a_name``/``b_name`` are ancestor↔descendant in the block
    tree (either direction), or one is literally the other's ``template``.
    Both are *declared* structural relationships distinct from ``connect``
    (a child conventionally sits inside/against its parent's envelope —
    the flagship "rotaxane axle" example in ``precis-nm-help.md`` nests a
    ``hub`` inside its parent ``axle`` at the shared default pose; an
    instance defaults to its template's own pose until ``set_pose`` moves
    it), so :func:`_envelope_overlap_findings` treats overlap between such
    a pair as intentional, the same way it treats a connected pair — only
    a pair with *no* declared relationship at all is a candidate finding."""

    def is_ancestor(anc: str, desc: str) -> bool:
        node = tree.blocks.get(desc)
        seen = {desc}
        while node is not None and node.parent is not None:
            if node.parent == anc:
                return True
            if node.parent in seen:  # a corrupted/cyclic parent chain
                return False  # — not this check's job; bail quietly
            seen.add(node.parent)
            node = tree.blocks.get(node.parent)
        return False

    a_node, b_node = tree.blocks.get(a_name), tree.blocks.get(b_name)
    if a_node is not None and a_node.template == b_name:
        return True
    if b_node is not None and b_node.template == a_name:
        return True
    return is_ancestor(a_name, b_name) or is_ancestor(b_name, a_name)


def _envelope_overlap_findings(tree: BlockTree) -> list[ValidationIssue]:
    """``envelope_overlap`` (error) — two blocks with no declared
    relationship (:func:`_connected`/:func:`_is_nested` both false) whose
    envelopes materially interpenetrate, via the same exact-sign CSG SDF
    :func:`~precis.cad.relate.clearance` ``view='clearance'`` already uses
    (module docstring's "Slice 4b" precedent: never re-derive the kernel
    call, reuse it). A physically real design never has two unrelated solid
    parts occupying the same space — this is as structurally broken as
    ``dangling_connect``, hence 'error', not 'warn'.

    **O(n) pre-pass, then the O(n²) pairwise loop** — each block's envelope
    is parsed (:func:`~precis.cad.dsl.build_config`) and its characteristic
    size (:func:`_envelope_diag`) computed exactly once, before the pair
    loop below, rather than re-parsing the same DSL string on every pair a
    block participates in."""
    findings: list[ValidationIssue] = []
    resolved: dict[str, tuple[Primitive, float]] = {}
    for name, node in tree.blocks.items():
        env = effective_envelope(tree, node)
        if not env:
            continue
        try:
            prim = cad_dsl.build_config(env)
        except cad_dsl.DslError:
            continue  # a malformed envelope — a different finding's job
        diag = _envelope_diag(prim)
        if diag is None:
            continue
        resolved[name] = (prim, diag)
    names = sorted(resolved)
    for i, a_name in enumerate(names):
        a_node = tree.blocks[a_name]
        a_prim, a_diag = resolved[a_name]
        for b_name in names[i + 1 :]:
            b_node = tree.blocks[b_name]
            b_prim, b_diag = resolved[b_name]
            if _connected(tree, a_name, b_name) or _is_nested(tree, a_name, b_name):
                continue
            design = CadDesign()
            design.add_component(
                a_name,
                design.prim(
                    a_name,
                    a_prim,
                    cad_pose(cad_as_vec3(a_node.pose), cad_as_vec3(a_node.rot)),
                ),
            )
            design.add_component(
                b_name,
                design.prim(
                    b_name,
                    b_prim,
                    cad_pose(cad_as_vec3(b_node.pose), cad_as_vec3(b_node.rot)),
                ),
            )
            result = cad_clearance(design, a_name, b_name)
            if result.gap >= 0:
                continue
            depth = -result.gap
            threshold = OVERLAP_DEPTH_FRACTION * min(a_diag, b_diag)
            if depth <= threshold:
                continue
            findings.append(
                ValidationIssue(
                    rule="envelope_overlap",
                    subject=f"{a_name}—{b_name}",
                    detail=(
                        f"envelopes of {a_name!r} and {b_name!r} interpenetrate "
                        f"by {depth:.3g} Å (> {threshold:.3g} Å, "
                        f"{OVERLAP_DEPTH_FRACTION:.0%} of the smaller block's "
                        "own envelope size) with no declared connect or "
                        "nesting between them — connect them if this is a "
                        "real bond/contact, or move one so they no longer "
                        "share material"
                    ),
                    severity="error",
                )
            )
    return findings


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


def _connect_cycle_findings(tree: BlockTree) -> list[ValidationIssue]:
    """``connect_cycle`` (warn) — the connect graph (nodes: block names;
    edges: every live connect, by index so a genuine parallel connect
    between the same two blocks counts as its own 2-cycle too) is not
    acyclic. The block *tree* is parent-child by construction, but connects
    between ports can close a loop across it (the dogfood's 5-block chain
    connected head-to-tail) — a macrocycle IS real chemistry (a crown
    ether, a ring polymer), so this never says "forbidden", only "verify
    this is intended" (warn, not error). One finding per independent cycle
    in a spanning-tree cycle basis (standard graph-theory technique: BFS
    each connected component, and every non-tree edge closes exactly one
    cycle against the tree already built) — not an exhaustive enumeration
    of every cycle a denser graph might contain, which is combinatorial;
    a basis names every independent loop at least once, which is what
    "verify this is intended" needs."""
    edges = [
        (c.a_block, c.b_block)
        for c in tree.connects
        if c.a_block in tree.blocks
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


def _bond_length_findings(tree: BlockTree) -> list[ValidationIssue]:
    """``bond_length_sanity`` (warn) — a ``kind='bond'`` connect whose two
    blocks' pose-to-pose gap (distance minus each block's own envelope
    extent along that line — an approximation this module's docstring
    already flags as a known limitation: ports have no stored position of
    their own, only their owning block's pose) is wildly beyond a
    plausible bond. Never gates (``warn``): the approximation can read long
    for a legitimate reason (a bent/off-axis port), so this only ever
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
        if distance <= 1e-9:
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
                    f"block-pose gap ≈{gap:.3g} Å (pose distance {distance:.3g} "
                    f"Å minus each block's own envelope extent along the "
                    f"line — an approximation: ports have no stored position "
                    f"of their own, only their block's pose) exceeds the "
                    f"{threshold:.3g} Å scale-relative threshold for a "
                    "plausible bond — not a chemically real covalent bond "
                    "at this distance"
                ),
                severity="warn",
            )
        )
    return findings


def _bond_vector_findings(tree: BlockTree) -> list[ValidationIssue]:
    """``bond_vector_alignment`` (warn) — a ``kind='bond'`` connect whose
    two ports both declare a ``direction`` (skipped when either doesn't —
    ``direction`` is optional), but those vectors are far from anti
    -parallel (:data:`BOND_VECTOR_MAX_DEVIATION_DEG` off 180°). A bonded
    port's ``direction`` is meant to point outward along the bond axis, so
    two real bond partners point at each other, not away — the dogfood's
    actual failure mode, which proved these vectors are pure decoration
    today (module docstring) unless something reads them.

    ``direction`` is declared in the block's own LOCAL frame — the same
    convention ``envelope`` uses (:func:`envelope_fit`'s docstring, "Posed
    at identity, not the block's world pose/rot") — so each vector is
    rotated into world frame via the block's own pose/rot
    (:func:`~precis.cad.vec.pose`'s :meth:`~precis.cad.vec.Transform.
    apply_dir`, rotation only — no translation for a direction) before the
    dot product; comparing the raw stored vectors would silently misjudge
    every rotated block (a spurious warn on a genuinely anti-parallel pair,
    or a miss on a genuinely misaligned one)."""
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
        angle_deg = math.degrees(math.acos(cos))
        deviation = 180.0 - angle_deg
        if deviation <= BOND_VECTOR_MAX_DEVIATION_DEG:
            continue
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        findings.append(
            ValidationIssue(
                rule="bond_vector_alignment",
                subject=subject,
                detail=(
                    f"port direction vectors are {angle_deg:.1f}° apart "
                    f"(expect ≈180°, anti-parallel, within "
                    f"{BOND_VECTOR_MAX_DEVIATION_DEG:g}° — a bonded port's "
                    "direction points outward along the bond) — "
                    f"{c.a_block}.{c.a_port} and {c.b_block}.{c.b_port} "
                    "don't point at each other"
                ),
                severity="warn",
            )
        )
    return findings


def validate(
    tree: BlockTree,
    *,
    bound_scenes: dict[str, dict[str, str] | None] | None = None,
    bound_full_scenes: dict[str, StructScene] | None = None,
) -> list[ValidationIssue]:
    """Return all L0(-ish; threading/binding are L2/L5) findings (empty =
    clean). Pure read over ``tree`` plus the optional pre-hydrated
    ``bound_scenes``/``bound_full_scenes`` (module docstring) — omitted or
    missing a slug simply skips the checks that need it, rather than
    raising, so a caller that hasn't wired binding/envelope-fit validation
    yet still gets every other finding."""
    findings: list[ValidationIssue] = []
    bound_scenes = bound_scenes or {}

    # 1/2. dangling_connect (error) + port_capability (error, defense in
    # depth — ops.py's connect op already gates this at write time; this
    # re-checks whatever ended up stored). One connect can only ever
    # contribute to one of the two: a dangling endpoint can't be capability
    # -checked (there's no PortSpec to read roles off), so #2 skips any
    # connect #1 already flagged.
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        a_node = tree.blocks.get(c.a_block)
        b_node = tree.blocks.get(c.b_block)
        a_spec = (
            effective_ports(tree, a_node).get(c.a_port) if a_node is not None else None
        )
        b_spec = (
            effective_ports(tree, b_node).get(c.b_port) if b_node is not None else None
        )
        dangling = []
        if a_node is None:
            dangling.append(f"block {c.a_block!r} no longer exists")
        elif a_spec is None:
            dangling.append(f"port {c.a_block}.{c.a_port} no longer exists")
        if b_node is None:
            dangling.append(f"block {c.b_block!r} no longer exists")
        elif b_spec is None:
            dangling.append(f"port {c.b_block}.{c.b_port} no longer exists")
        if dangling:
            findings.append(
                ValidationIssue(
                    rule="dangling_connect",
                    subject=subject,
                    detail="; ".join(dangling)
                    + " — disconnect it or restore the endpoint",
                    severity="error",
                )
            )
            continue
        assert a_spec is not None and b_spec is not None  # both resolved above
        role = connect_role(c.kind, c.objectives)
        if role is None:
            continue
        offenders = [
            (blk, prt, spec.roles)
            for blk, prt, spec in (
                (c.a_block, c.a_port, a_spec),
                (c.b_block, c.b_port, b_spec),
            )
            if role not in spec.roles
        ]
        if offenders:
            # Re-checks the same *declared* roles ops.py's connect op gated
            # on at write time — this never independently verifies a role
            # against real chemistry (the pcb-component-model trust model:
            # capability labelling, not proof), so a finding here means the
            # stored connect is inconsistent with its own endpoints' labels,
            # not that the bond is chemically implausible.
            detail = "; ".join(
                f"{blk}.{prt} affords {roles or ['(none)']}, missing {role!r}"
                for blk, prt, roles in offenders
            )
            findings.append(
                ValidationIssue(
                    rule="port_capability",
                    subject=subject,
                    detail=detail,
                    severity="error",
                )
            )

    # 3. unconnected_port (warn) — a live, block-owned port no connect
    # references, counting a connect on an *instance* as referencing the
    # instance's resolved template port (an instance never owns ports of
    # its own — see ops.py's `_op_add_port` rejection — so the only ports
    # that can ever be "the subject" here belong to an ordinary block).
    #
    # gripe 334769: before this, the ONLY way to silence this warn was to
    # author a connect — even a fake one, which the dogfood proved an LLM
    # will happily do (a 48.5 Å "covalent bond" existed for no reason but
    # to quiet this line). ``add_port(annotations={"external": True})``
    # (``annotations``, not ``roles`` — ``roles`` is a checked chemistry
    # capability set gated at connect time, module docstring's "Capability
    # gate"; "this port is deliberately left open" is a *design-intent*
    # note about the port itself, exactly what the shared core's open,
    # merely-descriptive ``annotations`` dict is for) now marks a port as
    # intentionally external (an antenna, a future attachment point) —
    # skipped here with a distinct 'info' line instead of a 'warn', so a
    # design that legitimately has dangling attachment points can say so
    # without the perverse incentive to fake a bond.
    referenced: set[tuple[str, str]] = set()
    for c in tree.connects:
        for blk, prt in ((c.a_block, c.a_port), (c.b_block, c.b_port)):
            node = tree.blocks.get(blk)
            if node is None:
                continue  # already reported as dangling_connect
            source = node.template if node.template is not None else blk
            referenced.add((source, prt))
    for node in tree.blocks.values():
        for port in node.ports.values():
            if (node.name, port.name) in referenced:
                continue
            # ``is True`` on purpose, not a truthy check: an LLM-authored
            # ``{"external": "false"}`` (a JSON string, not a boolean) must
            # NOT silently read as external — only the literal JSON boolean
            # ``true`` gets the info-tier treatment; anything else (a
            # string, 0, a stray falsy) stays a normal warn.
            if port.annotations.get("external") is True:
                findings.append(
                    ValidationIssue(
                        rule="unconnected_port",
                        subject=f"{node.name}.{port.name}",
                        detail=(
                            "external by design — annotated "
                            "external=true, no live connect required"
                        ),
                        severity="info",
                    )
                )
                continue
            findings.append(
                ValidationIssue(
                    rule="unconnected_port",
                    subject=f"{node.name}.{port.name}",
                    detail=(
                        "no live connect references this port — fine "
                        "mid-design, but a scaffold that never gets "
                        "wired never becomes a real machine (or mark it "
                        "add_port(annotations={'external': True}) if it's "
                        "intentionally left open)"
                    ),
                    severity="warn",
                )
            )

    # 4. blocks_without_envelope (warn) — a block with declared ports but
    # no envelope: a port needs geometry eventually to mean anything at L1.
    for node in tree.blocks.values():
        if node.ports and not node.envelope:
            findings.append(
                ValidationIssue(
                    rule="blocks_without_envelope",
                    subject=node.name,
                    detail=(
                        f"block {node.name!r} declares "
                        f"{len(node.ports)} port(s) but has no envelope — "
                        "set one (add_block/instance the block with an "
                        "envelope) before this scaffold can be placed"
                    ),
                    severity="warn",
                )
            )

    # 5. dangling_threading (error) — a threading row naming a block that
    # no longer exists. ``_op_remove_block`` already drops threading
    # touching a removed subtree (ops.py's vacancy-precedent extension),
    # so a live finding here means the row got here some other way (hand
    # correction, a future bug) — the same defense-in-depth shape as
    # ``dangling_connect`` above.
    for t in tree.threading:
        missing = [n for n in (t.a, t.b) if n not in tree.blocks]
        if missing:
            findings.append(
                ValidationIssue(
                    rule="dangling_threading",
                    subject=f"{t.a}→{t.b}",
                    detail=(
                        f"block(s) {', '.join(missing)} no longer exist — "
                        "remove_threading it, or restore the block"
                    ),
                    severity="error",
                )
            )

    # 6. threaded_without_envelope (warn) — a threading pair where either
    # endpoint has no effective envelope, so the interlock this pair
    # asserts can never be verified geometrically (get(view='clearance')
    # needs an envelope on both sides).
    for t in tree.threading:
        a_node = tree.blocks.get(t.a)
        b_node = tree.blocks.get(t.b)
        if a_node is None or b_node is None:
            continue  # already reported as dangling_threading
        missing_env = [
            n
            for n, node in ((t.a, a_node), (t.b, b_node))
            if not effective_envelope(tree, node)
        ]
        if missing_env:
            findings.append(
                ValidationIssue(
                    rule="threaded_without_envelope",
                    subject=f"{t.a}→{t.b}",
                    detail=(
                        f"block(s) {', '.join(missing_env)} have no "
                        "envelope — the interlock can never be verified "
                        "geometrically until one is set"
                    ),
                    severity="warn",
                )
            )

    # 7/8. dangling_binding (error) + binding_element_mismatch (warn) — the
    # bind-time capability gate (handler's bind_structure) re-checked
    # against currently-hydrated scene data, defense in depth like
    # port_capability above. An instance never owns a binding of its own
    # (bind_structure rejects it — bind via the template), so only an
    # ordinary block's own ``bound_design``/ports are ever the subject here.
    for node in tree.blocks.values():
        if node.template is not None or node.bound_design is None:
            continue
        if node.bound_design not in bound_scenes:
            continue  # caller didn't hydrate this slug — skip, don't guess
        atoms = bound_scenes[node.bound_design]
        if atoms is None:
            findings.append(
                ValidationIssue(
                    rule="dangling_binding",
                    subject=node.name,
                    detail=(
                        f"block {node.name!r} is bound to structure design "
                        f"{node.bound_design!r}, which no longer resolves — "
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

    # 9. envelope_fit (warn) — the L1↔L5 agreement check (module docstring,
    # slice 4b): a bound block's realized atoms should sit inside its
    # declared envelope plus a vdW margin. Only an ordinary, bound block
    # with a resolvable effective envelope is ever the subject — a block
    # with no envelope has nothing to check against (``blocks_without_
    # envelope`` already covers that gap for ported blocks), and a slug the
    # caller didn't hydrate into ``bound_full_scenes`` is skipped rather
    # than guessed at (the same "caller didn't hydrate — skip" discipline
    # rule 7/8 uses for ``bound_scenes``; a dangling/unresolvable design is
    # already reported once, by ``dangling_binding`` above).
    bound_full_scenes = bound_full_scenes or {}
    for node in tree.blocks.values():
        if node.template is not None or node.bound_design is None:
            continue
        env = effective_envelope(tree, node)
        if not env:
            continue
        scene = bound_full_scenes.get(node.bound_design)
        if scene is None:
            continue
        worst = envelope_fit(env, scene)
        if worst is not None:
            atom_label, protrusion = worst
            findings.append(
                ValidationIssue(
                    rule="envelope_fit",
                    subject=node.name,
                    detail=(
                        f"atom {atom_label!r} (in bound structure "
                        f"{node.bound_design!r}) protrudes {protrusion:.3g} Å "
                        f"beyond block {node.name!r}'s declared envelope "
                        f"{env!r} (+{VDW_MARGIN_A:g} Å vdW margin) — the L1 "
                        "envelope and the L5 realized atoms have drifted "
                        "apart; widen the envelope or rebind"
                    ),
                    severity="warn",
                )
            )

    # 10-13. gripe 334768's four checks (helpers above) — envelope overlap
    # beyond declared contact, connect-graph cycles, bond-length sanity, and
    # bond-vector anti-alignment. Each is its own pure function purely to
    # keep this dispatcher readable; none needs bound-scene data, so all
    # four run unconditionally, same as rules 1-6.
    findings.extend(_envelope_overlap_findings(tree))
    findings.extend(_connect_cycle_findings(tree))
    findings.extend(_bond_length_findings(tree))
    findings.extend(_bond_vector_findings(tree))

    return findings
