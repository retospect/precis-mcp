"""Parts-catalog normalization (ADR 0042 §5, Flow A).

The primary source is the community **jlcparts** dump (yaqwsx/jlcparts) — the
whole JLCPCB assembly catalog as a daily SQLite/JSON. Everything in it is
JLCPCB-*assemblable* by definition (that flag is the one thing only JLCPCB
knows; Octopart/Nexar, Digi-Key, Mouser carry parametrics/stock but not it).

This module is the pure row → ``parts``-column adapter, kept separate from the
DB import (:meth:`precis.store._pcb_ops.PcbMixin.parts_import`) so it is
testable without a database and a second source (Nexar enrichment) can layer
on later.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any


def _lcsc_number(raw: Any) -> str | None:
    """jlcparts stores the C-number as the integer ``lcsc`` (e.g. 25804) or the
    string ``C25804``. Normalise to the canonical ``C…`` form."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return s if s.startswith("C") else f"C{s}"


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "basic", "preferred")


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _height_mm(extra: Any) -> float | None:
    """Best-effort height from the parametrics blob (often absent)."""
    if not isinstance(extra, dict):
        return None
    attrs = (
        extra.get("attributes") if isinstance(extra.get("attributes"), dict) else extra
    )
    for key in ("Height", "height", "Height(mm)", "Max Height"):
        val = attrs.get(key) if isinstance(attrs, dict) else None
        if val is None:
            continue
        num = "".join(c for c in str(val) if c.isdigit() or c in ".-")
        try:
            return float(num)
        except ValueError:
            continue
    return None


def normalize_jlcparts_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one jlcparts row to our ``parts`` columns; ``None`` if no C-number.

    jlcparts column quirks handled: ``lcsc`` may be int or ``C…``; ``mfr`` is
    the *manufacturer part number* while ``manufacturer`` is the maker;
    ``basic``/``preferred`` are the Basic-vs-Extended signal; ``extra`` carries
    parametrics. Every dump row is JLCPCB-assemblable.
    """
    lcsc = _lcsc_number(row.get("lcsc") or row.get("lcsc_id") or row.get("C"))
    if lcsc is None:
        return None
    extra = row.get("extra") or row.get("params") or {}
    return {
        "lcsc": lcsc,
        "mfr": row.get("manufacturer") or row.get("mfr_name"),
        "mfr_part": row.get("mfr")
        or row.get("mfr_part")
        or row.get("manufacturer_part"),
        "description": row.get("description") or "",
        "jlcpcb_assemblable": True,  # the dump IS the JLCPCB assembly catalog
        "basic": _to_bool(row.get("basic")) or _to_bool(row.get("preferred")),
        "stock": _to_int(row.get("stock")) or 0,
        "price": row.get("price"),  # JSON list of qty breaks → jsonb
        "package": row.get("package") or row.get("footprint"),
        "height_mm": _height_mm(extra),
        "params": extra if isinstance(extra, dict) else None,
        "datasheet_url": row.get("datasheet") or row.get("datasheet_url"),
    }


def min_unit_price(price: Any) -> float | None:
    """Cheapest unit price across the qty breaks (jlcparts ``price`` JSON)."""
    if not isinstance(price, list):
        return None
    vals = []
    for brk in price:
        if isinstance(brk, dict) and brk.get("price") is not None:
            try:
                vals.append(float(brk["price"]))
            except (TypeError, ValueError):
                continue
    return min(vals) if vals else None


def read_jlcparts_sqlite(path: str, *, batch: int = 5000) -> Iterator[dict[str, Any]]:
    """Yield raw rows from a jlcparts ``cache.sqlite3`` ``components`` table.

    JSON columns (``price`` / ``extra``) are decoded to Python; the rows are
    still *raw* (jlcparts column names) — feed each to
    :func:`normalize_jlcparts_row`. Streamed in batches so the full ~300k-row
    dump never loads at once.
    """
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM components")
        while True:
            chunk = cur.fetchmany(batch)
            if not chunk:
                break
            for r in chunk:
                row = dict(r)
                for jcol in ("price", "extra"):
                    if isinstance(row.get(jcol), str):
                        try:
                            row[jcol] = json.loads(row[jcol])
                        except (ValueError, TypeError):
                            pass
                yield row
    finally:
        conn.close()


def refresh_parts_from_sqlite(store: Any, path: str) -> dict[str, int]:
    """Flow A end-to-end: read a jlcparts SQLite dump → normalize → import
    (upsert + turnover). ``store`` provides :meth:`parts_import`. Returns the
    import counts. The per-minute worker / ``precis pcb refresh-parts`` CLI
    call this; here so it's testable against a fixture dump."""
    rows = [
        norm
        for raw in read_jlcparts_sqlite(path)
        if (norm := normalize_jlcparts_row(raw)) is not None
    ]
    return store.parts_import(rows)
