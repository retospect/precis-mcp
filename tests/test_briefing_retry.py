"""Unit tests for the briefing's transient-LLM-error retry.

Regression guard: a single litellm-proxy blip (a dropped connection, a 5xx)
used to fail the whole day's briefing job with no retry — the daily cron does
not backfill a missed tick, so the morning news silently vanished. The retry
must ride out transient failures but fail fast on a permanent 4xx (e.g. the
retired ``opus`` alias) so misconfiguration surfaces immediately.
"""

from __future__ import annotations

import urllib.error
from typing import cast

import pytest

from precis.workers.briefing import _complete_with_retry, _is_transient_llm_error
from precis.workers.llm_summarize import LlmClient, LlmResult


class _FlakyLlm:
    """Raise ``exc`` for the first ``fail_times`` calls, then return a result."""

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc = exc
        self._fail_times = fail_times
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> LlmResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return LlmResult(text="ok", total_tokens=None)


def _no_sleep(_seconds: float) -> None:
    return None


def _run(llm: _FlakyLlm, *, attempts: int = 3) -> LlmResult:
    return _complete_with_retry(
        cast(LlmClient, llm), [], attempts=attempts, backoff_s=0.0, sleep=_no_sleep
    )


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://proxy/v1", code, "err", {}, None)  # type: ignore[arg-type]


def test_retries_transient_then_succeeds() -> None:
    llm = _FlakyLlm(urllib.error.URLError("Remote end closed connection"), fail_times=2)
    result = _run(llm)
    assert result.text == "ok"
    assert llm.calls == 3  # two failures + one success


def test_exhausts_attempts_and_raises_last() -> None:
    llm = _FlakyLlm(urllib.error.URLError("still down"), fail_times=99)
    with pytest.raises(urllib.error.URLError):
        _run(llm)
    assert llm.calls == 3  # bounded — does not loop forever


def test_permanent_4xx_not_retried() -> None:
    llm = _FlakyLlm(_http(400), fail_times=99)
    with pytest.raises(urllib.error.HTTPError):
        _run(llm)
    assert llm.calls == 1  # a bad request won't fix itself → fail fast


def test_transient_5xx_is_retried() -> None:
    llm = _FlakyLlm(_http(503), fail_times=1)
    result = _run(llm)
    assert result.text == "ok"
    assert llm.calls == 2


def test_classification() -> None:
    assert _is_transient_llm_error(urllib.error.URLError("x"))
    assert _is_transient_llm_error(ConnectionError("x"))
    assert _is_transient_llm_error(TimeoutError("x"))
    assert _is_transient_llm_error(_http(429))
    assert _is_transient_llm_error(_http(502))
    assert not _is_transient_llm_error(_http(400))
    assert not _is_transient_llm_error(_http(404))
    assert not _is_transient_llm_error(RuntimeError("malformed response"))
