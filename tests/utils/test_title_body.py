"""title_body_score on synthetic title/body pairs (gr264184)."""

from __future__ import annotations

from precis.utils.title_body import is_placeholder_title, title_body_score, title_terms

MOS2_BODY = (
    "We study monolayer MoS 2 field-effect transistors. The MoS2 channel "
    "shows high mobility and the quantum transport simulation of the "
    "sub-1 nm gate length devices is performed with ab-initio methods."
)


def test_matching_title_scores_high() -> None:
    s = title_body_score(
        "Ab-Initio Quantum Transport Simulation of Monolayer MoS2 Transistors",
        MOS2_BODY,
    )
    assert s is not None and s.score > 0.8
    assert "MoS2" in s.matched


def test_mismatched_title_scores_low() -> None:
    s = title_body_score(
        "A developmental model for thalidomide defects",
        "High-resolution TEM of carbon nanotube peapods filled with fullerenes.",
    )
    assert s is not None and s.score < 0.2


def test_formula_token_dominates() -> None:
    # Same generic words match; only the material differs.
    same = title_body_score(
        "Quantum transport in monolayer MoS2 transistors", MOS2_BODY
    )
    swapped = title_body_score(
        "Quantum transport in monolayer WSe2 transistors", MOS2_BODY
    )
    assert same is not None and swapped is not None
    assert same.score - swapped.score > 0.4
    assert "WSe2" in swapped.missing


def test_formula_matches_split_subscript() -> None:
    s = title_body_score("WSe2 devices", "The WSe 2 devices were measured.")
    assert s is not None and s.score == 1.0


def test_nothing_to_judge_returns_none() -> None:
    assert title_body_score("A study of the new approach", "anything") is None
    assert title_body_score("Graphene transistors", "   ") is None


def test_terms_weights() -> None:
    t = title_terms("HRTEM of WSe2 peapods")
    assert t["WSe2"] > t["HRTEM"] > t["peapods"]


def test_missing_formula_caps_rank_score() -> None:
    s = title_body_score("Quantum transport in monolayer WSe2 transistors", MOS2_BODY)
    assert s is not None
    assert s.missing_formulas == ("WSe2",)
    assert s.rank_score <= 0.35


def test_placeholder_titles() -> None:
    for t in (
        "Microsoft Word - Appendix.doc",
        "nnano.2006.77 feature.indd",
        "Untitled #2",
        "(no title)",
        "Session Title",
        "Query",
        "RadicalAbundance",
        "Template for Electronic Submission to ACS Journals",
        "a35v364a.pdf",
    ):
        assert is_placeholder_title(t), t


def test_real_titles_not_placeholders() -> None:
    for t in (
        "A developmental model for thalidomide defects",
        "Template-engineered hollow porous MnO2 microspheres",
        "Atomically thin boron nitride: a tunnelling barrier for graphene devices",
    ):
        assert not is_placeholder_title(t), t
