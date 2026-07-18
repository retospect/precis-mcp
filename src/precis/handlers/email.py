"""``email`` kind — live, read-only IMAP browse (slice 2).

A live adapter over a configured mailbox account (``precis email add``). IMAP
is the source of truth — this handler mirrors nothing: every call fetches live
and renders. The summarization path (later slice) is what promotes a chosen
message into the chunk pipeline; browsing is read-only and, via ``BODY.PEEK`` +
readonly SELECT, never marks mail ``\\Seen``.

Agent surface (v1):

    get(kind='email')                       — overview: recent mail in the
                                              primary folder + account info
    get(kind='email', id='INBOX')           — list recent messages in a folder
    get(kind='email', id='INBOX/12345')     — read one message (headers + body)
    get(kind='email', account='rs@…', …)    — pick an account (optional when
                                              exactly one is configured)

Send (SMTP), search, and injection-scan gating land in later slices.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Upstream
from precis.mail import message as mail_message
from precis.mail.account import Account, enabled_accounts, load_account
from precis.mail.imap import ImapAuthError
from precis.protocol import Handler, KindSpec
from precis.response import Response


class EmailHandler(Handler):
    """Live read-only IMAP browse for configured mailbox accounts."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="email",
        title="Email (IMAP browse)",
        description=(
            "Browse a configured mailbox live over IMAP (read-only; never "
            "marks mail read). get(kind='email') lists recent mail; "
            "id='INBOX' lists a folder; id='INBOX/<uid>' reads one message. "
            "Pass account='addr@host' when more than one is configured. "
            "Configure accounts with the `precis email` CLI; the password "
            "lives in the secrets vault. IMAP is the source of truth — "
            "nothing is mirrored into precis until you promote a message."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("email: store required")
        self.store = hub.store
        self.hub = hub

    # ── verb ───────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        account: str | None = None,
        view: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        **_kw: Any,
    ) -> Response:
        acct = self._resolve_account(account)
        n = limit or mail_message.DEFAULT_LIST_LIMIT

        target = "" if id is None else str(id).strip()
        if target in ("", "/"):
            return self._overview(acct, limit=n)

        folder, uid = _parse_target(target)
        try:
            if uid is not None:
                return self._read_message(acct, folder=folder, uid=uid)
            return self._list_folder(acct, folder=folder, limit=n)
        except ImapAuthError as exc:
            raise Upstream(
                f"email: IMAP auth failed for {acct.address}: {exc}",
                next="check the vault secret; re-run `precis email test`",
            ) from exc
        except OSError as exc:
            raise Upstream(
                f"email: IMAP connection failed for {acct.address}: {exc}",
                next="check the host is reachable and try again",
            ) from exc

    # ── account resolution ─────────────────────────────────────────────

    def _resolve_account(self, account: str | None) -> Account:
        if account:
            acct = load_account(self.store, account.strip())
            if acct is None:
                raise BadInput(
                    f"email: no account {account!r} configured",
                    next="precis email add <address> --imap-host <host>",
                )
            return acct
        accts = enabled_accounts(self.store)
        if not accts:
            raise BadInput(
                "email: no accounts configured",
                next="precis email add <address> --imap-host <host> --password-stdin",
            )
        if len(accts) > 1:
            raise BadInput(
                "email: multiple accounts configured — pass account=",
                options=[a.address for a in accts],
                next="get(kind='email', account='<address>')",
            )
        return accts[0]

    # ── renders ────────────────────────────────────────────────────────

    def _overview(self, acct: Account, *, limit: int) -> Response:
        primary = acct.folders[0] if acct.folders else "INBOX"
        try:
            headers = mail_message.list_recent(
                acct, store=self.store, folder=primary, limit=limit
            )
        except ImapAuthError as exc:
            raise Upstream(
                f"email: IMAP auth failed for {acct.address}: {exc}",
                next="check the vault secret; re-run `precis email test`",
            ) from exc
        except OSError as exc:
            raise Upstream(
                f"email: IMAP connection failed for {acct.address}: {exc}",
                next="check the host is reachable and try again",
            ) from exc

        lines = [
            f"# {acct.address} — {primary}",
            "",
            f"_watched folders: {', '.join(acct.folders)}_",
            "",
            _render_header_table(headers, folder=primary),
        ]
        if len(acct.folders) > 1:
            others = ", ".join(f"`{f}`" for f in acct.folders if f != primary)
            lines.append("")
            lines.append(f"Other folders: {others} — get(kind='email', id='<folder>')")
        return Response(body="\n".join(lines))

    def _list_folder(self, acct: Account, *, folder: str, limit: int) -> Response:
        headers = mail_message.list_recent(
            acct, store=self.store, folder=folder, limit=limit
        )
        body = f"# {acct.address} — {folder}\n\n" + _render_header_table(
            headers, folder=folder
        )
        return Response(body=body)

    def _read_message(self, acct: Account, *, folder: str, uid: int) -> Response:
        msg = mail_message.fetch_one(acct, store=self.store, folder=folder, uid=uid)
        if msg is None:
            raise NotFound(
                f"email: no message {folder}/{uid} on {acct.address}",
                next=f"get(kind='email', id={folder!r}) to list the folder",
            )
        lines = [
            f"# {msg.subject or '(no subject)'}",
            "",
            f"- **From:** {msg.from_}",
            f"- **To:** {msg.to}",
            f"- **Date:** {msg.date}",
            f"- **Id:** `{folder}/{uid}`",
        ]
        if msg.truncated_html:
            lines.append("- _(body extracted from HTML — formatting stripped)_")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(msg.body_text or "_(empty body)_")
        return Response(body="\n".join(lines))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_target(target: str) -> tuple[str, int | None]:
    """Split an id into ``(folder, uid)``.

    ``INBOX/12345`` → ``('INBOX', 12345)`` (uid = trailing all-digit segment);
    ``INBOX`` / ``INBOX/Lists`` → ``('INBOX'|'INBOX/Lists', None)`` (folder
    listing). A leading ``/`` is tolerated and stripped.
    """
    work = target.lstrip("/")
    if "/" in work:
        head, tail = work.rsplit("/", 1)
        if tail.isdigit() and head:
            return head, int(tail)
    return work, None


def _render_header_table(
    headers: list[mail_message.MessageHeader], *, folder: str
) -> str:
    if not headers:
        return "_(no messages)_"
    lines = []
    for h in headers:
        subject = h.subject or "(no subject)"
        if len(subject) > 78:
            subject = subject[:75] + "..."
        sender = _short_from(h.from_)
        lines.append(f"- `{folder}/{h.uid}`  {sender} — {subject}")
    lines.append("")
    lines.append("_Read one: get(kind='email', id='<folder>/<uid>')_")
    return "\n".join(lines)


def _short_from(from_: str) -> str:
    """Prefer a display name; fall back to the address, capped."""
    val = from_.strip()
    if not val:
        return "(unknown)"
    # "Name <addr>" → Name; bare "addr" → addr.
    if "<" in val:
        name = val.split("<", 1)[0].strip().strip('"')
        if name:
            val = name
    return val[:32]
