"""Tests for `WebHandler` — page-fetch caching kind.

httpx is mocked at the call-site (we replace the `httpx.Client` factory
with a stub) so tests stay offline. Trafilatura's extraction is real
— extraction is pure-CPU and self-contained.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput, Upstream
from precis.handlers.web import WebHandler, _extract_title
from precis.store import Store

# ── stub httpx.Client ────────────────────────────────────────────────


class _StubResp:
    def __init__(
        self,
        *,
        text: str = "",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html"}


class _StubClient:
    """Drop-in for `httpx.Client(...)` context manager."""

    last_url: str | None = None
    response: _StubResp | None = None
    raise_on_get: Exception | None = None

    def __init__(self, **_kw: Any) -> None:
        pass

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_e: Any) -> None:
        pass

    def get(self, url: str) -> _StubResp:
        type(self).last_url = url
        if type(self).raise_on_get is not None:
            raise type(self).raise_on_get  # type: ignore[misc]
        assert type(self).response is not None
        return type(self).response


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.Client to the stub for every web test."""
    import httpx

    _StubClient.last_url = None
    _StubClient.response = _StubResp(text=_SAMPLE_HTML)
    _StubClient.raise_on_get = None

    monkeypatch.setattr(httpx, "Client", _StubClient)


@pytest.fixture
def handler(store: Store) -> WebHandler:
    return WebHandler(store=store)


_SAMPLE_HTML = """
<html>
<head><title>Sample article — Example Site</title></head>
<body>
<header><nav>top nav stuff</nav></header>
<article>
<h1>The main headline</h1>
<p>The first real paragraph of the article. It is informative and substantive.</p>
<p>A second paragraph with more content so trafilatura considers this an article.</p>
<p>Yet another paragraph. The body should be extracted and the chrome (nav, footer) stripped.</p>
</article>
<footer>copyright 2026</footer>
</body>
</html>
""".strip()


# ── basic flow ────────────────────────────────────────────────────────


def test_first_fetch_extracts_and_caches(handler: WebHandler) -> None:
    resp = handler.get(id="https://example.com/article")
    # Trafilatura should pull out the article body.
    assert "first real paragraph" in resp.body
    # And drop the chrome.
    assert "top nav stuff" not in resp.body
    assert "copyright 2026" not in resp.body
    # Title from <title> tag.
    assert "Sample article" in resp.body
    # Attribution + source URL line.
    assert "Source: web page" in resp.body
    assert "Source: https://example.com/article" in resp.body
    # Free, fresh fetch.
    assert resp.cost == "[cost: free]"


def test_second_call_hits_cache(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    handler.get(id="https://example.com/article")
    # `_StubClient.last_url` is overwritten on each get; we know the
    # second call is a cache hit because the body didn't change. The
    # cleanest signal: the cache_state row only has one fetched_at.
    # Easier: hit the upstream once, then change the response — if we
    # cached, the second call still returns the original body.
    _StubClient.response = _StubResp(text="<html><body>different</body></html>")
    resp2 = handler.get(id="https://example.com/article")
    assert "first real paragraph" in resp2.body  # still cached
    assert "different" not in resp2.body


def test_canonicalization_collapses_variants(handler: WebHandler) -> None:
    handler.get(id="https://Example.COM/article")
    handler.get(id="https://example.com/article?utm_source=newsletter")
    handler.get(id="https://example.com/article#anchor")
    # All three should canonicalise to the same key → one upstream call.
    # We can't easily count without monkey-patching the store; rely on
    # the body not having the second/third stub responses (which we
    # don't change here — cache hit means the same body is returned).
    # Safer check: the cache slug is the same for all three.
    canonical_url_str = "https://example.com/article"
    expected_slug = "example-com-article"
    cached = handler.store.get_cache_entry(
        provider="web",
        request_hash=handler._hash(canonical_url_str),  # type: ignore[arg-type]
    )
    assert cached is not None
    ref, _ = cached
    assert ref.slug == expected_slug


# ── input validation ─────────────────────────────────────────────────


def test_non_url_raises(handler: WebHandler) -> None:
    with pytest.raises(BadInput, match="not a valid URL"):
        handler.get(id="not a url")


def test_ftp_url_rejected(handler: WebHandler) -> None:
    with pytest.raises(BadInput, match="http"):
        handler.get(id="ftp://example.com/file.txt")


# ── upstream errors ──────────────────────────────────────────────────


def test_4xx_status_raises_upstream(handler: WebHandler) -> None:
    _StubClient.response = _StubResp(text="not found", status_code=404)
    with pytest.raises(Upstream, match="HTTP 404"):
        handler.get(id="https://example.com/missing")


def test_5xx_status_raises_upstream(handler: WebHandler) -> None:
    _StubClient.response = _StubResp(text="server error", status_code=500)
    with pytest.raises(Upstream, match="HTTP 500"):
        handler.get(id="https://example.com/down")


def test_network_error_raises_upstream(handler: WebHandler) -> None:
    import httpx

    _StubClient.raise_on_get = httpx.ConnectError("DNS fail")
    with pytest.raises(Upstream, match="fetch failed"):
        handler.get(id="https://nonexistent.example/")


# ── extraction edge cases ────────────────────────────────────────────


def test_low_text_page_falls_back_to_stub(handler: WebHandler) -> None:
    """Login wall / JS shell — trafilatura returns nothing; we cache a stub."""
    _StubClient.response = _StubResp(
        text="<html><body><div id='app'></div></body></html>"
    )
    resp = handler.get(id="https://js-shell.example/")
    assert "no readable content extracted" in resp.body


def test_extracts_title_from_html() -> None:
    assert _extract_title("<html><head><title>Hi</title></head></html>") == "Hi"
    assert (
        _extract_title("<html><head><title>  Hello\n  World  </title></head>")
        == "Hello World"
    )


def test_no_title_returns_none() -> None:
    assert _extract_title("<html><body>no title</body></html>") is None


# ── cache key sanity ─────────────────────────────────────────────────


def test_slug_uses_human_readable_form(handler: WebHandler) -> None:
    handler.get(id="https://github.com/modelcontextprotocol/servers")
    cached = handler.store.get_cache_entry(
        provider="web",
        request_hash=handler._hash("https://github.com/modelcontextprotocol/servers"),  # type: ignore[arg-type]
    )
    assert cached is not None
    ref, _ = cached
    assert "github" in ref.slug
    assert "modelcontextprotocol" in ref.slug
    assert "servers" in ref.slug
