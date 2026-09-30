"""Resolving a ``{block: state name}`` argument against a design's
declared states — one implementation for every se surface that takes
one: ``get(kind='se', args={'state': …})`` (:mod:`precis_se.handler`)
and the ``relax_chain`` op's ``state=`` key
(:mod:`precis_se.atomic.apply`). Both must reject an unknown block or an
undeclared state the same way, naming what IS available, and neither may
silently apply half a map.

Also the one place a set of resolved states is flattened into the single
occupancy map the chain domain reads (:func:`merged_occupancy`): a walker
state's ``occupancy`` is keyed by strand domain, so two blocks' states
naming the same leg would be a contradiction, refused rather than
last-writer-wins.
"""

from __future__ import annotations

from typing import Any

from precis.design import states as design_states
from precis.errors import BadInput, NotFound
from precis_se.identity import AmbiguousLabel, resolve_block
from precis_se.ops import SeTree

__all__ = ["merged_occupancy", "resolve_state_arg"]


def resolve_state_arg(
    store: Any,
    ref_id: int,
    tree: SeTree,
    raw: Any,
    *,
    what: str = "args.state",
    example: str = (
        "get(kind='se', id='<slug>', view='block', "
        "args={'name': '<block>', 'state': {'<block>': '<state name>'}})"
    ),
) -> dict[str, design_states.BlockState]:
    """``raw`` (``{block: state_name}``) → ``{block name: BlockState}``,
    fully vetted up front. Ordinary blocks only — an instance has no
    states of its own (they live on its template; instance-side posing is
    a later round). ``None``/empty → ``{}``."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BadInput(
            f"{what} must be a JSON object of {{block: state_name}}", next=example
        )
    resolved: dict[str, design_states.BlockState] = {}
    for block_token, state_name in raw.items():
        try:
            node = resolve_block(tree, block_token)
        except AmbiguousLabel as exc:
            raise BadInput(str(exc)) from exc
        if node is None:
            known = ", ".join(sorted(tree.blocks)) or "none"
            raise NotFound(
                f"{what}: no block {block_token!r} in this design — blocks: {known}"
            )
        if node.template is not None:
            raise BadInput(
                f"{what}: block {node.name!r} is an instance (of "
                f"{node.template!r}) — declared states live on the "
                "template, and instance-side posing isn't supported yet; "
                f"pose {node.template!r} instead"
            )
        if node.uid is None:
            raise BadInput(
                f"{what}: block {node.name!r} has no uid yet — states are "
                "declared against a SAVED design (put first, then edit)"
            )
        by_name = {s.name: s for s in design_states.states_for(store, ref_id, node.uid)}
        if not by_name:
            raise BadInput(
                f"{what}: block {node.name!r} has no declared states "
                "(declare_states first)"
            )
        want = str(state_name).strip()
        if want not in by_name:
            raise BadInput(
                f"{what}: block {node.name!r} has no state "
                f"{state_name!r} — declared: {', '.join(sorted(by_name))}"
            )
        resolved[node.name] = by_name[want]
    return resolved


def merged_occupancy(
    resolved: dict[str, design_states.BlockState],
) -> dict[str, str | None]:
    """The one occupancy map a set of resolved states implies. A leg
    domain named by two blocks' states is refused by name."""
    out: dict[str, str | None] = {}
    owner: dict[str, str] = {}
    for block, state in resolved.items():
        for key, target in (state.occupancy or {}).items():
            if key in owner:
                raise BadInput(
                    f"states of {owner[key]!r} and {block!r} both set the "
                    f"occupancy of {key!r} — one walker per leg"
                )
            owner[key] = block
            out[key] = target
    return out
