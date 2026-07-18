"""``email_account`` CRUD — the email kind's per-account registry.

Mixin on :class:`precis.store.Store`. Backs slice 1 of
``docs/design/email-kind.md``. Migration ``0075_email_account.sql`` defines
the table: one row per mailbox account, with a JSONB ``config`` bag and a
poll high-water mark (``last_uid`` guarded by ``uidvalidity``). The secret
(password / OAuth token) is NOT here — ``secret_name`` is a vault key (ADR
0055); the reader resolves it with :func:`precis.secrets.get_secret`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EmailAccount:
    """One row from ``email_account``.

    ``config`` is the open-ended JSONB bag (imap/smtp host+port+tls, folders,
    poll_seconds, auth mode, scan_policy); the typed accessors on
    :class:`precis.mail.account.Account` interpret it. ``secret_name`` is the
    vault key holding the password/token, never the secret itself.
    """

    account: str
    enabled: bool
    secret_name: str
    last_uid: int
    uidvalidity: int | None
    config: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


_ACCT_COLS = "account, enabled, secret_name, last_uid, uidvalidity, config, updated_at"


def _row_to_account(row: tuple[Any, ...]) -> EmailAccount:
    return EmailAccount(
        account=str(row[0]),
        enabled=bool(row[1]),
        secret_name=str(row[2]),
        last_uid=int(row[3]),
        uidvalidity=None if row[4] is None else int(row[4]),
        config=dict(row[5] or {}),
        updated_at=row[6],
    )


# UPSERT the config half of a row (add / re-configure an account). The poll
# high-water marks (``last_uid`` / ``uidvalidity``) are deliberately NOT
# touched here — they're owned by the poll loop's advance path, so re-running
# ``precis email add`` to tweak config never rewinds the mailbox cursor.
_UPSERT_ACCOUNT = (
    "INSERT INTO email_account (account, enabled, secret_name, config) "
    "VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (account) DO UPDATE SET "
    "  enabled = EXCLUDED.enabled, "
    "  secret_name = EXCLUDED.secret_name, "
    "  config = EXCLUDED.config, "
    "  updated_at = now()"
)

# Advance the poll high-water mark. Guarded on uidvalidity: only move
# ``last_uid`` forward, and reset it to the given floor when uidvalidity
# changes (folder resync). The caller decides the values; this is the write.
_SET_HIGHWATER = (
    "UPDATE email_account SET last_uid = %s, uidvalidity = %s, updated_at = now() "
    "WHERE account = %s"
)


class EmailAccountMixin:
    """``email_account`` reads/writes for :class:`precis.store.Store`."""

    pool: Any  # provided by Store

    def upsert_email_account(
        self,
        account: str,
        *,
        secret_name: str,
        config: dict[str, Any],
        enabled: bool = True,
    ) -> None:
        """Create or re-configure an account (leaves poll cursor untouched)."""
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn:
            conn.execute(
                _UPSERT_ACCOUNT,
                (account, enabled, secret_name, Jsonb(config)),
            )

    def get_email_account(self, account: str) -> EmailAccount | None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                f"SELECT {_ACCT_COLS} FROM email_account WHERE account = %s",
                (account,),
            )
            row = cur.fetchone()
        return None if row is None else _row_to_account(row)

    def list_email_accounts(self, *, enabled_only: bool = False) -> list[EmailAccount]:
        where = "WHERE enabled" if enabled_only else ""
        with self.pool.connection() as conn:
            cur = conn.execute(
                f"SELECT {_ACCT_COLS} FROM email_account {where} ORDER BY account"
            )
            rows = cur.fetchall()
        return [_row_to_account(r) for r in rows]

    def delete_email_account(self, account: str) -> bool:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM email_account WHERE account = %s RETURNING account",
                (account,),
            )
            return cur.fetchone() is not None

    def set_email_account_highwater(
        self, account: str, *, last_uid: int, uidvalidity: int | None
    ) -> None:
        """Advance the poll cursor (owned by the poll loop, slice 3)."""
        with self.pool.connection() as conn:
            conn.execute(_SET_HIGHWATER, (last_uid, uidvalidity, account))
