"""Wayback Machine ``Save Page Now`` client for the ``web:`` kind.

Archival policy for stored bookmarks (see :doc:`docs/websites-plan`):

- **On by default.**  Agents / users opt out per-call with
  ``archive=False`` or globally via ``PRECIS_WEB_AUTO_ARCHIVE=0``.
- **Private URLs never leave the host.**  A syntactic + DNS guard
  short-circuits before the HTTP call for localhost, RFC1918, Tailscale
  CGNAT, ``.local`` / ``.internal`` / ``.lan`` / ``.home.arpa``, and any
  non-http(s) scheme.
- **Polite.**  One fire-and-forget call per save, 5 s timeout, global
  10-calls/min token bucket (below archive.org's documented 15/min
  anonymous cap).
- **Never blocks the write.**  On any failure the bookmark still
  succeeds; the caller records ``wayback_url=None`` +
  ``archive_skipped_reason='...'`` in meta.

All state lives in this module (token bucket, skipped-reason cache).
There are no side-effects on import.  The network call is gated on
``httpx`` being present, so the module is import-safe without the
``[external]`` extra.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# httpx is an optional extra (``precis-mcp[external]``).  Import at
# module level so tests can patch it; fall back to ``None`` when the
# extra isn't installed so this module stays import-safe.
try:
    import httpx as _httpx
except ImportError:  # pragma: no cover - extra-not-installed path
    _httpx = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────
# Policy constants
# ─────────────────────────────────────────────────────────────────────

#: Archive.org Save Page Now endpoint.  Archive saves are ``/save/<url>``
#: and respond with a ``Location: /web/<ts>/<url>`` header on success.
_SAVE_ENDPOINT = "https://web.archive.org/save/"

#: Base for the final snapshot URL we store in meta.wayback_url.
_WAYBACK_BASE = "https://web.archive.org/web/"

#: Per-call timeout for the Save Page Now request (seconds).
_SAVE_TIMEOUT_S = 5.0

#: Token-bucket: at most ``_MAX_CALLS`` in the last ``_WINDOW_S`` seconds.
#: 10/min is deliberately below archive.org's 15/min anonymous cap.
_MAX_CALLS = 10
_WINDOW_S = 60.0

#: Host suffixes that we never archive.  Kept as a tuple so matching is
#: O(n) and obvious to audit.
_PRIVATE_TLDS: tuple[str, ...] = (
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".test",
    ".invalid",
)


# ─────────────────────────────────────────────────────────────────────
# Skip reasons (agent-surfaced in meta.archive_skipped_reason)
# ─────────────────────────────────────────────────────────────────────


class SkipReason(str, Enum):
    """Why ``archive_url`` returned ``None``.

    The string value is what we store in
    ``meta.archive_skipped_reason`` so agents can explain the state.
    """

    USER_OPTOUT = "user_optout"
    GLOBAL_OPTOUT = "global_optout"
    PRIVATE_URL = "private_url"
    NON_HTTP = "non_http_scheme"
    RATE_LIMITED = "rate_limited"
    HTTPX_MISSING = "httpx_not_installed"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"


@dataclass
class ArchiveResult:
    """Outcome of an ``archive_url`` call.

    Exactly one of ``wayback_url`` or ``skipped_reason`` is set.
    """

    wayback_url: str | None = None
    skipped_reason: SkipReason | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.wayback_url is not None


# ─────────────────────────────────────────────────────────────────────
# Private-URL guard
# ─────────────────────────────────────────────────────────────────────


def is_private_url(url: str, *, resolve_dns: bool = False) -> tuple[bool, str]:
    """Return ``(True, reason)`` if ``url`` must not be sent to archive.org.

    Syntactic checks are always run.  DNS resolution (``resolve_dns=True``)
    catches hostnames that point at private IPs — off by default because
    the caller may want to avoid the DNS round-trip.

    ``reason`` is an empty string when the URL is safe to archive.
    """
    if not url:
        return True, "empty URL"
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return True, f"malformed URL: {exc}"

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return True, f"non-http(s) scheme: {scheme!r}"

    host = (parts.hostname or "").lower()
    if not host:
        return True, "URL has no host"

    # Literal loopback.
    if host in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True, f"loopback host: {host}"

    # Private-TLD suffixes (.local, .internal, .lan, .home.arpa, …).
    for suffix in _PRIVATE_TLDS:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            return True, f"private TLD: {suffix}"

    # Literal IP in the hostname (IPv4 or IPv6).
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None

    if ip is not None and _is_private_ip(ip):
        return True, f"private IP literal: {ip}"

    if resolve_dns and ip is None:
        # Resolve the name and check every returned address.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # Unresolvable host — don't archive (can't reach archive.org
            # with it either, and we can't confirm it's public).
            return True, f"DNS resolution failed for {host}"
        for info in infos:
            sockaddr = info[4]
            try:
                resolved = ipaddress.ip_address(sockaddr[0])
            except (ValueError, IndexError):
                continue
            if _is_private_ip(resolved):
                return True, f"{host} resolves to private IP {resolved}"

    return False, ""


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True for RFC1918, loopback, link-local, CGNAT, ULA, etc.

    Uses ``ipaddress.is_private`` which already covers 10/8, 172.16/12,
    192.168/16, 127/8, 169.254/16, fc00::/7, ::1, etc.  Adds an explicit
    CGNAT check (100.64.0.0/10) because Python's ``is_private`` returns
    False for that range despite it being a Tailscale / carrier-NAT
    range we don't want to leak.
    """
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    # Tailscale CGNAT.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network(
        "100.64.0.0/10"
    ):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Token bucket — shared across handler instances in-process
# ─────────────────────────────────────────────────────────────────────


class _TokenBucket:
    """Fixed-window rate limiter for archive.org saves.

    Not a strict token bucket — it's a bounded deque of call timestamps.
    On check we drop timestamps older than ``_WINDOW_S`` and compare
    the remaining count to ``_MAX_CALLS``.  Sufficient for our 10/min
    policy; no daemon thread required.
    """

    def __init__(self, max_calls: int = _MAX_CALLS, window_s: float = _WINDOW_S):
        self.max_calls = max_calls
        self.window_s = window_s
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        """Record a call and return True if it was within the budget."""
        now = time.monotonic()
        with self._lock:
            while self._calls and (now - self._calls[0]) > self.window_s:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True

    def reset(self) -> None:
        """Clear the window — used by tests."""
        with self._lock:
            self._calls.clear()


_BUCKET = _TokenBucket()


def reset_rate_limiter() -> None:
    """Clear the global rate-limiter window — for tests only."""
    _BUCKET.reset()


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def global_auto_archive() -> bool:
    """Return the default archive policy from the environment.

    ``PRECIS_WEB_AUTO_ARCHIVE=0`` / ``no`` / ``false`` → False.
    Anything else (or unset) → True.  Empty string is treated as unset.
    """
    raw = os.environ.get("PRECIS_WEB_AUTO_ARCHIVE", "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "no", "false", "off")


def archive_url(
    url: str,
    *,
    requested: bool | None = None,
    resolve_dns: bool = False,
) -> ArchiveResult:
    """Archive ``url`` via Wayback ``Save Page Now`` with full policy.

    Order of checks:

    1. Per-call opt-out — ``requested=False``.
    2. Global opt-out — ``PRECIS_WEB_AUTO_ARCHIVE=0``.
    3. Private-URL guard (always runs).
    4. Rate-limit token bucket.
    5. HTTP call (if ``httpx`` is importable).

    Returns :class:`ArchiveResult` — never raises.  Failures populate
    ``skipped_reason`` so the caller can record it in ``meta`` and
    surface it to the agent.
    """
    # Per-call opt-out beats everything else.
    if requested is False:
        return ArchiveResult(skipped_reason=SkipReason.USER_OPTOUT)

    # Per-call force-on (``requested=True``) bypasses the env default.
    # ``requested=None`` means "ask the env".
    if requested is None and not global_auto_archive():
        return ArchiveResult(skipped_reason=SkipReason.GLOBAL_OPTOUT)

    private, reason = is_private_url(url, resolve_dns=resolve_dns)
    if private:
        return ArchiveResult(
            skipped_reason=SkipReason.PRIVATE_URL, detail=reason
        )

    if not _BUCKET.try_acquire():
        return ArchiveResult(
            skipped_reason=SkipReason.RATE_LIMITED,
            detail=f"{_MAX_CALLS} saves / {_WINDOW_S:.0f}s cap reached",
        )

    if _httpx is None:
        return ArchiveResult(
            skipped_reason=SkipReason.HTTPX_MISSING,
            detail="install precis-mcp[external] to enable archiving",
        )

    try:
        # Fire-and-forget: archive.org's Save Page Now queues the job
        # and redirects to the eventual snapshot.  We want the final
        # /web/<ts>/<url> Location so we follow redirects.
        resp = _httpx.get(
            _SAVE_ENDPOINT + url,
            timeout=_SAVE_TIMEOUT_S,
            follow_redirects=True,
            headers={
                "User-Agent": "precis-mcp/5.1 (+https://github.com/retostamm)"
            },
        )
    except _httpx.TimeoutException as exc:
        return ArchiveResult(
            skipped_reason=SkipReason.NETWORK_ERROR,
            detail=f"timeout after {_SAVE_TIMEOUT_S}s: {exc}",
        )
    except _httpx.HTTPError as exc:
        return ArchiveResult(
            skipped_reason=SkipReason.NETWORK_ERROR,
            detail=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code >= 400:
        return ArchiveResult(
            skipped_reason=SkipReason.HTTP_ERROR,
            detail=f"HTTP {resp.status_code}",
        )

    wayback = _extract_wayback_url(resp, url)
    if not wayback:
        return ArchiveResult(
            skipped_reason=SkipReason.HTTP_ERROR,
            detail="save succeeded but no Content-Location header",
        )
    return ArchiveResult(wayback_url=wayback)


def _extract_wayback_url(resp, original_url: str) -> str | None:
    """Pull the Wayback snapshot URL out of a Save Page Now response.

    Prefers ``Content-Location`` (final snapshot), falls back to the
    final ``resp.url`` if it lives under ``/web/``.
    """
    content_loc = resp.headers.get("content-location") or resp.headers.get(
        "Content-Location"
    )
    if content_loc:
        if content_loc.startswith("/web/"):
            return "https://web.archive.org" + content_loc
        if content_loc.startswith(_WAYBACK_BASE):
            return content_loc

    final = str(resp.url)
    if _WAYBACK_BASE in final:
        return final
    return None
