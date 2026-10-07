"""Quota result metadata and executor parking share one reset horizon."""

from datetime import UTC, datetime, timedelta

import pytest

from precis.utils.claude_agent import AgentResult
from precis.utils.claude_p import ClaudePResult
from precis.utils.llm import quota, router
from precis.workers.executors import _common

NOW = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.mark.parametrize("adapter", ["agent", "claude_p"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You've hit your session limit · resets 9pm (UTC)", NOW + timedelta(hours=1)),
        ("You've hit your weekly limit · resets 9am (UTC)", NOW + timedelta(hours=13)),
        (
            "You're out of extra usage · resets 3:40pm (America/Los_Angeles)",
            datetime(2026, 10, 5, 22, 40, tzinfo=UTC),
        ),
        ("You've hit your session limit · resets 9pm", NOW + timedelta(hours=1)),
        ("You've hit your session limit", NOW + timedelta(hours=6)),
        ("You've hit your weekly limit · resets 13pm (UTC)", NOW + timedelta(hours=6)),
        (
            "You've hit your weekly limit · resets 9pm (Invalid/Zone)",
            NOW + timedelta(hours=6),
        ),
        ("Claude AI usage limit reached|1757203200", NOW + timedelta(hours=2)),
        (
            "You have reached your specified API usage limits. "
            "You will regain access on 2026-10-07 at 00:00 UTC.",
            datetime(2026, 10, 7, tzinfo=UTC),
        ),
        (
            "You have reached your specified API usage limits. "
            "You will regain access on 2026-10-01 at 00:00 UTC.",
            NOW,
        ),
        (
            "You have reached your specified API usage limits. "
            "You will regain access on 2026-99-07 at 00:00 UTC.",
            NOW + timedelta(hours=2),
        ),
    ],
)
def test_quota_metadata_agrees_with_executor_horizon(
    monkeypatch, adapter, text, expected
):
    monkeypatch.setattr(quota, "datetime", FrozenDateTime)
    monkeypatch.setattr(_common, "datetime", FrozenDateTime)
    if adapter == "agent":
        agent_result = AgentResult(
            final_text=text, cost_usd=0.0, duration_s=0.0, turns_used=1
        )
        result = router.result_from_agent(
            agent_result, model="claude-sonnet-5", tier=router.Tier.BIG
        )
    else:
        claude_p_result = ClaudePResult(
            data={}, raw_stdout=text, cost_usd=0.0, text=text
        )
        result = router.result_from_claude_p(
            claude_p_result, model="claude-sonnet-5", tier=router.Tier.BIG
        )

    assert result.reason_class == "quota"
    assert result.paused and result.quota_exhausted
    assert result.retry_at == expected
    assert result.error is not None
    hours = _common.classify_transient_backoff_hours(result.error)
    assert hours is not None
    assert NOW + timedelta(hours=hours) == result.retry_at


def test_parsed_answer_does_not_gain_quota_metadata():
    text = "You've hit your session limit · resets 9pm (UTC)"
    raw = ClaudePResult(data={"note": text}, raw_stdout=text, cost_usd=0.0, text=text)
    result = router.result_from_claude_p(
        raw, model="claude-sonnet-5", tier=router.Tier.BIG
    )
    assert not result.paused
    assert result.reason_class is None and result.retry_at is None


def test_successful_agent_answer_keeps_metadata_unset():
    raw = AgentResult(
        final_text="A useful answer.", cost_usd=0.0, duration_s=0.0, turns_used=1
    )
    result = router.result_from_agent(
        raw, model="claude-sonnet-5", tier=router.Tier.BIG
    )
    assert result.reason_class is None and result.retry_at is None


def test_bare_rate_limit_has_no_quota_reset():
    assert quota.quota_retry_at("API error 429: rate limit exceeded", now=NOW) is None
    assert (
        _common.classify_transient_backoff_hours("API error 429: rate limit exceeded")
        == 0.25
    )
