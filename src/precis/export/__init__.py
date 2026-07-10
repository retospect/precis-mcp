"""Document export engines (LaTeX → Tier-B). ADR 0033."""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput

#: Ref kinds a document exporter (LaTeX / docx) will render as a
#: deliverable. A ``plan`` (ADR 0051 §2b) is a reasoning outline — the
#: thread's todo-list + notes, ``corpus_role='none'`` — and is deliberately
#: **not** here: it is rendered whole for the model but never exported.
EXPORTABLE_KINDS: frozenset[str] = frozenset({"draft"})


def guard_exportable(ref: Any) -> None:
    """Reject an export whose target ref is not an exportable deliverable.

    The single chokepoint every export entry point (CLI ``precis draft
    export``, the web PDF/Word routes, the ``draft_export`` job) funnels
    through via :func:`~precis.export.latex.export_draft` /
    :func:`~precis.export.docx.export_docx`. A ``plan`` must never leave the
    system as a document (ADR 0051 §2b)."""
    kind = getattr(ref, "kind", None)
    if kind is not None and kind not in EXPORTABLE_KINDS:
        raise BadInput(
            f"{kind} is a reasoning outline, not an exportable deliverable — "
            "only a draft exports to LaTeX/PDF/Word",
            next="export the project's draft (kind='draft'), not its plan",
        )
