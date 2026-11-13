"""gr495190: cross-kind fan-out is parallel, capped and deadline-bounded."""

from __future__ import annotations

import threading
import time

import pytest

from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from tests._timing import budget


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


def test_fan_out_shares_the_dispatch_query_embedding(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr519679: the vector the dispatcher computed once must serve every
    kind. A per-kind re-embed inside a worker is a remote call on the
    fan-out deadline — a slow/at-capacity embed service turns ~14 kinds
    into stragglers and starves every kind queued behind them."""
    from precis.embedder import MockEmbedder

    rt = runtime_with_store
    query = "zebra quoll sensor"

    class _CountingEmbedder(MockEmbedder):
        """Flagged production: a mock backend now skips the semantic leg
        up front (gr628278), which is exactly what this test is not
        about — it pins the once-per-call shared embed for a real
        embedder."""

        def __init__(self, dim: int) -> None:
            super().__init__(dim=dim)
            self.calls = 0

        @property
        def is_production(self) -> bool:
            return True

        def embed_one(self, text: str) -> list[float]:
            # Only the query string counts: the skill index (in the fan-out
            # since td610272) warms its corpus through this embedder on a
            # background thread, and those chunk embeds are not re-embeds.
            if text == query:
                self.calls += 1
            return super().embed_one(text)

    store = rt.hub.store
    assert store is not None
    emb = _CountingEmbedder(dim=store.embedding_dim())
    monkeypatch.setattr(rt.hub, "embedder", emb)
    rt._dispatch_cross_kind("*", {"q": query})
    assert emb.calls == 1


def test_per_kind_statement_timeout_caps_a_straggler(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr519679: a kind may not hold a pooled worker for the whole
    deadline — its server-side statement timeout is capped, so Postgres
    cancels a runaway query early and the queue drains to the cheap
    kinds waiting behind it."""
    from precis.store._chunks_ops import ANN_STATEMENT_TIMEOUT_MS

    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    monkeypatch.setattr(search_mod, "CROSS_KIND_PER_KIND_S", 2.0)
    seen: dict[str, int | None] = {}

    def fake(k: str) -> list:
        seen[k] = ANN_STATEMENT_TIMEOUT_MS.get()
        return []

    _patch_invoke(monkeypatch, rt, fake)
    rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})
    for k in kinds:
        timeout = seen[k]
        assert timeout is not None and timeout <= 2000


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
    assert elapsed < budget(2.0)
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
    assert time.perf_counter() - t0 < budget(5.0)
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


def test_fan_out_pins_pool_checkouts_to_the_call_deadline(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr664970: each fanned job carries the shared call deadline into
    its pool checkouts, so a straggler can't hold a shared worker on a
    default 30 s checkout long after its results were discarded."""
    from precis.store.pool import POOL_CHECKOUT_DEADLINE

    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:2]
    deadline_at = time.monotonic() + 5.0
    seen: dict[str, float | None] = {}

    def fake(k: str) -> list:
        seen[k] = POOL_CHECKOUT_DEADLINE.get()
        return []

    _patch_invoke(monkeypatch, rt, fake)
    rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10}, deadline_at=deadline_at)
    assert set(seen) == set(kinds)
    assert all(dl == deadline_at for dl in seen.values())


def test_checkout_deadline_fails_fast_past_the_deadline(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr664970: a checkout in a context whose deadline already passed
    raises PoolTimeout immediately — it must not wait the pool's 30 s
    default (or take a free conn) for work whose results are discarded."""
    from psycopg_pool import PoolTimeout

    from precis.store.pool import POOL_CHECKOUT_DEADLINE

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    token = POOL_CHECKOUT_DEADLINE.set(time.monotonic() - 0.01)
    try:
        t0 = time.monotonic()
        with pytest.raises(PoolTimeout):
            with store.pool.connection():
                pass
        assert time.monotonic() - t0 < budget(1.0)
    finally:
        POOL_CHECKOUT_DEADLINE.reset(token)


def test_checkout_deadline_bounds_a_waiting_checkout(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The bound shrinks the wait, not just the past-deadline case: a
    checkout with seconds left may not queue for the pool's full 30 s
    default — the straggler fails at its call deadline and frees the
    executor worker."""
    from precis.store.pool import POOL_CHECKOUT_DEADLINE

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    checkouts: list[float | None] = []
    orig_getconn = store.pool.getconn

    def spy(timeout: float | None = None):
        checkouts.append(timeout)
        return orig_getconn(timeout=timeout)

    token = POOL_CHECKOUT_DEADLINE.set(time.monotonic() + 2.0)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store.pool, "getconn", spy)
            with store.pool.connection():
                pass
    finally:
        POOL_CHECKOUT_DEADLINE.reset(token)
    assert checkouts and all(t is not None and 0 < t <= 2.0 for t in checkouts)
