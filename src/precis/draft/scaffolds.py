"""Document-class scaffolding — genre briefs, section styles, skeletons.

The heading-styles + numbering lock step 4: picking a document genre (``doc_type``) at draft creation
lays down a styled section skeleton to fill, and folds a standing-guidance
line into the project brief so the planner writes in the right register.
Shared by the web ``/drafts/new`` form (``precis_web.routes.drafts``) and
the MCP ``edit(kind='draft', scaffold=…)`` surface (``handlers/draft.py``,
paper-writing pipeline rung 4, ``docs/backlog/paper-writing-pipeline.md``
§"Document classes") — a genre picked from either surface materialises the
same skeleton, since both read this one table.
"""

from __future__ import annotations

#: Document types offered by the "+ New draft" form. Each maps to a
#: standing guidance line folded into the project brief (so the planner
#: writes in the right register — the brief is injected as the
#: ``## Project context`` block on every tick) and stashed structurally
#: as ``meta.workspace.doc_type`` for the future export documentclass
#: switch. ``brief`` is "" for the neutral default (adds no guidance).
DOC_TYPES: list[dict[str, str]] = [
    {
        "value": "paper",
        "label": "Research paper",
        "brief": "This is a research paper: an abstract, motivated "
        "introduction, methods/results, and a discussion, with rigorous "
        "citations throughout.",
    },
    {
        "value": "patent",
        "label": "Patent application",
        "brief": "This is a patent application: write in patent register — "
        "a technical field and background, a summary, a detailed description "
        "of embodiments, and numbered claims. Be precise and broad in claim "
        "scope; avoid marketing language.",
    },
    {
        "value": "proposal",
        "label": "Proposal (answers a call)",
        "brief": "This is a funding/grant proposal answering a specific "
        "call. There is no fixed template: mirror the linked call's "
        "required sections exactly (one draft section each) and respect "
        "every section's word limit. Lead with the idea and its impact, "
        "show the team's capability to deliver, and tie each claim to the "
        "call's stated evaluation criteria.",
    },
    {
        "value": "report",
        "label": "Technical report",
        "brief": "This is a technical report: an executive summary up front, "
        "clearly sectioned findings, and concrete recommendations.",
    },
    {
        "value": "review",
        "label": "Review / survey",
        "brief": "This is a review/survey article: synthesise and compare the "
        "literature, organise by theme, and map open problems rather than "
        "presenting new results.",
    },
    {
        "value": "manufacturing",
        "label": "System / manufacturing spec",
        "brief": "This is a system-description / manufacturing document: "
        "describe the design and how it is built, and maintain a components "
        "list where each part is a registry entry with a short name, a "
        "description, its manufacturer part number (MPN), and a datasheet / "
        "ordering link. Register a part once, then refer to it by its short "
        "name or number in the prose; the callout numbers are taken in order "
        "and stay stable.",
    },
    {
        "value": "book",
        "label": "Book / monograph",
        "brief": "Multi-chapter monograph.",
    },
    {
        "value": "summary",
        "label": "Summary / brief",
        "brief": "Short digest / brief — the key points, not a comprehensive review.",
    },
    {
        "value": "article",
        "label": "General article",
        "brief": "",
    },
]
DOC_TYPE_BRIEF: dict[str, str] = {d["value"]: d["brief"] for d in DOC_TYPES}

#: Section styles offered in the per-heading "style ▾" dropdown, keyed by
#: ``doc_type``. Each ``(slug, label)`` sets ``meta.style`` on the heading
#: (the slug is a section-style skill served by ``get(kind=
#: 'skill')``). The picker is scoped to the genre so the menu stays short;
#: the scaffold normally sets these, this is the manual override.
_SCI_SECTION = [
    ("sci-abstract", "Abstract"),
    ("sci-introduction", "Introduction"),
    ("sci-related-work", "Related work"),
    ("sci-methods", "Methods"),
    ("sci-results", "Results"),
    ("sci-discussion", "Discussion"),
    ("sci-conclusion", "Conclusion"),
]
SECTION_STYLES: dict[str, list[tuple[str, str]]] = {
    "patent": [
        ("patent-description", "Description"),
        ("patent-claim", "Claim"),
        ("patent-image-part", "Drawings + parts"),
        ("patent-prior-art", "Prior art"),
        ("patent-abstract", "Abstract"),
    ],
    "paper": _SCI_SECTION,
    "report": _SCI_SECTION,
    "review": [
        ("sci-abstract", "Abstract"),
        ("sci-introduction", "Introduction"),
        ("sci-methods", "Scope & method"),
        ("sci-survey-section", "Synthesis section"),
        ("sci-discussion", "Discussion"),
        ("sci-conclusion", "Conclusion"),
    ],
    "manufacturing": [
        ("sci-abstract", "Overview"),
        ("components", "Components / BOM"),
        ("sci-methods", "Description"),
    ],
    # book/summary reuse the same sci-* styles as their closest existing
    # genre (paper/review) rather than inventing new ones — see SCAFFOLDS
    # below for the per-heading mapping + rationale.
    "book": [
        ("sci-abstract", "Preface"),
        ("sci-introduction", "Introduction"),
        ("sci-related-work", "Background"),
        ("sci-survey-section", "Chapter"),
        ("sci-conclusion", "Conclusion"),
    ],
    "summary": [
        ("sci-abstract", "Summary"),
        ("sci-results", "Key points"),
        ("sci-methods", "Details"),
    ],
}


#: The standard section skeleton laid down when a draft of this ``doc_type``
#: is created: an ordered list of ``(heading, style)``.
#: The new-draft flow appends these as styled headings after the title, so
#: picking a genre yields a styled skeleton to fill (each section's style
#: skill then fires when editing under it). Empty/absent → no scaffold.
#: A ``None`` style means "no matching sci-* style" (rather than inventing
#: one) — the section still lands, just without a style skill.
SCAFFOLDS: dict[str, list[tuple[str, str | None]]] = {
    "patent": [
        ("Field of the Invention", "patent-description"),
        ("Background", "patent-description"),
        ("Summary", "patent-description"),
        ("Brief Description of the Drawings", "patent-image-part"),
        ("Detailed Description", "patent-description"),
        ("Claims", "patent-claim"),
        ("Abstract", "patent-abstract"),
        ("Prior Art / IDS Disclosures", "patent-prior-art"),
    ],
    "paper": [
        ("Abstract", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Related Work", "sci-related-work"),
        ("Methods", "sci-methods"),
        ("Results", "sci-results"),
        ("Discussion", "sci-discussion"),
        ("Conclusion", "sci-conclusion"),
    ],
    "report": [
        ("Executive Summary", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Findings", "sci-results"),
        ("Discussion", "sci-discussion"),
        ("Conclusion", "sci-conclusion"),
    ],
    "review": [
        ("Abstract", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Scope & Method", "sci-methods"),
        ("Themes", "sci-survey-section"),
        ("Open Problems", "sci-survey-section"),
        ("Conclusion", "sci-conclusion"),
    ],
    "manufacturing": [
        ("Overview", "sci-abstract"),
        ("Components", "components"),
        ("Description", "sci-methods"),
    ],
    # book (multi-chapter monograph, paper-writing pipeline rung 4):
    # Preface/Background mirror a paper's Abstract/Related-Work framing
    # role; each numbered chapter is an open-ended synthesis section, the
    # closest existing analog being the review's "Themes"/"Open Problems"
    # (``sci-survey-section``); Bibliography has no sci-* analog (a
    # reference list, not a discussion section) so it is left unstyled
    # rather than inventing a style for one scaffold.
    "book": [
        ("Preface", "sci-abstract"),
        ("Introduction", "sci-introduction"),
        ("Background", "sci-related-work"),
        ("Chapter 1", "sci-survey-section"),
        ("Chapter 2", "sci-survey-section"),
        ("Chapter 3", "sci-survey-section"),
        ("Conclusion", "sci-conclusion"),
        ("Bibliography", None),
    ],
    # summary (short digest, distinct from the comprehensive `review`):
    # Summary parallels a report's Executive Summary; Key Points mirrors
    # Findings (``sci-results``); Details is the substantive body, the
    # closest analog being a methods/description section
    # (``sci-methods``); References has no sci-* analog, left unstyled
    # (same reasoning as book's Bibliography above).
    "summary": [
        ("Summary", "sci-abstract"),
        ("Key Points", "sci-results"),
        ("Details", "sci-methods"),
        ("References", None),
    ],
}


def section_styles_for(doc_type: str) -> list[tuple[str, str]]:
    """The section styles to offer for a genre (empty → no dropdown)."""
    return SECTION_STYLES.get(doc_type, [])
