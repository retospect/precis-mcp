"""``draft_export`` job_type — render a draft to LaTeX and compile a PDF.

Deterministic, in-process work (no claude). Registered with a plugin
``dispatch`` so ``claude_inproc`` runs it directly (the executor calls
``spec.dispatch(ctx, spec)`` and skips the claude subprocess entirely):
``export_draft`` → ``compile_pdf``, streaming each step as ``job_event``
chunks so the run is followable on the task page, and landing the PDF path
in ``refs.meta`` + the ``job_summary``.

Started from the ``/drafts`` "export PDF" button, or by an agent::

    put(kind='job', job_type='draft_export', parent_id=<project todo id>,
        params={'draft': '<slug>'})

The docx path is synchronous (toolchain-free) and does **not** go through a
job — see ``precis.export.docx`` / the ``/drafts/{ident}/export.docx`` route.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        "format": {"type": "string"},  # reserved; only 'pdf' today
    },
    "required": ["draft"],
    "additionalProperties": False,
}


def _export_root() -> Path:
    """Fallback export location when there's no project workspace /
    ``PRECIS_ROOT``. ``PRECIS_EXPORT_DIR`` overrides; else a stable temp
    subtree so a dev host still works (the PDF path is reported either
    way)."""
    base = os.environ.get("PRECIS_EXPORT_DIR")
    return Path(base) if base else Path(tempfile.gettempdir()) / "precis-export"


def _resolve_out_dir(ctx: Any, slug: str) -> tuple[Path, bool]:
    """Where to write the export. Prefer the **project workspace** under
    ``PRECIS_ROOT`` — that's where the per-todo PDF viewer
    (``tasks._resolve_workspace_pdf``) looks for ``<entrypoint-stem>.pdf``,
    so the compiled ``main.pdf`` shows on the project's task page for free
    (export_draft writes ``main.tex``; the default workspace entrypoint is
    ``main.tex``). Fall back to a temp export dir when there's no
    ``PRECIS_ROOT`` or the project has no workspace.

    Returns ``(dir, in_workspace)``."""
    precis_root = os.environ.get("PRECIS_ROOT")
    if precis_root:
        try:
            from precis.utils.workspace import Workspace

            job = ctx.store.get_ref(kind="job", id=ctx.ref_id)
            parent_id = getattr(job, "parent_id", None)
            project = (
                ctx.store.get_ref(kind="todo", id=parent_id) if parent_id else None
            )
            ws = Workspace.from_meta(getattr(project, "meta", None)) if project else None
            if ws is not None:
                return ws.absolute_root(Path(precis_root)), True
        except Exception:  # pragma: no cover — fall back to temp
            log.warning(
                "draft_export: workspace resolve failed; using temp dir",
                exc_info=True,
            )
    return _export_root() / slug, False


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.
    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.export.compile import compile_pdf, have_latexmk
    from precis.export.latex import export_draft

    params = (ctx.meta or {}).get("params") or {}
    slug = str(params.get("draft") or "").strip()
    if not slug:
        ctx.record_failure("draft_export: params.draft is required")
        return
    ref = ctx.store.get_ref(kind="draft", id=slug)
    if ref is None:
        ctx.record_failure(f"draft_export: no draft {slug!r}")
        return

    out_dir, in_workspace = _resolve_out_dir(ctx, slug)
    where = (
        "project workspace (shows on the task page)"
        if in_workspace
        else "export dir"
    )
    ctx.append_chunk("job_event", f"exporting draft {slug!r} → {out_dir} [{where}]")
    try:
        result = export_draft(ctx.store, ref, target_dir=out_dir)
    except Exception as exc:
        log.warning("draft_export: render failed for %s", slug, exc_info=True)
        ctx.record_failure(f"draft_export: LaTeX render failed: {exc}")
        return
    for w in result.warnings:
        ctx.append_chunk("job_event", f"warn: {w}")
    ctx.append_chunk(
        "job_event",
        f"wrote {result.main_tex.name} + {result.bib.name}; "
        f"{len(result.cited_slugs)} citation(s)",
    )

    if not have_latexmk():
        ctx.append_chunk(
            "job_summary",
            f"LaTeX project exported to {out_dir} — latexmk not installed, "
            "PDF compile skipped (install mactex / texlive on the worker).",
        )
        ctx.set_meta(export_dir=str(out_dir), pdf=None)
        return

    cres = compile_pdf(out_dir)
    ctx.append_chunk("job_event", f"latexmk rc={cres.returncode}\n{cres.log_tail}")
    if cres.ok and cres.pdf is not None:
        ctx.append_chunk("job_summary", f"PDF compiled: {cres.pdf}")
        ctx.set_meta(export_dir=str(out_dir), pdf=str(cres.pdf))
    else:
        ctx.record_failure(
            f"draft_export: latexmk failed (rc={cres.returncode}); see the log event"
        )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("draft_export runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="draft_export",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Render a draft to LaTeX and compile a PDF (deterministic).",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
