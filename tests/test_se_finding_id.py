"""`se` findings carry a stable id (skill precis-se-help, "Finding ids"):
derived from ``(rule, sorted participants)`` via the hash step shared with
``pcb``, rendered as an ``id`` column, with a caller-supplied ``previous``
diffed into new / still / gone. Fixture shape from ``test_se_block_states.py``."""

from __future__ import annotations

import json
import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.finding_id import finding_hash
from precis.pcb.drc import DrcFinding, finding_key
from precis.store import Store
from precis_se import finding_id as fid
from precis_se.handler import SeHandler


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _blocks(b_x: float) -> list[dict[str, object]]:
    return [
        {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": [b_x, 0.0, 0.0],
        },
    ]


_CONNECT: list[dict[str, object]] = [
    {"op": "add_port", "block": "a", "name": "p"},
    {"op": "add_port", "block": "b", "name": "q"},
    {"op": "connect", "a": "a.p", "b": "b.q"},
]


def _put(handler: SeHandler, slug: str, ops: list[dict[str, object]]) -> None:
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def _ids(body: str) -> list[str]:
    return re.findall(r"\b[0-9a-f]{12}\b", body)


def test_id_is_order_independent_and_ignores_detail() -> None:
    assert fid.finding_id("undeclared_interpenetration", "a—b") == fid.finding_id(
        "undeclared_interpenetration", "b—a"
    )
    assert fid.finding_id("r", "a—b") != fid.finding_id("r", "a—c")
    assert fid.finding_id("r", "a—b") != fid.finding_id("s", "a—b")


def test_se_and_pcb_share_the_hash_rule_shape() -> None:
    assert fid.finding_id("r", "x—y") == finding_hash("r", ["x", "y"])
    f = DrcFinding(rule="r", severity="error", where="", detail="", objects=())
    assert finding_key(f) == finding_hash("r", [], None)


def test_parse_previous_forms_and_refusal() -> None:
    assert fid.parse_previous(None) is None
    assert fid.parse_previous({"previous": "aaaaaaaaaaaa, bbbbbbbbbbbb"}) == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
    ]
    assert fid.parse_previous({"previous": ["aaaaaaaaaaaa"]}) == ["aaaaaaaaaaaa"]
    with pytest.raises(BadInput):
        fid.parse_previous({"previous": ["nope"]})
    with pytest.raises(BadInput):
        fid.parse_previous({"previous": 3})


def test_annotate_is_a_multiset_diff() -> None:
    rows, text = fid.annotate(
        [{"k": 1}, {"k": 2}, {"k": 3}],
        ["a" * 12, "a" * 12, "b" * 12],
        ["a" * 12, "c" * 12],
    )
    assert [r["delta"] for r in rows] == ["still", "new", "new"]
    assert "2 new, 1 still, 1 gone" in text
    assert "- " + "c" * 12 in text


def test_validate_ids_stable_across_runs_and_depth_change(handler: SeHandler) -> None:
    _put(handler, "fid1", _blocks(0.005))
    ids = _ids(handler.get(id="fid1", view="validate").body)
    assert ids
    assert _ids(handler.get(id="fid1", view="validate").body) == ids

    # deeper overlap, same two blocks: same id, reads still
    _put(handler, "fid1", _blocks(0.002))
    body = handler.get(id="fid1", view="validate", args={"previous": ids}).body
    assert f"0 new, {len(ids)} still, 0 gone" in body


def test_validate_delta_reports_gone(handler: SeHandler) -> None:
    _put(handler, "fid2", _blocks(0.005))
    ids = _ids(handler.get(id="fid2", view="validate").body)
    _put(handler, "fid2", _blocks(0.5))  # blocks apart: overlap finding resolved
    body = handler.get(id="fid2", view="validate", args={"previous": ids}).body
    assert "gone since previous" in body
    assert ids[0] in body
    body2 = handler.get(id="fid2", view="validate", args={"previous": ["0" * 12]}).body
    assert "gone" in body2 and "0" * 12 in body2


def test_clearance_digest_and_drc_carry_ids(handler: SeHandler) -> None:
    _put(handler, "fid3", [*_blocks(0.005), *_CONNECT])
    ids = _ids(handler.get(id="fid3", view="clearance").body)
    assert len(ids) == 1
    assert _ids(handler.get(id="fid3", view="clearance").body) == ids

    _put(handler, "fid3", [*_blocks(0.002), *_CONNECT])
    again = handler.get(id="fid3", view="clearance", args={"previous": ids}).body
    assert _ids(again)[0] == ids[0]
    assert "0 new, 1 still, 0 gone" in again

    drc = handler.get(id="fid3", view="drc", args={"previous": []}).body
    assert "vs previous:" in drc
