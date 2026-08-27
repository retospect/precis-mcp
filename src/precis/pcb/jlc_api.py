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
    Policy,
    VendorError,
    with_backoff,
)

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

#: Both single-lookup and bulk enumeration hit this one endpoint — a single
#: C-number filter for the former, a ``lastKey`` cursor walk for the latter.
COMPONENT_INFO_PATH = "/overseas/openapi/component/getComponentInfos"

#: Secret names in the vault (``precis.secrets`` — NOT env vars, NOT the
#: deploy template).
APP_ID_SECRET = "JLCPCB_APP_ID"
ACCESS_KEY_SECRET = "JLCPCB_ACCESS_KEY"
SECRET_KEY_SECRET = "JLCPCB_SECRET_KEY"

_REQUEST_TIMEOUT_S = 30.0


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
    for key in ("componentInfos", "list", "rows", "data", "result"):
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


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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
        "stock": _to_int(row.get("stockCount") or row.get("stock")) or 0,
        "price": row.get("componentPrices") or row.get("price"),
        "package": row.get("componentSpecificationEn") or row.get("package"),
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
    ) -> None:
        self._store = store
        self._creds = _load_credentials(store=store)
        self._send = send or _default_send
        self._sleep = sleep
        self._now = now
        #: Resume cursor for :meth:`iter_components`, updated after every
        #: page — a caller that stops the walk early reads this to
        #: checkpoint. ``None`` until the first page comes back.
        self.last_key: str | None = None

    @property
    def available(self) -> bool:
        """True iff all three credentials resolved at construction time."""
        return self._creds is not None

    def _call(
        self, path: str, payload: dict[str, Any], *, policy: Policy
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
            timestamp = str(int(time.time() * 1000))
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

        try:
            resp = with_backoff(
                _thunk,
                service="jlcpcb",
                policy=policy,
                sleep=self._sleep,
                now=self._now,
            )
        except VendorError as exc:
            if exc.status == 403:
                raise JlcPermissionError(
                    "jlcpcb",
                    "API insufficient permissions (HTTP 403) — the request "
                    "signature is valid, but this app has not been granted "
                    "the Components API scope. This is a human action in the "
                    "JLCPCB Open API console (grant the scope for this app), "
                    "not an engineering problem — do not re-debug the signing "
                    "code. Retry once the scope is granted.",
                    status=403,
                ) from exc
            raise
        if resp.status_code != 200:
            raise VendorError(
                "jlcpcb",
                f"HTTP {resp.status_code} from {path}",
                status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise VendorError("jlcpcb", f"non-JSON response from {path}") from exc
        return data if isinstance(data, dict) else {}

    def component_info(self, lcsc: str) -> dict[str, Any] | None:
        """One C-number's live existence/stock/price/basic-vs-extended —
        the "in stock now" check at part-selection time, as opposed to
        dump-age stock. ``None`` when credentials are absent (caller falls
        back to the community dump) or the part isn't found."""
        if self._creds is None:
            return None
        data = self._call(
            COMPONENT_INFO_PATH, {"componentCodes": [lcsc]}, policy=DEFAULT_POLICY
        )
        rows = _extract_rows(data)
        if not rows:
            return None
        return normalize_api_row(rows[0])

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
            data = self._call(
                COMPONENT_INFO_PATH,
                {"lastKey": cursor, "pageSize": page_size},
                policy=BULK_POLICY,
            )
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


__all__ = [
    "ACCESS_KEY_SECRET",
    "APP_ID_SECRET",
    "COMPONENT_INFO_PATH",
    "HOST",
    "SECRET_KEY_SECRET",
    "Credentials",
    "JlcApiClient",
    "JlcPermissionError",
    "credentials_available",
    "normalize_api_row",
    "sign_request",
]
