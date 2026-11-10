"""Observed wire identity on ``LlmResult`` + the route-log (S5, td568208).

``features.requested`` records what the router asked for (requested model,
effort, booked provider/quant — all inferred routing inputs);
``features.observed`` records only what the wire itself reported back:
the response body's ``model``/``provider`` on OpenAI-compatible wires,
``modelUsage`` on the claude lanes. Absent wire fields leave
``observed`` ``None`` — never synthesized from the request.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import precis.utils.llm.router as router
from precis import route_log
from precis.utils.claude_agent import AgentResult
from precis.utils.claude_p import ClaudePResult
from precis.utils.llm.router import LlmRequest, Tier, route


class _WireTransport:
    """OpenAI ``/chat/completions`` fake: returns ``body`` verbatim."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def post_json(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.body


def _openai_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": "wire reply"}}],
        "usage": {"total_tokens": 9},
    }
    body.update(extra)
    return body


def _complete(body: dict[str, Any]) -> Any:
    """Run the summarize-client completion over ``body``."""
    from precis.workers.llm_summarize import LlmClient, LlmConfig

    cfg = LlmConfig(
        enabled=True,
        url="http://example.invalid/v1",
        api_key="sk",
        model="requested-model",
        max_tokens=64,
        timeout=5.0,
        concurrency=1,
    )
    return LlmClient(cfg, transport=_WireTransport(body)).complete(
        [{"role": "user", "content": "x"}]
    )


def _dispatch_openai_compat(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> router.LlmResult:
    """Run ``_dispatch_openai_compat`` over a fake HTTP 200 carrying ``body``."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    real = summ.LlmClient
    monkeypatch.setattr(
        summ, "LlmClient", lambda cfg: real(cfg, transport=_WireTransport(body))
    )
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    return router._dispatch_openai_compat(
        LlmRequest(tier=Tier.MEDIUM, prompt="x"), model="m"
    )


def _agent_stdout(models: list[str]) -> str:
    """A two-line stream-json transcript ending in a ``result`` event."""
    usage = {m: {"inputTokens": 1, "outputTokens": 1} for m in models}
    return _agent_stdout_usage(usage)


def _agent_stdout_usage(usage: dict[str, Any]) -> str:
    """The same transcript with an explicit ``modelUsage`` payload."""
    return "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "done",
                    "modelUsage": usage,
                }
            ),
        ]
    )


def test_openai_complete_reports_wire_model_and_provider() -> None:
    res = _complete(_openai_body(model="actual-model-20260101", provider="deepinfra"))
    assert res.text == "wire reply"
    assert res.response_model == "actual-model-20260101"
    assert res.response_provider == "deepinfra"


def test_openai_complete_without_identity_fields_reports_none() -> None:
    res = _complete(_openai_body())
    assert res.response_model is None
    assert res.response_provider is None


def test_openai_compat_dispatch_carries_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _dispatch_openai_compat(
        monkeypatch, _openai_body(model="actual-model-20260101", provider="deepinfra")
    )
    assert out.error is None
    assert out.observed == {
        "model": "actual-model-20260101",
        "provider": "deepinfra",
        "quant": None,
    }


def test_openai_compat_dispatch_without_identity_leaves_observed_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _dispatch_openai_compat(monkeypatch, _openai_body())
    assert out.error is None
    assert out.observed is None


def test_result_from_agent_reports_modelusage() -> None:
    res = AgentResult(
        final_text="done",
        cost_usd=0.01,
        duration_s=1.0,
        turns_used=2,
        raw_stdout=_agent_stdout(["claude-opus-5-5-20261101", "claude-haiku-5-5"]),
    )
    out = router.result_from_agent(res, model="claude-opus-5-5", tier=Tier.MEDIUM)
    assert out.observed == {
        "model": "claude-opus-5-5-20261101",
        "models": ["claude-haiku-5-5", "claude-opus-5-5-20261101"],
        "provider": None,
        "quant": None,
    }


def test_result_from_agent_fingerprints_the_primary_model() -> None:
    """``model`` is the ``modelUsage`` id with the most output tokens —
    the primary; the full billed list stays in ``models``."""
    res = AgentResult(
        final_text="done",
        cost_usd=0.01,
        duration_s=1.0,
        turns_used=2,
        raw_stdout=_agent_stdout_usage(
            {
                "claude-opus-5-5-20261101": {"inputTokens": 3, "outputTokens": 900},
                "claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 4},
            }
        ),
    )
    out = router.result_from_agent(res, model="claude-opus-5-5", tier=Tier.MEDIUM)
    assert out.observed == {
        "model": "claude-opus-5-5-20261101",
        "models": ["claude-haiku-5-5", "claude-opus-5-5-20261101"],
        "provider": None,
        "quant": None,
    }


def test_result_from_agent_fingerprint_tie_picks_greater_model_id() -> None:
    """Equal ``outputTokens``: the pick is deterministic — the greater
    model id under the ``(tokens, id)`` max key wins."""
    res = AgentResult(
        final_text="done",
        cost_usd=0.01,
        duration_s=1.0,
        turns_used=2,
        raw_stdout=_agent_stdout_usage(
            {
                "claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 7},
                "claude-opus-5-5-20261101": {"inputTokens": 3, "outputTokens": 7},
            }
        ),
    )
    out = router.result_from_agent(res, model="claude-haiku-5-5", tier=Tier.MEDIUM)
    assert out.observed == {
        "model": "claude-opus-5-5-20261101",
        "models": ["claude-haiku-5-5", "claude-opus-5-5-20261101"],
        "provider": None,
        "quant": None,
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"inputTokens": 9},
        {"inputTokens": 9, "outputTokens": None},
        {"inputTokens": 9, "outputTokens": True},
        {"inputTokens": 9, "outputTokens": "900"},
        {"inputTokens": 9, "outputTokens": [5]},
    ],
    ids=["missing", "none", "bool", "string", "list"],
)
def test_result_from_agent_malformed_output_tokens_counts_as_zero(
    entry: dict[str, Any],
) -> None:
    """A missing/``None``/bool/non-numeric ``outputTokens`` counts as 0
    and never raises, so the other billed model wins the fingerprint."""
    res = AgentResult(
        final_text="done",
        cost_usd=0.01,
        duration_s=1.0,
        turns_used=2,
        raw_stdout=_agent_stdout_usage(
            {
                "claude-haiku-5-5": entry,
                "claude-opus-5-5-20261101": {"outputTokens": 5},
            }
        ),
    )
    out = router.result_from_agent(res, model="m", tier=Tier.MEDIUM)
    assert out.observed == {
        "model": "claude-opus-5-5-20261101",
        "models": ["claude-haiku-5-5", "claude-opus-5-5-20261101"],
        "provider": None,
        "quant": None,
    }


def test_result_from_agent_without_stream_leaves_observed_none() -> None:
    res = AgentResult(final_text="done", cost_usd=0.01, duration_s=1.0, turns_used=None)
    out = router.result_from_agent(res, model="m", tier=Tier.MEDIUM)
    assert out.observed is None


def test_result_from_claude_p_envelope_reports_modelusage() -> None:
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": '{"ok": true}',
            "modelUsage": {"claude-sonnet-4-5-20250929": {"inputTokens": 3}},
        }
    )
    res = ClaudePResult(data={"ok": True}, raw_stdout=envelope, cost_usd=0.01)
    out = router.result_from_claude_p(res, model="sonnet", tier=Tier.SMALL)
    assert out.observed == {
        "model": "claude-sonnet-4-5-20250929",
        "models": ["claude-sonnet-4-5-20250929"],
        "provider": None,
        "quant": None,
    }


def test_logged_row_carries_requested_and_observed(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a routed openai_compat call logs both identity dicts."""
    import precis.secrets as secrets
    import precis.workers.llm_summarize as summ

    tag = uuid4().hex[:8]
    src = f"obs-{tag}"
    body = _openai_body(model="wire-model-9", provider="deepinfra")
    real = summ.LlmClient
    monkeypatch.setattr(
        summ, "LlmClient", lambda cfg: real(cfg, transport=_WireTransport(body))
    )
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "sk")
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("PRECIS_MODEL_HAIKU", "qwen-small")
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)

    route_log.bind_store(store)
    try:
        out = route(
            LlmRequest(
                tier=Tier.MEDIUM,
                prompt="x",
                source=src,
                effort="high",
                endpoint={"provider": "booked-prov", "quant": "fp8"},
            )
        )
    finally:
        route_log.bind_store(None)
    assert out.error is None
    with store.pool.connection() as conn:
        feats = conn.execute(
            "SELECT features FROM llm_call_log WHERE source=%s", (src,)
        ).fetchone()[0]
    assert feats["requested"] == {
        "model": "qwen-small",
        "effort": "high",
        "provider": "booked-prov",
        "quant": "fp8",
    }
    assert feats["observed"] == {
        "model": "wire-model-9",
        "provider": "deepinfra",
        "quant": None,
    }
