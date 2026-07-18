"""``precis.mail`` — the email kind's IMAP/SMTP machinery.

Named ``mail`` (not ``email``) so it never shadows the stdlib ``email``
package, which the body-parsing slices import. Slice 1 is the account model
(:mod:`precis.mail.account`) and a connect+SEARCH probe
(:mod:`precis.mail.imap`); browse handler, poll pass, and injection scan land
in later slices (docs/design/email-kind.md).
"""

from __future__ import annotations

from precis.mail.account import Account, AuthMode, ImapSettings, SmtpSettings

__all__ = ["Account", "AuthMode", "ImapSettings", "SmtpSettings"]
