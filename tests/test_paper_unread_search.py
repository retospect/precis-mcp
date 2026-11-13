"""``search(kind='paper', args={'unread'/'ingested_after'|
'ingested_before'})`` — gr653528.

The read-state cohort: ``unread`` keeps papers with no visible
``read-paper:`` closing note (the marker ``precis-read-paper``'s step 6
appends; a note hidden via ``delete(args={'comment': K})`` re-opens the
paper). ``ingested_after``/``ingested_before`` bound the *landing*
stamp — first live body chunk, ``refs.created_at`` for card-only or
chunkless refs — not publish year (``after=``/``before=``) or ref mint
(``since=``/``until=``). A daily recurring sweeps new landings; a retro
sweep pages the unread corpus under a tag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import precis.handlers.paper as paper_mod
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store, Tag
from precis.tools.command_parser import parse_command
from precis.utils import handle_registry
from tests.hintcheck import extract_hints

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stub-minting here, but the note-append door shares ``put``
    with the lookup paths — keep them stubbed like test_paper_notes."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"_resolved": True}
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", lambda *_: None)


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed(store: Store, slug: str, *, body: bool = True, tag: str | None = None) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=f"Title of {slug}",
        provider="manual",
        meta={},
    )
    if body:
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=f"body of {slug}")])
    if tag is not None:
        store.add_tag(ref.id, Tag.open(tag), set_by="agent")
    return ref.id


def _handle(ref_id: int) -> str:
    return handle_registry.format_handle("paper", ref_id)


def _read(handler: PaperHandler, slug: str) -> None:
    """Close a read-paper pass on ``slug`` the skill's way."""
    handler.put(id=slug, text="read-paper: setup me1; hubs fi2")


def _set_chunk_created(store: Store, ref_id: int, ts: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = %s WHERE ref_id = %s",
            (ts, ref_id),
        )


def _set_ref_created(store: Store, ref_id: int, ts: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (ts, ref_id),
        )


class TestUnread:
    def test_lists_only_papers_without_a_read_note(
        self, papers: PaperHandler, store: Store
    ) -> None:
        unread_a = _seed(store, "unread-alpha")
        read_b = _seed(store, "read-beta")
        _read(papers, "read-beta")
        body = papers.search(unread=True).body
        assert _handle(unread_a) in body and _handle(read_b) not in body, body

    def test_unread_false_keeps_read_papers(
        self, papers: PaperHandler, store: Store
    ) -> None:
        unread_a = _seed(store, "unread-alpha")
        read_b = _seed(store, "read-beta")
        _read(papers, "read-beta")
        body = papers.search(unread=False).body
        assert _handle(read_b) in body and _handle(unread_a) not in body, body

    def test_other_notes_do_not_close_the_pass(
        self, papers: PaperHandler, store: Store
    ) -> None:
        noted = _seed(store, "noted-but-unread")
        papers.put(id="noted-but-unread", text="an ordinary remark")
        body = papers.search(unread=True).body
        assert _handle(noted) in body, body

    def test_hidden_note_reopens_the_paper(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = _seed(store, "hidden-note")
        _read(papers, "hidden-note")
        body = papers.search(unread=True).body
        assert _handle(rid) not in body, body
        papers.delete(id="hidden-note", args={"comment": 1})
        body = papers.search(unread=True).body
        assert _handle(rid) in body, body

    def test_unread_is_paper_only(self, hub: Hub) -> None:
        from precis.handlers.cfp import CfpHandler

        with pytest.raises(BadInput, match="paper-only"):
            CfpHandler(hub=hub).search(unread=True)

    def test_unread_must_be_boolean(self, papers: PaperHandler) -> None:
        bad: Any = "yes"  # the runtime boundary sees args= values as JSON
        with pytest.raises(BadInput, match="boolean"):
            papers.search(unread=bad)

    def test_tags_narrow_the_unread_listing(
        self, papers: PaperHandler, store: Store
    ) -> None:
        on_topic = _seed(store, "anchor-paper", tag="area:test-anchor")
        off_topic = _seed(store, "off-anchor")
        _read(papers, "off-anchor")  # both off-topic AND read anyway
        body = papers.search(tags=["area:test-anchor"], unread=True).body
        assert _handle(on_topic) in body and _handle(off_topic) not in body, body

    def test_q_restricts_to_the_unread_cohort(
        self, papers: PaperHandler, store: Store
    ) -> None:
        _seed(store, "alpha-q")
        _seed(store, "beta-q")
        _read(papers, "beta-q")
        body = papers.search(q="body of", mode="lexical", unread=True).body
        # Paper-mode rows carry chunk handles (pc<N>); the surviving
        # hit's slug text is what distinguishes them.
        assert "alpha-q" in body and "beta-q" not in body, body

    def test_view_count_composes(self, papers: PaperHandler, store: Store) -> None:
        _seed(store, "unread-a")
        _seed(store, "unread-b")
        _seed(store, "read-c")
        _read(papers, "read-c")
        body = papers.search(view="count", unread=True).body
        assert "# 2 paper entries" in body, body

    def test_hit_shape_args_refuse_on_the_listing(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput):
            papers.search(unread=True, good=True)
        with pytest.raises(BadInput):
            papers.search(unread=True, queries=["a", "b"])

    def test_good_search_refuses_unread(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="deep-search"):
            papers.search(q="catalysis", good=True, unread=True)


class TestIngestedWindow:
    def test_landing_stamp_is_first_body_chunk(
        self, papers: PaperHandler, store: Store
    ) -> None:
        # A stub minted weeks ago whose body landed today is in today's
        # sweep — refs.created_at would miss it.
        rid = _seed(store, "late-body")
        old = datetime.now(UTC) - timedelta(days=30)
        _set_ref_created(store, rid, old)
        today = datetime.now(UTC)
        _set_chunk_created(store, rid, today)
        cut = (today - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = papers.search(ingested_after=cut).body
        assert _handle(rid) in body, body
        # …and since= (ref mint) does NOT see it — the two axes differ.
        body = papers.search(since=cut, unread=True).body
        assert _handle(rid) not in body, body

    def test_card_only_landing_uses_ref_created(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = _seed(store, "card-only", body=False)
        old = datetime.now(UTC) - timedelta(days=30)
        _set_ref_created(store, rid, old)
        cut = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        body = papers.search(ingested_after=cut).body
        assert _handle(rid) not in body, body

    def test_window_both_bounds(self, papers: PaperHandler, store: Store) -> None:
        recent = _seed(store, "recent-landing")
        old_one = _seed(store, "old-landing")
        _set_chunk_created(store, old_one, datetime(2020, 1, 1, tzinfo=UTC))
        body = papers.search(
            ingested_after="2025-01-01",
        ).body
        assert _handle(recent) in body and _handle(old_one) not in body, body
        body = papers.search(ingested_before="2021-01-01").body
        assert _handle(old_one) in body and _handle(recent) not in body, body

    def test_inverted_window_refuses(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="not before"):
            papers.search(ingested_after="2026-12-01", ingested_before="2026-01-01")

    def test_bad_timestamp_refuses(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="ISO"):
            papers.search(ingested_after="last tuesday")

    def test_unread_and_ingested_compose(
        self, papers: PaperHandler, store: Store
    ) -> None:
        fresh_unread = _seed(store, "fresh-unread")
        fresh_read = _seed(store, "fresh-read")
        stale_unread = _seed(store, "stale-unread")
        _set_chunk_created(store, stale_unread, datetime(2020, 1, 1, tzinfo=UTC))
        _read(papers, "fresh-read")
        body = papers.search(unread=True, ingested_after="2025-01-01").body
        assert _handle(fresh_unread) in body
        assert _handle(fresh_read) not in body
        assert _handle(stale_unread) not in body


class TestIdBounds:
    """``args={'id_min'/'id_max'}`` — the ref-id range: the listing's
    keyset cursor (a retro sweep walks a backlog deeper than the
    page_size cap, where ``page=`` refuses) and a standalone
    "papers since id N" browse."""

    def test_id_bounds_alone_list(self, papers: PaperHandler, store: Store) -> None:
        lo = _seed(store, "low")
        mid = _seed(store, "mid")
        hi = _seed(store, "hi")
        body = papers.search(id_min=mid).body
        assert _handle(mid) in body and _handle(hi) in body
        assert _handle(lo) not in body
        body = papers.search(id_max=mid).body
        assert _handle(lo) in body and _handle(mid) in body
        assert _handle(hi) not in body

    def test_inverted_range_refuses(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="id_min"):
            papers.search(id_min=10, id_max=5)

    def test_listing_orders_newest_ref_id_first(
        self, papers: PaperHandler, store: Store
    ) -> None:
        lo = _seed(store, "id-lo")
        hi = _seed(store, "id-hi")
        body = papers.search(unread=True).body
        assert body.index(_handle(hi)) < body.index(_handle(lo))

    def test_cursor_walks_past_page_size(
        self, papers: PaperHandler, store: Store
    ) -> None:
        ids = [_seed(store, f"walk-{i}") for i in range(6)]
        seen: list[str] = []
        cursor: int | None = None
        for _ in range(4):  # one page per call, then one exhausted pass
            body = papers.search(unread=True, id_max=cursor, page_size=2).body
            page_rows = [h for h in (_handle(i) for i in ids) if h in body]
            assert not set(page_rows) & set(seen), body
            seen += page_rows
            if "'id_max':" not in body:
                break
            cursor = min(i for i in ids if _handle(i) in body) - 1
        assert len(seen) == len(ids), seen

    def test_cursor_continuation_emits_id_max(
        self, papers: PaperHandler, store: Store
    ) -> None:
        ids = [_seed(store, f"cur-{i}") for i in range(3)]
        body = papers.search(unread=True, page_size=1).body
        # The page shows the newest row; the walk continues under it.
        newest = max(ids)
        assert _handle(newest) in body
        assert f"'id_max': {newest - 1}" in body, body

    def test_continuation_preserves_filters(
        self, papers: PaperHandler, store: Store
    ) -> None:
        for i in range(3):
            _seed(store, f"keep-{i}", tag="area:test-anchor")
        _seed(store, "other-tag")
        body = papers.search(tags=["area:test-anchor"], unread=True, page_size=1).body
        assert "tags=['area:test-anchor']" in body
        assert "'unread': true" in body
        assert "'id_max':" in body

    def test_continuation_replays_cohort_args(
        self, papers: PaperHandler, store: Store
    ) -> None:
        """Stamp / year-window / exclusion legs re-issue verbatim in the
        ``next:`` call — the review finding (gr653528): a continuation
        that drops them would walk a wider cohort than the caller set."""
        excluded_ref = store.insert_ref(
            kind="paper",
            slug="excluded-stamped",
            title="Title of excluded-stamped",
            provider="manual",
            meta={"fetch_state": "ok"},
            year=2024,
        )
        store.chunks.insert_chunks(excluded_ref.id, [ChunkInsert(ord=0, text="b")])
        stamped: list[int] = []
        for i in range(3):
            ref = store.insert_ref(
                kind="paper",
                slug=f"stamped-{i}",
                title=f"Title of stamped-{i}",
                provider="manual",
                meta={"fetch_state": "ok"},
                year=2024,
            )
            store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="b")])
            stamped.append(ref.id)
        body = papers.search(
            unread=True,
            stamp="fetch_state",
            after=2020,
            exclude=[_handle(excluded_ref.id)],
            page_size=1,
        ).body
        # Four stamped refs less the excluded one → the cohort totals 3.
        assert f" of {len(stamped)}" in body, body
        assert "'stamp': 'fetch_state'" in body, body
        assert "'unread': true" in body
        assert "after=2020" in body
        assert f"exclude={[_handle(excluded_ref.id)]!r}" in body
        assert "'id_max':" in body

    def test_id_bounds_compose_with_unread(
        self, papers: PaperHandler, store: Store
    ) -> None:
        ids = [_seed(store, f"u-{i}") for i in range(4)]
        body = papers.search(unread=True, id_min=ids[1], id_max=ids[2]).body
        assert _handle(ids[1]) in body and _handle(ids[2]) in body
        assert _handle(ids[0]) not in body and _handle(ids[3]) not in body

    def test_view_count_composes_id_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        ids = [_seed(store, f"c-{i}") for i in range(3)]
        body = papers.search(view="count", id_min=ids[1]).body
        assert "# 2 paper entries" in body, body

    def test_other_views_refuse_id_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = _seed(store, "status-target")
        with pytest.raises(BadInput, match="id_min"):
            papers.search(view="status", ids=[str(rid)], id_min=1)
        with pytest.raises(BadInput, match="id_min"):
            papers.search(view="ingest", days=7, id_min=1)

    def test_q_path_composes_id_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        lo = _seed(store, "q-lo")
        hi = _seed(store, "q-hi")
        body = papers.search(q="body of", mode="lexical", id_max=lo).body
        assert "q-lo" in body and "q-hi" not in body, body

    def test_tied_ingest_stamps_still_page(
        self, papers: PaperHandler, store: Store
    ) -> None:
        # A bulk ingest mints chunk created_at ties inside one
        # transaction — the landing stamp can't order them, but the
        # ref-id cursor still walks every row exactly once.
        ids = [_seed(store, f"tied-{i}") for i in range(4)]
        tied = datetime(2026, 1, 1, tzinfo=UTC)
        for rid in ids:
            _set_chunk_created(store, rid, tied)
        seen: set[str] = set()
        cursor: int | None = None
        for _ in range(5):
            body = papers.search(
                ingested_after="2025-12-01", id_max=cursor, page_size=2
            ).body
            page_rows = {h for h in (_handle(i) for i in ids) if h in body}
            assert not (page_rows & seen), body
            seen |= page_rows
            shown_ids = [i for i in ids if _handle(i) in body]
            if "'id_max':" not in body:
                break
            cursor = min(shown_ids) - 1
        assert len(seen) == len(ids), seen


class TestDoiScope:
    """gr653528 review: the bare-DOI fast path (gr244678) promoted the
    resolved paper even when the call's ref-level filters excluded it —
    a read paper on an ``unread`` sweep, an out-of-range id, an
    out-of-scope dispatch cohort. The DOI now reports "already in the
    corpus … excluded by the filters", never the paper itself."""

    @staticmethod
    def _seed_doi(store: Store, slug: str, doi: str) -> int:
        rid = _seed(store, slug)
        store.insert_ref_identifiers(rid, [("doi", doi, "manual")])
        return rid

    def test_doi_plain_still_promotes(self, papers: PaperHandler, store: Store) -> None:
        rid = self._seed_doi(store, "doi-held", "10.5555/held.1")
        body = papers.search(q="10.5555/held.1").body
        assert _handle(rid) in body, body

    @staticmethod
    def _assert_filtered(body: str, rid: int) -> None:
        """The ref is filtered out as a *result* — the empty-hits
        guidance still names its handle (honesty, not a hit)."""
        assert "no paper blocks match" in body, body
        assert f"corpus as '{_handle(rid)}'" in body, body
        assert "excluded" in body, body

    def test_doi_outside_include_scope_filtered(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = self._seed_doi(store, "doi-in", "10.5555/in.1")
        other = _seed(store, "doi-other")
        body = papers.search(q="10.5555/in.1", include_ref_ids=[other]).body
        self._assert_filtered(body, rid)

    def test_doi_empty_include_scope_is_zero_hits(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = self._seed_doi(store, "doi-empty", "10.5555/empty.1")
        body = papers.search(q="10.5555/empty.1", include_ref_ids=[]).body
        self._assert_filtered(body, rid)

    def test_doi_respects_unread(self, papers: PaperHandler, store: Store) -> None:
        rid = self._seed_doi(store, "doi-read", "10.5555/read.1")
        _read(papers, "doi-read")
        body = papers.search(q="10.5555/read.1", unread=True).body
        self._assert_filtered(body, rid)
        # unread=False keeps exactly the read cohort — the same ref
        # resolves again once it satisfies the filter.
        body = papers.search(q="10.5555/read.1", unread=False).body
        assert _handle(rid) in body, body

    def test_doi_respects_ingested_window(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = self._seed_doi(store, "doi-old", "10.5555/old.1")
        _set_chunk_created(store, rid, datetime(2020, 1, 1, tzinfo=UTC))
        body = papers.search(q="10.5555/old.1", ingested_after="2025-01-01").body
        self._assert_filtered(body, rid)

    def test_doi_respects_id_bounds(self, papers: PaperHandler, store: Store) -> None:
        rid = self._seed_doi(store, "doi-id", "10.5555/id.1")
        body = papers.search(q="10.5555/id.1", id_max=rid - 1).body
        self._assert_filtered(body, rid)
        body = papers.search(q="10.5555/id.1", id_min=rid).body
        assert _handle(rid) in body, body

    def test_doi_respects_exclude_channel(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = self._seed_doi(store, "doi-ex", "10.5555/ex.1")
        body = papers.search(q="10.5555/ex.1", exclude_ref_ids=[rid]).body
        self._assert_filtered(body, rid)

    def test_doi_respects_junk_verdict(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = self._seed_doi(store, "doi-junk", "10.5555/junk.1")
        store.add_tag(rid, Tag.closed("JUNK", "junk"))
        body = papers.search(q="10.5555/junk.1").body
        self._assert_filtered(body, rid)


class TestViewRefusals:
    """The fixed views (``status``/``ingest``) render a set the
    read-state and ingest-window args can't express — they refuse
    rather than silently drop them (gr653528 review)."""

    def test_status_refuses_unread(self, papers: PaperHandler, store: Store) -> None:
        rid = _seed(store, "status-rs")
        with pytest.raises(BadInput, match="unread"):
            papers.search(view="status", ids=[str(rid)], unread=True)

    def test_ingest_refuses_window(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="ingested"):
            papers.search(view="ingest", days=7, ingested_after="2026-01-01")

    def test_status_refuses_ingested_before(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = _seed(store, "status-ib")
        with pytest.raises(BadInput, match="ingested"):
            papers.search(view="status", ids=[str(rid)], ingested_before="2027-01-01")

    def test_status_refuses_byline(self, papers: PaperHandler, store: Store) -> None:
        rid = _seed(store, "status-bl")
        with pytest.raises(BadInput, match="title"):
            papers.search(view="status", ids=[str(rid)], title="Title")


class TestSearchHitsScope:
    """``search_hits`` — the structured leg the cross-kind fan-out and
    agentic callers use — honours the same read-state / id-bound args
    (gr653528 review), folded onto the include channel before the
    chunk query."""

    def test_search_hits_honours_unread(
        self, papers: PaperHandler, store: Store
    ) -> None:
        a = _seed(store, "hits-unread")
        b = _seed(store, "hits-read")
        _read(papers, "hits-read")
        hits = papers.search_hits(q="body of", unread=True, mode="lexical")
        ids = {h.ref_id for h in hits}
        assert a in ids, ids
        assert b not in ids, ids

    def test_search_hits_honours_id_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        a = _seed(store, "hits-a")
        b = _seed(store, "hits-b")
        hits = papers.search_hits(q="body of", id_max=a, mode="lexical")
        ids = {h.ref_id for h in hits}
        assert a in ids, ids
        assert b not in ids, ids


class TestBoundEcho:
    """Window bounds echo in the scope label / continuation as UTC
    ``Z`` stamps with fractional seconds kept (an offset-aware input
    must not print its local hour under ``Z``)."""

    def test_offset_bound_echoes_as_utc(
        self, papers: PaperHandler, store: Store
    ) -> None:
        _seed(store, "offset-bound")
        body = papers.search(ingested_after="2026-01-01T10:00:00+02:00").body
        assert "2026-01-01T08:00:00Z" in body, body
        assert "10:00:00" not in body, body

    def test_fractional_seconds_kept(self, papers: PaperHandler, store: Store) -> None:
        _seed(store, "frac")
        body = papers.search(
            ingested_after="2026-01-01T00:00:00.250000Z",
            ingested_before="2026-01-02T00:00:00.750000Z",
        ).body
        assert "2026-01-01T00:00:00.250000Z" in body, body
        assert "2026-01-02T00:00:00.750000Z" in body, body


class TestRankedHintEcho:
    """gr653528 review — the ranked path's ``Next:`` hints re-issue the
    cohort verbatim (``BlockSearchResult.cohort_args`` /
    ``cohort_kwargs`` / ``cohort_note``): a follow-up that dropped
    ``unread``/ingest/id bounds (or tags/exclude/year bounds) would
    silently widen the retro sweep to read or out-of-window papers.
    Dispatch-injected scopes can't respell — the hint's note names
    them instead, the read-state listing's ``injected_scope``
    discipline."""

    def test_drill_and_steer_hints_keep_unread(
        self, papers: PaperHandler, store: Store
    ) -> None:
        for i in range(4):
            _seed(store, f"echo-{i}")
        _read(papers, "echo-3")  # read papers stay out of the cohort
        body = papers.search(q="body of", mode="lexical", unread=True, page_size=2).body
        # The paper→chunk drill AND the tighten-the-query steer both
        # carry the read-state filter (and the mode).
        assert "args={'unread': True}, mode='lexical', unique_per='chunk'" in body, body
        assert "<salient term>', args={'unread': True}, mode='lexical'" in body, body
        assert "echo-3" not in body, body

    def test_hints_reissue_window_and_id_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        ids = [_seed(store, f"win-{i}") for i in range(5)]
        body = papers.search(
            q="body of",
            mode="lexical",
            unread=True,
            ingested_after="2020-01-01",
            id_min=ids[0],
            id_max=ids[4],
            page_size=2,
        ).body
        assert "'unread': True" in body
        assert "'ingested_after': '2020-01-01T00:00:00Z'" in body, body
        assert f"'id_min': {ids[0]}" in body
        assert f"'id_max': {ids[4]}" in body

    def test_hints_reissue_tags_and_exclude(
        self, papers: PaperHandler, store: Store
    ) -> None:
        for i in range(4):
            _seed(store, f"tag-{i}", tag="area:test-anchor")
        _seed(store, "tag-x", tag="area:test-anchor")
        body = papers.search(
            q="body of",
            mode="lexical",
            tags=["area:test-anchor"],
            exclude=["tag-x"],
            page_size=2,
        ).body
        assert "tags=['area:test-anchor']" in body, body
        assert "exclude=['tag-x']" in body, body
        # …and the excluded paper is not itself a hit.
        assert "tag-x" not in body.split("Next:")[0], body

    def test_widen_hint_keeps_cohort_on_empty_result(
        self, papers: PaperHandler, store: Store
    ) -> None:
        _seed(store, "empty-seed")
        body = papers.search(
            q="unmatched-xyzzy",
            mode="lexical",
            unread=True,
            ingested_after="2020-01-01",
            after=2000,
            exclude=["empty-seed"],
        ).body
        assert "no paper blocks match" in body, body
        assert "page_size=50" in body
        assert "'unread': True" in body
        assert "'ingested_after': '2020-01-01T00:00:00Z'" in body
        assert "exclude=['empty-seed']" in body
        assert "after=2000" in body

    def test_title_match_byline_hint_keeps_cohort(
        self, papers: PaperHandler, store: Store
    ) -> None:
        # Chunkless ref: the title callout lands but no block promotes,
        # so the empty branch offers the byline follow-up — which must
        # still carry the read-state filter.
        _seed(store, "bli-one", body=False)
        body = papers.search(q="Title of bli-one", unread=True).body
        assert "no paper blocks match" in body, body
        assert "'title': 'Title of bli-one'" in body, body
        assert "'unread': True" in body, body

    def test_injected_scope_hint_names_the_scope(
        self, papers: PaperHandler, store: Store
    ) -> None:
        """A dispatch-resolved cohort (cited=/uncited=/hubbed=) has no
        ``args=`` spelling — the hints say to re-add the scope."""
        ids = [_seed(store, f"inj-{i}") for i in range(3)]
        body = papers.search(
            q="body of", mode="lexical", include_ref_ids=ids, page_size=2
        ).body
        assert "re-add your cited=/uncited=/hubbed= scope" in body, body

    def test_followed_drill_hint_stays_in_cohort(
        self,
        papers: PaperHandler,
        store: Store,
        runtime_with_store: Any,
    ) -> None:
        """Round-trip the emitted call through the real parser + the
        real dispatcher (which flattens ``args={…}``): the chunk drill
        lands the same unread cohort, never a widened one."""
        for i in range(4):
            _seed(store, f"rt-{i}")
        _read(papers, "rt-3")
        body = papers.search(q="body of", mode="lexical", unread=True, page_size=2).body
        drill = [h for h in extract_hints(body) if "unique_per='chunk'" in h]
        assert drill, body
        verb, kwargs = parse_command(drill[0])
        assert verb == "search"
        # The MCP boundary flattens ``args={…}`` into kwargs before the
        # dispatcher sees them — mirror that here (the dispatcher takes
        # only declared kwargs).
        flat = {**kwargs.pop("args", {}), **kwargs}
        out = runtime_with_store.dispatch(verb, flat)
        for i in (0, 1, 2):
            assert f"rt-{i}" in out, out
        assert "rt-3" not in out, out
