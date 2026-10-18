"""gr494831 step 2 — forgiving inputs on the gripe kind.

An input with one reading runs as the canonical call and the reply's first
line names the reading (``precis.handlers._interpret``); an input with two
plausible readings still refuses with ``options=``. Patterns come from the
prod tool-call ledger, 2026-10-15..17.
"""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.gripe import GripeHandler
from precis.response import Response


def _gripe(h: GripeHandler, text: str) -> int:
    h.put(text=text)
    return next(r.id for r in h.store.list_refs(kind="gripe") if r.title == text)


def _two(hub: Hub) -> tuple[GripeHandler, int, int]:
    h = GripeHandler(hub=hub)
    return h, _gripe(h, "Synthetic gripe one"), _gripe(h, "Synthetic gripe two")


def _first(resp: Response) -> str:
    return resp.body.split("\n", 1)[0]


def _rest(resp: Response) -> str:
    return resp.body.split("\n", 1)[1]


def _comments(h: GripeHandler, ref_id: int) -> list[object]:
    return [
        c
        for c in h.store.chunks.list_chunks_for_ref(ref_id)
        if c.chunk_kind == "gripe_comment"
    ]


# ── put(id=N, link=/tags=) with no text= ─────────────────────────────


def test_link_only_comment_put_links_and_names_the_reading(hub: Hub) -> None:
    h, a, b = _two(hub)
    resp = h.put(id=a, link=f"gripe:{b}", rel="related-to")
    assert _first(resp) == (
        f"interpreted: put(kind='gripe', id={a}) with no text= read as "
        f"link(kind='gripe', id={a}, target='gripe:{b}', rel='related-to')"
    )
    assert "linked" in _rest(resp)
    assert any(lk.dst_ref_id == b for lk in h.store.links_for(a, direction="out"))
    assert _comments(h, a) == []


def test_tags_only_comment_put_tags_and_names_the_reading(hub: Hub) -> None:
    h, a, _ = _two(hub)
    resp = h.put(id=a, tags=["area:search"])
    assert _first(resp) == (
        f"interpreted: put(kind='gripe', id={a}) with no text= read as "
        f"tag(kind='gripe', id={a}, add=['area:search'])"
    )
    assert "area:search" in {str(t) for t in h.store.tags_for(a)}
    assert _comments(h, a) == []


def test_comment_put_without_text_or_edges_still_refuses(hub: Hub) -> None:
    """Nothing to do, a bare rel=, or mode='comment' (the caller said
    "comment" but sent none) has no single reading: refused, nothing written."""
    h, a, b = _two(hub)
    with pytest.raises(BadInput, match="requires text="):
        h.put(id=a)
    with pytest.raises(BadInput, match="requires text="):
        h.put(id=a, rel="related-to")
    with pytest.raises(BadInput, match="requires text="):
        h.put(id=a, mode="comment", link=f"gripe:{b}")
    assert not h.store.links_for(a, direction="out")
    assert _comments(h, a) == []


# ── get view aliases ─────────────────────────────────────────────────


def test_view_full_reads_as_comments(hub: Hub) -> None:
    h, a, _ = _two(hub)
    h.put(id=a, text="a comment")
    resp = h.get(id=a, view="full")
    assert _first(resp) == "interpreted: view='full' read as view='comments'"
    assert _rest(resp) == h.get(id=a, view="comments").body


def test_view_default_reads_as_the_default_view(hub: Hub) -> None:
    h, a, _ = _two(hub)
    resp = h.get(id=a, view="default")
    assert _first(resp) == "interpreted: view='default' read as the default view"
    assert _rest(resp) == h.get(id=a).body


def test_view_with_two_readings_still_refuses(hub: Hub) -> None:
    """'abstract' could mean the body head or a summary: refused with the
    view vocabulary."""
    h, a, _ = _two(hub)
    with pytest.raises(Unsupported) as exc:
        h.get(id=a, view="abstract")
    assert "comments" in (exc.value.options or [])


# ── PRIO near-miss ───────────────────────────────────────────────────


def test_prio_medium_reads_as_normal_on_create(hub: Hub) -> None:
    h = GripeHandler(hub=hub)
    resp = h.put(text="Synthetic prio gripe", tags=["PRIO:medium"])
    assert _first(resp) == "interpreted: 'PRIO:medium' read as 'PRIO:normal'"
    ref = next(
        r for r in h.store.list_refs(kind="gripe") if r.title == "Synthetic prio gripe"
    )
    assert ref.prio == 5  # PRIO:normal


def test_prio_medium_reads_as_normal_on_tag(hub: Hub) -> None:
    h, a, _ = _two(hub)
    resp = h.tag(id=a, add=["PRIO:medium"])
    assert _first(resp) == "interpreted: 'PRIO:medium' read as 'PRIO:normal'"
    ref = h.store.get_ref(kind="gripe", id=a)
    assert ref is not None and ref.prio == 5


def test_prio_medium_on_comment_put_is_named_once(hub: Hub) -> None:
    h, a, _ = _two(hub)
    resp = h.put(id=a, text="bumping", tags=["PRIO:medium"])
    assert resp.body.count("interpreted:") == 1
    assert _first(resp) == "interpreted: 'PRIO:medium' read as 'PRIO:normal'"


def test_numeric_prio_still_refuses(hub: Hub) -> None:
    """PRIO:3 has several readings (1..5, P0..P4, the 1..10 column), so it
    refuses with the tag vocabulary and files nothing."""
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match="invalid PRIO value") as exc:
        h.put(text="Synthetic numeric prio gripe", tags=["PRIO:3"])
    assert exc.value.options == ["high", "low", "normal", "urgent"]
    assert not [
        r
        for r in h.store.list_refs(kind="gripe")
        if r.title == "Synthetic numeric prio gripe"
    ]


def test_numeric_prio_refusal_points_at_prio_kwarg(hub: Hub) -> None:
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match="invalid PRIO value") as exc:
        h.put(text="Synthetic numeric prio next", tags=["PRIO:2"])
    assert "prio=<1..10>" in (exc.value.next or "")


def test_search_view_index_is_the_plain_listing(hub: Hub) -> None:
    h, _a, _b = _two(hub)
    plain = h.search(status="open")
    assert h.search(status="open", view="index").body == plain.body
