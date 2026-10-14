"""Parts-catalog normalization (Flow A).

The primary source is the community **jlcparts** dump (yaqwsx/jlcparts) — the
whole JLCPCB assembly catalog as a daily SQLite/JSON. Everything in it is
JLCPCB-*assemblable* by definition (that flag is the one thing only JLCPCB
knows; Octopart/Nexar, Digi-Key, Mouser carry parametrics/stock but not it).

This module is the pure row → ``parts``-column adapter, kept separate from the
DB import (:meth:`precis.store._pcb_ops.PcbMixin.parts_import`) so it is
testable without a database and a second source (Nexar enrichment) can layer
on later.

Two dump formats are read (see :func:`jlcparts_format`): the legacy v1
``components`` table (~300k rows) and the current ``source-db-v2``
(``meta.format``) with ``jlc_components`` (~2M rows, most of them not
JLCPCB-assemblable — see :func:`normalize_jlcparts_v2_row`).

The 2026-10 v2 dump lacks C-numbers below ~6.37M (upstream's walk lost them,
gr464537), so dump loads are followed by the :mod:`precis.pcb.jlc_library`
top-up of the Basic/Preferred library.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from precis.utils.coerce import to_int

if TYPE_CHECKING:
    from precis.store import Store

#: An agent-typed C-number: ``C`` + 3–8 digits (any case). The smallest
#: catalogued parts are 3-digit (C67, …) and the dump walks well into
#: 8 digits.
_LCSC_ID_RE = re.compile(r"[Cc](\d{3,8})")

#: A C-number embedded in a pasted product URL — ``…_C2655145.html``
#: (lcsc.com), ``…/C2655145`` (jlcpcb.com partdetail/urlSuffix),
#: ``?searchTxt=C2655145``. Alphanumeric boundaries (not ``\b`` — ``_``
#: is a word char) so ``RN60C9992FB14`` does not match.
_LCSC_URL_RE = re.compile(r"(?<![A-Za-z0-9])[Cc](\d{3,8})(?![A-Za-z0-9])")


def lcsc_id(raw: Any) -> str | None:
    """Canonical ``C<n>`` for an agent-supplied part id, or ``None``.

    Accepts a bare ``C…`` (any case) and a pasted LCSC/JLCPCB product URL —
    the C-number is extracted from the string; the URL is never fetched.
    Anything else returns ``None`` (callers keep their existing handling
    for non-C-number ids)."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _LCSC_ID_RE.fullmatch(s)
    if m is not None:
        return f"C{m.group(1)}"
    m = _LCSC_URL_RE.search(s)
    return f"C{m.group(1)}" if m is not None else None


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
    if "library_type" in row:  # source-db-v2 column set
        return normalize_jlcparts_v2_row(row)
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
        "stock": to_int(row.get("stock")) or 0,
        "price": row.get("price"),  # JSON list of qty breaks → jsonb
        "package": row.get("package") or row.get("footprint"),
        "height_mm": _height_mm(extra),
        "params": extra if isinstance(extra, dict) else None,
        "datasheet_url": row.get("datasheet") or row.get("datasheet_url"),
    }


# v2 ``assembly_mode`` values that mean "JLCPCB places it by machine".
_V2_MACHINE_MODES = frozenset({"smtweld", "furnaceweld"})


def parse_v2_price(raw: Any) -> list[dict[str, Any]] | None:
    """v2 ``price`` text ``"1-49:0.0204,50-149:0.0168,5000-:0.0108"`` →
    the v1 qty-break list ``[{"qFrom":1,"qTo":49,"price":0.0204}, …]`` (open
    top break has ``qTo`` None). ``None`` for empty/unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    out: list[dict[str, Any]] = []
    for part in raw.split(","):
        rng, _, val = part.partition(":")
        lo, _, hi = rng.partition("-")
        try:
            out.append(
                {
                    "qFrom": int(lo),
                    "qTo": int(hi) if hi.strip() else None,
                    "price": float(val),
                }
            )
        except ValueError:
            return None
    return out or None


def normalize_jlcparts_v2_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one ``jlc_components`` row (``source-db-v2``) to ``parts`` columns.

    * ``lcsc`` int → ``C<n>``; ``mfr`` → ``mfr_part``; ``manufacturer`` →
      ``mfr``; ``attributes`` (JSON text) → ``params``; ``price`` text →
      qty-break list (:func:`parse_v2_price`); ``stock`` → ``stock``.
    * ``basic`` = ``library_type == 'base'`` OR ``preferred = 1`` (same
      Basic-or-Preferred collapse as v1). Upstream marks Basic as ``base``,
      everything else ``expand``.
    * ``jlcpcb_assemblable`` = ``present = 1`` AND ``assembly_mode`` is a
      machine-placement mode (``smtWeld``/``furnaceWeld``). Deliberately NOT
      the dump's ``assembly`` column: on the 2026-10 dump it is 0 for
      all 753 SMT *preferred* parts (clearly assemblable) and 1 only for
      73k manual-weld THT rows, so it is not an assemblability flag. The
      other ~1.7M rows (manualWeld/THT, no mode) are kept but flagged
      ``False`` so the selector's hard filter skips them.
    * ``package``/``category``/``description``/``datasheet`` straight over;
      category rides in ``params`` under ``_category`` (``parts`` has no
      column for it).
    """
    lcsc = _lcsc_number(row.get("lcsc"))
    if lcsc is None:
        return None
    attrs = row.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            attrs = None
    params: dict[str, Any] | None = attrs if isinstance(attrs, dict) else None
    category = " / ".join(c for c in (row.get("category"), row.get("subcategory")) if c)
    if category:
        params = {**(params or {}), "_category": category}
    mode = str(row.get("assembly_mode") or "").strip().lower()
    return {
        "lcsc": lcsc,
        "mfr": row.get("manufacturer") or None,
        "mfr_part": row.get("mfr") or None,
        "description": row.get("description") or "",
        "jlcpcb_assemblable": bool(to_int(row.get("present", 1)))
        and mode in _V2_MACHINE_MODES,
        "basic": str(row.get("library_type") or "").strip().lower() == "base"
        or bool(to_int(row.get("preferred"))),
        "stock": to_int(row.get("stock")) or 0,
        "price": parse_v2_price(row.get("price")),
        "package": row.get("package") or None,
        "height_mm": _height_mm(params),
        "params": params,
        "datasheet_url": row.get("datasheet") or None,
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


_V2_COLUMNS = (
    "lcsc",
    "present",
    "category",
    "subcategory",
    "mfr",
    "package",
    "manufacturer",
    "library_type",
    "preferred",
    "description",
    "datasheet",
    "stock",
    "price",
    "attributes",
    "assembly_mode",
)


def jlcparts_format(conn: sqlite3.Connection) -> str:
    """``"v2"`` (``jlc_components`` / ``meta.format = source-db-v2``) or
    ``"v1"`` (legacy ``components``). Raises ``ValueError`` for neither."""
    # Name via a local so tests/test_sql_schema_drift.py's static extractor
    # doesn't try to EXPLAIN this SQLite query against the Postgres schema.
    catalog_table = "sqlite_master"
    tables = {
        r[0]
        for r in conn.execute(f"SELECT name FROM {catalog_table} WHERE type='table'")
    }
    if "jlc_components" in tables:
        return "v2"
    if "components" in tables:
        return "v1"
    raise ValueError(
        "not a jlcparts dump: neither a 'jlc_components' (source-db-v2) nor a "
        f"'components' (v1) table (found: {sorted(tables)})"
    )


def read_jlcparts_sqlite(path: str, *, batch: int = 5000) -> Iterator[dict[str, Any]]:
    """Yield raw rows from a jlcparts ``cache.sqlite3`` (v1 or v2).

    v1 JSON columns (``price`` / ``extra``) are decoded to Python; v2 rows
    are yielded as-is (text ``price``/``attributes``, decoded by
    :func:`normalize_jlcparts_v2_row`) and filtered to ``present = 1`` (rows
    upstream no longer sees on the JLCPCB site). The rows are still *raw*
    (jlcparts column names) — feed each to :func:`normalize_jlcparts_row`.
    Streamed with ``fetchmany`` so even the ~2M-row v2 dump never loads at
    once (one batch of dicts in memory at a time).
    """
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        fmt = jlcparts_format(conn)
        # Explicit column list rather than ``SELECT *`` — derived from
        # ``PRAGMA table_info`` (not hardcoded) so this stays tolerant of
        # jlcparts dump schema drift: :func:`normalize_jlcparts_row`
        # deliberately checks multiple alias column names per field
        # because upstream's column names have moved before. A fixed list
        # would silently drop rows (or hard-error) the moment a real dump
        # doesn't match this file's test fixture's column set.
        # Quote each identifier (doubling embedded ``"``): the dump is an
        # externally-supplied file, so a reserved-word or adversarial
        # column name must neither break the query nor splice into it.
        table = "jlc_components" if fmt == "v2" else "components"
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        if fmt == "v2":
            # skip the wide unused columns (attrition, assembly_process, …)
            columns = [c for c in _V2_COLUMNS if c in columns]
        quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
        sql = f"SELECT {quoted} FROM {table}" if columns else f"SELECT * FROM {table}"
        if fmt == "v2" and "present" in columns:
            sql += " WHERE present = 1"
        cur = conn.execute(sql)
        while True:
            chunk = cur.fetchmany(batch)
            if not chunk:
                break
            for r in chunk:
                row = dict(r)
                if fmt == "v1":
                    for jcol in ("price", "extra"):
                        if isinstance(row.get(jcol), str):
                            try:
                                row[jcol] = json.loads(row[jcol])
                            except (ValueError, TypeError):
                                pass
                else:
                    row.setdefault("library_type", "")  # normalizer's v2 marker
                yield row
    finally:
        conn.close()


def refresh_parts_from_sqlite(store: Store, path: str) -> dict[str, int]:
    """Flow A end-to-end: read a jlcparts SQLite dump → normalize → import
    (per-row upsert + turnover via :meth:`Store.parts_import`). Returns the
    import counts. Right-sized for a small/incremental dump (and this
    module's own test fixtures); a real dump refresh (v1 ~300k, v2 ~2M rows) should use
    :func:`bulk_refresh_parts_from_sqlite` instead — see that function."""
    rows = [
        norm
        for raw in read_jlcparts_sqlite(path)
        if (norm := normalize_jlcparts_row(raw)) is not None
    ]
    return store.parts_import(rows)


def bulk_refresh_parts_from_sqlite(store: Store, path: str) -> dict[str, int]:
    """Flow A full-dump reload: read a jlcparts SQLite dump → normalize →
    staging + atomic swap (:meth:`precis.store._pcb_ops.PcbMixin.
    parts_bulk_replace`, per the 0047 design) — the scale lever for the
    whole catalog (v1 ~300k, v2 ~2M rows), unlike :func:`refresh_parts_from_sqlite`'s
    per-row upsert. Streams rows as a generator (never materializes the
    full dump as a list) so a real dump doesn't have to fit in memory.
    ``precis pcb refresh-parts --from-sqlite`` (and its no-API-credentials
    fallback) calls this."""
    rows = (
        norm
        for raw in read_jlcparts_sqlite(path)
        if (norm := normalize_jlcparts_row(raw)) is not None
    )
    return store.parts_bulk_replace(rows)
