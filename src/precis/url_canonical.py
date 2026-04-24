"""URL canonicalisation for the ``web:`` bookmark kind.

Phase 1 of :doc:`docs/websites-plan`.  Two bookmarks of the same page
should collapse to one ref; the canonical-URL primary key is how we
enforce that.  All helpers here are pure functions so the handler +
tests don't need network or database.

The canonicalisation rules are deliberately gentle — we don't follow
redirects, hit DNS, or mutate paths.  We just normalise the shape so
``HTTPS://Example.COM/Foo/?utm_source=x#anchor`` and
``https://example.com/Foo`` compare equal.

Normalisation applied (in order):

1. Strip leading/trailing whitespace.
2. Lowercase the **scheme** and **host** only (path/query are case-sensitive).
3. Drop the default port (``:80`` for ``http``, ``:443`` for ``https``).
4. Remove common tracking query params (``utm_*``, ``fbclid``, ``gclid``,
   ``mc_cid``, ``mc_eid``, ``_ga``, ``igshid``, ``ref_src``) — empty query
   is stripped entirely.
5. Strip trailing ``/`` from the path, except for the root ``/``.
6. Strip the fragment ``#...`` unless the host is in :data:`SPA_HOSTS`
   (where the fragment is part of the routable URL, e.g. old Twitter,
   arXiv abstract anchors, GitHub blob line refs).

The slug derivation is separate: :func:`slug_from_url` produces a
human-readable slug from the canonical URL for the ``web:<slug>`` id,
with collision-disambiguation handled by the handler.
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

# Hosts where the URL fragment is routable (not just a scroll anchor).
# Kept small and documented — every entry is an explicit decision.
SPA_HOSTS: frozenset[str] = frozenset(
    {
        # arXiv abstract anchors (``/abs/2301.12345#some-section``) are
        # useful to preserve.
        "arxiv.org",
        # GitHub blob line refs (``#L42``), gist line refs.
        "github.com",
        "gist.github.com",
        # Notion SPA paths put content in the fragment.
        "notion.so",
        "www.notion.so",
    }
)

# Tracking/attribution params we always strip (exact match).  Keep the
# list tight; anything that *looks* like a genuine routing param
# (pagination, filters, search terms) stays.
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
        "s",  # twitter.com/?s=...
        "t",  # twitter.com/?t=...
    }
)

# Param-name prefixes we strip.  ``utm_*`` is the big one (Google
# Analytics / mailchimp / etc.).  Matched case-insensitively.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    """Return True if ``name`` should be stripped from a canonical URL."""
    lower = name.lower()
    if lower in TRACKING_PARAMS:
        return True
    return any(lower.startswith(p) for p in TRACKING_PREFIXES)


def canonicalise_url(url: str) -> str:
    """Return a canonical form of ``url``.

    Applies the 6-step normalisation described at module level.  Raises
    ``ValueError`` if the URL has no scheme or no host (both are
    mandatory for a bookmark).  Non-http(s) schemes are preserved as-is
    after trimming — e.g. ``ftp://`` URLs are returned unchanged except
    for whitespace trim.  Callers that only want http(s) URLs should
    check :func:`is_http_url` first.
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

    # Non-http(s) passes through with only scheme-lowercase + trim.
    if scheme not in ("http", "https"):
        return urlunsplit(
            (scheme, parts.netloc, parts.path, parts.query, parts.fragment)
        )

    # Split host / port for independent normalisation.
    host = parts.hostname or ""
    host = host.lower()
    port = parts.port
    if port is not None:
        default = 80 if scheme == "http" else 443
        netloc = host if port == default else f"{host}:{port}"
    else:
        netloc = host

    # Preserve userinfo if present (rare for bookmarks but valid URL
    # syntax).
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    # Path: strip trailing slash except the bare root.
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # Query: drop tracking params, keep the rest in original order.
    if parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        query = urlencode(kept, doseq=True)
    else:
        query = ""

    # Fragment: strip unless host is an SPA exception.
    if host in SPA_HOSTS:
        fragment = parts.fragment
    else:
        fragment = ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def is_http_url(url: str) -> bool:
    """Return ``True`` if ``url`` is a syntactically valid http(s) URL.

    Does not hit the network; only checks shape.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slug_from_url(canonical: str, *, max_len: int = 60) -> str:
    """Derive a readable slug body from a canonical URL.

    The returned slug is the *body* only — the handler prepends the
    ``web:`` scheme.  Produces e.g.::

        https://github.com/modelcontextprotocol/servers
        → github-com-modelcontextprotocol-servers

        https://example.com/
        → example-com

        https://arxiv.org/abs/2301.12345
        → arxiv-org-abs-2301-12345

    The slug is deterministic (no hashing) so duplicate prevention
    via DB uniqueness works naturally.  Length is capped so long paths
    don't blow out the Postgres slug column.
    """
    parts = urlsplit(canonical)
    host = (parts.hostname or "").lower()
    # Strip leading ``www.`` — ``www.example.com`` and ``example.com``
    # are usually the same site and the canonical form keeps whichever
    # the user passed, so only the slug de-dupes.
    host = re.sub(r"^www\.", "", host)

    path = unquote(parts.path or "").lower()
    # Path segments, ignoring empty (double slashes) and pure-dotted
    # (``.``, ``..``) components.
    segments = [seg for seg in path.split("/") if seg and seg not in (".", "..")]
    # Cap the number of path segments used in the slug so very deep
    # URLs don't produce 300-char slugs.
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
