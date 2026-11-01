"""Feedback stays session-local and cheap; all SQL uses the test database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from psycopg import Connection

from precis import search_feedback
from precis.cli import traces
from precis.cli.main import _build_parser
from precis.store import ChunkInsert, Tag


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("PRECIS_CURRENT_AGENTLOG", "123")
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "1")


def _search(runtime):
    store = runtime.live_store
    ref = store.insert_ref(kind="memory", slug=None, title="feedback evidence")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="feedback evidence retrieval")]
    )
    body, error = runtime.dispatch_with_status(
        "search",
        {
            "kind": "memory",
            "q": "feedback evidence",
            "mode": "lexical",
            "view": "index",
        },
    )
    assert not error, body
    assert f"me{ref.id}" in body
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE meta->>'trace' = 'search'"
        ).fetchone()
    return f"me{ref.id}", row[0]


def _meta(store, ident):
    return store.get_ref(kind="agentlog", id=ident).meta


@pytest.mark.parametrize("numeric", [False, True])
def test_search_then_get_appends_followed(runtime_with_store, numeric, monkeypatch):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    args = {"kind": "memory", "id": int(handle[2:])} if numeric else {"id": handle}
    updates = []
    execute = Connection.execute

    def spy(conn, query, params=None, **kwargs):
        if str(query).startswith("UPDATE refs r SET meta = jsonb_set"):
            updates.append(params)
        return execute(conn, query, params, **kwargs)

    monkeypatch.setattr(Connection, "execute", spy)
    for _ in range(2):
        body, error = runtime.dispatch_with_status("get", args.copy())
        assert not error, body
    assert _meta(runtime.live_store, ident)["followed"] == [handle]
    assert len(updates) == 1


@pytest.mark.parametrize("verb", ["put", "edit", "link"])
def test_successful_writes_append_used(runtime_with_store, verb):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    if verb == "put":
        args = {"kind": "memory", "text": f"A cited observation [{handle}]."}
    elif verb == "edit":
        args = {"id": handle, "text": f"Updated observation cites [{handle}]."}
    else:
        todo = runtime.live_store.insert_ref(
            kind="todo", slug=None, title="use evidence"
        )
        args = {"kind": "todo", "id": todo.id, "target": handle, "rel": "related-to"}
    body, error = runtime.dispatch_with_status(verb, args)
    assert not error, body
    assert _meta(runtime.live_store, ident)["used"] == [handle]


@pytest.mark.parametrize("other_session", ["124", None])
def test_get_in_another_or_missing_session_does_not_follow(
    runtime_with_store, monkeypatch, other_session
):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    if other_session:
        monkeypatch.setenv("PRECIS_CURRENT_AGENTLOG", other_session)
    else:
        monkeypatch.delenv("PRECIS_CURRENT_AGENTLOG")
    body, error = runtime.dispatch_with_status("get", {"id": handle})
    assert not error, body
    assert "followed" not in _meta(runtime.live_store, ident)


def test_expired_trace_does_not_query_or_follow(runtime_with_store):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    future = datetime.now(UTC) + timedelta(minutes=31)
    with patch.object(search_feedback, "datetime") as clock:
        clock.now.return_value = future
        with patch.object(search_feedback, "_signals") as signals:
            body, error = runtime.dispatch_with_status("get", {"id": handle})
            assert not error, body
            signals.assert_not_called()
    assert "followed" not in _meta(runtime.live_store, ident)


def test_get_without_recent_trace_adds_no_trace_sql(runtime_with_store, monkeypatch):
    runtime = runtime_with_store
    ref = runtime.live_store.insert_ref(kind="memory", slug=None, title="untraced")
    queries = []
    execute = Connection.execute

    def spy(conn, query, params=None, **kwargs):
        queries.append(str(query))
        return execute(conn, query, params, **kwargs)

    monkeypatch.setattr(Connection, "execute", spy)
    body, error = runtime.dispatch_with_status("get", {"id": f"me{ref.id}"})
    assert not error, body
    assert not any("feedback" in q or "30 minutes" in q for q in queries)
    assert not any("meta->>'trace' = 'search'" in q for q in queries)


def test_dark_switch_disables_cached_feedback(runtime_with_store, monkeypatch):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "0")
    body, error = runtime.dispatch_with_status("get", {"id": handle})
    assert not error, body
    assert "followed" not in _meta(runtime.live_store, ident)


def test_body_signals_ignore_find_anchors_and_titles():
    field, handles = search_feedback._signals(
        "edit",
        {
            "title": "[me2]",
            "ops": [
                {"find": "[me3]", "text": "[me1]"},
                {"before": "[me4]", "after": "[me5]", "text": "[me6]"},
            ],
        },
    )
    assert field == "used"
    assert handles == {"me1", "me6"}
    assert (
        search_feedback._signals(
            "link", {"id": "me1", "target": "me2", "mode": "remove"}
        )[1]
        == set()
    )


def test_recent_cache_is_bounded_per_session_and_store(store):
    now = datetime.now(UTC)
    meta = {"session": 123, "candidates": [{"handle": "me1"}]}
    for ident in range(search_feedback.MAX_TRACES + 2):
        search_feedback.remember(store, ident, now, meta)
    entries = search_feedback._RECENT[store][123]
    assert len(entries) == search_feedback.MAX_TRACES
    assert entries[0].id == 2
    for session in range(search_feedback.MAX_SESSIONS + 1):
        search_feedback.remember(store, session, now, {**meta, "session": session})
    assert len(search_feedback._RECENT[store]) == search_feedback.MAX_SESSIONS
    assert 0 not in search_feedback._RECENT[store]


def test_feedback_failure_never_fails_get(runtime_with_store, monkeypatch, caplog):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    execute = Connection.execute

    def broken(conn, query, params=None, **kwargs):
        if str(query).startswith("UPDATE refs r SET meta = jsonb_set"):
            raise RuntimeError("feedback unavailable")
        return execute(conn, query, params, **kwargs)

    monkeypatch.setattr(Connection, "execute", broken)
    with caplog.at_level("DEBUG", logger="precis.search_feedback"):
        body, error = runtime.dispatch_with_status("get", {"id": handle})
    assert not error, body
    assert "followed" not in _meta(runtime.live_store, ident)
    assert "search trace feedback skipped" in caplog.text


def test_failed_and_dry_run_writes_do_not_count(runtime_with_store):
    runtime = runtime_with_store
    handle, ident = _search(runtime)
    body, error = runtime.dispatch_with_status(
        "edit",
        {
            "id": handle,
            "mode": "find-replace",
            "find": "not present",
            "text": f"[{handle}]",
        },
    )
    assert error, body
    body, error = runtime.dispatch_with_status(
        "put", {"kind": "memory", "text": f"Cites [{handle}]", "dry_run": True}
    )
    assert not error, body
    assert "used" not in _meta(runtime.live_store, ident)


def _trace(store, candidates, **meta):
    return store.insert_ref(
        kind="agentlog",
        slug=None,
        title="Search trace",
        meta={
            "trace": "search",
            "q": "feedback",
            "kinds": ["memory"],
            "mode": "hybrid",
            "candidates": candidates,
            **meta,
        },
    ).id


def _export(store, **kwargs):
    with store.pool.connection() as conn:
        return list(traces.export_rows(conn, since="90 days", **kwargs))


def test_export_labels_and_all_miss_sources(runtime_with_store):
    store = runtime_with_store.live_store
    refs = [
        store.insert_ref(kind="memory", slug=None, title=f"export candidate {n}")
        for n in range(5)
    ]
    handles = [f"me{r.id}" for r in refs]
    candidates = [
        {"handle": h, "rendered": n < 4, "cut": None if n < 4 else "fold"}
        for n, h in enumerate(handles)
    ]
    ident = _trace(store, candidates, followed=[handles[0]], used=[handles[1]])
    todo = store.insert_ref(kind="todo", slug=None, title="review memory")
    store.add_tag(todo.id, Tag.open("memory-review"), set_by="system")
    store.add_link(src_ref_id=todo.id, dst_ref_id=refs[2].id, set_by="system")
    concern = store.insert_ref(kind="memory", slug=None, title="concern")
    store.add_link(
        src_ref_id=concern.id,
        dst_ref_id=refs[3].id,
        relation="raises-concern-about",
        set_by="system",
    )
    row = _export(store, kind="memory")[0]
    assert row["id"] == f"ag{ident}"
    assert row["candidates"] == candidates
    assert row["positives"] == sorted(handles[:2])
    assert row["negatives"] == sorted(handles[1:4])
    assert row["misses"] == sorted(handles[2:4])
    assert row["trace_miss"] is False
    assert handles[4] not in row["positives"] + row["negatives"] + row["misses"]
    body, error = runtime_with_store.dispatch_with_status(
        "tag", {"id": f"ag{ident}", "add": ["trace:miss"]}
    )
    assert not error, body
    row = _export(store)[0]
    assert row["trace_miss"] is True
    assert row["misses"] == sorted(handles[:4])
    assert _export(store, kind="paper") == []


def test_export_resolves_chunk_graph_misses_and_empty_explicit_miss(store):
    ref = store.insert_ref(kind="paper", slug="tracechunk26", title="chunk candidate")
    chunk = store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="evidence")])[0]
    handle = f"pc{chunk.id}"
    ident = _trace(store, [{"handle": handle, "rendered": True}])
    todo = store.insert_ref(kind="todo", slug=None, title="review")
    store.add_tag(todo.id, Tag.open("memory-review"), set_by="system")
    store.add_link(src_ref_id=todo.id, dst_ref_id=ref.id, set_by="system")
    empty = _trace(store, [])
    store.add_tag(empty, Tag.open("trace:miss"), set_by="system")
    rows = {r["id"]: r for r in _export(store)}
    assert rows[f"ag{ident}"]["misses"] == [handle]
    assert rows[f"ag{empty}"]["trace_miss"] is True
    assert rows[f"ag{empty}"]["misses"] == []


def test_export_filters_old_and_retired_traces_and_is_read_only(store):
    recent = _trace(store, [])
    old = _trace(store, [])
    retired = _trace(store, [])
    store.retire_ref(retired)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '91 days' WHERE ref_id = %s",
            (old,),
        )
    with store.pool.connection() as conn:
        rows = traces.export_rows(conn, since="90 days")
        assert next(rows)["id"] == f"ag{recent}"
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert list(rows) == []


def test_export_cli_jsonl_and_duration_validation(store, capsys, monkeypatch):
    _trace(store, [])
    parser = _build_parser(("traces",))
    args = parser.parse_args(["traces", "export", "--jsonl", "--kind", "memory"])
    assert args.since == "90 days"
    with store.pool.connection() as conn:
        monkeypatch.setattr(traces, "resolve_dsn", lambda value: "test")
        with patch.object(traces.psycopg, "connect", return_value=conn):
            traces.run(args)
    row = json.loads(capsys.readouterr().out)
    assert row["q"] == "feedback"
    assert row["kind"] == ["memory"]
    with pytest.raises(SystemExit):
        parser.parse_args(["traces", "export", "--jsonl", "--since", "-1d"])
