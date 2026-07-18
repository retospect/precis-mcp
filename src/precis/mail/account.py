"""Typed view over an ``email_account`` row + its JSONB ``config`` bag.

The DB layer (:mod:`precis.store._email_ops`) hands back an
:class:`~precis.store._email_ops.EmailAccount` — flat columns plus an
open-ended ``config`` dict. This module interprets that dict into typed
settings and resolves the vault secret, so the IMAP/SMTP code never pokes at
raw JSON. Provider presets (gmail/fastmail/…) fill host/port defaults so a
minimal ``precis email add`` is enough for the common cases.

Auth is pluggable: ``password`` (covers plain-password providers *and*
Gmail-via-app-password — an app password is a normal LOGIN credential) is
implemented in v1; ``xoauth2`` (refresh-token flow for Gmail/O365 where app
passwords are disabled) is a documented stub the connect path rejects until a
later slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store._email_ops import EmailAccount


class AuthMode(StrEnum):
    PASSWORD = "password"
    XOAUTH2 = "xoauth2"


class TlsMode(StrEnum):
    SSL = "ssl"  # implicit TLS (IMAPS 993 / SMTPS 465)
    STARTTLS = "starttls"
    NONE = "none"


#: Host/port presets keyed by the account's domain. A row can still override
#: any of these via ``config.imap`` / ``config.smtp``; the preset only fills
#: what the operator didn't specify.
_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "gmail.com": {
        "imap": {"host": "imap.gmail.com", "port": 993, "tls": "ssl"},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "tls": "ssl"},
    },
    "googlemail.com": {
        "imap": {"host": "imap.gmail.com", "port": 993, "tls": "ssl"},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "tls": "ssl"},
    },
    "fastmail.com": {
        "imap": {"host": "imap.fastmail.com", "port": 993, "tls": "ssl"},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "tls": "ssl"},
    },
}

DEFAULT_FOLDERS = ["INBOX"]
DEFAULT_POLL_SECONDS = 900
DEFAULT_SCAN_POLICY = "quarantine"


@dataclass(frozen=True, slots=True)
class ImapSettings:
    host: str
    port: int
    tls: TlsMode
    user: str  # LOGIN username (defaults to the account address)


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    tls: TlsMode
    from_addr: str


def _domain(account: str) -> str:
    return account.rsplit("@", 1)[-1].lower() if "@" in account else ""


def _merge_preset(account: str, section: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Explicit config wins; provider preset fills the gaps."""
    preset = _PROVIDER_PRESETS.get(_domain(account), {}).get(section, {})
    return {**preset, **cfg.get(section, {})}


@dataclass(frozen=True, slots=True)
class Account:
    """Typed, secret-resolving view over an ``email_account`` row."""

    address: str
    enabled: bool
    secret_name: str
    last_uid: int
    uidvalidity: int | None
    auth: AuthMode
    imap: ImapSettings
    smtp: SmtpSettings | None
    folders: list[str]
    poll_seconds: int
    scan_policy: str

    @classmethod
    def from_row(cls, row: EmailAccount) -> Account:
        cfg = row.config or {}
        auth = AuthMode(str(cfg.get("auth", AuthMode.PASSWORD.value)))

        imap_cfg = _merge_preset(row.account, "imap", cfg)
        if not imap_cfg.get("host"):
            raise ValueError(
                f"email account {row.account!r}: no IMAP host "
                f"(no provider preset for its domain; set config.imap.host)"
            )
        imap = ImapSettings(
            host=str(imap_cfg["host"]),
            port=int(imap_cfg.get("port", 993)),
            tls=TlsMode(str(imap_cfg.get("tls", "ssl"))),
            user=str(imap_cfg.get("user", row.account)),
        )

        smtp_cfg = _merge_preset(row.account, "smtp", cfg)
        smtp = None
        if smtp_cfg.get("host"):
            smtp = SmtpSettings(
                host=str(smtp_cfg["host"]),
                port=int(smtp_cfg.get("port", 465)),
                tls=TlsMode(str(smtp_cfg.get("tls", "ssl"))),
                from_addr=str(smtp_cfg.get("from", row.account)),
            )

        folders = list(cfg.get("folders") or DEFAULT_FOLDERS)
        return cls(
            address=row.account,
            enabled=row.enabled,
            secret_name=row.secret_name,
            last_uid=row.last_uid,
            uidvalidity=row.uidvalidity,
            auth=auth,
            imap=imap,
            smtp=smtp,
            folders=folders,
            poll_seconds=int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)),
            scan_policy=str(cfg.get("scan_policy", DEFAULT_SCAN_POLICY)),
        )

    def resolve_secret(self, *, store: Store) -> str:
        """Reveal the account password/token from the vault (ADR 0055)."""
        from precis.secrets import require_secret

        return require_secret(self.secret_name, store=store)


def default_secret_name(account: str) -> str:
    """The conventional vault key for an account's password/token."""
    return f"email.{account}.password"


def load_account(store: Store, address: str) -> Account | None:
    """Load one account by address, or ``None`` if not configured."""
    row = store.get_email_account(address)
    return None if row is None else Account.from_row(row)


def enabled_accounts(store: Store) -> list[Account]:
    """All enabled accounts, as typed views."""
    return [Account.from_row(r) for r in store.list_email_accounts(enabled_only=True)]
