"""SSRF guard for outbound HTTP fetches.

Both the ``web`` kind (``handlers/web.py``) and the OA cascade
(``workers/fetch_oa.py``) pull URLs that ultimately originate from
agent-supplied input (a ``put(kind='web', id=URL)``, a DOI handed to
``add``, an Unpaywall ``url_for_pdf`` chosen by the publisher). Each
previously used ``httpx.Client(follow_redirects=True)`` with only a
shape check on the URL — letting an attacker (or a benign publisher
mis-config) redirect us to a private/loopback/link-local address.

This module centralises the guard:

* :func:`assert_public_http_url` — synchronous DNS resolve + IP
  classification. Raises :class:`SsrfBlocked` on private/loopback/
  link-local/multicast/reserved/unspecified addresses.
* :func:`safe_get` — wraps ``client.get`` with manual redirect
  following; each ``Location`` is revalidated before the next hop.
* :func:`safe_stream` — context manager around ``client.stream`` that
  walks the redirect chain in stream mode (no body read on
  intermediate hops), validating each new URL.

We block the host at DNS-resolution time *before* any byte hits the
wire, so a host that resolves to RFC1918 / loopback / link-local
(including 169.254.169.254 — AWS / GCP / Azure instance metadata)
short-circuits with a clear error.

Callers MUST construct their ``httpx.Client`` with
``follow_redirects=False`` — the helpers do the redirect dance
themselves.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def assert_public_http_url(url: str) -> None:
    """Reject non-public targets before any byte hits the network.

    Raises:
        SsrfBlocked: scheme is not http(s), the URL has no host, the
            host fails to resolve, or any A/AAAA record falls in a
            blocked range.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise SsrfBlocked(f"refusing non-http(s) URL {url!r} (scheme={parts.scheme!r})")
    host = (parts.hostname or "").strip()
    if not host:
        raise SsrfBlocked(f"refusing URL with no host: {url!r}")

    # If the host parses directly as an IP literal, classify it without
    # consulting DNS — a literal IP can't be re-pointed mid-redirect.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_blocked(literal):
            raise SsrfBlocked(f"refusing host {host!r}: literal IP in a blocked range")
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SsrfBlocked(f"refusing host {host!r}: DNS lookup failed ({exc})") from exc

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


def _is_redirect(status_code: int) -> bool:
    """True for 301/302/303/307/308."""
    return status_code in (301, 302, 303, 307, 308)


def safe_get(client: httpx.Client, url: str, /, **kwargs: Any) -> httpx.Response:
    """``client.get(url, ...)`` with SSRF-validated redirects.

    The caller's ``client`` must be configured with
    ``follow_redirects=False``; we follow up to :data:`MAX_REDIRECTS`
    hops manually, calling :func:`assert_public_http_url` against each
    Location before issuing the next request.
    """
    assert_public_http_url(url)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        resp = client.get(current, **kwargs)
        if not _is_redirect(resp.status_code):
            return resp
        location = resp.headers.get("Location")
        if not location:
            return resp
        nxt = urljoin(str(resp.url), location)
        assert_public_http_url(nxt)
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
    """
    assert_public_http_url(url)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        with client.stream(method, current, **kwargs) as resp:
            if not _is_redirect(resp.status_code):
                yield resp
                return
            location = resp.headers.get("Location")
            if not location:
                yield resp
                return
        # Inner ``with`` closed the connection without consuming the
        # body. Re-resolve and continue.
        nxt = urljoin(current, location)
        assert_public_http_url(nxt)
        current = nxt
    raise SsrfBlocked(
        f"exceeded redirect limit ({_MAX_REDIRECTS}) starting from {url!r}"
    )


__all__ = [
    "MAX_REDIRECTS",
    "SsrfBlocked",
    "assert_public_http_url",
    "safe_get",
    "safe_stream",
]
