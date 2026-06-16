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

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.conversation import ConversationHandler
from precis.handlers.oracle import OracleHandler
from precis.store import BlockInsert, Store

# ── seed helpers ────────────────────────────────────────────────────


def _seed_oracle(store: Store, slug: str, title: str = "Test Oracle") -> int:
    ref = store.insert_ref(kind="oracle", slug=slug, title=title)
    return ref.id


def _seed_conv(store: Store, slug: str, title: str = "Test Thread") -> int:
    ref = store.insert_ref(kind="conv", slug=slug, title=title)
    # One block so list_blocks_for_ref / chunk parsing has data to chew on.
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text="hello")])
    return ref.id


def _seed_paper(store: Store, slug: str, title: str = "P") -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    return ref.id


# ── OracleHandler.put ───────────────────────────────────────────────


@pytest.fixture
def oracle(hub: Hub) -> OracleHandler:
    return OracleHandler(hub=hub)


class TestOraclePutAccepted:
    def test_link_oracle_to_paper(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "reviewer-rigor")
        _seed_paper(store, "smith2024")
        out = oracle.link(id="reviewer-rigor", target="paper:smith2024", rel="cites")
        assert "+1 link" in out.body
        assert "reviewer-rigor" in out.body
        out_links = store.links_for(o_id, relation="cites", direction="out")
        assert len(out_links) == 1

    def test_open_tag_added(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        out = oracle.tag(id="rubric-x", add=["topic-citation-style"])
        assert "+1 tag" in out.body
        rows = store.tags_for(o_id)
        assert any(t.value == "topic-citation-style" for t in rows)

    def test_untag_removes(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        oracle.tag(id="rubric-x", add=["topic-foo"])
        out = oracle.tag(id="rubric-x", remove=["topic-foo"])
        assert "-1 tag" in out.body
        rows = store.tags_for(o_id)
        assert all(t.value != "topic-foo" for t in rows)

    def test_unlink_removes(self, store: Store, oracle: OracleHandler) -> None:
        o_id = _seed_oracle(store, "rubric-x")
        _seed_paper(store, "p1")
        oracle.link(id="rubric-x", target="paper:p1", rel="cites")
        out = oracle.link(id="rubric-x", target="paper:p1", mode="remove", rel="cites")
        assert "-1 link" in out.body
        assert store.links_for(o_id, relation="cites", direction="out") == []


class TestOraclePutRejected:
    """Oracle bodies are curated externally; ``put`` is unwired on
    this kind after the seven-verb cutover. Tag and link mutation
    move to the dedicated verbs.
    """

    def test_put_unsupported(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        from precis.errors import Unsupported

        with pytest.raises(Unsupported, match="oracle does not support put"):
            oracle.put(id="rubric-x", text="rewrite me")

    def test_unknown_slug_on_link(self, oracle: OracleHandler) -> None:
        with pytest.raises(NotFound, match="oracle slug 'no-such' not found"):
            oracle.link(id="no-such", target="paper:foo")

    def test_status_axis_rejected(self, oracle: OracleHandler, store: Store) -> None:
        """Oracles have no closed-axis tags — STATUS: must reject."""
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="axis not allowed on kind 'oracle'"):
            oracle.tag(id="rubric-x", add=["STATUS:open"])

    def test_tag_no_op_rejected(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="requires add= or remove="):
            oracle.tag(id="rubric-x")

    def test_link_target_required(self, oracle: OracleHandler, store: Store) -> None:
        _seed_oracle(store, "rubric-x")
        with pytest.raises(BadInput, match="requires target="):
            oracle.link(id="rubric-x")


# ── ConversationHandler.put ─────────────────────────────────────────


@pytest.fixture
def conv(hub: Hub) -> ConversationHandler:
    return ConversationHandler(hub=hub)


class TestConvPutAccepted:
    def test_link_conv_to_paper(self, store: Store, conv: ConversationHandler) -> None:
        c_id = _seed_conv(store, "thread-1")
        _seed_paper(store, "smith2024")
        out = conv.link(id="thread-1", target="paper:smith2024", rel="derived-from")
        assert "+1 link" in out.body
        out_links = store.links_for(c_id, relation="derived-from", direction="out")
        assert len(out_links) == 1

    def test_open_tag_added(self, store: Store, conv: ConversationHandler) -> None:
        c_id = _seed_conv(store, "thread-1")
        out = conv.tag(id="thread-1", add=["topic-noxrr"])
        assert "+1 tag" in out.body
        rows = store.tags_for(c_id)
        assert any(t.value == "topic-noxrr" for t in rows)


class TestConvPutRejected:
    """Conv ``put`` is the chat-bridge entry point — it appends a turn
    to the ref's block list. The historical "put unsupported" case has
    been superseded by that wiring; the remaining tests in this class
    cover the shape-level rejections on link (chunk selector, unknown
    slug, path view).
    """

    def test_put_appends_a_turn(self, conv: ConversationHandler, store: Store) -> None:
        """put(text=…) appends a block to the conv ref; not unsupported."""
        rid = _seed_conv(store, "thread-1")
        before = len(store.list_blocks_for_ref(rid))
        conv.put(id="thread-1", text="follow-up message", author="alice")
        assert len(store.list_blocks_for_ref(rid)) == before + 1

    def test_unknown_slug_on_link(self, conv: ConversationHandler) -> None:
        with pytest.raises(NotFound, match="conv slug 'no-such' not found"):
            conv.link(id="no-such", target="paper:foo")

    def test_chunk_selector_rejected(
        self, conv: ConversationHandler, store: Store
    ) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="conv ops operate at ref level"):
            conv.link(id="thread-1~0", target="paper:foo")

    def test_path_view_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="conv ops operate at ref level"):
            conv.link(id="thread-1/transcript", target="paper:foo")

    def test_status_axis_rejected(
        self, conv: ConversationHandler, store: Store
    ) -> None:
        """Conversations have no closed-axis tags — STATUS: rejects."""
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="axis not allowed on kind 'conv'"):
            conv.tag(id="thread-1", add=["STATUS:open"])

    def test_tag_no_op_rejected(self, conv: ConversationHandler, store: Store) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="requires add= or remove="):
            conv.tag(id="thread-1")

    def test_link_target_required(
        self, conv: ConversationHandler, store: Store
    ) -> None:
        _seed_conv(store, "thread-1")
        with pytest.raises(BadInput, match="requires target="):
            conv.link(id="thread-1")
