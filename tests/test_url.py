"""Tests for `precis.utils.url` — canonicalisation + slug derivation.

Pure logic. No network, no DB.
"""

from __future__ import annotations

import pytest

from precis.utils.url import (
    canonical_url,
    host_of,
    is_http_url,
    slug_from_url,
)


class TestCanonicalUrl:
    def test_lowercases_scheme_and_host(self) -> None:
        assert canonical_url("HTTPS://Example.COM/Foo") == "https://example.com/Foo"

    def test_path_case_preserved(self) -> None:
        # Path is case-sensitive on most servers; we leave it alone.
        url = canonical_url("https://example.com/Foo/Bar")
        assert "/Foo/Bar" in url

    def test_drops_default_port_https(self) -> None:
        assert canonical_url("https://example.com:443/x") == "https://example.com/x"

    def test_drops_default_port_http(self) -> None:
        assert canonical_url("http://example.com:80/x") == "http://example.com/x"

    def test_keeps_nondefault_port(self) -> None:
        assert canonical_url("https://example.com:8443/") == "https://example.com:8443/"

    def test_strips_utm_params(self) -> None:
        url = canonical_url(
            "https://example.com/x?utm_source=newsletter&utm_medium=email&keep=1"
        )
        assert url == "https://example.com/x?keep=1"

    def test_strips_tracking_exact_params(self) -> None:
        url = canonical_url("https://example.com/x?fbclid=abc&keep=1")
        assert url == "https://example.com/x?keep=1"

    def test_empty_query_after_strip(self) -> None:
        url = canonical_url("https://example.com/x?utm_source=x")
        assert url == "https://example.com/x"

    def test_strips_trailing_slash_except_root(self) -> None:
        assert canonical_url("https://example.com/x/") == "https://example.com/x"
        assert canonical_url("https://example.com/") == "https://example.com/"

    def test_strips_fragment_on_normal_host(self) -> None:
        assert canonical_url("https://example.com/x#section") == "https://example.com/x"

    def test_keeps_fragment_on_spa_host(self) -> None:
        # arxiv preserves fragments
        assert (
            canonical_url("https://arxiv.org/abs/2301.12345#sec-1")
            == "https://arxiv.org/abs/2301.12345#sec-1"
        )

    def test_keeps_fragment_on_github(self) -> None:
        assert (
            canonical_url("https://github.com/x/y/blob/main/file.py#L42")
            == "https://github.com/x/y/blob/main/file.py#L42"
        )

    def test_idempotent(self) -> None:
        url = "HTTPS://Example.COM:443/Foo/?utm_source=x#anchor"
        once = canonical_url(url)
        twice = canonical_url(once)
        assert once == twice

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_url("")

    def test_no_scheme_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_url("example.com/x")

    def test_no_host_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_url("https:///x")


class TestIsHttpUrl:
    def test_http(self) -> None:
        assert is_http_url("http://example.com")

    def test_https(self) -> None:
        assert is_http_url("https://example.com/x")

    def test_ftp_rejected(self) -> None:
        assert not is_http_url("ftp://example.com")

    def test_no_scheme_rejected(self) -> None:
        assert not is_http_url("example.com")

    def test_garbage_rejected(self) -> None:
        assert not is_http_url("not a url")


class TestSlugFromUrl:
    def test_simple(self) -> None:
        assert slug_from_url("https://example.com/") == "example-com"

    def test_strips_www(self) -> None:
        assert slug_from_url("https://www.example.com/x") == "example-com-x"

    def test_path_segments(self) -> None:
        assert (
            slug_from_url("https://github.com/modelcontextprotocol/servers")
            == "github-com-modelcontextprotocol-servers"
        )

    def test_arxiv(self) -> None:
        assert (
            slug_from_url("https://arxiv.org/abs/2301.12345")
            == "arxiv-org-abs-2301-12345"
        )

    def test_caps_at_max_len(self) -> None:
        long = "https://example.com/" + ("x" * 200)
        slug = slug_from_url(long, max_len=30)
        assert len(slug) <= 30
        assert slug.startswith("example-com-")

    def test_skips_dotted_segments(self) -> None:
        slug = slug_from_url("https://example.com/foo/./bar/../baz")
        assert "foo" in slug and "bar" in slug and "baz" in slug

    def test_caps_path_depth(self) -> None:
        # 6 path segments → only first 5 used
        slug = slug_from_url("https://example.com/a/b/c/d/e/f/g")
        assert "f" not in slug
        assert "g" not in slug


class TestHostOf:
    def test_simple(self) -> None:
        assert host_of("https://example.com/x") == "example.com"

    def test_strips_www(self) -> None:
        assert host_of("https://www.example.com") == "example.com"

    def test_uppercase(self) -> None:
        assert host_of("HTTPS://EXAMPLE.COM/x") == "example.com"

    def test_garbage(self) -> None:
        assert host_of("not a url") == ""
