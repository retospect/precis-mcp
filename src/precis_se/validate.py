"""L0/L1 feasibility findings over a loaded :class:`~precis_se.ops.SeTree`
— the ``structure.validate``/:mod:`precis_nm.validate` shape (error/warn
tiers, a rule/subject/detail finding per row), applied to the se
block/port/connect graph and its envelopes.

This is a **read-time re-check over stored data**, not the op-time gate
restated: op-time validation only protects data that went through
``apply_ops``; a row that got there some other way (hand correction, a
future bug, direct persist-layer manipulation) must still be caught,
loudly, the next time anyone looks. Nothing here mutates or gates a write;
it only reports — and per se-kind.md's "suggestive by contract" decision,
absence (no envelope, no ports, nothing connected yet) is *warn-tier at
most*: an empty design must read as unfilled, never as failed.

The geometry-tier check this round is **undeclared interpenetration**
(se-kind.md L4 "Design DRC, geometry tier"): two blocks whose posed
envelopes overlap while neither nests the other and no connect sanctions
the contact. Overlap between blocks a joint/connect relates is *normal*
(a shaft in its bore, a press fit); overlap nobody declared is the
finding. Envelope-vs-envelope only, via the cad kernel's exact-sign
:func:`~precis.cad.relate.clearance` at metres — realized-solid DRC
(walls, net-empty after cuts) needs L3 solids and lands with realization.
Poses are treated as world-frame, the nm ``view='clearance'`` v1
convention; array members are not expanded (the array node itself is
checked at its own pose — a later increment poses members).
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se import bom as se_bom
from precis_se.ops import SeBlock, SeTree, effective_envelope, effective_ports


@dataclass
class ValidationIssue:
    """One validator finding — mirrors ``precis_nm.validate.
    ValidationIssue`` (rule/subject/detail + severity)."""

    rule: str
    subject: str
    detail: str
    #: 'error' (structurally broken — a dangling reference) or 'warn'
    #: (advisory — scaffolding-in-progress is normal; so is contact the
    #: designer may simply not have declared yet).
    severity: str = "error"


def _is_ancestor(tree: SeTree, a: str, b: str) -> bool:
    """True when ``a`` is on ``b``'s parent chain."""
    seen: set[str] = set()
    cur = tree.blocks.get(b)
    while cur is not None and cur.parent is not None and cur.parent not in seen:
        if cur.parent == a:
            return True
        seen.add(cur.parent)
        cur = tree.blocks.get(cur.parent)
    return False


#: Dimension keys that are not lengths — counts and angles pass through
#: kernel-unit normalization unscaled (everything else the DSL stores is a
#: length in the design's own metres).
_UNSCALED_KEYS = frozenset({"n", "angle"})

#: Characteristic lengths inside this band feed the kernel as-is
#: (``scale == 1.0``) — the kernel's tolerances (``LINEAR_REL_EPS``,
#: ``CONTACT_TOL_REL``) are scale-relative now (units-policy-cutover), but
#: this band still matters: ``_CROSS_SCALE_RATIO`` below refuses to
#: combine wildly different-scale blocks in one SDF query regardless, and
#: every pre-normalization se design lived here, so in-band behaviour
#: stays bit-identical to the historical unscaled path.
_KERNEL_BAND = (1e-3, 1e6)

#: Out-of-band designs are normalized so the query's SMALLEST block lands
#: here — squarely inside the kernel's comfort zone. Keyed off the
#: smallest (not largest) so a nano part paired with a big one can never
#: be left sub-epsilon by its partner (reviewer finding, 2026-09-09).
_KERNEL_TARGET = 100.0

#: Blocks whose characteristic lengths differ by more than this cannot
#: share one SDF query. The clearance minimizer no longer depends on a
#: grid point landing inside the smaller body — its closest-point seeds
#: are spaced by the bodies themselves (gr334763) — but a huge ratio still
#: degrades the answer: the joint query region, the descent's trust step
#: and the reported resolution are all sized off one governing length,
#: which cannot be both bodies' at once. Cross-scale pairs are
#: refused/reported honestly instead (``kernel_scale`` → ``None``);
#: lifting the cap needs a per-body scale in the kernel, not a bigger
#: number here.
_CROSS_SCALE_RATIO = 6.0


def _characteristic_length(envelope: str) -> float:
    """A block's own culling-risk proxy: its largest envelope length
    param. Pose is deliberately excluded — where a shape sits in the
    world doesn't change whether its faces survive ``LINEAR_EPS``.
    0.0 when the envelope doesn't parse (the caller's downstream
    handling owns that case)."""
    try:
        spec = cad_dsl.parse(envelope)
    except (cad_dsl.DslError, ValueError):
        return 0.0
    lengths = [abs(float(v)) for k, v in spec.params.items() if k not in _UNSCALED_KEYS]
    return max(lengths, default=0.0)


def kernel_scale(*posed: tuple[str, SeBlock]) -> float | None:
    """Metres → kernel-unit factor for one geometry query, or ``None``
    when the blocks are too far apart in scale to share a query.

    Historically the cad kernel's tolerances were **absolute in whatever
    numbers it was handed** (``LINEAR_EPS = 1e-6`` culled "degenerate"
    faces, so a nanometre-scale box arrived with *zero* faces and
    vacuously contained everything — the boxel-3nm ValueError,
    2026-09-09); the units-policy-cutover relative-tolerance audit fixed
    that at the source (``LINEAR_REL_EPS``, per-primitive). This seam
    stays for a narrower, still-real reason: combining a housing-scale and
    a bolt-scale primitive in **one** SDF query degrades the numerics
    (the descent's grid spacing and trust step are sized off one governing
    length, which cannot be both bodies' at once) however precise each
    primitive's own tolerance is — so an out-of-band query is still
    normalized so its smallest block lands at ``_KERNEL_TARGET``, and
    every returned length divides back by the factor. In-band queries
    return exactly ``1.0`` (bit-identical to the historical path). A pair
    whose sizes differ by more than ``_CROSS_SCALE_RATIO`` returns
    ``None`` — the caller must skip/refuse legibly, never compute a
    garbage gap."""
    lengths = [_characteristic_length(env) for env, _node in posed]
    positive = [x for x in lengths if x > 0.0]
    if not positive:
        return 1.0
    smallest, largest = min(positive), max(positive)
    if _KERNEL_BAND[0] <= smallest <= _KERNEL_BAND[1]:
        # In-band pairs always pass through unscaled — including
        # high-ratio ones, whose optimizer limits predate normalization
        # and stay the historical, tolerated behaviour.
        return 1.0
    if largest / smallest > _CROSS_SCALE_RATIO:
        return None
    return _KERNEL_TARGET / smallest


def _posed_component(
    design: CadDesign, name: str, envelope: str, node: SeBlock, scale: float = 1.0
):
    """Add ``envelope`` as a one-primitive component posed at the block's
    own pose/rot (world-frame v1), with lengths multiplied by ``scale``
    (:func:`kernel_scale` — kernel units; 1.0 = metres as-is). Returns the
    component expression, or ``None`` when the stored envelope no longer
    parses (a malformed stored envelope is not this check's finding to
    raise on — op-time validation gates it; render paths re-check legibly)."""
    try:
        spec = cad_dsl.parse(envelope)
        if scale != 1.0:
            spec = cad_dsl.ShapeSpec(
                spec.alias,
                {
                    k: (v if k in _UNSCALED_KEYS else v * scale)
                    for k, v in spec.params.items()
                },
            )
        prim = cad_dsl.build(spec)
    except (cad_dsl.DslError, ValueError):
        return None
    pose_scaled = [float(c) * scale for c in node.pose]
    xform = cad_pose(cad_as_vec3(pose_scaled), cad_as_vec3(node.rot))
    design.add_component(name, design.prim(name, prim, xform))
    return design.components[name]


def envelope_overlaps(
    tree: SeTree,
) -> tuple[list[tuple[str, str, float]], list[tuple[str, str]]]:
    """``(overlaps, cross_scale)`` over every unordered pair of blocks —
    excluding ancestor/descendant pairs (a child inside its parent
    module's envelope is containment, not interference). Pure geometry;
    the caller decides which overlaps a connect sanctions.

    ``overlaps`` holds pairs whose posed effective envelopes interpenetrate
    (signed gap < −contact tolerance), gap in metres. ``cross_scale``
    holds pairs the check could NOT run on — sizes too far apart for one
    SDF query (:func:`kernel_scale` → ``None``) — reported rather than
    silently dropped, so a nano bolt inside a macro housing reads as
    *unverifiable*, never as *fine*."""
    posed: list[tuple[str, SeBlock, str]] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        env = effective_envelope(tree, node)
        if env:
            posed.append((name, node, env))
    out: list[tuple[str, str, float]] = []
    cross: list[tuple[str, str]] = []
    for i, (a_name, a_node, a_env) in enumerate(posed):
        for b_name, b_node, b_env in posed[i + 1 :]:
            if _is_ancestor(tree, a_name, b_name) or _is_ancestor(tree, b_name, a_name):
                continue
            scale = kernel_scale((a_env, a_node), (b_env, b_node))
            if scale is None:
                cross.append((a_name, b_name))
                continue
            design = CadDesign()
            a_expr = _posed_component(design, a_name, a_env, a_node, scale)
            b_expr = _posed_component(design, b_name, b_env, b_node, scale)
            if a_expr is None or b_expr is None:
                continue
            result = cad_relate.clearance(design, a_name, b_name)
            # Compared in kernel units against the query's OWN resolution
            # (a fraction of the smaller block's size, gr334763) rather
            # than a fixed 10⁻² that means one thing for a 10 mm part and
            # another for a 10 m one — and that nanoscale overlap, even
            # after normalization, could never reach.
            if result.gap < -result.resolution:
                out.append((a_name, b_name, float(result.gap) / scale))
    return out, cross


def validate(tree: SeTree) -> list[ValidationIssue]:
    """Return all findings (empty = clean — but see the handler's
    filled-fraction header: clean-and-empty must render as *unfilled*,
    never as done). Pure over ``tree``; no store access."""
    findings: list[ValidationIssue] = []

    # 1. dangling_connect (error) — an endpoint that no longer resolves
    # (defense in depth; ops.py's connect op gates this at write time).
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        dangling = []
        for blk, prt in ((c.a_block, c.a_port), (c.b_block, c.b_port)):
            node = tree.blocks.get(blk)
            if node is None:
                dangling.append(f"block {blk!r} no longer exists")
            elif prt not in effective_ports(tree, node):
                dangling.append(f"port {blk}.{prt} no longer exists")
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

    # 2. unconnected_port (warn) — a live, block-owned port no connect
    # references, counting a connect on an instance/array as referencing
    # the resolved template port.
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
            if (node.name, port.name) not in referenced:
                findings.append(
                    ValidationIssue(
                        rule="unconnected_port",
                        subject=f"{node.name}.{port.name}",
                        detail=(
                            "no live connect references this port — fine "
                            "mid-design, but a scaffold that never gets "
                            "wired never becomes a real assembly"
                        ),
                        severity="warn",
                    )
                )

    # 3. block_without_envelope (warn) — ports declared but no envelope: a
    # port needs geometry eventually to mean anything at L1.
    for node in tree.blocks.values():
        if node.ports and not node.envelope:
            findings.append(
                ValidationIssue(
                    rule="block_without_envelope",
                    subject=node.name,
                    detail=(
                        f"block {node.name!r} declares {len(node.ports)} "
                        "port(s) but has no envelope — set one "
                        "(set_envelope) before this scaffold can be placed"
                    ),
                    severity="warn",
                )
            )

    # 4. undeclared_interpenetration (warn) — posed envelope overlap no
    # connect sanctions (module docstring). A connect between the two
    # blocks — any ports, stored names — declares the contact intended.
    connected_pairs = {frozenset({c.a_block, c.b_block}) for c in tree.connects}
    overlaps, cross_scale = envelope_overlaps(tree)
    for a_name, b_name, gap in overlaps:
        if frozenset({a_name, b_name}) in connected_pairs:
            continue
        findings.append(
            ValidationIssue(
                rule="undeclared_interpenetration",
                subject=f"{a_name}—{b_name}",
                detail=(
                    f"posed envelopes overlap by {-gap:g} m with no "
                    "connect between the two blocks — declare the "
                    "relation (connect their ports) if intended, or "
                    "re-pose"
                ),
                severity="warn",
            )
        )
    if cross_scale:
        # One aggregate finding, not one per pair — a design that mixes
        # scales pairs every small block with every big one, and N×M
        # identical warns would drown the rest of the report.
        shown = ", ".join(f"{a}—{b}" for a, b in cross_scale[:5])
        more = f" (+{len(cross_scale) - 5} more)" if len(cross_scale) > 5 else ""
        findings.append(
            ValidationIssue(
                rule="cross_scale_unverifiable",
                subject=f"{len(cross_scale)} pair(s)",
                detail=(
                    f"{shown}{more}: block sizes differ too much to share "
                    "one interpenetration check (SDF grid can't resolve "
                    "both) — these pairs are UNCHECKED, not clear; verify "
                    "cross-scale seating at L3/binding level"
                ),
                severity="warn",
            )
        )

    # 5. dangling_bom (error) — a bought item hung off a target that no
    # longer resolves (defense in depth: ops' vacancy rules drop these
    # with their target; a hand-corrected row still has to surface).
    for line in tree.bom:
        occurrences, note = se_bom.line_occurrences(tree, line)
        if occurrences is None:
            findings.append(
                ValidationIssue(
                    rule="dangling_bom",
                    subject=f"{line.item_kind}:{line.item}",
                    detail=(
                        f"hung off {line.target!r} — {note}; the quantity "
                        "can't be counted (remove_bom, or restore the "
                        "target)"
                    ),
                    severity="error",
                )
            )
    return findings
