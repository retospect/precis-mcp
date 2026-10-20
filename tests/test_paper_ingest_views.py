"""``search(kind='paper', view='count'|'status'|'ingest')`` —
gr500649 (count), gr500651 (bulk fetch status), gr500643 (ingest rate).

Full-text = a live non-card, non-``references`` body chunk; ``card_*``
chunks (``ord < 0``) are abstract/title cards only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

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
