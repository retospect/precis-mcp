"""IMAP connect + a connect/SEARCH probe (slice 1).

Stdlib ``imaplib`` only — zero new dependency, enough to prove credentials
and reach the mailbox. Later slices add fetch/parse (browse handler) and the
UID-windowed poll; those may swap in a richer client, but the connect seam
(``connect``) stays.
"""

from __future__ import annotations

import contextlib
import imaplib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.mail.account import Account, AuthMode, TlsMode

if TYPE_CHECKING:
    from precis.store import Store

#: A socket-level timeout so a wedged server can't hang the poll loop.
CONNECT_TIMEOUT_S = 30.0


class ImapAuthError(RuntimeError):
    """Login rejected, or an unsupported auth mode was requested."""


@dataclass(frozen=True, slots=True)
class FolderProbe:
    """One folder's state as seen by a probe."""

    folder: str
    exists: int  # message count (IMAP EXISTS)
    uidvalidity: int | None
    uidnext: int | None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    account: str
    host: str
    folders: list[FolderProbe]


@contextlib.contextmanager
def connect(account: Account, *, store: Store) -> Iterator[imaplib.IMAP4]:
    """Open an authenticated IMAP connection, yielded as a context manager.

    Resolves the vault secret, opens the right TLS flavour, logs in, and
    guarantees ``logout`` on exit. ``xoauth2`` is not yet implemented — it
    raises :class:`ImapAuthError` rather than silently falling back.
    """
    if account.auth is not AuthMode.PASSWORD:
        raise ImapAuthError(
            f"auth mode {account.auth.value!r} not implemented in v1 "
            f"(only 'password' / app-password); see docs/design/email-kind.md"
        )

    settings = account.imap
    if settings.tls is TlsMode.SSL:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            settings.host, settings.port, timeout=CONNECT_TIMEOUT_S
        )
    else:
        conn = imaplib.IMAP4(settings.host, settings.port, timeout=CONNECT_TIMEOUT_S)
        if settings.tls is TlsMode.STARTTLS:
            conn.starttls()

    try:
        secret = account.resolve_secret(store=store)
        try:
            conn.login(settings.user, secret)
        except imaplib.IMAP4.error as exc:  # bad creds, disabled login, etc.
            raise ImapAuthError(
                f"IMAP login failed for {account.address} on {settings.host}: {exc}"
            ) from exc
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _status_int(conn: imaplib.IMAP4, folder: str, item: str) -> int | None:
    """Read one STATUS attribute (UIDVALIDITY / UIDNEXT) as an int."""
    typ, data = conn.status(_quote(folder), f"({item})")
    if typ != "OK" or not data or data[0] is None:
        return None
    # data[0] like: b'INBOX (UIDVALIDITY 1234567890)'
    text = data[0].decode("ascii", "replace")
    marker = f"{item} "
    idx = text.find(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker) :].split(")", 1)[0].strip()
    return int(tail) if tail.isdigit() else None


def _quote(folder: str) -> str:
    """Quote a mailbox name for an IMAP command."""
    return '"' + folder.replace('"', '\\"') + '"'


def probe(account: Account, *, store: Store) -> ProbeResult:
    """Connect, log in, and report each watched folder's count + UIDVALIDITY.

    The slice-1 end-to-end proof: exercises the vault → TLS → LOGIN → SELECT
    path and returns something human-readable, without fetching any bodies.
    """
    folders: list[FolderProbe] = []
    with connect(account, store=store) as conn:
        for folder in account.folders:
            typ, data = conn.select(_quote(folder), readonly=True)
            exists = 0
            if typ == "OK" and data and data[0] is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    exists = int(data[0].decode("ascii", "replace"))
            folders.append(
                FolderProbe(
                    folder=folder,
                    exists=exists,
                    uidvalidity=_status_int(conn, folder, "UIDVALIDITY"),
                    uidnext=_status_int(conn, folder, "UIDNEXT"),
                )
            )
    return ProbeResult(account=account.address, host=account.imap.host, folders=folders)
