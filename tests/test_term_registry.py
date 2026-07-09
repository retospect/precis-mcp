"""Pure unit tests for the term-registry numbering policy (ADR 0052).

No DB — the callout arithmetic lives in :mod:`precis.draft.registry` precisely
so the two behaviours (insert-stable vs render-spaced) can be pinned in
isolation.
"""

from __future__ import annotations

from precis.draft import registry as R


def test_policies_are_configured() -> None:
    assert R.policy_for("components").assign == "insert"
    assert R.policy_for("parts").assign == "render"
    assert R.policy_for("glossary").assign == "none"
    # Unknown / unset → glossary (unnumbered) default.
    assert R.policy_for(None).assign == "none"
    assert R.policy_for("nonsense").assign == "none"


def test_section_style_binds_registry() -> None:
    assert R.registry_for_style("patent-image-part") == "parts"
    assert R.registry_for_style("components") == "components"
    assert R.registry_for_style("bom") == "components"
    # A style that owns no registry falls through to the glossary default.
    assert R.registry_for_style("sci-methods") == R.DEFAULT_REGISTRY
    assert R.registry_for_style(None) == R.DEFAULT_REGISTRY


def test_insert_callout_is_consecutive_from_start() -> None:
    pol = R.policy_for("components")  # {start:1, step:1}
    assert R.next_insert_callout([], pol) == 1
    assert R.next_insert_callout([1], pol) == 2
    assert R.next_insert_callout([1, 2, 3], pol) == 4


def test_insert_callout_is_stable_under_reorder_and_gaps() -> None:
    """A BOM item number is taken as it goes and never re-derived, so it does
    not move when the table is re-sorted or an earlier row is removed."""
    pol = R.policy_for("components")
    # Order of the existing set doesn't change the next index.
    assert R.next_insert_callout([3, 1, 2], pol) == 4
    # A hole from a deleted row (1,2 gone) does NOT get back-filled — the next
    # number always advances past the current max, so live numbers never shift.
    assert R.next_insert_callout([3], pol) == 4


def test_render_callouts_are_spaced_and_boundary_aligned() -> None:
    pol = R.policy_for("parts")  # {start:100, step:5}
    got = R.render_callouts(["dc1", "dc2", "dc3"], pol)
    assert got == {"dc1": 100, "dc2": 105, "dc3": 110}


def test_render_callouts_recompute_on_reorder() -> None:
    """Unlike insert, render numerals are positional: moving a leaf renumbers
    the whole series so the spacing stays clean (ADR §3, reorder-safe)."""
    pol = R.policy_for("parts")
    before = R.render_callouts(["a", "b", "c"], pol)
    after = R.render_callouts(["b", "a", "c"], pol)  # a and b swapped
    assert before == {"a": 100, "b": 105, "c": 110}
    assert after == {"b": 100, "a": 105, "c": 110}


def test_heading_title_defaults() -> None:
    assert R.heading_title("glossary") == "Glossary"
    assert R.heading_title("parts") == "Reference Numerals"
    assert R.heading_title("components") == "Components"
