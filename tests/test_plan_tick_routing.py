"""plan_tick OSS-branch tier selection + turn-cap helpers.

The transport is the router's decision: the ANTHROPIC backend routes the tick
to ``claude -p`` (``CLAUDE_AGENT`` — covered by ``test_plan_tick_claude``); a
tools-capable OSS backend routes it to the in-process ``tools=`` loop. On the
OSS branch :func:`plan_tick._resolve_oss_tier` keeps the ``LLM:<tag>`` cloud
tier when the router would route it to the OSS loop, else falls to ``LOCAL_BIG``
(a served OSS model). DB-free: pure env / router reads, no store, no ``claude``
binary.
"""

from __future__ import annotations

import pytest

from precis.utils.llm.router import Tier
from precis.workers.job_types import plan_tick as pt

# ── _resolve_oss_tier: tag → served tier, backend-aware ────────────────

_TAG_TIER = {
    "opus": Tier.CLOUD_SUPER,
    "sonnet": Tier.CLOUD_MID,
    "haiku": Tier.CLOUD_SMALL,
}


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_resolve_oss_tier_honours_tag_under_tools_capable_cloud(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a tools-capable cloud backend (``openai``) the router routes the
    tag's cloud tier to the OSS loop, so ``_resolve_oss_tier`` keeps it."""
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    assert pt._resolve_oss_tier(alias) is _TAG_TIER[alias]


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_resolve_oss_tier_falls_to_local_big_under_anthropic(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ANTHROPIC backend routes tool-using cloud tiers to ``claude -p``
    (not the OSS loop), so the tick falls to the served ``LOCAL_BIG`` tier."""
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "anthropic")
    assert pt._resolve_oss_tier(alias) is Tier.LOCAL_BIG


@pytest.mark.parametrize("backend", ["openai", "anthropic"])
def test_resolve_oss_tier_local_always_runs_local_big(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``local`` alias names ``LOCAL_BIG`` (the cluster's served qwen +
    tools) — always the OSS loop, so it runs local under either backend."""
    monkeypatch.setenv("PRECIS_LLM_BACKEND", backend)
    assert pt._resolve_oss_tier("local") is Tier.LOCAL_BIG


def test_resolve_oss_tier_unknown_tag_defaults_to_cloud_super_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized tag maps to the cloud-super family, then subject to the
    same backend gate."""
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    assert pt._resolve_oss_tier("no-such-tier") is Tier.CLOUD_SUPER


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
