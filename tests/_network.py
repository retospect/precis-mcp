"""Deny outbound Python sockets while retaining local test servers and IPC."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict


def _test_db_host() -> str | None:
    """Hostname of the disposable test DB — outside this guard by design.

    ``docs/conventions/testing.md`` carves the isolated PostgreSQL test
    database out of the Python guard (native-libpq connections only).
    psycopg3 nonetheless resolves a *named* host through Python
    ``socket.getaddrinfo`` in its per-host attempt splitter, so the
    compose service name (``precis-test-db``) needs an explicit pass.
    """
    dsn = os.environ.get("PRECIS_TEST_PG_URL", "")
    if not dsn:
        return None
    try:
        host = conninfo_to_dict(dsn).get("host")
    except Exception:  # pragma: no cover — malformed DSN surfaces elsewhere
        return None
    return str(host) if host else None


#: Resolved at import: the guard compares the raw (pre-DNS) hostname, so
#: the exemption must be by name, not address.
_TEST_DB_HOST = _test_db_host()


def _check_address(address: Any) -> None:
    if not isinstance(address, tuple):
        return  # Unix-domain sockets
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost" or host == _TEST_DB_HOST:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and ip.ipv4_mapped
            and ip.ipv4_mapped.is_loopback
        )
    ):
        return
    raise AssertionError(
        f"outbound network blocked in tests: {host!r}; inject a fake transport "
        "or explicitly mark an integration test allow_network"
    )


@pytest.fixture(autouse=True)
def _no_outbound_network(request: pytest.FixtureRequest) -> Any:
    """A private patch context survives tests calling monkeypatch.undo().

    This covers Python sockets, not subprocesses or native-library sockets
    (notably libpq, which connects to the isolated test database).
    """
    if request.node.get_closest_marker("allow_network") is not None:
        yield
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto
    getaddrinfo = socket.getaddrinfo
    sendmsg = getattr(socket.socket, "sendmsg", None)

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        _check_address(address)
        connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        _check_address(address)
        return connect_ex(sock, address)

    def guarded_sendto(sock: socket.socket, data: bytes, *args: Any) -> int:
        if args:
            _check_address(args[-1])
        return sendto(sock, data, *args)

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None:
            _check_address((host, port))
        return getaddrinfo(host, port, *args, **kwargs)

    def guarded_sendmsg(sock: socket.socket, *args: Any) -> int:
        if len(args) >= 4 and args[3] is not None:
            _check_address(args[3])
        assert sendmsg is not None
        return int(sendmsg(sock, *args))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", guarded_connect)
        patch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        patch.setattr(socket.socket, "sendto", guarded_sendto)
        patch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
        if sendmsg is not None:
            patch.setattr(socket.socket, "sendmsg", guarded_sendmsg)
        yield
