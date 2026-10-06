"""``retry_transient`` (connect-phase retry) and its Crossref call-site wraps."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import requests

from precis.utils.http import retry_transient


class _Flaky:
    """Callable raising ``errors`` in order, then returning ``"ok"``."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def _ct() -> httpx.ConnectTimeout:
    return httpx.ConnectTimeout("_ssl.c:989: The handshake operation timed out")


def test_retries_once_on_connect_timeout_then_succeeds() -> None:
    sleeps: list[float] = []
    fn = _Flaky(_ct())
    assert retry_transient(fn, sleep=sleeps.append) == "ok"
    assert fn.calls == 2
    assert sleeps == [3.0]


def test_retries_on_connect_error_and_requests_connect_timeout() -> None:
    for exc in (httpx.ConnectError("refused"), requests.exceptions.ConnectTimeout()):
        fn = _Flaky(exc)
        assert retry_transient(fn, sleep=lambda _s: None) == "ok"
        assert fn.calls == 2


def test_gives_up_after_two_attempts_and_reraises() -> None:
    sleeps: list[float] = []
    fn = _Flaky(_ct(), _ct(), _ct())
    with pytest.raises(httpx.ConnectTimeout):
        retry_transient(fn, sleep=sleeps.append)
    assert fn.calls == 2
    assert sleeps == [3.0]


def test_custom_backoff_is_used() -> None:
    sleeps: list[float] = []
    fn = _Flaky(_ct(), _ct())
    assert retry_transient(fn, attempts=3, backoff_s=(1.0, 2.0), sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]


def test_does_not_retry_read_timeout() -> None:
    sleeps: list[float] = []
    fn = _Flaky(httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        retry_transient(fn, sleep=sleeps.append)
    assert fn.calls == 1
    assert sleeps == []


def test_does_not_retry_http_status_error() -> None:
    req = httpx.Request("GET", "https://api.crossref.org/works/x")
    err = httpx.HTTPStatusError(
        "503", request=req, response=httpx.Response(503, request=req)
    )
    fn = _Flaky(err)
    with pytest.raises(httpx.HTTPStatusError):
        retry_transient(fn, sleep=lambda _s: None)
    assert fn.calls == 1


def test_logs_one_info_line_with_host(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="precis.utils.http")
    retry_transient(
        _Flaky(_ct()),
        host="https://api.crossref.org/works/10.1/x",
        sleep=lambda _s: None,
    )
    lines = [
        r.getMessage() for r in caplog.records if "retry_transient" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "api.crossref.org" in lines[0]
    assert "/works/" not in lines[0]


# ---------------------------------------------------------------------------
# Call sites: first attempt raises ConnectTimeout, second answers.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr("precis.utils.http.time.sleep", sleeps.append)
    return sleeps


class _FlakyTransportHandler:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.n += 1
        if self.n == 1:
            raise httpx.ConnectTimeout("handshake timed out", request=request)
        return httpx.Response(200, json=self.body, request=request)


def test_fetch_oa_crossref_leg_retries(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    from precis.workers import fetch_oa

    link = {"URL": "https://p.example/a.pdf", "content-type": "application/pdf"}
    handler = _FlakyTransportHandler({"message": {"link": [link]}})
    real = httpx.Client

    def factory(**kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(handler)
        return real(**kw)

    monkeypatch.setattr(fetch_oa.httpx, "Client", factory)
    assert fetch_oa._query_crossref_pdf_links("10.1/x", email="a@b.c") == [
        "https://p.example/a.pdf"
    ]
    assert handler.n == 2
    assert no_sleep == [3.0]


def test_bib_parse_crossref_query_retries_in_its_own_loop_only(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    # bib_parse's attempt loop owns the connect retry; retry_transient is
    # not nested inside it, so no 3 s retry_transient sleep happens.
    from precis.workers import bib_parse

    drains: list[float] = []

    def fake_drain(seconds: float) -> bool:
        drains.append(seconds)
        return False

    monkeypatch.setattr(bib_parse, "drain_sleep", fake_drain)
    calls = {"n": 0}

    def fake_safe_get(client: Any, url: str, **kw: Any) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        return httpx.Response(200, json={"message": {"items": [{"DOI": "10.1/x"}]}})

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
    items = bib_parse._crossref_query("Smith 2020 Some title")
    assert items == [{"DOI": "10.1/x"}]
    assert calls["n"] == 2
    assert no_sleep == []
    assert drains == [1.0]


def test_provenance_doi_validity_retries(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    from precis.ingest import provenance

    calls = {"n": 0}

    def fake_safe_get(client: Any, url: str, **kw: Any) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        return httpx.Response(200, json={})

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
    assert provenance._fetch_doi_validity("10.1/x", mailto=None) == "valid"
    assert calls["n"] == 2
    assert no_sleep == [3.0]


class _FakeCrossref:
    n = 0

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def works(self, **kw: Any) -> dict[str, Any]:
        type(self).n += 1
        if type(self).n == 1:
            raise requests.exceptions.ConnectTimeout("handshake timed out")
        return {"message": {"DOI": "10.1/x", "items": []}}


def test_habanero_sites_retry_requests_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    import habanero

    from precis.ingest import crossref, provenance

    monkeypatch.setattr(habanero, "Crossref", _FakeCrossref)
    monkeypatch.setattr(crossref, "Crossref", _FakeCrossref)

    _FakeCrossref.n = 0
    assert crossref.fetch_message("10.1/x") == {"DOI": "10.1/x", "items": []}
    assert _FakeCrossref.n == 2

    _FakeCrossref.n = 0
    assert provenance._fetch_crossref_message("10.1/x", None) is not None
    assert _FakeCrossref.n == 2

    _FakeCrossref.n = 0
    assert provenance._search_crossref_works(title="t", author=None, mailto=None) == []
    assert _FakeCrossref.n == 2
    assert no_sleep == [3.0, 3.0, 3.0]


def test_si_discovery_crossref_leg_retries(no_sleep: list[float]) -> None:
    from precis.ingest import si_discovery

    seen: dict[str, int] = {}

    def fetch(url: str) -> si_discovery.HttpResult:
        seen[url] = seen.get(url, 0) + 1
        if "crossref" in url and seen[url] == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        return si_discovery.HttpResult(status=404, text="", url=url, headers={})

    result = si_discovery.discover("10.1/x", fetch)
    cr_url = f"{si_discovery._CROSSREF_API}/10.1/x"
    assert seen[cr_url] == 2
    assert no_sleep == [3.0]
    # retried -> the leg ends in a 404 miss, not an error:ConnectTimeout miss
    assert not any(
        m["source"] == si_discovery.SOURCE_CROSSREF
        and str(m["reason"]).startswith("error:")
        for m in result.misses
    )
