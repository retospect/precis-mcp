"""gr495190: cross-kind fan-out is parallel, capped and deadline-bounded."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from tests._timing import budget


def _patch_invoke(monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime, fn) -> None:
    monkeypatch.setattr(
        type(rt), "_cross_kind_invoke_search_hits", lambda self, h, k, kw: fn(k)
    )


def _frozen_clock(
    monkeypatch: pytest.MonkeyPatch, start: float = 1000.0
) -> dict[str, float]:
    """Replace ``search_mod.time`` with a controllable clock.

    ``search.py`` calls ``time.monotonic()`` through its module-global
    ``time`` name, so swapping that name freezes the fan-out's clock
    without touching the real ``time`` module the pool internals (and
    other threads) still use. Mutate ``clock["t"]`` to advance it.
    """
    clock = {"t": start}

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

    monkeypatch.setattr(search_mod, "time", _Clock())
    return clock


def _serial_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the shared executor down to one worker so submission order
    is execution order — a deterministic queue for floor-window tests."""
    monkeypatch.setattr(search_mod, "CROSS_KIND_MAX_WORKERS", 1)
    monkeypatch.setattr(search_mod, "_cross_kind_pool", None)


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


def test_starved_kind_runs_lexical_instead_of_timing_out(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr664954: a kind dequeued inside the min-start floor used to
    report TimeoutError without trying — cheap kinds vanished from the
    merge purely on queue position. Inside the smaller lexical floor it
    now runs lexical-only and is collected for the footer."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    clock = _frozen_clock(monkeypatch)
    _serial_fan_out(monkeypatch)
    seen: dict[str, dict[str, Any]] = {}

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        seen[kind] = dict(kw)
        clock["t"] += 11.0  # each kind leaves the next ~1 s of the 12 s budget
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    fallback: set[str] = set()
    out = rt._cross_kind_fan_out(
        kinds, {"q": "x"}, deadline_at=1012.0, lexical_fallback=fallback
    )

    # kinds[0] dequeued with the whole budget: full legs, no degrade.
    assert "mode" not in seen[kinds[0]]
    # kinds[1] dequeued at ~1 s remaining — inside the lexical floor
    # window — so it ran lexical-only and is marked for the footer.
    assert seen[kinds[1]]["mode"] == "lexical"
    assert fallback == {kinds[1]}
    # kinds[2] saw a past deadline: still an honest TimeoutError.
    assert isinstance(out[kinds[2]], TimeoutError)


def test_dispatch_merges_and_names_lexical_fallback_kinds(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664954 end-to-end: a wildcard search whose tail kinds only
    reach a worker inside the deadline's last slice still merges their
    lexical hits and names the degrade in a footer — the old behaviour
    dropped them to TimeoutError."""
    from precis.embedder import MockEmbedder
    from precis.utils.search_merge import SearchHit

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    kinds = rt._cross_kind_kinds()
    assert len(kinds) >= 3

    class _ProductionStubEmbedder(MockEmbedder):
        @property
        def is_production(self) -> bool:
            return True

    monkeypatch.setattr(
        rt.hub, "embedder", _ProductionStubEmbedder(dim=store.embedding_dim())
    )
    # No up-front leg skips — every kind keeps its hybrid legs, so the
    # floor-window degrade is what the second kind exercises.
    monkeypatch.setattr(
        type(rt), "_cross_kind_slow_semantic_kinds", lambda self, ks, d: []
    )
    clock = _frozen_clock(monkeypatch)
    _serial_fan_out(monkeypatch)

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        clock["t"] += 11.0
        if kind == kinds[0]:
            return [
                SearchHit(
                    score=0.9,
                    kind=kind,
                    title="alpha xyzzy",
                    preview="p",
                    ref_id=None,
                    slug=None,
                )
            ]
        if kind == kinds[1]:
            return [
                SearchHit(
                    score=0.8,
                    kind=kind,
                    title="beta xyzzy",
                    preview="p",
                    ref_id=None,
                    slug=None,
                )
            ]
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    out = rt._dispatch_cross_kind("*", {"q": "xyzzy"})

    assert "beta xyzzy" in out.body  # the salvaged kind's hits merge
    assert f"semantic leg skipped for {kinds[1]}" in out.body
    assert "ran lexical-only this turn" in out.body


def test_starved_explicit_list_keeps_the_timeout(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lexical_fallback=None`` — the explicit kind-list path — keeps
    the old contract: a kind that cannot fit the full legs reports the
    timeout rather than degrading silently (caller intent, same as the
    up-front leg skip)."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    clock = _frozen_clock(monkeypatch)
    _serial_fan_out(monkeypatch)

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        clock["t"] += 11.0
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    out = rt._cross_kind_fan_out(kinds, {"q": "x"}, deadline_at=1012.0)
    assert out[kinds[0]] == []
    assert isinstance(out[kinds[1]], TimeoutError)


def test_starved_semantic_mode_keeps_the_timeout(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode='semantic'`` is the caller asking for the leg knowingly —
    a late-dequeued kind reports the timeout rather than answering
    lexical-only."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:2]
    clock = _frozen_clock(monkeypatch)
    _serial_fan_out(monkeypatch)

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        clock["t"] += 11.0
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    fallback: set[str] = set()
    out = rt._cross_kind_fan_out(
        kinds,
        {"q": "x", "mode": "semantic"},
        deadline_at=1012.0,
        lexical_fallback=fallback,
    )
    assert out[kinds[0]] == []
    assert isinstance(out[kinds[1]], TimeoutError)
    assert fallback == set()


@pytest.mark.parametrize("mode", ["verbatim", "literal"])
def test_starved_exact_mode_keeps_the_timeout(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """An exact-term ``mode=`` is caller intent: rewriting it to
    ``'lexical'`` would silently broaden the match, so a late-dequeued
    kind reports the timeout rather than degrading (review td665127)."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:2]
    clock = _frozen_clock(monkeypatch)
    _serial_fan_out(monkeypatch)

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        clock["t"] += 11.0
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    fallback: set[str] = set()
    out = rt._cross_kind_fan_out(
        kinds,
        {"q": "x", "mode": mode},
        deadline_at=1012.0,
        lexical_fallback=fallback,
    )
    assert out[kinds[0]] == []
    assert isinstance(out[kinds[1]], TimeoutError)
    assert fallback == set()


def test_prepare_filtered_ann_recomputes_the_checkout_budget(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr664954 (review td665127): ``ANN_STATEMENT_TIMEOUT_MS`` holds
    the budget remaining at fan-out *dequeue*; a statement that starts
    after further work would outlive the call deadline if that stale
    value were applied. ``_prepare_filtered_ann`` re-derives the
    scope's remaining budget at execution and takes the tightest
    bound."""
    from precis.store._chunks_ops import (
        ANN_STATEMENT_TIMEOUT_MS,
        _prepare_filtered_ann,
    )
    from precis.store.pool import CHECKOUT_BUDGET

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None

    # Stale dequeue-time value (11 s of the 12 s deadline was left when
    # the worker started) vs ~2 s actually remaining now — the applied
    # timeout must track the fresher number, not the stale 11000.
    ann_tok = ANN_STATEMENT_TIMEOUT_MS.set(11_000)
    budget_tok = CHECKOUT_BUDGET.set((time.monotonic() + 2.0, 60_000.0))
    try:
        with store.pool.connection() as conn:
            _prepare_filtered_ann(conn, 10)
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()
        assert row is not None
        assert 0 < int(row[0]) <= 2000
    finally:
        ANN_STATEMENT_TIMEOUT_MS.reset(ann_tok)
        CHECKOUT_BUDGET.reset(budget_tok)

    # A *tighter* dequeue-time cap still binds — the recompute only
    # ever lowers the applied timeout, never lifts it.
    ann_tok = ANN_STATEMENT_TIMEOUT_MS.set(500)
    budget_tok = CHECKOUT_BUDGET.set((time.monotonic() + 60.0, 60_000.0))
    try:
        with store.pool.connection() as conn:
            _prepare_filtered_ann(conn, 10)
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()
        assert row is not None and int(row[0]) == 500
    finally:
        ANN_STATEMENT_TIMEOUT_MS.reset(ann_tok)
        CHECKOUT_BUDGET.reset(budget_tok)


def test_checkout_budget_caps_every_statement(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr664954: inside a CHECKOUT_BUDGET scope the pool applies a
    transaction-local statement_timeout to every checkout — a non-ANN
    straggler statement can no longer hold a fan-out worker past the
    call deadline."""
    from precis.store.pool import CHECKOUT_BUDGET

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    token = CHECKOUT_BUDGET.set((time.monotonic() + 60.0, 4000.0))
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()
        assert row is not None and int(row[0]) == 4000
    finally:
        CHECKOUT_BUDGET.reset(token)
    # Outside the scope a checkout keeps the ambient default — the
    # scope's cap must not leak into it.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
        ).fetchone()
    assert row is not None and int(row[0]) != 4000


def test_checkout_budget_bounds_the_pool_wait(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A saturated pool used to hold a fan-out worker on the pool's
    unbounded checkout wait — inside the budget it fails fast instead."""
    from psycopg_pool import PoolTimeout

    from precis.store.pool import CHECKOUT_BUDGET

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    held = [store.pool.getconn() for _ in range(int(store.pool.max_size))]
    token = CHECKOUT_BUDGET.set((time.monotonic() - 1.0, 4000.0))  # past
    try:
        t0 = time.perf_counter()
        with pytest.raises(PoolTimeout), store.pool.connection():
            pass
        assert time.perf_counter() - t0 < budget(2.0)
    finally:
        CHECKOUT_BUDGET.reset(token)
        for conn in held:
            store.pool.putconn(conn)
