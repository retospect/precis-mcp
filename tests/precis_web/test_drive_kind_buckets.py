"""Every registered kind has one reachable Drive category; Reto's R14 rulings."""

from __future__ import annotations

from collections import Counter, defaultdict
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from precis_web.item_view import _ARTIFACT_KIND_FALLBACK
from precis_web.routes.drive import _kind_buckets

EXPECTED = {
    "se": "design",
    "finding": "author",
    "tex": "author",
    "news": "sources",
    "anki": "derived",
    "citation": "derived",
    "message": "machine",
}


class _Kinds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs: dict[str, list[dict[str, str | None]]] = defaultdict(list)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "k":
            value = attributes["value"]
            assert value is not None
            self.inputs[value].append(attributes)


def test_every_registered_kind_has_exactly_one_rendered_bucket(
    runtime_with_store, runtime, client
):
    runtime.hub = runtime_with_store.hub
    buckets = _kind_buckets(runtime.hub)
    counts = Counter(kk for kinds in buckets.values() for kk in kinds)
    assert runtime.hub.kinds <= counts.keys()
    assert all(count == 1 for count in counts.values())
    for kind, bucket in EXPECTED.items():
        assert kind in buckets[bucket]
    inventory = _Kinds()
    response = client.get("/drive")
    assert response.status_code == 200
    inventory.feed(response.text)
    for kind in runtime.hub.kinds:
        assert len(inventory.inputs[kind]) == 1
    for kind, bucket in EXPECTED.items():
        assert inventory.inputs[kind][0]["data-kind-bucket"] == bucket
    for kind in ("se", "finding", "tex", "anki", "citation", "news"):
        assert "checked" in inventory.inputs[kind][0]
    assert "checked" not in inventory.inputs["message"][0]


def test_future_kinds_join_author_or_visible_other():
    placements = {"future-design": "artifact", "future-stream": "stream"}
    hub = SimpleNamespace(
        kinds=set(placements),
        handler_for=lambda kind: SimpleNamespace(
            spec=SimpleNamespace(placement=placements[kind])
        ),
    )
    buckets = _kind_buckets(hub)
    assert "future-design" in buckets["author"]
    assert "future-stream" in buckets["other"]
    assert (
        Counter(kk for kinds in buckets.values() for kk in kinds)["future-design"] == 1
    )


class _BrokenHub:
    @property
    def kinds(self):
        raise RuntimeError("hub unreachable")


@pytest.mark.parametrize("hub", [None, _BrokenHub()])
def test_plugin_fallback_keeps_se_in_design_and_fresh_selection(client, runtime, hub):
    plugins = {"se", "protein", "route", "pathway"}
    assert plugins <= set(_ARTIFACT_KIND_FALLBACK)
    runtime.hub = hub
    response = client.get("/drive")
    assert response.status_code == 200
    inventory = _Kinds()
    inventory.feed(response.text)
    for kind in plugins:
        assert len(inventory.inputs[kind]) == 1
        assert "checked" in inventory.inputs[kind][0]
    assert inventory.inputs["se"][0]["data-kind-bucket"] == "design"


def test_bucket_presets_and_saved_unknown_kind_remain_serializable(client, runtime):
    response = client.get("/drive?scope=derived&tag=DIM:2d")
    assert response.status_code == 200
    assert set(runtime.store.recent_kinds) == {"anki", "citation"}
    assert "scope=derived" in response.text and "tag=DIM%3A2d" in response.text
    assert "items_kinds" not in response.cookies
    client.get("/drive?scope=mine")
    assert {"finding", "tex", "se"} <= set(runtime.store.recent_kinds)
    client.get("/drive?scope=machine")
    assert "message" in runtime.store.recent_kinds
    client.get("/drive?scope=sources")
    assert "news" in runtime.store.recent_kinds
    client.get("/drive?scope=derived&k=se")
    assert runtime.store.recent_kinds == ["se"]
    client.cookies.set("items_kinds", "stale-plugin-kind,finding")
    response = client.get("/drive")
    inventory = _Kinds()
    inventory.feed(response.text)
    assert runtime.store.recent_kinds == ["stale-plugin-kind", "finding"]
    assert len(inventory.inputs["stale-plugin-kind"]) == 1
    assert inventory.inputs["stale-plugin-kind"][0]["data-kind-bucket"] == "other"
    assert "checked" in inventory.inputs["stale-plugin-kind"][0]
    assert client.cookies.get("items_kinds") == "stale-plugin-kind,finding"
