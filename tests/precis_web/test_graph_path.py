"""Real-PG tests for graph path finding (web-graph-navigation slice 5).

``GET /graph/path?from=&to=&depth=&rels=`` runs
:meth:`Store.link_path` — a bounded bidirectional BFS over typed links,
walked either way — and renders the shortest path as a chain of ref
cards. Pinned here (AC 5): two refs three links apart are joined at depth
4 and not at depth 2, a cycle in the graph terminates, plus the stored
direction on each arrow, the ``rels`` narrowing, the handle endpoint form
and the 400/404s.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig


@pytest.fixture
def graph_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _memory(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


@pytest.fixture
def chain(store: Store) -> dict[str, int]:
    """a -cites-> b <-related-to- c -serves-> d, three links a..d, plus a
    cycle b -see-also-> e -see-also-> c and a dead end off a."""
    ids = {k: _memory(store, f"node {k}") for k in "abcdex"}
    store.add_link(src_ref_id=ids["a"], dst_ref_id=ids["b"], relation="cites")
    store.add_link(src_ref_id=ids["c"], dst_ref_id=ids["b"], relation="related-to")
    store.add_link(src_ref_id=ids["c"], dst_ref_id=ids["d"], relation="serves")
    store.add_link(src_ref_id=ids["b"], dst_ref_id=ids["e"], relation="see-also")
    store.add_link(src_ref_id=ids["e"], dst_ref_id=ids["c"], relation="see-also")
    store.add_link(src_ref_id=ids["a"], dst_ref_id=ids["x"], relation="related-to")
    return ids


def _h(ref_id: int) -> str:
    return handle_registry.format_handle("memory", ref_id)


def test_store_path_is_shortest_with_stored_direction(
    store: Store, chain: dict[str, int]
) -> None:
    got = store.link_path(chain["a"], chain["d"], depth=4)
    assert got["ids"] == [chain["a"], chain["b"], chain["c"], chain["d"]]
    assert got["links"] == [
        (chain["a"], chain["b"], "cites"),
        (chain["c"], chain["b"], "related-to"),
        (chain["c"], chain["d"], "serves"),
    ]
    assert not got["budget_hit"]
    # symmetric: the reverse query walks the same links back
    back = store.link_path(chain["d"], chain["a"], depth=4)
    assert back["ids"] == got["ids"][::-1]


def test_store_path_depth_bound_and_cycle_terminates(
    store: Store, chain: dict[str, int]
) -> None:
    assert store.link_path(chain["a"], chain["d"], depth=2)["ids"] is None
    island = _memory(store, "island")
    # the b-c-e cycle is fully explored at depth 4 and the search ends
    got = store.link_path(chain["a"], island, depth=4)
    assert got == {"ids": None, "links": [], "budget_hit": False}


def test_store_path_rels_admit_either_slug_of_a_pair(
    store: Store, chain: dict[str, int]
) -> None:
    # without serves the only route to d is gone
    assert (
        store.link_path(
            chain["a"], chain["d"], depth=4, rels=["cites", "related-to", "see-also"]
        )["ids"]
        is None
    )
    # the inverse slug selects the stored row
    got = store.link_path(chain["a"], chain["b"], depth=1, rels=["cited-by"])
    assert got["ids"] == [chain["a"], chain["b"]]


def test_path_page_found_at_depth_4(
    graph_client: TestClient, chain: dict[str, int]
) -> None:
    r = graph_client.get(
        f"/graph/path?from=memory/{chain['a']}&to=memory/{chain['d']}&depth=4"
    )
    assert r.status_code == 200
    assert "3 links from" in r.text
    order = re.findall(r'data-handle="([^"]+)"', r.text)
    assert order == [_h(chain[k]) for k in "abcd"]
    # arrows in stored direction: a->b down, c->b up, c->d down
    arrows = re.findall(r'aria-label="link"[^>]*>\s*([↓↑]) ([a-z-]+)', r.text)
    assert arrows == [("↓", "cites"), ("↑", "related-to"), ("↓", "serves")]
    # each card re-focuses and reads
    assert f'href="/graph/memory/{chain["c"]}"' in r.text


def test_path_page_not_found_at_depth_2_is_an_answer(
    graph_client: TestClient, chain: dict[str, int]
) -> None:
    r = graph_client.get(
        f"/graph/path?from={_h(chain['a'])}&to={_h(chain['d'])}&depth=2"
    )
    assert r.status_code == 200
    assert "No path within depth 2" in r.text


def test_path_page_empty_form_and_errors(
    graph_client: TestClient, chain: dict[str, int]
) -> None:
    r = graph_client.get("/graph/path")
    assert r.status_code == 200
    assert 'name="from"' in r.text and "No path" not in r.text
    assert (
        graph_client.get(f"/graph/path?from=nonsense&to=memory/{chain['a']}")
    ).status_code == 400
    assert (
        graph_client.get(
            f"/graph/path?from=memory/{chain['a']}&to=memory/{chain['b']}&depth=9"
        )
    ).status_code == 400
    assert (
        graph_client.get(f"/graph/path?from=memory/{chain['a']}&to=memory/999999999")
    ).status_code == 404


def test_focus_page_links_the_path_form(
    graph_client: TestClient, chain: dict[str, int]
) -> None:
    r = graph_client.get(f"/graph/memory/{chain['a']}")
    assert 'action="/graph/path"' in r.text
    assert f'value="memory/{chain["a"]}"' in r.text
