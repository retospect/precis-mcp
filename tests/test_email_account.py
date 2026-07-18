"""``email_account`` store CRUD against a real DB (email-kind slice 1).

Exercises the :class:`EmailAccountMixin` upsert/get/list/delete + the poll
high-water advance, including the invariant that re-configuring an account
(``upsert``) never rewinds the poll cursor.
"""

from __future__ import annotations


def test_upsert_and_get_roundtrip(store) -> None:
    store.upsert_email_account(
        "rs@retostamm.com",
        secret_name="email.rs@retostamm.com.password",
        config={"imap": {"host": "mail.retostamm.com", "port": 993}},
    )
    row = store.get_email_account("rs@retostamm.com")
    assert row is not None
    assert row.enabled is True
    assert row.secret_name == "email.rs@retostamm.com.password"
    assert row.last_uid == 0
    assert row.uidvalidity is None
    assert row.config["imap"]["host"] == "mail.retostamm.com"


def test_get_missing_returns_none(store) -> None:
    assert store.get_email_account("nobody@nowhere.test") is None


def test_list_and_enabled_filter(store) -> None:
    store.upsert_email_account("a@x.test", secret_name="s.a", config={}, enabled=True)
    store.upsert_email_account("b@x.test", secret_name="s.b", config={}, enabled=False)
    all_rows = store.list_email_accounts()
    assert {r.account for r in all_rows} == {"a@x.test", "b@x.test"}
    enabled = store.list_email_accounts(enabled_only=True)
    assert [r.account for r in enabled] == ["a@x.test"]


def test_delete(store) -> None:
    store.upsert_email_account("gone@x.test", secret_name="s", config={})
    assert store.delete_email_account("gone@x.test") is True
    assert store.get_email_account("gone@x.test") is None
    assert store.delete_email_account("gone@x.test") is False  # idempotent


def test_highwater_advance(store) -> None:
    store.upsert_email_account("hw@x.test", secret_name="s", config={})
    store.set_email_account_highwater("hw@x.test", last_uid=42, uidvalidity=1717)
    row = store.get_email_account("hw@x.test")
    assert row is not None and row.last_uid == 42 and row.uidvalidity == 1717


def test_reconfigure_preserves_highwater(store) -> None:
    """Re-running `email add` must not rewind the poll cursor."""
    store.upsert_email_account(
        "keep@x.test", secret_name="s", config={"poll_seconds": 900}
    )
    store.set_email_account_highwater("keep@x.test", last_uid=100, uidvalidity=5)

    # Re-configure (different config, same account).
    store.upsert_email_account(
        "keep@x.test", secret_name="s", config={"poll_seconds": 300}
    )
    row = store.get_email_account("keep@x.test")
    assert row is not None
    assert row.config["poll_seconds"] == 300  # config updated
    assert row.last_uid == 100 and row.uidvalidity == 5  # cursor untouched
