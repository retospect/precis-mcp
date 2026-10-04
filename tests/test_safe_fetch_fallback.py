"""Multi-address connect fallback in the pinning backend (``safe_fetch``).

Claims (see ``_pinning_backend_class``): one predicate checks every address
before any dial (1); one resolution, no re-resolve (2); fallback only on
connect / TLS-handshake failure (3, C1 cert failures never fall back, C2 same
ssl_context + server_hostname, C3 no wrapper survives, nothing leaks); redirect
hops re-check (4); one ``2T`` budget (5); a single address is the plain dial
(6); visibility (C4).

``httpcore.SyncBackend.connect_tcp`` is replaced with a scripted fake, so no
socket is ever opened.
"""

from __future__ import annotations

import datetime
import logging
import socket
import ssl
import tempfile
import threading
from typing import Any

import httpcore
import httpx
import pytest

from precis.utils import safe_fetch as sf
from precis.utils.safe_fetch import SsrfBlocked

PUB_A = "93.184.216.34"
PUB_B = "93.184.216.35"
PUB_C = "93.184.216.36"
T = 10.0


def _cert_error() -> httpcore.ConnectError:
    """What httpcore raises for a failed cert verification in start_tls."""
    try:
        raise ssl.SSLCertVerificationError("certificate verify failed")
    except ssl.SSLError as exc:
        err = httpcore.ConnectError("cert")
        err.__cause__ = exc
        return err


def _caused(cls: type[Exception], cause: BaseException) -> Any:
    err = cls("wrapped")
    err.__cause__ = cause
    return err


class FakeStream:
    """Scripted stand-in for httpcore's SyncStream."""

    def __init__(
        self,
        addr: str,
        events: list[tuple[str, str]],
        *,
        tls_error: Exception | None = None,
        tls_advance: float = 0.0,
        clock: Clock | None = None,
        body: bytes = b"",
    ) -> None:
        self.addr = addr
        self.events = events
        self.tls_error = tls_error
        self.tls_advance = tls_advance
        self.clock = clock
        self.tls_calls: list[dict[str, Any]] = []
        self.closed = False
        self.tls_result: FakeStream | None = None
        self._body = body
        self.read_error: Exception | None = None

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.tls_calls.append(
            {"ctx": ssl_context, "sni": server_hostname, "timeout": timeout}
        )
        self.events.append(("tls", self.addr))
        if self.clock is not None:
            self.clock.t += self.tls_advance
        if self.tls_error is not None:
            raise self.tls_error
        self.tls_result = FakeStream(self.addr, self.events)
        return self.tls_result

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        data, self._body = self._body, b""
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.events.append(("close", self.addr))

    def get_extra_info(self, info: str) -> Any:
        return None


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Net:
    """Scripted ``SyncBackend.connect_tcp``: per-address stream or error."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.dials: list[dict[str, Any]] = []
        self.script: dict[str, Any] = {}
        self.streams: dict[str, FakeStream] = {}
        self.clock: Clock | None = None
        self.connect_advance: dict[str, float] = {}

    def stream(self, addr: str, **kw: Any) -> FakeStream:
        s = FakeStream(addr, self.events, clock=self.clock, **kw)
        self.script[addr] = s
        self.streams[addr] = s
        return s

    def fail(self, addr: str, exc: Exception) -> None:
        self.script[addr] = exc

    def connect_tcp(
        self,
        _backend: Any,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self.dials.append(
            {
                "host": host,
                "port": port,
                "timeout": timeout,
                "local_address": local_address,
                "socket_options": socket_options,
            }
        )
        self.events.append(("connect", host))
        if self.clock is not None:
            self.clock.t += self.connect_advance.get(host, 0.0)
        item = self.script[host]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    n = Net()

    def _connect(self: Any, *a: Any, **k: Any) -> Any:
        return n.connect_tcp(self, *a, **k)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", _connect)
    return n


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, net: Net) -> Clock:
    c = Clock()
    net.clock = c
    monkeypatch.setattr(sf, "_monotonic", c)
    return c


class Resolver:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, host: str, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
        self.calls.append(host)
        if len(self.calls) > 1:
            # A second answer would be the rebinding shape: private.
            ips = ["127.0.0.1"]
        else:
            ips = self.mapping[host]
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


def resolve(monkeypatch: pytest.MonkeyPatch, host: str, ips: list[str]) -> Resolver:
    r = Resolver({host: ips})
    monkeypatch.setattr(socket, "getaddrinfo", r)
    return r


def backend() -> Any:
    return sf._pinning_backend_class()()


def refused() -> httpcore.ConnectError:
    return _caused(httpcore.ConnectError, ConnectionRefusedError("refused"))


# -- claim 1 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "ips",
    [
        [PUB_A, "10.0.0.9"],
        ["10.0.0.9", PUB_A],
        [PUB_A, "::ffff:10.0.0.1"],
        ["::ffff:10.0.0.1", PUB_A],
        [PUB_A, "169.254.169.254"],
        ["169.254.169.254", PUB_A],
    ],
)
def test_claim1_any_blocked_address_refuses_before_any_dial(
    monkeypatch: pytest.MonkeyPatch, net: Net, ips: list[str]
) -> None:
    resolve(monkeypatch, "mixed.test", ips)
    with pytest.raises(SsrfBlocked):
        backend().connect_tcp("mixed.test", 443, timeout=T)
    assert net.dials == []


def test_claim1_all_returns_deduped_list_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve(monkeypatch, "h.test", [PUB_B, PUB_A, PUB_B])
    assert sf._classify_and_pin_all("h.test") == [PUB_B, PUB_A]
    # _classify_and_pin_host is addrs[0] of it (fresh resolver: first call).
    resolve(monkeypatch, "h.test", [PUB_B, PUB_A])
    assert sf._classify_and_pin_host("h.test") == PUB_B


def test_claim1_ip_literal_is_none() -> None:
    assert sf._classify_and_pin_all(PUB_A) is None
    assert sf._classify_and_pin_host(PUB_A) is None
    with pytest.raises(SsrfBlocked):
        sf._classify_and_pin_all("10.0.0.1")


# -- claim 2 ---------------------------------------------------------------


def test_claim2_one_resolution_for_three_attempts(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    r = resolve(monkeypatch, "h.test", [PUB_A, PUB_B, PUB_C])
    net.fail(PUB_A, refused())
    net.fail(PUB_B, refused())
    s = net.stream(PUB_C)
    out = backend().connect_tcp("h.test", 80, timeout=T)
    assert [d["host"] for d in net.dials] == [PUB_A, PUB_B, PUB_C]
    assert r.calls == ["h.test"]
    assert out.get_extra_info("x") is None and out._inner is s


# -- claim 3 / C1 / C2 / C3 -----------------------------------------------


def test_claim3_tls_failure_falls_back_to_next_address(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("handshake timed out"))
    b = net.stream(PUB_B)
    ctx = ssl.create_default_context()
    out = backend().connect_tcp("h.test", 443, timeout=T).start_tls(ctx, "h.test", T)
    assert out is b.tls_result
    assert [d["host"] for d in net.dials] == [PUB_A, PUB_B]
    assert a.closed


def test_claim3_failure_after_tls_success_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A)
    net.stream(PUB_B)
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    tls = fs.start_tls(ssl.create_default_context(), "h.test", T)
    tls.read_error = httpcore.ReadError("reset mid-response")
    with pytest.raises(httpcore.ReadError):
        tls.read(10)
    assert len(net.dials) == 1
    assert a.tls_result is tls


def test_claim3_http_5xx_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(
        PUB_A,
        body=b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    )
    net.stream(PUB_B)
    with httpx.Client(transport=sf.pinning_transport()) as client:
        resp = client.get("http://h.test/")
    assert resp.status_code == 503
    assert [d["host"] for d in net.dials] == [PUB_A]


def test_claim3_plain_http_falls_back_only_on_tcp_connect_failure(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.fail(PUB_A, refused())
    net.stream(
        PUB_B,
        body=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    )
    with httpx.Client(transport=sf.pinning_transport()) as client:
        resp = client.get("http://h.test/")
    assert resp.status_code == 200
    assert [d["host"] for d in net.dials] == [PUB_A, PUB_B]
    assert all(e[0] != "tls" for e in net.events)


def test_c1_cert_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=_cert_error())
    net.stream(PUB_B)
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectError):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert len(net.dials) == 1


def test_c1_cert_error_in_context_chain_also_blocks_fallback(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    # Implicit chaining (__context__) rather than ``from``.
    try:
        try:
            raise ssl.SSLCertVerificationError("bad")
        except ssl.SSLError:
            raise httpcore.ConnectError("x") from None
    except httpcore.ConnectError as err:
        net.stream(PUB_A, tls_error=err)
    net.stream(PUB_B)
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectError):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert len(net.dials) == 1


@pytest.mark.parametrize(
    "err",
    [
        httpcore.ConnectTimeout("handshake timed out"),
        _caused(httpcore.ConnectError, ConnectionResetError("reset")),
        _caused(httpcore.ConnectError, OSError(113, "no route to host")),
        _caused(httpcore.ConnectError, ssl.SSLError("handshake failure")),
    ],
)
def test_c1_connection_level_failures_fall_back(
    monkeypatch: pytest.MonkeyPatch, net: Net, err: Exception
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=err)
    net.stream(PUB_B)
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert len(net.dials) == 2


def test_c1_non_connect_errors_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.fail(PUB_A, RuntimeError("bug"))
    net.stream(PUB_B)
    with pytest.raises(RuntimeError):
        backend().connect_tcp("h.test", 443, timeout=T)
    assert len(net.dials) == 1


def test_c2_same_ssl_context_and_server_hostname_every_attempt(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B, PUB_C])
    a = net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"))
    b = net.stream(PUB_B, tls_error=_caused(httpcore.ConnectError, OSError("rst")))
    c = net.stream(PUB_C)
    ctx = ssl.create_default_context()
    backend().connect_tcp("h.test", 443, timeout=T).start_tls(ctx, "h.test", T)
    for s in (a, b, c):
        (call,) = s.tls_calls
        assert call["ctx"] is ctx
        assert call["sni"] == "h.test"  # the name, never an IP


def test_c2_second_address_with_wrong_cert_fails_not_accepted(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"))
    net.stream(PUB_B, tls_error=_cert_error())  # cert for another name
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectError) as ei:
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert isinstance(ei.value.__cause__, ssl.SSLCertVerificationError)
    assert len(net.dials) == 2  # no third attempt, no acceptance


def test_c3_no_wrapper_survives_and_abandoned_streams_closed_before_next_dial(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B, PUB_C])
    a = net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"))
    b = net.stream(PUB_B, tls_error=httpcore.ConnectTimeout("t"))
    c = net.stream(PUB_C)
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    out = fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert not isinstance(out, sf._FallbackStream)
    assert out is c.tls_result
    assert a.closed and b.closed
    ev = net.events
    assert ev.index(("close", PUB_A)) < ev.index(("connect", PUB_B))
    assert ev.index(("close", PUB_B)) < ev.index(("connect", PUB_C))


def test_c3_tcp_connect_failure_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.fail(PUB_A, refused())
    net.fail(PUB_B, refused())
    with pytest.raises(httpcore.ConnectError):
        backend().connect_tcp("h.test", 443, timeout=T)
    assert len(net.dials) == 2


def test_c3_all_tls_attempts_fail_closes_every_stream(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"))
    b = net.stream(PUB_B, tls_error=httpcore.ConnectTimeout("t"))
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectTimeout):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert a.closed and b.closed


# -- claim 4 ---------------------------------------------------------------


def test_claim4_redirect_hop_to_private_host_refused(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    r = Resolver({"pub.test": [PUB_A, PUB_B], "priv.test": ["10.0.0.9"]})

    def gai(host: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        # Each connection resolves fresh; Resolver's 2nd-call trap is per
        # host here, so use a plain lookup.
        r.calls.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in r.mapping[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    net.stream(
        PUB_A,
        body=b"HTTP/1.1 302 Found\r\nLocation: http://priv.test/\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n",
    )
    with httpx.Client(transport=sf.pinning_transport()) as client:
        with pytest.raises(SsrfBlocked, match="10.0.0.9"):
            sf.safe_get(client, "http://pub.test/")
    assert [d["host"] for d in net.dials] == [PUB_A]
    assert r.calls == ["pub.test", "priv.test"]


# -- claim 5 ---------------------------------------------------------------


def test_claim5_first_attempt_uses_exactly_t(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A)
    backend().connect_tcp("h.test", 443, timeout=T).start_tls(
        ssl.create_default_context(), "h.test", T
    )
    assert net.dials[0]["timeout"] == T
    assert a.tls_calls[0]["timeout"] == T


def test_claim5_stalled_tls_leaves_second_address_budget_t(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"), tls_advance=T)
    b = net.stream(PUB_B)
    backend().connect_tcp("h.test", 443, timeout=T).start_tls(
        ssl.create_default_context(), "h.test", T
    )
    assert net.dials[1]["timeout"] == T
    assert b.tls_calls[0]["timeout"] == T


def test_claim5_three_stalled_addresses_raise_by_2t(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B, PUB_C])
    for ip in (PUB_A, PUB_B, PUB_C):
        net.stream(ip, tls_error=httpcore.ConnectTimeout("t"), tls_advance=T)
    start = clock.t
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectTimeout):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert clock.t - start <= 2 * T
    assert [d["host"] for d in net.dials] == [PUB_A, PUB_B]  # third never dialed


def test_claim5_tls_phase_still_gets_t_after_a_7s_tcp_phase(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A)
    clock.t += 0  # connect itself instantaneous in the fake
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    clock.t += 7.0  # TCP phase "took" 7s
    fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert a.tls_calls[0]["timeout"] == T  # D-now = 13 > T: still T


def test_claim5_timeout_none_means_no_fallback(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A)
    net.stream(PUB_B)
    out = backend().connect_tcp("h.test", 443, timeout=None)
    assert out is a
    assert [d["host"] for d in net.dials] == [PUB_A]
    assert net.dials[0]["timeout"] is None


# -- claim 6 ---------------------------------------------------------------


def test_claim6_single_address_is_plain_dial_no_wrapper(
    monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A])
    a = net.stream(PUB_A)
    opts = [(1, 2, 3)]
    out = backend().connect_tcp(
        "h.test", 8443, timeout=T, local_address="0.0.0.0", socket_options=opts
    )
    assert out is a
    assert not isinstance(out, sf._FallbackStream)
    assert net.dials == [
        {
            "host": PUB_A,
            "port": 8443,
            "timeout": T,
            "local_address": "0.0.0.0",
            "socket_options": opts,
        }
    ]


def test_claim6_ip_literal_dials_as_is(net: Net) -> None:
    a = net.stream(PUB_A)
    out = backend().connect_tcp(PUB_A, 80, timeout=T)
    assert out is a
    assert [d["host"] for d in net.dials] == [PUB_A]


# -- C4 --------------------------------------------------------------------


def test_c4_fallback_is_counted_and_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    net: Net,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolve(monkeypatch, "count.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"))
    net.stream(PUB_B)
    before = sf.fallback_counts().get("count.test", 0)
    with caplog.at_level(logging.WARNING, logger=sf.logger.name):
        backend().connect_tcp("count.test", 443, timeout=T).start_tls(
            ssl.create_default_context(), "count.test", T
        )
    assert sf.fallback_counts()["count.test"] == before + 1
    recs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert "count.test" in msg and "ConnectTimeout" in msg and "attempt=1" in msg


def test_c4_no_fallback_no_count(monkeypatch: pytest.MonkeyPatch, net: Net) -> None:
    resolve(monkeypatch, "quiet.test", [PUB_A, PUB_B])
    net.stream(PUB_A)
    backend().connect_tcp("quiet.test", 443, timeout=T).start_tls(
        ssl.create_default_context(), "quiet.test", T
    )
    assert "quiet.test" not in sf.fallback_counts()


# -- budget exhaustion, counter cap, real TLS ---------------------------------


def test_claim5_stream_dialed_at_the_deadline_is_closed(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    net.stream(PUB_A, tls_error=httpcore.ConnectTimeout("t"), tls_advance=T - 1)
    b = net.stream(PUB_B)
    net.connect_advance[PUB_B] = T + 1  # B connects exactly at D = start + 2T
    start = clock.t
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectTimeout):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert b.closed
    assert b.tls_calls == []
    assert clock.t - start <= 2 * T


def test_claim5_tcp_returning_at_deadline_raises_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, net: Net, clock: Clock
) -> None:
    resolve(monkeypatch, "h.test", [PUB_A, PUB_B])
    a = net.stream(PUB_A)
    net.connect_advance[PUB_A] = 2 * T
    start = clock.t
    fs = backend().connect_tcp("h.test", 443, timeout=T)
    with pytest.raises(httpcore.ConnectTimeout):
        fs.start_tls(ssl.create_default_context(), "h.test", T)
    assert a.tls_calls == []
    assert a.closed
    assert clock.t - start <= 2 * T


def test_c4_counter_is_capped_and_folds_new_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sf, "_FALLBACK_COUNTS", sf.Counter())
    monkeypatch.setattr(sf, "_MAX_COUNTED_HOSTS", 2)
    err = httpcore.ConnectTimeout("t")
    for host in ("a.test", "b.test", "c.test", "d.test", "a.test"):
        sf._record_fallback(host, 1, err)
    assert sf.fallback_counts() == {"a.test": 2, "b.test": 1, sf._OTHER_HOSTS: 2}


def _self_signed_pem(cn: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_c1_real_httpcore_cert_failure_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real ``SyncStream.start_tls`` against a local self-signed TLS server.

    The classifier is patched (only here) to return two loopback addresses,
    because the real predicate blocks loopback; everything after that is the
    real backend and real httpcore, with only a dial spy around the real
    ``SyncBackend.connect_tcp``.
    """
    cert_pem, key_pem = _self_signed_pem("h.test")
    with tempfile.TemporaryDirectory() as d:
        cert_path, key_path = f"{d}/c.pem", f"{d}/k.pem"
        with open(cert_path, "wb") as f:
            f.write(cert_pem)
        with open(key_path, "wb") as f:
            f.write(key_pem)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_path, key_path)

    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    port = lsock.getsockname()[1]

    def serve() -> None:
        lsock.settimeout(5)
        try:
            while True:
                conn, _ = lsock.accept()
                try:
                    server_ctx.wrap_socket(conn, server_side=True).close()
                except OSError:
                    conn.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()

    monkeypatch.setattr(
        sf, "_classify_and_pin_all", lambda host: ["127.0.0.1", "127.0.0.1"]
    )
    orig = httpcore.SyncBackend.connect_tcp
    dials: list[str] = []

    def spy(self: Any, host: str, *a: Any, **k: Any) -> Any:
        dials.append(host)
        return orig(self, host, *a, **k)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", spy)
    try:
        fs = backend().connect_tcp("h.test", port, timeout=5.0)
        with pytest.raises(httpcore.ConnectError) as ei:
            fs.start_tls(ssl.create_default_context(), "h.test", 5.0)
    finally:
        lsock.close()
    assert isinstance(fs, sf._FallbackStream)
    assert sf._wraps_cert_failure(ei.value)
    assert len(dials) == 1
