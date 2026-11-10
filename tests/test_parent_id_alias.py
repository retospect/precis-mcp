"""gr581237: the ``parent`` near-miss aliases onto ``parent_id``.

``parent`` is the name of the rel that parents a todo
(``link(kind='todo', rel='parent')``), so callers generalised that
spelling into the create call and died at the extras gate
(``keys ['parent'] not accepted by todo.put``) — three sessions in
24 h, all rejected before real work. ``parent_id`` is the parenting
kwarg on every kind that takes one (todo / job / finding ``put``), so
the near-miss has exactly one reading and
:func:`precis.runtime.dispatch._alias_parent_id` rewrites it at both
kwargs gates. Both spellings at once still refuse — two values can't
collapse into one slot silently — and a kind with no ``parent_id``
kwarg still refuses ``parent``.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from tests.conftest import id_of


def test_args_parent_aliased_to_parent_id_on_todo_create(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler = TodoHandler(hub=hub)
    root_id = id_of(handler.put(text="root").body)
    body = runtime_with_store.dispatch(
        "put",
        {"kind": "todo", "text": "child", "__extras__": {"parent": root_id}},
    )
    assert "created todo" in body and "[error" not in body
    ref = handler.store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None and ref.parent_id == root_id


def test_args_parent_digit_string_aliased_on_todo_create(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler = TodoHandler(hub=hub)
    root_id = id_of(handler.put(text="root").body)
    body = runtime_with_store.dispatch(
        "put",
        {"kind": "todo", "text": "child", "__extras__": {"parent": str(root_id)}},
    )
    assert "created todo" in body and "[error" not in body
    ref = handler.store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None and ref.parent_id == root_id


def test_parent_and_parent_id_together_is_an_error(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    root_id = id_of(TodoHandler(hub=hub).put(text="root").body)
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "todo",
            "text": "child",
            "parent_id": root_id,
            "__extras__": {"parent": root_id},
        },
    )
    assert "[error:BadInput]" in body
    assert "pass one, not both" in body


def test_args_parent_on_kind_without_parent_id_still_refused(
    runtime_with_store: PrecisRuntime,
) -> None:
    body = runtime_with_store.dispatch(
        "put",
        {"kind": "memory", "text": "a note", "__extras__": {"parent": 1}},
    )
    assert "[error:BadInput]" in body
    assert "not accepted by memory.put" in body


def test_top_level_parent_aliased_on_todo_create(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """A direct-dispatch payload can carry ``parent`` top-level (no MCP
    verb advertises it, so this path is in-process only); same alias."""
    handler = TodoHandler(hub=hub)
    root_id = id_of(handler.put(text="root").body)
    body = runtime_with_store.dispatch(
        "put", {"kind": "todo", "text": "child", "parent": root_id}
    )
    assert "created todo" in body and "[error" not in body
    ref = handler.store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None and ref.parent_id == root_id
