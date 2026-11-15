"""gr625031 — the identify-publication skill entry point.

File-level checks over ``src/precis/data/skills/`` (no DB):
``precis-identify-publication-help`` declares the ``paper`` family so
the paper-family index lists it, its body names all six outcome classes
the identification procedure yields, and the three sibling paper skills
link to it.
"""

from __future__ import annotations

from precis.utils.skill_common import parse_frontmatter, skill_family
from tests._skill_roots import skill_path

_SLUG = "precis-identify-publication-help"


def _text() -> str:
    return skill_path(_SLUG).read_text(encoding="utf-8")


def test_family_index_lists_it() -> None:
    """``family: paper`` puts the skill on the paper-family toc
    (``get(kind='skill', id='toc', args={'family': 'paper'})``)."""
    fm = parse_frontmatter(_text())
    assert fm.id == _SLUG
    assert skill_family(_SLUG, fm) == "paper"


def test_names_six_outcome_classes() -> None:
    """Every outcome class the gripe demands appears in the body."""
    body = _text()
    for klass in (
        "corresponding manuscript",
        "prior method",
        "duplicate version",
        "meeting-abstract-only",
        "no verified public publication",
        "provider-blocked",
    ):
        assert klass in body, f"missing outcome class: {klass}"


def test_linked_from_paper_stubs_author_skills() -> None:
    """paper-help, stubs-help and author-discovery-help all link in."""
    for slug in (
        "precis-paper-help",
        "precis-stubs-help",
        "precis-author-discovery-help",
    ):
        assert _SLUG in skill_path(slug).read_text(encoding="utf-8"), slug
