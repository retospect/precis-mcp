"""gr495190: cross-kind fan-out is parallel, capped and deadline-bounded."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.errors import QueryCanceled

from precis.embedder import MockEmbedder
from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from precis.utils.search_merge import SearchHit
from tests._timing import budget


class _ProductionStubEmbedder(MockEmbedder):
    """Deterministic vectors flagged production — a real backend
    stand-in without the network/torch dependency."""

    @property
    def is_production(self) -> bool:
        return True


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
    is execution order — a deterministic queue for floor-window tests.

    Cost-ordered submission (gr664988) must not reorder these fixtures:
    a clean cost memory restores the all-unmeasured alphabetical order
    the ``kinds`` slices assume — earlier tests in the same process
    leave measured EMAs that would otherwise reshuffle the queue."""
    monkeypatch.setattr(search_mod, "CROSS_KIND_MAX_WORKERS", 1)
    monkeypatch.setattr(search_mod, "_cross_kind_pool", None)
    search_mod._cross_kind_costs.clear()


@contextmanager
def _fresh_cost_memory() -> Iterator[None]:
    """Isolate the process-wide fan-out cost EMA (gr664988)."""
    saved = dict(search_mod._cross_kind_costs)
    search_mod._cross_kind_costs.clear()
    try:
        yield
    finally:
        search_mod._cross_kind_costs.clear()
        search_mod._cross_kind_costs.update(saved)


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
    # gr700451: earlier calls can leave stragglers holding shared workers.
    # Give each kind a worker so this tests the deadline, not pool contention.
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        monkeypatch.setattr(search_mod, "_cross_kind_executor", lambda: pool)
        try:
            t0 = time.perf_counter()
            out = rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})
            elapsed = time.perf_counter() - t0
        finally:
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
    # Own pool, one worker per kind: on a busy shared executor the sleepy job
    # can be dropped unstarted at the deadline, so no query ever reaches the
    # server and ``errors`` stays empty (r349 gate-fast flake).
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        monkeypatch.setattr(search_mod, "_cross_kind_executor", lambda: pool)
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


@pytest.mark.parametrize(("timeout", "expected"), [(None, 2.0), (1.0, 1.0), (5.0, 2.0)])
def test_checkout_deadline_bounds_a_waiting_checkout(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None, expected: float
) -> None:
    """gr700450: pin the forwarded wait without a live pool or wall clock.

    A shared runtime pool also serves boot reconciliation checkouts, so
    spying on it can collect unrelated waits. An unopened pool and a
    simulated saturated checkout isolate the deadline contract, including
    preservation of a tighter explicit timeout.
    """
    from psycopg import Connection
    from psycopg_pool import PoolTimeout

    from precis.store.pool import POOL_CHECKOUT_DEADLINE, PrecisPool

    clock = _patch_pool_clock(monkeypatch)
    pool = PrecisPool(open=False, timeout=30.0)
    checkouts: list[float | None] = []

    def saturated(timeout: float | None = None) -> Connection:
        checkouts.append(timeout)
        raise PoolTimeout("simulated saturated pool")

    monkeypatch.setattr(pool, "getconn", saturated)
    token = POOL_CHECKOUT_DEADLINE.set(clock["t"] + 2.0)
    try:
        with pytest.raises(PoolTimeout, match="simulated saturated pool"):
            with pool.connection(timeout=timeout):
                pytest.fail("saturated checkout must not yield a connection")
    finally:
        POOL_CHECKOUT_DEADLINE.reset(token)
        pool.close()
    assert checkouts == [expected]


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


def test_dispatch_merges_and_names_dropped_kinds(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wildcard end-to-end under tiering (gr698809): a best-effort kind
    that cannot fit its tier budget is dropped at deadline and named in
    the header — never read as zero matches — while the kinds that
    answered inside their budget keep their hits in the merge. (The
    shorter per-kind execution cap still applies inside the shared
    window.)"""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None

    monkeypatch.setattr(
        rt.hub, "embedder", _ProductionStubEmbedder(dim=store.embedding_dim())
    )
    # No up-front leg skips — the deadline drop is what "web" exercises.
    monkeypatch.setattr(
        type(rt), "_cross_kind_slow_semantic_kinds", lambda self, ks, d: []
    )
    monkeypatch.setattr(search_mod, "CROSS_KIND_PER_KIND_S", 0.2)
    released = threading.Event()

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        if kind == "web":
            released.wait(2)  # exceeds the 0.2 s execution cap
            return []
        if kind == "agentlog":
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
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    try:
        out = rt._dispatch_cross_kind("*", {"q": "xyzzy"})
    finally:
        released.set()

    assert "alpha xyzzy" in out.body  # the on-time kind's hits merge
    dropped = [line for line in out.body.splitlines() if "dropped at deadline" in line]
    assert dropped and "web" in dropped[0]
    assert "errored, omitted" not in out.body  # a miss is not an error
    counts = next(line for line in out.body.splitlines() if "(per kind:" in line)
    assert "agentlog: 1" in counts
    assert "web:" not in counts


def test_wildcard_best_effort_kind_can_start_after_first_wave(
    runtime_stateless: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_stateless
    clock = _frozen_clock(monkeypatch)
    hit = SearchHit(
        score=0.9,
        kind="skill",
        title="Call deadlines",
        preview="p",
        ref_id=None,
        slug=None,
    )

    def fake(kind: str) -> list[SearchHit]:
        if kind == "anki":
            clock["t"] += 2.1
            return []
        return [hit]

    monkeypatch.setattr(rt.hub, "handler_for", lambda kind: SimpleNamespace())
    _patch_invoke(monkeypatch, rt, fake)
    with _fresh_cost_memory(), ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(search_mod, "_cross_kind_executor", lambda: pool)
        monkeypatch.setitem(search_mod._cross_kind_costs, "anki", 0.0)
        monkeypatch.setitem(search_mod._cross_kind_costs, "skill", 1.0)
        out = rt._cross_kind_fan_out(
            ["anki", "skill"], {"q": "open calls"}, lexical_fallback=set(), tiered=True
        )

    assert isinstance(out["anki"], search_mod.KindSearchDeadlineMissed)
    assert out["skill"] == [hit]


def _stub_cancelled_fan_out(
    monkeypatch: pytest.MonkeyPatch,
    rt: PrecisRuntime,
    search_hits: Callable[..., list[SearchHit]],
) -> None:
    handlers = {
        "memory": SimpleNamespace(search_hits=search_hits),
        "todo": SimpleNamespace(search_hits=lambda **kwargs: []),
    }
    original_handler_for = rt.hub.handler_for

    def handler_for(kind: str) -> Any:
        return handlers[kind] if kind in handlers else original_handler_for(kind)

    monkeypatch.setattr(rt.hub, "handler_for", handler_for)
    monkeypatch.setattr(type(rt), "_cross_kind_kinds", lambda self: list(handlers))


@pytest.mark.parametrize(
    ("elapsed", "retry_fails", "expected_modes"),
    [
        (1.0, False, ["hybrid", "lexical"]),
        (1.0, True, ["hybrid", "lexical"]),
        (4.75, False, ["hybrid"]),
    ],
)
def test_cancelled_kind_retries_lexical(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    retry_fails: bool,
    expected_modes: list[str],
) -> None:
    rt = runtime_stateless
    clock = _frozen_clock(monkeypatch)
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 5.0)
    monkeypatch.setattr(search_mod, "CROSS_KIND_PRIORITY_S", 5.0)
    # A production embedder must be wired: with no embedder the fan-out
    # degrades the whole call to lexical up front (named in the header)
    # and the hybrid leg these parametrizations exercise never runs.
    monkeypatch.setattr(rt.hub, "embedder", _ProductionStubEmbedder())
    modes: list[str] = []

    def search_hits(*, mode: str = "hybrid", **kwargs: Any) -> list[SearchHit]:
        modes.append(mode)
        if mode == "hybrid":
            clock["t"] += elapsed
            raise QueryCanceled("hybrid leg canceled")
        if retry_fails:
            raise QueryCanceled("lexical leg canceled")
        return [
            SearchHit(
                score=0.9,
                kind="memory",
                title="recovered xyzzy",
                preview="p",
                ref_id=None,
                slug=None,
            )
        ]

    _stub_cancelled_fan_out(monkeypatch, rt, search_hits)

    # Finish todo before memory advances the clock, and finish both before
    # harvest can wait on them. At 4.75 s the cancel leaves 0.25 s, below
    # the 0.5 s lexical retry floor.
    class InlineExecutor:
        def submit(
            self, fn: Callable[..., list[SearchHit] | Exception], *args: Any
        ) -> Future[list[SearchHit] | Exception]:
            future: Future[list[SearchHit] | Exception] = Future()
            future.set_result(fn(*args))
            return future

    with _fresh_cost_memory():
        search_mod._cross_kind_costs["memory"] = 1.0
        executor = InlineExecutor()
        monkeypatch.setattr(search_mod, "_cross_kind_priority_pool", executor)
        out = rt._dispatch_cross_kind("*", {"q": "xyzzy"})

    assert modes == expected_modes
    recovered = "lexical" in expected_modes and not retry_fails
    assert ("recovered xyzzy" in out.body) == recovered
    assert ("semantic leg skipped for memory" in out.body) == recovered
    assert ("memory (QueryCanceled)" in out.body) != recovered


@pytest.mark.parametrize(
    ("elapsed", "retry_fails", "expected_modes"),
    [
        (1.0, False, ["hybrid", "lexical"]),
        (1.0, True, ["hybrid", "lexical"]),
        (4.75, False, ["hybrid"]),
    ],
)
def test_timed_out_kind_retries_lexical(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    retry_fails: bool,
    expected_modes: list[str],
) -> None:
    """gr776663: a built-in timeout uses the same reserved lexical retry
    budget and reports the same degradation as a cancelled leg."""
    rt = runtime_stateless
    clock = _frozen_clock(monkeypatch)
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 5.0)
    monkeypatch.setattr(search_mod, "CROSS_KIND_PRIORITY_S", 5.0)
    # A production embedder must be wired: with no embedder the fan-out
    # degrades the whole call to lexical up front (named in the header)
    # and the hybrid leg these parametrizations exercise never runs.
    monkeypatch.setattr(rt.hub, "embedder", _ProductionStubEmbedder())
    modes: list[str] = []

    def search_hits(*, mode: str = "hybrid", **kwargs: Any) -> list[SearchHit]:
        modes.append(mode)
        if mode == "hybrid":
            clock["t"] += elapsed
            raise TimeoutError("hybrid leg client wait expired")
        if retry_fails:
            raise TimeoutError("lexical leg client wait expired")
        return [
            SearchHit(
                score=0.9,
                kind="memory",
                title="recovered xyzzy",
                preview="p",
                ref_id=None,
                slug=None,
            )
        ]

    _stub_cancelled_fan_out(monkeypatch, rt, search_hits)

    class InlineExecutor:
        def submit(
            self, fn: Callable[..., list[SearchHit] | Exception], *args: Any
        ) -> Future[list[SearchHit] | Exception]:
            future: Future[list[SearchHit] | Exception] = Future()
            future.set_result(fn(*args))
            return future

    with _fresh_cost_memory():
        search_mod._cross_kind_costs["memory"] = 1.0
        executor = InlineExecutor()
        monkeypatch.setattr(search_mod, "_cross_kind_priority_pool", executor)
        out = rt._dispatch_cross_kind("*", {"q": "xyzzy"})

    assert modes == expected_modes
    recovered = "lexical" in expected_modes and not retry_fails
    assert ("recovered xyzzy" in out.body) == recovered
    assert ("semantic leg skipped for memory" in out.body) == recovered
    assert ("memory (TimeoutError)" in out.body) != recovered


@pytest.mark.parametrize(
    ("kind", "args"),
    [("memory,todo", {}), ("*", {"mode": "hybrid"})],
)
def test_cancelled_explicit_list_keeps_error(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    args: dict[str, str],
) -> None:
    rt = runtime_stateless
    # Wire a production embedder: with none, the fan-out degrades the
    # call to lexical up front and the hybrid cancel these cases pin
    # never happens (see test_cancelled_kind_retries_lexical).
    monkeypatch.setattr(rt.hub, "embedder", _ProductionStubEmbedder())
    modes: list[str] = []

    def search_hits(*, mode: str = "hybrid", **kwargs: Any) -> list[SearchHit]:
        modes.append(mode)
        raise QueryCanceled("hybrid leg canceled")

    _stub_cancelled_fan_out(monkeypatch, rt, search_hits)
    out = rt._dispatch_cross_kind(kind, {"q": "xyzzy", **args})

    assert modes == ["hybrid"]
    assert "memory (QueryCanceled)" in out.body
    assert "semantic leg skipped for memory" not in out.body


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
    # Saturate the pool by draining it to empty rather than assuming all
    # ``max_size`` connections are free: ``boot``'s background threads
    # (``precis-checklist-sync``) hold a checkout for the test's
    # duration, so a fixed ``max_size`` count waits out the full 30 s
    # getconn timeout instead of saturating.
    held: list[Any] = []
    try:
        while True:
            held.append(store.pool.getconn(timeout=2.0))
    except PoolTimeout:
        pass
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


def test_apply_checkout_budget_recaps_a_late_statement(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr664954 (review td665127): ``statement_timeout`` freezes at the
    value the checkout's ``set_config`` wrote — a further statement on
    the same checkout re-applies so it cannot outlive the call
    deadline."""
    from precis.store.pool import CHECKOUT_BUDGET, apply_checkout_budget

    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    token = CHECKOUT_BUDGET.set((time.monotonic() + 60.0, 4000.0))
    try:
        with store.pool.connection() as conn:
            # The scope's remaining budget shrank mid-checkout: the next
            # statement must not keep the 4000 ms the checkout wrote.
            CHECKOUT_BUDGET.set((time.monotonic() + 0.5, 4000.0))
            apply_checkout_budget(conn)
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()
        assert row is not None
        assert 0 < int(row[0]) <= 500
    finally:
        CHECKOUT_BUDGET.reset(token)


def test_lexical_or_retry_recomputes_the_statement_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664954 (review td665127): ``search_chunks_lexical`` runs its
    strict AND leg and the OR retry on one checkout. The AND leg spends
    budget first, so the retry must re-derive the remaining budget —
    not inherit the stale checkout-time cap and hold a fan-out worker
    past the call deadline."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from typing import cast

    from precis.store import pool as pool_mod
    from precis.store._chunks_ops import ChunkStore
    from precis.store.core import StoreCore
    from precis.store.pool import CHECKOUT_BUDGET

    clock = {"t": 1000.0}

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

    monkeypatch.setattr(pool_mod, "time", _Clock())

    calls: list[tuple[str, Any]] = []

    class _Rows:
        def fetchall(self) -> list:
            return []

        def fetchone(self) -> None:
            return None

    class _Conn:
        def execute(self, sql: Any, params: Any = None) -> _Rows:
            calls.append((str(sql), params))
            # The strict AND leg consumes 9.5 s of the scope's 10 s
            # deadline before coming back empty — what remains for the
            # OR retry is ~500 ms, not the checkout's 10 s cap.
            if "websearch_to_tsquery" in str(sql):
                clock["t"] += 9.5
            return _Rows()

    class _Pool:
        @contextmanager
        def connection(self, timeout: float | None = None) -> Any:
            yield _Conn()

    chunks = ChunkStore.__new__(ChunkStore)
    chunks._core = cast(StoreCore, SimpleNamespace(pool=_Pool()))

    token = CHECKOUT_BUDGET.set((clock["t"] + 10.0, 40_000.0))
    try:
        out = chunks.search_chunks_lexical(q="alpha beta gamma", limit=5)
    finally:
        CHECKOUT_BUDGET.reset(token)
    assert out == []

    statements = [s for s, _p in calls]
    and_i = next(i for i, s in enumerate(statements) if "websearch_to_tsquery" in s)
    or_i = next(i for i, s in enumerate(statements) if "plainto_tsquery" in s)
    recaps = [
        params
        for i, (s, params) in enumerate(calls)
        if "set_config('statement_timeout'" in s and and_i < i < or_i
    ]
    assert recaps, "no statement_timeout re-cap between the AND leg and OR retry"
    for params in recaps:
        assert params is not None and 0 < int(params[0]) <= 500


def _patch_pool_clock(
    monkeypatch: pytest.MonkeyPatch, start: float = 1000.0
) -> dict[str, float]:
    """Freeze the clock ``precis.store.pool`` reads, so
    ``checkout_budget_cap_ms`` sees a controllable *now*."""
    from precis.store import pool as pool_mod

    clock = {"t": start}

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

    monkeypatch.setattr(pool_mod, "time", _Clock())
    return clock


class _CapRows:
    """Cursor stand-in for the fake-conn re-cap tests below."""

    def __init__(self, one: Any = None, rows: list[Any] | None = None) -> None:
        self._one = one
        self._rows = list(rows or [])

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _cap_writes(calls: list[tuple[str, Any]]) -> list[tuple[int, int]]:
    """``(call index, cap_ms)`` for each ``set_config('statement_timeout'…)``."""
    return [
        (i, int(params[0]))
        for i, (s, params) in enumerate(calls)
        if "set_config('statement_timeout'" in s and params is not None
    ]


def test_ann_kind_state_recaps_before_the_marker_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664954 (review td674196): ``_ann_kind_state`` issues the marker
    table read as a second statement on the checkout — after the
    catalogue probe spent budget it must re-derive the remaining cap,
    not inherit the stale checkout-time bound and outlive the call
    deadline."""
    from types import SimpleNamespace
    from typing import cast

    from psycopg import Connection

    from precis.store import _chunks_ops as chunks_mod
    from precis.store.pool import CHECKOUT_BUDGET

    clock = _patch_pool_clock(monkeypatch)
    calls: list[tuple[str, Any]] = []

    class _Conn:
        info = SimpleNamespace(host="fake", port=5432, dbname="fake")

        def execute(self, sql: Any, params: Any = None) -> _CapRows:
            calls.append((str(sql), params))
            if "pg_constraint" in str(sql):
                # The catalogue probe consumed most of the scope's 10 s.
                clock["t"] += 9.5
                return _CapRows(one=(False, {}, True))
            if "FROM chunk_embeddings_kind_ready" in str(sql):
                return _CapRows(rows=[("memory",)])
            return _CapRows()

    chunks_mod._reset_ann_state_cache()
    token = CHECKOUT_BUDGET.set((clock["t"] + 10.0, 40_000.0))
    try:
        state = chunks_mod._ann_kind_state(cast(Connection, _Conn()))
    finally:
        CHECKOUT_BUDGET.reset(token)
        chunks_mod._reset_ann_state_cache()
    assert state.marked == frozenset({"memory"})

    cat_i = next(i for i, (s, _p) in enumerate(calls) if "pg_constraint" in s)
    mark_i = next(
        i for i, (s, _p) in enumerate(calls) if "FROM chunk_embeddings_kind_ready" in s
    )
    recaps = [cap for i, cap in _cap_writes(calls) if cat_i < i < mark_i]
    assert recaps, "no statement_timeout re-cap before the marker read"
    for cap in recaps:
        assert 0 < cap <= 500


def test_prepare_filtered_ann_recaps_each_late_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664954 (review td674196): ``_prepare_filtered_ann`` froze
    ``statement_timeout`` once at entry — the version probe and the GUC
    write that follow spend budget under that stale bound, so each
    later statement (and the caller's ANN query after return) must
    re-derive the remaining cap."""
    from typing import cast

    from psycopg import Connection

    from precis.store import _chunks_ops as chunks_mod
    from precis.store.pool import CHECKOUT_BUDGET

    clock = _patch_pool_clock(monkeypatch)
    monkeypatch.setattr(chunks_mod, "_ann_iterative_supported", None)
    calls: list[tuple[str, Any]] = []

    class _Conn:
        def execute(self, sql: Any, params: Any = None) -> _CapRows:
            calls.append((str(sql), params))
            if "pg_extension" in str(sql):
                clock["t"] += 9.0  # the version probe spent most of 10 s
                return _CapRows(one=("0.8.0",))
            if "hnsw.iterative_scan" in str(sql):
                clock["t"] += 0.4  # the GUC write spent a little more
            return _CapRows()

    token = CHECKOUT_BUDGET.set((clock["t"] + 10.0, 40_000.0))
    try:
        chunks_mod._prepare_filtered_ann(cast(Connection, _Conn()), 10)
    finally:
        CHECKOUT_BUDGET.reset(token)

    ver_i = next(i for i, (s, _p) in enumerate(calls) if "pg_extension" in s)
    guc_i = next(i for i, (s, _p) in enumerate(calls) if "hnsw.iterative_scan" in s)
    caps = _cap_writes(calls)
    # Entry cap (~10 s) bounds the version probe; a re-cap (~1 s)
    # precedes the GUC write; the final re-cap — the bound the caller's
    # ANN statement inherits — follows it and tracks the ~600 ms
    # actually remaining, not the stale 10 s.
    assert caps[0] == (0, 10_000)
    assert any(ver_i < i < guc_i and cap <= 1_000 for i, cap in caps)
    assert caps[-1][0] > guc_i and caps[-1][1] <= 600
    assert [cap for _i, cap in caps] == sorted((cap for _i, cap in caps), reverse=True)


def test_fan_out_submits_cheapest_measured_kinds_first(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr664988: alphabetical submission spent the deadline's first
    worker-seconds on early-letter heavy kinds while the cheap tail
    starved to TimeoutError on queue position. The fan-out now submits
    unmeasured kinds first, then ascending measured cost — a serial
    executor makes the submission order directly observable."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:5]
    with _fresh_cost_memory():
        slow, fast = kinds[0], kinds[1]
        unmeasured = sorted(kinds[2:])
        search_mod._cross_kind_costs[slow] = 9.0
        search_mod._cross_kind_costs[fast] = 0.1
        order: list[str] = []

        def fake(k: str) -> list:
            order.append(k)
            return []

        solo = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(search_mod, "_cross_kind_executor", lambda: solo)
        _patch_invoke(monkeypatch, rt, fake)
        rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})

        assert order == unmeasured + [fast, slow]


def test_fan_out_records_what_each_kind_cost(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr664988: every attempt that ran feeds the cost EMA — hits or a
    caught exception alike — so the next call's ordering is measured,
    not guessed. A kind the deadline never reached records nothing and
    stays unmeasured (front of the next call's queue)."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:2]
    with _fresh_cost_memory():

        def fake(k: str) -> list:
            time.sleep(0.2)
            return []

        _patch_invoke(monkeypatch, rt, fake)
        rt._cross_kind_fan_out(kinds, {"q": "x", "top_k": 10})

        for k in kinds:
            cost = search_mod._cross_kind_costs.get(k)
            assert cost is not None and cost >= 0.15, (k, cost)


def test_dropped_partial_kinds_start_behind_unmeasured_ones() -> None:
    """gr664988: a dropped-partial kind (``draft``, ``quest``, ``tex``)
    carries an at-cap cold-start prior — its ANN leg permanently takes
    the global-fallback post-filter, the documented 10-30 s worst
    case — so even a fresh process schedules it after every unmeasured
    kind instead of alphabetically early. A designated global-index
    kind (``paper``) carries NO prior: it dominates the global index,
    its post-filter keeps nearly every candidate (~0.3 s), and seeding
    it slow hid a healthy kind behind the tail (review td665388)."""
    from precis.store._chunks_ops import (
        _ANN_DROPPED_PARTIAL_KINDS,
        _ANN_GLOBAL_KINDS,
    )

    assert _ANN_DROPPED_PARTIAL_KINDS, "test expects a dropped-partial kind"
    assert _ANN_GLOBAL_KINDS, "test expects a designated global-index kind"
    with _fresh_cost_memory():
        for k in _ANN_DROPPED_PARTIAL_KINDS:
            prior = search_mod._cross_kind_job_key(k)[0]
            assert prior >= search_mod.CROSS_KIND_PER_KIND_S
            assert prior > search_mod._cross_kind_job_key("memory")[0]
        for k in _ANN_GLOBAL_KINDS:
            assert search_mod._cross_kind_job_key(k)[0] == 0.0


def test_priority_tier_survives_best_effort_deadline(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    monkeypatch.setattr(search_mod, "CROSS_KIND_PER_KIND_S", 0.1)
    monkeypatch.setattr(search_mod, "CROSS_KIND_PRIORITY_S", 1.0)
    release = threading.Event()
    started = threading.Event()

    def fake(kind: str) -> list:
        if kind == "web":
            started.set()
            release.wait(2)
        else:
            assert started.wait(1)
            # Finish after the best-effort deadline.
            time.sleep(0.2)
        return []

    _patch_invoke(monkeypatch, rt, fake)
    try:
        out = rt._cross_kind_fan_out(["web", "todo"], {"q": "blocker"}, tiered=True)
        assert out["todo"] == []
        assert isinstance(out["web"], TimeoutError)
    finally:
        release.set()
