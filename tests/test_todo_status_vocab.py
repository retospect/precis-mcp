"""Per-kind STATUS vocabularies for todo and quest (gr482126).

``STATUS:`` is a shared union axis; ``_KIND_STATUS_VALUES`` scopes it
per kind. Before this, ``tag(kind='todo', add=['STATUS:abandoned'])``
was accepted — quest vocabulary on a todo — and every terminal-status
query then treated the row as still open (nursery long-wait alerts,
doctor, hubs, quest search). The todo terminal set lives once in
``precis.todo.TERMINAL_TODO_STATUSES``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.store.types import _KIND_STATUS_VALUES, Tag
from precis.todo import TERMINAL_TODO_STATUSES

pytestmark = pytest.mark.db


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def test_parse_strict_rejects_abandoned_on_todo() -> None:
    with pytest.raises(BadInput, match="invalid STATUS value for kind 'todo'"):
        Tag.parse_strict("STATUS:abandoned", kind="todo")


def test_todo_error_lists_the_todo_options() -> None:
    with pytest.raises(BadInput) as ei:
        Tag.parse_strict("STATUS:abandoned", kind="todo")
    assert ei.value.options == sorted(_KIND_STATUS_VALUES["todo"])
    assert "abandoned" not in ei.value.options


def test_parse_strict_accepts_the_todo_vocabulary() -> None:
    for value in _KIND_STATUS_VALUES["todo"]:
        assert Tag.parse_strict(f"STATUS:{value}", kind="todo").value == value


def test_parse_strict_rejects_done_on_quest() -> None:
    with pytest.raises(BadInput, match="invalid STATUS value for kind 'quest'"):
        Tag.parse_strict("STATUS:done", kind="quest")


def test_terminal_constant_is_inside_the_todo_vocab() -> None:
    assert _KIND_STATUS_VALUES["todo"] > TERMINAL_TODO_STATUSES
    assert frozenset({"done", "won't-do", "auto-timeout"}) == TERMINAL_TODO_STATUSES


def test_tag_verb_rejects_abandoned_on_todo(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    rid = _id_of(handler.put(text="probe").body)
    with pytest.raises(BadInput, match="invalid STATUS value for kind 'todo'"):
        handler.tag(id=rid, add=["STATUS:abandoned"])


def test_quest_create_rejects_done(hub: Hub) -> None:
    """The create path used to bypass ``QuestHandler.tag()``'s lifecycle
    guard and happily stamp ``STATUS:done`` on a new quest."""
    handler = QuestHandler(hub=hub)
    with pytest.raises(BadInput, match="STATUS"):
        handler.put(text="probe", tags=["STATUS:done"])
