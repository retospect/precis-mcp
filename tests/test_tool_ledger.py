"""Tests for the tool-call ledger (tool_ledger, migration 0133).

DB-backed. Tag every row with a uuid so assertions survive the shared
``precis_test`` DB (filter by the tag, never absolute counts).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from precis import tool_ledger
from precis.tool_ledger import ToolCallRecord


def _rec(**kw: Any) -> ToolCallRecord:
    base: dict[str, Any] = {
        "verb": "get",
        "kind": "memory",
        "input_keys": ["kind", "id"],
        "outcome": "ok",
    }
    base.update(kw)
    return ToolCallRecord(**base)


def test_record_call_noop_without_store() -> None:
    tool_ledger.record_call(None, _rec())  # must not raise


def test_record_call_writes_row(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"get-{tag}"
    tool_ledger.record_call(
        store,
        _rec(verb=verb, kind="memory", input_keys=["kind", "id"]),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, outcome, input_keys FROM tool_calls WHERE verb=%s",
            (verb,),
        ).fetchone()
    assert row is not None
    assert row[0] == "memory"
    assert row[1] == "ok"
    assert sorted(row[2]) == ["id", "kind"]


def test_record_call_never_stores_payload_values(store: Any) -> None:
    # A distinctive value passed as an argument must NEVER land in any
    # column — only the argument NAME goes into input_keys.
    tag = uuid4().hex[:8]
    verb = f"put-{tag}"
    secret_value = f"super-secret-payload-{tag}"
    tool_ledger.record_call(
        store,
        _rec(
            verb=verb,
            kind="memory",
            input_keys=["kind", "text"],  # names only — never the value below
            error_type=None,
        ),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT verb, kind, input_keys, outcome, error_type FROM tool_calls "
            "WHERE verb=%s",
            (verb,),
        ).fetchone()
        # Also scan the whole row cast to text — belt & suspenders against
        # any future column accidentally carrying prose.
        row_text = conn.execute(
            "SELECT tool_calls::text FROM tool_calls WHERE verb=%s", (verb,)
        ).fetchone()
    assert row is not None
    assert secret_value not in str(row)
    assert secret_value not in row_text[0]
    assert sorted(row[2]) == ["kind", "text"]


def test_record_call_swallows_write_errors() -> None:
    class _BadStore:
        @property
        def pool(self) -> Any:
            raise RuntimeError("db down")

    tool_ledger.record_call(cast(Any, _BadStore()), _rec())  # must not raise


def test_record_call_persists_agentlog_source_profile(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"search-{tag}"
    tool_ledger.record_call(
        store,
        _rec(
            verb=verb,
            agentlog_id=424242,
            source="sonnet",
            profile="typed",
            latency_ms=17,
            result_count=3,
        ),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT agentlog_id, source, profile, latency_ms, result_count "
            "FROM tool_calls WHERE verb=%s",
            (verb,),
        ).fetchone()
    assert row == (424242, "sonnet", "typed", 17, 3)


def test_record_call_persists_error_outcome(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"edit-{tag}"
    tool_ledger.record_call(
        store,
        _rec(verb=verb, outcome="error", error_type="BadInput"),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT outcome, error_type FROM tool_calls WHERE verb=%s",
            (verb,),
        ).fetchone()
    assert row == ("error", "BadInput")


def test_gc_prunes_aged_rows(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"gc-{tag}"
    tool_ledger.record_call(store, _rec(verb=verb))
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE tool_calls SET ts = now() - interval '100 days' WHERE verb=%s",
            (verb,),
        )
        conn.commit()
    deleted = tool_ledger.gc(store, retention_days=30)
    assert deleted >= 1
    with store.pool.connection() as conn:
        n_rows = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE verb=%s", (verb,)
        ).fetchone()[0]
    assert n_rows == 0


def test_gc_leaves_fresh_rows(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"gcfresh-{tag}"
    tool_ledger.record_call(store, _rec(verb=verb))
    tool_ledger.gc(store, retention_days=30)
    with store.pool.connection() as conn:
        n_rows = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE verb=%s", (verb,)
        ).fetchone()[0]
    assert n_rows == 1


def test_gc_single_flight_skips_when_lock_held(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"gclock-{tag}"
    tool_ledger.record_call(store, _rec(verb=verb))
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE tool_calls SET ts = now() - interval '100 days' WHERE verb=%s",
            (verb,),
        )
        conn.commit()
    with store.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (tool_ledger._GC_LOCK,))
        holder.commit()
        assert tool_ledger.gc(store, retention_days=30) == 0  # locked out
        with store.pool.connection() as check:
            still = check.execute(
                "SELECT count(*) FROM tool_calls WHERE verb=%s", (verb,)
            ).fetchone()[0]
        assert still == 1
        holder.execute("SELECT pg_advisory_unlock(%s)", (tool_ledger._GC_LOCK,))
        holder.commit()
    assert tool_ledger.gc(store, retention_days=30) >= 1
