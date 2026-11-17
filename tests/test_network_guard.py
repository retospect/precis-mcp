"""Regression coverage for accidental registry publication and socket traffic."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from precis.nanopub import registry
from tests._network import _check_address


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_remote_tcp_is_blocked(method: str, family: int) -> None:
    host = "192.0.2.1" if family == socket.AF_INET else "2001:db8::1"
    with socket.socket(family) as sock:
        with pytest.raises(AssertionError, match="outbound network blocked"):
            getattr(sock, method)((host, 443))


def test_remote_udp_and_dns_are_blocked() -> None:
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="outbound network blocked"):
            sock.sendto(b"must not leave", ("192.0.2.1", 53))
    with pytest.raises(AssertionError, match="outbound network blocked"):
        socket.getaddrinfo("registry.petapico.org", 443)


def test_remote_sendmsg_is_blocked() -> None:
    if not hasattr(socket.socket, "sendmsg"):
        pytest.skip("sendmsg unavailable on this platform")
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="outbound network blocked"):
            sock.sendmsg([b"must not leave"], [], 0, ("192.0.2.1", 53))


def test_sendto_without_address_preserves_socket_error() -> None:
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        sock.connect(("127.0.0.1", 9))
        # CPython requires an address even for a connected UDP socket.
        sendto: Any = sock.sendto
        with pytest.raises(TypeError):
            sendto(b"local")


def test_loopback_server_is_available() -> None:
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen()
        client.connect(server.getsockname())
        connection, _ = server.accept()
        with connection:
            client.sendall(b"local")
            assert connection.recv(5) == b"local"


@pytest.mark.parametrize("address", ["/tmp/test.sock", ("::1", 80), ("localhost", 80)])
def test_local_addresses_are_allowed(address: object) -> None:
    _check_address(address)


def test_monkeypatch_undo_preserves_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    with pytest.raises(AssertionError, match="outbound network blocked"):
        socket.getaddrinfo("registry.petapico.org", 443)


@pytest.mark.allow_network
def test_explicit_opt_in_restores_socket_methods() -> None:
    # Inspect the binding, without contacting an external service.
    assert socket.socket.connect.__name__ == "connect"


@pytest.mark.allow_network
def test_registry_defaults_remain_blocked_with_network_opt_in() -> None:
    with pytest.raises(pytest.fail.Exception, match="inject post=/fetch="):
        registry._default_post(registry.DEFAULT_REGISTRY_URL, b"never publish")
    with pytest.raises(pytest.fail.Exception, match="inject post=/fetch="):
        registry._default_lookup(registry.DEFAULT_REGISTRY_URL)
