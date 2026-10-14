"""Real-PG tests for the neighbourhood panel (web-graph-navigation slice 2).

The panel is an HTMX fragment — ``GET /graph/<kind>/<id>/panel`` — mounted
on the shared ref detail template (and the papers reader) via
``graph/_panel_mount.html.j2``. Pinned here: the mount itself, the
``ring_group`` heading → rel → kind grouping with counts, the per-group
"show all" expand (``?group=<heading>``), the "Show memories" toggle and
its spec default, and the 404.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.graph import PANEL_GROUP_ROWS


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
    """A paper with links both ways, spanning three ring buckets + Other —
    the same shape the JSON tests pin (``test_graph_json.py``)."""
    focus = _paper(store, "focus2020")
    cited = _paper(store, "cited2019")
    citer = _paper(store, "citer2021")
    note = _memory(store, "a note")
    goal = _memory(store, "a goal")
    old = _memory(store, "an old note")
    store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=focus, relation="cites")
    store.add_link(src_ref_id=note, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=focus, dst_ref_id=goal, relation="serves")
    store.add_link(src_ref_id=focus, dst_ref_id=old, relation="supersedes")
    return {
        "focus": focus,
        "cited": cited,
        "citer": citer,
        "note": note,
        "goal": goal,
        "old": old,
    }


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.format_handle(kind, ref_id)


def test_detail_page_mounts_the_panel(graph_client, store) -> None:
    """The shared detail template carries the mount for a generic-rendered
    kind — the papers reader gets it too (``/refs/paper`` 303s there)."""
    mid = _memory(store, "a memory")
    r = graph_client.get(f"/refs/memory/{mid}")
    assert r.status_code == 200
    assert 'id="graph-panel"' in r.text
    assert f'hx-get="/graph/memory/{mid}/panel"' in r.text
    assert f'href="/graph/memory/{mid}"' in r.text

    pid = _paper(store, "reader2020")
    r = graph_client.get(f"/refs/paper/{pid}", follow_redirects=False)
    assert r.status_code == 303
    r = graph_client.get(f"/papers/{pid}")
    assert r.status_code == 200
    assert f'hx-get="/graph/paper/{pid}/panel"' in r.text


def test_panel_groups_and_counts(graph_client, fixture_graph) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}/panel?memories=1")
    assert r.status_code == 200
    body = r.text
    # ring_group headings in ring order, Other last
    i_road = body.index("Roadmap")
    i_notes = body.index("Notes &amp; links")
    i_other = body.index("Other")
    assert i_road < i_notes < i_other
    # rel + kind counts, both directions
    assert "serves" in body and "cites" in body and "related-to" in body
    assert "cited-by" in body and "supersedes" in body
    assert "paper&nbsp;×1" in body and "memory&nbsp;×1" in body
    # rows: far ref's handle linked to its reader, direction glyph present
    assert f'href="/papers/{fixture_graph["cited"]}"' in body
    assert _h("paper", fixture_graph["cited"]) in body
    assert _h("memory", fixture_graph["note"]) in body
    assert "→" in body and "←" in body
    # every edge row get(view='links') would show: 5 edges, all rendered
    assert body.count('title="outbound"') + body.count('title="inbound"') == 5
    # the toggle radios carry the explicit state
    assert 'name="memories" value="1" checked' in body


def test_panel_memories_default(graph_client, fixture_graph, store) -> None:
    """The spec default: ON when the centre is a memory or memory is the
    strict majority kind among the direct links, OFF otherwise; the radio
    posts the explicit 0|1 either way."""
    focus = fixture_graph["focus"]
    # fixture: 3 memory links of 5 → majority → default ON
    r = graph_client.get(f"/graph/paper/{focus}/panel")
    assert 'name="memories" value="1" checked' in r.text
    assert _h("memory", fixture_graph["note"]) in r.text

    # memory-minority centre (1 of 3 links) → default OFF: memory rows and
    # their count buckets vanish; ``?memories=1`` restores them.
    hub = _paper(store, "hub2020")
    a = _memory(store, "mem a")
    c = _paper(store, "leaf2020")
    d = _paper(store, "leaf2021")
    store.add_link(src_ref_id=hub, dst_ref_id=a, relation="related-to")
    store.add_link(src_ref_id=hub, dst_ref_id=c, relation="cites")
    store.add_link(src_ref_id=hub, dst_ref_id=d, relation="cites")
    r = graph_client.get(f"/graph/paper/{hub}/panel")
    assert 'name="memories" value="0" checked' in r.text
    assert _h("memory", a) not in r.text
    r = graph_client.get(f"/graph/paper/{hub}/panel?memories=1")
    assert _h("memory", a) in r.text

    # memory-centred → default ON
    note = fixture_graph["note"]
    r = graph_client.get(f"/graph/memory/{note}/panel")
    assert 'name="memories" value="1" checked' in r.text
    assert _h("paper", focus) in r.text


def test_panel_expand_filters_to_one_group(graph_client, fixture_graph) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}/panel?group=Notes+%26+links&memories=1")
    assert r.status_code == 200
    assert "Notes &amp; links" in r.text
    assert "Roadmap" not in r.text and "cited-by" not in r.text
    assert _h("paper", fixture_graph["cited"]) in r.text
    assert _h("memory", fixture_graph["note"]) in r.text
    # an unknown heading is an honest empty fragment, not a 500
    r = graph_client.get(f"/graph/paper/{focus}/panel?group=nosuch")
    assert r.status_code == 200 and "no group named" in r.text


def test_panel_expand_past_the_row_cap(graph_client, store) -> None:
    """A group over PANEL_GROUP_ROWS renders the window + a "show all"
    affordance; ``?group=`` answers the full returned row set."""
    focus = _memory(store, "hub memory")
    leaves = [_memory(store, f"leaf {i}") for i in range(PANEL_GROUP_ROWS + 5)]
    for leaf in leaves:
        store.add_link(src_ref_id=focus, dst_ref_id=leaf, relation="related-to")

    r = graph_client.get(f"/graph/memory/{focus}/panel")
    assert r.status_code == 200
    rows = r.text.count('title="outbound"') + r.text.count('title="inbound"')
    assert rows == PANEL_GROUP_ROWS
    assert "show all 30" in r.text
    assert "group=Notes" in r.text

    r = graph_client.get(
        f"/graph/memory/{focus}/panel?group=Notes+%26+links&memories=1"
    )
    rows = r.text.count('title="outbound"') + r.text.count('title="inbound"')
    assert rows == 30
    assert "show all" not in r.text


def test_panel_404(graph_client) -> None:
    r = graph_client.get("/graph/paper/999999999/panel")
    assert r.status_code == 404
    assert "ref not found" in r.text
