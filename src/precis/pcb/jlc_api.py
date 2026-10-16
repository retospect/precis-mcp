"""JLCPCB Open API client — live stock/price verification (Flow B).

Complements the community jlcparts dump (:mod:`precis.pcb.catalog`, Flow A):
the dump is a daily snapshot, this client is the "is it in stock *right
now*" check at part-selection time, and the bulk incremental pull that can
eventually replace the dump. Degrades to ``None``/an empty walk whenever
credentials are absent — callers fall back to the dump, never hard-fail on
a missing vault entry.

Auth — spike-verified 2026-08-27 against ``open.jlcpcb.com`` (**not**
``api.jlcpcb.com``, which 404s: that host is the portal SPA). Per-request
HMAC-SHA256 signing, no token-issuance endpoint, so there is nothing here to
cache or refresh — see :func:`sign_request`. Do not re-derive this; it is
recorded in ``docs/backlog/pcb-guided-place-route.md`` (Export + order).

Every outbound call funnels through :meth:`JlcApiClient._call`, which wraps
the wire send in :func:`precis.pcb._http.with_backoff` — 401/403 are never
retried (an auth failure looping forever looks exactly like an attack). A
403 here means one specific, expected thing: the signature is accepted but
the app's Open API console hasn't been granted the Components scope. That
is a human console action, not a bug in this file — :class:`JlcPermissionError`
says so explicitly so nobody re-debugs the signing code chasing it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis import secrets
from precis.pcb._http import (
    BULK_POLICY,
    DEFAULT_POLICY,
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

    #: One raw wire send: method + path + headers (Authorization already
    #: set) + pre-serialized body → the response. Swappable in tests so the
    #: client can be exercised with zero network and no real ``httpx.Client``.
    SendFn = Callable[[str, str, dict[str, str], str], httpx.Response]

log = logging.getLogger(__name__)

#: The Open API host. Fixed, never agent-supplied — see module docstring.
HOST = "https://open.jlcpcb.com"

#: Bulk enumeration only: a ``lastKey`` cursor walk.
COMPONENT_INFO_PATH = "/overseas/openapi/component/getComponentInfos"
#: Lookup by LCSC code. ``getComponentInfos`` is the paged bulk walk and
#: ignores a ``componentCodes`` filter (the first row came back C1002 for a
#: C2128 probe); this endpoint takes ``{"componentCodes": ["C2040"]}``.
COMPONENT_DETAIL_PATH = "/overseas/openapi/component/getComponentDetailByCode"

#: Secret names in the vault (``precis.secrets`` — NOT env vars, NOT the
#: deploy template).
APP_ID_SECRET = "JLCPCB_APP_ID"
ACCESS_KEY_SECRET = "JLCPCB_ACCESS_KEY"
SECRET_KEY_SECRET = "JLCPCB_SECRET_KEY"

_REQUEST_TIMEOUT_S = 30.0

#: ``external_rate_limits`` row pacing the Open API (jlc_api + jlc_order).
_RATE_PROVIDER = "jlcpcb"


def signing_timestamp() -> str:
    """Now as the ``timestamp`` signing field, in **seconds**.

    JLC's signature docs (api.jlcpcb.com/docs/api-request-signature) say
    seconds. The client sent milliseconds until 2026-10-16, which JLC
    answers with HTTP 500 "unknown error" -- the "outage since 10-07"
    (gr464537, gr481902, gr493051). Verified live 2026-10-16 19:21Z: ms ->
    500, seconds -> 200 with componentInfos. No opt-out.
    """
    return str(int(time.time()))


class JlcPermissionError(VendorError):
    """403 from JLCPCB: signature verified, console scope not granted.

    The single highest-value error message in this file — it exists so a
    403 reads as "go grant the Components scope in the console" instead of
    tempting someone to re-litigate the HMAC signing.
    """


@dataclass(frozen=True)
class Credentials:
    app_id: str
    access_key: str
    secret_key: str


def credentials_available(*, store: Store | None = None) -> bool:
    """True iff all three JLCPCB secrets resolve — the kind-availability gate."""
    return all(
        secrets.is_available(name, store=store)
        for name in (APP_ID_SECRET, ACCESS_KEY_SECRET, SECRET_KEY_SECRET)
    )


def _load_credentials(*, store: Store | None) -> Credentials | None:
    """``None`` when any secret is unset — the constructor's degrade path,
    not an exception. A call site that must fail loudly instead should check
    :func:`credentials_available` itself before constructing anything."""
    if not credentials_available(store=store):
        return None
    return Credentials(
        app_id=secrets.require_secret(APP_ID_SECRET, store=store),
        access_key=secrets.require_secret(ACCESS_KEY_SECRET, store=store),
        secret_key=secrets.require_secret(SECRET_KEY_SECRET, store=store),
    )


def sign_request(
    secret_key: str, *, method: str, path: str, timestamp: str, nonce: str, body: str
) -> str:
    """The exact JLCPCB signature — spike-verified, do not re-derive:
    ``base64(HMAC-SHA256(secret_key, "{METHOD}\\n{path}\\n{timestamp}\\n{nonce}\\n{body}\\n"))``.
    """
    string_to_sign = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    digest = hmac.new(
        secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _authorization_header(
    creds: Credentials, *, method: str, path: str, body: str, timestamp: str, nonce: str
) -> str:
    signature = sign_request(
        creds.secret_key,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return (
        f'JOP appid="{creds.app_id}",accesskey="{creds.access_key}",'
        f'timestamp="{timestamp}",nonce="{nonce}",signature="{signature}"'
    )


def _default_send(
    method: str, path: str, headers: dict[str, str], body: str
) -> httpx.Response:
    """Real transport: a fixed-host client via :func:`http_client`, sent
    through :func:`safe_stream`. The body is pre-serialized by the caller
    (not httpx's ``json=``) so the bytes we sign are exactly the bytes on
    the wire — no risk of a signature/body mismatch from a differing JSON
    serialization. ``resp.read()`` buffers the body before the client
    context closes, so the returned response stays usable."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_stream

    with http_client(timeout=_REQUEST_TIMEOUT_S) as client:
        with safe_stream(
            client, method, f"{HOST}{path}", headers=headers, content=body
        ) as resp:
            resp.read()
            return resp


def _extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the component list out of a response envelope, tolerant of the
    wrapper-key variations common to this API family (``data``/``result``
    nesting one level around the actual list)."""
    for key in (
        "componentInfos",
        "componentDetailResponseVOList",
        "list",
        "rows",
        "data",
        "result",
    ):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            nested = _extract_rows(val)
            if nested:
                return nested
    return []


def _extract_cursor(data: dict[str, Any]) -> str | None:
    """The ``lastKey`` resume cursor, wherever the envelope put it."""
    for key in ("lastKey", "last_key", "nextKey", "cursor"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            nested = _extract_cursor(val)
            if nested:
                return nested
    return None


def _lcsc_number(raw: Any) -> str | None:
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


def normalize_api_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one JLCPCB Open API component row to the same ``parts`` columns
    :func:`precis.pcb.catalog.normalize_jlcparts_row` produces, so
    :meth:`precis.store._pcb_ops.PcbMixin.parts_import` accepts either
    source's rows unchanged. ``None`` when the row carries no C-number.
    """
    lcsc = _lcsc_number(
        row.get("componentCode")
        or row.get("lcscCode")
        or row.get("lcsc")
        or row.get("C")
    )
    if lcsc is None:
        return None
    return {
        "lcsc": lcsc,
        "mfr": row.get("manufacturer") or row.get("manufacturerName"),
        "mfr_part": row.get("mfrPartNumber")
        or row.get("componentModel")
        or row.get("mfr_part"),
        "description": row.get("description") or row.get("componentName") or "",
        "jlcpcb_assemblable": True,  # every row this endpoint returns is
        "basic": _to_bool(row.get("basic"))
        or _to_bool(row.get("preferred"))
        or str(row.get("componentLibraryType") or "").strip().lower() == "base",
        "stock": to_int(row.get("stockCount") or row.get("stock")) or 0,
        "price": row.get("componentPrices") or row.get("price"),
        "package": row.get("componentSpecificationEn")
        or row.get("componentSpecification")
        or row.get("package"),
        "height_mm": None,  # not carried by this endpoint; the footprint
        # cache / dump remain the source for physical dimensions.
        "params": None,
        "datasheet_url": row.get("dataManualUrl") or row.get("datasheet_url"),
    }


class JlcApiClient:
    """Live JLCPCB stock/price lookups (slice 2 — no ordering, no upload).

    Construct once per call site; it never raises on missing credentials
    (:attr:`available` is ``False`` instead) so a caller can build one
    unconditionally and let :meth:`component_info`/:meth:`iter_components`
    degrade. ``send`` is the network seam for tests — bypass real
    httpx/network entirely by injecting a fake ``(method, path, headers,
    body) -> httpx.Response``.
    """

    def __init__(
        self,
        *,
        store: Store | None = None,
        send: SendFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        pace: Callable[[str], object] = acquire,
    ) -> None:
        self._store = store
        self._creds = _load_credentials(store=store)
        self._send = send or _default_send
        self._sleep = sleep
        self._now = now
        #: Fleet-wide pacing (``external_rate_limits`` row ``jlcpcb``), taken
        #: before every wire attempt, retries included. Injected in tests.
        self._pace = pace
        #: Resume cursor for :meth:`iter_components`, updated after every
        #: page — a caller that stops the walk early reads this to
        #: checkpoint. ``None`` until the first page comes back.
        self.last_key: str | None = None

    @property
    def available(self) -> bool:
        """True iff all three credentials resolved at construction time."""
        return self._creds is not None

    def _roundtrip(
        self,
        thunk: Callable[[], httpx.Response],
        *,
        policy: Policy,
        idempotent: bool = True,
    ) -> httpx.Response:
        """Run one prepared send through :func:`with_backoff`; translate a
        403 into :class:`JlcPermissionError` (see module docstring). Split
        from :meth:`_call` so :mod:`precis.pcb.jlc_order`'s upload path —
        which signs a ``meta`` form field instead of the JSON body — gets
        the identical retry/permission behaviour instead of a copy."""

        def _paced() -> httpx.Response:
            self._pace(_RATE_PROVIDER)
            return thunk()

        try:
            return with_backoff(
                _paced,
                service="jlcpcb",
                policy=policy,
                sleep=self._sleep,
                now=self._now,
                idempotent=idempotent,
            )
        except VendorError as exc:
            if exc.status == 403:
                raise JlcPermissionError(
                    "jlcpcb",
                    "API insufficient permissions (HTTP 403) — the request "
                    "signature is valid, but this app has not been granted "
                    "the scope this endpoint needs (Components for "
                    "/component/*, PCB for /pcb/*). This is a human action "
                    "in the JLCPCB Open API console (grant the scope for "
                    "this app), not an engineering problem — do not "
                    "re-debug the signing code. Retry once the scope is "
                    "granted.",
                    status=403,
                ) from exc
            raise

    @staticmethod
    def _finish(resp: httpx.Response, path: str) -> dict[str, Any]:
        """The post-wire half of :meth:`_call`: status gate + JSON decode."""
        if resp.status_code != 200:
            raise VendorError(
                "jlcpcb",
                f"HTTP {resp.status_code} from {path}",
                status=resp.status_code,
                trace_id=resp.headers.get(TRACE_ID_HEADER),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise VendorError("jlcpcb", f"non-JSON response from {path}") from exc
        if not isinstance(data, dict):
            return {}
        return check_envelope("jlcpcb", data, resp, path)

    def _call(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        policy: Policy,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        """Sign + send one JSON POST through :func:`with_backoff`; return
        the decoded body. Raises :class:`JlcPermissionError` on 403 (see
        module docstring) and lets any other :class:`VendorError`/
        :class:`VendorUnavailable` propagate as-is."""
        creds = self._creds
        if creds is None:  # not an assert — asserts vanish under -O
            raise VendorError("jlcpcb", "no credentials; check .available first")
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        def _thunk() -> httpx.Response:
            timestamp = signing_timestamp()
            nonce = uuid.uuid4().hex
            headers = {
                "Content-Type": "application/json",
                "Authorization": _authorization_header(
                    creds,
                    method="POST",
                    path=path,
                    body=body,
                    timestamp=timestamp,
                    nonce=nonce,
                ),
            }
            return self._send("POST", path, headers, body)

        resp = self._roundtrip(_thunk, policy=policy, idempotent=idempotent)
        return self._finish(resp, path)

    def component_info(self, lcsc: str) -> dict[str, Any] | None:
        """One C-number's live existence/stock/price/basic-vs-extended —
        the "in stock now" check at part-selection time, as opposed to
        dump-age stock. ``None`` when credentials are absent (caller falls
        back to the community dump) or the part isn't found."""
        if self._creds is None:
            return None
        data = self._call(
            COMPONENT_DETAIL_PATH, {"componentCodes": [lcsc]}, policy=DEFAULT_POLICY
        )
        want = _lcsc_number(lcsc)
        for raw in _extract_rows(data):
            norm = normalize_api_row(raw)
            if norm is not None and norm["lcsc"] == want:
                return norm
        return None

    def iter_components(
        self, *, since_key: str | None = None, page_size: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Bulk enumeration over :data:`COMPONENT_INFO_PATH`'s ``lastKey``
        cursor. Yields normalized rows; after the generator is exhausted or
        the caller stops early, :attr:`last_key` holds the cursor to resume
        from. Yields nothing (does not raise) when credentials are absent —
        the per-minute refresh job just skips this source that cycle. 403
        (scope not granted) surfaces as :class:`JlcPermissionError` rather
        than looking like a silent empty walk.
        """
        self.last_key = since_key
        if self._creds is None:
            log.info("jlc_api: iter_components skipped — no credentials configured")
            return
        cursor = since_key
        while True:
            # First page omits lastKey entirely: a JSON null made prod's
            # getComponentInfos answer HTTP 500 on every call (gr464537,
            # best-guess fix pending a prod probe).
            payload: dict[str, Any] = {"pageSize": page_size}
            if cursor:
                payload["lastKey"] = cursor
            data = self._call(COMPONENT_INFO_PATH, payload, policy=BULK_POLICY)
            rows = _extract_rows(data)
            previous, cursor = cursor, _extract_cursor(data)
            self.last_key = cursor
            for raw in rows:
                norm = normalize_api_row(raw)
                if norm is not None:
                    yield norm
            if not rows or not cursor:
                break
            if cursor == previous:
                # A cursor that doesn't advance while rows keep arriving is
                # how a bulk walk turns into an unbounded hammer on someone
                # else's server. Stop and say so; last_key lets an operator
                # resume deliberately.
                log.warning(
                    "jlc_api: lastKey cursor stalled (%r) — ending walk to "
                    "avoid re-requesting the same page indefinitely",
                    cursor,
                )
                break


#: Negative cache for live lookups that came back empty: ``{C-no: monotonic
#: at miss}``. A real miss (endpoint reachable, no exact row) is stable
#: enough that re-asking within the TTL is just latency.
_LIVE_MISS_TTL_S = 300.0
_live_misses: dict[str, float] = {}

#: Live lookups per batch refresh -- an interactive cap so a stale-heavy
#: board can't hang a tool call; the un-refreshed tail just stays stale.
_LIVE_BATCH_MAX = 12


def reset_live_cache() -> None:
    """Clear the live-lookup negative cache (tests; an operator retry)."""
    _live_misses.clear()


def live_lookup(
    store: Store, lcsc: str, *, client: JlcApiClient | None = None
) -> dict[str, Any] | None:
    """Fetch one C-number live via :meth:`JlcApiClient.component_info` and
    upsert it into ``parts`` (the catalog-miss fallback behind
    ``get(kind='part')``). ``None`` on a real miss, negative-cached for
    :data:`_LIVE_MISS_TTL_S`. Without credentials this raises
    :class:`VendorError` -- a missing key is not a miss, so it is neither
    cached nor reported as "no such part"."""
    client = client or JlcApiClient(store=store)
    if not client.available:
        raise VendorError("jlcpcb", "no JLCPCB API credentials configured")
    key = str(lcsc).strip().upper()
    missed_at = _live_misses.get(key)
    if missed_at is not None and time.monotonic() - missed_at < _LIVE_MISS_TTL_S:
        return None
    row = client.component_info(key)
    if row is None:
        _live_misses[key] = time.monotonic()
        return None
    _live_misses.pop(key, None)
    store.parts_import([row])
    return row


def live_refresh_many(
    store: Store,
    lcscs: list[str],
    *,
    client: JlcApiClient | None = None,
    limit: int = _LIVE_BATCH_MAX,
) -> dict[str, str]:
    """Live-lookup each C-number, bounded by ``limit``. Returns
    ``{lcsc: "hit" | "miss" | "error: <detail>"}``; entries past ``limit``
    are absent (the caller reports them as still-stale)."""
    client = client or JlcApiClient(store=store)
    out: dict[str, str] = {}
    for lcsc in list(lcscs)[:limit]:
        try:
            hit = live_lookup(store, lcsc, client=client) is not None
            out[str(lcsc)] = "hit" if hit else "miss"
        except VendorError as exc:
            out[str(lcsc)] = f"error: {exc}"
    return out


__all__ = [
    "ACCESS_KEY_SECRET",
    "APP_ID_SECRET",
    "COMPONENT_DETAIL_PATH",
    "COMPONENT_INFO_PATH",
    "HOST",
    "SECRET_KEY_SECRET",
    "Credentials",
    "JlcApiClient",
    "JlcPermissionError",
    "credentials_available",
    "live_lookup",
    "live_refresh_many",
    "normalize_api_row",
    "reset_live_cache",
    "sign_request",
]
