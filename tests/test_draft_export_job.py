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
    assert spec.default_executor == "job_inproc"  # gr592838: off the LLM lane
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
    # gr592838 want (a): an ``executor=``-less put — the shape the web
    # export button and the gripe's own re-queue take — resolves the
    # deterministic ``job_inproc`` lane off ``claude_inproc``.
    jid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))
    job = hub.live_store.get_ref(kind="job", id=jid)
    assert job is not None
    assert job.meta["executor"] == "job_inproc"
    # …and an explicit claude_inproc opt-in stays accepted.
    out2 = JobHandler(hub=hub).put(
        job_type="draft_export",
        parent_id=pid,
        executor="claude_inproc",
        params={"draft": slug},
    )
    jid2 = int(out2.body.split("id=")[1].split()[0].rstrip(",.()"))
    job2 = hub.live_store.get_ref(kind="job", id=jid2)
    assert job2 is not None and job2.meta["executor"] == "claude_inproc"
    # bad params rejected at submit
    import pytest

    from precis.errors import BadInput

    with pytest.raises(BadInput, match="requires params.draft"):
        JobHandler(hub=hub).put(job_type="draft_export", parent_id=pid, params={})


def test_default_executor_must_be_compatible() -> None:
    """A ``default_executor`` outside ``compatible_executors`` would mint
    rows no lane may claim — ``__post_init__`` refuses it at spec
    build time (gr592838)."""
    import pytest

    from precis.workers.job_types import JobTypeSpec

    with pytest.raises(ValueError, match="default_executor"):
        JobTypeSpec(
            name="bogus",
            params_schema={},
            compatible_executors=frozenset({"job_inproc"}),
            requires=frozenset(),
            description="bogus",
            default_executor="claude_inproc",
            dispatch=lambda ctx, spec: None,
        )


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

    failure_classes: list[str | None] = field(default_factory=list)
    open_tags: list[str | None] = field(default_factory=list)

    def record_failure(
        self,
        reason: str,
        *,
        failure_class: str | None = None,
        open_tag: str | None = None,
    ) -> None:
        self.failures.append(reason)
        self.failure_classes.append(failure_class)
        self.open_tags.append(open_tag)

    def is_cancel_requested(self) -> bool:
        return False


def test_dispatch_exports_and_skips_pdf_without_latexmk(
    hub: Hub, monkeypatch: Any
) -> None:
    # Force the no-toolchain path (the gate container ships real TeX since
    # gr53208, so "latexmk absent" must be simulated, not assumed) → export
    # succeeds, PDF is skipped.
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
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


def test_dispatch_records_export_override_audit(hub: Hub, monkeypatch: Any) -> None:
    """gr491602: a bare export is a read and writes no ``ref_events`` rows,
    but the ``draft_export`` job is the explicit write op — it passes
    ``record_override=True``, so the ``export_override`` audit row still
    lands when a cited finding folded clean via an author's override."""
    import precis.export.compile as _compile
    from precis.handlers.finding import FindingHandler
    from precis.store.types import Tag
    from precis.utils import handle_registry
    from tests.factories import seed_paper_with_body

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _pid, slug = _make_project_and_draft(hub)
    seed_paper_with_body(hub.live_store, "jobov1")
    resp = FindingHandler(hub=hub).put(
        title="Pd/C catalyzes Suzuki coupling at RT",
        body="claim body",
        scope={},
        cited_in="jobov1",
    )
    fid = int(resp.body.split("id=")[1].split()[0].rstrip(",.()"))
    hub.live_store.update_ref(
        fid,
        meta_patch={
            "primary_cite_key": "jobov1",
            "dead_reason": "unacquirable",
            "unacquirable_override": {
                "by": "agent",
                "at": "2026-08-04T00:00:00+00:00",
                "note": "print-only 1962 monograph",
            },
        },
    )
    hub.live_store.add_tag(
        fid,
        Tag.closed("STATUS", "dead_chain"),
        set_by="chase",
        replace_prefix=True,
    )
    fi = handle_registry.format_handle("finding", fid)
    DraftHandler(hub=hub).put(
        id=slug,
        chunk_kind="paragraph",
        text=f"Claim [{fi}] holds.",
        at={"last": True},
    )

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    events = hub.live_store.events_for(ref.id, event="export_override")
    assert len(events) == 1
    assert events[0].payload["overridden"][0]["finding_ref_id"] == fid


def test_dispatch_records_retraction_override_when_flagged(
    hub: Hub, monkeypatch: Any
) -> None:
    """``params.ignore_retractions`` (set by the route once the user has
    already bypassed the export-gate block) makes the job re-derive the
    same no-network retraction report and thread its ``retracted`` list into
    the sources appendix — ``docs/backlog/retraction-override-appendix-
    trace.md``'s trace. No sources-appendix param → nothing to record it
    in, so it's a no-op without ``include_sources``."""
    import precis.export.retraction as retraction_mod
    from precis.export.retraction import CitedPaper, DraftRetractionReport

    _pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="paragraph", text="Some prose.", at={"last": True}
    )

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[
                CitedPaper(
                    ref_id=1, slug="smith2024", title="Bad Paper", status="retracted"
                )
            ]
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)

    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None

    ctx = _FakeCtx(
        store=hub.store,
        meta={"params": {"draft": slug, "include_sources": True}},
    )
    spec.dispatch(ctx, spec)
    assert not ctx.failures, ctx.failures

    ctx_override = _FakeCtx(
        store=hub.store,
        meta={
            "params": {
                "draft": slug,
                "include_sources": True,
                "ignore_retractions": True,
            }
        },
    )
    spec.dispatch(ctx_override, spec)
    assert not ctx_override.failures, ctx_override.failures
    assert not any("retraction override" in t for _k, t in ctx.events)
    assert any("retraction override" in t for _k, t in ctx_override.events)


def test_dispatch_fails_on_uncleared_figure(hub: Hub) -> None:
    """The clearance gate: a third-party figure without a
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


def test_placeholder_figures_waives_imageless_gate(hub: Hub, monkeypatch: Any) -> None:
    """An image-less (caption-only) figure blocks the export by default, but
    the ``placeholder_figures`` opt-in waives it — the same waiver
    ``remarkable_send`` offers, previously missing from ``draft_export``.
    Proven by dispatch reaching the later latexmk step and logging a
    placeholder warning event."""
    from precis.export import compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: False)
    _pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="figure", text="Fig 1: planned TEM observation."
    )
    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None

    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    spec.dispatch(ctx, spec)
    assert any("not cleared" in f for f in ctx.failures), ctx.failures

    ctx2 = _FakeCtx(
        store=hub.store,
        meta={"params": {"draft": slug, "placeholder_figures": True}},
    )
    spec.dispatch(ctx2, spec)
    assert not any("not cleared" in f for f in ctx2.failures), ctx2.failures
    assert any("placeholder" in t for _k, t in ctx2.events), ctx2.events


def test_placeholder_figures_withholds_a_licensing_block(
    hub: Hub, monkeypatch: Any
) -> None:
    """Under ``placeholder_figures`` a third-party figure WITH a real image and
    no granted permission no longer fails the job: its handle is passed to the
    exporter as ``withheld_figures`` (rendered as a box, never embedded).
    Without the opt-in it stays a hard stop."""
    import base64

    from precis.export import compile as compile_mod
    from precis.export import latex as latex_mod

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
    assert spec is not None and spec.dispatch is not None
    seen: dict[str, Any] = {}

    def _fake_export(*_a: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(latex_mod, "export_draft", _fake_export)
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)

    # Without the opt-in: still a hard stop, exporter never reached.
    ctx0 = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    spec.dispatch(ctx0, spec)
    assert any("not cleared" in f for f in ctx0.failures), ctx0.failures
    assert not seen

    ctx = _FakeCtx(
        store=hub.store, meta={"params": {"draft": slug, "placeholder_figures": True}}
    )
    spec.dispatch(ctx, spec)
    assert not any("not cleared" in f for f in ctx.failures), ctx.failures
    assert any("withheld" in t for _k, t in ctx.events), ctx.events
    handle = next(iter(seen["withheld_figures"]))
    assert handle.startswith("dc")
    assert any(handle in t for _k, t in ctx.events), ctx.events


# ── cite-drift gate (docs/backlog/cite-pins-hub-version.md, task 3) ─────


def _cite_hub_in_draft(hub: Hub, slug: str, sentence: str) -> tuple[int, str]:
    """Mint a claim hub with ``sentence`` and cite it (``[fi<id>]``) from a
    fresh paragraph appended to draft ``slug``. Returns ``(hub_ref_id,
    fi_handle)``."""
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub
    from precis.utils import handle_registry

    hub_id = mint_hub(hub.live_store, CanonicalClaim(sentence=sentence, scope={}))
    fi = handle_registry.format_handle("finding", hub_id)
    DraftHandler(hub=hub).put(
        id=slug,
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"last": True},
    )
    return hub_id, fi


def test_dispatch_fails_on_drifted_cite(hub: Hub) -> None:
    """A drifted cite (the hub was reworded since the citing prose was
    written) BLOCKS the export — same precedent as the figure clearance
    gate — and the failure names the offending chunk and both
    statements."""
    from precis.taproot.hub import refine_claim_sentence

    _pid, slug = _make_project_and_draft(hub)
    old_sentence = "Graphene has a tensile strength of 130 GPa."
    hub_id, _fi = _cite_hub_in_draft(hub, slug, old_sentence)

    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    dc = hub.live_store.drafts.reading_order(ref.id)[-1].dc

    result = refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert any("drifted" in f for f in ctx.failures), ctx.failures
    failure = next(f for f in ctx.failures if "drifted" in f)
    assert dc in failure
    assert old_sentence in failure
    assert result["new_title"] in failure
    # Blocking: nothing exported.
    assert not ctx.meta_set


def test_dispatch_succeeds_with_unsigned_hub_advisory_only(
    hub: Hub, monkeypatch: Any
) -> None:
    """A cite resting on an unsigned hub (the common case — most hubs are
    ``candidate``) is advisory only: the export succeeds and reports a
    count, never blocks."""
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _pid, slug = _make_project_and_draft(hub)
    _cite_hub_in_draft(hub, slug, "Graphene has a tensile strength of 130 GPa.")

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert any("unsigned" in t for _k, t in ctx.events)
    assert any("1 cited claim hub" in t for _k, t in ctx.events)


def test_dispatch_resyncs_links_before_reading_edges(
    hub: Hub, monkeypatch: Any
) -> None:
    """A chunk-scoped write can leave another chunk's edge stale; export
    runs a full link sync first, so a cite whose edge is missing is still
    seen by the edge-reading gates (here: the unsigned-hub advisory)."""
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _pid, slug = _make_project_and_draft(hub)
    hub_id, _fi = _cite_hub_in_draft(hub, slug, "Graphene conducts heat well.")
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    for link in hub.live_store.links_for(ref.id, direction="out", relation="cites"):
        hub.live_store.remove_link(
            src_ref_id=ref.id,
            src_pos=link.src_ord,
            dst_ref_id=link.dst_ref_id,
            dst_pos=link.dst_ord,
            relation="cites",
        )

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not ctx.failures, ctx.failures
    assert any("1 cited claim hub" in t for _k, t in ctx.events)
    dsts = {
        link.dst_ref_id
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
    }
    assert hub_id in dsts


def test_dispatch_unstamped_cite_never_blocks(hub: Hub, monkeypatch: Any) -> None:
    """A pre-pin (unstamped) cite must never block an export — only true
    drift (a stamped version that has since moved) does."""
    import precis.export.compile as _compile
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _pid, slug = _make_project_and_draft(hub)
    hub_id = mint_hub(
        hub.live_store,
        CanonicalClaim(
            sentence="Graphene has a tensile strength of 130 GPa.", scope={}
        ),
    )
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    # Hand-write a legacy cites edge, bypassing sync_draft_links — the
    # shape every edge had before the pin existed.
    hub.live_store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=hub_id,
        relation="cites",
        src_pos=0,
        set_by="agent",
        meta={"auto": "mention"},
    )

    spec = get_job_type("draft_export")
    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": slug}})
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(ctx, spec)

    assert not any("drifted" in f for f in ctx.failures), ctx.failures


def test_put_rejects_traversal_slug(hub: Hub) -> None:
    """A draft slug becomes an export path segment (draft_export writes
    <export-root>/<slug>/main.tex), so path separators / `.`/`..` are rejected
    at creation — a traversal draft like `../../etc/x` can't be minted."""
    import pytest

    from precis.errors import BadInput

    pid, _ = _make_project_and_draft(hub)
    for bad in ("../../etc/evil", "a/b", "..", "x\\y"):
        with pytest.raises(BadInput, match="path separator"):
            DraftHandler(hub=hub).put(id=bad, title="T", project=pid)


def test_resolve_out_dir_contains_traversal_slug(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Defense in depth at the write sink: even if a traversal slug reached
    the export (e.g. a pre-existing draft), the temp-dir fallback refuses to
    resolve outside the export root."""
    import pytest

    from precis.workers.job_types import draft_export

    monkeypatch.delenv("PRECIS_ROOT", raising=False)
    monkeypatch.setenv("PRECIS_EXPORT_DIR", str(tmp_path))
    ctx = _FakeCtx(store=None, meta={})
    # A plain slug resolves inside the export root...
    out, in_ws = draft_export._resolve_out_dir(ctx, "nanotrans")
    assert out == (tmp_path / "nanotrans").resolve()
    assert in_ws is False
    # ...an escaping one is rejected before any mkdir/write.
    with pytest.raises(ValueError, match="escapes the export root"):
        draft_export._resolve_out_dir(ctx, "../../../etc/evil")


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
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
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
    assert any("todo page" in t for _k, t in ctx.events)


# ── per-draft export lock + job_inproc lane (gr592838) ─────────────


def _job_ctx(hub: Hub, job_id: int) -> Any:
    """A real ``DispatchContext`` for ``job_id`` — record_failure writes
    through to the job row (STATUS/meta/tags/bubble), which the fake
    can't see."""
    from precis.workers.executors.claude_inproc import _build_dispatch_context

    job = hub.live_store.get_ref(kind="job", id=job_id)
    assert job is not None
    return _build_dispatch_context(
        hub.live_store, job.id, job.title, dict(job.meta or {})
    )


def test_second_export_waits_for_first_lock(hub: Hub, monkeypatch: Any) -> None:
    """gr592838: two draft_export jobs for one draft claimed in the same
    agentlane sweep ran latexmk in the same export dir and clobbered
    each other's aux/log files. The per-draft advisory lock serializes
    them: while a sibling holds it, dispatch must not reach
    ``compile_pdf``; a wait outliving the budget fails infra instead of
    colliding."""
    import threading
    from types import SimpleNamespace

    import psycopg

    import precis.export.compile as _compile
    from precis.workers.job_types import draft_export

    monkeypatch.setattr(_compile, "have_latexmk", lambda: True)
    monkeypatch.setattr(draft_export, "_LOCK_POLL_S", 0.05)
    monkeypatch.setattr(draft_export, "_LOCK_WAIT_S", 30.0)

    pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="paragraph", text="prose.", at={"last": True}
    )
    jout = JobHandler(hub=hub).put(
        job_type="draft_export", parent_id=pid, params={"draft": slug}
    )
    jid = int(jout.body.split("id=")[1].split()[0].rstrip(",.()"))
    ctx = _job_ctx(hub, jid)

    compiled = threading.Event()

    def _fake_compile(out_dir: Any) -> Any:
        compiled.set()
        return SimpleNamespace(
            ok=True, pdf=out_dir / "main.pdf", returncode=0, log_tail=""
        )

    monkeypatch.setattr(_compile, "compile_pdf", _fake_compile)

    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None
    dsn = hub.live_store.dsn
    assert dsn is not None
    holder = psycopg.connect(dsn)
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"draft_export:{slug}",),
        )
        worker = threading.Thread(target=spec.dispatch, args=(ctx, spec), daemon=True)
        worker.start()
        # Still queued behind the sibling's lock: no compile while held.
        assert not compiled.wait(2.0)
        holder.rollback()  # releases the lock
    finally:
        holder.close()
    worker.join(timeout=30.0)
    assert not worker.is_alive()
    assert compiled.is_set()
    job = hub.live_store.get_ref(kind="job", id=jid)
    assert job is not None
    assert job.meta.get("failure_class") is None

    # Same contention past the wait budget → infra failure, not a clash.
    monkeypatch.setattr(draft_export, "_LOCK_WAIT_S", 0.1)
    jout2 = JobHandler(hub=hub).put(
        job_type="draft_export", parent_id=pid, params={"draft": slug}
    )
    jid2 = int(jout2.body.split("id=")[1].split()[0].rstrip(",.()"))
    ctx2 = _job_ctx(hub, jid2)
    holder2 = psycopg.connect(dsn)
    try:
        holder2.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"draft_export:{slug}",),
        )
        spec.dispatch(ctx2, spec)
    finally:
        holder2.rollback()
        holder2.close()
    job2 = hub.live_store.get_ref(kind="job", id=jid2)
    assert job2 is not None
    assert job2.meta["failure_class"] == "infra"
    assert "export lock" in str(job2.meta.get("error"))


def test_job_inproc_accepted_and_no_latexmk_is_infra(
    hub: Hub, monkeypatch: Any
) -> None:
    """gr592838: a PDF export needs no LLM, so ``executor='job_inproc'``
    is accepted at put. A job_inproc claim on a TeX-less host can't
    compile — it fails ``failure_class='infra'`` stamped
    ``infra:no-latexmk`` (a job_bubble.INFRA_FAILURE_TAGS member), so the
    first failure does NOT latch ``child-failed`` on the parent: the
    bounded infra retry re-arms instead."""
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="paragraph", text="prose.", at={"last": True}
    )

    out = JobHandler(hub=hub).put(
        job_type="draft_export",
        parent_id=pid,
        executor="job_inproc",
        params={"draft": slug},
    )
    assert "id=" in out.body  # put accepted (was BadInput pre-fix)
    jid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))
    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(_job_ctx(hub, jid), spec)

    job = hub.live_store.get_ref(kind="job", id=jid)
    assert job is not None
    assert job.meta["failure_class"] == "infra"
    job_tags = {str(t) for t in hub.live_store.tags_for(jid)}
    assert "STATUS:failed" in job_tags
    assert "infra:no-latexmk" in job_tags
    parent_tags = {str(t) for t in hub.live_store.tags_for(pid)}
    assert f"child-failed:{jid}" not in parent_tags


def test_lock_timeout_stamps_infra_lock_timeout_tag(hub: Hub, monkeypatch: Any) -> None:
    """gr592838 review td677026: ``job_bubble`` classifies infra by open
    tag, not ``failure_class`` — a lock wait that outlives the budget is
    contention, not content, so the failure must stamp the
    ``infra:lock-timeout`` member of INFRA_FAILURE_TAGS and the first
    lapse must NOT latch ``child-failed`` on the parent."""
    import psycopg

    import precis.export.compile as _compile
    from precis.utils.job_bubble import INFRA_FAILURE_TAGS
    from precis.workers.job_types import draft_export

    assert "infra:lock-timeout" in INFRA_FAILURE_TAGS
    monkeypatch.setattr(_compile, "have_latexmk", lambda: True)
    monkeypatch.setattr(draft_export, "_LOCK_POLL_S", 0.05)
    monkeypatch.setattr(draft_export, "_LOCK_WAIT_S", 0.1)

    pid, slug = _make_project_and_draft(hub)
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind="paragraph", text="prose.", at={"last": True}
    )
    out = JobHandler(hub=hub).put(
        job_type="draft_export", parent_id=pid, params={"draft": slug}
    )
    jid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))
    ctx = _job_ctx(hub, jid)

    dsn = hub.live_store.dsn
    assert dsn is not None
    holder = psycopg.connect(dsn)
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"draft_export:{slug}",),
        )
        spec = get_job_type("draft_export")
        assert spec is not None and spec.dispatch is not None
        spec.dispatch(ctx, spec)
    finally:
        holder.rollback()
        holder.close()

    job = hub.live_store.get_ref(kind="job", id=jid)
    assert job is not None
    assert job.meta["failure_class"] == "infra"
    assert "export lock" in str(job.meta.get("error"))
    job_tags = {str(t) for t in hub.live_store.tags_for(jid)}
    assert "STATUS:failed" in job_tags
    assert "infra:lock-timeout" in job_tags
    parent_tags = {str(t) for t in hub.live_store.tags_for(pid)}
    assert f"child-failed:{jid}" not in parent_tags
