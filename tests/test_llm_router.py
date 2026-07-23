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
    Backend,
    LlmRequest,
    LlmResult,
    Tier,
    Transport,
    dispatch,
    resolve_backend,
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
        # cloud triad — the pinned plan_tick._model_alias defaults. The
        # cloud-super default is the consolidated opus-4.8 reasoning tier
        # (reviewers / dream / fix-gripe / generic claude_agent all resolve it).
        (Tier.CLOUD_SUPER, "claude-opus-4-8"),
        (Tier.CLOUD_MID, "claude-sonnet-5"),
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
        (Tier.LOCAL_BIG, False, Transport.OPENAI_TOOLS),
        (Tier.LOCAL_BIG, True, Transport.OPENAI_TOOLS),
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
        raw_stdout="<stream-json>",
        terminal_reason="max_turns",
    )
    got = result_from_agent(raw, model="claude-opus-4-7", tier=Tier.CLOUD_SUPER)
    assert got == LlmResult(
        text="done thinking",
        cost_usd=0.42,
        turns_used=5,
        model="claude-opus-4-7",
        tier=Tier.CLOUD_SUPER,
        duration_s=3.1,  # preserved for dream/review telemetry
        # The raw stream + terminal reason ride through so a caller (plan_tick)
        # can keep a debuggable transcript and map an exhaustion to a resume.
        raw_text="<stream-json>",
        terminal_reason="max_turns",
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
    assert got.data == {"verdict": "ok"}  # parsed dict preserved for judges


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
    assert out.duration_s == 1.0  # telemetry preserved through dispatch
    assert out.error is None
    assert calls["prompt"] == "hi"
    assert calls["model"] == "claude-sonnet-5"  # resolved from tier


def test_dispatch_agent_forwards_disallowed_tools_and_log_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new LlmRequest knobs reach call_claude_agent byte-for-byte, so a
    migrated call site (dream / CAD / follow-up) keeps its behavior."""
    seen: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        seen.update(kwargs)
        return AgentResult(final_text="ok", cost_usd=None, duration_s=0.0, turns_used=1)

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    sentinel_store = object()

    dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            prompt="x",
            tools_needed=True,
            disallowed_tools=("WebFetch", "WebSearch"),
            log_event=(sentinel_store, 42, "dream"),
            output_format="stream-json",
        )
    )
    assert seen["disallowed_tools"] == ("WebFetch", "WebSearch")
    assert seen["log_event"] == (sentinel_store, 42, "dream")
    assert seen["output_format"] == "stream-json"


def test_dispatch_agent_max_tokens_truncates_post_hoc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``claude_agent`` has no completion-length flag (``call_claude_agent``
    accepts no such kwarg — only ``--max-turns`` / ``--max-budget-usd``), so a
    caller-pinned ``max_tokens`` can't reach a real generation-time stop.
    :class:`~precis.utils.llm.router.ClaudeAgentProvider` instead truncates the
    final text post-hoc to (roughly) that word budget — the regression this
    guards is a migrated cloud pass (meditation/briefing/cards) silently losing
    its pre-router-migration litellm cap entirely (gripe: post-ship review of
    8eb59b86)."""

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        # No max_tokens kwarg reaches call_claude_agent — the CLI has none.
        assert "max_tokens" not in kwargs
        return AgentResult(
            final_text=" ".join(f"word{i}" for i in range(1, 101)),
            cost_usd=1.0,
            duration_s=1.0,
            turns_used=1,
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)

    out = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            prompt="hi",
            tools_needed=True,
            max_tokens=14,  # ~10 words at the 1.4 tokens/word ratio
        )
    )

    assert out.error is None
    words = out.text.split()
    assert len(words) <= 10
    assert words[0] == "word1"  # truncated from the front, not reordered


def test_dispatch_agent_no_max_tokens_leaves_text_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_tokens=None`` (the default — no call site sets it before this
    fix) must not truncate — byte-identical to today for every caller that
    hasn't opted in."""
    long_text = " ".join(f"word{i}" for i in range(1, 101))

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        return AgentResult(
            final_text=long_text, cost_usd=1.0, duration_s=1.0, turns_used=1
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="hi", tools_needed=True))

    assert out.text == long_text


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


def test_dispatch_local_threads_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-pinned ``max_tokens`` overrides the LlmConfig default so a
    migrated direct-``LlmClient`` pass (paper_glossary=2000) keeps its budget."""
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["max_tokens"] = getattr(config, "max_tokens", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MAX_TOKENS", raising=False)

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p", max_tokens=2000))
    assert seen["max_tokens"] == 2000

    # Unset ⇒ the env/config default (220) — byte-identical to today.
    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))
    assert seen["max_tokens"] == 220


def test_dispatch_local_routes_to_served_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserved local slot that declares a direct ``endpoint`` (llama-swap)
    overrides the local dispatch's URL + model — the Phase-2 litellm-retire flip.
    """
    import precis.workers.llm_summarize as summ
    from precis.utils.llm import local_serving as ls

    # A reserved slot carrying a direct endpoint + server-side model name.
    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h",
            resource=f"llm:{model}",
            reserved=True,
            paused=False,
            endpoint="http://127.0.0.1:11445/v1",
            served_model="qwen3-next-80b-a3b-q4_k_m",
        ),
    )
    monkeypatch.setattr(ls, "release", lambda slot: None)

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["url"] = getattr(config, "url", None)
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="local gloss", total_tokens=7)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="hi"))

    # URL + model came from the slot's endpoint, not the litellm proxy default.
    assert seen["url"] == "http://127.0.0.1:11445/v1"
    assert seen["model"] == "qwen3-next-80b-a3b-q4_k_m"
    assert out.model == "qwen3-next-80b-a3b-q4_k_m"


def test_dispatch_local_slot_without_endpoint_keeps_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserved slot with NO endpoint leaves URL + model at today's defaults."""
    import precis.workers.llm_summarize as summ
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=True, paused=False
        ),
    )
    monkeypatch.setattr(ls, "release", lambda slot: None)

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["url"] = getattr(config, "url", None)
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x")

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MODEL", raising=False)
    monkeypatch.delenv("PRECIS_SUMMARIZE_LLM_URL", raising=False)

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="hi"))

    assert seen["url"] == "http://127.0.0.1:4000/v1"  # litellm proxy default
    assert seen["model"] == "summarizer"


def test_dispatch_local_big_routes_to_tools_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_BIG (local model + tools) now runs the OSS tools loop, not a
    NotImplementedError — routed regardless of the cloud backend flag."""
    seen: dict[str, object] = {}

    def fake_tools(req: LlmRequest, model: str) -> LlmResult:
        seen["model"] = model
        seen["tier"] = req.tier
        return LlmResult(
            text="looped", cost_usd=None, turns_used=2, model=model, tier=req.tier
        )

    monkeypatch.setattr(router, "_dispatch_openai_tools", fake_tools)
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert out.text == "looped"
    assert out.turns_used == 2
    assert seen["model"] == "qwen-heavy"  # resolved from the tier table


def test_run_oss_tool_loop_honors_local_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local-serving slot's endpoint routes the OSS tools loop to that
    llama-swap URL with an authless dummy key — the LOCAL_BIG per-host flip,
    winning over PRECIS_LLM_BASE_URL + the vault key."""
    from precis.utils.llm.router import run_oss_tool_loop

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(
            self, *, url: str, api_key: str, model: str, timeout: float
        ) -> None:
            seen["url"] = url
            seen["api_key"] = api_key
            seen["model"] = model

    monkeypatch.setattr("precis.utils.llm.openai_tools.ToolChatClient", FakeClient)
    monkeypatch.setattr(
        "precis.utils.llm.openai_tools.run_tool_loop", lambda *a, **k: object()
    )
    monkeypatch.setattr("precis.utils.llm.precis_tools.precis_tool_specs", lambda: [])
    monkeypatch.setattr("precis.utils.llm.precis_tools.runtime_executor", lambda: None)
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "http://hosted-oss:9999/v1")

    run_oss_tool_loop(
        prompt="think hard",
        model="qwen3-235b-thinking-2507-ud-q3_k_xl",
        local_url="http://127.0.0.1:11444/v1",
    )

    assert seen["url"] == "http://127.0.0.1:11444/v1"  # local wins over the hosted base
    assert seen["api_key"] == "dummy"  # authless loopback, not the vault key
    assert seen["model"] == "qwen3-235b-thinking-2507-ud-q3_k_xl"


def test_openai_tools_threads_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OPENAI_TOOLS transport threads the loop's definitive tool-call count
    into LlmResult.tool_calls — so the review seam's empty-result assertion works
    on the local/OSS backend, not just the claude_agent path. A `0` here must be
    a real 0 (not None), or a silent-empty pass on a backend-switched reviewer
    would go undetected."""
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.llm.router import LlmRequest, Tier, _dispatch_openai_tools

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="",
            turns_used=1,
            tool_calls_made=0,
            total_tokens=None,
            stop_reason="stop",
        ),
    )
    empty = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True), "m"
    )
    assert empty.tool_calls == 0  # definitive zero, NOT None

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="did stuff",
            turns_used=3,
            tool_calls_made=4,
            total_tokens=None,
            stop_reason="stop",
        ),
    )
    acted = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True), "m"
    )
    assert acted.tool_calls == 4


def test_dispatch_client_routes_through_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DispatchClient.complete folds a local completion through dispatch,
    threading model + max_tokens, and returns a result carrying text +
    total_tokens (the summarize/classify/glossary passes' contract)."""
    import precis.workers.llm_summarize as summ
    from precis.utils.llm.router import DispatchClient

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["model"] = getattr(config, "model", None)
            seen["max_tokens"] = getattr(config, "max_tokens", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            seen["messages"] = messages
            return _FakeOpenAI(text="gloss out", total_tokens=99)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MODEL", raising=False)
    monkeypatch.delenv("PRECIS_SUMMARIZE_MAX_TOKENS", raising=False)

    client = DispatchClient(
        tier=Tier.LOCAL_SMALL, model="summarizer", max_tokens=2000, source="glossary"
    )
    msgs = [{"role": "user", "content": "define terms"}]
    out = client.complete(msgs)

    assert out.text == "gloss out"
    assert out.total_tokens == 99  # accounting preserved through dispatch
    assert seen["model"] == "summarizer"
    assert seen["max_tokens"] == 2000
    assert seen["messages"] == msgs


def test_dispatch_client_cloud_tier_splits_messages_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DispatchClient on a cloud tier (``tools_needed=True``) folds a
    ``.complete(messages)`` call through ``claude_agent`` — the router-migrated
    shape the four former direct-``LlmClient`` cast passes (``reading/cards``,
    ``workers/briefing``, ``reading/meditation``, ``reading/briefing_cast``) now
    share. ``messages`` (an OpenAI-shaped ``[system, user]`` pair) is split into
    ``system_prompt`` + ``prompt`` — the shape ``claude_agent`` actually reads —
    and ``model``/``tier``/``source`` thread through unchanged."""
    from precis.utils.llm.router import DispatchClient

    seen: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        seen["prompt"] = prompt
        seen["system_prompt"] = kwargs.get("system_prompt")
        seen["model"] = kwargs.get("model")
        seen["mcp_config"] = kwargs.get("mcp_config")
        return AgentResult(
            final_text="a lovely nidra", cost_usd=0.5, duration_s=2.0, turns_used=1
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)

    client = DispatchClient(
        tier=Tier.CLOUD_SUPER,
        model="claude-opus-4-8",
        tools_needed=True,
        source="meditation",
        log_call=True,
    )
    msgs = [
        {"role": "system", "content": "You are a calm narrator."},
        {"role": "user", "content": "Walk these ideas: gravity, entropy."},
    ]
    out = client.complete(msgs)

    assert out.text == "a lovely nidra"
    assert seen["prompt"] == "Walk these ideas: gravity, entropy."
    assert seen["system_prompt"] == "You are a calm narrator."
    assert seen["model"] == "claude-opus-4-8"
    assert seen["mcp_config"] is None  # no tools advertised — text-only agent wrapper


def test_dispatch_client_cloud_tier_raises_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloud-tier dispatch failure raises :class:`DispatchError` (a
    ``RuntimeError`` subclass) — same "the pass marks the item failed" contract
    as the local tier, but distinguishable so a caller's retry policy can treat
    a router-level failure differently from an unrelated ``RuntimeError``."""
    from precis.utils.claude_agent import ClaudeAgentError
    from precis.utils.llm.router import DispatchClient, DispatchError

    def boom(prompt: str, **kwargs: object) -> AgentResult:
        raise ClaudeAgentError("claude -p (agent) exited 1: kaboom", returncode=1)

    monkeypatch.setattr(router, "call_claude_agent", boom)

    client = DispatchClient(tier=Tier.CLOUD_SUPER, tools_needed=True, source="cast")
    with pytest.raises(DispatchError, match="kaboom"):
        client.complete([{"role": "user", "content": "x"}])


def test_dispatch_client_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch error (transport failure / breaker pause) surfaces as a raise,
    so the pass marks the item failed + retries — the raw-client contract."""
    import precis.workers.llm_summarize as summ
    from precis.utils.llm.router import DispatchClient

    class BoomClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            raise RuntimeError("proxy down")

    monkeypatch.setattr(summ, "LlmClient", BoomClient)

    client = DispatchClient(tier=Tier.LOCAL_SMALL, model="summarizer")
    with pytest.raises(RuntimeError, match="proxy down"):
        client.complete([{"role": "user", "content": "x"}])


def test_dispatch_log_call_false_skips_route_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_call=False (the mechanical batch passes) skips the route-log so a
    corpus-scale backfill doesn't add a row per chunk."""
    import precis.workers.llm_summarize as summ
    from precis import route_log

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(route_log, "enabled", lambda: True)
    recorded: list[object] = []
    monkeypatch.setattr(route_log, "record_call", lambda rec: recorded.append(rec))

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p", log_call=False))
    assert recorded == []  # opted out

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))  # default logs
    assert len(recorded) == 1


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


def test_dispatch_breaker_trip_is_flagged_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A breaker trip folds into the normalized result with paused=True so a
    # pinned pass can skip (window-scoped pause) rather than record a failure.
    def _boom(*a: object, **kw: object) -> AgentResult:
        raise AssertionError("provider must not run when the breaker trips")

    monkeypatch.setattr(router, "call_claude_agent", _boom)
    monkeypatch.setattr(
        "precis.budget.breaker.gate_tier",
        lambda *a, **kw: "budget: daily cap $20.00 reached ($85.06 spent) — paused",
    )

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))

    assert out.paused is True
    assert out.error is not None and "daily cap" in out.error
    assert out.text == ""


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


# ── OpenAI-compatible backend (LLM independence, ships dark) ───────────


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (None, Backend.ANTHROPIC),  # unset default
        ("anthropic", Backend.ANTHROPIC),
        ("openai", Backend.OPENAI),
        ("OpenAI", Backend.OPENAI),  # case-insensitive
        ("bogus", Backend.ANTHROPIC),  # unknown degrades, never darks
    ],
)
def test_resolve_backend(
    env: str | None, expected: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is None:
        monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("PRECIS_LLM_BACKEND", env)
    assert resolve_backend() is expected


@pytest.mark.parametrize(
    ("tier", "tools_needed", "expected"),
    [
        # Tool-less cloud diverts to the OpenAI-compatible transport…
        (Tier.CLOUD_SMALL, False, Transport.OPENAI_COMPAT),
        (Tier.CLOUD_SUPER, False, Transport.OPENAI_COMPAT),
        # …and tool-using cloud diverts to the OSS tools loop.
        (Tier.CLOUD_SUPER, True, Transport.OPENAI_TOOLS),
        (Tier.CLOUD_MID, True, Transport.OPENAI_TOOLS),
        # local-small stays on the loopback proxy; local-big is the tools loop.
        (Tier.LOCAL_SMALL, False, Transport.LITELLM),
        (Tier.LOCAL_BIG, True, Transport.OPENAI_TOOLS),
    ],
)
def test_select_transport_openai_backend(
    tier: Tier, tools_needed: bool, expected: Transport
) -> None:
    got = select_transport(tier, tools_needed=tools_needed, backend=Backend.OPENAI)
    assert got is expected


def test_provider_registry_is_total() -> None:
    # Every Transport (incl. OPENAI_COMPAT) must have a provider row.
    assert set(router._PROVIDERS) == set(Transport)


def test_dispatch_openai_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRECIS_LLM_BACKEND=openai + a base url routes a tool-less cloud call to
    the hosted OSS backend, keyed from the vault, at the resolved model."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["url"] = getattr(config, "url", None)
            seen["api_key"] = getattr(config, "api_key", None)
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            seen["messages"] = messages
            return _FakeOpenAI(text="oss out", total_tokens=7)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk-vault-key")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_MODEL_HAIKU", "qwen-small")  # OSS id via the tier table

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt="judge this"))

    assert out.text == "oss out"
    assert out.cost_usd is None
    assert out.error is None
    assert seen["url"] == "https://openrouter.ai/api/v1"
    assert seen["api_key"] == "sk-vault-key"
    assert seen["model"] == "qwen-small"
    assert seen["messages"] == [{"role": "user", "content": "judge this"}]


def test_dispatch_openai_backend_without_base_url_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend on but no base url → cloud calls fall back to claude rather than
    POST to a phantom endpoint (ships-dark safety)."""
    calls: dict[str, object] = {}

    def fake_p(prompt: str, **kwargs: object) -> ClaudePResult:
        calls["model"] = kwargs.get("model")
        return ClaudePResult(data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.0)

    monkeypatch.setattr(router, "call_claude_p", fake_p)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_HAIKU", raising=False)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt="x"))

    assert out.text == '{"ok": true}'  # claude_p ran, not the OSS path
    assert calls["model"] == "claude-haiku-4-5-20251001"


def test_dispatch_openai_backend_tools_routes_to_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the OpenAI backend, a tool-using cloud call routes to the OSS tools
    loop (not claude_agent) — the LLM-independence path for agentic work."""
    called: dict[str, object] = {}

    def fake_tools(req: LlmRequest, model: str) -> LlmResult:
        called["ran"] = True
        called["model"] = model
        return LlmResult(
            text="ok", cost_usd=None, turns_used=1, model=model, tier=req.tier
        )

    monkeypatch.setattr(router, "_dispatch_openai_tools", fake_tools)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "deepseek-v3")

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))
    assert called.get("ran") is True
    assert called["model"] == "deepseek-v3"
    assert out.text == "ok"


def test_dispatch_anthropic_backend_tools_still_uses_claude_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (anthropic) backend: tool-using cloud calls stay on
    claude_agent — the OSS path engages only when opted in."""
    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        calls["ran"] = True
        return AgentResult(final_text="a", cost_usd=None, duration_s=0.0, turns_used=1)

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)

    dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))
    assert calls.get("ran") is True


# ── FailoverProvider ladder (LLM-independence safety net) ──────────────

from precis.utils.llm.router import FailoverProvider, Rung


class _FakeProv:
    """A provider returning a scripted LlmResult; records calls + model seen."""

    def __init__(self, result: LlmResult) -> None:
        self._result = result
        self.calls = 0
        self.model_seen: str | None = None

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        self.calls += 1
        self.model_seen = model
        return self._result


def _ok(text: str, model: str = "m") -> LlmResult:
    return LlmResult(
        text=text, cost_usd=None, turns_used=None, model=model, tier=Tier.CLOUD_SUPER
    )


def _err(msg: str, model: str = "m") -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=Tier.CLOUD_SUPER,
        error=msg,
    )


def test_failover_first_rung_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _FakeProv(_ok("primary out"))
    fallback = _FakeProv(_ok("fallback out"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)]
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="oss-model")

    assert out.text == "primary out"
    assert primary.calls == 1
    assert fallback.calls == 0  # short-circuits on success


def test_failover_falls_through_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _FakeProv(_err("backend down"))
    fallback = _FakeProv(_ok("fallback out"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT, model="claude-x")]
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="oss-model")

    assert out.text == "fallback out"
    assert primary.calls == 1 and fallback.calls == 1
    assert primary.model_seen == "oss-model"  # rung model=None → given model
    assert fallback.model_seen == "claude-x"  # rung pins its own model


def test_failover_all_error_returns_last(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _FakeProv(_err("down"))
    fallback = _FakeProv(_err("also down"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)]
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")

    assert out.error == "also down"  # the last attempt, with its error


def test_failover_accept_gate_rejects_low_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _FakeProv(_ok("bad"))
    fallback = _FakeProv(_ok("good"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    # accept only results whose text == "good" → primary's error-free "bad" is
    # rejected, falls through to the claude fallback.
    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)],
        accept=lambda r: r.text == "good",
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")

    assert out.text == "good"
    assert primary.calls == 1 and fallback.calls == 1


def test_failover_empty_rungs_rejected() -> None:
    with pytest.raises(ValueError, match="at least one rung"):
        FailoverProvider([])


# ── the default ladder + claude-default resolution ─────────────────────


def test_failover_ladder_oss_tools_has_claude_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    ladder = router._failover_ladder(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )
    assert [r.transport for r in ladder] == [
        Transport.OPENAI_TOOLS,
        Transport.CLAUDE_AGENT,
    ]
    # the claude fallback pins the compiled-in claude id…
    assert ladder[1].model == "claude-opus-4-8"


def test_failover_ladder_oss_judge_has_claude_p_fallback() -> None:
    ladder = router._failover_ladder(
        Tier.CLOUD_SMALL, tools_needed=False, backend=Backend.OPENAI
    )
    assert [r.transport for r in ladder] == [
        Transport.OPENAI_COMPAT,
        Transport.CLAUDE_P,
    ]


def test_failover_ladder_anthropic_has_no_fallback() -> None:
    ladder = router._failover_ladder(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.ANTHROPIC
    )
    # a claude primary has nothing to fall back to.
    assert [r.transport for r in ladder] == [Transport.CLAUDE_AGENT]


def test_claude_default_ignores_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with PRECIS_MODEL_OPUS pointed at an OSS id, the claude fallback
    # resolves the compiled-in claude id — so OSS ids never leak onto claude -p.
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "deepseek-ai/DeepSeek-V3")
    assert router._claude_default(Tier.CLOUD_SUPER) == "claude-opus-4-8"


def test_dispatch_failover_flag_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: backend=openai + failover on, OSS tool loop errors → the
    claude agent runs instead (with the claude model, not the OSS one)."""
    oss = _FakeProv(_err("oss unreachable"))
    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="claude saved it", cost_usd=None, duration_s=0.0, turns_used=1
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, oss)
    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))

    assert out.text == "claude saved it"
    assert out.error is None
    assert calls["model"] == "claude-opus-4-8"  # claude fallback, not the OSS id


# ── FailoverProvider warns when a fallback rung runs (cost visibility) ──


def test_failover_warns_on_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    primary = _FakeProv(_err("oss down"))
    fallback = _FakeProv(_ok("claude saved it"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [
            Rung(Transport.OPENAI_TOOLS, label="oss"),
            Rung(Transport.CLAUDE_AGENT, label="claude-fallback"),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="precis.utils.llm.router"):
        out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")

    assert out.text == "claude saved it"
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "llm-failover" in msgs
    assert "oss" in msgs and "failed: oss down" in msgs  # the failed primary
    assert "fell back to rung 1" in msgs  # the fallback firing


def test_failover_no_warning_when_primary_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    primary = _FakeProv(_ok("fine"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(
        router._PROVIDERS, Transport.CLAUDE_AGENT, _FakeProv(_ok("unused"))
    )
    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)]
    )
    with caplog.at_level(logging.WARNING, logger="precis.utils.llm.router"):
        prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")
    assert not [r for r in caplog.records if "llm-failover" in r.getMessage()]


# ── openrouter_routing: variant pin → OpenRouter provider{} block (162624) ──


def test_openrouter_routing_pins_provider_and_quant() -> None:
    from precis.utils.llm.router import openrouter_routing

    body = openrouter_routing(
        {"provider": "DeepInfra", "quant": "fp4", "tag": "deepinfra/fp4"},
        effort="medium",
    )
    assert body["provider"]["order"] == ["deepinfra"]  # slug from the tag
    assert body["provider"]["quantizations"] == ["fp4"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["require_parameters"] is True
    assert body["reasoning"] == {"effort": "medium"}


def test_openrouter_routing_falls_back_to_provider_name() -> None:
    from precis.utils.llm.router import openrouter_routing

    body = openrouter_routing({"provider": "Baidu", "quant": "fp8"})
    assert body["provider"]["order"] == ["baidu"]
    assert body["provider"]["quantizations"] == ["fp8"]
    assert "reasoning" not in body  # no effort → no reasoning block


def test_openrouter_routing_omits_unknown_quant_and_empty() -> None:
    from precis.utils.llm.router import openrouter_routing

    body = openrouter_routing({"provider": "X", "quant": "unknown"})
    assert "quantizations" not in body["provider"]
    assert openrouter_routing(None) == {}  # nothing to pin → bare slug


def test_dispatch_openai_compat_threads_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The booked endpoint on the request lands as extra_body on the wire.
    import precis.workers.llm_summarize as summ
    from precis.utils.llm.router import _dispatch_openai_compat

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, cfg: object) -> None:
            pass

        def complete(self, messages, *, extra_body=None):  # type: ignore[no-untyped-def]
            captured["extra_body"] = extra_body
            return summ.LlmResult(text="ok", total_tokens=3)

    monkeypatch.setattr(summ, "LlmClient", _FakeClient)
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "http://backend.example/v1")
    req = LlmRequest(
        tier=Tier.CLOUD_SUPER,
        prompt="hi",
        endpoint={"provider": "DeepInfra", "quant": "fp4", "tag": "deepinfra/fp4"},
        effort="high",
    )
    res = _dispatch_openai_compat(req, "z-ai/glm-5.2")
    assert res.error is None and res.text == "ok"
    eb = captured["extra_body"]
    assert eb["provider"]["order"] == ["deepinfra"]  # type: ignore[index]
    assert eb["reasoning"] == {"effort": "high"}  # type: ignore[index]
