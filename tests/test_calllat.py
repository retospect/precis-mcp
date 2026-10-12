"""Per-call latency recorder (gr479849)."""

from __future__ import annotations

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
    assert calllat.status_rows() == []
    calllat.record("get", None, None, outcome="ok", duration_ms=10)
    rows = dict(calllat.status_rows())
    assert rows["tool_calls_1h"].startswith("1 calls")
    assert "tool_latency_1h get/-" in rows


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
