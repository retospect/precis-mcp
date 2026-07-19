"""inject_scan — tier-1/2 model injection scan + quarantine ladder (slice 4).

The LLM lane of the email kind (docs/design/email-kind.md), the deep rung of
the cascade whose tier-0 regex sibling runs inline in ``mail_poll``. Each pass:

1. **Claim** tier-0 verdicts a deeper scan hasn't reached
   (``store.pending_email_scans`` — the ``email_scan_pending_idx`` partial
   index over ``tier < 1``). No row lock is held across the model call; the
   ``tier < new_tier`` compare-and-swap in ``store.upgrade_email_scan`` is the
   race-guard, so a re-run or a concurrent runner is an idempotent no-op.
2. **Re-fetch the body** from IMAP (``email_scan`` stores no body — IMAP stays
   the source of truth) and score it with a local model
   (:data:`precis.mail.inject.TIER1_SYSTEM`). Ambiguous ``suspect`` verdicts
   escalate to a stronger model (tier 2) when one is configured.
3. **Upgrade the verdict** (guarded), and on ``high`` **raise an alert** — the
   surfacing half of the quarantine ladder. The *withholding* half (a ``high``
   body is kept out of every LLM context) is enforced at read time in
   ``handlers/email.py``; this pass only produces the verdict that gates it.

The model call is injected as ``client`` (a ``DispatchClient``); ``fetch_body``
is injectable so tests exercise the pass without a live IMAP server. Nothing is
ever deleted: a ``high`` message stays intact in the mailbox and its listing;
the verdict only escalates *handling*, never removes mail (design §ladder).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from precis.alerts import raise_alert
from precis.mail.account import Account, load_account
from precis.mail.imap import ImapAuthError
from precis.mail.inject import (
    TIER1_SYSTEM,
    TIER1_VERSION,
    build_tier1_prompt,
    parse_tier1_verdict,
)
from precis.mail.message import Message
from precis.mail.message import fetch_one as _default_fetch_one
from precis.store import Store
from precis.store._email_ops import EmailScan

log = logging.getLogger(__name__)

#: ``(account, *, store, folder, uid) -> Message | None``.
FetchBodyFn = Callable[..., Message | None]


def run_inject_scan_pass(
    store: Store,
    *,
    client: Any,
    escalate_client: Any | None = None,
    batch_size: int = 8,
    fetch_body: FetchBodyFn | None = None,
) -> dict[str, int]:
    """One claim→scan→upgrade cycle. Returns ``{claimed, ok, failed}``.

    ``ok`` counts rows advanced to a deeper tier (including a message that has
    since left the mailbox — retired so it stops being pending); ``failed``
    counts rows left pending for a retry (model returned nothing, IMAP error,
    or the account is unavailable).
    """
    fetch = fetch_body or _default_fetch_one
    rows = store.pending_email_scans(limit=batch_size)
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    accounts: dict[str, Account | None] = {}
    ok = failed = 0
    for scan in rows:
        if scan.account not in accounts:
            try:
                accounts[scan.account] = load_account(store, scan.account)
            except ValueError:  # misconfigured IMAP bag — can't re-fetch
                accounts[scan.account] = None
        account = accounts[scan.account]

        if account is None:  # account deleted / misconfigured — can't re-fetch
            _retire(store, scan, note="account-unavailable")
            failed += 1
            continue

        try:
            msg = fetch(account, store=store, folder=scan.folder, uid=scan.uid)
        except (ImapAuthError, OSError) as exc:  # transient — retry next tick
            log.warning(
                "inject_scan: fetch %s/%s failed: %s", scan.account, scan.uid, exc
            )
            failed += 1
            continue

        if msg is None:  # message gone from the mailbox — nothing to read/scan
            _retire(store, scan, note="message-absent")
            ok += 1
            continue

        verdict, tier, evidence = _scan_one(client, escalate_client, scan, msg)
        if verdict is None:  # model unparseable / errored — leave pending
            failed += 1
            continue

        applied = store.upgrade_email_scan(
            scan.account,
            folder=scan.folder,
            uidvalidity=scan.uidvalidity,
            uid=scan.uid,
            verdict=verdict,
            tier=tier,
            evidence=evidence,
        )
        if applied and verdict == "high":
            _raise_quarantine_alert(store, scan, msg, evidence)
        ok += 1

    log.info("inject_scan pass: %d claimed, %d ok, %d failed", len(rows), ok, failed)
    return {"claimed": len(rows), "ok": ok, "failed": failed}


def _judge(client: Any, scan: EmailScan, msg: Message) -> tuple[str | None, str]:
    """One model call → ``(verdict, reason)``; ``None`` verdict on any error."""
    signals = tuple(scan.evidence.get("signals", []) or ())
    prompt = build_tier1_prompt(msg.subject, msg.body_text, tier0_signals=signals)
    try:
        out = client.complete(
            [
                {"role": "system", "content": TIER1_SYSTEM},
                {"role": "user", "content": prompt},
            ]
        )
    except Exception as exc:  # model/proxy down — treat as a scan failure
        log.warning("inject_scan: model call failed: %s", exc)
        return None, ""
    return parse_tier1_verdict(out.text)


def _scan_one(
    client: Any, escalate_client: Any | None, scan: EmailScan, msg: Message
) -> tuple[str | None, int, dict[str, Any]]:
    """Cascade one message: tier-1, then escalate an ambiguous ``suspect``.

    Returns ``(verdict, tier, evidence)`` or ``(None, 0, {})`` when the model
    gave nothing usable (caller leaves the row pending).
    """
    verdict, reason = _judge(client, scan, msg)
    if verdict is None:
        return None, 0, {}

    evidence: dict[str, Any] = {
        "version": TIER1_VERSION,
        "signals": list(scan.evidence.get("signals", []) or ()),
        "tier1": {"verdict": verdict, "reason": reason[:400]},
    }
    tier = 1

    # Escalate only the ambiguous middle — a stronger model breaks the tie.
    if verdict == "suspect" and escalate_client is not None:
        ev, ev_reason = _judge(escalate_client, scan, msg)
        if ev is not None:
            tier = 2
            verdict = ev
            evidence["tier2"] = {"verdict": ev, "reason": ev_reason[:400]}

    return verdict, tier, evidence


def _retire(store: Store, scan: EmailScan, *, note: str) -> None:
    """Advance a scan to tier 1 keeping its verdict — for messages we can no
    longer fetch (gone / account unavailable), so they stop being pending."""
    evidence = dict(scan.evidence)
    evidence["tier1"] = {"verdict": scan.verdict, "reason": note}
    store.upgrade_email_scan(
        scan.account,
        folder=scan.folder,
        uidvalidity=scan.uidvalidity,
        uid=scan.uid,
        verdict=scan.verdict,
        tier=1,
        evidence=evidence,
    )


def _raise_quarantine_alert(
    store: Store, scan: EmailScan, msg: Message, evidence: dict[str, Any]
) -> None:
    """Surface a quarantined (``high``) message — badge is always shown by the
    handler; a ``high`` also raises a (warn) alert so it can't hide silently."""
    detail = (evidence.get("tier2") or evidence.get("tier1") or {}).get("reason", "")
    raise_alert(
        store,
        source="inject_scan",
        fingerprint=f"{scan.account}:{scan.folder}:{scan.uidvalidity}:{scan.uid}",
        title=f"Suspected prompt injection: {(msg.subject or '(no subject)')[:80]}",
        detail=f"{scan.account} {scan.folder}/{scan.uid} from {msg.from_}: {detail}"[
            :500
        ],
        severity="warn",
    )


__all__ = ["run_inject_scan_pass"]
