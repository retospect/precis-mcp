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

    def test_empty_string_marks_as_zero_chars(self) -> None:
        """gr503460: an explicitly-sent empty string reads ``<0 chars>`` —
        before, ``""`` was indistinguishable from an omitted key in the
        ledger (payload keys already used the ``<N chars>`` marker)."""
        got = json.loads(_sample({"kind": "memory", "id": "", "q": "x"}))
        assert got["id"] == "<0 chars>"
        assert got["q"] == "x"

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
    slot). ``client_params`` mimics ``ServerSession``'s — ``None`` until
    the test declares an initialize handshake."""

    client_params: Any = None


class TestSessionKey:
    def test_unbound_is_null(self) -> None:
        assert tool_ledger.current_session_key() == (None, None)

    def test_bound_session_mints_id_and_counts(self) -> None:
        from precis import serve_ledger

        # 56ca460e1: current_session_key PEEKS the next row's seq; the
        # ledger's writer (_mint_session_key) consumes it.
        with serve_ledger.session_scope(_FakeSession()):
            peek1 = tool_ledger.current_session_key()
            peek2 = tool_ledger.current_session_key()
            sid1, seq1 = tool_ledger._mint_session_key()
            sid2, seq2 = tool_ledger._mint_session_key()
            after = tool_ledger.current_session_key()
        assert sid1 == sid2 is not None
        assert peek1 == peek2 == (sid1, seq1)
        assert seq2 == (seq1 or 0) + 1
        assert after == (sid2, (seq2 or 0) + 1)

    def test_distinct_sessions_get_distinct_ids(self) -> None:
        from precis import serve_ledger

        with serve_ledger.session_scope(_FakeSession()):
            sid_a, _ = tool_ledger.current_session_key()
        with serve_ledger.session_scope(_FakeSession()):
            sid_b, _ = tool_ledger.current_session_key()
        assert sid_a is not None and sid_b is not None and sid_a != sid_b


class TestCurrentClient:
    def test_unbound_is_none(self) -> None:
        assert tool_ledger.current_client() is None

    def test_session_without_params_is_none(self) -> None:
        from precis import serve_ledger

        with serve_ledger.session_scope(_FakeSession()):
            assert tool_ledger.current_client() is None

    def test_clientinfo_name_version(self) -> None:
        from types import SimpleNamespace

        from precis import serve_ledger

        session = _FakeSession()
        session.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="test-client", version="9.9"),
            meta=None,
        )
        with serve_ledger.session_scope(session):
            assert tool_ledger.current_client() == "test-client/9.9"

    def test_meta_model_appended(self) -> None:
        from types import SimpleNamespace

        from precis import serve_ledger

        session = _FakeSession()
        session.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="devin-cli", version="1.2"),
            meta=SimpleNamespace(model="swe-test"),
        )
        with serve_ledger.session_scope(session):
            assert tool_ledger.current_client() == "devin-cli/1.2 (model:swe-test)"

    def test_name_without_version(self) -> None:
        from types import SimpleNamespace

        from precis import serve_ledger

        session = _FakeSession()
        session.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="bare", version=None), meta=None
        )
        with serve_ledger.session_scope(session):
            assert tool_ledger.current_client() == "bare"


def test_record_call_persists_client(store: Any) -> None:
    tag = uuid4().hex[:8]
    verb = f"client-{tag}"
    tool_ledger.record_call(
        store,
        _rec(verb=verb, client="claude-code/9.9 (model:test-1)"),
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT client FROM tool_calls WHERE verb=%s", (verb,)
        ).fetchone()
    assert row == ("claude-code/9.9 (model:test-1)",)


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


class TestCallsRead:
    """gr520238 ask 3 — the ledger's agent-facing readers (previously
    write-only; "which views did that run call" needed a transcript
    regex)."""

    def test_calls_for_agentlog_round_trip(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        alog = int(tag, 16)  # agentlog_id has no FK — a uuid tag is a run
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                kind="paper",
                input_keys=["id", "kind", "view"],
                agentlog_id=alog,
                latency_ms=137,
                arg_sample='{"kind":"paper","id":"pa1","view":"toc"}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb=f"search-{tag}",
                agentlog_id=alog,
                outcome="error",
                error_type="BadInput",
                error_head="[error:BadInput] q empty",
            ),
        )
        tool_ledger.record_call(  # a different run — must not leak in
            store, _rec(verb=f"other-{tag}", agentlog_id=alog + 1)
        )
        total, rows = tool_ledger.calls_for_agentlog(store, alog)
        assert total == 2 and len(rows) == 2
        assert [r["verb"] for r in rows] == ["get", f"search-{tag}"]
        line = tool_ledger.call_line(rows[0])
        assert f"agentlog {alog}" in line
        assert "kind='paper'" in line
        assert "id='pa1'" in line and "view='toc'" in line
        assert line.endswith("→ ok 137ms")
        err_line = tool_ledger.call_line(rows[1])
        assert "→ ERROR BadInput" in err_line and "q empty" in err_line

    def test_calls_for_agentlog_caps_to_newest(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        alog = int(tag, 16)
        for i in range(5):
            tool_ledger.record_call(
                store, _rec(verb=f"cap-{tag}-{i}", agentlog_id=alog)
            )
        total, rows = tool_ledger.calls_for_agentlog(store, alog, limit=2)
        assert total == 5 and len(rows) == 2
        assert [r["verb"] for r in rows] == [f"cap-{tag}-3", f"cap-{tag}-4"]

    def test_calls_mining_groups_filters_and_limits(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        kind = f"tk-{tag}"
        sample = json.dumps({"kind": kind, "view": "toc"})
        tool_ledger.record_call(
            store, _rec(verb="get", kind=kind, arg_sample=sample, latency_ms=9)
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                kind=kind,
                arg_sample=sample,
                outcome="error",
                error_type="NotFound",
            ),
        )
        tool_ledger.record_call(store, _rec(verb="get", kind=f"other-{tag}"))
        out = tool_ledger.calls_mining(store, kind=kind, since_hours=1)
        assert out["total"] == 2
        grp = next(g for g in out["groups"] if g["kind"] == kind)
        assert grp["verb"] == "get" and grp["view"] == "toc"
        assert grp["n"] == 2 and grp["errors"] == 1
        assert len(out["rows"]) == 2
        # The view filter digs into arg_sample, errors= narrows to fails.
        assert (
            tool_ledger.calls_mining(store, kind=kind, view="toc", since_hours=1)[
                "total"
            ]
            == 2
        )
        assert (
            tool_ledger.calls_mining(store, kind=kind, view="abstract", since_hours=1)[
                "total"
            ]
            == 0
        )
        errs = tool_ledger.calls_mining(store, kind=kind, errors=True, since_hours=1)
        assert errs["total"] == 1
        assert errs["rows"][0]["error_type"] == "NotFound"
        # A kind filter another kind's rows never match.
        assert (
            tool_ledger.calls_mining(store, kind=f"other-{tag}", since_hours=1)["total"]
            == 1
        )

    def test_call_line_minimal_row(self) -> None:
        """A row with no agentlog / sample / latency still renders."""
        line = tool_ledger.call_line({"verb": "put", "kind": "memory", "outcome": "ok"})
        assert line == "? put kind='memory' → ok"

    def test_call_line_kind_column_fills_missing_sample(self) -> None:
        line = tool_ledger.call_line(
            {
                "verb": "search",
                "kind": "job",
                "outcome": "error",
                "error_type": "BadInput",
                "arg_sample": {"q": "x"},
            }
        )
        assert "kind='job'" in line and "q='x'" in line
        assert "→ ERROR BadInput" in line


class TestSessionsRollup:
    """Block-navigation slice 2 — the per-session meter: hops, summed
    response bytes, calls by verb, budget refusals, wall span."""

    def _session_rows(self, store: Any, tag: str) -> tuple[str, str]:
        """Two tagged sessions. ``sess-<tag>-a``: get ok (100 B) + a get
        refused by the result budget (120 B error body) + search×1
        (50 B); ``sess-<tag>-b``: get ok (10 B) + an ordinary BadInput
        refusal (80 B) plus a NULL-bytes row."""
        sid_a, sid_b = f"sess-{tag}-a", f"sess-{tag}-b"
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid_a,
                seq=1,
                result_bytes=100,
                arg_sample='{"kind":"paper","id":"pa1"}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid_a,
                seq=2,
                outcome="error",
                error_type="ResultBudgetExceeded",
                result_bytes=120,
                arg_sample='{"kind":"paper","id":"pa1"}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="search",
                session_id=sid_a,
                seq=3,
                result_bytes=50,
                arg_sample='{"kind":"paper","q":"x"}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid_b,
                seq=1,
                error_type=tool_ledger.BUDGET_FITTED,  # ok row, cut to head+outline
                result_bytes=10,
                arg_sample='{"kind":"job"}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid_b,
                seq=2,
                outcome="error",
                error_type="BadInput",
                result_bytes=80,
                arg_sample='{"kind":"job","id":"x"}',
            ),
        )
        tool_ledger.record_call(
            store, _rec(verb="get", session_id=sid_b, seq=3, result_bytes=None)
        )
        # An unrelated session — must stay out of the tagged totals.
        tool_ledger.record_call(
            store, _rec(verb="get", session_id=f"other-{tag}", seq=1)
        )
        return sid_a, sid_b

    def test_rollup_hops_bytes_verbs_budget_refusals(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        sid_a, sid_b = self._session_rows(store, tag)
        out = tool_ledger.sessions_rollup(store, session=f"sess-{tag}-", since_hours=1)
        assert out["total"] == 2
        rows = {r["session_id"]: r for r in out["rows"]}
        a, b = rows[sid_a], rows[sid_b]
        assert a["hops"] == 3 and a["resp_bytes"] == 270
        assert a["calls_by_verb"] == {"get": 2, "search": 1}
        assert a["errors"] == 1 and a["budget_refusals"] == 1
        # An ordinary refusal is an error, never a budget refusal.
        assert b["hops"] == 3 and b["resp_bytes"] == 90
        assert b["calls_by_verb"] == {"get": 3}
        assert b["errors"] == 1 and b["budget_refusals"] == 0
        # A fitted (ok) row is its own counter, never a refusal or error.
        assert a["budget_fits"] == 0 and b["budget_fits"] == 1
        assert "continuations" not in a
        assert a["first_ts"] is not None and a["last_ts"] is not None
        assert isinstance(a["span_seconds"], int)

    def test_rollup_wall_span(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        sid_a, _ = self._session_rows(store, tag)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE tool_calls SET ts = ts - interval '30 minutes' "
                "WHERE session_id = %s AND seq = 1",
                (sid_a,),
            )
            conn.commit()
        out = tool_ledger.sessions_rollup(store, session=sid_a, since_hours=1)
        (a,) = out["rows"]
        assert a["span_seconds"] >= 1800

    def test_rollup_verb_filter_narrows_session_and_counts(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        sid_a, _ = self._session_rows(store, tag)
        out = tool_ledger.sessions_rollup(
            store, session=f"sess-{tag}-", verb="search", since_hours=1
        )
        # Only sid_a ran a search; its counts cover the search rows only.
        assert out["total"] == 1
        (a,) = out["rows"]
        assert a["session_id"] == sid_a
        assert a["hops"] == 1 and a["resp_bytes"] == 50
        assert a["calls_by_verb"] == {"search": 1}

    def test_rollup_window_excludes_old_sessions(self, store: Any) -> None:
        tag = uuid4().hex[:8]
        sid = f"old-{tag}"
        tool_ledger.record_call(store, _rec(verb="get", session_id=sid, seq=1))
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE tool_calls SET ts = now() - interval '2 hours' "
                "WHERE session_id = %s",
                (sid,),
            )
            conn.commit()
        out = tool_ledger.sessions_rollup(store, session=sid, since_hours=1)
        assert out["total"] == 0 and out["rows"] == []

    def test_rollup_page_arg_is_no_longer_a_signal(self, store: Any) -> None:
        """``page`` in ``arg_sample`` fed the old ``continuations``
        count; with pagination gone the roll-up reads only
        ``error_type`` — a page-shaped sample is neither counted nor
        able to poison the read (a malformed ``page`` once broke
        ``/sessions`` for the whole window)."""
        tag = uuid4().hex[:8]
        sid = f"sess-{tag}"
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid,
                seq=1,
                arg_sample='{"kind":"job","page":99999999999}',
            ),
        )
        tool_ledger.record_call(
            store,
            _rec(
                verb="get",
                session_id=sid,
                seq=2,
                arg_sample='{"kind":"job","page":"abc"}',
            ),
        )
        out = tool_ledger.sessions_rollup(store, session=sid, since_hours=1)
        (row,) = out["rows"]
        assert row["session_id"] == sid
        assert row["hops"] == 2 and row["budget_refusals"] == 0
        assert "continuations" not in row


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
