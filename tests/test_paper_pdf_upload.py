"""Slice 6 (gr517423): the signed-upload doors — ``put(args=
{'pdf_upload_link': True})`` minting a single-use POST URL, and
``put(args={'pdf_upload': '<sha>'})`` queueing a ``paper_pull`` attach
of the staged file — plus the staging sweep.

Staging lives in ``PRECIS_UPLOAD_STAGING``, a dir OUTSIDE the watched
inbox (feat-a2 MF1: the out-of-repo acatome-extract watcher scans
``PRECIS_WATCH_INBOX`` and would fold a staged PDF as an unbound
paper). The fixtures here mirror the deploy shape — staging a sibling
of the inbox on the same filesystem, so ``paper_pull``'s
``os.replace`` is a rename.

The web half (``POST /files/upload/{ref_id}``) is covered in
``tests/precis_web/test_files.py``; here the staging is faked by writing
the ``<ref_id>-<sha>.pdf`` file directly — the file IS the
record the route produces (no ``pdfs`` row at upload time).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.paper import PaperHandler
from precis.ingest import pdf_upload
from precis.ingest.fetch_sidecar import read_sidecar
from precis.workers.job_types import paper_pull


def _make_pdf(pages: list[str]) -> bytes:
    """A real text PDF, one string per page (fitz reads it back)."""
    import fitz

    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return bytes(data)


_BODY = ["A stub paper\nDOI 10.1234/stub", "page two", "page three"]
_PDF = _make_pdf(_BODY)
_SHA = hashlib.sha256(_PDF).hexdigest()
_OTHER_SHA = "cd" * 32


@pytest.fixture
def link_key(monkeypatch):
    """The token key resolves env → vault → file; env is enough."""
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "upload-test-key")


@pytest.fixture
def paper(store):
    # Registered the way boot does it: the job handler's parent guard asks
    # the hub which kinds opted into ``can_own_jobs``.
    hub = Hub(store=store)
    handler = PaperHandler(hub=hub)
    handler.hub = hub
    hub.register_handler("paper", handler)
    hub.register_ability("paper", "put", None, lambda **kw: None)
    return handler


def _stub(
    store, *, body: bool = False, pdf: bool = False, doi: str = "10.1234/stub"
) -> int:
    ref_id, _ = store.upsert_stub_paper(
        identifiers=[("doi", doi)], title="A stub paper"
    )
    with store.pool.connection() as conn:
        if body:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                "VALUES (%s, 0, 'paragraph', 'body', '{}'::jsonb)",
                (ref_id,),
            )
        if pdf:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/stub') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                ("ab" * 32, "ab" * 32),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                ("ab" * 32, ref_id),
            )
        conn.commit()
    return ref_id


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    """``(inbox, staging)`` — the deploy shape: staging a SIBLING of the
    watch inbox on the same filesystem."""
    inbox = tmp_path / "inbox"
    staging = tmp_path / "staging"
    inbox.mkdir()
    staging.mkdir()
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(inbox))
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    return inbox, staging


@pytest.fixture
def inbox(dirs):
    return dirs[0]


@pytest.fixture
def staging(dirs):
    return dirs[1]


def _stage_upload(staging: Path, ref_id: int, sha: str = _SHA, data: bytes = _PDF):
    """What ``POST /files/upload`` leaves behind: the staged
    ``<ref_id>-<sha>.pdf`` file — the whole record (no ``pdfs`` row is
    written at upload time; ingest creates it when the attach folds)."""
    staged = staging / pdf_upload.staged_name(ref_id, sha)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    return staged


def _jobs(store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT meta->'params' FROM refs WHERE kind = 'job' "
                "AND meta->>'job_type' = 'paper_pull'"
            ).fetchall()
        ]


def _events(store, ref_id: int, event: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM ref_events WHERE ref_id = %s AND event = %s",
                (ref_id, event),
            ).fetchall()
        ]


def _job_ids(store) -> list[int]:
    with store.pool.connection() as conn:
        return [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'job' "
                "AND meta->>'job_type' = 'paper_pull' ORDER BY ref_id"
            ).fetchall()
        ]


def _job_status(store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _job_summaries(store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'job_summary' ORDER BY ord",
                (ref_id,),
            ).fetchall()
        ]


def _run_job_dispatch(store, jid: int) -> None:
    """Drive one paper_pull job through the executor's own dispatch +
    finalize path (the helpers ``job_inproc`` reuses)."""
    from precis.workers.executors.claude_inproc import (
        _build_dispatch_context,
        _finalize_plugin_dispatch,
    )

    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()[0]
    ctx = _build_dispatch_context(store, jid, "paper_pull", dict(meta or {}))
    paper_pull._dispatch(ctx, paper_pull.SPEC)
    _finalize_plugin_dispatch(store, jid)


# -- staging dir -------------------------------------------------------


def test_uploads_dir_resolves_outside_inbox(dirs):
    """MF1: staging is its own dir — never under the watched inbox."""
    inbox, staging = dirs
    assert pdf_upload.uploads_dir() == staging
    with pytest.raises(ValueError):
        staging.resolve().relative_to(inbox.resolve())


def test_uploads_dir_refuses_a_path_inside_the_inbox(monkeypatch, tmp_path):
    """A ``PRECIS_UPLOAD_STAGING`` resolving inside the watch inbox is
    the misconfig this var exists to remove — it reads as unconfigured,
    never as a fallback into the watched tree."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(inbox))
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(inbox / ".uploads"))
    assert pdf_upload.uploads_dir() is None
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(inbox))
    assert pdf_upload.uploads_dir() is None


def test_uploads_dir_unset_is_none(monkeypatch):
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    assert pdf_upload.uploads_dir() is None


# -- mint door -------------------------------------------------------------


def test_mint_returns_single_use_url(paper, store, dirs, link_key):
    ref_id = _stub(store)
    body = paper.put(id=ref_id, args={"pdf_upload_link": True}).body
    assert f"/files/upload/{ref_id}?t=v1.put." in body
    assert "single use" in body
    assert "curl -f --data-binary @paper.pdf" in body
    assert "'pdf_upload': '<sha256>'" in body


def test_mint_refuses_complete_target(paper, store, dirs, link_key):
    """A ref holding BOTH a body and a PDF can't take an upload — its drop
    would mint a duplicate rather than fold (gr519746)."""
    ref_id = _stub(store, body=True, pdf=True)
    with pytest.raises(BadInput, match="already has a body"):
        paper.put(id=ref_id, args={"pdf_upload_link": True})


def test_mint_refuses_non_paper(paper, store, dirs, link_key):
    other = store.insert_ref(kind="todo", slug=None, title="t")
    with pytest.raises(NotFound):
        paper.put(id=other.id, args={"pdf_upload_link": True})


def test_mint_refuses_without_staging(paper, store, monkeypatch, link_key):
    """No staging dir configured — mint refuses rather than hand out a
    link whose POST can only 503."""
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    ref_id = _stub(store)
    with pytest.raises(Unsupported, match="PRECIS_UPLOAD_STAGING"):
        paper.put(id=ref_id, args={"pdf_upload_link": True})


# -- attach door -----------------------------------------------------------


def test_attach_enqueues_pull_with_upload_source(paper, store, staging):
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    body = paper.put(
        id=ref_id, args={"pdf_upload": _SHA, "reason": "have the PDF locally"}
    ).body
    assert "queued" in body
    assert _jobs(store) == [
        {"ref_id": ref_id, "upload_sha": _SHA, "reason": "have the PDF locally"}
    ]
    # Idempotent: the same attach doesn't double-queue.
    assert (
        "already queued"
        in paper.put(
            id=ref_id, args={"pdf_upload": _SHA, "reason": "have the PDF locally"}
        ).body
    )


def test_attach_unknown_sha_refuses_toward_mint(paper, store, staging):
    ref_id = _stub(store)
    with pytest.raises(BadInput, match="pdf_upload_link"):
        paper.put(id=ref_id, args={"pdf_upload": _OTHER_SHA, "reason": "r"})
    assert _jobs(store) == []


def test_attach_refuses_without_staging(paper, store, monkeypatch):
    """Staging unconfigured — the sha can never name a staged file, so
    the refusal names the missing variable, not the mint door."""
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    ref_id = _stub(store)
    with pytest.raises(Unsupported, match="PRECIS_UPLOAD_STAGING"):
        paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})
    assert _jobs(store) == []


def test_attach_corpus_sha_refuses(paper, store, staging):
    """A sha whose pdfs row names a real corpus path — not an upload —
    is refused, not re-attached. The staged file is the whole record:
    no ``<ref>-<sha>.pdf`` in the staging dir means nothing to attach,
    whatever ``pdfs`` says."""
    ref_id = _stub(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs "
            "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
            "VALUES (%s, %s, 3, 100, '/corpus/d/doe.pdf')",
            (_OTHER_SHA, _OTHER_SHA),
        )
        conn.commit()
    with pytest.raises(BadInput, match="pdf_upload_link"):
        paper.put(id=ref_id, args={"pdf_upload": _OTHER_SHA, "reason": "r"})


def test_attach_refuses_staging_for_another_ref(paper, store, staging):
    """The staged name binds the upload to its token's ref: a staging
    for ref A can never be attached onto ref B."""
    ref_id = _stub(store)
    other = _stub(store, doi="10.1234/other")
    _stage_upload(staging, other)
    with pytest.raises(BadInput, match="pdf_upload_link"):
        paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})
    assert _jobs(store) == []


def test_attach_accepts_uppercase_sha(paper, store, staging):
    """The POST's hex answer lower-cases on input — an upper-case paste
    still finds its staging."""
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    body = paper.put(id=ref_id, args={"pdf_upload": _SHA.upper(), "reason": "r"}).body
    assert "queued" in body
    assert _jobs(store)[0]["upload_sha"] == _SHA


def test_attach_refuses_bad_sha_shape(paper, store, staging):
    ref_id = _stub(store)
    with pytest.raises(BadInput, match="sha256"):
        paper.put(id=ref_id, args={"pdf_upload": "notasha", "reason": "r"})


def test_attach_refuses_without_reason(paper, store, staging):
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    with pytest.raises(BadInput, match="reason"):
        paper.put(id=ref_id, args={"pdf_upload": _SHA})
    assert _jobs(store) == []


def test_attach_refuses_complete_target(paper, store, staging):
    ref_id = _stub(store, body=True, pdf=True)
    _stage_upload(staging, ref_id)
    with pytest.raises(BadInput, match="already has a body"):
        paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})


def test_attach_accepts_shaless_bodied_target(paper, store, staging):
    """A sha-less ref that already has a body (the gr519746 repair case:
    markup-ingested body, pdf cleared) takes the upload attach — the fold
    lands it attach-only, keeping the body."""
    ref_id = _stub(store, body=True)
    _stage_upload(staging, ref_id)
    body = paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"}).body
    assert "queued" in body
    assert len(_jobs(store)) == 1


def test_attach_dry_run_writes_nothing(paper, store, staging):
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    body = paper.put(
        id=ref_id,
        args={"pdf_upload": _SHA, "reason": "r"},
        dry_run=True,
    ).body
    assert "dry run" in body
    assert _jobs(store) == []


def test_doors_dont_combine(paper, store, staging):
    ref_id = _stub(store)
    with pytest.raises(BadInput, match="does not combine"):
        paper.put(
            id=ref_id,
            args={"pdf_upload_link": True, "pdf_upload": _SHA, "reason": "r"},
        )
    with pytest.raises(BadInput, match="does not combine"):
        paper.put(id=ref_id, args={"pdf_upload_link": True, "title": "x"})


# -- job submit validation -------------------------------------------------


def test_validate_requires_exactly_one_source(store):
    with pytest.raises(BadInput, match="exactly one"):
        paper_pull.validate(store, {"ref_id": 1})
    with pytest.raises(BadInput, match="exactly one"):
        paper_pull.validate(
            store,
            {"ref_id": 1, "url": "https://x/y.pdf", "upload_sha": _SHA},
        )
    paper_pull.validate(store, {"ref_id": 1, "url": "https://x/y.pdf"})
    paper_pull.validate(store, {"ref_id": 1, "upload_sha": _SHA})


def test_validate_refuses_bad_sha_shape(store):
    for bad in ("notasha", "zz" * 32, _SHA + "0"):
        with pytest.raises(BadInput, match="sha256"):
            paper_pull.validate(store, {"ref_id": 1, "upload_sha": bad})


# -- job: pull from the staged upload --------------------------------------


def test_pull_from_upload_drops_pdf_and_sidecar(store, inbox, staging):
    """The staged file moves ACROSS from the sibling staging dir into
    the inbox — a same-filesystem ``os.replace``."""
    ref_id = _stub(store)
    staged = _stage_upload(staging, ref_id)
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA, reason="mine")
    assert (rec["status"], rec["sha256"]) == ("dropped", _SHA)
    pdfs = list(inbox.glob("*.pdf"))
    assert len(pdfs) == 1 and pdfs[0].read_bytes() == _PDF
    sc = read_sidecar(pdfs[0])
    assert sc is not None
    assert (sc.ref_id, sc.source) == (ref_id, "agent_upload")
    # A bodiless stub needs the Marker run — not a printable attach.
    assert sc.printable_only is False
    # The staging is consumed — moved into the inbox proper.
    assert not staged.exists()
    events = _events(store, ref_id, "pdf_uploaded")
    assert len(events) == 1
    assert events[0]["identity"] == "doi" and events[0]["reason"] == "mine"


def test_pull_upload_missing_staging_fails(store, inbox, staging):
    ref_id = _stub(store)
    # Nothing staged — the file was swept or never uploaded.
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA)
    assert (rec["status"], rec["reason"]) == ("failed", "upload_missing")
    assert list(inbox.glob("*.pdf")) == []


def test_pull_upload_without_staging_dir_fails(store, inbox, monkeypatch):
    """``PRECIS_UPLOAD_STAGING`` unset on the worker — a recorded
    failure naming the var, never an ``upload_missing`` red herring."""
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    ref_id = _stub(store)
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA)
    assert (rec["status"], rec["reason"]) == ("failed", "no_upload_staging")
    assert list(inbox.glob("*.pdf")) == []


def test_pull_refuses_bad_source_params(store, inbox, staging):
    ref_id = _stub(store)
    rec = paper_pull.pull(store, ref_id)  # neither source
    assert (rec["status"], rec["reason"]) == ("failed", "bad_source")
    rec = paper_pull.pull(
        store, ref_id, "https://x/y.pdf", upload_sha=_SHA
    )  # both sources
    assert (rec["status"], rec["reason"]) == ("failed", "bad_source")
    rec = paper_pull.pull(store, ref_id, upload_sha="notasha")
    assert (rec["status"], rec["reason"]) == ("failed", "bad_source")


def test_pull_upload_identity_mismatch_drops_nothing(store, inbox, staging):
    ref_id = _stub(store)
    _stage_upload(staging, ref_id, data=_make_pdf(["Other Subject", "x", "y"]))
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA)
    assert (rec["status"], rec["reason"]) == ("failed", "identity_mismatch")
    assert list(inbox.glob("*.pdf")) == [] and list(inbox.glob("*.json")) == []
    events = _events(store, ref_id, "pdf_uploaded")
    assert events[0]["failure"] == "identity_mismatch"


def test_pull_upload_rechecks_target(store, inbox, staging):
    ref_id = _stub(store, body=True, pdf=True)
    _stage_upload(staging, ref_id)
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA)
    assert rec["reason"] == "bad_target"
    assert list(inbox.glob("*.pdf")) == []


def test_pull_upload_drops_for_shaless_bodied_ref(store, inbox, staging):
    """The gr519746 target shape — sha-less ref WITH a body: the drop
    proceeds; the fold attaches the file printable-side (attach-only)."""
    ref_id = _stub(store, body=True)
    _stage_upload(staging, ref_id)
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA, reason="mine")
    assert rec["status"] == "dropped"
    pdfs = list(inbox.glob("*.pdf"))
    assert len(pdfs) == 1
    sc = read_sidecar(pdfs[0])
    assert sc is not None and sc.ref_id == ref_id
    # The target already carries a body → the drop is marked attach-only
    # so the watcher skips the Marker run the fold would discard.
    assert sc.printable_only is True
    events = _events(store, ref_id, "pdf_uploaded")
    assert len(events) == 1 and events[0]["status"] == "dropped"


def test_replace_pdf_web_to_corpus_end_to_end(
    store, dirs, runtime_with_store, tmp_path, monkeypatch
):
    """gr519746 acceptance, all three legs on the real store:

    1. ``POST /papers/<id>/replace-pdf`` on a sha-less BODIED ref stages
       the bytes, queues the ``paper_pull`` job, journals
       ``pdf_upload_staged`` and flashes ``pdf_note`` on the redirect;
    2. ``paper_pull.pull`` moves the staging into the watch inbox with an
       ``agent_upload`` sidecar marked ``printable_only`` (the target has
       a body — no wasted Marker run) and journals ``pdf_uploaded``;
    3. the watcher's ``process_pdf`` fold attaches the file: it lands at
       the cite_key corpus path, the stale ``pdf_cleared`` survivor moves
       to ``errors/superseded/``, ``refs.pdf_sha256`` is set, the ``pdfs``
       row points at the corpus path, the body is untouched, and
       ``pdf_superseded`` is journaled.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from precis.cli.watch import process_pdf
    from precis.ingest.db_writer import PaperToWrite
    from precis_web.app import create_app
    from precis_web.config import WebConfig

    inbox, staging = dirs
    corpus = tmp_path / "corpus"
    ref_id = _stub(store, body=True)
    with store.pool.connection() as conn:
        cite_key = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'cite_key'",
            (ref_id,),
        ).fetchone()[0]

    # The stale corpus file the attach must displace (never delete).
    old_bytes = b"%PDF-1.4\nstale pdf_cleared survivor\n"
    old = corpus / cite_key[0].lower() / f"{cite_key}.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(old_bytes)
    old_tag = hashlib.sha256(old_bytes).hexdigest()[:8]

    # ── leg 1: the web upload ──────────────────────────────────────────
    client = TestClient(
        create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=corpus))
    )
    resp = client.post(
        f"/papers/{ref_id}/replace-pdf",
        files={"file": ("paper.pdf", _PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "tab=Meta" in loc and "pdf_note=" in loc
    assert "queued" in unquote(loc)
    assert (staging / pdf_upload.staged_name(ref_id, _SHA)).read_bytes() == _PDF
    assert _jobs(store) == [
        {"ref_id": ref_id, "upload_sha": _SHA, "reason": "web replace-pdf upload"}
    ]
    staged_events = _events(store, ref_id, "pdf_upload_staged")
    assert len(staged_events) == 1 and staged_events[0]["sha"] == _SHA

    # ── leg 2: the paper_pull job ──────────────────────────────────────
    rec = paper_pull.pull(
        store, ref_id, upload_sha=_SHA, reason="web replace-pdf upload"
    )
    assert rec["status"] == "dropped"
    dropped = list(inbox.glob("*.pdf"))
    assert len(dropped) == 1 and dropped[0].read_bytes() == _PDF
    sc = read_sidecar(dropped[0])
    assert sc is not None
    assert (sc.ref_id, sc.source, sc.printable_only) == (
        ref_id,
        "agent_upload",
        True,
    )

    # ── leg 3: the watcher drop ────────────────────────────────────────
    # Marker is not the seam under test — extract_paper is faked with the
    # file's real sha so the fold's promotion detection runs for real.
    def _fake_extract(pdf_path, **_kw):
        return PaperToWrite(
            title="A stub paper",
            authors=[],
            year=None,
            provider="embedded",
            cite_key_prefix=cite_key,
            pdf_sha256=_SHA,
            pdf_storage_path=str(pdf_path),
            pdf_page_count=len(_BODY),
            pdf_size_bytes=len(_PDF),
        )

    monkeypatch.setattr("precis.ingest.pipeline.extract_paper", _fake_extract)
    dest = process_pdf(
        dropped[0],
        store=store,
        watch_dir=inbox,
        corpus_dir=corpus,
        corpus_pres_dir=tmp_path / "corpus_pres",
        errors_dir=inbox / "errors",
        duplicates_dir=inbox / "errors" / "duplicates",
        debounce=0.01,
        user="owner",
    )

    assert dest == corpus / cite_key[0].lower() / f"{cite_key}.pdf"
    assert dest is not None
    assert dest.read_bytes() == _PDF
    assert not dropped[0].exists()
    superseded = inbox / "errors" / "superseded" / f"{cite_key}-{old_tag}.pdf"
    assert superseded.read_bytes() == old_bytes

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert row[0] == _SHA
        row = conn.execute(
            "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s", (_SHA,)
        ).fetchone()
        assert row[0] == str(dest)
        bodies = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0", (ref_id,)
        ).fetchall()
        assert [r[0] for r in bodies] == ["body"]
    assert _events(store, ref_id, "pdf_uploaded")
    superseded_events = _events(store, ref_id, "pdf_superseded")
    assert len(superseded_events) == 1
    assert superseded_events[0]["superseded"] == superseded.name


# -- sweep ------------------------------------------------------------------


def _age(path, seconds: float) -> None:
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime - seconds))


def test_sweep_removes_stale_artifacts_files_only(store, staging):
    ref_id = _stub(store)
    staged = _stage_upload(staging, ref_id)
    done = staged.with_name("1.sig.done")
    done.touch()
    part = staged.with_name("1.sig.part")
    part.touch()
    for p in (staged, done, part):
        _age(p, pdf_upload.STALE_AFTER_S + 60)
    assert pdf_upload.sweep_stale_uploads() == 3
    assert list(staging.iterdir()) == []


def test_sweep_keeps_fresh_artifacts(store, staging):
    ref_id = _stub(store)
    staged = _stage_upload(staging, ref_id)
    assert pdf_upload.sweep_stale_uploads() == 0
    assert staged.exists()


def test_sweep_of_abandoned_reupload_keeps_attached_pdf(store, inbox, staging):
    """The M2 data-loss shape: upload sha X, attach it (the paper's
    ``pdfs`` row now points at the corpus), upload the same bytes again,
    abandon — the sweep deletes only the stale staged FILE, never the
    paper's ``pdfs`` row."""
    ref_id = _stub(store)
    staged = _stage_upload(staging, ref_id)
    # Attach — the drop moves the staged file into the inbox.
    rec = paper_pull.pull(store, ref_id, upload_sha=_SHA, reason="mine")
    assert rec["status"] == "dropped"
    assert not staged.exists()
    dropped = list(inbox.glob("*.pdf"))
    assert len(dropped) == 1
    # Ingest folds the drop: the sha's pdfs row now names the corpus.
    corpus_path = f"/corpus/{dropped[0].name}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs "
            "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
            "VALUES (%s, %s, %s, %s, %s)",
            (_SHA, _SHA, len(_BODY), len(_PDF), corpus_path),
        )
        conn.commit()
    # Same bytes uploaded again under a second token, then abandoned.
    reupload = _stage_upload(staging, ref_id)
    _age(reupload, pdf_upload.STALE_AFTER_S + 60)
    assert pdf_upload.sweep_stale_uploads() == 1
    assert not reupload.exists()
    # The paper still has its PDF: row and dropped file untouched.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s", (_SHA,)
        ).fetchone()
    assert row is not None and row[0] == corpus_path
    assert dropped[0].exists()


def test_sweep_without_staging_is_noop(store, monkeypatch):
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    assert pdf_upload.sweep_stale_uploads() == 0


# -- gr537770: failed pull reads failed; retry isn't deduped ----------------


def test_failed_pull_marks_job_failed_with_reason(
    paper, store, inbox, staging, monkeypatch
):
    """(b) The pull's recorded failure must drive the JOB to
    ``STATUS:failed`` too — before this fix the job showed ``succeeded``
    while the summary read ``failed (no_upload_staging)``."""
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})
    jid = _job_ids(store)[-1]
    # A staging-less worker claims it: the run fails no_upload_staging.
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    _run_job_dispatch(store, jid)
    assert _job_status(store, jid) == "failed"
    assert any("no_upload_staging" in s for s in _job_summaries(store, jid))
    assert _events(store, ref_id, "pdf_uploaded")[0]["failure"] == ("no_upload_staging")


def test_failed_pull_does_not_block_reupload_same_sha(
    paper, store, inbox, staging, monkeypatch
):
    """(c) Re-uploading the same file mints the same ``upload:<sha>``
    idem_key — a FAILED prior job must not dedupe the fresh pull onto
    itself (terminal statuses never block; this pins the invariant
    end-to-end through the real put)."""
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})
    jid1 = _job_ids(store)[-1]
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    _run_job_dispatch(store, jid1)
    assert _job_status(store, jid1) == "failed"
    # Same bytes uploaded again → same staged name → same idem_key.
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    _stage_upload(staging, ref_id)
    body = paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "retry"}).body
    assert "queued" in body and "already queued" not in body
    assert len(_job_ids(store)) == 2


def test_queued_pull_still_dedupes_same_sha(paper, store, staging):
    """The other half of the idem contract: a still-active job DOES
    block a duplicate queue (unchanged behaviour)."""
    ref_id = _stub(store)
    _stage_upload(staging, ref_id)
    paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"})
    body = paper.put(id=ref_id, args={"pdf_upload": _SHA, "reason": "r"}).body
    assert "already queued" in body
    assert len(_job_ids(store)) == 1


def test_replace_pdf_reupload_after_failed_pull_not_deduped(
    store, dirs, runtime_with_store, tmp_path, monkeypatch
):
    """(c) through the gripe's own door — ``POST /replace-pdf`` twice with
    the same bytes. The first ``paper_pull`` dies terminal on a
    staging-less worker; its ``upload:<sha>`` idem_key must NOT dedupe
    the second POST onto the failed job (terminal statuses never block
    a re-put). A third POST while the retry is still queued dedupes onto
    it — the dedupe still guards active work."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    _inbox, staging = dirs
    ref_id = _stub(store, body=True)
    client = TestClient(
        create_app(
            runtime=runtime_with_store,
            web_config=WebConfig(corpus_dir=tmp_path / "corpus"),
        )
    )
    files = {"file": ("paper.pdf", _PDF, "application/pdf")}

    resp = client.post(
        f"/papers/{ref_id}/replace-pdf", files=files, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "queued" in unquote(resp.headers["location"])
    jid1 = _job_ids(store)[-1]

    # A staging-less worker claims the pull — the terminal failure (b)
    # made real. The staged file is untouched: no_upload_staging refuses
    # before it is read.
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    _run_job_dispatch(store, jid1)
    assert _job_status(store, jid1) == "failed"

    # The user's retry: same bytes → same idem_key → a FRESH job, since
    # STATUS:failed is terminal and never blocks a re-put.
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    resp = client.post(
        f"/papers/{ref_id}/replace-pdf", files=files, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "queued" in unquote(resp.headers["location"])
    (jid2,) = [j for j in _job_ids(store) if j != jid1]
    assert _job_status(store, jid2) == "queued"

    # While the retry is still in flight, a third identical POST dedupes
    # onto it — no third job is minted.
    resp = client.post(
        f"/papers/{ref_id}/replace-pdf", files=files, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _job_ids(store) == [jid1, jid2]
