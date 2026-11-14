"""gr628278: the cross-kind fan-out fits the ``search/-`` tool-call
budget — its deadline is capped under ``TOOL_LATENCY_BUDGET_MS``, a
non-production embedder's hash-noise semantic leg is skipped up front,
and a kind whose ANN leg can only take the global-index route runs
lexical-only (named in a footer) instead of dying at the per-kind
statement cap.

td649559: the ANN route probe and the fan-out share one call deadline —
the probe's pool checkout and gate read draw from the budget instead of
running unbounded before the fan-out's clock starts."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from precis.store import Store
from precis.store.pool import PrecisPool
from tests._timing import budget


class _ProductionStubEmbedder(MockEmbedder):
    """Deterministic vectors flagged production — a real backend
    stand-in without the network/torch dependency (same shape as
    test_mcp_critic_regressions' stub)."""

    @property
    def is_production(self) -> bool:
        return True


def _record_invoke(
    monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime
) -> dict[str, dict[str, Any]]:
    """Stub the per-kind invoke; record the kwargs each kind was handed."""
    seen: dict[str, dict[str, Any]] = {}

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        seen[kind] = dict(kw)
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    return seen


def _spy_embeds(monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime) -> list[str]:
    """Wrap the hub embedder's ``embed_one``; return the embedded texts."""
    emb = rt.hub.embedder
    assert emb is not None
    texts: list[str] = []
    orig = emb.embed_one

    def spy(text: str) -> list[float]:
        texts.append(text)
        return orig(text)

    monkeypatch.setattr(emb, "embed_one", spy)
    return texts


def test_fanout_deadline_fits_the_tool_call_budget() -> None:
    """``search/-`` reports against ``TOOL_LATENCY_BUDGET_MS`` (15 s);
    the fan-out deadline plus the embed/merge headroom must sit under
    it — the 30 s deadline could never fit (search/- p50 was 17.5 s)."""
    from precis.health_checks import TOOL_LATENCY_BUDGET_MS

    assert search_mod.CROSS_KIND_DEADLINE_S < TOOL_LATENCY_BUDGET_MS / 1000
    assert search_mod.CROSS_KIND_PER_KIND_S < search_mod.CROSS_KIND_DEADLINE_S


def test_mock_embedder_skips_the_semantic_leg_up_front(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a non-production embedder the fan-out forces lexical for
    every kind: the query is never embedded, no ``query_vec`` reaches a
    handler, and the marker says the leg was skipped — no ANN scan pays
    for a ranking that can only be hash noise."""
    rt = runtime_with_store
    q = "gr628278uniquemarker"
    embedded = _spy_embeds(monkeypatch, rt)
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": q})

    assert q not in embedded
    assert seen, "every fanned kind should still run"
    assert all(kw.get("mode") == "lexical" for kw in seen.values())
    assert all("query_vec" not in kw for kw in seen.values())
    assert "non-production" in out.body
    assert "semantic leg was skipped" in out.body


def test_mock_embedder_honours_explicit_semantic_mode(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode='semantic'`` is caller intent — the leg runs even when its
    vector is mock noise."""
    rt = runtime_with_store
    q = "gr628278uniquemarker"
    embedded = _spy_embeds(monkeypatch, rt)
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": q, "mode": "semantic"})

    assert q in embedded
    assert all(kw.get("mode") == "semantic" for kw in seen.values())
    assert "semantic leg was skipped" not in out.body


def test_global_fallback_kind_gets_lexical_leg_and_is_named(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wildcard under a production embedder: a ``_ANN_PARTIAL_KINDS``
    member whose index is missing/unready (none are built in the test
    DB) routes its ANN leg over the global index — the 10-30 s scan —
    so it is forced lexical up front and named in a footer, while
    ``paper`` (a designated global-index kind) and the small kinds keep
    the full hybrid legs."""
    from precis.store._chunks_ops import _ANN_PARTIAL_KINDS

    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})

    skipped = set(seen) & set(_ANN_PARTIAL_KINDS)
    assert skipped, "the test DB builds no partial indexes — fallback expected"
    assert all(seen[k].get("mode") == "lexical" for k in skipped)
    kept = set(seen) - skipped
    assert kept and all(seen[k].get("mode") != "lexical" for k in kept)
    assert all("query_vec" in seen[k] for k in kept)
    assert "semantic leg skipped for" in out.body
    for k in sorted(skipped):
        assert k in out.body


def test_explicit_kind_list_keeps_the_slow_kind_on_full_legs(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit comma-list is caller intent: ``memory`` (a partial-
    index kind with no index in the test DB) keeps its hybrid legs —
    the per-kind statement cap remains the honest backstop there."""
    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("paper,memory", {"q": "gr628278uniquemarker"})

    assert "memory" in seen
    assert seen["memory"].get("mode") != "lexical"
    assert "query_vec" in seen["memory"]
    assert "semantic leg skipped for" not in out.body


def test_mock_embedder_keeps_an_explicit_list_on_full_legs(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit comma-list is caller intent — even under a
    non-production embedder the named kinds keep their legs (the up-front
    skip is the wildcard default expansion's optimization, same gate the
    domain fence uses)."""
    rt = runtime_with_store
    seen = _record_invoke(monkeypatch, rt)

    rt._dispatch_cross_kind("paper,memory", {"q": "gr628278uniquemarker"})

    assert seen
    assert all(kw.get("mode") != "lexical" for kw in seen.values())
    assert all("query_vec" in kw for kw in seen.values())


def test_probe_and_fan_out_share_one_call_deadline(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td649559: ``_dispatch_cross_kind`` hands the probe and the fan-out
    the same deadline instant — the probe can no longer spend an
    unbounded prefix before the fan-out's clock starts."""
    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen_deadlines: dict[str, float] = {}

    def probe(self: Any, kinds: list[str], deadline_at: float) -> list[str]:
        seen_deadlines["probe"] = deadline_at
        return []

    def fanout(
        self: Any,
        kinds: list[str],
        base_kwargs: dict[str, Any],
        kind_overrides: dict[str, dict[str, Any]] | None = None,
        *,
        deadline_at: float | None = None,
        lexical_fallback: set[str] | None = None,
    ) -> dict[str, Any]:
        seen_deadlines["fanout"] = deadline_at if deadline_at is not None else -1.0
        return {}

    monkeypatch.setattr(type(rt), "_cross_kind_slow_semantic_kinds", probe)
    monkeypatch.setattr(type(rt), "_cross_kind_fan_out", fanout)

    t0 = time.monotonic()
    rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})

    assert seen_deadlines["probe"] == seen_deadlines["fanout"]
    assert t0 <= seen_deadlines["probe"] <= t0 + search_mod.CROSS_KIND_DEADLINE_S + 5.0


def test_stalled_probe_sql_dies_at_the_shared_deadline(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td649559: a stalled gate read used to block the dispatch thread
    for as long as the statement ran — past the tool latency budget,
    before the fan-out even started. The probe's statement timeout is
    now the remaining call budget: Postgres cancels the stall at the
    deadline, the probe fails open (no leg skip is claimed), and the
    call returns inside the budget — the kinds honestly report their
    TimeoutError once the probe has eaten the clock."""
    import precis.store._chunks_ops as chunks_ops
    from precis.errors import Internal

    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 0.8)
    # The route gate read is replaced by an unconditional server-side
    # stall; a stale cached gate state would skip the SQL entirely.
    monkeypatch.setattr(
        chunks_ops, "_ANN_STATE_SQL", "SELECT pg_sleep(30) /* probe-stall-test */"
    )
    chunks_ops._reset_ann_state_cache()

    t0 = time.perf_counter()
    with pytest.raises(Internal, match="cross-kind search failed in every kind"):
        rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})
    elapsed = time.perf_counter() - t0

    assert elapsed < budget(5.0), f"stalled probe ate the call: {elapsed:.1f}s"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE query LIKE %s",
            ("%probe-stall-test%",),
        ).fetchone()
    assert row is not None and row[0] == 0, "stalled probe still running on the server"


def test_probe_with_no_remaining_budget_fails_open_without_the_pool(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe asked for after the deadline is already gone fails open
    without touching the pool — the fan-out keeps whatever is left.
    (Unbounded ``None`` timeouts recorded here belong to other threads'
    checkouts — only a bounded one could be this probe's.)"""
    rt = runtime_with_store
    checkouts: list[float | None] = []
    orig = PrecisPool.connection

    @contextmanager
    def spy(self: Any, timeout: float | None = None) -> Iterator[Any]:
        checkouts.append(timeout)
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", spy)

    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() - 0.01)

    assert out == []
    assert all(t is None for t in checkouts)


def test_probe_checkout_is_bounded_by_the_remaining_budget(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's pool checkout waits at most the remaining call budget
    (td649559: an unbounded checkout under pool contention could spend
    the whole tool budget before the fan-out began). Unbounded ``None``
    checkouts belong to other threads and are ignored."""
    rt = runtime_with_store
    checkouts: list[float | None] = []
    orig = PrecisPool.connection

    @contextmanager
    def spy(self: Any, timeout: float | None = None) -> Iterator[Any]:
        checkouts.append(timeout)
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", spy)

    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() + 5.0)

    # The test DB builds no partial indexes — memory's ANN leg routes
    # over the global index, so the probe names it.
    assert out == ["memory"]
    bounded = [t for t in checkouts if t is not None]
    assert bounded, "the probe should check out a connection with a bound"
    assert all(0 < t <= 5.0 for t in bounded)


def test_contended_probe_checkout_cannot_starve_the_fan_out(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664970: the probe shares the call deadline but may not spend
    it. A checkout waiting on a contended pool used to wait the ENTIRE
    remaining budget (``timeout=remaining_s``): the fan-out began with
    ~0s left, every kind came back TimeoutError and the whole call
    raised Internal. Its slice is now capped — over budget it fails
    open (no leg skip is claimed) and the kinds keep the deadline."""
    from psycopg_pool import PoolTimeout

    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen = _record_invoke(monkeypatch, rt)
    monkeypatch.setattr(search_mod, "CROSS_KIND_PROBE_BUDGET_S", 0.05)
    checkouts: list[float | None] = []
    orig = PrecisPool.connection

    @contextmanager
    def contended(self: Any, timeout: float | None = None) -> Iterator[Any]:
        # Honour the caller's bound the way psycopg's pool does — wait
        # that long, then report exhaustion. Unbounded checkouts belong
        # to other threads and are left alone.
        checkouts.append(timeout)
        if timeout is not None:
            time.sleep(timeout)
            raise PoolTimeout("simulated pool contention")
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", contended)

    t0 = time.monotonic()
    out = rt._dispatch_cross_kind("*", {"q": "gr664970uniquemarker"})
    elapsed = time.monotonic() - t0

    # Before the cap the probe ate the whole deadline and the call
    # raised Internal; now it spends only its slice, fails open, and
    # the fan-out still runs.
    assert seen, "the fan-out must still run when the probe can't answer"
    assert elapsed < budget(5.0)
    assert "semantic leg skipped for" not in out.body
    assert any(t is not None and 0 < t <= 0.05 for t in checkouts)


def test_checkout_wait_leaves_only_the_rest_of_the_slice_for_reads(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664970 review: the probe's slice is ONE absolute deadline, not
    a per-step budget. Checkout and EACH statement used to get the
    whole slice — a contended checkout followed by a stalled gate read
    could spend ~2x it (and a second stalled read ~3x). Now the cap is
    what the slice has LEFT after the checkout: a pg_sleep gate read
    after a waited checkout dies at the deadline, not a full slice
    later."""
    import psycopg

    import precis.store._chunks_ops as chunks_ops

    rt = runtime_with_store
    monkeypatch.setattr(search_mod, "CROSS_KIND_PROBE_BUDGET_S", 1.0)
    stmt_caps: list[int] = []
    orig_execute = psycopg.Connection.execute

    def spy_execute(
        self: Any, query: Any, params: Any = None, *a: Any, **kw: Any
    ) -> Any:
        if isinstance(query, str) and "set_config" in query and params:
            stmt_caps.append(int(params[0]))
        return orig_execute(self, query, params, *a, **kw)

    monkeypatch.setattr(psycopg.Connection, "execute", spy_execute)

    orig = PrecisPool.connection

    @contextmanager
    def waited_checkout(self: Any, timeout: float | None = None) -> Iterator[Any]:
        # A contended pool spends part of the slice before the probe
        # sees a connection — honouring the caller's bound like psycopg.
        # Unbounded checkouts belong to other threads and are left alone.
        if timeout is not None:
            time.sleep(min(timeout, 0.4))
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", waited_checkout)
    monkeypatch.setattr(
        chunks_ops,
        "_ANN_STATE_SQL",
        "SELECT pg_sleep(30) /* probe-stall-after-wait */",
    )
    chunks_ops._reset_ann_state_cache()

    t0 = time.monotonic()
    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() + 30.0)
    elapsed = time.monotonic() - t0

    assert out == [], "a probe that can't answer inside its slice fails open"
    # The statement cap is the ~600ms left after the 0.4s checkout wait —
    # the pre-review code armed the whole 1000ms slice again.
    assert stmt_caps and 0 < stmt_caps[0] <= 650
    # pg_sleep(30) died at the deadline (~0.4 wait + ~0.6 left), not a
    # full slice after the wait (~1.4s pre-review). The floor proves the
    # stall ran — an invalid stall SQL would fail open in ~0.4s.
    assert 0.9 <= elapsed < budget(1.3)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE query LIKE %s",
            ("%probe-stall-after-wait%",),
        ).fetchone()
    assert row is not None and row[0] == 0, "stalled read still running"


def test_second_gate_read_cannot_spend_the_slice_again(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr664970 review: the marker-table SELECT is a SECOND statement
    after the gate read — ``statement_timeout`` is per-statement, so a
    cap set once lets read2 spend a whole new slice past the deadline.
    The watchdog cancels whatever is in flight when the deadline lands:
    a fast first read followed by a stalled second still dies at the
    deadline, not a stale cap later."""
    import precis.store._chunks_ops as chunks_ops

    rt = runtime_with_store
    monkeypatch.setattr(search_mod, "CROSS_KIND_PROBE_BUDGET_S", 1.0)
    # Read 1 answers fast-but-slow (0.6s of the 1.0s slice) with the
    # marker table reachable, so read 2 — the marker SELECT — stalls.
    monkeypatch.setattr(
        chunks_ops,
        "_ANN_STATE_SQL",
        "SELECT pg_sleep(0.6) IS NULL, '{}'::jsonb, true",
    )
    monkeypatch.setattr(
        chunks_ops,
        "_ANN_KIND_READY_TABLE",
        "(SELECT pg_sleep(30) AS kind) s",
    )
    chunks_ops._reset_ann_state_cache()

    t0 = time.monotonic()
    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() + 30.0)
    elapsed = time.monotonic() - t0

    assert out == [], "a probe that can't answer inside its slice fails open"
    # Read 2 starts at ~0.6s with only ~0.4s of slice left; the watchdog
    # kills it at the 1.0s deadline — a once-set cap would let it run
    # to ~1.6s (pre-review: the whole 1.0s slice again). The floor proves
    # read 1's pg_sleep actually ran — an invalid stall SQL would fail
    # open in milliseconds.
    assert 0.9 <= elapsed < budget(1.3)


def test_fired_watchdog_is_reaped_before_the_conn_returns(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr664970 review: ``Timer.cancel()`` stops only an un-started
    callback — a watchdog that already fired kept running, and its
    ``conn.cancel_safe()`` could land AFTER the checkout ended, on
    whatever the next borrower runs on the connection. The probe now
    joins the watchdog first: a cancel held inside the callback must
    pin the connection, not trail it back to the pool."""
    import threading

    import psycopg

    import precis.store._chunks_ops as chunks_ops

    rt = runtime_with_store
    monkeypatch.setattr(search_mod, "CROSS_KIND_PROBE_BUDGET_S", 0.2)

    cancel_entered = threading.Event()
    release_cancel = threading.Event()
    probe_conns: list[Any] = []

    def held_cancel(self: Any, **kwargs: Any) -> None:
        probe_conns.append(self)
        cancel_entered.set()
        release_cancel.wait(10)

    monkeypatch.setattr(psycopg.Connection, "cancel_safe", held_cancel)

    def past_deadline_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Still running when the 0.2 s slice ends — the watchdog fires
        # deterministically while the probe holds the connection.
        time.sleep(0.4)
        return {"route": "local", "kind": None}

    monkeypatch.setattr(chunks_ops, "ann_route_plan", past_deadline_plan)

    returned: list[Any] = []
    orig = PrecisPool.connection

    @contextmanager
    def spy(self: Any, timeout: float | None = None) -> Iterator[Any]:
        with orig(self, timeout=timeout) as conn:
            yield conn
        returned.append(conn)

    monkeypatch.setattr(PrecisPool, "connection", spy)

    done = threading.Event()

    def run() -> None:
        rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() + 30.0)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    assert cancel_entered.wait(5), "watchdog never fired"
    probe_conn = probe_conns[0]
    # The cancel is still in flight — pre-fix the checkout ended here
    # anyway, and the pending cancel would eat the next borrower's work.
    assert all(c is not probe_conn for c in returned)
    assert not done.wait(0.5), "probe returned while its cancel was held"
    release_cancel.set()
    assert done.wait(5)
    t.join(5)
    assert any(c is probe_conn for c in returned)
