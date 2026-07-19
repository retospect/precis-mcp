"""inject_scan pass — tier-1/2 model scan + quarantine ladder (slice 4).

Real store for the ``email_scan`` rows; the model ``client`` and the IMAP
``fetch_body`` are injected so no proxy / server is needed. Exercises the
control flow: claim tier-0 → score → guarded upgrade → alert on ``high``,
escalation of ``suspect``, the CAS guard, and the retry / retire edges.
"""

from __future__ import annotations

from precis.alerts import list_open_alerts
from precis.mail.imap import ImapAuthError
from precis.mail.message import Message
from precis.workers.inject_scan import run_inject_scan_pass


class _Client:
    """Stub model client: returns a fixed JSON verdict; records call count."""

    def __init__(self, verdict: str, reason: str = "because") -> None:
        self._verdict = verdict
        self.reason = reason
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return _Out(f'{{"verdict": "{self._verdict}", "reason": "{self.reason}"}}')


class _Out:
    def __init__(self, text: str) -> None:
        self.text = text


def _msg(uid: int, *, body: str = "the body", subject: str = "hi") -> Message:
    return Message(
        uid=uid,
        folder="INBOX",
        from_="Sender <s@x>",
        to="me@x",
        subject=subject,
        date="today",
        body_text=body,
        truncated_html=False,
    )


def _seed(store, account="rs@x.test") -> None:
    store.upsert_email_account(
        account,
        secret_name=f"email.{account}.password",
        config={"imap": {"host": "mail.x.test"}},
    )


def _flag(store, *, uid, verdict="suspect", account="rs@x.test", uidv=1) -> None:
    """A tier-0 verdict awaiting a deep scan."""
    store.record_email_scan(
        account,
        folder="INBOX",
        uidvalidity=uidv,
        uid=uid,
        verdict=verdict,
        tier=0,
        evidence={"signals": ["ignore-previous"]},
    )


def _fetch(bodies: dict[int, Message | None]):
    def _fn(account, *, store, folder, uid):
        return bodies.get(uid)

    return _fn


def test_no_pending_is_noop(store) -> None:
    _seed(store)
    r = run_inject_scan_pass(store, client=_Client("high"), fetch_body=_fetch({}))
    assert r == {"claimed": 0, "ok": 0, "failed": 0}


def test_scan_upgrades_and_records_tier1(store) -> None:
    _seed(store)
    _flag(store, uid=5, verdict="suspect")
    r = run_inject_scan_pass(
        store, client=_Client("clean"), fetch_body=_fetch({5: _msg(5)})
    )
    assert r["ok"] == 1 and r["failed"] == 0
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=5)
    assert row is not None and row.verdict == "clean" and row.tier == 1
    assert row.evidence["tier1"]["verdict"] == "clean"
    # No longer pending.
    assert store.pending_email_scans(limit=10) == []


def test_high_verdict_raises_alert(store) -> None:
    _seed(store)
    _flag(store, uid=6, verdict="suspect")
    run_inject_scan_pass(store, client=_Client("high"), fetch_body=_fetch({6: _msg(6)}))
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=6)
    assert row is not None and row.verdict == "high"
    assert any(a["source"] == "inject_scan" for a in list_open_alerts(store))


def test_suspect_escalates_to_tier2(store) -> None:
    _seed(store)
    _flag(store, uid=7, verdict="suspect")
    primary = _Client("suspect")  # tier-1 stays ambiguous
    escalate = _Client("high")  # tier-2 breaks the tie
    run_inject_scan_pass(
        store,
        client=primary,
        escalate_client=escalate,
        fetch_body=_fetch({7: _msg(7)}),
    )
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=7)
    assert row is not None and row.verdict == "high" and row.tier == 2
    assert escalate.calls == 1  # escalated exactly once
    assert row.evidence["tier2"]["verdict"] == "high"


def test_clean_tier1_does_not_escalate(store) -> None:
    _seed(store)
    _flag(store, uid=8, verdict="suspect")
    escalate = _Client("high")
    run_inject_scan_pass(
        store,
        client=_Client("clean"),
        escalate_client=escalate,
        fetch_body=_fetch({8: _msg(8)}),
    )
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=8)
    assert row is not None and row.verdict == "clean" and row.tier == 1
    assert escalate.calls == 0  # only ambiguous verdicts escalate


def test_unparseable_model_leaves_row_pending(store) -> None:
    _seed(store)
    _flag(store, uid=9, verdict="suspect")

    class _Bad:
        def complete(self, messages):
            return _Out("i refuse to answer in json")

    r = run_inject_scan_pass(store, client=_Bad(), fetch_body=_fetch({9: _msg(9)}))
    assert r["failed"] == 1 and r["ok"] == 0
    # Still tier-0, still pending for a retry — never silently downgraded.
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=9)
    assert row is not None and row.tier == 0
    assert {s.uid for s in store.pending_email_scans(limit=10)} == {9}


def test_imap_error_leaves_row_pending(store) -> None:
    _seed(store)
    _flag(store, uid=11, verdict="suspect")

    def _boom(account, *, store, folder, uid):
        raise ImapAuthError("nope")

    r = run_inject_scan_pass(store, client=_Client("high"), fetch_body=_boom)
    assert r["failed"] == 1
    assert {s.uid for s in store.pending_email_scans(limit=10)} == {11}


def test_absent_message_is_retired_keeping_verdict(store) -> None:
    _seed(store)
    _flag(store, uid=12, verdict="suspect")
    # Message gone from the mailbox since tier-0: fetch returns None.
    r = run_inject_scan_pass(
        store, client=_Client("high"), fetch_body=_fetch({12: None})
    )
    assert r["ok"] == 1
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=12)
    # Retired at tier-1 (no longer pending) but keeps the coarse tier-0 verdict.
    assert row is not None and row.tier == 1 and row.verdict == "suspect"
    assert store.pending_email_scans(limit=10) == []


def test_cas_guard_does_not_clobber_existing_tier2(store) -> None:
    _seed(store)
    # A tier-2 verdict already present; the pending index won't pick it (tier>=1),
    # but assert directly that a tier-1 write can't overwrite it.
    store.record_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=13,
        verdict="high",
        tier=2,
        evidence={},
    )
    r = run_inject_scan_pass(
        store, client=_Client("clean"), fetch_body=_fetch({13: _msg(13)})
    )
    assert r == {"claimed": 0, "ok": 0, "failed": 0}  # not even claimed
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=13)
    assert row is not None and row.verdict == "high" and row.tier == 2
