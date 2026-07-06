"""Thin ``httpx`` shim over the free SEC EDGAR APIs for the ``edgar`` kind.

Mirrors ``_patent_ops.py`` in shape (a ``Protocol`` the handler depends
on, a live client, and a fake for unit tests) but talks to three
key-less SEC endpoints instead of EPO OPS:

===================  =====================================================
Endpoint             Base
===================  =====================================================
Full-text search     ``https://efts.sec.gov/LATEST/search-index`` (JSON)
Submissions          ``https://data.sec.gov/submissions/CIK##########.json``
Filing archive       ``https://www.sec.gov/Archives/edgar/data/<cik>/<accn>/<doc>``
===================  =====================================================

Plus ``https://www.sec.gov/files/company_tickers.json`` for ticker→CIK
resolution (cached in-process with a TTL).

Unlike OPS, the SEC APIs need **no credentials** — only a descriptive
``User-Agent`` (SEC hard-blocks requests without one). They share a
**10 req/s** courtesy limit, so this shim self-throttles with a
client-side token bucket rather than reacting to throttling headers
(§ Divergences from patent, item 2).

All document/JSON methods return raw ``bytes``; parsing lives in
``_edgar_parse.py``. The live integration test
(``PRECIS_EDGAR_TEST_LIVE=1``) hits real SEC; unit tests use
``FakeEdgarClient`` from this module.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Endpoint constants — fixed trusted SEC hosts (only path/query vary)
# ---------------------------------------------------------------------------

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

#: SEC courtesy limit, requests per second, shared across all endpoints.
SEC_RATE_LIMIT_PER_SEC = 10.0

#: How long a fetched ticker→CIK map stays fresh before a re-fetch.
TICKER_MAP_TTL_SECONDS = 24 * 60 * 60  # 1 day


# ---------------------------------------------------------------------------
# Errors — mirror the OpsError family
# ---------------------------------------------------------------------------


class EdgarError(Exception):
    """Base class for EDGAR-shim errors."""


class EdgarNotFound(EdgarError):
    """SEC returned 404 — the filing / CIK doesn't exist."""


class EdgarRateLimited(EdgarError):
    """SEC throttled or blocked us (HTTP 429 / 403)."""


class EdgarHttpError(EdgarError):
    """Any other non-2xx SEC HTTP response, with status + body preview."""

    def __init__(self, status: int, body_preview: str) -> None:
        super().__init__(f"EDGAR HTTP {status}: {body_preview[:200]}")
        self.status = status
        self.body_preview = body_preview


# ---------------------------------------------------------------------------
# Result type for search()
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EdgarSearchResponse:
    """Search response — raw JSON bytes + byte count for fair-use accounting."""

    json: bytes
    bytes_out: int


# ---------------------------------------------------------------------------
# Client-side token bucket (10 req/s)
# ---------------------------------------------------------------------------


class TokenBucket:
    """Simple thread-safe token bucket for client-side throttling.

    ``rate`` tokens accrue per second up to ``capacity``. :meth:`acquire`
    blocks until a token is available. Wall-clock based on
    ``time.monotonic`` so it's immune to system-clock jumps.
    """

    def __init__(
        self, *, rate: float = SEC_RATE_LIMIT_PER_SEC, capacity: float | None = None
    ) -> None:
        self._rate = rate
        self._capacity = capacity if capacity is not None else rate
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self._rate
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Protocol — what the handler depends on
# ---------------------------------------------------------------------------


class EdgarClientProto(Protocol):
    """Subset of the live EDGAR client used by the handler + ingest.

    Tests pass a ``FakeEdgarClient`` implementing the same shape.
    ``resolve_ticker`` also satisfies
    :class:`precis.handlers._edgar_query.TickerResolver`.
    """

    def search(
        self, params: dict[str, str], *, from_: int = 0, size: int = 20
    ) -> EdgarSearchResponse: ...

    def submissions(self, cik: str) -> bytes: ...

    def filing_document(
        self, *, cik: str, accession_dashless: str, primary_doc: str
    ) -> bytes: ...

    def company_tickers(self) -> bytes: ...

    def resolve_ticker(self, ticker: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Live client
# ---------------------------------------------------------------------------


class EdgarClient:
    """Real client. Lazy-imports ``httpx`` on first use.

    The constructor doesn't talk to SEC — the first method call opens
    a client. All requests pass through the shared token bucket to
    honour SEC's 10 req/s courtesy limit.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        bucket: TokenBucket | None = None,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise EdgarError(
                "PRECIS_EDGAR_USER_AGENT must be set to a descriptive "
                "User-Agent (SEC blocks requests without one), e.g. "
                "'precis-mcp/x.y (you@example.com)'"
            )
        self._user_agent = user_agent.strip()
        self._timeout = timeout
        self._bucket = bucket if bucket is not None else TokenBucket()
        # In-process ticker→CIK cache (lowercased ticker → digit CIK).
        self._ticker_map: dict[str, str] | None = None
        self._ticker_map_fetched_at: float = 0.0

    # -- public methods -------------------------------------------------

    def search(
        self, params: dict[str, str], *, from_: int = 0, size: int = 20
    ) -> EdgarSearchResponse:
        query = dict(params)
        if from_:
            query["from"] = str(from_)
        # EDGAR FTS ignores unknown params; ``size`` isn't a real FTS
        # knob (it always returns 10/hit page) but we keep the arg for
        # signature parity with the patent client's range window.
        del size
        body = self._get(FTS_URL, params=query)
        return EdgarSearchResponse(json=body, bytes_out=len(body))

    def submissions(self, cik: str) -> bytes:
        cik10 = _cik10(cik)
        return self._get(SUBMISSIONS_URL.format(cik10=cik10))

    def filing_document(
        self, *, cik: str, accession_dashless: str, primary_doc: str
    ) -> bytes:
        cik_int = str(int(cik))
        url = f"{ARCHIVE_BASE}/{cik_int}/{accession_dashless}/{primary_doc}"
        return self._get(url)

    def company_tickers(self) -> bytes:
        return self._get(COMPANY_TICKERS_URL)

    def resolve_ticker(self, ticker: str) -> str | None:
        """Ticker symbol → CIK digit string, via the cached SEC map."""
        key = (ticker or "").strip().lower()
        if not key:
            return None
        self._ensure_ticker_map()
        assert self._ticker_map is not None
        return self._ticker_map.get(key)

    # -- helpers --------------------------------------------------------

    def _ensure_ticker_map(self) -> None:
        now = time.monotonic()
        fresh = (
            self._ticker_map is not None
            and (now - self._ticker_map_fetched_at) < TICKER_MAP_TTL_SECONDS
        )
        if fresh:
            return
        try:
            raw = self.company_tickers()
            self._ticker_map = parse_company_tickers(raw)
            self._ticker_map_fetched_at = now
        except EdgarError:
            # Keep any stale map on a fetch failure rather than wiping
            # resolution entirely; only initialise to empty on cold miss.
            if self._ticker_map is None:
                self._ticker_map = {}

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> bytes:
        from precis.utils.http import http_client, require_httpx

        httpx = require_httpx()
        self._bucket.acquire()
        try:
            with http_client(
                timeout=self._timeout,
                headers={"Accept-Encoding": "gzip, deflate"},
                # SEC hosts legitimately redirect (e.g. archive index);
                # hosts are fixed constants, not agent-supplied URLs.
                follow_redirects=True,
                user_agent=self._user_agent,
            ) as client:
                resp = client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise EdgarError(f"EDGAR transport error: {exc}") from exc
        return _check_response(resp)


def _check_response(resp: Any) -> bytes:
    """Translate an httpx response into bytes or an ``EdgarError``."""
    status = resp.status_code
    if status == 200:
        return bytes(resp.content)
    body = resp.text if hasattr(resp, "text") else ""
    if status == 404:
        raise EdgarNotFound(f"EDGAR 404: {body[:200]}")
    if status in (403, 429):
        raise EdgarRateLimited(f"EDGAR {status} (throttled/blocked): {body[:200]}")
    raise EdgarHttpError(status, body)


def _cik10(cik: str) -> str:
    """Digit CIK → zero-padded 10-digit form for the submissions URL."""
    digits = "".join(c for c in str(cik) if c.isdigit())
    if not digits:
        raise EdgarError(f"invalid CIK: {cik!r}")
    return str(int(digits)).zfill(10)


def parse_company_tickers(raw: bytes) -> dict[str, str]:
    """Parse ``company_tickers.json`` into ``{ticker_lower: cik_digits}``.

    SEC ships the map as an object keyed by row index::

        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

    Malformed rows are skipped rather than raising — a partial map still
    resolves the common tickers.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    rows: Any = data.values() if isinstance(data, dict) else data
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        cik = row.get("cik_str")
        if ticker and cik is not None:
            out[str(ticker).strip().lower()] = str(cik)
    return out


# ---------------------------------------------------------------------------
# Fake client — used by unit tests
# ---------------------------------------------------------------------------


class FakeEdgarClient:
    """Pre-loaded responses keyed by CIK / accession-doc / search-tuple.

    Tests construct one with dicts of canned bytes; calls miss the
    network entirely. Use ``raises`` to bind an exception to a key.
    """

    def __init__(
        self,
        *,
        searches: dict[str, bytes] | None = None,
        submissions: dict[str, bytes] | None = None,
        documents: dict[str, bytes] | None = None,
        tickers: dict[str, str] | None = None,
        company_tickers_json: bytes | None = None,
        raises: dict[tuple[str, str], EdgarError] | None = None,
    ) -> None:
        self._searches = dict(searches or {})
        self._submissions = dict(submissions or {})
        self._documents = dict(documents or {})
        self._tickers = {k.lower(): v for k, v in (tickers or {}).items()}
        self._company_tickers_json = company_tickers_json
        self._raises = dict(raises or {})
        self.calls: list[tuple[str, str]] = []

    def search(
        self, params: dict[str, str], *, from_: int = 0, size: int = 20
    ) -> EdgarSearchResponse:
        key = _search_key(params, from_=from_)
        body = self._lookup("search", key, self._searches)
        return EdgarSearchResponse(json=body, bytes_out=len(body))

    def submissions(self, cik: str) -> bytes:
        return self._lookup("submissions", str(int(cik)), self._submissions)

    def filing_document(
        self, *, cik: str, accession_dashless: str, primary_doc: str
    ) -> bytes:
        key = f"{int(cik)}/{accession_dashless}/{primary_doc}"
        return self._lookup("document", key, self._documents)

    def company_tickers(self) -> bytes:
        if self._company_tickers_json is not None:
            return self._company_tickers_json
        return json.dumps(
            {
                str(i): {"cik_str": int(cik), "ticker": tkr.upper()}
                for i, (tkr, cik) in enumerate(self._tickers.items())
            }
        ).encode()

    def resolve_ticker(self, ticker: str) -> str | None:
        return self._tickers.get((ticker or "").strip().lower())

    def _lookup(self, endpoint: str, key: str, bag: dict[str, bytes]) -> bytes:
        self.calls.append((endpoint, key))
        if (endpoint, key) in self._raises:
            raise self._raises[(endpoint, key)]
        try:
            return bag[key]
        except KeyError as e:
            raise EdgarNotFound(
                f"FakeEdgarClient has no {endpoint!r} response for {key!r}"
            ) from e


def _search_key(params: dict[str, str], *, from_: int = 0) -> str:
    """Stable key for a search request — sorted params + offset.

    Tests key their ``searches`` dict with the same helper (or just
    the ``q`` value; see the test module) so lookups are predictable.
    """
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{ordered}#{from_}" if from_ else ordered


__all__ = [
    "ARCHIVE_BASE",
    "COMPANY_TICKERS_URL",
    "FTS_URL",
    "SUBMISSIONS_URL",
    "EdgarClient",
    "EdgarClientProto",
    "EdgarError",
    "EdgarHttpError",
    "EdgarNotFound",
    "EdgarRateLimited",
    "EdgarSearchResponse",
    "FakeEdgarClient",
    "TokenBucket",
    "parse_company_tickers",
]
