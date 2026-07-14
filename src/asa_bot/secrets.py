"""Secrets resolver — read asa_bot's secrets from the precis DB vault.

Mirrors precis-mcp's secrets vault (ADR 0055): values are pgcrypto-encrypted in
``vault.secrets`` and reached through ``vault.reveal(name)``. asa already holds
a precis DB DSN (for the NOTIFY listener), so it reveals over that connection —
no new credential.

Best-effort and env-override-wins: ``reveal_secret`` returns ``None`` on any
error (vault absent, key unset, DB unreachable) so callers fall back to their
existing env / file path. The DSN comes from ``PRECIS_DATABASE_URL`` in the
environment, read at call time (before any scrub).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_warned = False


def reveal_secret(name: str, *, dsn: str | None = None) -> str | None:
    """Return ``vault.reveal(name)`` from the precis DB, or ``None`` on any
    failure. ``dsn`` defaults to ``$PRECIS_DATABASE_URL``."""
    global _warned
    dsn = dsn or os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=10) as conn:
            row = conn.execute("SELECT vault.reveal(%s)", (name,)).fetchone()
    except Exception as exc:
        if not _warned:
            _warned = True
            log.warning(
                "asa secrets: vault reveal unavailable (%s: %s); "
                "falling back to env/file",
                type(exc).__name__,
                exc,
            )
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


__all__ = ["reveal_secret"]
