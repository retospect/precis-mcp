"""Parts catalog (ADR 0042 §5, Slice 2) — normalizer (pure) + import /
turnover / selector / footprint cache / auto-stamp / sqlite refresh (DB).
"""

from __future__ import annotations

import sqlite3

import pytest

from precis.dispatch import Hub
from precis.handlers.part import PartHandler
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog, footprint


# ── pure normalizer ──────────────────────────────────────────────────
def test_normalize_maps_jlcparts_row():
    raw = {
        "lcsc": 25804,
        "manufacturer": "Samsung",
        "mfr": "CL05B104KO5NNNC",
        "description": "100nF 16V X7R 0402",
        "basic": 1,
        "stock": 500000,
        "package": "0402",
        "datasheet": "http://x/ds.pdf",
        "price": [{"qFrom": 1, "qTo": 100, "price": 0.0023}],
        "extra": {"attributes": {"Capacitance": "100nF"}},
    }
    n = catalog.normalize_jlcparts_row(raw)
    assert n is not None
    assert n["lcsc"] == "C25804"  # C-prefixed
    assert n["mfr_part"] == "CL05B104KO5NNNC" and n["mfr"] == "Samsung"
    assert n["jlcpcb_assemblable"] is True and n["basic"] is True
    assert n["stock"] == 500000 and n["package"] == "0402"


def test_normalize_lcsc_forms_and_missing():
    assert catalog.normalize_jlcparts_row({"lcsc": "C7"})["lcsc"] == "C7"
    assert catalog.normalize_jlcparts_row({"lcsc": 7})["lcsc"] == "C7"
    assert catalog.normalize_jlcparts_row({"description": "x"}) is None  # no C-number


def test_min_unit_price():
    assert (
        catalog.min_unit_price([{"price": 0.01}, {"price": 0.004}, {"price": 0.007}])
        == 0.004
    )
    assert catalog.min_unit_price(None) is None


# ── DB: import + selector + turnover ─────────────────────────────────
@pytest.fixture
def part(store):
    return PartHandler(hub=Hub(store=store))


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _rows():
    return [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 1001,
                "description": "100nF 16V X7R 0402 capacitor",
                "basic": 1,
                "stock": 800000,
                "package": "0402",
                "price": [{"price": 0.002}],
            },
            {
                "lcsc": 1002,
                "description": "100nF 50V X7R 0402 capacitor",
                "basic": 0,
                "stock": 5000,
                "package": "0402",
                "price": [{"price": 0.01}],
            },
        ]
    ]


def test_parts_import_and_basic_first_selector(store, part):
    store.parts_import([r for r in _rows() if r])
    resp = part.search(q="100nF 0402 capacitor")
    body = resp.body
    assert "C1001" in body and "C1002" in body
    # Basic (C1001) ranks before Extended (C1002)
    assert body.index("C1001") < body.index("C1002")


def test_turnover_ranks_restocked_first(store, part):
    # two equally-Basic parts; one gets restocked across dumps → ranks first
    base = [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 2001,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            },
            {
                "lcsc": 2002,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            },
        ]
    ]
    store.parts_import([r for r in base if r])
    # second dump: 2001 restocked (stock rose), 2002 drained
    nxt = [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 2001,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 5000,
                "package": "0402",
            },
            {
                "lcsc": 2002,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 200,
                "package": "0402",
            },
        ]
    ]
    counts = store.parts_import([r for r in nxt if r])
    assert counts["restocked"] == 1  # only 2001 rose
    body = part.search(q="10k 0402 resistor").body
    assert body.index("C2001") < body.index("C2002")  # higher turnover first


def test_part_get(store, part):
    store.parts_import([r for r in _rows() if r])
    resp = part.get(id="C1001")
    assert "C1001" in resp.body and "0402" in resp.body


# ── footprint cache (Flow B, fake fetcher) ───────────────────────────
def test_footprint_cache_fetches_once(store):
    calls = []

    def fake(lcsc):
        calls.append(lcsc)
        return {
            "pads": [{"n": "1"}],
            "pin_map": {"1": "A"},
            "courtyard": {"w": 1.0, "h": 0.5},
            "source": "fake",
        }

    f1 = footprint.ensure_footprint(store, "C9001", fetcher=fake)
    assert f1 is not None and f1["source"] == "fake"
    f2 = footprint.ensure_footprint(store, "C9001", fetcher=fake)
    assert f2 is not None
    assert calls == ["C9001"]  # second call hit the cache, no re-fetch


# ── auto-stamp a catalog part onto a component ───────────────────────
def test_component_auto_stamps_footprint_from_catalog(store, pcb):
    store.parts_import(
        [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 3003,
                    "description": "100nF 0402",
                    "basic": 1,
                    "stock": 9000,
                    "package": "0402",
                }
            )
        ]
    )
    # the LLM picks the part by C-number only — footprint/height get stamped
    pcb.put(
        id="b",
        args={
            "components": [
                {
                    "refdes": "C1",
                    "label": "100nF",
                    "part": "C3003",
                    "pins": [{"name": "1"}, {"name": "2"}],
                }
            ]
        },
    )
    ref = store.get_ref(kind="pcb", id="b")
    loaded = store.pcb_load(ref.id)
    c1 = next(i for i in loaded["instances"] if i["refdes"] == "C1")
    assert c1["part_lcsc"] == "C3003"
    assert c1["footprint"] == "0402"  # stamped from the catalog


# ── Flow A end-to-end from a jlcparts SQLite fixture ─────────────────
def test_refresh_from_sqlite(store, tmp_path):
    db = tmp_path / "cache.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE components (lcsc INTEGER, manufacturer TEXT, mfr TEXT, "
        "description TEXT, basic INTEGER, stock INTEGER, package TEXT, "
        "datasheet TEXT, price TEXT, extra TEXT)"
    )
    conn.execute(
        "INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            44004,
            "Yageo",
            "RC0402",
            "1k 0402 resistor",
            1,
            12345,
            "0402",
            "http://x",
            '[{"price": 0.001}]',
            '{"attributes": {}}',
        ),
    )
    conn.commit()
    conn.close()

    counts = catalog.refresh_parts_from_sqlite(store, str(db))
    assert counts["upserted"] == 1
    row = store.part_row("C44004")
    assert row is not None and row["package"] == "0402" and row["basic"] is True
