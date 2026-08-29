"""remarkable_papers_send job_type — registration, the per-draft target
folder, and the plugin dispatch guards against a fake DispatchContext."""

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
from precis.workers.job_types import remarkable_papers_send as rps


def test_remarkable_papers_send_registered() -> None:
    spec = get_job_type("remarkable_papers_send")
    assert spec is not None
    assert spec.dispatch is not None  # runs via plugin dispatch, not claude
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires
    assert "remarkable_papers_send" in known_job_types()


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "remarkable_papers_send"
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


def test_target_folder_default_setting_and_override(hub: Hub) -> None:
    from precis.budget.settings import set_setting

    ctx = _FakeCtx(store=hub.live_store, meta={})
    # no setting → default base + per-draft segment
    assert rps._target_folder(ctx, {}, "173020") == "/Precis/173020"
    # app_settings value wins over the default base
    set_setting(hub.live_store, rps.TARGET_FOLDER_KEY, "/Reading")
    assert rps._target_folder(ctx, {}, "173020") == "/Reading/173020"
    # an explicit params.folder wins verbatim over the computed folder
    assert rps._target_folder(ctx, {"folder": "/Inbox"}, "173020") == "/Inbox"


def test_draft_segment_sanitises_unsafe_chars() -> None:
    assert rps._draft_segment("draft/../evil name!") == "draft evil name"
    assert rps._draft_segment("###") == "sources"


def test_dispatch_fails_without_credential(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    slug = _project_and_draft(hub)
    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("credential" in f for f in ctx.failures), ctx.failures


def test_dispatch_fails_on_unknown_draft(hub: Hub) -> None:
    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": "nope"}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("no draft" in f for f in ctx.failures)


def test_dispatch_fails_without_draft_param(hub: Hub) -> None:
    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("params.draft is required" in f for f in ctx.failures)


def test_dispatch_happy_path_two_present_sources(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=True), _entry("jones2022", present=True)]
    )
    # collect_cited_sources is imported inside _dispatch — patch it at the
    # source module so the local import picks up the fake.
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    calls: list[dict[str, Any]] = []

    def fake_send_pdf(path, *, folder, display_name, store, login):
        calls.append(
            {
                "path": path,
                "folder": folder,
                "display_name": display_name,
                "login": login,
            }
        )
        return SendResult(
            ok=True, folder=folder, name=display_name, returncode=0, output=""
        )

    from precis.export import remarkable as rm_mod

    monkeypatch.setattr(rm_mod, "send_pdf", fake_send_pdf)

    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert len(calls) == 2
    assert calls[0]["folder"] == f"/Precis/{slug}"
    assert calls[0]["display_name"] == "Title of smith2024"
    assert calls[1]["display_name"] == "Title of jones2022"
    assert any(k == "job_summary" for k, _ in ctx.events)
    assert ctx.meta_set["remarkable_count"] == 2
    assert ctx.meta_set["remarkable_folder"] == f"/Precis/{slug}"


def test_dispatch_reports_missing_and_fails_when_all_missing(
    hub: Hub, monkeypatch: Any
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)

    bundle = SourceBundle(
        entries=[
            _entry("smith2024", present=False, reason="not-on-host"),
            _entry("jones2022", present=False, reason="no-pdf"),
        ]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert any("not on this host: smith2024" in t for _, t in ctx.events)
    assert any("not on this host: jones2022" in t for _, t in ctx.events)
    assert any("none of the 2 cited source PDF" in f for f in ctx.failures)


def test_dispatch_no_cited_sources_fails(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)

    bundle = SourceBundle(entries=[])
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("cites no paper/patent/datasheet sources" in f for f in ctx.failures)


def test_dispatch_one_upload_fails_names_the_slug(hub: Hub, monkeypatch: Any) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)

    bundle = SourceBundle(
        entries=[_entry("smith2024", present=True), _entry("jones2022", present=True)]
    )
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    seen_slugs: list[str] = []

    def fake_send_pdf(path, *, folder, display_name, store, login):
        seen_slugs.append(display_name)
        ok = "smith" not in display_name
        return SendResult(
            ok=ok,
            folder=folder,
            name=display_name,
            returncode=0 if ok else 1,
            output="" if ok else "boom",
            error="" if ok else "rmapi upload failed",
        )

    from precis.export import remarkable as rm_mod

    monkeypatch.setattr(rm_mod, "send_pdf", fake_send_pdf)

    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert len(seen_slugs) == 2  # both were attempted
    assert any("smith2024" in f and "1 of 2" in f for f in ctx.failures), ctx.failures


@pytest.mark.parametrize("skipped_error", ["rmapi binary not installed"])
def test_dispatch_mid_run_credential_or_binary_loss(
    hub: Hub, monkeypatch: Any, skipped_error: str
) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    slug = _project_and_draft(hub)

    bundle = SourceBundle(entries=[_entry("smith2024", present=True)])
    from precis.export import sources as sources_mod

    monkeypatch.setattr(sources_mod, "collect_cited_sources", lambda store, ref: bundle)

    def fake_send_pdf(path, *, folder, display_name, store, login):
        return SendResult(
            ok=False,
            folder=folder,
            name=display_name,
            returncode=-1,
            output="",
            skipped=True,
            error=skipped_error,
        )

    from precis.export import remarkable as rm_mod

    monkeypatch.setattr(rm_mod, "send_pdf", fake_send_pdf)

    spec = get_job_type("remarkable_papers_send")
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any(skipped_error in f for f in ctx.failures), ctx.failures
