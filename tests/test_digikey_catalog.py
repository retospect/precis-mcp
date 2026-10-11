"""Digi-Key v4 category/filter contracts against fake transport only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

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
    # options, parametric, keyword, near-match (all empty here)
    assert len(bodies) == 4
    assert bodies[0]["FilterOptionsRequest"]["CategoryFilter"] == [{"Id": "572"}]
    param = bodies[1]["FilterOptionsRequest"]
    assert "CategoryFilter" not in param
    assert param["ParameterFilterRequest"]["ParameterFilters"][0]["FilterValues"] == [
        {"Id": "m4"}
    ]
    assert bodies[1]["Keywords"] == "socket head cap screw"
    assert bodies[2]["Keywords"] == "socket head cap screw M4X12"
    assert "ParameterFilterRequest" not in bodies[2]["FilterOptionsRequest"]
    assert bodies[3]["Keywords"] == "socket head cap screw M4"
    assert "category 572" in response.body


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


def _product(desc, mpn="P1"):
    return {
        "ManufacturerProductNumber": mpn,
        "Description": {"ProductDescription": desc},
        "QuantityAvailable": 5,
        "ProductVariations": [{"DigiKeyProductNumber": mpn + "-ND"}],
    }


def _ladder_quote(adapter, monkeypatch, responder, **kwargs):
    monkeypatch.setattr(base, "adapters", lambda: [adapter])
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        return responder(body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        result = base.quote(
            "socket head cap screw M4X12",
            category_id=572,
            parameters={"77": ["m4"]},
            ladder={
                "family": "socket head cap screw",
                "thread": "M4",
                "size": "M4X12",
            },
            **kwargs,
        )
    return result, bodies


def test_ladder_falls_through_parent_category_400(adapter, monkeypatch):
    def respond(body):
        if "ParameterFilterRequest" in body.get("FilterOptionsRequest", {}):
            return httpx.Response(
                400,
                json={
                    "detail": "Parametric Filtering Not Support at Parent Category Level."
                },
            )
        return httpx.Response(200, json={"Products": [_product("SCREW,CAP (M4X12)")]})

    result, bodies = _ladder_quote(adapter, monkeypatch, respond)
    assert len(bodies) == 2 and bodies[1]["Keywords"] == "socket head cap screw M4X12"
    assert result.outcomes[0].error is None and result.outcomes[0].count == 1
    assert result.quotes[0].match_confidence == "keyword"
    assert result.outcomes[0].parameters is None


def test_ladder_near_match_labelled_and_exact_filtered(adapter, monkeypatch):
    def respond(body):
        if body["Keywords"] == "socket head cap screw M4":
            return httpx.Response(
                200,
                json={
                    "Products": [
                        _product("SCREW (M4X16)", "A"),
                        _product("SCREW (M4X12)", "B"),
                    ]
                },
            )
        return httpx.Response(200, json={"Products": []})

    result, _ = _ladder_quote(adapter, monkeypatch, respond)
    assert [q.sku for q in result.quotes] == ["B-ND"]

    result, _ = _ladder_quote(
        adapter,
        monkeypatch,
        lambda body: httpx.Response(
            200,
            json={"Products": [_product("SCREW (M4X16)")]}
            if body["Keywords"] == "socket head cap screw M4"
            else {"Products": []},
        ),
    )
    assert result.quotes[0].description.startswith("[near match, not M4X12]")
    assert result.quotes[0].match_confidence == "keyword"


def test_nut_series_sends_no_category(adapter, monkeypatch):
    monkeypatch.setattr(base, "adapters", lambda: [adapter])
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"Products": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        stub: Any = SimpleNamespace()
        handler = ComponentHandler(hub=Hub(store=stub))
        handler._render_stock(
            SimpleNamespace(id="n", meta={"series": "iso-4032", "size": "M4"}),
            args=None,
        )
    assert len(bodies) == 1
    assert bodies[0]["Keywords"] == "hex nut M4"
    assert "FilterOptionsRequest" not in bodies[0]


_LADDER = {"family": "socket head cap screw", "thread": "M4", "size": "M4X12"}


def _criteria_quote(adapter, monkeypatch, responder, *, ladder, fastener):
    """quote() with ``criteria`` (no explicit parameters) and a stubbed filter lookup."""
    monkeypatch.setattr(base, "adapters", lambda: [adapter])
    monkeypatch.setattr(adapter, "fastener_parameters", fastener)
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        return responder(body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        result = base.quote(
            "socket head cap screw M4X12",
            category_id=572,
            criteria={"thread": "M4"},
            ladder=ladder,
        )
    return result, bodies


def _unavailable(*_a, **_k):
    raise FilterUnavailable("no such filter")


@pytest.mark.parametrize("status", [500, 403])
def test_ladder_parametric_non_400_reraises(adapter, monkeypatch, status):
    def respond(body):
        if "ParameterFilterRequest" in body.get("FilterOptionsRequest", {}):
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(200, json={"Products": [_product("SCREW (M4X12)")]})

    result, bodies = _ladder_quote(adapter, monkeypatch, respond)
    assert len(bodies) == 1  # no keyword rung attempted
    assert result.quotes == []
    assert f"HTTP {status} at search" in result.outcomes[0].error


def test_ladder_parametric_hit_returns_selected_without_keyword_rungs(
    adapter, monkeypatch
):
    result, bodies = _ladder_quote(
        adapter,
        monkeypatch,
        lambda body: httpx.Response(
            200, json={"Products": [_product("SCREW (M4X12)")]}
        ),
    )
    assert len(bodies) == 1
    assert "ParameterFilterRequest" in bodies[0]["FilterOptionsRequest"]
    assert result.outcomes[0].count == 1
    assert result.outcomes[0].parameters == {"77": ["m4"]}


def test_quote_filter_unavailable_with_ladder_falls_to_keyword(adapter, monkeypatch):
    result, bodies = _criteria_quote(
        adapter,
        monkeypatch,
        lambda body: httpx.Response(
            200, json={"Products": [_product("SCREW (M4X12)")]}
        ),
        ladder=_LADDER,
        fastener=_unavailable,
    )
    assert len(bodies) == 1
    assert "ParameterFilterRequest" not in bodies[0].get("FilterOptionsRequest", {})
    assert bodies[0]["Keywords"] == "socket head cap screw M4X12"
    assert result.outcomes[0].error is None and result.outcomes[0].count == 1
    assert result.outcomes[0].parameters is None


def test_quote_filter_unavailable_without_ladder_becomes_outcome_error(
    adapter, monkeypatch
):
    result, bodies = _criteria_quote(
        adapter,
        monkeypatch,
        lambda body: httpx.Response(200, json={"Products": []}),
        ladder=None,
        fastener=_unavailable,
    )
    assert bodies == [] and result.quotes == []
    assert result.outcomes[0].error == "no such filter"


_HEX = {
    "family": "hex head bolt",
    "thread": "M4",
    "size": "M4X12",
    "match_words": ["HEX HEAD", "HEX BOLT"],
}


def _marker_quote(adapter, monkeypatch, responder):
    monkeypatch.setattr(base, "adapters", lambda: [adapter])
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        return responder(body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr("precis.supply.digikey.httpx.post", client.post)
        result = base.quote(
            "hex head bolt M4X12", category_id=572, limit=5, ladder=_HEX
        )
    return result, bodies


def test_ladder_rung2_wrong_head_form_is_not_the_match(adapter, monkeypatch):
    pan = _product("MACH SCREW PAN PHILLIPS M4X0.7", "PAN")
    result, bodies = _marker_quote(
        adapter, monkeypatch, lambda body: httpx.Response(200, json={"Products": [pan]})
    )
    assert len(bodies) == 2  # fell through to rung 3
    assert [q.sku for q in result.quotes] == ["PAN-ND"]
    assert result.quotes[0].description.startswith("[near match, not hex head bolt]")


def test_ladder_rung2_marker_hit_kept_case_insensitive(adapter, monkeypatch):
    products = [
        _product("MACH SCREW PAN PHILLIPS M4X0.7", "PAN"),
        _product("Screw, Hex Head (M4X12)", "HEX"),
    ]
    result, bodies = _marker_quote(
        adapter,
        monkeypatch,
        lambda body: httpx.Response(200, json={"Products": products}),
    )
    assert len(bodies) == 1
    assert [q.sku for q in result.quotes] == ["HEX-ND"]


def test_ladder_rung3_needs_size_and_marker_and_requests_limit_50(adapter, monkeypatch):
    def respond(body):
        if body["Keywords"] == "hex head bolt M4":
            return httpx.Response(
                200,
                json={
                    "Products": [
                        _product("PAN SCREW (M4X12)", "PAN"),
                        _product("HEX HEAD (M4X16)", "LONG"),
                        _product("HEX HEAD (M4X12)", "OK"),
                    ]
                },
            )
        return httpx.Response(200, json={"Products": []})

    result, bodies = _marker_quote(adapter, monkeypatch, respond)
    assert [q.sku for q in result.quotes] == ["OK-ND"]
    wide = [b for b in bodies if b["Keywords"] == "hex head bolt M4"]
    assert len(wide) == 1 and wide[0]["Limit"] == 50


def test_ladder_rung3_near_matches_say_what_failed(adapter, monkeypatch):
    def respond(body):
        if body["Keywords"] == "hex head bolt M4":
            return httpx.Response(
                200,
                json={
                    "Products": [
                        _product("PAN SCREW (M4X12)", "PAN"),
                        _product("HEX HEAD (M4X16)", "LONG"),
                    ]
                },
            )
        return httpx.Response(200, json={"Products": []})

    result, _ = _marker_quote(adapter, monkeypatch, respond)
    desc = {q.sku: q.description for q in result.quotes}
    assert desc["PAN-ND"].startswith("[near match, not hex head bolt]")
    assert desc["LONG-ND"].startswith("[near match, not M4X12]")


def test_digikey_series_keywords_and_match_words():
    for sid, kw in [
        ("iso-10642", "flat head socket"),
        ("iso-4026", "set screw"),
        ("iso-4017", "hex head bolt"),
        ("iso-14579", "torx"),
        ("iso-7089", "flat washer"),
        ("iso-7090", "flat washer"),
        ("iso-7040", "nylon lock nut"),
    ]:
        mapping = series_mapping(sid)
        assert mapping is not None
        assert mapping["keywords"][sid] == kw
    for sid in ("iso-7089", "iso-7090", "iso-7040"):
        mapping = series_mapping(sid)
        assert mapping is not None
        assert mapping.get("stock_category", True) is False
    torx = series_mapping("iso-14586")
    countersunk = series_mapping("iso-10642")
    assert torx is not None and countersunk is not None
    assert torx["match_words"]["iso-14586"] == ["TORX"]
    assert countersunk["match_words"]["iso-10642"] == [
        "COUNTERSUNK",
        "FLAT HEAD",
    ]


def test_stock_view_passes_match_words_to_ladder(adapter, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_quote(query, **kw):
        seen.update(kw, query=query)
        return base.QuoteResult(quotes=[], outcomes=[])

    monkeypatch.setattr("precis.supply.quote", fake_quote)
    monkeypatch.setattr("precis.supply.unavailable_reason", lambda: None)
    handler = ComponentHandler(hub=Hub(store=cast(Any, SimpleNamespace())))
    handler._render_stock(
        SimpleNamespace(id="n", meta={"series": "iso-10642", "size": "M4x12"}),
        args=None,
    )
    assert seen["query"] == "flat head socket M4X12"
    assert seen["ladder"]["match_words"] == ["COUNTERSUNK", "FLAT HEAD"]
