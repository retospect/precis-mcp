"""The budget degradation ladder (gr479851): step order, opt-in, pause
classification and the local fallback pick."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from precis.utils.llm import degrade
from precis.utils.llm.degrade import DegradeStep, degrade_steps, is_budget_pause
from precis.utils.llm.router import Tier, resolve_model


def test_no_opt_in_means_no_ladder() -> None:
    assert degrade_steps(Tier.FRONTIER, opt_in=False) == []


def test_frontier_walks_big_medium_then_local() -> None:
    assert degrade_steps(Tier.FRONTIER, opt_in=True) == [
        DegradeStep(Tier.BIG, resolve_model(Tier.BIG), "cloud"),
        DegradeStep(Tier.MEDIUM, resolve_model(Tier.MEDIUM), "cloud"),
        DegradeStep(Tier.BIG, None, "local"),
    ]


def test_medium_goes_straight_to_local() -> None:
    steps = degrade_steps(Tier.MEDIUM, opt_in=True)
    assert [s.local for s in steps] == [True]


@pytest.mark.parametrize(
    ("paused", "reason_class", "expected"),
    [
        (True, "quota", True),
        (True, "budget", True),
        (True, "rate", False),
        (False, "quota", False),
    ],
)
def test_is_budget_pause(paused: bool, reason_class: str, expected: bool) -> None:
    assert is_budget_pause(paused, reason_class) is expected


def test_tier_for_model() -> None:
    assert degrade.tier_for_model("local") is Tier.BIG
    assert degrade.tier_for_model("Opus") is Tier.FRONTIER
    assert degrade.tier_for_model(resolve_model(Tier.MEDIUM)) is Tier.MEDIUM
    assert degrade.tier_for_model("some-custom-model") is Tier.FRONTIER


def _llm_card(model_id: str, tools: bool, ctx: int | None = None) -> SimpleNamespace:
    facts: dict[str, object] = {
        "supported_parameters": ["tools"] if tools else ["temperature"]
    }
    if ctx is not None:
        facts["context_length"] = ctx
    return SimpleNamespace(meta={"model_id": model_id, "facts_openrouter": facts})


def _bind(monkeypatch: pytest.MonkeyPatch, cards: list[SimpleNamespace]) -> None:
    store = SimpleNamespace(list_refs=lambda **kw: cards)
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: store)
    monkeypatch.setattr(
        "precis.utils.llm.local_serving.served_locally",
        lambda m: m != "beta-unserved",
    )


def test_local_fallback_model_picks_lowest_served_tool_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        [
            _llm_card("zeta", True),
            _llm_card("alpha-notools", False),
            _llm_card("mid", True),
            _llm_card("beta-unserved", True),
        ],
    )
    assert degrade.local_fallback_model(tools_needed=True) == "mid"
    assert degrade.local_fallback_model(tools_needed=False) == "alpha-notools"


def test_local_fallback_model_prefers_the_larger_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        [_llm_card("aaa-small", True, 8192), _llm_card("zzz", True, 131072)],
    )
    assert degrade.local_fallback_model(tools_needed=True) == "zzz"


def test_local_fallback_model_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: None)
    assert degrade.local_fallback_model(tools_needed=True) is None
