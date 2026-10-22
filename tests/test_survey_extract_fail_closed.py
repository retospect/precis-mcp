"""survey_extract fails closed: consecutive errors accumulate, and a run
that errored without writing a row ends failed (gr502793)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.utils.llm.router import Tier
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import survey_extract as job


class _Ctx:
    def __init__(self) -> None:
        self.chunks: list[tuple[str, str]] = []

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))


def _ctx() -> DispatchContext:
    return cast(DispatchContext, _Ctx())


def _call(stats: dict[str, int], escalations: list[dict[str, Any]]) -> str | None:
    outcome, _placement = job._process_chunk(
        _ctx(),
        chunk={"chunk_id": 1, "chunk_kind": "paragraph", "text": "x"},
        paper=1,
        paper_title=None,
        vocab={},
        run_token="r",
        tier=Tier.BIG,
        model_pin=None,
        stats=stats,
        escalations=escalations,
    )
    return outcome


def test_consecutive_errors_accumulate_and_trip_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_SURVEY_EXTRACT_MAX_ERRORS", "3")
    monkeypatch.setattr(
        job,
        "route",
        lambda req: SimpleNamespace(error="no local rung", text="", paused=False),
    )
    stats = job._fresh_stats()
    esc: list[dict[str, Any]] = []
    outcomes = [_call(stats, esc) for _ in range(3)]
    assert outcomes == [None, None, "failed"]
    assert stats["consecutive_errors"] == 3


def test_pauses_do_not_count_toward_the_error_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated local slot is a wait, not an error — a busy castor
    must not read as a down lane (fable-castor-trial-review finding 7)."""
    monkeypatch.setenv("PRECIS_SURVEY_EXTRACT_MAX_ERRORS", "3")
    monkeypatch.setattr(
        job,
        "route",
        lambda req: SimpleNamespace(error=None, text="", paused=True),
    )
    stats = job._fresh_stats()
    esc: list[dict[str, Any]] = []
    outcomes = [_call(stats, esc) for _ in range(5)]
    assert outcomes == ["paused"] * 5
    assert stats["consecutive_errors"] == 0
    assert stats["paused"] == 5
    assert stats["llm_errors"] == 0
    assert esc == []


def test_real_errors_still_trip_the_cap_after_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pauses never spend the error budget; the cap still trips on real
    errors once they resume."""
    monkeypatch.setenv("PRECIS_SURVEY_EXTRACT_MAX_ERRORS", "3")
    results = iter(
        [SimpleNamespace(error=None, text="", paused=True)] * 5
        + [SimpleNamespace(error="no local rung", text="", paused=False)] * 3
    )
    monkeypatch.setattr(job, "route", lambda req: next(results))
    stats = job._fresh_stats()
    esc: list[dict[str, Any]] = []
    outcomes = [_call(stats, esc) for _ in range(8)]
    assert outcomes == ["paused"] * 5 + [None, None, "failed"]
    assert stats["paused"] == 5
    assert stats["llm_errors"] == 3
    assert stats["consecutive_errors"] == 3


def test_length_stop_counts_as_truncated_not_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply cut at the token cap is its own class — truncation, not a
    malformed-JSON failure (fable-castor-trial-review finding 9)."""
    monkeypatch.setattr(
        job,
        "route",
        lambda req: SimpleNamespace(
            error=None,
            text='{"results": [',
            paused=False,
            model="m",
            stop_reason="length",
        ),
    )
    stats = job._fresh_stats()
    esc: list[dict[str, Any]] = []
    assert _call(stats, esc) is None
    assert stats["truncated"] == 1
    assert stats["unparseable_reply"] == 0
    assert stats["dropped"] == 1
    assert esc == [
        {
            "kind": "truncated",
            "chunk": 1,
            "chunk_kind": "paragraph",
            "stop_reason": "length",
            "detail": '{"results": [',
        }
    ]


def test_parse_failure_without_length_stop_is_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job,
        "route",
        lambda req: SimpleNamespace(
            error=None,
            text="not json",
            paused=False,
            model="m",
            stop_reason="stop",
        ),
    )
    stats = job._fresh_stats()
    esc: list[dict[str, Any]] = []
    assert _call(stats, esc) is None
    assert stats["unparseable_reply"] == 1
    assert stats["truncated"] == 0
    assert esc[0]["kind"] == "unparseable_reply"
    assert esc[0]["stop_reason"] == "stop"


def test_zero_writes_with_errors_is_failed_with_dominant_error() -> None:
    stats = {"calls": 3, "written": 0, "llm_errors": 3}
    esc = [
        {"kind": "llm_error", "chunk": 1, "detail": "other"},
        {"kind": "llm_error", "chunk": 2, "detail": "no local rung"},
        {"kind": "llm_error", "chunk": 3, "detail": "no local rung"},
    ]
    done = job._survey_done(_ctx(), stats, esc, "r", "skipped", None)
    assert done.success is False
    assert "dominant error: no local rung" in done.summary


def test_empty_paper_still_succeeds() -> None:
    stats = {"chunks": 0, "calls": 0, "written": 0, "llm_errors": 0}
    done = job._survey_done(_ctx(), stats, [], "r", "skipped", None)
    assert done.success is True


def test_partial_errors_with_writes_succeeds() -> None:
    stats = {"calls": 3, "written": 2, "llm_errors": 1}
    done = job._survey_done(_ctx(), stats, [], "r", "skipped", None)
    assert done.success is True
