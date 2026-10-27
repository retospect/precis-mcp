"""Live single-part fallback on a catalog miss (gr485250).

``get(kind='part', id='C…')`` on a part the jlcparts dump + library top-up
never carried asks the JLCPCB Open API (``getComponentDetailByCode`` via
:func:`precis.pcb.jlc_api.live_lookup`), upserts via ``parts_import`` and
returns the row; free-text search still uses the keyword listing
(:func:`precis.pcb.jlc_library.live_search`). No network here: the
``post=`` seam feeds canned listing envelopes and an injected
``JlcApiClient(send=...)`` feeds canned detail envelopes.
"""

from __future__ import annotations

import json
import typing
from typing import Any

import httpx
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers.part import PartHandler
from precis.pcb import catalog, jlc_api, jlc_library
from precis.pcb._http import VendorError

#: Trimmed real row: keyword 'C2655145' → AS7341 DLGT (expand, stock 492).
C2655145 = {
    "componentCode": "C2655145",
    "componentLibraryType": "expand",
    "preferredComponentFlag": False,
    "stockCount": 492,
    "componentPrices": [
        {"startNumber": 1, "endNumber": 9, "productPrice": 7.9527},
        {"startNumber": 30, "endNumber": 99, "productPrice": 6.1518},
        {"startNumber": 104, "endNumber": -1, "productPrice": 5.5808},
    ],
    "componentBrandEn": "AMS",
    "componentModelEn": "AS7341-DLGT",
    "componentSpecificationEn": "OLGA-8(2x3.1)",
    "describe": "-30℃~+70℃ I2C OLGA-8(2x3.1) Ambient Light Sensors ROHS",
    "dataManualUrl": "https://www.lcsc.com/datasheet/x_C2655145.pdf",
    "lcscGoodsUrl": "https://www.lcsc.com/product-detail/_C2655145.html",
    "firstSortName": "Ambient Light Sensors",
    "secondSortName": "Sensors",
    "attributes": [
        {"attribute_name_en": "Voltage - Supply", "attribute_value_name": "1.7V~2V"}
    ],
}

#: What a never-issued C-number actually gets back: fuzzy neighbours.
_NEIGHBOUR = {
    "componentCode": "C1396897",
    "componentLibraryType": "expand",
    "componentModelEn": "RN60C9992FB14",
    "stockCount": 0,
}


def _post_with(rows, *, pages=1, calls=None):
    def post(body):
        if calls is not None:
            calls.append(dict(body))
        page = body.get("currentPage", 1)
        return {
            "data": {
                "componentPageInfo": {
                    "total": len(rows) * pages,
                    "pages": pages,
                    "hasNextPage": False,
                    "list": rows if page == 1 else [],
                }
            }
        }

    return post


#: The same part as the Open API's getComponentDetailByCode returns it.
API_C2655145 = {
    "componentCode": "C2655145",
    "manufacturer": "AMS",
    "mfrPartNumber": "AS7341-DLGT",
    "description": "Ambient Light Sensors ROHS",
    "basic": False,
    "stockCount": 492,
    "componentSpecificationEn": "OLGA-8(2x3.1)",
}


@pytest.fixture(autouse=True)
def _fresh_live_cache():
    jlc_api.reset_live_cache()
    yield
    jlc_api.reset_live_cache()


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, "test-secret-key")


def _api_client(rows_by_code, calls=None, *, status=200):
    """A credentialed ``JlcApiClient`` whose wire is a fake: each request's
    ``componentCodes`` are answered from ``rows_by_code`` (absent -> empty
    list = a real miss), or with ``status`` when it isn't 200."""

    def send(method, path, headers, body):
        codes = json.loads(body)["componentCodes"]
        if calls is not None:
            calls.extend(codes)
        if status != 200:
            return httpx.Response(status, text="boom")
        rows = [rows_by_code[c] for c in codes if c in rows_by_code]
        return httpx.Response(
            200, json={"data": {"componentDetailResponseVOList": rows}}
        )

    return jlc_api.JlcApiClient(
        send=send, sleep=lambda _s: None, pace=lambda _name: None
    )


def test_lcsc_id_forms():
    assert catalog.lcsc_id("C2655145") == "C2655145"
    assert catalog.lcsc_id("c79616") == "C79616"
    assert catalog.lcsc_id(" C5358484 ") == "C5358484"
    assert (
        catalog.lcsc_id(
            "https://www.lcsc.com/product-detail/ambient-light-sensors_ams-as7341-dlgt_C2655145.html"
        )
        == "C2655145"
    )
    assert (
        catalog.lcsc_id("https://jlcpcb.com/partdetail/AMS-AS7341DLGT/C2655145")
        == "C2655145"
    )
    assert (
        catalog.lcsc_id("https://jlcpcb.com/parts/componentSearch?searchTxt=C2655145")
        == "C2655145"
    )
    assert catalog.lcsc_id("RN60C9992FB14") is None  # mid-token is not an id
    assert catalog.lcsc_id("ESP32-C3") is None  # too few digits
    assert catalog.lcsc_id("banana") is None
    assert catalog.lcsc_id("") is None


def test_live_lookup_exact_match_upserts(store, creds):
    calls: list[str] = []
    client = _api_client({"C2655145": API_C2655145}, calls)
    row = jlc_api.live_lookup(store, "c2655145", client=client)
    assert row is not None and row["lcsc"] == "C2655145"
    assert row["basic"] is False
    assert row["stock"] == 492
    got = store.part_row("C2655145")
    assert got is not None and got["mfr_part"] == "AS7341-DLGT"
    assert calls == ["C2655145"]


def test_live_lookup_miss_is_negative_cached(store, creds):
    calls: list[str] = []
    client = _api_client({}, calls)
    assert jlc_api.live_lookup(store, "C99999999", client=client) is None
    assert jlc_api.live_lookup(store, "C99999999", client=client) is None
    assert calls == ["C99999999"]  # second call served by the miss cache
    assert store.part_row("C99999999") is None


def test_live_lookup_without_credentials_raises_uncached(store, monkeypatch):
    for name in (
        jlc_api.APP_ID_SECRET,
        jlc_api.ACCESS_KEY_SECRET,
        jlc_api.SECRET_KEY_SECRET,
    ):
        monkeypatch.delenv(name, raising=False)

    def send(*_a):
        raise AssertionError("no-credential client must not touch the wire")

    client = jlc_api.JlcApiClient(send=send)
    assert not client.available
    for _ in range(2):  # a missing key is not a miss: never negative-cached
        with pytest.raises(VendorError, match="no JLCPCB API credentials"):
            jlc_api.live_lookup(store, "C2655145", client=client)
    assert "C2655145" not in jlc_api._live_misses


def test_live_lookup_error_propagates_and_is_not_cached(store, creds):
    client = _api_client({}, status=500)
    with pytest.raises(VendorError):
        jlc_api.live_lookup(store, "C2655145", client=client)
    assert "C2655145" not in jlc_api._live_misses


def test_live_refresh_many_maps_hit_miss_error(store, creds):
    client = _api_client({"C2655145": API_C2655145})
    out = jlc_api.live_refresh_many(store, ["C2655145", "C99999999"], client=client)
    assert out == {"C2655145": "hit", "C99999999": "miss"}
    bad = _api_client({}, status=500)
    out = jlc_api.live_refresh_many(store, ["C1"], client=bad)
    assert out["C1"].startswith("error: ")


def test_live_refresh_many_without_credentials_reports_errors(store, monkeypatch):
    for name in (
        jlc_api.APP_ID_SECRET,
        jlc_api.ACCESS_KEY_SECRET,
        jlc_api.SECRET_KEY_SECRET,
    ):
        monkeypatch.delenv(name, raising=False)
    out = jlc_api.live_refresh_many(store, ["C1"], client=jlc_api.JlcApiClient())
    assert out["C1"].startswith("error: ")


def test_live_refresh_many_honours_limit(store, creds):
    calls: list[str] = []
    client = _api_client({}, calls)
    codes = [f"C9{i:07d}" for i in range(5)]
    out = jlc_api.live_refresh_many(store, codes, client=client, limit=3)
    assert list(out) == codes[:3]
    assert calls == codes[:3]
    assert jlc_api._LIVE_BATCH_MAX == 12


def test_get_falls_back_to_live(store, creds):
    client = _api_client({"C2655145": API_C2655145})
    part = PartHandler(
        hub=Hub(store=store),
        live_lookup=lambda s, c: jlc_api.live_lookup(s, c, client=client),
    )
    body = part.get(id="C2655145").body
    assert "C2655145" in body and "AS7341-DLGT" in body
    assert store.part_row("C2655145") is not None  # the hit was catalogued


def test_get_real_miss_names_the_live_check(store, creds):
    client = _api_client({})
    part = PartHandler(
        hub=Hub(store=store),
        live_lookup=lambda s, c: jlc_api.live_lookup(s, c, client=client),
    )
    with pytest.raises(NotFound, match="C99999999") as exc:
        part.get(id="C99999999")
    assert "live" in (exc.value.next or [""])[0]


def test_get_live_outage_degrades_to_named_failure(store):
    def boom(store_, lcsc):
        raise VendorError("jlcpcb-web", "HTTP 503 from listing")

    part = PartHandler(hub=Hub(store=store), live_lookup=boom)
    with pytest.raises(NotFound, match="C2655145") as exc:
        part.get(id="C2655145")
    nxt = exc.value.next
    hints = [nxt] if isinstance(nxt, str) else (nxt or [])
    assert "503" in " ".join(hints)


def test_get_non_c_number_never_calls_live(store):
    called = []

    def live(store_, lcsc):
        called.append(lcsc)
        return None

    part = PartHandler(hub=Hub(store=store), live_lookup=live)
    with pytest.raises(NotFound):
        part.get(id="not-a-part")
    assert called == []


# ── search fallback ─────────────────────────────────────────────────


def test_live_search_upserts_every_row(store):
    post = _post_with([_NEIGHBOUR, C2655145])
    rows = jlc_library.live_search(store, "as7341", post=post)
    assert [r["lcsc"] for r in rows] == ["C1396897", "C2655145"]
    assert store.part_row("C1396897") is not None
    assert store.part_row("C2655145") is not None


def test_search_live_fallback_marks_results(store):
    post = _post_with([C2655145])
    part = PartHandler(
        hub=Hub(store=store),
        live_search=lambda s, q, page_size=25: jlc_library.live_search(
            s, q, post=post, page_size=page_size
        ),
    )
    body = part.search(q="AS7341 sensor").body
    assert "LIVE" in body and "C2655145" in body
    assert store.part_row("C2655145") is not None


def test_search_c_number_is_a_point_lookup(store):
    """A C-number q hits ``part_row`` directly — the selector's
    description-tsv would never match it — without touching the network."""
    store.parts_import(
        [
            {
                "lcsc": "C2655145",
                "mfr_part": "AS7341-DLGT",
                "description": "ambient light sensor",  # no C-number in text
                "jlcpcb_assemblable": True,
                "stock": 492,
            }
        ]
    )
    called = []

    def live_search(*_a, **_k):
        called.append(1)
        return []

    part = PartHandler(hub=Hub(store=store), live_search=live_search)
    body = part.search(q="C2655145").body
    assert "C2655145" in body and "AS7341-DLGT" in body
    assert called == []


def test_search_partial_match_ranks_in_stock_first_and_skips_live(store):
    """gr495293: 'receptacle'/'16pin' are not in the catalog description
    ('16P'), so the all-terms query is empty; the OR fallback finds the
    connector, in-stock before an out-of-stock junk row, without the live
    call that used to answer with stock-0 listings."""
    desc = "16P 5A Female Surface Mount, Right Angle Type-C SMD USB Connectors"
    store.parts_import(
        [
            {
                "lcsc": "C9900091898",
                "mfr_part": "USB_C_Receptacle",
                "description": "USB-C-SMD_TYPE-C New Arrivals",
                "jlcpcb_assemblable": True,
                "stock": 0,
            },
            {
                "lcsc": "C165948",
                "mfr_part": "TYPE-C-31-M-12",
                "description": desc,
                "jlcpcb_assemblable": True,
                "stock": 365713,
            },
        ]
    )
    called = []

    def live_search(*_a, **_k):
        called.append(1)
        return []

    part = PartHandler(hub=Hub(store=store), live_search=live_search)
    body = part.search(q="USB Type-C 16pin receptacle SMD").body
    assert "PARTIAL" in body
    assert body.index("C165948") < body.index("C9900091898")
    assert called == []
    strict = part.search(q="USB Type-C SMD").body
    assert "PARTIAL" not in strict
    assert strict.index("C165948") < strict.index("C9900091898")


def test_search_no_buy_row_shows_no_price(store):
    """gr519681: a stored ``params._lifecycle.no_buy`` row's ladder is a
    placeholder (identical ladders repeat across unrelated C99xxxxxxx
    ICs at stock 0) — the row renders ``—`` for ``$ea``, not the
    number."""
    store.parts_import(
        [
            {
                "lcsc": "C9900257678",
                "mfr_part": "NRF54L15",
                "description": "nRF54L15 wireless SoC",
                "jlcpcb_assemblable": True,
                "stock": 0,
                "price": [{"qFrom": 1, "qTo": None, "price": 0.0205}],
                "params": {
                    "_lifecycle": {"no_buy": "This product is no longer manufactured."}
                },
            },
            {
                "lcsc": "C25804",
                "mfr_part": "0603WAF1002T5E",
                "description": "nRF54L15 wireless SoC companion",
                "jlcpcb_assemblable": True,
                "stock": 9,
                "price": [{"qFrom": 1, "qTo": None, "price": 0.01}],
            },
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="nRF54L15 wireless").body
    assert "C9900257678" in body
    assert "0.0205" not in body
    assert "0.01" in body  # the priced sibling still shows


def test_search_partial_drops_rows_matching_no_type_term(store):
    """gr519680: 'solar cell SMD' — the OR fallback surfaced in-stock
    rows that matched 'smd' alone (tactile switches, caps, buzzers).
    A PARTIAL hit must now match a part-type term; a query whose type
    the catalog doesn't hold falls through to the live listing instead
    of answering with junk."""
    store.parts_import(
        [
            {
                "lcsc": "C720001",
                "mfr_part": "TS-1101",
                "description": "Tactile Switch SMD 6x6mm 50mA",
                "jlcpcb_assemblable": True,
                "stock": 50000,
            },
            {
                "lcsc": "C720002",
                "mfr_part": "KXOB25-05X3F",
                "description": "SMD photovoltaic module 2.5V",
                "jlcpcb_assemblable": True,
                "stock": 10,
            },
            {
                "lcsc": "C720003",
                "mfr_part": "BZ-3V",
                "description": "Buzzer SMD 3V passive 0.47g",
                "jlcpcb_assemblable": True,
                "stock": 9000,
            },
        ]
    )
    called = []

    def live_search(*_a, **_k):
        called.append(1)
        return []

    part = PartHandler(hub=Hub(store=store), live_search=live_search)
    body = part.search(q="solar cell SMD").body
    assert "PARTIAL" in body
    # 'photovoltaic' in the description counts as 'solar' (synonym).
    assert "C720002" in body
    # The 'smd'-only matches are gone.
    assert "C720001" not in body and "C720003" not in body
    assert called == []

    # The catalog holds no supercap: instead of the buzzer (which
    # OR-matched 'smd' / '0.47' / 'v') the live listing is asked.
    body = part.search(q="supercapacitor 5.5V 0.47F SMD").body
    assert called == [1]
    assert "no assemblable parts match" in body


def test_search_partial_synonym_matches_super_capacitor(store):
    """gr519680: a part filed as 'Super Capacitor' answers a
    'supercapacitor' query — the two-word spelling is a required-set
    alternate, not a miss."""
    store.parts_import(
        [
            {
                "lcsc": "C720010",
                "mfr_part": "FC0V474ZFTBR24",
                "description": "Super Capacitor 0.47F 5.5V SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
            }
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="supercapacitor 0.47F SMD").body
    assert "PARTIAL" in body
    assert "C720010" in body


def test_search_category_filter(store):
    """gr519680: ``category=`` substring-matches the jlcparts category
    carried in ``params._category`` — connectors stop leaking into a
    capacitor ask."""
    store.parts_import(
        [
            {
                "lcsc": "C730001",
                "mfr_part": "CL21B106KAYQNNE",
                "description": "10uF 25V X7R 0805 SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
                "params": {"_category": "Capacitors / Multilayer Ceramic"},
            },
            {
                "lcsc": "C730002",
                "mfr_part": "USB-R-10",
                "description": "10uF connector housing SMD",
                "jlcpcb_assemblable": True,
                "stock": 9999,
                "params": {"_category": "Connectors / USB"},
            },
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="10uF SMD", category="Capacitors").body
    assert "C730001" in body
    assert "C730002" not in body


def test_search_category_escapes_like_metachars(store):
    """``category`` is a literal substring — ``%``/``_`` in the ask are
    escaped, so they can't widen into a LIKE pattern."""
    store.parts_import(
        [
            {
                "lcsc": "C730010",
                "mfr_part": "MLCC-PCT",
                "description": "10uF 25V X7R 0805 SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
                "params": {"_category": "Capacitors 10%0 derated"},
            },
            {
                "lcsc": "C730011",
                "mfr_part": "MLCC-WILD",
                "description": "10uF 25V X7R 0805 SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
                "params": {"_category": "Capacitors 10xyz0"},
            },
            {
                "lcsc": "C730012",
                "mfr_part": "RES-UND",
                "description": "10uF 25V X7R 0805 SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
                "params": {"_category": "Res_A"},
            },
            {
                "lcsc": "C730013",
                "mfr_part": "RES-ANY",
                "description": "10uF 25V X7R 0805 SMD",
                "jlcpcb_assemblable": True,
                "stock": 100,
                "params": {"_category": "ResxA"},
            },
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="10uF SMD", category="10%0").body
    assert "C730010" in body
    assert "C730011" not in body
    body = part.search(q="10uF SMD", category="Res_A").body
    assert "C730012" in body
    assert "C730013" not in body


def test_search_live_rows_list_in_stock_first(store):
    out_of_stock = {**C2655145, "stockCount": 0, "componentCode": "C9900000001"}
    part = PartHandler(
        hub=Hub(store=store),
        live_search=lambda s, q, page_size=25: jlc_library.live_search(
            s, q, post=_post_with([out_of_stock, C2655145]), page_size=page_size
        ),
    )
    body = part.search(q="AS7341 sensor").body
    assert body.index("C2655145") < body.index("C9900000001")


def test_search_live_outage_names_the_failure(store):
    def boom(store_, q, page_size=25):
        raise VendorError("jlcpcb-web", "HTTP 503 from listing")

    part = PartHandler(hub=Hub(store=store), live_search=boom)
    body = part.search(q="unobtanium sensor xyz").body
    assert "no assemblable parts match" in body and "503" in body


def test_search_live_empty_says_so(store):
    part = PartHandler(
        hub=Hub(store=store),
        live_search=lambda *a, **k: [],
    )
    body = part.search(q="unobtanium sensor xyz").body
    assert "live jlcpcb.com listing found nothing" in body


# ── soft dielectric/voltage terms (gr520325) ────────────────────────

#: C52923-like: basic, 9.5M stock — description carries a *different*
#: dielectric/voltage (50V X5R) than the query asks (10V X7R).
_BASIC_CAP = {
    "lcsc": "C52923",
    "mfr_part": "CL05A105KP5NNNC",
    "description": "1uF ±10% 50V X5R 0402 Multilayer Ceramic "
    "Capacitors MLCC - SMD RoHS",
    "jlcpcb_assemblable": True,
    "basic": True,
    "stock": 9548000,
    "package": "0402",
}
_EXT_CAP = {
    "lcsc": "C7500381",
    "mfr_part": "GRM155R71A105KE01D",
    "description": "1uF ±10% 10V X7R 0402 Multilayer Ceramic "
    "Capacitors MLCC - SMD RoHS",
    "jlcpcb_assemblable": True,
    "basic": False,
    "stock": 2,
    "package": "0402",
}


def test_search_dielectric_voltage_are_soft_terms(store):
    """gr520325: 'X7R 10V' used to AND into the description tsv and drop
    the basic 9.5M-stock 50V X5R reel; now they only re-rank, so the
    basic part surfaces and basic + stock still outrank the literal
    match."""
    store.parts_import([_EXT_CAP, _BASIC_CAP])
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="1uF 0402 X7R 10V").body
    assert "C52923" in body and "C7500381" in body
    assert body.index("C52923") < body.index("C7500381")
    # An all-soft query keeps the AND semantics — no unfiltered scan.
    body = part.search(q="X7R 10V").body
    assert "C7500381" in body and "C52923" not in body


def test_soft_term_floor_semantics():
    """The gr520325 split + scorer: dielectric is equal-or-better class,
    voltage is a rating floor (a 16 V part answers a 10V ask)."""
    from precis.store._pcb_ops import _part_query_soft, _part_soft_hits

    hard, diels, volts = _part_query_soft("1uF 0402 X7R 10V")
    assert hard == "1uF 0402"
    assert diels == ["x7r"]
    assert volts == [("10v", 10.0)]
    # 50V X5R: voltage floor met, worse dielectric is not — one hit.
    assert _part_soft_hits(str(_BASIC_CAP["description"]), diels, volts) == 1
    # 10V X7R literal: both satisfied.
    assert _part_soft_hits(str(_EXT_CAP["description"]), diels, volts) == 2
    # 16V C0G: a higher rating and a better class both count.
    assert _part_soft_hits("1uF 16V C0G 0402 MLCC capacitor", diels, volts) == 2
    # 6.3V X7R: below the floor — only the dielectric counts.
    assert _part_soft_hits("1uF 6.3V X7R 0402 MLCC capacitor", diels, volts) == 1
    # All-soft: the whole query stays the hard text.
    hard, diels, volts = _part_query_soft("X7R 10V")
    assert hard == "X7R 10V" and diels == ["x7r"] and volts == [("10v", 10.0)]
    # Off a capacitor ask a voltage token is identity, not a floor.
    hard, diels, volts = _part_query_soft("LDO 3.3V")
    assert hard == "LDO 3.3V" and diels == [] and volts == []
    # A capacitance value token alone makes it a capacitor ask.
    hard, diels, volts = _part_query_soft("1uF 50V")
    assert hard == "1uF" and diels == [] and volts == [("50v", 50.0)]


def test_voltage_stays_hard_off_a_capacitor_ask(store):
    """gr520325 fix: an IC's output voltage is its identity — 'LDO 3.3V'
    keeps ``3.3V`` in the hard tsquery, so in-stock 5V/adjustable LDOs
    never enter the candidate set at all."""
    store.parts_import(
        [
            {
                "lcsc": "C58069",
                "mfr_part": "LDO-5V-1A",
                "description": "5V 1A LDO Voltage Regulators SOT-23-5 RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 9000000,
            },
            {
                "lcsc": "C75510",
                "mfr_part": "LDO-ADJ-1A",
                "description": "Adjustable 1A LDO Voltage Regulators SOT-223 RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 8000000,
            },
            {
                "lcsc": "C10333",
                "mfr_part": "LDO-3V3-1A",
                "description": "3.3V 1A LDO Voltage Regulators SOT-23-5 RoHS",
                "jlcpcb_assemblable": True,
                "basic": False,
                "stock": 3,
            },
        ]
    )
    called = []

    def live(*_a, **_k):
        called.append(1)
        return []

    part = PartHandler(hub=Hub(store=store), live_search=live)
    body = part.search(q="LDO 3.3V").body
    assert "C10333" in body
    assert "C58069" not in body and "C75510" not in body
    assert called == []


def test_voltage_floor_drops_under_rated_and_ranks_exact_first(store):
    """gr520325 fix: '1uF X7R 100V' — a parsed rating below the floor is
    a wrong answer, not a near miss: under-rated rows drop outright while
    a no-parse row stays, and the exact 100V match ranks first."""
    store.parts_import(
        [
            {
                "lcsc": "C100016",
                "mfr_part": "CAP-1UF-16V",
                "description": "1uF ±10% 16V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 9000000,
            },
            {
                "lcsc": "C100050",
                "mfr_part": "CAP-1UF-50V",
                "description": "1uF ±10% 50V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 8000000,
            },
            {
                "lcsc": "C100100",
                "mfr_part": "CAP-1UF-100V",
                "description": "1uF ±10% 100V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": False,
                "stock": 5,
            },
            {
                "lcsc": "C100999",
                "mfr_part": "CAP-1UF-NOSPEC",
                "description": "1uF 0402 Multilayer Ceramic Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": False,
                "stock": 100,
            },
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="1uF X7R 100V").body
    assert "C100100" in body and "C100999" in body
    assert body.index("C100100") < body.index("C100999")
    assert "C100016" not in body and "C100050" not in body


def test_soft_only_miss_reaches_the_live_lookup(store):
    """gr520325 ruling: a query with no FULL local match still reaches
    the live listing — soft matches don't count as a hit."""
    store.parts_import(
        [
            {
                "lcsc": "C200001",
                "mfr_part": "CAP-1UF-X7R",
                "description": "1uF ±10% 50V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 1000,
            }
        ]
    )
    calls = []
    post = _post_with([C2655145])

    def live(s, q, page_size=25):
        calls.append(q)
        return jlc_library.live_search(s, q, post=post, page_size=page_size)

    part = PartHandler(hub=Hub(store=store), live_search=live)
    # 'C0G' can't be satisfied by the X7R row — a near-miss, not a hit.
    body = part.search(q="1uF 0402 C0G 50V").body
    assert calls == ["1uF 0402 C0G 50V"]
    assert "LIVE" in body and "C2655145" in body


def test_soft_only_miss_with_live_empty_shows_near_misses(store):
    """The ruling's other half: live finds nothing either, so the local
    near-misses still surface with the empty live answer named."""
    store.parts_import(
        [
            {
                "lcsc": "C200001",
                "mfr_part": "CAP-1UF-X7R",
                "description": "1uF ±10% 50V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 1000,
            }
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="1uF 0402 C0G 50V").body
    assert "C200001" in body
    assert "live jlcpcb.com listing found nothing" in body


def test_full_match_past_the_page_cut_still_suppresses_live(store):
    """gr520325 r3: '10uF X7R 10V' at page_size=5 — six basic in-stock
    near-misses outrank the extended full matches in the ladder, but
    ``all_soft`` is judged over the whole fetched window: the full
    matches replace the trailing near-misses and lead a page that never
    exceeds page_size, and the live lookup never fires (prod lost
    C14860 to exactly this)."""
    near = [
        {
            "lcsc": f"C910{i:04d}",
            "mfr_part": f"CAP-10UF-X5R-{i}",
            "description": "10uF ±10% 50V X5R 0603 Multilayer Ceramic "
            "Capacitors MLCC - SMD RoHS",
            "jlcpcb_assemblable": True,
            "basic": True,
            "stock": 9_000_000 - i,
        }
        for i in range(6)
    ]
    full = [
        {
            "lcsc": f"C920{i:04d}",
            "mfr_part": f"CAP-10UF-X7R-{i}",
            "description": "10uF ±10% 25V X7R 0603 Multilayer Ceramic "
            "Capacitors MLCC - SMD RoHS",
            "jlcpcb_assemblable": True,
            "basic": False,
            "stock": 900_000 - i,
        }
        for i in range(2)
    ]
    store.parts_import(near + full)
    called = []

    def live(*_a, **_k):
        called.append(1)
        return []

    part = PartHandler(hub=Hub(store=store), live_search=live)
    body = part.search(q="10uF X7R 10V", page_size=5).body
    assert called == []
    # The page stays at page_size and the extended X7R full matches
    # lead: they replaced the trailing near-misses.
    assert "# 5 assemblable part(s)" in body
    assert "C9200000" in body and "C9200001" in body
    assert "C9100000" in body  # the surviving near-misses still list
    assert body.index("C9200001") < body.index("C9100000")


def test_below_floor_drop_grows_the_window_until_the_page_fills(store):
    """gr520325 r2: '1uF X7R 100V' — the under-rated drop runs after
    SQL's LIMIT, so a first window full of 16 V basics leaves a short
    page; the refetch grows the window until the satisfying rows fill
    it."""
    under = [
        {
            "lcsc": f"C911{i:04d}",
            "mfr_part": f"CAP-1UF-16V-{i}",
            "description": "1uF ±10% 16V X7R 0402 Multilayer Ceramic "
            "Capacitors MLCC - SMD RoHS",
            "jlcpcb_assemblable": True,
            "basic": True,
            "stock": 9_000_000 - i,
        }
        for i in range(150)
    ]
    full = [
        {
            "lcsc": f"C921{i:04d}",
            "mfr_part": f"CAP-1UF-100V-{i}",
            "description": "1uF ±10% 100V X7R 1206 Multilayer Ceramic "
            "Capacitors MLCC - SMD RoHS",
            "jlcpcb_assemblable": True,
            "basic": False,
            "stock": 50_000 + i,
        }
        for i in range(25)
    ]
    store.parts_import(under + full)
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="1uF X7R 100V", page_size=20).body
    assert "# 20 assemblable part(s)" in body
    assert "CAP-1UF-100V" in body
    assert "CAP-1UF-16V" not in body  # no under-rated row survived


def test_near_miss_merges_live_then_labelled_local_rows(store):
    """gr520325 r2 ruling: no full local match + a live answer MERGES —
    the live row leads, the local near-miss follows labelled with the
    soft term it failed; local rows are never discarded."""
    store.parts_import(
        [
            {
                "lcsc": "C200001",
                "mfr_part": "CAP-1UF-X7R",
                "description": "1uF ±10% 50V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 1000,
            }
        ]
    )
    calls = []
    post = _post_with([C2655145])

    def live(s, q, page_size=25):
        calls.append(q)
        return jlc_library.live_search(s, q, post=post, page_size=page_size)

    part = PartHandler(hub=Hub(store=store), live_search=live)
    body = part.search(q="1uF 0402 C0G 50V").body
    assert calls == ["1uF 0402 C0G 50V"]
    # Live leads; the X7R row follows, labelled with the failed term.
    assert body.index("C2655145") < body.index("C200001")
    assert "near miss: C0G" in body
    assert "LIVE" in body


def test_live_dedup_emptied_still_names_the_lookup(store):
    """gr520325 r3: live answers but every hit is already a catalog
    near-miss — the dedup empties the merge, and the page still names
    the live outcome instead of the plain header."""
    store.parts_import(
        [
            {
                "lcsc": "C200001",
                "mfr_part": "CAP-1UF-X7R",
                "description": "1uF ±10% 50V X7R 0402 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": True,
                "stock": 1000,
            }
        ]
    )
    part = PartHandler(
        hub=Hub(store=store),
        live_search=lambda *a, **k: [{"lcsc": "C200001", "stock": 9}],
    )
    body = part.search(q="1uF 0402 C0G 50V").body
    assert "C200001" in body
    assert "live lookup: 1 hit(s), all already listed" in body


def test_kv_rating_counts_against_the_voltage_floor(store):
    """gr520325 r3: kV ratings parse — '1.25kV'/'1kV' used to come back
    ``rated_v=None``, so a 1250 V cap was labelled 'near miss: 250V' on
    a 250V ask."""
    from precis.pcb.passive import parse_passive_description

    spec = parse_passive_description(
        "1uF ±10% 1.25kV X7R 2220 Multilayer Ceramic Capacitors MLCC - SMD RoHS"
    )
    assert spec is not None and spec["rated_v"] == 1250.0
    spec = parse_passive_description(
        "1uF ±10% 1kV X7R 1210 Multilayer Ceramic Capacitors MLCC - SMD RoHS"
    )
    assert spec is not None and spec["rated_v"] == 1000.0
    store.parts_import(
        [
            {
                "lcsc": "C9301250",
                "mfr_part": "CAP-1UF-1250V",
                "description": "1uF ±10% 1.25kV X7R 2220 Multilayer Ceramic "
                "Capacitors MLCC - SMD RoHS",
                "jlcpcb_assemblable": True,
                "basic": False,
                "stock": 100,
            }
        ]
    )
    part = PartHandler(hub=Hub(store=store), live_search=lambda *a, **k: [])
    body = part.search(q="1uF 250V").body
    assert "C9301250" in body
    assert "near miss" not in body


def test_lowercase_cap_value_still_softens_voltage():
    """CAP_VALUE_RE is case-insensitive (gr520325 r2): '100nf 250v' is
    the same capacitor ask as '100nF 250V' — the voltage softens."""
    from precis.store._pcb_ops import _part_query_soft

    hard, diels, volts = _part_query_soft("100nf 250v")
    assert hard == "100nf" and diels == [] and volts == [("250v", 250.0)]


# ── C-number recognition everywhere ─────────────────────────────────


def test_link_mints_a_live_only_part(store, monkeypatch):
    """link(target='part:<C-no>') on a catalog miss asks the live listing
    once, then mints the ref off the row it catalogued."""
    from precis.handlers.memory import MemoryHandler

    def live(s, c, **_k):
        s.parts_import([{"lcsc": c, "mfr_part": "AS7341-DLGT", "description": "als"}])
        return {"lcsc": c}

    monkeypatch.setattr(jlc_api, "live_lookup", live)
    memory = MemoryHandler(hub=Hub(store=store))
    mem = store.insert_ref(kind="memory", slug=None, title="t").id
    memory.link(id=mem, target="part:C2655145", rel="related-to")
    assert store.part_ref_id("C2655145") is not None
    assert store.part_row("C2655145")["mfr_part"] == "AS7341-DLGT"


def test_link_real_miss_still_refuses_with_live_note(store, monkeypatch):
    from precis.handlers.memory import MemoryHandler

    monkeypatch.setattr(jlc_api, "live_lookup", lambda *a, **k: None)
    memory = MemoryHandler(hub=Hub(store=store))
    mem = store.insert_ref(kind="memory", slug=None, title="t").id
    with pytest.raises(NotFound, match="C99999999") as exc:
        memory.link(id=mem, target="part:C99999999", rel="related-to")
    nxt = exc.value.next
    assert any("live" in n for n in ([nxt] if isinstance(nxt, str) else nxt or []))
    assert store.part_ref_id("C99999999") is None


def test_link_target_accepts_a_product_url(store):
    """``part:<lcsc.com URL>`` mints and resolves under the C-number the
    URL carries."""
    from precis.handlers.memory import MemoryHandler

    store.parts_import([{"lcsc": "C25804", "stock": 1}])
    memory = MemoryHandler(hub=Hub(store=store))
    mem = store.insert_ref(kind="memory", slug=None, title="t").id
    memory.link(
        id=mem,
        target="part:https://www.lcsc.com/product-detail/x_C25804.html",
        rel="related-to",
    )
    ref_id = store.part_ref_id("C25804")
    assert ref_id is not None
    out = store.links_for(ref_id, direction="in")
    assert {lk.src_ref_id for lk in out} == {mem}


def test_pcb_apply_normalizes_part_to_c_number(store):
    ref, _created, _counts = store.pcb_apply(
        slug="b-url",
        title="b",
        components=[
            {
                "refdes": "U1",
                "part": "https://jlcpcb.com/partdetail/AMS-AS7341DLGT/C2655145",
                "pins": [{"name": "1"}],
            },
            {"refdes": "R1", "part": "c25804", "pins": [{"name": "1"}]},
            {"refdes": "X1", "part": "WIDGET-NOT-CATALOG", "pins": [{"name": "1"}]},
        ],
        nets=[],
        connections=[],
    )
    design = store.pcb_load(ref.id)
    by_refdes = {i["refdes"]: i.get("part_lcsc") for i in design["instances"]}
    assert by_refdes["U1"] == "C2655145"
    assert by_refdes["R1"] == "C25804"
    assert by_refdes["X1"] == "WIDGET-NOT-CATALOG"  # untouched, not coerced


def test_datasheet_part_lcsc_accepts_url():
    from precis.handlers.datasheet import DatasheetHandler

    patch = DatasheetHandler._datasheet_meta_patch(
        None, None, "https://www.lcsc.com/product-detail/_C2655145.html"
    )
    assert patch["part_lcsc"] == "C2655145"
    assert (
        DatasheetHandler._datasheet_meta_patch(None, None, "")["part_lcsc"] == ""
    )  # blank still clears


def test_unknown_kind_with_c_number_suggests_part(runtime_with_store):
    body = runtime_with_store.dispatch("get", {"kind": "lcsc", "id": "C2655145"})
    assert "[error:NotFound]" in body
    assert "kind='part'" in body and "C2655145" in body


def test_notfound_on_another_kind_suggests_part(runtime_with_store):
    # 'component' is a slug kind — a C-number id misses with NotFound.
    body = runtime_with_store.dispatch("get", {"kind": "component", "id": "C2655145"})
    assert "[error:NotFound]" in body
    assert "kind='part'" in body


def test_part_get_notfound_does_not_self_suggest(runtime_with_store):
    body = runtime_with_store.dispatch("get", {"kind": "part", "id": "C99999999"})
    assert "[error:NotFound]" in body
    # No "looks like an LCSC" re-suggestion on the part kind's own miss —
    # its own next: already names the live path.
    assert "looks like an LCSC" not in body


def test_cross_kind_search_tips_part(runtime_with_store):
    body = runtime_with_store.dispatch("search", {"kind": "*", "q": "C2655145"})
    assert "kind='part'" in body and "C2655145" in body


# ── view='availability' (EOL sweep) ──────────────────────────────────


def _board(store, slug: str, parts: dict[str, str | None]) -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=f"board {slug}",
        components=[
            {"refdes": refdes, "part_lcsc": lcsc, "pins": [{"name": "1"}]}
            for refdes, lcsc in parts.items()
        ],
        nets=[],
        connections=[],
    )
    return int(ref.id)


def _age_row(store, lcsc: str, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE parts SET refreshed_at = now() - make_interval(days => %s) "
            "WHERE lcsc = %s",
            (days, lcsc),
        )


def test_availability_view_sweeps_stale_rows_live(store, creds):
    from precis.handlers.pcb import PcbHandler

    store.parts_import(
        [
            {
                "lcsc": "C25804",
                "mfr_part": "0603WAF1002T5E",
                "description": "10k 0603",
                "basic": True,
                "stock": 1000,
                "price": [{"qFrom": 1, "qTo": None, "price": 0.001}],
            },
            # Stale catalog row — gets re-checked live.
            {"lcsc": "C2655145", "mfr_part": "OLD-ALS", "stock": 3},
        ]
    )
    _age_row(store, "C2655145", days=2)
    board = _board(
        store,
        "sweep",
        {
            "U1": "C2655145",  # stale row → live hit, re-catalogued
            "R1": "C25804",  # fresh row → no fetch
            "R2": "C99999999",  # uncatalogued → live probed, misses
            "J1": None,  # no LCSC number at all
        },
    )
    fetched: list[str] = []
    client = _api_client({"C2655145": API_C2655145}, fetched)
    pcb = PcbHandler(
        hub=Hub(store=store),
        live_refresh=lambda s, lcs: jlc_api.live_refresh_many(s, lcs, client=client),
    )
    body = pcb.get(id="sweep", view="availability").body
    # The live pass refreshed only the stale/absent rows.
    assert "C25804" not in fetched  # fresh row: untouched
    assert set(fetched) == {"C2655145", "C99999999"}
    assert "2 re-catalogued" in body or "1 re-catalogued" in body
    # C2655145's refreshed row shows live data; C99999999 is named absent.
    assert "AS7341-DLGT" in body and "492" in body
    assert "absent from jlcpcb.com" in body  # R2's line
    assert "no LCSC number" in body  # J1's line


def test_availability_view_renders_lifecycle_signals(store):
    from precis.handlers.pcb import PcbHandler

    store.parts_import(
        [
            {
                "lcsc": "C2177020",
                "mfr_part": "CBR06C999B5GAC",
                "stock": 0,
                "params": {
                    "_lifecycle": {"merged": "C9001", "alts": "C111,C222", "loss": 4}
                },
            }
        ]
    )
    _board(store, "eol", {"R1": "C2177020"})
    pcb = PcbHandler(hub=Hub(store=store), live_refresh=lambda *a, **k: {})
    body = pcb.get(id="eol", view="availability").body
    assert "merged→C9001" in body
    assert "alts C111,C222" in body
    assert "stock-loss 4" in body  # warehouse write-offs, self-explaining
    assert "stock 0" in body
    assert "ext" in body  # library class column


def test_availability_lib_survives_a_live_refresh(store, creds):
    """gr495286: a Basic part's ``lib`` cell must stay ``basic`` after the
    stale-row live refresh — the detail endpoint spells the class
    ``libraryType``, which ``normalize_api_row`` used to drop, writing
    ``basic=False`` over the catalog's flag."""
    from precis.handlers.pcb import PcbHandler

    store.parts_import([{"lcsc": "C1525", "basic": True, "stock": 5}])
    _age_row(store, "C1525", days=2)
    _board(store, "libflag", {"C1": "C1525"})
    client = _api_client(
        {"C1525": {"componentCode": "C1525", "libraryType": "base", "stockCount": 42}}
    )
    pcb = PcbHandler(
        hub=Hub(store=store),
        live_refresh=lambda s, lcs: jlc_api.live_refresh_many(s, lcs, client=client),
    )
    body = pcb.get(id="libflag", view="availability").body
    assert "\tbasic\t" in body
    assert "\text\t" not in body


def test_availability_live_refresh_is_bounded(store, creds):
    from precis.handlers.pcb import PcbHandler

    parts: dict[str, str | None] = {f"U{i}": f"C9{i:07d}" for i in range(15)}
    _board(store, "fat", parts)
    calls: list[str] = []
    client = _api_client({}, calls)
    pcb = PcbHandler(
        hub=Hub(store=store),
        live_refresh=lambda s, lcs: jlc_api.live_refresh_many(s, lcs, client=client),
    )
    body = pcb.get(id="fat", view="availability").body
    assert len(calls) == jlc_api._LIVE_BATCH_MAX  # 12, not 15
    assert "skipped (bounded)" in body
    assert body.count("not in catalog") >= 3


# ── batch get (gr520100) ──────────────────────────────────────────


def test_get_batch_returns_every_id_in_order(runtime_with_store, store, monkeypatch):
    """gr520100: a list id used to stringify and regex out the first
    C-number — every later id silently dropped. The batch maps the
    single-id read over all of them, in order; a miss is a named
    per-id block, not an error for the call."""
    store.parts_import(
        [
            {"lcsc": "C25804", "mfr_part": "RC0603FR-0710KL", "stock": 100},
            {"lcsc": "C2655145", "mfr_part": "AS7341-DLGT", "stock": 492},
        ]
    )
    # The miss must not depend on credentials/network — pin the live seam.
    monkeypatch.setattr(jlc_api, "live_lookup", lambda *a, **k: None)
    body = runtime_with_store.dispatch(
        "get", {"kind": "part", "id": ["C2655145", "C99999999", "C25804"]}
    )
    assert "[error:" not in body
    assert "AS7341-DLGT" in body and "RC0603FR-0710KL" in body
    assert "C99999999" in body and "not found" in body
    assert body.index("C2655145") < body.index("C99999999") < body.index("C25804")


def test_get_batch_dedupes_and_validates(store):
    part = PartHandler(hub=Hub(store=store))
    store.parts_import([{"lcsc": "C25804", "stock": 1}])
    body = part.get(id=["c25804", "C25804"]).body
    assert body.startswith("# 1 parts (batch)")  # spellings fold to one id
    with pytest.raises(BadInput, match="empty"):
        part.get(id=[])
    with pytest.raises(BadInput, match="at most"):
        part.get(id=[f"C{i + 100}" for i in range(51)])
    with pytest.raises(BadInput, match=r"id\[0\]"):
        part.get(id=typing.cast(Any, [{"lcsc": "C25804"}]))


# ── partdetail fallback on an Open-API miss (gr519923) ────────────────
#
# The listing endpoint (and the Open API's detail call) can't see whole
# categories — Supercapacitors answer total=0 to keyword, exact-MPN AND
# secondSortName. The bare /partdetail/C<n> page serves every C-number's
# record inside its Next.js RSC payload. Canned pages below carry the
# real structure (trimmed from a live C1578291 capture — BZ013A144ZSB,
# expand, no-buy, stock 0): $-ref'd prices/attributes, "--"/"$undefined"
# sentinels, and related-product rows for OTHER parts.


def _push(*chunks: str) -> str:
    """One ``self.__next_f.push`` script tag per RSC chunk, JSON-escaped
    the way the page embeds them."""
    return "".join(
        f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks
    )


_PARTDETAIL_HIT = _push(
    '1:HL["https://assets.jlcpcb.com/web-resources/x/latest"]',
    '8:I[99925,["90","static/chunks/90.js"],""]',
    "f:T12,not json\ncontinuation line",
    '30:["$31","$32"]',
    '31:{"componentPriceKeyId":null,"componentCode":"C1578291","startNumber":1,'
    '"endNumber":9,"productPrice":9.8385}',
    '32:{"componentPriceKeyId":null,"componentCode":"C1578291","startNumber":10,'
    '"endNumber":-1,"productPrice":8.5504}',
    '37:{"componentModelEn":"BZ013A144ZSB","componentBrandEn":"--",'
    '"componentCode":"C1578291","componentName":"BZ013A144ZSB",'
    '"describe":"-20%~+80% 0.14F 3.6V 70mΩ SMD Supercapacitors ROHS",'
    '"componentSpecificationEn":"SMD","componentSpecification":"$undefined",'
    '"lcscComponentId":1669106,"dataManualUrl":"--",'
    '"componentLibraryType":"expand","assemblyComponentFlag":false,'
    '"preferredComponentFlag":"$undefined","prices":"$30"}',
    '3b:["$3c","$3d"]',
    '3c:{"attribute_name_en":"Capacitance","component_code":"C1578291",'
    '"attribute_value_name":"0.14F"}',
    '3d:{"attribute_name_en":"Voltage Rating","component_code":"C1578291",'
    '"attribute_value_name":"3.6V"}',
    '3a:{"componentModelEn":"BZ013A144ZSB","componentBrandEn":"--",'
    '"componentCode":"C1578291","attributes":"$3b",'
    '"firstSortName":"Capacitors","secondSortName":"Supercapacitors",'
    '"secondTypeNameId":3232}',
    '50:{"componentCode":"C1578291","componentModelEn":"BZ013A144ZSB",'
    '"componentBrandEn":"--","isBuyComponent":"0",'
    '"componentAlternativesCode":null,"canPresaleNumber":0,'
    '"overseasStockCount":0,"initialPrice":9.8385,"prices":"$30",'
    '"lossNumber":0,"leastPatchNumber":0,'
    '"noBuyReason":"This product is no longer manufactured."}',
    # a related-product row for a DIFFERENT part — the code-match guard
    # must keep it out even on a hit page
    '44:{"componentCode":"C17507162","componentModelEn":"CWR11JH225KCB",'
    '"componentBrandEn":"Kyocera AVX","price":23.4,"stockCount":7}',
)

_PARTDETAIL_MISS = _push(
    '1:HL["https://assets.jlcpcb.com/web-resources/x/latest"]',
    '7:["urlSuffix","C99999999","c"]',
    '44:{"componentCode":"C17507162","componentModelEn":"CWR11JH225KCB",'
    '"componentBrandEn":"Kyocera AVX","price":23.4,"stockCount":7}',
    '45:{"componentCode":"C6383141","componentModelEn":"008263051100000+",'
    '"componentBrandEn":"Kyocera AVX","price":"$undefined","stockCount":0}',
)


def _fetch_with(html: str, urls: list[str] | None = None):
    def fetch(url: str) -> str:
        if urls is not None:
            urls.append(url)
        return html

    return fetch


def test_live_lookup_partdetail_fallback_hit(store, creds):
    """gr519923: the Open API misses Supercapacitors-category parts; the
    partdetail page's RSC record fills the gap and lands in ``parts``."""
    calls: list[str] = []
    client = _api_client({}, calls)  # the API misses
    urls: list[str] = []
    row = jlc_api.live_lookup(
        store,
        "C1578291",
        client=client,
        fetch=_fetch_with(_PARTDETAIL_HIT, urls),
    )
    assert row is not None and row["lcsc"] == "C1578291"
    assert row["mfr_part"] == "BZ013A144ZSB"
    assert row["mfr"] is None  # the "--" placeholder is absent, not a brand
    assert row["package"] == "SMD" and row["basic"] is False
    assert urls == [f"{jlc_library.HOST}{jlc_library.PARTDETAIL_PATH}C1578291"]
    assert calls == ["C1578291"]  # one API call, then the page
    # a no-buy record's ladder is a placeholder → suppressed (gr519681);
    # the reason survives in params._lifecycle
    assert row["price"] is None
    assert row["params"]["_lifecycle"]["no_buy"] == (
        "This product is no longer manufactured."
    )
    assert row["params"]["_category"] == "Capacitors / Supercapacitors"
    assert row["params"]["Capacitance"] == "0.14F"
    assert row["params"]["_lcsc_url"].endswith("/partdetail/C1578291")
    assert row["datasheet_url"] is None  # "--" → absent, not the literal
    got = store.part_row("C1578291")
    assert got is not None and got["mfr_part"] == "BZ013A144ZSB"
    assert got["stock"] == 0 and "Supercapacitors" in got["description"]


def test_partdetail_lookup_normalizes_the_rsc_record():
    """The seam's own contract: HTML in, a ``normalize_library_row`` row
    out — bare/lower-case C-numbers canonicalise too."""
    urls: list[str] = []
    row = jlc_library.partdetail_lookup(
        "c1578291", fetch=_fetch_with(_PARTDETAIL_HIT, urls)
    )
    assert row is not None and row["lcsc"] == "C1578291"
    assert row["basic"] is False and row["stock"] == 0
    assert urls == [f"{jlc_library.HOST}{jlc_library.PARTDETAIL_PATH}C1578291"]


def test_live_lookup_partdetail_miss_is_a_real_miss(store, creds):
    """A partdetail page with no record for the code (related products
    only, the ``null | null | JLCPCB`` miss page) is a real miss — None,
    negative-cached, no second fetch."""
    calls: list[str] = []
    client = _api_client({}, calls)
    urls: list[str] = []
    fetch = _fetch_with(_PARTDETAIL_MISS, urls)
    assert jlc_api.live_lookup(store, "C99999999", client=client, fetch=fetch) is None
    assert jlc_api.live_lookup(store, "C99999999", client=client, fetch=fetch) is None
    assert calls == ["C99999999"] and len(urls) == 1
    assert store.part_row("C99999999") is None


def test_live_lookup_partdetail_error_degrades_to_miss(store, creds):
    """A wedged partdetail page doesn't mask the API's answer: the lookup
    reports the miss it already has (logged), never an outage error on a
    part the API said it can't see."""
    client = _api_client({})

    def boom(url: str) -> str:
        raise VendorError("jlcpcb-web", "HTTP 503 from partdetail")

    assert jlc_api.live_lookup(store, "C99999999", client=client, fetch=boom) is None
    assert "C99999999" in jlc_api._live_misses


def test_live_lookup_api_hit_never_fetches_partdetail(store, creds):
    """The page is only consulted on an API miss — a live hit pays no
    second request."""
    client = _api_client({"C2655145": API_C2655145})
    urls: list[str] = []
    row = jlc_api.live_lookup(
        store,
        "C2655145",
        client=client,
        fetch=_fetch_with(_PARTDETAIL_HIT, urls),
    )
    assert row is not None and row["lcsc"] == "C2655145"
    assert urls == []


# ── view='categories' — the JLCPCB sort-taxonomy browse (gr519793) ────
#
# The handler never sees wire shapes: ``live_categories`` is the seam,
# a ``(store, path) -> {tree, path, unmatched, parts, total}`` fake in
# ``browse_categories``' shape (the wire-shape tests live in
# tests/test_pcb_jlc_library.py).

_CAT_TREE: list[dict[str, Any]] = [
    {
        "name": "Capacitors",
        "sort_id": 2,
        "count": 1012692,
        "children": [
            {
                "name": "Supercapacitors",
                "sort_id": 3232,
                "count": 5,
                "children": [],
            },
            {
                "name": "MLCC - SMD/SMT",
                "sort_id": 2930,
                "count": 18586,
                "children": [],
            },
        ],
    },
    {
        "name": "Connectors",
        "sort_id": 8,
        "count": 1200580,
        "children": [
            {
                "name": "Photovoltaic(Solar Panel)Connectors",
                "sort_id": 2223,
                "count": 31,
                "children": [],
            }
        ],
    },
]


def _browse_result(tree=_CAT_TREE, matched=(), unmatched="", parts=(), total=0):
    return {
        "tree": tree,
        "path": list(matched),
        "unmatched": unmatched,
        "parts": list(parts),
        "total": total,
    }


def test_categories_view_lists_first_sorts(store):
    seen: list[str | None] = []

    def _record(_store, path):
        seen.append(path)
        return _browse_result()

    part = PartHandler(
        hub=Hub(store=store),
        live_categories=_record,
    )
    body = part.get(view="categories").body
    assert seen == [None]
    assert "Capacitors" in body and "Connectors" in body
    assert "1012692" in body and "categories" in body.splitlines()[0]


def test_categories_view_mid_node_lists_children(store):
    part = PartHandler(
        hub=Hub(store=store),
        live_categories=lambda s, path: _browse_result(
            matched=[_CAT_TREE[0]], unmatched=""
        ),
    )
    body = part.get(view="categories", id="Capacitors").body
    assert "Supercapacitors" in body and "MLCC - SMD/SMT" in body
    assert "Capacitors" in body.splitlines()[0]


def test_categories_view_leaf_lists_live_parts(store):
    row = {
        "lcsc": "C1578291",
        "mfr_part": "BZ013A144ZSB",
        "description": "0.14F 3.6V SMD Supercapacitors",
        "basic": False,
        "stock": 0,
        "package": "SMD",
        "price": None,
        "params": {"_category": "Capacitors / Supercapacitors"},
    }
    part = PartHandler(
        hub=Hub(store=store),
        live_categories=lambda s, path: _browse_result(
            matched=[_CAT_TREE[0], _CAT_TREE[0]["children"][0]],
            unmatched="",
            parts=[row],
            total=5,
        ),
    )
    body = part.get(view="categories", id="Capacitors/Supercapacitors").body
    assert "C1578291" in body and "5 part(s)" in body
    assert "Capacitors / Supercapacitors" in body.splitlines()[0]


def test_categories_view_unmatched_falls_to_keyword_paths(store):
    part = PartHandler(
        hub=Hub(store=store),
        live_categories=lambda s, path: _browse_result(matched=[], unmatched=path),
    )
    body = part.get(view="categories", id="solar").body
    assert "matching 'solar'" in body
    assert "Connectors / Photovoltaic(Solar Panel)Connectors" in body


def test_categories_view_unknown_path_is_refused(store):
    part = PartHandler(
        hub=Hub(store=store),
        live_categories=lambda s, path: _browse_result(matched=[], unmatched=path),
    )
    with pytest.raises(BadInput) as ei:
        part.get(view="categories", id="zzzzz")
    assert "Capacitors" in str(ei.value.options or "")


def test_categories_view_live_failure_is_upstream(store):
    def boom(_s, _p):
        raise VendorError("jlcpcb-web", "HTTP 503 from the listing")

    part = PartHandler(hub=Hub(store=store), live_categories=boom)
    with pytest.raises(Upstream):
        part.get(view="categories")
