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
    return job._process_chunk(
        _ctx(),
        chunk={"chunk_id": 1, "text": "x"},
        paper=1,
        paper_title=None,
        vocab={},
        run_token="r",
        tier=Tier.BIG,
        model_pin=None,
        stats=stats,
        escalations=escalations,
    )


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
