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
from tests._gripe import insert_gripe
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
