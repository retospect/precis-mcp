"""``plan_tick`` picks its harness from the placement chain, not the backend.

Before 2026-08-07 the branch was ``select_transport(tier, tools_needed=True,
backend)``, which under the default ANTHROPIC backend *always* answers
``CLAUDE_AGENT``. An ``llm.op.plan_tick`` tier override reached ``route()``
but not the branch, so the tick still took the claude harness — leaving the
fleet's largest LLM line item steerable only by the global backend flip, which
moves every call site at once.

The second half (al514787 / td510493): the chooser also read only the *tier*
chain — an ``llm.op.plan_tick`` row's own ``chain`` and the request's
``placement`` pin reached ``route()`` but not the harness branch, so a local
rung could be handed to ``_run_claude_tick``. The chooser now resolves the
same ladder ``route()`` walks: op chain → tier chain → placement filter.
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


# ── the op row's own `chain` + the placement pin (al514787 / td510493) ──
#
# The override is patched at live_config so the real resolve_op /
# resolve_op_chain run — the rung-dict grammar and the tools filter
# (filter_tool_rungs) are exercised, not stubbed. Chain dicts use the
# `llm.op.<source>` `chain` grammar the prod row carries.

#: Cloud-first op chain — a `placement='local'` pin must demote the claude
#: rung; with no pin the chain's own order decides.
_CLOUD_CLAUDE_RUNG = {
    "placement": "cloud",
    "model": "claude-opus-4-8",
    "transport": "claude_agent",
}
_LOCAL_OSS_RUNG = {
    "placement": "local",
    "model": "openai/gpt-oss-120b",
    "transport": "openai_tools",
}


def _op_row(monkeypatch: Any, override: dict | None) -> None:
    """The ``llm.op.plan_tick`` app_settings row — ``None`` = no row."""
    monkeypatch.setattr(
        "precis.utils.llm.live_config.op_override",
        lambda _source: override,
        raising=True,
    )


def test_op_chain_local_placement_selects_the_local_rung(monkeypatch):
    """The gripe itself: an ``llm.op.plan_tick`` chain with a local gpt-oss
    rung, tick pinned ``placement='local'`` → the OSS tools loop.

    Before the fix the chooser saw only the (cloud) tier chain, so the local
    rung ``route()`` would dispatch was handed to ``_run_claude_tick``.
    """
    _op_row(monkeypatch, {"chain": [_CLOUD_CLAUDE_RUNG, _LOCAL_OSS_RUNG]})
    _chain(monkeypatch, [Rung(transport=Transport.CLAUDE_AGENT)])
    assert _tick_transport("opus", "local") is Transport.OPENAI_TOOLS


def test_op_chain_rung0_picks_the_harness_without_a_local_pin(monkeypatch):
    """No pin — or a ``'cloud'`` pin — keeps the op chain's leading cloud
    rung on the claude harness; the chooser follows the chain's order, it
    does not manufacture a local preference."""
    _op_row(monkeypatch, {"chain": [_CLOUD_CLAUDE_RUNG, _LOCAL_OSS_RUNG]})
    assert _tick_transport("opus") is Transport.CLAUDE_AGENT
    assert _tick_transport("opus", "cloud") is Transport.CLAUDE_AGENT


def test_op_row_without_a_chain_falls_back_to_the_tier_chain(monkeypatch):
    """An op row with only ``tier``/``model`` keys — or no row at all —
    resolves no op chain, so the tier chain decides exactly as before."""
    _chain(monkeypatch, [Rung(transport=Transport.OPENAI_TOOLS, model="qwen")])
    _op_row(monkeypatch, {"tier": "big"})  # a row, but no `chain` key
    assert _tick_transport("opus") is Transport.OPENAI_TOOLS
    _op_row(monkeypatch, None)  # no row at all
    assert _tick_transport("opus") is Transport.OPENAI_TOOLS


def test_local_placement_with_no_local_rung_still_refuses(monkeypatch):
    """A cloud-only chain + ``placement='local'`` still refuses through
    ``route()``'s placement gate — the harness pick must not manufacture a
    local rung the chain doesn't have, and the existing "no local rung"
    message reaches the outcome's stderr."""
    import precis.workers.planner_prompt as planner_prompt
    from precis import agentlog
    from precis.workers.job_types import plan_tick as pt

    class _FakePrompts:
        system = "SYS"
        user = "USR"

    _op_row(monkeypatch, {"chain": [_CLOUD_CLAUDE_RUNG]})
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("PRECIS_MCP_CONFIG", "mcp.json")
    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda *a, **k: None)
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 55)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)

    # `route` is NOT stubbed — the real placement gate produces the refusal.
    outcome = pt.run(
        store=object(),
        job_ref_id=1,
        parent_ref_id=2,
        params={"model": "opus", "select": {"placement": "local"}},
    )
    assert outcome.exit_code == 1
    assert "has no local rung" in outcome.stderr
