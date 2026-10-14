"""JLCPCB Open API ordering surface — upload → quote → create.

Slice 9's order leg, on top of the slice-2 component client
(:mod:`precis.pcb.jlc_api`): same host, same ``JOP`` HMAC-SHA256 signing,
same :mod:`precis.pcb._http` politeness layer. Findings and the full
endpoint inventory live in ``docs/backlog/pcb-jlc-order.md``; the two that
shape this file:

* **There is no cart endpoint.** The documented flow is ``uploadGerber``
  → ``pcb/calculate`` → ``pcb/create``. The API exposes no payment call —
  payment happens on jlcpcb.com by a human — so ``create`` produces an
  unpaid order: that *is* "add to cart" here. :meth:`create_order` is
  hard-guarded (:data:`CONFIRM_ADD_TO_CART`) because nothing past it is.
* **Uploads sign the ``meta`` form field, not the file bytes.** The
  signature's body slot takes the ``meta`` JSON string; the gerber zip
  rides the ``file`` multipart part (per the signing docs and both
  reference clients — see the backlog doc).

``pcbParam`` craft codes (``pcbColor``, ``surfaceFinish``, ``plateType``,
…) are JLC-internal ints whose enum table sits behind the console docs —
not publicly scrapeable, so the builders carry only the structural facts
(layers, dimensions, qty, thickness) and merge caller overrides verbatim
rather than guess codes.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.pcb import jlc_api
from precis.pcb._http import DEFAULT_POLICY, VendorError

if TYPE_CHECKING:
    import httpx

    from precis.store import Store

#: Endpoint paths — ``docs/backlog/pcb-jlc-order.md`` for the sources.
GERBER_UPLOAD_PATH = "/overseas/openapi/pcb/uploadGerber"
QUOTE_PATH = "/overseas/openapi/pcb/calculate"
ORDER_CREATE_PATH = "/overseas/openapi/pcb/create"
ORDER_DETAIL_PATH = "/overseas/openapi/pcb/order/detail"
ORDER_AUDIT_PATH = "/overseas/openapi/pcb/audit/get"

#: The one string that arms :meth:`JlcOrderClient.create_order` and
#: ``PcbHandler._op_order``'s live path. Not a flag — a caller must type
#: it, verbatim, per call.
CONFIRM_ADD_TO_CART = "add-to-cart"

_UPLOAD_TIMEOUT_S = 120.0  # a gerber zip can be MBs; match the SDKs' 120s

#: The multipart seam for tests: (path, headers, meta_json, filename,
#: file_bytes) -> response. Swappable like ``JlcApiClient``'s ``send``.
UploadFn = Callable[[str, dict[str, str], str, str, bytes], "httpx.Response"]


def _default_upload(
    path: str, headers: dict[str, str], meta_json: str, filename: str, blob: bytes
) -> httpx.Response:
    """Real multipart transport via :func:`safe_stream` — ``data``/``files``
    pass through to ``client.build_request``, so httpx encodes the form and
    sets the multipart Content-Type itself. Only ``headers`` carries the
    signature (computed over ``meta_json``, not the encoded form)."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_stream

    with http_client(timeout=_UPLOAD_TIMEOUT_S) as client:
        with safe_stream(
            client,
            "POST",
            f"{jlc_api.HOST}{path}",
            headers=headers,
            data={"meta": meta_json},
            files={"file": (filename, blob, "application/zip")},
        ) as resp:
            resp.read()
            return resp


# ─────────────────────────────────────────────────────────────────────
# request builders — pure, no network
# ─────────────────────────────────────────────────────────────────────
def board_dimensions_mm(outline: list[list[float]]) -> tuple[float, float]:
    """(width, length) of a closed outline polygon — the order payload's
    ``pcbParam.width``/``length``. Callers supply
    ``PcbHandler._outline_from_features``'s output (or its bbox fallback)."""
    xs = [float(p[0]) for p in outline]
    ys = [float(p[1]) for p in outline]
    return (round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2))


def pcb_param(
    *,
    layers: int,
    width_mm: float,
    length_mm: float,
    qty: int = 5,
    thickness_mm: float = 1.6,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``pcbParam`` craft block: only structural facts this package
    actually knows (layer count, board size, qty, thickness), then the
    caller's ``overrides`` verbatim — JLC's option codes
    (``pcbColor``/``surfaceFinish``/``plateType``/… ints) come through
    there, never guessed here."""
    param: dict[str, Any] = {
        "layer": int(layers),
        "width": round(float(width_mm), 2),
        "length": round(float(length_mm), 2),
        "qty": int(qty),
        "thickness": float(thickness_mm),
    }
    param.update(overrides or {})
    return param


def build_quote_request(
    *,
    pcb: dict[str, Any],
    order_type: int = 1,
    file_key: str | None = None,
    batch_num: str | None = None,
    country: str | None = None,
    post_code: str | None = None,
    city: str | None = None,
    shipping_method: str | None = None,
    smt_stencil: dict[str, Any] | None = None,
    achieve_date: int | None = None,
) -> dict[str, Any]:
    """The ``pcb/calculate`` body — the read-only quote step. ``fileKey``
    is optional per the API model; a parametric quote (dims + process, no
    gerber) is the dry-run path's one allowed call."""
    req: dict[str, Any] = {"orderType": int(order_type), "pcbParam": dict(pcb)}
    for key, val in (
        ("fileKey", file_key),
        ("batchNum", batch_num),
        ("country", country),
        ("postCode", post_code),
        ("city", city),
        ("shippingMethod", shipping_method),
        ("smtStencilParam", smt_stencil),
        ("achieveDate", achieve_date),
    ):
        if val is not None:
            req[key] = val
    return req


def build_order_request(
    *,
    pcb: dict[str, Any],
    order_type: int = 1,
    file_key: str | None = None,
    batch_num: str | None = None,
    shipping_address: dict[str, Any] | None = None,
    billing_address: dict[str, Any] | None = None,
    billing_address_flag: int | None = None,
    tax_or_vat: str | None = None,
    shipping_method: str | None = None,
    smt_stencil: dict[str, Any] | None = None,
    achieve_date: int | None = None,
) -> dict[str, Any]:
    """The ``pcb/create`` body — same craft fields as the quote plus the
    shipping/billing blocks an order needs."""
    req = build_quote_request(
        pcb=pcb,
        order_type=order_type,
        file_key=file_key,
        batch_num=batch_num,
        shipping_method=shipping_method,
        smt_stencil=smt_stencil,
        achieve_date=achieve_date,
    )
    for key, val in (
        ("shippingAddress", shipping_address),
        ("billingAddress", billing_address),
        ("billingAddressFlag", billing_address_flag),
        ("TaxOrVATNumber", tax_or_vat),
    ):
        if val is not None:
            req[key] = val
    return req


def _extract_data(data: dict[str, Any]) -> Any:
    """The ``data`` field of an Open API envelope — tolerating the same
    ``result``-style wrapper drift :func:`jlc_api._extract_rows` guards."""
    for key in ("data", "result"):
        if key in data:
            return data[key]
    return data


def _extract_file_key(data: dict[str, Any]) -> str | None:
    """The upload's ``fileKey`` — a bare string or a ``{fileKey|fileStoreId}``
    object depending on the envelope."""
    d = _extract_data(data)
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        for key in ("fileKey", "fileStoreId", "key"):
            if d.get(key):
                return str(d[key])
    return None


def extract_order_ids(data: dict[str, Any]) -> dict[str, str]:
    """``pcb/create``'s answer — order id, batch number, uuid — under
    whichever envelope keys carried them."""
    d = _extract_data(data)
    out: dict[str, str] = {}
    if isinstance(d, dict):
        for ours, theirs in (
            ("order_id", "orderId"),
            ("order_uuid", "orderUUID"),
            ("batch_num", "batchNum"),
            ("order_type", "orderType"),
        ):
            if d.get(theirs) is not None:
                out[ours] = str(d[theirs])
    return out


# ─────────────────────────────────────────────────────────────────────
# client
# ─────────────────────────────────────────────────────────────────────
class JlcOrderClient(jlc_api.JlcApiClient):
    """The PCB order endpoints: ``quote`` (read-only), ``upload_gerber``
    (multipart), ``create_order`` (the guarded terminal step),
    ``order_detail``/``audit`` (read-back). Construction, credentials and
    the ``send`` test seam are :class:`JlcApiClient`'s; ``upload`` is the
    matching seam for the multipart path.
    """

    def __init__(
        self,
        *,
        store: Store | None = None,
        send: jlc_api.SendFn | None = None,
        upload: UploadFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(store=store, send=send, sleep=sleep, now=now)
        self._upload = upload or _default_upload

    def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """``pcb/calculate`` — price + lead time. Creates nothing."""
        return self._call(QUOTE_PATH, request, policy=DEFAULT_POLICY)

    def order_detail(self, batch_num: str) -> dict[str, Any]:
        """``pcb/order/detail`` — read-back of a created order."""
        return self._call(
            ORDER_DETAIL_PATH, {"batchNum": batch_num}, policy=DEFAULT_POLICY
        )

    def audit(self, key: str, *, language: int | None = None) -> dict[str, Any]:
        """``pcb/audit/get`` — JLC's engineering/DFM read on an uploaded
        file (the gerber-preflight oracle
        ``docs/backlog/pcb-guided-place-route.md`` planned around)."""
        body: dict[str, Any] = {"key": key}
        if language is not None:
            body["language"] = int(language)
        return self._call(ORDER_AUDIT_PATH, body, policy=DEFAULT_POLICY)

    def upload_gerber(
        self, filename: str, blob: bytes, *, meta: dict[str, Any] | None = None
    ) -> str:
        """``uploadGerber`` — multipart ``meta`` (signed) + ``file`` (the
        zip). Returns the ``fileKey`` the quote/create calls consume."""
        creds = self._creds
        if creds is None:  # not an assert — asserts vanish under -O
            raise VendorError("jlcpcb", "no credentials; check .available first")
        meta_json = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True)

        def _thunk() -> httpx.Response:
            timestamp = str(int(time.time() * 1000))
            nonce = uuid.uuid4().hex
            headers = {
                "Authorization": jlc_api._authorization_header(
                    creds,
                    method="POST",
                    path=GERBER_UPLOAD_PATH,
                    body=meta_json,
                    timestamp=timestamp,
                    nonce=nonce,
                ),
            }
            return self._upload(GERBER_UPLOAD_PATH, headers, meta_json, filename, blob)

        resp = self._roundtrip(_thunk, policy=DEFAULT_POLICY)
        file_key = _extract_file_key(self._finish(resp, GERBER_UPLOAD_PATH))
        if not file_key:
            raise VendorError(
                "jlcpcb", "uploadGerber returned no fileKey", url=GERBER_UPLOAD_PATH
            )
        return file_key

    def create_order(self, request: dict[str, Any], *, confirm: str) -> dict[str, Any]:
        """``pcb/create`` — the terminal step this surface reaches, and
        only behind ``confirm='add-to-cart'`` typed verbatim. The API has
        no pay endpoint: this produces an UNPAID order awaiting checkout
        on jlcpcb.com — which is still a real order on the account, so the
        refusal here is a coding contract, not a convenience. No arg, or
        any other string, is a :class:`VendorError`, not a quiet False."""
        if confirm != CONFIRM_ADD_TO_CART:
            raise VendorError(
                "jlcpcb",
                "create_order refused: pass confirm='add-to-cart' verbatim "
                "— there is no cart endpoint, this call creates the order",
            )
        return self._call(ORDER_CREATE_PATH, request, policy=DEFAULT_POLICY)


__all__ = [
    "CONFIRM_ADD_TO_CART",
    "GERBER_UPLOAD_PATH",
    "ORDER_AUDIT_PATH",
    "ORDER_CREATE_PATH",
    "ORDER_DETAIL_PATH",
    "QUOTE_PATH",
    "JlcOrderClient",
    "board_dimensions_mm",
    "build_order_request",
    "build_quote_request",
    "extract_order_ids",
    "pcb_param",
]
