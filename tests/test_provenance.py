"""Provenance tiers for recall candidates (source-backfill slice 6)."""

from __future__ import annotations

from precis.backfill.provenance import (
    LEAD,
    PEER_REVIEWED,
    PRIOR_ART,
    TIERS,
    tier_for,
    tier_tag,
)


def test_tier_for_maps_kinds() -> None:
    assert tier_for("paper") is PEER_REVIEWED
    assert tier_for("cfp") is PEER_REVIEWED
    assert tier_for("patent") is PRIOR_ART
    assert tier_for("datasheet") is PRIOR_ART
    assert tier_for("web") is PRIOR_ART
    assert tier_for("memory") is LEAD


def test_tier_for_unknown_defaults_to_lead() -> None:
    # conservative: an unknown / own-authored kind is never silently "evidence"
    assert tier_for("scribble") is LEAD
    assert tier_for(None) is LEAD


def test_tiers_ordered_by_strength() -> None:
    ranks = [t.rank for t in TIERS]
    assert ranks == sorted(ranks)  # strongest (rank 0) first
    weights = [t.weight for t in TIERS]
    assert weights == sorted(weights, reverse=True)  # weight falls with rank
    assert PEER_REVIEWED.weight == 1.0  # the reference tier is un-penalised
    assert all(0.0 < t.weight <= 1.0 for t in TIERS)


def test_tier_tag_brackets() -> None:
    assert tier_tag("paper") == "[peer-reviewed]"
    assert tier_tag("patent") == "[prior-art]"
    assert tier_tag("memory") == "[own-note]"
    assert tier_tag(None) == "[own-note]"


def test_every_tier_has_an_admonition() -> None:
    # the skill surfaces these verbatim — none may be blank
    assert all(t.admonition.strip() for t in TIERS)
