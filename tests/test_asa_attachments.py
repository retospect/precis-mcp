"""Discord PDF attachment -> inbox drop (:mod:`asa_bot.attachments`, gr434187)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from asa_bot import attachments
from precis.workers.job_types import datasheet_pull as dl

PDF = b"%PDF-1.7\n" + b"x" * 64


def _att(name="paper.pdf", size=100, ctype="application/pdf"):
    return SimpleNamespace(
        filename=name, size=size, content_type=ctype, url="https://cdn.example/x.pdf"
    )


def _msg(atts, content=""):
    return SimpleNamespace(id=42, attachments=atts, content=content)


class Rig:
    def __init__(self, monkeypatch, tmp_path: Path, body: bytes = PDF, ref=None):
        self.inbox = tmp_path / "inbox"
        self.inbox.mkdir()
        self.sent: list[str] = []
        self.tools: list[tuple[str, dict]] = []
        self.fetched: list[str] = []
        self.refs = [ref] if not isinstance(ref, list) else list(ref)

        def fake_dl(url, staged):
            self.fetched.append(url)
            if not body.startswith(b"%PDF"):
                raise dl._Failed("not_pdf", f"head={body[:8]!r}")
            staged.write_bytes(body)
            return url, hashlib.sha256(body).hexdigest()

        async def fake_find(dsn, sha):
            return self.refs.pop(0) if len(self.refs) > 1 else self.refs[0]

        monkeypatch.setattr(attachments, "_download_sync", fake_dl)
        monkeypatch.setattr(attachments, "find_ref_id", fake_find)

    async def send(self, text):
        self.sent.append(text)

    async def call_tool(self, name, args):
        self.tools.append((name, args))
        return "ok"

    def run(self, message, abbrev="rs", inbox="default"):
        return asyncio.run(
            attachments.handle_message_attachments(
                message,
                abbrev=abbrev,
                dsn="dsn",
                call_tool=self.call_tool,
                send=self.send,
                inbox=self.inbox if inbox == "default" else inbox,
            )
        )


def test_pdf_lands_in_inbox_and_replies_with_handle_attributed(monkeypatch, tmp_path):
    # find_ref_id: None before the drop (dedup probe), None once, then minted.
    rig = Rig(monkeypatch, tmp_path, ref=[None, None, 7])
    monkeypatch.setattr(attachments, "POLL_INTERVAL_S", 0)

    async def go():
        await attachments.handle_message_attachments(
            _msg([_att()]),
            abbrev="rs",
            dsn="dsn",
            call_tool=rig.call_tool,
            send=rig.send,
            inbox=rig.inbox,
        )

    asyncio.run(go())
    dropped = list(rig.inbox.glob("discord-42-*.pdf"))
    assert [p.read_bytes() for p in dropped] == [PDF]
    assert not list((rig.inbox / ".staging").iterdir())
    assert rig.tools == [("tag", {"kind": "paper", "id": "pa7", "add": ["user:rs"]})]
    assert "queued" in rig.sent[0]
    assert "pa7" in rig.sent[-1]


def test_known_hash_is_not_redropped(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=9)
    assert rig.run(_msg([_att()])) is True
    assert list(rig.inbox.glob("*.pdf")) == []
    assert "pa9" in rig.sent[-1] and "already" in rig.sent[-1]
    assert rig.tools == []


def test_non_pdf_attachment_only_message_is_refused(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    assert rig.run(_msg([_att("pic.png", ctype="image/png")])) is True
    assert "only PDF" in rig.sent[0]
    assert rig.fetched == []


def test_non_pdf_with_chat_text_falls_through_to_the_turn(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    msg = _msg([_att("pic.png", ctype="image/png")], content="what is this?")
    assert rig.run(msg) is False
    assert rig.sent == []


def test_oversize_declared_size_is_refused_before_fetch(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    assert rig.run(_msg([_att(size=dl.MAX_BYTES + 1)])) is True
    assert "cap" in rig.sent[0]
    assert rig.fetched == []
    assert list(rig.inbox.glob("*.pdf")) == []


def test_bad_magic_bytes_refused_and_nothing_dropped(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, body=b"<html>nope</html>", ref=None)
    rig.run(_msg([_att()]))
    assert "%PDF" in rig.sent[0]
    assert list(rig.inbox.glob("*.pdf")) == []
    assert rig.tools == []


def test_unmapped_speaker_is_refused_without_fetching(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    rig.run(_msg([_att()]), abbrev=None)
    assert "/account" in rig.sent[0]
    assert rig.fetched == []


def test_unconfigured_inbox_is_refused(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    rig.run(_msg([_att()]), inbox=None)
    assert "no ingest inbox" in rig.sent[0]
    assert rig.fetched == []


def test_ingest_never_appearing_reports_timeout(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    monkeypatch.setattr(attachments, "POLL_INTERVAL_S", 0)

    async def go():
        await attachments.ingest_attachment(
            _att(),
            message_id=42,
            abbrev="rs",
            dsn="dsn",
            inbox=rig.inbox,
            call_tool=rig.call_tool,
            send=rig.send,
            poll_interval_s=1.0,
            poll_timeout_s=2.0,
        )

    real_sleep = asyncio.sleep

    async def fast(_s):
        await real_sleep(0)

    monkeypatch.setattr(attachments.asyncio, "sleep", fast)
    asyncio.run(go())
    assert "no paper after" in rig.sent[-1]
    assert rig.tools == []


def test_tag_failure_still_reports_the_handle(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=[None, 5])

    async def boom(name, args):
        raise RuntimeError("down")

    monkeypatch.setattr(rig, "call_tool", boom)
    monkeypatch.setattr(attachments, "POLL_INTERVAL_S", 0)
    rig.run(_msg([_att()]))
    assert "pa5" in rig.sent[-1] and "could not tag" in rig.sent[-1]
