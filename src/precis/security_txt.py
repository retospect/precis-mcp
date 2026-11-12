"""RFC 9116 ``/.well-known/security.txt`` body, shared by every public host.

One source of truth: the MCP ASGI app (:mod:`precis.server`) and any other
public surface render the file here, so contact and ``Expires`` renewal never
drift between hosts. ``Expires`` is rolling (``_VALID_DAYS`` ahead of today,
UTC), so the file never goes stale. The contact comes from
``PRECIS_SECURITY_CONTACT`` (a ``mailto:`` or ``https:`` URI); unset means the
host serves no file rather than an invented contact.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

PATH = "/.well-known/security.txt"
_VALID_DAYS = 180


def render(now: datetime | None = None) -> str | None:
    """The security.txt text, or ``None`` when no contact is configured."""
    contact = os.environ.get("PRECIS_SECURITY_CONTACT", "").strip()
    if not contact:
        return None
    today = (
        (now or datetime.now(UTC))
        .astimezone(UTC)
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    expires = (today + timedelta(days=_VALID_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Contact: {contact}\nExpires: {expires}\nPreferred-Languages: en\n"
