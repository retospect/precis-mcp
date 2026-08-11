"""SSRF guard for outbound HTTP fetches.

Both the ``web`` kind (``handlers/web.py``) and the OA cascade
(``workers/fetch_oa.py``) pull URLs that ultimately originate from
agent-supplied input (a ``put(kind='web', id=URL)``, a DOI handed to
``add``, an Unpaywall ``url_for_pdf`` chosen by the publisher). Each
previously used ``httpx.Client(follow_redirects=True)`` with only a
shape check on the URL — letting an attacker (or a benign publisher
mis-config) redirect us to a private/loopback/link-local address.

This module centralises the guard:

* :func:`resolve_pinned_ip` — resolve the host **once**, classify every
  A/AAAA record, and return the single validated IP the request will be
  dialed against (or ``None`` for a host that is already an IP literal).
  Raises :class:`SsrfBlocked` on private/loopback/link-local/multicast/
  reserved/unspecified addresses. A standalone pre-check for callers that
  want to validate a URL without issuing a request; the send path does
  **not** use it (see below).
* :func:`assert_public_http_url` — validate-only wrapper (resolve +
  classify, discard the pinned IP) kept for callers that just want the
  check.
* :func:`pinning_transport` — an ``httpx.HTTPTransport`` whose httpcore
  network backend classifies-and-pins the DNS at ``connect_tcp``. This is
  where the actual send-path guard lives; :func:`http_client` installs it
  on every client by default.
* :func:`safe_get` — wraps ``client.send`` with manual redirect
  following; the client must carry :func:`pinning_transport` (built via
  :func:`http_client`) so each connection is classified-and-pinned.
* :func:`safe_stream` — context manager that walks the redirect chain in
  stream mode (no body read on intermediate hops).

**Resolve-once-at-connect, dial-the-validated-IP (no DNS-rebinding
TOCTOU).** An early design validated the *hostname* and then let httpx
re-resolve it at connect time — a time-of-check/time-of-use window where
an attacker controlling DNS for their own domain could answer the
validation lookup with a public IP and the connect-time lookup (moments
later, 0-TTL) with ``127.0.0.1`` / ``169.254.169.254`` / an internal
service IP. A second design closed that window by rewriting the outbound
request URL to the validated IP literal — but that collapsed httpcore's
connection-pool key from ``(scheme, host, port)`` to ``(scheme, IP,
port)``, letting two distinct hostnames that share one public IP reuse a
TLS connection cert-verified for the wrong name (gr180122).

We now pin at the **connection layer** instead: a custom httpcore
:class:`SyncBackend` (:func:`pinning_transport`) resolves-and-classifies
the host inside ``connect_tcp`` and dials the single validated IP, while
the request URL keeps its **hostname**. So there is still exactly one DNS
resolution and it is the one that dials (no rebinding window), *and* the
pool key + TLS ``server_hostname`` stay per-hostname (correct cert
verification, no cross-host connection reuse).

A literal-IP host (e.g. ``http://93.184.216.34/``) can't be re-pointed
mid-connect, so it is classified in place and dialed as-is.

Callers MUST route through :func:`http_client` (which installs
:func:`pinning_transport` and defaults ``follow_redirects=False``) — the
helpers do the redirect dance themselves and assert the pinning backend
is present.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from precis.utils.optional_deps import require_optional

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpcore
    import httpx


class SsrfBlocked(Exception):
    """Raised when a target URL resolves to a non-public address."""


# Networks we always refuse. ``ipaddress`` already classifies most of
# these via ``is_private``/``is_loopback``/``is_link_local``, but we
# enumerate explicitly so the intent is auditable from one place.
# 169.254.169.254 is the cloud-instance metadata endpoint on AWS,
# GCP, Azure, Hetzner, … — the canonical SSRF-to-credential target.
_BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),  # "this" network
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC1918
    ipaddress.IPv4Network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local + cloud metadata
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC1918
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC1918
    ipaddress.IPv4Network("198.18.0.0/15"),  # benchmark
    ipaddress.IPv4Network("224.0.0.0/4"),  # multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # reserved
)

_BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("::/128"),  # unspecified
    ipaddress.IPv6Network("fc00::/7"),  # unique local
    ipaddress.IPv6Network("fe80::/10"),  # link-local
    ipaddress.IPv6Network("ff00::/8"),  # multicast
)

# Redirect cap. httpx defaults to 20; we cap lower because each hop is
# a DNS+classify round-trip and the legitimate cases (publisher → CDN
# → CDN) finish well under this.
_MAX_REDIRECTS: int = 10

MAX_REDIRECTS: int = _MAX_REDIRECTS


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ``ip`` falls in any blocked network."""
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _BLOCKED_V4)
    if any(ip in net for net in _BLOCKED_V6):
        return True
    # IPv4-mapped IPv6 (``::ffff:10.0.0.1``) — extract the v4 part and
    # re-check so the v6 path can't bypass the v4 blocklist.
    mapped = ip.ipv4_mapped
    return mapped is not None and any(mapped in net for net in _BLOCKED_V4)


def _classify_and_pin_host(host: str) -> str | None:
    """Classify a bare host and return the IP to dial (or ``None``).

    Resolves ``host`` **exactly once** and classifies every A/AAAA
    record. If any resolves to a blocked range the host is refused
    (strict — matches a host that mixes a public and a private record).
    Otherwise returns the first validated IP for the caller to dial.
    Returns ``None`` when ``host`` is already an IP literal — there is
    nothing to re-resolve, so the caller dials it as-is.

    ``host`` is a hostname or IP literal only (no scheme/port) — the
    single resolution point shared by the standalone :func:`resolve_pinned_ip`
    pre-check and the connect-time backend (:func:`pinning_transport`).

    Raises:
        SsrfBlocked: the host fails to resolve to any address, or any
            A/AAAA record falls in a blocked range.
    """
    # If the host parses directly as an IP literal, classify it without
    # consulting DNS — a literal IP can't be re-pointed mid-connect, so
    # there is no IP to pin: dial it as-is.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_blocked(literal):
            raise SsrfBlocked(f"refusing host {host!r}: literal IP in a blocked range")
        return None

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SsrfBlocked(f"refusing host {host!r}: DNS lookup failed ({exc})") from exc

    pinned: str | None = None
    seen: set[str] = set()
    for info in infos:
        # info[4] is the sockaddr tuple; element 0 is the address as
        # ``str | int`` per typeshed, but in practice always ``str``
        # for ``AF_INET`` and ``AF_INET6``. Coerce defensively.
        ip_str = str(info[4][0])
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _ip_blocked(ip):
            raise SsrfBlocked(
                f"refusing host {host!r}: resolved to {ip_str} "
                f"(private/loopback/link-local/reserved)"
            )
        if pinned is None:
            pinned = ip_str
    if pinned is None:
        raise SsrfBlocked(f"refusing host {host!r}: no usable address resolved")
    return pinned


def _url_host(url: str) -> str:
    """Parse ``url``, enforce http(s) + a present host, return the host.

    DNS-free syntactic gate. Raises :class:`SsrfBlocked` for a non-http(s)
    scheme or a missing host — the cheap pre-flight the send path runs
    before handing the URL to httpx (the DNS classify+pin happens once, at
    connect, in :func:`pinning_transport`'s backend).
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise SsrfBlocked(f"refusing non-http(s) URL {url!r} (scheme={parts.scheme!r})")
    host = (parts.hostname or "").strip()
    if not host:
        raise SsrfBlocked(f"refusing URL with no host: {url!r}")
    return host


def resolve_pinned_ip(url: str) -> str | None:
    """Validate ``url``'s host and return the IP that would be dialed.

    Standalone pre-check: enforces the http(s) scheme + present host, then
    resolves-and-classifies the host once via :func:`_classify_and_pin_host`.
    Returns the validated IP (or ``None`` for an IP-literal host). Kept for
    callers that want to validate a URL *without* issuing a request — the
    send path (:func:`safe_get`/:func:`safe_stream`) does not call this;
    it pins at connect via the backend so there is no second resolution.

    Raises:
        SsrfBlocked: scheme is not http(s), the URL has no host, the
            host fails to resolve to any address, or any A/AAAA record
            falls in a blocked range.
    """
    return _classify_and_pin_host(_url_host(url))


@lru_cache(maxsize=1)
def _pinning_backend_class() -> type[httpcore.SyncBackend]:
    """Return (cached) the httpcore backend that pins DNS at connect.

    Defined lazily so ``httpcore`` (an ``[external]``-extra dep, pulled in
    with ``httpx``) is imported only when a pinning client is actually
    built. ``connect_tcp`` receives the **hostname** from the request URL
    (the pool key is kept per-hostname), classifies-and-pins it once, and
    dials the validated IP — the single resolution that also validates, so
    there is no DNS-rebinding window.
    """
    httpcore = require_optional("httpcore", extra="external")

    class _PinningBackend(httpcore.SyncBackend):  # type: ignore[name-defined]
        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> Any:
            pinned = _classify_and_pin_host(host)
            dial = pinned if pinned is not None else host
            return super().connect_tcp(
                dial,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    return _PinningBackend


def pinning_transport(**httptransport_kwargs: Any) -> httpx.HTTPTransport:
    """Build an ``httpx.HTTPTransport`` that pins DNS at ``connect_tcp``.

    The transport is a stock :class:`httpx.HTTPTransport` with its
    httpcore pool's network backend swapped for :func:`_pinning_backend_class`
    — so every connection it opens is SSRF-classified and pinned at connect
    while the request URL keeps its hostname (per-hostname pool key + TLS
    ``server_hostname``). :func:`http_client` installs this by default; call
    it directly only to build a bespoke client that must still be guarded.
    """
    httpx = require_optional("httpx", extra="external")
    transport = httpx.HTTPTransport(**httptransport_kwargs)
    # httpx.HTTPTransport builds an internal httpcore.ConnectionPool and
    # stores the backend as ``_pool._network_backend``; swap it before any
    # request so all connections route through the pinning backend. Private
    # attrs, guarded by test_pinning_backend_is_installed.
    transport._pool._network_backend = _pinning_backend_class()()
    return transport


def _assert_pinned_client(client: httpx.Client) -> None:
    """Refuse a client that isn't carrying the pinning backend.

    ``safe_get``/``safe_stream`` move the SSRF guard into the client's
    transport, so a client built *without* :func:`pinning_transport` (e.g. a
    plain ``httpx.Client()``) would fetch agent-supplied URLs unguarded.
    Assert the backend is present so that misuse fails loud and closed
    rather than silently unguarded. Build clients via :func:`http_client`.
    """
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if not isinstance(backend, _pinning_backend_class()):
        raise SsrfBlocked(
            "safe_get/safe_stream require a client built via http_client() "
            "(pinning_transport); got an unguarded client"
        )


def assert_public_http_url(url: str) -> None:
    """Reject non-public targets before any byte hits the network.

    Validate-only wrapper around :func:`resolve_pinned_ip` (resolves +
    classifies, discards the pinned IP). Kept for callers that want the
    check without issuing a request. ``safe_get``/``safe_stream`` call
    :func:`resolve_pinned_ip` directly so they resolve once and dial the
    validated IP.

    Raises:
        SsrfBlocked: scheme is not http(s), the URL has no host, the
            host fails to resolve, or any A/AAAA record falls in a
            blocked range.
    """
    resolve_pinned_ip(url)


def _is_redirect(status_code: int) -> bool:
    """True for 301/302/303/307/308."""
    return status_code in (301, 302, 303, 307, 308)


def _pinned_request(
    client: httpx.Client, method: str, url: str, kwargs: dict[str, Any]
) -> httpx.Request:
    """Build the request for ``url`` after the DNS-free shape gate.

    Enforces the http(s) scheme + present host via :func:`_url_host`
    (raising :class:`SsrfBlocked` before any byte hits the wire), then
    builds the request with the URL **unchanged** — the hostname is kept
    so the connection pool keys per-hostname and TLS verifies the right
    name. The DNS classify-and-pin happens once, at connect, in the
    client's pinning backend (:func:`pinning_transport`).
    """
    _url_host(url)
    return client.build_request(method, url, **kwargs)


def _split_send_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Peel off ``client.send``-only kwargs from build-request kwargs.

    ``auth`` is a send-time argument, not a ``build_request`` one; carry
    it through. ``follow_redirects`` is dropped — the caller's client must
    be ``follow_redirects=False`` and we always walk the chain ourselves,
    revalidating each hop.
    """
    send_kw: dict[str, Any] = {}
    if "auth" in kwargs:
        send_kw["auth"] = kwargs.pop("auth")
    kwargs.pop("follow_redirects", None)
    return send_kw


def safe_get(client: httpx.Client, url: str, /, **kwargs: Any) -> httpx.Response:
    """``client.get(url, ...)`` with SSRF-validated, IP-pinned redirects.

    The caller's ``client`` must be built via :func:`http_client` (so it
    carries the pinning backend) and configured with
    ``follow_redirects=False``; we follow up to :data:`MAX_REDIRECTS`
    hops manually. Each connection is classified-and-pinned at connect by
    the client's backend — the host is resolved once and that exact
    validated IP is dialed, so there is no DNS-rebinding window.
    """
    _assert_pinned_client(client)
    send_kw = _split_send_kwargs(kwargs)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        request = _pinned_request(client, "GET", current, kwargs)
        resp = client.send(request, **send_kw)
        if not _is_redirect(resp.status_code):
            return resp
        location = resp.headers.get("Location")
        if not location:
            return resp
        # Resolve the Location against ``current`` (the original hostname
        # URL), NOT ``resp.url`` — the latter now carries the pinned IP, so
        # a relative Location against it would drop the hostname.
        nxt = urljoin(current, location)
        current = nxt
    raise SsrfBlocked(
        f"exceeded redirect limit ({_MAX_REDIRECTS}) starting from {url!r}"
    )


@contextmanager
def safe_stream(
    client: httpx.Client,
    method: str,
    url: str,
    /,
    **kwargs: Any,
) -> Iterator[httpx.Response]:
    """Context manager around ``client.stream`` with safe redirects.

    Yields the final post-redirect ``httpx.Response`` for the caller
    to iterate via ``resp.iter_bytes(...)``. Intermediate redirect
    hops use ``client.stream`` too so we never download an
    interstitial body just to read its Location header.

    Caller pattern::

        with safe_stream(client, "GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                ...

    The client must be built via :func:`http_client` so its backend
    classifies-and-pins each connection at connect (one resolution, no
    DNS-rebinding window).
    """
    _assert_pinned_client(client)
    send_kw = _split_send_kwargs(kwargs)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        request = _pinned_request(client, method, current, kwargs)
        resp = client.send(request, stream=True, **send_kw)
        location: str | None = None
        try:
            if not _is_redirect(resp.status_code):
                yield resp
                return
            location = resp.headers.get("Location")
            if not location:
                yield resp
                return
        finally:
            # Final response: closed on generator finalisation, after the
            # caller has iterated. Redirect hop: closed here without
            # consuming the interstitial body, before the next request.
            resp.close()
        nxt = urljoin(current, location)
        current = nxt
    raise SsrfBlocked(
        f"exceeded redirect limit ({_MAX_REDIRECTS}) starting from {url!r}"
    )


__all__ = [
    "MAX_REDIRECTS",
    "SsrfBlocked",
    "assert_public_http_url",
    "pinning_transport",
    "resolve_pinned_ip",
    "safe_get",
    "safe_stream",
]
