"""The integration ledger — paper-writing pipeline rung 2 (docs/backlog/paper-writing-pipeline.md §"The integration ledger"; migration 0085).

Covers:

1. The four disposition relations (`cited-in` / `corroborates` /
   `superseded-in` / `off-topic-for`) validate and link paper→draft.
2. `Store.integration_ledger` — with and without a section anchor.
3. `Store.unintegrated_papers` — the `topic:X` minus `integrated-into`
   gap-review query.
4. `get(kind='draft', id=<dossier>, view='integration')` renders both
   the INTEGRATED and PENDING sections.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers._link_tag_ops import validate_relation
from precis.handlers.draft import DraftHandler
from precis.store.types import Tag

DISPOSITIONS = ["cited-in", "corroborates", "superseded-in", "off-topic-for"]


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Proj").id


def _paper(store, slug: str, title: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title, meta={}).id


def _chunk_ord(store, chunk_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# 1. Relations
# ---------------------------------------------------------------------------


class TestDispositionRelations:
    def test_relations_validate(self, store) -> None:
        for rel in DISPOSITIONS:
            assert validate_relation(rel, store=store) == rel

    def test_migration_seeded_relations_table(self, store) -> None:
        # Migration 0085 — the DB is the FK authority; store.valid_relations()
        # reads it directly.
        assert set(DISPOSITIONS) <= store.valid_relations()

    def test_link_paper_to_draft_each_relation(
        self, store, draft: DraftHandler
    ) -> None:
        proj = _proj(store)
        draft.put(id="rel-dossier", title="Dossier", project=proj)
        dossier_ref = store.get_ref(kind="draft", id="rel-dossier")

        for i, rel in enumerate(DISPOSITIONS):
            paper_id = _paper(store, f"rel-paper-{i}", f"Paper {i}")
            link = store.add_link(
                src_ref_id=paper_id, dst_ref_id=dossier_ref.id, relation=rel
            )
            assert link.relation == rel
            assert link.src_ref_id == paper_id
            assert link.dst_ref_id == dossier_ref.id


# ---------------------------------------------------------------------------
# 2. integration_ledger
# ---------------------------------------------------------------------------


class TestIntegrationLedger:
    def test_ledger_with_and_without_section_anchor(
        self, store, draft: DraftHandler
    ) -> None:
        proj = _proj(store)
        draft.put(id="ledger-dossier", title="Dossier", project=proj)
        dossier_ref = store.get_ref(kind="draft", id="ledger-dossier")
        title_chunk = store.drafts.reading_order(dossier_ref.id)[0]
        draft.put(
            id="ledger-dossier",
            chunk_kind="heading",
            text="Current state",
            at={"after": title_chunk.dc},
        )
        section = store.drafts.reading_order(dossier_ref.id)[1]
        section_ord = _chunk_ord(store, section.chunk_id)

        p1 = _paper(store, "ledger-p1", "Paper One")
        p2 = _paper(store, "ledger-p2", "Paper Two")

        store.add_link(src_ref_id=p1, dst_ref_id=dossier_ref.id, relation="cited-in")
        store.add_link(
            src_ref_id=p2,
            dst_ref_id=dossier_ref.id,
            relation="corroborates",
            dst_pos=section_ord,
        )

        rows = store.integration_ledger(dossier_ref.id)
        by_paper = {r["paper_ref_id"]: r for r in rows}

        assert by_paper[p1]["paper_title"] == "Paper One"
        assert by_paper[p1]["section_chunk_id"] is None
        assert by_paper[p1]["section_heading"] is None
        assert by_paper[p1]["relation"] == "cited-in"
        assert by_paper[p1]["at"] is not None

        assert by_paper[p2]["section_chunk_id"] == section.chunk_id
        assert by_paper[p2]["section_heading"] == "Current state"
        assert by_paper[p2]["relation"] == "corroborates"


# ---------------------------------------------------------------------------
# 3. unintegrated_papers
# ---------------------------------------------------------------------------


class TestUnintegratedPapers:
    def test_gap_query(self, store, draft: DraftHandler) -> None:
        proj = _proj(store)
        draft.put(id="gap-dossier", title="Dossier", project=proj)
        dossier_ref = store.get_ref(kind="draft", id="gap-dossier")

        pending = _paper(store, "gap-pending", "Pending Paper")
        store.add_tag(pending, Tag.open("topic:mof"), set_by="agent")

        integrated = _paper(store, "gap-integrated", "Integrated Paper")
        store.add_tag(integrated, Tag.open("topic:mof"), set_by="agent")

        rejected = _paper(store, "gap-rejected", "Rejected Paper")
        store.add_tag(rejected, Tag.open("topic:mof"), set_by="agent")

        untagged = _paper(store, "gap-untagged", "Untagged Paper")

        # Before any disposition edge: pending, integrated-to-be, and
        # rejected-to-be all show (none has an edge yet); the untagged
        # paper never shows.
        before = {
            r["paper_ref_id"]
            for r in store.unintegrated_papers(dossier_ref.id, ["mof"])
        }
        assert {pending, integrated, rejected} <= before
        assert untagged not in before

        store.add_link(
            src_ref_id=integrated, dst_ref_id=dossier_ref.id, relation="cited-in"
        )
        store.add_link(
            src_ref_id=rejected, dst_ref_id=dossier_ref.id, relation="off-topic-for"
        )

        after = {
            r["paper_ref_id"]
            for r in store.unintegrated_papers(dossier_ref.id, ["mof"])
        }
        assert pending in after
        assert integrated not in after
        assert rejected not in after

    def test_empty_topics_returns_empty(self, store) -> None:
        assert store.unintegrated_papers(1, []) == []


# ---------------------------------------------------------------------------
# 4. view='integration'
# ---------------------------------------------------------------------------


class TestIntegrationView:
    def test_renders_integrated_and_pending(self, store, draft: DraftHandler) -> None:
        proj = _proj(store)
        draft.put(id="view-dossier", title="Dossier", project=proj)
        dossier_ref = store.get_ref(kind="draft", id="view-dossier")
        store.add_tag(dossier_ref.id, Tag.open("topic:mof"), set_by="agent")

        woven = _paper(store, "view-woven", "Woven Paper")
        store.add_tag(woven, Tag.open("topic:mof"), set_by="agent")
        store.add_link(src_ref_id=woven, dst_ref_id=dossier_ref.id, relation="cited-in")

        pending = _paper(store, "view-pending", "Pending Paper Two")
        store.add_tag(pending, Tag.open("topic:mof"), set_by="agent")

        resp = draft.get(id="view-dossier", view="integration")
        body = resp.body

        assert "INTEGRATED" in body
        assert "PENDING" in body
        assert "Woven Paper" in body
        assert "cited-in" in body
        assert "Pending Paper Two" in body

    def test_no_topic_tag_pending_unavailable(self, store, draft: DraftHandler) -> None:
        proj = _proj(store)
        draft.put(id="notopic-dossier", title="Dossier", project=proj)
        resp = draft.get(id="notopic-dossier", view="integration")
        assert "pending set unavailable" in resp.body
