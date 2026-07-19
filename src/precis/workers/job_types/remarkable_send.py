"""``remarkable_send`` job_type — export a draft in reMarkable mode and
upload the PDF to the tablet.

Deterministic, in-process work (no claude), like ``draft_export``: render
the draft with ``remarkable=True`` (RM2 page geometry + source citations as
self-contained footnotes), compile the PDF, then push it to the reMarkable
cloud via ``precis.export.remarkable.send_pdf`` (the ``rmapi`` CLI). Each
step streams as a ``job_event`` so the run is followable on the task page.

Started from the ``/drafts`` "Send to reMarkable" button (shown only when a
device credential is configured), or by an agent::

    put(kind='job', job_type='remarkable_send', parent_id=<project todo id>,
        params={'draft': '<slug>'})

The destination folder is the ``remarkable.target_folder`` app_setting
(default ``/Precis``); the device credential lives in the secrets vault
(``REMARKABLE_RMAPI_CONFIG`` / ``REMARKABLE_TOKEN``), never in app_settings.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: app_settings key for the tablet destination folder (non-secret knob).
TARGET_FOLDER_KEY = "remarkable.target_folder"
_DEFAULT_FOLDER = "/Precis"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        # Override the app_settings destination folder for this one send.
        "folder": {"type": "string"},
    },
    "required": ["draft"],
    "additionalProperties": False,
}


def _target_folder(ctx: Any, params: dict[str, Any]) -> str:
    """The destination folder: an explicit ``params.folder`` wins, else the
    ``remarkable.target_folder`` app_setting, else the default."""
    override = str(params.get("folder") or "").strip()
    if override:
        return override
    try:
        from precis.budget.settings import get_setting

        return (get_setting(ctx.store, TARGET_FOLDER_KEY) or _DEFAULT_FOLDER).strip()
    except Exception:  # pragma: no cover — a settings read never blocks a send
        return _DEFAULT_FOLDER


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.export.compile import compile_pdf, have_latexmk
    from precis.export.latex import export_draft
    from precis.export.remarkable import remarkable_configured, send_pdf

    params = (ctx.meta or {}).get("params") or {}
    slug = str(params.get("draft") or "").strip()
    if not slug:
        ctx.record_failure("remarkable_send: params.draft is required")
        return
    ref = ctx.store.get_ref(kind="draft", id=slug)
    if ref is None:
        ctx.record_failure(f"remarkable_send: no draft {slug!r}")
        return
    if not remarkable_configured(ctx.store):
        ctx.record_failure(
            "remarkable_send: no reMarkable credential configured — set "
            "REMARKABLE_RMAPI_CONFIG (or REMARKABLE_TOKEN) in the vault (/secrets)."
        )
        return

    # Figure clearance gate (ADR 0034 §4) — same as draft_export: an
    # uncleared figure must not ship.
    from precis.utils.figure_clearance import draft_figure_clearance

    clearance = draft_figure_clearance(ctx.store, ref.id)
    if clearance.uncleared:
        lines = "; ".join(f"{f.dc} ({f.reason})" for f in clearance.uncleared)
        ctx.record_failure(
            f"remarkable_send: {len(clearance.uncleared)} of {clearance.total} "
            f"figure(s) not cleared to ship — {lines}."
        )
        return

    if not have_latexmk():
        ctx.record_failure(
            "remarkable_send: latexmk not installed on this worker — cannot "
            "compile the PDF to send (install mactex / texlive)."
        )
        return

    folder = _target_folder(ctx, params)
    title = (ref.title or ref.slug or slug).split("\n", 1)[0]

    # The reMarkable PDF is transient (uploaded, then discardable) — render
    # into a temp dir, not the project workspace (don't clobber the normal
    # project PDF the task page shows).
    with tempfile.TemporaryDirectory(prefix="rm-send-") as td:
        out_dir = Path(td)
        ctx.append_chunk(
            "job_event", f"exporting {slug!r} in reMarkable mode → {folder}"
        )
        try:
            result = export_draft(ctx.store, ref, target_dir=out_dir, remarkable=True)
        except Exception as exc:
            log.warning("remarkable_send: render failed for %s", slug, exc_info=True)
            ctx.record_failure(f"remarkable_send: LaTeX render failed: {exc}")
            return
        for w in result.warnings:
            ctx.append_chunk("job_event", f"warn: {w}")
        ctx.append_chunk(
            "job_event", f"{len(result.cited_slugs)} citation(s) → footnotes; compiling"
        )
        cres = compile_pdf(out_dir)
        ctx.append_chunk("job_event", f"latexmk rc={cres.returncode}")
        if not (cres.ok and cres.pdf is not None):
            ctx.append_chunk("job_event", cres.log_tail)
            ctx.record_failure(
                f"remarkable_send: latexmk failed (rc={cres.returncode}); see the log"
            )
            return
        ctx.append_chunk("job_event", f"uploading {cres.pdf.name!r} to reMarkable")
        sres = send_pdf(cres.pdf, folder=folder, display_name=title, store=ctx.store)

    if sres.ok:
        ctx.append_chunk(
            "job_summary", f"Sent {sres.name!r} to reMarkable folder {sres.folder}."
        )
        ctx.set_meta(remarkable_folder=sres.folder, remarkable_name=sres.name)
    else:
        if sres.output:
            ctx.append_chunk("job_event", sres.output)
        ctx.record_failure(f"remarkable_send: {sres.error}")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("remarkable_send runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="remarkable_send",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Export a draft in reMarkable mode and upload the PDF to the tablet.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "TARGET_FOLDER_KEY", "load"]
