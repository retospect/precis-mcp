"""Router-owned failure classification (docs/backlog/llm-quota-failure-classification.md).

One table (:mod:`precis.utils.llm.failure`) classifies a live result in the
router and a captured reason string in the executor; a ``content`` error
never falls through a chain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any
from urllib.error import HTTPError

import pytest

from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.llm import failure, router
from precis.utils.llm.router import (
    FailoverProvider,
    LlmRequest,
    LlmResult,
    Rung,
    Tier,
    Transport,
    route,
)
from precis.workers.executors import _common

NOW = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)


def _http(code: int, msg: str) -> HTTPError:
    return HTTPError("http://x", code, msg, Message(), None)


@pytest.mark.parametrize(
    ("reason", "cls", "horizon"),
    [
        ("API error 429: rate limit exceeded", "rate", timedelta(minutes=15)),
        ("HTTP Error 429: Too Many Requests", "rate", timedelta(minutes=15)),
        ("upstream overloaded (529)", "rate", timedelta(minutes=15)),
        (
            "budget: hourly cap $5.00 reached ($5.10 spent)",
            "budget",
            timedelta(hours=2),
        ),
        ("Insufficient credits on this key", "budget", timedelta(hours=2)),
        ("HTTP Error 503: Service Unavailable", "transport", timedelta(minutes=30)),
        ("connection refused", "transport", timedelta(minutes=30)),
        ("request timed out after 30.0s", "transport", timedelta(minutes=30)),
        ("output blocked by content_policy", "content", None),
        (
            "You've hit your session limit · resets 9pm (UTC)",
            "quota",
            timedelta(hours=1),
        ),
        ("Claude AI usage limit reached|1757203200", "quota", timedelta(hours=2)),
    ],
)
def test_classify_text_classes_and_horizons(reason, cls, horizon) -> None:
    found = failure.classify_text(reason, now=NOW)
    assert found.reason_class == cls
    assert found.retry_at == (NOW + horizon if horizon is not None else None)


def test_unknown_wording_stays_unclassified() -> None:
    assert (
        failure.classify_text("JSON parse error at col 3", now=NOW)
        == failure.UNCLASSIFIED
    )
    assert failure.classify_text("", now=NOW) == failure.UNCLASSIFIED


def test_classify_prefers_structural_exception() -> None:
    # urllib's str() is just "HTTP Error 402: Payment Required" — the status decides.
    assert (
        failure.classify("x", exc=_http(402, "Payment Required"), now=NOW).reason_class
        == "budget"
    )
    assert (
        failure.classify("x", exc=_http(429, "Too Many"), now=NOW).reason_class
        == "rate"
    )
    assert (
        failure.classify("x", exc=_http(500, "ISE"), now=NOW).reason_class
        == "transport"
    )
    assert (
        failure.classify("x", exc=ConnectionResetError(), now=NOW).reason_class
        == "transport"
    )
    # A 4xx other than 402/429 does not decide structurally; the text does.
    assert (
        failure.classify("bad request", exc=_http(400, "Bad"), now=NOW).reason_class
        is None
    )
    assert failure.classify("slow", timed_out=True, now=NOW).reason_class == "transport"


def test_quota_text_wins_over_structural_429() -> None:
    found = failure.classify(
        "You've hit your weekly limit · resets 9am (UTC)",
        exc=_http(429, "Too Many"),
        now=NOW,
    )
    assert found.reason_class == "quota"
    assert found.retry_at == NOW + timedelta(hours=13)


def test_executor_backoff_reads_the_router_table() -> None:
    assert _common.classify_transient_backoff_hours("API error 429") == pytest.approx(
        0.25
    )
    assert _common.classify_transient_backoff_hours("bad gateway") == pytest.approx(0.5)
    assert _common.classify_transient_backoff_hours("out of credits") == pytest.approx(
        2.0
    )
    assert _common.classify_transient_backoff_hours("content_policy violation") is None
    assert _common.classify_transient_backoff_hours("a plain bug") is None


def test_error_result_classifies_claude_process_error() -> None:
    exc = ClaudeProcessError("claude exited 1: API error 429 rate limit", returncode=1)
    out = router._error_result(exc, model="m", tier=Tier.BIG)
    assert out.reason_class == "rate"
    assert out.retry_at is not None
    timed = ClaudeProcessError("claude timed out after 600s", returncode=None)
    timed.timed_out = True
    out = router._error_result(timed, model="m", tier=Tier.BIG)
    assert out.reason_class == "transport" and out.paused


def _errored(msg: str, **kw: Any) -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="m",
        tier=Tier.MEDIUM,
        error=msg,
        **kw,
    )


class _Prov:
    def __init__(self, result: LlmResult) -> None:
        self._result, self.calls = result, 0

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        self.calls += 1
        return self._result


def test_failover_content_error_never_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Prov(_errored("refused: content_policy"))
    fallback = _Prov(
        LlmResult(
            text="ok", cost_usd=None, turns_used=None, model="m", tier=Tier.MEDIUM
        )
    )
    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_COMPAT, primary)
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_P, fallback)
    out = FailoverProvider(
        [Rung(Transport.OPENAI_COMPAT), Rung(Transport.CLAUDE_P, model="c")]
    ).run(LlmRequest(tier=Tier.MEDIUM, prompt="x"), model="m")
    assert out.error is not None and out.reason_class == "content"
    assert fallback.calls == 0


def test_failover_rate_and_unclassified_errors_fall_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = _Prov(
        LlmResult(
            text="ok", cost_usd=None, turns_used=None, model="m", tier=Tier.MEDIUM
        )
    )
    monkeypatch.setitem(router._PROVIDERS, Transport.CLAUDE_P, fallback)
    for msg in ("HTTP Error 429", "something odd happened"):
        monkeypatch.setitem(
            router._PROVIDERS, Transport.OPENAI_COMPAT, _Prov(_errored(msg))
        )
        out = FailoverProvider(
            [Rung(Transport.OPENAI_COMPAT), Rung(Transport.CLAUDE_P, model="c")]
        ).run(LlmRequest(tier=Tier.MEDIUM, prompt="x"), model="m")
        assert out.text == "ok" and out.error is None


def test_route_local_429_is_rate_class(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.workers.llm_summarize as summ

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> object:
            raise _http(429, "Too Many Requests")

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.setenv("PRECIS_LLM_RETRY_ATTEMPTS", "1")
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.error is not None and out.paused
    assert out.reason_class == "rate"
    assert out.retry_at is not None and out.retry_at > datetime.now(UTC)


def test_route_features_carry_reason_class() -> None:
    res = _errored("HTTP Error 429", reason_class="rate", retry_at=NOW)
    feats = router._route_features(LlmRequest(tier=Tier.SMALL, prompt="x"), res)
    assert feats["reason_class"] == "rate"
    assert feats["retry_at"] == NOW.isoformat()
    ok = LlmResult(
        text="fine", cost_usd=None, turns_used=None, model="m", tier=Tier.SMALL
    )
    assert "reason_class" not in router._route_features(
        LlmRequest(tier=Tier.SMALL, prompt="x"), ok
    )


def test_breaker_trip_is_classified_by_gate_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.budget import breaker

    monkeypatch.setattr(
        breaker,
        "gate_tier",
        lambda *a, **k: "budget: claude subscription quota reached",
    )
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    out = route(LlmRequest(tier=Tier.FRONTIER, prompt="x"))
    assert out.paused and out.reason_class == "quota"
    assert out.retry_at is not None
    monkeypatch.setattr(
        breaker, "gate_tier", lambda *a, **k: "budget: hourly cap reached"
    )
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    out = route(LlmRequest(tier=Tier.MEDIUM, prompt="x"))
    assert out.paused and out.reason_class == "budget"


# ── SMALL-lane in-process retry (item 3) ──────────────────────────────


def _flaky_client(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[int]:
    """Install a fake local ``LlmClient`` whose ``complete`` pops ``outcomes``
    in order (an exception instance raises; anything else is returned)."""
    import precis.workers.llm_summarize as summ

    calls: list[int] = []

    class FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> object:
            calls.append(1)
            out = outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out

    monkeypatch.setattr(summ, "LlmClient", FakeClient)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_LLM_RETRY_ATTEMPTS", raising=False)
    return calls


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(router, "_retry_sleep", waits.append)
    return waits


class _Reply:
    def __init__(self, text: str) -> None:
        self.text, self.total_tokens, self.cost_usd = text, 3, None
        self.prompt_tokens = self.completion_tokens = None


def test_small_local_429_then_success_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _flaky_client(monkeypatch, [_http(429, "Too Many"), _Reply("recovered")])
    waits = _patch_sleep(monkeypatch)
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.error is None and out.text == "recovered"
    assert len(calls) == 2 and len(waits) == 1
    assert 1.5 <= waits[0] <= 2.5  # 2s·2⁰ with ±25% jitter


def test_small_retry_exhausts_at_the_bound_and_surfaces_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _flaky_client(
        monkeypatch, [_http(429, "a"), _http(503, "b"), _http(429, "c")]
    )
    waits = _patch_sleep(monkeypatch)
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.error is not None and out.reason_class == "rate" and out.paused
    assert len(calls) == 3 and len(waits) == 2
    assert sum(waits) < 30.0 and waits[1] > waits[0] * 1.2


def test_small_retry_env_off_and_other_tiers_run_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _flaky_client(monkeypatch, [_http(429, "a"), _Reply("never")])
    waits = _patch_sleep(monkeypatch)
    monkeypatch.setenv("PRECIS_LLM_RETRY_ATTEMPTS", "1")
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.reason_class == "rate" and len(calls) == 1 and waits == []


def test_small_retry_skips_content_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    waits = _patch_sleep(monkeypatch)
    calls = _flaky_client(
        monkeypatch, [RuntimeError("blocked by content_policy"), _Reply("n")]
    )
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.reason_class == "content" and len(calls) == 1
    calls = _flaky_client(
        monkeypatch, [TimeoutError("timed out after 30s"), _Reply("n")]
    )
    # A raised socket timeout is a plain transport fault: retried, recovers.
    out = route(LlmRequest(tier=Tier.SMALL, prompt="x"))
    assert out.error is None and len(calls) == 2 and len(waits) == 1
    # A transport-flagged wall-clock timeout (LlmResult.timed_out) is never retried.
    prov = _Prov(_errored("stalled", timed_out=True, paused=True))
    out = router._run_with_retry(
        prov,
        LlmRequest(tier=Tier.SMALL, prompt="x"),
        model="m",
        transport=Transport.LOCAL,
    )
    assert out.reason_class == "transport" and prov.calls == 1
