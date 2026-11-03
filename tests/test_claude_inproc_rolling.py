"""claude_inproc — the rolling pool (gr551639).

The pass used to claim a batch, join the whole pool, and only then
claim again: one long job idled every free slot, and claimed-but-
waiting jobs sat ``STATUS:running`` with their 90-min lease ticking
before they ever started. Now a job is claimed only when a pool slot
is free to start it — ``running`` and the lease begin at actual
start, and each completion triggers the next claim until the queue
is empty, the drain flag is set, or the pass budget
(``PRECIS_INPROC_PASS_BUDGET_S``) is spent.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc
from precis.workers.job_types import JobTypeSpec

pytestmark = pytest.mark.db


def _spec(*, dispatch: Any, name: str = "fake_tick") -> JobTypeSpec:
    def _run(*_a: Any, **_k: Any) -> str:
        return "noop"

    return JobTypeSpec(
        name=name,
        params_schema={"type": "object"},
        compatible_executors=frozenset({"claude_inproc"}),
        requires=frozenset(),
        description="fake job_type for tests",
        run=_run,
        dispatch=dispatch,
    )


def _mk_job(store: Store, title: str) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=title,
        meta={"executor": "claude_inproc", "job_type": "fake_tick"},
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def test_rolling_pool_drains_free_slots(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One long job parked on an event + eight short jobs at concurrency
    4 / limit 4: every short job finishes while the long job still
    blocks — the freed slots keep refilling instead of waiting out the
    long one (gr551639).

    No sleeps: the long job gates on ``long_gate``, the test blocks on
    ``shorts_done``, so the result is timing-independent."""
    long_gate = threading.Event()
    long_job = _mk_job(store, "long job")
    short_ids = [_mk_job(store, f"short job {i}") for i in range(8)]

    lock = threading.Lock()
    started: set[int] = set()
    marked_running: set[int] = set()
    running_now = {"n": 0, "max": 0}
    marked_ahead = {"max": 0}
    shorts_done = threading.Event()
    short_done = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        with lock:
            started.add(ctx.ref_id)
            running_now["n"] += 1
            running_now["max"] = max(running_now["max"], running_now["n"])
            marked_ahead["max"] = max(
                marked_ahead["max"], len(marked_running) - len(started)
            )
        try:
            if ctx.ref_id == long_job:
                long_gate.wait()
            else:
                with lock:
                    short_done["n"] += 1
                    if short_done["n"] == len(short_ids):
                        shorts_done.set()
        finally:
            with lock:
                running_now["n"] -= 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    real_set_status = claude_inproc._set_status

    def _spy(store_: Any, ref_id: int, value: str, **kw: Any) -> None:
        if value == "running":
            with lock:
                marked_running.add(ref_id)
                marked_ahead["max"] = max(
                    marked_ahead["max"], len(marked_running) - len(started)
                )
        real_set_status(store_, ref_id, value, **kw)

    monkeypatch.setattr(claude_inproc, "_set_status", _spy)
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "4")
    monkeypatch.delenv("PRECIS_INPROC_PASS_BUDGET_S", raising=False)

    # The pass runs on its own thread: it blocks in the pool join on the
    # gated long job while the test verifies the shorts already drained.
    res_holder: dict[str, Any] = {}
    pass_thread = threading.Thread(
        target=lambda: res_holder.setdefault(
            "res", claude_inproc.run_claude_inproc_pass(store, limit=4)
        ),
        daemon=True,
    )
    pass_thread.start()
    try:
        assert shorts_done.wait(timeout=30), (
            "short jobs did not drain behind the blocked long job — "
            "the pass stopped refilling free slots (gr551639)"
        )
    finally:
        long_gate.set()
    pass_thread.join(timeout=30)
    assert not pass_thread.is_alive()

    assert res_holder["res"] == {"claimed": 9, "ok": 9, "failed": 0}
    # At most PRECIS_INPROC_CONCURRENCY jobs run at once.
    assert running_now["max"] <= 4
    # No job was stamped running before a pool slot was free to take
    # it: the marked-but-not-started gap never exceeds the pool width
    # (a capped/over-claiming pass stamps more rows than it can seat).
    assert marked_ahead["max"] <= 4
    for rid in (long_job, *short_ids):
        assert _status(store, rid) == "succeeded"


def test_pass_budget_claims_first_fill_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_INPROC_PASS_BUDGET_S=0`` bounds the pass to its first
    fill: one pool's worth is claimed, run and joined; the queued
    remainder waits for the next pass.

    ``limit=6`` > pool width 4 deliberately: a pass that treats
    ``limit`` as a total claim cap (1e7a05c78) claims 6 — two rows
    stamped ``running`` with no slot to start them."""
    gate = threading.Event()
    ids = [_mk_job(store, f"job {i}") for i in range(6)]

    lock = threading.Lock()
    started: list[int] = []
    four_started = threading.Event()

    def _dispatch(ctx: Any, _s: Any) -> None:
        with lock:
            started.append(ctx.ref_id)
            if len(started) == 4:
                four_started.set()
        gate.wait()
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "4")
    monkeypatch.setenv("PRECIS_INPROC_PASS_BUDGET_S", "0")

    res_holder: dict[str, Any] = {}
    pass_thread = threading.Thread(
        target=lambda: res_holder.setdefault(
            "res", claude_inproc.run_claude_inproc_pass(store, limit=6)
        ),
        daemon=True,
    )
    pass_thread.start()
    try:
        assert four_started.wait(timeout=30)
    finally:
        gate.set()
    pass_thread.join(timeout=30)
    assert not pass_thread.is_alive()

    assert res_holder["res"] == {"claimed": 4, "ok": 4, "failed": 0}
    assert set(started) == set(ids[:4])
    for rid in ids[:4]:
        assert _status(store, rid) == "succeeded"
    # The remainder is untouched for the next pass — still queued, never
    # stamped running.
    for rid in ids[4:]:
        assert _status(store, rid) == "queued"


def test_no_jobs_returns_zeros(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_inproc,
        "get_job_type",
        lambda name: _spec(dispatch=lambda c, s: c.set_status("succeeded")),
    )
    assert claude_inproc.run_claude_inproc_pass(store, limit=2) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }


def test_poison_guard_still_trips(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt cap still fails a crash-looping job before it runs."""
    from precis.workers.executors._common import MAX_ATTEMPTS

    dispatched = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        dispatched["n"] += 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_job(store, "poisoned job")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || "
            "jsonb_build_object('attempts', %s) WHERE ref_id = %s",
            (MAX_ATTEMPTS, rid),
        )
        conn.commit()

    res = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert res == {"claimed": 1, "ok": 0, "failed": 1}
    assert dispatched["n"] == 0
    assert _status(store, rid) == "failed"


def test_repoll_claims_late_queued_job(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job queued AFTER the queue ran dry is still claimed mid-pass.

    The long job alone drains the queue, so the pass parks its in-flight
    wait behind the empty-queue latch. A job inserted then must be
    claimed on a ``_RECLAIM_POLL_S`` tick — while the long job still
    blocks — not after it finishes; a latched ``queue_empty`` plus a
    wait with no timeout recreates the gr551639 idle behind one long
    job (here: doctor_tick-length).

    ``_RECLAIM_POLL_S`` is patched short (production 10 s) so the tick
    lands in milliseconds; the events do all synchronizing, no sleeps.
    """
    long_gate = threading.Event()
    long_started = threading.Event()
    late_done = threading.Event()
    long_job = _mk_job(store, "long job")

    def _dispatch(ctx: Any, _s: Any) -> None:
        if ctx.ref_id == long_job:
            long_started.set()
            long_gate.wait()
        else:
            late_done.set()
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "4")
    monkeypatch.delenv("PRECIS_INPROC_PASS_BUDGET_S", raising=False)
    monkeypatch.setattr(claude_inproc, "_RECLAIM_POLL_S", 0.05)

    res_holder: dict[str, Any] = {}
    pass_thread = threading.Thread(
        target=lambda: res_holder.setdefault(
            "res", claude_inproc.run_claude_inproc_pass(store, limit=4)
        ),
        daemon=True,
    )
    pass_thread.start()
    late_id = -1
    try:
        assert long_started.wait(timeout=30)
        # The queue is dry behind the parked long job — a row landing
        # now is only reachable through a re-poll wake.
        late_id = _mk_job(store, "late job")
        assert late_done.wait(timeout=30), (
            "late-queued job was never claimed while the long job "
            "blocked — queue_empty stayed latched and the wait never "
            "re-polled (gr551639)"
        )
    finally:
        long_gate.set()
    pass_thread.join(timeout=30)
    assert not pass_thread.is_alive()

    assert res_holder["res"] == {"claimed": 2, "ok": 2, "failed": 0}
    assert _status(store, late_id) == "succeeded"
    assert _status(store, long_job) == "succeeded"


def test_budget_expiry_mid_run_stops_refill(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positive budget spent mid-run latches stop the same as ``0``:
    the first fill claims a pool's worth, the clock runs past the
    deadline while the jobs park, and the freed slots claim nothing
    more — the pass joins what it has and returns.

    ``time.monotonic`` is patched so the deadline is driven, never
    slept on; ``threading``/``concurrent.futures`` timeouts keep real
    time (``_time`` binds at import)."""
    gate = threading.Event()
    ids = [_mk_job(store, f"job {i}") for i in range(6)]

    lock = threading.Lock()
    started: list[int] = []
    four_started = threading.Event()

    def _dispatch(ctx: Any, _s: Any) -> None:
        with lock:
            started.append(ctx.ref_id)
            if len(started) == 4:
                four_started.set()
        gate.wait()
        ctx.set_status("succeeded")

    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "4")
    monkeypatch.setenv("PRECIS_INPROC_PASS_BUDGET_S", "100")

    res_holder: dict[str, Any] = {}
    pass_thread = threading.Thread(
        target=lambda: res_holder.setdefault(
            "res", claude_inproc.run_claude_inproc_pass(store, limit=4)
        ),
        daemon=True,
    )
    pass_thread.start()
    try:
        assert four_started.wait(timeout=30)
        clock["t"] = 1_000.0  # past deadline = t0 + 100
    finally:
        gate.set()
    pass_thread.join(timeout=30)
    assert not pass_thread.is_alive()

    assert res_holder["res"] == {"claimed": 4, "ok": 4, "failed": 0}
    assert set(started) == set(ids[:4])
    for rid in ids[:4]:
        assert _status(store, rid) == "succeeded"
    for rid in ids[4:]:
        assert _status(store, rid) == "queued"


def test_drain_requested_stops_refill(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``drain_requested()`` (SIGTERM/SIGINT) latches the same stop: the
    pass claims the first fill, the flag flips while the jobs park, and
    the freed slots claim nothing more — in-flight work joins and the
    queued remainder waits for a healthier process."""
    gate = threading.Event()
    ids = [_mk_job(store, f"job {i}") for i in range(6)]

    lock = threading.Lock()
    started: list[int] = []
    four_started = threading.Event()

    def _dispatch(ctx: Any, _s: Any) -> None:
        with lock:
            started.append(ctx.ref_id)
            if len(started) == 4:
                four_started.set()
        gate.wait()
        ctx.set_status("succeeded")

    draining = {"on": False}
    monkeypatch.setattr(claude_inproc, "drain_requested", lambda: draining["on"])
    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "4")
    monkeypatch.delenv("PRECIS_INPROC_PASS_BUDGET_S", raising=False)

    res_holder: dict[str, Any] = {}
    pass_thread = threading.Thread(
        target=lambda: res_holder.setdefault(
            "res", claude_inproc.run_claude_inproc_pass(store, limit=4)
        ),
        daemon=True,
    )
    pass_thread.start()
    try:
        assert four_started.wait(timeout=30)
        draining["on"] = True
    finally:
        gate.set()
    pass_thread.join(timeout=30)
    assert not pass_thread.is_alive()

    assert res_holder["res"] == {"claimed": 4, "ok": 4, "failed": 0}
    for rid in ids[:4]:
        assert _status(store, rid) == "succeeded"
    for rid in ids[4:]:
        assert _status(store, rid) == "queued"
