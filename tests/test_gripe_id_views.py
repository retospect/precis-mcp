"""gr514477: id-range filters and the compact ``view='ids'`` listing.

A triage pass over ~270 open gripes needed the ids of the whole set,
gripe bodies for an id range, and a batch body read — three things the
surface couldn't do without ``more()`` elision or raw SQL. Now:
``args={'id_min': N, 'id_max': M}`` bounds every search path (SQL on
the listing and count views, the ``include_ref_ids`` channel on ranked
ones); ``view='ids'`` renders the filtered set one handle per line so a
full page never mid-elides; ``get(id=[...], view='body')`` is the batch
title+body read.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store.types import Tag
from tests._refs import insert_gripe
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _seed(handler: GripeHandler, n: int = 5) -> list[int]:
    return [
        int(insert_gripe(handler.store, f"id view probe {i}\ndetail line {i}").id)
        for i in range(n)
    ]


# ── view='ids': the compact, never-elided enumeration ────────────────


def test_ids_view_is_one_handle_per_line(handler: GripeHandler) -> None:
    ids = _seed(handler)
    body = handler.search(tags=["STATUS:open"], view="ids", page_size=100).body
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    for gid in ids:
        assert f"gr{gid}" in lines
    # no TOON table cells, no summary column — the listing is the ids
    assert "remaining_words" not in body
    assert "links=" not in body


def test_ids_view_paginates_without_eliding(handler: GripeHandler) -> None:
    _seed(handler, n=12)
    body = handler.search(tags=["STATUS:open"], view="ids", page_size=10).body
    id_lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert len(id_lines) == 10
    assert "page 1 of" in body


def test_ids_view_ranked_search_too(handler: GripeHandler) -> None:
    ids = [
        id_of(handler.put(text=f"id view probe {i}\ndetail {i}").body) for i in range(5)
    ]
    body = handler.search(q="id view probe", view="ids", page_size=100).body
    id_lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert set(id_lines) == {f"gr{gid}" for gid in ids}
    assert "rank=" not in body


# ── view='list': the compact id + title listing (gr515522) ──────────


def test_list_view_is_handle_plus_title_per_line(handler: GripeHandler) -> None:
    ids = _seed(handler)
    body = handler.search(tags=["STATUS:open"], view="list", page_size=100).body
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert len(lines) == len(ids)
    for i, gid in enumerate(ids):
        assert f"gr{gid}  id view probe {i}" in lines
    # still one line per hit — no folded hit-table cells
    assert "remaining_words" not in body
    assert "links=" not in body


def test_list_view_paginates_without_eliding(handler: GripeHandler) -> None:
    _seed(handler, n=12)
    body = handler.search(tags=["STATUS:open"], view="list", page_size=10).body
    id_lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert len(id_lines) == 10
    assert "page 1 of" in body


def test_list_view_ranked_search_too(handler: GripeHandler) -> None:
    ids = [
        id_of(handler.put(text=f"list view probe {i}\ndetail {i}").body)
        for i in range(5)
    ]
    body = handler.search(q="list view probe", view="list", page_size=100).body
    id_lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert {ln.split(" ")[0] for ln in id_lines} == {f"gr{g}" for g in ids}
    assert "rank=" not in body


def test_list_view_stays_one_line_for_long_title(handler: GripeHandler) -> None:
    insert_gripe(handler.store, "long title " + "padding " * 40 + "\nrest")
    body = handler.search(tags=["STATUS:open"], view="list", page_size=100).body
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert len(lines) == 1
    assert len(lines[0]) < 250
    assert "rest" not in body


def test_list_view_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler = GripeHandler(hub=hub)
    ids = _seed(handler)
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "gripe",
            "view": "list",
            "tags": ["STATUS:open"],
            "page_size": 100,
        },
    )
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert {ln.split(" ")[0] for ln in lines} == {f"gr{g}" for g in ids}
    assert "id view probe" in body
    assert "[error" not in body


# ── args={'id_min', 'id_max'}: the id-range filter ───────────────────


def test_id_range_bounds_the_tag_listing(handler: GripeHandler) -> None:
    ids = _seed(handler)
    lo, hi = ids[1], ids[3]
    body = handler.search(
        tags=["STATUS:open"], id_min=lo, id_max=hi, page_size=100
    ).body
    assert f"gr{ids[0]}" not in body and f"gr{ids[4]}" not in body
    for gid in (ids[1], ids[2], ids[3]):
        assert f"gr{gid}" in body


def test_id_range_bounds_the_ids_view(handler: GripeHandler) -> None:
    ids = _seed(handler)
    body = handler.search(
        view="ids", tags=["STATUS:open"], id_min=ids[2], page_size=100
    ).body
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert set(lines) == {f"gr{gid}" for gid in ids[2:]}


def test_id_range_alone_is_a_complete_enumeration_filter(
    handler: GripeHandler,
) -> None:
    ids = _seed(handler)
    body = handler.search(id_min=ids[3], page_size=100).body
    assert f"gr{ids[4]}" in body and f"gr{ids[0]}" not in body


def test_id_range_bounds_the_count_view(handler: GripeHandler) -> None:
    ids = _seed(handler)
    resp = handler.search(
        view="count", tags=["STATUS:open"], id_min=ids[1], id_max=ids[3]
    )
    assert resp.result_count == 3


def test_id_range_bounds_a_ranked_search(handler: GripeHandler) -> None:
    ids = [
        id_of(handler.put(text=f"id view probe {i}\ndetail {i}").body) for i in range(5)
    ]
    body = handler.search(q="id view probe", id_min=ids[3], page_size=100).body
    assert f"gripe {ids[4]}" in body or f"gr{ids[4]}" in body
    assert f"gripe {ids[0]}" not in body and f"gr{ids[0]}" not in body


def test_id_range_validation(handler: GripeHandler) -> None:
    _seed(handler)
    with pytest.raises(BadInput, match="greater than"):
        handler.search(id_min=10, id_max=5)
    with pytest.raises(BadInput, match="integer"):
        handler.search(id_min="abc")


def test_id_range_refused_on_link_search(handler: GripeHandler) -> None:
    ids = _seed(handler)
    with pytest.raises(BadInput, match="link"):
        handler.search(link=f"gripe:{ids[0]}", id_min=ids[0])


def test_id_range_through_dispatch(runtime_with_store: PrecisRuntime, hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    ids = _seed(handler)
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "gripe",
            "view": "ids",
            "tags": ["STATUS:open"],
            "page_size": 100,
            "__extras__": {"id_min": ids[2], "id_max": ids[4]},
        },
    )
    lines = [ln for ln in body.splitlines() if ln.startswith("gr")]
    assert set(lines) == {f"gr{gid}" for gid in ids[2:]}
    assert "[error" not in body


def test_list_view_carries_ranked_tag_chips(handler: GripeHandler) -> None:
    """gr520321: a gripe's tags render as rank-ordered chips on
    ``view='list'`` rows; the context-restating ``STATUS:open`` drops on
    this open-by-default kind."""
    gid = int(insert_gripe(handler.store, "chips probe gripe\nbody").id)
    handler.store.add_tag(gid, Tag.closed("PRIO", "high"), set_by="system")

    list_body = handler.search(tags=["STATUS:open"], view="list", page_size=100).body
    line = next(ln for ln in list_body.splitlines() if ln.startswith(f"gr{gid}"))
    assert "[PRIO:high]" in line
    assert "STATUS" not in line


# ── get(id=[...], view='body'): the batch body read ──────────────────


def test_batch_get_view_body_is_title_and_body_only(
    handler: GripeHandler,
) -> None:
    first = id_of(handler.put(text="batch body one\none detail").body)
    second = id_of(handler.put(text="batch body two\ntwo detail").body)
    handler.put(id=second, text="a comment on two")
    body = handler.get(id=[first, second], view="body").body
    assert body.startswith("# 2 gripe (batch, view='body')")
    assert "batch body one\none detail" in body
    assert "batch body two\ntwo detail" in body
    # the batch body read carries no comment timeline
    assert "## comment" not in body and "a comment on two" not in body
