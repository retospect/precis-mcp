"""gr494831 step 1: the dispatcher retries transient database faults.

Reads (and idem-keyed puts) are replayed; any other write is never
replayed. A fault that persists surfaces as ``Upstream`` naming the retry
count.
"""

from __future__ import annotations

import psycopg.errors as pge
import psycopg_pool
import pytest

from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.runtime import dispatch as d

_TRANSIENT = [
    pge.LockNotAvailable("lock"),
    psycopg_pool.PoolTimeout("pool"),
    pge.SerializationFailure("ser"),
    pge.DeadlockDetected("dead"),
]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    slept: list[int] = []
    monkeypatch.setattr(d, "_retry_sleep", slept.append)
    return slept


def _flaky(runtime: PrecisRuntime, handler_verb: str, failures: int, exc: Exception):
    """Make calc.<verb> raise ``exc`` ``failures`` times, then answer."""
    calc = runtime.hub.handler_for("calc")
    assert calc is not None
    calls = {"n": 0}

    def fn(**_kw: object) -> Response:
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return Response(body="ok-after-retry")

    setattr(calc, handler_verb, fn)
    return calls


@pytest.mark.parametrize("exc", _TRANSIENT, ids=lambda e: type(e).__name__)
def test_read_retries_and_succeeds(
    runtime: PrecisRuntime, exc: Exception, _no_sleep: list[int]
) -> None:
    calls = _flaky(runtime, "get", 2, exc)
    out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    assert "ok-after-retry" in out
    assert calls["n"] == 3 and _no_sleep == [1, 2]


def test_persistent_failure_names_the_retry_count(runtime: PrecisRuntime) -> None:
    calls = _flaky(runtime, "get", 99, pge.LockNotAvailable("lock"))
    out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    assert calls["n"] == d._RETRY_ATTEMPTS
    assert "[error:Upstream]" in out
    assert f"retried {d._RETRY_ATTEMPTS - 1} times" in out
    assert "next:" in out and "retry" in out


def test_non_idempotent_put_is_not_retried(runtime: PrecisRuntime) -> None:
    calls = {"n": 0}

    def boom(verb: str, args: dict) -> None:
        calls["n"] += 1
        raise pge.LockNotAvailable("lock")

    runtime._dispatch_inner = boom  # type: ignore[method-assign]
    out = runtime.dispatch("put", {"kind": "memory", "text": "x"})
    assert calls["n"] == 1
    assert "[error:Upstream]" in out and "retried" not in out


@pytest.mark.parametrize("verb", ["edit", "delete", "tag", "link"])
def test_other_writes_are_not_retried(runtime: PrecisRuntime, verb: str) -> None:
    assert not d._replay_safe(verb, {"idem_key": "k"})


def test_put_with_idem_key_is_replay_safe() -> None:
    assert d._replay_safe("put", {"kind": "job", "idem_key": "job:1"})
    assert not d._replay_safe("put", {"kind": "job", "idem_key": None})
    assert not d._replay_safe("put", {"kind": "job"})
    # only jobs dedupe on idem_key
    assert not d._replay_safe("put", {"kind": "memory", "idem_key": "k"})


def test_paid_or_external_reads_are_not_replayed() -> None:
    for kind in ("perplexity-research", "web", "websearch", "llm"):
        assert not d._replay_safe("get", {"kind": kind})
    assert d._replay_safe("search", {"kind": "paper"})
    assert d._replay_safe("search", {})


def test_non_transient_error_is_not_retried(runtime: PrecisRuntime) -> None:
    calls = _flaky(runtime, "get", 99, KeyError("zz"))
    out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    assert calls["n"] == 1 and "[error:Internal]" in out
