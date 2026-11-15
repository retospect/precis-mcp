"""gr625031 — the identify-publication skill exists, is indexed and linked.

Pure file stats over the shipped corpus: no DB, no embedder.
"""

from __future__ import annotations

from tests._skill_roots import skill_path

_SLUG = "precis-identify-publication-help"

#: The six outcome classes the gripe demands the runbook separate:
#: verified match, prior-method/cited/related, twin versions,
#: abstract-only, exhausted-nothing, and provider uncertainty.
_OUTCOME_CLASSES = (
    "corresponding manuscript",
    "related work",
    "duplicate version",
    "meeting-abstract-only",
    "no verified publication",
    "provider-blocked",
)


def _text() -> str:
    return skill_path(_SLUG).read_text(encoding="utf-8")


def test_family_index_lists_it() -> None:
    """``family: paper`` frontmatter puts the skill on the paper-family
    toc (``get(kind='skill', id='toc', args={'family': 'paper'})``),
    and its ``kinds`` land it on ``kinds='paper'`` too."""
    text = _text()
    fm = text.split("---", 2)[1]
    assert "family: paper" in fm
    kinds_line = next(line for line in fm.splitlines() if line.startswith("kinds:"))
    for kind in ("paper", "semanticscholar", "orcid", "web", "websearch"):
        assert kind in kinds_line, f"{kind} missing from kinds:"


def test_names_six_outcome_classes() -> None:
    body = _text()
    for cls in _OUTCOME_CLASSES:
        assert cls in body, f"outcome class {cls!r} not named"
    assert "provider-blocked" in body


def test_linked_from_paper_stubs_author_skills() -> None:
    for slug in (
        "precis-paper-help",
        "precis-stubs-help",
        "precis-author-discovery-help",
    ):
        assert _SLUG in skill_path(slug).read_text(encoding="utf-8"), (
            f"{slug} does not link {_SLUG}"
        )
