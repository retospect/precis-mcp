"""Tests for the tool-call ledger's write path at the dispatch chokepoint
(``DispatchMixin.dispatch_with_status`` / ``_record_tool_call``, migration
0133) — as opposed to ``test_tool_ledger.py``, which tests the
``tool_ledger`` module's SQL directly.

DB-backed. Tag every row with a uuid so assertions survive the shared
``precis_test`` DB (filter by the tag, never absolute counts).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from precis.runtime import PrecisRuntime


def test_dispatch_writes_one_ledger_row(runtime_with_store: PrecisRuntime) -> None:
    tag = uuid4().hex[:8]
    title = f"tool-ledger-title-{tag}"
    body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": f"body-{tag}", "title": title}
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT verb, kind, outcome, input_keys, error_type, latency_ms "
            "FROM tool_calls WHERE kind='memory' AND ts > now() - interval '1 minute' "
            "AND input_keys @> '[\"text\"]' ORDER BY call_id DESC LIMIT 5"
        ).fetchall()
    # At least one row from this call landed; find it by input_keys shape
    # (kind/text/title) rather than assuming it's the very last row, since
    # sibling tests may write concurrently to the shared test DB.
    matches = [r for r in rows if sorted(r[3]) == ["kind", "text", "title"]]
    assert matches, f"no matching ledger row among {rows!r}"
    row = matches[0]
    assert row[0] == "put"
    assert row[1] == "memory"
    assert row[2] == "ok"
    assert row[4] is None
    assert row[5] is None or row[5] >= 0


def test_dispatch_ledger_drops_none_valued_keys(
    runtime_with_store: PrecisRuntime,
) -> None:
    """None-valued kwargs are wrapper defaults, not caller input.

    tools/core.py's put/edit/get pass EVERY declared kwarg defaulted to
    None, so recording bare key presence would log the same static
    ~70-key list for every put — useless for arg-shape mining. Only keys
    the caller actually set (non-None) may land in input_keys.
    """
    tag = uuid4().hex[:8]
    title = f"tool-ledger-none-{tag}"
    body, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "memory",
            "text": f"body-{tag}",
            "title": title,
            "id": None,
            "mode": None,
            "url": None,
        },
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT input_keys FROM tool_calls "
            "WHERE kind='memory' AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 10"
        ).fetchall()
    key_sets = [sorted(r[0]) for r in rows]
    assert ["kind", "text", "title"] in key_sets, key_sets
    # The pre-fix signature: this call's row carrying its None-padded kwargs.
    assert ["id", "kind", "mode", "text", "title", "url"] not in key_sets


def test_dispatch_ledger_never_leaks_payload_values(
    runtime_with_store: PrecisRuntime,
) -> None:
    tag = uuid4().hex[:8]
    secret_text = f"super-secret-body-value-{tag}"
    secret_title = f"super-secret-title-value-{tag}"
    _body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": secret_text, "title": secret_title}
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        # Scan the ENTIRE tool_calls table cast to text for the distinctive
        # secret strings — belt & suspenders against any column (now or
        # added later) accidentally carrying prose instead of key names.
        text_row = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE tool_calls::text LIKE %s",
            (f"%{secret_text}%",),
        ).fetchone()
        title_row = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE tool_calls::text LIKE %s",
            (f"%{secret_title}%",),
        ).fetchone()
    assert text_row is not None and title_row is not None
    hit_text = text_row[0]
    hit_title = title_row[0]
    assert hit_text == 0
    assert hit_title == 0


def test_dispatch_ledger_records_error_outcome(
    runtime_with_store: PrecisRuntime,
) -> None:
    tag = uuid4().hex[:8]
    # A bogus kind produces a BadInput/NotFound — is_error True.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": f"no-such-kind-{tag}", "id": "1"}
    )
    assert is_error is True

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT outcome, error_type FROM tool_calls "
            "WHERE kind=%s ORDER BY call_id DESC LIMIT 1",
            (f"no-such-kind-{tag}",),
        ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert row[1] is not None  # a PrecisError subclass name, e.g. NotFound/BadInput


def test_dispatch_survives_ledger_write_failure(
    runtime_with_store: PrecisRuntime, monkeypatch: Any
) -> None:
    # Fail-open: a raising ledger writer must never break the verb call
    # itself — the dispatch still succeeds and returns the normal body.
    import precis.tool_ledger as tool_ledger

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("ledger write exploded")

    monkeypatch.setattr(tool_ledger, "record_call", _boom)

    tag = uuid4().hex[:8]
    body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": f"resilient-{tag}", "title": f"t-{tag}"}
    )
    assert is_error is False
    assert isinstance(body, str) and body  # normal ack body, call was unaffected


def test_dispatch_ledger_noop_without_store(runtime: PrecisRuntime) -> None:
    # ``runtime`` (stateless, no store) must not raise when dispatching —
    # ``_record_tool_call`` no-ops cleanly when ``self.store`` is None.
    body, is_error = runtime.dispatch_with_status("get", {"kind": "skill", "id": "toc"})
    assert isinstance(body, str)
