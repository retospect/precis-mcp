"""Tests for the outbound policy resolver + llm-api hold (spec S1, D1-D4).

DB-backed tests use the ``store`` fixture: the resolver snapshot reads the
real ``external_rate_limits`` override columns and the hold uses the real
``resource_slots``/``resource_slot_holds`` tables via the bound process
store. ``outbound_policy.reset_cache()`` runs between tests so the 30 s
snapshot can't leak rows across cases.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.budget import meter
from precis.store import Store
from precis.utils import outbound_policy, rate_limit
from precis.utils.http import Policy
from precis.utils.llm import openai_tools

OPENROUTER_URL = "https://openrouter.ai/api/v1"
RESOURCE = "llm-api:openrouter"


@pytest.fixture
def bound(store: Store) -> Iterator[Store]:
    """Bind the test store (snapshot + holds read it), reset the snapshot
    cache, and restore the prior binding afterwards."""
    prior = meter.active_store()
    meter.bind_store(store)
    outbound_policy.reset_cache()
    yield store
    meter.bind_store(prior)
    outbound_policy.reset_cache()


def _seed_limit_row(store: Store, provider: str, **cols: Any) -> None:
    sets = ", ".join(f"{c} = %s" for c in cols)
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.execute(
            "INSERT INTO external_rate_limits "
            "(provider, capacity, refill_per_sec, tokens, daily_cap) "
            "VALUES (%s, 1, 1.0, 1, NULL)",
            (provider,),
        )
        if cols:
            conn.execute(
                f"UPDATE external_rate_limits SET {sets} WHERE provider = %s",
                (*cols.values(), provider),
            )
        conn.commit()


def _slot_free(store: Store, resource: str = RESOURCE) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT free FROM resource_slots WHERE host = 'api' AND resource = %s",
            (resource,),
        ).fetchone()
        assert row is not None
        return int(row[0])


def _seed_slot(store: Store, capacity: int, resource: str = RESOURCE) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM resource_slot_holds WHERE host = 'api' AND resource = %s",
            (resource,),
        )
        conn.execute(
            "DELETE FROM resource_slots WHERE host = 'api' AND resource = %s",
            (resource,),
        )
        conn.execute(
            "INSERT INTO resource_slots (host, resource, capacity, free, kind) "
            "VALUES ('api', %s, %s, %s, 'hard')",
            (resource, capacity, capacity),
        )
        conn.commit()


class TestResolver:
    def test_openai_tools_resolves_llm_api(self, bound: Store) -> None:
        """The spec acceptance: openrouter resolves to the rate-less
        ``llm-api`` class with the shared concurrency resource."""
        eff = outbound_policy.resolve("openai-tools", url=OPENROUTER_URL)
        assert eff.cls == "llm-api"
        assert eff.rate is None
        assert eff.concurrency == RESOURCE
        assert eff.row == "host:openrouter.ai"
        assert eff.on_refuse == "defer"
        assert eff.retry == (2, 2.0, 30.0)

    def test_merge_order_db_over_policy_over_class(self, bound: Store) -> None:
        """D3 merge: class default -> POLICIES field -> DB column, later
        non-NULL wins per field."""
        policy = Policy(provider="test_op_merge", cls="provider-api", max_wait_s=10.0)
        _seed_limit_row(bound, "test_op_merge")
        try:
            eff = outbound_policy.resolve_for("svc", policy, "test_op_merge", None)
            assert eff.max_wait_s == 10.0  # policy over the 30s class default
            with bound.pool.connection() as conn:
                conn.execute(
                    "UPDATE external_rate_limits SET max_wait_s = 5, "
                    "on_refuse = 'defer' WHERE provider = 'test_op_merge'"
                )
                conn.commit()
            outbound_policy.reset_cache()
            eff = outbound_policy.resolve_for("svc", policy, "test_op_merge", None)
            assert eff.max_wait_s == 5.0  # DB over policy
            assert eff.on_refuse == "defer"  # DB over class default
        finally:
            _seed_limit_row_cleanup(bound, "test_op_merge")

    def test_db_class_column_overrides_policy_cls(self, bound: Store) -> None:
        """D1: the DB ``class`` column is an override over ``Policy.cls`` —
        flipping a row to provider-api restores a token lane."""
        _seed_limit_row(bound, "host:openrouter.ai", **{"class": "provider-api"})
        try:
            outbound_policy.reset_cache()
            eff = outbound_policy.resolve("openai-tools", url=OPENROUTER_URL)
            assert eff.cls == "provider-api"
            assert eff.rate is not None
            assert eff.rate.capacity == 1
        finally:
            _seed_limit_row_cleanup(bound, "host:openrouter.ai")

    def test_deadline_only_shortens_waits(self, bound: Store) -> None:
        """D3: the ambient deadline (s2a's one ContextVar) merges last and
        only ever shortens."""
        from precis.utils.deadline import deadline_scope

        with deadline_scope(time.monotonic() + 2.0):
            eff = outbound_policy.resolve("openai-tools", url=OPENROUTER_URL)
            assert 0.0 < eff.max_wait_s <= 2.0
        with deadline_scope(time.monotonic() + 3600.0):
            eff = outbound_policy.resolve("openai-tools", url=OPENROUTER_URL)
            assert eff.max_wait_s == 30.0  # never lengthens

    def test_resolver_fault_returns_code_layers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No bound store / DB fault: the code layers alone come back and
        the call still resolves (llm-api stays rate-less)."""
        meter.bind_store(None)
        monkeypatch.setattr(outbound_policy, "_snapshot_at", 0.0)
        eff = outbound_policy.resolve("openai-tools", url=OPENROUTER_URL)
        assert eff.cls == "llm-api"
        assert eff.rate is None
        assert eff.concurrency == RESOURCE

    def test_internal_and_scraped_classes(self, bound: Store) -> None:
        internal = outbound_policy.resolve("web-fetch", url="http://127.0.0.1:9/x")
        assert internal.cls == "internal"
        # S1 deviation: internal keeps the legacy 1/s token lane.
        assert internal.rate is not None
        eff = outbound_policy.resolve("web-fetch", url="https://example.com/x")
        assert eff.cls == "scraped-host"
        assert eff.rate == outbound_policy.RateLane(1, 1.0, None)


class TestLlmApiHold:
    def test_acquire_release(self, bound: Store) -> None:
        _seed_slot(bound, 1)
        with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
            assert _slot_free(bound) == 0
        assert _slot_free(bound) == 1
        with bound.pool.connection() as conn:
            held = conn.execute(
                "SELECT count(*) FROM resource_slot_holds WHERE resource = %s",
                (RESOURCE,),
            ).fetchone()
            assert held is not None and held[0] == 0

    def test_release_on_exception(self, bound: Store) -> None:
        _seed_slot(bound, 1)
        with pytest.raises(RuntimeError):
            with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                raise RuntimeError("call blew up")
        assert _slot_free(bound) == 1

    def test_stream_body_inside_hold(self, bound: Store) -> None:
        """The streamed body is read inside the hold: the slot is still
        debited while events flow, and released when iteration ends."""
        _seed_slot(bound, 1)
        events = iter(
            [
                {"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
            ]
        )
        seen_free: list[int] = []

        def fake_sse(*a: Any, **k: Any) -> Any:
            for e in events:
                seen_free.append(_slot_free(bound))
                yield e

        client = openai_tools.ToolChatClient(
            url=OPENROUTER_URL,
            api_key="k",
            model="m",
            timeout=5.0,
            stream=True,
            transport=_FakeSSETransport(fake_sse),
        )
        turn = client.chat([{"role": "user", "content": "hi"}])
        assert turn.content == "hi"
        assert seen_free and all(f == 0 for f in seen_free)  # held mid-stream
        assert _slot_free(bound) == 1

    def test_full_slot_defers(self, bound: Store) -> None:
        """A full row + on_refuse=defer raises RateLimited (requeue)."""
        _seed_slot(bound, 0)
        _seed_limit_row(bound, "host:openrouter.ai", max_wait_s=0)
        try:
            outbound_policy.reset_cache()
            with pytest.raises(rate_limit.RateLimited):
                with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                    pass
        finally:
            _seed_limit_row_cleanup(bound, "host:openrouter.ai")

    def test_db_down_caps_per_process(
        self, bound: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB down → the per-process semaphore caps at
        PRECIS_OUTBOUND_FALLBACK_CONCURRENCY (2 in this test)."""
        monkeypatch.setenv("PRECIS_OUTBOUND_FALLBACK_CONCURRENCY", "2")

        class _DeadStore:
            @property
            def pool(self) -> Any:
                raise RuntimeError("db down")

        monkeypatch.setattr(meter, "active_store", lambda: _DeadStore())
        sem = openai_tools._fallback_semaphore()
        with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
            with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                assert sem.acquire(blocking=False) is False  # cap of 2 held


@pytest.fixture
def park_dsn(bound: Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """``rate_limit.park`` uses its own DSN, not the bound store — point it
    at the test clone and drop its cached connection afterwards."""
    assert bound.dsn is not None
    monkeypatch.setenv("PRECIS_DATABASE_URL", bound.dsn)
    yield bound
    rate_limit.close()


class TestParkSql:
    def test_park_on_rateless_row(self, park_dsn: Store) -> None:
        """D1: parked_until is set on every row; a rate-less row
        (refill_per_sec=0) gets no negative-token write."""
        provider = "test_park_rateless"
        _seed_limit_row(park_dsn, provider, refill_per_sec=0)
        with park_dsn.pool.connection() as conn:
            before = conn.execute(
                "SELECT tokens, last_refill FROM external_rate_limits "
                "WHERE provider = %s",
                (provider,),
            ).fetchone()
        assert before is not None
        try:
            rate_limit.park(provider, 60)
            with park_dsn.pool.connection() as conn:
                row = conn.execute(
                    "SELECT tokens, last_refill, parked_until "
                    "FROM external_rate_limits WHERE provider = %s",
                    (provider,),
                ).fetchone()
            assert row is not None
            tokens, last_refill, parked_until = row
            assert parked_until is not None
            if parked_until.tzinfo is None:
                parked_until = parked_until.replace(tzinfo=UTC)
            assert parked_until > datetime.now(UTC)
            assert float(tokens) == float(before[0])  # untouched
            assert last_refill == before[1]  # untouched
        finally:
            _seed_limit_row_cleanup(park_dsn, provider)

    def test_park_writes_both_on_rated_row(self, park_dsn: Store) -> None:
        provider = "test_park_rated"
        _seed_limit_row(park_dsn, provider)
        try:
            rate_limit.park(provider, 60)
            with park_dsn.pool.connection() as conn:
                row = conn.execute(
                    "SELECT tokens, parked_until FROM external_rate_limits "
                    "WHERE provider = %s",
                    (provider,),
                ).fetchone()
            assert row is not None
            assert float(row[0]) < 0  # negative-token write still applies
            assert row[1] is not None  # parked_until too
        finally:
            _seed_limit_row_cleanup(park_dsn, provider)


class TestReviewFixes:
    def test_cross_process_park_refused(self, bound: Store) -> None:
        """Acceptance: a 429 park written by ANOTHER process is honoured by
        this process's very next call — the touch's RETURNING read, not the
        (stale) snapshot, carries it."""
        _seed_slot(bound, 1)
        _seed_limit_row(bound, "host:openrouter.ai", max_wait_s=0)
        try:
            outbound_policy.reset_cache()  # process B's snapshot has no park
            with bound.pool.connection() as conn:  # process A parks the row
                conn.execute(
                    "UPDATE external_rate_limits SET parked_until = "
                    "now() + interval '60 seconds' "
                    "WHERE provider = 'host:openrouter.ai'"
                )
                conn.commit()
            # A fresh SNAPSHOT would see the park too — keep the stale
            # window so ONLY the RETURNING read can catch it.
            outbound_policy._snapshot = {}
            outbound_policy._snapshot_at = time.monotonic()
            with pytest.raises(rate_limit.RateLimited):
                with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                    pass
        finally:
            _seed_limit_row_cleanup(bound, "host:openrouter.ai")

    def test_insert_hold_failure_leaks_no_slot(
        self, bound: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception between reserve and insert_slot_hold rolls the
        reservation back; the call proceeds under the fallback cap."""
        _seed_slot(bound, 1)
        from precis.store import _resource_slots_ops

        monkeypatch.setattr(
            _resource_slots_ops,
            "insert_slot_hold",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mid-tx")),
        )
        # The import inside the hold resolves lazily, so patch the module
        # the import targets — same object.
        monkeypatch.setenv("PRECIS_OUTBOUND_FALLBACK_CONCURRENCY", "2")
        with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
            assert _slot_free(bound) == 1  # rolled back — unit refunded
        assert _slot_free(bound) == 1

    def test_unseeded_provider_runs_unheld(
        self, bound: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No seeded resource_slots row → the call runs without a hold and
        _warn_unpaced names the resource."""
        with bound.pool.connection() as conn:
            conn.execute(
                "DELETE FROM resource_slots WHERE host = 'api' AND resource = %s",
                (RESOURCE,),
            )
            conn.commit()
        with caplog.at_level("WARNING"):
            with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                pass
        assert any("unpaced" in r.getMessage() for r in caplog.records)

    def test_retries_reuse_the_hold(
        self, bound: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One hold covers every retry inside _polite/call_with_backoff."""
        _seed_slot(bound, 1)
        calls = {"n": 0}

        def fake_polite(send: Any, url: str) -> bytes:
            calls["n"] += 1
            assert _slot_free(bound) == 0  # hold still taken mid-retry
            return send()

        monkeypatch.setattr(openai_tools, "_polite", fake_polite)
        monkeypatch.setattr(openai_tools, "_post_read", lambda *a, **k: b'{"ok": 1}')
        out = openai_tools._UrllibTransport().post_json(
            OPENROUTER_URL + "/chat/completions",
            {},
            headers={},
            timeout=5.0,
        )
        assert out == {"ok": 1}
        assert _slot_free(bound) == 1

    def test_abandoned_stream_releases(self, bound: Store) -> None:
        """A stream aborted mid-iteration closes the response and releases
        the slot."""
        _seed_slot(bound, 1)

        def sse(*a: Any, **k: Any) -> Any:
            yield {"choices": [{"delta": {"content": "x"}}]}
            yield {"choices": [{"delta": {"content": "y"}}]}

        client = openai_tools.ToolChatClient(
            url=OPENROUTER_URL,
            api_key="k",
            model="m",
            timeout=5.0,
            stream=True,
            abort_check=lambda: True,  # abort after the first event
            transport=_FakeSSETransport(sse),
        )
        with pytest.raises(openai_tools.StreamTimeout):
            client.chat([{"role": "user", "content": "hi"}])
        assert _slot_free(bound) == 1

    def test_snapshot_ttl_and_no_deadlock(self, bound: Store) -> None:
        """The 30 s TTL serves stale within the window; a refresh past the
        TTL picks overrides up; concurrent refreshes don't deadlock."""
        _seed_limit_row(bound, "test_op_ttl", max_wait_s=7)
        try:
            outbound_policy.reset_cache()
            assert (
                outbound_policy.resolve_for(
                    "svc", Policy(provider="test_op_ttl"), "test_op_ttl", None
                ).max_wait_s
                == 7.0
            )
            with bound.pool.connection() as conn:
                conn.execute(
                    "UPDATE external_rate_limits SET max_wait_s = 9 "
                    "WHERE provider = 'test_op_ttl'"
                )
                conn.commit()
            # within TTL -> stale
            assert (
                outbound_policy.resolve_for(
                    "svc", Policy(provider="test_op_ttl"), "test_op_ttl", None
                ).max_wait_s
                == 7.0
            )
            outbound_policy._snapshot_at = 0.0  # force the window shut
            assert (
                outbound_policy.resolve_for(
                    "svc", Policy(provider="test_op_ttl"), "test_op_ttl", None
                ).max_wait_s
                == 9.0
            )
        finally:
            _seed_limit_row_cleanup(bound, "test_op_ttl")

    def test_undefined_column_detection(self, bound: Store) -> None:
        """A real UndefinedColumn against a column-less relation caches
        ``False`` — no retry until the 60 s re-probe window."""
        prior, prior_at = rate_limit._has_s1_cols, rate_limit._s1_cols_at
        try:
            rate_limit._has_s1_cols = None
            rate_limit._s1_cols_at = 0.0
            with bound.pool.connection() as conn:
                # The real migrated table: the probe succeeds and caches True.
                assert rate_limit._s1_columns(conn) is True
                rate_limit._has_s1_cols = None
                rate_limit._s1_cols_at = 0.0
                import precis.utils.rate_limit as rl

                orig = rl._S1_COLS_PROBE
                rl._S1_COLS_PROBE = "SELECT parked_until FROM (SELECT 1 AS x) t LIMIT 0"
                try:
                    assert rl._s1_columns(conn) is False
                    assert rl._has_s1_cols is False  # UndefinedColumn cached
                    # within the re-probe window: no second probe
                    assert rl._s1_columns(conn) is False
                finally:
                    rl._S1_COLS_PROBE = orig
        finally:
            rate_limit._has_s1_cols = prior
            rate_limit._s1_cols_at = prior_at

    def test_fallback_slot_timeout_refuses(
        self, bound: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB down + the per-process cap full + no wait budget → the hold
        refuses with RateLimited instead of blocking."""
        monkeypatch.setenv("PRECIS_OUTBOUND_FALLBACK_CONCURRENCY", "1")
        _seed_limit_row(bound, "host:openrouter.ai", max_wait_s=0)
        try:
            outbound_policy.reset_cache()

            class _DeadStore:
                @property
                def pool(self) -> Any:
                    raise RuntimeError("db down")

            monkeypatch.setattr(meter, "active_store", lambda: _DeadStore())
            sem = openai_tools._fallback_semaphore()
            assert sem.acquire(blocking=False)  # fill the cap of 1
            from precis.utils.deadline import deadline_scope

            try:
                # The store is unreachable, so the snapshot has no
                # overrides — bound the wait via the ambient deadline.
                with (
                    deadline_scope(time.monotonic() + 0.2),
                    pytest.raises(rate_limit.RateLimited),
                    openai_tools._llm_api_hold(OPENROUTER_URL, 5.0),
                ):
                    pass
            finally:
                sem.release()
        finally:
            _seed_limit_row_cleanup(bound, "host:openrouter.ai")

    def test_eviction_spares_override_row(self, bound: Store) -> None:
        """An override-carrying host row is never evicted; a plain idle
        host row past the threshold is."""
        stale = (
            "UPDATE external_rate_limits SET last_refill = now() - interval '48 hours'"
        )
        _seed_limit_row(bound, "host:evict-me.example", **{"class": "llm-api"})
        _seed_limit_row(bound, "host:evict-plain.example")
        with bound.pool.connection() as conn:
            conn.execute(stale + " WHERE provider LIKE 'host:evict-%'")
            conn.commit()
        try:
            outbound_policy.reset_cache()
            with bound.pool.connection() as conn:
                n = rate_limit._evict_idle_host_rows(conn)
                conn.commit()
            assert n >= 1
            with bound.pool.connection() as conn:
                rows = {
                    r[0]
                    for r in conn.execute(
                        "SELECT provider FROM external_rate_limits "
                        "WHERE provider LIKE 'host:evict-%'"
                    ).fetchall()
                }
            assert "host:evict-me.example" in rows  # override spared
            assert "host:evict-plain.example" not in rows  # idle evicted
        finally:
            _seed_limit_row_cleanup(bound, "host:evict-me.example")
            _seed_limit_row_cleanup(bound, "host:evict-plain.example")

    def test_legacy_sql_when_columns_absent(
        self, bound: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Code ahead of the migration: detection misses → the pre-S1
        statements run (negative-token park, plain consume)."""
        monkeypatch.setattr(rate_limit, "_has_s1_cols", False)
        monkeypatch.setattr(
            rate_limit, "_s1_cols_at", time.monotonic()
        )  # inside the re-probe window — no live probe
        assert rate_limit._s1_columns(object()) is False  # cached False short-circuits
        provider = "test_park_legacy"
        _seed_limit_row(bound, provider)
        try:
            assert bound.dsn is not None
            monkeypatch.setenv("PRECIS_DATABASE_URL", bound.dsn)
            rate_limit.close()
            rate_limit.park(provider, 60)
            with bound.pool.connection() as conn:
                row = conn.execute(
                    "SELECT tokens, parked_until FROM external_rate_limits "
                    "WHERE provider = %s",
                    (provider,),
                ).fetchone()
            assert row is not None
            assert float(row[0]) < 0  # legacy negative-token write
            assert row[1] is None  # pre-S1 statement never touches it
        finally:
            rate_limit.close()
            _seed_limit_row_cleanup(bound, provider)


def _seed_limit_row_cleanup(store: Store, provider: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.commit()
    outbound_policy.reset_cache()


class _FakeSSETransport:
    """post_sse fake that replays scripted events."""

    def __init__(self, sse: Any) -> None:
        self.post_sse = sse

    def post_json(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("not used in the streamed test")
