"""thread_type → persona registry (ADR 0051 §2, slice A2)."""

from __future__ import annotations

from precis.workers import thread_persona as tp


def test_default_persona_is_the_operational_manual() -> None:
    """The write-document floor is precis-tasks-help — byte-identical to the
    pre-A2 pinned skill, so the cached layer does not shift."""
    spec = tp.persona_for("write-document")
    assert spec.persona_skill_id == "precis-tasks-help"


def test_unknown_and_none_thread_type_fall_back_to_default() -> None:
    default = tp.persona_for(tp.DEFAULT_THREAD_TYPE)
    assert tp.persona_for(None) == default
    assert tp.persona_for("no-such-thread-type") == default


def test_review_persona_registered_with_extension_verbs() -> None:
    spec = tp.persona_for("review")
    assert spec.persona_skill_id == "precis-draft-reviewer"
    assert "flag-claim" in spec.extension_verbs


def test_registered_personas_point_at_shipped_skills() -> None:
    """Every registry persona must resolve to a real skill body (never the
    error stub) — a missing skill would silently degrade the floor."""
    from precis.handlers.skill import _load_skills_map

    slugs = set(_load_skills_map())
    for thread_type, spec in tp.THREAD_PERSONAS.items():
        assert spec.persona_skill_id in slugs, (
            f"{thread_type} persona {spec.persona_skill_id!r} is not a shipped skill"
        )


def test_resolve_thread_type_from_signals() -> None:
    assert tp.resolve_thread_type() == tp.DEFAULT_THREAD_TYPE
    assert tp.resolve_thread_type(has_review=True) == "review"
    assert tp.resolve_thread_type(is_dream=True) == "dream"
    # review wins over dream when both signal (a reviewer tick is a review)
    assert tp.resolve_thread_type(has_review=True, is_dream=True) == "review"
