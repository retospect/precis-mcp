"""Per-call latency recorder (gr479849)."""

from __future__ import annotations

import time
from pathlib import Path

import anyio
import pytest

from precis import calllat, server


@pytest.fixture(autouse=True)
def _clean():
    calllat.reset()
    yield
    calllat.reset()


def test_summary_percentiles_and_order():
    for ms in range(1, 101):
        calllat.record("get", "paper", "-", outcome="ok", duration_ms=ms)
    calllat.record("search", "web", None, outcome="error", duration_ms=5)
    rows = calllat.summary()
    assert rows[0][:3] == ("get", "paper", 100)
    assert rows[0][3] == 51 and rows[0][4] == 96
    assert rows[1] == ("search", "web", 1, 5, 5, 1)


def test_window_drops_old_calls():
    calllat.record("get", "paper", None, outcome="ok", duration_ms=1)
    assert calllat.summary(window_s=0, now=1e12) == []


def test_status_rows_empty_then_filled():
    # dispatch_slots reads config, not traffic, so it renders even before
    # the first call; the tool_* rows appear only once a call was served.
    rows = dict(calllat.status_rows())
    assert "tool_calls_1h" not in rows
    assert rows["dispatch_slots"].startswith("limit=")
    calllat.record("get", None, None, outcome="ok", duration_ms=10)
    rows = dict(calllat.status_rows())
    assert rows["tool_calls_1h"].startswith("1 calls")
    assert "tool_latency_1h get/-" in rows


def test_status_rows_include_dispatch_slots():
    """gr677857: the status page shows the dispatch ceiling — the slot
    limit, the live load, and how many calls the queue bound refused in
    the last hour — so "server busy" is diagnosable from the page agents
    are sent to."""
    calllat.record_refusal()
    calllat.record_refusal()
    rows = dict(calllat.status_rows())
    assert "dispatch_slots" in rows
    assert "limit=" in rows["dispatch_slots"]
    assert "refused_1h=2" in rows["dispatch_slots"]
    # A refusal older than the window is not counted.
    rows = dict(calllat.status_rows(now=time.monotonic() + 7200))
    assert "refused_1h=0" in rows["dispatch_slots"]


def test_status_skill_documents_dispatch_slots():
    """The page is only half the fix: precis-status-help must tell an
    agent what the row means and that a busy refusal carries
    ``retry_after_s`` and means the call did not run."""
    text = (
        Path(__file__).parent.parent
        / "src"
        / "precis"
        / "data"
        / "skills"
        / "precis-status-help.md"
    ).read_text(encoding="utf-8")
    assert "dispatch_slots" in text
    assert "retry_after_s" in text
    assert "NOT run" in text


def test_retry_after_s_ignores_unrun_calls():
    """Regression for review td696650: a refused call records
    duration == wait — its whole measured time was queue wait — and so
    does a call cancelled while still queued. If either fed the median,
    the first refusal at the default 45 s bound would pin the hint at
    the 30 s cap instead of the 10 s fallback."""
    calllat.record(
        "get", "paper", None, outcome="error", duration_ms=45_000, wait_ms=45_000
    )
    assert calllat.retry_after_s() == 10
    calllat.record(
        "get",
        "paper",
        None,
        outcome="abandoned",
        duration_ms=45_000,
        wait_ms=45_000,
    )
    assert calllat.retry_after_s() == 10
    # A served call — queued 50 ms, ran to 20 s — feeds the median.
    calllat.record("get", "paper", None, outcome="ok", duration_ms=20_000, wait_ms=50)
    assert calllat.retry_after_s() == 20


def test_refusals_counts_past_the_former_cap():
    """``refused_1h`` reports the real hourly total: the former
    ``maxlen=_MAX_PER_KEY`` deque silently stopped counting at 2000,
    hiding exactly the overload the ``dispatch_slots`` row exists to
    show (review td696650)."""
    for _ in range(calllat._MAX_PER_KEY + 1):
        calllat.record_refusal()
    assert calllat.refusals() == calllat._MAX_PER_KEY + 1
    row = dict(calllat.status_rows())["dispatch_slots"]
    assert f"refused_1h={calllat._MAX_PER_KEY + 1}" in row


def test_log_line_has_no_argument_text(caplog):
    caplog.set_level("INFO", logger="precis.calllat")
    calllat.record("put", "memory", "create", outcome="ok", duration_ms=3, size=42)
    msg = caplog.records[0].getMessage()
    assert "verb=put kind=memory mode=create outcome=ok" in msg
    assert "size=42" in msg


def test_wrapper_records_ok_error_and_exception():
    def get(kind: str = "x", view: str | None = None) -> str:
        if kind == "boom":
            raise ValueError("no")
        return "[error:BadInput] nope" if kind == "bad" else "fine"

    wrapped = server._offload_sync(get, semaphore=anyio.Semaphore(1))

    async def go():
        await wrapped(kind="x", view="v")
        await wrapped(kind="bad")
        with pytest.raises(ValueError):
            await wrapped(kind="boom")

    anyio.run(go)
    by = {r[1]: r for r in calllat.summary()}
    assert by["x"][5] == 0 and by["bad"][5] == 1 and by["boom"][5] == 1
