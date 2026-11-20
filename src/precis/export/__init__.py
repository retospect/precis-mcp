"""Document export engines (LaTeX → Tier-B).

The packaged preamble loads ``fontspec``, so PDF compilation and the CLI's
manual compile hint use LuaLaTeX (``latexmk -lualatex``).
Adsorbate stars share the literal-star decision with Word and house-style
lint. LaTeX emits remaining prose stars with an explicit centered-star
command so automatic notation and author-escaped stars use the same glyph;
protected math and code keep their original star syntax.
PDF exports allow five minutes by default for large drafts and their
bibliography/glossary passes (gr684953); ``PRECIS_LATEXMK_TIMEOUT_S`` or
an explicit compile timeout can override that budget.

PDF jobs optionally refresh persisted retraction checks (30-day TTL) before
reapplying the retracted-citation gate. Word stays read-only; both formats
place shared coverage warnings after Unverified claims so an unchecked
citation cannot silently read as clean.

End-matter sections (each vanishes byte-identically when empty): trust
marks ("Unverified claims", :mod:`._trust_marks`), nanopub "Published
claim artifacts" (:mod:`._nanopub_appendix`), and the "Data package"
appendix (:mod:`._data_package` — 7 pt monospace tables + JSON embedded
in the PDF via ``embedfile`` + a ``data-package.json`` sidecar, generated
from snapshots frozen into figure chunks at mint time so the numbers
always match the plotted pixels).

Inline cite-link group (LaTeX/docx, independent ``doi_links``/
``library_links`` switches, both default on): a ``doi`` hyperlink plus a
per-install library-discovery-search hyperlink appended right after each
citation marker. ``._cite_ids`` holds the shared "DOI, else arXiv id,
else none" lookup both exporters memoize per export; the pure URL
builders live in :mod:`precis.utils.paper_links`.

Abbreviation first-use expansion: :mod:`._abbrev` is the one matcher both
exporters reach through :func:`.walker.term_segments` (which occurrence
defines a short, what is left alone); each exporter only renders the
result (docx text, LaTeX ``\\gls``).

Shared walker (:mod:`.walker`, the export-latex-docx-parity work):
the decisions both document backends must agree on, moved there family by
family; each backend is a ``Renderer`` that only formats. Citations
(numbering, SI resolution, grouping), abbreviation uses, cross-refs (the
section / figure / equation pre-pass) and back matter (which blocks apply,
in ``BACK_MATTER_ORDER``) are there now.
Every move is checked byte-for-byte against
``tests/fixtures/export/golden/``."""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput

#: Ref kinds a document exporter (LaTeX / docx) will render as a
#: deliverable. A ``plan`` is a reasoning outline — the
#: thread's todo-list + notes, ``corpus_role='none'`` — and is deliberately
#: **not** here: it is rendered whole for the model but never exported.
EXPORTABLE_KINDS: frozenset[str] = frozenset({"draft"})

#: The Word export's "citation machinery" choices, value -> label (the
#: export panel's dropdown text). Only the modes in
#: :data:`IMPLEMENTED_CITATION_MODES` can be built; each later mode lands by
#: adding its value there.
CITATION_MODES: dict[str, str] = {
    "endnote": "EndNote fields",
    "word": "Word citations",
    "zotero": "Zotero fields",
    "plain": "plain text",
}
IMPLEMENTED_CITATION_MODES: tuple[str, ...] = (
    "endnote",
    "word",
    "zotero",
    "plain",
)


def guard_citation_mode(mode: str) -> None:
    """Refuse an unknown ``citations`` value, or a known one not built yet."""
    if mode not in CITATION_MODES:
        raise BadInput(
            f"unknown citations mode {mode!r}",
            next="use one of " + ", ".join(IMPLEMENTED_CITATION_MODES),
        )
    if mode not in IMPLEMENTED_CITATION_MODES:
        raise BadInput(
            f"citations={mode!r} ({CITATION_MODES[mode]}) is not available yet",
            next="use one of " + ", ".join(IMPLEMENTED_CITATION_MODES),
        )


def guard_exportable(ref: Any) -> None:
    """Reject an export whose target ref is not an exportable deliverable.

    The single chokepoint every export entry point (CLI ``precis draft
    export``, the web PDF/Word routes, the ``draft_export`` job) funnels
    through via :func:`~precis.export.latex.export_draft` /
    :func:`~precis.export.docx.export_docx`. A ``plan`` must never leave the
    system as a document."""
    kind = getattr(ref, "kind", None)
    if kind is not None and kind not in EXPORTABLE_KINDS:
        raise BadInput(
            f"{kind} is a reasoning outline, not an exportable deliverable — "
            "only a draft exports to LaTeX/PDF/Word",
            next="export the project's draft (kind='draft'), not its plan",
        )


def guard_no_secrets(store: Any, ref: Any) -> None:
    """Refuse an export whose draft text holds a credential.

    An exported PDF/docx is handed to people outside the system and cannot be
    recalled, so a pasted credential is refused before anything is rendered
    or written. The message names the chunk handle (``dc`` handle) and a
    masked excerpt, never the full match. Called next to
    :func:`guard_exportable` by both exporters (LaTeX/PDF and docx)."""
    from precis.errors import BadInput
    from precis.utils.secret_scan import find_secrets

    fields: list[tuple[str, str | None]] = [
        ("draft title", getattr(ref, "title", None))
    ]
    for c in store.drafts.reading_order(ref.id):
        fields.append((f"chunk {c.handle}", c.text))
    hits: list[str] = []
    total = 0
    for label, text in fields:
        for f in find_secrets(text or ""):
            total += 1
            if len(hits) < 3:
                hits.append(f"{label} line {f.line}: {f.kind} ({f.excerpt_masked})")
    if total:
        more = f" (+{total - len(hits)} more)" if total > len(hits) else ""
        raise BadInput(
            "refusing to export: the draft contains what looks like a "
            "credential -- " + "; ".join(hits) + more,
            next="remove it from the draft (reference it by vault name), then re-export",
        )
