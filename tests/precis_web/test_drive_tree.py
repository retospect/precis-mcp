"""gr664408 — Drive ``layout=tree&rel=<rel>``.

``build_tree`` nests the filtered rows under a named edge source
(``drive_tree._TREE_EDGES``: the ``serves`` links relation,
``refs.parent_id``); the template renders the result as nested
``<ul class="drive-tree">`` lists, ``data-depth`` marking each level.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.drive_tree import build_tree


def _row(rid: int) -> dict[str, Any]:
    return {"id": rid, "kind": "quest", "title": f"quest {rid}"}


def _depth_map(nodes: list[dict[str, Any]], depth: int = 0) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for n in nodes:
        out.setdefault(n["row"]["id"], []).append(depth)
        for rid, ds in _depth_map(n["children"], depth + 1).items():
            out.setdefault(rid, []).extend(ds)
    return out


def test_build_tree_dag_and_cycle() -> None:
    rows = [_row(1), _row(2), _row(3), _row(4)]
    # 2 and 3 serve 1; 4 serves both — a DAG child lands under *every*
    # in-set parent.
    forest = build_tree(rows, {2: [1], 3: [1], 4: [2, 3]})
    assert [n["row"]["id"] for n in forest] == [1]
    kids = {n["row"]["id"]: n for n in forest[0]["children"]}
    assert sorted(kids) == [2, 3]
    assert [c["row"]["id"] for c in kids[2]["children"]] == [4]
    assert [c["row"]["id"] for c in kids[3]["children"]] == [4]
    assert _depth_map(forest) == {1: [0], 2: [1], 3: [1], 4: [2, 2]}

    # A 2-cycle: no root, so both promote — finite nesting, never a runaway.
    forest = build_tree(rows[:2], {1: [2], 2: [1]})
    depths = _depth_map(forest)
    assert sorted(depths) == [1, 2]
    assert max(d for ds in depths.values() for d in ds) <= 1

    # A parent the filter cut is simply absent — 2's parent (9) is
    # off-page, so every row is a root.
    forest = build_tree(rows[:3], {2: [9]})
    assert [n["row"]["id"] for n in forest] == [1, 2, 3]


class _TreeDepth(HTMLParser):
    """Row ``open_url`` hrefs → their depth inside ``ul.drive-tree``
    nesting (root rows sit in the depth-0 ``<ul>``, a child inside one
    more nested ``<ul>`` per level). ``href_re`` names the row links to
    record (e.g. ``/todo/\\d+``, ``/refs/quest/\\d+``)."""

    def __init__(self, href_re: str) -> None:
        super().__init__()
        self._uls: list[bool] = []
        self.depths: dict[str, int] = {}
        self._re = re.compile(href_re)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "ul":
            self._uls.append("drive-tree" in (a.get("class") or "").split())
        elif tag == "a":
            href = a.get("href") or ""
            if self._re.fullmatch(href) and href not in self.depths:
                self.depths[href] = self._uls.count(True) - 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" and self._uls:
            self._uls.pop()


@pytest.fixture
def drive_client(runtime_with_store: Any, tmp_path: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_todo_parent_edges(drive_client: TestClient, store: Any) -> None:
    """Three todos a>b>c nest at depths 0,1,2 — the ``parent_id``
    hierarchy the flat ``k=todo`` list shows only as rows."""
    a = store.insert_ref(kind="todo", slug=None, title="triage root")
    b = store.insert_ref(kind="todo", slug=None, title="middle slice", parent_id=a.id)
    c = store.insert_ref(kind="todo", slug=None, title="leaf build", parent_id=b.id)
    resp = drive_client.get("/drive?k=todo&layout=tree&rel=parent")
    assert resp.status_code == 200
    p = _TreeDepth(r"/todo/\d+")
    p.feed(resp.text)
    assert p.depths[f"/todo/{a.id}"] == 0
    assert p.depths[f"/todo/{b.id}"] == 1
    assert p.depths[f"/todo/{c.id}"] == 2


def test_quest_serves_tree_nests_under_the_served_quest(
    drive_client: TestClient, store: Any
) -> None:
    """``rel=serves``: a quest hangs under each live quest it serves —
    the ``/refs/quest`` forest's edge direction, served by Drive."""
    parent = store.insert_ref(kind="quest", slug=None, title="top quest")
    child = store.insert_ref(kind="quest", slug=None, title="serving quest")
    store.add_link(
        src_ref_id=child.id, dst_ref_id=parent.id, relation="serves", set_by="system"
    )
    resp = drive_client.get("/drive?k=quest&layout=tree&rel=serves")
    assert resp.status_code == 200
    p = _TreeDepth(r"/refs/quest/\d+")
    p.feed(resp.text)
    assert p.depths[f"/refs/quest/{parent.id}"] == 0
    assert p.depths[f"/refs/quest/{child.id}"] == 1


def test_unknown_rel_400s_naming_valid_rels(drive_client: TestClient) -> None:
    resp = drive_client.get("/drive?k=todo&layout=tree&rel=bogus")
    assert resp.status_code == 400
    assert "parent" in resp.text and "serves" in resp.text
    # …and a rel with no tree layout is inert, not an error.
    assert drive_client.get("/drive?k=todo&rel=bogus").status_code == 200
    # An unknown layout 400s too.
    assert drive_client.get("/drive?k=todo&layout=bogus").status_code == 400


def test_sidebar_links_the_todo_tree_preset(drive_client: TestClient) -> None:
    resp = drive_client.get("/drive")
    assert resp.status_code == 200
    assert "/drive?k=todo&amp;layout=tree&amp;rel=parent" in resp.text


def test_tree_pagination_keeps_descendants_with_their_root(
    drive_client: TestClient, store: Any
) -> None:
    """Root pagination (review td707116): ``page`` steps through the
    forest's top level, not the flat rows. R's child is the newest row,
    so flat-row paging put it on page 1 with R stranded on page 2 —
    promoting the child to a root and splitting the branch. Now the
    child rides with R on page 2, and page 1 is 100 real roots."""
    root = store.insert_ref(kind="todo", slug=None, title="root")
    fillers = [
        store.insert_ref(kind="todo", slug=None, title=f"filler {i}")
        for i in range(100)
    ]
    child = store.insert_ref(kind="todo", slug=None, title="child", parent_id=root.id)
    # 102 rows, 101 top-level nodes (the child nests under root).
    resp = drive_client.get("/drive?k=todo&layout=tree&rel=parent")
    assert resp.status_code == 200
    assert "Showing 100 of 101 top-level items." in resp.text
    assert "Page 1 of 2" in resp.text
    p = _TreeDepth(r"/todo/\d+")
    p.feed(resp.text)
    assert sum(1 for d in p.depths.values() if d == 0) == 100
    # The branch does not split: neither the child (nested) nor its root
    # (page 2's only root) renders on page 1.
    assert f"/todo/{child.id}" not in p.depths
    assert f"/todo/{root.id}" not in p.depths

    resp2 = drive_client.get("/drive?k=todo&layout=tree&rel=parent&page=2")
    assert resp2.status_code == 200
    assert "Showing 1 of 101 top-level items." in resp2.text
    assert "Page 2 of 2" in resp2.text
    p2 = _TreeDepth(r"/todo/\d+")
    p2.feed(resp2.text)
    assert p2.depths[f"/todo/{root.id}"] == 0
    assert p2.depths[f"/todo/{child.id}"] == 1
    # A filler root belongs to exactly one page.
    assert f"/todo/{fillers[0].id}" in p2.depths or f"/todo/{fillers[0].id}" in p.depths


def test_search_tree_nests_matches_under_their_in_set_parent(
    drive_client: TestClient, store: Any
) -> None:
    """``q`` + tree: the search path fetches its result set whole
    before nesting, so a matching child
    hangs under its matching parent instead of paging apart."""
    from precis.store import ChunkInsert

    parent = store.insert_ref(
        kind="conv", slug="thread/parent", title="alpha parent", meta={}
    )
    child = store.insert_ref(
        kind="conv",
        slug="thread/child",
        title="alpha child",
        meta={},
        parent_id=parent.id,
    )
    store.chunks.insert_chunks(
        parent.id, [ChunkInsert(ord=0, text="alpha parent body")]
    )
    store.chunks.insert_chunks(child.id, [ChunkInsert(ord=0, text="alpha child body")])
    resp = drive_client.get("/drive?q=alpha&k=conv&layout=tree&rel=parent")
    assert resp.status_code == 200
    p = _TreeDepth(r"/refs/conv/\d+")
    p.feed(resp.text)
    assert p.depths[f"/refs/conv/{parent.id}"] == 0
    assert p.depths[f"/refs/conv/{child.id}"] == 1
    assert "top-level match" in resp.text


@pytest.mark.parametrize(
    "page, size, total, next_page", [(2, 100, 201, True), (3, 1, 201, False)]
)
def test_doable_tree_preserves_queue_page(
    drive_client: TestClient,
    store: Any,
    page: int,
    size: int,
    total: int,
    next_page: bool,
) -> None:
    """Queue leaves are paginated once; page two keeps rows and its probe."""
    for i in range(201):
        store.insert_ref(kind="todo", slug=None, title=f"queue leaf {i}")
    flat = drive_client.get(f"/drive?k=todo&queue=doable&page={page}")
    resp = drive_client.get(
        f"/drive?k=todo&queue=doable&layout=tree&rel=parent&page={page}"
    )
    assert flat.status_code == resp.status_code == 200
    p = _TreeDepth(r"/todo/\d+")
    p.feed(resp.text)
    assert len(p.depths) == size
    assert set(p.depths.values()) == {0}
    assert f"Showing {size} of {total} top-level items." in resp.text
    for href in p.depths:
        assert f'href="{href}"' in flat.text
    assert (f"page={page + 1}" in resp.text) == next_page


def test_search_tree_keeps_semantic_parent_beyond_lexical_cap(
    drive_client: TestClient, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A semantic-only parent after hit 100 must not strand its child."""
    from precis.store import ChunkInsert

    parent = store.insert_ref(
        kind="conv", slug="semantic/parent", title="distant parent"
    )
    child = store.insert_ref(
        kind="conv", slug="semantic/child", title="alpha child", parent_id=parent.id
    )
    fillers = [
        store.insert_ref(kind="conv", slug=f"semantic/filler-{i}", title=f"filler {i}")
        for i in range(100)
    ]
    store.chunks.insert_chunks(child.id, [ChunkInsert(ord=0, text="alpha child body")])
    # Simulate the fused ranking: one lexical child, then semantic-only
    # matches, with the parent beyond the former max(lexical_count, 100).
    ranked = [child, *fillers, parent]
    calls: list[tuple[int, int]] = []

    def fused(**kwargs: Any) -> list[tuple[None, Any, float]]:
        limit, offset = kwargs["limit"], kwargs["offset"]
        calls.append((limit, offset))
        return [(None, ref, 1.0) for ref in ranked[offset : offset + limit]]

    monkeypatch.setattr(store.chunks, "search_chunks_across_kinds", fused)
    pages = [
        drive_client.get(f"/drive?q=alpha&k=conv&layout=tree&rel=parent&page={page}")
        for page in (1, 2)
    ]
    assert all(resp.status_code == 200 for resp in pages)
    assert all(limit >= len(ranked) and offset == 0 for limit, offset in calls)
    parsers = [_TreeDepth(r"/refs/conv/\d+") for _ in pages]
    for parser, resp in zip(parsers, pages, strict=True):
        parser.feed(resp.text)
    assert f"/refs/conv/{child.id}" not in parsers[0].depths
    assert parsers[1].depths[f"/refs/conv/{parent.id}"] == 0
    assert parsers[1].depths[f"/refs/conv/{child.id}"] == 1
    assert "Showing 1 of ≈101 top-level matches." in pages[1].text
