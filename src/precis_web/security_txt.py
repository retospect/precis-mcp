"""RFC 9116 ``security.txt`` for the public funnel host (gr627864).

The pentest-tools website scan (2026-11-10) flagged the missing
``/.well-known/security.txt`` on the publicly funnelled :443 host. This
module renders the file's body; :func:`precis_web.app.create_app` serves
it at that path, and :data:`precis_web.auth._OPEN_PATHS` exempts it from
the HTTP Basic gate — a security researcher reporting a vulnerability has
no credentials, and the file names nothing sensitive.

**``Expires`` is recomputed on every request** (``now + _EXPIRES_DAYS``),
which is the renewal path: a rolling expiry can never go stale the way a
baked-in timestamp does, and 364 days stays under the RFC's one-year
ceiling with margin for clock skew. The contact comes from
:data:`precis_web.config.WebConfig.security_contacts`
(``PRECIS_SECURITY_CONTACT``, comma-separated), defaulting to the owner's
documented address.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Rolling lifetime of the ``Expires`` field, in days. Deliberately 364,
#: not 365: the RFC caps the expiry at one year into the future, and this
#: keeps the file valid even if a checker measures against a slightly
#: skewed clock.
_EXPIRES_DAYS = 364


def render_security_txt(
    *,
    contacts: tuple[str, ...],
    languages: str,
    now: datetime,
    canonical_url: str,
) -> str:
    """The security.txt body for a request arriving at *canonical_url*.

    *now* must be tz-aware UTC; it is the request time, so every render
    carries a fresh ``Expires``. ``Canonical`` is the request's own URL:
    the file is served under whatever host the funnel answers as, and the
    RFC wants the absolute URI of the file itself.
    """
    expires = (now + timedelta(days=_EXPIRES_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    lines = [f"Contact: {contact}" for contact in contacts]
    lines.append(f"Expires: {expires}")
    lines.append(f"Canonical: {canonical_url}")
    if languages:
        lines.append(f"Preferred-Languages: {languages}")
    return "\n".join(lines) + "\n"
