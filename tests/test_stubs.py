"""The "papers we still need to get" backlog.

Two layers:

- store engine (``Store.stub_backlog``): the stub predicate
  (``pdf_sha256 IS NULL`` + an external id), the latest-fetcher-event
  join, the ``awaiting`` filter, and the one-line ``state`` summary.
- dispatch (``search(view='stubs')``): routing, rendering, ``q=``
  ignored, paper-only — end to end through ``runtime.dispatch``.

Shared by ``precis stubs`` (CLI) and the MCP view, so this guards both
(the stub surfaces; ``store/_stub_predicate.py``).
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import Tag


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


def _acquire_flag(store: Store, ref_id: int, value: str) -> None:
    """Set an acquisition-provenance OPEN flag (as /papers-needed does)."""
    store.add_tag(ref_id, Tag.open(value))


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


def test_stub_backlog_ungettable_both_routes_sink_to_back(store: Store) -> None:
    # A is the oldest request (would normally sort first), but it's been
    # marked unreachable via BOTH manual routes — so it sinks to the back.
    a = _stub(store, cite_key="a2024", doi="10.1/a")
    b = _stub(store, cite_key="b2024", doi="10.1/b")
    c = _stub(store, cite_key="c2024", doi="10.1/c")
    _acquire_flag(store, a, "cant-get-uol")
    _acquire_flag(store, a, "cant-get-scholar")
    order = [r["ref_id"] for r in store.stub_backlog()]
    assert order == [b, c, a]


def test_stub_backlog_single_route_flag_keeps_position(store: Store) -> None:
    # Only one route tried (UoL) — not enough to deprioritize; the oldest
    # stub keeps its front-of-list position.
    a = _stub(store, cite_key="a2024", doi="10.1/a")
    b = _stub(store, cite_key="b2024", doi="10.1/b")
    _acquire_flag(store, a, "cant-get-uol")
    order = [r["ref_id"] for r in store.stub_backlog()]
    assert order == [a, b]


def test_stub_backlog_is_book_flag_sinks_to_back(store: Store) -> None:
    # A book isn't a paper we chase through the OA fetcher — flagging it
    # is-book sinks the oldest stub to the back on its own.
    a = _stub(store, cite_key="a2024", doi="10.1/a")
    b = _stub(store, cite_key="b2024", doi="10.1/b")
    c = _stub(store, cite_key="c2024", doi="10.1/c")
    _acquire_flag(store, a, "is-book")
    order = [r["ref_id"] for r in store.stub_backlog()]
    assert order == [b, c, a]


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


# ── store engine: retraction exclusion ────────────────────────────────


def test_stub_backlog_excludes_retracted(store: Store) -> None:
    # A stub already stamped retracted has nothing worth fetching an OA
    # copy for — it must drop out of the backlog entirely, not just get
    # deprioritized.
    retracted = _stub(store, cite_key="retracted2024", doi="10.1/retracted")
    store.set_retraction_status(retracted, status="retracted")
    want = _stub(store, cite_key="want2024b", doi="10.1/wantb")
    assert [r["ref_id"] for r in store.stub_backlog()] == [want]


def test_stub_backlog_keeps_null_retraction_status(store: Store) -> None:
    # NULL == never checked — the state every stub starts in — must stay
    # eligible so the fetch-time gate actually gets a chance to check it.
    unchecked = _stub(store, cite_key="unchecked2024", doi="10.1/unchecked")
    assert [r["ref_id"] for r in store.stub_backlog()] == [unchecked]


def test_stub_backlog_keeps_non_retracted_statuses(store: Store) -> None:
    # 'corrected' / 'expression_of_concern' are informational, not a
    # fetch-blocker — only the exact 'retracted' literal excludes.
    corrected = _stub(store, cite_key="corrected2024", doi="10.1/corrected")
    store.set_retraction_status(corrected, status="corrected")
    concern = _stub(store, cite_key="concern2024", doi="10.1/concern")
    store.set_retraction_status(concern, status="expression_of_concern")
    rows = {r["ref_id"] for r in store.stub_backlog()}
    assert rows == {corrected, concern}


def test_stub_backlog_count_excludes_retracted(store: Store) -> None:
    retracted = _stub(store, cite_key="cnt_retracted2024", doi="10.1/cntretracted")
    store.set_retraction_status(retracted, status="retracted")
    _stub(store, cite_key="cnt_want2024", doi="10.1/cntwant")
    assert store.stub_backlog_count() == 1


# ── store engine: id_kinds / sort (Part 2) ───────────────────────────


def test_stub_backlog_id_kinds_doi_only_excludes_other_kinds(store: Store) -> None:
    doi_only = _stub(store, cite_key="doi2024", doi="10.1/doi")
    _stub(store, cite_key="arxiv2024", arxiv="2401.00002")
    rows = store.stub_backlog(id_kinds=("doi",))
    assert [r["ref_id"] for r in rows] == [doi_only]


def test_stub_backlog_id_kinds_doi_only_excludes_s2_only(store: Store) -> None:
    doi_only = _stub(store, cite_key="doi2024b", doi="10.1/doib")
    ref = store.insert_ref(kind="paper", slug="s2only2024", title="X", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 's2', 's2abc', 'manual')",
            (ref.id,),
        )
    rows = store.stub_backlog(id_kinds=("doi",))
    assert [r["ref_id"] for r in rows] == [doi_only]


def test_stub_backlog_sort_last_tried_never_tried_first(store: Store) -> None:
    # `tried` was attempted recently; `never` has no fetcher event at all.
    # oldest-request-first would put `tried` on top (created first); the
    # last-tried sort must put the never-attempted one on top instead.
    tried = _stub(store, cite_key="tried2024", doi="10.1/tried")
    _fetch_event(store, tried, "no_oa_version", hours_ago=1)
    never = _stub(store, cite_key="never2024b", doi="10.1/neverb")
    order = [r["ref_id"] for r in store.stub_backlog(sort="last-tried")]
    assert order == [never, tried]


def test_stub_backlog_sort_last_tried_orders_by_oldest_attempt(store: Store) -> None:
    a = _stub(store, cite_key="a2024b", doi="10.1/ab")
    b = _stub(store, cite_key="b2024b", doi="10.1/bb")
    _fetch_event(store, a, "no_oa_version", hours_ago=1)  # tried 1h ago
    _fetch_event(store, b, "no_oa_version", hours_ago=5)  # tried 5h ago (older)
    order = [r["ref_id"] for r in store.stub_backlog(sort="last-tried")]
    assert order == [b, a]  # longest-since-tried first


def test_stub_backlog_sort_default_unchanged(store: Store) -> None:
    # Omitting id_kinds/sort reproduces the original oldest-request-first
    # behavior — default args must not shift existing callers.
    a = _stub(store, cite_key="a2024c", doi="10.1/ac")
    b = _stub(store, cite_key="b2024c", arxiv="2401.00003")
    assert [r["ref_id"] for r in store.stub_backlog()] == [a, b]


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


# ── dispatch: search(view='chase-queue') ─────────────────────────────


def test_view_chase_queue_empty_message(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"view": "chase-queue"})
    assert "no DOI-only stubs" in out


def test_view_chase_queue_is_doi_only(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    doi_rid = _stub(store, cite_key="cq_doi2024", doi="10.1/cqdoi")
    arxiv_rid = _stub(store, cite_key="cq_arxiv2024", arxiv="2401.00004")

    out = runtime_with_store.dispatch("search", {"view": "chase-queue"})
    assert "chase queue" in out
    assert f"ref {doi_rid}" in out
    assert f"ref {arxiv_rid}" not in out


def test_view_chase_queue_never_tried_first(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    tried = _stub(store, cite_key="cq_tried2024", doi="10.1/cqtried")
    _fetch_event(store, tried, "no_oa_version", hours_ago=1)
    never = _stub(store, cite_key="cq_never2024", doi="10.1/cqnever")

    out = runtime_with_store.dispatch("search", {"view": "chase-queue"})
    assert out.index(f"ref {never}") < out.index(f"ref {tried}")


def test_view_chase_queue_ignores_q(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    rid = _stub(store, cite_key="cq_qignored2024", doi="10.1/cqqignored")

    out = runtime_with_store.dispatch(
        "search", {"view": "chase-queue", "q": "totally unrelated query"}
    )
    assert f"ref {rid}" in out


# ── store engine: requeue_stubs_for_fetch (Part 3) ───────────────────


def _has_oa_requeued(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'oa_requeued' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row and row[0])


def test_requeue_stubs_for_fetch_stamps_never_tried_doi_stubs(store: Store) -> None:
    a = _stub(store, cite_key="rq_a2024", doi="10.1/rqa")
    b = _stub(store, cite_key="rq_b2024", doi="10.1/rqb")
    # An arxiv-only stub is out of scope (requeue is DOI-only).
    _stub(store, cite_key="rq_c2024", arxiv="2401.00005")

    stamped = store.requeue_stubs_for_fetch(limit=25)
    assert stamped == 2
    assert _has_oa_requeued(store, a)
    assert _has_oa_requeued(store, b)

    events = store.events_for(a)
    assert any(e.event == "oa_requeued" for e in events)


def test_requeue_stubs_for_fetch_caps_at_limit(store: Store) -> None:
    for i in range(5):
        _stub(store, cite_key=f"rq_lim{i}2024", doi=f"10.1/rqlim{i}")
    stamped = store.requeue_stubs_for_fetch(limit=2)
    assert stamped == 2


def test_requeue_stubs_for_fetch_skips_already_stamped(store: Store) -> None:
    a = _stub(store, cite_key="rq_skip2024", doi="10.1/rqskip")
    first = store.requeue_stubs_for_fetch(limit=25)
    assert first == 1
    assert _has_oa_requeued(store, a)

    # A second call must not re-stamp / double-count the already-stamped stub.
    second = store.requeue_stubs_for_fetch(limit=25)
    assert second == 0


def test_requeue_stubs_for_fetch_excludes_already_tried(store: Store) -> None:
    tried = _stub(store, cite_key="rq_tried2024", doi="10.1/rqtried")
    _fetch_event(store, tried, "no_oa_version", hours_ago=1)
    stamped = store.requeue_stubs_for_fetch(limit=25)
    assert stamped == 0
    assert not _has_oa_requeued(store, tried)


def test_requeue_stubs_for_fetch_ref_ids_scopes_to_one_stub(store: Store) -> None:
    """``ref_ids=`` (the Sources/Cited tabs' single-paper Fetch) stamps
    only the named stub even when other eligible stubs exist."""
    a = _stub(store, cite_key="rq_scope_a2024", doi="10.1/rqscopea")
    b = _stub(store, cite_key="rq_scope_b2024", doi="10.1/rqscopeb")
    stamped = store.requeue_stubs_for_fetch(ref_ids=[a])
    assert stamped == 1
    assert _has_oa_requeued(store, a)
    assert not _has_oa_requeued(store, b)


def test_requeue_stubs_for_fetch_id_kinds_widens_to_s2_only_stub(store: Store) -> None:
    """The single-ref caller widens ``id_kinds`` past the batch button's
    DOI-only default so an S2-only stub (no DOI resolved yet) still gets
    stamped."""
    ref = store.insert_ref(kind="paper", slug="rq_s2only2024", title="X", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 's2', 'abc123', 'manual')",
            (ref.id,),
        )
    # DOI-only default (the batch caller's behaviour) skips it...
    assert store.requeue_stubs_for_fetch(ref_ids=[ref.id]) == 0
    # ...but widening id_kinds picks it up.
    stamped = store.requeue_stubs_for_fetch(
        ref_ids=[ref.id], id_kinds=("doi", "arxiv", "s2")
    )
    assert stamped == 1
    assert _has_oa_requeued(store, ref.id)
