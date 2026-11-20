"""The precis brand icon, shared by every public host (gr633600).

One source of truth — the :mod:`precis.security_txt` pattern: the MCP
ASGI app (:mod:`precis.server`), the web host (:mod:`precis_web.app`)
and the OAuth client records (:mod:`precis.oauth.provider`) all carry
the same packaged files, so connector pickers and browsers never fall
back to the infrastructure host's icon — the Tailscale mark that
prompted this. ``data/icons/precis.svg`` is the canonical
hand-authored mark (emerald ``#34d399``, the web UI's wordmark colour,
on slate-900); ``favicon.ico`` is a multi-resolution PIL render of the
same geometry for legacy ``/favicon.ico`` fetchers — regenerate it
from the SVG coordinates with :mod:`PIL` whenever the SVG changes.

Both paths are served **unauthenticated** on each host: the icon fetch
happens before any credential exists, exactly like ``security.txt``
and OAuth discovery.
"""

from __future__ import annotations

import base64
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.types import Icon

_PACKAGE = "precis.data.icons"
_SVG_FILE = "precis.svg"
_ICO_FILE = "favicon.ico"

#: Public GET/HEAD path each host answers with the vector mark.
SVG_PATH = "/favicon.svg"
#: Public GET/HEAD path each host answers with the raster pack.
ICO_PATH = "/favicon.ico"
PUBLIC_PATHS = frozenset({SVG_PATH, ICO_PATH})

SVG_MEDIA_TYPE = "image/svg+xml"
ICO_MEDIA_TYPE = "image/x-icon"

#: ``serverInfo.websiteUrl`` — the project's public homepage.
WEBSITE_URL = "https://github.com/retospect/precis-mcp"

#: Icon responses are brand assets: identical for every caller, safe to
#: share and safe to keep for a day.
CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}


def svg_bytes() -> bytes:
    """The canonical vector mark."""
    return resources.files(_PACKAGE).joinpath(_SVG_FILE).read_bytes()


def ico_bytes() -> bytes:
    """The multi-resolution raster pack."""
    return resources.files(_PACKAGE).joinpath(_ICO_FILE).read_bytes()


def data_uri() -> str:
    """An inline ``data:`` URI carrying the vector mark.

    ``serverInfo.icons`` accepts data URIs (MCP spec), so the icon
    reaches clients that never fetch the origin — stdio servers and
    deployments whose public URL is unknown at import time.
    """
    return (
        f"data:{SVG_MEDIA_TYPE};base64,{base64.b64encode(svg_bytes()).decode('ascii')}"
    )


def https_url(origin: str) -> str:
    """The fetchable icon URL on a public HTTPS *origin* (no slash)."""
    return origin.rstrip("/") + SVG_PATH


def for_path(path: str) -> tuple[bytes, str] | None:
    """``(body, media_type)`` for a public icon path, else ``None``."""
    if path == SVG_PATH:
        return svg_bytes(), SVG_MEDIA_TYPE
    if path == ICO_PATH:
        return ico_bytes(), ICO_MEDIA_TYPE
    return None


def mcp_icons(origin: str | None = None) -> list[Icon]:
    """``serverInfo.icons`` entries: fetchable URL first, inline fallback.

    The ``https:`` entry leads when the deployment's public origin is
    known (OAuth-configured streamable-http) so a connector can fetch a
    small file instead of storing an inline blob; the ``data:`` entry
    always follows so every transport still carries the mark.
    """
    from mcp.types import Icon

    entries = (
        [Icon(src=https_url(origin), mimeType=SVG_MEDIA_TYPE, sizes=["any"])]
        if origin
        else []
    )
    entries.append(Icon(src=data_uri(), mimeType=SVG_MEDIA_TYPE, sizes=["any"]))
    return entries
