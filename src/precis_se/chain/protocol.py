"""``make_steps`` — a state machine's transitions written out as an
ordered ``make`` tree, one step per transition, so the protocol a design
implies (illuminate at 405 nm, then at 365 nm, …) exists as a thing another
tool can consume — the zone compiler of
``docs/backlog/ewod-synthesis-protocol.md`` reads exactly this shape.

The walk is greedy over the block's stored transitions: from ``start``
take the first declared edge to a state not yet visited, repeat until no
edge leads anywhere new. For hand-over-hand stations (``declare_stations``)
that is ``st0 → st1 → st2 …`` along the forward edges; the reverse edges
are never taken because their targets were already visited. A design
whose graph branches gets the first-declared branch, which the echo names
so an author can pass ``start=`` or reorder the declaration.

Each step carries the numbers a dispenser needs in ``meta``: ``illuminate
= {wavelength_nm, duration_s?}`` for a light edge (``duration_s`` from the
transition's ``params.duration_s`` when authored, otherwise absent — never
guessed), ``rxn`` for a reaction edge, and ``station`` = the state the step
reaches. The tree is linked ``made-by`` from the design so both
``get(kind='make')``'s "makes:" line and the design's links view show the
pairing.

Handler-level like ``relax_chain`` (:data:`precis_se.atomic.apply.HANDLER_LEVEL_OPS`):
it reads the store (transitions live in ``design_transitions``, not on the
tree) and writes another kind. **The write is not deferred**: the make tree
is created before the caller's ``save_tree`` commits the se revision, so a
failed save leaves the tree behind — the same residual ``structure_save``
documents. It refuses to append to an existing tree (re-running would
duplicate every step): delete it or pass ``make=`` a fresh slug.
"""

from __future__ import annotations

from typing import Any

from precis.design import states as design_states
from precis.errors import BadInput
from precis_se.chain.spectral import parse_wavelength_nm

__all__ = ["op_make_steps"]


def op_make_steps(store: Any, tree: Any, op: dict[str, Any], design_slug: str) -> str:
    """``{'op': 'make_steps', 'block': <state-carrying block>, 'start'?:
    <state>, 'make'?: <make slug>}`` → the echo line. See the module
    docstring for the walk and the step shape."""
    block = str(op.get("block") or "").strip()
    if not block:
        raise BadInput(
            "make_steps: needs block= (the block whose transitions become steps)",
            next="make_steps{block:'w'}",
        )
    node = tree.blocks.get(block)
    if node is None:
        raise BadInput(f"make_steps: no block {block!r} in this design")
    ref = store.get_ref(kind="se", id=design_slug)
    uid = getattr(node, "uid", None)
    if ref is None or uid is None:
        raise BadInput(
            "make_steps: needs a SAVED design with declared transitions — put "
            "the design (declare_stations / declare_transitions) first, then "
            "edit it with make_steps"
        )
    transitions = design_states.transitions_for(store, ref.id, uid)
    if not transitions:
        raise BadInput(
            f"make_steps: block {block!r} has no transitions — declare_stations "
            "or declare_transitions first"
        )
    states = {s.name: s for s in design_states.states_for(store, ref.id, uid)}
    start = str(op.get("start") or "").strip()
    if not start:
        start = design_states.current_state(store, ref.id, uid) or (
            "st0" if "st0" in states else transitions[0].from_state
        )
    if start not in states:
        raise BadInput(
            f"make_steps: start state {start!r} is not one of {block!r}'s "
            f"states ({', '.join(sorted(states)) or 'none'})"
        )
    by_from: dict[str, list[design_states.Transition]] = {}
    for t in transitions:
        by_from.setdefault(t.from_state, []).append(t)
    order: list[design_states.Transition] = []
    visited = {start}
    cursor = start
    while True:
        edge = next(
            (t for t in by_from.get(cursor, []) if t.to_state not in visited), None
        )
        if edge is None:
            break
        order.append(edge)
        visited.add(edge.to_state)
        cursor = edge.to_state
    if not order:
        raise BadInput(
            f"make_steps: no transition leaves {start!r} toward an unvisited "
            "state — pass start= a state with an outgoing edge"
        )
    make_slug = str(op.get("make") or f"{design_slug}-{block}-protocol").strip()
    if store.get_ref(kind="make", id=make_slug) is not None:
        raise BadInput(
            f"make_steps: make tree {make_slug!r} already exists — re-running "
            "would duplicate its steps; delete(kind='make', id=...) it, or pass "
            "make='<new slug>'"
        )
    make_ref, _root = store.drafts.create_draft(
        name=make_slug, title=f"{design_slug}: {block} protocol", kind="make"
    )
    for i, t in enumerate(order, 1):
        meta: dict[str, Any] = {
            "design": design_slug,
            "block": block,
            "from_state": t.from_state,
            "station": t.to_state,
            "driver_kind": t.driver_kind,
        }
        target = states.get(t.to_state)
        reach = f"{t.from_state} → {t.to_state}"
        if target is not None and target.descr:
            reach += f" ({target.descr})"
        if t.driver_kind == "light":
            illuminate: dict[str, Any] = {}
            nm = parse_wavelength_nm(t.driver_ref)
            if nm is not None:
                illuminate["wavelength_nm"] = nm
            elif t.driver_ref:
                illuminate["source"] = t.driver_ref
            duration = (t.params or {}).get("duration_s")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                illuminate["duration_s"] = float(duration)
            meta["illuminate"] = illuminate
            text = f"step {i}: illuminate at {t.driver_ref or '? (no driver_ref)'} — {reach}"
        elif t.driver_kind == "reaction":
            meta["rxn"] = t.driver_ref
            text = f"step {i}: react ({t.driver_ref or 'no rxn named'}) — {reach}"
        else:
            text = f"step {i}: {t.driver_kind}{' ' + t.driver_ref if t.driver_ref else ''} — {reach}"
        store.drafts.add_chunks(
            ref_id=make_ref.id,
            chunk_kind="step",
            text=text,
            meta=meta,
            kind="make",
            split=False,
        )
    store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=make_ref.id,
        relation="made-by",
        meta={"block": block},
    )
    path = " → ".join([start, *(t.to_state for t in order)])
    return (
        f"make_steps: {len(order)} step(s) → make:{make_slug} ({path}); "
        f"get(kind='make', id='{make_slug}')"
    )
