"""Kimi Code subscription adapter (gr543829) — dark-gated provider profile.

Offline: every wire is faked (the provider is unreachable from tests); the
service_config gate is a stub store; the profile table rides
``PRECIS_LLM_PROFILES`` like the gr543825 profile tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

import precis.utils.llm.router as router
import precis.workers.llm_summarize as summ
from precis import secrets
from precis.utils.llm import kimi
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    resolve_profile,
    route,
)


def _http429(url: str, message: str) -> urllib.error.HTTPError:
    import email.message

    return urllib.error.HTTPError(
        url, 429, f"status code: 429, message: {message}", email.message.Message(), None
    )


class _FakeOpenAI:
    def __init__(self, text: str = "ok", total_tokens: int = 5) -> None:
        self.text = text
        self.total_tokens = total_tokens
        self.prompt_tokens = 3
        self.completion_tokens = 2
        self.finish_reason = "stop"


class _GateStore:
    """A ``meter.active_store`` stand-in whose service_config read says
    the gate is lit (or not). Only that one query is answered: the
    budget breaker reads the same bound store (spend sums, app_settings
    caps), and a blanket ``(1,)`` there fakes a tripped $1 cap — the
    meter must read dark (no rows → zero spend, default caps)."""

    def __init__(self, lit: bool) -> None:
        self._lit = lit
        self.pool = self

    def connection(self) -> Any:
        lit = self._lit

        class _CM:
            def __enter__(self) -> Any:
                class _Conn:
                    def __init__(self) -> None:
                        self._q = ""

                    def execute(self, q: str, *a: Any, **k: Any) -> _Conn:
                        self._q = q
                        return self

                    def fetchone(self) -> Any:
                        if "service_config" in self._q:
                            return (1,) if lit else None
                        return None

                return _Conn()

            def __exit__(self, *a: Any) -> None:
                return None

        return _CM()


def _gate(monkeypatch: pytest.MonkeyPatch, lit: bool) -> None:
    from precis.budget import meter

    monkeypatch.setattr("precis.budget.meter.active_store", lambda: _GateStore(lit))
    # The meter memoises BudgetStatus for 15s process-wide, not per store —
    # drop a status a neighbouring test computed so it can't gate (or
    # un-gate) the dispatches below.
    meter._cached = None


def _profiles(monkeypatch: pytest.MonkeyPatch, profiles: dict) -> None:
    monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps(profiles))


def _kimi_profile(**over: Any) -> dict:
    return {kimi.PROFILE_NAME: {**kimi.default_profile(), **over}}


def _paid_profile() -> dict:
    return {
        "paid": {
            "base_url": "https://paid.example.invalid/v1",
            "api_key_ref": "PAID_LLM_KEY",
            "kind": "metered",
        }
    }


def _wire_fakes(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]) -> None:
    """Script the hosted wire: record (url, api_key, model, extra_body)
    per LlmClient; ``complete`` answers ok unless the url carries
    'quota'/'boom' markers."""

    class FakeClient:
        def __init__(self, config: object) -> None:
            seen.append(
                {
                    "url": getattr(config, "url", None),
                    "api_key": getattr(config, "api_key", None),
                    "model": getattr(config, "model", None),
                    "extra_body": None,
                }
            )

        def complete(
            self,
            messages: list[dict[str, str]],
            *,
            extra_body: dict | None = None,
        ) -> _FakeOpenAI:
            seen[-1]["extra_body"] = extra_body
            url = str(seen[-1]["url"])
            if "quota5h" in url:
                raise _http429(
                    url,
                    "You've reached your 5-hour usage limit. Your quota "
                    "will reset when the window rolls over.",
                )
            if "conc" in url:
                raise _http429(
                    url,
                    "You've reached your concurrent request limit. Please "
                    "wait for your current requests to finish.",
                )
            return _FakeOpenAI()

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setattr(
        secrets,
        "get_secret",
        lambda name, **kw: {
            "KIMI_CODE_API_KEY": "sk-kimi",
            "PAID_LLM_KEY": "sk-paid",
        }.get(name),
    )


def _chain(*rungs: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rungs)


def test_default_profile_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped profile spec parses into a ProviderProfile — gate,
    adapter, subscription kind and the coding-ops allowlist all land."""
    _profiles(monkeypatch, _kimi_profile())
    p = resolve_profile(kimi.PROFILE_NAME)
    assert p is not None
    assert p.base_url == kimi.DEFAULT_BASE_URL
    assert p.api_key_ref == kimi.API_KEY_REF
    assert p.kind == "subscription"
    assert p.adapter == kimi.ADAPTER
    assert p.gate == kimi.SERVICE_NAME
    assert p.ops == frozenset(kimi.DEFAULT_OPS)


def test_dark_gate_drops_the_kimi_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate unlit: the kimi rung is skipped at host availability — no wire
    call — and the chain walks to the paid rung."""
    _profiles(monkeypatch, {**_kimi_profile(), **_paid_profile()})
    _gate(monkeypatch, lit=False)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {"transport": "openai_compat", "model": "k3", "profile": "kimi"},
            {"transport": "openai_compat", "model": "m", "profile": "paid"},
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    router._sub_ledger.clear()

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="plan_tick"))

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]


def test_lit_gate_dispatches_at_kimi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate lit: the rung dispatches at the profile's base_url authed by
    KIMI_CODE_API_KEY — never a host-keyed default key."""
    _profiles(monkeypatch, _kimi_profile())
    _gate(monkeypatch, lit=True)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {"transport": "openai_compat", "model": "k3", "profile": "kimi"},
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    router._sub_ledger.clear()

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="plan_tick"))

    assert out.error is None
    assert seen[0]["url"] == kimi.DEFAULT_BASE_URL
    assert seen[0]["api_key"] == "sk-kimi"


def test_ops_allowlist_refuses_non_coding_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source outside the profile's ops allowlist is refused —
    ``subscription_op_refused`` — and the chain falls through."""
    _profiles(monkeypatch, {**_kimi_profile(), **_paid_profile()})
    _gate(monkeypatch, lit=True)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    recorded: list[Any] = []
    from precis import route_log

    monkeypatch.setattr(route_log, "enabled", lambda: True)
    monkeypatch.setattr(route_log, "record_call", lambda rec: recorded.append(rec))
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {"transport": "openai_compat", "model": "k3", "profile": "kimi"},
            {"transport": "openai_compat", "model": "m", "profile": "paid"},
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    router._sub_ledger.clear()

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="reading_brief"))

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]
    skipped = recorded[-2]
    assert skipped.reason_class == "subscription_op_refused"
    assert skipped.features["profile"] == "kimi"


def test_kimi_wire_never_sends_thinking_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """k3.1-pro rejects thinking-disabled requests — on the kimi wire a
    ``thinking=False`` default must not emit ``reasoning.enabled``."""
    _profiles(monkeypatch, _kimi_profile(ops=["my_custom_op"]))
    _gate(monkeypatch, lit=True)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {
                "transport": "openai_compat",
                "model": "k3.1-pro",
                "profile": "kimi",
            },
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    router._sub_ledger.clear()

    out = route(
        LlmRequest(tier=Tier.BIG, prompt="x", source="my_custom_op", thinking=False)
    )

    assert out.error is None
    body = seen[0]["extra_body"]
    assert not body or "reasoning" not in body


def test_wire_routing_shapes() -> None:
    """The kimi wire vocabulary: no disable flag, ``reasoning_effort``
    mapped onto kimi's low/high/max."""
    assert kimi.wire_routing("k3.1-pro", thinking=False) == {}
    assert kimi.wire_routing("k3", thinking=True) == {}
    assert kimi.wire_routing("k3", effort="medium") == {"reasoning_effort": "high"}
    assert kimi.wire_routing("k3", effort="high") == {"reasoning_effort": "max"}
    assert kimi.wire_routing("k3", effort="low") == {"reasoning_effort": "low"}


def test_openrouter_thinking_required_suppresses_disable() -> None:
    """The generic ``thinking_required`` profile field suppresses
    ``reasoning.enabled: false`` on the OpenRouter wire too."""
    body = router.openrouter_routing(None, thinking=False, thinking_required=True)
    assert "reasoning" not in body
    body = router.openrouter_routing(None, thinking=False)
    assert body["reasoning"]["enabled"] is False


def test_quota_error_pauses_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: an exhausted plan pauses the kimi rung (``quota``,
    ``quota_exhausted``) and the chain falls through."""
    _profiles(
        monkeypatch,
        {
            **_kimi_profile(base_url="https://quota5h.example.invalid/v1"),
            **_paid_profile(),
        },
    )
    _gate(monkeypatch, lit=True)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    recorded: list[Any] = []
    from precis import route_log

    monkeypatch.setattr(route_log, "enabled", lambda: True)
    monkeypatch.setattr(route_log, "record_call", lambda rec: recorded.append(rec))
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {"transport": "openai_compat", "model": "k3", "profile": "kimi"},
            {"transport": "openai_compat", "model": "m", "profile": "paid"},
        ),
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    router._sub_ledger.clear()

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="plan_tick"))

    assert out.error is None
    assert seen[-1]["url"] == "https://paid.example.invalid/v1"
    skipped = recorded[-2]
    assert skipped.reason_class == "quota"
    assert skipped.features["profile"] == "kimi"


def test_concurrent_limit_stays_rate() -> None:
    """The concurrent-request limit is risk-control, not quota — the
    transient ``rate`` class with its short horizon."""
    found = kimi.classify_failure(
        "status code: 429, message: You've reached your concurrent "
        "request limit. Please wait for your current requests to finish."
    )
    assert found is not None
    assert found.reason_class == "rate"


def test_unknown_429_maps_to_quota() -> None:
    """Fallback rule: an undocumented 429 on this wire is an
    account-side limit → ``quota`` (the gripe's instruction)."""
    exc = _http429("u", "HTTP Error 429")
    found = kimi.classify_failure(str(exc), exc=exc)
    assert found is not None
    assert found.reason_class == "quota"
    found_text = kimi.classify_failure("status code: 429, message: ??")
    assert found_text is not None and found_text.reason_class == "quota"


def test_quota_class_sets_paused_flags() -> None:
    """adjust_failure marks a quota hit paused + quota_exhausted — the
    rung-pauses acceptance shape."""
    from precis.utils.llm.router import LlmResult

    res = LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="k3",
        tier=Tier.BIG,
        error="status code: 429, message: You've reached your monthly "
        "usage limit for this billing cycle",
    )
    out = kimi.adjust_failure(res)
    assert out.reason_class == "quota"
    assert out.paused is True
    assert out.quota_exhausted is True
    assert out.retry_at is not None


def test_user_agent_is_precis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance: the request's User-Agent is ours — never the Kimi CLI
    or another client's identity."""
    from precis.utils.llm import openai_tools

    monkeypatch.setattr(openai_tools, "polite_user_agent", lambda: "precis-mcp/test")
    captured: dict[str, str] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = 0) -> Any:
        for name, value in req.header_items():
            if name.lower() == "user-agent":
                captured["ua"] = value
        raise OSError("stop")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OSError, match="stop"):
        openai_tools._post_open(
            "https://api.kimi.ai/coding/v1/chat/completions", b"{}", {}, 5
        )
    assert captured["ua"] == "precis-mcp/test"


def test_seed_cards_writes_subscription_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each documented model id mints a card whose offering carries the
    subscription billing class — never a token price."""
    calls: list[dict[str, Any]] = []

    def _fake_upsert(store: Any, **kw: Any) -> tuple[int, bool]:
        calls.append(kw)
        return 1000 + len(calls), True

    monkeypatch.setattr("precis.llm_catalog.upsert_card", _fake_upsert)
    results = kimi.seed_cards(object())

    assert [r[0] for r in results] == list(kimi.MODELS)
    for kw in calls:
        (offering,) = kw["offerings"]
        assert offering["billing"] == "subscription"
        assert offering["transport"] == "openai_compat"
        assert "price_in" not in offering and "price_out" not in offering


def test_offering_billing_validation() -> None:
    """``billing`` is a closed vocabulary on offerings."""
    from precis.errors import BadInput
    from precis.llm_catalog import build_meta

    meta = build_meta(model_id="m", offerings=[{"billing": "subscription"}])
    assert meta["offerings"][0]["billing"] == "subscription"
    with pytest.raises(BadInput):
        build_meta(model_id="m", offerings=[{"billing": "free"}])
