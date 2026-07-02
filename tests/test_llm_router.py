"""Tests for :mod:`precis.utils.llm.router` — the routing seam (ADR 0046).

DB-free and network-free: the tier→model table is pure env reads, the
transport selection is a pure function, and dispatch is exercised by
monkeypatching the three wrappers so no real ``claude`` subprocess spawns
and no litellm proxy is hit.

The resolver assertions double as the **behavior-preservation contract**
unit 4b relies on: each ``resolve_model(tier)`` must reproduce the default
the corresponding call site resolves to today.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from precis.utils.claude_agent import AgentResult
from precis.utils.claude_p import ClaudePResult
from precis.utils.llm import router
from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Tier,
    Transport,
    dispatch,
    resolve_model,
    result_from_agent,
    result_from_claude_p,
    result_from_openai,
    select_transport,
    transport_for_profile,
)

# ── resolve_model: defaults reproduce current call sites ───────────────


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        # cloud triad — the pinned plan_tick._model_alias defaults, shared
        # verbatim by dream / tex-fix / reviewers / fix-gripe.
        (Tier.CLOUD_SUPER, "claude-opus-4-7"),
        (Tier.CLOUD_MID, "claude-sonnet-4-6"),
        (Tier.CLOUD_SMALL, "claude-haiku-4-5-20251001"),
        # local — the litellm summarizer alias (LlmConfig.model default).
        (Tier.LOCAL_SMALL, "summarizer"),
        # local-big — the ADR 0024 dream alias (resolvable, not dispatchable).
        (Tier.LOCAL_BIG, "qwen-heavy"),
    ],
)
def test_resolve_model_defaults(
    tier: Tier, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear every override so we observe the compiled-in defaults.
    for var in (
        "PRECIS_MODEL_OPUS",
        "PRECIS_MODEL_SONNET",
        "PRECIS_MODEL_HAIKU",
        "PRECIS_SUMMARIZE_MODEL",
        "PRECIS_LOCAL_BIG_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert resolve_model(tier) == expected


@pytest.mark.parametrize(
    ("tier", "env_var"),
    [
        (Tier.CLOUD_SUPER, "PRECIS_MODEL_OPUS"),
        (Tier.CLOUD_MID, "PRECIS_MODEL_SONNET"),
        (Tier.CLOUD_SMALL, "PRECIS_MODEL_HAIKU"),
        (Tier.LOCAL_SMALL, "PRECIS_SUMMARIZE_MODEL"),
        (Tier.LOCAL_BIG, "PRECIS_LOCAL_BIG_MODEL"),
    ],
)
def test_resolve_model_env_override(
    tier: Tier, env_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, "pinned-model-x")
    assert resolve_model(tier) == "pinned-model-x"


def test_tier_table_is_total() -> None:
    # The import-time assert already guards this; make it an explicit test
    # so a future tier without a resolver row fails loudly here too.
    assert set(router._TIER_MODEL) == set(Tier)


# ── select_transport: (tier, tools_needed) → transport ─────────────────


@pytest.mark.parametrize(
    ("tier", "tools_needed", "expected"),
    [
        (Tier.LOCAL_SMALL, False, Transport.LITELLM),
        (Tier.LOCAL_SMALL, True, Transport.LITELLM),  # local-small is tool-less
        (Tier.LOCAL_BIG, False, Transport.LOCAL_BIG_TOOLS),
        (Tier.LOCAL_BIG, True, Transport.LOCAL_BIG_TOOLS),
        (Tier.CLOUD_SMALL, False, Transport.CLAUDE_P),
        (Tier.CLOUD_SMALL, True, Transport.CLAUDE_AGENT),
        (Tier.CLOUD_MID, False, Transport.CLAUDE_P),
        (Tier.CLOUD_MID, True, Transport.CLAUDE_AGENT),
        (Tier.CLOUD_SUPER, False, Transport.CLAUDE_P),
        (Tier.CLOUD_SUPER, True, Transport.CLAUDE_AGENT),
    ],
)
def test_select_transport(tier: Tier, tools_needed: bool, expected: Transport) -> None:
    assert select_transport(tier, tools_needed=tools_needed) is expected


def test_transport_for_profile() -> None:
    from precis.utils.prompt.model import Profile

    # AGENT ⇒ tools ⇒ claude_agent; HELPER ⇒ no tools ⇒ claude_p.
    assert (
        transport_for_profile(Profile.AGENT, Tier.CLOUD_MID) is Transport.CLAUDE_AGENT
    )
    assert transport_for_profile(Profile.HELPER, Tier.CLOUD_SMALL) is Transport.CLAUDE_P


# ── LlmResult normalization from each wrapper's raw shape ──────────────


def test_result_from_agent() -> None:
    raw = AgentResult(
        final_text="done thinking",
        cost_usd=0.42,
        duration_s=3.1,
        turns_used=5,
    )
    got = result_from_agent(raw, model="claude-opus-4-7", tier=Tier.CLOUD_SUPER)
    assert got == LlmResult(
        text="done thinking",
        cost_usd=0.42,
        turns_used=5,
        model="claude-opus-4-7",
        tier=Tier.CLOUD_SUPER,
    )
    assert got.error is None


def test_result_from_claude_p() -> None:
    raw = ClaudePResult(
        data={"verdict": "ok"},
        raw_stdout='{"verdict": "ok"}',
        cost_usd=0.01,
    )
    got = result_from_claude_p(
        raw, model="claude-haiku-4-5-20251001", tier=Tier.CLOUD_SMALL
    )
    # text is the raw stdout (JSON block lives inside); turns None.
    assert got.text == '{"verdict": "ok"}'
    assert got.cost_usd == 0.01
    assert got.turns_used is None
    assert got.tier is Tier.CLOUD_SMALL


@dataclass
class _FakeOpenAI:
    """Duck type of llm_summarize.LlmResult (text + total_tokens)."""

    text: str
    total_tokens: int | None = None


def test_result_from_openai() -> None:
    raw = _FakeOpenAI(text="a gloss", total_tokens=120)
    got = result_from_openai(raw, model="summarizer", tier=Tier.LOCAL_SMALL)
    # local proxy reports tokens, not dollars ⇒ cost_usd None.
    assert got.text == "a gloss"
    assert got.cost_usd is None
    assert got.turns_used is None
    assert got.model == "summarizer"
    assert got.tier is Tier.LOCAL_SMALL


# ── dispatch: routes to the right transport (wrappers monkeypatched) ────


def test_dispatch_cloud_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        calls["prompt"] = prompt
        calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="agent out", cost_usd=1.0, duration_s=1.0, turns_used=3
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_MID, prompt="hi", tools_needed=True))

    assert out.text == "agent out"
    assert out.turns_used == 3
    assert out.error is None
    assert calls["prompt"] == "hi"
    assert calls["model"] == "claude-sonnet-4-6"  # resolved from tier


def test_dispatch_cloud_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_p(prompt: str, **kwargs: object) -> ClaudePResult:
        calls["model"] = kwargs.get("model")
        return ClaudePResult(
            data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.02
        )

    monkeypatch.setattr(router, "call_claude_p", fake_p)
    monkeypatch.delenv("PRECIS_MODEL_HAIKU", raising=False)

    out = dispatch(
        LlmRequest(tier=Tier.CLOUD_SMALL, prompt="judge this", tools_needed=False)
    )

    assert out.text == '{"ok": true}'
    assert out.turns_used is None
    assert calls["model"] == "claude-haiku-4-5-20251001"


def test_dispatch_local(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the lazily-imported LlmClient so no proxy is hit.
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            seen["messages"] = messages
            return _FakeOpenAI(text="local gloss", total_tokens=42)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="summarize me"))

    assert out.text == "local gloss"
    assert out.cost_usd is None
    assert out.model == "summarizer"
    assert seen["model"] == "summarizer"  # resolved tier model overrides config
    assert seen["messages"] == [{"role": "user", "content": "summarize me"}]


def test_dispatch_local_uses_explicit_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            seen["messages"] = messages
            return _FakeOpenAI(text="x")

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, messages=msgs))

    assert seen["messages"] == msgs


def test_dispatch_local_big_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="local-big"):
        dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))


def test_dispatch_folds_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.utils.claude_p import ClaudePError

    def boom(prompt: str, **kwargs: object) -> ClaudePResult:
        raise ClaudePError(
            "claude -p exited 1: kaboom", stdout="partial", stderr="", returncode=1
        )

    monkeypatch.setattr(router, "call_claude_p", boom)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt="x"))

    # Error is folded into the normalized result, not raised.
    assert out.error is not None
    assert "kaboom" in out.error
    assert out.text == "partial"  # partial stdout preserved
    assert out.cost_usd is None


def test_dispatch_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="", cost_usd=None, duration_s=0.0, turns_used=None
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)

    dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            prompt="x",
            tools_needed=True,
            model="pinned-override",
        )
    )
    assert calls["model"] == "pinned-override"
