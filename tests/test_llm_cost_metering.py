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


def test_claude_p_unwraps_envelope_usage_tokens(monkeypatch: Any) -> None:
    """The envelope's ``usage`` object round-trips into the four token fields.

    Same key mapping as the agentic lane's ``_stream_usage``:
    ``usage.input_tokens``/``output_tokens``/``cache_read_input_tokens``/
    ``cache_creation_input_tokens``.
    """
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = (
        '{"type":"result","total_cost_usd":0.0412,'
        '"result":"{\\"verdict\\": \\"yes\\"}",'
        '"usage":{"input_tokens":123,"output_tokens":45,'
        '"cache_read_input_tokens":6,"cache_creation_input_tokens":7}}'
    )
    monkeypatch.setattr(
        claude_p, "run_claude", _stub_run({}, stdout=envelope, stderr="")
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.input_tokens == 123
    assert res.output_tokens == 45
    assert res.cache_read_tokens == 6
    assert res.cache_creation_tokens == 7


def test_claude_p_missing_usage_leaves_token_fields_none(monkeypatch: Any) -> None:
    """No ``usage`` block (legacy CLI / test stub) → all-``None``, never a false 0."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = '{"type":"result","total_cost_usd":0.01,"result":"{\\"ok\\": true}"}'
    monkeypatch.setattr(
        claude_p, "run_claude", _stub_run({}, stdout=envelope, stderr="")
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.input_tokens is None
    assert res.output_tokens is None
    assert res.cache_read_tokens is None
    assert res.cache_creation_tokens is None


def test_claude_p_malformed_usage_leaves_token_fields_none(monkeypatch: Any) -> None:
    """A ``usage`` block that isn't a dict (or has non-numeric fields) never raises."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = (
        '{"type":"result","total_cost_usd":0.01,"result":"{\\"ok\\": true}",'
        '"usage":"not-a-dict"}'
    )
    monkeypatch.setattr(
        claude_p, "run_claude", _stub_run({}, stdout=envelope, stderr="")
    )

    res = claude_p.call_claude_p("judge this. reply JSON {}")

    assert res.input_tokens is None
    assert res.output_tokens is None
    assert res.cache_read_tokens is None
    assert res.cache_creation_tokens is None


def test_result_from_claude_p_threads_token_fields() -> None:
    """``result_from_claude_p`` copies the token fields onto ``LlmResult``."""
    from precis.utils.claude_p import ClaudePResult
    from precis.utils.llm.router import result_from_claude_p

    pres = ClaudePResult(
        data={"ok": True},
        raw_stdout="{}",
        cost_usd=0.01,
        text='{"ok": true}',
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=1,
        cache_creation_tokens=2,
    )
    out = result_from_claude_p(pres, model="claude-haiku-4-5", tier=Tier.MEDIUM)

    assert out.input_tokens == 10
    assert out.output_tokens == 20
    assert out.cache_read_tokens == 1
    assert out.cache_creation_tokens == 2


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


def test_unmetered_claude_p_on_subscription_is_silent(
    caplog: Any, monkeypatch: Any
) -> None:
    """gr549279: a subscription claude_p call stamps ``billed_usd = 0`` —
    the ceiling accounts for it correctly even when the nominal
    ``cost_usd`` is missing, so the meter warning does not fire."""
    monkeypatch.delenv("PRECIS_CLAUDE_P_BILLED", raising=False)
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec(transport="claude_p"))
    assert caplog.text == ""


def test_unmetered_claude_p_on_a_billed_login_warns(
    caplog: Any, monkeypatch: Any
) -> None:
    """``PRECIS_CLAUDE_P_BILLED`` truthy → ``billed_usd = cost_usd``, so a
    NULL cost on claude_p is the same ceiling-blind metering gap the
    claude_agent check exists for."""
    monkeypatch.setenv("PRECIS_CLAUDE_P_BILLED", "1")
    route_log.bind_store(None)
    with caplog.at_level(logging.WARNING, logger="precis.route_log"):
        route_log.record_call(_rec(transport="claude_p"))
    assert "no cost_usd" in caplog.text


def test_claude_p_unparseable_envelope_keeps_cost_usage_and_flags(
    monkeypatch: Any,
) -> None:
    """The metering envelope's telemetry rides the raised error — a failed
    call's spend must not be invisible (gr543828)."""
    import json as _json

    import pytest

    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "a prose answer, no JSON",
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            },
        }
    )
    monkeypatch.setattr(claude_p, "run_claude", _stub_run({}, stdout=envelope))

    with pytest.raises(claude_p.ClaudePUnparseableError) as ei:
        claude_p.call_claude_p("reply JSON {}")

    exc = ei.value
    assert exc.reply == "a prose answer, no JSON"
    assert exc.cost_usd == 0.0123
    assert exc.usage is not None and exc.usage["output_tokens"] == 20
    assert exc.is_error is False
    assert exc.subtype == "success"


def test_claude_p_null_result_error_envelope_is_unparseable_not_silent(
    monkeypatch: Any,
) -> None:
    """gr570522: a ``result: null`` error envelope must NOT fall back to the
    raw-stdout path — the old string-``result`` discriminant let the block
    scan return the metering envelope ITSELF as ``data`` (envelope-as-
    answer: billed, marked ok, answer garbage). Metering keys are the
    discriminant now; an empty payload raises with the envelope's flags."""
    import json as _json

    import pytest

    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": None,
            "duration_ms": 31,
            "num_turns": 1,
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 5, "output_tokens": 0},
        }
    )
    monkeypatch.setattr(claude_p, "run_claude", _stub_run({}, stdout=envelope))

    with pytest.raises(claude_p.ClaudePUnparseableError) as ei:
        claude_p.call_claude_p("reply JSON {}")

    exc = ei.value
    assert exc.is_error is True
    assert exc.subtype == "error_during_execution"
    assert exc.cost_usd == 0.02


def test_claude_p_structured_result_dict_is_the_answer(monkeypatch: Any) -> None:
    """A ``result`` that is already a dict (a structured-output envelope)
    is dumped back to text so the scan returns the answer, not the
    envelope (gr570522)."""
    import json as _json

    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": {"verdict": "yes", "score": 3},
            "total_cost_usd": 0.01,
            "num_turns": 1,
            "duration_ms": 9,
        }
    )
    monkeypatch.setattr(claude_p, "run_claude", _stub_run({}, stdout=envelope))

    res = claude_p.call_claude_p("reply JSON {}")

    assert res.data == {"verdict": "yes", "score": 3}
    assert res.cost_usd == 0.01


def test_claude_p_noise_lines_around_the_envelope_still_unwrap(
    monkeypatch: Any,
) -> None:
    """A hook/plugin notice printed ahead of or behind the envelope must
    not strand the metering wrapper on the raw path (gr570522)."""
    import json as _json

    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"verdict": "yes"}',
            "total_cost_usd": 0.011,
            "num_turns": 1,
        }
    )
    stdout = "Warning: plugin 'x' failed to load\n" + envelope + "\nDone."
    monkeypatch.setattr(claude_p, "run_claude", _stub_run({}, stdout=stdout))

    res = claude_p.call_claude_p("reply JSON {}")

    assert res.data == {"verdict": "yes"}
    assert res.cost_usd == 0.011
    assert res.text == '{"verdict": "yes"}'


def test_claude_p_bare_type_result_answer_is_not_an_envelope(
    monkeypatch: Any,
) -> None:
    """A judge whose own answer IS a ``type: "result"`` dict with a
    non-string ``result`` and no metering keys stays on the raw path — it
    parses as the model's answer (the metering-key discriminant keeps it
    off the envelope path, gr570522)."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    monkeypatch.setattr(
        claude_p,
        "run_claude",
        _stub_run({}, stdout='{"type": "result", "result": null, "verdict": "yes"}'),
    )

    res = claude_p.call_claude_p("reply JSON {}")

    assert res.data == {"type": "result", "result": None, "verdict": "yes"}
