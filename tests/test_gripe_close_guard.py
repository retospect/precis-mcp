"""Closing a gripe: STATUS:done/wontfix needs a newest ``resolved:`` comment."""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler


def _gripe(hub: Hub) -> tuple[GripeHandler, int]:
    h = GripeHandler(hub=hub)
    h.put(text="Synthetic close-guard gripe")
    gid = next(
        r.id for r in h.store.list_refs(kind="gripe") if "close-guard" in r.title
    )
    return h, gid


def _tags(h: GripeHandler, gid: int) -> set[str]:
    return {str(t) for t in h.store.tags_for(gid)}


@pytest.mark.parametrize("status", ["STATUS:done", "STATUS:wontfix"])
def test_close_refused_without_resolved_comment(hub: Hub, status: str) -> None:
    h, gid = _gripe(hub)
    with pytest.raises(BadInput) as ei:
        h.tag(id=gid, add=[status])
    assert f"put(kind='gripe', id={gid}, text='resolved:" in str(ei.value.next)
    assert "STATUS:open" in _tags(h, gid)


def test_close_refused_when_newest_comment_is_not_resolved(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — ok")
    h.put(id=gid, text="actually still broken")
    with pytest.raises(BadInput):
        h.tag(id=gid, add=["STATUS:done"])


def test_done_succeeds_after_resolved_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc123 (r41) — ran the repro, passes")
    h.tag(id=gid, add=["STATUS:done"])
    assert "STATUS:done" in _tags(h, gid)


def test_wontfix_succeeds_after_resolved_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: wontfix — by design")
    h.tag(id=gid, add=["STATUS:wontfix"])
    assert "STATUS:wontfix" in _tags(h, gid)


def test_put_with_tags_closes_in_one_call(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — verified", tags=["STATUS:done"])
    assert "STATUS:done" in _tags(h, gid)


def test_other_status_changes_unguarded(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.tag(id=gid, add=["STATUS:triaged"])
    assert "STATUS:triaged" in _tags(h, gid)


def test_soft_deleted_get_shows_title_status_and_last_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — verified", tags=["STATUS:done"])
    h.delete(id=gid)
    body = h.get(id=gid).body
    assert "soft-deleted" in body
    assert "Synthetic close-guard gripe" in body
    assert "STATUS:done" in body
    assert "resolved: abc (r1) — verified" in body


def test_create_already_closed_refused(hub: Hub) -> None:
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput):
        h.put(text="born closed", tags=["STATUS:done"])


def test_hidden_resolved_comment_does_not_count(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — ok")
    h.delete(id=gid, args={"comment": 1})
    with pytest.raises(BadInput):
        h.tag(id=gid, add=["STATUS:done"])
