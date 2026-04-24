"""Tests for :mod:`precis.web_archive` — archive.org policy + client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis.web_archive import (
    ArchiveResult,
    SkipReason,
    _BUCKET,
    _extract_wayback_url,
    archive_url,
    global_auto_archive,
    is_private_url,
    reset_rate_limiter,
)


@pytest.fixture(autouse=True)
def _clean_bucket():
    """Each test starts with an empty rate-limit window."""
    reset_rate_limiter()
    yield
    reset_rate_limiter()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with auto-archive unset (defaults on)."""
    monkeypatch.delenv("PRECIS_WEB_AUTO_ARCHIVE", raising=False)
    yield


# ---------------------------------------------------------------------------
# Private-URL guard
# ---------------------------------------------------------------------------


class TestIsPrivateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/",
            "http://localhost:8080/api",
            "https://127.0.0.1/",
            "https://[::1]/",
            "http://ip6-localhost/",
        ],
    )
    def test_loopback_blocked(self, url):
        private, reason = is_private_url(url)
        assert private
        assert reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/",
            "http://10.255.255.254/",
            "http://172.16.0.1/",
            "http://172.31.0.1/",
            "http://192.168.1.1/",
            "http://192.168.6.8/",  # matches cluster LAN
        ],
    )
    def test_rfc1918_blocked(self, url):
        private, reason = is_private_url(url)
        assert private
        assert "private" in reason.lower() or "ip" in reason.lower()

    def test_cgnat_blocked(self):
        # Tailscale uses 100.64.0.0/10 — Python's ip.is_private returns
        # False for this range, so we need the explicit check.
        private, reason = is_private_url("http://100.110.11.106/")
        assert private
        assert "private IP" in reason or "100." in reason

    def test_cgnat_edge(self):
        # 100.64.0.0 is the start, 100.127.255.255 is the end.
        assert is_private_url("http://100.64.0.0/")[0]
        assert is_private_url("http://100.127.255.255/")[0]
        # 100.63.x.x and 100.128.x.x are *not* CGNAT.
        assert not is_private_url("http://100.63.0.1/")[0]
        assert not is_private_url("http://100.128.0.1/")[0]

    @pytest.mark.parametrize(
        "host_suffix",
        [".local", ".internal", ".lan", ".home.arpa", ".test", ".invalid"],
    )
    def test_private_tlds_blocked(self, host_suffix):
        url = f"https://myhost{host_suffix}/page"
        private, reason = is_private_url(url)
        assert private
        assert host_suffix in reason

    def test_non_http_blocked(self):
        private, reason = is_private_url("file:///etc/passwd")
        assert private
        assert "scheme" in reason

        private, reason = is_private_url("ftp://ftp.example.com/")
        assert private

    def test_empty_blocked(self):
        assert is_private_url("")[0]

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/",
            "https://github.com/org/repo",
            "http://en.wikipedia.org/wiki/Python",
            "https://8.8.8.8/",  # Google DNS — public IP, should pass.
        ],
    )
    def test_public_urls_pass(self, url):
        private, reason = is_private_url(url)
        assert not private, f"{url!r} wrongly blocked: {reason}"

    def test_malformed_url_blocked(self):
        # A malformed URL can't be archived anyway; guard returns True.
        private, reason = is_private_url("http://")
        assert private

    def test_dns_resolve_flag_off_by_default(self):
        # If resolve_dns=False (default), a hostname-only URL that
        # points at a private IP *might* slip through — we only check
        # syntactic form.  Document this behaviour explicitly.
        # Use a public-looking name; since we don't hit DNS, this passes.
        private, _ = is_private_url("https://some-example-host.com/")
        assert not private


# ---------------------------------------------------------------------------
# global_auto_archive env flag
# ---------------------------------------------------------------------------


class TestGlobalAutoArchive:
    def test_default_on_when_unset(self, monkeypatch):
        monkeypatch.delenv("PRECIS_WEB_AUTO_ARCHIVE", raising=False)
        assert global_auto_archive() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRECIS_WEB_AUTO_ARCHIVE", "")
        assert global_auto_archive() is True

    @pytest.mark.parametrize("val", ["0", "no", "NO", "false", "False", "off"])
    def test_off_values(self, monkeypatch, val):
        monkeypatch.setenv("PRECIS_WEB_AUTO_ARCHIVE", val)
        assert global_auto_archive() is False

    @pytest.mark.parametrize("val", ["1", "yes", "true", "on", "y"])
    def test_on_values(self, monkeypatch, val):
        monkeypatch.setenv("PRECIS_WEB_AUTO_ARCHIVE", val)
        assert global_auto_archive() is True


# ---------------------------------------------------------------------------
# archive_url — policy paths (no HTTP)
# ---------------------------------------------------------------------------


class TestArchivePolicy:
    def test_per_call_optout_short_circuits(self):
        # Per-call False beats every other check — no private-URL
        # inspection, no rate-limit call, no HTTP.
        result = archive_url("https://example.com/", requested=False)
        assert not result.ok
        assert result.skipped_reason == SkipReason.USER_OPTOUT

    def test_global_optout(self, monkeypatch):
        monkeypatch.setenv("PRECIS_WEB_AUTO_ARCHIVE", "0")
        result = archive_url("https://example.com/")
        assert not result.ok
        assert result.skipped_reason == SkipReason.GLOBAL_OPTOUT

    def test_per_call_true_overrides_global_off(self, monkeypatch):
        # requested=True bypasses the env default (per-call force-on).
        monkeypatch.setenv("PRECIS_WEB_AUTO_ARCHIVE", "0")
        # Block at the httpx-missing step (or at the network step if
        # httpx is installed in the test env).  We just care that we
        # got past the global-optout gate.
        result = archive_url("https://example.com/", requested=True)
        assert result.skipped_reason != SkipReason.GLOBAL_OPTOUT

    def test_private_url_blocked_even_when_requested(self):
        # Explicitly asking for an archive of localhost MUST fail —
        # leak guard takes precedence over user intent.
        result = archive_url("http://localhost/admin", requested=True)
        assert not result.ok
        assert result.skipped_reason == SkipReason.PRIVATE_URL
        assert "loopback" in result.detail or "localhost" in result.detail

    def test_rate_limit_short_circuits(self):
        # Fill the bucket, then try one more — should hit RATE_LIMITED
        # before any HTTP call.
        from precis.web_archive import _MAX_CALLS

        with patch("precis.web_archive._httpx") as mock_httpx:
            mock_httpx.get.return_value = _success_response()
            mock_httpx.TimeoutException = Exception
            mock_httpx.HTTPError = Exception
            for _ in range(_MAX_CALLS):
                archive_url("https://example.com/", requested=True)
            # One more — the bucket is exhausted.
            result = archive_url("https://example.com/more", requested=True)

        assert not result.ok
        assert result.skipped_reason == SkipReason.RATE_LIMITED


# ---------------------------------------------------------------------------
# archive_url — HTTP paths (mocked httpx)
# ---------------------------------------------------------------------------


def _success_response(
    content_location: str = "/web/20260424010203/https://example.com/",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Location": content_location}
    resp.url = "https://web.archive.org" + content_location
    return resp


class TestArchiveHttp:
    def test_happy_path_returns_wayback_url(self):
        with patch("precis.web_archive._httpx") as mock_httpx:
            mock_httpx.get.return_value = _success_response()
            mock_httpx.TimeoutException = type("TimeoutException", (Exception,), {})
            mock_httpx.HTTPError = Exception
            result = archive_url("https://example.com/page", requested=True)

        assert result.ok
        assert result.wayback_url is not None
        assert result.wayback_url.startswith("https://web.archive.org/web/")
        assert result.skipped_reason is None

    def test_http_error_recorded(self):
        with patch("precis.web_archive._httpx") as mock_httpx:
            err_resp = MagicMock()
            err_resp.status_code = 429
            err_resp.headers = {}
            err_resp.url = ""
            mock_httpx.get.return_value = err_resp
            mock_httpx.TimeoutException = type("TimeoutException", (Exception,), {})
            mock_httpx.HTTPError = Exception
            result = archive_url("https://example.com/page", requested=True)

        assert not result.ok
        assert result.skipped_reason == SkipReason.HTTP_ERROR
        assert "429" in result.detail

    def test_timeout_is_skipped_not_raised(self):
        # Network failures must NEVER propagate — the bookmark write
        # has to succeed regardless.
        class FakeTimeout(Exception):
            pass

        with patch("precis.web_archive._httpx") as mock_httpx:
            mock_httpx.TimeoutException = FakeTimeout
            mock_httpx.HTTPError = Exception
            mock_httpx.get.side_effect = FakeTimeout("server too slow")
            result = archive_url("https://example.com/page", requested=True)

        assert not result.ok
        assert result.skipped_reason == SkipReason.NETWORK_ERROR
        assert "timeout" in result.detail.lower() or "too slow" in result.detail

    def test_missing_httpx_recorded_not_raised(self):
        # Simulate the [external] extra not being installed: the
        # module-level ``_httpx`` sentinel is None when httpx isn't
        # importable.  Patch that sentinel and verify the skip path.
        with patch("precis.web_archive._httpx", None):
            result = archive_url("https://example.com/page", requested=True)

        assert not result.ok
        assert result.skipped_reason == SkipReason.HTTPX_MISSING


# ---------------------------------------------------------------------------
# _extract_wayback_url
# ---------------------------------------------------------------------------


class TestExtractWaybackUrl:
    def test_content_location_relative(self):
        resp = MagicMock()
        resp.headers = {"Content-Location": "/web/20260424/https://example.com/"}
        resp.url = ""
        assert (
            _extract_wayback_url(resp, "https://example.com/")
            == "https://web.archive.org/web/20260424/https://example.com/"
        )

    def test_content_location_absolute(self):
        resp = MagicMock()
        resp.headers = {
            "Content-Location": (
                "https://web.archive.org/web/20260424/https://example.com/"
            )
        }
        resp.url = ""
        assert _extract_wayback_url(resp, "https://example.com/").startswith(
            "https://web.archive.org/web/"
        )

    def test_falls_back_to_resp_url(self):
        resp = MagicMock()
        resp.headers = {}
        resp.url = "https://web.archive.org/web/20260424/https://example.com/"
        assert _extract_wayback_url(resp, "https://example.com/")

    def test_none_when_no_wayback_path(self):
        resp = MagicMock()
        resp.headers = {}
        resp.url = "https://example.com/redirected"
        assert _extract_wayback_url(resp, "https://example.com/") is None


# ---------------------------------------------------------------------------
# ArchiveResult contract
# ---------------------------------------------------------------------------


class TestArchiveResult:
    def test_ok_property_true_when_url_set(self):
        r = ArchiveResult(wayback_url="https://web.archive.org/web/x")
        assert r.ok

    def test_ok_property_false_when_skipped(self):
        r = ArchiveResult(skipped_reason=SkipReason.USER_OPTOUT)
        assert not r.ok

    def test_ok_property_false_when_nothing_set(self):
        assert not ArchiveResult().ok


# ---------------------------------------------------------------------------
# Token bucket — exercised directly
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_acquires_up_to_cap(self):
        from precis.web_archive import _MAX_CALLS

        for _ in range(_MAX_CALLS):
            assert _BUCKET.try_acquire()
        assert not _BUCKET.try_acquire()

    def test_reset_clears_window(self):
        _BUCKET.try_acquire()
        _BUCKET.try_acquire()
        _BUCKET.reset()
        # After reset we should be able to acquire the full budget again.
        from precis.web_archive import _MAX_CALLS

        for _ in range(_MAX_CALLS):
            assert _BUCKET.try_acquire()
