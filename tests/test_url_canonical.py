"""Tests for :mod:`precis.url_canonical`.

Pure-function tests: no network, no store, no fixtures.  Every
canonicalisation rule documented at the top of ``url_canonical.py`` is
exercised by at least one test here.
"""

from __future__ import annotations

import pytest

from precis.url_canonical import (
    SPA_HOSTS,
    TRACKING_PARAMS,
    canonicalise_url,
    host_of,
    is_http_url,
    slug_from_url,
)


# ---------------------------------------------------------------------------
# canonicalise_url
# ---------------------------------------------------------------------------


class TestCanonicaliseUrl:
    def test_trims_whitespace(self):
        assert (
            canonicalise_url("  https://example.com/page  ")
            == "https://example.com/page"
        )

    def test_lowercases_scheme_and_host(self):
        assert (
            canonicalise_url("HTTPS://EXAMPLE.COM/Foo")
            == "https://example.com/Foo"
        )

    def test_preserves_path_case(self):
        # Paths are case-sensitive on most servers; never lowercase them.
        assert (
            canonicalise_url("https://example.com/My/Page")
            == "https://example.com/My/Page"
        )

    def test_drops_default_port_https(self):
        assert (
            canonicalise_url("https://example.com:443/x")
            == "https://example.com/x"
        )

    def test_drops_default_port_http(self):
        assert (
            canonicalise_url("http://example.com:80/x")
            == "http://example.com/x"
        )

    def test_keeps_nondefault_port(self):
        assert (
            canonicalise_url("https://example.com:8443/x")
            == "https://example.com:8443/x"
        )

    def test_strips_trailing_slash(self):
        assert (
            canonicalise_url("https://example.com/foo/")
            == "https://example.com/foo"
        )

    def test_keeps_root_slash(self):
        assert canonicalise_url("https://example.com/") == "https://example.com/"

    def test_empty_path_normalised_to_root(self):
        assert canonicalise_url("https://example.com") == "https://example.com/"

    def test_strips_utm_params(self):
        result = canonicalise_url(
            "https://example.com/x?utm_source=twitter&utm_medium=social&keep=1"
        )
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "keep=1" in result

    def test_strips_fbclid_gclid(self):
        result = canonicalise_url(
            "https://example.com/x?fbclid=abc&gclid=def&q=search"
        )
        assert "fbclid" not in result
        assert "gclid" not in result
        assert "q=search" in result

    def test_strips_all_tracking_when_query_becomes_empty(self):
        # All params were tracking → entire query dropped.
        assert (
            canonicalise_url("https://example.com/x?utm_source=a&utm_medium=b")
            == "https://example.com/x"
        )

    def test_preserves_pagination_params(self):
        # Genuine routing params (page, q, id) must survive.
        result = canonicalise_url("https://example.com/list?page=2&q=foo")
        assert "page=2" in result
        assert "q=foo" in result

    def test_strips_fragment_for_regular_host(self):
        assert (
            canonicalise_url("https://example.com/page#anchor")
            == "https://example.com/page"
        )

    def test_preserves_fragment_for_spa_hosts(self):
        # arxiv.org is in SPA_HOSTS — fragment is part of the route.
        result = canonicalise_url("https://arxiv.org/abs/2301.12345#section-2")
        assert result.endswith("#section-2")

        # github.com blob line ref.
        result = canonicalise_url(
            "https://github.com/foo/bar/blob/main/x.py#L42"
        )
        assert result.endswith("#L42")

    def test_strips_www_is_not_done(self):
        # canonicalise_url deliberately does NOT strip ``www.`` — some
        # sites have distinct content on the bare apex.  Only the slug
        # derivation strips www for readability.
        assert (
            canonicalise_url("https://www.example.com/x")
            == "https://www.example.com/x"
        )

    def test_raises_on_empty(self):
        with pytest.raises(ValueError, match="empty"):
            canonicalise_url("")
        with pytest.raises(ValueError, match="empty"):
            canonicalise_url("   ")

    def test_raises_on_missing_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            canonicalise_url("example.com/page")

    def test_raises_on_missing_host(self):
        with pytest.raises(ValueError, match="host"):
            canonicalise_url("https:///path")

    def test_non_http_scheme_passes_through(self):
        # ftp:, file:, mailto: — caller can check is_http_url if they
        # need to reject these.  We just trim + lowercase scheme.
        assert (
            canonicalise_url("FTP://Example.com/file")
            == "ftp://Example.com/file"
        )

    def test_userinfo_preserved(self):
        result = canonicalise_url("https://user:pass@example.com/private")
        assert "user:pass@example.com" in result

    def test_idempotent(self):
        # Running twice should be a no-op — the classic canonical
        # function invariant.
        once = canonicalise_url(
            "HTTPS://Example.COM:443/Foo/?utm_source=x&page=1#anchor"
        )
        twice = canonicalise_url(once)
        assert once == twice
        assert once == "https://example.com/Foo?page=1"

    def test_spa_hosts_set_is_frozen(self):
        # Regression guard: SPA_HOSTS must be immutable so a module-level
        # mutation can't change fragment-stripping semantics at runtime.
        with pytest.raises(AttributeError):
            SPA_HOSTS.add("newhost.com")  # type: ignore[attr-defined]

    def test_tracking_params_set_is_frozen(self):
        with pytest.raises(AttributeError):
            TRACKING_PARAMS.add("newtrack")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# is_http_url
# ---------------------------------------------------------------------------


class TestIsHttpUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com",
            "https://example.com/path?q=1",
            "http://example.com:8080",
        ],
    )
    def test_accepts_http(self, url):
        assert is_http_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "mailto:foo@bar.com",
            "javascript:alert(1)",
            "",
            "   ",
            "not a url",
            "example.com",
        ],
    )
    def test_rejects_non_http(self, url):
        assert not is_http_url(url)


# ---------------------------------------------------------------------------
# slug_from_url
# ---------------------------------------------------------------------------


class TestSlugFromUrl:
    def test_bare_host(self):
        assert slug_from_url("https://example.com/") == "example-com"

    def test_strips_www(self):
        # www.example.com and example.com → same slug so they de-dupe.
        assert slug_from_url("https://www.example.com/") == "example-com"

    def test_path_segments(self):
        assert (
            slug_from_url("https://github.com/modelcontextprotocol/servers")
            == "github-com-modelcontextprotocol-servers"
        )

    def test_caps_length(self):
        slug = slug_from_url(
            "https://example.com/a-very-long-path-with-many-segments-"
            "that-should-definitely-get-truncated-eventually",
            max_len=30,
        )
        assert len(slug) <= 30
        assert not slug.endswith("-")

    def test_strips_non_slug_chars(self):
        # Percent-encoded spaces, uppercase, punctuation all normalise.
        slug = slug_from_url("https://example.com/foo%20bar/baz.html")
        assert slug == "example-com-foo-bar-baz-html"

    def test_cap_path_depth(self):
        # Deep URLs stop at 5 segments to prevent 300-char slugs.
        slug = slug_from_url(
            "https://example.com/a/b/c/d/e/f/g/h/i/j"
        )
        # Host + at most 5 path segments.
        parts = slug.split("-")
        assert parts[0] == "example"
        assert parts[1] == "com"
        # remaining should be at most 5 segment tokens
        assert len([p for p in parts[2:] if p]) <= 5

    def test_empty_path(self):
        # No path → slug is just the host.
        slug = slug_from_url("https://example.com")
        assert slug == "example-com"

    def test_deterministic(self):
        # Identical input → identical output (important for DB
        # uniqueness-as-dedup).
        url = "https://news.ycombinator.com/item?id=12345"
        assert slug_from_url(url) == slug_from_url(url)

    def test_distinct_pages_distinct_slugs(self):
        # Different canonical URLs must produce different slugs.
        a = slug_from_url("https://example.com/a")
        b = slug_from_url("https://example.com/b")
        assert a != b


# ---------------------------------------------------------------------------
# host_of
# ---------------------------------------------------------------------------


class TestHostOf:
    def test_basic(self):
        assert host_of("https://example.com/path") == "example.com"

    def test_strips_www(self):
        assert host_of("https://www.example.com/path") == "example.com"

    def test_lowercases(self):
        assert host_of("https://EXAMPLE.COM/") == "example.com"

    def test_empty_on_invalid(self):
        assert host_of("not a url") == ""

    def test_empty_on_empty(self):
        assert host_of("") == ""
