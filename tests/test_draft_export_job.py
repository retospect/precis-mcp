"""draft_export job_type — registration, submit through JobHandler, and
the plugin dispatch (export → compile) against a fake DispatchContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.job import JobHandler
from precis.handlers.todo import TodoHandler
from precis.workers.job_types import get_job_type, known_job_types


def test_draft_export_registered() -> None:
    spec = get_job_type("draft_export")
    assert spec is not None
    assert spec.dispatch is not None  # runs via plugin dispatch, not claude
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires  # deterministic — no executor capabilities
    assert "draft_export" in known_job_types()


def _make_project_and_draft(hub: Hub) -> tuple[int, str]:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    DraftHandler(hub=hub).put(id="d1", title="T", project=pid)
    return pid, "d1"


def test_submit_export_job_through_handler(hub: Hub) -> None:
    pid, slug = _make_project_and_draft(hub)
    out = JobHandler(hub=hub).put(
        job_type="draft_export", parent_id=pid, params={"draft": slug}
    )
    assert "id=" in out.body  # a job row was created
    # bad params rejected at submit
    import pytest

    from precis.errors import BadInput

    with pytest.raises(BadInput, match="requires params.draft"):
        JobHandler(hub=hub).put(job_type="draft_export", parent_id=pid, params={})


# ── plugin dispatch against a fake context ────────────────────────


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "draft_export"
    events: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    meta_set: dict[str, Any] = field(default_factory=dict)
    status: str = "running"

    def set_status(self, v: str) -> None:
        self.status = v

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)

    def is_cancel_requested(self) -> bool:
        return False


def test_dispatch_exports_and_skips_pdf_without_latexmk(hub: Hub) -> None:
    # latexmk isn't on the test host → export succeeds, PDF is skipped
    # (the deterministic path we can verify without a TeX toolchain).
    _pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="paragraph", text="Some prose.", at={"last": True}
    )
    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert not ctx.failures, ctx.failures
    # exported the project, then reported the skip in the summary
    kinds = [k for k, _ in ctx.events]
    assert "job_event" in kinds
    summaries = [t for k, t in ctx.events if k == "job_summary"]
    assert summaries and "skipped" in summaries[0].lower()
    assert "export_dir" in ctx.meta_set


def test_dispatch_fails_on_uncleared_figure(hub: Hub) -> None:
    """The clearance gate (ADR 0034 §4): a third-party figure without a
    granted permission must not ship — the export fails before render."""
    import base64

    _pid, slug = _make_project_and_draft(hub)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    DraftHandler(hub=hub).put(
        id=slug,
        chunk_kind="figure",
        text="Fig 1 (borrowed).",
        image=png,
        origin="third_party",
        permission={"publisher": "X", "permission_id": "Y", "status": "requested"},
    )
    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("not cleared" in f for f in ctx.failures), ctx.failures


def test_dispatch_fails_on_unknown_draft(hub: Hub) -> None:
    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": "nope"}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert any("no draft" in f for f in ctx.failures)


def test_dispatch_targets_project_workspace(
    hub: Hub, monkeypatch: Any, tmp_path: Any
) -> None:
    """With PRECIS_ROOT set and the project owning a workspace, the export
    lands in the workspace dir (where the task-page PDF viewer looks),
    not a temp dir."""
    monkeypatch.setenv("PRECIS_ROOT", str(tmp_path))
    pid = int(
        TodoHandler(hub=hub)
        .put(
            text="proj",
            meta={
                "workspace": {
                    "path": "projects/p1",
                    "format": "tex",
                    "entrypoint": "main.tex",
                }
            },
        )
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    DraftHandler(hub=hub).put(id="p1", title="T", project=pid)
    DraftHandler(hub=hub).put(
        id="p1", chunk_kind="paragraph", text="prose.", at={"last": True}
    )
    jout = JobHandler(hub=hub).put(
        job_type="draft_export", parent_id=pid, params={"draft": "p1"}
    )
    jid = int(jout.body.split("id=")[1].split()[0].rstrip(",.()"))

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": "p1"}}, ref_id=jid)
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)
    assert not ctx.failures, ctx.failures
    # main.tex written into the project workspace under PRECIS_ROOT
    assert (tmp_path / "projects" / "p1" / "main.tex").is_file()
    assert any("task page" in t for _k, t in ctx.events)
