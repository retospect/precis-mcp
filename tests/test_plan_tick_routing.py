"""plan_tick OSS-branch tier selection + turn-cap helpers.

The in-process harness keeps the tag's tier when its resolved model uses
OPENAI_TOOLS and falls back to BIG for Claude models. These checks read
router configuration without a store or Claude subprocess.
"""

from __future__ import annotations

import pytest

from precis.utils.llm.router import Tier
from precis.workers.job_types import plan_tick as pt

# ── _resolve_oss_tier: tag → tier by resolved transport ────────────────

_TAG_TIER = {
    "opus": Tier.FRONTIER,
    "sonnet": Tier.BIG,
    "haiku": Tier.MEDIUM,
}


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_resolve_oss_tier_honours_tag_under_tools_capable_cloud(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag keeps its tier when its model uses the OSS tools loop."""
    monkeypatch.setenv(
        "PRECIS_LLM_MODEL_" + _TAG_TIER[alias].value.upper(), "qwen-test"
    )
    assert pt._resolve_oss_tier(alias) is _TAG_TIER[alias]


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_resolve_oss_tier_falls_to_big_for_claude_model(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude model makes the in-process harness fall back to BIG."""
    monkeypatch.setenv("PRECIS_LLM_MODEL_" + _TAG_TIER[alias].value.upper(), "sonnet")
    assert pt._resolve_oss_tier(alias) is Tier.BIG


@pytest.mark.parametrize("model", ["qwen-test", "sonnet"])
def test_resolve_oss_tier_local_always_runs_big(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local alias and the in-process fallback both resolve to BIG."""
    monkeypatch.setenv("PRECIS_LLM_MODEL_BIG", model)
    assert pt._resolve_oss_tier("local") is Tier.BIG


def test_resolve_oss_tier_unknown_tag_defaults_to_frontier_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized tag uses FRONTIER when its model supports OSS tools."""
    monkeypatch.setenv("PRECIS_LLM_MODEL_FRONTIER", "qwen-test")
    assert pt._resolve_oss_tier("no-such-tier") is Tier.FRONTIER


# ── _max_turns: env-overridable turn ceiling ───────────────────────────


def test_max_turns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_PLAN_TICK_MAX_TURNS", raising=False)
    assert pt._max_turns() == pt._DEFAULT_MAX_TURNS == 60


def test_max_turns_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_TURNS", "90")
    assert pt._max_turns() == 90


def test_max_turns_malformed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_TURNS", "not-an-int")
    assert pt._max_turns() == pt._DEFAULT_MAX_TURNS


# ── validate_submit: model must be a live PLANNER_MODEL_ALIASES member ─


def test_validate_submit_accepts_local_alias() -> None:
    """``local`` is one of PLANNER_MODEL_ALIASES (gr193672-adjacent live bug):
    a todo with meta.llm_tier='local' must clear the submit-time gate, not
    just the closed-vocab PARAMS_SCHEMA it already satisfied."""
    assert pt.validate_submit(None, params={"model": "local"}) is None


@pytest.mark.parametrize("alias", list(pt.PLANNER_MODEL_ALIASES))
def test_validate_submit_accepts_every_planner_alias(alias: str) -> None:
    assert pt.validate_submit(None, params={"model": alias}) is None


def test_validate_submit_rejects_unknown_model() -> None:
    err = pt.validate_submit(None, params={"model": "gpt-9"})
    assert err is not None
    assert "gpt-9" in err
