"""``put(kind='paper', id=<stub>, args={'pdf_url': ...})`` (gr478881): the
handler's refusals + enqueue, and the ``paper_pull`` job's inbox drop.

The download is faked at ``datasheet_pull.safe_stream`` (the shared bounded
download); the inbox is a tmp dir named by ``PRECIS_WATCH_INBOX``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.ingest.fetch_sidecar import read_sidecar
from precis.workers.job_types import datasheet_pull as dl
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
_URL = "https://repo.example.edu/papers/x.pdf"


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
    store,
    *,
    body: bool = False,
    pdf: bool = False,
    title: str | None = "A stub paper",
    doi: str | None = "10.1234/stub",
    entry_type: str | None = None,
) -> int:
    ref_id, _ = store.upsert_stub_paper(
        identifiers=[("doi", doi)] if doi else [("cite_key", "nodoi2020")],
        title=title,
    )
    if entry_type:
        store.update_ref(ref_id, meta_patch={"entry_type": entry_type})
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
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/stub')",
                ("ab" * 32, "ab" * 32),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                ("ab" * 32, ref_id),
            )
        conn.commit()
    return ref_id


def _jobs(store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT meta->'params' FROM refs WHERE kind = 'job' "
                "AND meta->>'job_type' = 'paper_pull'"
            ).fetchall()
        ]


class _Resp:
    def __init__(self, body: bytes, url: str) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.url = url
        self._body = body

    def iter_bytes(self, chunk_size: int = 0):
        yield self._body


@pytest.fixture
def serve(monkeypatch):
    state: dict[str, Any] = {"body": _PDF}

    @contextmanager
    def fake_stream(client, method, url, /, **kw):
        yield _Resp(state["body"], url)

    monkeypatch.setattr(dl, "safe_stream", fake_stream)
    return state


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path))
    return tmp_path


# -- handler ---------------------------------------------------------------


def test_put_refuses_non_paper(paper, store):
    other = store.insert_ref(kind="todo", slug=None, title="t")
    with pytest.raises(NotFound):
        paper.put(id=other.id, args={"pdf_url": _URL})
    assert _jobs(store) == []


def test_put_refuses_bodied_stub(paper, store):
    ref_id = _stub(store, body=True)
    with pytest.raises(BadInput, match="already has a body"):
        paper.put(id=ref_id, args={"pdf_url": _URL})
    assert _jobs(store) == []


def test_put_accepts_bodiless_ref_holding_a_pdf(paper, store):
    # A preview PDF with no body is refetchable: the sidecar fold accepts it.
    ref_id = _stub(store, pdf=True)
    paper.put(id=ref_id, args={"pdf_url": _URL})
    assert len(_jobs(store)) == 1


def test_put_refuses_non_http_url(paper, store):
    ref_id = _stub(store)
    with pytest.raises(BadInput, match="http"):
        paper.put(id=ref_id, args={"pdf_url": "file:///etc/passwd"})


def test_put_enqueues_once(paper, store):
    ref_id = _stub(store)
    assert "queued" in paper.put(id=ref_id, args={"pdf_url": _URL}).body
    assert "already queued" in paper.put(id=ref_id, args={"pdf_url": _URL}).body
    assert _jobs(store) == [{"ref_id": ref_id, "url": _URL}]


# -- job -------------------------------------------------------------------


def test_pull_writes_pdf_and_sidecar(store, serve, inbox):
    ref_id = _stub(store)
    rec = paper_pull.pull(store, ref_id, _URL)
    assert rec["status"] == "dropped"
    pdfs = list(inbox.glob("*.pdf"))
    assert len(pdfs) == 1 and pdfs[0].read_bytes() == _PDF
    sc = read_sidecar(pdfs[0])
    assert sc is not None
    assert (sc.ref_id, sc.source, sc.url) == (ref_id, "agent_url", _URL)
    assert not list((inbox / ".staging").iterdir())
    meta = store.get_ref(kind="paper", id=ref_id).meta
    assert meta["paper_pull"]["status"] == "dropped"
    json.dumps(meta["paper_pull"])


def test_pull_refuses_non_pdf_response(store, serve, inbox):
    serve["body"] = b"<html>login wall</html>" * 100
    ref_id = _stub(store)
    rec = paper_pull.pull(store, ref_id, _URL)
    assert (rec["status"], rec["reason"]) == ("failed", "not_pdf")
    assert list(inbox.glob("*.pdf")) == []
    assert list(inbox.glob("*.json")) == []


def test_pull_rechecks_target_state(store, serve, inbox):
    ref_id = _stub(store, body=True)
    rec = paper_pull.pull(store, ref_id, _URL)
    assert rec["reason"] == "bad_target"
    assert list(inbox.glob("*.pdf")) == []


def test_pull_without_inbox_env_fails_recorded(store, serve, monkeypatch):
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    rec = paper_pull.pull(store, _stub(store), _URL)
    assert rec["reason"] == "no_inbox"


def test_dispatch_set_meta_does_not_collide_with_job_ref_id(store, serve, monkeypatch):
    # The executor binds the job's ref_id positionally (claude_inproc
    # _ctx_set_meta -> _set_meta(conn, ref_id, **fields)); a ``ref_id=``
    # field raised TypeError on prod (r24, job 479579).
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    ref_id = _stub(store)
    written: dict[str, Any] = {}

    def _set_meta(job_ref_id: int, **fields: Any) -> None:
        written.update(fields)

    class _Ctx:
        def __init__(self) -> None:
            self.store = store
            self.meta = {"params": {"ref_id": ref_id, "url": _URL}}

        def append_chunk(self, _kind: str, _text: str) -> None:
            pass

        def set_meta(self, **fields: Any) -> None:
            _set_meta(999, **fields)

    ctx: Any = _Ctx()
    paper_pull._dispatch(ctx, paper_pull.SPEC)
    assert written["paper_ref_id"] == ref_id
    assert written["pull_reason"] == "no_inbox"


# -- blocklist -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://sci-hub.se/10.1234/x",
        "https://www.Sci-Hub.RU/x.pdf",
        "https://libgen.is/book",
        "https://gen.lib.rus.ec/x",
        "https://z-lib.id/x",
        "https://1lib.sk/x",
        "https://singlelib.org/x",
        "https://annas-archive.org/md5/1",
        "https://b-ok.cc/x",
        "https://oceanofpdf.com/x",
        "https://www.pdfdrive.com/x",
    ],
)
def test_blocked_host_matches(url):
    assert paper_pull.blocked_host(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/pdf/1.pdf",
        "https://repo.example.edu/a.pdf",
        "https://www.nature.com/x.pdf",
    ],
)
def test_blocked_host_spares_normal_hosts(url):
    assert paper_pull.blocked_host(url) is None


def test_put_refuses_blocked_host(paper, store):
    ref_id = _stub(store)
    with pytest.raises(BadInput, match="shadow library"):
        paper.put(id=ref_id, args={"pdf_url": "https://sci-hub.se/x.pdf"})
    assert _jobs(store) == []


def test_pull_refuses_blocked_final_host(store, inbox, monkeypatch):
    @contextmanager
    def redirected(client, method, url, /, **kw):
        yield _Resp(_PDF, "https://libgen.is/final.pdf")

    monkeypatch.setattr(dl, "safe_stream", redirected)
    rec = paper_pull.pull(store, _stub(store), _URL)
    assert (rec["status"], rec["reason"]) == ("failed", "blocked_host")
    assert list(inbox.glob("*")) == [inbox / ".staging"]
    assert list((inbox / ".staging").iterdir()) == []


def test_pull_refuses_blocked_initial_host(store, serve, inbox):
    rec = paper_pull.pull(store, _stub(store), "https://sci-hub.se/x.pdf")
    assert rec["reason"] == "blocked_host"


# -- identity --------------------------------------------------------------


def test_identity_by_doi(store, serve, inbox):
    serve["body"] = _make_pdf(
        ["unrelated heading\nhttps://doi.org/10.1234/STUB", "x", "y"]
    )
    ref_id = _stub(store, title="Totally different words here")
    rec = paper_pull.pull(store, ref_id, _URL)
    assert (rec["status"], rec["identity"]) == ("dropped", "doi")


def test_identity_by_title(store, serve, inbox):
    serve["body"] = _make_pdf(["Quantum Widgets: a study!", "x", "y"])
    ref_id = _stub(store, title="A Study of quantum widgets", doi="10.9/other")
    rec = paper_pull.pull(store, ref_id, _URL)
    assert (rec["status"], rec["identity"]) == ("dropped", "title")


def test_identity_mismatch_writes_nothing(store, serve, inbox):
    serve["body"] = _make_pdf(["Completely Other Subject Matter", "x", "y"])
    ref_id = _stub(store, title="Quantum widgets and gadgets", doi="10.9/other")
    rec = paper_pull.pull(store, ref_id, _URL)
    assert (rec["status"], rec["reason"]) == ("failed", "identity_mismatch")
    assert "Completely Other Subject Matter" in rec["detail"]
    assert list(inbox.glob("*.pdf")) == [] and list(inbox.glob("*.json")) == []


def test_identity_unchecked_without_title_or_doi(store, serve, inbox):
    serve["body"] = _make_pdf(["anything", "x", "y"])
    ref_id = _stub(store, title=None, doi=None)
    rec = paper_pull.pull(store, ref_id, _URL)
    assert rec["status"] == "dropped" and rec["identity_unchecked"] is True


# -- preview ---------------------------------------------------------------


def test_two_page_article_refused(store, serve, inbox):
    serve["body"] = _make_pdf(["A stub paper 10.1234/stub", "p2"])
    rec = paper_pull.pull(store, _stub(store, entry_type="article"), _URL)
    assert (rec["status"], rec["reason"]) == ("failed", "preview_suspected")
    assert list(inbox.glob("*.pdf")) == []


def test_two_page_unknown_type_refused(store, serve, inbox):
    serve["body"] = _make_pdf(["A stub paper 10.1234/stub", "p2"])
    rec = paper_pull.pull(store, _stub(store), _URL)
    assert rec["reason"] == "preview_suspected"


def test_two_page_book_allowed(store, serve, inbox):
    serve["body"] = _make_pdf(["A stub paper 10.1234/stub", "p2"])
    rec = paper_pull.pull(store, _stub(store, entry_type="book"), _URL)
    assert rec["status"] == "dropped"


def test_preview_marker_refused(store, serve, inbox):
    serve["body"] = _make_pdf(
        ["A stub paper 10.1234/stub\nAccess through your institution", "b", "c"]
    )
    rec = paper_pull.pull(store, _stub(store), _URL)
    assert rec["reason"] == "preview_suspected"


# -- source_url + back-scan ------------------------------------------------


def test_watcher_stamps_source_url(store):
    from precis.cli.watch import _stamp_source_url
    from precis.ingest.add import IngestResult
    from precis.ingest.fetch_sidecar import FetchSidecar

    ref_id = _stub(store)
    result = IngestResult(
        ref_id=ref_id,
        inserted=True,
        paper_id="p",
        pub_id=None,
        cite_key="k",
        pdf_sha256=None,
        content_hash=None,
        chunks_written=0,
        identifiers={},
    )
    sc = FetchSidecar(ref_id=ref_id, identifiers={}, source="agent_url", url=_URL)
    _stamp_source_url(sc, result, store=store)
    assert store.get_ref(kind="paper", id=ref_id).meta["source_url"] == _URL
    other = _stub(store, title="Other thing", doi="10.1/o")
    result2 = IngestResult(**{**result.__dict__, "ref_id": other})
    _stamp_source_url(
        FetchSidecar(ref_id=other, identifiers={}, source="fetcher:oa", url=_URL),
        result2,
        store=store,
    )
    assert "source_url" not in store.get_ref(kind="paper", id=other).meta


def test_suspect_previews_backscan(store):
    from precis.cli.paper import suspect_previews

    def with_pages(ref_id: int, lo: int, hi: int) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET pdf_pages = int4range(%s, %s) WHERE ref_id = %s",
                (lo, hi, ref_id),
            )
            conn.commit()

    short = _stub(store, title="Short one", doi="10.1/s")
    book = _stub(store, title="A book", doi="10.1/b", entry_type="book")
    full = _stub(store, title="Full one", doi="10.1/f")
    with_pages(short, 0, 1)
    with_pages(book, 0, 2)
    with_pages(full, 0, 12)
    rows = suspect_previews(store)
    assert [r[0] for r in rows] == [short]
    assert rows[0][1:] == ("Short one", 1, 0)
