"""URL canonicalisation + slug derivation.

Ported from v1 ``precis.url_canonical`` with the rename
``canonicalise_url`` → ``canonical_url`` (American spelling matches
the rest of v2). All helpers are pure — no network, no DB.

The canonicalisation rules collapse trivially-different forms of the
same URL so two cache lookups for the "same page" share a row:

    https://Example.COM/Foo?utm_source=x#anchor   →  https://example.com/Foo
    HTTP://example.com/                            →  http://example.com/
    https://example.com:443/x                      →  https://example.com/x

Steps applied (in order):

1. Strip leading/trailing whitespace.
2. Lowercase the **scheme** and **host** only (path/query are case-sensitive).
3. Drop the default port (``:80`` for ``http``, ``:443`` for ``https``).
4. Remove tracking query params (``utm_*``, ``fbclid``, ``gclid``, …).
5. Strip trailing ``/`` from the path, except the bare root.
6. Strip the fragment ``#…`` unless the host is in :data:`SPA_HOSTS`.
"""

from __future__ import annotations

import re
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

# Hosts where the URL fragment is routable (not a scroll anchor).
SPA_HOSTS: frozenset[str] = frozenset(
    {
        "arxiv.org",
        "github.com",
        "gist.github.com",
        "notion.so",
        "www.notion.so",
    }
)

# Tracking params we always strip (exact match, lowercased).
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "igshid",
        "ref_src",
        "ref_url",
        "s",  # twitter ?s=
        "t",  # twitter ?t=
    }
)

# Param-name prefixes we strip (case-insensitive).
TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    lower = name.lower()
    if lower in TRACKING_PARAMS:
        return True
    return any(lower.startswith(p) for p in TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """Return a canonical form of ``url``.

    Raises ``ValueError`` if the URL has no scheme or no host. Non-http
    schemes (ftp, mailto, …) pass through with only a whitespace + scheme
    lowercase normalisation.
    """
    if not url or not url.strip():
        raise ValueError("empty URL")
    raw = url.strip()

    parts = urlsplit(raw)
    if not parts.scheme:
        raise ValueError(f"URL missing scheme: {url!r}")
    if not parts.netloc:
        raise ValueError(f"URL missing host: {url!r}")

    scheme = parts.scheme.lower()

    if scheme not in ("http", "https"):
        return urlunsplit(
            (scheme, parts.netloc, parts.path, parts.query, parts.fragment)
        )

    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None:
        default = 80 if scheme == "http" else 443
        netloc = host if port == default else f"{host}:{port}"
    else:
        netloc = host

    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    if parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        query = urlencode(kept, doseq=True)
    else:
        query = ""

    fragment = parts.fragment if host in SPA_HOSTS else ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def is_http_url(url: str) -> bool:
    """Return True if ``url`` is a syntactically valid http(s) URL.

    No network. Just checks shape.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slug_from_url(canonical: str, *, max_len: int = 60) -> str:
    """Derive a readable slug body from a canonical URL.

    Output is deterministic (no hashing) so DB-level uniqueness suffices
    for dedup. Examples::

        https://github.com/modelcontextprotocol/servers
            → github-com-modelcontextprotocol-servers

        https://example.com/  → example-com

        https://arxiv.org/abs/2301.12345
            → arxiv-org-abs-2301-12345
    """
    parts = urlsplit(canonical)
    host = (parts.hostname or "").lower()
    host = re.sub(r"^www\.", "", host)

    path = unquote(parts.path or "").lower()
    segments = [seg for seg in path.split("/") if seg and seg not in (".", "..")]
    segments = segments[:5]

    raw = "-".join([host, *segments]) if host else "-".join(segments)
    cleaned = _SLUG_STRIP_RE.sub("-", raw).strip("-")
    cleaned = cleaned[:max_len].rstrip("-")
    return cleaned


def host_of(url: str) -> str:
    """Return the lowercase host of ``url`` (without ``www.``), or ``""``."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", host)
