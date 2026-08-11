"""Direct tests for the SSRF guard (:mod:`precis.utils.safe_fetch`).

Two layers:

* **Classification** — :func:`resolve_pinned_ip` / :func:`assert_public_http_url`
  against literal IPs and (mocked-DNS) hostnames: private / loopback /
  link-local / metadata / reserved ranges are refused; a public host
  returns the IP the request would be pinned to.
* **Connect-layer pinning (the DNS-rebinding TOCTOU closure)** — against a
  real loopback HTTP server: the request's URL keeps its *hostname* while
  the client's :func:`pinning_transport` backend classifies-and-pins the
  DNS at ``connect_tcp`` (one resolution, dialed directly). Because the URL
  host stays the hostname, httpcore's connection-pool key + TLS
  ``server_hostname`` stay per-hostname (gr180122 — no cross-host reuse).
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
from typing import Any

import httpx
import pytest

from precis.utils import safe_fetch as sf
from precis.utils.http import http_client
from precis.utils.safe_fetch import (
    SsrfBlocked,
    assert_public_http_url,
    pinning_transport,
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
    with http_client(timeout=5.0, user_agent=None) as client:
        resp = safe_get(client, f"http://pinned.test:{server.port}/final")
    assert resp.status_code == 200 and resp.text == "BODY"
    path, host_hdr, peer = server.log[-1]
    assert peer == "127.0.0.1"  # backend dialed the validated IP literal
    assert host_hdr == f"pinned.test:{server.port}"  # hostname preserved
    assert calls["n"] == 1  # resolved once, at connect — no re-resolution


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
    with http_client(timeout=5.0, user_agent=None) as client:
        resp = safe_get(client, f"http://pinned.test:{server.port}/redir")
    assert resp.text == "BODY"
    # Both the redirect hop and the followed /final hop kept the hostname
    # (a relative Location resolves against the hostname URL) and the
    # backend dialed loopback on each.
    assert [p for p, _, _ in server.log] == ["/redir", "/final"]
    assert all(h == f"pinned.test:{server.port}" for _, h, _ in server.log)
    assert all(peer == "127.0.0.1" for *_, peer in server.log)


def test_safe_stream_pins_to_validated_ip(
    server: Any, allow_loopback: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_getaddrinfo(monkeypatch, "pinned.test", "127.0.0.1")
    with http_client(timeout=5.0, user_agent=None) as client:
        with safe_stream(client, "GET", f"http://pinned.test:{server.port}/final") as r:
            body = r.read()
    assert body == b"BODY"
    _path, host_hdr, peer = server.log[-1]
    assert peer == "127.0.0.1"
    assert host_hdr == f"pinned.test:{server.port}"
    assert calls["n"] == 1


class _ThreadedKeepAliveServer:
    """HTTP/1.1 keep-alive loopback server (one thread per connection) that
    302-redirects ``/redir`` on one host to ``/final`` on a *different*
    host. Needed for the gr180122 pool-key regression: keep-alive lets a
    connection be reused, and threading avoids the single-thread deadlock a
    kept-alive connection would otherwise cause."""

    def __init__(self, redirect_host: str) -> None:
        self.log: list[tuple[str, str | None]] = []
        log = self.log

        class _H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                log.append((self.path, self.headers.get("Host")))
                if self.path == "/redir":
                    server_address = self.server.server_address
                    assert isinstance(server_address, tuple)
                    port = server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://{redirect_host}:{port}/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"BODY"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: Any) -> None:
                pass

        class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._srv = _Srv(("127.0.0.1", 0), _H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._srv.shutdown()


def test_no_cross_hostname_connection_reuse(
    allow_loopback: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr180122 end-to-end. A redirect chain across two *different*
    hostnames that both resolve to one IP must open **two** connections,
    keyed per-hostname — proving the pool key was NOT collapsed to the
    shared IP (which would let hop-2 reuse hop-1's mis-verified TLS conn).
    The earlier URL-rewrite design would key both hops to ``127.0.0.1`` and
    reuse a single connection."""
    srv = _ThreadedKeepAliveServer(redirect_host="hostb.test")
    real = socket.getaddrinfo

    def _gai(h: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        if h in ("hosta.test", "hostb.test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real(h, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    try:
        with http_client(timeout=5.0, user_agent=None) as client:
            resp = safe_get(client, f"http://hosta.test:{srv.port}/redir")
            assert resp.text == "BODY"
            # httpcore keys the pool (and TLS server_hostname) off the URL
            # host — which stays the hostname, so the two hops land on two
            # distinct connections, one per hostname.
            pool = client._transport._pool  # type: ignore[attr-defined]
            origins = sorted(conn._origin.host.decode() for conn in pool.connections)
        assert origins == ["hosta.test", "hostb.test"]
    finally:
        srv.stop()
    # Each hop carried its own Host, never the other's.
    assert [p for p, _ in srv.log] == ["/redir", "/final"]
    assert srv.log[0][1] == f"hosta.test:{srv.port}"
    assert srv.log[1][1] == f"hostb.test:{srv.port}"


# ── connect-layer design: URL keeps its hostname (gr180122) ──────────


def test_pinned_request_keeps_hostname_no_dns() -> None:
    """gr180122 regression. ``_pinned_request`` must NOT rewrite the URL to
    the IP (which would collapse httpcore's pool key to ``(scheme, IP,
    port)`` and let two hostnames sharing one IP reuse a mis-verified TLS
    connection). The URL host stays the hostname; no ``sni_hostname``
    rewrite; and it does no DNS itself (classify+pin happens at connect)."""
    with httpx.Client(follow_redirects=False) as client:
        req = sf._pinned_request(client, "GET", "https://api.test/v1", {})
    # Hostname preserved in the URL → per-hostname pool key + TLS server name.
    assert req.url.host == "api.test"
    assert req.headers["Host"] == "api.test"
    assert "sni_hostname" not in req.extensions


def test_pinned_request_shape_gate_still_fires() -> None:
    """The DNS-free syntactic gate (scheme + host) still rejects up front."""
    with httpx.Client(follow_redirects=False) as client:
        for bad in ("ftp://api.test/x", "file:///etc/passwd", "https:///nohost"):
            with pytest.raises(SsrfBlocked):
                sf._pinned_request(client, "GET", bad, {})


def test_pinning_backend_installed_on_factory_clients() -> None:
    """http_client() and pinning_transport() must carry the pinning backend
    — the injection reaches into httpcore private attrs, so guard it: if
    httpcore renames ``_pool``/``_network_backend`` this fails loudly."""
    backend_cls = sf._pinning_backend_class()
    with http_client(timeout=1.0) as client:
        backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
        assert isinstance(backend, backend_cls)
    transport = pinning_transport()
    assert isinstance(transport._pool._network_backend, backend_cls)


def test_safe_get_refuses_unguarded_client() -> None:
    """A plain client (no pinning backend) would fetch agent URLs
    unguarded — safe_get/safe_stream must fail closed, not silently."""
    with httpx.Client(follow_redirects=False) as plain:
        with pytest.raises(SsrfBlocked, match="unguarded client"):
            safe_get(plain, "http://example.com/")
        with pytest.raises(SsrfBlocked, match="unguarded client"):
            with safe_stream(plain, "GET", "http://example.com/"):
                pass


def test_pinning_backend_blocks_private_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the backend classifies at connect, so a host that
    resolves to a private address is refused when the connection opens
    (no pre-flight resolve needed) — SsrfBlocked propagates unwrapped."""
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"rebind.evil": ["127.0.0.1"]})
    )
    with http_client(timeout=5.0) as client:
        with pytest.raises(SsrfBlocked, match="127.0.0.1"):
            safe_get(client, "http://rebind.evil/")
