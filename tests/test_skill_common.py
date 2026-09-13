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
    VALID_TAGS,
    FrontmatterError,
    SkillFrontmatter,
    extract_wikilinks,
    flavor_tag,
    kind_label,
    parse_frontmatter,
    unknown_kinds,
    unknown_tags,
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


# ── answers (list shapes, question-target build) ───────────────────────


def test_answers_block_form() -> None:
    text = (
        "---\n"
        "summary: top-level orientation\n"
        "answers:\n"
        "  - how do I check my build?\n"
        "  - how do I file a bug?\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm.answers == (
        "how do I check my build?",
        "how do I file a bug?",
    )
    assert fm.summary == "top-level orientation"


def test_answers_inline_comma_form() -> None:
    text = "---\nanswers: first question?, second question?\n---\n"
    fm = parse_frontmatter(text)
    assert fm.answers == ("first question?", "second question?")


def test_answers_default_empty() -> None:
    fm = parse_frontmatter("---\nid: precis-overview\n---\n")
    assert fm.answers == ()


def test_answers_is_a_known_key_not_extra() -> None:
    # ``answers:`` must validate as a registered field, not fall into
    # the unknown-key ``extra`` bucket.
    fm = parse_frontmatter("---\nanswers:\n  - a question?\n---\n")
    assert fm.answers == ("a question?",)
    assert "answers" not in fm.extra


def test_answers_and_summary_absent_on_plain_skill() -> None:
    # Most existing skills predate the question-targets build and carry
    # neither field — the parser must not require them.
    text = "---\nid: precis-overview\nstatus: active\n---\nbody\n"
    fm = parse_frontmatter(text)
    assert fm.summary is None
    assert fm.answers == ()


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


# ── tags: axis (docs/backlog/skill-graph.md slice 1) ───────────────────


def test_tags_block_form() -> None:
    text = "---\ntags:\n  - orientation\n  - workflow\n---\n"
    fm = parse_frontmatter(text)
    assert fm.tags == ("orientation", "workflow")


def test_tags_inline_comma_form() -> None:
    text = "---\ntags: orientation, workflow\n---\n"
    fm = parse_frontmatter(text)
    assert fm.tags == ("orientation", "workflow")


def test_tags_default_empty() -> None:
    fm = parse_frontmatter("---\nid: precis-overview\n---\n")
    assert fm.tags == ()


def test_unknown_tags_flags_out_of_vocab_value() -> None:
    assert unknown_tags(("orientation", "not-a-real-tag")) == ("not-a-real-tag",)


def test_unknown_tags_flags_kind_named_tag() -> None:
    # "paper" is a registered kind name — rejected as a tag even though
    # it isn't itself in VALID_TAGS; kinds are a separate axis.
    assert "paper" not in VALID_TAGS
    assert unknown_tags(("paper",)) == ("paper",)


def test_unknown_tags_empty_for_clean_input() -> None:
    assert unknown_tags(VALID_TAGS) == ()


# ── kinds: axis (docs/backlog/skill-graph.md slice 1) ──────────────────


def test_kinds_block_form() -> None:
    text = "---\nkinds:\n  - paper\n  - patent\n---\n"
    fm = parse_frontmatter(text)
    assert fm.kinds == ("paper", "patent")


def test_kinds_inline_comma_form() -> None:
    text = "---\nkinds: paper, patent\n---\n"
    fm = parse_frontmatter(text)
    assert fm.kinds == ("paper", "patent")


def test_kinds_absent_is_none() -> None:
    # Distinct from present-but-empty — absence means "not yet
    # migrated", and availability gating falls back to applies-to:.
    fm = parse_frontmatter("---\nid: precis-overview\n---\n")
    assert fm.kinds is None


def test_kinds_present_but_empty_is_empty_tuple() -> None:
    text = "---\nkinds:\nstatus: active\n---\n"
    fm = parse_frontmatter(text)
    assert fm.kinds == ()
    assert fm.status == "active"


def test_unknown_kinds_flags_unregistered_name() -> None:
    assert unknown_kinds(("paper", "not-a-kind")) == ("not-a-kind",)


def test_unknown_kinds_empty_for_registered_names() -> None:
    assert unknown_kinds(("paper", "patent")) == ()


def test_validators_see_plugin_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gates must consult the merged registry (built-ins + plugin
    # kinds), not the raw KIND_CODES module dict: a plugin kind is a
    # valid kinds: entry and a *rejected* tag (kind-named).
    from precis.utils import handle_registry

    monkeypatch.setitem(handle_registry._plugin_kind_codes, "plugkind", "zq")
    monkeypatch.setattr(handle_registry, "_load_plugin_codes", lambda: None)
    assert unknown_kinds(("plugkind",)) == ()
    assert unknown_tags(("plugkind",)) == ("plugkind",)


def test_kind_label_includes_short_code() -> None:
    assert kind_label("paper") == "paper (pa)"


def test_kind_label_falls_back_for_unregistered_name() -> None:
    assert kind_label("not-a-kind") == "not-a-kind"


# ── [[slug]] wikilink extraction ────────────────────────────────────────


def test_extract_wikilinks_finds_targets() -> None:
    text = "See [[precis-overview]] and [[precis-toc]] for more."
    assert extract_wikilinks(text) == ("precis-overview", "precis-toc")


def test_extract_wikilinks_deduplicates_order_preserving() -> None:
    text = "[[a]] ... [[b]] ... [[a]] again"
    assert extract_wikilinks(text) == ("a", "b")


def test_extract_wikilinks_empty_when_none() -> None:
    assert extract_wikilinks("no links here") == ()
