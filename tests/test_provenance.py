"""Provenance grades for recall candidates (source-backfill slice 6)."""

from __future__ import annotations

from precis.backfill.provenance import (
    LEAD,
    PEER_REVIEWED,
    PRIOR_ART,
    SOURCE_GRADES,
    grade_for,
    grade_tag,
)


def test_grade_for_maps_kinds() -> None:
    assert grade_for("paper") is PEER_REVIEWED
    assert grade_for("cfp") is PEER_REVIEWED
    assert grade_for("patent") is PRIOR_ART
    assert grade_for("datasheet") is PRIOR_ART
    assert grade_for("web") is PRIOR_ART
    assert grade_for("memory") is LEAD


def test_grade_for_unknown_defaults_to_lead() -> None:
    # conservative: an unknown / own-authored kind is never silently "evidence"
    assert grade_for("scribble") is LEAD
    assert grade_for(None) is LEAD


def test_grades_ordered_by_strength() -> None:
    ranks = [t.rank for t in SOURCE_GRADES]
    assert ranks == sorted(ranks)  # strongest (rank 0) first
    weights = [t.weight for t in SOURCE_GRADES]
    assert weights == sorted(weights, reverse=True)  # weight falls with rank
    assert PEER_REVIEWED.weight == 1.0  # the reference grade is un-penalised
    assert all(0.0 < t.weight <= 1.0 for t in SOURCE_GRADES)


def test_grade_tag_brackets() -> None:
    assert grade_tag("paper") == "[peer-reviewed]"
    assert grade_tag("patent") == "[prior-art]"
    assert grade_tag("memory") == "[own-note]"
    assert grade_tag(None) == "[own-note]"


def test_every_grade_has_an_admonition() -> None:
    # the skill surfaces these verbatim — none may be blank
    assert all(t.admonition.strip() for t in SOURCE_GRADES)
