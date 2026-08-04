"""gr191264 — the dedicated heartbeat thread (``start_heartbeat_thread``).

The in-rotation ``heartbeat`` ref-pass (``tests/test_heartbeat_pass.py``)
rides ``workers/runner.py``'s strictly-serial rotation, so a single long
pass starves the ``host_heartbeat`` row toward nursery's host-dark
threshold. The thread beats independently of the rotation, on its OWN
``Store.connect`` (never the main loop's store — psycopg connections aren't
usable concurrently across threads), sharing the module-global throttle in
``run_heartbeat_pass`` so a double-fire against the in-rotation pass is a
harmless no-op.

These tests never touch a real DB: they monkeypatch ``Store.connect`` to
hand back a tiny in-process fake, so they exercise only the thread's own
loop/reconnect/throttle-sharing logic.
"""

from __future__ import annotations

import time

import pytest

from precis.store import Store as StoreClass
from precis.workers import heartbeat as hb

# Generous but bounded — these tests poll a background thread; a real
# failure should time out rather than hang the suite forever.
_POLL_DEADLINE_S = 5.0
_POLL_STEP_S = 0.02


def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_last_beat_monotonic", None)


class _FakeStore:
    """A minimal stand-in for ``Store`` — only ``record_heartbeat`` is
    exercised meaningfully; everything ``_report_resource_slots`` touches
    (``sync_host_resource_slots`` etc.) is absent on purpose, which is fine:
    that helper swallows any exception (including ``AttributeError``) and
    degrades to ``"n/a"`` — heartbeat's liveness signal never depends on it.
    """

    def __init__(self, *, raise_on_record: bool = False) -> None:
        self.record_calls: list[str] = []
        self.closed = False
        self._raise_on_record = raise_on_record

    def record_heartbeat(self, host: str, **kwargs: object) -> None:
        if self._raise_on_record:
            raise RuntimeError("simulated bad connection")
        self.record_calls.append(host)

    def close(self) -> None:
        self.closed = True


def _poll_until(predicate, *, deadline_s: float = _POLL_DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(_POLL_STEP_S)
    return predicate()


@pytest.fixture
def fast_tick(monkeypatch: pytest.MonkeyPatch):
    """Shrink the thread's inter-attempt sleep so tests run in well under
    a second per attempt instead of the production ~5s cadence."""
    monkeypatch.setattr(hb, "_THREAD_TICK_SECONDS", 0.02)


def test_thread_beats_without_the_rotation(monkeypatch, fast_tick) -> None:
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "0.05")

    store = _FakeStore()
    monkeypatch.setattr(StoreClass, "connect", lambda dsn, **kw: store)

    stop = {"flag": False}
    thread = hb.start_heartbeat_thread(
        "fake-dsn", host="thread-host", should_stop=lambda: stop["flag"]
    )
    try:
        assert _poll_until(lambda: len(store.record_calls) >= 2), (
            f"expected >=2 beats, got {store.record_calls}"
        )
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    assert store.closed


def test_thread_reconnects_after_pass_reports_failure(monkeypatch, fast_tick) -> None:
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "0.05")

    bad_store = _FakeStore(raise_on_record=True)
    good_store = _FakeStore()
    stores = [bad_store, good_store]
    connect_calls: list[_FakeStore] = []

    def fake_connect(dsn: str, **kw: object) -> _FakeStore:
        s = stores.pop(0)
        connect_calls.append(s)
        return s

    monkeypatch.setattr(StoreClass, "connect", fake_connect)

    stop = {"flag": False}
    thread = hb.start_heartbeat_thread("fake-dsn", should_stop=lambda: stop["flag"])
    try:
        assert _poll_until(lambda: len(connect_calls) >= 2), (
            "expected a reconnect (2nd Store.connect) after the failed beat"
        )
        assert _poll_until(lambda: len(good_store.record_calls) >= 1), (
            "the new store after reconnect should go on to beat successfully"
        )
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    # The bad store was closed best-effort before being dropped.
    assert bad_store.closed


def test_thread_beat_shares_throttle_with_the_rotation_pass(
    monkeypatch, fast_tick
) -> None:
    """After the thread lands a beat, an immediate in-rotation
    ``run_heartbeat_pass`` call must no-op (``claimed=0``) — the module-
    global throttle is shared, so the two triggers never double-write."""
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "60")

    thread_store = _FakeStore()
    monkeypatch.setattr(StoreClass, "connect", lambda dsn, **kw: thread_store)

    stop = {"flag": False}
    thread = hb.start_heartbeat_thread("fake-dsn", should_stop=lambda: stop["flag"])
    try:
        assert _poll_until(lambda: len(thread_store.record_calls) >= 1)
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)

    other_store = _FakeStore()
    result = hb.run_heartbeat_pass(other_store, host="rotation-host")
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert other_store.record_calls == []


def test_thread_survives_a_connect_failure_without_crashing(
    monkeypatch, fast_tick
) -> None:
    """A connect that raises must not kill the thread — it retries on the
    next tick (belt-and-suspenders: heartbeat must never take the worker
    down with it)."""
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "0.05")

    good_store = _FakeStore()
    attempts = {"n": 0}

    def flaky_connect(dsn: str, **kw: object) -> _FakeStore:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated connect failure")
        return good_store

    monkeypatch.setattr(StoreClass, "connect", flaky_connect)

    stop = {"flag": False}
    thread = hb.start_heartbeat_thread("fake-dsn", should_stop=lambda: stop["flag"])
    try:
        assert _poll_until(lambda: len(good_store.record_calls) >= 1), (
            "thread should recover and beat after the first failed connect"
        )
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()


def test_default_should_stop_is_a_never_stop_callable(monkeypatch, fast_tick) -> None:
    """``should_stop=None`` must not mean "stop immediately" — it defaults
    to an always-``False`` callable, and the caller (``cli/worker.py``)
    relies on that to keep the thread alive when it DOES pass one.

    Verified by stubbing the loop body itself (rather than running the real
    loop with no way to stop it, which would leak an uncontrollable daemon
    thread across the rest of the suite) and inspecting what ``should_stop``
    it was handed.
    """
    received: dict[str, object] = {}

    def _fake_body(dsn, host, should_stop):
        received["should_stop"] = should_stop

    monkeypatch.setattr(hb, "_heartbeat_thread_body", _fake_body)

    thread = hb.start_heartbeat_thread("fake-dsn")
    thread.join(timeout=_POLL_DEADLINE_S)

    assert not thread.is_alive()
    assert callable(received["should_stop"])
    assert received["should_stop"]() is False
