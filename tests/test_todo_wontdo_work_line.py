"""A won't-do todo linked ``fixes`` to a fixing gripe leaves one ``work:`` line.

The note lands inside the todo's tag transaction
(:func:`precis.handlers.gripe.todo_wontdo_work_note`, called from
``TodoHandler._after_tag_mutation``). Only a ``fixes`` edge onto a gripe
still at ``STATUS:fixing`` qualifies — the line is the claim's answer, so
an open/done gripe and a ``STATUS:done`` todo close write nothing.
"""

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag


def _gripe(hub: Hub) -> tuple[GripeHandler, int]:
    h = GripeHandler(hub=hub)
    h.put(text="Synthetic wontdo-note gripe")
    gid = next(
        r.id for r in h.store.list_refs(kind="gripe") if "wontdo-note" in r.title
    )
    return h, gid


def _todo(hub: Hub) -> tuple[TodoHandler, int]:
    t = TodoHandler(hub=hub)
    t.put(text="Synthetic wontdo-note todo")
    tid = next(
        r.id for r in t.store.list_refs(kind="todo") if "wontdo-note todo" in r.title
    )
    return t, tid


def _gripe_comments(store: Store, gid: int) -> list[str]:
    return [
        c.text
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


def _set_status(h: GripeHandler, gid: int, status: str) -> None:
    try:
        h.tag(id=gid, add=[f"STATUS:{status}"])
    except Exception:
        h.store.add_tag(gid, Tag.closed("STATUS", status), replace_prefix=True)


def test_wontdo_fixes_todo_posts_one_work_line(hub: Hub) -> None:
    h, gid = _gripe(hub)
    todo, tid = _todo(hub)
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="fixes")
    _set_status(h, gid, "fixing")
    todo.put(id=tid, text="superseded by td9")

    todo.tag(id=tid, add=["STATUS:won't-do"])
    assert _gripe_comments(todo.store, gid) == [
        f"work: td{tid} dropped (superseded by td9)"
    ]

    # Idempotent: re-adding the same STATUS requests it again, so the
    # hook re-runs — the gripe still carries exactly one work: line.
    todo.tag(id=tid, add=["STATUS:won't-do"])
    assert _gripe_comments(todo.store, gid) == [
        f"work: td{tid} dropped (superseded by td9)"
    ]


def test_done_close_writes_no_work_line(hub: Hub) -> None:
    h, gid = _gripe(hub)
    todo, tid = _todo(hub)
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="fixes")
    _set_status(h, gid, "fixing")

    todo.tag(id=tid, add=["STATUS:done"])
    assert _gripe_comments(todo.store, gid) == []


@pytest.mark.parametrize("gripe_status", ["open", "done"])
def test_wontdo_skips_non_fixing_gripe(hub: Hub, gripe_status: str) -> None:
    h, gid = _gripe(hub)
    todo, tid = _todo(hub)
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="fixes")
    _set_status(h, gid, gripe_status)

    todo.tag(id=tid, add=["STATUS:won't-do"])
    assert _gripe_comments(todo.store, gid) == []
