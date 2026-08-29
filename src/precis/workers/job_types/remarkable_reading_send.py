"""``remarkable_reading_send`` job_type — the "reading → reMarkable" button's
job: typeset each cited source (papers / patents / datasheets) of a draft as
a tablet-sized "reading edition" — the source's body chunks + a claims
appendix, then the original PDF appended when this host holds a copy — and
send each to the reMarkable tablet.

Deterministic, in-process work (no claude): per source,
``precis.export.reading_edition.export_reading_edition`` writes a LaTeX
project (reusing the draft-export preamble/geometry), ``precis.export.
compile.compile_pdf`` renders it, and ``precis.export.remarkable.send_pdf``
(the ``rmapi`` CLI) uploads the result. Each step streams as a ``job_event``
so the run is followable on the task page.

**Body + claims come from the database, not the filesystem** — the
key difference from ``remarkable_papers_send`` (which sends the raw original
PDFs as-is): a source whose PDF isn't on this host still gets a full reading
edition, just without part 3 appended (a note in the document says so). Only
an ``unresolved`` cited slug (no matching ref at all), or a source with zero
body chunks *and* no local PDF, has nothing worth typesetting and is
skipped.

Destination folder + per-user credential resolution are identical to
``remarkable_papers_send`` — a per-draft subfolder under the
``remarkable.target_folder`` app_setting (default ``/Precis``), an explicit
``params.folder`` override wins verbatim, and ``params.user`` resolves a
paired device first, falling back to the deployment-wide credential.

Started from the ``/drafts`` "reading → reMarkable" button (shown only when
a device credential is configured), or by an agent::

    put(kind='job', job_type='remarkable_reading_send', parent_id=<project todo id>,
        params={'draft': '<slug>'})

``params.source`` restricts the run to one cited source (by slug) — fails
loudly if that slug isn't in the draft's cited set, rather than silently
building nothing.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.remarkable_papers_send import _target_folder

log = logging.getLogger(__name__)

#: Truncated tail of a failed compile's LaTeX log surfaced on the failing
#: source's job_event — long enough to diagnose, short enough not to flood
#: the task page across a multi-source run.
_LOG_TAIL_CHARS = 500

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        # Restrict the run to one cited source (slug); absent = all of them.
        "source": {"type": "string"},
        # Override the computed per-draft destination folder for this send.
        "folder": {"type": "string"},
        # The signed-in login to resolve a per-user paired device for —
        # threaded by the web route; absent for agent-started sends.
        "user": {"type": "string"},
    },
    "required": ["draft"],
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.export.compile import compile_pdf, have_latexmk
    from precis.export.reading_edition import export_reading_edition
    from precis.export.remarkable import remarkable_configured, send_pdf
    from precis.export.sources import collect_cited_sources

    params = (ctx.meta or {}).get("params") or {}
    slug = str(params.get("draft") or "").strip()
    only_source = str(params.get("source") or "").strip() or None
    login = str(params.get("user") or "").strip() or None
    if not slug:
        ctx.record_failure("remarkable_reading_send: params.draft is required")
        return
    ref = ctx.store.get_ref(kind="draft", id=slug)
    if ref is None:
        ctx.record_failure(f"remarkable_reading_send: no draft {slug!r}")
        return
    if not remarkable_configured(ctx.store, login=login):
        ctx.record_failure(
            "remarkable_reading_send: no reMarkable credential configured — "
            "pair your tablet at /account, or set REMARKABLE_RMAPI_CONFIG "
            "(or REMARKABLE_TOKEN) in the vault (/secrets) for a "
            "deployment-wide device."
        )
        return
    if not have_latexmk():
        ctx.record_failure(
            "remarkable_reading_send: latexmk not installed on this worker "
            "— cannot compile reading editions (install mactex / texlive)."
        )
        return

    try:
        bundle = collect_cited_sources(ctx.store, ref)
    except Exception as exc:
        log.warning(
            "remarkable_reading_send: source resolution failed for %s",
            slug,
            exc_info=True,
        )
        ctx.record_failure(f"remarkable_reading_send: source resolution failed: {exc}")
        return
    if not bundle.entries:
        ctx.record_failure(
            f"remarkable_reading_send: draft {slug!r} cites no paper/patent/"
            "datasheet sources"
        )
        return

    entries = bundle.entries
    if only_source is not None:
        entries = [e for e in entries if e.slug == only_source]
        if not entries:
            ctx.record_failure(
                f"remarkable_reading_send: {only_source!r} is not among the "
                f"{len(bundle.entries)} source(s) draft {slug!r} cites"
            )
            return

    folder = _target_folder(ctx, params, slug)
    total = len(entries)
    succeeded: list[str] = []
    failed: list[str] = []
    for i, e in enumerate(entries, 1):
        if e.reason == "unresolved":
            ctx.append_chunk(
                "job_event", f"skipping {e.slug}: no matching ref in corpus"
            )
            continue
        sref = ctx.store.get_ref(kind=e.kind, id=e.slug)
        if sref is None:
            ctx.append_chunk(
                "job_event", f"skipping {e.slug}: no matching ref in corpus"
            )
            continue
        name = e.title or e.slug
        ctx.append_chunk("job_event", f"building {i}/{total}: {name}")
        with tempfile.TemporaryDirectory(prefix="rm-reading-") as td:
            out_dir = Path(td)
            try:
                result = export_reading_edition(
                    ctx.store, sref, out_dir, original_pdf=e.local_path
                )
            except Exception as exc:
                log.warning(
                    "remarkable_reading_send: typeset failed for %s",
                    e.slug,
                    exc_info=True,
                )
                ctx.append_chunk("job_event", f"typeset failed: {e.slug}: {exc}")
                failed.append(e.slug)
                continue
            if result.chunk_count == 0 and not result.has_original:
                ctx.append_chunk("job_event", f"nothing to typeset: {e.slug}")
                continue
            cres = compile_pdf(out_dir)
            if not (cres.ok and cres.pdf is not None):
                tail = cres.log_tail[-_LOG_TAIL_CHARS:]
                ctx.append_chunk("job_event", f"compile failed: {e.slug}: {tail}")
                failed.append(e.slug)
                continue
            sres = send_pdf(
                cres.pdf,
                folder=folder,
                display_name=f"{name} — reading",
                store=ctx.store,
                login=login,
            )
        if sres.skipped:
            ctx.record_failure(f"remarkable_reading_send: {sres.error}")
            return
        if sres.ok:
            succeeded.append(e.slug)
        else:
            failed.append(e.slug)
            if sres.output:
                ctx.append_chunk("job_event", sres.output)

    if not succeeded:
        ctx.record_failure(
            "remarkable_reading_send: nothing was sent"
            + (f" — failed: {', '.join(failed)}" if failed else "")
        )
        return

    if failed:
        ctx.append_chunk(
            "job_event", f"{len(succeeded)} of {len(succeeded) + len(failed)} sent"
        )
        ctx.record_failure(
            f"remarkable_reading_send: {len(failed)} source(s) failed: "
            f"{', '.join(failed)}"
        )
        return

    ctx.append_chunk(
        "job_summary",
        f"Sent {len(succeeded)} reading edition(s) to reMarkable folder {folder}.",
    )
    ctx.set_meta(remarkable_folder=folder, remarkable_count=len(succeeded))


SPEC = JobTypeSpec(
    name="remarkable_reading_send",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description=(
        "Typeset each cited source (body + claims appendix + original when "
        "held) as a reading edition and send it to the reMarkable tablet."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
