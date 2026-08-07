"""Every billed Claude call must report a cost.

Cost is not something a provider volunteers — both claude lanes only report it
when the call site asks via ``--output-format``. That made metering a silent
per-call-site opt-in, and the sites that never asked (briefing 815 calls,
meditation, card_forge, reading_brief, quest_tick, figure) logged >1000 Claude
calls at ``NULL`` cost — invisible to ``PRECIS_DAILY_COST_CEILING``, which sums
``llm_call_log.cost_usd``. Every spend figure was a lower bound.

Three guards, one per layer of the fix:

* the ``claude_agent`` lane asks by default (``LlmRequest.output_format``),
* the ``claude_p`` lane asks by default and unwraps the envelope,
* the log chokepoint gets loud when a billed call still reports nothing.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import precis.utils.claude_p as claude_p
from precis import route_log
from precis.route_log import LlmCallRecord
from precis.utils.llm.router import LlmRequest, Tier

# ── the claude_agent lane ──────────────────────────────────────────────


def test_llm_request_defaults_to_a_cost_reporting_output_format() -> None:
    """``text`` yields no cost — the default must be the metered shape.

    ``claude -p`` reports ``total_cost_usd`` only in the trailing stream-json
    ``result`` event; the text path falls back to a stderr regex for a
    ``Cost: $N.NN`` line modern Claude Code no longer prints. Defaulting here
    makes cost the default and text the deliberate opt-out.
    """
    assert LlmRequest(tier=Tier.BIG, prompt="hi").output_format == "stream-json"


# ── the claude_p lane ──────────────────────────────────────────────────


def _stub_run(captured: dict, *, stdout: str, stderr: str = "") -> Any:
    def _fake(args, *, binary, label, timeout_s, error_cls, env=None):
        captured["args"] = list(args)
        return SimpleNamespace(stdout=stdout, stderr=stderr)

    return _fake


def test_claude_p_asks_for_the_json_envelope(monkeypatch: Any) -> None:
    """Without ``--output-format json`` this lane can never report a cost."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    captured: dict = {}
    monkeypatch.setattr(
        claude_p, "run_claude", _stub_run(captured, stdout='{"ok": true}')
    )

    claude_p.call_claude_p("reply JSON {}")

    args = captured["args"]
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "json"


def test_claude_p_unwraps_envelope_cost_and_payload(monkeypatch: Any) -> None:
    """The envelope carries the dollars; ``result`` carries the model's JSON."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = (
        '{"type":"result","total_cost_usd":0.0412,"num_turns":2,'
        '"result":"here you go: {\\"verdict\\": \\"yes\\"}"}'
    )
    monkeypatch.setattr(
        claude_p, "run_claude", _stub_run({}, stdout=envelope, stderr="")
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.cost_usd == 0.0412
    # The parse contract is unchanged: last balanced block of the *payload*,
    # not of the envelope (which would have grabbed the envelope itself).
    assert res.data == {"verdict": "yes"}


def test_claude_p_result_text_is_the_answer_not_the_envelope(
    monkeypatch: Any,
) -> None:
    """Turning on metering must not redefine what ``text`` means.

    ``raw_stdout`` gained a JSON wrapper; ``LlmResult.text`` must not. Callers
    like ``taproot.canon`` do ``res.data or _parse_json_object(res.text)`` — fed
    the envelope, that fallback would parse the CLI's metadata keys as if they
    were the judge's answer.
    """
    from precis.utils.llm.router import result_from_claude_p

    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = (
        '{"type":"result","total_cost_usd":0.01,"result":"{\\"verdict\\": \\"yes\\"}"}'
    )
    monkeypatch.setattr(claude_p, "run_claude", _stub_run({}, stdout=envelope))

    res = claude_p.call_claude_p("judge this. reply JSON {}")
    out = result_from_claude_p(res, model="claude-haiku-4-5", tier=Tier.MEDIUM)

    assert "total_cost_usd" not in out.text
    assert out.text == '{"verdict": "yes"}'
    assert out.cost_usd == 0.01


def test_claude_p_does_not_mistake_a_judge_result_field_for_the_envelope(
    monkeypatch: Any,
) -> None:
    """A judge whose own schema has a string ``result`` must survive.

    Discriminating on a string ``result`` alone would unwrap this reply and
    then fail to parse the unwrapped string, raising "no parseable JSON block"
    — an error pointing nowhere near the real cause.
    """
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr(
        claude_p,
        "run_claude",
        _stub_run({}, stdout='{"result": "same", "confidence": 0.9}'),
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.data == {"result": "same", "confidence": 0.9}


def test_claude_p_passes_through_a_bare_json_body(monkeypatch: Any) -> None:
    """A stub binary / older CLI that ignores the flag must still parse.

    Backward compatibility is what keeps this change safe: anything that is not
    a recognizable envelope (``result`` holding a *string*) is handed back
    untouched, and cost falls back to the legacy stderr line.
    """
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr(
        claude_p,
        "run_claude",
        _stub_run({}, stdout='{"verdict": "yes"}', stderr="Cost: $0.0009"),
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.data == {"verdict": "yes"}
    assert res.cost_usd == 0.0009


# ── the log chokepoint ─────────────────────────────────────────────────


def _rec(**kw: Any) -> LlmCallRecord:
    base: dict[str, Any] = {
        "source": "briefing",
        "tier": "big",
        "transport": "claude_agent",
        "model": "claude-sonnet-5",
        "tools_needed": True,
        "request_text": "REQ",
        "response_text": "RESP",
        "cost_usd": None,
        "turns_used": 1,
        "duration_ms": 10,
        "errored": False,
        "error": None,
        "data_parsed": None,
    }
    base.update(kw)
    return LlmCallRecord(**base)


def test_unmetered_billed_call_warns(caplog: Any) -> None:
    """The standing detector: a default only protects today's call sites."""
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec())
    assert "no cost_usd" in caplog.text
    assert "briefing" in caplog.text


def test_local_transport_with_no_cost_is_silent(caplog: Any) -> None:
    """``None`` on a local lane means free, not broken — must not cry wolf."""
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec(transport="openai_compat", source="classify"))
    assert caplog.text == ""


def test_errored_billed_call_is_silent(caplog: Any) -> None:
    """A transport that blew up legitimately has no cost to report."""
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec(errored=True, error="boom"))
    assert caplog.text == ""


def test_metered_billed_call_is_silent(caplog: Any) -> None:
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec(cost_usd=0.02))
    assert caplog.text == ""
