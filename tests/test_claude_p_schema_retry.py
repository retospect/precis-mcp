"""gr622034: a clean format flake gets ONE ``--json-schema`` resample.

``claude -p`` calls whose reply carried no parseable JSON block used to
raise :class:`ClaudePUnparseableError` outright — card_describe,
quest_tick and draft_refresh dropped ~13% of their dispatches that way
in one 24 h window. The wrapper now re-runs the identical call once with
``--json-schema {"type": "object"}``: the CLI validates the answer
server-side and lands it on the envelope's ``structured_output`` field,
converting the flake into a parseable object instead of a dropped
result. These tests pin the retry's trigger, argv shape, telemetry merge
and — just as hard — its refusal to fire on the paths that must not pay
a second call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import precis.utils.claude_p as claude_p


def _envelope(
    result: Any,
    *,
    cost: float | None = 0.01,
    subtype: str = "success",
    is_error: bool = False,
    structured: Any = None,
    usage: dict[str, Any] | None = None,
) -> str:
    env: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "num_turns": 1,
        "duration_ms": 9,
    }
    if cost is not None:
        env["total_cost_usd"] = cost
    if structured is not None:
        env["structured_output"] = structured
    if usage is not None:
        env["usage"] = usage
    return json.dumps(env)


def _queue_run(returns: list[Any], calls: list[list[str]]) -> Any:
    """A ``run_claude`` stub replaying ``returns`` in order; a
    :class:`Exception` entry is raised instead of returned."""

    def _fake(
        args: Any,
        *,
        binary: str,
        label: str,
        timeout_s: float,
        error_cls: Any,
        env: Any = None,
        **kw: Any,
    ) -> Any:
        calls.append(list(args))
        out = returns[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stdout=out[0], stderr=out[1])

    return _fake


def _stub_calls(monkeypatch: Any, returns: list[Any]) -> list[list[str]]:
    """Patch ``run_claude`` to replay ``returns``; return the argv list
    each invocation saw (one entry per call)."""
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")
    calls: list[list[str]] = []
    monkeypatch.setattr(claude_p, "run_claude", _queue_run(returns, calls))
    return calls


def test_schema_retry_recovers_structured_output(monkeypatch: Any) -> None:
    """The happy path: prose flake → retry → validated object lands."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("a prose answer, no JSON", cost=0.011), ""),
            (
                _envelope(
                    "done",
                    cost=0.009,
                    structured={"verdict": "yes"},
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 5,
                    },
                ),
                "",
            ),
        ],
    )
    res = claude_p.call_claude_p("reply JSON {}")

    assert res.data == {"verdict": "yes"}
    # BOTH runs are billed to the caller — the flaked call's spend stays
    # visible, it is not replaced by the retry's.
    assert res.cost_usd == pytest.approx(0.02)
    assert res.input_tokens == 10
    assert res.output_tokens == 5


def test_schema_retry_passes_the_flag_and_drops_max_turns(
    monkeypatch: Any,
) -> None:
    """``--max-turns 1`` (card_describe's one-shot pin) would cut the
    extraction turn — the retry lifts it; every other arg is identical."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("prose"), ""),
            (_envelope("done", structured={"ok": True}), ""),
        ],
    )
    claude_p.call_claude_p(
        "reply JSON {}", extra_args=("--max-turns", "1", "--verbose")
    )

    assert len(calls) == 2
    first, retry = calls
    assert "--json-schema" not in first
    i = retry.index("--json-schema")
    assert json.loads(retry[i + 1]) == {"type": "object"}
    assert i < retry.index("--")  # a flag, before the prompt sentinel
    assert "--max-turns" not in retry
    assert "1" not in retry  # its value went with it
    assert "--verbose" in retry  # unrelated caller args survive
    # Same prompt, same model cap, same no-tools guarantee.
    assert retry[retry.index("--disallowedTools") + 1] == "*"
    assert retry[-1] == first[-1]


def test_schema_retry_failure_surfaces_the_original_error(
    monkeypatch: Any,
) -> None:
    """A retry-side transport failure (an old CLI that doesn't know the
    flag exits non-zero) must not rewrite the caller's error — the FIRST
    call's unparseable error propagates unchanged, telemetry included."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("prose, no JSON", cost=0.012), ""),
            claude_p.ClaudePError("claude -p exited 1: unknown option"),
        ],
    )
    with pytest.raises(claude_p.ClaudePUnparseableError) as ei:
        claude_p.call_claude_p("reply JSON {}")

    assert len(calls) == 2
    exc = ei.value
    assert "no parseable JSON block" in str(exc)
    assert exc.reply == "prose, no JSON"
    assert exc.cost_usd == 0.012


def test_schema_retry_still_unparseable_raises_original(
    monkeypatch: Any,
) -> None:
    """A second prose reply under the schema flag ends the lane — one
    retry, never a loop, and the first call's error shape is kept."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("prose one"), ""),
            (_envelope("prose two"), ""),
        ],
    )
    with pytest.raises(claude_p.ClaudePUnparseableError):
        claude_p.call_claude_p("reply JSON {}")
    assert len(calls) == 2


def test_no_retry_on_envelope_error(monkeypatch: Any) -> None:
    """An ``is_error``/error-``subtype`` envelope is the CLI's verdict
    (budget stop, API refusal) — it raises before the parse path, so no
    resample is ever billed."""
    calls = _stub_calls(
        monkeypatch,
        [
            (
                _envelope(
                    "Reached your specified limit",
                    subtype="error_max_budget_usd",
                    is_error=True,
                ),
                "",
            ),
        ],
    )
    with pytest.raises(claude_p.ClaudePEnvelopeError):
        claude_p.call_claude_p("reply JSON {}")
    assert len(calls) == 1


def test_no_retry_when_caller_already_constrained(monkeypatch: Any) -> None:
    """A caller that pinned its own ``--json-schema`` gets no resample —
    its flake is a validation failure, not a format one."""
    calls = _stub_calls(
        monkeypatch,
        [(_envelope("prose"), "")],
    )
    with pytest.raises(claude_p.ClaudePUnparseableError):
        claude_p.call_claude_p(
            "reply JSON {}", extra_args=("--json-schema", '{"type":"object"}')
        )
    assert len(calls) == 1


def test_retry_envelope_error_surfaces_original(monkeypatch: Any) -> None:
    """The retry's OWN error envelope (``error_max_structured_output_retries``)
    degrades to the original flake error, not a second failure class."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("prose"), ""),
            (
                _envelope(
                    "validation failed",
                    subtype="error_max_structured_output_retries",
                    is_error=True,
                ),
                "",
            ),
        ],
    )
    with pytest.raises(claude_p.ClaudePUnparseableError):
        claude_p.call_claude_p("reply JSON {}")
    assert len(calls) == 2


def test_retry_falls_back_to_parsing_the_payload(monkeypatch: Any) -> None:
    """``structured_output`` absent but the reply text now carries a JSON
    block — the ordinary tolerant scan still recovers it."""
    calls = _stub_calls(
        monkeypatch,
        [
            (_envelope("prose"), ""),
            (_envelope('here: {"verdict": "yes"}'), ""),
        ],
    )
    res = claude_p.call_claude_p("reply JSON {}")
    assert res.data == {"verdict": "yes"}
    assert len(calls) == 2


def test_retry_bare_text_stub_reply(monkeypatch: Any) -> None:
    """No metering envelope at all (stub/legacy CLI): a JSON object in the
    retried bare-text stdout still parses."""
    calls = _stub_calls(
        monkeypatch,
        [
            ("prose, no json", ""),
            ('{"verdict": "yes"}', "Cost: $0.0009"),
        ],
    )
    res = claude_p.call_claude_p("reply JSON {}")
    assert res.data == {"verdict": "yes"}
    assert res.cost_usd == 0.0009


def test_drop_max_turns_pairs() -> None:
    assert claude_p._drop_max_turns(("--max-turns", "1", "--verbose")) == ("--verbose",)
    assert claude_p._drop_max_turns(("--max-turns=1",)) == ()
    assert claude_p._drop_max_turns(("--verbose",)) == ("--verbose",)
    assert claude_p._drop_max_turns(("--max-turns",)) == ()
