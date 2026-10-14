"""Live single-part fallback on a catalog miss (gr485250).

``get(kind='part', id='C…')`` on a part the jlcparts dump + library top-up
never carried queries JLCPCB's public listing endpoint by ``keyword``,
exact-matches the C-number (the endpoint fuzzy-matches — a never-issued
number returns neighbours, not an empty page), upserts via ``parts_import``
and returns the row. No network here: the ``post=`` seam feeds canned
listing envelopes.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.part import PartHandler
from precis.pcb import catalog, jlc_library
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


@pytest.fixture(autouse=True)
def _fresh_live_cache():
    jlc_library.reset_live_cache()
    yield
    jlc_library.reset_live_cache()


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


def test_live_lookup_exact_match_upserts(store):
    post = _post_with([_NEIGHBOUR, C2655145])
    row = jlc_library.live_lookup(store, "C2655145", post=post)
    assert row is not None and row["lcsc"] == "C2655145"
    assert row["basic"] is False  # componentLibraryType 'expand'
    assert row["stock"] == 492 and row["params"]["_category"].endswith("Sensors")
    got = store.part_row("C2655145")
    assert got is not None and got["mfr_part"] == "AS7341-DLGT"
    # Neighbours the endpoint fuzzy-matched are NOT imported.
    assert store.part_row("C1396897") is None


def test_live_lookup_miss_is_negative_cached(store):
    calls: list[dict] = []
    post = _post_with([_NEIGHBOUR], calls=calls)
    assert jlc_library.live_lookup(store, "C99999999", post=post) is None
    assert jlc_library.live_lookup(store, "C99999999", post=post) is None
    assert len(calls) == 1  # second call served by the miss cache


def test_live_lookup_error_wraps(store):
    def post(body):
        raise RuntimeError("boom")

    with pytest.raises(VendorError, match="live part lookup failed"):
        jlc_library.live_lookup(store, "C2655145", post=post)


def test_get_falls_back_to_live(store):
    post = _post_with([C2655145])
    part = PartHandler(
        hub=Hub(store=store),
        live_lookup=lambda s, c: jlc_library.live_lookup(s, c, post=post),
    )
    body = part.get(id="C2655145").body
    assert "C2655145" in body and "AS7341-DLGT" in body
    assert store.part_row("C2655145") is not None  # the hit was catalogued


def test_get_real_miss_names_the_live_check(store):
    post = _post_with([_NEIGHBOUR])
    part = PartHandler(
        hub=Hub(store=store),
        live_lookup=lambda s, c: jlc_library.live_lookup(s, c, post=post),
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


# ── C-number recognition everywhere ─────────────────────────────────


def test_link_mints_a_live_only_part(store, monkeypatch):
    """link(target='part:<C-no>') on a catalog miss asks the live listing
    once, then mints the ref off the row it catalogued."""
    from precis.handlers.memory import MemoryHandler

    def live(s, c, **_k):
        s.parts_import([{"lcsc": c, "mfr_part": "AS7341-DLGT", "description": "als"}])
        return {"lcsc": c}

    monkeypatch.setattr(jlc_library, "live_lookup", live)
    memory = MemoryHandler(hub=Hub(store=store))
    mem = store.insert_ref(kind="memory", slug=None, title="t").id
    memory.link(id=mem, target="part:C2655145", rel="related-to")
    assert store.part_ref_id("C2655145") is not None
    assert store.part_row("C2655145")["mfr_part"] == "AS7341-DLGT"


def test_link_real_miss_still_refuses_with_live_note(store, monkeypatch):
    from precis.handlers.memory import MemoryHandler

    monkeypatch.setattr(jlc_library, "live_lookup", lambda *a, **k: None)
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

#: A live row carrying the endpoint's lifecycle signals.
_EOL_ROW = {
    **_NEIGHBOUR,
    "componentCode": "C2177020",
    "componentLibraryType": "expand",
    "componentModelEn": "CBR06C999B5GAC",
    "stockCount": 0,
    "mergedComponentCode": "C9001",
    "componentAlternativesCode": "C111,C222",
    "lossNumber": 4,
}


def _post_by_keyword(mapping, calls=None):
    def post(body):
        if calls is not None:
            calls.append(dict(body))
        rows = mapping.get(body.get("keyword") or "", [])
        return {
            "data": {
                "componentPageInfo": {
                    "total": len(rows),
                    "pages": 1,
                    "hasNextPage": False,
                    "list": rows,
                }
            }
        }

    return post


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


def test_availability_view_sweeps_stale_rows_live(store):
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
    calls: list[dict] = []
    post = _post_by_keyword({"C2655145": [C2655145], "C99999999": [_NEIGHBOUR]}, calls)
    pcb = PcbHandler(
        hub=Hub(store=store),
        live_refresh=lambda s, lcs: jlc_library.live_refresh_many(s, lcs, post=post),
    )
    body = pcb.get(id="sweep", view="availability").body
    # The live pass refreshed only the stale/absent rows.
    fetched = [c["keyword"] for c in calls]
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
    assert "stock 0" in body
    assert "ext" in body  # library class column


def test_availability_live_refresh_is_bounded(store):
    from precis.handlers.pcb import PcbHandler

    parts: dict[str, str | None] = {f"U{i}": f"C9{i:07d}" for i in range(15)}
    _board(store, "fat", parts)
    calls: list[dict] = []
    pcb = PcbHandler(
        hub=Hub(store=store),
        live_refresh=lambda s, lcs: jlc_library.live_refresh_many(
            s, lcs, post=_post_by_keyword({}, calls)
        ),
    )
    body = pcb.get(id="fat", view="availability").body
    assert len(calls) == jlc_library._LIVE_BATCH_MAX  # 12, not 15
    assert "skipped (bounded)" in body
    assert body.count("not in catalog") >= 3
