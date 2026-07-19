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


def test_due_accounts_survives_large_backoff(store) -> None:
    # A permanently-failing account must not overflow the backoff arithmetic
    # (poll_seconds * 2^consecutive_errors) and crash due_email_accounts for
    # every account. 2^60 * 900 blows past int4 if cast before the least() cap.
    _seed(store, "broken@x.test", poll_seconds=900)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE email_account SET consecutive_errors = 60, "
            "last_polled_at = now() WHERE account = %s",
            ("broken@x.test",),
        )
    due = {a.account for a in store.due_email_accounts()}  # must not raise
    assert "broken@x.test" not in due  # backed off (capped at 1 day), not due now


def test_due_accounts_skips_disabled(store) -> None:
    store.upsert_email_account(
        "off@x.test", secret_name="s", config={"imap": {"host": "h"}}, enabled=False
    )
    due = {a.account for a in store.due_email_accounts()}
    assert "off@x.test" not in due


# ── slice 4: guarded verdict upgrade + pending claim + badge lookup ─────


def _seed_scan(
    store, *, uid, verdict="suspect", tier=0, folder="INBOX", uidv=1
) -> None:
    _seed(store)
    store.record_email_scan(
        "rs@x.test",
        folder=folder,
        uidvalidity=uidv,
        uid=uid,
        verdict=verdict,
        tier=tier,
        evidence={"signals": ["ignore-previous"]},
    )


def test_upgrade_deepens_a_tier0_verdict(store) -> None:
    _seed_scan(store, uid=5, verdict="suspect", tier=0)
    moved = store.upgrade_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=5,
        verdict="high",
        tier=1,
        evidence={"tier1": {"verdict": "high"}},
    )
    assert moved is True
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=5)
    assert row is not None and row.verdict == "high" and row.tier == 1


def test_upgrade_cas_never_clobbers_a_deeper_verdict(store) -> None:
    # A tier-2 verdict is in place; a stray tier-1 write must not overwrite it.
    _seed_scan(store, uid=6, verdict="high", tier=2)
    moved = store.upgrade_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=6,
        verdict="clean",
        tier=1,
        evidence={},
    )
    assert moved is False  # tier < 1 guard: 2 is not < 1, so no-op
    row = store.get_email_scan("rs@x.test", folder="INBOX", uidvalidity=1, uid=6)
    assert row is not None and row.verdict == "high" and row.tier == 2


def test_upgrade_is_idempotent_at_same_tier(store) -> None:
    _seed_scan(store, uid=7, verdict="suspect", tier=1)
    moved = store.upgrade_email_scan(
        "rs@x.test",
        folder="INBOX",
        uidvalidity=1,
        uid=7,
        verdict="high",
        tier=1,
        evidence={},
    )
    assert moved is False  # tier 1 is not < 1 — a re-run does nothing


def test_pending_scans_returns_only_tier0(store) -> None:
    _seed_scan(store, uid=10, tier=0)
    _seed_scan(store, uid=11, tier=0)
    _seed_scan(store, uid=12, tier=1)  # already deep — excluded
    pending = {s.uid for s in store.pending_email_scans(limit=10)}
    assert pending == {10, 11}


def test_pending_scans_respects_limit(store) -> None:
    for u in range(20, 25):
        _seed_scan(store, uid=u, tier=0)
    assert len(store.pending_email_scans(limit=3)) == 3


def test_list_verdicts_maps_uid_to_verdict(store) -> None:
    _seed_scan(store, uid=30, verdict="high", tier=1)
    _seed_scan(store, uid=31, verdict="clean", tier=1)
    got = store.list_email_scan_verdicts(
        "rs@x.test", folder="INBOX", uidvalidity=1, uids=[30, 31, 99]
    )
    assert got == {30: "high", 31: "clean"}  # 99 unscanned → absent


def test_list_verdicts_is_uidvalidity_scoped(store) -> None:
    _seed_scan(store, uid=40, verdict="high", tier=1, uidv=1)
    # A resync (new uidvalidity) must not leak the stale verdict.
    got = store.list_email_scan_verdicts(
        "rs@x.test", folder="INBOX", uidvalidity=2, uids=[40]
    )
    assert got == {}


def test_list_verdicts_empty_uids_is_empty(store) -> None:
    assert (
        store.list_email_scan_verdicts(
            "rs@x.test", folder="INBOX", uidvalidity=1, uids=[]
        )
        == {}
    )
