"""money_refresh — the weekly FX/CPI cache fill (gr510555, slice 2 of
``docs/backlog/measure-currency-units.md``).

The measure currency conversion reads two cache tables — ``fx_rates``
(daily ECB euro reference quotes, units of the currency per 1 EUR) and
``price_index`` (annual series; today only the World Bank US CPI).
Reads never touch the network: this worker is the only writer and runs
as a ``money_refresh`` job on a recurring todo
(:mod:`precis.workers.job_types.money_refresh`).

Sources — module constants, public and keyless; never agent-supplied:

- ``ECB_HIST_URL``: ``eurofxref-hist.zip``, the full daily history
  since 1999-01-04 in one GET. Gap cells hold ``N/A`` or are empty;
  suspended quotes (ISK 2009–2017) are whole ``N/A`` runs.
- ``WB_CPI_URL``: the World Bank v2 API for the US CPI
  (``FP.CPI.TOTL``), annual from 1960.

Discipline: every fetch streams through ``safe_stream`` under
``call_with_backoff`` (one limiter token per attempt, ``Retry-After``
honoured) inside ``http_client(timeout=30)``, with a 20 MB wire cap
that aborts mid-stream — the body is never buffered unbounded (60 MB
on the unzipped CSV). Each source fetches, parses and upserts in ONE
transaction — a fetch or parse fault fails the job and leaves the
tables untouched. A parse that yields fewer than 90 % of the rows
already cached for that source is refused as a truncated download:
better a failed job than a cache that silently lost half its history.

The upserts skip unchanged rows (``IS DISTINCT FROM``), so a row's
``fetched_at`` marks the last value CHANGE; ``money_refresh_log`` bumps
on every successful poll and is the freshness answer.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from precis.taxonomy.money import CPI_SERIES
from precis.utils.http import (
    POLICIES,
    call_with_backoff,
    http_client,
    netloc_provider,
    polite_user_agent,
)
from precis.utils.safe_fetch import safe_stream

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
#: ``per_page`` because the default page (50) is shorter than the series
#: (~66 annual observations); one GET must carry the whole history.
WB_CPI_URL = (
    "https://api.worldbank.org/v2/country/USA/indicator/FP.CPI.TOTL"
    "?format=json&per_page=200"
)

#: Provenance stamps on the cache rows — also the truncation guard's
#: "rows already cached for that source" key.
ECB_SOURCE = "ecb:eurofxref-hist"
WB_SOURCE = "worldbank:v2"

_FETCH_TIMEOUT_S = 30.0
#: Wire cap per response — the job must fail, not buffer an unbounded body.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
#: Decompression cap on the ECB zip member (zip-bomb guard).
MAX_UNZIPPED_BYTES = 60 * 1024 * 1024
#: A parse yielding under this fraction of the cached rows is refused
#: as a truncated download.
TRUNCATION_FLOOR = 0.9

_SOURCES = ("ecb", "worldbank")

#: ``(url) -> response body bytes``; injected in tests.
Fetch = Callable[[str], bytes]

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

#: One fx_rates row as parsed: (currency, rate_date, per_eur).
FxRow = tuple[str, date, Decimal]
#: One price_index row as parsed: (series, year, value).
CpiRow = tuple[str, int, Decimal]


class MoneyRefreshError(RuntimeError):
    """A fetch, size-cap, parse or truncation-guard refusal."""


def _default_fetch(url: str) -> bytes:
    """Stream ``url`` to bytes under the politeness core.

    One attempt opens the stream with ``safe_stream`` and reads it in
    chunks; a transport error or a retryable status restarts the whole
    fetch via ``call_with_backoff``. ``MAX_RESPONSE_BYTES`` is a wire
    cap — a declared ``Content-Length`` over it or a stream crossing it
    aborts at once, so an oversized body is never fully read or
    buffered.
    """

    def attempt() -> bytes:
        body = bytearray()
        with http_client(
            timeout=_FETCH_TIMEOUT_S, user_agent=polite_user_agent()
        ) as client:
            with safe_stream(client, "GET", url) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("Content-Length")
                if (
                    declared is not None
                    and declared.isdigit()
                    and int(declared) > MAX_RESPONSE_BYTES
                ):
                    raise MoneyRefreshError(
                        f"Content-Length {declared} bytes > {MAX_RESPONSE_BYTES} cap"
                    )
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise MoneyRefreshError(
                            f"response exceeded {MAX_RESPONSE_BYTES} "
                            "byte cap mid-stream"
                        )
        return bytes(body)

    return call_with_backoff(
        attempt,
        service=netloc_provider(url) or "money_refresh",
        policy=POLICIES["web-fetch"],
        url=url,
    )


# ── parsers ──────────────────────────────────────────────────────────


def parse_ecb_hist(payload: bytes) -> list[FxRow]:
    """``eurofxref-hist.zip`` -> ``[(currency, rate_date, per_eur)]``.

    The single CSV member has a ``Date,USD,JPY,…`` header (trailing
    comma; the ECB appends new currencies at the end). ``N/A`` and empty
    cells are skipped — gaps and suspensions are absent data, not zero.
    A cell that is neither empty/``N/A`` nor a positive number, a
    non-ISO date, or a non-ISO currency column is a parse fault.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            members = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
            if len(members) != 1:
                raise MoneyRefreshError(
                    "eurofxref-hist: expected exactly one csv member, "
                    f"got {[i.filename for i in zf.infolist()]}"
                )
            info = members[0]
            if info.file_size > MAX_UNZIPPED_BYTES:
                raise MoneyRefreshError(
                    f"eurofxref-hist: {info.filename} unzips to "
                    f"{info.file_size} bytes > {MAX_UNZIPPED_BYTES} cap"
                )
            raw = zf.read(info)
    except zipfile.BadZipFile as exc:
        raise MoneyRefreshError(f"eurofxref-hist: not a zip: {exc}") from exc
    if len(raw) > MAX_UNZIPPED_BYTES:
        # A lying file_size must not smuggle an oversized body through.
        raise MoneyRefreshError(
            f"eurofxref-hist: {info.filename} unzipped to {len(raw)} "
            f"bytes > {MAX_UNZIPPED_BYTES} cap"
        )
    rows: list[FxRow] = []
    reader = csv.reader(io.StringIO(raw.decode("utf-8")))
    header = next(reader, None)
    if not header or header[0].strip() != "Date":
        raise MoneyRefreshError("eurofxref-hist: header does not start 'Date'")
    currencies = [c.strip() for c in header[1:] if c.strip()]
    bad = [c for c in currencies if not _ISO_CURRENCY_RE.match(c)]
    if not currencies or bad:
        raise MoneyRefreshError(
            f"eurofxref-hist: bad currency column(s) {bad or currencies!r}"
        )
    for lineno, line in enumerate(reader, start=2):
        if not line or not line[0].strip():
            continue
        try:
            day = date.fromisoformat(line[0].strip())
        except ValueError as exc:
            raise MoneyRefreshError(
                f"eurofxref-hist line {lineno}: bad date {line[0]!r}"
            ) from exc
        for i, ccy in enumerate(currencies):
            cell = line[i + 1].strip() if i + 1 < len(line) else ""
            if not cell or cell == "N/A":
                continue
            try:
                rate = Decimal(cell)
            except InvalidOperation as exc:
                raise MoneyRefreshError(
                    f"eurofxref-hist line {lineno}: bad rate {cell!r} for {ccy}"
                ) from exc
            if rate <= 0:
                raise MoneyRefreshError(
                    f"eurofxref-hist line {lineno}: non-positive rate "
                    f"{cell!r} for {ccy}"
                )
            rows.append((ccy, day, rate))
    if not rows:
        raise MoneyRefreshError("eurofxref-hist: no rates parsed")
    return rows


def parse_worldbank_cpi(payload: Any) -> list[CpiRow]:
    """World Bank v2 ``[meta, rows]`` payload -> ``[(series, year, value)]``.

    ``value`` is ``None`` for years without an estimate (the current
    year, early series gaps) — skipped, never fabricated. The ``meta``
    block must agree with the body: ``pages > 1``, or a row count short
    of ``total``, is a silent short read — a fault, not a partial fill.
    Anything else malformed is a parse fault.
    """
    if not (
        isinstance(payload, list) and len(payload) == 2 and isinstance(payload[1], list)
    ):
        raise MoneyRefreshError("worldbank cpi: expected [meta, rows]")
    meta = payload[0] if isinstance(payload[0], dict) else {}
    try:
        pages = int(meta["pages"]) if meta.get("pages") is not None else 1
        total = int(meta["total"]) if meta.get("total") is not None else len(payload[1])
    except (TypeError, ValueError) as exc:
        raise MoneyRefreshError(f"worldbank cpi: bad meta {meta!r}") from exc
    if pages > 1:
        raise MoneyRefreshError(
            f"worldbank cpi: meta says {pages} pages — one GET must "
            "carry the whole series"
        )
    if len(payload[1]) < total:
        raise MoneyRefreshError(
            f"worldbank cpi: {len(payload[1])} rows, meta total {total} — a short read"
        )
    out: list[CpiRow] = []
    for row in payload[1]:
        if not isinstance(row, dict):
            raise MoneyRefreshError(f"worldbank cpi: bad row {row!r}")
        value = row.get("value")
        if value is None:
            continue
        try:
            year = int(str(row["date"]))
            v = Decimal(str(value))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise MoneyRefreshError(f"worldbank cpi: bad row {row!r}") from exc
        if not 1900 <= year <= 2200 or v <= 0:
            raise MoneyRefreshError(f"worldbank cpi: bad row {row!r}")
        out.append((CPI_SERIES, year, v))
    if not out:
        raise MoneyRefreshError("worldbank cpi: no observations parsed")
    return out


# ── upsert ───────────────────────────────────────────────────────────

#: ``IS DISTINCT FROM`` skips unchanged rows — the weekly poll rewrites
#: only what moved, so ``fetched_at`` marks the last change and a quiet
#: week costs zero dead tuples.
_FX_UPSERT = (
    "INSERT INTO fx_rates (currency, rate_date, per_eur, source, fetched_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (currency, rate_date) DO UPDATE SET "
    "per_eur = EXCLUDED.per_eur, source = EXCLUDED.source, "
    "fetched_at = EXCLUDED.fetched_at "
    "WHERE fx_rates.per_eur IS DISTINCT FROM EXCLUDED.per_eur"
)
_CPI_UPSERT = (
    "INSERT INTO price_index (series, year, value, source, fetched_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (series, year) DO UPDATE SET "
    "value = EXCLUDED.value, source = EXCLUDED.source, "
    "fetched_at = EXCLUDED.fetched_at "
    "WHERE price_index.value IS DISTINCT FROM EXCLUDED.value"
)
#: Freshness marker: one row per source, bumped on every successful
#: refresh — cache ``fetched_at`` only moves on a change.
_REFRESH_LOG_UPSERT = (
    "INSERT INTO money_refresh_log (source, refreshed_at, rows_written) "
    "VALUES (%s, now(), %s) "
    "ON CONFLICT (source) DO UPDATE SET "
    "refreshed_at = EXCLUDED.refreshed_at, "
    "rows_written = EXCLUDED.rows_written"
)


def _upsert_source(store: Store, source: str, rows: list[Any]) -> int:
    """Guard + upsert one source's parse inside ONE transaction."""
    if source == "ecb":
        table, upsert, stamp = "fx_rates", _FX_UPSERT, ECB_SOURCE
        params = [(c, d, r, stamp) for c, d, r in rows]
    else:
        table, upsert, stamp = "price_index", _CPI_UPSERT, WB_SOURCE
        params = [(s, y, v, stamp) for s, y, v in rows]
    with store.tx() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE source = %s", (stamp,)
        ).fetchone()
        cached = int(row[0]) if row else 0
        if cached and len(rows) < TRUNCATION_FLOOR * cached:
            raise MoneyRefreshError(
                f"{source}: parsed {len(rows)} rows < "
                f"{TRUNCATION_FLOOR:.0%} of the {cached} cached — "
                "refusing a truncated download"
            )
        conn.cursor().executemany(upsert, params)
        conn.execute(_REFRESH_LOG_UPSERT, (source, len(rows)))
    return len(rows)


# ── the pass ─────────────────────────────────────────────────────────


def run_money_refresh(
    store: Store,
    *,
    sources: Iterable[str] | None = None,
    fetch: Fetch | None = None,
) -> dict[str, int]:
    """Fetch + parse + upsert each configured source.

    Returns ``{source: rows_upserted}``. Each source runs in its own
    transaction, so a fault leaves the tables untouched; after every
    source has been attempted (a good source still commits), the first
    failures raise one :class:`MoneyRefreshError` naming each failed
    source and why.
    """
    wanted = list(sources) if sources is not None else list(_SOURCES)
    unknown = [s for s in wanted if s not in _SOURCES]
    if unknown:
        raise MoneyRefreshError(
            f"unknown source(s) {unknown!r}; expected {sorted(_SOURCES)}"
        )
    do_fetch = fetch or _default_fetch
    counts: dict[str, int] = {}
    errors: list[str] = []
    for source in wanted:
        try:
            url = ECB_HIST_URL if source == "ecb" else WB_CPI_URL
            body = do_fetch(url)
            if len(body) > MAX_RESPONSE_BYTES:
                raise MoneyRefreshError(
                    f"{source}: response {len(body)} bytes > {MAX_RESPONSE_BYTES} cap"
                )
            if source == "ecb":
                rows: list[Any] = parse_ecb_hist(body)
            else:
                rows = parse_worldbank_cpi(json.loads(body))
            counts[source] = _upsert_source(store, source, rows)
        except Exception as exc:
            log.warning("money_refresh: %s failed", source, exc_info=True)
            errors.append(f"{source}: {exc}")
    if errors:
        raise MoneyRefreshError("; ".join(errors))
    return counts


__all__ = [
    "CPI_SERIES",
    "ECB_HIST_URL",
    "ECB_SOURCE",
    "MAX_RESPONSE_BYTES",
    "MAX_UNZIPPED_BYTES",
    "TRUNCATION_FLOOR",
    "WB_CPI_URL",
    "WB_SOURCE",
    "Fetch",
    "MoneyRefreshError",
    "parse_ecb_hist",
    "parse_worldbank_cpi",
    "run_money_refresh",
]
