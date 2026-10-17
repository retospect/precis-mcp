"""gr493222 S0: mirror pacing/backoff, honest CORE UA, no fake mailto."""

from __future__ import annotations

import contextlib
import random
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from precis.ingest import si_discovery
from precis.nanopub import mirror
from precis.workers import fetch_oa


def test_mirror_default_delay_is_at_least_one_second() -> None:
    assert mirror.FETCH_DELAY_S >= 1.0


def test_sync_sleeps_the_default_delay_between_artifacts(monkeypatch: Any) -> None:
    codes = [f"RA{c * 43}" for c in "abc"]
    monkeypatch.setattr(mirror, "list_registry_codes", lambda fetch=None: (codes, "h"))
    monkeypatch.setattr(mirror, "fetch_code", lambda *a, **k: b"x")
    monkeypatch.setattr(
        mirror, "ingest_one", lambda *a, **k: SimpleNamespace(verified=True)
    )
    store: Any = SimpleNamespace(mirror_codes=lambda: set())
    slept: list[float] = []
    res = mirror.sync(store, limit=10, sleep=slept.append)
    assert res.fetched == 3
    assert slept == [mirror.FETCH_DELAY_S] * 3
    assert all(s >= 1.0 for s in slept)


def test_failover_backs_off_with_jitter() -> None:
    tried: list[str] = []

    def fetch(url: str) -> bytes:
        tried.append(url)
        if url.startswith(mirror.MIRROR_HOSTS[-1]):
            return b"[]"
        raise OSError("down")

    slept: list[float] = []
    codes, host = mirror.list_registry_codes(
        fetch=fetch, sleep=slept.append, rng=random.Random(7)
    )
    assert host == mirror.MIRROR_HOSTS[-1] and codes == []
    assert len(tried) == len(mirror.MIRROR_HOSTS)
    # One sleep per failover, none before the first host, each jittered
    # within the exponential ceiling.
    assert len(slept) == len(mirror.MIRROR_HOSTS) - 1
    pol = mirror.FAILOVER_POLICY
    for i, s in enumerate(slept):
        assert 0.0 <= s <= min(pol.cap, pol.base * 2**i)
    assert len(set(slept)) == len(slept)


def test_first_host_success_does_not_sleep() -> None:
    slept: list[float] = []
    mirror.list_registry_codes(fetch=lambda u: b"[]", sleep=slept.append)
    assert slept == []


def _contact(monkeypatch: Any, value: str | None) -> None:
    monkeypatch.setattr(
        fetch_oa.settings, "get_str", lambda key, *a, **k: value, raising=True
    )


def test_core_request_carries_precis_ua_not_browser(monkeypatch: Any) -> None:
    _contact(monkeypatch, "ops@lab.test")
    seen: list[str] = []
    real = fetch_oa.http_client

    @contextlib.contextmanager
    def spy(*a: Any, **k: Any) -> Any:
        with real(*a, **k) as client:

            def get(url: str, **kw: Any) -> httpx.Response:
                seen.append(client.headers["User-Agent"])
                return httpx.Response(
                    200, json={"results": []}, request=httpx.Request("GET", url)
                )

            loose: Any = client
            loose.get = get
            yield client

    monkeypatch.setattr(fetch_oa, "http_client", spy)
    fetch_oa._query_core_fulltext_urls("10.1/x", api_key="k")
    assert seen and seen[0].startswith("precis-mcp/")
    assert "Mozilla" not in seen[0] and "Chrome" not in seen[0]


@pytest.mark.parametrize("contact", [None, ""])
def test_fetch_oa_ua_falls_back_to_the_maintainer_contact(
    monkeypatch: Any, contact: str | None
) -> None:
    # S3: the UA is the shared polite_user_agent(), which names the real
    # fallback contact rather than sending none (never a made-up address).
    from precis.utils.http import FALLBACK_CONTACT, check_user_agent

    _contact(monkeypatch, contact)
    ua = fetch_oa._user_agent_header()
    assert ua.startswith("precis-mcp/") and f"mailto:{FALLBACK_CONTACT}" in ua
    assert check_user_agent(ua) == ua
    assert "example.com" not in ua


def test_fetch_oa_ua_uses_real_contact(monkeypatch: Any) -> None:
    _contact(monkeypatch, None)
    assert "mailto:me@lab.test" in fetch_oa._user_agent_header("me@lab.test")


@pytest.mark.parametrize("email", ["", "me@lab.test"])
def test_si_discovery_ua_never_fake_mailto(monkeypatch: Any, email: str) -> None:
    seen: list[str | None] = []

    @contextlib.contextmanager
    def spy(*a: Any, **k: Any) -> Any:
        seen.append(k.get("user_agent"))
        yield SimpleNamespace()

    monkeypatch.setattr("precis.utils.http.http_client", spy)
    monkeypatch.setattr(
        "precis.utils.safe_fetch.safe_get",
        lambda c, u: httpx.Response(404, request=httpx.Request("GET", u), text=""),
    )
    si_discovery.default_fetch(email)("https://example.org/x")
    ua = seen[0] or ""
    assert "example.com" not in ua and "noreply" not in ua
    if email:
        assert "mailto:me@lab.test" in ua
    else:
        from precis.utils.http import check_user_agent

        assert check_user_agent(ua) == ua and "mailto:" in ua


def test_openalex_meta_ua_never_fake_mailto() -> None:
    from precis.ingest import openalex_meta

    assert "mailto" not in openalex_meta._ua("")
    assert "example.com" not in openalex_meta._ua("")
    assert "mailto:me@lab.test" in openalex_meta._ua("me@lab.test")
