"""Direct tests for the SSRF guard (:mod:`precis.utils.safe_fetch`).

Two layers:

* **Classification** — :func:`resolve_pinned_ip` / :func:`assert_public_http_url`
  against literal IPs and (mocked-DNS) hostnames: private / loopback /
  link-local / metadata / reserved ranges are refused; a public host
  returns the IP the request will be pinned to.
* **Pinned connect (the DNS-rebinding TOCTOU closure)** — against a real
  loopback HTTP server: the request dials the *validated IP literal*
  (one DNS resolution, reused as the connect target) while the original
  hostname rides the ``Host`` header + TLS ``sni_hostname`` extension.
"""

from __future__ import annotations

import http.server
import socket
import threading
from typing import Any

import httpx
import pytest

from precis.utils import safe_fetch as sf
from precis.utils.safe_fetch import (
    SsrfBlocked,
    assert_public_http_url,
    resolve_pinned_ip,
    safe_get,
    safe_stream,
)

# ── classification: literal IPs (no DNS) ─────────────────────────────

BLOCKED_LITERALS = [
    "http://127.0.0.1/",  # loopback
    "http://169.254.169.254/",  # cloud metadata
    "http://10.0.0.5/",  # RFC1918
    "http://192.168.1.1/",  # RFC1918
    "http://172.16.0.1/",  # RFC1918
    "http://0.0.0.0/",  # "this" network
    "http://100.64.0.1/",  # CGNAT
    "http://[::1]/",  # IPv6 loopback
    "http://[fe80::1]/",  # IPv6 link-local
    "http://[fc00::1]/",  # IPv6 ULA
    "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 → private v4
]


@pytest.mark.parametrize("url", BLOCKED_LITERALS)
def test_blocked_literal_ip_refused(url: str) -> None:
    with pytest.raises(SsrfBlocked):
        resolve_pinned_ip(url)


def test_public_literal_ip_returns_none() -> None:
    # A literal public IP can't be re-pointed mid-connect → dial as-is.
    assert resolve_pinned_ip("http://93.184.216.34/") is None
    assert resolve_pinned_ip("http://[2606:2800:220:1:248:1893:25c8:1946]/") is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://example.com/",
        "https:///no-host",
    ],
)
def test_bad_scheme_or_missing_host_refused(url: str) -> None:
    with pytest.raises(SsrfBlocked):
        resolve_pinned_ip(url)


# ── classification: hostnames (mocked DNS) ───────────────────────────


def _fake_getaddrinfo(mapping: dict[str, list[str]]) -> Any:
    def _gai(host: str, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
        ips = mapping.get(host)
        if ips is None:
            raise OSError(f"no fake record for {host!r}")
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 0),
            )
            for ip in ips
        ]

    return _gai


def test_host_resolving_public_returns_pinned_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"good.test": ["93.184.216.34"]})
    )
    assert resolve_pinned_ip("http://good.test/path") == "93.184.216.34"


def test_host_resolving_private_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rebinding shape: attacker's host answers with a loopback/private A.
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"rebind.evil": ["127.0.0.1"]})
    )
    with pytest.raises(SsrfBlocked, match="127.0.0.1"):
        resolve_pinned_ip("http://rebind.evil/")


def test_mixed_public_and_private_records_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strict: a host mixing a public and a private record is refused
    # wholesale (an attacker can't smuggle a private target past us by
    # padding the record set with a public IP).
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"mixed.test": ["93.184.216.34", "10.0.0.9"]}),
    )
    with pytest.raises(SsrfBlocked):
        resolve_pinned_ip("http://mixed.test/")


def test_dns_failure_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    with pytest.raises(SsrfBlocked, match="DNS lookup failed"):
        resolve_pinned_ip("http://nope.test/")


def test_assert_public_http_url_is_validate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"good.test": ["93.184.216.34"]})
    )
    assert_public_http_url("http://good.test/")  # no raise = valid
    with pytest.raises(SsrfBlocked):
        assert_public_http_url("http://127.0.0.1/")


# ── pinned connect against a real loopback server ────────────────────


class _RecordingServer:
    """Loopback HTTP server that records (path, Host, peer-ip) per request
    and can emit one 302 redirect (path ``/redir`` → ``/final``)."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str | None, str]] = []
        log = self.log

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                peer = self.connection.getpeername()[0]
                log.append((self.path, self.headers.get("Host"), peer))
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"BODY")

            def log_message(self, *_a: Any) -> None:  # silence
                pass

        self._srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._srv.shutdown()


@pytest.fixture
def server() -> Any:
    s = _RecordingServer()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the pin-mechanics tests only: let the classifier accept the
    loopback IP so we can pin to the local test server. The blocking
    logic itself is covered by the classification tests above."""
    monkeypatch.setattr(sf, "_ip_blocked", lambda _ip: False)


def _count_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, host: str, ip: str
) -> dict[str, int]:
    """Map ``host`` → ``ip`` and count how many times DNS is consulted.

    A poison value is returned on any call after the first so that a
    *second* resolution (a re-resolve at connect time) would dial an
    unroutable address and fail — the pinned path must resolve exactly
    once and reuse the result.
    """
    calls = {"n": 0}
    real = socket.getaddrinfo

    def _gai(h: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        if h == host:
            calls["n"] += 1
            served = ip if calls["n"] == 1 else "203.0.113.9"  # TEST-NET poison
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (served, 0))]
        return real(h, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    return calls


def test_safe_get_pins_to_validated_ip(
    server: Any, allow_loopback: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_getaddrinfo(monkeypatch, "pinned.test", "127.0.0.1")
    with httpx.Client(follow_redirects=False, timeout=5) as client:
        resp = safe_get(client, f"http://pinned.test:{server.port}/final")
    assert resp.status_code == 200 and resp.text == "BODY"
    path, host_hdr, peer = server.log[-1]
    assert peer == "127.0.0.1"  # dialed the validated IP literal
    assert host_hdr == f"pinned.test:{server.port}"  # hostname preserved
    assert calls["n"] == 1  # resolved once — no connect-time re-resolution


def test_safe_get_preserves_host_across_pinned_redirect(
    server: Any, allow_loopback: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stable mapping (no poison): each redirect hop legitimately
    # resolves-and-pins the hostname again. Pass through for other hosts
    # (httpcore re-resolves the pinned 127.0.0.1 literal to open the socket).
    real = socket.getaddrinfo

    def _gai(h: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        if h == "pinned.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real(h, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    with httpx.Client(follow_redirects=False, timeout=5) as client:
        resp = safe_get(client, f"http://pinned.test:{server.port}/redir")
    assert resp.text == "BODY"
    # Both the redirect hop and the followed /final hop kept the hostname
    # (a relative Location must resolve against the hostname URL, not the
    # rewritten IP URL) and dialed loopback.
    assert [p for p, _, _ in server.log] == ["/redir", "/final"]
    assert all(h == f"pinned.test:{server.port}" for _, h, _ in server.log)
    assert all(peer == "127.0.0.1" for *_, peer in server.log)


def test_safe_stream_pins_to_validated_ip(
    server: Any, allow_loopback: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_getaddrinfo(monkeypatch, "pinned.test", "127.0.0.1")
    with httpx.Client(follow_redirects=False, timeout=5) as client:
        with safe_stream(client, "GET", f"http://pinned.test:{server.port}/final") as r:
            body = r.read()
    assert body == b"BODY"
    _path, host_hdr, peer = server.log[-1]
    assert peer == "127.0.0.1"
    assert host_hdr == f"pinned.test:{server.port}"
    assert calls["n"] == 1


def test_pinned_request_sets_host_and_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit check of the rewrite: URL host → validated IP; original host
    preserved as Host header + ``sni_hostname`` extension (so TLS SNI and
    cert verification still target the real hostname)."""
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"api.test": ["93.184.216.34"]})
    )
    with httpx.Client(follow_redirects=False) as client:
        req = sf._pinned_request(client, "GET", "https://api.test/v1", {})
    assert req.url.host == "93.184.216.34"
    assert req.headers["Host"] == "api.test"
    assert req.extensions.get("sni_hostname") == "api.test"


def test_pinned_request_leaves_literal_ip_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with httpx.Client(follow_redirects=False) as client:
        req = sf._pinned_request(client, "GET", "http://93.184.216.34/x", {})
    assert req.url.host == "93.184.216.34"
    assert "sni_hostname" not in req.extensions
