"""``draft_export`` job_type — render a draft to LaTeX and compile a PDF.

Deterministic, in-process work (no claude). Registered with a plugin
``dispatch`` so ``claude_inproc`` or ``job_inproc`` runs it directly (the
executor calls ``spec.dispatch(ctx, spec)`` and skips the claude
subprocess entirely): ``export_draft`` → ``compile_pdf``, streaming each
step as ``job_event`` chunks so the run is followable on the todo page,
and landing the PDF path in ``refs.meta`` + the ``job_summary``.

``default_executor='job_inproc'`` (gr592838): a PDF export needs no LLM,
so ordinary puts — and the ``/drafts`` "export PDF" button, which omits
``executor=`` — route off the agent lane; ``claude_inproc`` stays an
explicit opt-in. Claims are held to hosts advertising ``latexmk`` (the
``ServiceSpec.placement`` row in ``workers/registry.py``, check-only on
every claim path): a TeX-less host leaves the row queued for a capable
one, and when *no* live host advertises the toolchain the sweeper's
unschedulable alert pages. As a second line, a claim that still finds
``latexmk`` absent fails ``failure_class='infra'`` with the
``infra:no-latexmk`` tag (bounded retry) instead of quietly exporting a
PDF-less tree.

The file-writing half runs under a per-draft advisory lock
(:func:`_draft_lock`, gr592838): two ``draft_export`` jobs for one slug
claimed in the same agentlane sweep ran ``latexmk`` in the same export
dir and clobbered each other's aux/log files — the loser now queues
behind the winner; a wait past ``_LOCK_WAIT_S`` fails infra with the
``infra:lock-timeout`` open tag (the same bounded-retry classification
as ``infra:no-latexmk`` — contention is an infra condition, not a
content error, so the parent's ``child-failed:`` latch is not spent).

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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        "format": {"type": "string"},  # reserved; only 'pdf' today
        "audience": {
            "type": "string",
            "enum": ["dev", "external"],
            "pattern": r"\A(?:dev|external)\Z",
        },
        # Bundle cited paper/datasheet PDFs into a pdfpages appendix so the
        # compiled PDF is self-contained (report + its sources).
        "include_sources": {"type": "boolean"},
        # Set by the export route when the retraction gate was bypassed via
        # ?ignore_retractions=1 — the sources appendix records which cited
        # papers were overridden (see precis.export.retraction).
        "ignore_retractions": {"type": "boolean"},
        # Opt-in: export even when figures are uncleared — image-less ones
        # ship as the exporter's visible placeholders, uncleared real images
        # are withheld (framed box, never embedded). Same waiver
        # remarkable_send offers.
        "placeholder_figures": {"type": "boolean"},
        # Append the doi hyperlink run after each in-text \cite{...} mark
        # (precis.export.latex.render_body's doi_links). Default on.
        # Independent of library_links.
        "doi_links": {"type": "boolean"},
        # Append the library-discovery-search hyperlink run after each
        # in-text \cite{...} mark (render_body's library_links). Default
        # on. Independent of doi_links.
        "library_links": {"type": "boolean"},
        # Closing "Published claim artifacts" appendix (export_draft's
        # claim_appendix). Default on; false omits it.
        "claim_appendix": {"type": "boolean"},
        # biblatex bibliography style (precis.export.latex._BIB_STYLES:
        # numeric-comp | chem-rsc/rsc | chem-acs/acs | nature). Absent →
        # the draft's meta.workspace.style, else numeric-comp.
        "bib_style": {"type": "string"},
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
    so the compiled ``main.pdf`` shows on the project's todo page for free
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
            ws = (
                Workspace.from_meta(getattr(project, "meta", None)) if project else None
            )
            if ws is not None:
                return ws.absolute_root(Path(precis_root)), True
        except Exception:  # pragma: no cover — fall back to temp
            log.warning(
                "draft_export: workspace resolve failed; using temp dir",
                exc_info=True,
            )
    # Containment: the draft slug is agent-controlled and unvalidated at the
    # DB layer (`slug` is just `text NOT NULL` — no format CHECK), so a slug
    # like `../../etc/cron.d/x` would let this bare join escape the export
    # root and write main.tex/refs.bib/sources (then run latexmk) at an
    # arbitrary location. Reject any slug whose resolved dir isn't under the
    # export root — mirrors the `.resolve()` + `relative_to()` guard every
    # file-kind handler applies.
    root = _export_root().resolve()
    out = (root / slug).resolve()
    if out != root and root not in out.parents:
        raise ValueError(f"draft slug {slug!r} escapes the export root {root}")
    return out, False


def _resolve_doc_type(ctx: Any) -> str:
    """The project workspace's ``doc_type`` (e.g. ``patent``) for this export
    job, via the job → parent-project walk (same as ``_resolve_out_dir``).
    ``""`` when there is no workspace — the neutral export genre."""
    from precis.utils.workspace import Workspace

    try:
        job = ctx.store.get_ref(kind="job", id=ctx.ref_id)
        parent_id = getattr(job, "parent_id", None)
        project = ctx.store.get_ref(kind="todo", id=parent_id) if parent_id else None
        ws = Workspace.from_meta(getattr(project, "meta", None)) if project else None
        return ws.doc_type if ws else ""
    except Exception:  # pragma: no cover — genre is a nicety, never fatal
        return ""


def _resolve_audience_out_dir(ctx: Any, slug: str, audience: str) -> tuple[Path, bool]:
    """External builds cannot overwrite or compile a collaborator build's files."""
    out, in_workspace = _resolve_out_dir(ctx, slug)
    if audience == "external":
        root = out.resolve()
        out = (root / "external").resolve()
        if root not in out.parents:
            raise ValueError("external export directory escapes the export root")
    return out, in_workspace


#: Per-draft export serialization budget (gr592838): a second export of
#: the same slug waits on the lock up to this long before failing infra.
_LOCK_WAIT_S = 600.0
#: Poll interval while waiting on the per-draft lock.
_LOCK_POLL_S = 2.0


@contextmanager
def _draft_lock(store: Any, slug: str) -> Iterator[bool]:
    """Serialize concurrent exports of one draft; yield ``True`` once held.

    ``pg_try_advisory_xact_lock(hashtext('draft_export:<slug>'))`` on a
    dedicated (non-pool) connection — the same pgbouncer-safe shape
    :func:`precis.store.advisory.try_xact_advisory_lock` enforces: an open
    transaction pins one server backend for the lock's whole life, the
    lock ends with the transaction, and a dead holder releases with its
    connection. Reimplemented rather than reused because this caller
    *waits*: poll every ``_LOCK_POLL_S`` until ``_LOCK_WAIT_S`` elapses,
    then yield ``False`` (nothing held) instead of blocking forever on a
    wedged sibling.

    A store without a ``dsn`` (fakes in tests) yields ``True`` unguarded.
    """
    dsn = getattr(store, "dsn", None)
    if not dsn:
        yield True
        return
    conn = psycopg.connect(dsn)
    try:
        # The holder tx sits idle while the export runs on pool
        # connections — lift prod's idle-in-transaction kill for THIS
        # transaction only (SET LOCAL), same as advisory.py.
        conn.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
        conn.execute("SET LOCAL application_name = 'precis-draft-export-lock'")
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                (f"draft_export:{slug}",),
            ).fetchone()
            if row and row[0]:
                break
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(_LOCK_POLL_S)
        yield True
    finally:
        try:
            conn.rollback()
        except (psycopg.OperationalError, psycopg.InterfaceError):
            # The server dropped the lock connection: the transaction
            # ended with it, so the lock is already released.
            log.warning("draft_export: lock connection lost before release")
        finally:
            conn.close()


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc``/``job_inproc`` for a
    claimed job. ``ctx`` is a
    :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.export.compile import have_latexmk

    params = (ctx.meta or {}).get("params") or {}
    slug = str(params.get("draft") or "").strip()
    if not slug:
        ctx.record_failure("draft_export: params.draft is required")
        return
    ref = ctx.store.get_ref(kind="draft", id=slug)
    if ref is None:
        ctx.record_failure(f"draft_export: no draft {slug!r}")
        return

    # A job_inproc claim exists to compile the PDF (gr592838): on a host
    # without TeX the run could only produce the LaTeX tree, so fail
    # infra instead of reporting a PDF-less "success". The
    # ``infra:no-latexmk`` open tag joins job_bubble.INFRA_FAILURE_TAGS —
    # the parent gets the bounded infra retry (no child-failed latch)
    # and a TeX-carrying host picks the re-mint up. ``claude_inproc``
    # keeps the historical export-and-skip path below.
    if str((ctx.meta or {}).get("executor") or "") == "job_inproc" and (
        not have_latexmk()
    ):
        ctx.record_failure(
            "draft_export: claimed on job_inproc but latexmk is not "
            "installed on this host — PDF cannot compile here",
            failure_class="infra",
            open_tag="infra:no-latexmk",
        )
        return

    # Full link sync before the edge-reading gates below. Chunk-scoped
    # writes carry other chunks' auto-mention edges without re-resolving
    # them, so a mention that only now resolves (or one whose target was
    # retired) is stale until an unscoped sync; the cite-drift gate and the
    # unsigned-hub advisory read those edges as truth. Best-effort, like
    # every other caller: a resolution failure never fails the export.
    from precis.dispatch import Hub
    from precis.handlers.draft import DraftHandler

    DraftHandler(hub=Hub(store=ctx.store)).sync_draft_links(ref.id)

    # Figure clearance gate: an uncleared figure must not
    # ship, so it fails the export — the way a bare ``\cite`` fails review.
    # ``placeholder_figures`` waives every uncleared figure: image-less ones
    # export as visible placeholders, uncleared real images are WITHHELD (the
    # exporter prints a framed box naming the publisher and source, never the
    # image), so nothing uncleared actually ships. Same waiver remarkable_send
    # offers.
    from precis.utils.figure_clearance import (
        draft_figure_clearance,
        partition_uncleared,
        withheld_handles,
    )

    clearance = draft_figure_clearance(ctx.store, ref.id)
    uncleared, waived = partition_uncleared(
        clearance.uncleared,
        placeholder_figures=bool(params.get("placeholder_figures")),
    )
    withheld = withheld_handles(waived)
    pending = [f for f in waived if f.dc not in withheld]
    if pending:
        ctx.append_chunk(
            "job_event",
            f"warn: {len(pending)} image-less figure(s) ship as visible "
            f"placeholders — {'; '.join(f.dc for f in pending)}",
        )
    if withheld:
        ctx.append_chunk(
            "job_event",
            f"warn: {len(withheld)} third-party figure(s) withheld pending "
            f"permission — {'; '.join(sorted(withheld))}",
        )
    if uncleared:
        lines = "; ".join(f"{f.dc} ({f.reason})" for f in uncleared)
        ctx.record_failure(
            f"draft_export: {len(uncleared)} of {clearance.total} "
            f"figure(s) not cleared to ship — {lines}. Clear each (grant/renew "
            "the permission, or fix the origin) and re-export."
        )
        return

    # Cite-drift gate (docs/backlog/cite-pins-hub-version.md): a citing
    # passage's pinned hub version no longer matches the hub's live
    # version — the paraphrase may no longer say what the hub says today.
    # Blocking, same precedent as the figure clearance gate above.
    # Unstamped (pre-pin) cites are unknown, not drift, and never block —
    # ``find_drifted_cites`` already excludes them from its ``drifted``
    # list.
    from precis.draft.lint import find_drifted_cites

    drifted, _unstamped = find_drifted_cites(ctx.store, ref.id)
    if drifted:
        parts = []
        for d in drifted:
            old = (
                f'"{d.stamped_title}"'
                if d.stamped_title is not None
                else "old statement not recorded"
            )
            parts.append(
                f'{d.dc} (fi{d.hub_ref_id}: was {old}, now "{d.current_title}")'
            )
        ctx.record_failure(
            f"draft_export: {len(drifted)} cite(s) drifted from the hub they "
            f"cite — {'; '.join(parts)}. Re-check the passage against the "
            "hub's current statement and rewrite it (which re-stamps the "
            "pin), then re-export."
        )
        return

    # Unsigned-hub advisory (same backlog item): counted only, never
    # blocking — most cited hubs are ``candidate``, so a hard fail here
    # would block nearly every export in the corpus. "Signed" is the same
    # publish-state ladder ``trust='signed'`` already tests
    # (``handlers/finding.py::_SIGNED_STATES`` — signed/anchored/
    # published), read off the same batched ``hub_rows`` the hygiene view
    # uses for posture.
    from precis.nanopub.overview import draft_cited_hub_ids, hub_rows
    from precis.store.types import SIGNED_FINDING_STATES as _SIGNED_STATES

    cited_hub_ids = draft_cited_hub_ids(ctx.store, ref.id)
    if cited_hub_ids:
        unsigned = sum(
            1
            for row in hub_rows(ctx.store, ref_ids=sorted(cited_hub_ids))
            if row.state not in _SIGNED_STATES
        )
        if unsigned:
            ctx.append_chunk(
                "job_event",
                f"advisory: {unsigned} cited claim hub(s) rest on an "
                "unsigned publish state (not yet signed/anchored/"
                "published) — see get(kind='draft', id=<slug>, "
                "view='hygiene').",
            )

    # Serialize the file-writing half per draft (gr592838): two exports
    # of one slug claimed in the same sweep ran latexmk in the same
    # export dir and clobbered each other's aux/log files. The lock
    # loser queues behind the winner; past ``_LOCK_WAIT_S`` it fails
    # infra rather than colliding.
    with _draft_lock(ctx.store, slug) as held:
        if not held:
            ctx.record_failure(
                f"draft_export: waited {_LOCK_WAIT_S:g}s for the "
                f"per-draft export lock on {slug!r} — a sibling export "
                "is still running or died holding it",
                failure_class="infra",
                # job_bubble classifies infra by open tag, not by
                # failure_class — without this member of
                # INFRA_FAILURE_TAGS the very first contention latches
                # child-failed on the parent (review td677026).
                open_tag="infra:lock-timeout",
            )
            return
        _export_and_compile(ctx, ref, params, slug, withheld)


def _export_and_compile(
    ctx: Any,
    ref: Any,
    params: dict[str, Any],
    slug: str,
    withheld: frozenset[str],
) -> None:
    """Render + compile under the per-draft lock — the file-writing half
    of ``_dispatch`` (everything after the read-only gates)."""
    from precis.export.compile import compile_pdf, have_latexmk
    from precis.export.latex import export_draft

    include_sources = bool(params.get("include_sources"))
    doc_type = _resolve_doc_type(ctx)
    if doc_type == "poster" and include_sources:
        # The poster template doesn't load pdfpages — a sources appendix
        # is a paper-genre feature; don't let it break a poster compile.
        ctx.append_chunk("job_event", "warn: include_sources ignored for poster export")
        include_sources = False
    # The route already decided (and blocked, if not overridden) — recompute
    # the same no-network read here so the appendix records exactly which
    # cites were retracted, without threading paper details through the job
    # params. Only meaningful alongside a sources appendix to record it in.
    retraction_override: list[Any] = []
    if include_sources and params.get("ignore_retractions"):
        from precis.export.retraction import draft_retraction_report

        retraction_override = draft_retraction_report(ctx.store, ref).retracted
    audience_name = str(params.get("audience") or "dev")
    try:
        out_dir, in_workspace = _resolve_audience_out_dir(ctx, slug, audience_name)
    except ValueError as exc:
        ctx.record_failure(f"draft_export: {exc}")
        return
    where = (
        "project workspace (shows on the todo page)" if in_workspace else "export dir"
    )
    ctx.append_chunk("job_event", f"exporting draft {slug!r} → {out_dir} [{where}]")
    try:
        result = export_draft(
            ctx.store,
            ref,
            target_dir=out_dir,
            audience=audience_name,
            include_sources=include_sources,
            doc_type=doc_type,
            retraction_override=retraction_override,
            doi_links=bool(params.get("doi_links", True)),
            library_links=bool(params.get("library_links", True)),
            claim_appendix=bool(params.get("claim_appendix", True)),
            bib_style=str(params.get("bib_style") or "").strip() or None,
            withheld_figures=withheld,
            # A queued export job is the explicit write op — it records the
            # override audit row the read paths (web GETs) skip (gr491602).
            record_override=True,
        )
    except Exception as exc:
        log.warning("draft_export: render failed for %s", slug, exc_info=True)
        ctx.record_failure(f"draft_export: LaTeX render failed: {exc}")
        return
    for w in result.warnings:
        ctx.append_chunk("job_event", f"warn: {w}")
    if audience_name == "external":
        supplement = out_dir / "supplement.zip"
        ctx.set_meta(supplement=str(supplement))
        ctx.append_chunk("job_event", f"data supplement: {supplement}")
    if result.poster_profile is not None:
        ctx.append_chunk(
            "job_event", f"poster branding profile → {result.poster_profile.name}"
        )
    ctx.append_chunk(
        "job_event",
        f"wrote {result.main_tex.name} + {result.bib.name}; "
        f"{len(result.cited_slugs)} citation(s)",
    )
    if include_sources and result.source_bundle is not None:
        b = result.source_bundle
        ctx.append_chunk(
            "job_event",
            f"bundled {len(b.present)}/{len(b.entries)} cited source PDF(s) "
            "as a pdfpages appendix",
        )
    if retraction_override:
        ctx.append_chunk(
            "job_event",
            f"retraction override recorded for {len(retraction_override)} "
            "cite(s) in the sources appendix",
        )

    # The collaborator link origin, reported on the job summary (gr592892):
    # one operator-set PRECIS_WEB_BASE_URL per installation — never a
    # per-export param — so the summary says what the links used.
    origin_line: str | None = None
    if audience_name == "dev":
        from precis.export.audience import SET_ORIGIN_COMMAND, web_base_url

        origin = web_base_url(ctx.store)
        origin_line = (
            f"record-handle links → {origin}"
            if origin
            else "record-handle links are path-only — PRECIS_WEB_BASE_URL "
            f"unset; set it once per installation: {SET_ORIGIN_COMMAND}"
        )

    if not have_latexmk():
        ctx.append_chunk(
            "job_summary",
            f"LaTeX project exported to {out_dir} — latexmk not installed, "
            "PDF compile skipped (install mactex / texlive on the worker).",
        )
        if origin_line is not None:
            ctx.append_chunk("job_summary", origin_line)
        ctx.set_meta(export_dir=str(out_dir), pdf=None)
        return

    cres = compile_pdf(out_dir)
    ctx.append_chunk("job_event", f"latexmk rc={cres.returncode}\n{cres.log_tail}")
    if cres.ok and cres.pdf is not None:
        ctx.append_chunk("job_summary", f"PDF compiled: {cres.pdf}")
        if origin_line is not None:
            ctx.append_chunk("job_summary", origin_line)
        ctx.set_meta(export_dir=str(out_dir), pdf=str(cres.pdf))
    else:
        ctx.record_failure(
            f"draft_export: latexmk failed (rc={cres.returncode}); see the log event"
        )


SPEC = JobTypeSpec(
    name="draft_export",
    params_schema=_PARAMS_SCHEMA,
    # job_inproc too (gr592838): a PDF export needs no LLM — only a TeX
    # toolchain; a TeX-less job_inproc claim fails infra (no-latexmk).
    compatible_executors=frozenset({"claude_inproc", "job_inproc"}),
    # …and it is the DEFAULT (gr592838 want (a)): puts omitting
    # ``executor=`` — including the web export button — leave the agent
    # lane alone. The ServiceSpec.placement row (``latexmk``) holds the
    # claim on TeX-less hosts.
    default_executor="job_inproc",
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Render a draft to LaTeX and compile a PDF (deterministic).",
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
