"""Digi-Key v4 category/filter contracts against fake transport only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.component import ComponentHandler
from precis.supply import base
from precis.supply.catalog import FilterUnavailable, metric_parameters, series_mapping
from precis.supply.digikey import DigiKeyAdapter


@pytest.fixture
def adapter(monkeypatch):
    instance = DigiKeyAdapter()
    monkeypatch.setattr(instance, "_access_token", lambda: "fake-token")
    monkeypatch.setattr(instance, "_credentials", lambda: ("fake-id", "fake-secret"))
    return instance


def test_category_uncached_stamp_copy_and_detail(adapter, monkeypatch):
    tick = [100.0]
    monkeypatch.setattr("precis.supply.digikey.time.monotonic", lambda: tick[0])
    calls = []
    root: dict[str, Any] = {
        "CategoryId": 26,
        "ParentId": 0,
        "Name": "Hardware",
        "ProductCount": 123,
        "Children": [
            {
                "CategoryId": 572,
                "ParentId": 26,
                "Name": "Screws, Bolts",
                "ProductCount": 100,
                "Children": [],
            }
        ],
    }

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={"Category": root["Children"][0]}
            if str(request.url).endswith("/572")
            else {"Categories": [root]},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.get", client.get)
        first = adapter.categories()
        first.categories[0]["Name"] = "tampered"
        second = adapter.categories()
        assert second.categories[0]["Name"] == "Hardware"
        assert second.retrieved.tzinfo is not None
        assert len(calls) == 2
        detail = adapter.categories(572)
        assert detail.categories[0]["ParentId"] == 26
        tick[0] += 7 * 86400 + 1
        adapter.categories()
        assert len(calls) == 4
    assert calls[2].endswith("/search/categories/572")


def test_category_http_error_is_not_cached(adapter, monkeypatch):
    from precis.supply.base import SupplierError

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "fake-secret"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.get", client.get)
        for _ in range(2):
            with pytest.raises(SupplierError, match="HTTP 403 at categories"):
                adapter.categories()
    assert len(calls) == 2


def test_keyword_filter_wire_shape(adapter, monkeypatch):
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"Products": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        adapter.search(
            "socket cap", category_id=572, parameters={"77": ["v-m4"], "88": ["v-12mm"]}
        )
    assert bodies == [
        {
            "Keywords": "socket cap",
            "Limit": 5,
            "FilterOptionsRequest": {
                "CategoryFilter": [{"Id": "572"}],
                "ParameterFilterRequest": {
                    "CategoryFilter": {"Id": "572"},
                    "ParameterFilters": [
                        {"ParameterId": 77, "FilterValues": [{"Id": "v-m4"}]},
                        {"ParameterId": 88, "FilterValues": [{"Id": "v-12mm"}]},
                    ],
                },
            },
        }
    ]


@pytest.mark.parametrize(
    ("category", "parameters"),
    [
        (0, None),
        (True, None),
        ("572", None),
        (None, {"1": ["x"]}),
        (572, {"x": ["x"]}),
        (572, {"1": []}),
        (572, {"1": [False]}),
    ],
)
def test_invalid_filters_refuse_before_transport(
    adapter, monkeypatch, category, parameters
):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid filters made an HTTP call")

    monkeypatch.setattr("precis.supply.digikey.httpx.post", unexpected)
    with pytest.raises(ValueError):
        adapter.search("x", category_id=category, parameters=parameters)


def _options():
    return [
        {
            "Category": {"Id": 572},
            "ParameterId": 77,
            "ParameterName": "Thread Size",
            "FilterValues": [
                {"ValueId": "wrong-pitch", "ValueName": "M4x0.5"},
                {"ValueId": "m4", "ValueName": "M4x0.7"},
            ],
        },
        {
            "Category": {"Id": 572},
            "ParameterId": 88,
            "ParameterName": "Length - Below Head",
            "FilterValues": [
                {"ValueId": "12", "ValueName": '0.472" (12.00mm)'},
                {"ValueId": "120", "ValueName": "120.00mm"},
            ],
        },
    ]


def test_metric_parameters_do_not_fuzzy_match_or_broaden():
    assert metric_parameters(
        _options(), thread="M4", pitch=0.7, length=12, category_id=572
    ) == {"77": ["m4"], "88": ["12"]}
    with pytest.raises(FilterUnavailable, match="thread value unavailable"):
        metric_parameters(
            _options(), thread="M40", pitch=0.7, length=12, category_id=572
        )
    with pytest.raises(FilterUnavailable, match="length value unavailable"):
        metric_parameters(
            _options(), thread="M4", pitch=0.7, length=1.2, category_id=572
        )


@pytest.mark.parametrize("args", [None, {"category_id": None}, {"parameters": None}])
def test_mapped_stock_resolves_options_and_applies_thread_length(
    adapter, monkeypatch, args
):
    monkeypatch.setattr(base, "adapters", lambda: [adapter])
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={"Products": [], "FilterOptions": {"ParametricFilters": _options()}},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        stub: Any = SimpleNamespace()
        handler = ComponentHandler(hub=Hub(store=stub))
        response = handler._render_stock(
            SimpleNamespace(id="fixture", meta={"series": "iso-4762", "size": "M4x12"}),
            args=args,
        )
    assert len(bodies) == 2
    assert bodies[0]["FilterOptionsRequest"]["CategoryFilter"] == [{"Id": "572"}]
    assert bodies[1]["FilterOptionsRequest"]["ParameterFilterRequest"][
        "ParameterFilters"
    ][0]["FilterValues"] == [{"Id": "m4"}]
    assert "parameters={'77': ['m4'], '88': ['12']}" in response.body


def test_series_mapping_sources_and_unverified_family():
    for series, category in [("iso-4762", 572), ("iso-4032", 573), ("iso-7090", 571)]:
        mapping = series_mapping(series)
        assert mapping is not None
        assert mapping["category_id"] == category and mapping["source"].startswith(
            "https://www.digikey.com/"
        )
    assert series_mapping("insert-brass-heatset") is None


def test_supplier_navigation_roots_children_query_and_bad_id(adapter, monkeypatch):
    monkeypatch.setattr("precis.supply.digikey.DigiKeyAdapter", lambda: adapter)
    roots = [
        {
            "CategoryId": 26,
            "Name": "Hardware",
            "ProductCount": 100,
            "Children": [
                {
                    "CategoryId": 572,
                    "Name": "Screws, Bolts",
                    "ProductCount": 90,
                    "Children": [],
                }
            ],
        }
    ]

    def handle(request):
        return httpx.Response(200, json={"Categories": roots, "Category": roots[0]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.get", client.get)
        stub: Any = SimpleNamespace()
        handler = ComponentHandler(hub=Hub(store=stub))
        response = handler.get(view="supplier-categories")
        assert "Hardware" in response.body
        assert "live, uncached, not stored" in response.body
        assert "cached up to" not in response.body
        assert response.transient
        assert "Screws, Bolts" in handler.get(view="supplier-categories", id=26).body
        body = handler.get(view="supplier-categories", q="screws").body
        assert "Hardware > Screws, Bolts" in body and "90" in body
        with pytest.raises(BadInput):
            handler.get(view="supplier-categories", id="component-slug")
