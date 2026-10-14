"""JLCPCB parts-library top-up (Basic + Preferred) from the public website.

Prod ``parts`` was filled from the yaqwsx/jlcparts ``source-db-v2`` dump, but
that dump has no C-number below ~6.37M (upstream's walk lost the low range),
so JLCPCB Basic parts (C25804, C1525, …) were missing, and the Open API
(:mod:`precis.pcb.jlc_api`) answers HTTP 500 on every call (gr464537). This
module tops up the *library* (~1.6k Basic + Preferred rows) from the
credential-less listing endpoint behind jlcpcb.com's parts-library page:
``GET`` an XSRF cookie, then ``POST`` the paged list with the token echoed in
a header. Applied after every dump reload (``precis pcb refresh-parts``) and
in the daily ``parts_refresh`` pass.

The server's ``hasNextPage`` is False even when more pages exist, so paging
runs on ``pages``. ``assemblyComponentFlag`` is false even for Basic parts
and is deliberately unused: the library is JLCPCB's assembly library by
definition. The host is fixed, never agent-supplied.

The same listing endpoint takes a ``keyword`` field — that is the *live*
lookup path (gr485250): ``get``/``search`` for a part the catalog misses
queries it directly (:func:`live_lookup`, :func:`live_search`), so Extended
parts the jlcparts dump never carried are still reachable. Live calls use
the tighter :data:`LIVE_POLICY` (an agent is waiting) and a short
in-process negative cache (:data:`_LIVE_MISS_TTL_S`) so a genuinely absent
C-number doesn't re-hit the site on every retry.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from precis.pcb import catalog
from precis.pcb._http import BULK_POLICY, Policy, VendorError, with_backoff
from precis.utils.coerce import to_int

if TYPE_CHECKING:
    import httpx

    from precis.store import Store

log = logging.getLogger(__name__)

HOST = "https://jlcpcb.com"
XSRF_PATH = "/api/overseas-pcb-order/v1/getAll"
LIST_PATH = "/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"

#: One listing walk per filter; a part in both (Basic and Preferred) is
#: deduped by C-number.
LIBRARY_FILTERS: tuple[dict[str, Any], ...] = (
    {"componentLibraryType": "base"},
    {"preferredComponentFlag": True},
)

#: Safety cap per filter (pages of 100; the library is ~13 + ~13 pages).
MAX_PAGES = 50
_PAGE_SIZE = 100
_SERVICE = "jlcpcb-web"

#: Live single-part/keyword lookups run inside a tool call — a tighter
#: policy than the bulk walk: fewer attempts, a small cap, and a shorter
#: breaker cooldown so an agent is not pinned for minutes on a wedged
#: endpoint.
LIVE_POLICY = Policy(attempts=3, base=0.6, cap=8.0, breaker_cooldown=120.0)

#: Page size / page budget for live keyword queries. The listing ranks by
#: relevance, so an exact C-number — when it exists — lands on page one;
#: the extra pages are a safety net for the endpoint's fuzzy matching
#: (a never-issued number still returns neighbours).
_LIVE_PAGE_SIZE = 25
_MAX_LIVE_PAGES = 4

#: Negative cache for live lookups that came back empty: ``{C-no: monotonic
#: at miss}``. A real miss (endpoint reachable, no exact row) is stable
#: enough that re-asking within the TTL is just latency.
_LIVE_MISS_TTL_S = 300.0
_live_misses: dict[str, float] = {}

#: One list request body in -> decoded JSON out. Swappable in tests.
PostFn = Callable[[dict[str, Any]], dict[str, Any]]


def reset_live_cache() -> None:
    """Clear the live-lookup negative cache (tests; an operator retry)."""
    _live_misses.clear()


class _WebPoster:
    """Real transport: XSRF handshake once, then POSTs via ``safe_stream``."""

    def __init__(self, *, timeout: float = 30.0, policy: Policy = BULK_POLICY) -> None:
        from precis.utils.http import http_client

        self._client = http_client(timeout=timeout)
        self._policy = policy
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _xsrf(self) -> str:
        if self._token is None:
            from precis.utils.safe_fetch import safe_get

            resp = with_backoff(
                lambda: safe_get(self._client, f"{HOST}{XSRF_PATH}"),
                service=_SERVICE,
                policy=self._policy,
            )
            token = resp.cookies.get("XSRF-TOKEN") or self._client.cookies.get(
                "XSRF-TOKEN"
            )
            if not token:
                raise VendorError(_SERVICE, "no XSRF-TOKEN cookie from handshake")
            self._token = token
        return self._token

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        from precis.utils.safe_fetch import safe_stream

        token = self._xsrf()
        headers = {
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": token,
            "Cookie": f"XSRF-TOKEN={token}",
        }
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)

        def _thunk() -> httpx.Response:
            with safe_stream(
                self._client,
                "POST",
                f"{HOST}{LIST_PATH}",
                headers=headers,
                content=payload,
            ) as resp:
                resp.read()
                return resp

        resp = with_backoff(_thunk, service=_SERVICE, policy=self._policy)
        if resp.status_code != 200:
            raise VendorError(
                _SERVICE,
                f"HTTP {resp.status_code} from {LIST_PATH}",
                status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise VendorError(_SERVICE, f"non-JSON response from {LIST_PATH}") from exc
        return data if isinstance(data, dict) else {}


def walk_listing(
    store: Store,
    *,
    row_budget: int,
    start_page: int = 1,
    page_size: int = _PAGE_SIZE,
    post: PostFn | None = None,
) -> dict[str, Any]:
    """Bounded pull of the UNFILTERED listing — the whole ~7.27M-row
    JLCPCB-assemblable catalog in ascending ``componentCode`` order
    (verified 2026-10-14: a bare ``{currentPage, pageSize}`` POST returns
    ``total=7269753``). This is the credential-free substitute for the
    dead ``getComponentInfos`` Open API walk (gr481902/gr464537): same
    catalog scope, plus per-row fields the API never normalized
    (``params._category``/``_lifecycle``/``_lcsc_url``).

    The listing has no ``lastKey`` cursor — ``currentPage`` IS the resume
    token, so this returns ``next_page`` for the caller to checkpoint
    (``None`` when the walk reached the last page or an empty page — the
    caller stores "" and tomorrow starts a fresh sweep). New parts land
    at the END of the ascending order, so a wrap's early pages are a
    stock/price rescan, not a discovery pass — same profile as the old
    cursor walk."""
    poster: _WebPoster | None = None
    send = post
    if send is None:
        poster = _WebPoster()
        send = poster
    rows: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    last_page = start_page - 1
    pages = 0
    try:
        page = start_page
        while len(rows) < row_budget:
            try:
                data = send({"currentPage": page, "pageSize": page_size})
            except VendorError:
                raise
            except Exception as exc:
                raise VendorError(_SERVICE, f"listing walk failed: {exc!r}") from exc
            info = (data.get("data") or {}).get("componentPageInfo") or {}
            batch = list(info.get("list") or [])
            pages = to_int(info.get("pages")) or 0
            for raw in batch:
                if (n := normalize_library_row(raw)) is not None:
                    rows.append(n)
            last_page = page
            if not batch or page >= pages:
                break
            page += 1
        counts = store.parts_import(rows) if rows else {"upserted": 0, "restocked": 0}
        exhausted = not batch or last_page >= pages
        return {
            "rows": len(rows),
            **counts,
            "next_page": None if exhausted else last_page + 1,
            "pages": pages,
        }
    finally:
        if poster is not None:
            poster.close()


def iter_library_rows(post: PostFn | None = None) -> Iterator[dict[str, Any]]:
    """Raw listing rows for every :data:`LIBRARY_FILTERS` walk, deduped by
    ``componentCode``. Pages on ``pages`` (``hasNextPage`` lies)."""
    poster: _WebPoster | None = None
    send: PostFn
    if post is None:
        poster = _WebPoster()
        send = poster
    else:
        send = post
    seen: set[str] = set()
    try:
        for flt in LIBRARY_FILTERS:
            page = 1
            while True:
                data = send({"currentPage": page, "pageSize": _PAGE_SIZE, **flt})
                info = (data.get("data") or {}).get("componentPageInfo") or {}
                for row in info.get("list") or []:
                    code = row.get("componentCode")
                    if code and code not in seen:
                        seen.add(code)
                        yield row
                pages = to_int(info.get("pages")) or 0
                if page >= pages:
                    break
                if page >= MAX_PAGES:
                    log.warning(
                        "jlc_library: hit MAX_PAGES=%d for filter %s (pages=%d)",
                        MAX_PAGES,
                        flt,
                        pages,
                    )
                    break
                page += 1
    finally:
        if poster is not None:
            poster.close()


def _price(raw: Any) -> list[dict[str, Any]] | None:
    out: list[dict[str, Any]] = []
    for b in raw or []:
        try:
            end = int(b["endNumber"])
            out.append(
                {
                    "qFrom": int(b["startNumber"]),
                    "qTo": end if end >= 0 else None,
                    "price": float(b["productPrice"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            return None
    return out or None


#: Listing-row fields that carry a lifecycle / buyability signal for the
#: EOL sweep (view='availability'). All confirmed present on the live
#: ``selectSmtComponentList`` row (2026-10-14); any of them can be null.
#: ``merged`` is the strongest EOL tell — JLCPCB folded this part into
#: another C-number — ``alts`` names suggested successors, ``no_buy`` a
#: sale stop reason, ``est`` a restock estimate, ``idle`` a slow-moving
#: flag, ``loss`` write-offs.
_LIFECYCLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("mergedComponentCode", "merged"),
    ("componentAlternativesCode", "alts"),
    ("noBuyReason", "no_buy"),
    ("idleFlag", "idle"),
    ("estimateDate", "est"),
    ("lossNumber", "loss"),
)


def normalize_library_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One listing row -> the ``parts`` columns
    :func:`precis.pcb.catalog.normalize_jlcparts_v2_row` produces. ``None``
    when the row has no C-number.

    ``params`` additionally carries two ``_``-prefixed vendor keys the dump
    never supplies: ``_lcsc_url`` (the lcsc.com product page) and
    ``_lifecycle`` (EOL/buyability signals, only when at least one is
    non-empty — see :data:`_LIFECYCLE_FIELDS`). Both ride the parametric
    blob the same way ``_category`` does; a bare code+flags stub row still
    gets ``params=None``."""
    lcsc = catalog._lcsc_number(row.get("componentCode"))
    if lcsc is None:
        return None
    params: dict[str, Any] = {}
    for attr in row.get("attributes") or []:
        name = attr.get("attribute_name_en")
        if name:
            params[name] = attr.get("attribute_value_name")
    cat = " / ".join(
        s for s in (row.get("secondSortName"), row.get("firstSortName")) if s
    )
    if cat:
        params["_category"] = cat
    if url := row.get("lcscGoodsUrl"):
        params["_lcsc_url"] = url
    lifecycle = {
        key: v
        for src, key in _LIFECYCLE_FIELDS
        if (v := row.get(src)) not in (None, "", 0)
    }
    if lifecycle:
        params["_lifecycle"] = lifecycle
    return {
        "lcsc": lcsc,
        "mfr": row.get("componentBrandEn") or None,
        "mfr_part": row.get("componentModelEn") or None,
        "description": row.get("describe") or "",
        "jlcpcb_assemblable": True,
        "basic": row.get("componentLibraryType") == "base"
        or bool(row.get("preferredComponentFlag")),
        "stock": to_int(row.get("stockCount")) or 0,
        "price": _price(row.get("componentPrices")),
        "package": row.get("componentSpecificationEn") or None,
        "height_mm": catalog._height_mm(params),
        "params": params or None,
        "datasheet_url": row.get("dataManualUrl") or None,
    }


def refresh_library_parts(
    store: Store, *, post: PostFn | None = None
) -> dict[str, int]:
    """Fetch the Basic + Preferred library and upsert it into ``parts``.
    An empty listing is a failure (:class:`VendorError`), never a no-op.

    Any fetch/parse failure surfaces as :class:`VendorError` — ``with_backoff``
    only wraps ``httpx.HTTPError``, and ``safe_fetch.SsrfBlocked`` (a DNS blip
    at connect) or a malformed envelope would otherwise escape and kill the
    caller's API walk. ``parts_import`` (DB) errors still propagate."""
    try:
        rows = [
            n
            for raw in iter_library_rows(post)
            if (n := normalize_library_row(raw)) is not None
        ]
    except VendorError:
        raise
    except Exception as exc:
        raise VendorError(_SERVICE, f"parts-library fetch failed: {exc!r}") from exc
    if not rows:
        raise VendorError(_SERVICE, "parts-library listing returned no rows")
    counts = store.parts_import(rows)
    return {"rows": len(rows), **counts}


def _live_poster(post: PostFn | None) -> tuple[PostFn, _WebPoster | None]:
    """``(send, poster-to-close)`` for a live call: the injected fake, or a
    real poster on the interactive policy + a tighter socket timeout."""
    if post is not None:
        return post, None
    poster = _WebPoster(timeout=15.0, policy=LIVE_POLICY)
    return poster, poster


def iter_keyword_rows(
    keyword: str,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
    max_pages: int = 1,
) -> Iterator[dict[str, Any]]:
    """Raw listing rows for one ``keyword`` search, paged on ``pages`` like
    :func:`iter_library_rows` (``hasNextPage`` lies there too), bounded by
    ``max_pages``. Live policy + timeout when no ``post`` is injected."""
    poster: _WebPoster | None = None
    if post is None:
        send, poster = _live_poster(post)
    else:
        send = post
    try:
        page = 1
        while True:
            data = send(
                {
                    "currentPage": page,
                    "pageSize": page_size,
                    "keyword": str(keyword),
                }
            )
            info = (data.get("data") or {}).get("componentPageInfo") or {}
            yield from info.get("list") or []
            pages = to_int(info.get("pages")) or 0
            if page >= pages or page >= max_pages:
                break
            page += 1
    finally:
        if poster is not None:
            poster.close()


def live_lookup(
    store: Store, lcsc: str, *, post: PostFn | None = None
) -> dict[str, Any] | None:
    """Fetch one C-number live from the listing endpoint and upsert it.

    The endpoint fuzzy-matches a keyword (a never-issued number returns
    neighbours, not an empty page), so only an exact ``componentCode`` match
    counts — neighbours are NOT imported: importing them would fill the
    catalog with rows the agent never asked for under a query that looks
    like a hit for ``lcsc``. Returns the normalized ``parts``-shape dict,
    or ``None`` on a real miss (negative-cached for
    :data:`_LIVE_MISS_TTL_S`)."""
    key = str(lcsc).strip().upper()
    missed_at = _live_misses.get(key)
    if missed_at is not None and time.monotonic() - missed_at < _LIVE_MISS_TTL_S:
        return None
    hit: dict[str, Any] | None = None
    try:
        for raw in iter_keyword_rows(key, post=post, max_pages=_MAX_LIVE_PAGES):
            if str(raw.get("componentCode") or "").strip().upper() == key:
                hit = raw
                break
    except VendorError:
        raise
    except Exception as exc:
        raise VendorError(_SERVICE, f"live part lookup failed: {exc!r}") from exc
    if hit is None:
        _live_misses[key] = time.monotonic()
        return None
    row = normalize_library_row(hit)
    if row is None:
        _live_misses[key] = time.monotonic()
        return None
    _live_misses.pop(key, None)
    store.parts_import([row])
    return row


def live_search(
    store: Store,
    q: str,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Keyword-search the listing endpoint, upsert every part-shaped row,
    and return the normalized rows — the live fallback for
    ``search(kind='part')`` when the local catalog has no hit. One page,
    bounded by ``page_size``."""
    rows = [
        n
        for raw in iter_keyword_rows(q, post=post, page_size=page_size, max_pages=1)
        if (n := normalize_library_row(raw)) is not None
    ]
    if rows:
        store.parts_import(rows)
    return rows


#: Live lookups per batch refresh — an interactive cap so a stale-heavy
#: board can't hang a tool call; the un-refreshed tail just stays stale.
_LIVE_BATCH_MAX = 12


def live_refresh_many(
    store: Store,
    lcscs: list[str],
    *,
    post: PostFn | None = None,
    limit: int = _LIVE_BATCH_MAX,
) -> dict[str, str]:
    """Live-lookup each C-number, one shared poster, bounded by ``limit``.

    Returns ``{lcsc: "hit" | "miss" | "error: <detail>"}``; entries past
    ``limit`` are absent (the caller reports them as still-stale)."""
    send, poster = _live_poster(post)
    out: dict[str, str] = {}
    try:
        for lcsc in list(lcscs)[:limit]:
            try:
                out[str(lcsc)] = (
                    "hit" if live_lookup(store, lcsc, post=send) is not None else "miss"
                )
            except VendorError as exc:
                out[str(lcsc)] = f"error: {exc}"
    finally:
        if poster is not None:
            poster.close()
    return out


__all__ = [
    "HOST",
    "LIBRARY_FILTERS",
    "LIST_PATH",
    "LIVE_POLICY",
    "MAX_PAGES",
    "XSRF_PATH",
    "PostFn",
    "iter_keyword_rows",
    "iter_library_rows",
    "live_lookup",
    "live_refresh_many",
    "live_search",
    "normalize_library_row",
    "refresh_library_parts",
    "reset_live_cache",
    "walk_listing",
]
