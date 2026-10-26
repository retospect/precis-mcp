"""gr518349 — the dedicated scheduler thread (``start_scheduler_thread``).

The in-rotation ``scheduler`` ref-pass rides ``workers/runner.py``'s
strictly-serial rotation, so a single long pass (observed: hub_refine
holding the rotation ~10 min on the agent host) starves every cadence whose only
lit/eligible host is that worker (gripe_triage, deep_review, cron_tick).
The thread claims leases on its own tick, on its OWN ``Store.connect``
(never the main loop's store — psycopg connections aren't usable
concurrently across threads); lease claims are atomic conditional advances,
so the in-rotation backstop pass racing this thread is a harmless
``claimed=0`` for the loser, never a double fire — the same contract the
heartbeat thread already runs under (``tests/workers/test_heartbeat_thread.py``).

These tests exercise the REAL lease machinery on the ``store`` fixture:
``Store.connect`` is monkeypatched to hand back a proxy over it (``close()``
no-op'd — the shared fixture must survive the thread's error/exit closes).
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from precis.workers import scheduler as sched
from precis.workers.scheduler import Cadence, start_scheduler_thread

# Generous but bounded — these tests poll a background thread; a real
# failure should time out rather than hang the suite forever.
_POLL_DEADLINE_S = 10.0
_POLL_STEP_S = 0.02


class _StoreProxy:
    """Delegate everything to the real ``store`` fixture except ``close()``,
    which the thread calls on error and on exit — the shared fixture must
    survive for the test's own assertions and for the rest of the suite."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        self.closed = True


def _cad(run) -> Cadence:
    return Cadence(name=f"c-{uuid4().hex}", interval_s=60, run=run)


def _poll_until(predicate, *, deadline_s: float = _POLL_DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(_POLL_STEP_S)
    return predicate()


@pytest.fixture
def fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the thread's inter-attempt sleep so tests run in well under
    a second per attempt instead of the production ~5s cadence."""
    monkeypatch.setattr(sched, "_SCHEDULER_TICK_SECONDS", 0.02)


@pytest.fixture
def connect_proxy(store, monkeypatch: pytest.MonkeyPatch) -> list[_StoreProxy]:
    """``Store.connect`` → a fresh proxy over the real ``store`` fixture,
    recording every proxy so tests can count connects and inspect closes."""
    from precis.store import Store as StoreClass

    proxies: list[_StoreProxy] = []

    def fake_connect(dsn: str, **kw: object) -> _StoreProxy:
        proxy = _StoreProxy(store)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(StoreClass, "connect", fake_connect)
    return proxies


def test_thread_claims_while_the_rotation_is_blocked(
    store, fast_tick, connect_proxy
) -> None:
    """The gripe's required test: a rotation step holding the main thread
    for N seconds does not delay a due cadence's claim — the claim lands on
    the scheduler thread's own tick while the "rotation" is still busy."""
    fired_at: list[float] = []
    cad = _cad(lambda s, b: fired_at.append(time.monotonic()))

    stop = {"flag": False}
    thread = start_scheduler_thread(
        "fake-dsn", host="h", cadences=(cad,), should_stop=lambda: stop["flag"]
    )
    try:
        # Simulate a wedged rotation step holding the main thread ~0.6s.
        block_end = time.monotonic() + 0.6
        while time.monotonic() < block_end:
            time.sleep(0.05)
        assert fired_at, "cadence never fired while the rotation was blocked"
        assert fired_at[0] < block_end
        lease = {ln.name: ln for ln in store.scheduler_leases()}[cad.name]
        assert lease.last_fired_at is not None
        assert lease.last_host == "h"
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()


def test_gate_false_skips_the_claim(store, fast_tick, connect_proxy) -> None:
    """``gate("scheduler")`` False → the tick does nothing (no seed, no
    claim) — the same dimming the in-rotation pass gets from
    ``service_config``, so an operator's ``prio 0`` row dims the thread
    within one TTL too."""
    fired: list[str] = []
    cad = _cad(lambda s, b: fired.append("fired"))

    stop = {"flag": False}
    thread = start_scheduler_thread(
        "fake-dsn",
        host="h",
        cadences=(cad,),
        gate=lambda service: False,
        should_stop=lambda: stop["flag"],
    )
    try:
        time.sleep(0.25)
        assert fired == []
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    names = {ln.name for ln in store.scheduler_leases()}
    assert cad.name not in names


def test_connect_failure_retries_next_tick(store, monkeypatch, fast_tick) -> None:
    """A connect that raises must not kill the thread — it retries on the
    next tick (belt-and-suspenders: the scheduler must never take the
    worker down with it)."""
    from precis.store import Store as StoreClass

    fired: list[str] = []
    cad = _cad(lambda s, b: fired.append("fired"))
    attempts = {"n": 0}

    def flaky_connect(dsn: str, **kw: object) -> _StoreProxy:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated connect failure")
        return _StoreProxy(store)

    monkeypatch.setattr(StoreClass, "connect", flaky_connect)

    stop = {"flag": False}
    thread = start_scheduler_thread(
        "fake-dsn", host="h", cadences=(cad,), should_stop=lambda: stop["flag"]
    )
    try:
        assert _poll_until(lambda: len(fired) >= 1), (
            "thread should recover and fire after the first failed connect"
        )
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    assert attempts["n"] >= 2


def test_pass_raise_drops_the_store_and_reconnects(
    store, monkeypatch, fast_tick, connect_proxy
) -> None:
    """A raise out of ``run_scheduler_pass`` must not kill the thread —
    it logs, closes/drops the (suspect) store and reconnects on the next
    tick."""
    calls = {"n": 0}

    def flaky_pass(s, *, host, batch_size, cadences, **kw) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated pass failure")

    monkeypatch.setattr(sched, "run_scheduler_pass", flaky_pass)

    stop = {"flag": False}
    thread = start_scheduler_thread(
        "fake-dsn", host="h", cadences=(), should_stop=lambda: stop["flag"]
    )
    try:
        assert _poll_until(lambda: calls["n"] >= 2), (
            "expected the thread to retry the pass on the next tick"
        )
        assert _poll_until(lambda: len(connect_proxy) >= 2), (
            "expected a reconnect (2nd Store.connect) after the failed tick"
        )
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    # The first store was closed best-effort before being dropped.
    assert connect_proxy[0].closed


def test_default_should_stop_is_a_never_stop_callable(monkeypatch, fast_tick) -> None:
    """``should_stop=None`` must not mean "stop immediately" — it defaults
    to an always-``False`` callable, and the caller (``cli/worker.py``)
    relies on that to keep the thread alive when it DOES pass one.

    Verified by stubbing the loop body itself (rather than running the real
    loop with no way to stop it, which would leak an uncontrollable daemon
    thread across the rest of the suite) and inspecting what ``should_stop``
    it was handed — mirrors the heartbeat thread's equivalent test.
    """
    received: dict[str, object] = {}

    def _fake_body(dsn, host, batch_size, gate, cadences, should_stop) -> None:
        received["should_stop"] = should_stop

    monkeypatch.setattr(sched, "_scheduler_thread_body", _fake_body)

    thread = start_scheduler_thread("fake-dsn", host="h")
    thread.join(timeout=_POLL_DEADLINE_S)

    assert not thread.is_alive()
    assert callable(received["should_stop"])
    assert received["should_stop"]() is False
