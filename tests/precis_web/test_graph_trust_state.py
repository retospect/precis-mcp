"""The /graph surfaces show each node's and edge's trust state (migration
0214, graph-node-trust-state slice 1) beside — not instead of — the
finding-tier filter, whose chip and control now say "finding tier"."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.revision_context import revision_context
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


@pytest.fixture
def graph(store: Store) -> dict[str, int]:
    focus = store.insert_ref(kind="memory", slug=None, title="legacy focus").id
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        note = store.insert_ref(kind="memory", slug=None, title="model note").id
        store.add_link(src_ref_id=focus, dst_ref_id=note, relation="related-to")
    return {"focus": focus, "note": note}


def test_json_carries_node_edge_and_focus_trust(
    graph_client: TestClient, graph: dict[str, int]
) -> None:
    data = graph_client.get(f"/graph/memory/{graph['focus']}.json").json()
    assert data["focus"]["trust"] == "unknown · unknown"
    (node,) = data["nodes"]
    assert node["trust"] == "generated · hunch"
    (edge,) = data["edges"]
    assert edge["trust"] == "generated · hunch"


def test_panel_and_outline_render_the_stamp(
    graph_client: TestClient, graph: dict[str, int]
) -> None:
    panel = graph_client.get(f"/graph/memory/{graph['focus']}/panel?memories=1")
    assert panel.status_code == 200
    assert "generated · hunch" in panel.text
    assert "finding tier · off" in panel.text
    outline = graph_client.get(f"/graph/memory/{graph['focus']}?view=outline")
    assert "generated · hunch" in outline.text
    assert "unknown · unknown" in outline.text  # the legacy focus
