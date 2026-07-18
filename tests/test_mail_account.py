"""Account config parsing — pure, no DB (docs/design/email-kind.md slice 1).

:class:`precis.mail.account.Account` interprets the flat row + JSONB ``config``
into typed IMAP/SMTP settings, applying provider presets by domain and letting
explicit config win. These tests pin that interpretation.
"""

from __future__ import annotations

import pytest

from precis.mail.account import Account, AuthMode, TlsMode, default_secret_name
from precis.store._email_ops import EmailAccount


def _row(account: str, config: dict) -> EmailAccount:
    return EmailAccount(
        account=account,
        enabled=True,
        secret_name=default_secret_name(account),
        last_uid=0,
        uidvalidity=None,
        config=config,
    )


def test_gmail_preset_fills_host_and_port() -> None:
    acct = Account.from_row(_row("someone@gmail.com", {}))
    assert acct.imap.host == "imap.gmail.com"
    assert acct.imap.port == 993
    assert acct.imap.tls is TlsMode.SSL
    assert acct.imap.user == "someone@gmail.com"  # defaults to the address
    assert acct.smtp is not None and acct.smtp.host == "smtp.gmail.com"


def test_explicit_config_overrides_preset() -> None:
    acct = Account.from_row(
        _row(
            "someone@gmail.com",
            {"imap": {"host": "imap.example.test", "port": 1993, "user": "u1"}},
        )
    )
    assert acct.imap.host == "imap.example.test"
    assert acct.imap.port == 1993
    assert acct.imap.user == "u1"


def test_unknown_domain_needs_explicit_host() -> None:
    with pytest.raises(ValueError, match="no IMAP host"):
        Account.from_row(_row("rs@retostamm.com", {}))


def test_unknown_domain_with_explicit_host_ok() -> None:
    acct = Account.from_row(
        _row("rs@retostamm.com", {"imap": {"host": "mail.retostamm.com"}})
    )
    assert acct.imap.host == "mail.retostamm.com"
    assert acct.imap.port == 993  # default when unspecified


def test_defaults_folders_poll_scan_auth() -> None:
    acct = Account.from_row(_row("someone@gmail.com", {}))
    assert acct.folders == ["INBOX"]
    assert acct.poll_seconds == 900
    assert acct.scan_policy == "quarantine"
    assert acct.auth is AuthMode.PASSWORD


def test_config_overrides_folders_and_policy() -> None:
    acct = Account.from_row(
        _row(
            "someone@gmail.com",
            {
                "folders": ["INBOX", "Lists"],
                "poll_seconds": 300,
                "scan_policy": "flag-only",
            },
        )
    )
    assert acct.folders == ["INBOX", "Lists"]
    assert acct.poll_seconds == 300
    assert acct.scan_policy == "flag-only"


def test_xoauth2_auth_mode_parses() -> None:
    # Parsing accepts xoauth2; the connect path is what rejects it (v1 stub).
    acct = Account.from_row(_row("someone@gmail.com", {"auth": "xoauth2"}))
    assert acct.auth is AuthMode.XOAUTH2


def test_no_smtp_host_yields_none() -> None:
    acct = Account.from_row(_row("rs@retostamm.com", {"imap": {"host": "h"}}))
    assert acct.smtp is None  # unknown domain, no smtp configured → send not set up


def test_default_secret_name_shape() -> None:
    assert default_secret_name("rs@retostamm.com") == "email.rs@retostamm.com.password"
