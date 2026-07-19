"""mail_poll — per-account IMAP poll + inline tier-0 injection scan (slice 3).

The mechanical, LLM-free lane of the email kind (docs/design/email-kind.md).
Each pass, for every account whose cadence is due:

1. **First poll / after a UIDVALIDITY change** — adopt the folder's current
   high-water (``UIDNEXT - 1``) *without* back-filling history. Watching a
   newsletter mailbox means scanning new mail going forward, not re-scanning
   thousands of archived messages; a human can promote an old one on demand.
2. **Steady state** — fetch messages with ``UID > last_uid`` (oldest-first,
   capped so a backlog drains across ticks), run the tier-0 regex scan on each
   inline, persist a verdict row to ``email_scan``, and advance ``last_uid``.

The poll paces itself off ``email_account.last_polled_at`` (cadence from
``config.poll_seconds``) and backs off exponentially on IMAP error via
``consecutive_errors`` — the same discipline as ``news_poll`` / ``fetch`` /
``chase``, so a wedged mailbox can't be re-hammered every tick.

v1 watches the account's **primary** folder (``folders[0]``, normally INBOX) —
the 0075 schema keeps one account-level cursor. Per-folder cursors are a later
slice. Bodies are never stored (IMAP is the source of truth); only the scan
verdict + evidence persist. The tier-1/2 model scan + the quarantine ladder are
slice 4; this pass only writes tier-0.

``watermark`` / ``fetch_new`` are injectable so tests exercise the pass without
a live IMAP server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from precis.mail.account import Account
from precis.mail.imap import ImapAuthError
from precis.mail.imap import folder_watermark as _default_watermark
from precis.mail.inject import scan_tier0
from precis.mail.message import DEFAULT_POLL_BATCH, PollBatch
from precis.mail.message import fetch_new as _default_fetch_new
from precis.store import Store

log = logging.getLogger(__name__)

#: ``(account, *, store, folder) -> (uidvalidity, uidnext)``.
WatermarkFn = Callable[..., tuple[int | None, int | None]]
#: ``(account, *, store, folder, since_uid, limit) -> PollBatch``.
FetchNewFn = Callable[..., PollBatch]


def run_mail_poll(
    store: Store,
    *,
    limit_accounts: int | None = None,
    batch_size: int = DEFAULT_POLL_BATCH,
    only_account: str | None = None,
    force: bool = False,
    watermark: WatermarkFn | None = None,
    fetch_new: FetchNewFn | None = None,
) -> dict[str, int]:
    """Poll accounts' primary folder; tier-0 scan new messages.

    By default (the worker path) polls every account whose cadence is *due*.
    ``only_account`` restricts to one account regardless of cadence/enabled
    (operator-forced); ``force`` polls every enabled account now, ignoring the
    cadence. Returns ``{claimed, ok, failed}``: ``claimed`` = accounts polled,
    ``ok`` = messages scanned, ``failed`` = accounts that errored.
    """
    wm = watermark or _default_watermark
    fetch = fetch_new or _default_fetch_new

    if only_account is not None:
        row = store.get_email_account(only_account)
        accounts = [row] if row is not None else []
    elif force:
        accounts = store.list_email_accounts(enabled_only=True)
    else:
        accounts = store.due_email_accounts(limit=limit_accounts)
    claimed = scanned = failed = 0

    for row in accounts:
        claimed += 1
        try:
            account = Account.from_row(row)
        except ValueError as exc:  # bad/missing IMAP config in the JSONB bag
            log.warning("mail_poll: %s misconfigured: %s", row.account, exc)
            store.record_email_poll(row.account, status=f"error: {exc}"[:200])
            failed += 1
            continue

        folder = account.folders[0] if account.folders else "INBOX"
        try:
            scanned += _poll_account(store, account, folder, batch_size, wm, fetch)
        except (ImapAuthError, OSError) as exc:  # login / socket / server error
            log.warning("mail_poll: %s poll failed: %s", account.address, exc)
            store.record_email_poll(account.address, status=f"error: {exc}"[:200])
            failed += 1
            continue

        store.record_email_poll(account.address, status="ok")

    log.info(
        "mail_poll pass: %d accounts, %d scanned, %d failed", claimed, scanned, failed
    )
    return {"claimed": claimed, "ok": scanned, "failed": failed}


def _init_highwater(
    store: Store, account: Account, folder: str, wm: WatermarkFn
) -> int:
    """Adopt the folder's current high-water without back-filling. Scans 0."""
    uidvalidity, uidnext = wm(account, store=store, folder=folder)
    if uidvalidity is None or uidnext is None:
        return 0  # STATUS incomplete — leave first-poll state, retry next tick
    store.set_email_account_highwater(
        account.address, last_uid=max(uidnext - 1, 0), uidvalidity=uidvalidity
    )
    log.info(
        "mail_poll: %s/%s init high-water at uid<%s", account.address, folder, uidnext
    )
    return 0


def _poll_account(
    store: Store,
    account: Account,
    folder: str,
    batch_size: int,
    wm: WatermarkFn,
    fetch: FetchNewFn,
) -> int:
    """Poll one account's primary folder; return messages scanned this tick."""
    # First poll ever: adopt the watermark, don't back-fill the archive.
    if account.uidvalidity is None:
        return _init_highwater(store, account, folder, wm)

    batch = fetch(
        account,
        store=store,
        folder=folder,
        since_uid=account.last_uid,
        limit=batch_size,
    )

    # UIDVALIDITY changed under us → the cursor is meaningless. Re-adopt from
    # now (like a first poll) rather than back-fill the whole re-numbered folder.
    if batch.uidvalidity is not None and batch.uidvalidity != account.uidvalidity:
        log.info(
            "mail_poll: %s/%s UIDVALIDITY %s→%s, resync",
            account.address,
            folder,
            account.uidvalidity,
            batch.uidvalidity,
        )
        return _init_highwater(store, account, folder, wm)

    scanned = 0
    for msg in batch.messages:
        result = scan_tier0(msg.subject, msg.body_text)
        store.record_email_scan(
            account.address,
            folder=folder,
            uidvalidity=account.uidvalidity,
            uid=msg.uid,
            verdict=result.verdict,
            tier=0,
            evidence=result.evidence,
        )
        scanned += 1

    if batch.messages:
        new_high = max(account.last_uid, max(m.uid for m in batch.messages))
        store.set_email_account_highwater(
            account.address, last_uid=new_high, uidvalidity=account.uidvalidity
        )
    return scanned


__all__ = ["run_mail_poll"]
