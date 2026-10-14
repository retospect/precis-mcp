"""Tests for the tool-call ledger (tool_ledger, migration 0133).

DB-backed. Tag every row with a uuid so assertions survive the shared
``precis_test`` DB (filter by the tag, never absolute counts).
"""

from __future__ import annotations

import json
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


def _sample(args: dict[str, Any]) -> str:
    """``arg_sample`` narrowed — asserts a non-None sample, returns str."""
    out = tool_ledger.arg_sample(args)
    assert out is not None
    return out


#: Assembled from fragments — the credential gate
#: (tests/test_deploy_tree_no_secrets.py) flags any source LINE that is
#: credential-shaped; a runtime-built fake keeps the masking assertion
#: identical while no literal ever looks like a key.
_FAKE_KEY = "sk-" + "ant-" + "FAKETESTKEY" + "123456"


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


def test_record_call_persists_0209_fields(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"sess-{tag}"
    tool_ledger.record_call(
        store,
        _rec(
            verb=verb,
            session_id="testhost-1-deadbeef",
            seq=7,
            result_bytes=512,
            error_head="[error:BadInput] kind must be one of …",
            arg_sample='{"kind":"job","under":"qu459585"}',
        ),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT session_id, seq, result_bytes, error_head, arg_sample "
            "FROM tool_calls WHERE verb=%s",
            (verb,),
        ).fetchone()
    assert row == (
        "testhost-1-deadbeef",
        7,
        512,
        "[error:BadInput] kind must be one of …",
        {"kind": "job", "under": "qu459585"},
    )


class TestArgSample:
    def test_scalars_and_truncation(self) -> None:
        got = json.loads(_sample({"kind": "job", "q": "x" * 200, "page": 2}))
        assert got["kind"] == "job"
        assert got["page"] == 2
        assert len(got["q"]) <= 81  # 80 chars + ellipsis
        assert got["q"].endswith("…")

    def test_payload_keys_sized_not_carried(self) -> None:
        secret = f"super-secret-body-{uuid4().hex[:8]}"
        got = json.loads(_sample({"kind": "memory", "text": secret, "title": secret}))
        assert secret not in str(got)
        assert got["text"] == f"<{len(secret)} chars>"
        assert got["title"] == f"<{len(secret)} chars>"

    def test_secretish_keys_redacted(self) -> None:
        got = json.loads(_sample({"kind": "job", "api_key": "abc123", "token": "tok"}))
        assert got["api_key"] == "<redacted>"
        assert got["token"] == "<redacted>"
        assert "abc123" not in json.dumps(got)

    def test_secret_values_masked_not_carried(self) -> None:
        got = json.loads(_sample({"kind": "job", "q": f"key is {_FAKE_KEY}"}))
        assert _FAKE_KEY not in got["q"]

    def test_meta_is_keys_only(self) -> None:
        got = json.loads(
            _sample({"kind": "job", "meta": {"job_type": "sweep", "secret": "v"}})
        )
        assert got["meta"].startswith("<dict:")
        assert "sweep" not in str(got) and '"v"' not in str(got)

    def test_extras_sampled_one_level(self) -> None:
        got = json.loads(
            _sample({"kind": "job", "__extras__": {"group_by": "state", "depth": 3}})
        )
        assert got["__extras__"] == {"group_by": "state", "depth": 3}

    def test_none_args(self) -> None:
        assert tool_ledger.arg_sample({}) is None

    def test_bounded_total(self) -> None:
        args = {f"k{i}": "v" * 200 for i in range(50)}
        sample = _sample(args)
        assert len(sample) <= 2100
        json.loads(sample)  # still valid JSON — never truncated mid-value


class TestErrorHead:
    def test_first_line_masked_bounded(self) -> None:
        body = f"[error:BadInput] use {_FAKE_KEY} instead\nnext: other"
        head = tool_ledger.error_head(body)
        assert head is not None
        assert head.startswith("[error:BadInput]")
        assert _FAKE_KEY not in head
        assert "next:" not in head

    def test_empty(self) -> None:
        assert tool_ledger.error_head("") is None


class _FakeSession:
    """Weakrefable stand-in for the FastMCP session object (a bare
    ``object()`` is NOT weakrefable — CPython gives it no ``__weakref__``
    slot)."""


class TestSessionKey:
    def test_unbound_is_null(self) -> None:
        assert tool_ledger.current_session_key() == (None, None)

    def test_bound_session_mints_id_and_counts(self) -> None:
        from precis import serve_ledger

        with serve_ledger.session_scope(_FakeSession()):
            sid1, seq1 = tool_ledger.current_session_key()
            sid2, seq2 = tool_ledger.current_session_key()
        assert sid1 == sid2 is not None
        assert seq2 == (seq1 or 0) + 1

    def test_distinct_sessions_get_distinct_ids(self) -> None:
        from precis import serve_ledger

        with serve_ledger.session_scope(_FakeSession()):
            sid_a, _ = tool_ledger.current_session_key()
        with serve_ledger.session_scope(_FakeSession()):
            sid_b, _ = tool_ledger.current_session_key()
        assert sid_a is not None and sid_b is not None and sid_a != sid_b


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
