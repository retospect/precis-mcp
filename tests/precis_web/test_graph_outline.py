"""Real-PG tests for the ``?view=outline`` presentation of
``GET /graph/<kind>/<id>`` (gr486410).

The outline is the same neighbourhood read as the radial SVG — same
``_Filters`` axes, ``depth=2`` — rendered as a copyable bullet list:
focus header, hop-1 rows under the panel's ring-group headings, hop-2
nested under its anchor (never back at the focus), retired endpoints as
tombstones (``include_retired``). Pinned: node-set parity with the SVG
view under the same filters (the outline's non-tombstone rows), nesting,
tombstones, escaping and the ``view=`` validation.
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


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Paper {slug}", provider="manual", meta={}
    ).id


def _memory(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.format_handle(kind, ref_id)


@pytest.fixture
def fixture_graph(store: Store) -> dict[str, int]:
    """The focus-page fixture: hop-1 links both ways, a hop-2 leaf off the
    goal, and a second hop-2 path through the citer."""
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
    store.add_link(src_ref_id=citer, dst_ref_id=leaf, relation="related-to")
    return {
        "focus": focus,
        "cited": cited,
        "citer": citer,
        "note": note,
        "goal": goal,
        "leaf": leaf,
    }


def _svg_nodes(html_text: str) -> set[tuple[str, int]]:
    """The node set the radial view draws (every node's re-focus link)."""
    m = re.search(r"<svg.*?</svg>", html_text, re.DOTALL)
    assert m, "no <svg> block in the page"
    return {
        (k, int(i)) for k, i in re.findall(r'href="/graph/([a-z]+)/(\d+)"', m.group(0))
    }


def _outline_rows(html_text: str) -> list[tuple[str, bool]]:
    """Every row the outline renders, in order — ``(handle, retired)``."""
    return [
        (h, bool(ret))
        for h, ret in re.findall(r'data-node="([^"]+)"( data-retired)?', html_text)
    ]


def _outline_node_set(html_text: str, *, retired: bool = False) -> set:
    """Row handles → (kind, id), split on the tombstone marker."""
    out = set()
    for h, ret in _outline_rows(html_text):
        if ret == retired:
            parsed = handle_registry.parse(h)
            assert parsed is not None
            out.add((parsed[0], parsed[2]))
    return out


@pytest.mark.parametrize(
    "params",
    ["", "kinds=paper", "kinds=memory", "rels=related-to", "until=2999-01-01"],
)
def test_outline_node_set_parity_with_svg(
    graph_client: TestClient, fixture_graph: dict[str, int], params: str
) -> None:
    """Same filters → the outline's live rows are exactly the SVG's node
    set (the SVG drops tombstones; parity is over the live set)."""
    focus = fixture_graph["focus"]
    svg = graph_client.get(f"/graph/paper/{focus}?{params}")
    out = graph_client.get(f"/graph/paper/{focus}?view=outline&{params}")
    assert svg.status_code == out.status_code == 200
    assert 'aria-label="link neighbourhood"' not in out.text
    assert _outline_node_set(out.text) == _svg_nodes(svg.text)


def test_outline_shape(graph_client: TestClient, fixture_graph: dict[str, int]) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}?view=outline")
    body = r.text
    # focus header: handle, title, kind
    assert _h("paper", focus) in body and "Paper focus2020" in body
    # the panel's ring-group grouping with counts
    assert "Roadmap" in body and "Notes &amp; links" in body
    assert "serves ×1" in body and "cited-by ×1" in body
    # rows: handle → reader, ◦ → its own focus page
    assert f'href="/papers/{fixture_graph["cited"]}"' in body
    assert f'href="/graph/paper/{fixture_graph["cited"]}"' in body
    # the filter form keeps the view; the toggle back names radial
    assert 'name="view" value="outline"' in body
    assert f'href="/graph/paper/{focus}?view=radial"' in body
    radial = graph_client.get(f"/graph/paper/{focus}")
    assert f'href="/graph/paper/{focus}?view=outline"' in radial.text


def test_outline_nests_hop2_under_its_anchor(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    """The leaf is reached via ``citer`` and ``goal`` — it nests once,
    under the first anchor by store order (newest link first: ``citer``),
    and never as a top-level row."""
    focus = fixture_graph["focus"]
    body = graph_client.get(f"/graph/paper/{focus}?view=outline").text
    citer_h, leaf_h = (
        _h("paper", fixture_graph["citer"]),
        _h("memory", fixture_graph["leaf"]),
    )
    rows = _outline_rows(body)
    assert [h for h, _ in rows].count(leaf_h) == 1
    # nested inside a hop-1 row's kid list, not at the top level
    m = re.search(
        rf'data-node="{citer_h}".*?<ul class="ml-7[^"]*">.*?data-node="{leaf_h}"',
        body,
        re.DOTALL,
    )
    assert m, "leaf not nested under its hop-1 anchor"
    # its kid row carries the hop-2 edge's rel
    assert re.search(rf'data-node="{leaf_h}".*?related-to', body, re.DOTALL)


def test_outline_tombstones(
    graph_client: TestClient, fixture_graph: dict[str, int], store: Store
) -> None:
    """A retired neighbour renders as a struck, marked tombstone in the
    outline — and stays absent from the SVG; the JSON returns it as a
    tombstone (the liaison contract, td470556)."""
    focus = fixture_graph["focus"]
    store.retire_ref(fixture_graph["cited"])

    body = graph_client.get(f"/graph/paper/{focus}?view=outline").text
    rows = _outline_rows(body)
    assert (_h("paper", fixture_graph["cited"]), True) in rows
    assert "line-through" in body and "· retired" in body
    # tombstones are outline-only: the same URL minus the view drops them
    assert _outline_node_set(body) == _svg_nodes(
        graph_client.get(f"/graph/paper/{focus}").text
    )
    assert {
        n["id"]: n.get("retired", False)
        for n in graph_client.get(f"/graph/paper/{focus}.json").json()["nodes"]
    }[fixture_graph["cited"]] is True


def test_outline_escapes_labels(graph_client: TestClient, store: Store) -> None:
    focus = _memory(store, "plain focus")
    leaf = _memory(store, '"><img src=x onerror=alert(1)>')
    store.add_link(src_ref_id=focus, dst_ref_id=leaf, relation="related-to")
    r = graph_client.get(f"/graph/memory/{focus}?view=outline")
    assert r.status_code == 200
    # the payload survives only as escaped text (the page chrome itself
    # legitimately contains <script> tags, so pin the payload, not the tag)
    assert '"><img src=x' not in r.text
    assert "&lt;img" in r.text


def test_view_validation_and_errors(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}?view=bogus")
    assert r.status_code == 400 and "view" in r.text
    # filters still apply — a since past everything gives the empty state
    r = graph_client.get(f"/graph/paper/{focus}?view=outline&since=2999-01-01")
    assert "no links match the active filters" in r.text
    assert _outline_rows(r.text) == []


def test_outline_retired_focus(graph_client: TestClient, store: Store) -> None:
    """A retired focus still renders — its own header is the tombstone."""
    mid = _memory(store, "gone")
    store.retire_ref(mid)
    r = graph_client.get(f"/graph/memory/{mid}?view=outline")
    assert r.status_code == 200
    assert "gone" in r.text and "· retired" in r.text
