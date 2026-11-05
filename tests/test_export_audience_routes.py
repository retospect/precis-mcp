"""Audience travels through the existing download/job/CLI export surfaces."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from collections.abc import MutableMapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from zipfile import ZipFile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("docx")

from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from precis.errors import BadInput
from precis.export import audience as audience_mod
from precis.store import Store
from precis.utils import handle_registry
from precis_web import export_jobs
from precis_web.routes import drafts


def _request(query: str = "", *, body: bytes = b"") -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body}

    return Request(
        {
            "type": "http",
            "method": "POST" if body else "GET",
            "path": "/drafts/audience-draft/export.docx",
            "query_string": query.encode("ascii"),
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "export_store": SimpleNamespace(
                drafts=SimpleNamespace(reading_order=lambda _id: [])
            ),
            "export_ref": SimpleNamespace(
                id=7, slug="audience-draft", title="Audience draft", meta={}
            ),
            "export_preflight": SimpleNamespace(
                retraction_blocked=False,
                figures_blocked=[],
                retraction_override=[],
                withheld_handles=frozenset(),
            ),
        },
        receive,
    )


@pytest.fixture(autouse=True)
def configured_web_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audience_mod, "web_base_url", lambda _store: "https://precis.example.test"
    )


@pytest.mark.parametrize("audience", [None, "dev", "external"])
def test_docx_audience_forwarding_and_download(
    audience: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.export import docx

    captured: dict[str, Any] = {}

    def export(_store: Any, _ref: Any, *, target_path: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        target_path.write_bytes(b"document")
        (target_path.parent / "supplement.zip").write_bytes(b"supplement")
        return SimpleNamespace(cited_slugs=[])

    monkeypatch.setattr(docx, "export_docx", export)
    monkeypatch.setattr(drafts.tempfile, "mkdtemp", lambda **_kw: str(tmp_path))
    query = urlencode({"audience": audience}) if audience else ""
    response = asyncio.run(drafts.export_docx_route(_request(query), "audience-draft"))
    assert isinstance(response, FileResponse)
    assert captured["audience"] == (audience or "dev")
    if audience == "external":
        assert response.media_type == "application/zip"
        with ZipFile(response.path) as bundle:
            document = next(n for n in bundle.namelist() if n.endswith(".docx"))
            assert bundle.read(document) == b"document"
            assert bundle.read("supplement.zip") == b"supplement"
    else:
        assert response.media_type == drafts._DOCX_MEDIA
        assert Path(response.path).read_bytes() == b"document"


@pytest.mark.parametrize("audience", ["dev", "external"])
def test_pdf_caches_only_dev_exports(
    audience: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.export import compile as compile_mod
    from precis.export import latex

    assert drafts._pdf_cache_dir(7, "v1") == drafts._pdf_cache_dir(
        7, "v1", audience="dev"
    )
    assert drafts._pdf_cache_dir(7, "v1") != drafts._pdf_cache_dir(
        7, "v1", audience="external"
    )
    seen: list[str] = []
    compiled: list[Path] = []

    def export(_store: Any, _ref: Any, *, target_dir: Path, **kwargs: Any) -> None:
        seen.append(kwargs["audience"])
        (target_dir / "supplement.zip").write_bytes(b"supplement")

    def compile_pdf(target_dir: Path) -> Any:
        compiled.append(target_dir)
        (target_dir / "main.pdf").write_bytes(b"%PDF fake")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(drafts, "_pdf_cache_dir", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(drafts, "_pdf_cache_token", lambda *_a: "v1")
    # Even existing artifacts must never skip an external rebuild.
    monkeypatch.setattr(drafts.tempfile, "mkdtemp", lambda **_kw: str(tmp_path))
    monkeypatch.setattr(latex, "export_draft", export)
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setattr(compile_mod, "compile_pdf", compile_pdf)
    for _ in range(2):
        response = asyncio.run(
            drafts.pdf(_request(f"audience={audience}"), "audience-draft")
        )
        assert isinstance(response, FileResponse)
        if audience == "external":
            assert response.media_type == "application/zip"
            with ZipFile(response.path) as bundle:
                assert bundle.read("supplement.zip") == b"supplement"
                assert any(n.endswith(".pdf") for n in bundle.namelist())
        else:
            assert response.media_type == "application/pdf"
    expected_builds = 2 if audience == "external" else 1
    assert seen == [audience] * expected_builds
    assert len(compiled) == expected_builds


@pytest.fixture
def pdf_build_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from precis.export import compile as compile_mod
    from precis.export import latex

    work = tmp_path / "precis-draft-external-test"
    work.mkdir()

    def export(_store: Any, _ref: Any, *, target_dir: Path, **_kwargs: Any) -> None:
        (target_dir / "main.tex").write_text("build source", encoding="utf-8")
        (target_dir / "supplement.zip").write_bytes(b"supplement")

    def compile_pdf(target_dir: Path) -> Any:
        (target_dir / "main.pdf").write_bytes(b"%PDF fake")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(drafts.tempfile, "mkdtemp", lambda **_kw: str(work))
    monkeypatch.setattr(drafts, "_pdf_cache_dir", lambda *_a, **_kw: work)
    monkeypatch.setattr(drafts, "_pdf_cache_token", lambda *_a: "v1")
    monkeypatch.setattr(latex, "export_draft", export)
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setattr(compile_mod, "compile_pdf", compile_pdf)
    monkeypatch.setattr(
        drafts.templates,
        "TemplateResponse",
        lambda _request, _template, context, *, status_code: Response(
            context["detail"], status_code=status_code
        ),
    )
    return work


@pytest.mark.parametrize("audience", ["dev", "external"])
def test_pdf_direct_download_cleans_external_directory_after_send(
    audience: str, pdf_build_directory: Path
) -> None:
    request = _request(f"audience={audience}")
    response = asyncio.run(drafts.pdf(request, "audience-draft"))
    assert isinstance(response, FileResponse)
    assert pdf_build_directory.exists()
    artifact = Path(response.path).read_bytes()
    messages: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        assert pdf_build_directory.exists()
        messages.append(message)

    asyncio.run(response(request.scope, request.receive, send))
    assert b"".join(m.get("body", b"") for m in messages) == artifact
    assert pdf_build_directory.exists() is (audience == "dev")


@pytest.mark.parametrize(
    ("failure", "state", "status"),
    [
        ("unavailable", "blocked", 503),
        ("refused", "blocked", 422),
        ("export", "error", None),
        ("compile-failed", "blocked", 500),
        ("compile", "error", None),
        ("bundle", "error", None),
    ],
)
def test_failed_background_pdf_build_cleans_external_directory(
    failure: str,
    state: str,
    status: int | None,
    pdf_build_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.export import compile as compile_mod
    from precis.export import latex

    def fail(*_a: Any, **_kw: Any) -> None:
        if failure == "refused":
            raise BadInput("private URL", next="remove the private URL")
        raise RuntimeError("build failed")

    if failure == "unavailable":
        monkeypatch.setattr(compile_mod, "have_latexmk", lambda: False)
    elif failure in {"refused", "export"}:
        monkeypatch.setattr(latex, "export_draft", fail)
    elif failure == "compile-failed":
        monkeypatch.setattr(
            compile_mod,
            "compile_pdf",
            lambda _target: SimpleNamespace(ok=False, log_tail="build failed"),
        )
    elif failure == "compile":
        monkeypatch.setattr(compile_mod, "compile_pdf", fail)
    else:
        monkeypatch.setattr(drafts, "_bundle_supplement", fail)

    monkeypatch.setattr(export_jobs, "_JOBS", {})
    runners: list[Any] = []
    # Capture only the job thread; run it after restoring real threads so
    # the handler's asyncio.to_thread calls still use their normal executor.
    with monkeypatch.context() as capture:
        capture.setattr(
            export_jobs.threading,
            "Thread",
            lambda **kw: SimpleNamespace(start=lambda: runners.append(kw["target"])),
        )
        job = export_jobs.start(
            _request(), "audience-draft", "pdf", drafts.pdf, b"audience=external"
        )
    runners[0]()
    assert job.state == state
    if status is not None:
        assert job.response is not None
        assert job.response.status_code == status
    assert not pdf_build_directory.exists()


@pytest.mark.parametrize(
    ("directory", "removed"),
    [
        ("precis-draft-external-test", True),
        ("precis-docx-test", True),
        ("precis-draft-pdf-cache", False),
    ],
)
def test_expired_export_job_cleans_scratch_directory(
    directory: str, removed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / directory
    work.mkdir()
    artifact = work / "external-bundle.zip"
    artifact.write_bytes(b"download")
    (work / "main.tex").write_text("build source", encoding="utf-8")
    job = export_jobs.ExportJob(
        token="expiry-test",
        ident="audience-draft",
        fmt="pdf",
        key="pdf:audience=external",
        state="done",
        response=FileResponse(artifact),
        finished=100.0,
    )
    monkeypatch.setattr(export_jobs, "_JOBS", {job.token: job})
    with export_jobs._LOCK:
        export_jobs._purge_locked(job.finished + export_jobs._TTL_S - 1)
        assert export_jobs._JOBS[job.token] is job
        assert artifact.read_bytes() == b"download"
        export_jobs._purge_locked(job.finished + export_jobs._TTL_S + 1)
    assert job.token not in export_jobs._JOBS
    assert work.exists() is not removed


def test_second_external_export_rebuilds_with_changed_record(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.export import compile as compile_mod

    monkeypatch.setattr(handle_registry, "_plugins_loaded", True)
    monkeypatch.setattr(handle_registry, "_plugin_kind_codes", {"pathway": "pw"})
    monkeypatch.setattr(handle_registry, "_plugin_chunk_codes", {})
    candidate = store.insert_ref(
        kind="structure", slug="rebuild-candidate", title="Original candidate"
    )
    pathway = store.insert_ref(
        kind="pathway",
        slug="rebuild-pathway",
        title="Stored pathway",
        meta={"candidate_ref": candidate.id},
    )
    ref, _ = store.drafts.create_draft(name="rebuild-report", title="Report")
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Evidence [pw{pathway.id}].",
        at={"last": True},
    )
    request = _request("audience=external")
    request.scope["export_store"] = store
    request.scope["export_ref"] = ref
    mkdtemp = drafts.tempfile.mkdtemp
    monkeypatch.setattr(
        drafts.tempfile, "mkdtemp", lambda **kw: mkdtemp(dir=tmp_path, **kw)
    )
    compiled: list[Path] = []

    def compile_pdf(target_dir: Path) -> Any:
        compiled.append(target_dir)
        (target_dir / "main.pdf").write_bytes((target_dir / "main.tex").read_bytes())
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setattr(compile_mod, "compile_pdf", compile_pdf)
    first = asyncio.run(drafts.pdf(request, "rebuild-report"))
    assert isinstance(first, FileResponse)
    with ZipFile(first.path) as bundle:
        old_pdf = bundle.read(next(n for n in bundle.namelist() if n.endswith(".pdf")))
        with ZipFile(io.BytesIO(bundle.read("supplement.zip"))) as supplement:
            entry = json.loads(supplement.read("S1.1/entry.json"))
            assert "Original candidate" in entry["description"]
    store.update_ref(candidate.id, title="Renamed candidate")
    refreshed = asyncio.run(drafts.pdf(request, "rebuild-report"))
    assert isinstance(refreshed, FileResponse)
    assert refreshed.path != first.path
    assert len(compiled) == 2
    with ZipFile(refreshed.path) as bundle:
        new_pdf = bundle.read(next(n for n in bundle.namelist() if n.endswith(".pdf")))
        assert new_pdf != old_pdf
        with ZipFile(io.BytesIO(bundle.read("supplement.zip"))) as supplement:
            entry = json.loads(supplement.read("S1.1/entry.json"))
            assert "Renamed candidate" in entry["description"]
            assert "Original candidate" not in entry["description"]


def test_pdf_cached_external_still_refuses_private_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.pdf").write_bytes(b"%PDF cached")
    (tmp_path / "supplement.zip").write_bytes(b"supplement")
    monkeypatch.setattr(drafts, "_pdf_cache_dir", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(drafts, "_pdf_cache_token", lambda *_a: "v1")
    monkeypatch.setattr(
        drafts.templates,
        "TemplateResponse",
        lambda _request, _template, context, *, status_code: Response(
            context["detail"], status_code=status_code
        ),
    )
    request = _request("audience=external")
    request.scope["export_store"].drafts.reading_order = lambda _id: [
        SimpleNamespace(
            handle="dc42", dc="dc42", text="https://precis.example.test/r/pw7", meta={}
        )
    ]
    response = asyncio.run(drafts.pdf(request, "audience-draft"))
    assert response.status_code == 422
    assert b"dc42" in response.body


@pytest.mark.parametrize("format", ["docx", "pdf"])
def test_exporter_supplement_refusal_names_chunk(
    format: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.export import compile as compile_mod
    from precis.export import docx, latex

    def refuse(*_a: Any, **_kw: Any) -> None:
        raise BadInput(
            "external export contains a private URL in chunk dc42 supplement",
            next="remove the private URL",
        )

    monkeypatch.setattr(docx, "export_docx", refuse)
    monkeypatch.setattr(latex, "export_draft", refuse)
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setattr(drafts, "_pdf_cache_dir", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(drafts, "_pdf_cache_token", lambda *_a: "v1")
    monkeypatch.setattr(drafts.tempfile, "mkdtemp", lambda **_kw: str(tmp_path))
    monkeypatch.setattr(
        drafts.templates,
        "TemplateResponse",
        lambda _request, _template, context, *, status_code: Response(
            context["detail"], status_code=status_code
        ),
    )
    handler = drafts.export_docx_route if format == "docx" else drafts.pdf
    response = asyncio.run(handler(_request("audience=external"), "audience-draft"))
    assert response.status_code == 422
    assert b"dc42" in response.body


@pytest.mark.parametrize("audience", [None, "external"])
def test_project_export_reuses_job_type_and_separates_audience(
    audience: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = urlencode({"sources": "1", **({"audience": audience} if audience else {})})
    request = _request(body=body.encode("ascii"))
    monkeypatch.setattr(drafts, "get_store", lambda _r: request.scope["export_store"])
    monkeypatch.setattr(drafts, "_draft_ref", lambda *_a: request.scope["export_ref"])
    monkeypatch.setattr(drafts, "_job_parent", lambda *_a: 1)
    monkeypatch.setattr(
        drafts,
        "draft_export_preflight",
        lambda *_a, **_kw: request.scope["export_preflight"],
    )
    captured: dict[str, Any] = {}

    async def redirect(
        _request: Any, verb: str, payload: dict[str, Any], **_kw: Any
    ) -> Response:
        assert verb == "put"
        captured.update(payload)
        return Response(status_code=303)

    monkeypatch.setattr(drafts, "redirect_or_error", redirect)
    response = asyncio.run(drafts.export_pdf_route(request, "audience-draft"))
    assert response.status_code == 303
    assert captured["job_type"] == "draft_export"
    assert captured["params"]["include_sources"] is True
    assert captured["params"].get("audience", "dev") == (audience or "dev")
    assert captured["idem_key"] == "draft_export:audience-draft:sources" + (
        ":external" if audience == "external" else ""
    )


def test_existing_job_schema_accepts_audience_and_rejects_unknown_choice() -> None:
    from precis.handlers.job import _validate_params
    from precis.workers.job_types.draft_export import SPEC

    for audience in (None, "dev", "external"):
        params = {"draft": "audience-draft"}
        if audience is not None:
            params["audience"] = audience
        _validate_params(params, SPEC.params_schema, job_type=SPEC.name)
    with pytest.raises(BadInput, match="audience"):
        _validate_params(
            {"draft": "audience-draft", "audience": "unknown"},
            SPEC.params_schema,
            job_type=SPEC.name,
        )


@pytest.mark.parametrize("in_workspace", [False, True])
def test_worker_audiences_have_separate_build_directories(
    in_workspace: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import draft_export

    monkeypatch.setattr(
        draft_export, "_resolve_out_dir", lambda *_a: (tmp_path, in_workspace)
    )
    dev, dev_workspace = draft_export._resolve_audience_out_dir(None, "draft", "dev")
    external, external_workspace = draft_export._resolve_audience_out_dir(
        None, "draft", "external"
    )
    assert dev_workspace == external_workspace == in_workspace
    assert dev == tmp_path
    assert external == tmp_path / "external"
    external.mkdir()
    (dev / "main.tex").write_text("collaborator links", encoding="utf-8")
    (external / "main.tex").write_text("external supplement", encoding="utf-8")
    assert (dev / "main.tex").read_text(encoding="utf-8") == "collaborator links"


def test_pdf_cache_invalidates_old_exports_and_changed_web_url() -> None:
    legacy = drafts._pdf_cache_dir(7, "v1").parent / "v1"
    dev = drafts._pdf_cache_dir(7, "v1", base_url="https://precis.example.test")
    moved = drafts._pdf_cache_dir(7, "v1", base_url="https://precis.example.test/new")
    external = drafts._pdf_cache_dir(7, "v1", audience="external")
    assert len({legacy, dev, moved, external}) == 4
    assert external.name != "v1-external"
    assert "precis.example.test" not in dev.name


@pytest.mark.parametrize("audience", [None, "external"])
def test_cli_export_audience(
    audience: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.cli import draft

    parser = argparse.ArgumentParser()
    draft.add_parser(parser.add_subparsers())
    argv = ["draft", "export", "audience-draft", "--out", str(tmp_path)]
    if audience is not None:
        argv += ["--audience", audience]
    args = parser.parse_args(argv)
    captured: dict[str, Any] = {}

    def export(*_a: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            warnings=[],
            source_bundle=None,
            main_tex=tmp_path / "main.tex",
            bib=tmp_path / "refs.bib",
            cited_slugs=[],
            acronyms={},
        )

    store = SimpleNamespace(
        get_ref=lambda **_kw: SimpleNamespace(id=7, slug="audience-draft"),
        close=lambda: None,
    )
    monkeypatch.setattr(draft, "resolve_dsn", lambda _dsn: "unused")
    monkeypatch.setattr(draft.Store, "connect", lambda _dsn: store)
    monkeypatch.setattr(draft, "export_draft", export)
    draft._run_export(args)
    assert captured["audience"] == (audience or "dev")
