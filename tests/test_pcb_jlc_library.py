"""JLCPCB parts-library top-up (gr464537) — no network: a fake ``PostFn``."""

from __future__ import annotations

import pytest

from precis.pcb import jlc_library
from precis.pcb.catalog import min_unit_price

#: Trimmed real row (C25804, Basic) from the public listing endpoint.
C25804 = {
    "componentCode": "C25804",
    "componentLibraryType": "base",
    "preferredComponentFlag": False,
    "assemblyComponentFlag": False,
    "stockCount": 33071371,
    "componentPrices": [
        {"startNumber": 1, "endNumber": 999, "productPrice": 0.0022},
        {"startNumber": 50000, "endNumber": 50199, "productPrice": 0.0014},
        {"startNumber": 50200, "endNumber": -1, "productPrice": 0.0014},
    ],
    "componentBrandEn": "UNI-ROYAL(Uniroyal Elec)",
    "componentModelEn": "0603WAF1002T5E",
    "componentSpecificationEn": "0603",
    "describe": "100mW 10kΩ 75V Thick Film Resistor ±1% 0603",
    "dataManualUrl": "https://www.lcsc.com/datasheet/x_C25804.pdf",
    "firstSortName": "Chip Resistor - Surface Mount",
    "secondSortName": "Resistors",
    "attributes": [
        {"attribute_name_en": "Resistance", "attribute_value_name": "10kΩ"},
        {"attribute_name_en": "Tolerance", "attribute_value_name": "±1%"},
    ],
}


def test_normalize_c25804():
    n = jlc_library.normalize_library_row(C25804)
    assert n is not None
    assert n["lcsc"] == "C25804"
    assert n["basic"] is True and n["jlcpcb_assemblable"] is True
    assert n["stock"] == 33071371
    assert n["price"][0] == {"qFrom": 1, "qTo": 999, "price": 0.0022}
    assert n["price"][-1] == {"qFrom": 50200, "qTo": None, "price": 0.0014}
    assert min_unit_price(n["price"]) == 0.0014
    assert n["params"]["_category"] == "Resistors / Chip Resistor - Surface Mount"
    assert n["params"]["Resistance"] == "10kΩ"
    assert n["package"] == "0603" and n["mfr_part"] == "0603WAF1002T5E"
    assert n["mfr"] == "UNI-ROYAL(Uniroyal Elec)"
    assert n["datasheet_url"].endswith("C25804.pdf")


def test_normalize_preferred_and_junk():
    pref = {
        "componentCode": "C1",
        "componentLibraryType": "expand",
        "preferredComponentFlag": True,
        "componentPrices": [],
    }
    n = jlc_library.normalize_library_row(pref)
    assert n is not None
    assert n["basic"] is True and n["price"] is None and n["params"] is None
    assert n["stock"] == 0 and n["description"] == ""
    assert jlc_library.normalize_library_row({"componentCode": ""}) is None


def _r(code):
    return {"componentCode": code}


def _keyword_post(pages, calls=None):
    """``hasNextPage`` is always False, as on the real server."""

    def post(body):
        if calls is not None:
            calls.append(body)
        return {
            "data": {
                "componentPageInfo": {
                    "pages": len(pages),
                    "hasNextPage": False,
                    "list": pages[body["currentPage"] - 1],
                }
            }
        }

    return post


def test_keyword_rows_page_on_pages_not_has_next_page():
    calls: list[dict] = []
    post = _keyword_post([[_r("C1"), _r("C2")], [_r("C3")]], calls)
    codes = [
        r["componentCode"]
        for r in jlc_library.iter_keyword_rows("cap", post=post, max_pages=5)
    ]
    assert codes == ["C1", "C2", "C3"]
    assert [c["currentPage"] for c in calls] == [1, 2]
    assert all(c["keyword"] == "cap" for c in calls)


def test_keyword_rows_respect_max_pages():
    calls: list[dict] = []
    post = _keyword_post([[_r("C1")], [_r("C2")], [_r("C3")]], calls)
    rows = list(jlc_library.iter_keyword_rows("cap", post=post, max_pages=2))
    assert len(rows) == 2 and len(calls) == 2


def test_live_search_upserts_normalized_rows(store):
    post = _keyword_post([[C25804, {"componentCode": ""}]])
    rows = jlc_library.live_search(store, "10k 0603", post=post)
    assert [r["lcsc"] for r in rows] == ["C25804"]  # the junk row is dropped
    row = store.part_row("C25804")
    assert row is not None and row["basic"] is True


def test_live_search_empty_upserts_nothing(store):
    assert jlc_library.live_search(store, "zzz", post=_keyword_post([[]])) == []


def test_normalized_row_keeps_dump_only_fields(store):
    """The listing has no height and may lack a datasheet: the upsert must
    refresh stock/basic without blanking what the dump recorded."""
    store.parts_import(
        [
            {
                "lcsc": "C25804",
                "stock": 5,
                "basic": False,
                "height_mm": 0.45,
                "datasheet_url": "https://example.invalid/ds.pdf",
                "params": {"Resistance": "10kΩ"},
            }
        ]
    )
    drop = ("firstSortName", "secondSortName")
    thin = {k: v for k, v in C25804.items() if k not in drop}
    thin.update(dataManualUrl=None, attributes=[])
    jlc_library.live_search(store, "x", post=_keyword_post([[thin]]))
    row = store.part_row("C25804")
    assert row["basic"] is True and row["stock"] == C25804["stockCount"]
    assert row["datasheet_url"] == "https://example.invalid/ds.pdf"
    assert row["height_mm"] == pytest.approx(0.45)


def test_removed_walk_surface_is_gone():
    """gr464537: the website walk/top-up is retired -- only the keyword
    search (the Open API has none) remains here."""
    for name in (
        "walk_listing",
        "iter_library_rows",
        "refresh_library_parts",
        "live_lookup",
        "live_refresh_many",
        "reset_live_cache",
    ):
        assert not hasattr(jlc_library, name), name
