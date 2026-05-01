"""Tests for the slimmer phase-5 state kinds (gripe, fc, oracle, conv, quest).

Heavy lifting (CRUD shape) lives in `_NumericRefHandler`, exercised
already via `test_memory.py` and `test_todo.py`. These tests verify
the *unique* behaviour of each leaf handler — list views, slug
addressing, view paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.conversation import ConversationHandler
from precis.handlers.flashcard import FlashcardHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.oracle import OracleHandler
from precis.handlers.quest import QuestHandler
from precis.store import Store
from precis.store.types import BlockInsert

# ── GripeHandler — almost-pure NumericRefHandler ─────────────────────


class TestGripe:
    @pytest.fixture
    def gripe(self, hub: Hub) -> GripeHandler:
        return GripeHandler(hub=hub)

    def test_create_and_read(self, gripe: GripeHandler) -> None:
        r = gripe.put(text="VS Code keeps reloading the workspace")
        gripe_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
        out = gripe.get(id=gripe_id)
        assert "VS Code" in out.body
        assert "gripe" in out.body  # header

    def test_no_default_tags(self, gripe: GripeHandler) -> None:
        """Unlike todos, gripes don't auto-stamp STATUS:open."""
        gripe.put(text="anything")
        refs = gripe.store.list_refs(kind="gripe", limit=10)
        tags = gripe.store.tags_for(refs[0].id)
        assert not any("STATUS:" in str(t) for t in tags)

    def test_search(self, gripe: GripeHandler) -> None:
        gripe.put(text="postgres is being slow today")
        gripe.put(text="completely unrelated")
        r = gripe.search(q="postgres")
        assert "postgres" in r.body
        assert "1 gripe match" in r.body


# ── FlashcardHandler ────────────────────────────────────────────────


class TestFlashcard:
    @pytest.fixture
    def fc(self, hub: Hub) -> FlashcardHandler:
        return FlashcardHandler(hub=hub)

    def test_create_and_read(self, fc: FlashcardHandler) -> None:
        r = fc.put(text="Paris is the capital of France")
        fc_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
        out = fc.get(id=fc_id)
        assert "Paris" in out.body
        assert "flashcard" in out.body

    def test_due_view_includes_untouched(self, fc: FlashcardHandler) -> None:
        """Cards without next_review meta count as due (never reviewed)."""
        fc.put(text="card A")
        fc.put(text="card B")
        out = fc.get(id="/due")
        assert "2 flashcard(s) due" in out.body
        assert "card A" in out.body
        assert "card B" in out.body

    def test_due_view_filters_future_review(self, fc: FlashcardHandler) -> None:
        fc.put(text="card-due")
        fc.put(text="card-far-future")
        refs = fc.store.list_refs(kind="fc", limit=10)
        # Set far-future review on the second card.
        far = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        far_card = next(r for r in refs if "far-future" in r.title)
        fc.store.update_ref(far_card.id, meta_patch={"next_review": far})

        out = fc.get(id="/due")
        assert "card-due" in out.body
        # The far-future card shouldn't be in either due or upcoming.
        assert "far-future" not in out.body

    def test_upcoming_within_3_days(self, fc: FlashcardHandler) -> None:
        fc.put(text="card-soon")
        ref = fc.store.list_refs(kind="fc", limit=10)[0]
        soon = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        fc.store.update_ref(ref.id, meta_patch={"next_review": soon})
        out = fc.get(id="/due")
        assert "card-soon" in out.body
        assert "due within 3 days" in out.body

    def test_due_empty(self, fc: FlashcardHandler) -> None:
        out = fc.get(id="/due")
        assert "no flashcards due" in out.body


# ── OracleHandler — slug-addressed, read-only ────────────────────────


class TestOracle:
    @pytest.fixture
    def oracle(self, hub: Hub) -> OracleHandler:
        return OracleHandler(hub=hub)

    def _seed_oracle(self, store: Store, slug: str, title: str, body_text: str) -> int:
        """Insert an oracle directly via the store — there's no put() yet."""
        with store.tx() as conn:
            corpus_id = store.ensure_corpus("default")
            ref = store.insert_ref(
                corpus_id=corpus_id,
                kind="oracle",
                slug=slug,
                title=title,
                meta={},
                conn=conn,
            )
            store.insert_blocks(ref.id, [BlockInsert(pos=0, text=body_text)], conn=conn)
        return ref.id

    def test_get_renders_blocks(self, oracle: OracleHandler) -> None:
        self._seed_oracle(
            oracle.store,
            slug="reviewer-rigor",
            title="Rigor reviewer rubric",
            body_text="Verify every claim against its cited source.",
        )
        out = oracle.get(id="reviewer-rigor")
        assert "reviewer-rigor" in out.body
        assert "Verify every claim" in out.body

    def test_get_missing_404s(self, oracle: OracleHandler) -> None:
        with pytest.raises(NotFound, match="oracle slug 'nope'"):
            oracle.get(id="nope")

    def test_list_view(self, oracle: OracleHandler) -> None:
        self._seed_oracle(oracle.store, "a", "Oracle A", "body a")
        self._seed_oracle(oracle.store, "b", "Oracle B", "body b")
        out = oracle.get()
        assert "2 oracle(s)" in out.body
        assert "a" in out.body and "b" in out.body

    def test_list_empty(self, oracle: OracleHandler) -> None:
        out = oracle.get()
        assert "no oracles" in out.body


# ── ConversationHandler ────────────────────────────────────────────


class TestConversation:
    @pytest.fixture
    def conv(self, hub: Hub) -> ConversationHandler:
        return ConversationHandler(hub=hub)

    def _seed_conv(self, store: Store, slug: str, title: str, turns: list[str]) -> int:
        with store.tx() as conn:
            corpus_id = store.ensure_corpus("default")
            ref = store.insert_ref(
                corpus_id=corpus_id,
                kind="conv",
                slug=slug,
                title=title,
                meta={"participants": ["agent", "user"]},
                conn=conn,
            )
            store.insert_blocks(
                ref.id,
                [BlockInsert(pos=i, text=t) for i, t in enumerate(turns)],
                conn=conn,
            )
        return ref.id

    def test_overview_lists_turn_count(self, conv: ConversationHandler) -> None:
        self._seed_conv(conv.store, "thread-1", "About auth", ["hi", "hello"])
        out = conv.get(id="thread-1")
        assert "thread-1" in out.body
        assert "2 turns" in out.body
        assert "participants" in out.body

    def test_transcript_view(self, conv: ConversationHandler) -> None:
        self._seed_conv(
            conv.store,
            "thread-1",
            "About auth",
            ["how do I auth?", "use OAuth"],
        )
        out = conv.get(id="thread-1/transcript")
        assert "## turn ~0" in out.body
        assert "## turn ~1" in out.body
        assert "how do I auth" in out.body
        assert "use OAuth" in out.body

    def test_single_turn(self, conv: ConversationHandler) -> None:
        self._seed_conv(conv.store, "thread-1", "x", ["alpha", "beta", "gamma"])
        out = conv.get(id="thread-1~1")
        assert "thread-1~1" in out.body
        assert "beta" in out.body
        assert "alpha" not in out.body
        assert "gamma" not in out.body

    def test_missing_turn_404s(self, conv: ConversationHandler) -> None:
        self._seed_conv(conv.store, "t", "x", ["one"])
        with pytest.raises(NotFound, match="no turn at"):
            conv.get(id="t~99")

    def test_list_view(self, conv: ConversationHandler) -> None:
        self._seed_conv(conv.store, "a", "thread A", ["x"])
        self._seed_conv(conv.store, "b", "thread B", ["y"])
        out = conv.get()
        assert "2 conversation(s)" in out.body


# ── QuestHandler — slug-addressed with auto-mint ─────────────────────


class TestQuest:
    @pytest.fixture
    def quest(self, hub: Hub) -> QuestHandler:
        return QuestHandler(hub=hub)

    def test_create_mints_slug_from_text(self, quest: QuestHandler) -> None:
        r = quest.put(text="Ingest paper acheson 2026")
        assert "ingest-paper-acheson-2026" in r.body
        assert "status: open" in r.body

    def test_create_then_read(self, quest: QuestHandler) -> None:
        quest.put(text="Re-deploy hermes profile")
        out = quest.get(id="re-deploy-hermes-profile")
        assert "Re-deploy hermes profile" in out.body
        assert "STATUS:open" in out.body

    def test_collision_appends_suffix(self, quest: QuestHandler) -> None:
        quest.put(text="duplicate")
        r = quest.put(text="duplicate")
        # Second create gets "-2" suffix.
        assert "duplicate-2" in r.body

    def test_list_open(self, quest: QuestHandler) -> None:
        quest.put(text="open one")
        r2 = quest.put(text="closed one")
        slug = r2.body.split("'")[1]  # extract 'closed-one' from message
        quest.put(id=slug, tags=["STATUS:done"])
        out = quest.get(id="/open")
        assert "open-one" in out.body
        assert "closed-one" not in out.body

    def test_status_transition(self, quest: QuestHandler) -> None:
        quest.put(text="task")
        quest.put(id="task", tags=["STATUS:doing"])
        out = quest.get(id="task")
        assert "STATUS:doing" in out.body
        assert "STATUS:open" not in out.body

    def test_create_requires_text(self, quest: QuestHandler) -> None:
        with pytest.raises(BadInput, match="creating a quest"):
            quest.put()

    def test_delete(self, quest: QuestHandler) -> None:
        quest.put(text="ephemeral")
        quest.put(id="ephemeral", mode="delete")
        with pytest.raises(NotFound):
            quest.get(id="ephemeral")
