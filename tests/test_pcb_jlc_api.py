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
import json

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
        assert path == jlc_api.COMPONENT_DETAIL_PATH
        assert json.loads(body) == {"componentCodes": ["C25804"]}
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


def test_component_info_uses_detail_endpoint_and_matches_code(creds):
    """Code lookup goes to getComponentDetailByCode with ``componentCodes``
    (getComponentInfos ignores the filter), and a non-matching row is not
    returned as the answer."""
    seen: list[tuple[str, dict]] = []

    def fake_send(method, path, headers, body):
        seen.append((path, json.loads(body)))
        return httpx.Response(
            200,
            json={
                "data": {
                    "componentDetailResponseVOList": [
                        {"componentCode": "C1002", "stockCount": 9},
                        {"componentCode": "C2128", "stockCount": 7},
                    ]
                }
            },
        )

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    row = client.component_info("C2128")
    assert seen == [
        (
            "/overseas/openapi/component/getComponentDetailByCode",
            {"componentCodes": ["C2128"]},
        )
    ]
    assert row is not None and row["lcsc"] == "C2128" and row["stock"] == 7
    assert client.component_info("C9999") is None


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
    # gr464537: first page must not send lastKey (null made JLC 500);
    # the second page carries the cursor from the first response.
    assert "lastKey" not in calls[0]
    assert '"pageSize":100' in calls[0]
    assert '"lastKey":"page2"' in calls[1]


def test_iter_components_resumes_from_since_key(creds):
    def fake_send(method, path, headers, body):
        assert '"lastKey":"resume-here"' in body
        return httpx.Response(200, json={"componentInfos": [], "lastKey": None})

    client = jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep)
    assert list(client.iter_components(since_key="resume-here")) == []


def _walk_client(pages):
    calls: list[str] = []

    def fake_send(method, path, headers, body):
        calls.append(body)
        return httpx.Response(200, json=pages[len(calls) - 1])

    return jlc_api.JlcApiClient(send=fake_send, sleep=_noop_sleep), calls


def test_iter_components_parses_list_nested_under_data_result(creds):
    pages = [
        {
            "data": {"result": {"list": [{"componentCode": "C1", "stockCount": 1}]}},
            "lastKey": "page2",
        },
        {"result": {"data": {"rows": [{"componentCode": "C2"}]}}},
    ]
    client, calls = _walk_client(pages)
    rows = list(client.iter_components())
    assert [r["lcsc"] for r in rows] == ["C1", "C2"]
    assert '"lastKey":"page2"' in calls[1]


def _prod_shaped_page(first: int, n: int, *, last_key: str | None, code_key: str):
    """A page shaped like prod's captured envelope (gr494906): wrapper keys
    plus ``data: {componentInfos, lastKey}``, ``n`` rows of a real page size."""
    data: dict = {
        "componentInfos": [
            {
                code_key: f"C{first + i}",
                "componentModelEn": f"PART-{first + i}",
                "stockCount": i,
                "componentLibraryType": "base" if i % 2 else "expand",
            }
            for i in range(n)
        ]
    }
    if last_key is not None:
        data["lastKey"] = last_key
    return {"success": True, "code": 200, "message": "ok", "data": data}


def test_extract_cursor_descends_into_data_and_result():
    assert jlc_api._extract_cursor({"data": {"lastKey": "k1"}}) == "k1"
    assert jlc_api._extract_cursor({"result": {"data": {"nextKey": "k2"}}}) == "k2"
    assert jlc_api._extract_cursor({"data": {"componentInfos": []}}) is None


def test_prod_shaped_walk_follows_data_lastkey(creds):
    pages = [
        _prod_shaped_page(1, 1000, last_key="p2", code_key="componentCode"),
        _prod_shaped_page(1001, 1000, last_key=None, code_key="componentCode"),
    ]
    client, calls = _walk_client(pages)
    rows = list(client.iter_components())
    assert len(rows) == 2000
    assert len(calls) == 2
    assert '"lastKey":"p2"' in calls[1]
    assert client.last_key is None


def test_parts_refresh_pass_persists_data_lastkey(creds, store):
    from precis.workers.parts_refresh import CURSOR_SETTING_KEY, run_parts_refresh_pass

    client, _ = _walk_client(
        [_prod_shaped_page(1, 1000, last_key="p2", code_key="componentCode")]
    )
    result = run_parts_refresh_pass(store, client=client, row_budget=1000)
    assert result["claimed"] == 1000
    assert result["failed"] == 0
    assert store.get_setting(CURSOR_SETTING_KEY) == "p2"


def test_iter_components_logs_row_key_names_once(creds, caplog):
    client, _ = _walk_client(
        [
            _prod_shaped_page(1, 5, last_key="p2", code_key="componentCode"),
            _prod_shaped_page(6, 5, last_key=None, code_key="componentCode"),
        ]
    )
    with caplog.at_level("INFO", logger=jlc_api.log.name):
        list(client.iter_components())
    lines = [r.getMessage() for r in caplog.records if "row keys=" in r.getMessage()]
    assert len(lines) == 1
    assert "componentCode" in lines[0]
    assert "C1" not in lines[0].replace("componentCode", "")


def _live_row(i: int) -> dict:
    """Row with the key set of the live getComponentInfos probe (gr494906,
    2026-10-18); values are made up."""
    return {
        "lcscPart": f"C{100 + i}",
        "firstCategory": "Resistors",
        "secondCategory": "Chip Resistor",
        "mfrPart": f"RES-{i}",
        "solderJoint": 2,
        "manufacturer": "ExampleCorp",
        "libraryType": "base",
        "description": "10k 0603",
        "datasheet": f"https://example.invalid/ds/{i}.pdf",
        "price": "1-9:0.01",
        "stock": 500 + i,
        "package": "0603",
    }


def test_normalize_api_row_live_probe_key_set():
    out = jlc_api.normalize_api_row(_live_row(7))
    assert out is not None
    assert out["lcsc"] == "C107"
    assert out["mfr_part"] == "RES-7"
    assert out["datasheet_url"] == "https://example.invalid/ds/7.pdf"
    assert out["mfr"] == "ExampleCorp"
    assert out["basic"] is True
    assert out["stock"] == 507
    assert out["package"] == "0603"


def test_parts_refresh_pass_claims_live_probe_rows(creds, store):
    from precis.workers.parts_refresh import run_parts_refresh_pass

    page = {
        "success": True,
        "code": 200,
        "message": "ok",
        "data": {"componentInfos": [_live_row(i) for i in range(50)], "lastKey": "p2"},
    }
    client, _ = _walk_client([page])
    result = run_parts_refresh_pass(store, client=client, row_budget=50)
    assert result["claimed"] > 0
    assert result["failed"] == 0


def test_iter_components_all_unknown_key_page_raises(creds):
    client, _ = _walk_client(
        [_prod_shaped_page(1, 1000, last_key="p2", code_key="mysteryPartId")]
    )
    with pytest.raises(VendorError, match="none normalized") as exc:
        list(client.iter_components())
    msg = str(exc.value)
    assert "mysteryPartId" in msg
    assert "next:" in msg
    assert "C1" not in msg.replace("componentCode", "")
    assert client.last_key is None


def test_parts_refresh_pass_alerts_on_all_unknown_key_page(creds, store):
    from precis.alerts import open_alert_severity
    from precis.workers.parts_refresh import CURSOR_SETTING_KEY, run_parts_refresh_pass

    client, _ = _walk_client(
        [_prod_shaped_page(1, 1000, last_key="p2", code_key="mysteryPartId")]
    )
    result = run_parts_refresh_pass(store, client=client)
    assert result["failed"] == 1 and result["claimed"] == 0
    assert "none normalized" in result["error"]
    assert store.get_setting(CURSOR_SETTING_KEY) in (None, "")
    assert (
        open_alert_severity(store, source="parts_refresh", fingerprint="jlcpcb-api")
        is not None
    )


def test_iter_components_empty_first_page_is_a_failure(creds):
    client, _ = _walk_client([{"code": 200, "message": "ok", "data": {"total": 0}}])
    with pytest.raises(VendorError, match="empty first page"):
        list(client.iter_components())
    assert client.last_key is None


def test_iter_components_empty_later_page_is_the_normal_end(creds):
    client, calls = _walk_client(
        [
            {"componentInfos": [{"componentCode": "C1"}], "lastKey": "p2"},
            {"componentInfos": []},
        ]
    )
    assert [r["lcsc"] for r in client.iter_components()] == ["C1"]
    assert len(calls) == 2


def test_iter_components_single_page_catalog_without_cursor_is_not_a_failure(creds):
    client, _ = _walk_client([{"componentInfos": [{"componentCode": "C1"}]}])
    assert [r["lcsc"] for r in client.iter_components()] == ["C1"]


def test_iter_components_empty_first_page_with_cursor_is_not_a_failure(creds):
    # Not the failure path; the walk's existing `not rows` break ends it.
    client, calls = _walk_client([{"componentInfos": [], "lastKey": "p2"}])
    assert list(client.iter_components()) == []
    assert len(calls) == 1


def test_iter_components_empty_resumed_page_is_the_normal_end(creds):
    client, _ = _walk_client([{"componentInfos": []}])
    assert list(client.iter_components(since_key="resume")) == []


def test_envelope_log_emits_key_names_only(creds, caplog):
    secret_value = "VALUE-must-not-be-logged"
    page = {
        "componentInfos": [{"componentCode": "C1", "mfr": secret_value}],
        "token": secret_value,
        "data": {"inner": secret_value, "nested": {"deep": secret_value}},
    }
    client, _ = _walk_client([page])
    with caplog.at_level("INFO", logger=jlc_api.log.name):
        list(client.iter_components())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "keys=componentInfos,token,data" in text
    assert "data.keys=inner,nested" in text
    assert "deep" not in text
    assert secret_value not in text


def test_parts_refresh_pass_alerts_on_empty_first_page(creds, store):
    from precis.alerts import open_alert_severity
    from precis.workers.parts_refresh import CURSOR_SETTING_KEY, run_parts_refresh_pass

    client, _ = _walk_client([{"unexpected": {"shape": 1}}])
    result = run_parts_refresh_pass(store, client=client)
    assert result["failed"] == 1 and result["claimed"] == 0
    assert "empty first page" in result["error"]
    assert store.get_setting(CURSOR_SETTING_KEY) in (None, "")
    assert (
        open_alert_severity(store, source="parts_refresh", fingerprint="jlcpcb-api")
        is not None
    )


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


def test_normalize_api_row_reads_detail_endpoint_library_type(creds):
    """``getComponentDetailByCode`` spells the library class
    ``libraryType`` — no ``basic``/``preferred``/``componentLibraryType``
    keys (probed live 2026-10-17, gr495286). Unmapped it normalized every
    Basic part to ``basic=False``, and :meth:`parts_import` wrote that over
    the catalog's true flag."""
    n = jlc_api.normalize_api_row(
        {"componentCode": "C1525", "libraryType": "base", "stockCount": 24_774_268}
    )
    assert n is not None and n["basic"] is True
    n = jlc_api.normalize_api_row({"componentCode": "C1", "libraryType": "expand"})
    assert n is not None and n["basic"] is False


def test_live_lookup_refresh_keeps_the_catalog_basic_flag(store, creds):
    """The availability sweep's refresh path end to end: a stale Basic row
    re-catalogued through ``component_info`` stays Basic (gr495286)."""
    store.parts_import([{"lcsc": "C1525", "basic": True, "stock": 5}])
    client, _calls = _walk_client(
        [
            {
                "data": {
                    "componentDetailResponseVOList": [
                        {
                            "componentCode": "C1525",
                            "libraryType": "base",
                            "stockCount": 99,
                        }
                    ]
                }
            }
        ]
    )
    row = jlc_api.live_lookup(store, "C1525", client=client)
    assert row is not None and row["basic"] is True
    got = store.part_row("C1525")
    assert got is not None and got["basic"] is True and got["stock"] == 99
