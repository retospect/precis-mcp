"""Fake regional supplier reads: no credentials, network or persistent content."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from precis.supply import base
from precis.supply.digikey import DigiKeyAdapter
from precis.supply.farnell import FarnellAdapter
from precis.supply.mouser import MouserAdapter


def wire(monkeypatch, payload, *, status=200):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "get", client.get)
    monkeypatch.setattr(httpx, "post", client.post)
    for module in ("farnell", "mouser"):
        monkeypatch.setattr(
            f"precis.supply.{module}.get_secret", lambda name: "fake-key-sensitive"
        )
    return calls


def farnell_product(stock):
    return {
        "sku": "1234",
        "displayName": "Socket cap M4 x 12mm",
        "translatedManufacturerPartNumber": "SC412",
        "productURL": "https://ie.farnell.com/part/dp/1234",
        "stock": stock,
    }


@pytest.mark.parametrize(
    "store,destination",
    [("ie.farnell.com", "IE"), ("uk.farnell.com", "GB"), ("de.farnell.com", "DE")],
)
def test_farnell_store_and_detailed_warehouse_stock(monkeypatch, store, destination):
    calls = wire(
        monkeypatch,
        {
            "keywordSearchReturn": {
                "products": [
                    farnell_product(
                        {
                            "level": 12005,
                            "breakdown": [
                                {"inv": 12000, "region": "UK", "warehouse": "UK1"},
                                {"inv": 5, "region": "DE", "warehouse": "DE1"},
                            ],
                            "regionalBreakdown": [{"level": 12005, "warehouse": "UK"}],
                        }
                    )
                ]
            }
        },
    )
    monkeypatch.setenv("PRECIS_FARNELL_STORE", store)
    adapter = FarnellAdapter()
    assert adapter.configured() is None
    quotes = adapter.search("M4 x 12mm socket head cap screw")
    assert [q.quantity for q in quotes] == [12000, 5]
    assert [q.region for q in quotes] == ["GB", "DE"]
    assert quotes[0].warehouse == "UK1" and quotes[0].ships_from == "GB"
    assert quotes[0].ships_to == destination and quotes[0].unit_price is None
    assert quotes[0].match_confidence == "keyword"
    params = calls[0].url.params
    assert params["storeInfo.id"] == store
    assert params["term"] == "any:M4 x 12mm socket head cap screw"
    assert params["versionNumber"] == "1.4"
    assert params["resultsSettings.responseGroup"] == "inventory"
    assert params["callInfo.apiKey"] == "fake-key-sensitive"


@pytest.mark.parametrize(
    "stock,region,warehouse",
    [
        ({"regionalBreakdown": [{"level": 30, "warehouse": "UK"}]}, "GB", "UK"),
        ({"regionalBreakdown": [{"level": 30, "warehouse": "Liege"}]}, None, "Liege"),
        ({"level": 30}, None, None),
    ],
)
def test_farnell_fallback_does_not_invent_store_origin(
    monkeypatch, stock, region, warehouse
):
    wire(monkeypatch, {"keywordSearchReturn": {"products": [farnell_product(stock)]}})
    result = FarnellAdapter().search("SC412")[0]
    assert result.region == region and result.warehouse == warehouse
    assert result.quantity == 30 and result.match_confidence == "exact_mpn"


def mouser_part(**patch):
    return {
        "MouserPartNumber": "603-RC0402",
        "ManufacturerPartNumber": "RC0402FR-0710KL",
        "Description": "10k resistor 0402",
        "AvailabilityInStock": "13180000",
        "ProductDetailUrl": "https://www.mouser.ie/ProductDetail/Yageo/RC0402",
        "PriceBreaks": [
            {"Quantity": 1000, "Price": "0.00289", "Currency": "EUR"},
            {"Quantity": 1, "Price": "0.03", "Currency": "EUR"},
        ],
        **patch,
    }


def test_mouser_documented_request_stock_price_identity(monkeypatch):
    calls = wire(
        monkeypatch, {"Errors": [], "SearchResults": {"Parts": [mouser_part()]}}
    )
    adapter = MouserAdapter()
    assert adapter.configured() is None
    quote = adapter.search("RC0402FR-0710KL")[0]
    import json

    request = json.loads(calls[0].content)["SearchByKeywordRequest"]
    assert calls[0].url.path == "/api/v1/search/keyword"
    assert request == {
        "keyword": "RC0402FR-0710KL",
        "records": 5,
        "startingRecord": 0,
        "searchOptions": "None",
        "searchWithYourSignUpLanguage": "false",
    }
    assert (
        quote.quantity == 13180000
        and quote.currency == "EUR"
        and quote.unit_price == 0.03
    )
    assert quote.match_confidence == "exact_mpn"
    assert quote.region is quote.warehouse is quote.ships_from is None


@pytest.mark.parametrize(
    "stock,expected", [("12,000 In Stock", 12000), ("0 In Stock", 0)]
)
def test_mouser_english_fallback(monkeypatch, stock, expected):
    wire(
        monkeypatch,
        {
            "SearchResults": {
                "Parts": [
                    mouser_part(
                        AvailabilityInStock=None,
                        Availability=stock,
                        PriceBreaks=[
                            {"Quantity": 1, "Price": "€0,03", "Currency": "EUR"}
                        ],
                    )
                ]
            }
        },
    )
    quote = MouserAdapter().search("10k resistor")[0]
    assert (
        quote.quantity == expected
        and quote.unit_price is None
        and quote.currency == "EUR"
    )
    assert quote.match_confidence == "keyword"


@pytest.mark.parametrize(
    "adapter,empty",
    [
        (FarnellAdapter, {"keywordSearchReturn": {"products": []}}),
        (MouserAdapter, {"SearchResults": {"Parts": []}}),
    ],
)
def test_empty_is_success_and_errors_never_echo_keys(
    monkeypatch, caplog, adapter, empty
):
    wire(monkeypatch, empty)
    assert adapter().search("M4") == []
    wire(monkeypatch, {"error": "fake-key-sensitive"}, status=403)
    monkeypatch.setattr(base, "adapters", lambda: [adapter()])
    result = base.quote("M4")
    assert result.quotes == []
    assert result.outcomes[0].error == "HTTP 403 at search"
    assert "fake-key-sensitive" not in caplog.text + result.outcomes[0].line()


@pytest.mark.parametrize(
    "adapter,payload",
    [
        (FarnellAdapter, {"error": "fake-key-sensitive"}),
        (FarnellAdapter, {"keywordSearchReturn": {"products": [farnell_product({})]}}),
        (MouserAdapter, {"Errors": [{"Message": "fake-key-sensitive"}]}),
        (
            MouserAdapter,
            {
                "SearchResults": {
                    "Parts": [
                        mouser_part(AvailabilityInStock="", Availability="On order 20")
                    ]
                }
            },
        ),
    ],
)
def test_api_or_missing_quantity_is_error_not_zero(monkeypatch, adapter, payload):
    wire(monkeypatch, payload)
    monkeypatch.setattr(base, "adapters", lambda: [adapter()])
    result = base.quote("M4")
    assert result.outcomes[0].error == "ValueError"
    assert (
        result.outcomes[0].count is None
        and "0 products" not in result.outcomes[0].line()
    )


def test_digikey_locale_defaults_override_currency_and_cache_isolation(monkeypatch):
    for key in ("PRECIS_DIGIKEY_SITE", "PRECIS_DIGIKEY_CURRENCY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(DigiKeyAdapter, "_access_token", lambda self: "fake")
    monkeypatch.setattr(
        DigiKeyAdapter, "_credentials", staticmethod(lambda: ("fake", "fake"))
    )
    calls = wire(monkeypatch, {"Categories": []})
    ie = DigiKeyAdapter()
    assert ie._headers()["X-DIGIKEY-Locale-Site"] == "IE"
    assert ie._headers()["X-DIGIKEY-Locale-Currency"] == "EUR"
    ie.categories()
    ie.categories()
    monkeypatch.setenv("PRECIS_DIGIKEY_SITE", "UK")
    monkeypatch.setenv("PRECIS_DIGIKEY_CURRENCY", "GBP")
    uk = DigiKeyAdapter()
    assert uk._headers()["X-DIGIKEY-Locale-Site"] == "UK"
    assert uk._headers()["X-DIGIKEY-Locale-Currency"] == "GBP"
    assert uk._headers(currency="USD")["X-DIGIKEY-Locale-Currency"] == "USD"
    uk.categories()
    assert len(calls) == 3
    wire(
        monkeypatch,
        {
            "SearchLocaleUsed": {"Currency": "GBP"},
            "Products": [
                {
                    "QuantityAvailable": 12,
                    "ManufacturerProductNumber": "X1",
                    "ProductVariations": [
                        {
                            "DigiKeyProductNumber": "DK1",
                            "StandardPricing": [{"UnitPrice": 2}],
                        }
                    ],
                }
            ],
        },
    )
    quote = uk.search("X1")[0]
    assert quote.currency == "GBP" and quote.ships_to == "UK"
    assert quote.region is None and quote.warehouse is None


def test_home_preference_sort_unknown_origin_and_partial_failure(monkeypatch):
    def item(sku, region, quantity, currency="EUR"):
        return base.StockQuote(
            "test",
            sku,
            sku,
            quantity,
            0.1,
            currency,
            None,
            datetime(2026, 10, 5, tzinfo=UTC),
            region=region,
        )

    class Good:
        name = "good"

        def configured(self):
            return None

        def search(self, query, *, limit=5):
            return [
                item("US-million", "US", 1000000, "USD"),
                item("GB-local", "GB", 12000),
                item("IE-zero", "IE", 0),
                item("DE-local", "DE", 500),
                item("unknown", None, 2000000),
            ]

    class Bad:
        name = "bad"

        def configured(self):
            return None

        def search(self, query, *, limit=5):
            raise base.SupplierError("search", 403)

    monkeypatch.setenv("PRECIS_SUPPLY_REGION_PREFERENCE", "DE,GB,IE")
    monkeypatch.setattr(base, "adapters", lambda: [Good(), Bad()])
    result = base.quote("M4")
    assert [q.sku for q in result.quotes] == [
        "DE-local",
        "GB-local",
        "unknown",
        "US-million",
        "IE-zero",
    ]
    assert result.outcomes[1].error == "HTTP 403 at search"
    assert "origin unknown" in result.quotes[2].line()


def test_automatic_digikey_criteria_keep_other_supplier_keyword_reads(monkeypatch):
    calls = []

    class Other:
        name = "other"

        def configured(self):
            return None

        def search(self, query, *, limit=5):
            calls.append(query)
            return []

    monkeypatch.setattr(base, "adapters", lambda: [Other()])
    result = base.quote(
        "M4 x 12mm socket head cap screw", category_id=572, criteria={"thread": 4}
    )
    assert result.outcomes[0].count == 0 and result.outcomes[0].category_id is None
    assert len(calls) == 1
    result = base.quote("M4", category_id=572, parameters={"1": ["2"]})
    assert "unsupported" in (result.outcomes[0].error or "") and len(calls) == 1


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        (FarnellAdapter, {"keywordSearchReturn": {"products": []}}),
        (MouserAdapter, {"SearchResults": {"Parts": []}}),
    ],
)
def test_httpx_info_logs_do_not_expose_query_credentials(
    monkeypatch, caplog, adapter, payload
):
    import logging

    wire(monkeypatch, payload)
    caplog.set_level(logging.INFO, logger="httpx")
    adapter().search("M4 socket cap")
    assert "HTTP Request" in caplog.text
    assert "REDACTED" in caplog.text
    assert "fake-key-sensitive" not in caplog.text


def test_farnell_documented_zero_count_without_product_array(monkeypatch):
    wire(monkeypatch, {"keywordSearchReturn": {"numberOfResults": 0}})
    assert FarnellAdapter().search("M4") == []


@pytest.mark.parametrize(
    ("reported", "expected"), [("$0.03", 0.03), ("€0.03", 0.03), ("€0,03", None)]
)
def test_mouser_formatted_prices_keep_reported_currency(
    monkeypatch, reported, expected
):
    wire(
        monkeypatch,
        {
            "SearchResults": {
                "Parts": [
                    mouser_part(
                        PriceBreaks=[
                            {"Quantity": 1, "Price": reported, "Currency": "EUR"}
                        ]
                    )
                ]
            }
        },
    )
    quote = MouserAdapter().search("10k resistor")[0]
    assert quote.currency == "EUR" and quote.unit_price == expected
