"""Tests for the redesigned frontmatter parser in ``handlers._skill_common``.

Covers:
- Scalar parsing + kebab→snake normalisation (parity with the old
  ``skill.py:_parse_frontmatter`` for existing files).
- Flavour validation (decision 7 hard-fail static gate).
- Inline + block list shapes for ``invokes-personas:``.
- Unknown keys preserved in ``extra``.
- ``flavor_tag()`` helper.
"""

from __future__ import annotations

import pytest

from precis.handlers._skill_common import (
    VALID_FLAVORS,
    FrontmatterError,
    SkillFrontmatter,
    flavor_tag,
    parse_frontmatter,
)

# ── basic shape ───────────────────────────────────────────────────────


def test_no_frontmatter_returns_empty() -> None:
    fm = parse_frontmatter("# precis-overview\n\nbody text\n")
    assert fm == SkillFrontmatter()


def test_unterminated_frontmatter_returns_empty() -> None:
    # Missing closing ``---`` — treat as no frontmatter, don't crash.
    fm = parse_frontmatter("---\nid: foo\n\nbody\n")
    assert fm == SkillFrontmatter()


def test_scalar_fields_parse() -> None:
    text = (
        "---\n"
        "id: precis-overview\n"
        "title: precis — seven verbs\n"
        "status: phase-10\n"
        "tier: 1\n"
        "floor: any\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.id == "precis-overview"
    assert fm.title == "precis — seven verbs"
    assert fm.status == "phase-10"
    assert fm.tier == "1"
    assert fm.floor == "any"


def test_kebab_keys_map_to_snake_fields() -> None:
    text = (
        "---\n"
        "applies-to: put (every kind that supports it)\n"
        "last-updated: 2026-05-24\n"
        "available-when: PRECIS_EPO_KEY\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.applies_to == "put (every kind that supports it)"
    assert fm.last_updated == "2026-05-24"
    assert fm.available_when == "PRECIS_EPO_KEY"


def test_quotes_are_stripped() -> None:
    text = "---\ntitle: \"precis — seven verbs\"\nstatus: 'active'\n---\n"
    fm = parse_frontmatter(text)
    assert fm.title == "precis — seven verbs"
    assert fm.status == "active"


# ── flavour validation ────────────────────────────────────────────────


@pytest.mark.parametrize("flavor", VALID_FLAVORS)
def test_each_defined_flavor_accepted(flavor: str) -> None:
    text = f"---\nflavor: {flavor}\n---\n"
    fm = parse_frontmatter(text)
    assert fm.flavor == flavor


def test_invalid_flavor_raises() -> None:
    text = "---\nflavor: vibes\n---\n"
    with pytest.raises(FrontmatterError, match="flavor='vibes'"):
        parse_frontmatter(text)


def test_no_flavor_is_fine() -> None:
    # Skills predating the redesign carry no flavour. Parser tolerates it.
    text = "---\nid: precis-overview\n---\n"
    fm = parse_frontmatter(text)
    assert fm.flavor is None


# ── invokes_personas (list shapes) ────────────────────────────────────


def test_invokes_personas_block_form() -> None:
    text = (
        "---\n"
        "flavor: runbook\n"
        "invokes-personas:\n"
        "  - precis-adversarial-reviewer\n"
        "  - precis-citation-reviewer\n"
        "  - precis-flow-reviewer\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm.invokes_personas == (
        "precis-adversarial-reviewer",
        "precis-citation-reviewer",
        "precis-flow-reviewer",
    )


def test_invokes_personas_inline_comma_form() -> None:
    text = (
        "---\n"
        "flavor: runbook\n"
        "invokes-personas: precis-adversarial-reviewer, precis-citation-reviewer\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm.invokes_personas == (
        "precis-adversarial-reviewer",
        "precis-citation-reviewer",
    )


def test_invokes_personas_default_empty() -> None:
    fm = parse_frontmatter("---\nflavor: persona\n---\n")
    assert fm.invokes_personas == ()


def test_invokes_personas_block_followed_by_another_key() -> None:
    # A blank line (or another key) terminates the list-in-progress.
    text = (
        "---\n"
        "invokes-personas:\n"
        "  - precis-citation-reviewer\n"
        "  - precis-flow-reviewer\n"
        "status: active\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm.invokes_personas == (
        "precis-citation-reviewer",
        "precis-flow-reviewer",
    )
    assert fm.status == "active"


# ── unknown keys ──────────────────────────────────────────────────────


def test_unknown_keys_preserved_in_extra() -> None:
    text = "---\nid: foo\nexperimental-knob: yes\n---\n"
    fm = parse_frontmatter(text)
    assert fm.id == "foo"
    assert fm.extra == {"experimental-knob": "yes"}


# ── flavor_tag helper ─────────────────────────────────────────────────


def test_flavor_tag_uppercases_prefix() -> None:
    fm = SkillFrontmatter(flavor="persona")
    assert flavor_tag(fm) == "FLAVOR:persona"


def test_flavor_tag_none_when_no_flavor() -> None:
    fm = SkillFrontmatter()
    assert flavor_tag(fm) is None
