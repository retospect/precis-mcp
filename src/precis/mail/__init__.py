"""``precis.mail`` — the email kind's IMAP/SMTP machinery.

Named ``mail`` (not ``email``) so it never shadows the stdlib ``email``
package, which the body-parsing modules import. Design:
docs/backlog/email-kind.md. Slices 1-4 are built; v1 is read-only (send +
promotion/brief are later slices). IMAP is the source of truth — nothing
mirrors a mailbox into refs.

- :mod:`precis.mail.account` — typed view over an ``email_account`` row +
  JSONB config; provider presets; pluggable ``password``/``xoauth2`` auth
  (password in the vault, ``email.<addr>.password``).
- :mod:`precis.mail.imap` — stdlib connect + probe.
- :mod:`precis.mail.message` — list/fetch. ``BODY.PEEK`` + readonly SELECT
  means browsing never marks mail ``\\Seen``.
- :mod:`precis.mail.inject` — the email-worded tier-1 prompt/parse rung;
  re-exports the source-agnostic tier-0 core
  (:mod:`precis.utils.inject_scan`).

The browse handler is :mod:`precis.handlers.email`; the ``mail_poll`` +
``inject_scan`` worker passes (per-account poll → tier-0 scan → ``email_scan``
verdicts, then the model rung + quarantine) live under ``precis.workers`` and
run dark until enabled on one host. ``precis email poll|scan`` tick by hand.
"""

from __future__ import annotations

from precis.mail.account import Account, AuthMode, ImapSettings, SmtpSettings

__all__ = ["Account", "AuthMode", "ImapSettings", "SmtpSettings"]
