"""Cross-linking on conv + oracle (phase-8 follow-up).

Same shape as the paper + perplexity link/tag put surfaces — see
``test_paper_research_crosslinking.py`` for the original pass.
These two kinds were the remaining "still queued" items from
that session: ``conv`` (capture-on-write transcripts) and
``oracle`` (curated reference nodes). Bodies stay non-mutable
from the agent surface; link/tag CRUD opens up.

Tests pin:

* Accepted ops — link, unlink, tags, untags, rel.
* Rejections — text=, mode=, missing id, unknown slug, chunk
  selectors / path views, link/unlink mutex, bare rel=, no-op.
* Per-kind axis enforcement — both kinds have empty closed-axis
  allowlists, so STATUS:/PRIO: must reject.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers.conversation import ConversationHandler
from precis.handlers.oracle import OracleHandler
from precis.store import BlockInsert, Store

# ── seed helpers ────────────────────────────────────────────────────


def _seed_oracle(store: Store, slug: str, title: str = "Test Oracle") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(corpus_id=cid, kind="oracle", slug=slug, title=title)
    return ref.id


def _seed_conv(store: Store, slug: str, title: str = "Test Thread") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(corpus_id=cid, kind="conv", slug=slug, title=title)
    # One block so list_blocks_for_ref / chunk parsing has data to chew on.
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text="hello")])
    return ref.id


def _seed_paper(store: Store, slug: str, title: str = "P") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(corpus_id=cid, kind="paper", slug=slug, title=title)
    return ref.id


# ── OracleHandler.put ───────────────────────────────────────────────


@pytest.fixture
def oracle(store: Store) -> OracleHandler:
    return OracleHandler(store=store)


class TestOraclePutAccepted:
    def test_link_oracle_to_paper(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "reviewer-rigor")
        _seed_paper(store, "smith2024")
        out = oracle.put(id="reviewer-rigor", link="paper:smith2024", rel="cites")
        assert "+1 link" in out.body
        assert "reviewer-rigor" in out.body
        out_links = store.links_for(o_id, relation="cites", direction="out")
        assert len(out_links) == 1

    def test_open_tag_added(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        out = oracle.put(id="rubric-x", tags=["topic-citation-style"])
        assert "+1 tag" in out.body
        rows = store.tags_for(o_id)
        assert any(t.value == "topic-citation-style" for t in rows)

    def test_untag_removes(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        oracle.put(id="rubric-x", tags=["topic-foo"])
        out = oracle.put(id="rubric-x", untags=["topic-foo"])
        assert "-1 tag" in out.body
        rows = store.tags_for(o_id)
        assert all(t.value != "topic-foo" for t in rows)

    def test_unlink_removes(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        _seed_paper(store, "p1")
        oracle.put(id="rubric-x", link="paper:p1", rel="cites")
        out = oracle.put(id="rubric-x", unlink="paper:p1", rel="cites")
        assert "-1 link" in out.body
        assert store.links_for(o_id, relation="cites", direction="out") == []


class TestOraclePutRejected:
    def test_text_rejected(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="oracle bodies are curated"):
            oracle.put(id="rubric-x", text="rewrite me")

    def test_mode_rejected(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="mode='replace' not supported"):
            oracle.put(id="rubric-x", mode="replace")

    def test_missing_id(self, oracle: OracleHandler) -> None:
        with pytest.raises(BadInput, match="requires id="):
            oracle.put(link="paper:foo")

    def test_unknown_slug(self, oracle: OracleHandler) -> None:
        with pytest.raises(NotFound, match="oracle slug 'no-such' not found"):
            oracle.put(id="no-such", link="paper:foo")

    def test_status_axis_rejected(self, oracle: OracleHandler, store: Store) -> None:
        """Oracles have no closed-axis tags — STATUS: must reject."""
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="axis not allowed on kind 'oracle'"):
            oracle.put(id="rubric-x", tags=["STATUS:open"])

    def test_no_op_rejected(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="requires at least one"):
            oracle.put(id="rubric-x")

    def test_link_unlink_mutex(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        _seed_paper(store, "p1")
        with pytest.raises(BadInput, match="link= and unlink= are mutually exclusive"):
            oracle.put(id="rubric-x", link="paper:p1", unlink="paper:p1")


# ── ConversationHandler.put ─────────────────────────────────────────


@pytest.fixture
def conv(store: Store) -> ConversationHandler:
    return ConversationHandler(store=store)


class TestConvPutAccepted:
    def test_link_conv_to_paper(self, store: Store, conv: ConversationHandler) -> None:
        c_id = _seed_conv(store, "thread-1")
        _seed_paper(store, "smith2024")
        out = conv.put(id="thread-1", link="paper:smith2024", rel="derived-from")
        assert "+1 link" in out.body
        out_links = store.links_for(c_id, relation="derived-from", direction="out")
        assert len(out_links) == 1

    def test_open_tag_added(self, store: Store, conv: ConversationHandler) -> None:
        c_id = _seed_conv(store, "thread-1")
        out = conv.put(id="thread-1", tags=["topic-noxrr"])
        assert "+1 tag" in out.body
        rows = store.tags_for(c_id)
        assert any(t.value == "topic-noxrr" for t in rows)


class TestConvPutRejected:
    def test_text_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="capture-on-write"):
            conv.put(id="thread-1", text="rewrite")

    def test_mode_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="mode='append' not supported"):
            conv.put(id="thread-1", mode="append")

    def test_missing_id(self, conv: ConversationHandler) -> None:
        with pytest.raises(BadInput, match="requires id="):
            conv.put(link="paper:foo")

    def test_unknown_slug(self, conv: ConversationHandler) -> None:
        with pytest.raises(NotFound, match="conv slug 'no-such' not found"):
            conv.put(id="no-such", link="paper:foo")

    def test_chunk_selector_rejected(
        self, conv: ConversationHandler, store: Store
    ) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="conv put operates at ref level"):
            conv.put(id="thread-1~0", link="paper:foo")

    def test_path_view_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="conv put operates at ref level"):
            conv.put(id="thread-1/transcript", link="paper:foo")

    def test_status_axis_rejected(
        self, conv: ConversationHandler, store: Store
    ) -> None:
        """Conversations have no closed-axis tags — STATUS: rejects."""
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="axis not allowed on kind 'conv'"):
            conv.put(id="thread-1", tags=["STATUS:open"])

    def test_no_op_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="requires at least one"):
            conv.put(id="thread-1")

    def test_bare_rel_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="rel= requires link= or unlink="):
            conv.put(id="thread-1", rel="cites", tags=["topic-x"])
