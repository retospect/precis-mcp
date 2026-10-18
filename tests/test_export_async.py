"""gr493192 — slow draft exports: per-export ref memo (output identity +
query count), the progress hook, and the async export job/route/UI state.

The .docx output must be byte-identical (modulo the core-properties
timestamps) with the memo on or off; the PDF route is exercised through
the job wrapper with the LaTeX toolchain stubbed (its LaTeX source path is
not touched by this change)."""

from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("docx")

from fastapi.testclient import TestClient

import precis.export.docx as docx_mod
from precis.export import progress
from precis.export.docx import export_docx
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import ChunkInsert
from precis_web.app import create_app
from precis_web.config import WebConfig

N_PAPERS = 6


@pytest.fixture
def seeded(runtime_with_store: Any) -> tuple[Any, Any]:
    hub = runtime_with_store.hub
    store = hub.live_store
    for i in range(N_PAPERS):
        store.insert_ref(
            kind="paper",
            slug=f"asyncp{i}",
            title=f"Async paper {i}",
            year=2010 + i,
            provider="manual",
        )
        r = store.get_ref(kind="paper", id=f"asyncp{i}")
        store.chunks.insert_chunks(r.id, [ChunkInsert(ord=0, text="b", slug=f"ab{i}")])
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft = DraftHandler(hub=hub)
    draft.put(id="asyncd", title="Async draft", project=pid)
    out = draft.put(
        id="asyncd", chunk_kind="heading", text="Methods", at={"last": True}
    )
    m = re.search(r"dc\d+", out.body)
    assert m is not None
    sec = m.group(0)
    for k in range(18):
        a, b = k % N_PAPERS, (k * 5 + 1) % N_PAPERS
        draft.put(
            id="asyncd",
            chunk_kind="paragraph",
            text=f"Claim {k} **bold** [§asyncp{a}~0] and [§asyncp{b}~0].",
            at={"into": sec, "last": True},
        )
    return store, store.get_ref(kind="draft", id="asyncd")


def _docx_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"}


@pytest.mark.parametrize("citations", ["plain", "endnote"])
def test_docx_identical_with_and_without_ref_memo(
    seeded: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    citations: str,
) -> None:
    store, ref = seeded
    memo = tmp_path / "memo.docx"
    plain = tmp_path / "nomemo.docx"
    export_docx(store, ref, target_path=memo, citations=citations)
    monkeypatch.setattr(docx_mod, "RefLookupMemo", lambda s: s)
    export_docx(store, ref, target_path=plain, citations=citations)
    assert _docx_parts(memo) == _docx_parts(plain)


def test_docx_ref_lookups_scale_with_papers_not_cites(
    seeded: tuple[Any, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ref = seeded
    seen: list[str] = []
    orig = psycopg.Cursor.execute

    def spy(self: Any, query: Any, *a: Any, **k: Any) -> Any:
        seen.append(str(query))
        return orig(self, query, *a, **k)

    monkeypatch.setattr(psycopg.Cursor, "execute", spy)
    export_docx(store, ref, target_path=tmp_path / "q.docx", citations="plain")
    lookups = [q for q in seen if "id_kind = 'cite_key'" in q]
    # 36 cite markers over N_PAPERS papers: one lookup per distinct
    # (kind, slug) probe, never one per marker (was 1848 on a 288-cite draft).
    assert len(lookups) <= 4 * N_PAPERS, len(lookups)
    idents = [q for q in seen if "FROM ref_identifiers WHERE ref_id = ANY" in q]
    assert len(idents) <= N_PAPERS + 2, len(idents)


def test_export_docx_reports_progress(seeded: tuple[Any, Any], tmp_path: Path) -> None:
    store, ref = seeded
    events: list[tuple[str, int | None, int | None]] = []
    with progress.bind(lambda s, d, t: events.append((s, d, t))):
        export_docx(store, ref, target_path=tmp_path / "p.docx")
    rendering = [e for e in events if e[0] == "rendering" and e[2]]
    assert rendering and rendering[-1][1] is not None
    done, total = rendering[-1][1], rendering[-1][2]
    assert done is not None and total is not None and done < total
    assert events[-1][0] == "saving .docx"


def test_progress_is_noop_without_binding() -> None:
    progress.report("x", 1, 2)  # must not raise


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path, seeded: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _wait(client: TestClient, url: str, timeout_s: float = 60.0) -> dict[str, Any]:
    t0 = time.monotonic()
    while True:
        snap = client.get(url).json()
        if snap["state"] != "running" or time.monotonic() - t0 > timeout_s:
            return snap
        time.sleep(0.1)


def test_async_docx_matches_direct_download(client: TestClient) -> None:
    start = client.post("/drafts/asyncd/export/start?fmt=docx&citations=plain")
    assert start.status_code == 200
    tok = start.json()["token"]
    snap = _wait(client, f"/drafts/asyncd/export/status/{tok}")
    assert snap["state"] == "done" and snap["pct"] == 100
    got = client.get(f"/drafts/asyncd/export/download/{tok}")
    assert got.status_code == 200
    assert got.content[:2] == b"PK"
    assert "attachment" in got.headers["content-disposition"]
    assert ".docx" in got.headers["content-disposition"]
    direct = client.get("/drafts/asyncd/export.docx?citations=plain")
    assert direct.status_code == 200
    import io

    def parts(b: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            return {n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"}

    assert parts(got.content) == parts(direct.content)


def test_async_pdf_runs_the_pdf_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import precis.export.compile as compile_mod
    import precis.export.latex as latex_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setattr(latex_mod, "export_draft", lambda *a, **k: None)

    class _Ok:
        ok = True
        log_tail = ""

    def fake_compile(project_dir: Path, **_: Any) -> _Ok:
        Path(project_dir).mkdir(parents=True, exist_ok=True)
        (Path(project_dir) / "main.pdf").write_bytes(b"%PDF-stub-asyncd")
        return _Ok()

    monkeypatch.setattr(compile_mod, "compile_pdf", fake_compile)
    tok = client.post("/drafts/asyncd/export/start?fmt=pdf").json()["token"]
    snap = _wait(client, f"/drafts/asyncd/export/status/{tok}")
    assert snap["state"] == "done"
    got = client.get(f"/drafts/asyncd/export/download/{tok}")
    assert got.content == b"%PDF-stub-asyncd"
    assert got.content == client.get("/drafts/asyncd/pdf").content


def test_async_gated_export_reports_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import precis.export.compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: False)
    monkeypatch.setattr(
        "precis_web.routes.drafts._pdf_cache_dir",
        lambda *a, **k: Path("/nonexistent-precis-export-cache"),
    )
    tok = client.post("/drafts/asyncd/export/start?fmt=pdf").json()["token"]
    snap = _wait(client, f"/drafts/asyncd/export/status/{tok}")
    assert snap["state"] == "blocked"
    # Nothing to download; the page falls back to the plain GET.
    r = client.get(f"/drafts/asyncd/export/download/{tok}", follow_redirects=False)
    assert r.status_code == 303


def test_async_start_rejects_unknown_format_and_draft(client: TestClient) -> None:
    assert client.post("/drafts/asyncd/export/start?fmt=odt").status_code == 400
    assert client.post("/drafts/nope-zz/export/start?fmt=docx").status_code == 404
    assert client.get("/drafts/asyncd/export/status/bogus").status_code == 404


def test_reader_page_wires_the_progress_ui(client: TestClient) -> None:
    html = client.get("/drafts/asyncd").text
    assert "startExport('docx')" in html
    assert "startExport('pdf')" in html
    assert "data-export-progress" in html
    assert "/export/start" in html


def test_first_progress_event_precedes_render(
    seeded: tuple[Any, Any], tmp_path: Path
) -> None:
    """gr493192: the bar used to say "starting…" through the preflight
    gates (retraction + figure clearance walks); the first event now comes
    from that phase, well before the render stage."""
    from precis.export.preflight import draft_export_preflight

    store, ref = seeded
    events: list[str] = []
    with progress.bind(lambda s, d, t: events.append(s)):
        pf = draft_export_preflight(store, ref)
        export_docx(store, ref, target_path=tmp_path / "e.docx")
    assert events[0] == "checking cited papers for retractions"
    assert "checking figure permissions" in events
    assert not pf.blocked
    assert events.index("checking figure permissions") < events.index("rendering")
    assert events.index("reading the draft") < events.index("rendering")
