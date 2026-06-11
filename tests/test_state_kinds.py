"""Tests for the slimmer phase-5 state kinds (gripe, fc, oracle, conv).

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
from precis.handlers.job import JobHandler
from precis.handlers.oracle import OracleHandler
from precis.handlers.presentation import PresentationHandler
from precis.store import Store
from precis.store.types import BlockInsert

# ── GripeHandler — first-class bug tracker ──────────────────────────


class TestGripe:
    """Gripe is the project's bug tracker (since migration 0005).

    The body and the append-only comment timeline live as chunks
    (`gripe_body` for the body, `gripe_comment` for each comment).
    Standard CRUD shape via NumericRefHandler; the gripe-specific
    behaviour we verify here is the chunk-based timeline and the
    `put(id=N, text=...)` comment-append idiom.
    """

    @pytest.fixture
    def gripe(self, hub: Hub) -> GripeHandler:
        return GripeHandler(hub=hub)

    def test_put_creates_a_gripe_with_body_chunk(self, gripe: GripeHandler) -> None:
        r = gripe.put(text="VS Code keeps reloading the workspace")
        assert "id=" in r.body
        refs = gripe.store.list_refs(kind="gripe", limit=10)
        ref = next(r for r in refs if "VS Code" in r.title)
        # Body materialises as a chunk so the embed + chunk_keywords
        # workers can index it for search.
        blocks = gripe.store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 1
        assert blocks[0].chunk_kind == "gripe_body"
        assert "VS Code" in blocks[0].text

    def test_default_status_open_tag(self, gripe: GripeHandler) -> None:
        """New gripes start at STATUS:open, like todos."""
        gripe.put(text="anything")
        refs = gripe.store.list_refs(kind="gripe", limit=10)
        tags = gripe.store.tags_for(refs[0].id)
        assert any("STATUS:open" in str(t) for t in tags)

    def test_put_with_id_appends_comment_chunk(self, gripe: GripeHandler) -> None:
        gripe.put(text="paper slug NotFound has no near-match suggestions")
        refs = gripe.store.list_refs(kind="gripe", limit=10)
        gripe_id = refs[0].id
        gripe.put(id=gripe_id, text="only triggers when the slug has a hyphen")
        blocks = gripe.store.list_blocks_for_ref(gripe_id)
        assert len(blocks) == 2
        assert blocks[0].chunk_kind == "gripe_body"
        assert blocks[1].chunk_kind == "gripe_comment"
        assert "hyphen" in blocks[1].text

    def test_get_renders_body_plus_comment_timeline(self, gripe: GripeHandler) -> None:
        gripe.put(text="search drops duplicate hits")
        refs = gripe.store.list_refs(kind="gripe", limit=10)
        gripe_id = refs[0].id
        gripe.put(id=gripe_id, text="reproduces on HNSW ties")
        gripe.put(id=gripe_id, text="ranking comparator needs a tiebreaker")
        body = gripe.get(id=gripe_id).body
        assert "search drops duplicate hits" in body
        assert "## comment 1" in body
        assert "reproduces on HNSW ties" in body
        assert "## comment 2" in body
        assert "tiebreaker" in body

    def test_kindspec_is_first_class(self) -> None:
        """Regression guard: the v0 write-only KindSpec was inverted
        in migration 0005 / handler rewrite. If any of these flags
        drift back to False the dispatch boundary silently drops
        the verb."""
        spec = GripeHandler.spec
        assert spec.supports_put is True
        assert spec.supports_get is True
        assert spec.supports_search is True
        assert spec.supports_search_hits is True
        assert spec.supports_delete is True
        assert spec.supports_tag is True
        assert spec.supports_link is True


# ── JobHandler — offline-work substrate ─────────────────────────────


class TestJob:
    """JobHandler validates put at submit time.

    The validation paths don't need a real store — they reject
    before any DB write — so the rejection tests run without the
    `hub` fixture. The happy-path / DB-backed tests use the fixture
    and skip when no database is available.
    """

    @pytest.fixture
    def job(self, hub: Hub) -> JobHandler:
        return JobHandler(hub=hub)

    def test_kindspec_is_first_class(self) -> None:
        spec = JobHandler.spec
        assert spec.supports_put is True
        assert spec.supports_get is True
        assert spec.supports_search is True
        assert spec.supports_tag is True
        assert spec.supports_link is True
        assert spec.supports_delete is True

    def test_put_requires_job_type(self) -> None:
        # Validation runs before any store access, so we don't need
        # the hub fixture for this rejection path.
        handler = JobHandler.__new__(JobHandler)
        # Store-less handler: the validation we want to verify
        # runs before store access.
        with pytest.raises(BadInput, match="requires job_type"):
            handler.put()

    def test_put_rejects_unknown_job_type(self) -> None:
        handler = JobHandler.__new__(JobHandler)
        with pytest.raises(BadInput, match="unknown job_type"):
            handler.put(job_type="simulate_warp_drive", link="gripe:1", rel="fixes")

    def test_put_rejects_incompatible_executor(self) -> None:
        handler = JobHandler.__new__(JobHandler)
        with pytest.raises(
            BadInput,
            match=r"does not support executor 'slurm'|unknown executor 'slurm'",
        ):
            handler.put(
                job_type="fix_gripe",
                executor="slurm",
                link="gripe:1",
                rel="fixes",
            )

    def test_put_rejects_id_present(self) -> None:
        handler = JobHandler.__new__(JobHandler)
        with pytest.raises(BadInput, match="put on existing job"):
            handler.put(id=42)

    def test_put_rejects_fix_gripe_without_link(self) -> None:
        handler = JobHandler.__new__(JobHandler)
        with pytest.raises(BadInput, match="fix_gripe requires link"):
            handler.put(job_type="fix_gripe")


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
            ref = store.insert_ref(
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
        # Headline pluralisation auto-resolves the legacy ``foo(s)``
        # template to ``2 oracles`` (MCP critic MINOR-C 2026-05-02).
        assert "2 oracles" in out.body
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
            ref = store.insert_ref(
                kind="oracle",
                slug=slug,
                title=title,
                meta={},
                conn=conn,
            )
            # 1-indexed positions match the production oracle ingest
            # path (see ``ingest_oracles.ingest_paper``) so I-Ching
            # ``iching~49`` resolves to Hexagram 49 verbatim.
            inserts = [
                BlockInsert(
                    pos=i + 1,
                    text=body,
                    meta={"section_path": [entry_title]},
                )
                for i, (entry_title, body) in enumerate(entries)
            ]
            store.insert_blocks(ref.id, inserts, conn=conn)
        return ref.id

    def test_multi_entry_get_returns_one_random(self, oracle: OracleHandler) -> None:
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

    def test_get_selector_returns_specific_entry(self, oracle: OracleHandler) -> None:
        """``get(id='<slug>~N')`` returns entry N deterministically.

        Positions are 1-indexed at ingest, so the second entry
        addresses as ``~2``. (Matches I-Ching hexagram numbering;
        see ``precis-oracle-help``.)
        """
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[
                ("Knuth's law", "Don't tune what isn't slow."),
                ("Chesterton's fence", "Don't remove a fence blindly."),
            ],
        )
        out = oracle.get(id="eng~2")
        assert "Chesterton's fence" in out.body
        assert "Don't remove a fence blindly." in out.body
        assert "Don't tune what isn't slow." not in out.body
        # Handle form in body.
        assert "oracle eng~2" in out.body

    def test_get_selector_out_of_range_raises(self, oracle: OracleHandler) -> None:
        self._seed_multi_oracle(
            oracle.store,
            slug="eng",
            title="Engineering",
            entries=[("a", "A body"), ("b", "B body")],
        )
        with pytest.raises(NotFound, match="no entry at position 99"):
            oracle.get(id="eng~99")

    def test_get_selector_non_integer_raises(self, oracle: OracleHandler) -> None:
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
        # Handle hints are present (1-indexed).
        assert "eng~1" in out.body
        assert "eng~2" in out.body
        # First-line preview appears; background text does NOT.
        assert "first line of a longer entry" in out.body
        assert "Background details" not in out.body, (
            "index must clip to the first line — caller fetches "
            "~N to read the full body"
        )

    def test_get_view_index_path_form_equivalent(self, oracle: OracleHandler) -> None:
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

    def test_single_block_oracle_renders_verbatim(self, oracle: OracleHandler) -> None:
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
            ref = store.insert_ref(
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
        # Headline pluralisation auto-resolves to ``2 conversations``
        # (MCP critic MINOR-C 2026-05-02).
        assert "2 conversations" in out.body

    # ── put (chat-bridge capture-on-write) ──────────────────────────

    def test_put_creates_ref_and_first_turn(
        self, conv: ConversationHandler
    ) -> None:
        out = conv.put(
            id="discord/g/c/t",
            text="hi there",
            author="alice",
            msg_id="m1",
            title="kickoff",
            ref_meta={"platform": "discord", "channel_id": "c"},
        )
        assert "created + appended" in out.body
        assert "discord/g/c/t~0" in out.body
        ref = conv.store.get_ref(kind="conv", id="discord/g/c/t")
        assert ref is not None
        assert ref.title == "kickoff"
        assert ref.meta.get("platform") == "discord"
        blocks = conv.store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 1
        assert blocks[0].text == "hi there"
        assert blocks[0].meta.get("author") == "alice"
        assert blocks[0].meta.get("msg_id") == "m1"

    def test_put_appends_subsequent_turns(
        self, conv: ConversationHandler
    ) -> None:
        conv.put(id="t1", text="one", author="alice", msg_id="m1")
        conv.put(id="t1", text="two", author="bob", msg_id="m2")
        conv.put(id="t1", text="three", author="alice", msg_id="m3")
        ref = conv.store.get_ref(kind="conv", id="t1")
        assert ref is not None
        blocks = conv.store.list_blocks_for_ref(ref.id)
        assert [b.text for b in blocks] == ["one", "two", "three"]
        assert [b.pos for b in blocks] == [0, 1, 2]

    def test_put_is_idempotent_on_msg_id(
        self, conv: ConversationHandler
    ) -> None:
        conv.put(id="t1", text="one", author="alice", msg_id="m1")
        out = conv.put(id="t1", text="one (replay)", author="alice", msg_id="m1")
        assert "already captured" in out.body
        ref = conv.store.get_ref(kind="conv", id="t1")
        assert ref is not None
        blocks = conv.store.list_blocks_for_ref(ref.id)
        # No duplicate. Replay text is ignored.
        assert len(blocks) == 1
        assert blocks[0].text == "one"

    def test_put_without_msg_id_just_appends(
        self, conv: ConversationHandler
    ) -> None:
        conv.put(id="t1", text="one", author="alice")
        conv.put(id="t1", text="two", author="bob")
        ref = conv.store.get_ref(kind="conv", id="t1")
        assert ref is not None
        blocks = conv.store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 2
        # No msg_id idempotency key on either block.
        assert "msg_id" not in (blocks[0].meta or {})

    def test_put_rejects_missing_id_or_text(
        self, conv: ConversationHandler
    ) -> None:
        with pytest.raises(BadInput, match="requires id"):
            conv.put(text="hi")
        with pytest.raises(BadInput, match="requires text"):
            conv.put(id="t1")
        with pytest.raises(BadInput, match="requires text"):
            conv.put(id="t1", text="   ")  # whitespace-only rejected


# ── PresentationHandler ────────────────────────────────────────────


class TestPresentation:
    """`pres` is for slide decks + unpublished writeups (migration
    0008). Slug-addressed, one block per slide (or paragraph for
    writeups). Block ``chunk_kind`` defaults to ``pres_slide``."""

    @pytest.fixture
    def pres(self, hub: Hub) -> PresentationHandler:
        return PresentationHandler(hub=hub)

    def test_put_creates_deck_and_first_slide(
        self, pres: PresentationHandler
    ) -> None:
        out = pres.put(
            id="2026-06-talk-foo",
            text="Title slide",
            pos=0,
            title="Talk Foo",
            subtype="slides",
            ref_meta={"venue": "demo day", "date": "2026-06-04"},
        )
        assert "created + appended" in out.body
        assert "2026-06-talk-foo~0" in out.body
        assert "subtype='slides'" in out.body
        ref = pres.store.get_ref(kind="pres", id="2026-06-talk-foo")
        assert ref is not None
        assert ref.title == "Talk Foo"
        assert ref.meta.get("venue") == "demo day"
        blocks = pres.store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 1
        assert blocks[0].text == "Title slide"
        assert blocks[0].chunk_kind == "pres_slide"

    def test_put_appends_in_order_without_pos(
        self, pres: PresentationHandler
    ) -> None:
        pres.put(id="d", text="slide 0")
        pres.put(id="d", text="slide 1")
        pres.put(id="d", text="slide 2")
        ref = pres.store.get_ref(kind="pres", id="d")
        assert ref is not None
        blocks = pres.store.list_blocks_for_ref(ref.id)
        assert [b.text for b in blocks] == ["slide 0", "slide 1", "slide 2"]
        assert [b.pos for b in blocks] == [0, 1, 2]

    def test_put_overwrites_at_explicit_pos(
        self, pres: PresentationHandler
    ) -> None:
        pres.put(id="d", text="original slide 0", pos=0)
        pres.put(id="d", text="original slide 1", pos=1)
        out = pres.put(id="d", text="fixed slide 0", pos=0)
        assert "overwrote" in out.body
        ref = pres.store.get_ref(kind="pres", id="d")
        assert ref is not None
        blocks = pres.store.list_blocks_for_ref(ref.id)
        assert [b.pos for b in blocks] == [0, 1]
        # Block 0 holds the new text; block 1 untouched.
        by_pos = {b.pos: b.text for b in blocks}
        assert by_pos[0] == "fixed slide 0"
        assert by_pos[1] == "original slide 1"

    def test_put_writeup_uses_paragraph_chunk_kind(
        self, pres: PresentationHandler
    ) -> None:
        pres.put(
            id="postmortem",
            text="The cluster went down at 03:14.",
            chunk_kind="paragraph",
            subtype="writeup",
            title="Cluster postmortem",
        )
        ref = pres.store.get_ref(kind="pres", id="postmortem")
        assert ref is not None
        blocks = pres.store.list_blocks_for_ref(ref.id)
        assert blocks[0].chunk_kind == "paragraph"

    def test_get_overview_lists_block_count(
        self, pres: PresentationHandler
    ) -> None:
        pres.put(
            id="d",
            text="s0",
            title="Deck",
            ref_meta={"venue": "demo day", "date": "2026-06-04"},
        )
        pres.put(id="d", text="s1")
        out = pres.get(id="d")
        assert "d" in out.body
        assert "Deck" in out.body
        assert "2 blocks" in out.body
        assert "venue" in out.body

    def test_get_full_renders_all_blocks_labelled(
        self, pres: PresentationHandler
    ) -> None:
        pres.put(id="d", text="alpha", title="x")
        pres.put(id="d", text="beta")
        out = pres.get(id="d/full")
        assert "## slide ~0" in out.body
        assert "## slide ~1" in out.body
        assert "alpha" in out.body
        assert "beta" in out.body

    def test_get_single_block(self, pres: PresentationHandler) -> None:
        pres.put(id="d", text="alpha", title="x")
        pres.put(id="d", text="beta")
        pres.put(id="d", text="gamma")
        out = pres.get(id="d~1")
        assert "d~1 (slide)" in out.body
        assert "beta" in out.body
        assert "alpha" not in out.body

    def test_missing_block_404s(self, pres: PresentationHandler) -> None:
        pres.put(id="d", text="one", title="x")
        with pytest.raises(NotFound, match="no block at"):
            pres.get(id="d~99")

    def test_put_rejects_missing_id_or_text(
        self, pres: PresentationHandler
    ) -> None:
        with pytest.raises(BadInput, match="requires id"):
            pres.put(text="hi")
        with pytest.raises(BadInput, match="requires text"):
            pres.put(id="d")

    def test_list_view(self, pres: PresentationHandler) -> None:
        pres.put(id="a", text="hi", title="deck A")
        pres.put(id="b", text="hi", title="deck B")
        out = pres.get()
        assert "2 presentations" in out.body
