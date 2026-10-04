"""Discover defaults, saved-preference isolation and explicit date orders."""

from __future__ import annotations

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import ChunkInsert
from precis_web.app import create_app
from precis_web.config import WebConfig


def test_fresh_discover_includes_findings_and_filed_items(client, runtime):
    response = client.get("/drive")
    assert response.status_code == 200
    assert {"finding", "citation", "concept", "structure"} <= set(
        runtime.store.recent_kinds
    )
    assert runtime.store.recent_created
    assert not runtime.store.recent_unfiled_only
    assert "items_kinds" not in response.cookies
    assert "Return" not in response.text and "Showcase" not in response.text


@pytest.mark.parametrize("saved", ["paper", "", "stale-plugin-kind"])
def test_discover_bypasses_but_preserves_saved_kinds(client, runtime, saved):
    client.cookies.set("items_kinds", saved)
    response = client.get("/drive?task=discover")
    assert "finding" in runtime.store.recent_kinds
    assert "stale-plugin-kind" not in runtime.store.recent_kinds
    assert "items_kinds" not in response.cookies
    assert client.cookies.get("items_kinds") == saved
    restored = client.get("/drive")
    assert runtime.store.recent_kinds == ([saved] if saved else [])
    assert "Using your saved kinds" in restored.text
    assert "Discover across kinds, including findings" in restored.text


def test_discover_explicit_facets_and_pager_are_authoritative(client, runtime):
    response = client.get(
        "/drive?task=discover&k=finding&folder=200&tag=DIM:2d&sort=modified&page=2"
    )
    assert runtime.store.recent_kinds == ["finding"]
    assert runtime.store.recent_parent_id == 200
    assert runtime.store.recent_tags == ["DIM:2d"]
    assert not runtime.store.recent_created
    assert "task=discover" in response.text
    assert "sort=modified" in response.text
    assert "folder=200" in response.text
    assert "tag=DIM%3A2d" in response.text
    assert "items_kinds" not in response.cookies


@pytest.mark.parametrize("sort", ["created", "modified", "recency"])
def test_named_sorts_reach_browse_and_search(client, runtime, sort):
    client.get(f"/drive?k=finding&sort={sort}")
    assert runtime.store.recent_created == (sort == "created")
    client.get(f"/drive?q=graphene&k=finding&sort={sort}")
    assert runtime.store.search_sort == sort


class _FormInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.picker_open = None
        self.kinds = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "details" and attributes.get("x-ref") == "picker":
            self.picker_open = "open" in attributes
        if tag == "input" and attributes.get("name") == "k":
            self.kinds[attributes["value"]] = attributes


def test_closed_searchable_picker_keeps_selection_serializable(client, runtime):
    runtime.hub = SimpleNamespace(
        kinds={"finding", "future-kind"},
        handler_for=lambda kind: SimpleNamespace(spec=None),
    )
    response = client.get("/drive?k=finding&k=future-kind")
    inventory = _FormInventory()
    inventory.feed(response.text)
    assert inventory.picker_open is False
    for kind in ("finding", "future-kind"):
        attributes = inventory.kinds[kind]
        assert "checked" in attributes and "disabled" not in attributes
        assert attributes["type"] == "checkbox"
    assert 'id="drive-kind-search"' in response.text
    assert 'aria-label="Active filters"' in response.text
    assert ">Reset</a>" in response.text


def test_unknown_deep_link_and_machine_inputs_survive_picker(client):
    response = client.get("/drive?k=another-plugin-kind")
    inventory = _FormInventory()
    inventory.feed(response.text)
    assert "checked" in inventory.kinds["another-plugin-kind"]
    assert {"job", "orcid", "agentlog"} <= inventory.kinds.keys()


@pytest.mark.parametrize("query", ["", "&q=graphene"])
def test_active_restrictions_name_where_they_apply(client, query):
    response = client.get(
        f"/drive?k=paper&since=2024-01-01&folder=200&state=stub{query}"
    )
    if query:
        assert "Folder 200 (browse only)" in response.text
        assert "Show: stub (browse only)" in response.text
    else:
        assert "Created since 2024-01-01 (search only)" in response.text


def test_failed_hub_introspection_keeps_compatibility_picker(client, runtime):
    class BrokenHub:
        @property
        def kinds(self):
            raise RuntimeError("kind roster unavailable")

    runtime.hub = BrokenHub()
    response = client.get("/drive?task=discover")
    assert response.status_code == 200
    assert "finding" in runtime.store.recent_kinds


def test_empty_kind_selection_survives_facet_removal(client, runtime):
    from urllib.parse import urlencode

    response = client.get("/drive?task=discover&submitted=1&sort=created")
    assert runtime.store.recent_kinds == []
    url = "/drive?" + urlencode(
        [("submitted", "1"), ("folder", "*"), ("task", "discover"), ("sort", "")]
    )
    from html import escape

    assert escape(url, quote=True) in response.text
    client.get(url)
    assert runtime.store.recent_kinds == []


@pytest.fixture
def pg_client(runtime_with_store, tmp_path):
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


@pytest.mark.parametrize("query", ["", "&q=graphene"])
@pytest.mark.parametrize(
    ("sort", "first"), [("created", "newer design"), ("modified", "older finding")]
)
def test_created_modified_order_with_filed_design_and_finding(
    store, pg_client, query, sort, first
):
    finding = store.insert_ref(kind="finding", slug=None, title="older finding")
    design = store.insert_ref(kind="cad", slug="newer-design", title="newer design")
    folder = store.insert_ref(kind="folder", slug=None, title="filed outputs")
    for ref in (finding, design):
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text="graphene discovery comparison")]
        )
    with store.pool.connection() as connection:
        connection.execute(
            "UPDATE refs SET created_at = '2024-01-01', updated_at = '2024-03-01' "
            "WHERE ref_id = %s",
            (finding.id,),
        )
        connection.execute(
            "UPDATE refs SET created_at = '2024-02-01', updated_at = '2024-02-01', "
            "parent_id = %s WHERE ref_id = %s",
            (folder.id, design.id),
        )
        connection.commit()
    response = pg_client.get(f"/drive?task=discover&sort={sort}{query}")
    assert response.status_code == 200
    other = "older finding" if first == "newer design" else "newer design"
    assert response.text.index(first) < response.text.index(other)
    assert "All folders" in response.text
