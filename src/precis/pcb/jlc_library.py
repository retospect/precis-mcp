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
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from precis.pcb import catalog
from precis.pcb._http import BULK_POLICY, VendorError, with_backoff
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

#: One list request body in -> decoded JSON out. Swappable in tests.
PostFn = Callable[[dict[str, Any]], dict[str, Any]]


class _WebPoster:
    """Real transport: XSRF handshake once, then POSTs via ``safe_stream``."""

    def __init__(self) -> None:
        from precis.utils.http import http_client

        self._client = http_client(timeout=30.0)
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _xsrf(self) -> str:
        if self._token is None:
            from precis.utils.safe_fetch import safe_get

            resp = with_backoff(
                lambda: safe_get(self._client, f"{HOST}{XSRF_PATH}"),
                service=_SERVICE,
                policy=BULK_POLICY,
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

        resp = with_backoff(_thunk, service=_SERVICE, policy=BULK_POLICY)
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


def normalize_library_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One listing row -> the ``parts`` columns
    :func:`precis.pcb.catalog.normalize_jlcparts_v2_row` produces. ``None``
    when the row has no C-number."""
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


__all__ = [
    "HOST",
    "LIBRARY_FILTERS",
    "LIST_PATH",
    "MAX_PAGES",
    "XSRF_PATH",
    "PostFn",
    "iter_library_rows",
    "normalize_library_row",
    "refresh_library_parts",
]
