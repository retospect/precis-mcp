"""Message parsing + fetch-response decoding — pure (email-kind slice 2).

No IMAP: exercises the RFC822 parse, the HTML fallback, MIME-header decode,
the imaplib FETCH-response payload extraction, and the id→(folder, uid)
routing. Live IMAP list/fetch is covered structurally by the handler test.
"""

from __future__ import annotations

from precis.handlers.email import _parse_target, _short_from
from precis.mail.message import (
    _iter_fetch_payloads,
    _strip_html,
    parse_message,
)

_PLAIN = (
    b"From: Alice Example <alice@example.com>\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: Hello there\r\n"
    b"Date: Mon, 01 Jan 2026 10:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"This is the body.\r\nSecond line.\r\n"
)

_HTML_ONLY = (
    b"From: news@example.com\r\n"
    b"Subject: HTML news\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<html><body><p>Hi <b>there</b></p>"
    b"<script>evil()</script></body></html>\r\n"
)

_ENCODED_SUBJECT = (
    b"From: x@example.com\r\n"
    b"Subject: =?utf-8?B?SGVsbG8gV29ybGQ=?=\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_parse_plain_message() -> None:
    msg = parse_message(_PLAIN, folder="INBOX", uid=5)
    assert msg.uid == 5 and msg.folder == "INBOX"
    assert "Alice" in msg.from_
    assert msg.to == "bob@example.com"
    assert msg.subject == "Hello there"
    assert "This is the body." in msg.body_text
    assert msg.truncated_html is False


def test_parse_html_only_strips_tags_and_scripts() -> None:
    msg = parse_message(_HTML_ONLY, folder="INBOX", uid=9)
    assert msg.truncated_html is True
    assert "Hi" in msg.body_text and "there" in msg.body_text
    assert "<" not in msg.body_text  # tags gone
    assert "evil()" not in msg.body_text  # script dropped


def test_parse_decodes_mime_subject() -> None:
    msg = parse_message(_ENCODED_SUBJECT, folder="INBOX", uid=1)
    assert msg.subject == "Hello World"


def test_strip_html_collapses_whitespace() -> None:
    out = _strip_html("<p>one</p>\n\n\n<p>two</p>")
    assert "one" in out and "two" in out
    assert "\n\n\n" not in out


def test_iter_fetch_payloads_extracts_uid_and_body() -> None:
    data = [
        (b"1 (UID 5 BODY[HEADER] {14}", b"Subject: X\r\n\r\n"),
        b")",
        (b"2 (UID 6 BODY[HEADER] {14}", b"Subject: Y\r\n\r\n"),
        b")",
    ]
    pairs = _iter_fetch_payloads(data)
    assert pairs == [(5, b"Subject: X\r\n\r\n"), (6, b"Subject: Y\r\n\r\n")]


def test_iter_fetch_payloads_ignores_separators_and_bad_items() -> None:
    data = [b")", (b"no-uid-here", b"payload"), (b"3 (UID 7", b"ok")]
    assert _iter_fetch_payloads(data) == [(7, b"ok")]


# ── id routing ─────────────────────────────────────────────────────────


def test_parse_target_message() -> None:
    assert _parse_target("INBOX/12345") == ("INBOX", 12345)


def test_parse_target_nested_folder_message() -> None:
    assert _parse_target("INBOX/Lists/98") == ("INBOX/Lists", 98)


def test_parse_target_folder_listing() -> None:
    assert _parse_target("INBOX") == ("INBOX", None)
    assert _parse_target("INBOX/Lists") == ("INBOX/Lists", None)


def test_parse_target_strips_leading_slash() -> None:
    assert _parse_target("/INBOX") == ("INBOX", None)
    assert _parse_target("/INBOX/7") == ("INBOX", 7)


def test_short_from_prefers_display_name() -> None:
    assert _short_from("Alice Example <alice@example.com>") == "Alice Example"
    assert _short_from("bob@example.com") == "bob@example.com"
    assert _short_from("") == "(unknown)"
