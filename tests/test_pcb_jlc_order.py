"""JLCPCB Open API ordering client (``precis.pcb.jlc_order``): request
builders, the ``meta``-field upload signing, and the ``create`` guard.

No network — every test injects fake ``send``/``upload`` callables plus
env-var credentials, the same seam pattern ``test_pcb_jlc_api.py`` uses.
Endpoint paths and field names are documented in
``docs/backlog/pcb-jlc-order.md``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from precis.pcb import jlc_order
from precis.pcb._http import VendorError, reset_circuit

_FAKE_SECRET = "test-secret-key"  # obviously fake — never a real credential


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_circuit("jlcpcb")
    yield
    reset_circuit("jlcpcb")


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_order.jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_order.jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_order.jlc_api.SECRET_KEY_SECRET, _FAKE_SECRET)


def _noop_sleep(_seconds: float) -> None:
    pass


def _boom_send(
    method: str, path: str, headers: dict[str, str], body: str
) -> httpx.Response:
    raise AssertionError("no network without creds")


def _boom_upload(
    path: str, headers: dict[str, str], meta_json: str, filename: str, blob: bytes
) -> httpx.Response:
    raise AssertionError("no network without creds")


def _client(calls: list[tuple], *, send_payload=None, upload_payload=None):
    def fake_send(method, path, headers, body):
        calls.append(("send", method, path, body))
        return httpx.Response(200, json=send_payload or {"code": 200, "data": {}})

    def fake_upload(path, headers, meta_json, filename, blob):
        calls.append(("upload", path, meta_json, filename, blob, headers))
        return httpx.Response(200, json=upload_payload or {"code": 200, "data": {}})

    return jlc_order.JlcOrderClient(
        send=fake_send, upload=fake_upload, sleep=_noop_sleep
    )


# ── request builders (pure) ────────────────────────────────────────────


def test_pcb_param_carries_structure_and_overrides():
    p = jlc_order.pcb_param(
        layers=4,
        width_mm=20.005,
        length_mm=30.0,
        qty=10,
        thickness_mm=1.2,
        overrides={"pcbColor": 1, "surfaceFinish": 2},
    )
    assert p == {
        "layer": 4,
        "width": 20.0,  # rounded to 2 decimals
        "length": 30.0,
        "qty": 10,
        "thickness": 1.2,
        "pcbColor": 1,
        "surfaceFinish": 2,
    }


def test_board_dimensions_mm_is_the_outline_bbox():
    outline = [[1.0, 2.0], [21.0, 2.0], [21.0, 12.0], [1.0, 12.0]]
    assert jlc_order.board_dimensions_mm(outline) == (20.0, 10.0)


def test_build_quote_request_drops_absent_fields():
    req = jlc_order.build_quote_request(pcb={"layer": 4, "qty": 5})
    assert req == {"orderType": 1, "pcbParam": {"layer": 4, "qty": 5}}
    assert "fileKey" not in req and "shippingAddress" not in req


def test_build_order_request_carries_addresses_and_file_key():
    req = jlc_order.build_order_request(
        pcb={"layer": 4},
        file_key="fk-1",
        shipping_address={"country": "CH", "city": "Zurich"},
        shipping_method="std",
    )
    assert req["fileKey"] == "fk-1"
    assert req["shippingAddress"] == {"country": "CH", "city": "Zurich"}
    assert req["shippingMethod"] == "std"
    assert req["orderType"] == 1


def test_extract_order_ids_from_envelope():
    d = jlc_order.extract_order_ids(
        {"code": 200, "data": {"orderId": 991, "batchNum": "B123", "orderType": 1}}
    )
    assert d == {"order_id": "991", "batch_num": "B123", "order_type": "1"}


# ── the client ─────────────────────────────────────────────────────────


def test_quote_posts_to_calculate(creds):
    calls: list[tuple] = []
    client = _client(calls)
    client.quote({"orderType": 1, "pcbParam": {"layer": 4}})
    assert calls[0][2] == jlc_order.QUOTE_PATH


def test_order_detail_posts_batch_num(creds):
    calls: list[tuple] = []
    client = _client(calls)
    client.order_detail("B123")
    kind, _method, path, body = calls[0]
    assert path == jlc_order.ORDER_DETAIL_PATH
    assert '"batchNum":"B123"' in body


def test_upload_gerber_signs_the_meta_json_not_the_file(creds):
    """The signature's body slot is the ``meta`` form field string — the
    gerber zip's bytes are never signed (the documented multipart rule)."""
    seen: dict = {}

    def upload(path, headers, meta_json, filename, blob):
        seen.update(
            path=path,
            meta=meta_json,
            filename=filename,
            blob=blob,
            auth=headers["Authorization"],
        )
        return httpx.Response(200, json={"code": 200, "data": {"fileKey": "fk-9"}})

    client = jlc_order.JlcOrderClient(
        send=_boom_send,
        upload=upload,
        sleep=_noop_sleep,
    )
    key = client.upload_gerber("b-fab.zip", b"ZIPBYTES", meta={"fileName": "b-fab.zip"})
    assert key == "fk-9"
    assert seen["path"] == jlc_order.GERBER_UPLOAD_PATH
    assert seen["blob"] == b"ZIPBYTES"
    assert seen["filename"] == "b-fab.zip"
    # The signed body is exactly the meta string — recompute and compare.
    auth = seen["auth"]
    assert auth.startswith("JOP ")
    # signature = last quoted field
    sig_b64 = auth.rsplit('signature="', 1)[1].rstrip('"')
    # timestamp + nonce are the other quoted fields
    ts = auth.split('timestamp="', 1)[1].split('"', 1)[0]
    nonce = auth.split('nonce="', 1)[1].split('"', 1)[0]
    expected = base64.b64encode(
        hmac.new(
            _FAKE_SECRET.encode(),
            f"POST\n{jlc_order.GERBER_UPLOAD_PATH}\n{ts}\n{nonce}\n"
            f"{seen['meta']}\n".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert sig_b64 == expected
    assert json.loads(seen["meta"]) == {"fileName": "b-fab.zip"}


def test_upload_gerber_raises_when_no_file_key(creds):
    client = jlc_order.JlcOrderClient(
        send=_boom_send,
        upload=lambda *a: httpx.Response(200, json={"code": 200, "data": {}}),
        sleep=_noop_sleep,
    )
    with pytest.raises(VendorError, match="fileKey"):
        client.upload_gerber("b.zip", b"x")


def test_create_order_refuses_without_confirm(creds):
    calls: list[tuple] = []
    client = _client(calls)
    with pytest.raises(VendorError, match="add-to-cart"):
        client.create_order({"pcbParam": {}}, confirm=None)
    with pytest.raises(VendorError, match="add-to-cart"):
        client.create_order({"pcbParam": {}}, confirm="yes")
    assert calls == []  # the refusal precedes any network


def test_create_order_posts_with_confirm(creds):
    calls: list[tuple] = []
    client = _client(calls, send_payload={"code": 200, "data": {"batchNum": "B1"}})
    out = client.create_order({"pcbParam": {"qty": 5}}, confirm="add-to-cart")
    assert calls[0][2] == jlc_order.ORDER_CREATE_PATH
    assert out["data"]["batchNum"] == "B1"


def test_no_credentials_block_every_call(monkeypatch):
    monkeypatch.delenv(jlc_order.jlc_api.APP_ID_SECRET, raising=False)
    monkeypatch.delenv(jlc_order.jlc_api.ACCESS_KEY_SECRET, raising=False)
    monkeypatch.delenv(jlc_order.jlc_api.SECRET_KEY_SECRET, raising=False)
    from precis import secrets

    secrets.bind_store(None)
    client = jlc_order.JlcOrderClient(
        send=_boom_send,
        upload=_boom_upload,
    )
    assert client.available is False
    with pytest.raises(VendorError, match="credentials"):
        client.quote({})
    with pytest.raises(VendorError, match="credentials"):
        client.upload_gerber("b.zip", b"x")
    with pytest.raises(VendorError, match="credentials"):
        client.create_order({}, confirm="add-to-cart")
