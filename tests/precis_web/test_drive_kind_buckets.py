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
    named = {
        kk for bucket, kinds in buckets.items() if bucket != "other" for kk in kinds
    }
    for kind in named:
        assert len(inventory.inputs[kind]) == 1
    # gr664402 — an "Other" kind renders a chip only when it can return a
    # row: ref-backed (the fake's ``ref_kinds`` seed) or already selected.
    # Compute/file-backed kinds (calc/random/time/skill/md/python/…)
    # register and land in "other" but store no refs, so they offer
    # nothing; ``tag`` keeps its chip via its own row source (_tag_rows).
    row_kinds = runtime.store.ref_kinds | {"tag"}
    for kind in buckets["other"]:
        assert len(inventory.inputs[kind]) == (1 if kind in row_kinds else 0), kind
    for kind, bucket in EXPECTED.items():
        assert inventory.inputs[kind][0]["data-kind-bucket"] == bucket
    for kind in ("se", "finding", "tex", "anki", "citation", "news"):
        assert "checked" in inventory.inputs[kind][0]
    assert "checked" not in inventory.inputs["message"][0]


def test_selected_rowless_kind_keeps_its_chip(runtime_with_store, runtime, client):
    """gr664402 — a deep-linked ``k=`` scope stays serializable: an
    explicit ``k=calc`` still renders its chip (checked) even though calc
    can never return a row; unselected, it is not offered at all."""
    runtime.hub = runtime_with_store.hub
    if "calc" not in runtime.hub.kinds:
        pytest.skip("calc kind not registered in this build")
    assert "calc" in _kind_buckets(runtime.hub)["other"]
    response = client.get("/drive?k=calc&submitted=1")
    assert response.status_code == 200
    inventory = _Kinds()
    inventory.feed(response.text)
    assert len(inventory.inputs["calc"]) == 1
    assert "checked" in inventory.inputs["calc"][0]
    assert inventory.inputs["calc"][0]["data-kind-bucket"] == "other"
    # A typed ``/calc`` token selects the same dead kind — ``_split_kind_tokens``
    # reassigns ``selected_kinds`` before the facet filter runs, so the chip
    # must still render (and serialize through the next submit).
    response = client.get("/drive", params={"q": "/calc"})
    assert response.status_code == 200
    inventory = _Kinds()
    inventory.feed(response.text)
    assert len(inventory.inputs["calc"]) == 1
    assert "checked" in inventory.inputs["calc"][0]
    assert inventory.inputs["calc"][0]["data-kind-bucket"] == "other"
    response = client.get("/drive?k=paper&submitted=1")
    assert response.status_code == 200
    inventory = _Kinds()
    inventory.feed(response.text)
    assert "calc" not in inventory.inputs


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
