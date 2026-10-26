"""``search(kind='paper', view='count'|'status'|'ingest')`` —
gr500649 (count), gr500651 (bulk fetch status), gr500643 (ingest rate).

Full-text = a live non-card, non-``references`` body chunk; ``card_*``
chunks (``ord < 0``) are abstract/title cards only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk

pytestmark = pytest.mark.db


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _paper(
    store: Store, slug: str, *, body: int = 0, card: bool = False, refs: int = 0
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug, year=2024)
    if card:
        seed_chunk(
            store,
            ref_id=ref.id,
            ord=-1,
            chunk_kind="card_abstract",
            text="abstract card",
        )
    for i in range(body):
        seed_chunk(store, ref_id=ref.id, ord=i, text=f"body paragraph {i} of {slug}")
    for j in range(refs):
        seed_chunk(
            store, ref_id=ref.id, ord=body + j, chunk_kind="references", text="[1] Ref"
        )
    return ref.id


def _event(store: Store, ref_id: int, source: str, event: str, ts: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "VALUES (%s, %s, %s, '{}'::jsonb, %s)",
            (ref_id, source, event, ts),
        )
        conn.commit()


def _backdate(store: Store, ref_id: int, when: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.execute(
            "UPDATE chunks SET created_at = %s WHERE ref_id = %s", (when, ref_id)
        )
        conn.commit()


class TestCount:
    def test_total_and_tag_filter(self, papers: PaperHandler, store: Store) -> None:
        a = _paper(store, "alpha2024a", body=1)
        _paper(store, "beta2024b", body=1)
        store.add_tag(a, Tag.open("studytype:review"), set_by="agent")
        assert papers.search(view="count").body.startswith("# 2 paper entries")
        resp = papers.search(view="count", tags=["studytype:review"])
        assert resp.body.startswith("# 1 paper entry"), resp.body

    def test_group_by_tag_axis(self, papers: PaperHandler, store: Store) -> None:
        a = _paper(store, "alpha2024a", body=1)
        _paper(store, "beta2024b", body=1)
        store.add_tag(a, Tag.open("studytype:review"), set_by="agent")
        body = papers.search(view="count", group_by="tag:studytype").body
        assert "studytype:review: 1" in body or "review: 1" in body, body
        assert "(none): 1" in body, body

    def test_group_by_fulltext(self, papers: PaperHandler, store: Store) -> None:
        """gr518168: ``group_by='fulltext'`` buckets papers full-text /
        card-only / stub and trails a passages line — the "how many
        full-text papers and passages" question needed prod-psql."""
        _paper(store, "ft2024a", body=2, card=True, refs=1)  # references ≠ full
        _paper(store, "co2024b", card=True)
        _paper(store, "st2024c")
        body = papers.search(view="count", group_by="fulltext").body
        assert body.startswith("# 3 paper entries"), body
        assert "fulltext:" in body
        assert "  full-text: 1" in body
        assert "  card-only: 1" in body
        assert "  stub: 1" in body
        # passages = live body chunks (ord>=0, non-references): 2.
        assert "passages: 2 live body chunks" in body

    def test_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _paper(store, "alpha2024a", body=1)
        body = runtime_with_store.dispatch("search", {"kind": "paper", "view": "count"})
        assert body.startswith("# 1 paper entry"), body

    def test_unknown_view_refused_with_next(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput) as ei:
            papers.search(view="bogus")
        assert "count" in (ei.value.options or [])
        assert "view='status'" in (ei.value.next or "")


class TestFetchStatus:
    def test_body_states_and_last_fetch(
        self, papers: PaperHandler, store: Store
    ) -> None:
        full = _paper(store, "full2024a", body=2, card=True, refs=1)
        card = _paper(store, "card2024b", card=True)
        stub = _paper(store, "stub2024c")
        refs_only = _paper(store, "refsonly2024d", card=True, refs=3)
        now = datetime.now(UTC)
        _event(store, stub, "fetcher:oa", "no_oa_version", now - timedelta(hours=3))
        _event(store, stub, "fetcher:oa", "fetch_failed", now - timedelta(hours=1))
        ids = [
            handle_registry.format_handle("paper", i)
            for i in (full, card, stub, refs_only)
        ]
        body = papers.search(view="status", ids=ids).body
        by_slug = {}
        for line in body.splitlines():
            m = re.search(r"\[(\w+)\]\s+(full-text|card-only|stub)", line)
            if m:
                by_slug[m.group(1)] = (m.group(2), line)
        assert by_slug["full2024a"][0] == "full-text"
        assert "(2 body chunks)" in by_slug["full2024a"][1]
        assert by_slug["card2024b"][0] == "card-only"
        assert by_slug["refsonly2024d"][0] == "card-only"  # references != full text
        assert by_slug["stub2024c"][0] == "stub"
        assert "fetch_failed" in by_slug["stub2024c"][1]  # newest, not first
        assert "never tried" in by_slug["card2024b"][1]
        assert "summary: 1 card-only" not in body  # two card-only
        assert "2 card-only · 1 full-text · 1 stub" in body

    def test_unresolved_counted_and_slugs_accepted(
        self, papers: PaperHandler, store: Store
    ) -> None:
        _paper(store, "full2024a", body=1)
        body = papers.search(view="status", ids=["full2024a", "pa99999999"]).body
        assert "1 of 2 ids resolved" in body, body
        assert "1 unresolved" in body, body

    def test_needs_ids(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput) as ei:
            papers.search(view="status")
        assert "ids=" in str(ei.value)


class TestIngestRate:
    def test_full_vs_card_per_day(self, papers: PaperHandler, store: Store) -> None:
        now = datetime.now(UTC)
        today = now.date()
        old = now - timedelta(days=3)
        f1 = _paper(store, "fullnow2024a", body=2, card=True)
        _paper(store, "cardnow2024b", card=True)
        f2 = _paper(store, "fullold2024c", body=1, card=True)
        c2 = _paper(store, "cardold2024d", card=True)
        _backdate(store, f2, old)
        _backdate(store, c2, old)
        _paper(store, "stub2024e")  # no chunks: counted nowhere
        _paper(store, "fullnow2024f", body=1)
        _backdate(
            store, _paper(store, "ancient2024g", body=1), now - timedelta(days=60)
        )
        del f1
        body = papers.search(view="ingest", days=14).body
        assert f"{today}: 2 full-text · 1 card-only" in body, body
        assert f"{old.date()}: 1 full-text · 1 card-only" in body, body
        assert (
            "ancient" not in body and str(now.date() - timedelta(days=60)) not in body
        )
        assert re.search(r"last 14 days .*: \d+ full-text · \d+ card-only", body)

    def test_days_validated(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput) as ei:
            papers.search(view="ingest", days=0)
        assert "days" in str(ei.value)


class TestStampSearch:
    """gr517627: ``args={'stamp': '<meta key>'}`` lists the live papers
    carrying that ``refs.meta`` worker/enrichment stamp — ids + count,
    so a stamp cohort is verifiable without SQL."""

    def _stamped(self, store: Store, slug: str, stamp: str) -> int:
        return store.insert_ref(
            kind="paper",
            slug=slug,
            title=slug,
            meta={stamp: {"v": 3, "at": "2026-10-25"}},
        ).id

    def test_ids_and_count(self, papers: PaperHandler, store: Store) -> None:
        a = self._stamped(store, "markeda2024", "context_sentence_failed")
        _paper(store, "unstamped2024", body=1)
        body = papers.search(stamp="context_sentence_failed").body
        assert body.startswith("# 1 paper entry stamped 'context_sentence_failed'"), (
            body
        )
        assert handle_registry.format_handle("paper", a) in body

    def test_cohort_total_in_headline(self, papers: PaperHandler, store: Store) -> None:
        a = self._stamped(store, "cohort-one", "s2_enriched_at")
        b = self._stamped(store, "cohort-two", "s2_enriched_at")
        _paper(store, "unstamped2024", body=1)
        body = papers.search(stamp="s2_enriched_at", page_size=1).body
        assert " of 2" in body, body
        assert "page=2" in body
        page2 = papers.search(stamp="s2_enriched_at", page_size=1, page=2).body
        handles = {handle_registry.format_handle("paper", i) for i in (a, b)}
        assert sum(h in body for h in handles) == 1
        assert sum(h in page2 for h in handles) == 1
        assert {h for h in handles if h in body or h in page2} == handles

    def test_unstamped_ref_excluded_and_retired_excluded(
        self, papers: PaperHandler, store: Store
    ) -> None:
        a = self._stamped(store, "markedlive2024", "context_sentence_no_abstract")
        dead = self._stamped(store, "markeddead2024", "context_sentence_no_abstract")
        store.retire_ref(dead)
        _paper(store, "unstamped2024", body=1)
        body = papers.search(stamp="context_sentence_no_abstract").body
        assert body.startswith(
            "# 1 paper entry stamped 'context_sentence_no_abstract'"
        ), body
        assert handle_registry.format_handle("paper", a) in body
        assert handle_registry.format_handle("paper", dead) not in body

    def test_no_cohort(self, papers: PaperHandler, store: Store) -> None:
        _paper(store, "plain2024", body=1)
        body = papers.search(stamp="never_stamped").body
        assert "no live paper entries stamped 'never_stamped'" in body

    def test_no_q_filters_apply(self, papers: PaperHandler, store: Store) -> None:
        """Ruling: a no-q stamp listing is not an early return —
        tags=/serves=/record_type= still narrow the cohort, and the
        headline count is the filtered total."""
        tagged = self._stamped(store, "stagged2024", "context_sentence_failed")
        untagged = self._stamped(store, "stuntagged2024", "context_sentence_failed")
        store.add_tag(tagged, Tag.open("studytype:review"), set_by="agent")

        body = papers.search(
            stamp="context_sentence_failed", tags=["studytype:review"]
        ).body
        assert body.startswith("# 1 paper entry"), body
        assert handle_registry.format_handle("paper", tagged) in body
        assert handle_registry.format_handle("paper", untagged) not in body

        # record_type= narrows the same cohort.
        ds = store.insert_ref(
            kind="paper",
            slug="styped2024",
            title="styped2024",
            meta={"context_sentence_failed": True, "record_type": "dataset"},
        ).id
        body = papers.search(
            stamp="context_sentence_failed", record_type="dataset"
        ).body
        assert body.startswith("# 1 paper entry"), body
        assert handle_registry.format_handle("paper", ds) in body
        assert handle_registry.format_handle("paper", tagged) not in body

        # serves= narrows to the quest's inbound serves edges.
        quest = store.insert_ref(kind="quest", slug=None, title="q1")
        store.add_link(
            src_ref_id=untagged,
            dst_ref_id=quest.id,
            relation="serves",
            set_by="agent",
        )
        body = papers.search(
            stamp="context_sentence_failed", serves=f"quest:{quest.id}"
        ).body
        assert body.startswith("# 1 paper entry"), body
        assert handle_registry.format_handle("paper", untagged) in body
        assert handle_registry.format_handle("paper", tagged) not in body

    def test_no_q_has_fulltext_filter(self, papers: PaperHandler, store: Store) -> None:
        full = _paper(store, "stfull2024", body=1)
        stub = self._stamped(store, "ststub2024", "context_sentence_failed")
        store.stamp_ref_meta(full, {"context_sentence_failed": True})
        body = papers.search(stamp="context_sentence_failed", has_fulltext=True).body
        assert body.startswith("# 1 paper entry"), body
        assert handle_registry.format_handle("paper", full) in body
        assert handle_registry.format_handle("paper", stub) not in body

    def test_empty_stamp_refused(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput):
            papers.search(stamp="  ")
        non_str: Any = True  # args= can carry a non-str over JSON
        with pytest.raises(BadInput):
            papers.search(stamp=non_str)

    def test_no_q_id_channels_and_date_bounds(
        self, papers: PaperHandler, store: Store
    ) -> None:
        """r2 S2: the no-q stamp listing applies the include/exclude id
        channels, exclude=, after=/before= and since=/until= rather than
        dropping them — an empty include list means zero hits."""
        a = self._stamped(store, "stina2024", "context_sentence_failed")
        b = self._stamped(store, "stinb2024", "context_sentence_failed")
        h_a = handle_registry.format_handle("paper", a)
        h_b = handle_registry.format_handle("paper", b)

        body = papers.search(stamp="context_sentence_failed", include_ref_ids=[a]).body
        assert h_a in body and h_b not in body, body
        empty = papers.search(stamp="context_sentence_failed", include_ref_ids=[]).body
        assert "no live paper entries stamped" in empty, empty

        dropped = papers.search(
            stamp="context_sentence_failed", exclude_ref_ids=[a]
        ).body
        assert h_a not in dropped and h_b in dropped, dropped
        by_slug = papers.search(
            stamp="context_sentence_failed", exclude=["stinb2024"]
        ).body
        assert h_b not in by_slug and h_a in by_slug, by_slug

        # after=/before= bound refs.year; since=/until= bound created_at.
        _backdate(store, a, datetime(2020, 1, 1, tzinfo=UTC))
        body = papers.search(stamp="context_sentence_failed", after=2025).body
        assert "no live paper entries stamped" in body, body  # year is NULL here
        old = store.insert_ref(
            kind="paper",
            slug="stold2010",
            title="stold2010",
            year=2010,
            meta={"context_sentence_failed": True},
        ).id
        h_old = handle_registry.format_handle("paper", old)
        body = papers.search(stamp="context_sentence_failed", after=2020).body
        assert h_old not in body, body
        body = papers.search(stamp="context_sentence_failed", before=2015).body
        assert h_old in body and h_a not in body and h_b not in body, body
        body = papers.search(stamp="context_sentence_failed", since="2021-01-01").body
        assert h_a not in body and h_b in body, body
        body = papers.search(stamp="context_sentence_failed", until="2021-01-01").body
        assert h_a in body and h_b not in body, body
        with pytest.raises(BadInput):
            papers.search(stamp="context_sentence_failed", after=2030, before=2010)


class TestTagsOnlyFilters:
    """r2 S2: the tags-only listing (no q=) honours the same id channels
    and date bounds — previously every one was silently dropped."""

    def test_include_exclude_ids_and_dates(
        self, papers: PaperHandler, store: Store
    ) -> None:
        a = _paper(store, "tagonly-a")
        b = _paper(store, "tagonly-b")
        for rid in (a, b):
            store.add_tag(rid, Tag.open("studytype:review"), set_by="agent")
        h_a = handle_registry.format_handle("paper", a)
        h_b = handle_registry.format_handle("paper", b)

        body = papers.search(tags=["studytype:review"], include_ref_ids=[a]).body
        assert h_a in body and h_b not in body, body
        empty = papers.search(tags=["studytype:review"], include_ref_ids=[]).body
        assert "no paper entries" in empty, empty

        dropped = papers.search(tags=["studytype:review"], exclude_ref_ids=[a]).body
        assert h_a not in dropped and h_b in dropped, dropped
        by_slug = papers.search(tags=["studytype:review"], exclude=["tagonly-b"]).body
        assert h_b not in by_slug and h_a in by_slug, by_slug

        _backdate(store, a, datetime(2020, 1, 1, tzinfo=UTC))
        body = papers.search(tags=["studytype:review"], after=2025).body
        assert "no paper entries" in body, body
        body = papers.search(tags=["studytype:review"], since="2021-01-01").body
        assert h_a not in body and h_b in body, body
        body = papers.search(tags=["studytype:review"], until="2021-01-01").body
        assert h_a in body and h_b not in body, body
        with pytest.raises(BadInput):
            papers.search(tags=["studytype:review"], after=2030, before=2010)


class TestLogViewIsoUtc:
    """gr517627: ``get(kind='paper', view='log')`` event times carry the
    date — ISO-8601 UTC — so before/after a deploy reads off the log."""

    def test_event_line_has_iso_utc_stamp(
        self, papers: PaperHandler, store: Store
    ) -> None:
        rid = _paper(store, "logts2024")
        _event(
            store,
            rid,
            "fetcher:oa",
            "fetch_ok",
            datetime(2026, 10, 25, 17, 3, 42, tzinfo=UTC),
        )
        body = papers.get(id=rid, view="log").body
        assert "2026-10-25T17:03:42Z" in body, body
