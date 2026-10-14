"""Real-PG tests for the ``/graph`` filter axes (web-graph-navigation
slice 4).

One :class:`_Filters` parse feeds the same ``Store.neighbourhood`` call
on all three surfaces — ``GET /graph/<kind>/<id>.json``, the ``/panel``
HTMX fragment and the ``/graph/<kind>/<id>`` focus page — so a filter set
narrows nodes and edges identically everywhere. Pinned here: each axis
narrows on every surface, node/edge parity JSON ↔ panel ↔ page, the
filter controls + chips, the empty state (a ``since`` after every link's
creation) and the 400s. ``trust`` exercises the single-sourced tier
predicate (``nanopub.overview.passes_trust``): a bare finding has no
publish posture, so it fails every tier but ``any``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
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


@pytest.fixture
def fixture_graph(store: Store) -> dict[str, int]:
    """Focus paper with hop-1 links both ways across four rels (plus a
    posture-less ``finding`` for the trust axis) and one hop-2 leaf."""
    focus = _paper(store, "focus2020")
    cited = _paper(store, "cited2019")
    citer = _paper(store, "citer2021")
    note = _memory(store, "a note")
    goal = _memory(store, "a goal")
    old = _memory(store, "an old note")
    leaf = _memory(store, "leaf")
    fin = store.insert_ref(kind="finding", slug=None, title="a claim").id
    store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=focus, relation="cites")
    store.add_link(src_ref_id=note, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=focus, dst_ref_id=goal, relation="serves")
    store.add_link(src_ref_id=focus, dst_ref_id=old, relation="supersedes")
    store.add_link(src_ref_id=focus, dst_ref_id=fin, relation="related-to")
    store.add_link(src_ref_id=goal, dst_ref_id=leaf, relation="related-to")
    return {
        "focus": focus,
        "cited": cited,
        "citer": citer,
        "note": note,
        "goal": goal,
        "old": old,
        "leaf": leaf,
        "fin": fin,
    }


_KIND_OF = {
    "cited": "paper",
    "citer": "paper",
    "note": "memory",
    "goal": "memory",
    "old": "memory",
    "leaf": "memory",
    "fin": "finding",
}
_ALL_HOP1 = ("cited", "citer", "note", "goal", "old", "fin")


def _node_set(fixture_graph: dict[str, int], keys: tuple[str, ...]) -> set:
    return {(_KIND_OF[k], fixture_graph[k]) for k in keys}


def _svg_block(html_text: str) -> str:
    m = re.search(r"<svg.*?</svg>", html_text, re.DOTALL)
    assert m, "no <svg> block in the page"
    return m.group(0)


def _page_nodes(html_text: str) -> set[tuple[str, int]]:
    """The node set the SVG draws — every node's re-focus link."""
    return {
        (k, int(i))
        for k, i in re.findall(r'href="/graph/([a-z]+)/(\d+)"', _svg_block(html_text))
    }


def _page_edges(html_text: str) -> int:
    root = ET.fromstring(_svg_block(html_text))
    return sum(
        1
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "path" and el.get("marker-end")
    )


def _panel_nodes(body: str, focus_id: int) -> set[tuple[str, int]]:
    """The hop-1 node set a panel renders — one ◦ re-focus link per row.
    Chip hrefs carry a ``?`` and never match; the form ``action`` is the
    focus's own page and is excluded by id."""
    return {
        (k, int(i))
        for k, i in re.findall(r'href="/graph/([a-z]+)/(\d+)"', body)
        if int(i) != focus_id
    }


def _panel_edges(body: str) -> int:
    return body.count('title="outbound"') + body.count('title="inbound"')


@pytest.mark.parametrize(
    "params, exp_keys",
    [
        ("", _ALL_HOP1),
        ("rels=related-to", ("note", "fin")),
        ("rels=cites", ("cited",)),
        ("rels=cited-by", ("citer",)),
        ("kinds=paper", ("cited", "citer")),
        ("kinds=paper,finding", ("cited", "citer", "fin")),
        ("trust=any", _ALL_HOP1),
        ("trust=verified", ("cited", "citer", "note", "goal", "old")),
        ("until=2999-01-01", _ALL_HOP1),
        ("kinds=paper,memory&rels=cites,cited-by", ("cited", "citer")),
    ],
)
def test_filter_narrows_identically_on_all_three_surfaces(
    graph_client: TestClient,
    fixture_graph: dict[str, int],
    params: str,
    exp_keys: tuple[str, ...],
) -> None:
    """The slice-4 acceptance: the same query string narrows the JSON, the
    panel and the page (at ``depth=1``) to the same node set and edge
    count."""
    focus = fixture_graph["focus"]
    exp_nodes = _node_set(fixture_graph, exp_keys)
    exp_edges = len(exp_keys)  # one edge per neighbour in this fixture

    body = graph_client.get(f"/graph/paper/{focus}.json?{params}").json()
    assert {(n["kind"], n["id"]) for n in body["nodes"]} == exp_nodes
    assert len(body["edges"]) == exp_edges

    panel = graph_client.get(
        f"/graph/paper/{focus}/panel?memories=1" + (f"&{params}" if params else "")
    )
    assert panel.status_code == 200
    assert _panel_nodes(panel.text, focus) == exp_nodes
    assert _panel_edges(panel.text) == exp_edges

    page = graph_client.get(f"/graph/paper/{focus}?depth=1&{params}")
    assert page.status_code == 200
    assert _page_nodes(page.text) == exp_nodes
    assert _page_edges(page.text) == exp_edges


def test_depth2_parity_json_and_page(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    """At ``depth=2`` the page and the JSON carry the same hop-2 nodes and
    edges (the panel is hop-1 by contract — no depth axis)."""
    focus = fixture_graph["focus"]
    body = graph_client.get(f"/graph/paper/{focus}.json?depth=2&kinds=memory").json()
    exp = _node_set(fixture_graph, ("note", "goal", "old", "leaf"))
    assert {(n["kind"], n["id"]) for n in body["nodes"]} == exp
    assert len(body["edges"]) == 4  # 3 hop-1 + 1 hop-2

    page = graph_client.get(f"/graph/paper/{focus}?depth=2&kinds=memory")
    assert _page_nodes(page.text) == exp
    assert _page_edges(page.text) == 4


def test_since_after_everything_gives_the_empty_state(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    """``since`` past every linked ref's creation: empty on all three
    surfaces, and the panel/page say so rather than rendering blank."""
    focus = fixture_graph["focus"]
    body = graph_client.get(f"/graph/paper/{focus}.json?since=2999-01-01").json()
    assert body["nodes"] == [] and body["edges"] == [] and body["counts"] == {}

    panel = graph_client.get(f"/graph/paper/{focus}/panel?since=2999-01-01&memories=1")
    assert panel.status_code == 200
    assert "no links match the active filters" in panel.text

    page = graph_client.get(f"/graph/paper/{focus}?since=2999-01-01")
    assert "no links match the active filters" in page.text
    assert "since:2999-01-01" in page.text  # the chip reflects the state
    _svg_block(page.text)  # the SVG still renders — the focus stands alone


def test_filter_controls_and_chips(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    """The page carries a plain GET form + a chip per active value; the
    panel carries the same axes in its HTMX form and chips that refetch
    the fragment (hx) with the focus page as the no-JS fallback (href)."""
    focus = fixture_graph["focus"]

    page = graph_client.get(f"/graph/paper/{focus}?kinds=paper&trust=verified&depth=1")
    for field in ("kinds", "rels", "since", "until", "trust", "depth"):
        assert f'name="{field}"' in page.text
    assert "kind:paper ×" in page.text and "trust:verified ×" in page.text
    assert "depth:1 ×" in page.text
    assert ">clear ×<" in page.text  # >1 active value → clear-all chip
    # a chip's href is the same URL minus that one value (chips carry
    # title="remove this filter"; the view-toggle link shares the shape)
    chip_hrefs = re.findall(
        r'<a href="([^"]+)"[^>]*title="remove this filter"', page.text
    )
    assert any("trust=verified" in h and "kinds=" not in h for h in chip_hrefs), (
        chip_hrefs
    )
    # the json cross-link carries the active filters
    assert ".json?kinds=paper" in page.text

    panel = graph_client.get(
        f"/graph/paper/{focus}/panel?memories=1&kinds=paper&trust=verified"
    )
    for field in ("kinds", "rels", "since", "until", "trust"):
        assert f'name="{field}"' in panel.text
    assert "kind:paper ×" in panel.text and "trust:verified ×" in panel.text
    chip_hx = re.findall(
        r'<a href="[^"]+" hx-get="([^"]+)"[^>]*title="remove this filter"',
        panel.text,
    )
    assert any(
        "trust=verified" in h and "kinds=" not in h and "memories=1" in h
        for h in chip_hx
    ), chip_hx


def test_filter_errors_all_surfaces(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    # unknown trust tier → the store's BadInput surfaces everywhere
    r = graph_client.get(f"/graph/paper/{focus}.json?trust=bogus")
    assert r.status_code == 400 and "trust" in r.json()["error"]
    assert graph_client.get(f"/graph/paper/{focus}?trust=bogus").status_code == 400
    r = graph_client.get(f"/graph/paper/{focus}/panel?trust=bogus")
    assert r.status_code == 400 and "trust" in r.text
    # unknown kind → 400 (the /eye registry validation), not a silent empty
    assert (
        graph_client.get(f"/graph/paper/{focus}.json?kinds=nosuch").status_code == 400
    )
    assert graph_client.get(f"/graph/paper/{focus}?kinds=nosuch").status_code == 400
    assert (
        graph_client.get(f"/graph/paper/{focus}/panel?kinds=nosuch").status_code == 400
    )
    # depth is the page's axis
    assert graph_client.get(f"/graph/paper/{focus}?depth=3").status_code == 400
    assert graph_client.get(f"/graph/paper/{focus}?depth=1").status_code == 200


def test_group_expand_keeps_the_filters(graph_client: TestClient, store: Store) -> None:
    """A filtered panel's "show all" re-fetch carries the filters, so an
    expanded group stays the narrowed view, not the whole ring."""
    focus = _memory(store, "hub memory")
    for i in range(30):
        leaf = _memory(store, f"leaf {i}")
        store.add_link(src_ref_id=focus, dst_ref_id=leaf, relation="related-to")
    r = graph_client.get(f"/graph/memory/{focus}/panel?kinds=memory")
    m = re.search(r'hx-get="([^"]*group=[^"]*)"', r.text)
    assert m and "kinds=memory" in m.group(1)
    r = graph_client.get(
        f"/graph/memory/{focus}/panel?group=Notes+%26+links&kinds=memory"
    )
    assert r.status_code == 200
    assert _panel_edges(r.text) == 30
