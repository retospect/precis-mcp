"""gr495190: cross-kind fan-out is parallel, capped and deadline-bounded."""

from __future__ import annotations

import threading
import time

import pytest

from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime


def _patch_invoke(monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime, fn) -> None:
    monkeypatch.setattr(
        type(rt), "_cross_kind_invoke_search_hits", lambda self, h, k, kw: fn(k)
    )


def test_fan_out_concurrency_never_exceeds_cap(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()
    assert len(kinds) > search_mod.CROSS_KIND_MAX_WORKERS
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fake(k: str) -> list:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return []

    _patch_invoke(monkeypatch, rt, fake)
    out = rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})
    assert set(out) == set(kinds)
    assert 1 < state["peak"] <= search_mod.CROSS_KIND_MAX_WORKERS


def test_fan_out_deadline_names_stragglers_and_keeps_fast_kinds(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 0.3)
    release = threading.Event()

    def fake(k: str) -> list:
        if k == kinds[0]:
            release.wait(5)
        return []

    _patch_invoke(monkeypatch, rt, fake)
    t0 = time.perf_counter()
    out = rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})
    elapsed = time.perf_counter() - t0
    release.set()
    assert elapsed < 2.0
    assert isinstance(out[kinds[0]], TimeoutError)
    assert out[kinds[1]] == [] and out[kinds[2]] == []


def test_deadline_cancels_the_query_on_the_server(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abandoning the future is not enough: after the deadline Postgres must
    have cancelled the kind's query (statement_timeout), not left it running."""
    from precis.store._chunks_ops import _prepare_filtered_ann

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    kinds = rt._cross_kind_kinds()[:2]
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 0.5)
    marker = "SELECT pg_sleep(20) /* fanout-cancel-test */"
    errors: list[Exception] = []

    def sleepy(k: str) -> list:
        if k != kinds[0]:
            return []
        with store.pool.connection() as conn:
            _prepare_filtered_ann(conn, 10)
            try:
                conn.execute(marker)
            except Exception as exc:
                errors.append(exc)
        return []

    _patch_invoke(monkeypatch, rt, sleepy)
    t0 = time.perf_counter()
    out = rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})
    assert time.perf_counter() - t0 < 5.0
    deadline = time.perf_counter() + 5.0
    running = 1
    while time.perf_counter() < deadline:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE %s AND query NOT LIKE %s AND state = 'active'",
                ("%fanout-cancel-test%", "%pg_stat_activity%"),
            ).fetchone()
        running = row[0] if row else 1
        if running == 0:
            break
        time.sleep(0.1)
    assert running == 0, "kind's query still running on the server after deadline"
    assert errors and "statement timeout" in str(errors[0]).lower()
    assert kinds[0] in out
