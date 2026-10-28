"""Conditional mechanical/environment review uses the tapeout ledger (gr519925).

The DB tests exercise shipped sync, assignments, handler dispatch and persisted
verdicts. Only the model call is faked, as in test_pcb_review_job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.checklist.pcb import mech_env_applies
from precis.dispatch import Hub
from precis.handlers.checklist import ChecklistHandler
from precis.handlers.pcb import PcbHandler
from precis.jobs.checklist_sync import (
    bundled_checklists_dir,
    check_drift,
    load_checklist_file,
    sync_all,
)
from precis.pcb import review
from precis.store import Store
from precis.workers.auto_check_evaluators import checklist_clean
from tests.test_pcb_review_job import _board, _install, _run

_CHECKLIST = "pcb-mech-env"
_ITEMS = {
    "bend-zones-clear",
    "stiffeners-support-loads",
    "coverlay-clearances",
    "coating-keepoff",
    "post-coating-access",
    "mounting-strain-relief",
    "operating-temperature",
    "condensation-ingress",
}
_YAML = (
    Path(__file__).resolve().parents[1] / "src/precis/data/checklists/pcb-mech-env.yaml"
)


@pytest.fixture
def mech_board(store: Store) -> int:
    assert sync_all(store, src_dir=bundled_checklists_dir())["status"] == "ok"
    ref_id = _board(store, slug="mech-board")
    store.pcb_merge_board_meta(ref_id, {"flex": True, "coating": "parylene"})
    return ref_id


@pytest.mark.parametrize(
    "design, applies",
    [
        ({"board": None}, False),
        ({"board": {"meta": {}}}, False),
        ({"board": {"meta": {"process": "rigid-4layer-35um"}}}, False),
        ({"board": {"meta": {"process": "flex-2layer-35um"}}}, True),
        ({"board": {"meta": {"flex": True, "coating": "parylene"}}}, True),
        ({"board": {"meta": {"enclosure": "sealed"}}}, True),
        ({"board": {"meta": {"environment": "outdoor, wet"}}}, True),
        ({"board": {"meta": {"outdoor": True}}}, True),
        ({"board": {"meta": {"wet": True}}}, True),
        # Fallback declarations are presence-based, including incomplete ones.
        ({"board": {"meta": {"coating": None}}}, True),
        (
            {"board": {"meta": {}}, "features": [{"ftype": "bend_zone"}]},
            True,
        ),
        (
            {"board": {"meta": {}}, "features": [{"ftype": "stiffener"}]},
            True,
        ),
        (
            {"board": {"meta": {}}, "features": [{"ftype": "coverlay_opening"}]},
            True,
        ),
        (
            {"board": {"meta": {}}, "features": [{"ftype": "tape_mask"}]},
            True,
        ),
    ],
)
def test_mech_env_declarations(design: dict[str, Any], applies: bool) -> None:
    assert mech_env_applies(design) is applies


def test_shipped_checklist_has_all_eight_judgment_items() -> None:
    checklist = load_checklist_file(_YAML)
    assert checklist.name == _CHECKLIST
    assert checklist.default_for == ("pcb",)
    assert {item["name"] for item in checklist.items} == _ITEMS
    assert len(checklist.items) == len(_ITEMS)
    assert all(item["decidability"] == "judgment" for item in checklist.items)
    assert all(item["prevents"].strip() for item in checklist.items)
    coverlay = next(i for i in checklist.items if i["name"] == "coverlay-clearances")
    assert "0.1" in coverlay["body"] and "0.15" in coverlay["body"]


def test_shipped_sync_is_idempotent(store: Store) -> None:
    assert sync_all(store, src_dir=bundled_checklists_dir())["status"] == "ok"
    checklist = store.checklist_get(_CHECKLIST)
    assert checklist is not None
    assert checklist["origin"] == "shipped"
    before = store.checklist_items_current(checklist["id"])
    assert len(before) == 8
    assert all(item["origin"] == "shipped" for item in before)
    assert sync_all(store, src_dir=bundled_checklists_dir())["status"] == "ok"
    assert store.checklist_items_current(checklist["id"]) == before
    assert check_drift(store, src_dir=bundled_checklists_dir()) == []


def test_assignment_and_review_queue_exclude_plain_rigid_board(
    store: Store, mech_board: int
) -> None:
    plain = _board(store, slug="rigid-board")
    handler = ChecklistHandler(hub=Hub(store=store))
    for ref_id, slug, applies in [
        (mech_board, "mech-board", True),
        (plain, "rigid-board", False),
    ]:
        assigned = store.checklist_assignments_for_target(ref_id, target_kind="pcb")
        names = {row["name"] for row in assigned}
        assert (_CHECKLIST in names) is applies
        assert "pcb-tapeout" in names
        listing = handler.get(target=f"pcb:{slug}").body
        assert (_CHECKLIST in listing) is applies
        detail = handler.get(id=_CHECKLIST, target=f"pcb:{slug}").body
        assert ("no checklist assigned" not in detail) is applies
        queued_names = {st.name for st in review.queue(store, ref_id).todo}
        assert queued_names & _ITEMS == (_ITEMS if applies else set())


def test_explicit_assignment_still_works_on_rigid_board(
    store: Store, mech_board: int
) -> None:
    plain = _board(store, slug="rigid-board")
    handler = ChecklistHandler(hub=Hub(store=store))
    handler.edit(id=_CHECKLIST, op="assign", target="pcb:rigid-board")
    assert {st.name for st in review.queue(store, plain).todo} >= _ITEMS
    assert (
        "no checklist assigned"
        not in handler.get(id=_CHECKLIST, target="pcb:rigid-board").body
    )


@pytest.mark.parametrize("declaration", ["process", "tape_mask"])
def test_stored_flex_process_and_coating_geometry_assign_checklist(
    store: Store, mech_board: int, declaration: str
) -> None:
    ref_id = _board(store, slug="declared-board")
    if declaration == "process":
        store.pcb_merge_board_meta(ref_id, {"process": "flex-4layer-35um"})
    else:
        store.pcb_apply(
            slug="declared-board",
            title="declared-board",
            components=[],
            nets=[],
            connections=[],
            features=[
                {
                    "ftype": "tape_mask",
                    "layer": "top",
                    "geom": {
                        "polygon": [[0, 0], [2, 0], [2, 2], [0, 2]],
                        "kind": "parylene",
                    },
                }
            ],
        )
    assigned = store.checklist_assignments_for_target(ref_id, target_kind="pcb")
    assert _CHECKLIST in {row["name"] for row in assigned}
    assert {st.name for st in review.queue(store, ref_id).todo} >= _ITEMS


def test_unassigned_rigid_board_cannot_reuse_old_clean_verdicts(
    store: Store, mech_board: int
) -> None:
    _board(store, slug="rigid-board")
    handler = ChecklistHandler(hub=Hub(store=store))
    handler.edit(id=_CHECKLIST, op="assign", target="pcb:rigid-board")
    for item in sorted(_ITEMS):
        handler.edit(
            id=_CHECKLIST,
            op="verdict",
            target="pcb:rigid-board",
            item=item,
            verdict="n/a",
            evidence={"reason": "plain indoor rigid board"},
        )
    spec = {
        "type": "checklist_clean",
        "checklist": _CHECKLIST,
        "target": "pcb:rigid-board",
    }
    assert checklist_clean.evaluate(store, spec) is True
    handler.edit(id=_CHECKLIST, op="unassign", target="pcb:rigid-board")
    assert checklist_clean.evaluate(store, spec) is None


def test_op_review_and_job_record_every_mechanical_item_once(
    store: Store, mech_board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(
        monkeypatch,
        {
            "verdict": "not_checkable",
            "reason": "not checkable (gr520329): required geometry is absent",
            "quote": "",
            "anchors": ["R1"],
            "inputs": [],
        },
    )
    pcb = PcbHandler(hub=Hub(store=store))
    # The named-item route must find an item outside pcb-tapeout.
    assert (
        "enqueued"
        in pcb.put(
            id="mech-board", args={"op": "review", "item": "post-coating-access"}
        ).body
    )
    assert (
        "enqueued"
        in pcb.put(id="mech-board", args={"op": "review", "page_size": 60}).body
    )
    summaries, failures = _run(store, mech_board, limit=60)
    assert not failures
    checklist = store.checklist_get(_CHECKLIST)
    assert checklist is not None
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=mech_board, checklist_id=checklist["id"]
    )
    assert set(verdicts) == _ITEMS
    for name, verdict in verdicts.items():
        assert verdict["verdict"] == "not checkable", name
        assert "not checkable (gr520329)" in verdict["evidence"]["reason"]
        assert verdict["origin"] == "machine"
        assert verdict["checked_by"] == "pcb_review"
        assert not verdict["anchors"]
        assert "pcb:mech-env" in verdict["inputs"]
    assert all(name in "\n".join(summaries) for name in _ITEMS)
    assert not review.queue(store, mech_board).todo
    calls = model.calls
    repeated, failures = _run(store, mech_board, limit=60)
    assert not failures
    assert "nothing to review" in repeated[0]
    assert model.calls == calls
    assert (
        "nothing to review"
        in pcb.put(
            id="mech-board", args={"op": "review", "item": "post-coating-access"}
        ).body
    )


def test_unsupported_geometry_cannot_silently_pass(
    store: Store, mech_board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        {
            "verdict": "pass",
            "reason": "looks fine",
            "quote": "R1 is present",
            "anchors": ["R1"],
            "inputs": [],
        },
    )
    summaries, failures = _run(store, mech_board, item="mounting-strain-relief")
    assert not failures
    status = next(
        st
        for st in review.queue(store, mech_board).statuses
        if st.name == "mounting-strain-relief"
    )
    assert status.status == "not checkable"
    assert "not checkable (gr520329)" in status.evidence["reason"]
    assert "not checkable (gr520329)" in summaries[0]


def test_operating_temperature_needs_facts_even_for_generic_passive(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sync_all(store, src_dir=bundled_checklists_dir())["status"] == "ok"
    ref, _, _ = store.pcb_apply(
        slug="passive-temperature",
        title="passive-temperature",
        components=[
            {
                "refdes": "R1",
                "label": "10k",
                "footprint": "0402",
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}],
        connections=[
            {"net": "VCC", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    )
    store.pcb_merge_board_meta(ref.id, {"environment": {"max_c": 85}})
    _install(
        monkeypatch,
        {
            "verdict": "pass",
            "reason": "generic passive",
            "quote": "R1: 10k 0402",
            "anchors": ["R1"],
            "inputs": [],
        },
    )
    _, failures = _run(store, ref.id, item="operating-temperature")
    assert not failures
    status = next(
        st
        for st in review.queue(store, ref.id).statuses
        if st.name == "operating-temperature"
    )
    assert status.status == "not checkable"
    assert "R1" in status.evidence["reason"]
    assert "no datasheet facts" in status.evidence["reason"]


def test_environment_and_fab_features_invalidate_mechanical_verdicts(
    store: Store, mech_board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        {
            "verdict": "not_checkable",
            "reason": "not checkable (gr520329): no assembly geometry",
            "anchors": ["R1"],
            "inputs": [],
        },
    )
    item = "mounting-strain-relief"
    _run(store, mech_board, item=item)
    assert not review.queue(store, mech_board, item=item).todo
    store.pcb_merge_board_meta(mech_board, {"environment": {"max_c": 85}})
    changed = review.queue(store, mech_board, item=item).todo
    assert len(changed) == 1 and changed[0].status.startswith("stale")
    _run(store, mech_board, item=item)
    assert not review.queue(store, mech_board, item=item).todo
    # pcb_graph omits feature geometry: the mechanical input must still move.
    store.pcb_apply(
        slug="mech-board",
        title="mech-board",
        components=[],
        nets=[],
        connections=[],
        features=[
            {
                "ftype": "tape_mask",
                "layer": "top",
                "geom": {
                    "polygon": [[0, 0], [2, 0], [2, 2], [0, 2]],
                    "kind": "parylene",
                },
            }
        ],
    )
    changed = review.queue(store, mech_board, item=item).todo
    assert len(changed) == 1
    assert changed[0].status.startswith("stale (input changed: pcb:mech-env)")
