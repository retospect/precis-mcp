"""Discord PDF attachment -> inbox drop (:mod:`asa_bot.attachments`, gr434187)."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from asa_bot import attachments
from precis.ingest.fetch_sidecar import read_sidecar, sidecar_path
from precis.workers.job_types import datasheet_pull as dl

PDF = b"%PDF-1.7\n" + b"x" * 64


def _att(name="paper.pdf", size=100, ctype="application/pdf", id=81):
    return SimpleNamespace(
        id=id,
        filename=name,
        size=size,
        content_type=ctype,
        url="https://cdn.example/x.pdf",
    )


def _msg(atts, content=""):
    return SimpleNamespace(
        id=42, author=SimpleNamespace(id=123), attachments=atts, content=content
    )


class Rig:
    def __init__(
        self,
        monkeypatch,
        tmp_path: Path,
        body: bytes = PDF,
        ref=None,
        real_download=False,
    ):
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
            return hashlib.sha256(body).hexdigest()

        async def fake_find(dsn, sha):
            return self.refs.pop(0) if len(self.refs) > 1 else self.refs[0]

        if not real_download:
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
    sidecar = read_sidecar(dropped[0])
    assert sidecar is not None
    assert sidecar.source == "discord"
    assert sidecar.ref_id is None
    assert sidecar.discord == {"message_id": "42", "author": "rs", "author_id": "123"}


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


def test_non_pdf_with_chat_text_is_refused(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=None)
    msg = _msg([_att("pic.png", ctype="image/png")], content="what is this?")
    assert rig.run(msg) is True
    assert len(rig.sent) == 1 and "only PDF" in rig.sent[0]


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


@pytest.mark.parametrize("existing_ref", [7, 9])
def test_named_stub_queues_known_hash_without_premature_success(
    monkeypatch, tmp_path, existing_ref
):
    rig = Rig(monkeypatch, tmp_path, ref=existing_ref)
    monkeypatch.setattr(attachments, "POLL_TIMEOUT_S", 0)
    rig.run(_msg([_att()], "Please attach this to pa7 (pa7)."))
    dropped = next(rig.inbox.glob("pa7-*.pdf"))
    sidecar = read_sidecar(dropped)
    assert sidecar is not None and sidecar.ref_id is None
    assert sidecar.discord is not None
    assert sidecar.discord["paper_id"] == "pa7"
    assert "fold into pa7" in rig.sent[0]
    assert all("pa9" not in message for message in rig.sent)
    assert rig.tools == []


def test_named_stub_reports_survivor_after_watcher_finishes(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=[9, 7])

    async def consume(_seconds):
        pdf = next(rig.inbox.glob("pa7-*.pdf"))
        pdf.unlink()
        sidecar_path(pdf).unlink()

    monkeypatch.setattr(attachments.asyncio, "sleep", consume)
    rig.run(_msg([_att()], "pa7"))
    assert "ingested as pa7" in rig.sent[-1]


def test_failed_fold_does_not_report_old_twin_as_ingest_success(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=9)

    async def fail_ingest(_seconds):
        pdf = next(rig.inbox.glob("pa7-*.pdf"))
        errors = rig.inbox / "errors"
        errors.mkdir()
        pdf.rename(errors / pdf.name)
        sidecar_path(pdf).unlink()

    monkeypatch.setattr(attachments.asyncio, "sleep", fail_ingest)
    rig.run(_msg([_att()], "pa7"))
    assert "held as pa9" in rig.sent[-1]
    assert "fold into pa7 was not confirmed" in rig.sent[-1]
    assert rig.tools == []


def test_multiple_distinct_paper_handles_are_refused(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path)
    rig.run(_msg([_att()], "pa7 or pa9"))
    assert "just one pa<id>" in rig.sent[0]
    assert rig.fetched == []


def test_mixed_attachments_refuse_non_pdf_and_queue_distinct_pdfs(
    monkeypatch, tmp_path
):
    rig = Rig(monkeypatch, tmp_path)
    monkeypatch.setattr(attachments, "POLL_TIMEOUT_S", 0)
    rig.run(_msg([_att("pic.png", ctype="image/png"), _att(id=82), _att(id=83)]))
    assert "only PDF" in rig.sent[0]
    assert len(list(rig.inbox.glob("*.pdf"))) == 2
    assert len(rig.fetched) == 2


def test_sidecar_is_visible_before_pdf(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, ref=[None, 7])
    replace = attachments.os.replace

    def checked_replace(source, target):
        if target.suffix == ".pdf":
            sidecar = read_sidecar(target)
            assert sidecar is not None and sidecar.discord is not None
            assert sidecar.discord["author_id"] == "123"
            assert not target.exists()
        replace(source, target)

    monkeypatch.setattr(attachments.os, "replace", checked_replace)
    rig.run(_msg([_att()]))
    assert "ingested as pa7" in rig.sent[-1]


@pytest.mark.parametrize("failure", ["sidecar", "pdf"])
def test_publish_failure_leaves_no_pdf_or_manifest(monkeypatch, tmp_path, failure):
    rig = Rig(monkeypatch, tmp_path)
    if failure == "sidecar":
        monkeypatch.setattr(attachments, "write_sidecar", lambda *a, **kw: None)
    else:
        replace = attachments.os.replace

        def fail_pdf(source, target):
            if target.suffix == ".pdf":
                raise OSError("disk full")
            replace(source, target)

        monkeypatch.setattr(attachments.os, "replace", fail_pdf)
    rig.run(_msg([_att()]))
    assert "could not write" in rig.sent[-1]
    assert not list(rig.inbox.glob("*.pdf*"))
    assert not list((rig.inbox / ".staging").iterdir())


@pytest.mark.parametrize("case", ["pdf", "magic", "stream_size", "header_size", "ssrf"])
def test_real_downloader_uses_safe_stream_and_enforces_limits(
    monkeypatch, tmp_path, case
):
    rig = Rig(monkeypatch, tmp_path, ref=[None, 7], real_download=True)
    monkeypatch.setattr(dl, "MAX_BYTES", 100)
    monkeypatch.setattr(dl, "http_client", lambda **kwargs: nullcontext(object()))
    calls = []

    def safe_stream(client, method, url):
        calls.append((method, url))
        if case == "ssrf":
            raise dl.SsrfBlocked("blocked test destination")
        body = b"<html>no PDF</html>" if case == "magic" else PDF
        chunks = [body, b"x" * 100] if case == "stream_size" else [body]
        return nullcontext(
            SimpleNamespace(
                status_code=200,
                headers={"Content-Length": "101"} if case == "header_size" else {},
                url=url,
                iter_bytes=lambda chunk_size: iter(chunks),
            )
        )

    monkeypatch.setattr(dl, "safe_stream", safe_stream)
    rig.run(_msg([_att(size=1)]))
    assert calls == [("GET", "https://cdn.example/x.pdf")]
    if case == "pdf":
        assert next(rig.inbox.glob("*.pdf")).read_bytes() == PDF
        assert "pa7" in rig.sent[-1]
    else:
        assert "Refused" in rig.sent[-1]
        assert not list(rig.inbox.glob("*.pdf*"))
    assert not list((rig.inbox / ".staging").iterdir())
