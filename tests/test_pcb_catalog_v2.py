"""jlcparts ``source-db-v2`` dump (``jlc_components``) — reader, normalizer,
and the batched staging+swap load. v1 coverage lives in test_pcb_catalog.py."""

from __future__ import annotations

import sqlite3

import pytest

from precis.pcb import catalog
from precis.store import _pcb_ops

_V2_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);"
    "CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY NOT NULL, "
    "fetched_at INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL, "
    "category TEXT NOT NULL, subcategory TEXT NOT NULL, mfr TEXT NOT NULL, "
    "package TEXT NOT NULL, manufacturer TEXT NOT NULL, "
    "library_type TEXT NOT NULL, preferred INTEGER NOT NULL, "
    "description TEXT NOT NULL, datasheet TEXT NOT NULL, stock INTEGER NOT NULL, "
    "price TEXT NOT NULL, attributes TEXT NOT NULL, assembly INTEGER, "
    "assembly_process TEXT, assembly_mode TEXT, attrition TEXT NOT NULL DEFAULT '');"
)


def _v2_row(lcsc, *, present=1, lib="expand", pref=0, stock=10, mode="smtWeld", **kw):
    return (
        lcsc,
        present,
        kw.get("category", "Resistors"),
        kw.get("subcategory", "Chip Resistor"),
        kw.get("mfr", f"RC{lcsc}"),
        kw.get("package", "0402"),
        kw.get("manufacturer", "Yageo"),
        lib,
        pref,
        kw.get("description", f"part {lcsc}"),
        "http://ds",
        stock,
        kw.get("price", "1-49:0.0204,50-:0.0108"),
        kw.get("attributes", '{"Resistance":"1kR"}'),
        0,
        "SMT",
        mode,
    )


def _write_v2(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(_V2_DDL)
    conn.execute("INSERT INTO meta VALUES ('format','source-db-v2')")
    conn.executemany(
        "INSERT INTO jlc_components (lcsc, present, category, subcategory, mfr, "
        "package, manufacturer, library_type, preferred, description, datasheet, "
        "stock, price, attributes, assembly, assembly_process, assembly_mode) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_parse_v2_price():
    assert catalog.parse_v2_price("1-49:0.0204,50-:0.0108") == [
        {"qFrom": 1, "qTo": 49, "price": 0.0204},
        {"qFrom": 50, "qTo": None, "price": 0.0108},
    ]
    assert catalog.parse_v2_price("") is None
    assert catalog.parse_v2_price("garbage") is None
    assert catalog.min_unit_price(catalog.parse_v2_price("1-9:2.5,10-:1.5")) == 1.5


def test_reader_detects_v2_and_filters_present(tmp_path):
    db = tmp_path / "cache.sqlite3"
    _write_v2(db, [_v2_row(100), _v2_row(101, present=0), _v2_row(102)])
    rows = list(catalog.read_jlcparts_sqlite(str(db)))
    assert [r["lcsc"] for r in rows] == [100, 102]  # present = 1 only
    assert "attrition" not in rows[0]  # wide unused columns not read


def test_reader_rejects_unknown_schema(tmp_path):
    db = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE foo (a)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="not a jlcparts dump"):
        list(catalog.read_jlcparts_sqlite(str(db)))


def test_normalize_v2_field_mapping(tmp_path):
    db = tmp_path / "cache.sqlite3"
    _write_v2(
        db,
        [
            _v2_row(25804, lib="base", stock=7, manufacturer="Yageo", mfr="RC0402"),
            _v2_row(25805, pref=1),
            _v2_row(25806, mode="manualWeld"),
            _v2_row(25807, attributes="not json", price=""),
        ],
    )
    a, b, c, d = (
        catalog.normalize_jlcparts_row(r) for r in catalog.read_jlcparts_sqlite(str(db))
    )
    assert a and b and c and d
    assert a["lcsc"] == "C25804"
    assert (a["mfr"], a["mfr_part"]) == ("Yageo", "RC0402")
    assert a["basic"] is True and a["stock"] == 7 and a["package"] == "0402"
    assert a["jlcpcb_assemblable"] is True
    assert a["price"][0] == {"qFrom": 1, "qTo": 49, "price": 0.0204}
    assert a["params"]["Resistance"] == "1kR"
    assert a["params"]["_category"] == "Resistors / Chip Resistor"
    assert a["datasheet_url"] == "http://ds" and a["description"] == "part 25804"
    assert b["basic"] is True  # preferred collapses to basic, as in v1
    assert c["basic"] is False and c["jlcpcb_assemblable"] is False  # manualWeld
    assert d["price"] is None
    assert d["params"] == {"_category": "Resistors / Chip Resistor"}


def test_bulk_refresh_v2_batches_and_swaps(store, tmp_path, monkeypatch):
    monkeypatch.setattr(_pcb_ops, "_BULK_BATCH", 3)  # several batches
    db = tmp_path / "cache.sqlite3"
    _write_v2(db, [_v2_row(1000 + i, stock=i) for i in range(10)])
    counts = catalog.bulk_refresh_parts_from_sqlite(store, str(db))
    assert counts == {"loaded": 10, "restocked": 0}
    row = store.part_row("C1007")
    assert row is not None and row["stock"] == 7 and row["package"] == "0402"

    # Second load: higher stock restocks; availability rolls across batches.
    db2 = tmp_path / "cache2.sqlite3"
    _write_v2(db2, [_v2_row(1000 + i, stock=i + 5) for i in range(10)])
    counts2 = catalog.bulk_refresh_parts_from_sqlite(store, str(db2))
    assert counts2 == {"loaded": 10, "restocked": 10}
    assert store.part_row("C1003")["restock_count"] == 1


def test_bulk_refresh_v2_shrink_guard(store, tmp_path):
    db = tmp_path / "cache.sqlite3"
    _write_v2(db, [_v2_row(2000 + i) for i in range(10)])
    catalog.bulk_refresh_parts_from_sqlite(store, str(db))
    small = tmp_path / "small.sqlite3"
    _write_v2(small, [_v2_row(2000)])
    with pytest.raises(ValueError, match="refusing to swap"):
        catalog.bulk_refresh_parts_from_sqlite(store, str(small))
    assert store.part_row("C2009") is not None  # live catalog untouched
