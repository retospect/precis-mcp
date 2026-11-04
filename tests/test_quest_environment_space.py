"""Human-set environment bounds: write refusals, read defaults and tick isolation."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.quest import QuestHandler, _check_environment_space
from precis.quest.environment_space import effective_environment_space
from precis.quest.tick import run_quest_tick


def _space() -> dict[str, Any]:
    return {
        "axes": {
            "T": [350, 900],
            "p_total": [0.1, 100, "log"],
            "NO": [0, 1],
            "H2": [0.01, 1, "log"],
            "U_RHE": [-0.4, 0],
            "pH": [7, 10],
            "ion_conc_M": [1e-6, 0.1, "log"],
        },
        "TOF_floor": 0.001,
        "selectivity_floor": 0.9,
        "tau_s": 2,
        "weights": {"m_th": 1, "m_kin": 0, "m_HER": 1, "s": 2, "r": 1, "m_Ea": 1},
    }


def _quest(handler: QuestHandler) -> int:
    created = handler.put(text="Find each slab's best environment")
    match = re.search(r"\bqu(\d+)\b", created.body)
    assert match is not None
    return int(match.group(1))


def test_environment_space_round_trips_and_shows(store: Any) -> None:
    handler = QuestHandler(hub=Hub(store=store))
    qid = _quest(handler)
    conditions = {"U_RHE": -0.2, "pH": 7}
    handler.edit(id=qid, meta={"operating_conditions": conditions})
    space = _space()
    ack = handler.edit(id=qid, meta={"environment_space": space})
    assert "environment_space" in ack.body
    meta = store.get_ref(kind="quest", id=qid).meta
    assert meta["environment_space"] == space
    assert meta["operating_conditions"] == conditions
    shown = next(
        line.removeprefix("environment_space: ")
        for line in handler.get(id=qid).body.splitlines()
        if line.startswith("environment_space: ")
    )
    assert json.loads(shown) == space
    assert "environment_space:" in handler.get(id=qid, view="meta").body
    assert "environment_space:" in handler.get(id=qid, view="fisheye").body

    # A smaller human edit replaces the block, removing prior optional goals.
    replacement = {"axes": {}, "TOF_floor": 0.01}
    handler.edit(id=qid, meta={"environment_space": replacement})
    assert store.get_ref(kind="quest", id=qid).meta["environment_space"] == replacement


@pytest.mark.parametrize(
    "patch",
    [
        {"axes": {"T": [900, 350]}},
        {"axes": {"temperature": [350, 900]}},
        {"axes": {"p_total": [0, 100, "log"]}},
        {"axes": {"NO": [-1, 1]}},
        {"axes": {"T": [True, 900]}},
        {"axes": {"pH": [7, float("nan")]}},
        {"axes": {"T": [350, float("inf")]}},
        {"axes": {"T": [350, 10**400]}},
        {"axes": {"T": [350]}},
        {"axes": {"T": "350:900"}},
        {"axes": {"p_total": [0.1, 100, "linear"]}},
        {"axes": []},
        {"TOF_floor": 0},
        {"TOF_floor": -1},
        {"TOF_floor": True},
        {"TOF_floor": float("nan")},
        {"TOF_floor": 10**400},
        {"selectivity_floor": 0},
        {"selectivity_floor": -0.1},
        {"selectivity_floor": 1.1},
        {"tau_s": 0},
        {"tau_s": float("inf")},
        {"weights": {}},
        {"weights": {"s": 0}},
        {"weights": {"s": -1}},
        {"weights": {"s": True}},
        {"weights": {"s": float("nan")}},
        {"weights": {"s": 10**400}},
        {"weights": {"unknown": 1}},
        {"weights": []},
        {"unknown": 1},
    ],
)
def test_environment_space_refuses_invalid_values(patch: dict[str, Any]) -> None:
    with pytest.raises(BadInput) as exc:
        _check_environment_space({"environment_space": {**_space(), **patch}})
    assert "environment_space" in str(exc.value)
    assert "edit(kind='quest'" in str(exc.value.next)


@pytest.mark.parametrize("value", [None, [], {}, {"axes": {}}, {"TOF_floor": 0.001}])
def test_environment_space_requires_block_shape(value: Any) -> None:
    with pytest.raises(BadInput) as exc:
        _check_environment_space({"environment_space": value})
    assert exc.value.next


@pytest.mark.parametrize(
    "patch",
    [{"axes": {"T": [900, 350]}}, {"TOF_floor": 0}, {"axes": {"typo": [1, 2]}}],
)
def test_invalid_edit_preserves_previous_space(
    store: Any, patch: dict[str, Any]
) -> None:
    handler = QuestHandler(hub=Hub(store=store))
    qid = _quest(handler)
    space = _space()
    handler.edit(id=qid, meta={"environment_space": space})
    with pytest.raises(BadInput) as exc:
        handler.edit(id=qid, meta={"environment_space": {**space, **patch}})
    assert exc.value.next
    assert store.get_ref(kind="quest", id=qid).meta["environment_space"] == space


def test_valid_minimal_and_fixed_space() -> None:
    _check_environment_space({})
    _check_environment_space({"environment_space": _space()})
    _check_environment_space({"environment_space": {"axes": {}, "TOF_floor": 0.001}})
    _check_environment_space(
        {"environment_space": {"axes": {"U_RHE": [-0.2, -0.2]}, "TOF_floor": 0.001}}
    )


def test_absent_space_uses_kinetics_box_and_operating_conditions() -> None:
    from precis_pathway.kinetics_job import QUEST_KINETICS_SWEEP

    meta = {
        "operating_conditions": {
            "U_RHE": -0.2,
            "pH": 7,
            "ion_conc_M": 1e-6,
            "window": {"U_RHE": [-0.5, 0]},
        }
    }
    before = deepcopy(meta)
    space = effective_environment_space(meta)
    assert space == {
        "axes": {
            "T": [350.0, 900.0],
            "p_total": [0.1, 100.0, "log"],
            "U_RHE": [-0.5, 0],
            "pH": [7, 7],
            "ion_conc_M": [1e-6, 1e-6],
        },
        "TOF_floor": 0.001,
        "tau_s": 1.0,
    }
    for axis in ("T", "p_total"):
        assert space["axes"][axis][:2] == QUEST_KINETICS_SWEEP["axes"][axis]
    assert QUEST_KINETICS_SWEEP["log"]["p_total"] is True
    space["axes"]["U_RHE"][0] = -1
    assert meta == before


def test_no_meta_and_no_conditions_do_not_invent_coordinates() -> None:
    assert effective_environment_space(None) == {
        "axes": {"T": [350.0, 900.0], "p_total": [0.1, 100.0, "log"]},
        "TOF_floor": 0.001,
        "tau_s": 1.0,
    }


def test_declared_space_overrides_defaults_without_mutation() -> None:
    declared: dict[str, Any] = {
        "axes": {"T": [400, 800], "U_RHE": [-0.8, -0.1]},
        "TOF_floor": 0.01,
        "selectivity_floor": 0.9,
        "weights": {"s": 2},
    }
    meta = {
        "operating_conditions": {"U_RHE": -0.2, "pH": 7, "window": {"pH": [5, 9]}},
        "environment_space": declared,
    }
    before = deepcopy(meta)
    space = effective_environment_space(meta)
    assert space == {
        **declared,
        "axes": {**declared["axes"], "p_total": [0.1, 100.0, "log"], "pH": [5, 9]},
        "tau_s": 1.0,
    }
    space["axes"]["T"][0] = 500
    space["weights"]["s"] = 9
    assert meta == before
    assert effective_environment_space({"environment_space": _space()})["tau_s"] == 2


@pytest.mark.parametrize("has_space", [False, True])
def test_tick_cannot_set_or_replace_environment_space(
    store: Any, has_space: bool
) -> None:
    handler = QuestHandler(hub=Hub(store=store))
    qid = _quest(handler)
    original = _space() if has_space else None
    conditions = {"U_RHE": -0.2, "pH": 7}
    handler.edit(id=qid, meta={"operating_conditions": conditions})
    if original is not None:
        handler.edit(id=qid, meta={"environment_space": original})
    attempted = {"axes": {"T": [400, 800]}, "TOF_floor": 1}
    patch = {
        "environment_space": attempted,
        "operating_conditions": {"U_RHE": 0.5, "pH": 14},
    }
    payload = {
        "logbook": [{"entry_type": "observation", "text": "Inspect the stored bounds"}],
        **patch,
        "meta": patch,
        "meta_patch": patch,
    }

    def dispatch(_request: Any) -> Any:
        return SimpleNamespace(data=payload, text="", error=None, cost_usd=0.0)

    outcome = run_quest_tick(store, qid, dispatch_fn=dispatch, compute=False)
    assert outcome.status == "succeeded"
    assert outcome.logbook_added == 1  # The tick applied its permitted writes.
    meta = store.get_ref(kind="quest", id=qid).meta
    if original is None:
        assert "environment_space" not in meta
    else:
        assert meta["environment_space"] == original
    assert meta["operating_conditions"] == conditions
