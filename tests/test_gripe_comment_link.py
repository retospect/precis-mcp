"""put(kind='gripe', id=N, text=..., link=..., rel=...) comments AND links."""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, PrecisError
from precis.handlers.gripe import GripeHandler


def _two(hub: Hub) -> tuple[GripeHandler, int, int]:
    h = GripeHandler(hub=hub)
    ids = []
    for text in ("Synthetic dup gripe", "Synthetic canonical gripe"):
        h.put(text=text)
        ids.append(
            next(r.id for r in h.store.list_refs(kind="gripe") if r.title == text)
        )
    return h, ids[0], ids[1]


def test_comment_and_link_in_one_put(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    resp = h.put(
        id=dup, text="dup of canonical", link=f"gripe:{canon}", rel="superseded-by"
    )
    assert "appended comment 1" in resp.body and "linked" in resp.body
    assert "dup of canonical" in h.get(id=dup).body
    assert any(lk.dst_ref_id == canon for lk in h.store.links_for(dup, direction="out"))


def test_bad_rel_writes_no_comment(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    with pytest.raises(PrecisError):
        h.put(id=dup, text="should not land", link=f"gripe:{canon}", rel="no-such-rel")
    assert "should not land" not in h.get(id=dup).body
    assert not h.store.links_for(dup, direction="out")


def test_rel_alone_and_link_plus_unlink_rejected(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    with pytest.raises(BadInput):
        h.put(id=dup, text="x", rel="superseded-by")
    with pytest.raises(BadInput):
        h.put(id=dup, text="x", link=f"gripe:{canon}", unlink=f"gripe:{canon}")


# ── tags=/untags= ride along on a comment append (gr484232) ────────


def _tags(h, ref_id: int) -> set[str]:
    return {str(t) for t in h.store.tags_for(ref_id)}


def test_comment_and_close_in_one_put(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    resp = h.put(id=dup, text="closing as wontfix", tags=["STATUS:wontfix"])
    assert "appended comment" in resp.body and "tagged" in resp.body
    assert "closing as wontfix" in h.get(id=dup).body
    tags = _tags(h, dup)
    assert "STATUS:wontfix" in tags and "STATUS:open" not in tags


def test_comment_tags_and_link_together(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    resp = h.put(
        id=dup,
        text="dup of canonical",
        link=f"gripe:{canon}",
        rel="superseded-by",
        tags=["STATUS:wontfix"],
    )
    assert "appended comment" in resp.body and "linked" in resp.body
    assert "STATUS:wontfix" in _tags(h, dup)


def test_comment_untags_removes_open_tag(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    h.tag(id=dup, add=["area:search"])
    h.put(id=dup, text="untagging", untags=["area:search"])
    assert "area:search" not in _tags(h, dup)


def test_bad_tag_writes_no_comment(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    with pytest.raises(BadInput):
        h.put(id=dup, text="should not land", tags=["STATUS:bogus"])
    assert "should not land" not in h.get(id=dup).body
    assert "STATUS:open" in _tags(h, dup)


def test_comment_bare_status_removal_refused(hub: Hub) -> None:
    """Same STATUS validation as tag(): a gripe always carries one STATUS,
    so untags=['STATUS:open'] with no replacement is refused — and the
    comment rolls back with it."""
    h, dup, canon = _two(hub)
    with pytest.raises(BadInput, match="always has one STATUS"):
        h.put(id=dup, text="should not land", untags=["STATUS:open"])
    assert "should not land" not in h.get(id=dup).body


def test_comment_empty_tags_lists_rejected(hub: Hub) -> None:
    h, dup, canon = _two(hub)
    with pytest.raises(BadInput, match="requires add= or remove="):
        h.put(id=dup, text="x", tags=[], untags=[])
