"""RFC 9116 ``security.txt`` for the public funnel host (gr627864).

The pentest-tools website scan (2026-11-10) flagged the missing
``/.well-known/security.txt`` on the publicly funnelled :443 host. This
module renders the file's body; :func:`precis_web.app.create_app` serves
it at that path, and :data:`precis_web.auth._OPEN_PATHS` exempts it from
the HTTP Basic gate — a security researcher reporting a vulnerability has
no credentials, and the file names nothing sensitive.

**``Expires`` is recomputed on every request** (``now + VALID_DAYS``, see :mod:`precis.security_txt`),
which is the renewal path: a rolling expiry can never go stale the way a
baked-in timestamp does, and 364 days stays under the RFC's one-year
ceiling with margin for clock skew. The contact comes from
:data:`precis_web.config.WebConfig.security_contacts`
(``PRECIS_SECURITY_CONTACT``, comma-separated), defaulting to the owner's
documented address.
"""

from __future__ import annotations

from datetime import datetime

from precis import security_txt


def render_security_txt(
    *,
    contacts: tuple[str, ...],
    languages: str,
    now: datetime,
    canonical_url: str,
) -> str:
    """The security.txt body for a request arriving at *canonical_url*.

    Delegates to :func:`precis.security_txt.render` so the web host and the
    MCP port share one body and one ``Expires`` renewal rule. *now* must be
    tz-aware; ``Canonical`` is the request's own URL.
    """
    return security_txt.render(
        now, contacts=contacts, languages=languages, canonical_url=canonical_url
    )
