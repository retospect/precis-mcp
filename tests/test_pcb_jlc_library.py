"""JLCPCB parts-library top-up (gr464537) — no network: a fake ``PostFn``."""

from __future__ import annotations

import pytest

from precis.pcb import jlc_library
from precis.pcb._http import VendorError
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


def _fake_post(pages_by_filter, calls=None):
    """``hasNextPage`` is always False, as on the real server."""

    def post(body):
        if calls is not None:
            calls.append(body)
        key = "base" if "componentLibraryType" in body else "pref"
        pages = pages_by_filter[key]
        return {
            "data": {
                "componentPageInfo": {
                    "total": 0,
                    "pages": len(pages),
                    "hasNextPage": False,
                    "list": pages[body["currentPage"] - 1],
                }
            }
        }

    return post


def _r(code):
    return {"componentCode": code}


def test_iter_paginates_on_pages_and_dedupes():
    calls: list[dict] = []
    post = _fake_post(
        {
            "base": [[_r("C1"), _r("C2")], [_r("C3")]],
            "pref": [[_r("C3"), _r("C4")]],
        },
        calls,
    )
    codes = [r["componentCode"] for r in jlc_library.iter_library_rows(post)]
    assert codes == ["C1", "C2", "C3", "C4"]
    assert [c["currentPage"] for c in calls] == [1, 2, 1]
    assert calls[0]["componentLibraryType"] == "base"
    assert calls[2]["preferredComponentFlag"] is True


def test_iter_respects_max_pages(monkeypatch):
    monkeypatch.setattr(jlc_library, "MAX_PAGES", 3)
    monkeypatch.setattr(
        jlc_library, "LIBRARY_FILTERS", ({"componentLibraryType": "base"},)
    )
    calls = []

    def post(body):
        calls.append(body)
        n = body["currentPage"]
        return {"data": {"componentPageInfo": {"pages": 99, "list": [_r(f"C{n}")]}}}

    rows = list(jlc_library.iter_library_rows(post))
    assert len(rows) == 3 and len(calls) == 3


def test_refresh_empty_is_a_failure(store):
    post = _fake_post({"base": [[]], "pref": [[]]})
    with pytest.raises(VendorError, match="returned no rows"):
        jlc_library.refresh_library_parts(store, post=post)


def test_refresh_upserts_rows(store):
    post = _fake_post({"base": [[C25804]], "pref": [[]]})
    counts = jlc_library.refresh_library_parts(store, post=post)
    assert counts["rows"] == 1 and counts["upserted"] == 1
    row = store.part_row("C25804")
    assert row is not None and row["basic"] is True


# -- worker pass -------------------------------------------------------
class _Client:
    last_key = None

    def __init__(self, *, available=True, error=None):
        self.available = available
        self._error = error

    def iter_components(self, *, since_key=None, page_size=100):
        del since_key, page_size
        if self._error is not None:
            raise self._error
        yield {"lcsc": "C90100", "stock": 1}


def _lib_ok(store):
    return {"rows": 2, "upserted": 2, "restocked": 0}


def _lib_fail(store):
    raise VendorError("jlcpcb-web", "HTTP 503")


def _sev(store, fp):
    from precis.alerts import open_alert_severity

    return open_alert_severity(store, source="parts_refresh", fingerprint=fp)


def test_pass_runs_library_when_api_is_dark(store):
    from precis.workers.parts_refresh import run_parts_refresh_pass

    r = run_parts_refresh_pass(store, client=_Client(available=False), library=_lib_ok)
    assert r == {"claimed": 0, "ok": 0, "failed": 0, "library": 2}


def test_pass_library_failure_raises_alert(store):
    from precis.workers.parts_refresh import run_parts_refresh_pass

    r = run_parts_refresh_pass(
        store, client=_Client(available=False), library=_lib_fail
    )
    assert r["failed"] == 1 and r["library"] == 0 and "HTTP 503" in r["library_error"]
    assert _sev(store, "jlcpcb-library") is not None
    assert _sev(store, "jlcpcb-api") is None


def test_pass_alert_lifecycle_per_fingerprint(store):
    from precis.pcb.jlc_api import JlcPermissionError
    from precis.workers.parts_refresh import run_parts_refresh_pass

    api_bad = _Client(error=JlcPermissionError("jlcpcb", "no scope", status=403))
    run_parts_refresh_pass(store, client=api_bad, library=_lib_fail)
    assert _sev(store, "jlcpcb-api") is not None
    assert _sev(store, "jlcpcb-library") is not None

    r = run_parts_refresh_pass(store, client=_Client(), library=_lib_fail)
    assert r["failed"] == 1 and r["ok"] == 1
    assert _sev(store, "jlcpcb-api") is None
    assert _sev(store, "jlcpcb-library") is not None

    r = run_parts_refresh_pass(store, client=_Client(), library=_lib_ok)
    assert r["failed"] == 0 and r["library"] == 2
    assert _sev(store, "jlcpcb-library") is None


def test_pass_dark_leaves_open_api_alert(store):
    from precis.pcb.jlc_api import JlcPermissionError
    from precis.workers.parts_refresh import run_parts_refresh_pass

    api_bad = _Client(error=JlcPermissionError("jlcpcb", "no scope", status=403))
    run_parts_refresh_pass(store, client=api_bad, library=_lib_ok)
    assert _sev(store, "jlcpcb-api") is not None
    # Dark skips the walk: neither confirms nor clears the API alert.
    run_parts_refresh_pass(store, client=_Client(available=False), library=_lib_ok)
    assert _sev(store, "jlcpcb-api") is not None


def test_refresh_wraps_non_vendor_fetch_errors(store):
    """SsrfBlocked (DNS blip at connect) is a plain Exception; it must surface
    as VendorError so the pass alerts instead of crashing the API walk."""
    from precis.utils.safe_fetch import SsrfBlocked

    def post(body):
        raise SsrfBlocked("jlcpcb.com did not resolve")

    with pytest.raises(VendorError, match="parts-library fetch failed"):
        jlc_library.refresh_library_parts(store, post=post)


def test_library_upsert_keeps_dump_only_fields(store):
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
    post = _fake_post({"base": [[thin]], "pref": [[]]})
    jlc_library.refresh_library_parts(store, post=post)
    row = store.part_row("C25804")
    assert row["basic"] is True and row["stock"] == C25804["stockCount"]
    assert row["datasheet_url"] == "https://example.invalid/ds.pdf"
    assert row["height_mm"] == pytest.approx(0.45)
