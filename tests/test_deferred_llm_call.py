"""``deferred_llm_call`` — a synchronous surface's LLM call run later
(docs/backlog/llm-quota-failure-classification.md item 4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis.utils.llm.router import LlmRequest, LlmResult, Tier
from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import deferred_llm_call as dlc
from precis.workers.job_types import get_job_type, known_job_types

NOW = datetime.now(UTC).replace(microsecond=0)


def test_registered_on_the_coordinator_lane() -> None:
    assert "deferred_llm_call" in known_job_types()
    spec = get_job_type("deferred_llm_call")
    assert spec is not None and spec.compatible_executors == {"coordinator"}
    assert spec.requires == {"claude_bin"} and spec.dispatch is dlc._dispatch


def test_request_round_trips_through_params(tmp_path: Path, monkeypatch) -> None:
    soul = tmp_path / "SOUL.md"
    soul.write_text("be kind", encoding="utf-8")
    req = LlmRequest(
        tier=Tier.FRONTIER,
        source="followup",
        prompt="why?",
        tools_needed=True,
        model="claude-x",
        system_prompt=soul,
        mcp_config=Path("/nonexistent/mcp.json"),
        timeout_s=12.5,
        disallowed_tools=("WebFetch", "WebSearch"),
        output_format="stream-json",
        extra_args=("--verbose",),
        log_event=(object(), 1, "followup"),
    )
    data = dlc.serialize_request(req)
    assert data["tier"] == "frontier" and data["system_prompt"] == str(soul)
    assert "log_event" not in data and data["disallowed_tools"] == [
        "WebFetch",
        "WebSearch",
    ]
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)
    back = dlc.request_from_args(data, log_event=("s", 2, "followup"))
    assert (
        back.tier is Tier.FRONTIER
        and back.prompt == "why?"
        and back.model == "claude-x"
    )
    assert back.system_prompt == soul and back.mcp_config is None
    assert back.disallowed_tools == ("WebFetch", "WebSearch")
    assert back.timeout_s == 12.5 and back.log_event == ("s", 2, "followup")
    # The running host's own env wins when the submitting host's path is absent here.
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(mcp))
    assert dlc.request_from_args(data).mcp_config == mcp


def test_deferral_params_default_horizon() -> None:
    p = dlc.deferral_params(surface="followup", args={"x": 1}, retry_at=None)
    at = datetime.fromisoformat(p["retry_at"])
    assert timedelta(hours=1, minutes=59) < at - datetime.now(UTC) <= timedelta(hours=2)


class _Ctx:
    def __init__(self, params: dict[str, Any], state: dict[str, Any] | None = None):
        self.store = object()
        self.ref_id = 77
        self.title = "t"
        self.meta: dict[str, Any] = {"params": params}
        if state is not None:
            self.meta["coordinator_state"] = state
        self.events: list[str] = []

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append(text)


def _params(retry_at: datetime | None) -> dict[str, Any]:
    return dlc.deferral_params(
        surface="followup",
        args={
            "request": {"tier": "frontier", "prompt": "why?", "tools_needed": True},
            "conv_slug": "followup/memory/20",
            "conv_ref_id": 345,
            "author": "asa",
            "question": "why?",
        },
        retry_at=retry_at,
    )


def _result(error: str | None = None, **kw: Any) -> LlmResult:
    return LlmResult(
        text="" if error else "Because.",
        cost_usd=0.02,
        turns_used=3,
        model="claude-x",
        tier=Tier.FRONTIER,
        error=error,
        duration_s=4.2,
        **kw,
    )


def test_first_slice_parks_until_retry_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dlc, "route", lambda req: pytest.fail("must not dispatch"))
    at = NOW + timedelta(hours=1)
    out = dlc._dispatch(_Ctx(_params(at)), None)
    assert isinstance(out, Yield)
    assert out.wake_when.kind == "at_time"
    assert out.wake_when.payload["ts"] == int(at.timestamp())
    assert out.state["retry_at"] == at.isoformat() and out.state["deferrals"] == 0


def test_after_horizon_runs_and_appends_the_asa_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    turns: list[dict[str, Any]] = []
    monkeypatch.setattr(
        dlc, "route", lambda req: seen.setdefault("req", req) and _result()
    )
    monkeypatch.setattr(dlc, "_append_turn", lambda store, **kw: turns.append(kw))
    past = NOW - timedelta(minutes=1)
    ctx = _Ctx(_params(past), state={"retry_at": past.isoformat(), "deferrals": 1})
    out = dlc._dispatch(ctx, None)
    assert isinstance(out, Done) and out.success
    req = seen["req"]
    assert req.prompt == "why?" and req.tier is Tier.FRONTIER
    assert req.log_event == (ctx.store, 345, "followup")
    assert len(turns) == 1
    assert turns[0]["slug"] == "followup/memory/20"
    assert turns[0]["author"] == "asa" and turns[0]["text"] == "Because."
    assert (
        turns[0]["meta"]["deferred_job"] == 77 and turns[0]["meta"]["cost_usd"] == 0.02
    )
    assert out.summary_meta["deferrals"] == 1


def test_quota_again_defers_to_the_new_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    turns: list[dict[str, Any]] = []
    monkeypatch.setattr(dlc, "_append_turn", lambda store, **kw: turns.append(kw))
    again = NOW + timedelta(hours=3)
    monkeypatch.setattr(
        dlc,
        "route",
        lambda req: _result("quota", paused=True, reason_class="quota", retry_at=again),
    )
    past = NOW - timedelta(minutes=1)
    ctx = _Ctx(_params(past), state={"retry_at": past.isoformat(), "deferrals": 0})
    out = dlc._dispatch(ctx, None)
    assert isinstance(out, Yield) and out.state["deferrals"] == 1
    assert out.wake_when.payload["ts"] == int(again.timestamp())
    assert turns == [] and any("deferring (1/" in e for e in ctx.events)


def test_bound_and_other_failures_tell_the_thread_as_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns: list[dict[str, Any]] = []
    monkeypatch.setattr(dlc, "_append_turn", lambda store, **kw: turns.append(kw))
    past = NOW - timedelta(minutes=1)
    # The deferral bound: one more quota result fails the job, in system's voice.
    monkeypatch.setattr(
        dlc,
        "route",
        lambda req: _result("quota again", paused=True, reason_class="quota"),
    )
    ctx = _Ctx(
        _params(past),
        state={"retry_at": past.isoformat(), "deferrals": dlc.MAX_DEFERRALS - 1},
    )
    out = dlc._dispatch(ctx, None)
    assert isinstance(out, Done) and not out.success
    assert turns[-1]["author"] == "system" and "thinking failed" in turns[-1]["text"]
    # A non-deferrable failure fails at once, same voice; never asa's.
    monkeypatch.setattr(
        dlc, "route", lambda req: _result("boom", reason_class="content")
    )
    out = dlc._dispatch(_Ctx(_params(past), state={"retry_at": past.isoformat()}), None)
    assert isinstance(out, Done) and not out.success
    assert turns[-1]["author"] == "system" and all(t["author"] != "asa" for t in turns)


def test_unknown_surface_fails_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dlc, "route", lambda req: pytest.fail("must not dispatch"))
    out = dlc._dispatch(_Ctx({"surface": "nope", "args": {}}), None)
    assert isinstance(out, Done) and not out.success
