"""Focused tests for the diagnostic Pourbaix quest slice."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.quest import QuestHandler, _check_operating_conditions
from precis.quest import pourbaix as pourbaix_mod
from precis.quest.frontier import _META_NON_MEASURE
from precis.quest.pourbaix import _request


class TestOperatingConditions:
    def test_accepts_human_set_point_and_window(self) -> None:
        _check_operating_conditions(
            {
                "operating_conditions": {
                    "U_RHE": -0.2,
                    "pH": 7,
                    "ion_conc_M": 1e-6,
                    "window": {"U_RHE": [-0.4, 0], "pH": [7, 10]},
                }
            }
        )

    @pytest.mark.parametrize(
        "conditions",
        [
            {"pH": 7},
            {"U_RHE": float("nan"), "pH": 7},
            {"U_RHE": -0.2, "pH": True},
            {"U_RHE": -0.2, "pH": 7, "ion_conc_M": 0},
            {"U_RHE": -0.2, "pH": 7, "window": {"pH": [8, 7]}},
            {"U_RHE": -0.2, "pH": 7, "extra": 1},
        ],
    )
    def test_rejects_malformed_conditions(self, conditions: dict[str, Any]) -> None:
        with pytest.raises(BadInput):
            _check_operating_conditions({"operating_conditions": conditions})

    def test_meta_only_edit_accepts_validated_conditions(self, store: Any) -> None:
        handler = QuestHandler(hub=Hub(store=store))
        created = handler.put(text="A test quest")
        import re

        match = re.search(r"qu(\d+)", created.body)
        assert match is not None
        quest_id = int(match.group(1))
        response = handler.edit(
            id=quest_id,
            meta={"operating_conditions": {"U_RHE": -0.2, "pH": 7}},
        )
        assert "operating_conditions" in response.body


def test_request_key_tracks_conditions_and_geometry() -> None:
    candidate = SimpleNamespace(id=42, meta={"geom_hash_c": "geom-a"})
    first = _request({"operating_conditions": {"U_RHE": -0.2, "pH": 7}}, candidate)
    assert first is not None
    params, key = first
    assert params["point"] == {"U_RHE": -0.2, "pH": 7}

    changed_conditions = _request(
        {"operating_conditions": {"U_RHE": -0.1, "pH": 7}}, candidate
    )
    changed_geometry = _request(
        {"operating_conditions": {"U_RHE": -0.2, "pH": 7}},
        SimpleNamespace(id=42, meta={"geom_hash_c": "geom-b"}),
    )
    assert changed_conditions is not None and changed_conditions[1] != key
    assert changed_geometry is not None and changed_geometry[1] != key
    assert _request({}, candidate) is None


def test_dispatch_requires_conditions_and_dedupes_same_basis(monkeypatch: Any) -> None:
    quest = SimpleNamespace(
        id=9, meta={"operating_conditions": {"U_RHE": -0.2, "pH": 7}}
    )
    candidate = SimpleNamespace(id=42, kind="structure", meta={"geom_hash_c": "g"})
    store = SimpleNamespace(fetch_refs_by_ids=lambda _ids: {9: quest})
    monkeypatch.setattr("precis.quest.gaps._live_servers", lambda *_: [candidate])

    class JobHandler:
        calls = 0

        def put(self, **kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(body="created job id=71 (STATUS:queued)")

    handler = JobHandler()
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    existing: set[str] = set()

    def find_job(_store: Any, key: str) -> tuple[int, str] | None:
        return (71, "queued") if key in existing else None

    monkeypatch.setattr("precis.quest.compute._find_job_by_idem_key", find_job)
    notes = pourbaix_mod.dispatch_pourbaix(store, 9, hub=hub)
    assert notes == ["created job id=71 (STATUS:queued)"]
    assert handler.calls == 1
    existing.add(handler_key := handler_key_from_put(handler, candidate, quest))
    assert pourbaix_mod.dispatch_pourbaix(store, 9, hub=hub) == []
    assert handler.calls == 1


def test_dispatch_does_nothing_without_operating_conditions() -> None:
    quest = SimpleNamespace(id=9, meta={})
    store = SimpleNamespace(fetch_refs_by_ids=lambda _ids: {9: quest})
    hub = SimpleNamespace(
        handler_for=lambda _kind: (_ for _ in ()).throw(
            AssertionError("dispatch called")
        )
    )

    assert pourbaix_mod.dispatch_pourbaix(store, 9, hub=hub) == []


def handler_key_from_put(_handler: Any, candidate: Any, quest: Any) -> str:
    built = _request(quest.meta, candidate)
    assert built is not None
    return built[1]


def test_harvest_stamps_successful_matching_verdict(monkeypatch: Any) -> None:
    quest = SimpleNamespace(
        id=9, meta={"operating_conditions": {"U_RHE": -0.2, "pH": 7}}
    )
    candidate = SimpleNamespace(id=42, title="Cu slab", meta={"geom_hash_c": "g"})
    job = SimpleNamespace(
        meta={
            "verdict": {
                "point": {"verdict": "stable", "U_RHE": -0.2, "pH": 7},
                "worst_in_window": "stable",
                "dG_pbx_eV_atom": -0.03,
                "domain": [{"label": "Cu"}],
                "basis": {
                    "U_RHE": -0.2,
                    "pH": 7,
                    "ion_conc_M": 1e-6,
                    "window": None,
                    "mp_version": "v2026.10",
                },
            }
        }
    )
    rows = {9: quest, 71: job}
    stamps: list[tuple[int, dict[str, Any]]] = []
    entries: list[dict[str, Any]] = []
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {
            ref_id: rows[ref_id] for ref_id in ids if ref_id in rows
        },
        stamp_ref_meta=lambda ref_id, meta: stamps.append((ref_id, meta)),
    )
    built = _request(quest.meta, candidate)
    assert built is not None
    monkeypatch.setattr(
        "precis.quest.compute._find_job_by_idem_key",
        lambda *_: (71, "succeeded"),
    )
    monkeypatch.setattr(
        "precis.quest.logbook.append_entry", lambda *_a, **kw: entries.append(kw)
    )
    monkeypatch.setattr("precis.utils.handle_registry.try_format", lambda *_: "st42")

    note = pourbaix_mod.harvest_pourbaix_candidate(store, 9, candidate)

    assert note == "bulk Pourbaix result for [st42]: stable at U_RHE=-0.2 V, pH=7"
    assert stamps[0][0] == 42
    assert stamps[0][1]["pourbaix_verdict"] == "stable"
    assert stamps[0][1]["pourbaix_worst_in_window"] == "stable"
    assert stamps[0][1]["pourbaix_dG_eV_atom"] == -0.03
    assert stamps[0][1]["pourbaix_job_id"] == 71
    assert stamps[0][1]["pourbaix_basis"]["mp_version"] == "v2026.10"
    assert entries and entries[0]["entry_type"] == "result"


def test_pourbaix_evidence_is_not_a_pareto_measure() -> None:
    assert {
        "pourbaix_verdict",
        "pourbaix_worst_in_window",
        "pourbaix_dG_eV_atom",
        "pourbaix_domain",
        "pourbaix_basis",
        "pourbaix_job_id",
    } <= _META_NON_MEASURE
