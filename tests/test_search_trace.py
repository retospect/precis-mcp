"""Search provenance uses the test DB only, never a live MCP server."""

from __future__ import annotations

import math
from contextlib import contextmanager
from threading import get_ident
from time import perf_counter
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg import Connection
from psycopg_pool import PoolTimeout

from precis import agentlog
from precis.embedder import MockEmbedder
from precis.graph.garden.candidates import _kind_count, _scan_window
from precis.mesh_eval.usefulness import collect_usefulness
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.tools import core as tools_core
from precis.utils import search_trace
from precis.workers.chunk_keywords import claim_chunks_without_keywords
from precis.workers.embed import EmbedHandler, unembedded_chunk_count
from precis.workers.llm_summarize import unsummarized_chunk_count
from precis.workers.summarize import RakeLemmaHandler
from precis_web.routes.status import _kind_counts


@pytest.fixture(autouse=True)
def _trace_settings(monkeypatch):
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "1")
    monkeypatch.delenv("PRECIS_SEARCH_FIXED_FLOORS", raising=False)
    monkeypatch.delenv("PRECIS_SEARCH_GAP_CUT", raising=False)
    monkeypatch.delenv("PRECIS_SEARCH_TRACE_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("PRECIS_CURRENT_AGENTLOG", raising=False)


def _traces(store: Store) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'agentlog' "
            "AND meta->>'trace' = 'search' AND retired_at IS NULL ORDER BY ref_id"
        ).fetchall()


class _FixedEmbedder(MockEmbedder):
    def embed_one(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (self.dim - 1)


def _corpus(runtime: PrecisRuntime) -> dict[str, int]:
    store = runtime.live_store
    dim = store.embedding_dim()
    runtime.hub.embedder = _FixedEmbedder(dim=dim)
    ids = {}
    for name, text, cosine in [
        ("lex", "alpha beta retrieval evidence", 0.9),
        ("strong", "quokkas roam across meadows", 0.9),
        ("weak", "wombats wander without corroboration", 0.5),
        ("floor", "unrelated prose beyond the corpus floor", 0.1),
    ]:
        ref = store.insert_ref(kind="memory", slug=None, title=f"{name} node")
        vector = [cosine, math.sqrt(1 - cosine**2)] + [0.0] * (dim - 2)
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text=text, embedding=vector)]
        )
        ids[name] = ref.id
    return ids


def test_search_timing(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.live_store
    ref = store.insert_ref(kind="memory", slug=None, title="trace timing")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="search trace timing baseline")]
    )
    args = {"kind": "memory", "q": "trace timing", "mode": "lexical", "view": "index"}
    runtime_with_store.dispatch("search", dict(args))
    started = perf_counter()
    body, error = runtime_with_store.dispatch_with_status("search", dict(args))
    elapsed = perf_counter() - started
    assert not error, body
    assert f"me{ref.id}" in body
    print(f"SRCHTRACE test-DB search: {elapsed * 1000:.3f} ms")


def test_hybrid_search_timing(runtime_with_store, monkeypatch):
    runtime = runtime_with_store
    _corpus(runtime)
    args = {"kind": "memory", "q": "alpha beta", "view": "index"}
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "0")
    runtime.dispatch("search", dict(args))
    started = perf_counter()
    before = runtime.dispatch("search", dict(args))
    before_ms = (perf_counter() - started) * 1000
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "1")
    started = perf_counter()
    after = runtime.dispatch("search", dict(args))
    after_ms = (perf_counter() - started) * 1000
    assert before == after
    print(f"SRCHTRACE test-DB hybrid off/on: {before_ms:.3f}/{after_ms:.3f} ms")


@pytest.mark.parametrize("view", [None, "index"])
def test_trace_retains_stage_values_and_cuts(runtime_with_store, monkeypatch, view):
    runtime = runtime_with_store
    ids = _corpus(runtime)
    monkeypatch.setenv("PRECIS_CURRENT_AGENTLOG", "123")
    body, error = runtime.dispatch_with_status(
        "search", {"kind": "memory", "q": "alpha beta", "view": view}
    )
    assert not error, body
    rows = _traces(runtime.live_store)
    assert len(rows) == 1
    ident, meta = rows[0]
    assert meta["q"] == "alpha beta"
    assert meta["mode"] == "hybrid"
    assert meta["kinds"] == ["memory"]
    assert meta["view"] == view
    assert meta["session"] == 123
    candidates = {c["handle"]: c for c in meta["candidates"]}
    assert meta["candidates_total"] == len(candidates) == 4
    lex = candidates[f"me{ids['lex']}"]
    assert lex["lexical_rank"] == 1
    assert lex["semantic_distance"] == pytest.approx(0.1)
    assert lex["fused_rank"] == 1
    assert lex["rendered"] is True
    assert lex["cut"] is None
    assert candidates[f"me{ids['strong']}"]["rendered"] is True
    assert candidates[f"me{ids['weak']}"]["cut"] == "gap"
    assert candidates[f"me{ids['weak']}"]["rendered"] is False
    assert candidates[f"me{ids['floor']}"]["cut"] == "corpus-floor"
    assert candidates[f"me{ids['floor']}"]["rendered"] is False
    table = runtime.dispatch("get", {"kind": "agentlog", "id": ident})
    assert (
        "| handle | lexical_rank | semantic_distance | fused_rank | cut | rendered |"
        in table
    )
    assert f"me{ids['floor']}" in table


def test_dark_switch_and_failed_write_do_not_change_search(
    runtime_with_store, monkeypatch
):
    runtime = runtime_with_store
    _corpus(runtime)
    args = {"kind": "memory", "q": "alpha beta", "view": "index"}
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "0")
    expected = runtime.dispatch("search", dict(args))
    assert _traces(runtime.live_store) == []
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "1")
    actual = runtime.dispatch("search", dict(args))
    assert actual == expected

    def broken_snapshot(self):
        raise RuntimeError("trace storage unavailable")

    monkeypatch.setattr(search_trace.SearchTrace, "snapshot", broken_snapshot)
    assert runtime.dispatch("search", dict(args)) == expected
    assert len(_traces(runtime.live_store)) == 1
    assert search_trace.CURRENT.get() is None


def test_page_cut_and_candidate_cap(runtime_with_store):
    runtime = runtime_with_store
    store = runtime.live_store
    for i in range(60):
        ref = store.insert_ref(kind="memory", slug=None, title=f"candidate {i}")
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text="trace candidate lexical match")]
        )
    body, error = runtime.dispatch_with_status(
        "search",
        {
            "kind": "memory",
            "q": "trace candidate",
            "mode": "lexical",
            "view": "index",
            "page_size": 20,
        },
    )
    assert not error, body
    meta = _traces(store)[0][1]
    assert len(meta["candidates"]) == 50
    assert meta["candidates_total"] == 60
    assert sum(c["rendered"] for c in meta["candidates"]) == 20
    assert any(c["cut"] == "page" for c in meta["candidates"])


def test_search_retention_uses_own_window(runtime_with_store, monkeypatch):
    store = runtime_with_store.live_store
    ids = []
    for source, days in [("search", 31), ("search", 59), ("search", 61), ("chat", 31)]:
        ident = agentlog.open_log(store, source=source, title=f"{source} {days}")
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - %s::interval, "
                "meta = meta || CASE WHEN meta->>'source' = 'search' "
                "THEN '{\"trace\":\"search\"}'::jsonb ELSE '{}'::jsonb END "
                "WHERE ref_id = %s",
                (f"{days} days", ident),
            )
        ids.append(ident)
    assert agentlog.gc_stale_logs(store) == 2
    assert store.get_ref(kind="agentlog", id=ids[0]) is not None
    assert store.get_ref(kind="agentlog", id=ids[1]) is not None
    assert store.get_ref(kind="agentlog", id=ids[2]) is None
    assert store.get_ref(kind="agentlog", id=ids[2], include_deleted=True) is None
    assert store.get_ref(kind="agentlog", id=ids[3]) is None
    monkeypatch.setenv("PRECIS_SEARCH_TRACE_RETENTION_DAYS", "45")
    assert agentlog.gc_stale_logs(store) == 1
    assert store.get_ref(kind="agentlog", id=ids[0]) is not None
    assert store.get_ref(kind="agentlog", id=ids[1]) is None
    assert store.get_ref(kind="agentlog", id=ids[1], include_deleted=True) is None


def test_trace_filters_and_default_listing_isolation(runtime_with_store):
    runtime = runtime_with_store
    store = runtime.live_store
    _corpus(runtime)
    for _ in range(3):
        runtime.dispatch(
            "search", {"kind": "memory", "q": "alpha beta", "view": "index"}
        )
    traces = _traces(store)
    assert len(traces) == 3
    trace_ids = {ident for ident, _meta in traces}
    assert not trace_ids & {r.id for r in store.list_refs(kind="agentlog")}
    assert not trace_ids & {r["ref_id"] for r in agentlog.list_recent(store)}
    recent = runtime.dispatch("get", {"kind": "agentlog", "id": "/recent"})
    assert "Search trace" not in recent
    listed, error = runtime.dispatch_with_status(
        "get",
        {
            "kind": "agentlog",
            "id": "/traces",
            "__extras__": {"since": "90d", "kind": "memory"},
        },
    )
    assert not error, listed
    assert listed.count("candidates") == 3
    other = runtime.dispatch(
        "get", {"kind": "agentlog", "id": "/traces?since=90d&kind=paper"}
    )
    assert "no search traces match" in other
    assert store.search_refs_lexical(kind="agentlog", q="Search trace") == []
    assert store.count_refs_lexical(kind="agentlog", q="Search trace") == 0
    assert store.count_refs(kind="agentlog") == 0
    assert store.count_refs_facets(kind="agentlog")[0] == 0
    with store.pool.connection() as conn:
        chunks = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = ANY(%s)", (list(trace_ids),)
        ).fetchall()
        assert chunks == []
        for ident in trace_ids:
            assert unembedded_chunk_count(conn, ref_id=ident) == 0
            assert unsummarized_chunk_count(conn, ref_id=ident) == 0
        embed = EmbedHandler(runtime.hub.embedder)
        summary = RakeLemmaHandler()
        claimed_embed = embed.claim_batch(conn, limit=100)
        claimed_summary = summary.claim_batch(conn, limit=100)
        claimed_keywords = claim_chunks_without_keywords(conn, limit=100)
        for queue, queued in (
            ("embed", {r.chunk_id for r in claimed_embed}),
            ("summarize", {r.chunk_id for r in claimed_summary}),
            ("keywords", {r[0] for r in claimed_keywords}),
        ):
            count = conn.execute(
                "SELECT count(*) FROM chunks WHERE chunk_id = ANY(%s) "
                "AND ref_id = ANY(%s)",
                (list(queued), list(trace_ids)),
            ).fetchone()
            assert count == (0,), queue


def test_numeric_ref_and_cross_kind_have_one_trace(runtime_with_store):
    runtime = runtime_with_store
    _corpus(runtime)
    todo = runtime.live_store.insert_ref(
        kind="todo", slug=None, title="alpha beta task"
    )
    result, error = runtime.dispatch_with_status(
        "search", {"kind": "todo", "q": "alpha beta", "mode": "lexical"}
    )
    assert not error, result
    meta = _traces(runtime.live_store)[0][1]
    assert len(meta["candidates"]) == 1
    assert meta["candidates"][0] == {
        "handle": f"td{todo.id}",
        "lexical_rank": 1,
        "semantic_distance": None,
        "fused_rank": 1,
        "cut": None,
        "rendered": True,
    }
    result, error = runtime.dispatch_with_status(
        "search", {"kind": "memory,todo", "q": "alpha beta", "page_size": 1}
    )
    assert not error, result
    rows = _traces(runtime.live_store)
    assert len(rows) == 2
    assert rows[-1][1]["kinds"] == ["memory", "todo"]
    assert sum(c["rendered"] for c in rows[-1][1]["candidates"]) == 1
    assert any(c["cut"] == "gap" for c in rows[-1][1]["candidates"])


def test_trace_adds_only_one_insert_and_no_queries(runtime_with_store, monkeypatch):
    runtime = runtime_with_store
    _corpus(runtime)
    args = {"kind": "memory", "q": "alpha beta", "view": "index"}
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "0")
    runtime.dispatch("search", dict(args))
    calls: list[str] = []
    execute = Connection.execute
    caller_thread = get_ident()

    def spy(conn, query, params=None, **kwargs):
        if get_ident() == caller_thread and str(query).strip():
            calls.append(str(query))
        return execute(conn, query, params, **kwargs)

    monkeypatch.setattr(Connection, "execute", spy)
    expected = runtime.dispatch("search", dict(args))
    before = list(calls)
    calls.clear()
    monkeypatch.setenv("PRECIS_SEARCH_TRACES", "1")
    assert runtime.dispatch("search", dict(args)) == expected
    assert len(calls) == len(before) + 1, (before, calls)
    assert sum(q.startswith("INSERT INTO refs (kind, title, meta)") for q in calls) == 1
    assert sum(q.lstrip().upper().startswith(("SELECT", "WITH")) for q in calls) == sum(
        q.lstrip().upper().startswith(("SELECT", "WITH")) for q in before
    )

    def fail_trace_insert(conn, query, params=None, **kwargs):
        if str(query).startswith("INSERT INTO refs (kind, title, meta)"):
            raise RuntimeError("trace insert failed")
        return execute(conn, query, params, **kwargs)

    monkeypatch.setattr(Connection, "execute", fail_trace_insert)
    assert runtime.dispatch("search", dict(args)) == expected
    assert len(_traces(runtime.live_store)) == 1


def test_get_tool_accepts_trace_kind_filter(runtime_with_store, monkeypatch):
    runtime = runtime_with_store
    _corpus(runtime)
    runtime.dispatch("search", {"kind": "memory", "q": "alpha beta"})

    def dispatch(verb, payload, ctx=None):
        return runtime.dispatch(verb, payload)

    monkeypatch.setattr(tools_core, "_dispatch", dispatch)
    result = tools_core.get(
        kind="agentlog", id="/traces", args={"since": "90d", "kind": "memory"}
    )
    assert isinstance(result, str)
    assert "# Search traces" in result
    assert f"ag{_traces(runtime.live_store)[0][0]}" in result
    assert "alpha beta" in result


@pytest.mark.parametrize("raw", ["", "bad", "0", "-1"])
def test_invalid_trace_retention_uses_default(monkeypatch, raw):
    monkeypatch.setenv("PRECIS_SEARCH_TRACE_RETENTION_DAYS", raw)
    assert agentlog.search_trace_retention_days() == 60


def test_source_is_not_a_trace_marker(runtime_with_store):
    store = runtime_with_store.live_store
    normal = agentlog.open_log(store, source="search", title="Ordinary search run")
    trace = store.insert_ref(
        kind="agentlog",
        slug=None,
        title="Trace with another source",
        meta={"trace": "search", "source": "quest_tick"},
    )
    assert {r.id for r in store.list_refs(kind="agentlog")} == {normal}
    assert {r["ref_id"] for r in agentlog.list_recent(store)} == {normal}
    assert store.count_refs(kind="agentlog") == 1
    assert store.count_recent_refs(["agentlog"]) == 1
    assert store.count_refs_facets(kind="agentlog")[0] == 1
    assert store.count_refs_lexical(kind="agentlog", q="search") == 1
    assert {
        r.id for r, _ in store.search_refs_lexical(kind="agentlog", q="search")
    } == {normal}
    listed = runtime_with_store.dispatch("get", {"kind": "agentlog", "id": "/traces"})
    assert f"ag{trace.id}" in listed
    assert f"ag{normal}" not in listed
    rendered = runtime_with_store.dispatch("get", {"kind": "agentlog", "id": trace.id})
    assert "| lexical_rank |" in rendered
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '61 days' "
            "WHERE ref_id = ANY(%s)",
            ([normal, trace.id],),
        )
    assert agentlog.gc_stale_logs(store) == 2
    assert store.get_ref(kind="agentlog", id=normal, include_deleted=True) is not None
    assert store.get_ref(kind="agentlog", id=trace.id, include_deleted=True) is None


@pytest.mark.parametrize("view", ["tags", "meta"])
def test_introspection_counts_exclude_traces(runtime_with_store, view):
    store = runtime_with_store.live_store
    agentlog.open_log(store, source="search", title="Ordinary run")
    store.insert_ref(
        kind="agentlog",
        slug=None,
        title="Trace",
        meta={"trace": "search", "source": "quest_tick", "trace_only": True},
    )
    body = runtime_with_store.dispatch("search", {"kind": "agentlog", "view": view})
    assert "(1 live ref)" in body
    assert "trace_only" not in body


def test_review_coverage_counts_exclude_traces(runtime_with_store):
    store = runtime_with_store.live_store
    agentlog.open_log(store, source="search", title="Ordinary run")
    store.insert_ref(
        kind="agentlog",
        slug=None,
        title="Trace",
        meta={"trace": "search", "source": "quest_tick"},
    )
    coverage = next(r for r in store.considered_coverage() if r["kind"] == "agentlog")
    assert coverage["live"] == 1


def test_corpus_kind_counts_exclude_traces(runtime_with_store):
    store = runtime_with_store.live_store
    normal = agentlog.open_log(store, source="search", title="Ordinary run")
    for _ in range(2):
        store.insert_ref(
            kind="agentlog",
            slug=None,
            title="Duplicate trace",
            meta={"trace": "search", "source": "quest_tick"},
        )
    assert next(r for r in _kind_counts(store) if r["kind"] == "agentlog")["count"] == 1
    assert _kind_count(store, "agentlog") == 1
    assert _scan_window(store, "agentlog", after=0, size=50)[0] == [normal]
    with store.pool.connection() as conn:
        report = collect_usefulness(conn)
    assert next(r for r in report.orphans if r[0] == "agentlog")[2] == 1
    assert next(r for r in report.stale if r[0] == "agentlog")[3] == 1
    assert not any(r[0] == "agentlog" for r in report.duplicate_groups)


def test_query_is_bounded_and_original_length_recorded(runtime_with_store):
    store = runtime_with_store.live_store
    q = "alpha " * 200
    agentlog.write_search_trace(
        store, {"kind": "memory", "q": q}, search_trace.SearchTrace()
    )
    meta = _traces(store)[0][1]
    assert meta["trace"] == "search"
    assert meta["q"] == q[:500]
    assert meta["q_len"] == len(q)


def test_pool_timeout_skips_trace_write(runtime_with_store, caplog):
    store = runtime_with_store.live_store
    timeouts = []

    @contextmanager
    def saturated(*, timeout=None):
        timeouts.append(timeout)
        raise PoolTimeout("no connection available")
        yield

    saturated_store = cast(
        Store, SimpleNamespace(pool=SimpleNamespace(connection=saturated))
    )
    agentlog.write_search_trace(
        saturated_store, {"q": "alpha"}, search_trace.SearchTrace()
    )
    assert timeouts == [0.05]
    assert _traces(store) == []
    assert "search trace write failed" in caplog.text
