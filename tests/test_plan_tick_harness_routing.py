"""``plan_tick`` picks its harness from the placement chain, not the backend.

Before 2026-08-07 the branch was ``select_transport(tier, tools_needed=True,
backend)``, which under the default ANTHROPIC backend *always* answers
``CLAUDE_AGENT``. An ``llm.op.plan_tick`` tier override reached ``route()``
but not the branch, so the tick still took the claude harness — leaving the
fleet's largest LLM line item steerable only by the global backend flip, which
moves every call site at once.
"""

from __future__ import annotations

from typing import Any

from precis.utils.llm.router import Rung, Tier, Transport
from precis.workers.job_types.plan_tick import _tick_transport


def _chain(monkeypatch: Any, rungs: list[Rung]) -> None:
    monkeypatch.setattr(
        "precis.workers.job_types.plan_tick.resolve_chain",
        lambda tier, **kw: rungs,
        raising=True,
    )


def _op(monkeypatch: Any, tier: Tier | None) -> None:
    """Registry answer for ``plan_tick`` — ``None`` = not registered."""
    monkeypatch.setattr(
        "precis.utils.llm.operations.resolve_op",
        lambda source: (tier, None) if tier is not None else None,
        raising=True,
    )


def test_openai_tools_rung_selects_the_oss_harness(monkeypatch):
    """The whole point: a BIG chain moves the harness, not just the model."""
    _op(monkeypatch, Tier.BIG)
    _chain(
        monkeypatch,
        [
            Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b-a22b-2507"),
            Rung(transport=Transport.OPENAI_TOOLS, model="z-ai/glm-5.2"),
        ],
    )
    assert _tick_transport("opus") is Transport.OPENAI_TOOLS


def test_claude_agent_rung_keeps_the_claude_harness(monkeypatch):
    _op(monkeypatch, Tier.FRONTIER)
    _chain(monkeypatch, [Rung(transport=Transport.CLAUDE_AGENT)])
    assert _tick_transport("opus") is Transport.CLAUDE_AGENT


def test_registry_tier_overrides_the_meta_llm_tier_alias(monkeypatch):
    """The alias says opus (FRONTIER); the registry says BIG. Registry wins —
    that is what makes `llm.op.plan_tick` a live steering knob."""
    seen: list[Tier] = []

    def _capture(tier: Tier, **kw: Any) -> list[Rung]:
        seen.append(tier)
        return [Rung(transport=Transport.OPENAI_TOOLS)]

    _op(monkeypatch, Tier.BIG)
    monkeypatch.setattr(
        "precis.workers.job_types.plan_tick.resolve_chain", _capture, raising=True
    )
    _tick_transport("opus")
    assert seen == [Tier.BIG]


def test_unregistered_source_falls_back_to_the_alias_tier(monkeypatch):
    """Ships dark: with no registry entry the alias tier is used exactly as
    before, so an unconfigured fleet routes byte-for-byte as it did."""
    seen: list[Tier] = []

    def _capture(tier: Tier, **kw: Any) -> list[Rung]:
        seen.append(tier)
        return [Rung(transport=Transport.CLAUDE_AGENT)]

    _op(monkeypatch, None)
    monkeypatch.setattr(
        "precis.workers.job_types.plan_tick.resolve_chain", _capture, raising=True
    )
    assert _tick_transport("opus") is Transport.CLAUDE_AGENT
    assert seen == [Tier.FRONTIER]


def test_harness_agrees_with_what_dispatch_will_resolve(monkeypatch):
    """Regression guard on the coherence hazard.

    Picking the harness from ``select_transport`` while ``route()`` resolves
    the *chain* could land ``_run_claude_tick`` on a tier whose rung 0 is
    ``openai_tools`` — a claude subprocess handed an OSS model id (the ``dream``
    api_error class). Both must read the same rung.
    """
    rungs = [Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b-a22b-2507")]
    _op(monkeypatch, Tier.BIG)
    _chain(monkeypatch, rungs)

    # What the branch picks must be the transport of the rung route() walks.
    assert _tick_transport("opus") is rungs[0].transport
