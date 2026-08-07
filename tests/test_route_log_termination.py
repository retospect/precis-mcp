"""`llm_call_log` must distinguish "answered" from "hit the turn ceiling".

Both land with `errored = false`: `max_turns` is a *resumable* exhaustion, not a
failure, which is correct for the executor but left the ledger unable to tell a
tick that did 60 turns of work from one that spun 60 times and produced nothing.

Prod, 2026-08-07, 3h window: five `plan_tick` rows with `turns_used = 60` and
`response_chars` 0–3, at 256–618s each — one todo (197295) exhausting twice.
Nothing alerted, because the only tell was the heuristic
`turns_used >= 60 AND response_chars < 10`.

`_route_features` now stamps the termination on the row. `exhausted` folds the
OSS loop's `stop_reason` and the claude lane's `terminal_reason` into one
boolean so a consumer needn't know which transport ran.
"""

from __future__ import annotations

from precis.utils.llm.router import LlmRequest, LlmResult, Tier, _route_features


def _req() -> LlmRequest:
    return LlmRequest(tier=Tier.BIG, prompt="task", tools_needed=True)


def _result(**kw: object) -> LlmResult:
    base: dict = dict(text="", cost_usd=None, turns_used=None, model="m", tier=Tier.BIG)
    base.update(kw)
    return LlmResult(**base)  # type: ignore[arg-type]


def test_clean_answer_is_not_flagged() -> None:
    f = _route_features(_req(), _result(text="a real answer", stop_reason="stop"))
    assert f["exhausted"] is False
    assert f["empty_output"] is False


def test_oss_turn_ceiling_is_flagged_exhausted() -> None:
    f = _route_features(
        _req(), _result(text="", turns_used=60, stop_reason="max_turns")
    )
    assert f["exhausted"] is True
    assert f["empty_output"] is True


def test_claude_terminal_reason_is_flagged_the_same() -> None:
    """The two lanes spell it differently — one boolean, so a watchdog written
    against the OSS loop doesn't silently miss the claude lane."""
    f = _route_features(_req(), _result(text="", terminal_reason="max_turns"))
    assert f["exhausted"] is True


def test_exhausted_but_productive_is_not_empty_output() -> None:
    """Hitting the ceiling *after* doing real work is a different animal from
    spinning and emitting nothing — the alert should fire on the latter."""
    f = _route_features(
        _req(),
        _result(
            text="minted 3 children and yielded", turns_used=60, stop_reason="max_turns"
        ),
    )
    assert f["exhausted"] is True
    assert f["empty_output"] is False


def test_features_without_a_result_stay_request_only() -> None:
    """Back-compat: the pre-result signature is still valid and adds no keys, so
    an old caller (or a record built before dispatch returns) is unchanged."""
    f = _route_features(_req())
    for k in ("exhausted", "empty_output", "stop_reason", "terminal_reason"):
        assert k not in f
    assert f["source"] is None
    assert f["tier"] == "big"
