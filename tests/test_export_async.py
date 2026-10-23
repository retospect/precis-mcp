"""gr493192 — slow draft exports: per-export ref memo (output identity +
query count), the progress hook, and the async export job/route/UI state.

The .docx output must be byte-identical (modulo the core-properties
timestamps) with the memo on or off; the PDF route is exercised through
the job wrapper with the LaTeX toolchain stubbed (its LaTeX source path is
not touched by this change)."""

from __future__ import annotations

import re
import threading
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


def test_preflight_retraction_walk_is_memoised(
    seeded: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192: the preflight's retraction walk renders the body to learn
    the cite set — the same per-marker ref reads the exporter does. The
    memo covers it, so the gate scales with papers, not cite markers."""
    from precis.export.preflight import draft_export_preflight

    store, ref = seeded
    seen: list[str] = []
    orig = psycopg.Cursor.execute

    def spy(self: Any, query: Any, *a: Any, **k: Any) -> Any:
        seen.append(str(query))
        return orig(self, query, *a, **k)

    monkeypatch.setattr(psycopg.Cursor, "execute", spy)
    pf = draft_export_preflight(store, ref)
    assert not pf.blocked
    lookups = [q for q in seen if "id_kind = 'cite_key'" in q]
    # 36 cite markers over N_PAPERS papers — same bound the docx render
    # itself meets (was thousands of lookups when this walk was unmemoised).
    assert len(lookups) <= 4 * N_PAPERS, len(lookups)


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


def test_async_export_hands_exporter_the_warmed_memo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192: the job's store is one RefLookupMemo across resolve →
    preflight → handler, so the exporter renders off the preflight's warm
    cache instead of re-reading every ref the gate just walked."""
    import precis.export.docx as docx_mod
    from precis.export._ref_cache import RefLookupMemo

    captured: list[Any] = []
    real_docx = docx_mod.export_docx

    def spy(*a: Any, **k: Any) -> Any:
        captured.append(a[0] if a else k.get("store"))
        return real_docx(*a, **k)

    monkeypatch.setattr(docx_mod, "export_docx", spy)
    tok = client.post("/drafts/asyncd/export/start?fmt=docx").json()["token"]
    assert _wait(client, f"/drafts/asyncd/export/status/{tok}")["state"] == "done"
    assert captured and isinstance(captured[0], RefLookupMemo)


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
    # The download route serves the gate/error page the job already
    # rendered — a second trip through the synchronous route would re-run
    # the whole preflight under a frozen bar (gr493192).
    r = client.get(f"/drafts/asyncd/export/download/{tok}", follow_redirects=False)
    assert r.status_code == 503
    assert "latexmk" in r.text


def test_async_zip_runs_without_preflight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192: every export control shares the job mechanism — the
    papers bundle builds behind the same progress bar. It is not an
    export document, so the retraction/figure gates don't run."""
    import precis_web.routes.drafts as drafts_mod

    calls: list[str] = []

    def no_preflight(*a: Any, **k: Any) -> Any:
        calls.append("preflight")
        return None

    monkeypatch.setattr(drafts_mod, "draft_export_preflight", no_preflight)
    start = client.post("/drafts/asyncd/export/start?fmt=zip")
    assert start.status_code == 200 and start.json()["ok"]
    tok = start.json()["token"]
    snap = _wait(client, f"/drafts/asyncd/export/status/{tok}")
    assert snap["state"] == "done"
    got = client.get(f"/drafts/asyncd/export/download/{tok}")
    assert got.status_code == 200 and got.content[:2] == b"PK"
    assert calls == []


def test_async_start_rejects_unknown_format_and_draft(client: TestClient) -> None:
    assert client.post("/drafts/asyncd/export/start?fmt=odt").status_code == 400
    # A missing draft no longer 404s the POST — the request does no draft
    # work at all; the job's stage-1 resolution reports the not-found
    # through the same "draft not found" message instead.
    start = client.post("/drafts/nope-zz/export/start?fmt=docx")
    assert start.status_code == 200 and start.json()["ok"]
    snap = _wait(client, f"/drafts/nope-zz/export/status/{start.json()['token']}")
    assert snap["state"] == "error" and snap["error"] == "draft not found"
    assert client.get("/drafts/asyncd/export/status/bogus").status_code == 404


def test_export_start_returns_before_preflight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192: /export/start hands the token back with no draft work in
    the request — preflight runs inside the job thread, after it."""
    import precis_web.routes.drafts as drafts_mod

    real_ref = drafts_mod._draft_ref
    real_preflight = drafts_mod.draft_export_preflight
    resolve_entered = threading.Event()
    release = threading.Event()
    preflight_calls: list[str] = []

    def parked_ref(store: Any, ident: str) -> Any:
        resolve_entered.set()
        release.wait(30)
        return real_ref(store, ident)

    def sentinel_preflight(*a: Any, **k: Any) -> Any:
        preflight_calls.append("preflight")
        return real_preflight(*a, **k)

    # Parking the draft resolver holds the job thread ahead of preflight,
    # so a preflight call during the request could not hide behind a race.
    monkeypatch.setattr(drafts_mod, "_draft_ref", parked_ref)
    monkeypatch.setattr(drafts_mod, "draft_export_preflight", sentinel_preflight)
    try:
        start = client.post("/drafts/asyncd/export/start?fmt=docx")
        assert start.status_code == 200 and start.json()["ok"]
        # The job is parked in stage-1 resolution; nothing preflight has run.
        assert resolve_entered.wait(10)
        assert preflight_calls == []
    finally:
        release.set()
    snap = _wait(client, f"/drafts/asyncd/export/status/{start.json()['token']}")
    assert snap["state"] == "done"
    assert preflight_calls == ["preflight"]


def test_export_preflight_failure_surfaces_as_job_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192: a preflight error arrives through the job status, not the
    start response."""
    import precis_web.routes.drafts as drafts_mod

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("preflight exploded")

    monkeypatch.setattr(drafts_mod, "draft_export_preflight", boom)
    start = client.post("/drafts/asyncd/export/start?fmt=docx")
    assert start.status_code == 200 and start.json()["ok"]
    snap = _wait(client, f"/drafts/asyncd/export/status/{start.json()['token']}")
    assert snap["state"] == "error"
    assert "preflight exploded" in snap["error"]


def test_export_job_reports_preflight_as_first_stage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192 review: the gates are the job's reported stage 1 — the
    first real stage the bar shows is the preflight, run once on the
    draft the job already loaded (no second _draft_ref in the handler)."""
    import precis.export.docx as docx_mod
    import precis_web.routes.drafts as drafts_mod

    real_docx = docx_mod.export_docx
    real_ref = drafts_mod._draft_ref
    ref_calls: list[str] = []
    release = threading.Event()

    def counting_ref(store: Any, ident: str) -> Any:
        ref_calls.append(ident)
        return real_ref(store, ident)

    def parked_docx(*a: Any, **k: Any) -> Any:
        release.wait(30)
        return real_docx(*a, **k)

    monkeypatch.setattr(drafts_mod, "_draft_ref", counting_ref)
    monkeypatch.setattr(docx_mod, "export_docx", parked_docx)
    first = None
    try:
        start = client.post("/drafts/asyncd/export/start?fmt=docx")
        assert start.status_code == 200
        tok = start.json()["token"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30:
            stage = client.get(f"/drafts/asyncd/export/status/{tok}").json()["stage"]
            if stage != "starting":
                first = stage
                break
            time.sleep(0.02)
        # The preflight reports its own first sub-stage at once, so on a
        # loaded runner the poll can land on that instead of the job's
        # "preflight" label; both are the preflight stage.
        assert first is not None and (
            first.startswith("preflight")
            or first == "checking cited papers for retractions"
        )
    finally:
        release.set()
    assert _wait(client, f"/drafts/asyncd/export/status/{tok}")["state"] == "done"
    assert ref_calls == ["asyncd"]


def test_export_jobs_queue_past_the_build_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493192 review: at most _BUILD_SLOTS jobs build at once; the rest
    hold a 'queued' stage until a slot frees."""
    import precis_web.routes.drafts as drafts_mod

    real_ref = drafts_mod._draft_ref
    entered = threading.Semaphore(0)
    release = threading.Event()

    def parked_ref(store: Any, ident: str) -> Any:
        entered.release()
        release.wait(30)
        return real_ref(store, ident)

    monkeypatch.setattr(drafts_mod, "_draft_ref", parked_ref)
    urls = [
        "/drafts/asyncd/export/start?fmt=docx",
        "/drafts/asyncd/export/start?fmt=docx&citations=plain",
        "/drafts/asyncd/export/start?fmt=docx&links=0",
    ]
    toks = [client.post(u).json()["token"] for u in urls[:2]]
    # Both build slots are held — both jobs parked in stage-1 resolution.
    assert entered.acquire(timeout=10)
    assert entered.acquire(timeout=10)
    toks.append(client.post(urls[2]).json()["token"])
    snaps = []
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30:
            snaps = [
                client.get(f"/drafts/asyncd/export/status/{t}").json() for t in toks
            ]
            if any(s["stage"] == "queued" for s in snaps):
                break
            time.sleep(0.05)
        assert any(s["stage"] == "queued" for s in snaps), snaps
    finally:
        release.set()
    for t in toks:
        assert _wait(client, f"/drafts/asyncd/export/status/{t}")["state"] == "done"


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
