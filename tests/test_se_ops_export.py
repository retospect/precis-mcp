"""``get(kind='se', view='ops')`` — the design → ops → design round-trip
(gripe 457931).

The load-bearing assertion is a single equality: replaying
:func:`precis_se.ops_export.design_ops` into an empty tree reproduces
:func:`precis_se.persist.tree_to_json` of the tree it came from. That one
check is what makes the export trustworthy — it fails the moment an
emitter names a key the op does not accept, or omits a facet the record
carries, which is exactly the class of mistake a per-op-kind serializer
invites (the op vocabulary is register-only, so there is no shared
definition for the two directions to drift from).

No store here: :func:`~precis_se.ops.apply_ops` on a bare
:class:`~precis_se.ops.SeTree` is the whole harness, which also means no
block ever carries a uid — the one thing the round-trip explicitly does
not preserve is therefore not silently masked by the fixture.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.vocab import ThreadingSpec
from precis_se.bom import BomLine
from precis_se.chain.vocab import DomainSpec
from precis_se.handler import SeHandler
from precis_se.measures import MeasureSpec
from precis_se.notes import NoteSpec
from precis_se.ops import ConnectSpec, OpError, PortSpec, SeBlock, SeTree, apply_ops
from precis_se.ops_export import NOT_CARRIED, design_ops, render_ops
from precis_se.persist import tree_to_json


def _tree(ops: list[dict[str, Any]]) -> SeTree:
    return apply_ops(SeTree(), ops)


#: Every collection in a tree is documented unordered (identity is a key,
#: not a position — :class:`~precis_se.ops.SeTree`'s field comments), and
#: the export emits blocks in DEPENDENCY order rather than the order they
#: were authored in. So the round-trip is compared on content, with each
#: list sorted by its own identity.
_SORT_KEYS: dict[str, Any] = {
    "blocks": lambda b: b["name"],
    "connects": lambda c: (c["a_block"], c["a_port"], c["b_block"], c["b_port"]),
    "measures": lambda m: (m["block"], m["name"]),
    "bom": lambda b: (b["item_kind"], b["item"], b["block"] or "", b["a_block"] or ""),
    "notes": lambda n: n["name"],
    "threading": lambda t: (t["a"], t["b"]),
    "domains": lambda d: (d["strand"], d["ord"]),
}


def _norm(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for key, sort_key in _SORT_KEYS.items():
        rows = out.get(key) or []
        out[key] = sorted(rows, key=sort_key)
    return out


def _assert_round_trips(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Author ``ops``, export, replay — and return the exported list so a
    caller can additionally assert on its shape."""
    original = _tree(ops)
    exported = design_ops(original)
    replayed = _tree(exported)
    assert _norm(tree_to_json(replayed)) == _norm(tree_to_json(original))
    return exported


# ── the structural core ─────────────────────────────────────────────────


#: A cart with a wheel template placed once, a 4-up linear array of it, a
#: polar array, ports with and without a pose, a jointed connect, and the
#: three ledgers. Deliberately one design rather than several: the phase
#: ORDER in ``design_ops`` is as much under test as the per-op shapes, and
#: only a design with cross-phase references (a connect naming ports on an
#: arrayed template, BOM hung off that connect) can catch a bad one.
_CART: list[dict[str, Any]] = [
    {"op": "add_block", "name": "cart", "envelope": "box:w0.6d0.4h0.1"},
    {
        "op": "add_block",
        "name": "wheel",
        "parent": "cart",
        "envelope": "cyl:r0.05h0.02",
        "desc": "road wheel",
        "use": "carries the cart",
        "pose": [0.1, 0.0, 0.0],
        "rot": [0.0, 0.0, 0.3],
    },
    {
        "op": "array_block",
        "name": "wheels",
        "template": "wheel",
        "parent": "cart",
        "linear": {"count": 4, "pitch": 0.3, "axis": [1, 0, 0]},
    },
    {
        "op": "array_block",
        "name": "studs",
        "template": "wheel",
        "polar": {"count": 6, "radius": 0.08},
    },
    {"op": "add_block", "name": "axle", "parent": "cart"},
    {"op": "instance_block", "name": "axle2", "template": "axle", "parent": "cart"},
    {
        "op": "add_port",
        "block": "cart",
        "name": "bore",
        "roles": ["bore"],
        "pose": [0.0, 0.0, 0.05],
        "rot": [0.0, 0.0, 0.0],
        "annotations": {"note": "reamed"},
    },
    {"op": "add_port", "block": "wheel", "name": "hub", "roles": ["shaft"]},
    {"op": "add_port", "block": "axle", "name": "end", "roles": ["shaft"]},
    {
        "op": "connect",
        "a": "cart.bore",
        "b": "wheel.hub",
        "joint": {"class": "revolute", "mechanism": "bearing"},
    },
    {
        "op": "add_measure",
        "block": "wheel",
        "name": "dia",
        "value": 0.1,
        "min": 0.098,
        "max": 0.102,
        "reason": "supplier tolerance",
    },
    {
        "op": "add_bom",
        "block": "wheels",
        "item_kind": "component",
        "item": "bearing-608",
        "qty": 2,
    },
    {
        "op": "add_bom",
        "a": "cart.bore",
        "b": "wheel.hub",
        "item_kind": "component",
        "item": "circlip-8",
    },
    {
        "op": "add_note",
        "name": "q-bearing",
        "kind": "question",
        "text": "is a 608 stiff enough at 4x load?",
        "about": ["wheel"],
    },
]


def test_a_full_design_round_trips_through_its_own_ops_export() -> None:
    """The headline property: export then replay is the identity on a
    design's stored state."""
    _assert_round_trips(_CART)


def test_the_export_replays_without_any_destructive_op() -> None:
    """Final-state, not history — a design whose authoring REMOVED things
    exports as if they were never added, so no ``remove_*``/``disconnect``
    ever appears and the list is safe to replay into an empty design."""
    exported = _assert_round_trips(
        [
            *_CART,
            {"op": "add_block", "name": "scrap", "parent": "cart"},
            {"op": "remove_block", "block": "scrap"},
            {"op": "disconnect", "a": "cart.bore", "b": "wheel.hub"},
        ]
    )
    emitted = {op["op"] for op in exported}
    assert not [name for name in emitted if name.startswith(("remove_", "clear_"))]
    assert "disconnect" not in emitted
    assert "scrap" not in {op.get("name") for op in exported}


def test_blocks_are_emitted_after_the_parent_and_template_they_name() -> None:
    """The phase order has to be a real dependency order: ``apply_ops``
    resolves ``parent``/``template`` against blocks that already exist, so
    an export that emitted authoring order would fail to replay whenever a
    child was authored first."""
    exported = _assert_round_trips(
        [
            # authored child-first and instance-before-template on purpose
            {"op": "add_block", "name": "root"},
            {"op": "add_block", "name": "leaf", "parent": "root"},
            {"op": "add_block", "name": "mid", "parent": "root"},
            {"op": "instance_block", "name": "leaf2", "template": "leaf"},
        ]
    )
    at = {op["name"]: i for i, op in enumerate(exported) if "name" in op}
    assert at["root"] < at["leaf"] < at["leaf2"]
    assert at["root"] < at["mid"]


# ── the per-op key traps ────────────────────────────────────────────────


def test_an_arrayed_block_re_nests_its_stored_spec_under_its_kind() -> None:
    """``array_block`` takes ``linear={...}``/``polar={...}`` but STORES a
    flattened ``{'kind': 'linear', ...}``. Emitting the stored dict
    verbatim is the obvious mistake and the op would reject it."""
    exported = _assert_round_trips(_CART)
    by_name = {op.get("name"): op for op in exported}
    assert by_name["wheels"]["linear"] == {
        "count": 4,
        "pitch": 0.3,
        "axis": [1.0, 0.0, 0.0],
    }
    assert by_name["studs"]["polar"]["count"] == 6
    assert "kind" not in by_name["wheels"]


def test_an_instance_never_carries_its_templates_envelope_or_prose() -> None:
    """``_instance_shared`` REJECTS envelope/desc/use on an instance
    rather than dropping them, so emitting the resolved values — which
    ``view='tree'`` happily shows — would make the export un-replayable."""
    exported = _assert_round_trips(_CART)
    for op in exported:
        if op["op"] in ("instance_block", "array_block"):
            assert not {"envelope", "desc", "use"} & set(op)


def test_a_note_and_a_measure_use_the_ops_key_names_not_the_records() -> None:
    """``add_note`` takes ``text`` where ``NoteSpec`` stores ``body``, and
    ``add_measure`` takes ``min``/``max`` where ``MeasureSpec`` stores
    ``min_value``/``max_value`` — the two places the two directions
    genuinely disagree."""
    exported = _assert_round_trips(_CART)
    (note,) = [op for op in exported if op["op"] == "add_note"]
    assert note["text"].startswith("is a 608")
    assert "body" not in note
    (measure,) = [op for op in exported if op["op"] == "add_measure"]
    assert (measure["min"], measure["max"]) == (0.098, 0.102)
    assert not {"min_value", "max_value"} & set(measure)


def test_a_zero_pose_is_omitted_rather_than_written_out() -> None:
    """Every block defaults to the origin, so emitting ``[0,0,0]`` would
    trible the size of a real export for no information."""
    exported = _assert_round_trips([{"op": "add_block", "name": "solo"}])
    assert exported == [{"op": "add_block", "name": "solo"}]


def test_a_false_or_zero_field_survives_the_emptiness_filter() -> None:
    """The filter drops ``None`` and empty containers only: a reverse
    domain (``forward=False``) and a zero ``start`` are real answers, and
    a plain falsy test would eat both."""
    exported = _assert_round_trips(
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 32,
                "lattice": "honeycomb",
            },
            {"op": "add_block", "name": "s"},
            {"op": "declare_strand", "block": "s"},
            {
                "op": "add_domain",
                "strand": "s",
                "helix": "h",
                "start": 0,
                "end": 8,
                "forward": False,
            },
        ]
    )
    (domain,) = [op for op in exported if op["op"] == "add_domain"]
    assert domain["start"] == 0
    assert domain["forward"] is False


# ── the rendered view ───────────────────────────────────────────────────


def test_the_rendered_body_states_every_gap_and_fences_a_put_payload() -> None:
    """A partial copy that does not say so is the failure mode worth
    guarding: the header prints the caveats, and the fenced block is
    exactly ``put``'s ``text=`` shape so it can be pasted unreshaped."""
    body = render_ops(_tree(_CART), "cart")
    for gap in NOT_CARRIED:
        assert gap in body
    payload = json.loads(body.split("```json")[1].split("```")[0])
    assert set(payload) == {"ops"}
    assert _norm(tree_to_json(_tree(payload["ops"]))) == _norm(
        tree_to_json(_tree(_CART))
    )


def test_an_empty_design_exports_a_replayable_empty_list() -> None:
    assert design_ops(SeTree()) == []
    assert "0 op(s)" in render_ops(SeTree(), "empty")


# ── the stated gap, asserted rather than only documented ────────────────


def test_facet_origin_stamps_are_not_carried_because_no_op_accepts_them() -> None:
    """``origins`` records user-vs-proposed per facet and is write-only
    from the ops' side, so the round-trip cannot preserve it. Asserted so
    the docstring's claim fails loudly if an op ever starts accepting it."""
    tree = _tree(
        [
            {"op": "add_block", "name": "b"},
            {
                "op": "set_envelope",
                "block": "b",
                "envelope": "box:w1d1h1",
                "origin": "proposed",
            },
        ]
    )
    assert tree.blocks["b"].origins.get("envelope") == "proposed"
    replayed = _tree(design_ops(tree))
    assert replayed.blocks["b"].origins == {}
    # …and the facet ITSELF still round-trips; only the stamp is lost.
    assert replayed.blocks["b"].envelope == "box:w1d1h1"


def test_a_cross_design_template_is_not_treated_as_a_local_dependency() -> None:
    """A ``'slug#block'`` template lives in another design's namespace, so
    the ordering walk must skip it rather than look it up and drop the
    block from the export entirely."""
    tree = SeTree()
    tree.own_slug = "here"
    with pytest.raises(OpError):
        # guards the fixture: a bare cross-design instance needs a
        # resolver, so the ordering property is asserted on the tree built
        # by hand below rather than through apply_ops.
        apply_ops(tree, [{"op": "instance_block", "name": "x", "template": "lib#w"}])
    tree.blocks["x"] = tree.make_block(name="x", template="lib#w")
    (op,) = design_ops(tree)
    assert op == {"op": "instance_block", "name": "x", "template": "lib#w"}


# ── end to end, through the store ───────────────────────────────────────


_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _fenced_ops(body: str) -> list[dict[str, Any]]:
    return list(json.loads(body.split("```json")[1].split("```")[0])["ops"])


def _tree_of(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def test_a_stored_design_copies_into_a_second_design_through_the_view(
    handler: SeHandler,
) -> None:
    """The use the export exists for: read a design out of one database
    and rebuild it in another, with nothing hand-written in between.

    Run against the STORE rather than a bare tree because that is where
    uids are minted — the one thing the round-trip does not carry, and so
    the one thing a store-free test could not have caught if the export
    had leaked them into the ops list.
    """
    handler.put(id="cart-src", text=json.dumps({"ops": _CART}))
    exported = _fenced_ops(handler.get(id="cart-src", view="ops").body)
    assert not any("uid" in op for op in exported)

    handler.put(id="cart-copy", text=json.dumps({"ops": exported}))
    src, copy = _tree_of(handler, "cart-src"), _tree_of(handler, "cart-copy")

    def _without_restamped(payload: dict[str, Any]) -> dict[str, Any]:
        """Drop exactly the two documented gaps, so anything ELSE that
        fails to copy still fails this assertion: uids (minted fresh) and
        note ``created_at`` (stamped by persist when the copy is written).
        """
        out = _norm(payload)
        out["blocks"] = [
            {k: v for k, v in b.items() if k != "uid"} for b in out["blocks"]
        ]
        out["notes"] = [
            {k: v for k, v in n.items() if k != "created_at"} for n in out["notes"]
        ]
        return out

    assert _without_restamped(tree_to_json(copy)) == _without_restamped(
        tree_to_json(src)
    )
    # …and the copy really did get its OWN identities, not the source's —
    # the documented caveat, asserted rather than only written down.
    src_uids = {b.uid for b in src.blocks.values()}
    copy_uids = {b.uid for b in copy.blocks.values()}
    assert src_uids and copy_uids and not (src_uids & copy_uids)


def test_the_view_is_reachable_and_an_unknown_view_still_lists_it(
    handler: SeHandler,
) -> None:
    handler.put(id="reach1", text="{}")
    assert "ops export" in handler.get(id="reach1", view="ops").body
    with pytest.raises(BadInput) as exc:
        handler.get(id="reach1", view="nosuch")
    # the roster lives on the breaking hint, not in the cause line
    assert "view='ops'" in str(exc.value.next)


# ── totality: no field may be added without a verdict ───────────────────


#: Every field of every record the export walks, with what the round-trip
#: does about it. The failure this guards is silent and delayed: someone
#: adds a field to ``SeBlock``, persist stores it, and the export quietly
#: stops being a faithful copy — with no test failing, because a fixture
#: written before the field existed cannot exercise it.
#:
#: ``"op"``   round-trips — some emitted op carries it.
#: ``"gap"``  deliberately not carried; named in :data:`NOT_CARRIED`.
#: ``"derived"`` recomputed or transient, never authored design intent, so
#:            there is nothing to carry.
_VERDICTS: dict[str, dict[str, str]] = {
    "SeBlock": {
        "array": "op",
        "bound": "op",
        "bound_kind": "op",
        "build_frame": "op",
        "chain": "op",
        "chromophore": "op",
        "derived": "derived",
        "descr": "op",
        "dof": "op",
        "envelope": "op",
        "local_pose": "op",
        "local_rot": "op",
        "mode": "op",
        "name": "op",
        "objectives": "op",
        "origins": "gap",
        "parent": "op",
        "pending_current_state": "derived",
        "pending_state_poses": "derived",
        "pending_states": "derived",
        "pending_transitions": "derived",
        "ports": "op",
        # the COMPOSED world placement; local_pose/local_rot above are the
        # authored values the pose ops actually take
        "pose": "derived",
        "process_overrides": "op",
        "rot": "derived",
        "template": "op",
        "uid": "gap",
        "use": "op",
    },
    "PortSpec": {
        "annotations": "op",
        "axis_atom": "gap",
        "bound_atom": "gap",
        "bound_design": "gap",
        "direction": "op",
        "expected_element": "op",
        "expected_hybridization": "op",
        "name": "op",
        "phase_atom": "gap",
        "pose": "op",
        # re-stamped by add_port from whether pose/rot was given
        "pose_source": "derived",
        "roles": "op",
        "rot": "op",
        "rot_source": "derived",
    },
    "ConnectSpec": {
        "a_block": "op",
        "a_port": "op",
        "b_block": "op",
        "b_port": "op",
        "joint": "op",
        "kind": "op",
        "objectives": "op",
        "optical": "op",
    },
    "MeasureSpec": {
        "block": "op",
        "datum": "op",
        "max_value": "op",
        "min_value": "op",
        "name": "op",
        "origin": "op",
        "reason": "op",
        "relation": "op",
        "strength": "op",
        "unit": "op",
        "value": "op",
    },
    "BomLine": {
        "a_block": "op",
        "a_port": "op",
        "b_block": "op",
        "b_port": "op",
        "block": "op",
        "item": "op",
        "item_kind": "op",
        "qty": "op",
        "reason": "op",
        "uom": "op",
    },
    "NoteSpec": {
        "about": "op",
        "body": "op",
        "created_at": "gap",
        "kind": "op",
        "name": "op",
        "origin": "op",
        "re": "op",
    },
    "ThreadingSpec": {"a": "op", "b": "op"},
    "DomainSpec": {
        "end": "op",
        # TRANSIENT, per-state only: apply_occupancy sets it on a copy and
        # group_domains drops the row — never persisted, never authored
        "free": "derived",
        "forward": "op",
        "geometry": "op",
        "helix": "op",
        "loop_before_nt": "op",
        # relax_chain's settled output; add_domain refuses it by contract
        "loop_curve": "derived",
        "ord": "op",
        "overrides": "op",
        "start": "op",
        "strand": "op",
    },
}

_RECORDS = {
    "SeBlock": SeBlock,
    "PortSpec": PortSpec,
    "ConnectSpec": ConnectSpec,
    "MeasureSpec": MeasureSpec,
    "BomLine": BomLine,
    "NoteSpec": NoteSpec,
    "ThreadingSpec": ThreadingSpec,
    "DomainSpec": DomainSpec,
}


@pytest.mark.parametrize("record", sorted(_RECORDS))
def test_every_stored_field_has_an_export_verdict(record: str) -> None:
    """Adding a field to one of these records forces a decision here.

    Without this, the export degrades silently as the schema grows: the
    round-trip equality only covers what a fixture happens to exercise,
    and a fixture cannot exercise a field written after it.
    """
    live = {f.name for f in dataclasses.fields(_RECORDS[record])}
    recorded = set(_VERDICTS[record])
    assert live == recorded, (
        f"{record} fields changed. For each one decide: emit it from "
        f"precis_se.ops_export (verdict 'op'), or declare it uncarried and "
        f"add a line to NOT_CARRIED (verdict 'gap'), or confirm it is "
        f"derived/transient (verdict 'derived') — then record it in "
        f"_VERDICTS. added={sorted(live - recorded)} "
        f"removed={sorted(recorded - live)}"
    )


def test_every_declared_gap_is_named_in_the_views_own_header() -> None:
    """A ``gap`` verdict is only honest if the rendered view says so — the
    header is where a reader of a copied design finds out what is
    missing."""
    blob = " ".join(NOT_CARRIED).lower()
    for record, verdicts in _VERDICTS.items():
        for field_name, verdict in verdicts.items():
            if verdict != "gap":
                continue
            token = {
                "uid": "uid",
                "origins": "origin stamps",
                "created_at": "created_at",
                "bound_design": "structure binding",
                "bound_atom": "structure binding",
                "axis_atom": "structure binding",
                "phase_atom": "structure binding",
            }[field_name]
            assert token.lower() in blob, (
                f"{record}.{field_name} is declared a gap but NOT_CARRIED "
                f"never mentions {token!r}"
            )
