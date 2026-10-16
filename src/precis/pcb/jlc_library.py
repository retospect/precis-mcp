"""Live keyword search of jlcpcb.com's parts listing -- the ONE website call left.

The Open API (:mod:`precis.pcb.jlc_api`) has a bulk cursor walk and a lookup
by C-number, but no keyword/free-text query. ``search(kind='part', q=...)``
falls back to a keyword search when the local catalog has no hit, and that is
the gap this module still covers, via the listing endpoint behind the
parts-library page: ``GET`` an XSRF cookie, then ``POST`` the list with the
token echoed in a header. Interactive only, one page per query; the catalog
refresh and C-number lookups all go through the Open API (gr464537). That
endpoint sits under ``/api``, which jlcpcb.com's robots.txt disallows, so it
leaves as soon as the Open API gains a keyword query. The host is fixed,
never agent-supplied. Live calls use the tight :data:`LIVE_POLICY` (an
agent is waiting).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from precis.pcb import catalog
from precis.pcb._http import (
    BULK_POLICY,
    TRACE_ID_HEADER,
    Policy,
    VendorError,
    check_envelope,
    with_backoff,
)
from precis.utils.coerce import to_int
from precis.utils.rate_limit import acquire

if TYPE_CHECKING:
    import httpx

    from precis.store import Store

log = logging.getLogger(__name__)

HOST = "https://jlcpcb.com"
XSRF_PATH = "/api/overseas-pcb-order/v1/getAll"
LIST_PATH = "/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"

_SERVICE = "jlcpcb-web"

#: Live keyword lookups run inside a tool call — a tighter
#: policy than the bulk walk: fewer attempts, a small cap, and a shorter
#: breaker cooldown so an agent is not pinned for minutes on a wedged
#: endpoint.
LIVE_POLICY = Policy(attempts=3, base=0.6, cap=8.0, breaker_cooldown=120.0)

#: Page size for live keyword queries (one page per query).
_LIVE_PAGE_SIZE = 25

#: One list request body in -> decoded JSON out. Swappable in tests.
PostFn = Callable[[dict[str, Any]], dict[str, Any]]


class _WebPoster:
    """Real transport: XSRF handshake once, then POSTs via ``safe_stream``."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        policy: Policy = BULK_POLICY,
        pace: Callable[[str], object] = acquire,
    ) -> None:
        from precis.utils.http import http_client

        self._client = http_client(timeout=timeout)
        self._policy = policy
        #: Fleet-wide pacing (``external_rate_limits`` row ``jlcpcb-web``),
        #: taken before every wire attempt, retries included.
        self._pace = pace
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _xsrf(self) -> str:
        if self._token is None:
            from precis.utils.safe_fetch import safe_get

            def _handshake() -> httpx.Response:
                self._pace(_SERVICE)
                return safe_get(self._client, f"{HOST}{XSRF_PATH}")

            resp = with_backoff(
                _handshake,
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
            self._pace(_SERVICE)
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
                trace_id=resp.headers.get(TRACE_ID_HEADER),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise VendorError(_SERVICE, f"non-JSON response from {LIST_PATH}") from exc
        if not isinstance(data, dict):
            return {}
        return check_envelope(_SERVICE, data, resp, LIST_PATH)


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


__all__ = [
    "HOST",
    "LIST_PATH",
    "LIVE_POLICY",
    "XSRF_PATH",
    "PostFn",
    "iter_keyword_rows",
    "live_search",
    "normalize_library_row",
]
