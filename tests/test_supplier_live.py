"""Supplier identity-only persistence and uncached single-component reads."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.component import ComponentHandler
from precis.supply.digikey import DigiKeyAdapter
from precis.supply.live import LINK_FIELDS, identity_link
from precis.workers.job_types import datasheet_pull as worker


@pytest.fixture
def supplier_wire(monkeypatch):
    calls: list[httpx.Request] = []
    state: dict[str, Any] = {"status": 200, "calls": calls}
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "fake-token")
    monkeypatch.setattr(
        DigiKeyAdapter, "_credentials", staticmethod(lambda: ("fake-id", "fake-secret"))
    )
    for name in ("farnell", "mouser"):
        monkeypatch.setattr(f"precis.supply.{name}.get_secret", lambda name: "fake-key")

    def handle(request):
        calls.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": "fake-key"})
        if "digikey" in request.url.host:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "Categories": [
                            {"CategoryId": 52, "Name": "Resistors", "Children": []}
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "SearchLocaleUsed": {"Currency": "EUR"},
                    "Products": [
                        {
                            "ManufacturerProductNumber": "MPN1",
                            "Manufacturer": {"Name": "Maker"},
                            "ProductUrl": "https://www.digikey.com/part/SKU1",
                            "DatasheetUrl": "https://maker.example/data.pdf",
                            "Description": {
                                "ProductDescription": "Live resistor description"
                            },
                            "Category": {"CategoryId": 52},
                            "Parameters": [
                                {"ParameterText": "Resistance", "ValueText": "10kOhms"}
                            ],
                            "QuantityAvailable": 12000,
                            "ProductVariations": [
                                {
                                    "DigiKeyProductNumber": "SKU1",
                                    "StandardPricing": [
                                        {"BreakQuantity": 1, "UnitPrice": 0.03}
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )
        if "mouser" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "Errors": [],
                    "SearchResults": {
                        "Parts": [
                            {
                                "MouserPartNumber": "SKU1",
                                "ManufacturerPartNumber": "MPN1",
                                "Manufacturer": "Maker",
                                "ProductDetailUrl": "https://www.mouser.ie/part/SKU1",
                                "DataSheetUrl": "https://maker.example/data.pdf",
                                "Description": "Live resistor description",
                                "Category": "Resistors",
                                "ProductAttributes": [
                                    {
                                        "AttributeName": "Resistance",
                                        "AttributeValue": "10kOhms",
                                    }
                                ],
                                "PriceBreaks": [
                                    {"Quantity": 1, "Price": "€0.03", "Currency": "EUR"}
                                ],
                                "AvailabilityInStock": "12000",
                            }
                        ]
                    },
                },
            )
        inventory = request.url.params["resultsSettings.responseGroup"] == "inventory"
        return httpx.Response(
            200,
            json={
                "keywordSearchReturn": {
                    "products": [
                        {
                            "sku": "SKU1",
                            "brandName": "Maker",
                            "translatedManufacturerPartNumber": "MPN1",
                            "productURL": "https://ie.farnell.com/part/dp/SKU1",
                            "datasheets": [{"url": "https://maker.example/data.pdf"}],
                            "displayName": "Live resistor description",
                            "attributes": [
                                {
                                    "attributeLabel": "Resistance",
                                    "attributeValue": "10kOhms",
                                }
                            ],
                            "prices": [{"from": 1, "cost": 0.03}],
                            "stock": {
                                "level": 12000,
                                "breakdown": [
                                    {"inv": 12000, "region": "UK", "warehouse": "UK1"}
                                ],
                            }
                            if inventory
                            else {},
                        }
                    ]
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "get", client.get)
    monkeypatch.setattr(httpx, "post", client.post)
    return state


@pytest.mark.parametrize("supplier", ["digikey", "farnell", "mouser"])
def test_link_whitelist_then_live_read_stored_bulk_and_error(
    store, monkeypatch, supplier_wire, supplier
):
    hub = Hub(store=store)
    handler = ComponentHandler(hub=hub)
    slug = "dogfood-live-" + supplier
    handler.put(id=slug, category="electronic", meta={"mpn": "MPN1"})
    jobs = []

    def submit(**kw):
        jobs.append(kw)
        return SimpleNamespace(reused=len(jobs) > 1)

    monkeypatch.setattr(Hub, "sibling", lambda self, kind: SimpleNamespace(put=submit))
    response = handler.put(
        id=slug,
        mode="supplier-link",
        source=supplier,
        args={"supplier_part_number": "SKU1"},
    )
    assert "linked" in response.body and "datasheet queued" in response.body
    ref = store.get_ref(kind="component", id=slug)
    link = ref.meta["supplier_links"][0]
    assert set(link) == set(LINK_FIELDS)
    assert "description" not in str(ref.meta) and "12000" not in str(ref.meta)
    assert store.component_values_for_ref(ref.id) == []
    assert jobs[0]["params"] == {
        "component_ref_id": ref.id,
        "url": "https://maker.example/data.pdf",
    }
    assert jobs[0]["job_type"] == "datasheet_pull"
    first = link["first_linked_at"]
    duplicate = handler.put(
        id=slug,
        mode="supplier-link",
        source=supplier,
        args={"supplier_part_number": "SKU1"},
    )
    assert duplicate.reused
    assert (
        store.get_ref(kind="component", id=slug).meta["supplier_links"][0][
            "first_linked_at"
        ]
        == first
    )
    calls = supplier_wire["calls"]
    before = len(calls)
    for view in ("stored", "table", "tree", "bom"):
        handler.get(id=slug, view=view)
    handler.get()
    handler.search(q="dogfood-live")
    assert len(calls) == before
    body = handler.get(id=slug).body
    assert f"live from {supplier}, retrieved" in body and "not stored" in body
    assert "Live resistor description" in body and "10kOhms" in body
    assert "price_breaks" in body and "12000" in body
    if supplier == "farnell":
        assert "UK1" in body and "not supplied by API" in body
    else:
        assert "EUR" in body
    assert store.get_ref(kind="component", id=slug).meta == ref.meta
    assert len(calls) > before
    assert all(request.extensions["timeout"]["read"] == 3 for request in calls)
    supplier_wire["status"] = 403
    failure = handler.get(id=slug).body
    assert "supplier unreachable: HTTP 403" in failure and "fake-key" not in failure
    assert store.component_values_for_ref(ref.id) == []


def test_keyword_refuses_link_and_api_import_not_supported(store, supplier_wire):
    handler = ComponentHandler(hub=Hub(store=store))
    handler.put(
        id="dogfood-keyword-link", category="electronic", meta={"mpn": "10k resistor"}
    )
    with pytest.raises(BadInput, match="no unique"):
        handler.put(id="dogfood-keyword-link", mode="supplier-link", source="digikey")
    with pytest.raises(BadInput, match="API data import is not supported"):
        handler.put(id="dogfood-keyword-link", mode="import", source="digikey")
    assert (
        "supplier_links"
        not in store.get_ref(kind="component", id="dogfood-keyword-link").meta
    )


def test_datasheet_worker_reuses_guarded_download_and_independent_ingest(
    store, monkeypatch
):
    handler = ComponentHandler(hub=Hub(store=store))
    handler.put(id="dogfood-datasheet-source", category="electronic")
    ref = store.get_ref(kind="component", id="dogfood-datasheet-source")
    url = "https://maker.example/data.pdf"
    link = identity_link(
        "digikey",
        {
            "supplier_part_number": "SKU1",
            "manufacturer": "Maker",
            "mpn": "MPN1",
            "product_url": "https://www.digikey.com/part/SKU1",
            "datasheet_url": url,
            "match_confidence": "exact_mpn",
        },
        datetime(2026, 10, 5, tzinfo=UTC),
    )
    store.component_supplier_link(ref.id, link)
    calls = []

    def download(received, path):
        calls.append(received)
        path.write_bytes(b"%PDF-dogfood-fixture")
        return received, "a" * 64

    monkeypatch.setattr(worker, "_download", download)
    monkeypatch.setattr(worker, "_existing_by_sha", lambda store, sha: None)
    datasheet = store.insert_ref(
        kind="datasheet",
        slug="dogfood-manufacturer-evidence",
        title="Fixture manufacturer datasheet",
    )
    monkeypatch.setattr(worker, "_ingest", lambda store, path: (datasheet.id, True))
    monkeypatch.setattr(worker, "_body_chunks", lambda store, id: 1)
    result = worker.pull_component(store, ref.id, url)
    assert result["status"] == "ok" and result["datasheet_ref_id"] == datasheet.id
    assert datetime.fromisoformat(result["at"]).tzinfo is not None
    assert calls == [url]
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM links WHERE src_ref_id=%s AND dst_ref_id=%s AND relation='datasheet-of'",
                (datasheet.id, ref.id),
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="match stored"):
        worker.pull_component(store, ref.id, "https://other.example/data.pdf")
    assert store.component_values_for_ref(ref.id) == []
    handler.put(
        id=ref.slug,
        spec="resistance",
        value=10000,
        unit="ohm",
        method="datasheet",
        source="datasheet:dogfood-manufacturer-evidence",
        as_of="2026-10-05",
    )
    values = store.component_values_for_ref(ref.id)
    assert len(values) == 1 and values[0]["source_ref_id"] == datasheet.id
    assert values[0]["source_url"] is None and values[0]["method"] == "datasheet"

    def failed_download(received, path):
        raise worker._Failed("download_failed")

    monkeypatch.setattr(worker, "_download", failed_download)
    failure = worker.pull_component(store, ref.id, url)
    assert failure["status"] == "failed"
    assert failure["reason"] == "download_failed"
    assert datetime.fromisoformat(failure["at"]).tzinfo is not None


def test_datasheet_params_schema_preserves_lcsc_and_accepts_component_target():
    from collections.abc import Callable
    from importlib import import_module

    jsonschema = import_module("jsonschema")
    validate: Callable[[dict[str, Any], dict[str, Any]], None] = jsonschema.validate
    ValidationError: type[Exception] = jsonschema.ValidationError

    validate({"lcsc": "C25900"}, worker.PARAMS_SCHEMA)
    validate(
        {"component_ref_id": 1, "url": "https://maker.example/data.pdf"},
        worker.PARAMS_SCHEMA,
    )
    with pytest.raises(ValidationError):
        validate(
            {
                "lcsc": "C1",
                "component_ref_id": 1,
                "url": "https://maker.example/data.pdf",
            },
            worker.PARAMS_SCHEMA,
        )
    with pytest.raises(ValidationError):
        validate({"component_ref_id": 1}, worker.PARAMS_SCHEMA)
