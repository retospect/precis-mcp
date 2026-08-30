"""``remarkable_papers_send`` job_type — the "papers → reMarkable" button's
job: send every cited source PDF (papers / patents / datasheets) of a draft
to the reMarkable tablet.

Deterministic, in-process work (no claude), like ``remarkable_send`` — but no
LaTeX render/compile is involved here: the cited sources are already-ingested
PDFs, resolved via ``precis.export.sources.collect_cited_sources`` (the same
primitive the LaTeX appendix and ``papers.zip`` use) and pushed one at a time
via ``precis.export.remarkable.send_pdf`` (the ``rmapi`` CLI). Each step
streams as a ``job_event`` so the run is followable on the todo page.

The destination is a per-draft subfolder under the ``remarkable.target_folder``
app_setting (default ``/Precis``) — e.g. ``/Precis/173020`` — so a tablet
holding sources for several drafts doesn't jumble them into one folder; an
explicit ``params.folder`` override wins verbatim, as-is. Per-user credential
resolution is identical to ``remarkable_send`` — ``params.user`` (the
signed-in login the web route threads through) resolves a paired device
first, falling back to the deployment-wide credential.

**Per-host caveat, not a hard failure.** The corpus is a per-host mount, so
this worker may not physically hold every cited PDF — a bundle can be
legitimately partial. Missing entries are reported as ``job_event``s and
skipped; the job only fails outright when *none* of the cited sources are on
this host, or when an upload actually fails.

Started from the ``/drafts`` "papers → reMarkable" button (shown only when a
device credential is configured), or by an agent::

    put(kind='job', job_type='remarkable_papers_send', parent_id=<project todo id>,
        params={'draft': '<slug>'})
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.remarkable_send import TARGET_FOLDER_KEY

log = logging.getLogger(__name__)

_DEFAULT_FOLDER = "/Precis"

#: Characters kept verbatim in a per-draft subfolder segment; everything else
#: collapses to a single space (mirrors the reMarkable folder charset, which
#: additionally allows "/" — not needed here since this is one segment).
_SEGMENT_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 _-]+")

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        # Override the computed per-draft destination folder for this send.
        "folder": {"type": "string"},
        # The signed-in login to resolve a per-user paired device for —
        # threaded by the web route; absent for agent-started sends.
        "user": {"type": "string"},
    },
    "required": ["draft"],
    "additionalProperties": False,
}


def _draft_segment(slug: str) -> str:
    """A safe per-draft subfolder segment derived from ``slug``."""
    cleaned = _SEGMENT_UNSAFE_RE.sub(" ", slug).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "sources"


def _target_folder(ctx: Any, params: dict[str, Any], slug: str) -> str:
    """The destination folder: an explicit ``params.folder`` wins verbatim,
    else a per-draft subfolder under the ``remarkable.target_folder``
    app_setting (default ``/Precis``)."""
    override = str(params.get("folder") or "").strip()
    if override:
        return override
    try:
        from precis.budget.settings import get_setting

        base = (get_setting(ctx.store, TARGET_FOLDER_KEY) or _DEFAULT_FOLDER).strip()
    except Exception:  # pragma: no cover — a settings read never blocks a send
        base = _DEFAULT_FOLDER
    return f"{base.rstrip('/') or ''}/{_draft_segment(slug)}"


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.export.remarkable import remarkable_configured, send_pdf
    from precis.export.sources import collect_cited_sources

    params = (ctx.meta or {}).get("params") or {}
    slug = str(params.get("draft") or "").strip()
    login = str(params.get("user") or "").strip() or None
    if not slug:
        ctx.record_failure("remarkable_papers_send: params.draft is required")
        return
    ref = ctx.store.get_ref(kind="draft", id=slug)
    if ref is None:
        ctx.record_failure(f"remarkable_papers_send: no draft {slug!r}")
        return
    if not remarkable_configured(ctx.store, login=login):
        ctx.record_failure(
            "remarkable_papers_send: no reMarkable credential configured — "
            "pair your tablet at /account, or set REMARKABLE_RMAPI_CONFIG "
            "(or REMARKABLE_TOKEN) in the vault (/secrets) for a "
            "deployment-wide device."
        )
        return

    # Same LaTeX-assembly step remarkable_send wraps — a draft with
    # malformed chunk data must fail with an actionable message, not an
    # uncaught-exception one.
    try:
        bundle = collect_cited_sources(ctx.store, ref)
    except Exception as exc:
        log.warning(
            "remarkable_papers_send: source resolution failed for %s",
            slug,
            exc_info=True,
        )
        ctx.record_failure(f"remarkable_papers_send: source resolution failed: {exc}")
        return
    if not bundle.present and not bundle.missing:
        ctx.record_failure(
            f"remarkable_papers_send: draft {slug!r} cites no paper/patent/"
            "datasheet sources"
        )
        return

    for e in bundle.missing:
        ctx.append_chunk("job_event", f"not on this host: {e.slug} ({e.reason})")

    present = bundle.present
    if not present:
        ctx.record_failure(
            f"remarkable_papers_send: none of the {len(bundle.entries)} cited "
            "source PDF(s) are on this worker host"
        )
        return

    folder = _target_folder(ctx, params, slug)
    total = len(present)
    succeeded: list[str] = []
    failed: list[str] = []
    for i, e in enumerate(present, 1):
        assert e.local_path is not None  # present ⇒ resolved to a path
        name = e.title or e.slug
        ctx.append_chunk("job_event", f"uploading {i}/{total}: {name}")
        sres = send_pdf(
            e.local_path, folder=folder, display_name=name, store=ctx.store, login=login
        )
        if sres.skipped:
            ctx.record_failure(f"remarkable_papers_send: {sres.error}")
            return
        if sres.ok:
            succeeded.append(e.slug)
        else:
            failed.append(e.slug)
            if sres.output:
                ctx.append_chunk("job_event", sres.output)

    if failed:
        ctx.append_chunk("job_event", f"{len(succeeded)} of {total} upload(s) sent")
        ctx.record_failure(
            f"remarkable_papers_send: {len(failed)} of {total} upload(s) "
            f"failed: {', '.join(failed)}"
        )
        return

    ctx.append_chunk(
        "job_summary", f"Sent {total} source PDF(s) to reMarkable folder {folder}."
    )
    ctx.set_meta(remarkable_folder=folder, remarkable_count=total)


SPEC = JobTypeSpec(
    name="remarkable_papers_send",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description=(
        "Send every cited source PDF (papers/patents/datasheets) of a draft "
        "to the reMarkable tablet."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
