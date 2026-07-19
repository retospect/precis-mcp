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
    vault key holding the password/token, never the secret itself. The
    ``last_polled_at`` / ``consecutive_errors`` / ``last_status`` trio is the
    ``mail_poll`` pass's bookkeeping (slice 3; migration 0076).
    """

    account: str
    enabled: bool
    secret_name: str
    last_uid: int
    uidvalidity: int | None
    config: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
    last_polled_at: datetime | None = None
    consecutive_errors: int = 0
    last_status: str | None = None


@dataclass(frozen=True, slots=True)
class EmailScan:
    """One row from ``email_scan`` — a per-message injection-scan verdict.

    No body is stored (IMAP is source of truth); only the verdict, the tier
    that produced it (0 = ``mail_poll`` regex), and the ``evidence`` bag
    (which signals fired + scanner version). Keyed by
    (account, folder, uidvalidity, uid).
    """

    account: str
    folder: str
    uidvalidity: int
    uid: int
    verdict: str
    tier: int
    evidence: dict[str, Any] = field(default_factory=dict)
    scanned_at: datetime | None = None


_ACCT_COLS = (
    "account, enabled, secret_name, last_uid, uidvalidity, config, updated_at, "
    "last_polled_at, consecutive_errors, last_status"
)


def _row_to_account(row: tuple[Any, ...]) -> EmailAccount:
    return EmailAccount(
        account=str(row[0]),
        enabled=bool(row[1]),
        secret_name=str(row[2]),
        last_uid=int(row[3]),
        uidvalidity=None if row[4] is None else int(row[4]),
        config=dict(row[5] or {}),
        updated_at=row[6],
        last_polled_at=row[7],
        consecutive_errors=int(row[8] or 0),
        last_status=None if row[9] is None else str(row[9]),
    )


_SCAN_COLS = "account, folder, uidvalidity, uid, verdict, tier, evidence, scanned_at"


def _row_to_scan(row: tuple[Any, ...]) -> EmailScan:
    return EmailScan(
        account=str(row[0]),
        folder=str(row[1]),
        uidvalidity=int(row[2]),
        uid=int(row[3]),
        verdict=str(row[4]),
        tier=int(row[5]),
        evidence=dict(row[6] or {}),
        scanned_at=row[7],
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

# Accounts whose next mail_poll tick is due. Cadence = config.poll_seconds
# (default 900), multiplied by 2^consecutive_errors for exponential backoff on
# a failing account, capped at one day — the news_sources/fetch/chase
# discipline. A never-polled account (last_polled_at IS NULL) is always due.
# The backoff stays in double precision and is capped by ``least`` BEFORE any
# int cast: a permanently-failing account would otherwise drive
# ``poll_seconds * 2^errors`` past int4's range (~21 errors) and throw, taking
# the whole poll pass down. make_interval(secs => …) accepts the double.
_DUE_ACCOUNTS = (
    f"SELECT {_ACCT_COLS} FROM email_account "
    "WHERE enabled = true AND ("
    "  last_polled_at IS NULL OR "
    "  now() - last_polled_at >= make_interval(secs => least("
    "    COALESCE(NULLIF(config->>'poll_seconds', '')::int, 900) "
    "      * power(2, consecutive_errors), 86400))) "
    "ORDER BY account"
)

# Record a poll outcome: stamp last_polled_at, reset the error counter on
# success or increment it on failure (drives the backoff above).
_RECORD_POLL = (
    "UPDATE email_account SET last_polled_at = now(), last_status = %s, "
    "consecutive_errors = CASE WHEN %s = 0 THEN 0 ELSE consecutive_errors + 1 END "
    "WHERE account = %s"
)

# Record a tier-0 verdict. INSERT-if-absent: a message is scanned exactly once
# (the high-water advances past it), and DO NOTHING guarantees tier-0 never
# clobbers a deeper (tier >= 1) verdict a later inject_scan pass wrote.
_INSERT_SCAN = (
    "INSERT INTO email_scan "
    "(account, folder, uidvalidity, uid, verdict, tier, evidence) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (account, folder, uidvalidity, uid) DO NOTHING"
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

    def due_email_accounts(self, *, limit: int | None = None) -> list[EmailAccount]:
        """Enabled accounts whose next ``mail_poll`` tick is due (cadence+backoff)."""
        sql = _DUE_ACCOUNTS + (f" LIMIT {int(limit)}" if limit is not None else "")
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_account(r) for r in rows]

    def record_email_poll(self, account: str, *, status: str) -> None:
        """Stamp a poll outcome; ``status == 'ok'`` clears the backoff counter."""
        err = 0 if status == "ok" else 1
        with self.pool.connection() as conn:
            conn.execute(_RECORD_POLL, (status[:200], err, account))

    def record_email_scan(
        self,
        account: str,
        *,
        folder: str,
        uidvalidity: int,
        uid: int,
        verdict: str,
        tier: int,
        evidence: dict[str, Any],
    ) -> bool:
        """Persist a tier-0 verdict; returns False if a row already existed."""
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn:
            cur = conn.execute(
                _INSERT_SCAN,
                (account, folder, uidvalidity, uid, verdict, tier, Jsonb(evidence)),
            )
            return cur.rowcount > 0

    def get_email_scan(
        self, account: str, *, folder: str, uidvalidity: int, uid: int
    ) -> EmailScan | None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                f"SELECT {_SCAN_COLS} FROM email_scan "
                "WHERE account = %s AND folder = %s AND uidvalidity = %s AND uid = %s",
                (account, folder, uidvalidity, uid),
            )
            row = cur.fetchone()
        return None if row is None else _row_to_scan(row)
