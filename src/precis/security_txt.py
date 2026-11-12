"""RFC 9116 ``/.well-known/security.txt`` body, shared by every public host.

One source of truth: the MCP ASGI app (:mod:`precis.server`) and the web host
(:mod:`precis_web.app`) both render the file here, so contact, languages and
``Expires`` renewal never drift between hosts. ``Expires`` is rolling
(``VALID_DAYS`` ahead of today, UTC), so the file never goes stale; 364 days
stays under the RFC's one-year ceiling with margin for clock skew. Contacts
come from ``PRECIS_SECURITY_CONTACT`` (comma-separated ``mailto:``/``https:``
URIs), defaulting to the owner's documented address; a security.txt without a
``Contact:`` line is invalid, so unset or blank never means "no contact".
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

PATH = "/.well-known/security.txt"
VALID_DAYS = 364

#: Default contact — the owner's documented address.
DEFAULT_CONTACTS = ("mailto:reto@retostamm.com",)


def contacts_from_env() -> tuple[str, ...]:
    """Comma-separated ``PRECIS_SECURITY_CONTACT`` entries, or the default."""
    raw = os.environ.get("PRECIS_SECURITY_CONTACT", "")
    entries = tuple(c.strip() for c in raw.split(",") if c.strip())
    return entries or DEFAULT_CONTACTS


def languages_from_env() -> str:
    """``PRECIS_SECURITY_LANGUAGES`` (default ``en``)."""
    return os.environ.get("PRECIS_SECURITY_LANGUAGES", "en")


def render(
    now: datetime | None = None,
    *,
    contacts: tuple[str, ...] | None = None,
    languages: str | None = None,
    canonical_url: str | None = None,
) -> str:
    """The security.txt text; unspecified fields come from the environment."""
    today = (
        (now or datetime.now(UTC))
        .astimezone(UTC)
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    expires = (today + timedelta(days=VALID_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    langs = languages_from_env() if languages is None else languages
    lines = [f"Contact: {c}" for c in (contacts or contacts_from_env())]
    lines.append(f"Expires: {expires}")
    if canonical_url:
        lines.append(f"Canonical: {canonical_url}")
    if langs:
        lines.append(f"Preferred-Languages: {langs}")
    return "\n".join(lines) + "\n"
