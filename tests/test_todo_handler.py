"""Tests for TodoHandler — state machine, creation, transitions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis.handlers.todo import (
    STATES,
    TRANSITIONS,
    TodoHandler,
    _coerce_tag_list,
    _parse_tags,
    _slugify,
)
from precis.protocol import PrecisError

# _get_store is imported by name in both _ref_base and todo modules,
# so we need to patch both bindings.
_PATCH_STORE = "precis.handlers._ref_base._get_store"
_PATCH_STORE_TODO = "precis.handlers.todo._get_store"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler():
    return TodoHandler()


def _mock_store(
    refs: list[dict] | None = None,
    blocks: list[dict] | None = None,
    link_count: dict | None = None,
):
    """Create a mock store with configurable return values."""
    store = MagicMock()

    _refs = {r["slug"]: r for r in (refs or [])}

    def _get(ident):
        if ident in _refs:
            return _refs[ident]
        # Try matching by ref_id
        for r in _refs.values():
            if r.get("ref_id") == ident or r.get("id") == ident:
                return r
        return None

    store.get.side_effect = _get
    store.list_papers.return_value = refs or []
    store.list_refs_by_corpus.return_value = refs or []
    store.get_blocks.return_value = blocks or []
    store.get_toc.return_value = []
    store.get_links.return_value = []
    store.get_link_count.return_value = link_count or {}
    store.search_text.return_value = []
    store.create_ref.return_value = 42
    store.update_ref_metadata.return_value = None
    store.update_block_text.return_value = None

    return store


def _todo_ref(
    slug="todo:fix-bug",
    title="Fix the bug",
    state="pending",
    priority="medium",
    ref_id=1,
    tags=None,
):
    ref = {
        "slug": slug,
        "title": title,
        "ref_id": ref_id,
        "id": ref_id,
        "meta": {
            "state": state,
            "priority": priority,
            "created": "2026-03-30T12:00:00Z",
        },
    }
    if tags is not None:
        import json as _json

        # Match acatome-store shape: tags column is a JSON-encoded string.
        ref["tags"] = _json.dumps(tags)
    return ref


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert _slugify("Fix the bug") == "todo:fix-the-bug"

    def test_special_chars(self):
        result = _slugify("Add CO₂ capture (v2)")
        assert result.startswith("todo:")
        assert " " not in result

    def test_empty(self):
        assert _slugify("") == ""

    def test_truncation(self):
        long_title = "a" * 100
        slug = _slugify(long_title)
        # "todo:" + max 60 chars
        assert len(slug) <= 65


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_all_states_have_transitions(self):
        for state in STATES:
            assert state in TRANSITIONS

    def test_pending_transitions(self):
        assert TRANSITIONS["pending"] == {"in_progress", "blocked", "cancelled"}

    def test_in_progress_transitions(self):
        assert "done" in TRANSITIONS["in_progress"]
        assert "pending" in TRANSITIONS["in_progress"]

    def test_done_can_reopen(self):
        assert "pending" in TRANSITIONS["done"]

    def test_cancelled_can_reopen(self):
        assert "pending" in TRANSITIONS["cancelled"]

    def test_blocked_transitions(self):
        assert "pending" in TRANSITIONS["blocked"]
        assert "in_progress" in TRANSITIONS["blocked"]


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


class TestRead:
    def test_overview(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="todo:fix-bug",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "fix-bug" in result
        assert "Fix the bug" in result
        assert "pending" in result

    def test_state_view(self):
        handler = _make_handler()
        ref = _todo_ref(state="in_progress")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="todo:fix-bug",
                selector=None,
                view="state",
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "in_progress" in result
        assert "done" in result  # valid transition

    def test_meta_view(self):
        handler = _make_handler()
        ref = _todo_ref(priority="high")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="todo:fix-bug",
                selector=None,
                view="meta",
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "high" in result
        assert "fix-bug" in result

    def test_list_todos(self):
        handler = _make_handler()
        refs = [
            _todo_ref("todo:fix-bug", "Fix the bug", "pending", ref_id=1),
            _todo_ref("todo:add-tests", "Add tests", "done", ref_id=2),
        ]
        store = _mock_store(refs=refs)
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "2 todos" in result
        assert "fix-bug" in result
        assert "add-tests" in result

    def test_not_found(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE, return_value=store):
            with pytest.raises(PrecisError, match="not in corpus"):
                handler.read(
                    path="todo:nonexistent",
                    selector=None,
                    view=None,
                    subview=None,
                    query="",
                    summarize=False,
                    depth=0,
                    page=1,
                )


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_todo(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="",
                selector=None,
                text="Fix the critical bug in parser",
                mode="append",
            )
        assert "Created todo" in result
        assert "pending" in result
        store.create_ref.assert_called_once()
        call_kwargs = store.create_ref.call_args
        assert call_kwargs.kwargs["corpus_id"] == "todos"
        assert call_kwargs.kwargs["metadata"]["state"] == "pending"

    def test_create_no_text(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="text= required"):
                handler.put(path="", selector=None, text="", mode="append")

    def test_create_slug_collision_disambiguates(self):
        handler = _make_handler()
        store = _mock_store()
        # First call raises, second succeeds
        store.create_ref.side_effect = [
            ValueError("Slug already exists: todo:fix-bug"),
            42,
        ]
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="",
                selector=None,
                text="Fix bug",
                mode="append",
            )
        assert "Created todo" in result
        assert store.create_ref.call_count == 2


class TestStateTransition:
    def test_valid_transition(self):
        handler = _make_handler()
        ref = _todo_ref(state="pending")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="in_progress",
                mode="state",
            )
        assert "pending" in result
        assert "in_progress" in result
        store.update_ref_metadata.assert_called_once_with(
            "todo:fix-bug", {"state": "in_progress"}
        )

    def test_invalid_transition(self):
        handler = _make_handler()
        ref = _todo_ref(state="pending")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="cannot transition"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="done",
                    mode="state",
                )

    def test_unknown_state(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="unknown state"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="gibberish",
                    mode="state",
                )

    def test_done_to_pending_reopen(self):
        handler = _make_handler()
        ref = _todo_ref(state="done")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="pending",
                mode="state",
            )
        assert "done" in result
        assert "pending" in result

    def test_full_lifecycle(self):
        """pending → in_progress → done."""
        handler = _make_handler()

        # Step 1: pending → in_progress
        ref = _todo_ref(state="pending")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="in_progress",
                mode="state",
            )
        assert "in_progress" in result

        # Step 2: in_progress → done
        ref = _todo_ref(state="in_progress")
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="done",
                mode="state",
            )
        assert "done" in result


class TestUpdateBody:
    def test_replace_body(self):
        handler = _make_handler()
        ref = _todo_ref()
        blocks = [
            {
                "node_id": "todo:fix-bug-b0000",
                "text": "old text",
                "block_type": "text",
                "block_index": 0,
            }
        ]
        store = _mock_store(refs=[ref], blocks=blocks)
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="Updated description",
                mode="replace",
            )
        assert "Updated" in result
        store.update_block_text.assert_called_once()

    def test_replace_no_text(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="text= required"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="",
                    mode="replace",
                )

    def test_unsupported_mode(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="not supported on todo"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="foo",
                    mode="before",
                )


# ---------------------------------------------------------------------------
# URI integration
# ---------------------------------------------------------------------------


class TestURI:
    def test_todo_uri_parse(self):
        from precis.uri import parse

        parsed = parse("todo:fix-the-bug")
        assert parsed.scheme == "todo"
        assert parsed.path == "fix-the-bug"

    def test_todo_uri_with_view(self):
        from precis.uri import parse

        parsed = parse("todo:fix-the-bug/state")
        assert parsed.scheme == "todo"
        assert "fix-the-bug" in parsed.path
        assert parsed.view == "state"

    def test_server_to_uri(self):
        from precis.server import _to_uri

        assert _to_uri("todo:fix-bug") == "todo:fix-bug"


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TestTagHelpers:
    def test_parse_tags_none(self):
        assert _parse_tags({}) == []

    def test_parse_tags_json_string(self):
        assert _parse_tags({"tags": '["a", "b"]'}) == ["a", "b"]

    def test_parse_tags_list(self):
        assert _parse_tags({"tags": ["a", "b"]}) == ["a", "b"]

    def test_parse_tags_malformed(self):
        assert _parse_tags({"tags": "not-json"}) == []

    def test_coerce_list(self):
        assert _coerce_tag_list(["a", "b", "b"]) == ["a", "b"]

    def test_coerce_comma_string(self):
        assert _coerce_tag_list("urgent, work") == ["urgent", "work"]

    def test_coerce_whitespace(self):
        assert _coerce_tag_list("  a   b  c  ") == ["a", "b", "c"]

    def test_coerce_none(self):
        assert _coerce_tag_list(None) == []


class TestCreateWithTags:
    def test_create_passes_tags_list(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            handler.put(
                path="",
                selector=None,
                text="Buy milk",
                mode="append",
                tags=["shopping", "home"],
            )
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["tags"] == ["shopping", "home"]
        assert kwargs["corpus_id"] == "todos"

    def test_create_passes_tags_comma_string(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            handler.put(
                path="",
                selector=None,
                text="Buy milk",
                mode="append",
                tags="shopping, home",
            )
        assert store.create_ref.call_args.kwargs["tags"] == ["shopping", "home"]

    def test_create_without_tags_passes_none(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            handler.put(
                path="", selector=None, text="Buy milk", mode="append"
            )
        assert store.create_ref.call_args.kwargs["tags"] is None

    def test_create_output_shows_tags(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            out = handler.put(
                path="",
                selector=None,
                text="Buy milk",
                mode="append",
                tags=["shopping"],
            )
        assert "shopping" in out


class TestTagsView:
    def test_empty_state(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="",
                selector=None,
                view="tags",
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "No tagged todos yet" in result
        assert "mode='tag'" in result

    def test_histogram(self):
        handler = _make_handler()
        refs = [
            _todo_ref("todo:a", "A", tags=["urgent", "work"], ref_id=1),
            _todo_ref("todo:b", "B", tags=["urgent"], ref_id=2),
            _todo_ref("todo:c", "C", tags=["home"], ref_id=3),
            _todo_ref("todo:d", "D", ref_id=4),  # no tags
        ]
        store = _mock_store(refs=refs)
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="",
                selector=None,
                view="tags",
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "3 distinct" in result
        # Counts: urgent=2, work=1, home=1. urgent must come first.
        urgent_idx = result.index("urgent")
        work_idx = result.index("work")
        assert urgent_idx < work_idx
        assert "2" in result  # urgent count


class TestListWithTags:
    def test_list_entry_shows_tags(self):
        handler = _make_handler()
        refs = [_todo_ref(tags=["urgent", "build"])]
        store = _mock_store(refs=refs)
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "#urgent" in result
        assert "#build" in result

    def test_overview_shows_tags(self):
        handler = _make_handler()
        ref = _todo_ref(tags=["urgent", "build"])
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="todo:fix-bug",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "tags:" in result
        assert "urgent" in result
        assert "build" in result

    def test_grep_filter_matches_tag(self):
        handler = _make_handler()
        refs = [
            _todo_ref("todo:a", "Alpha", tags=["urgent"], ref_id=1),
            _todo_ref("todo:b", "Bravo", tags=["home"], ref_id=2),
        ]
        store = _mock_store(refs=refs)
        with patch(_PATCH_STORE, return_value=store):
            result = handler.read(
                path="",
                selector=None,
                view=None,
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
                grep="tag:urgent",
            )
        assert "todo:a" in result
        assert "todo:b" not in result


class TestTagMutation:
    def test_tag_adds_via_store(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        store.add_tags.return_value = True
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="urgent,build",
                mode="tag",
            )
        store.add_tags.assert_called_once_with(
            "todo:fix-bug", ["urgent", "build"]
        )
        # Critical: tag mode must NOT touch create_ref, update_ref_metadata,
        # or update_block_text. Only the tags column changes.
        store.create_ref.assert_not_called()
        store.update_ref_metadata.assert_not_called()
        store.update_block_text.assert_not_called()
        store.remove_tags.assert_not_called()
        assert "Tagged" in result

    def test_tag_accepts_tags_kwarg(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        store.add_tags.return_value = True
        with patch(_PATCH_STORE_TODO, return_value=store):
            handler.put(
                path="todo:fix-bug",
                selector=None,
                text="",
                mode="tag",
                tags=["urgent"],
            )
        store.add_tags.assert_called_once_with("todo:fix-bug", ["urgent"])

    def test_untag_removes_via_store(self):
        handler = _make_handler()
        ref = _todo_ref(tags=["urgent", "build"])
        store = _mock_store(refs=[ref])
        store.remove_tags.return_value = True
        with patch(_PATCH_STORE_TODO, return_value=store):
            result = handler.put(
                path="todo:fix-bug",
                selector=None,
                text="urgent",
                mode="untag",
            )
        store.remove_tags.assert_called_once_with("todo:fix-bug", ["urgent"])
        # Critical: removing tags MUST NOT delete the underlying todo or
        # clear its state/metadata/body.
        store.create_ref.assert_not_called()
        store.update_ref_metadata.assert_not_called()
        store.update_block_text.assert_not_called()
        store.add_tags.assert_not_called()
        assert "Untagged" in result

    def test_tag_requires_id(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="id= required"):
                handler.put(
                    path="", selector=None, text="urgent", mode="tag"
                )

    def test_untag_requires_id(self):
        handler = _make_handler()
        store = _mock_store()
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="id= required"):
                handler.put(
                    path="", selector=None, text="urgent", mode="untag"
                )

    def test_tag_requires_at_least_one_tag(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="needs at least one tag"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="",
                    mode="tag",
                )

    def test_tag_store_reports_not_found(self):
        handler = _make_handler()
        ref = _todo_ref()
        store = _mock_store(refs=[ref])
        store.add_tags.return_value = False
        with patch(_PATCH_STORE_TODO, return_value=store):
            with pytest.raises(PrecisError, match="could not update tags"):
                handler.put(
                    path="todo:fix-bug",
                    selector=None,
                    text="urgent",
                    mode="tag",
                )
