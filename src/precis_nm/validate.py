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
"""

from __future__ import annotations

from dataclasses import dataclass

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
    #: ``port_capability``'s note on the trust model below) or 'warn'
    #: (advisory — scaffolding-in-progress is normal).
    severity: str = "error"


def validate(
    tree: BlockTree,
    *,
    bound_scenes: dict[str, dict[str, str] | None] | None = None,
) -> list[ValidationIssue]:
    """Return all L0(-ish; threading/binding are L2/L5) findings (empty =
    clean). Pure read over ``tree`` plus the optional pre-hydrated
    ``bound_scenes`` (module docstring) — omitted or missing a slug simply
    skips the checks that need it, rather than raising, so a caller that
    hasn't wired binding validation yet still gets every other finding."""
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
                            "wired never becomes a real machine"
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

    return findings
