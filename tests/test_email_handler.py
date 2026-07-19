"""EmailHandler routing + account resolution (email-kind slice 2).

Uses the real store for account rows but monkeypatches the IMAP list/fetch so
no server is needed — the point is the handler's dispatch (overview / folder /
message) and account resolution, not imaplib.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.email import EmailHandler
from precis.mail import message as mail_message
from precis.mail.message import Message, MessageHeader


def _handler(store) -> EmailHandler:
    return EmailHandler(hub=Hub(store=store))


def _seed(store, address="rs@retostamm.com", enabled=True) -> None:
    store.upsert_email_account(
        address,
        secret_name=f"email.{address}.password",
        config={"imap": {"host": "mail.example.test"}},
        enabled=enabled,
    )


def test_no_accounts_is_badinput(store) -> None:
    with pytest.raises(BadInput, match="no accounts configured"):
        _handler(store).get()


def test_multiple_accounts_requires_account_param(store) -> None:
    _seed(store, "a@x.test")
    _seed(store, "b@x.test")
    with pytest.raises(BadInput, match="multiple accounts"):
        _handler(store).get()


def test_unknown_account_is_badinput(store) -> None:
    _seed(store)
    with pytest.raises(BadInput, match="no account"):
        _handler(store).get(account="nope@x.test")


def test_overview_lists_primary_folder(store, monkeypatch) -> None:
    _seed(store)
    monkeypatch.setattr(
        mail_message,
        "list_recent",
        lambda acct, *, store, folder, limit: [
            MessageHeader(uid=7, from_="Alice <a@x>", subject="Hi", date="today"),
        ],
    )
    resp = _handler(store).get()
    assert "rs@retostamm.com" in resp.body
    assert "INBOX/7" in resp.body  # message id token is copy-pasteable
    assert "Hi" in resp.body


def test_folder_listing(store, monkeypatch) -> None:
    _seed(store)
    monkeypatch.setattr(
        mail_message,
        "list_recent",
        lambda acct, *, store, folder, limit: (
            [MessageHeader(uid=3, from_="x@y", subject="S", date="d")]
            if folder == "Lists"
            else []
        ),
    )
    resp = _handler(store).get(id="Lists")
    assert "Lists/3" in resp.body


def test_read_message(store, monkeypatch) -> None:
    _seed(store)
    monkeypatch.setattr(
        mail_message,
        "fetch_one",
        lambda acct, *, store, folder, uid: Message(
            uid=uid,
            folder=folder,
            from_="Alice <a@x>",
            to="me@x",
            subject="The subject",
            date="today",
            body_text="Hello world body.",
            truncated_html=False,
        ),
    )
    resp = _handler(store).get(id="INBOX/42")
    assert "The subject" in resp.body
    assert "Hello world body." in resp.body
    assert "`INBOX/42`" in resp.body


def test_read_missing_message_is_notfound(store, monkeypatch) -> None:
    _seed(store)
    monkeypatch.setattr(
        mail_message, "fetch_one", lambda acct, *, store, folder, uid: None
    )
    with pytest.raises(NotFound, match="no message"):
        _handler(store).get(id="INBOX/999")


def _msg(uid, folder, *, body):
    return Message(
        uid=uid,
        folder=folder,
        from_="Bad Actor <b@evil.test>",
        to="me@x",
        subject="Act now",
        date="today",
        body_text=body,
        truncated_html=False,
    )


def _arm_scan(store, *, uid, verdict, address="rs@retostamm.com", uidv=1000):
    """Give the account a uidvalidity cursor + a scan verdict for one uid."""
    store.set_email_account_highwater(address, last_uid=uid, uidvalidity=uidv)
    store.record_email_scan(
        address,
        folder="INBOX",
        uidvalidity=uidv,
        uid=uid,
        verdict=verdict,
        tier=1,
        evidence={},
    )


def test_read_high_withholds_body(store, monkeypatch) -> None:
    _seed(store)
    _arm_scan(store, uid=42, verdict="high")
    monkeypatch.setattr(
        mail_message,
        "fetch_one",
        lambda acct, *, store, folder, uid: _msg(
            uid, folder, body="SECRET-INJECTION-PAYLOAD do X"
        ),
    )
    resp = _handler(store).get(id="INBOX/42")
    assert "SECRET-INJECTION-PAYLOAD" not in resp.body  # body kept out of context
    assert "withheld" in resp.body.lower()
    assert "🚫" in resp.body


def test_read_suspect_shows_body_with_banner(store, monkeypatch) -> None:
    _seed(store)
    _arm_scan(store, uid=43, verdict="suspect")
    monkeypatch.setattr(
        mail_message,
        "fetch_one",
        lambda acct, *, store, folder, uid: _msg(uid, folder, body="borderline body"),
    )
    resp = _handler(store).get(id="INBOX/43")
    assert "borderline body" in resp.body  # suspect still passes
    assert "untrusted" in resp.body.lower()
    assert "⚠" in resp.body


def test_read_clean_renders_normally(store, monkeypatch) -> None:
    _seed(store)
    _arm_scan(store, uid=44, verdict="clean")
    monkeypatch.setattr(
        mail_message,
        "fetch_one",
        lambda acct, *, store, folder, uid: _msg(uid, folder, body="ordinary body"),
    )
    resp = _handler(store).get(id="INBOX/44")
    assert "ordinary body" in resp.body
    assert "🚫" not in resp.body and "withheld" not in resp.body.lower()


def test_overview_badges_flagged_messages(store, monkeypatch) -> None:
    _seed(store)
    _arm_scan(store, uid=7, verdict="high")
    monkeypatch.setattr(
        mail_message,
        "list_recent",
        lambda acct, *, store, folder, limit: [
            MessageHeader(uid=7, from_="b@evil", subject="Click", date="today"),
        ],
    )
    resp = _handler(store).get()
    assert "🚫" in resp.body  # quarantine badge in the listing
    assert "INBOX/7" in resp.body


def test_account_param_selects_among_many(store, monkeypatch) -> None:
    _seed(store, "a@x.test")
    _seed(store, "b@x.test")
    seen: dict = {}

    def _list(acct, *, store, folder, limit):
        seen["addr"] = acct.address
        return []

    monkeypatch.setattr(mail_message, "list_recent", _list)
    _handler(store).get(account="b@x.test")
    assert seen["addr"] == "b@x.test"
