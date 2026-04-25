"""Phase 7 — Links primitive (unlink + /links-in + cross-kind)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis import tools
from precis.handlers._ref_base import RefHandler
from precis.protocol import PrecisError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _link(
    link_id,
    src_slug,
    dst_slug,
    relation="references",
    direction="outbound",
    src_node_id=None,
    dst_node_id=None,
):
    return {
        "id": link_id,
        "src_slug": src_slug,
        "dst_slug": dst_slug,
        "src_node_id": src_node_id,
        "dst_node_id": dst_node_id,
        "relation": relation,
        "display_relation": relation,
        "direction": direction,
    }


def _store_with_links(outbound=None, inbound=None):
    """Make a store whose get_links dispatch respects the direction arg."""
    outbound = outbound or []
    inbound = inbound or []
    store = MagicMock()

    def _get_links(slug, *, node_id=None, relation=None, direction="both"):
        results = []
        if direction in ("outbound", "both"):
            for ln in outbound:
                if node_id is not None and ln.get("src_node_id") != node_id:
                    continue
                if relation and ln.get("relation") != relation:
                    continue
                results.append({**ln, "direction": "outbound"})
        if direction in ("inbound", "both"):
            for ln in inbound:
                if relation and ln.get("relation") != relation:
                    continue
                results.append({**ln, "direction": "inbound"})
        return results

    store.get_links.side_effect = _get_links
    store.delete_link.return_value = True
    store.get_blocks.return_value = []
    store.get_link_count.return_value = {}
    return store


class _FakeHandler(RefHandler):
    """Minimal RefHandler subclass for exercising _read_links."""

    scheme = "fake"
    writable = True
    corpus_id = "fake"
    _ref_noun = "fake"

    def _list_overview(self, store):  # pragma: no cover — unused
        return ""


# ---------------------------------------------------------------------------
# unlink= — dispatch through tools.put
# ---------------------------------------------------------------------------


class TestUnlinkDispatch:
    _PATCH_STORE = "precis.tools.get_store"
    _PATCH_STORE2 = "precis._store.get_store"

    def test_unlink_by_dst_any_relation(self):
        store = _store_with_links(
            outbound=[
                _link(1, "wang2020state", "memory:a", "references"),
                _link(2, "wang2020state", "memory:a", "supports"),
                _link(3, "wang2020state", "memory:other", "references"),
            ]
        )
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(uri="paper:wang2020state", unlink="memory:a")
        # Both links to memory:a should be deleted; memory:other stays.
        assert store.delete_link.call_count == 2
        assert "2 links removed" in out
        deleted_ids = {c.args[0] for c in store.delete_link.call_args_list}
        assert deleted_ids == {1, 2}

    def test_unlink_by_dst_and_relation(self):
        store = _store_with_links(
            outbound=[
                _link(1, "wang2020state", "memory:a", "references"),
                _link(2, "wang2020state", "memory:a", "supports"),
            ]
        )
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(uri="paper:wang2020state", unlink="memory:a:references")
        assert store.delete_link.call_count == 1
        assert "[references]" in out
        assert store.delete_link.call_args.args[0] == 1

    def test_unlink_no_match_raises_precis_error(self):
        store = _store_with_links(outbound=[])
        with patch(self._PATCH_STORE2, return_value=store):
            with pytest.raises(PrecisError) as exc:
                tools.put(uri="paper:wang2020state", unlink="memory:ghost")
        assert "no links found" in exc.value.cause

    def test_unlink_relation_missing_hints_at_get_links(self):
        store = _store_with_links(
            outbound=[_link(1, "wang2020state", "memory:a", "references")]
        )
        with patch(self._PATCH_STORE2, return_value=store):
            with pytest.raises(PrecisError) as exc:
                tools.put(
                    uri="paper:wang2020state",
                    unlink="memory:a:wrong-relation",
                )
        # Error message points the agent at /links for inspection.
        assert "/links" in exc.value.next

    def test_unlink_with_block_selector(self):
        """Selector narrows deletion to links from a specific block."""
        store = _store_with_links(
            outbound=[
                _link(
                    1,
                    "wang2020state",
                    "memory:a",
                    "references",
                    src_node_id="wang2020state-b0005",
                ),
                _link(
                    2,
                    "wang2020state",
                    "memory:a",
                    "references",
                    src_node_id="wang2020state-b0010",
                ),
            ]
        )
        # Mock blocks so the selector→node_id resolution works.
        store.get_blocks.return_value = [
            {"block_index": 5, "node_id": "wang2020state-b0005"},
            {"block_index": 10, "node_id": "wang2020state-b0010"},
        ]
        # Mix legacy ›5 selector on input with the canonical ~5 in output
        # — proves the parser accepts both but the rendered selector is
        # always the canonical ASCII form (mcp-critic rule E3).
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(uri="paper:wang2020state\u203a5", unlink="memory:a")
        assert store.delete_link.call_count == 1
        assert store.delete_link.call_args.args[0] == 1  # only the block-5 link
        assert "wang2020state~5" in out

    def test_unlink_parameter_priority_over_mode(self):
        """unlink= short-circuits before mode-based write dispatch."""
        store = _store_with_links(
            outbound=[_link(1, "memory:x", "memory:y", "references")]
        )
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(
                uri="memory:x", text="ignored", mode="replace", unlink="memory:y"
            )
        # delete_link was called, handler.put(replace) was not.
        assert store.delete_link.call_count == 1
        assert "link" in out.lower()


# ---------------------------------------------------------------------------
# /links-in — inbound-only view
# ---------------------------------------------------------------------------


class TestLinksInView:
    def test_links_in_renders_inbound_only(self):
        store = _store_with_links(
            outbound=[_link(1, "memory:x", "memory:a", "references")],
            inbound=[_link(2, "todo:9", "memory:x", "supports")],
        )
        ref = {"slug": "memory:x"}
        h = _FakeHandler()
        out = h._read_links(store, ref, None, direction="inbound")
        # Inbound link rendered with ← arrow.
        assert "← [supports] ←" in out
        assert "todo:9" in out
        # Outbound link NOT rendered (direction=inbound).
        assert "memory:a" not in out
        assert "Inbound links" in out

    def test_links_in_empty_gives_inbound_specific_hint(self):
        store = _store_with_links(outbound=[], inbound=[])
        ref = {"slug": "memory:x"}
        h = _FakeHandler()
        out = h._read_links(store, ref, None, direction="inbound")
        # No "create a link" hint for inbound-empty — that's the wrong
        # remedy.  Instead a status message.
        assert "no inbound links" in out.lower()
        assert "nothing references" in out

    def test_links_default_direction_is_both(self):
        store = _store_with_links(
            outbound=[_link(1, "memory:x", "memory:a", "references")],
            inbound=[_link(2, "todo:9", "memory:x", "supports")],
        )
        ref = {"slug": "memory:x"}
        h = _FakeHandler()
        out = h._read_links(store, ref, None)
        # Both directions visible.
        assert "memory:a" in out
        assert "todo:9" in out
        assert "Links for memory:x" in out

    def test_links_next_hints_adapt_to_direction(self):
        store = _store_with_links(
            outbound=[_link(1, "memory:x", "memory:a", "references")],
        )
        ref = {"slug": "memory:x"}
        h = _FakeHandler()
        # When viewing inbound, hint toward /links for full picture.
        out_in = h._read_links(store, ref, None, direction="inbound")
        # Store returns no inbound links, so this hits the empty branch.
        assert "no inbound" in out_in.lower()

        # Regular /links shows both and hints toward /links-in.
        out = h._read_links(store, ref, None)
        assert "/links-in" in out


# ---------------------------------------------------------------------------
# RefHandler — links-in is a recognised view
# ---------------------------------------------------------------------------


class TestRefHandlerViewRegistration:
    def test_links_in_is_in_views_base(self):
        assert "links-in" in RefHandler.views

    def test_all_ref_subclasses_expose_links_in(self):
        from precis.handlers.conversation import ConversationHandler
        from precis.handlers.flashcard import FlashcardHandler
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.todo import TodoHandler

        for cls in (
            TodoHandler,
            FlashcardHandler,
            MemoryHandler,
            ConversationHandler,
        ):
            # ``views`` is a dict[view → method] on the class; every
            # RefHandler subclass inherits the base views via
            # ``{**RefHandler.views, ...}`` merge.
            assert "links-in" in cls.views, f"{cls.__name__}.views missing 'links-in'"


# ---------------------------------------------------------------------------
# Cross-kind — a memory can link to a paper / todo / fc
# ---------------------------------------------------------------------------


class TestCrossKindLinks:
    _PATCH_STORE2 = "precis._store.get_store"

    def test_memory_to_paper_link_created(self):
        """The link primitive is scheme-agnostic — memory → paper works."""
        store = _store_with_links(outbound=[])
        store.create_link.return_value = MagicMock(id=99)
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(
                uri="memory:cluster-db-user",
                link="wang2020state:supports",
            )
        store.create_link.assert_called_once_with(
            "memory:cluster-db-user",
            "wang2020state",
            "supports",
            src_node_id=None,
        )
        assert "memory:cluster-db-user" in out
        assert "wang2020state" in out

    def test_cross_kind_unlink(self):
        """Can also remove a memory → paper link via unlink=."""
        store = _store_with_links(
            outbound=[
                _link(7, "memory:cluster-db-user", "wang2020state", "supports"),
            ]
        )
        with patch(self._PATCH_STORE2, return_value=store):
            out = tools.put(
                uri="memory:cluster-db-user",
                unlink="wang2020state:supports",
            )
        assert store.delete_link.call_count == 1
        assert "wang2020state" in out
