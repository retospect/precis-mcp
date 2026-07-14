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
