"""Stored build reports are readable without geometry or write access."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeBlock, SeTree


class ReadOnlyStore:
    def __init__(self, generated: Any) -> None:
        self.calls: list[tuple[str, str]] = []
        self.generated = generated

    def get_ref(self, *, kind: str, id: str) -> Any:
        self.calls.append((kind, id))
        if kind == "se":
            return SimpleNamespace(id=1, slug="design", meta={})
        if id == "built":
            return SimpleNamespace(meta={"generated": self.generated})
        return None


@pytest.fixture
def setup_report(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SeHandler, ReadOnlyStore, SeTree]:
    record = {
        "generator": "join",
        "report": {
            "ok": False,
            "findings": [
                {
                    "severity": "error",
                    "code": "seam.leak",
                    "where": "rim",
                    "message": "open seam",
                    "span": [2, 4],
                }
            ],
        },
    }
    store = ReadOnlyStore(record)
    tree = SeTree(
        blocks={
            "joined": SeBlock(
                name="joined", uid=41, bound_kind="structure", bound="built"
            ),
            "plain": SeBlock(name="plain"),
            "missing": SeBlock(name="missing", bound_kind="structure", bound="gone"),
        }
    )
    monkeypatch.setattr(persist, "load_tree", lambda *_args: copy.deepcopy(tree))
    return SeHandler(hub=Hub(store=cast(Store, store))), store, tree


@pytest.mark.parametrize("selector", ["joined", "#41"])
def test_named_stored_report_without_writes(setup_report: Any, selector: str) -> None:
    handler, store, tree = setup_report
    before = copy.deepcopy((tree, store.generated))
    body = handler.get(id="design", view="report", args={"block": selector}).body
    for expected in (
        "structure: built",
        "generator: join",
        "NOT ok",
        "error",
        "seam.leak",
        "rim",
        "open seam",
        "2:4",
    ):
        assert expected in body
    assert "plain" not in body
    assert (tree, store.generated) == before
    assert store.calls == [("se", "design"), ("structure", "built")]


def test_all_blocks_and_unavailable_are_not_passes(setup_report: Any) -> None:
    handler, _, _ = setup_report
    body = handler.get(id="design", view="report").body
    assert (
        body.index("block 'joined'")
        < body.index("block 'missing'")
        < body.index("block 'plain'")
    )
    assert body.count("Stored report unavailable") == 2
    assert "report: ok" not in body


@pytest.mark.parametrize(
    "record", [None, {}, {"report": {}}, {"report": {"ok": True, "findings": None}}]
)
def test_missing_report_or_findings_is_unavailable(
    setup_report: Any, record: Any
) -> None:
    handler, store, _ = setup_report
    store.generated = record
    body = handler.get(id="design", view="report", args={"block": "joined"}).body
    assert "unavailable" in body
    assert "report: ok" not in body


@pytest.mark.parametrize("ok", [True, False])
def test_empty_report_preserves_recorded_status(setup_report: Any, ok: bool) -> None:
    handler, store, _ = setup_report
    store.generated = {"generator": "join", "report": {"ok": ok, "findings": []}}
    body = handler.get(id="design", view="report", args={"block": "joined"}).body
    assert f"report: {'ok' if ok else 'NOT ok'} — no findings" in body


def test_report_rejects_missing_block_and_state_override(setup_report: Any) -> None:
    handler, _, _ = setup_report
    with pytest.raises(NotFound):
        handler.get(id="design", view="report", args={"block": "unknown"})
    with pytest.raises(BadInput):
        handler.get(id="design", view="report", args={"state": {"joined": "open"}})
    with pytest.raises(BadInput):
        handler.get(id="design", view="report", args={"block": " "})


def test_real_join_report_through_one_get(store: Store) -> None:
    handler = SeHandler(hub=Hub(store=store))
    spec = "hexfold 0.2\na: tube(8,0, len=3)\n"
    handler.put(
        id="join-report",
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": spec},
                    "name": "a",
                },
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": spec},
                    "name": "b",
                },
            ]
        },
    )
    handler.edit(
        id="join-report",
        ops=[
            {"op": "join", "name": "joined", "a": "a.out", "b": "b.in"},
        ],
    )
    body = handler.get(id="join-report", view="report", args={"block": "joined"}).body
    assert "generator: join" in body
    assert "structure: join-report-joined" in body
    assert "seam.rings" in body
    assert "INFO" in body
