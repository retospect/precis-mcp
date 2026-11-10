"""gr593337: the ``full=`` near-miss folds onto ``view='full'``.

``full`` is get's verb-declared skill knob (``tools/core.py::get`` — it
forces a full re-serve past ``have=<rev>``); no other kind's get
declares it, but callers generalise the spelling anyway — one prod
session re-issued ``get(kind='todo', id=…, full=true)`` eight times
inside a minute, refused at the kwargs gate every time
(``does not accept ['full']``). On a kind whose get doesn't declare
it, the flag has exactly one reading — the complete read — so
:func:`precis.runtime.dispatch._fold_get_full_flag` rewrites it to
``view='full'`` before either kwargs gate: todo/gripe/memory/job fold
that view to the complete card, and a kind that doesn't know the view
refuses with its own vocabulary. A handler that declares ``full``
(skill) or tolerates extras keeps it untouched; ``full=true`` against
an explicit different ``view=`` still refuses — two readings can't
collapse silently.
"""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from tests.conftest import id_of


def _todo_id(hub: Hub) -> int:
    return id_of(TodoHandler(hub=hub).put(text="full-flag fixture").body)


def test_full_true_on_todo_reads_the_complete_card(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch("get", {"kind": "todo", "id": rid, "full": True})
    assert "[error" not in body, body
    assert "full-flag fixture" in body
    # The fold is named, not silent (the interpreted convention).
    assert "view='full' read as the default view" in body


def test_full_true_via_args_spelling_folds_the_same(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": rid, "__extras__": {"full": True}}
    )
    assert "[error" not in body, body
    assert "full-flag fixture" in body


def test_full_false_is_a_noop(runtime_with_store: PrecisRuntime, hub: Hub) -> None:
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": rid, "full": False}
    )
    assert "[error" not in body, body
    assert "full-flag fixture" in body
    # Nothing was asked for, so no 'read as' line is owed.
    assert "read as" not in body


def test_full_string_false_is_a_noop(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """An undeclared key arrives schema-uncoerced — ``'false'`` must
    not read as set."""
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": rid, "full": "false"}
    )
    assert "[error" not in body, body
    assert "read as" not in body


def test_full_true_with_another_view_refuses(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": rid, "full": True, "view": "comments"}
    )
    assert "[error:BadInput]" in body
    assert "conflicts with view='comments'" in body
    assert "view='full'" in body


def test_full_true_on_skill_still_reaches_the_handler(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``kind='skill'`` declares ``full`` itself — the fold must leave
    the flag alone there."""
    body = runtime_with_store.dispatch(
        "get", {"kind": "skill", "id": "precis-overview", "full": True}
    )
    assert "[error" not in body, body
    assert len(body) > 100


def test_disagreeing_full_spellings_refuse(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get",
        {
            "kind": "todo",
            "id": rid,
            "full": True,
            "__extras__": {"full": False},
        },
    )
    assert "[error:BadInput]" in body
    assert "one argument" in body


def test_full_fold_names_its_source_id_in_output(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """The gripe's exact shape: ``id='td<N>'`` handle + ``full=true`` —
    previously ``does not accept ['full']``, now the card."""
    rid = _todo_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": f"td{rid}", "full": True}
    )
    assert "[error" not in body, body
    assert re.search(rf"td{rid}\b|id={rid}\b", body) is not None or (
        "full-flag fixture" in body
    )
