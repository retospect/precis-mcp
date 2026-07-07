"""Resolve a draft's cited sources to their on-disk PDFs, and bundle them.

Two draft-export affordances share one primitive here — "given a draft, which
source PDFs (papers / patents / datasheets) does it cite, and where do they
live on *this* host":

* the LaTeX/PDF exporter appends each cited source as a ``pdfpages`` appendix
  (``latex.export_draft(include_sources=True)``), and
* the reader / CLI zip up the cited PDFs + a manifest
  (:func:`build_sources_zip`).

The cited-slug set is the exact bibliography set the exporters already compute
(``render_body().cited_slugs``); we resolve each slug to a ref and its PDF with
the *same* order the corpus-presence pass uses — authoritative
``pdfs.storage_path`` first, then the cite_key convention across every
configured root (``precis.corpus_layout``), rebasing a foreign-mount path onto
this node (ADR 0029). No web-package import; pure ``precis``.

**Per-host caveat.** The corpus is a per-host mount, so the web/CLI process may
not physically hold every cited PDF. We bundle what resolves locally and
surface the rest (``SourceBundle.missing`` + a ``manifest.txt`` section),
noting when ``Store.pdf_held_anywhere`` says the cluster has it — a bundle can
be legitimately incomplete rather than an error.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.corpus_layout import (
    corpus_pdf_dest,
    corpus_roots_from_env,
    rebase_onto_local,
)

# Kinds a cited handle may resolve to that carry an ingested PDF worth
# bundling. Datasheets are citable (``corpus_role='evidence'``) so they can
# appear in the cited set alongside papers / patents.
_SOURCE_KINDS = ("paper", "patent", "datasheet")


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One cited source and where (if anywhere local) its PDF was found."""

    slug: str
    kind: str  # paper | patent | datasheet
    title: str
    authors: str  # formatted "A and B and …" (may be "")
    year: int | None
    pdf_sha256: str | None
    local_path: Path | None
    #: Why the PDF isn't bundled — "" when present. One of:
    #: ``unresolved`` (no such ref), ``no-pdf`` (ref has no pdf_sha256),
    #: ``not-on-host`` (has a PDF but not on this mount; may be held
    #: elsewhere), ``not-on-host-held-elsewhere`` (ditto, cluster has it).
    reason: str = ""


@dataclass
class SourceBundle:
    """The resolved cited sources for a draft, split present / missing."""

    entries: list[SourceEntry] = field(default_factory=list)

    @property
    def present(self) -> list[SourceEntry]:
        return [e for e in self.entries if e.local_path is not None]

    @property
    def missing(self) -> list[SourceEntry]:
        return [e for e in self.entries if e.local_path is None]


def _bibtex_authors(authors: list[dict[str, Any]] | None) -> str:
    """``A and B and …`` from a ref's authors list; "" when unknown.

    Mirrors ``latex._bibtex_authors`` (kept local to avoid importing the
    LaTeX module just for a string join)."""
    if not authors:
        return ""
    names: list[str] = []
    for a in authors:
        name = a.get("name") or " ".join(
            x for x in (a.get("given"), a.get("family")) if x
        )
        if name:
            names.append(name)
    return " and ".join(names)


def _resolve_pdf_for_ref(
    store: Any, corpus_dirs: tuple[Path, ...], ref: Any
) -> Path | None:
    """This host's copy of ``ref``'s PDF, or ``None`` if absent locally.

    Single-ref twin of ``corpus_reconcile._resolve_local``: authoritative
    ``storage_path`` (as-is, then rebased onto the local mount), then the
    cite_key convention across every root.
    """
    sha = getattr(ref, "pdf_sha256", None)
    if not sha:
        return None
    stored = store.pdf_storage_path(sha)
    if stored:
        p = Path(stored)
        if p.is_file():
            return p
        rebased = rebase_onto_local(stored, corpus_dirs)
        if rebased is not None:
            return rebased
    cite_key = getattr(ref, "slug", None)
    if cite_key:
        for root in corpus_dirs:
            cand = corpus_pdf_dest(cite_key, root)
            if cand.is_file():
                return cand
    return None


def _resolve_source_ref(store: Any, slug: str) -> Any | None:
    """The paper / patent / datasheet ref a cited slug names, or ``None``."""
    for kind in _SOURCE_KINDS:
        ref = store.get_ref(kind=kind, id=slug)
        if ref is not None:
            return ref
    return None


def collect_cited_sources(
    store: Any,
    ref: Any,
    *,
    cited_slugs: list[str] | None = None,
    corpus_dirs: tuple[Path, ...] | None = None,
) -> SourceBundle:
    """Resolve a draft's cited sources to their local PDFs.

    ``cited_slugs`` is reused from an already-run export when the caller has
    it (``ExportResult.cited_slugs``); otherwise we render the draft body once
    to compute it. ``corpus_dirs`` defaults to :func:`corpus_roots_from_env`.
    """
    if cited_slugs is None:
        # Local import: latex imports us for the appendix, so import lazily to
        # avoid a cycle at module load.
        from precis.export.latex import render_body

        cited_slugs = render_body(store, ref).cited_slugs
    if corpus_dirs is None:
        corpus_dirs = corpus_roots_from_env()

    entries: list[SourceEntry] = []
    seen: set[str] = set()
    for slug in cited_slugs:
        if slug in seen:
            continue
        seen.add(slug)
        sref = _resolve_source_ref(store, slug)
        if sref is None:
            entries.append(
                SourceEntry(slug, "", slug, "", None, None, None, "unresolved")
            )
            continue
        sha = getattr(sref, "pdf_sha256", None)
        path = _resolve_pdf_for_ref(store, corpus_dirs, sref)
        reason = ""
        if path is None:
            if not sha:
                reason = "no-pdf"
            elif store.pdf_held_anywhere(sha):
                reason = "not-on-host-held-elsewhere"
            else:
                reason = "not-on-host"
        entries.append(
            SourceEntry(
                slug=slug,
                kind=str(getattr(sref, "kind", "") or ""),
                title=str(getattr(sref, "title", None) or slug),
                authors=_bibtex_authors(getattr(sref, "authors", None)),
                year=getattr(sref, "year", None),
                pdf_sha256=sha,
                local_path=path,
                reason=reason,
            )
        )
    return SourceBundle(entries=entries)


_REASON_LABEL = {
    "unresolved": "no matching paper/datasheet in the corpus",
    "no-pdf": "record has no ingested PDF",
    "not-on-host": "PDF not present on this host and not held elsewhere",
    "not-on-host-held-elsewhere": (
        "PDF not on this host (held on another cluster node)"
    ),
}


def write_manifest(ref: Any, bundle: SourceBundle) -> str:
    """A plaintext ``manifest.txt`` — a numbered bibliography of the bundled
    sources plus a "not bundled" section explaining every gap."""
    title = str(getattr(ref, "title", None) or getattr(ref, "slug", None) or "draft")
    lines: list[str] = [
        f"Referenced sources for: {title}",
        "",
        f"Bundled: {len(bundle.present)} of {len(bundle.entries)} cited source(s).",
        "",
    ]
    if bundle.present:
        lines.append("== Included ==")
        for i, e in enumerate(bundle.present, 1):
            head = f"[{i}] {e.title}"
            if e.year:
                head += f" ({e.year})"
            lines.append(head)
            if e.authors:
                lines.append(f"    {e.authors}")
            lines.append(f"    {e.kind}:{e.slug}  ->  {e.slug}.pdf")
        lines.append("")
    if bundle.missing:
        lines.append("== Not bundled ==")
        for e in bundle.missing:
            why = _REASON_LABEL.get(e.reason, e.reason or "unknown")
            lines.append(f"- {e.kind or 'source'}:{e.slug} — {e.title}")
            lines.append(f"    {why}")
        lines.append("")
    return "\n".join(lines)


@dataclass
class ZipResult:
    """Outcome of :func:`build_sources_zip`."""

    path: Path
    bundle: SourceBundle


def build_sources_zip(
    store: Any,
    ref: Any,
    out_path: Path,
    *,
    cited_slugs: list[str] | None = None,
    report_path: Path | None = None,
    corpus_dirs: tuple[Path, ...] | None = None,
) -> ZipResult:
    """Write ``out_path`` — a zip of the draft's cited source PDFs (named
    ``<cite_key>.pdf``, deduped by sha) plus a ``manifest.txt``.

    When ``report_path`` is given (a compiled ``.pdf`` / ``.docx``), the
    report is added at the zip root and the sources go under ``sources/`` —
    a self-contained "report + its sources" bundle.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = collect_cited_sources(
        store, ref, cited_slugs=cited_slugs, corpus_dirs=corpus_dirs
    )
    src_prefix = "sources/" if report_path is not None else ""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if report_path is not None:
            rp = Path(report_path)
            zf.write(rp, arcname=rp.name)
        zf.writestr(f"{src_prefix}manifest.txt", write_manifest(ref, bundle))
        written: set[str] = set()
        for e in bundle.present:
            assert e.local_path is not None
            arcname = f"{src_prefix}{e.slug}.pdf"
            if arcname in written:
                continue
            written.add(arcname)
            zf.write(e.local_path, arcname=arcname)
    return ZipResult(path=out_path, bundle=bundle)


__all__ = [
    "SourceBundle",
    "SourceEntry",
    "ZipResult",
    "build_sources_zip",
    "collect_cited_sources",
    "write_manifest",
]
