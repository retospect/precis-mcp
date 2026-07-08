"""The "papers we still need to get" backlog.

Two layers:

- store engine (``Store.stub_backlog``): the stub predicate
  (``pdf_sha256 IS NULL`` + an external id), the latest-fetcher-event
  join, the ``awaiting`` filter, and the one-line ``state`` summary.
- dispatch (``search(view='stubs')``): routing, rendering, ``q=``
  ignored, paper-only — end to end through ``runtime.dispatch``.

Shared by ``precis stubs`` (CLI) and the MCP view, so this guards both
(docs/design/stubs-mcp-and-skill.md).
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime
from precis.store import Store


def _stub(
    store: Store,
    *,
    cite_key: str,
    doi: str | None = None,
    arxiv: str | None = None,
) -> int:
    """A paper ref with no PDF and the given external id(s)."""
    ref = store.insert_ref(kind="paper", slug=cite_key, title="X", meta={})
    with store.pool.connection() as conn:
        if doi is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', %s, 'manual')",
                (ref.id, doi),
            )
        if arxiv is not None:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'arxiv', %s, 'manual')",
                (ref.id, arxiv),
            )
    return ref.id


def _mark_held(store: Store, ref_id: int) -> None:
    sha = f"{ref_id:064d}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
            (sha, ref_id),
        )


def _fetch_event(
    store: Store,
    ref_id: int,
    event: str,
    *,
    hours_ago: float,
    payload: str = "{}",
    source: str = "fetcher:unpaywall",
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "VALUES (%s, %s, %s, %s::jsonb, "
            "        now() - make_interval(hours => %s))",
            (ref_id, source, event, payload, hours_ago),
        )


# ── store engine: stub_backlog ──────────────────────────────────────


def test_stub_backlog_empty(store: Store) -> None:
    assert store.stub_backlog() == []


def test_stub_backlog_lists_stub_with_external_id(store: Store) -> None:
    rid = _stub(store, cite_key="smith2024a", doi="10.1/x")
    rows = store.stub_backlog()
    assert [r["ref_id"] for r in rows] == [rid]
    assert rows[0]["identifier"] == "10.1/x"
    assert rows[0]["cite_key"] == "smith2024a"
    assert rows[0]["state"] == "awaiting fetch (never tried)"


def test_stub_backlog_excludes_held_paper(store: Store) -> None:
    held = _stub(store, cite_key="held2024", doi="10.1/held")
    _mark_held(store, held)
    want = _stub(store, cite_key="want2024", doi="10.1/want")
    assert [r["ref_id"] for r in store.stub_backlog()] == [want]


def test_stub_backlog_excludes_paper_without_external_id(store: Store) -> None:
    # A pdf-less paper with only a cite_key isn't fetchable → not a stub.
    store.insert_ref(kind="paper", slug="noid2024", title="X", meta={})
    with_id = _stub(store, cite_key="hasid2024", arxiv="2401.00001")
    rows = store.stub_backlog()
    assert [r["ref_id"] for r in rows] == [with_id]
    assert rows[0]["identifier"] == "arxiv:2401.00001"


def test_stub_backlog_state_reflects_latest_event(store: Store) -> None:
    rid = _stub(store, cite_key="oa2024", doi="10.1/oa")
    _fetch_event(store, rid, "fetch_failed", hours_ago=48)
    _fetch_event(store, rid, "no_oa_version", hours_ago=1)
    rows = store.stub_backlog()
    assert rows[0]["state"] == "no OA version available"


def test_stub_backlog_state_surfaces_failure_reason(store: Store) -> None:
    # A fetch_failed whose payload carries the attempted URL + httpx error
    # should render the concrete why (host + HTTP status), not a bare
    # "fetch_failed" — so /papers-needed shows "mdpi.com 403" at a glance.
    rid = _stub(store, cite_key="mdpi2023", doi="10.3390/x")
    _fetch_event(
        store,
        rid,
        "fetch_failed",
        hours_ago=1,
        source="fetcher:s2",
        payload=(
            '{"url": "https://www.mdpi.com/2227-9040/11/9/486/pdf?version=1", '
            "\"error\": \"Client error '403 Forbidden' for url 'x'\"}"
        ),
    )
    rows = store.stub_backlog()
    assert rows[0]["state"] == "fetch failed: mdpi.com 403 — will retry in 24h"


def test_stub_backlog_awaiting_filters_recent_attempts(store: Store) -> None:
    fresh = _stub(store, cite_key="fresh2024", doi="10.1/fresh")
    _fetch_event(store, fresh, "no_oa_version", hours_ago=1)  # tried recently
    stale = _stub(store, cite_key="stale2024", doi="10.1/stale")
    _fetch_event(store, stale, "no_oa_version", hours_ago=30)  # >24h ago
    never = _stub(store, cite_key="never2024", doi="10.1/never")  # never tried

    awaiting = {r["ref_id"] for r in store.stub_backlog(awaiting=True)}
    assert awaiting == {stale, never}
    assert fresh not in awaiting


def test_stub_backlog_limit(store: Store) -> None:
    for i in range(5):
        _stub(store, cite_key=f"p{i}2024", doi=f"10.1/{i}")
    assert len(store.stub_backlog(limit=2)) == 2


def test_stub_backlog_tolerates_duplicate_same_kind_identifiers(store: Store) -> None:
    # The ref_identifiers PK is (id_kind, id_value), so a ref may carry >1
    # identifier of the same kind. The backlog's per-kind scalar subqueries
    # must not assume uniqueness — a bare scalar subquery over duplicates
    # raises CardinalityViolation and 500s /papers-needed (regression).
    rid = _stub(store, cite_key="dup2024", doi="10.1/dup")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'cite_key', 'dup2024-alt', 'manual')",
            (rid,),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', '10.1/dup-alt', 'manual')",
            (rid,),
        )
    rows = store.stub_backlog()  # must not raise
    assert [r["ref_id"] for r in rows] == [rid]
    # MIN() picks a deterministic single value per kind.
    assert rows[0]["cite_key"] == "dup2024"
    assert rows[0]["identifier"] == "10.1/dup"


# ── dispatch: search(view='stubs') ──────────────────────────────────


def test_view_stubs_empty_message(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"view": "stubs"})
    assert "no stub papers" in out


def test_view_stubs_lists_backlog(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    rid = _stub(store, cite_key="needit2024", doi="10.1/needit")

    out = runtime_with_store.dispatch("search", {"view": "stubs"})
    assert "papers we still need to get" in out
    assert f"ref {rid}" in out
    assert "10.1/needit" in out
    assert "DREAM:acquire" in out  # the Next: block points at the tag view


def test_view_stubs_ignores_q(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    rid = _stub(store, cite_key="qignored2024", doi="10.1/qignored")

    out = runtime_with_store.dispatch(
        "search", {"view": "stubs", "q": "totally unrelated query"}
    )
    assert f"ref {rid}" in out
