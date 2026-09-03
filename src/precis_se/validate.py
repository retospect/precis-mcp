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


def _posed_component(design: CadDesign, name: str, envelope: str, node: SeBlock):
    """Add ``envelope`` as a one-primitive component posed at the block's
    own pose/rot (world-frame v1). Returns the component expression, or
    ``None`` when the stored envelope no longer parses (a malformed stored
    envelope is not this check's finding to raise on — op-time validation
    gates it; render paths re-check legibly)."""
    try:
        prim = cad_dsl.build_config(envelope)
    except (cad_dsl.DslError, ValueError):
        return None
    xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    design.add_component(name, design.prim(name, prim, xform))
    return design.components[name]


def envelope_overlaps(tree: SeTree) -> list[tuple[str, str, float]]:
    """Every unordered pair of blocks whose posed effective envelopes
    interpenetrate (signed gap < −contact tolerance), with the gap in
    metres — excluding ancestor/descendant pairs (a child inside its
    parent module's envelope is containment, not interference). Pure
    geometry; the caller decides which overlaps a connect sanctions."""
    posed: list[tuple[str, SeBlock, str]] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        env = effective_envelope(tree, node)
        if env:
            posed.append((name, node, env))
    out: list[tuple[str, str, float]] = []
    for i, (a_name, a_node, a_env) in enumerate(posed):
        for b_name, b_node, b_env in posed[i + 1 :]:
            if _is_ancestor(tree, a_name, b_name) or _is_ancestor(tree, b_name, a_name):
                continue
            design = CadDesign()
            a_expr = _posed_component(design, a_name, a_env, a_node)
            b_expr = _posed_component(design, b_name, b_env, b_node)
            if a_expr is None or b_expr is None:
                continue
            result = cad_relate.clearance(design, a_name, b_name)
            if result.gap < -cad_relate.CONTACT_TOL_MM:
                out.append((a_name, b_name, float(result.gap)))
    return out


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
    for a_name, b_name, gap in envelope_overlaps(tree):
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
    return findings
