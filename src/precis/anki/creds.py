"""Per-user AnkiWeb credentials in the secrets vault.

Same shape as :func:`precis.export.remarkable.user_config_secret`: each
web user's own AnkiWeb login lives under a colon-suffixed vault name
(``ANKI_USER:<login>`` / ``ANKI_PASSWORD:<login>``), never a bare
``PRECIS_ANKI_*`` env var — a shell can't export a name with a colon in
it, so a per-user credential can't be shadowed by a stray deployment-wide
variable of that shape. There is no deployment-wide AnkiWeb account
anymore: every card belongs to the login it syncs to (``refs.owner_login``,
migration 0164), and this module is how :func:`precis.workers.anki_sync.run_anki_sync`
discovers which logins have something to sync.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis import secrets

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Vault name prefixes — see :func:`user_secret` / :func:`password_secret`.
_USER_SECRET = "ANKI_USER"
_PASSWORD_SECRET = "ANKI_PASSWORD"


def user_secret(login: str) -> str:
    """Vault name holding one user's own AnkiWeb login email."""
    return f"{_USER_SECRET}:{login}"


def password_secret(login: str) -> str:
    """Vault name holding one user's own AnkiWeb password."""
    return f"{_PASSWORD_SECRET}:{login}"


def set_user_credentials(store: Store, login: str, email: str, password: str) -> None:
    """Store ``login``'s own AnkiWeb email + password, self-service from
    ``/account``."""
    secrets.set_secret(user_secret(login), email, store=store)
    secrets.set_secret(password_secret(login), password, store=store)


def clear_user_credentials(store: Store, login: str) -> bool:
    """Remove ``login``'s AnkiWeb credentials from the vault.

    Returns ``False`` on a vault outage, mirroring
    :func:`precis.export.remarkable.clear_user_config` — say so rather
    than let a "disconnected" credential silently keep working (and
    keep syncing).
    """
    try:
        secrets.delete_secret(user_secret(login), store=store)
        secrets.delete_secret(password_secret(login), store=store)
    except Exception:  # pragma: no cover - vault outage
        log.warning(
            "anki: credentials for %s cleared but still in the vault",
            login,
            exc_info=True,
        )
        return False
    return True


def user_anki_configured(store: Store, login: str) -> bool:
    """True when ``login`` has both an AnkiWeb email and password stored."""
    return secrets.is_available(
        user_secret(login), store=store
    ) and secrets.is_available(password_secret(login), store=store)


def get_user_credentials(store: Store, login: str) -> tuple[str, str] | None:
    """``(email, password)`` for ``login``, or ``None`` if either is missing.

    The only function in this module that ever returns the password —
    never log it, and never surface it anywhere else (not even masked).
    """
    email = secrets.get_secret(user_secret(login), store=store)
    password = secrets.get_secret(password_secret(login), store=store)
    if not email or not password:
        return None
    return email, password


def anki_logins(store: Store) -> list[str]:
    """Every enabled web user with AnkiWeb credentials configured, sorted.

    Drives the per-user fan-out in
    :func:`precis.workers.anki_sync.run_anki_sync` when no explicit
    ``login`` is given — a disabled account or one that hasn't filled in
    ``/account``'s Anki section is silently skipped, not an error.
    """
    return sorted(
        u.login
        for u in store.list_web_users()
        if u.enabled and user_anki_configured(store, u.login)
    )


__all__ = [
    "anki_logins",
    "clear_user_credentials",
    "get_user_credentials",
    "password_secret",
    "set_user_credentials",
    "user_anki_configured",
    "user_secret",
]
