"""Real-PG tests for the graph focus page (web-graph-navigation slice 3).

``GET /graph/<kind>/<id>`` renders the depth-2 neighbourhood as a
server-side radial SVG (``precis_web/graph_svg.py``): focus centre, rings
by hop, kinds as node shapes, rels as edge labels, node click = a plain
link to that node's own focus page. Pinned here: valid SVG for the
linked / unlinked / over-cap cases, one labelled edge per row, count
chips past the cap, the cross-links both ways, escaping (no ``<script>``
through a hostile title) and 404/400 errors.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

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


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Paper {slug}", provider="manual", meta={}
    ).id


def _memory(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _quest(store: Store, title: str) -> int:
    return store.insert_ref(kind="quest", slug=None, title=title).id


@pytest.fixture
def fixture_graph(store: Store) -> dict[str, int]:
    """Focus paper with hop-1 links both ways and one hop-2 leaf off the
    served memory — 6 nodes, 6 edges at depth 2."""
    focus = _paper(store, "focus2020")
    cited = _paper(store, "cited2019")
    citer = _paper(store, "citer2021")
    note = _memory(store, "a note")
    goal = _memory(store, "a goal")
    leaf = _memory(store, "leaf")
    store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=focus, relation="cites")
    store.add_link(src_ref_id=note, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=focus, dst_ref_id=goal, relation="serves")
    store.add_link(src_ref_id=goal, dst_ref_id=leaf, relation="related-to")
    return {
        "focus": focus,
        "cited": cited,
        "citer": citer,
        "note": note,
        "goal": goal,
        "leaf": leaf,
    }


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.format_handle(kind, ref_id)


def _svg_of(html_text: str) -> ET.Element:
    """The inlined ``<svg>…</svg>`` block, parsed — the validity check."""
    m = re.search(r"<svg.*?</svg>", html_text, re.DOTALL)
    assert m, "no <svg> block in the page"
    return ET.fromstring(m.group(0))


def test_focus_page_renders_linked_neighbourhood(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    root = _svg_of(r.text)

    # exactly N nodes (hop <= 2): every node's focus-page link appears
    # once for its shape and once for its label — the set of hrefs is the
    # node set.
    hrefs = {
        a.get("href")
        for a in root.iter()
        if a.tag.rsplit("}", 1)[-1] == "a" and a.get("href")
    }
    expected = {f"/graph/paper/{fixture_graph[k]}" for k in ("cited", "citer")} | {
        f"/graph/memory/{fixture_graph[k]}" for k in ("note", "goal", "leaf")
    }
    assert hrefs == expected

    # one labelled edge per neighbourhood row: 6 edges, every rel named
    # on its own <text>.
    texts = {el.text for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "text"}
    assert {"cites", "cited-by", "related-to", "serves"} <= texts
    edge_paths = [
        el
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "path" and el.get("marker-end")
    ]
    assert len(edge_paths) == 5

    # focus node is labelled; header cross-links go both ways
    assert _h("paper", focus) in r.text
    assert (
        f'href="/refs/paper/{focus}"' in r.text or f'href="/papers/{focus}"' in r.text
    )
    assert f'href="/graph/paper/{focus}.json"' in r.text
    assert 'href="/eye/' in r.text


def test_focus_page_node_with_no_links(graph_client: TestClient, store: Store) -> None:
    mid = _memory(store, "an island")
    r = graph_client.get(f"/graph/memory/{mid}")
    assert r.status_code == 200
    _svg_of(r.text)
    assert "no typed links" in r.text
    assert _h("memory", mid) in r.text


def test_focus_page_collapses_over_cap_to_chips(
    graph_client: TestClient, store: Store
) -> None:
    """Hop 1 over the cap → a "+N more" chip on ring 1; hop 2 counts-only
    → one "+N <kind>" chip per kind on ring 2."""
    focus = _paper(store, "hub2020")
    for i in range(6):
        leaf = _memory(store, f"hop1 leaf {i}")
        store.add_link(src_ref_id=focus, dst_ref_id=leaf, relation="related-to")
    # a hop-1 bridge carrying 6 hop-2 memories
    bridge = _memory(store, "bridge")
    store.add_link(src_ref_id=focus, dst_ref_id=bridge, relation="serves")
    for i in range(6):
        far = _memory(store, f"hop2 leaf {i}")
        store.add_link(src_ref_id=bridge, dst_ref_id=far, relation="related-to")

    r = graph_client.get(f"/graph/paper/{focus}?cap=4")
    assert r.status_code == 200
    root = _svg_of(r.text)
    # 4 hop-1 nodes under the cap, 0 hop-2 nodes (counts-only), chips stand in
    hrefs = {
        a.get("href")
        for a in root.iter()
        if a.tag.rsplit("}", 1)[-1] == "a" and a.get("href")
    }
    assert len(hrefs) == 4
    assert "+3 more" in r.text  # 7 hop-1 edges, 4 shown
    assert "+6 memory" in r.text  # the collapsed hop-2 ring
    assert "over cap" in r.text


def test_focus_page_escapes_labels(graph_client: TestClient, store: Store) -> None:
    focus = _memory(store, 'a"><script>alert(1)</script> memory')
    leaf = _memory(store, '"><img src=x onerror=alert(1)>')
    store.add_link(src_ref_id=focus, dst_ref_id=leaf, relation="related-to")
    r = graph_client.get(f"/graph/memory/{focus}")
    assert r.status_code == 200
    _svg_of(r.text)  # hostile titles must not break the markup
    m = re.search(r"<svg.*?</svg>", r.text, re.DOTALL)
    assert m is not None
    svg = m.group(0)
    # the payload survives only as escaped text, never as markup
    assert "<script>" not in svg and "<img" not in svg
    assert "&lt;script&gt;" in svg or "alert(1)" in svg
    assert 'href="http' not in svg and "xlink:href" not in svg


def test_focus_page_errors(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    r = graph_client.get("/graph/paper/999999999")
    assert r.status_code == 404 and "no paper ref" in r.text
    focus = fixture_graph["focus"]
    assert graph_client.get(f"/graph/memory/{focus}").status_code == 404
    r = graph_client.get(f"/graph/paper/{focus}?cap=0")
    assert r.status_code == 400 and "cap" in r.text
    assert graph_client.get(f"/graph/paper/{focus}?cap=201").status_code == 400


def test_panel_links_to_focus_page(graph_client: TestClient, store: Store) -> None:
    """The cross-link both ways: the ref detail's panel points at
    ``/graph/<kind>/<id>`` and each row's ◦ link re-focuses the graph."""
    mid = _memory(store, "centre")
    leaf = _memory(store, "leaf")
    store.add_link(src_ref_id=mid, dst_ref_id=leaf, relation="related-to")
    r = graph_client.get(f"/refs/memory/{mid}")
    assert f'href="/graph/memory/{mid}"' in r.text
    panel = graph_client.get(f"/graph/memory/{mid}/panel")
    assert f'href="/graph/memory/{leaf}"' in panel.text
