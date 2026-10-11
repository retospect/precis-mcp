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
