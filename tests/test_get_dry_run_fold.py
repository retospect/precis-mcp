"""gr688477: a ``dry_run=`` on ``get`` refuses with the mutating door named.

``get`` never writes, so the flag has no meaning on it — six prod
sessions in 24h passed ``args={'dry_run': True}`` (once with
``view='delete'``) on ``get(kind='memory', id=…)`` and each was refused
at the extras gate (``keys ['dry_run'] not accepted by memory.get; its
args= keys are listed below``), a dead end they retried.
:func:`precis.runtime.dispatch._fold_get_dry_run` intercepts the flag
before that gate and names the door that does preview writes:
``put``/``edit``/``delete`` with ``dry_run=True``. The suggested edit
example must be runnable as written — it carries ``mode='find-replace'``
explicitly (dispatch-level edit defaults ``mode`` to the replace door,
which refuses dry-run outright), and the anchor gate needs both
``find=`` and ``text=``, which the tests below execute to prove.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from tests.conftest import id_of

_BODY = "alpha bravo charlie delta"


def _memory_id(hub: Hub) -> int:
    return id_of(MemoryHandler(hub=hub).put(text=_BODY).body)


def test_dry_run_true_on_get_refuses_with_the_mutating_door(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _memory_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": rid, "dry_run": True}
    )
    assert "[error:BadInput]" in body
    assert "is a read" in body
    assert "dry_run" in body
    # The anchored preview example, with the caller's own id filled in.
    assert (
        f"edit(kind='memory', id={rid}, mode='find-replace', "
        "find='old', text='new', dry_run=True)" in body
    )
    assert f"delete(kind='memory', id={rid}, dry_run=True)" in body


def test_extras_spelling_refuses_the_same(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """The gripe's exact wire shape: the flag rides inside args=."""
    rid = _memory_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": rid, "__extras__": {"dry_run": True}}
    )
    assert "[error:BadInput]" in body
    assert "is a read" in body
    assert (
        f"edit(kind='memory', id={rid}, mode='find-replace', "
        "find='old', text='new', dry_run=True)" in body
    )


def test_suggested_edit_preview_runs_and_writes_nothing(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """The refusal's example must be copy-pasteable: both anchored
    arguments (find= AND text=) are required, so execute the exact
    suggested call and confirm it previews without writing."""
    rid = _memory_id(hub)
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": rid,
            "mode": "find-replace",
            "find": "bravo",
            "text": "BRAVO",
            "dry_run": True,
        },
    )
    assert "[error" not in out, out
    assert "bravo" in out
    after = runtime_with_store.dispatch("get", {"kind": "memory", "id": rid})
    assert "[error" not in after, after
    assert _BODY in after
    assert "BRAVO" not in after


def test_suggested_delete_preview_runs_and_retires_nothing(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _memory_id(hub)
    out = runtime_with_store.dispatch(
        "delete", {"kind": "memory", "id": rid, "dry_run": True}
    )
    assert "[error" not in out, out
    after = runtime_with_store.dispatch("get", {"kind": "memory", "id": rid})
    assert "[error" not in after, after
    assert _BODY in after


def test_view_delete_with_dry_run_names_the_delete_door(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """The ledger's ``view='delete'`` shape: previewing a retirement
    through get — the refusal points at delete's own dry-run door."""
    rid = _memory_id(hub)
    body = runtime_with_store.dispatch(
        "get",
        {"kind": "memory", "id": rid, "view": "delete", "dry_run": True},
    )
    assert "[error:BadInput]" in body
    assert "is a read" in body
    assert f"delete(kind='memory', id={rid}, dry_run=True)" in body


def test_dry_run_false_is_a_noop(runtime_with_store: PrecisRuntime, hub: Hub) -> None:
    rid = _memory_id(hub)
    body = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": rid, "dry_run": False}
    )
    assert "[error" not in body, body
    assert _BODY in body


def test_disagreeing_spellings_refuse(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    rid = _memory_id(hub)
    body = runtime_with_store.dispatch(
        "get",
        {
            "kind": "memory",
            "id": rid,
            "dry_run": True,
            "__extras__": {"dry_run": False},
        },
    )
    assert "[error:BadInput]" in body
    assert "one argument" in body


def test_todo_get_dry_run_refuses_with_the_generic_door(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """Non-memory kinds get the same refusal with the generic hint."""
    from precis.handlers.todo import TodoHandler

    rid = id_of(TodoHandler(hub=hub).put(text="dry-run fixture").body)
    body = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": rid, "dry_run": True}
    )
    assert "[error:BadInput]" in body
    assert "is a read" in body
    assert "put/edit/delete(kind='todo', ..., dry_run=True)" in body
