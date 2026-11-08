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
    # gr586613: the paid fallback profile's fake key needs a code
    # allowlist entry for its fake host.
    monkeypatch.setitem(
        router._PROFILE_KEY_HOST_ALLOWLISTS,
        "PAID_LLM_KEY",
        frozenset({"paid.example.invalid"}),
    )


def _chain(*rungs: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rungs)


def test_default_profile_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped profile spec parses into a ProviderProfile — gate,
    adapter, subscription kind and an EMPTY ops allowlist all land (the
    2026-11-01 ruling: no autonomous use, so the default serves no op)."""
    assert kimi.DEFAULT_OPS == ()
    _profiles(monkeypatch, _kimi_profile())
    p = resolve_profile(kimi.PROFILE_NAME)
    assert p is not None
    assert p.base_url == kimi.DEFAULT_BASE_URL
    assert p.api_key_ref == kimi.API_KEY_REF
    assert p.kind == "subscription"
    assert p.adapter == kimi.ADAPTER
    assert p.gate == kimi.SERVICE_NAME
    assert p.ops == frozenset()


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

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="plan_tick"))

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]


def test_lit_gate_dispatches_at_kimi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate lit + op allowlisted + an interactive call: the rung dispatches
    at the profile's base_url authed by KIMI_CODE_API_KEY — never a
    host-keyed default key."""
    _profiles(monkeypatch, _kimi_profile(ops=["my_op"]))
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

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="my_op",
            work_class="interactive",
        )
    )

    assert out.error is None
    assert seen[0]["url"] == kimi.DEFAULT_BASE_URL
    assert seen[0]["api_key"] == "sk-kimi"


def test_ops_allowlist_refuses_non_coding_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source outside the profile's ops allowlist is refused —
    ``subscription_op_refused`` — and the chain falls through. The call
    is interactive so the worker guard isn't what refuses it."""
    _profiles(monkeypatch, {**_kimi_profile(ops=["my_op"]), **_paid_profile()})
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

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="reading_brief",
            work_class="interactive",
        )
    )

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]
    skipped = recorded[-2]
    assert skipped.reason_class == "subscription_op_refused"
    assert skipped.features["profile"] == "kimi"


def test_default_profile_routes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped allowlist is empty: even an interactive call is refused
    (``subscription_op_refused``) — the default profile routes nothing
    until an operator lists ops."""
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

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="plan_tick",
            work_class="interactive",
        )
    )

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]
    skipped = recorded[-2]
    assert skipped.reason_class == "subscription_op_refused"
    assert skipped.features["profile"] == "kimi"


@pytest.mark.parametrize(
    "req_kwargs",
    [
        {"work_class": "background"},
        {"work_class": "deliverable"},
        {},
    ],
    ids=["background", "deliverable", "unset-defaults-background"],
)
def test_kimi_rung_refuses_worker_origin(
    monkeypatch: pytest.MonkeyPatch, req_kwargs: dict[str, Any]
) -> None:
    """The ruling: no autonomous use — a non-interactive call on the kimi
    rung is refused (``subscription_worker_refused``) even when its op IS
    allowlisted, and the chain falls through to the paid rung."""
    _profiles(monkeypatch, {**_kimi_profile(ops=["plan_tick"]), **_paid_profile()})
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

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="plan_tick", **req_kwargs))

    assert out.error is None
    assert [c["url"] for c in seen] == ["https://paid.example.invalid/v1"]
    skipped = recorded[-2]
    assert skipped.reason_class == "subscription_worker_refused"
    assert skipped.features["profile"] == "kimi"
    assert "interactive" in (skipped.error or "")


def test_pinned_kimi_dispatch_refuses_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned ``req.profile`` call bypasses the rung-level check — the
    dispatch-level guard still refuses a worker-origin call before the
    wire, with no fallthrough rung to hide it."""
    _profiles(monkeypatch, _kimi_profile(ops=["plan_tick"]))
    _gate(monkeypatch, lit=True)
    seen: list[dict[str, Any]] = []
    _wire_fakes(monkeypatch, seen)
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda _tier: _chain(
            {"transport": "openai_compat", "model": "k3"},
        ),
    )
    # The profile-less rung needs SOME hosted base url to survive
    # resolve_chain; req.profile pins kimi at dispatch regardless.
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://x.example.invalid/v1")

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="plan_tick",
            profile="kimi",
            work_class="background",
        )
    )

    assert out.reason_class == "subscription_worker_refused"
    assert "interactive" in (out.error or "")
    assert seen == []


def test_interactive_scope_allows_listed_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient ``work_class_scope('interactive')`` counts as interactive
    — a listed op dispatches at kimi without an explicit request flag."""
    from precis.budget.work_class import work_class_scope

    _profiles(monkeypatch, _kimi_profile(ops=["my_op"]))
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

    with work_class_scope("interactive"):
        out = route(LlmRequest(tier=Tier.BIG, prompt="x", source="my_op"))

    assert out.error is None
    assert seen[0]["url"] == kimi.DEFAULT_BASE_URL


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

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="my_custom_op",
            thinking=False,
            work_class="interactive",
        )
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
            **_kimi_profile(base_url="https://api.kimi.ai/quota5h/v1", ops=["my_op"]),
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

    out = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt="x",
            source="my_op",
            work_class="interactive",
        )
    )

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


# ── shared base-URL resolver (td573681 round 1) ───────────────────────────


def test_base_url_defaults_to_overseas(monkeypatch: pytest.MonkeyPatch) -> None:
    """No profile, no ``base_url`` on it — the documented overseas
    endpoint stands."""
    monkeypatch.delenv("PRECIS_LLM_PROFILES", raising=False)
    assert kimi.base_url() == kimi.BASE_URL_OVERSEAS


def test_base_url_reads_the_kimi_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account's region decides: a ``base_url`` on the ``kimi``
    profile wins (China's ``api.kimi.com`` host)."""
    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES",
        json.dumps({"kimi": {"base_url": kimi.BASE_URL_CHINA}}),
    )
    assert kimi.base_url() == kimi.BASE_URL_CHINA


@pytest.mark.parametrize("override", ["", 123], ids=["empty", "non-str"])
def test_base_url_unset_override_defaults(
    monkeypatch: pytest.MonkeyPatch, override: Any
) -> None:
    """An empty or non-string ``base_url`` reads as unset — the overseas
    default stands."""
    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES", json.dumps({"kimi": {"base_url": override}})
    )
    assert kimi.base_url() == kimi.BASE_URL_OVERSEAS
    assert kimi.base_url_refusal() is None


@pytest.mark.parametrize(
    "override",
    [
        "https://api.kimi.com/coding/v1",
        "https://API.KIMI.AI./coding/v1",
        "https://api.kimi.ai:443/coding/v1",
    ],
    ids=["china", "case-trailing-dot", "port-443"],
)
def test_base_url_accepts_allowlisted_hosts(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    """The documented endpoints pass — case and a trailing dot normalise
    before the exact-host check, and port 443 is the https default."""
    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES", json.dumps({"kimi": {"base_url": override}})
    )
    assert kimi.base_url() == override
    assert kimi.base_url_refusal() is None


@pytest.mark.parametrize(
    "override",
    [
        "http://api.kimi.com/coding/v1",
        "not-a-url",
        "https://evil.example.invalid/v1",
        "https://user:pass@api.kimi.ai/coding/v1",
        "https://api.kimi.ai:8443/coding/v1",
        "https://1.2.3.4/v1",
        "https://api.kimi.ai.evil.example/v1",
    ],
    ids=[
        "http",
        "garbage",
        "other-host",
        "userinfo",
        "port",
        "ip-literal",
        "suffix-host",
    ],
)
def test_base_url_refuses_off_allowlist(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    """Review round 2 item 3: any host off the documented pair — and any
    non-https, userinfo or non-443-port spelling — is refused, never a
    silent fallback to overseas: the key is Bearer-sent to whatever this
    resolves to, so a bad override is an exfiltration path. The refusal
    names the setting and the allowlist."""
    from precis.errors import BadInput

    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES", json.dumps({"kimi": {"base_url": override}})
    )
    with pytest.raises(BadInput, match="llm.profiles"):
        kimi.base_url()
    refused = kimi.base_url_refusal()
    assert refused is not None
    assert "llm.profiles" in refused
    assert "api.kimi.ai" in refused and "api.kimi.com" in refused
    assert "pass" not in refused  # never the raw URL — userinfo could carry one
