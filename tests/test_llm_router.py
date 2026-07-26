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

from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from urllib.error import HTTPError, URLError

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
        input_tokens=3555,
        output_tokens=4,
        cache_read_tokens=0,
        cache_creation_tokens=46653,
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
        # Token telemetry rides through unchanged — see AgentResult's matching
        # fields for the "trailing result event, already cumulative" note.
        input_tokens=3555,
        output_tokens=4,
        cache_read_tokens=0,
        cache_creation_tokens=46653,
    )
    assert got.error is None


def test_result_from_agent_usage_defaults_none() -> None:
    """An ``AgentResult`` built without the new token fields (e.g. the
    text/stub path) propagates ``None`` for all four onto ``LlmResult`` —
    never a false zero."""
    raw = AgentResult(final_text="ok", cost_usd=None, duration_s=0.0, turns_used=None)
    got = result_from_agent(raw, model="claude-opus-4-7", tier=Tier.CLOUD_SUPER)
    assert got.input_tokens is None
    assert got.output_tokens is None
    assert got.cache_read_tokens is None
    assert got.cache_creation_tokens is None


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


# ── ADR 0066 gen-param passthrough: per-tier thinking/temperature defaults ──


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (Tier.SMALL, (False, 0.0)),
        (Tier.LOCAL_SMALL, (False, 0.0)),
        (Tier.MEDIUM, (True, None)),
        (Tier.CLOUD_SMALL, (True, None)),
        (Tier.BIG, (True, None)),
        (Tier.CLOUD_MID, (True, None)),
        (Tier.LOCAL_BIG, (True, None)),
        (Tier.FRONTIER, (True, None)),
        (Tier.CLOUD_SUPER, (True, None)),
    ],
)
def test_tier_gen_defaults(tier: Tier, expected: tuple[bool, float | None]) -> None:
    """SMALL/LOCAL_SMALL (a categorizer) want thinking off + temperature 0 so
    they never burn reasoning tokens on a one-line judgment; every other
    tier (+ its ADR 0066 analogue) wants thinking on + the provider's own
    temperature default."""
    assert router._tier_gen_defaults(tier) == expected


def test_tier_gen_defaults_table_is_total() -> None:
    assert set(router._TIER_GEN_DEFAULTS) == set(Tier)


def test_dispatch_local_threads_tier_gen_default_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_SMALL (the SMALL analogue) resolves to temperature 0.0 — the
    LlmConfig default this replaces was hardcoded, now it's tier-driven."""
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["temperature"] = getattr(config, "temperature", "MISSING")

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))
    assert seen["temperature"] == 0.0


def test_dispatch_local_explicit_temperature_overrides_tier_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit LlmRequest.temperature wins over the tier default."""
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["temperature"] = getattr(config, "temperature", "MISSING")

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p", temperature=0.85))
    assert seen["temperature"] == 0.85

    # Unset ⇒ back to the tier default.
    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))
    assert seen["temperature"] == 0.0


def test_dispatch_claude_transport_ignores_gen_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking/temperature are no-ops on the claude transports — never
    raise, never reach the ``claude -p`` call."""
    calls: dict[str, object] = {}

    def fake_p(prompt: str, **kwargs: object) -> ClaudePResult:
        calls["kwargs"] = kwargs
        return ClaudePResult(data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.0)

    monkeypatch.setattr(router, "call_claude_p", fake_p)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)

    out = dispatch(
        LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", thinking=False, temperature=0.9)
    )
    assert out.error is None
    assert "thinking" not in calls["kwargs"]
    assert "temperature" not in calls["kwargs"]


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
            self,
            *,
            url: str,
            api_key: str,
            model: str,
            timeout: float,
            temperature: float | None = None,
            extra_body: dict[str, object] | None = None,
        ) -> None:
            seen["url"] = url
            seen["api_key"] = api_key
            seen["model"] = model
            seen["temperature"] = temperature
            seen["extra_body"] = extra_body

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
    assert seen["temperature"] is None  # not passed in ⇒ provider default
    # A local endpoint never gets the (unconfirmed) no-thinking directive —
    # see the NOTE in _dispatch_local / run_oss_tool_loop.
    assert seen["extra_body"] is None


def test_run_oss_tool_loop_hosted_thinking_off_disables_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *hosted* (no ``local_url``) OSS tools-loop call with thinking off
    gets the confirmed OpenRouter ``reasoning.enabled: false`` directive plus
    the resolved temperature — the LOCAL_BIG/BIG-analogue path's hosted leg."""
    import precis.secrets as secrets
    from precis.utils.llm.router import run_oss_tool_loop

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(
            self,
            *,
            url: str,
            api_key: str,
            model: str,
            timeout: float,
            temperature: float | None = None,
            extra_body: dict[str, object] | None = None,
        ) -> None:
            seen["temperature"] = temperature
            seen["extra_body"] = extra_body

    monkeypatch.setattr("precis.utils.llm.openai_tools.ToolChatClient", FakeClient)
    monkeypatch.setattr(
        "precis.utils.llm.openai_tools.run_tool_loop", lambda *a, **k: object()
    )
    monkeypatch.setattr("precis.utils.llm.precis_tools.precis_tool_specs", lambda: [])
    monkeypatch.setattr("precis.utils.llm.precis_tools.runtime_executor", lambda: None)
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk-vault-key")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    run_oss_tool_loop(prompt="x", model="m", temperature=0.0, thinking=False)

    assert seen["temperature"] == 0.0
    assert seen["extra_body"]["reasoning"] == {"enabled": False}


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


def test_openai_tools_threads_loop_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's own unavailability classification (set where the exception
    is actually caught, inside `run_tool_loop`) rides through to
    `LlmResult.paused` — ADR 0066 §5a."""
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.llm.router import LlmRequest, Tier, _dispatch_openai_tools

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="",
            turns_used=0,
            tool_calls_made=0,
            total_tokens=None,
            stop_reason="error",
            error="timed out after 120.0s",
            paused=True,
        ),
    )
    out = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True), "m"
    )
    assert out.paused is True
    assert out.error == "timed out after 120.0s"


# ── Part 2: openai_tools cost capture (un-blind the budget breaker) ────
# docs/proposals/glm-fleet-flip-safety.md Part 2 — the 14 "successful"
# openai_tools rows that all logged cost_usd=None.


def test_openai_tools_reads_loop_cost_usd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's own summed ``usage.cost`` (OpenRouter) reaches
    LlmResult.cost_usd — no longer hardcoded None (acceptance criterion 2)."""
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.llm.router import LlmRequest, Tier, _dispatch_openai_tools

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="ok",
            turns_used=2,
            tool_calls_made=1,
            total_tokens=500,
            stop_reason="stop",
            cost_usd=0.0137,
        ),
    )
    out = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True), "m"
    )
    assert out.cost_usd == pytest.approx(0.0137)


def test_openai_tools_falls_back_to_token_pricing_when_cost_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``usage.cost`` on any turn (a backend that reports tokens but not
    dollars) still yields a priced estimate via the same catalog
    ``cost_from_tokens`` fallback ``result_from_openai`` uses — not a silent
    ``None`` that blinds the breaker."""
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.llm.router import LlmRequest, Tier, _dispatch_openai_tools

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="ok",
            turns_used=1,
            tool_calls_made=0,
            total_tokens=1_000_000,
            stop_reason="stop",
            cost_usd=None,
        ),
    )
    out = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True),
        "deepseek-ai/DeepSeek-V3",  # a priced id in budget.pricing.PRICE_TABLE
    )
    assert out.cost_usd is not None
    assert out.cost_usd > 0


def test_openai_tools_unknown_model_and_no_cost_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cost reported AND the model isn't in the pricing table → cost_usd
    stays None (priced as free/unknown), not a fabricated number."""
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.llm.router import LlmRequest, Tier, _dispatch_openai_tools

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="ok",
            turns_used=1,
            tool_calls_made=0,
            total_tokens=1000,
            stop_reason="stop",
            cost_usd=None,
        ),
    )
    out = _dispatch_openai_tools(
        LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True), "qwen-heavy"
    )
    assert out.cost_usd is None


def test_dispatch_openai_tools_cost_reaches_route_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full dispatch() through the OPENAI_TOOLS transport writes the loop's
    cost into the route-log's LlmCallRecord.cost_usd — un-blinding the budget
    breaker end to end (acceptance criterion 2)."""
    from precis import route_log
    from precis.utils.llm.openai_tools import AgentLoopResult

    monkeypatch.setattr(
        "precis.utils.llm.router.run_oss_tool_loop",
        lambda **k: AgentLoopResult(
            final_text="ok",
            turns_used=1,
            tool_calls_made=1,
            total_tokens=200,
            stop_reason="stop",
            cost_usd=0.0055,
        ),
    )
    monkeypatch.setattr(route_log, "enabled", lambda: True)
    recorded: list[route_log.LlmCallRecord] = []
    monkeypatch.setattr(route_log, "record_call", lambda rec: recorded.append(rec))
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)

    dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.0055)


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


def test_dispatch_client_threads_thinking_and_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DispatchClient's thinking/temperature fields reach the LlmRequest
    dispatch resolves, same as max_tokens (ADR 0066 gen-param passthrough)."""
    import precis.workers.llm_summarize as summ
    from precis.utils.llm.router import DispatchClient

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["temperature"] = getattr(config, "temperature", "MISSING")

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)

    # Explicit pin wins over the (SMALL/LOCAL_SMALL) tier default of 0.0.
    client = DispatchClient(tier=Tier.LOCAL_SMALL, temperature=0.6)
    client.complete([{"role": "user", "content": "x"}])
    assert seen["temperature"] == 0.6

    # Bare client (defaults) ⇒ the tier default, unchanged from today.
    bare = DispatchClient(tier=Tier.LOCAL_SMALL)
    bare.complete([{"role": "user", "content": "x"}])
    assert seen["temperature"] == 0.0


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


# ── unavailability vs. semantic-error classification (ADR 0066 §5a) ────
#
# "A todo that can't run right now waits and retries; it does not park."
# `_is_unavailability` sorts a caught transport exception into `paused=True`
# (skip-and-retry) or a plain `error` (fails outright, no retry) — see
# docs/decisions/0066-capability-tiers-and-placement-chains.md §5a.


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timed out"), True),  # == socket.timeout, py>=3.10 alias
        (URLError("connection refused"), True),
        (ConnectionError("connection reset"), True),
        (OSError("some other os error"), True),
        (HTTPError("http://x", 500, "Internal Server Error", Message(), None), True),
        (HTTPError("http://x", 503, "Service Unavailable", Message(), None), True),
        (HTTPError("http://x", 429, "Too Many Requests", Message(), None), True),
        (HTTPError("http://x", 400, "Bad Request", Message(), None), False),
        (HTTPError("http://x", 401, "Unauthorized", Message(), None), False),
        (HTTPError("http://x", 403, "Forbidden", Message(), None), False),
        (HTTPError("http://x", 422, "Unprocessable Entity", Message(), None), False),
        (RuntimeError("summarizer returned no completion"), False),
        (ValueError("not a transport error at all"), False),
    ],
)
def test_is_unavailability_table(exc: BaseException, expected: bool) -> None:
    assert router._is_unavailability(exc) is expected


def test_error_result_claude_timeout_is_paused() -> None:
    """A claude wall-clock timeout → paused (retry), so a claude-only rung
    (e.g. FRONTIER, no local fallback) waits rather than parking the todo
    (ADR 0066 §5a). A non-timeout ClaudeProcessError stays a semantic error."""
    from precis.utils._claude_subprocess import ClaudeProcessError
    from precis.utils.llm.router import _error_result

    timed = ClaudeProcessError("claude -p timed out after 600s", timed_out=True)
    res = _error_result(timed, model="claude-opus-4-8", tier=Tier.FRONTIER)
    assert res.paused is True
    assert res.error and "timed out" in res.error  # still carries the error

    exited = ClaudeProcessError("claude -p exited 1", returncode=1)
    res2 = _error_result(exited, model="claude-opus-4-8", tier=Tier.FRONTIER)
    assert res2.paused is False


def test_dispatch_local_timeout_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request timeout on the local litellm transport is unavailability —
    skip-and-retry, not a hard failure that can park the todo."""
    import precis.workers.llm_summarize as summ

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(
            self, messages: list[dict[str, str]], *, extra_body: object = None
        ) -> _FakeOpenAI:
            raise TimeoutError("timed out after 120.0s")

    monkeypatch.setattr(summ, "LlmClient", FakeClient)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))

    assert out.paused is True
    assert out.error is not None and "timed out" in out.error


def test_dispatch_local_4xx_is_error_not_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx (non-429) is a genuine semantic failure — it will fail identically
    on retry, so it must NOT be flagged paused."""
    import precis.workers.llm_summarize as summ

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            raise HTTPError("http://x", 400, "Bad Request", Message(), None)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="p"))

    assert out.paused is False
    assert out.error is not None and "400" in out.error


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
        # local-small now mirrors the cloud split (OPENAI_COMPAT under the
        # openai backend — docs/proposals/llm-openrouter-bypass.md item 2);
        # local-big is unconditionally the tools loop either way.
        (Tier.LOCAL_SMALL, False, Transport.OPENAI_COMPAT),
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


def test_dispatch_openai_compat_thinking_off_disables_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMALL's thinking-off tier default reaches the wire as OpenRouter's
    documented ``reasoning.enabled: false`` switch, plus temperature 0.0 —
    the confirmed half of the ADR 0066 no-thinking directive."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["temperature"] = getattr(config, "temperature", "MISSING")

        def complete(
            self, messages: list[dict[str, str]], *, extra_body: dict | None = None
        ) -> _FakeOpenAI:
            seen["extra_body"] = extra_body
            return _FakeOpenAI(text="ok", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk-vault-key")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    out = dispatch(LlmRequest(tier=Tier.SMALL, prompt="classify this"))

    assert out.error is None
    assert seen["temperature"] == 0.0
    assert seen["extra_body"]["reasoning"] == {"enabled": False}


def test_dispatch_openai_compat_thinking_on_omits_reasoning_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM (thinking-on by tier default) sends no ``reasoning`` block and
    no ``temperature`` (the provider's own default — the field is omitted,
    not pinned to 0)."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["temperature"] = getattr(config, "temperature", "MISSING")

        def complete(
            self, messages: list[dict[str, str]], *, extra_body: dict | None = None
        ) -> _FakeOpenAI:
            seen["extra_body"] = extra_body
            return _FakeOpenAI(text="ok", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk-vault-key")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_MODEL_HAIKU", "qwen-small")

    out = dispatch(LlmRequest(tier=Tier.MEDIUM, prompt="judge this"))

    assert out.error is None
    assert seen["temperature"] is None  # omitted — provider default
    assert seen.get("extra_body") is None  # {} → no extra_body kwarg passed at all


def test_dispatch_openai_compat_explicit_thinking_false_overrides_big_tier_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit LlmRequest.thinking=False wins over BIG's thinking-on
    tier default, reaching the wire as the same reasoning-disabled block."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(
            self, messages: list[dict[str, str]], *, extra_body: dict | None = None
        ) -> _FakeOpenAI:
            seen["extra_body"] = extra_body
            return _FakeOpenAI(text="ok", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk-vault-key")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_MODEL_SONNET", "qwen-mid")

    out = dispatch(LlmRequest(tier=Tier.BIG, prompt="x", thinking=False))

    assert out.error is None
    assert seen["extra_body"]["reasoning"] == {"enabled": False}


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


# ── Part 1: transparent hosted-small remap ──────────────────────────────
# docs/proposals/glm-fleet-flip-safety.md Part 1 — the 395-error class:
# classify/summarize pin model="summarizer" (a local-only litellm alias),
# which 400s when it reaches a hosted OSS endpoint under the flip.


def test_dispatch_local_small_remaps_to_hosted_under_openai_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_SMALL pinning model="summarizer" under backend=openai + a hosted
    base url routes OPENAI_COMPAT with the HOSTED small-model id, not the dead
    local alias (acceptance criterion 1)."""
    seen: dict[str, object] = {}

    def fake_compat(req: LlmRequest, *, model: str) -> LlmResult:
        seen["model"] = model
        return LlmResult(
            text="hosted", cost_usd=0.001, turns_used=None, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, _RunFn(fake_compat))
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("PRECIS_LOCAL_SMALL_HOSTED_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", model="summarizer"))

    assert seen["model"] == "z-ai/glm-4.7-flash"  # the compiled hosted default
    assert seen["model"] != "summarizer"
    assert out.model == "z-ai/glm-4.7-flash"


def test_dispatch_local_small_remap_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRECIS_LOCAL_SMALL_HOSTED_MODEL overrides the compiled hosted default."""
    seen: dict[str, object] = {}

    def fake_compat(req: LlmRequest, *, model: str) -> LlmResult:
        seen["model"] = model
        return LlmResult(
            text="x", cost_usd=None, turns_used=None, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, _RunFn(fake_compat))
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_LOCAL_SMALL_HOSTED_MODEL", "some/other-small")

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", model="rake-lemma"))

    assert seen["model"] == "some/other-small"


def test_dispatch_local_small_no_remap_under_default_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the default (anthropic) backend LOCAL_SMALL still resolves to
    Transport.LITELLM — no hosted-OSS transport, so the remap is a hard no-op
    and `summarizer` reaches the loopback proxy unchanged (acceptance
    criterion 1 + 5: byte-identical to today)."""
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="local gloss", total_tokens=3)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", model="summarizer"))

    assert seen["model"] == "summarizer"
    assert out.model == "summarizer"


def test_dispatch_local_small_no_remap_when_base_url_unset_even_under_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backend=openai but PRECIS_LLM_BASE_URL absent → the existing
    ships-dark demotion already forces ANTHROPIC/LITELLM before the remap
    helper ever sees a hosted transport — still `summarizer` unchanged."""
    import precis.workers.llm_summarize as summ

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["model"] = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> _FakeOpenAI:
            return _FakeOpenAI(text="x", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)

    dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", model="summarizer"))

    assert seen["model"] == "summarizer"


def test_dispatch_served_by_slot_is_never_remapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local `served_by` slot with a direct endpoint is already correctly
    named for its own local server — the hosted remap must not clobber it,
    even though PRECIS_LLM_BASE_URL is set and the model name is one of the
    local-only aliases (acceptance criterion 1)."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h",
            resource=f"llm:{model}",
            reserved=True,
            paused=False,
            endpoint="http://127.0.0.1:11445/v1",
            served_model="qwen-heavy-served",
        ),
    )
    monkeypatch.setattr(ls, "release", lambda slot: None)

    seen: dict[str, object] = {}

    def fake_tools(req: LlmRequest, *, model: str) -> LlmResult:
        seen["model"] = model
        seen["local_url"] = req.local_url
        return LlmResult(
            text="served", cost_usd=None, turns_used=1, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, _RunFn(fake_tools))
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)

    dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert seen["model"] == "qwen-heavy-served"  # the slot's own name, not remapped
    assert seen["local_url"] == "http://127.0.0.1:11445/v1"


def test_dispatch_saturated_slot_escape_also_remaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saturated-local-slot ladder escape (retrying rung 0 against the
    hosted endpoint) is a SEPARATE code path from the normal call_req/
    call_model block — it must also apply the remap (not leave the dead
    local alias on the hosted retry), and not apply it twice."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    seen: dict[str, object] = {}

    def fake_compat(req: LlmRequest, *, model: str) -> LlmResult:
        seen["model"] = model
        return LlmResult(
            text="x", cost_usd=None, turns_used=None, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, _RunFn(fake_compat))
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", model="summarizer"))

    assert seen["model"] == "z-ai/glm-4.7-flash"
    assert out.model == "z-ai/glm-4.7-flash"


def test_hosted_small_model_override_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live_config override wins over the env var, which wins over the
    compiled default — same precedence as resolve_model."""
    monkeypatch.setattr(
        "precis.utils.llm.live_config.model_override",
        lambda tier: "app-settings/pick" if tier is Tier.LOCAL_SMALL else None,
    )
    monkeypatch.setenv("PRECIS_LOCAL_SMALL_HOSTED_MODEL", "env/pick")
    assert router._hosted_small_model() == "app-settings/pick"


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


class _RunFn:
    """A provider wrapping a plain ``(req, *, model) -> LlmResult`` callable —
    for tests that need to inspect the request rather than script a fixed
    result (e.g. checking ``req.local_url`` on a retried rung)."""

    def __init__(self, fn: Callable[..., LlmResult]) -> None:
        self._fn = fn

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return self._fn(req, model=model)


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


def _paused(msg: str, model: str = "m") -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=Tier.CLOUD_SUPER,
        error=msg,
        paused=True,
    )


def test_failover_all_unavailable_returns_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every rung raises unavailability (a timeout) — the ladder exhausts and
    returns the last rung's result, which must still carry ``paused=True`` so
    the caller backs off and retries rather than recording a hard failure
    (ADR 0066 §5a)."""
    primary = _FakeProv(_paused("primary timed out after 120.0s"))
    fallback = _FakeProv(_paused("fallback timed out after 600.0s"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)]
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")

    assert out.paused is True
    assert out.error == "fallback timed out after 600.0s"


def test_failover_all_semantic_error_stays_unpaused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx semantic error on every rung stays a plain (unpaused) error — it
    will fail identically on retry, so the ladder must not flag it paused."""
    primary = _FakeProv(_err("400 bad request"))
    fallback = _FakeProv(_err("401 unauthorized"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, fallback)

    prov = FailoverProvider(
        [Rung(Transport.OPENAI_TOOLS), Rung(Transport.CLAUDE_AGENT)]
    )
    out = prov.run(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x"), model="m")

    assert out.paused is False
    assert out.error == "401 unauthorized"


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


def test_failover_ladder_local_big_escalates_to_sonnet_not_qwen_heavy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_BIG's own _TIER_MODEL default ("qwen-heavy") is an OSS alias, not
    a claude id — its claude-fallback rung must not pin that nonsense model."""
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    ladder = router._failover_ladder(
        Tier.LOCAL_BIG, tools_needed=True, backend=Backend.ANTHROPIC
    )
    assert [r.transport for r in ladder] == [
        Transport.OPENAI_TOOLS,
        Transport.CLAUDE_AGENT,
    ]
    assert ladder[1].model == "claude-sonnet-5"


def test_failover_ladder_local_small_has_no_claude_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per the roster ("small" skips Anthropic entirely), LOCAL_SMALL's
    ladder never grows a claude-fallback rung, even under backend=openai."""
    ladder = router._failover_ladder(
        Tier.LOCAL_SMALL, tools_needed=False, backend=Backend.OPENAI
    )
    assert [r.transport for r in ladder] == [Transport.OPENAI_COMPAT]


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


def test_dispatch_failover_all_rungs_timeout_is_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end (ADR 0066 §5a): LOCAL_SMALL's ladder has no claude fallback
    (see ``test_failover_ladder_local_small_has_no_claude_rung``), so its one
    OSS rung timing out exhausts the ladder — the result must still come back
    ``paused=True``, not a plain error that can park the caller's todo."""
    import precis.workers.llm_summarize as summ

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(
            self,
            messages: list[dict[str, str]],
            *,
            extra_body: dict[str, object] | None = None,
        ) -> _FakeOpenAI:
            raise TimeoutError("timed out after 120.0s")

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x"))

    assert out.paused is True
    assert out.error is not None and "timed out" in out.error


def test_dispatch_failover_all_rungs_4xx_stays_error_not_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same single-rung ladder, but a 4xx (non-429) semantic failure — it will
    fail identically on retry, so it must stay a plain error, not paused."""
    import precis.workers.llm_summarize as summ

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(
            self,
            messages: list[dict[str, str]],
            *,
            extra_body: dict[str, object] | None = None,
        ) -> _FakeOpenAI:
            raise HTTPError("http://x", 401, "Unauthorized", Message(), None)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x"))

    assert out.paused is False
    assert out.error is not None and "401" in out.error


# ── resolve_chain: the chain-override layer (ADR 0066 §4 / §Phase B) ─────
#
# ADR 0066 Phase B: the chain is the always-on resolution path. With no
# ``llm.chain.<tier>`` row set, resolve_chain == _default_chain — a single
# primary rung by default, or the built-in _failover_ladder when
# PRECIS_LLM_FAILOVER is on. An operator override is read regardless of the
# flag (the Phase B unlock — see test_dispatch_chain_override_honored_flag_off).

_LADDER_CASES = [
    (Tier.CLOUD_SUPER, True, Backend.OPENAI),
    (Tier.CLOUD_SMALL, False, Backend.OPENAI),
    (Tier.CLOUD_SUPER, True, Backend.ANTHROPIC),
    (Tier.LOCAL_BIG, True, Backend.ANTHROPIC),
    (Tier.LOCAL_SMALL, False, Backend.OPENAI),
]


@pytest.mark.parametrize(("tier", "tools_needed", "backend"), _LADDER_CASES)
def test_resolve_chain_no_override_flag_off_is_single_primary_rung(
    monkeypatch: pytest.MonkeyPatch,
    tier: Tier,
    tools_needed: bool,
    backend: Backend,
) -> None:
    """Phase B default: no override + failover flag off → a single primary rung
    (``select_transport``), byte-for-byte the pre-Phase-B non-failover path —
    NOT the OSS→claude ladder (which is opt-in)."""
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override", lambda _tier: None
    )
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)

    chain = router.resolve_chain(tier, tools_needed=tools_needed, backend=backend)

    primary = router.select_transport(tier, tools_needed=tools_needed, backend=backend)
    assert chain == [Rung(primary, label="primary")]


@pytest.mark.parametrize(("tier", "tools_needed", "backend"), _LADDER_CASES)
def test_resolve_chain_no_override_flag_on_matches_failover_ladder(
    monkeypatch: pytest.MonkeyPatch,
    tier: Tier,
    tools_needed: bool,
    backend: Backend,
) -> None:
    """With PRECIS_LLM_FAILOVER on, no override falls to the built-in
    auto-failover ladder — byte-for-byte the legacy failover-on behaviour."""
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override", lambda _tier: None
    )
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    default = router._failover_ladder(tier, tools_needed=tools_needed, backend=backend)
    chain = router.resolve_chain(tier, tools_needed=tools_needed, backend=backend)

    assert chain == default


def test_resolve_chain_valid_override_maps_rungs_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "z-ai/glm-5.2",
                    "transport": "openai_tools",
                },
                {
                    "placement": "cloud",
                    "model": "claude-opus-4-8",
                    "transport": "claude_agent",
                },
            ]
            if tier is Tier.CLOUD_SUPER
            else None
        ),
    )

    chain = router.resolve_chain(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )

    assert chain == [
        Rung(Transport.OPENAI_TOOLS, model="z-ai/glm-5.2", label="cloud"),
        Rung(Transport.CLAUDE_AGENT, model="claude-opus-4-8", label="cloud"),
    ]


def test_resolve_chain_override_placement_missing_labels_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: [{"model": "z-ai/glm-5.2", "transport": "openai_tools"}],
    )
    chain = router.resolve_chain(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )
    assert chain == [Rung(Transport.OPENAI_TOOLS, model="z-ai/glm-5.2", label="chain")]


@pytest.mark.parametrize(
    "bad_rungs",
    [
        # unknown transport
        [{"placement": "cloud", "model": "m", "transport": "carrier_pigeon"}],
        # missing model
        [{"placement": "cloud", "transport": "openai_tools"}],
        # non-string model
        [{"placement": "cloud", "model": 123, "transport": "openai_tools"}],
        # non-object rung
        ["not-a-dict"],
    ],
)
def test_resolve_chain_malformed_override_falls_back_to_ladder(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad_rungs: list[object],
) -> None:
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override", lambda _tier: bad_rungs
    )
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)

    with caplog.at_level("WARNING"):
        chain = router.resolve_chain(
            Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
        )

    default = router._default_chain(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )
    assert chain == default
    assert any("llm-chain" in rec.message for rec in caplog.records)


def test_resolve_chain_empty_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list (as opposed to ``None``) is treated the same as no
    override — not a valid zero-rung chain (FailoverProvider requires ≥1)."""
    monkeypatch.setattr("precis.utils.llm.live_config.chain_override", lambda _tier: [])
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)

    chain = router.resolve_chain(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )
    default = router._default_chain(
        Tier.CLOUD_SUPER, tools_needed=True, backend=Backend.OPENAI
    )
    assert chain == default


def test_dispatch_chain_override_honored_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase B unlock: an operator ``llm.chain.<tier>`` override is walked
    even with PRECIS_LLM_FAILOVER OFF — Phase A left a set chain inert unless
    the flag was on, so the operator chain editor's rows would have been
    written but never read. Here rung 0 (OSS) errors and dispatch falls through
    to rung 1 (claude), proving the chain drove the routing."""
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "z-ai/glm-5.2",
                    "transport": "openai_tools",
                },
                {
                    "placement": "cloud",
                    "model": "claude-opus-4-8",
                    "transport": "claude_agent",
                },
            ]
            if tier is Tier.CLOUD_SUPER
            else None
        ),
    )
    oss = _FakeProv(_err("glm down"))
    claude = _FakeProv(_ok("claude saved it"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, oss)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, claude)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))

    assert out.text == "claude saved it"
    assert out.error is None
    assert oss.calls == 1 and claude.calls == 1
    assert oss.model_seen == "z-ai/glm-5.2"  # rung 0's pinned model
    assert claude.model_seen == "claude-opus-4-8"  # rung 1's pinned model


def test_dispatch_single_rung_chain_override_honors_pinned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-rung operator chain still pins its own model — dispatch must
    wrap it (``ladder[0].model is not None``) so the rung's model reaches the
    provider rather than the tier-resolved default."""
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "z-ai/glm-4.7",
                    "transport": "openai_compat",
                }
            ]
            if tier is Tier.CLOUD_SMALL
            else None
        ),
    )
    compat = _FakeProv(_ok("ok", model="z-ai/glm-4.7"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, compat)

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt="x"))

    assert out.text == "ok"
    assert compat.calls == 1
    assert compat.model_seen == "z-ai/glm-4.7"  # the chain's pin, not haiku


# ── cloud throttle (ADR 0066 §5): prune cloud rungs when disabled ───────


def test_rung_is_cloud_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    # operator placement label wins
    assert router._rung_is_cloud(Rung(Transport.OPENAI_TOOLS, model="m", label="cloud"))
    assert not router._rung_is_cloud(
        Rung(Transport.CLAUDE_AGENT, model="m", label="local")
    )
    # transport-intrinsic (no explicit placement)
    assert router._rung_is_cloud(Rung(Transport.CLAUDE_AGENT, label="primary"))
    assert router._rung_is_cloud(Rung(Transport.CLAUDE_P, label="claude-fallback"))
    assert not router._rung_is_cloud(Rung(Transport.LITELLM, label="primary"))
    # OSS transports: hosted (cloud) iff a base url is configured
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert router._rung_is_cloud(Rung(Transport.OPENAI_TOOLS, label="oss"))
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    assert not router._rung_is_cloud(Rung(Transport.OPENAI_TOOLS, label="oss"))


def test_apply_cloud_throttle_noop_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.utils.llm.live_config.cloud_enabled", lambda: True)
    chain = [
        Rung(Transport.CLAUDE_AGENT, label="primary"),
        Rung(Transport.LITELLM, model="q", label="local"),
    ]
    assert router._apply_cloud_throttle(chain) == chain


def test_apply_cloud_throttle_prunes_cloud_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.utils.llm.live_config.cloud_enabled", lambda: False)
    chain = [
        Rung(Transport.OPENAI_TOOLS, model="glm", label="cloud"),
        Rung(Transport.OPENAI_TOOLS, model="qwen", label="local"),
    ]
    assert router._apply_cloud_throttle(chain) == [chain[1]]  # only the local rung


def test_apply_cloud_throttle_empties_cloud_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.utils.llm.live_config.cloud_enabled", lambda: False)
    # FRONTIER's default chain is a single claude rung — no local rung to keep.
    chain = [Rung(Transport.CLAUDE_AGENT, label="primary")]
    assert router._apply_cloud_throttle(chain) == []


def test_dispatch_cloud_throttle_pauses_cloud_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Throttle on + a cloud-only tier (no local rung) → paused (skip-not-fail),
    the provider is never called, and nothing silently degrades to local."""
    monkeypatch.setattr("precis.utils.llm.live_config.cloud_enabled", lambda: False)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override", lambda _tier: None
    )
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    called = _FakeProv(_ok("should not run"))
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_AGENT, called)

    out = dispatch(LlmRequest(tier=Tier.FRONTIER, prompt="x", tools_needed=True))

    assert out.paused is True
    assert out.error is not None and "cloud is disabled" in out.error
    assert called.calls == 0


def test_dispatch_cloud_throttle_drops_to_local_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Throttle on + an operator chain with a local rung → the cloud rung is
    pruned and the local rung runs, keeping the tier flowing (ADR 0066 §5)."""
    monkeypatch.setattr("precis.utils.llm.live_config.cloud_enabled", lambda: False)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "z-ai/glm-5.2",
                    "transport": "openai_tools",
                },
                {
                    "placement": "local",
                    "model": "qwen-heavy",
                    "transport": "openai_tools",
                },
            ]
            if tier is Tier.BIG
            else None
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
    prov = _FakeProv(_ok("local qwen out"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, prov)

    out = dispatch(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=True))

    assert out.text == "local qwen out"
    assert prov.calls == 1
    assert prov.model_seen == "qwen-heavy"  # the local rung; the cloud rung pruned


def test_dispatch_chain_override_falls_through_to_rung_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a 2-rung chain override where rung 0's transport errors —
    FailoverProvider walks to rung 1, exactly as the default ladder would."""
    rung0 = _FakeProv(_err("rung0 down"))
    rung1_calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        rung1_calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="rung1 saved it", cost_usd=None, duration_s=0.0, turns_used=1
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, rung0)
    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "oss-primary",
                    "transport": "openai_tools",
                },
                {
                    "placement": "cloud",
                    "model": "claude-backup",
                    "transport": "claude_agent",
                },
            ]
            if tier is Tier.CLOUD_SUPER
            else None
        ),
    )
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")

    out = dispatch(LlmRequest(tier=Tier.CLOUD_SUPER, prompt="x", tools_needed=True))

    assert out.text == "rung1 saved it"
    assert out.error is None
    assert rung1_calls["model"] == "claude-backup"


# ── paused local slot degrades into the ladder (llm-openrouter-bypass item 3) ──


def test_dispatch_paused_local_slot_falls_back_to_hosted_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated (paused) local-big slot, with failover on, retries rung 0
    unmodified — no ``local_url`` override — so it lands on the hosted OSS
    endpoint (OpenRouter) instead of returning the paused error immediately."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    seen: dict[str, object] = {}

    def fake_tools(req: LlmRequest, *, model: str) -> LlmResult:
        seen["local_url"] = req.local_url
        seen["model"] = model
        return LlmResult(
            text="hosted rung", cost_usd=0.01, turns_used=1, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, _RunFn(fake_tools))
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert out.text == "hosted rung"
    assert out.error is None
    assert out.paused is False
    assert seen["local_url"] is None  # rung 0 retried WITHOUT the busy endpoint
    assert seen["model"] == "qwen-heavy"


def test_dispatch_paused_local_slot_still_falls_to_claude_if_hosted_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paused-slot retry is just rung 0 of the normal ladder — if the
    hosted OSS rung also errors, it falls to the claude rung same as any
    other transport failure."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    oss = _FakeProv(_err("hosted endpoint also unreachable"))
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, oss)

    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="claude saved it", cost_usd=None, duration_s=0.0, turns_used=1
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert out.text == "claude saved it"
    assert out.error is None
    # LOCAL_BIG has no claude id of its own (_TIER_MODEL's default is the OSS
    # alias "qwen-heavy") — it escalates through CLOUD_MID's compiled default.
    assert calls["model"] == "claude-sonnet-5"


def test_dispatch_paused_local_slot_local_small_default_backend_still_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_SMALL under the default (anthropic) backend resolves to
    Transport.LITELLM, which has no hosted mode — it always reads the local
    loopback proxy (``LlmConfig.from_env()``), never ``PRECIS_LLM_BASE_URL``.
    A paused slot must NOT retry through it (that would just re-hit the same
    saturated loopback), even with failover on: it should fall straight
    through to the immediate paused result, same as the no-failover case."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    def _boom(*a: object, **kw: object) -> LlmResult:
        raise AssertionError(
            "LITELLM transport must not be retried on a paused local slot — "
            "it has no hosted escape and would just re-hit the busy loopback"
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.LITELLM, _RunFn(_boom))
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", tools_needed=False))

    assert out.paused is True
    assert "busy" in (out.error or "")


def test_dispatch_paused_local_slot_local_small_openai_backend_falls_to_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL_SMALL under backend=openai resolves to Transport.OPENAI_COMPAT,
    which DOES have a hosted mode — a paused slot should retry rung 0
    unmodified and land on the hosted endpoint, same as LOCAL_BIG."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    seen: dict[str, object] = {}

    def fake_compat(req: LlmRequest, *, model: str) -> LlmResult:
        seen["local_url"] = req.local_url
        seen["model"] = model
        return LlmResult(
            text="hosted rung", cost_usd=0.01, turns_used=1, model=model, tier=req.tier
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, _RunFn(fake_compat))
    monkeypatch.setenv("PRECIS_LLM_FAILOVER", "1")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    out = dispatch(LlmRequest(tier=Tier.LOCAL_SMALL, prompt="x", tools_needed=False))

    assert out.text == "hosted rung"
    assert out.error is None
    assert out.paused is False
    assert seen["local_url"] is None  # rung 0 retried WITHOUT the busy endpoint


def test_dispatch_paused_local_slot_without_failover_still_returns_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failover off ⇒ byte-identical to today: a paused slot returns the
    paused result immediately, no ladder to fall back into."""
    from precis.utils.llm import local_serving as ls

    monkeypatch.setattr(
        ls,
        "acquire",
        lambda model: ls.LocalSlot(
            host="h", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )

    def _boom(*a: object, **kw: object) -> LlmResult:
        raise AssertionError("provider must not run on a paused, non-failover slot")

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, _RunFn(_boom))
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)
    monkeypatch.delenv("PRECIS_LOCAL_BIG_MODEL", raising=False)

    out = dispatch(LlmRequest(tier=Tier.LOCAL_BIG, prompt="x", tools_needed=True))

    assert out.paused is True
    assert "busy" in (out.error or "")


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


def _compat_capture_client(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    """Wire a fake LlmClient that records the extra_body it was called with."""
    import precis.workers.llm_summarize as summ

    class _FakeClient:
        def __init__(self, cfg: object) -> None:
            pass

        def complete(self, messages, *, extra_body=None):  # type: ignore[no-untyped-def]
            captured["extra_body"] = extra_body
            return summ.LlmResult(text="own", total_tokens=3)

    monkeypatch.setattr(summ, "LlmClient", _FakeClient)
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "http://backend.example/v1")


@pytest.mark.parametrize("tier", [Tier.SMALL, Tier.LOCAL_SMALL])
def test_openai_compat_pins_reasoning_off_for_small_judge(
    monkeypatch: pytest.MonkeyPatch, tier: Tier
) -> None:
    """A SMALL-tier tool-less call (classify/summarize/triage) pins reasoning
    OFF, so a reasoning model (z-ai/glm-4.7-flash) emits its short JSON answer
    instead of spending the whole 220-token budget on a reasoning trace and
    returning empty content. Regression for the silent 'None' failure that
    burned ~10k classify chunks."""
    from precis.utils.llm.router import _dispatch_openai_compat

    captured: dict[str, object] = {}
    _compat_capture_client(monkeypatch, captured)
    _dispatch_openai_compat(
        LlmRequest(tier=tier, prompt="classify me", tools_needed=False),
        "z-ai/glm-4.7-flash",
    )
    assert captured["extra_body"] == {"reasoning": {"enabled": False}}


def test_openai_compat_honors_explicit_effort_over_reasoning_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller explicitly set an effort, honour it — don't force
    reasoning off. The auto-off only guards the default (no-effort) SMALL
    judge shape."""
    from precis.utils.llm.router import _dispatch_openai_compat

    captured: dict[str, object] = {}
    _compat_capture_client(monkeypatch, captured)
    _dispatch_openai_compat(
        LlmRequest(tier=Tier.SMALL, prompt="p", tools_needed=False, effort="low"),
        "z-ai/glm-4.7-flash",
    )
    assert captured["extra_body"]["reasoning"] == {"effort": "low"}  # type: ignore[index]


def test_openai_compat_no_reasoning_off_for_non_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Larger / tool-using tiers are not force-disabled — the auto-off is scoped
    to the SMALL tool-less judge shape only (a bigger tier may legitimately
    reason)."""
    from precis.utils.llm.router import _dispatch_openai_compat

    captured: dict[str, object] = {}
    _compat_capture_client(monkeypatch, captured)
    # CLOUD_SUPER, no endpoint/effort → nothing to pin → bare call (extra_body {}).
    _dispatch_openai_compat(
        LlmRequest(tier=Tier.CLOUD_SUPER, prompt="p", tools_needed=False),
        "z-ai/glm-5.2",
    )
    assert "reasoning" not in (captured["extra_body"] or {})  # type: ignore[operator]


def _local_capture_client(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, object]
) -> None:
    """Fake LlmClient recording the LlmConfig.timeout it was constructed with."""
    import precis.workers.llm_summarize as summ

    class _FakeClient:
        def __init__(self, config: object) -> None:
            seen["timeout"] = config.timeout  # type: ignore[attr-defined]

        def complete(self, messages, *, extra_body=None):  # type: ignore[no-untyped-def]
            return summ.LlmResult(text="ok", total_tokens=1)

    monkeypatch.setattr(summ, "LlmClient", _FakeClient)


@pytest.mark.parametrize("tier", [Tier.SMALL, Tier.LOCAL_SMALL])
def test_dispatch_local_caps_small_tier_timeout(
    monkeypatch: pytest.MonkeyPatch, tier: Tier
) -> None:
    """A SMALL-tier local judge caps its timeout to _SMALL_LOCAL_TIMEOUT_S (far
    below the 120s LlmConfig default) so a stuck/flapping loopback proxy fails
    fast → failover, instead of a batch hanging past the worker watchdog
    (2026-07-26 classify stall)."""
    from precis.utils.llm.router import _SMALL_LOCAL_TIMEOUT_S, _dispatch_local

    seen: dict[str, object] = {}
    _local_capture_client(monkeypatch, seen)
    _dispatch_local(LlmRequest(tier=tier, prompt="classify me"), "summarizer")
    assert seen["timeout"] == _SMALL_LOCAL_TIMEOUT_S


def test_dispatch_local_explicit_timeout_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit req.timeout_s overrides the SMALL cap (a caller that
    deliberately wants a different local budget)."""
    from precis.utils.llm.router import _dispatch_local

    seen: dict[str, object] = {}
    _local_capture_client(monkeypatch, seen)
    _dispatch_local(
        LlmRequest(tier=Tier.SMALL, prompt="p", timeout_s=90.0), "summarizer"
    )
    assert seen["timeout"] == 90.0


# ── _provider_api_key: host→vault-key mapping (gripe 159988) ───────────
#
# A provider switch should be a *single* PRECIS_LLM_BASE_URL edit, not also a
# re-copy of the matching key into PRECIS_LLM_API_KEY.


@pytest.mark.parametrize(
    ("base_url", "expected_secret"),
    [
        ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        ("https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY"),
        # a real subdomain of a known host still routes to that host's key.
        ("https://eu.openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        # NOT a subdomain of openrouter.ai (a spoof-y lookalike domain) — the
        # host check is anchored, so this must fall through to the generic key.
        ("https://openrouter.ai.evil.example.com/v1", None),
    ],
)
def test_provider_api_key_routes_by_host(
    monkeypatch: pytest.MonkeyPatch, base_url: str, expected_secret: str | None
) -> None:
    import precis.secrets as secrets
    from precis.utils.llm.router import _provider_api_key

    seen: list[str] = []

    def fake_get_secret(name: str, **kw: object) -> str | None:
        seen.append(name)
        return f"key-for-{name}"

    monkeypatch.setattr(secrets, "get_secret", fake_get_secret)

    key = _provider_api_key(base_url)

    if expected_secret is not None:
        assert seen == [expected_secret]
        assert key == f"key-for-{expected_secret}"
    else:
        assert seen == ["PRECIS_LLM_API_KEY"]
        assert key == "key-for-PRECIS_LLM_API_KEY"


def test_provider_api_key_unlisted_host_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.secrets as secrets
    from precis.utils.llm.router import _provider_api_key

    seen: list[str] = []

    def fake_get_secret(name: str, **kw: object) -> str | None:
        seen.append(name)
        return "vault-key" if name == "PRECIS_LLM_API_KEY" else None

    monkeypatch.setattr(secrets, "get_secret", fake_get_secret)

    # a self-hosted vLLM / proxy host isn't in the provider table.
    assert _provider_api_key("http://vllm.internal:8000/v1") == "vault-key"
    assert seen == ["PRECIS_LLM_API_KEY"]


def test_provider_api_key_falls_back_when_provider_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listed host (OpenRouter) whose specific secret isn't in the vault
    still falls back to PRECIS_LLM_API_KEY — an existing single-key
    deployment keeps working unchanged."""
    import precis.secrets as secrets
    from precis.utils.llm.router import _provider_api_key

    def fake_get_secret(name: str, **kw: object) -> str | None:
        return "generic-key" if name == "PRECIS_LLM_API_KEY" else None

    monkeypatch.setattr(secrets, "get_secret", fake_get_secret)

    assert _provider_api_key("https://openrouter.ai/api/v1") == "generic-key"


def test_provider_api_key_no_host_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.secrets as secrets
    from precis.utils.llm.router import _provider_api_key

    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "fallback-key")

    assert _provider_api_key("") == "fallback-key"


def test_dispatch_openai_compat_uses_openrouter_key_for_openrouter_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pointing PRECIS_LLM_BASE_URL at OpenRouter alone (no other
    env change) resolves OPENROUTER_API_KEY, not PRECIS_LLM_API_KEY."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ
    from precis.utils.llm.router import _dispatch_openai_compat

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen["api_key"] = getattr(config, "api_key", None)

        def complete(self, messages: list[dict[str, str]]) -> object:
            return summ.LlmResult(text="oss out", total_tokens=3)

    def fake_get_secret(name: str, **kw: object) -> str | None:
        return {
            "OPENROUTER_API_KEY": "or-key",
            "DEEPINFRA_API_KEY": "di-key",
            "PRECIS_LLM_API_KEY": "generic-key",
        }.get(name)

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(secrets, "get_secret", fake_get_secret)
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    req = LlmRequest(tier=Tier.CLOUD_SUPER, prompt="hi")
    res = _dispatch_openai_compat(req, "z-ai/glm-5.2")

    assert res.error is None
    assert seen["api_key"] == "or-key"

    # Flip the base url to DeepInfra with NOTHING else changed — the key
    # follows automatically.
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    res2 = _dispatch_openai_compat(req, "z-ai/glm-5.2")
    assert res2.error is None
    assert seen["api_key"] == "di-key"


# ── dispatch_async: streaming twin (router-migration Phase 2) ──────────
#
# No ``pytest-asyncio`` in this repo yet; ``asyncio.run()`` inside a plain
# sync test is the lightest way to drive the coroutine without adding a new
# test-time dependency.


def test_dispatch_async_routes_to_streaming_agent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools_needed=True`` (⇒ CLAUDE_AGENT) + ``on_event`` set ⇒
    ``dispatch_async`` awaits the async agent path instead of the sync
    ``ClaudeAgentProvider``."""
    import asyncio

    from precis.utils.llm.router import dispatch_async

    calls: dict[str, object] = {}

    async def fake_agent_async(prompt: str, **kwargs: object) -> AgentResult:
        calls["prompt"] = prompt
        calls["on_event"] = kwargs.get("on_event")
        return AgentResult(
            final_text="streamed", cost_usd=0.1, duration_s=1.0, turns_used=2
        )

    monkeypatch.setattr(router, "call_claude_agent_async", fake_agent_async)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    async def on_event(evt: dict) -> None:
        pass

    out = asyncio.run(
        dispatch_async(
            LlmRequest(
                tier=Tier.CLOUD_MID,
                prompt="hi",
                tools_needed=True,
                on_event=on_event,
            )
        )
    )
    assert out.text == "streamed"
    assert out.error is None
    assert calls["prompt"] == "hi"
    assert calls["on_event"] is on_event


def test_dispatch_async_falls_back_to_sync_dispatch_without_on_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``on_event`` ⇒ ``dispatch_async`` delegates straight to the sync
    ``dispatch()``, even for a tools-needed cloud tier."""
    import asyncio

    from precis.utils.llm.router import dispatch_async

    seen: dict[str, object] = {}

    def fake_dispatch(req: LlmRequest) -> LlmResult:
        seen["req"] = req
        return LlmResult(
            text="sync path", cost_usd=None, turns_used=None, model="m", tier=req.tier
        )

    monkeypatch.setattr(router, "dispatch", fake_dispatch)

    out = asyncio.run(
        dispatch_async(LlmRequest(tier=Tier.CLOUD_MID, prompt="hi", tools_needed=True))
    )
    assert out.text == "sync path"
    assert seen["req"].prompt == "hi"  # type: ignore[attr-defined]


def test_dispatch_async_falls_back_to_sync_dispatch_for_non_agent_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_event`` is set, but the resolved transport isn't CLAUDE_AGENT (a
    tool-less tier ⇒ ``claude_p``) ⇒ still delegates to the sync
    ``dispatch()`` — streaming only exists on the agent transport."""
    import asyncio

    from precis.utils.llm.router import dispatch_async

    seen: dict[str, object] = {}

    def fake_dispatch(req: LlmRequest) -> LlmResult:
        seen["called"] = True
        return LlmResult(
            text="sync judge", cost_usd=None, turns_used=None, model="m", tier=req.tier
        )

    monkeypatch.setattr(router, "dispatch", fake_dispatch)

    async def on_event(evt: dict) -> None:
        pass

    out = asyncio.run(
        dispatch_async(
            LlmRequest(
                tier=Tier.CLOUD_SMALL,
                prompt="judge",
                tools_needed=False,
                on_event=on_event,
            )
        )
    )
    assert out.text == "sync judge"
    assert seen.get("called") is True


def test_dispatch_async_records_route_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming branch logs via the same ``_record_dispatch`` call the
    sync path uses."""
    import asyncio

    from precis.utils.llm.router import dispatch_async

    async def fake_agent_async(prompt: str, **kwargs: object) -> AgentResult:
        return AgentResult(
            final_text="streamed", cost_usd=0.1, duration_s=1.0, turns_used=1
        )

    monkeypatch.setattr(router, "call_claude_agent_async", fake_agent_async)

    recorded: dict[str, object] = {}

    def fake_record(
        req: object, result: object, *, transport: object, duration_ms: object
    ) -> None:
        recorded["transport"] = transport

    monkeypatch.setattr(router, "_record_dispatch", fake_record)

    async def on_event(evt: dict) -> None:
        pass

    asyncio.run(
        dispatch_async(
            LlmRequest(
                tier=Tier.CLOUD_MID, prompt="hi", tools_needed=True, on_event=on_event
            )
        )
    )
    assert recorded["transport"] is Transport.CLAUDE_AGENT


# ── ADR 0066 Phase A: FRONTIER/BIG/MEDIUM/SMALL — additive, analogue-exact ──
#
# The four new capability tiers must route byte-for-byte identically to
# their analogue (FRONTIER↔CLOUD_SUPER, BIG↔CLOUD_MID, MEDIUM↔CLOUD_SMALL,
# SMALL↔LOCAL_SMALL) — nothing constructs them outside tests/aliases yet, so
# this whole section is a *safety net for the future sweep*, not a behavior
# change today.

_ANALOGUE_PAIRS: tuple[tuple[Tier, Tier], ...] = (
    (Tier.FRONTIER, Tier.CLOUD_SUPER),
    (Tier.BIG, Tier.CLOUD_MID),
    (Tier.MEDIUM, Tier.CLOUD_SMALL),
    (Tier.SMALL, Tier.LOCAL_SMALL),
)

_ENV_VARS_TO_CLEAR = (
    "PRECIS_MODEL_OPUS",
    "PRECIS_MODEL_SONNET",
    "PRECIS_MODEL_HAIKU",
    "PRECIS_SUMMARIZE_MODEL",
    "PRECIS_LOCAL_BIG_MODEL",
)


def _clear_tier_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)


@pytest.mark.parametrize(("new", "analogue"), _ANALOGUE_PAIRS)
def test_new_tier_resolve_model_matches_analogue(
    new: Tier, analogue: Tier, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_tier_env(monkeypatch)
    for backend in (None, Backend.ANTHROPIC, Backend.OPENAI):
        if backend is None:
            assert resolve_model(new) == resolve_model(analogue)
        else:
            assert resolve_model(new, backend=backend) == resolve_model(
                analogue, backend=backend
            )


@pytest.mark.parametrize(("new", "analogue"), _ANALOGUE_PAIRS)
@pytest.mark.parametrize("tools_needed", [False, True])
@pytest.mark.parametrize("backend", [Backend.ANTHROPIC, Backend.OPENAI])
def test_new_tier_select_transport_matches_analogue(
    new: Tier, analogue: Tier, tools_needed: bool, backend: Backend
) -> None:
    assert select_transport(
        new, tools_needed=tools_needed, backend=backend
    ) is select_transport(analogue, tools_needed=tools_needed, backend=backend)


@pytest.mark.parametrize(("new", "analogue"), _ANALOGUE_PAIRS)
@pytest.mark.parametrize("tools_needed", [False, True])
@pytest.mark.parametrize("backend", [Backend.ANTHROPIC, Backend.OPENAI])
def test_new_tier_failover_ladder_matches_analogue(
    new: Tier,
    analogue: Tier,
    tools_needed: bool,
    backend: Backend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_tier_env(monkeypatch)
    # A hosted base url is needed to observe the OSS/claude-fallback shape at
    # all for the cloud analogues under OPENAI; the local analogues (SMALL/
    # LOCAL_SMALL) don't need it for their transport choice but do for the
    # ladder's OSS rung to resolve deterministically.
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    new_ladder = router._failover_ladder(
        new, tools_needed=tools_needed, backend=backend
    )
    analogue_ladder = router._failover_ladder(
        analogue, tools_needed=tools_needed, backend=backend
    )

    assert [r.transport for r in new_ladder] == [r.transport for r in analogue_ladder]
    assert [r.label for r in new_ladder] == [r.label for r in analogue_ladder]
    # A rung's pinned model differs in *label* space only if the analogue's
    # model is tier-specific (it isn't here — both draw the same env var/
    # default row) — so the models line up too.
    assert [r.model for r in new_ladder] == [r.model for r in analogue_ladder]


def test_small_gets_no_claude_fallback_like_local_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMALL (LOCAL_SMALL's analogue) skips Anthropic entirely — same
    no-claude-fallback ladder, even under backend=openai."""
    ladder = router._failover_ladder(
        Tier.SMALL, tools_needed=False, backend=Backend.OPENAI
    )
    assert [r.transport for r in ladder] == [Transport.OPENAI_COMPAT]


def test_tier_table_totality_holds_with_new_tiers() -> None:
    # 5 old + 4 new = 9 members; the import-time assert already guards this,
    # but make it explicit here so a future tier addition without a row
    # fails loudly in this test too.
    assert set(router._TIER_MODEL) == set(Tier)
    assert len(Tier) == 9


def test_new_aliases_resolve_to_new_tiers() -> None:
    assert router.PLANNER_TIER_BY_ALIAS["frontier"] is Tier.FRONTIER
    assert router.PLANNER_TIER_BY_ALIAS["big"] is Tier.BIG
    assert router.PLANNER_TIER_BY_ALIAS["medium"] is Tier.MEDIUM
    assert router.PLANNER_TIER_BY_ALIAS["small"] is Tier.SMALL


def test_resolve_model_byte_for_byte_against_legacy_analogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0066 Phase C: the cloud-alias remap in ``PLANNER_TIER_BY_ALIAS``
    is behavior-preserving ONLY because every new tier resolves to the exact
    same model as its legacy analogue — pin that invariant explicitly (on top
    of the parametrized ``test_new_tier_resolve_model_matches_analogue``
    above) so a future edit to ``_TIER_MODEL`` that breaks it fails loudly
    here too."""
    _clear_tier_env(monkeypatch)
    assert resolve_model(Tier.FRONTIER) == resolve_model(Tier.CLOUD_SUPER)
    assert resolve_model(Tier.BIG) == resolve_model(Tier.CLOUD_MID)
    assert resolve_model(Tier.MEDIUM) == resolve_model(Tier.CLOUD_SMALL)
    assert resolve_model(Tier.SMALL) == resolve_model(Tier.LOCAL_SMALL)


def test_dispatch_client_default_tier_is_new_small() -> None:
    """ADR 0066 Phase C: ``DispatchClient``'s bare default now names the new
    SMALL capability tier — its LOCAL_SMALL analogue, so a caller that never
    passes ``tier=`` keeps resolving to the same model/transport."""
    from precis.utils.llm.router import DispatchClient

    assert DispatchClient().tier is Tier.SMALL


def test_cloud_aliases_remapped_to_new_tiers_local_carve_out_holds() -> None:
    """ADR 0066 Phase C: the three cloud legacy aliases (opus/sonnet/haiku)
    now resolve through the new capability tiers — behavior-identical, since
    each new tier's ``_TIER_MODEL`` row mirrors its legacy analogue
    byte-for-byte. `local` keeps pinning LOCAL_BIG (the privacy carve-out),
    NOT the new BIG tier, until the local-only sensitivity constraint ships."""
    assert router.PLANNER_TIER_BY_ALIAS["opus"] is Tier.FRONTIER
    assert router.PLANNER_TIER_BY_ALIAS["sonnet"] is Tier.BIG
    assert router.PLANNER_TIER_BY_ALIAS["haiku"] is Tier.MEDIUM
    assert router.PLANNER_TIER_BY_ALIAS["local"] is Tier.LOCAL_BIG


def test_llm_tag_big_passes_todo_guards_vocab() -> None:
    """`LLM:big` (the new alias) is a valid closed-vocab tag value — the
    guard is single-sourced from PLANNER_MODEL_ALIASES, so this is a
    regression test on that wiring, not a hardcoded vocab copy."""
    from precis.handlers._todo_guards import _LLM_TAG_VALUES

    assert "big" in _LLM_TAG_VALUES
    assert "frontier" in _LLM_TAG_VALUES
    assert "medium" in _LLM_TAG_VALUES
    assert "small" in _LLM_TAG_VALUES
    # legacy vocab still present alongside the new names.
    assert {"opus", "sonnet", "haiku", "local"} <= _LLM_TAG_VALUES
