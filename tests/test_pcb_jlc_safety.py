"""gr493051: retry and response safety for the JLC POSTs. Mocks only — no
live JLC call, no live order creation."""

from __future__ import annotations

import re

import httpx
import pytest

from precis.pcb import jlc_api, jlc_order
from precis.pcb._http import (
    Policy,
    VendorError,
    check_envelope,
    reset_circuit,
    with_backoff,
)


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, "test-secret-key")


def _seq(items):
    it = iter(items)
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return send, calls


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.Response(503), httpx.Response(500)],
)
def test_non_idempotent_not_retried_on_read_timeout_or_5xx(failure):
    send, calls = _seq([failure, httpx.Response(200)])
    with pytest.raises(VendorError) as ei:
        with_backoff(send, service="t-ni", sleep=lambda _s: None, idempotent=False)
    assert calls["n"] == 1
    assert "non-idempotent" in str(ei.value)


def test_non_idempotent_retried_on_connect_error():
    send, calls = _seq([httpx.ConnectError("refused"), httpx.Response(200)])
    resp = with_backoff(send, service="t-ni", sleep=lambda _s: None, idempotent=False)
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_idempotent_default_still_retries_5xx():
    send, calls = _seq([httpx.Response(503), httpx.Response(200)])
    with_backoff(send, service="t-i", sleep=lambda _s: None)
    assert calls["n"] == 2


def test_retry_after_clamped_to_policy_cap():
    slept: list[float] = []
    send, _ = _seq(
        [httpx.Response(429, headers={"Retry-After": "900"}), httpx.Response(200)]
    )
    with_backoff(send, service="t-ra", policy=Policy(cap=8.0), sleep=slept.append)
    assert slept == [8.0]


def test_check_envelope_failure_carries_code_and_trace_id():
    resp = httpx.Response(200, headers={"J-Trace-ID": "trace-abc"})
    with pytest.raises(VendorError) as ei:
        check_envelope("jlcpcb", {"code": 40001, "message": "bad sign"}, resp, "/p")
    assert ei.value.code == 40001
    assert ei.value.trace_id == "trace-abc"
    assert "trace-abc" in str(ei.value)
    with pytest.raises(VendorError):
        check_envelope("jlcpcb", {"success": False, "code": 200}, resp, "/p")


def test_check_envelope_passes_success_and_unwrapped():
    resp = httpx.Response(200)
    oks: list[dict[str, object]] = [
        {"code": 200, "data": 1},
        {"success": True, "code": 5},
        {"rows": []},
        {"code": "000000"},
        {"code": 200.0},
        {"code": "SUCCESS"},
    ]
    for ok in oks:
        assert check_envelope("jlcpcb", ok, resp, "/p") is ok


def test_client_200_with_failure_code_raises(creds):
    def send(method, path, headers, body):
        return httpx.Response(
            200,
            json={"code": 50001, "success": False, "message": "boom"},
            headers={"J-Trace-ID": "tr-1"},
        )

    client = jlc_api.JlcApiClient(send=send, sleep=lambda _s: None)
    with pytest.raises(VendorError) as ei:
        client.component_info("C1")
    assert ei.value.code == 50001
    assert ei.value.trace_id == "tr-1"


def test_create_order_not_retried_on_5xx(creds):
    calls: list[int] = []

    def send(method, path, headers, body):
        calls.append(1)
        return httpx.Response(502)

    client = jlc_order.JlcOrderClient(send=send, sleep=lambda _s: None)
    with pytest.raises(VendorError):
        client.create_order({"x": 1}, confirm=jlc_order.CONFIRM_ADD_TO_CART)
    assert len(calls) == 1


def test_upload_not_retried_on_read_timeout_but_retried_on_connect(creds):
    attempts: list[int] = []

    def upload(path, headers, meta_json, filename, blob):
        attempts.append(1)
        raise httpx.ReadTimeout("slow")

    client = jlc_order.JlcOrderClient(upload=upload, sleep=lambda _s: None)
    with pytest.raises(VendorError):
        client.upload_gerber("a.zip", b"zip")
    assert len(attempts) == 1

    attempts.clear()
    outcomes = [
        httpx.ConnectError("refused"),
        httpx.Response(200, json={"code": 200, "data": {"fileKey": "k1"}}),
    ]

    def upload2(path, headers, meta_json, filename, blob):
        attempts.append(1)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    client = jlc_order.JlcOrderClient(upload=upload2, sleep=lambda _s: None)
    assert client.upload_gerber("a.zip", b"zip") == "k1"
    assert len(attempts) == 2


def test_signing_timestamp_is_seconds(creds):
    assert len(jlc_api.signing_timestamp()) == 10
    seen: list[str] = []

    def send(method, path, headers, body):
        m = re.search(r'timestamp="(\d+)"', headers["Authorization"])
        assert m is not None
        seen.append(m[1])
        return httpx.Response(200, json={"code": 200, "data": {}})

    jlc_api.JlcApiClient(send=send).component_info("C1")
    assert len(seen[0]) == 10
    assert not hasattr(jlc_api, "TIMESTAMP_SECONDS")


def test_order_client_signs_seconds(creds):
    seen: list[str] = []

    def send(method, path, headers, body):
        m = re.search(r'timestamp="(\d+)"', headers["Authorization"])
        assert m is not None
        seen.append(m[1])
        return httpx.Response(200, json={"code": 200, "data": {}})

    jlc_order.JlcOrderClient(send=send).order_detail("B1")
    assert len(seen[0]) == 10
