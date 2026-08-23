"""``doctor_tick`` job_type — self-healing spine Layer 3
(``docs/backlog/doctor-tick-report.md``).

Registry/plumbing + ``run()`` are DB-backed (the report artifact is a real
``draft`` ref) with the LLM call stubbed via ``router.dispatch``, mirroring
``test_plan_tick_claude.py``'s pattern for the claude-agent transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.utils.llm import router
from precis.utils.llm.router import LlmResult, Tier
from precis.workers import doctor_report
from precis.workers.job_types import doctor_tick as dt
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.review import _REVIEWER_DISALLOWED_TOOLS

pytestmark = pytest.mark.db


# ── registry ─────────────────────────────────────────────────────


def test_registered_with_run() -> None:
    spec = get_job_type("doctor_tick")
    assert spec is not None
    assert spec.run is dt.run
    assert spec.dispatch is None  # hardcoded run(), not the plugin protocol
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "claude_bin" in spec.requires and "mcp_config" in spec.requires
    assert "doctor_tick" in known_job_types()


# ── run(): happy path writes the day's report ───────────────────


def _clean_result(text: str = "## Classification\nall green\n") -> LlmResult:
    return LlmResult(
        text=text,
        cost_usd=0.05,
        turns_used=4,
        model="claude-opus-4-8",
        tier=Tier.BIG,
        raw_text="<stream-json>",
        duration_s=12.3,
    )


def test_run_happy_path_writes_report_and_uses_deny_list(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_dispatch(req: Any) -> LlmResult:
        seen["req"] = req
        return _clean_result()

    monkeypatch.setattr(router, "dispatch", fake_dispatch)
    monkeypatch.setenv("PRECIS_MCP_CONFIG", "")

    outcome = dt.run(store=store, job_ref_id=1, params={})

    assert outcome.exit_code == 0
    assert outcome.error is None
    assert outcome.report_ref_id is not None

    req = seen["req"]
    assert req.tier is Tier.BIG
    assert req.tools_needed is True
    assert req.disallowed_tools == _REVIEWER_DISALLOWED_TOOLS
    assert "Today's UTC date is" in req.prompt

    date_tag = doctor_report.utc_date_tag()
    ref = doctor_report.find_report(store, date_tag)
    assert ref is not None
    assert int(ref.id) == outcome.report_ref_id
    body = doctor_report.latest_report(store)
    assert body is not None
    assert "Classification" in body.body


def test_run_second_tick_same_day_appends_not_duplicates(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = iter([_clean_result("first tick body"), _clean_result("second tick body")])
    monkeypatch.setattr(router, "dispatch", lambda req: next(calls))

    first = dt.run(store=store, job_ref_id=1, params={})
    second = dt.run(store=store, job_ref_id=2, params={})

    assert first.report_ref_id == second.report_ref_id
    report = doctor_report.latest_report(store)
    assert report is not None
    assert "first tick body" in report.body
    assert "second tick body" in report.body


def test_run_dispatch_error_is_a_failure_and_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        router,
        "dispatch",
        lambda req: LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model="m",
            tier=Tier.BIG,
            error="claude -p (agent) exited 1: boom",
        ),
    )

    outcome = dt.run(store=store, job_ref_id=1, params={})

    assert outcome.exit_code == 1
    assert outcome.report_ref_id is None
    assert "boom" in (outcome.error or "")
    date_tag = doctor_report.utc_date_tag()
    assert doctor_report.find_report(store, date_tag) is None


def test_run_empty_reply_is_a_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "dispatch", lambda req: _clean_result(text="   "))

    outcome = dt.run(store=store, job_ref_id=1, params={})

    assert outcome.exit_code == 1
    assert outcome.report_ref_id is None
    assert "empty" in (outcome.error or "")


def test_run_missing_prompt_is_a_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dt, "_load_prompt", lambda: None)

    outcome = dt.run(store=store, job_ref_id=1, params={})

    assert outcome.exit_code == 1
    assert outcome.report_ref_id is None
    assert outcome.error is not None
