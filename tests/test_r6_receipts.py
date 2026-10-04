"""R6 native dogfood receipt and exposed-contract regressions."""

import inspect

import pytest

from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.tools.core import edit


@pytest.mark.parametrize(
    "tags,status,start",
    [
        ([], "open", True),
        (["STATUS:paused"], "paused", False),
        (["halt"], "open", False),
        (["halt:test"], "open", False),
        (["STATUS:done"], "done", False),
    ],
)
def test_todo_receipt_reports_persisted_status(hub, tags, status, start):
    handler = TodoHandler(hub=hub)
    result = handler.put(text="Synthetic receipt probe", tags=tags)
    assert f"STATUS:{status}" in result.body
    assert ("start work on this todo" in result.body) is start
    assert result.ref_id is not None
    assert f"STATUS:{status}" in [str(t) for t in handler.store.tags_for(result.ref_id)]


@pytest.mark.parametrize("status", ["active", "dormant", "abandoned"])
def test_quest_receipt_reports_persisted_status(hub, status):
    handler = QuestHandler(hub=hub)
    result = handler.put(text="Synthetic striving receipt", tags=[f"STATUS:{status}"])
    assert f"STATUS:{status}" in result.body
    assert f"STATUS:{status}" in handler.get(id=handler._last_created_id).body


def test_emitted_subtree_hint_reaches_native_search(runtime_with_store, monkeypatch):
    from precis.tools import core

    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    handler = runtime_with_store.hub.handler_for("todo")
    root = handler.put(text="Synthetic subtree A")
    other = handler.put(text="Synthetic subtree B")
    handler.put(text="Inside selected subtree", parent_id=root.ref_id)
    handler.put(text="Outside selected subtree", parent_id=other.ref_id)
    tree = handler.get(id=root.ref_id, view="tree").body
    assert f"under={root.ref_id}" in tree
    assert "args=" not in tree
    result = core.search(kind="todo", view="doable", under=root.ref_id)
    assert "Inside selected subtree" in result
    assert "Outside selected subtree" not in result
    # Preserve the old direct-handler args= route.
    assert (
        "Inside selected subtree"
        in handler.search(view="doable", args={"under": root.ref_id}).body
    )


def test_edit_advertisement_allows_body_only_todo_update(
    runtime_with_store, monkeypatch
):
    from precis.tools import core

    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    handler = runtime_with_store.hub.handler_for("todo")
    result = handler.put(text="Short fixture title", body="Original details")
    description = inspect.getdoc(edit)
    assert description is not None
    assert "todo" in description
    assert "body=" in description
    assert len(description.encode()) < 1024
    receipt = core.edit(
        kind="todo", id=result.ref_id, mode="replace", body="Revised details"
    )
    assert "replaced details body" in receipt
    assert "Short fixture title" in handler.get(id=result.ref_id).body
    assert "Revised details" in handler.get(id=result.ref_id).body


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"view": "active", "under": 1}, "requires view"),
        ({"view": "doable", "under": 1, "args": {"under": 2}}, "disagree"),
        ({"view": "doable", "under": "invalid"}, "must be an integer"),
    ],
)
def test_subtree_scope_rejects_invalid_or_conflicting_arguments(hub, kwargs, message):
    from precis.errors import BadInput

    with pytest.raises(BadInput, match=message):
        TodoHandler(hub=hub).search(**kwargs)


def test_native_edit_schema_describes_body_only_replace():
    from precis import server

    tool = server.mcp._tool_manager.get_tool("edit")
    assert tool is not None
    props = tool.parameters["properties"]
    assert "body=" in props["text"]["description"]
    assert "body=" in props["mode"]["description"]
    assert "text" not in tool.parameters.get("required", [])
