"""S4 "se hints" (gr516957): the leaf ``Next:`` rows on a leafless
connect, the ``principles`` note kind, and the ``STATUS:done`` finish
gate."""

from __future__ import annotations

from typing import Any

import pytest

from precis import design_tree
from precis.dispatch import Hub
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

#: A synthetic export: two leaves implying rigid+screw, one rigid+snap,
#: one rigid class-only — enough to see the class+mechanism ranking,
#: ``implies.rank`` ordering inside a tier, and the three-row cap.
_EXPORT: dict[str, Any] = {
    "version": 1,
    "nodes": {
        "join/service/threaded/clearance-tapped": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "screw", "rank": 2},
        },
        "join/service/threaded/helicoil": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "screw", "rank": 1},
        },
        "join/service/snap-fit": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "snap", "rank": 1},
        },
        "join/permanent/welded": {"role": "leaf", "implies": {"class": "rigid"}},
    },
    "leaves": [
        "join/permanent/welded",
        "join/service/snap-fit",
        "join/service/threaded/clearance-tapped",
        "join/service/threaded/helicoil",
    ],
    "axes": [],
    "principles": [],
}

#: Six rigid leaves — one over the broad-class cutoff — with ``rank``
#: values spread across the class+mechanism tier.
_RANKED_EXPORT: dict[str, Any] = {
    "version": 1,
    "nodes": {
        "join/screw/low": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "screw", "rank": 5},
        },
        "join/screw/high": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "screw", "rank": 1},
        },
        "join/screw/unranked": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "screw"},
        },
        "join/free/class-only": {"role": "leaf", "implies": {"class": "rigid"}},
        "join/snap/x": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "snap"},
        },
        "join/press/y": {
            "role": "leaf",
            "implies": {"class": "rigid", "mechanism": "press"},
        },
    },
    "leaves": [
        "join/free/class-only",
        "join/press/y",
        "join/screw/high",
        "join/screw/low",
        "join/screw/unranked",
        "join/snap/x",
    ],
    "axes": [],
    "principles": [],
}


@pytest.fixture()
def export(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The export ``dtree_hints`` reads — same monkeypatch seam as
    ``test_design_tree_export.py``'s ``fixture_export``."""
    monkeypatch.setattr(design_tree, "load_export", lambda: _EXPORT)
    return _EXPORT


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    # the shared test DB template carries only core migrations —
    # test_se_plugin.py's fixture shape, transferred.
    return SeHandler(hub=hub)


def _two_blocks() -> list[dict[str, Any]]:
    return [
        {"op": "add_block", "name": "hub"},
        {"op": "add_block", "name": "wheel"},
        {"op": "add_port", "block": "hub", "name": "axle"},
        {"op": "add_port", "block": "wheel", "name": "bore"},
    ]


def _mint_leaf_finding(store: Store, leaf: str) -> int:
    """Stand-in for the conventions importer's row: a live finding
    keyed ``meta.dtree.id``."""
    ref = store.insert_ref(
        kind="finding",
        slug=None,
        title=f"dtree leaf {leaf}",
        meta={"dtree": {"id": leaf}},
    )
    return ref.id


# ── leaf Next: rows ────────────────────────────────────────────────────


def test_leaf_rows_rank_and_degrade(
    handler: SeHandler, store: Store, export: dict[str, Any]
) -> None:
    handler.put(id="hinted", args={"ops": _two_blocks()})
    fi = _mint_leaf_finding(store, "join/service/threaded/clearance-tapped")
    resp = handler.edit(
        id="hinted",
        args={
            "ops": [
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "rigid", "mechanism": "screw"},
                }
            ]
        },
    )
    body = resp.body
    assert "Next:" in body
    # the imported leaf names its fi<N> handle; the rest degrade to the
    # importer's search door.
    assert f"get(kind='finding', id='fi{fi}', extent='fisheye+1hop')" in body
    assert "search(kind='finding', tags=['SPACE:design-conventions']" in body
    # class+mechanism hits lead, ordered by implies.rank (helicoil 1,
    # clearance-tapped 2); the mechanism-free welded leaf trails
    # class-only; 'snap-fit' implies snap — a contradiction, dropped.
    assert body.index("helicoil") < body.index("clearance-tapped")
    assert "join/permanent/welded" in body
    assert "join/service/snap-fit" not in body


def test_leaf_candidates_drop_contradicting_mechanism(
    export: dict[str, Any],
) -> None:
    from precis_se import dtree_hints

    # a snap-implying leaf contradicts a screw joint even under the cap;
    # the mechanism-free welded leaf stays as the class-only tier.
    assert dtree_hints.leaf_candidates("rigid", "screw") == [
        "join/service/threaded/helicoil",
        "join/service/threaded/clearance-tapped",
        "join/permanent/welded",
    ]
    assert dtree_hints.leaf_candidates("rigid", "snap") == [
        "join/service/snap-fit",
        "join/permanent/welded",
    ]
    # a mechanism-less connect contradicts nothing — every class match,
    # rank-ordered (snap-fit 1, helicoil 1 — id breaks the tie — then
    # clearance-tapped 2; welded unranked falls off the cap).
    assert dtree_hints.leaf_candidates("rigid", None) == [
        "join/service/snap-fit",
        "join/service/threaded/helicoil",
        "join/service/threaded/clearance-tapped",
    ]


def test_leaf_candidates_broad_class_drops_class_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_se import dtree_hints

    monkeypatch.setattr(design_tree, "load_export", lambda: _RANKED_EXPORT)
    # six rigid leaves — over the cutoff: only exact class+mechanism
    # hits remain, rank-ordered (high 1 < low 5 < unranked 99).
    assert dtree_hints.leaf_candidates("rigid", "screw") == [
        "join/screw/high",
        "join/screw/low",
        "join/screw/unranked",
    ]
    # a mechanism-less connect on a broad class is all class-only —
    # the tier drops and nothing is suggested.
    assert dtree_hints.leaf_candidates("rigid", None) == []


def test_leaf_rows_none_for_unknown_class(
    handler: SeHandler, export: dict[str, Any]
) -> None:
    handler.put(id="unmatched", args={"ops": _two_blocks()})
    resp = handler.edit(
        id="unmatched",
        args={
            "ops": [
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "revolute", "axis": [0, 0, 1]},
                }
            ]
        },
    )
    assert "kind='finding'" not in resp.body


def test_leaf_rows_skip_leaf_set_and_jointless(
    handler: SeHandler, export: dict[str, Any]
) -> None:
    handler.put(id="covered", args={"ops": _two_blocks()})
    resp = handler.edit(
        id="covered",
        args={
            "ops": [
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {
                        "class": "rigid",
                        "mechanism": "screw",
                        "params": {"leaf": "join/service/threaded/clearance-tapped"},
                    },
                }
            ]
        },
    )
    assert "kind='finding'" not in resp.body
    # a bare connect (no joint at all) has no class to match on.
    handler.put(id="bare", args={"ops": _two_blocks()})
    resp = handler.edit(
        id="bare",
        args={"ops": [{"op": "connect", "a": "hub.axle", "b": "wheel.bore"}]},
    )
    assert "kind='finding'" not in resp.body


def test_leaf_rows_on_set_joint(handler: SeHandler, export: dict[str, Any]) -> None:
    handler.put(
        id="rejointed",
        args={
            "ops": [
                *_two_blocks(),
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {
                        "class": "rigid",
                        "mechanism": "screw",
                        "params": {"leaf": "join/service/threaded/clearance-tapped"},
                    },
                },
            ]
        },
    )
    resp = handler.edit(
        id="rejointed",
        args={
            "ops": [
                {
                    "op": "set_joint",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "rigid", "mechanism": "snap"},
                }
            ]
        },
    )
    assert "join/service/snap-fit" in resp.body
    assert "join/permanent/welded" in resp.body
    # the screw leaves contradict the snap mechanism — no rows for them.
    assert "threaded" not in resp.body


def test_leaf_rows_empty_export(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        design_tree,
        "load_export",
        lambda: {"version": 1, "nodes": {}, "leaves": [], "axes": [], "principles": []},
    )
    handler.put(id="empty", args={"ops": _two_blocks()})
    resp = handler.edit(
        id="empty",
        args={
            "ops": [
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "rigid", "mechanism": "screw"},
                }
            ]
        },
    )
    assert "kind='finding'" not in resp.body


# ── the principles note kind ───────────────────────────────────────────


def test_principles_note_roundtrips(handler: SeHandler, store: Store) -> None:
    from precis_se import persist

    handler.put(
        id="principled",
        args={
            "ops": [
                {"op": "add_block", "name": "hub"},
                {
                    "op": "add_note",
                    "name": "principles-hub",
                    "kind": "principles",
                    "text": "axial DOF intended by the screw; radial stopped "
                    "by the bore; fails safe under gravity",
                    "about": ["hub"],
                },
            ]
        },
    )
    ref = store.get_ref(kind="se", id="principled")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    (note,) = tree.notes
    assert (note.name, note.kind, note.about) == (
        "principles-hub",
        "principles",
        ["hub"],
    )


def test_principles_note_vetting() -> None:
    tree = apply_ops(SeTree(), [{"op": "add_block", "name": "hub"}])
    with pytest.raises(OpError, match="needs 'about'"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_note",
                    "name": "p1",
                    "kind": "principles",
                    "text": "stops every DOF",
                }
            ],
        )
    with pytest.raises(OpError, match="takes no 're'"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_note",
                    "name": "p2",
                    "kind": "principles",
                    "text": "stops every DOF",
                    "about": ["hub"],
                    "re": "q1",
                }
            ],
        )
    with pytest.raises(OpError, match="principles"):
        apply_ops(
            tree,
            [{"op": "add_note", "name": "p3", "kind": "bogus", "text": "x"}],
        )


# ── the STATUS:done finish gate ────────────────────────────────────────


def test_finish_gate_warns_never_refuses(handler: SeHandler) -> None:
    handler.put(id="unfin", args={"ops": _two_blocks()})
    handler.edit(
        id="unfin",
        args={
            "ops": [
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "rigid", "mechanism": "screw"},
                }
            ]
        },
    )
    resp = handler.tag(id="unfin", add=["STATUS:done"])
    assert "finish check" in resp.body
    assert "parts with no principles note: hub, wheel" in resp.body
    assert "connects with no params.leaf: hub.axle—wheel.bore" in resp.body
    # the tag landed despite the warnings — soft gate, never a refusal.
    ref = handler.store.get_ref(kind="se", id="unfin")
    assert ref is not None


def test_finish_gate_clean_design_silent(
    handler: SeHandler, store: Store, export: dict[str, Any]
) -> None:
    from precis_se import persist

    handler.put(
        id="fin",
        args={
            "ops": [
                *_two_blocks(),
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {
                        "class": "rigid",
                        "mechanism": "screw",
                        "params": {"leaf": "join/service/threaded/clearance-tapped"},
                    },
                },
                {
                    "op": "add_note",
                    "name": "p-hub",
                    "kind": "principles",
                    "text": "all six DOF stopped by the tapped pair",
                    "about": ["hub"],
                },
                # the pre-S4 convention — a note NAMED principles-<part>
                # covers the part whatever its kind.
                {
                    "op": "add_note",
                    "name": "principles-wheel",
                    "kind": "decision",
                    "text": "rides the axle; cage stops lateral play",
                },
            ]
        },
    )
    resp = handler.tag(id="fin", add=["STATUS:done"])
    assert "finish check" not in resp.body
    ref = store.get_ref(kind="se", id="fin")
    assert ref is not None and persist.load_tree(store, ref.id) is not None


def test_finish_gate_only_on_status_done(handler: SeHandler) -> None:
    handler.put(id="other-tag", args={"ops": [{"op": "add_block", "name": "hub"}]})
    resp = handler.tag(id="other-tag", add=["topic:fixture"])
    assert "finish check" not in resp.body


def test_finish_gate_skips_in_use_templates(handler: SeHandler) -> None:
    handler.put(
        id="templated",
        args={
            "ops": [
                {"op": "add_block", "name": "leg"},
                {"op": "instance_block", "name": "leg2", "template": "leg"},
            ]
        },
    )
    resp = handler.tag(id="templated", add=["STATUS:done"])
    assert "finish check" in resp.body
    # the template definition is skipped; its instance is the part.
    assert "parts with no principles note: leg2" in resp.body
    assert "leg," not in resp.body


def test_finish_gate_skips_bought_and_matches_dot_anchors(
    handler: SeHandler,
) -> None:
    handler.put(
        id="bought",
        args={
            "ops": [
                *_two_blocks(),
                {
                    "op": "connect",
                    "a": "hub.axle",
                    "b": "wheel.bore",
                    "joint": {"class": "rigid"},
                },
                # a bought block and an instance of a bought template —
                # neither owes a principles note.
                {"op": "add_block", "name": "screw"},
                {
                    "op": "set_binding",
                    "block": "screw",
                    "kind": "component",
                    "design": "m3x10",
                },
                {"op": "add_block", "name": "bearing-spec"},
                {
                    "op": "set_binding",
                    "block": "bearing-spec",
                    "kind": "part",
                    "design": "C-6203",
                },
                {
                    "op": "instance_block",
                    "name": "bearing1",
                    "template": "bearing-spec",
                },
                {
                    "op": "add_note",
                    "name": "p-hub",
                    "kind": "principles",
                    "text": "all six DOF stopped by the tapped pair",
                    "about": ["hub"],
                },
                # a 'part.anchor' about covers the part.
                {
                    "op": "add_note",
                    "name": "p-wheel",
                    "kind": "principles",
                    "text": "rides the axle; cage stops lateral play",
                    "about": ["wheel.bore"],
                },
            ]
        },
    )
    resp = handler.tag(id="bought", add=["STATUS:done"])
    assert "finish check" in resp.body
    assert "parts with no principles note" not in resp.body
    assert "connects with no params.leaf: hub.axle—wheel.bore" in resp.body
