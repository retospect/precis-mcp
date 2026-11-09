"""Venue metrics feeds (gr606422): Scimago SJR CSV + OpenAlex ``/sources``.

Two free feeds fill ``venue_metrics`` (migration 0293); JCR is licensed
and stays out.

* **Scimago** — the yearly ``scimagojr`` CSV (CC BY) parses to one
  ``{issn, year, title, sjr, sjr_quartile}`` row per ISSN the venue lists
  (print + electronic both land, keyed separately). The file's delimiter
  and decimal-comma ``SJR`` cell are sniffed, never assumed.
* **OpenAlex** — ``GET /sources?filter=issn:<issn>`` per distinct corpus
  ISSN (the venue table only needs journals our papers actually appear
  in — ~thousands of targeted calls, not the 250k-source dump). Each
  returned source's ``summary_stats`` lands
  ``openalex_2yr_mean_citedness`` + ``openalex_h_index`` on rows keyed by
  *every* ISSN the source lists, at the fetch year.

Fixed OpenAlex host (no SSRF surface); the shared ``openalex`` politeness
row paces the calls — same discipline as
:mod:`precis.ingest.openalex_meta`, whose ``_params``/``_ua`` helpers this
reuses so the paid ``api_key`` and honest mailto ride along identically.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.ingest.openalex_meta import _params, _short_id, _ua
from precis.store._venue_ops import ISSN_NORMALISE_SQL, normalise_issn
from precis.utils.http import call_with_backoff, http_client, request_with_deadline

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

_OPENALEX_SOURCES = "https://api.openalex.org/sources"
_TIMEOUT_S = 30.0

_YEAR_RE = re.compile(r"(19|20)\d{2}")
_ISSN_TOKEN_RE = re.compile(r"[0-9Xx][0-9Xx\-]{6,}[0-9Xx]")
_QUARTILES = ("Q1", "Q2", "Q3", "Q4")


def scimago_year_from_name(name: str) -> int | None:
    """The vintage year a ``scimagojr`` filename carries, else ``None``."""
    m = _YEAR_RE.search(str(name))
    return int(m.group(0)) if m else None


def _sniff_dialect(text: str) -> csv.Dialect:
    """Delimiter from the header line; ``;`` is the scimagojr default."""
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=";,\t")()
    except csv.Error:

        class _Semicolon(csv.excel):
            delimiter = ";"

        return _Semicolon()


def _sjr_value(raw: str | None) -> float | None:
    """``SJR`` cell → float. Scimago writes decimal commas (``2,709``)."""
    v = str(raw or "").strip()
    if not v:
        return None
    if "," in v and "." not in v:
        v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _cell(row: Mapping[str, Any], *names: str) -> str:
    """First non-empty cell across case-folded column name variants."""
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k}
    for n in names:
        v = lowered.get(n.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def parse_scimago_csv(text: str, *, year: int) -> list[dict[str, Any]]:
    """One ``venue_metrics`` row per ISSN per CSV line, at ``year``.

    Column names are matched case-folded (``Issn`` / ``Title`` / ``SJR`` /
    ``SJR Best Quartile``) because the export's exact casing has moved
    between vintages. A venue's ``Issn`` cell lists every ISSN it
    publishes under — each lands its own row. Quartiles outside
    ``Q1``–``Q4`` (``-``, blank) parse to ``None``.
    """
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff_dialect(text))
    rows: list[dict[str, Any]] = []
    for line in reader:
        title = _cell(line, "Title") or None
        sjr = _sjr_value(_cell(line, "SJR"))
        quartile_raw = _cell(line, "SJR Best Quartile", "Quartile").upper()
        quartile = quartile_raw if quartile_raw in _QUARTILES else None
        seen: set[str] = set()
        for token in _ISSN_TOKEN_RE.findall(_cell(line, "Issn", "ISSN")):
            issn = normalise_issn(token)
            if issn and issn not in seen:
                seen.add(issn)
                rows.append(
                    {
                        "issn": issn,
                        "year": year,
                        "title": title,
                        "sjr": sjr,
                        "sjr_quartile": quartile,
                    }
                )
    return rows


def openalex_source_rows(
    source: Mapping[str, Any], *, year: int
) -> list[dict[str, Any]]:
    """One ``venue_metrics`` row per ISSN an OpenAlex ``source`` lists.

    Carries the ``summary_stats`` snapshot — ``2yr_mean_citedness`` +
    ``h_index`` — onto every ISSN the source publishes under
    (``issn_l`` + the ``issn`` list), so the corpus join hits whichever
    form a paper's ``meta.issn`` carries.
    """
    stats = source.get("summary_stats") or {}
    if not isinstance(stats, Mapping):
        stats = {}
    source_id = _short_id(str(source.get("id") or ""))
    title = source.get("display_name")
    issns = {
        n
        for n in (
            normalise_issn(str(i))
            for i in [source.get("issn_l"), *(source.get("issn") or [])]
        )
        if n
    }
    return [
        {
            "issn": issn,
            "year": year,
            "title": str(title).strip() if title else None,
            "openalex_source_id": source_id or None,
            "openalex_2yr_mean_citedness": stats.get("2yr_mean_citedness"),
            "openalex_h_index": stats.get("h_index"),
        }
        for issn in sorted(issns)
    ]


def corpus_issns(store: Store) -> list[str]:
    """Distinct normalized ISSNs the live paper corpus cites — the OA
    leg's target list (venues we actually hold papers from)."""
    norm = ISSN_NORMALISE_SQL % "r.meta->>'issn'"
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {norm} FROM refs r "
            "WHERE r.kind = 'paper' AND r.retired_at IS NULL "
            "AND r.meta ? 'issn'"
        ).fetchall()
    return sorted(str(r[0]) for r in rows if r[0] and len(str(r[0])) == 8)


def _hyphenate(issn: str) -> str:
    """``13858947`` → ``1385-8947`` — the form OpenAlex's ``issn:`` filter
    indexes."""
    return f"{issn[:4]}-{issn[4:]}" if len(issn) == 8 else issn


def fetch_sources_by_issn(
    issn: str, *, email: str = "", timeout: float = _TIMEOUT_S
) -> list[dict[str, Any]]:
    """``GET /sources?filter=issn:…`` — the OA sources publishing under
    ``issn`` (usually one). Returns ``[]`` on no match; HTTP errors raise
    through the shared politeness core."""
    params = {**_params(email), "filter": f"issn:{_hyphenate(issn)}", "per-page": "25"}

    def once() -> Any:
        with http_client(timeout=timeout, headers={"User-Agent": _ua(email)}) as client:
            resp = request_with_deadline(
                client,
                2 * timeout,
                lambda: client.get(_OPENALEX_SOURCES, params=params),
            )
            resp.raise_for_status()
            return resp.json()

    data = call_with_backoff(once, service="openalex")
    results = data.get("results") if isinstance(data, Mapping) else None
    return [r for r in results or [] if isinstance(r, dict)]


def openalex_venue_rows(
    issns: list[str],
    *,
    year: int | None = None,
    email: str = "",
    fetch: Any = None,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield ``venue_metrics`` rows for each corpus ISSN's OA sources.

    ``fetch`` is the single-ISSN door (injectable for tests);
    ``year`` defaults to the fetch year (UTC). A source listing several
    ISSNs fans out to one row each via :func:`openalex_source_rows`;
    fetch failures log-and-skip so one dead ISSN can't sink the sweep.
    """
    if year is None:
        year = datetime.now(UTC).year
    fetch_fn = fetch if fetch is not None else fetch_sources_by_issn
    for i, issn in enumerate(issns):
        if limit is not None and i >= limit:
            return
        try:
            sources = fetch_fn(issn, email=email)
        except Exception as exc:
            log.warning(
                "venue-metrics: OpenAlex fetch failed for issn %s: %s", issn, exc
            )
            continue
        for source in sources:
            yield from openalex_source_rows(source, year=year)
