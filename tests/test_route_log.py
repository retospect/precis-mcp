"""Tests for the full LLM interaction log (route_log, migration 0061).

DB-backed. Tag every row with a uuid so assertions survive the shared
``precis_test`` DB (filter by the tag, never absolute counts).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from precis import route_log
from precis.route_log import LlmCallRecord


def _rec(**kw: Any) -> LlmCallRecord:
    base: dict[str, Any] = {
        "source": "dream",
        "tier": "cloud-super",
        "transport": "claude_agent",
        "model": "claude-opus-4-8",
        "tools_needed": True,
        "request_text": "REQ",
        "response_text": "RESP",
        "cost_usd": 0.5,
        "turns_used": 3,
        "duration_ms": 1200,
        "errored": False,
        "error": None,
        "data_parsed": None,
        "features": {"prompt_chars": 3},
    }
    base.update(kw)
    return LlmCallRecord(**base)


def test_record_call_noop_without_store() -> None:
    # Unbound + no explicit store → a silent no-op, never raises.
    route_log.bind_store(None)
    assert route_log.enabled() is False
    route_log.record_call(_rec())  # must not raise


def test_record_call_writes_row_and_dedups_blobs(store: Any) -> None:
    tag = uuid4().hex[:8]
    src = f"test-{tag}"
    shared_req = f"REQ-{tag}"  # same request text on both calls → one blob
    route_log.record_call(
        _rec(source=src, request_text=shared_req, response_text=f"R1-{tag}"),
        store=store,
    )
    route_log.record_call(
        _rec(source=src, request_text=shared_req, response_text=f"R2-{tag}"),
        store=store,
    )
    with store.pool.connection() as conn:
        n_calls = conn.execute(
            "SELECT count(*) FROM llm_call_log WHERE source=%s", (src,)
        ).fetchone()[0]
        n_shared_blob = conn.execute(
            "SELECT count(*) FROM llm_blob WHERE text=%s", (shared_req,)
        ).fetchone()[0]
        row = conn.execute(
            "SELECT model, request_chars, tools_needed FROM llm_call_log "
            "WHERE source=%s ORDER BY id LIMIT 1",
            (src,),
        ).fetchone()
    assert n_calls == 2
    assert n_shared_blob == 1  # deduped — the big repeated request stored once
    assert row[0] == "claude-opus-4-8"
    assert row[1] == len(shared_req)
    assert row[2] is True


def test_record_call_persists_ref_id(store: Any) -> None:
    # gr162130: ref_id is stamped so a call is attributable to an entity, not
    # just a source pass. It cannot be back-filled — verify it round-trips.
    tag = uuid4().hex[:8]
    src = f"quest_tick-{tag}"
    route_log.record_call(_rec(source=src, ref_id=424242), store=store)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM llm_call_log WHERE source=%s", (src,)
        ).fetchone()
    assert row is not None and row[0] == 424242


def test_spend_rollup_groups_and_keeps_units_separate(store: Any) -> None:
    # The mining rollup buckets by a column and keeps natural units apart:
    # real_usd sums ONLY billed (non-null cost) rows; wall_ms sums duration;
    # a null-cost lane shows $0 (quota/local cost, not a gap).
    tag = uuid4().hex[:8]
    src = f"rollup-{tag}"
    # Two lanes under one tagged source: a billed cloud row + two null-cost OAuth
    # rows (one of which errored).
    route_log.record_call(
        _rec(source=src, transport="litellm", cost_usd=0.10, duration_ms=1000),
        store=store,
    )
    route_log.record_call(
        _rec(source=src, transport="claude_agent", cost_usd=None, duration_ms=2000),
        store=store,
    )
    route_log.record_call(
        _rec(
            source=src,
            transport="claude_agent",
            cost_usd=None,
            duration_ms=500,
            errored=True,
        ),
        store=store,
    )
    rows = {r.key: r for r in route_log.spend_rollup(store, days=1, source=src)}
    assert set(rows) == {"litellm", "claude_agent"}
    assert rows["litellm"].calls == 1
    assert abs(rows["litellm"].real_usd - 0.10) < 1e-9
    assert rows["litellm"].wall_ms == 1000
    # The OAuth lane: two calls, no real dollars, wall-clock summed, one error.
    assert rows["claude_agent"].calls == 2
    assert rows["claude_agent"].real_usd == 0.0
    assert rows["claude_agent"].wall_ms == 2500
    assert rows["claude_agent"].errors == 1


def test_lite_row_skips_blob_but_keeps_metadata(store: Any) -> None:
    # A lite row (store_blobs=False) records the mineable metadata — char counts,
    # cost, duration — but leaves the hashes NULL and stores NO replay blob.
    tag = uuid4().hex[:8]
    src = f"lite-{tag}"
    req_text = f"LITE-REQ-{tag}"
    route_log.record_call(
        _rec(
            source=src,
            request_text=req_text,
            response_text=f"LITE-RESP-{tag}",
            store_blobs=False,
        ),
        store=store,
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT request_hash, response_hash, request_chars FROM llm_call_log "
            "WHERE source=%s",
            (src,),
        ).fetchone()
        n_blob = conn.execute(
            "SELECT count(*) FROM llm_blob WHERE text=%s", (req_text,)
        ).fetchone()[0]
    assert row is not None
    assert row[0] is None and row[1] is None  # no blob linked
    assert row[2] == len(req_text)  # volume signal still recorded
    assert n_blob == 0  # the ~18 KB replay blob was NOT stored


def test_dispatch_lite_when_log_blobs_false(store: Any, monkeypatch: Any) -> None:
    import precis.utils.llm.router as router
    from precis.utils.claude_p import ClaudePResult
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    tag = uuid4().hex[:8]
    src = f"litedisp-{tag}"

    def fake_p(prompt: str, **kwargs: Any) -> ClaudePResult:
        return ClaudePResult(data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.0)

    monkeypatch.setattr(router, "call_claude_p", fake_p)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)

    route_log.bind_store(store)
    try:
        dispatch(
            LlmRequest(
                tier=Tier.CLOUD_SMALL, prompt="gloss this", source=src, log_blobs=False
            )
        )
    finally:
        route_log.bind_store(None)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT request_hash, request_chars FROM llm_call_log WHERE source=%s",
            (src,),
        ).fetchone()
    assert row is not None
    assert row[0] is None  # lite — no blob hash
    assert row[1] > 0  # chars still recorded


def test_gc_prunes_aged_rows_and_orphan_blobs(store: Any) -> None:
    # The sweeper-wired GC deletes rows past the retention window + their now-
    # orphaned blobs. Age a tagged row into the past, then GC with a 30d floor.
    tag = uuid4().hex[:8]
    src = f"gc-{tag}"
    old_req = f"GC-REQ-{tag}"
    route_log.record_call(
        _rec(source=src, request_text=old_req, response_text=f"GC-RESP-{tag}"),
        store=store,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE llm_call_log SET ts = now() - interval '100 days' WHERE source=%s",
            (src,),
        )
        conn.commit()
    deleted = route_log.gc(store, retention_days=30)
    assert deleted >= 1
    with store.pool.connection() as conn:
        n_rows = conn.execute(
            "SELECT count(*) FROM llm_call_log WHERE source=%s", (src,)
        ).fetchone()[0]
        n_blob = conn.execute(
            "SELECT count(*) FROM llm_blob WHERE text=%s", (old_req,)
        ).fetchone()[0]
    assert n_rows == 0  # aged row pruned
    assert n_blob == 0  # orphaned blob swept


def test_gc_single_flight_skips_when_lock_held(store: Any) -> None:
    # A concurrent worker holding the GC advisory lock makes gc() fast-fail
    # (return 0) instead of piling a second full blob sweep onto the DB — the
    # guard that stops the fleet from saturating the DB host. Age a row so a
    # lock-free run *would* delete >=1, proving the 0 comes from the lock.
    tag = uuid4().hex[:8]
    src = f"gclock-{tag}"
    route_log.record_call(_rec(source=src, request_text=f"L-{tag}"), store=store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE llm_call_log SET ts = now() - interval '100 days' WHERE source=%s",
            (src,),
        )
        conn.commit()
    # Hold the same advisory key session-scoped on a separate connection, so
    # gc()'s pg_try_advisory_xact_lock (a different session) fast-fails. Unlock
    # explicitly before returning the connection to the pool — a session lock
    # survives rollback and would otherwise leak into the next test.
    with store.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (route_log._GC_LOCK,))
        holder.commit()
        assert route_log.gc(store, retention_days=30) == 0  # locked out
        with store.pool.connection() as check:
            still = check.execute(
                "SELECT count(*) FROM llm_call_log WHERE source=%s", (src,)
            ).fetchone()[0]
        assert still == 1  # nothing deleted while locked
        holder.execute("SELECT pg_advisory_unlock(%s)", (route_log._GC_LOCK,))
        holder.commit()
    # Lock released → the next run reaps normally.
    assert route_log.gc(store, retention_days=30) >= 1


def test_spend_rollup_rejects_unknown_group_by(store: Any) -> None:
    import pytest

    with pytest.raises(ValueError):
        route_log.spend_rollup(store, group_by="prompt")


def test_record_call_swallows_write_errors() -> None:
    # A store whose connection blows up → the failure is swallowed (best-effort).
    class _BadStore:
        @property
        def pool(self) -> Any:
            raise RuntimeError("db down")

    route_log.record_call(_rec(), store=cast(Any, _BadStore()))  # must not raise


def test_dispatch_records_the_full_call(store: Any, monkeypatch: Any) -> None:
    import precis.utils.llm.router as router
    from precis.utils.claude_p import ClaudePResult
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    tag = uuid4().hex[:8]
    src = f"judge-{tag}"

    def fake_p(prompt: str, **kwargs: Any) -> ClaudePResult:
        return ClaudePResult(
            data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.02
        )

    monkeypatch.setattr(router, "call_claude_p", fake_p)
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)

    route_log.bind_store(store)
    try:
        out = dispatch(
            LlmRequest(tier=Tier.CLOUD_SMALL, prompt="judge this", source=src)
        )
    finally:
        route_log.bind_store(None)

    assert out.text == '{"ok": true}'
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT source, transport, model, data_parsed, errored, features "
            "FROM llm_call_log WHERE source=%s",
            (src,),
        ).fetchone()
        req = conn.execute(
            "SELECT b.text FROM llm_call_log l JOIN llm_blob b "
            "ON b.hash = l.request_hash WHERE l.source=%s",
            (src,),
        ).fetchone()
    assert row is not None
    assert row[0] == src
    assert row[1] == "claude_p"
    assert row[2] == "claude-haiku-4-5-20251001"  # resolved from the tier
    assert row[3] is True  # judge JSON parsed
    assert row[4] is False
    assert row[5]["source"] == src  # features captured
    assert "judge this" in req[0]  # the full request text is recorded
