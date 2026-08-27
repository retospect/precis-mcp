"""JLCPCB Open API client: signature determinism, the 403 console-permission
message, 401-not-retried, creds-absent degrade, and the ``normalize_api_row``
column contract. No network — every test injects a fake ``send``/fake
secrets store; see ``src/precis/pcb/jlc_api.py`` for the auth facts these
pin down.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest

from precis import secrets
from precis.pcb import jlc_api
from precis.pcb._http import VendorError, VendorUnavailable, reset_circuit

_FAKE_SECRET = "test-secret-key"  # obviously fake — never a real credential


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_circuit("jlcpcb")
    yield
    reset_circuit("jlcpcb")


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, _FAKE_SECRET)


def _noop_sleep(_seconds: float) -> None:
    pass


# ── signature determinism ───────────────────────────────────────────────


def test_sign_request_matches_hand_computed_hmac():
    string_to_sign = "POST\n/overseas/openapi/component/getComponentInfos\n1700000000000\nabc123\n{}\n"
    expected = base64.b64encode(
        hmac.new(
            _FAKE_SECRET.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
    ).decode("ascii")

    got = jlc_api.sign_request(
        _FAKE_SECRET,
        method="POST",
        path="/overseas/openapi/component/getComponentInfos",
        timestamp="1700000000000",
        nonce="abc123",
        body="{}",
    )
    assert got == expected


def test_sign_request_is_deterministic_and_input_sensitive():
    kwargs = dict(method="POST", path="/p", timestamp="1", nonce="n", body="{}")
    a = jlc_api.sign_request(_FAKE_SECRET, **kwargs)
    b = jlc_api.sign_request(_FAKE_SECRET, **kwargs)
    assert a == b
    c = jlc_api.sign_request(_FAKE_SECRET, **{**kwargs, "nonce": "different"})
    assert a != c


# ── 403 → actionable console-permission message ─────────────────────────


def test_403_raises_permission_error_naming_the_console_grant(creds):
    def fake_send(method, path, headers, body):
        return httpx.Response(403, text="API insufficient permissions")

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    with pytest.raises(jlc_api.JlcPermissionError) as excinfo:
        client.component_info("C25804")
    msg = str(excinfo.value).lower()
    assert "console" in msg
    assert "permission" in msg or "scope" in msg
    assert "grant" in msg
    # This is not a bug in our signing — say so.
    assert "not" in msg


def test_403_error_is_a_vendor_error_with_status(creds):
    def fake_send(method, path, headers, body):
        return httpx.Response(403, text="API insufficient permissions")

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    with pytest.raises(VendorError) as excinfo:
        client.component_info("C25804")
    assert excinfo.value.status == 403


# ── 401 must never be retried ────────────────────────────────────────────


def test_401_is_not_retried(creds):
    calls = []

    def fake_send(method, path, headers, body):
        calls.append(1)
        return httpx.Response(401, text="The request signature verify failed")

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    with pytest.raises(VendorError) as excinfo:
        client.component_info("C25804")
    assert excinfo.value.status == 401
    assert len(calls) == 1


# ── retryable statuses still go through with_backoff ─────────────────────


def test_5xx_is_retried_then_raises_unavailable(creds):
    calls = []

    def fake_send(method, path, headers, body):
        calls.append(1)
        return httpx.Response(500, text="oops")

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    # Patch the policy indirectly by keeping attempts small via monkeypatched
    # module-level DEFAULT_POLICY would affect other tests; instead assert
    # behaviour with the real DEFAULT_POLICY but a no-op sleep so it's fast.
    with pytest.raises(VendorUnavailable):
        client.component_info("C25804")
    assert len(calls) == jlc_api.DEFAULT_POLICY.attempts


# ── creds-absent degrades cleanly ────────────────────────────────────────


def test_component_info_returns_none_without_credentials(monkeypatch):
    monkeypatch.delenv(jlc_api.APP_ID_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.ACCESS_KEY_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.SECRET_KEY_SECRET, raising=False)
    secrets.bind_store(None)

    def boom(method, path, headers, body):
        raise AssertionError("no network call should be attempted without creds")

    client = jlc_api.JlcApiClient(send=boom)
    assert client.available is False
    assert client.component_info("C25804") is None


def test_iter_components_yields_nothing_without_credentials(monkeypatch):
    monkeypatch.delenv(jlc_api.APP_ID_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.ACCESS_KEY_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.SECRET_KEY_SECRET, raising=False)
    secrets.bind_store(None)

    def boom(method, path, headers, body):
        raise AssertionError("no network call should be attempted without creds")

    client = jlc_api.JlcApiClient(send=boom)
    assert list(client.iter_components()) == []
    assert client.last_key is None


def test_credentials_available_gate(monkeypatch):
    monkeypatch.delenv(jlc_api.APP_ID_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.ACCESS_KEY_SECRET, raising=False)
    monkeypatch.delenv(jlc_api.SECRET_KEY_SECRET, raising=False)
    secrets.bind_store(None)
    assert jlc_api.credentials_available() is False
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "a")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "b")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, "c")
    assert jlc_api.credentials_available() is True


# ── happy path + cursor checkpointing ────────────────────────────────────


def test_component_info_happy_path(creds):
    def fake_send(method, path, headers, body):
        assert method == "POST"
        assert path == jlc_api.COMPONENT_INFO_PATH
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("JOP ")
        return httpx.Response(
            200,
            json={
                "componentInfos": [
                    {
                        "componentCode": "C25804",
                        "manufacturer": "Samsung",
                        "mfrPartNumber": "CL05B104KO5NNNC",
                        "description": "100nF 16V X7R 0402",
                        "basic": True,
                        "stockCount": 500000,
                        "componentSpecificationEn": "0402",
                    }
                ]
            },
        )

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    row = client.component_info("C25804")
    assert row is not None
    assert row["lcsc"] == "C25804"
    assert row["stock"] == 500000
    assert row["basic"] is True


def test_iter_components_paginates_and_exposes_cursor(creds):
    pages = [
        {
            "componentInfos": [{"componentCode": "C1", "stockCount": 1}],
            "lastKey": "page2",
        },
        {
            "componentInfos": [{"componentCode": "C2", "stockCount": 2}],
            "lastKey": None,
        },
    ]
    calls = []

    def fake_send(method, path, headers, body):
        calls.append(body)
        return httpx.Response(200, json=pages[len(calls) - 1])

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    rows = list(client.iter_components())
    assert [r["lcsc"] for r in rows] == ["C1", "C2"]
    assert client.last_key is None
    assert len(calls) == 2


def test_iter_components_resumes_from_since_key(creds):
    def fake_send(method, path, headers, body):
        assert '"lastKey":"resume-here"' in body
        return httpx.Response(200, json={"componentInfos": [], "lastKey": None})

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    assert list(client.iter_components(since_key="resume-here")) == []


# ── normalize_api_row matches the parts_import column contract ──────────


def test_normalize_api_row_matches_parts_import_contract():
    from precis.pcb import catalog

    raw = {
        "componentCode": "25804",
        "manufacturer": "Samsung",
        "mfrPartNumber": "CL05B104KO5NNNC",
        "description": "100nF 16V X7R 0402",
        "basic": True,
        "stockCount": 500000,
        "componentSpecificationEn": "0402",
        "dataManualUrl": "http://x/ds.pdf",
        "componentPrices": [{"qFrom": 1, "qTo": 100, "price": 0.0023}],
    }
    n = jlc_api.normalize_api_row(raw)
    assert n is not None
    ref = catalog.normalize_jlcparts_row({"lcsc": "C1"})
    assert ref is not None
    assert set(n.keys()) == set(ref.keys())
    assert n["lcsc"] == "C25804"
    assert n["mfr"] == "Samsung"
    assert n["mfr_part"] == "CL05B104KO5NNNC"
    assert n["jlcpcb_assemblable"] is True
    assert n["basic"] is True
    assert n["stock"] == 500000
    assert n["package"] == "0402"
    assert n["datasheet_url"] == "http://x/ds.pdf"


def test_normalize_api_row_none_without_c_number():
    assert jlc_api.normalize_api_row({"description": "x"}) is None
