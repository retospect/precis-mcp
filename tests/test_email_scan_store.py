"""email_scan + poll-bookkeeping store methods against a real DB (slice 3)."""

from __future__ import annotations


def _seed(store, account="rs@x.test", poll_seconds=None) -> None:
    cfg = {"imap": {"host": "mail.x.test"}}
    if poll_seconds is not None:
        cfg["poll_seconds"] = poll_seconds
    store.upsert_email_account(
        account, secret_name=f"email.{account}.password", config=cfg
    )


def test_record_and_get_scan(store) -> None:
    _seed(store)
    inserted = store.record_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1717,
        uid=42,
        verdict="suspect",
        tier=0,
        evidence={"signals": ["ignore-previous"], "version": 1},
    )
    assert inserted is True
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1717, uid=42)
    assert row is not None
    assert row.verdict == "suspect"
    assert row.tier == 0
    assert row.evidence["signals"] == ["ignore-previous"]


def test_record_scan_is_insert_if_absent(store) -> None:
    # record_email_scan is the tier-0 writer: insert-if-absent. It never
    # clobbers an existing row (a later tier-1 pass upgrades via its own guarded
    # update, slice 4), so a second call for the same message is a no-op.
    _seed(store)
    first = store.record_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=7,
        verdict="suspect",
        tier=0,
        evidence={"signals": ["role-reassign"]},
    )
    again = store.record_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=7,
        verdict="clean",  # a re-scan that would have downgraded it
        tier=0,
        evidence={},
    )
    assert first is True and again is False  # first inserted, second no-op
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=7)
    assert row is not None and row.verdict == "suspect"  # first verdict kept
    assert row.evidence["signals"] == ["role-reassign"]


def test_get_missing_scan_is_none(store) -> None:
    assert (
        store.get_email_scan("no@x.test", folder="INBOX", uidvalidity=1, uid=1) is None
    )


def test_record_poll_ok_clears_errors(store) -> None:
    _seed(store)
    store.record_email_poll("rs@x.test", status="error: boom")
    store.record_email_poll("rs@x.test", status="error: boom again")
    row = store.get_email_account("rs@x.test")
    assert row is not None and row.consecutive_errors == 2
    assert row.last_status.startswith("error")

    store.record_email_poll("rs@x.test", status="ok")
    row = store.get_email_account("rs@x.test")
    assert row is not None and row.consecutive_errors == 0
    assert row.last_status == "ok"
    assert row.last_polled_at is not None


def test_due_accounts_never_polled_is_due(store) -> None:
    _seed(store, "fresh@x.test")
    due = store.due_email_accounts()
    assert "fresh@x.test" in {a.account for a in due}


def test_due_accounts_recently_polled_not_due(store) -> None:
    _seed(store, "recent@x.test", poll_seconds=3600)
    store.record_email_poll("recent@x.test", status="ok")  # last_polled_at = now()
    due = {a.account for a in store.due_email_accounts()}
    assert "recent@x.test" not in due


def test_due_accounts_skips_disabled(store) -> None:
    store.upsert_email_account(
        "off@x.test", secret_name="s", config={"imap": {"host": "h"}}, enabled=False
    )
    due = {a.account for a in store.due_email_accounts()}
    assert "off@x.test" not in due
