"""Opt-in grounded-authoring reviewer — persona-selection wiring.

A review-todo carrying ``meta.author=true`` on an author-eligible lens
(``cites``/``structure`` — see ``quest/review_fanout.py``'s
``_AUTHOR_ELIGIBLE_LENSES``) should flip the reviewer persona from the
read-only ``precis-draft-reviewer`` to the opt-in
``precis-review-authoring`` skill (``_load_review_persona`` /
``_m_reviewer_persona`` in ``workers/planner_prompt.py``). This only tests
the SELECTION wiring — the reviewer's actual authoring behavior is
model-driven and out of scope here.
"""

from __future__ import annotations

from precis.utils.prompt import AssemblyContext, Profile
from precis.workers.planner_prompt import _load_review_persona, _m_reviewer_persona

#: Unique markers proving which persona body loaded (see the two skill
#: files under ``data/skills/personas/``).
_AUTHORING_MARKER = "ground it or flag it"
_READONLY_MARKER = "not its author and not its editor"


def _ctx(review: str | None, author: bool) -> AssemblyContext:
    """A bare fixture context — no store, just the memoised extras the
    assembler would have already populated via ``has_review``'s predicate
    (``predicates._review_kind``) before calling the builder."""
    return AssemblyContext(
        store=None,
        ref_id=0,
        model="claude-test",
        profile=Profile.AGENT,
        extras={"review": review, "author": author},
    )


# --------------------------------------------------------------------------
# _load_review_persona — the skill-selection primitive
# --------------------------------------------------------------------------


def test_cites_with_author_selects_authoring_persona() -> None:
    body = _load_review_persona("cites", True)
    assert _AUTHORING_MARKER in body
    assert _READONLY_MARKER not in body


def test_structure_with_author_selects_authoring_persona() -> None:
    body = _load_review_persona("structure", True)
    assert _AUTHORING_MARKER in body
    assert _READONLY_MARKER not in body


def test_cites_without_author_selects_readonly_persona() -> None:
    body = _load_review_persona("cites", False)
    assert _READONLY_MARKER in body
    assert _AUTHORING_MARKER not in body


def test_flow_with_author_selects_readonly_persona() -> None:
    """``flow`` is never author-eligible — the flag is ignored for it."""
    body = _load_review_persona("flow", True)
    assert _READONLY_MARKER in body
    assert _AUTHORING_MARKER not in body


def test_adversarial_with_author_selects_readonly_persona() -> None:
    """``adversarial`` is likewise never author-eligible."""
    body = _load_review_persona("adversarial", True)
    assert _READONLY_MARKER in body
    assert _AUTHORING_MARKER not in body


# --------------------------------------------------------------------------
# _m_reviewer_persona — the module builder (extras-driven, as the assembler
# actually calls it for a `has_review`-gated tick)
# --------------------------------------------------------------------------


def test_reviewer_persona_module_selects_authoring_for_cites_author() -> None:
    prompt = _m_reviewer_persona(_ctx("cites", True))
    assert "meta.author=true" in prompt
    assert _AUTHORING_MARKER in prompt
    assert _READONLY_MARKER not in prompt


def test_reviewer_persona_module_selects_authoring_for_structure_author() -> None:
    prompt = _m_reviewer_persona(_ctx("structure", True))
    assert _AUTHORING_MARKER in prompt
    assert _READONLY_MARKER not in prompt


def test_reviewer_persona_module_stays_readonly_without_author_flag() -> None:
    prompt = _m_reviewer_persona(_ctx("cites", False))
    assert _READONLY_MARKER in prompt
    assert _AUTHORING_MARKER not in prompt
    assert "meta.author=true" not in prompt


def test_reviewer_persona_module_stays_readonly_for_flow_author() -> None:
    prompt = _m_reviewer_persona(_ctx("flow", True))
    assert _READONLY_MARKER in prompt
    assert _AUTHORING_MARKER not in prompt


def test_reviewer_persona_module_defaults_review_kind_when_unset() -> None:
    """A missing ``meta.review`` (extras value ``None``) still renders a
    reviewer stance — falls back to the pre-existing 'structural' label,
    unaffected by the author gate."""
    prompt = _m_reviewer_persona(_ctx(None, False))
    assert "Reviewer mode — structural" in prompt
    assert _READONLY_MARKER in prompt


# --------------------------------------------------------------------------
# both persona skills resolve (regression guard: a typo'd id degrades
# silently to the terse inline fallback, which would pass every assertion
# above for the wrong reason)
# --------------------------------------------------------------------------


def test_both_persona_skills_load_from_disk() -> None:
    from precis.handlers.skill import _load_skills_map

    slugs = _load_skills_map()
    assert "precis-review-authoring" in slugs
    assert "precis-draft-reviewer" in slugs
