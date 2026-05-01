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

    # MCP critic MAJOR-$ (round 2): oracle bodies were previously
    # unbounded — get(id='stoic') returned all 16 entries verbatim
    # (~1750 tokens). The fix introduces per-entry addressing so each
    # call hits a single block (~50–200 tokens) while leaving the
    # full catalog one call away (view='index').

    def _seed_multi_oracle(
        self, store: Store, slug: str, title: str, entries: list[tuple[str, str]]
    ) -> int:
        """Seed an oracle with multiple entries (title, body) — mimics
        the real ingest shape (section_path meta on each block)."""
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
            inserts = [
                BlockInsert(
                    pos=i,
                    text=body,
                    meta={"section_path": [entry_title]},
                )
                for i, (entry_title, body) in enumerate(entries)
            ]
            store.insert_blocks(ref.id, inserts, conn=conn)
        return ref.id

    def test_multi_entry_get_returns_one_random(
        self, oracle: OracleHandler
    ) -> None:
        """Default ``get(id=<slug>)`` on a multi-entry oracle returns
        exactly ONE entry — not the whole catalog. The entry is chosen
        at random; over many draws each entry appears with roughly
        equal probability."""
        self._seed_multi_oracle(
            oracle.store,
            slug="engineering",
            title="Engineering",
            entries=[
                ("Knuth's law", "Don't tune what isn't slow."),
                ("Chesterton's fence", "Don't remove a fence blindly."),
                ("Postel's law", "Be conservative in what you send."),
            ],
        )
        # 20 draws — with 3 entries, seeing only one value is p ≈ 3e-9.
        seen: set[str] = set()
        for _ in range(20):
            out = oracle.get(id="engineering")
            body = out.body
            # Exactly one entry's body text appears.
            hits = [
                body_frag
                for body_frag in (
                    "Don't tune what isn't slow.",
                    "Don't remove a fence blindly.",
                    "Be conservative in what you send.",
                )
                if body_frag in body
            ]
            assert len(hits) == 1, f"multi-hit body: {body!r}"
            seen.add(hits[0])
        # Randomness check: with 3 entries × 20 draws, we should
        # have seen at least 2 distinct entries.
        assert len(seen) >= 2

    def test_get_selector_returns_specific_entry(
        self, oracle: OracleHandler
    ) -> None:
        """``get(id='<slug>~N')`` returns entry N deterministically."""
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[
                ("Knuth's law", "Don't tune what isn't slow."),
                ("Chesterton's fence", "Don't remove a fence blindly."),
            ],
        )
        out = oracle.get(id="eng~1")
        assert "Chesterton's fence" in out.body
        assert "Don't remove a fence blindly." in out.body
        assert "Don't tune what isn't slow." not in out.body
        # Handle form in body.
        assert "oracle eng~1" in out.body

    def test_get_selector_out_of_range_raises(
        self, oracle: OracleHandler
    ) -> None:
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[("a", "A body"), ("b", "B body")],
        )
        with pytest.raises(NotFound, match="no entry at position 99"):
            oracle.get(id="eng~99")

    def test_get_selector_non_integer_raises(
        self, oracle: OracleHandler
    ) -> None:
        from precis.errors import BadInput

        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[("a", "A body"), ("b", "B body")],
        )
        with pytest.raises(BadInput, match="integer entry position"):
            oracle.get(id="eng~not-a-number")

    def test_get_view_index_lists_entries(self, oracle: OracleHandler) -> None:
        """``view='index'`` (or ``id='<slug>/index'``) returns a
        numbered catalog of entry handles with title + first-line
        preview. The preview is clipped so the index stays bounded
        even with 60+ entries (iching)."""
        long_body = (
            "This is the first line of a longer entry that continues "
            "across several paragraphs with elaboration and examples.\n"
            "\nBackground details go here, far beyond what the index "
            "should show."
        )
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[
                ("Knuth's law", long_body),
                ("Chesterton's fence", "Don't remove a fence blindly."),
            ],
        )
        out = oracle.get(id="eng", view="index")
        # Both entry titles surface.
        assert "Knuth's law" in out.body
        assert "Chesterton's fence" in out.body
        assert "2 entries" in out.body
        # Handle hints are present.
        assert "eng~0" in out.body
        assert "eng~1" in out.body
        # First-line preview appears; background text does NOT.
        assert "first line of a longer entry" in out.body
        assert "Background details" not in out.body, (
            "index must clip to the first line — caller fetches "
            "~N to read the full body"
        )

    def test_get_view_index_path_form_equivalent(
        self, oracle: OracleHandler
    ) -> None:
        """``id='<slug>/index'`` is equivalent to ``view='index'``."""
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[("a", "A body"), ("b", "B body")],
        )
        a = oracle.get(id="eng", view="index")
        b = oracle.get(id="eng/index")
        assert a.body == b.body

    def test_get_view_conflict_raises(self, oracle: OracleHandler) -> None:
        from precis.errors import BadInput

        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[("a", "A body")],
        )
        with pytest.raises(BadInput, match="conflicts with view="):
            oracle.get(id="eng/index", view="nonexistent")

    def test_single_block_oracle_renders_verbatim(
        self, oracle: OracleHandler
    ) -> None:
        """Single-block oracles (short rubrics, pinpoint prompts) don't
        shuffle — there's only one entry to return. Pinning this so
        the random path is truly a multi-entry affordance."""
        self._seed_oracle(
            oracle.store,
            slug="rubric-x",
            title="Short rubric",
            body_text="Always cite the source.",
        )
        out = oracle.get(id="rubric-x")
        assert "Always cite the source." in out.body


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
        quest.tag(id=slug, add=["STATUS:done"])
        out = quest.get(id="/open")
        assert "open-one" in out.body
        assert "closed-one" not in out.body

    def test_status_transition(self, quest: QuestHandler) -> None:
        quest.put(text="task")
        quest.tag(id="task", add=["STATUS:doing"])
        out = quest.get(id="task")
        assert "STATUS:doing" in out.body
        assert "STATUS:open" not in out.body

    def test_create_requires_text(self, quest: QuestHandler) -> None:
        with pytest.raises(BadInput, match="creating a quest"):
            quest.put()

    def test_delete(self, quest: QuestHandler) -> None:
        quest.put(text="ephemeral")
        quest.delete(id="ephemeral")
        with pytest.raises(NotFound):
            quest.get(id="ephemeral")
