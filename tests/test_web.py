"""Tests for `WebHandler` — page-fetch caching kind.

httpx is mocked at the call-site (we replace the `httpx.Client` factory
with a stub) so tests stay offline. Trafilatura's extraction is real
— extraction is pure-CPU and self-contained.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Upstream
from precis.handlers.web import WebHandler, _extract_title

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
        resp = type(self).response
        assert resp is not None
        return resp


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.Client to the stub for every web test."""
    import httpx

    _StubClient.last_url = None
    _StubClient.response = _StubResp(text=_SAMPLE_HTML)
    _StubClient.raise_on_get = None

    monkeypatch.setattr(httpx, "Client", _StubClient)


@pytest.fixture
def handler(hub: Hub) -> WebHandler:
    return WebHandler(hub=hub)


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


def test_get_by_slug_round_trips(handler: WebHandler) -> None:
    """The slug printed in ``/recent`` (and accepted by ``tag`` /
    ``link``) must round-trip through ``get(id=<slug>)``. Without
    this, the listing trailer's ``Next: get(kind='web', id='<slug>')
    to read one`` instruction ran straight into the URL canonicaliser
    and rejected the slug as ``not a valid URL``. (MCP critic
    MAJOR-C 2026-05-02.)
    """
    # Populate the cache so ``example-com-article`` becomes a known
    # slug.
    handler.get(id="https://example.com/article")

    # Slug-form get: must return the cached body without raising.
    resp = handler.get(id="example-com-article")
    assert "first real paragraph" in resp.body
    # Slug round-trip retains the same body the URL fetch produced.
    url_resp = handler.get(id="https://example.com/article")
    assert resp.body == url_resp.body


def test_get_by_unknown_slug_still_raises_url_error(handler: WebHandler) -> None:
    """Slug fallback only fires when ``_canonical_key`` raises AND
    a stored slug matches. A bare non-URL non-slug input still
    surfaces the original ``not a valid URL`` BadInput so 7B
    callers see the diagnostic and recover with a real URL.
    """
    # No fetch — ``unknown-slug`` is neither URL nor cached slug.
    with pytest.raises(BadInput, match="not a valid URL"):
        handler.get(id="unknown-slug")


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


# ── block-parsing + search ──────────────────────────────────────────
#
# Since the web-bookmark patch, ``_fetch`` routes the extracted
# markdown through ``_blocks_from_report`` so each paragraph /
# heading / list becomes one embedded block. That turns every
# fetched page into a searchable corpus member — both lexical
# (full-text index) and semantic (vector). These tests cover the
# new plumbing end-to-end.


def test_fetch_produces_multiple_blocks(handler: WebHandler) -> None:
    """A real article body must land as many blocks, not one
    monolithic row — that's what makes paragraph-level search
    useful."""
    handler.get(id="https://example.com/article")
    cached = handler.store.get_cache_entry(
        provider="web",
        request_hash=handler._hash("https://example.com/article"),  # type: ignore[arg-type]
    )
    assert cached is not None
    ref, _ = cached
    blocks = handler.store.list_blocks_for_ref(ref.id)
    # The sample HTML has a heading + three paragraphs → at least
    # three text blocks (trafilatura may fold tighter, but not
    # into one).
    assert len(blocks) >= 2, f"only {len(blocks)} block(s); expected paragraph split"


def test_fetched_blocks_are_embedded(handler: WebHandler) -> None:
    """Each extracted block must carry an embedding so semantic
    search lands hits and random() can pick from web pages."""
    handler.get(id="https://example.com/article")
    cached = handler.store.get_cache_entry(
        provider="web",
        request_hash=handler._hash("https://example.com/article"),  # type: ignore[arg-type]
    )
    assert cached is not None
    ref, _ = cached
    blocks = handler.store.list_blocks_for_ref(ref.id, with_embedding=True)
    # At least one block must carry a vector — the fallback
    # "couldn't parse" branch stores un-embedded blocks, so we
    # don't require every block to be embedded (parser may drop a
    # stub line), only that the typical path produces vectors.
    assert any(b.embedding is not None for b in blocks), (
        "no block carries an embedding; _blocks_from_report isn't wired"
    )


def test_search_finds_content_inside_fetched_page(
    handler: WebHandler,
) -> None:
    """``search(kind='web', q=...)`` lands a hit on body text of a
    previously-fetched page. This is the core capability the
    bookmark patch unlocks."""
    handler.get(id="https://example.com/article")
    resp = handler.search(q="first real paragraph")
    # The search header mentions the hit count and query echo.
    assert "first real paragraph" in resp.body or "first-real" in resp.body
    # Slug of the cached ref appears as a numbered hit.
    assert "example-com-article" in resp.body


def test_search_empty_query_raises(handler: WebHandler) -> None:
    with pytest.raises(BadInput, match="search requires q="):
        handler.search(q="")


def test_search_no_hits_returns_recovery_hint(handler: WebHandler) -> None:
    """Empty result set must include an actionable next-step so
    the caller knows whether to widen the query or fetch first."""
    resp = handler.search(q="nothing-will-match-this-xyzzy")
    assert "no web blocks match" in resp.body
    # Hint points at the example URL so agents know the cache
    # needs populating first.
    assert "https://example.com/article" in resp.body


# ── tag (bookmark) ──────────────────────────────────────────────────
#
# Tag usage on cache-backed kinds is identical to ref-backed
# kinds: closed prefixes are gated by the ``kinds.tags_allowed``
# axis, open tags pass through. For web we care most about the
# open-tag path (``add=['bookmark']``) and the ``CACHE:pinned``
# closed tag (prevent expiry).


def test_tag_bookmark_open_tag(handler: WebHandler) -> None:
    """Adding an open tag like ``bookmark`` should succeed and the
    ack should echo the count."""
    handler.get(id="https://example.com/article")
    resp = handler.tag(id="example-com-article", add=["bookmark"])
    assert "+1 tag" in resp.body
    # The tag must actually land on the ref.
    ref = handler.store.get_ref(kind="web", id="example-com-article")
    assert ref is not None
    tags = handler.store.tags_for(ref.id)
    assert any(t.value == "bookmark" for t in tags)


def test_tag_cache_pinned_closed_tag(handler: WebHandler) -> None:
    """``CACHE:pinned`` is a closed-axis tag allowed on every
    cache-backed kind."""
    handler.get(id="https://example.com/article")
    resp = handler.tag(id="example-com-article", add=["CACHE:pinned"])
    assert "+1 tag" in resp.body


def test_tag_before_fetch_raises_notfound(handler: WebHandler) -> None:
    """You can't tag a slug that isn't in the cache — NotFound
    with a "fetch first" recovery hint."""
    from precis.errors import NotFound

    with pytest.raises(NotFound, match="not found") as exc:
        handler.tag(id="never-fetched-slug", add=["bookmark"])
    assert exc.value.next is not None
    # Recovery hint tells the agent to fetch first.
    assert "get(kind='web'" in exc.value.next


def test_tag_requires_add_or_remove(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    with pytest.raises(BadInput, match="requires add= or remove="):
        handler.tag(id="example-com-article")


def test_untag_removes(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    handler.tag(id="example-com-article", add=["bookmark"])
    resp = handler.tag(id="example-com-article", remove=["bookmark"])
    assert "-1 tag" in resp.body
    ref = handler.store.get_ref(kind="web", id="example-com-article")
    assert ref is not None
    tags = handler.store.tags_for(ref.id)
    assert all(t.value != "bookmark" for t in tags)


# ── link (cross-reference) ──────────────────────────────────────────
#
# The canonical use-case the user named: link a fetched page to a
# memory that explains why it matters. ``target='memory:123'`` or
# ``target='paper:slug'`` both work.


def test_link_to_memory(handler: WebHandler, hub: Hub) -> None:
    """Link a web ref to a memory ref — the most common bookmark
    pattern ("I kept this page for the idea I wrote down in
    memory 42")."""
    # Seed a memory to link against.
    store = hub.store
    assert store is not None
    mem = store.insert_ref(kind="memory", slug=None, title="the idea")
    # Fetch the web page.
    handler.get(id="https://example.com/article")
    # Link web → memory.
    resp = handler.link(
        id="example-com-article",
        target=f"memory:{mem.id}",
    )
    assert "+1 link" in resp.body
    # The link must actually land.
    web_ref = handler.store.get_ref(kind="web", id="example-com-article")
    assert web_ref is not None
    links = handler.store.links_for(web_ref.id, direction="out")
    assert len(links) == 1
    assert links[0].dst_ref_id == mem.id


def test_link_to_paper(handler: WebHandler, hub: Hub) -> None:
    """``target='paper:slug'`` works the same way — web pages as
    supplementary reading for a paper."""
    store = hub.store
    assert store is not None
    paper = store.insert_ref(kind="paper", slug="miller2000food", title="Food")
    handler.get(id="https://example.com/article")
    resp = handler.link(
        id="example-com-article",
        target="paper:miller2000food",
    )
    assert "+1 link" in resp.body
    web_ref = handler.store.get_ref(kind="web", id="example-com-article")
    assert web_ref is not None
    links = handler.store.links_for(web_ref.id, direction="out")
    assert any(link.dst_ref_id == paper.id for link in links)


def test_unlink_removes(handler: WebHandler, hub: Hub) -> None:
    store = hub.store
    assert store is not None
    mem = store.insert_ref(kind="memory", slug=None, title="the idea")
    handler.get(id="https://example.com/article")
    handler.link(id="example-com-article", target=f"memory:{mem.id}")
    resp = handler.link(
        id="example-com-article",
        target=f"memory:{mem.id}",
        mode="remove",
    )
    assert "-1 link" in resp.body


def test_link_requires_target(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    with pytest.raises(BadInput, match="requires target="):
        handler.link(id="example-com-article")


def test_link_bad_mode_rejected(handler: WebHandler) -> None:
    handler.get(id="https://example.com/article")
    with pytest.raises(BadInput, match="link mode must be"):
        handler.link(
            id="example-com-article",
            target="memory:1",
            mode="toggle",
        )


# ── KindSpec advertises the new verbs ───────────────────────────────


def test_kindspec_declares_search_tag_link() -> None:
    """The dispatcher uses these flags to wire tools/list — if they
    regress, ``search(kind='web')`` stops showing up in the MCP
    surface even though the handler method still exists."""
    spec = WebHandler.spec
    assert spec.supports_search is True
    assert spec.supports_search_hits is True
    assert spec.supports_tag is True
    assert spec.supports_link is True
