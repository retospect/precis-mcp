"""remarkable_reading_send job_type — registration, the per-source typeset +
compile + upload loop, and its gates (credential / latexmk / cited-set)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.export.remarkable import SendResult
from precis.export.sources import SourceBundle, SourceEntry
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.workers.job_types import get_job_type, known_job_types


def test_remarkable_reading_send_registered() -> None:
    spec = get_job_type("remarkable_reading_send")
    assert spec is not None
    assert spec.dispatch is not None  # runs via plugin dispatch, not claude
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires
    assert "remarkable_reading_send" in known_job_types()


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "remarkable_reading_send"
    events: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    meta_set: dict[str, Any] = field(default_factory=dict)

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)


def _project_and_draft(hub: Hub) -> str:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    DraftHandler(hub=hub).put(id="d1", title="T", project=pid)
    DraftHandler(hub=hub).put(
        id="d1", chunk_kind="paragraph", text="prose.", at={"last": True}
    )
    return "d1"


def _seed_paper(store: Any, slug: str, title: str | None = None) -> None:
    store.insert_ref(
        kind="paper", slug=slug, title=title or f"Title of {slug}", provider="manual"
    )


def _entry(slug: str, *, present: bool, reason: str = "") -> SourceEntry:
    return SourceEntry(
        slug=slug,
        kind="paper",
        title=f"Title of {slug}",
        authors="A and B",
        year=2020,
        pdf_sha256="deadbeef" if present else None,
        local_path=Path(f"/tmp/{slug}.pdf") if present else None,
        reason="" if present else (reason or "not-on-host"),
    )


def _armed_compile(monkeypatch: Any, tmp_path: Path, *, ok: bool = True) -> Path:
    """Arm ``have_latexmk``/``compile_pdf`` for a happy compile; returns the
    stub PDF path."""
    from precis.export import compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    pdf_path = tmp_path / "main.pdf"
    pdf_path.write_bytes(b"%PDF")
    monkeypatch.setattr(
        compile_mod,
        "compile_pdf",
        lambda target_dir, **kw: compile_mod.CompileResult(
            ok=ok,
            pdf=pdf_path if ok else None,
            returncode=0 if ok else 1,
            log_tail="" if ok else "! Undefined control sequence.",
        ),
    )
    return pdf_path


def _stub_export(
    monkeypatch: Any, *, has_original: bool = True, chunk_count: int = 2
) -> list[Any]:
    """Monkeypatch ``export_reading_edition`` to a no-op stub; returns the
    list of ``original_pdf`` values it was called with, in call order."""
    from precis.export import reading_edition as re_mod

    seen: list[Any] = []

    def fake_export(
        store: Any, sref: Any, target_dir: Any, *, original_pdf: Any
    ) -> Any:
        seen.append(original_pdf)
        return re_mod.ReadingEditionResult(
            chunk_count=chunk_count,
            claim_count=0,
            has_original=original_pdf is not None,
        )

    monkeypatch.setattr(re_mod, "export_reading_edition", fake_export)
    return seen


def _stub_send(
    monkeypatch: Any, *, ok: bool = True, skipped: bool = False
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_send_pdf(
        path: Any, *, folder: str, display_name: str, store: Any, login: Any
    ) -> SendResult:
        calls.append(
            {
                "path": path,
                "folder": folder,
                "display_name": display_name,
                "login": login,
            }
        )
        return SendResult(
            ok=ok,
            folder=folder,
            name=display_name,
            returncode=0 if ok else 1,
            output="" if ok else "boom",
            skipped=skipped,
            error=""
            if ok
            else ("rmapi binary not installed" if skipped else "rmapi upload failed"),
        )

    from precis.export import remarkable as rm_mod

    monkeypatch.setattr(rm_mod, "send_pdf", fake_send_pdf)
    return calls


# ── gates ────────────────────────────────────────────────────────────


def test_dispatch_fails_without_draft_param(hub: Hub) -> None:
    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("params.draft is required" in f for f in ctx.failures)


def test_dispatch_fails_on_unknown_draft(hub: Hub) -> None:
    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": "nope"}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("no draft" in f for f in ctx.failures)


def test_dispatch_fails_without_credential(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("credential" in f for f in ctx.failures), ctx.failures


def test_dispatch_fails_without_latexmk(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    from precis.export import compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: False)
    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("latexmk" in f for f in ctx.failures), ctx.failures


def test_dispatch_no_cited_sources_fails(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _armed_compile(monkeypatch, tmp_path)

    bundle = SourceBundle(entries=[])
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("cites no paper/patent/datasheet sources" in f for f in ctx.failures)


def test_dispatch_source_resolution_failure_reported(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _armed_compile(monkeypatch, tmp_path)

    from precis.export import sources as sources_mod

    def _boom(store: Any, ref: Any) -> Any:
        raise RuntimeError("bad chunk data")

    monkeypatch.setattr(sources_mod, "collect_cited_sources", _boom)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("source resolution failed" in f for f in ctx.failures)


# ── source param ─────────────────────────────────────────────────────


def test_dispatch_source_param_not_in_cited_set_fails(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _armed_compile(monkeypatch, tmp_path)

    bundle = SourceBundle(entries=[_entry("smith2024", present=True)])
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(
        store=hub.live_store, meta={"params": {"draft": slug, "source": "nope"}}
    )
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("nope" in f and "not among" in f for f in ctx.failures), ctx.failures


def test_dispatch_source_param_filters_to_one(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")
    _seed_paper(hub.live_store, "jones2022")

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=True), _entry("jones2022", present=True)]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    seen = _stub_export(monkeypatch)
    _armed_compile(monkeypatch, tmp_path)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(
        store=hub.live_store, meta={"params": {"draft": slug, "source": "jones2022"}}
    )
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert len(seen) == 1
    assert len(calls) == 1
    assert calls[0]["display_name"] == "Title of jones2022 — reading"


# ── happy path / not-on-host / compile failure / mid-run skip ────────


def test_dispatch_happy_path_two_sources(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")
    _seed_paper(hub.live_store, "jones2022")

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=True), _entry("jones2022", present=True)]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    seen = _stub_export(monkeypatch)
    _armed_compile(monkeypatch, tmp_path)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert len(seen) == 2
    assert len(calls) == 2
    assert calls[0]["display_name"] == "Title of smith2024 — reading"
    assert calls[1]["display_name"] == "Title of jones2022 — reading"
    assert calls[0]["folder"] == f"/Precis/{slug}"
    assert any(k == "job_summary" for k, _ in ctx.events)
    assert ctx.meta_set["remarkable_count"] == 2
    assert ctx.meta_set["remarkable_folder"] == f"/Precis/{slug}"


def test_dispatch_not_on_host_source_still_builds(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    """A source with no local PDF still gets a reading edition — just
    ``original_pdf=None`` — the per-host caveat this job exists to honour."""
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "jones2022")

    bundle = SourceBundle(
        entries=[_entry("jones2022", present=False, reason="not-on-host")]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    seen = _stub_export(monkeypatch, has_original=False)
    _armed_compile(monkeypatch, tmp_path)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert seen == [None]  # original_pdf=None reached export_reading_edition
    assert len(calls) == 1
    assert ctx.meta_set["remarkable_count"] == 1


def test_dispatch_unresolved_entry_skipped(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")

    bundle = SourceBundle(
        entries=[
            SourceEntry("ghost", "", "ghost", "", None, None, None, "unresolved"),
            _entry("smith2024", present=True),
        ]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    seen = _stub_export(monkeypatch)
    _armed_compile(monkeypatch, tmp_path)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert len(seen) == 1  # only smith2024 was typeset
    assert len(calls) == 1
    assert any("skipping ghost" in t for _, t in ctx.events)


def test_dispatch_zero_chunks_no_original_skips_source(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=False, reason="not-on-host")]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    from precis.export import reading_edition as re_mod

    monkeypatch.setattr(
        re_mod,
        "export_reading_edition",
        lambda store, sref, target_dir, *, original_pdf: re_mod.ReadingEditionResult(
            chunk_count=0, claim_count=0, has_original=False
        ),
    )
    _armed_compile(monkeypatch, tmp_path)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not calls  # nothing was ever sent
    assert any("nothing to typeset: smith2024" in t for _, t in ctx.events)
    assert any("nothing was sent" in f for f in ctx.failures)


def test_dispatch_compile_failure_continues_and_fails_at_end(
    hub: Hub, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")
    _seed_paper(hub.live_store, "jones2022")

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=True), _entry("jones2022", present=True)]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)
    _stub_export(monkeypatch)

    from precis.export import compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    pdf_path = tmp_path / "main.pdf"
    pdf_path.write_bytes(b"%PDF")
    calls_n = {"n": 0}

    def fake_compile(target_dir: Any, **kw: Any) -> Any:
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            return compile_mod.CompileResult(
                ok=False,
                pdf=None,
                returncode=1,
                log_tail="! Undefined control sequence.",
            )
        return compile_mod.CompileResult(
            ok=True, pdf=pdf_path, returncode=0, log_tail=""
        )

    monkeypatch.setattr(compile_mod, "compile_pdf", fake_compile)
    calls = _stub_send(monkeypatch)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert len(calls) == 1  # only jones2022 made it to send_pdf
    assert any("compile failed: smith2024" in t for _, t in ctx.events)
    assert any("smith2024" in f and "1 source(s) failed" in f for f in ctx.failures), (
        ctx.failures
    )
    assert "remarkable_count" not in ctx.meta_set  # partial failure — no summary meta


@pytest.mark.parametrize("skipped_error", ["rmapi binary not installed"])
def test_dispatch_mid_run_credential_or_binary_loss(
    hub: Hub, monkeypatch: Any, tmp_path: Path, skipped_error: str
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)
    _seed_paper(hub.live_store, "smith2024")

    bundle = SourceBundle(entries=[_entry("smith2024", present=True)])
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)
    _stub_export(monkeypatch)
    _armed_compile(monkeypatch, tmp_path)
    _stub_send(monkeypatch, ok=False, skipped=True)

    spec = get_job_type("remarkable_reading_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any(skipped_error in f for f in ctx.failures), ctx.failures
