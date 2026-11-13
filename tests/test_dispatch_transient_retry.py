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


def test_non_idempotent_put_is_not_retried(
    runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def boom(verb: str, args: dict) -> None:
        calls["n"] += 1
        raise pge.LockNotAvailable("lock")

    monkeypatch.setattr(runtime, "_dispatch_inner", boom)
    out = runtime.dispatch("put", {"kind": "memory", "text": "x"})
    assert calls["n"] == 1
    assert "[error:Upstream]" in out and "retried" not in out


@pytest.mark.parametrize("verb", ["edit", "delete", "tag", "link"])
def test_other_writes_are_not_retried(runtime: PrecisRuntime, verb: str) -> None:
    assert not d._replay_safe(verb, {"idem_key": "k"})


def test_put_with_idem_key_carries_the_key() -> None:
    # _replay_safe answers only "does the call carry a dedupe key"; the
    # handler-side check (_put_accepts_idem_key) decides whether the key
    # is actually consumed — both layers are tested below.
    assert d._replay_safe("put", {"kind": "job", "idem_key": "job:1"})
    assert d._replay_safe("put", {"kind": "memory", "idem_key": "k"})
    assert not d._replay_safe("put", {"kind": "job", "idem_key": None})
    assert not d._replay_safe("put", {"kind": "job"})
    assert not d._replay_safe("put", {"kind": "memory"})
    assert not d._replay_safe("put", {"kind": "memory", "idem_key": ""})


def test_put_idem_gate_needs_a_handler_that_consumes_the_key(
    runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr658432: idem_key is a dedupe only where put declares it."""

    class _IdemPut:
        def put(self, *, idem_key: str | None = None, **_kw: object) -> None:
            pass

    class _SwallowingPut:
        def put(self, **_kw: object) -> None:
            pass

    handlers = {"memish": _IdemPut(), "swallow": _SwallowingPut()}
    monkeypatch.setattr(runtime.hub, "handler_for", handlers.get)
    assert runtime._put_accepts_idem_key("memish")
    assert not runtime._put_accepts_idem_key("swallow")
    assert not runtime._put_accepts_idem_key("ghost")
    assert not runtime._put_accepts_idem_key(None)


def test_idem_key_declared_on_ref_puts() -> None:
    """The kinds that dedupe a retried create on meta.idem_key."""
    from precis.handlers.gripe import GripeHandler
    from precis.handlers.job import JobHandler
    from precis.handlers.memory import MemoryHandler
    from precis.handlers.quest import QuestHandler
    from precis.handlers.taxon import TaxonHandler
    from precis.handlers.todo import TodoHandler

    for cls in (
        TodoHandler,
        GripeHandler,
        MemoryHandler,
        QuestHandler,
        TaxonHandler,
        JobHandler,
    ):
        assert "idem_key" in d._handler_accepted_kwargs(cls, "put"), cls.__name__

    # message's put is self-contained (its pg_notify side effect must
    # never be replayed); its **_kw swallows idem_key unread.
    from precis.handlers.message import MessageHandler

    assert "idem_key" not in d._handler_accepted_kwargs(MessageHandler, "put")


def test_idem_keyed_put_retries_on_transient(
    runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _IdemPut:
        def put(self, *, idem_key: str | None = None, **_kw: object) -> None:
            pass

    monkeypatch.setattr(runtime.hub, "handler_for", lambda kind: _IdemPut())
    calls = {"n": 0}

    def flaky(verb: str, args: dict) -> Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise pge.LockNotAvailable("lock")
        return Response(body="ok-after-retry")

    monkeypatch.setattr(runtime, "_dispatch_inner", flaky)
    out = runtime.dispatch("put", {"kind": "memish", "idem_key": "k1"})
    assert "ok-after-retry" in out
    assert calls["n"] == 2


def test_keyed_put_on_non_idem_kind_is_not_retried(
    runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kind whose put swallows idem_key unread stays replay-unsafe."""

    class _SwallowingPut:
        def put(self, **_kw: object) -> None:
            pass

    monkeypatch.setattr(runtime.hub, "handler_for", lambda kind: _SwallowingPut())
    calls = {"n": 0}

    def boom(verb: str, args: dict) -> None:
        calls["n"] += 1
        raise pge.LockNotAvailable("lock")

    monkeypatch.setattr(runtime, "_dispatch_inner", boom)
    out = runtime.dispatch("put", {"kind": "swallow", "idem_key": "k1"})
    assert calls["n"] == 1
    assert "[error:Upstream]" in out and "retried" not in out


def test_paid_or_external_reads_are_not_replayed() -> None:
    for kind in ("perplexity-research", "web", "websearch", "llm"):
        assert not d._replay_safe("get", {"kind": kind})
    assert d._replay_safe("search", {"kind": "paper"})
    assert d._replay_safe("search", {})


def test_non_transient_error_is_not_retried(runtime: PrecisRuntime) -> None:
    calls = _flaky(runtime, "get", 99, KeyError("zz"))
    out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    assert calls["n"] == 1 and "[error:Internal]" in out
