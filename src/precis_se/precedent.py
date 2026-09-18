"""Joining-precedent DRC — blocktree slice 5
(docs/backlog/blocktree-library-build-plan.md). A connect's ``bonded``
state is the product of a REACTION; pointing its transition's
``driver_ref`` at an ``rxn`` slug turns "does this joining chemistry work"
into a sourced-precedent read: how many yield rows exist for the rxn's
``reaction_class``, and across how many distinct reactions.

Two vocabularies meet here and are NOT the same string: a port pair's
joining *name* (``CuAAC``, :data:`~precis_se.atomic.vocab.JOINING_HALVES`)
and an ``rxn`` record's ``reaction_class`` (an RXNO id, ``refs.meta``). The
bridge is the transition — a block's ``driver_kind='reaction'`` transition
names the rxn by slug, and that record carries the class. No table maps
joining names to RXNO ids in this slice; the name stays a label, the rxn is
the claim.

Store-aware (unlike :mod:`precis_se.geometry_plausibility`, its sibling
connect-level pass): an rxn's ``reaction_class`` and precedent count live
in the store, and a foreign template's transitions live under that
template's OWN design ref id. :func:`findings` is appended by the
handler's ``_render_drc`` AFTER ``se_drc.drc(tree)`` — ``drc()`` itself
stays store-free by contract.
"""

from __future__ import annotations

from typing import Any

from precis.blocktree.types import parse_template_ref
from precis.design import states as design_states
from precis_se.atomic.vocab import _complementary_pair, _joining_name
from precis_se.ops import SeTree, effective_ports, resolve_template
from precis_se.validate import ValidationIssue


def _endpoint_roles(tree: SeTree, block: str, port: str) -> set[str]:
    """The roles ``block.port`` affords, resolved through the template for
    an instance/array member the same way :func:`effective_ports` already
    does for connect/render purposes. ``set()`` for an unresolvable block
    or port — a dangling connect endpoint is some OTHER finding's job."""
    node = tree.blocks.get(block)
    if node is None:
        return set()
    spec = effective_ports(tree, node).get(port)
    return set(spec.roles) if spec is not None else set()


def _endpoint_reaction_slugs(
    store: Any, tree: SeTree, ref_id: int, block: str
) -> set[str]:
    """The ``rxn`` slugs named by ``block``'s ``driver_kind='reaction'``
    transitions.

    Transitions are TEMPLATE-owned (``declare_transitions`` goes through
    ``_template_owned``; an instance/array node's own uid never carries
    ``design_transitions`` rows) — so for an instance/array member this
    resolves ``node.template`` first, through :func:`resolve_template`
    exactly as the block-view render does, spanning into a foreign design
    (``tree.foreign``) when the template is cross-design-qualified. The
    transitions then read against THAT template's own design ref id, not
    ``ref_id`` — a foreign template's ``design_transitions`` rows live
    under its own design's ref, never this one's."""
    node = tree.blocks.get(block)
    if node is None:
        return set()
    # ``template_node`` is a BlockNode generically (resolve_template's own
    # signature) but ``uid`` is se's own field (ops.py's SeBlock) — the
    # same ``Any``/``getattr`` posture the block-view render already uses
    # for this exact resolution (handler.py's ``states_owner``).
    template_node: Any = node
    owner_ref_id = ref_id
    if node.template is not None:
        template_node = resolve_template(tree, node.template)
        if template_node is None:
            return set()
        design_slug, _ = parse_template_ref(node.template)
        if design_slug is not None:
            foreign_ref = store.get_ref(kind="se", id=design_slug)
            if foreign_ref is None:
                return set()
            owner_ref_id = foreign_ref.id
    uid = getattr(template_node, "uid", None)
    if uid is None:
        return set()
    transitions = design_states.transitions_for(store, owner_ref_id, uid)
    return {
        t.driver_ref
        for t in transitions
        if t.driver_kind == "reaction" and t.driver_ref
    }


def findings(store: Any, tree: SeTree, ref_id: int) -> list[ValidationIssue]:
    """The four joining-precedent findings (module docstring) over every
    live connect (``tree.connects``), subjects labelled like
    ``connect_envelope_disjoint`` (:mod:`precis_se.geometry_plausibility`).

    Per connect: collect the ``driver_kind='reaction'`` transitions of
    BOTH endpoints, deduped by rxn slug. None, and the port pair is a
    known joining — ``joining_unnamed`` (info). None, and no joining
    either — nothing (a plain mechanical connect). One finding per rxn
    otherwise: ``joining_class_unknown`` (warn, no ``reaction_class`` on
    the rxn), else ``joining_unprecedented`` (warn, zero yield rows) or
    ``joining_precedent`` (info, the evidence count) via the store's
    :meth:`~precis.store._rxn_ops.RxnMixin.rxn_precedent_count`."""
    out: list[ValidationIssue] = []
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        slugs = _endpoint_reaction_slugs(
            store, tree, ref_id, c.a_block
        ) | _endpoint_reaction_slugs(store, tree, ref_id, c.b_block)
        if not slugs:
            pair = _complementary_pair(
                _endpoint_roles(tree, c.a_block, c.a_port),
                _endpoint_roles(tree, c.b_block, c.b_port),
            )
            if pair is not None:
                name = _joining_name(*pair).strip(" ()") or pair[0]
                out.append(
                    ValidationIssue(
                        rule="joining_unnamed",
                        subject=subject,
                        detail=(
                            f"joining {name} declared by roles only; no "
                            "reaction transition names an rxn — declare "
                            "one with driver_ref=<rxn slug> for a "
                            "precedent read"
                        ),
                        severity="info",
                    )
                )
            continue
        for slug in sorted(slugs):
            ref = store.get_ref(kind="rxn", id=slug)
            reaction_class = (
                None if ref is None else (ref.meta or {}).get("reaction_class")
            )
            if not reaction_class:
                out.append(
                    ValidationIssue(
                        rule="joining_class_unknown",
                        subject=subject,
                        detail=(
                            f"rxn {slug} has no reaction_class; precedent "
                            f"read impossible — edit(kind='rxn', "
                            f"id={slug!r}, reaction_class='RXNO:…')"
                        ),
                        severity="warn",
                    )
                )
                continue
            n_yield, n_rxn = store.rxn_precedent_count(reaction_class)
            if n_yield == 0:
                out.append(
                    ValidationIssue(
                        rule="joining_unprecedented",
                        subject=subject,
                        detail=(
                            f"no precedent: 0 yield rows for "
                            f"{reaction_class} (rxn {slug}) — "
                            "unprecedented step, see "
                            "search(kind='rxn', property='yield', "
                            f"reaction_class='{reaction_class}')"
                        ),
                        severity="warn",
                    )
                )
            else:
                out.append(
                    ValidationIssue(
                        rule="joining_precedent",
                        subject=subject,
                        detail=(
                            f"{n_yield} yield row(s) across {n_rxn} rxn(s) "
                            f"for {reaction_class} (rxn {slug})"
                        ),
                        severity="info",
                    )
                )
    return out
