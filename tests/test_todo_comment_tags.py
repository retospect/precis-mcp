"""put(kind='todo', id=N, text=..., tags=.../untags=...) comments AND retags.

gr570088: the comment append used to refuse ``tags=``/``untags=`` and send
the caller to ``tag()`` — the measured ``put|todo -> tag|todo`` chain ran
106 times in 14 d. The shared append helper now applies both edits in one
transaction, the gripe shape (gr484232).
"""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler


def _one(hub: Hub) -> tuple[TodoHandler, int]:
    h = TodoHandler(hub=hub)
    h.put(text="Synthetic todo")
    tid = next(
        r.id for r in h.store.list_refs(kind="todo") if r.title == "Synthetic todo"
    )
    return h, tid


def _tags(h: TodoHandler, ref_id: int) -> set[str]:
    return {str(t) for t in h.store.tags_for(ref_id)}


def test_comment_and_close_in_one_put(hub: Hub) -> None:
    """comment-then-close is one put: the comment lands and STATUS flips."""
    h, tid = _one(hub)
    resp = h.put(id=tid, text="done — shipped", tags=["STATUS:done"])
    assert "appended comment" in resp.body and "tagged" in resp.body
    assert "done — shipped" in h.get(id=tid, view="comments").body
    tags = _tags(h, tid)
    assert "STATUS:done" in tags and "STATUS:open" not in tags


def test_comment_and_open_tag_add(hub: Hub) -> None:
    h, tid = _one(hub)
    resp = h.put(id=tid, text="note", tags=["context:work"])
    assert "appended comment" in resp.body
    assert "context:work" in _tags(h, tid)


def test_comment_untags_removes_open_tag(hub: Hub) -> None:
    h, tid = _one(hub)
    h.tag(id=tid, add=["context:work"])
    h.put(id=tid, text="untagging", untags=["context:work"])
    assert "context:work" not in _tags(h, tid)


def test_bad_tag_writes_no_comment(hub: Hub) -> None:
    """Atomic: a refused tag edit rolls the appended comment back with it."""
    h, tid = _one(hub)
    with pytest.raises(BadInput, match="invalid STATUS value"):
        h.put(id=tid, text="should not land", tags=["STATUS:bogus"])
    assert "should not land" not in h.get(id=tid).body
    assert "STATUS:open" in _tags(h, tid)


def test_comment_empty_tags_lists_rejected(hub: Hub) -> None:
    h, tid = _one(hub)
    with pytest.raises(BadInput, match="requires add= or remove="):
        h.put(id=tid, text="x", tags=[], untags=[])


def test_other_put_kwargs_on_existing_id_still_refused(hub: Hub) -> None:
    """Only tags=/untags= ride along; every other put kwarg keeps the
    old refusal (gr450132) — link edits stay on the link verb."""
    h, tid = _one(hub)
    for kw in (
        {"prio": 2},
        {"meta": {"x": 1}},
        {"link": "td:99999"},
        {"mode": "create"},
        {"body": "x"},
    ):
        with pytest.raises(BadInput, match="put on existing todo"):
            h.put(id=tid, text="x", **kw)
