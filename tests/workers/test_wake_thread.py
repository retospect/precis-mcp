"""gr626353 — the dedicated wake thread (``start_wake_thread``).

``wake_runner`` used to run inside the serial ``run_loop`` rotation, which
only reaches it BETWEEN passes. The in-line job lane's ``embed_batch`` /
``derived_drain`` batches hold the rotation for up to ~10 min, so ``at_time``
wakes (``ci_verdict_poll`` on a release pass, quest ticks, lane parks) fired
5-19 min late. The thread runs ``run_wake_pass`` on its own ``Store`` every
``PRECIS_WAKE_INTERVAL_S``.

Real DB (``store`` fixture). ``Store.connect`` is monkeypatched to hand back a
proxy over the fixture store (``close()`` recorded, not forwarded) — the
convention of ``tests/test_scheduler_thread.py``; the shared pool is
thread-safe, which is all the thread needs of "its own" store here.
"""

from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from precis.store import Store, Tag
from precis.workers import wake_runner
from precis.workers.executors._common import set_meta
from precis.workers.runner import BatchResult, run_loop
from precis.workers.wake_runner import start_wake_thread

_POLL_DEADLINE_S = 10.0
_POLL_STEP_S = 0.02
#: The wake interval these tests run the thread at.
_INTERVAL_S = 0.1


class _StoreProxy:
    """Delegate to the ``store`` fixture; ``close()`` is recorded only."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def connect_proxy(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[_StoreProxy]:
    proxies: list[_StoreProxy] = []

    def fake_connect(dsn: str, **kw: object) -> _StoreProxy:
        proxy = _StoreProxy(store)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(Store, "connect", fake_connect)
    monkeypatch.setenv(wake_runner.WAKE_INTERVAL_ENV, str(_INTERVAL_S))
    return proxies


def _status_of(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        s = str(t)
        if s.startswith("STATUS:"):
            return s[len("STATUS:") :]
    return None


def _park_due(store: Store) -> int:
    """A job parked ``waiting_time`` whose ``at_time`` instant has passed."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="ci_verdict_poll",
        meta={"job_type": "ci_verdict_poll", "executor": "coordinator"},
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "waiting_time"),
        set_by="agent",
        replace_prefix=True,
    )
    with store.pool.connection() as conn:
        set_meta(
            conn,
            ref.id,
            wake_when={"kind": "at_time", "payload": {"ts": int(time.time()) - 5}},
        )
        conn.commit()
    return int(ref.id)


def _requeue_events(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_event'"
                " AND text LIKE 'wake_runner: re-queued%%'",
                (ref_id,),
            ).fetchall()
        ]


def _poll_until(predicate: Any, *, deadline_s: float = _POLL_DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(_POLL_STEP_S)
    return bool(predicate())


def test_wake_lands_within_interval_while_job_inproc_pass_blocks(
    store: Store, connect_proxy: list[_StoreProxy]
) -> None:
    """(a) The gripe's regression: a ``job_inproc`` pass holds ``run_loop``
    for 2 s; a past-due ``at_time`` row is re-queued within about one wake
    interval, long before the blocking pass returns."""
    block_s = 2.0
    block_started = threading.Event()
    block_finished = threading.Event()

    def _job_inproc_pass(batch_size: int) -> BatchResult:
        block_started.set()
        time.sleep(block_s)
        block_finished.set()
        return BatchResult(handler="job_inproc", claimed=1, ok=1, failed=0)

    job = _park_due(store)
    stop = {"flag": False}
    rotation = threading.Thread(
        target=lambda: run_loop(
            [],
            store=cast(Store, None),  # no chunk handlers: never touched
            ref_passes=[_job_inproc_pass],
            once=True,
        ),
        daemon=True,
    )
    rotation.start()
    assert block_started.wait(_POLL_DEADLINE_S), "blocking pass never started"
    t0 = time.monotonic()
    thread = start_wake_thread("fake-dsn", should_stop=lambda: stop["flag"])
    try:
        woke = _poll_until(lambda: _status_of(store, job) == "queued")
        waited = time.monotonic() - t0
        still_blocked = not block_finished.is_set()
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
        rotation.join(timeout=_POLL_DEADLINE_S)
    assert woke, "past-due at_time row never re-queued"
    assert still_blocked, "wake only landed after the blocking pass returned"
    # One interval plus the pass itself and generous scheduling slack — an
    # order of magnitude under the block, three under the 5-19 min bug.
    assert waited < 1.0, f"wake took {waited:.2f}s with a {_INTERVAL_S}s interval"
    assert len(_requeue_events(store, job)) == 1
    assert not thread.is_alive()


def _race_two_passes(store: Store) -> list[dict[str, int]]:
    """Run two wake passes so BOTH hold the same ready row before either
    writes it. The selectors' ``SKIP LOCKED`` only excludes a peer that
    selects while the first is still inside its selection transaction; the
    lock is released at that commit, before the re-queue write. So pass A is
    parked at its re-queue (selection already committed), pass B is then
    started and selects the still-``waiting_time`` row, and only then are
    both released."""
    real_requeue = wake_runner._requeue
    arrived: list[int] = []
    guard = threading.Lock()
    a_parked = threading.Event()
    b_parked = threading.Event()
    go = threading.Event()

    def _gated_requeue(*a: Any, **kw: Any) -> bool:
        with guard:
            arrived.append(1)
            (a_parked if len(arrived) == 1 else b_parked).set()
        assert go.wait(_POLL_DEADLINE_S)
        return real_requeue(*a, **kw)

    results: list[dict[str, int]] = []
    errors: list[BaseException] = []

    def _one() -> None:
        try:
            results.append(wake_runner.run_wake_pass(store))
        except BaseException as exc:
            errors.append(exc)

    wake_runner._requeue = _gated_requeue
    try:
        first = threading.Thread(target=_one)
        first.start()
        assert a_parked.wait(_POLL_DEADLINE_S), "pass A never reached its re-queue"
        second = threading.Thread(target=_one)
        second.start()
        assert b_parked.wait(_POLL_DEADLINE_S), "pass B never selected the row"
        go.set()
        first.join(timeout=_POLL_DEADLINE_S)
        second.join(timeout=_POLL_DEADLINE_S)
    finally:
        go.set()
        wake_runner._requeue = real_requeue
    assert not errors, errors
    assert len(results) == 2
    return results


def test_concurrent_wake_passes_requeue_a_row_once(store: Store) -> None:
    """(b) Two passes that both selected the same due row: exactly one
    re-queues it (one audit chunk, one ``ok``); the other backs off instead
    of flipping the job — which an executor may have claimed by now — back
    to ``queued``."""
    job = _park_due(store)
    results = _race_two_passes(store)
    assert sum(r["ok"] for r in results) == 1, results
    assert sum(r["claimed"] for r in results) == 1, results
    assert sum(r["failed"] for r in results) == 0, results
    assert _status_of(store, job) == "queued"
    assert len(_requeue_events(store, job)) == 1


def test_race_without_the_status_recheck_double_requeues(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for (b): with the under-lock STATUS re-check disabled the
    same race writes the audit chunk twice — the barrier really does put
    both passes past the selectors, so (b) is testing the guard."""
    monkeypatch.setattr(wake_runner, "_still_in", lambda conn, ref_id, expect: True)
    job = _park_due(store)
    _race_two_passes(store)
    assert len(_requeue_events(store, job)) == 2


def test_gate_off_wakes_nothing_then_gate_on_wakes(
    store: Store, connect_proxy: list[_StoreProxy]
) -> None:
    """(c) ``gate("wake_runner")`` False -> the thread idles: no wake, no
    connection; flipping the gate on (a live ``service_config`` change) wakes
    the same row on a later tick."""
    gate_on = {"on": False}
    asked: list[str] = []

    def gate(service: str) -> bool:
        asked.append(service)
        return gate_on["on"]

    job = _park_due(store)
    stop = {"flag": False}
    thread = start_wake_thread("fake-dsn", gate=gate, should_stop=lambda: stop["flag"])
    try:
        time.sleep(10 * _INTERVAL_S)
        assert _status_of(store, job) == "waiting_time"
        assert connect_proxy == [], "gated-off thread must not open a store"
        assert set(asked) == {"wake_runner"}
        gate_on["on"] = True
        assert _poll_until(lambda: _status_of(store, job) == "queued")
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()


def test_shutdown_stops_the_thread_and_closes_its_store(
    store: Store, connect_proxy: list[_StoreProxy]
) -> None:
    """(d) ``should_stop`` -> the loop exits promptly and closes its store."""
    stop = {"flag": False}
    thread = start_wake_thread("fake-dsn", should_stop=lambda: stop["flag"])
    assert _poll_until(lambda: len(connect_proxy) >= 1), "thread never connected"
    assert thread.is_alive()
    t0 = time.monotonic()
    stop["flag"] = True
    thread.join(timeout=_POLL_DEADLINE_S)
    assert not thread.is_alive()
    assert time.monotonic() - t0 < 2.0
    assert connect_proxy[0].closed


def test_pass_raise_reconnects_and_keeps_waking(
    store: Store, connect_proxy: list[_StoreProxy], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise out of the pass (dead connection) is logged, the store is
    dropped and reopened, and the next tick still wakes the row."""
    real = wake_runner.run_wake_pass
    calls = {"n": 0}

    def flaky(s: Any, *, limit: int = 16) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated connection loss")
        return real(s, limit=limit)

    monkeypatch.setattr(wake_runner, "run_wake_pass", flaky)
    job = _park_due(store)
    stop = {"flag": False}
    thread = start_wake_thread("fake-dsn", should_stop=lambda: stop["flag"])
    try:
        assert _poll_until(lambda: _status_of(store, job) == "queued")
        assert len(connect_proxy) >= 2, "expected a reconnect after the raise"
    finally:
        stop["flag"] = True
        thread.join(timeout=_POLL_DEADLINE_S)
    assert connect_proxy[0].closed
    assert not thread.is_alive()
