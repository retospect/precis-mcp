"""Tests for SkillHandler — markdown docs served from data/skills/.

The skills are real package data (`src/precis/data/skills/*.md`), so
these tests assert against the actual files shipped with the package.
That's intentional: they double as a packaging smoke test.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't actually need a store; pass None."""
    return SkillHandler(store=None)  # type: ignore[arg-type]


# ── single fetch ──────────────────────────────────────────────────────


def test_get_existing_skill(skill: SkillHandler) -> None:
    out = skill.get(id="precis-overview")
    # The first H1 should match the canonical skill name.
    assert "precis-overview" in out.body or "Precis" in out.body
    assert len(out.body) > 100  # not empty


def test_get_paper_skill_documents_navigation(skill: SkillHandler) -> None:
    """The phase 3.5 navigation update added a 'Navigate' section."""
    out = skill.get(id="precis-paper-help")
    assert "Navigate" in out.body or "TOC" in out.body


def test_get_missing_raises_with_options(skill: SkillHandler) -> None:
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill")
    err = excinfo.value
    assert err.options is not None
    assert any("precis-overview" in s for s in err.options)


def test_invalid_slug_raises(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="invalid skill slug"):
        skill.get(id="UPPERCASE")
    with pytest.raises(BadInput, match="invalid skill slug"):
        skill.get(id="path/traversal")


# ── index view ────────────────────────────────────────────────────────


def test_bare_get_lists_skills(skill: SkillHandler) -> None:
    out = skill.get()
    assert "skill" in out.body.lower()
    assert "precis-overview" in out.body
    # Title column populated for the major skills.
    # (Not every skill needs a title; we only check overall shape.)
    assert "Next:" in out.body  # hint trailer


def test_path_view_also_lists(skill: SkillHandler) -> None:
    out = skill.get(id="/index")
    assert "precis-overview" in out.body


# ── search ────────────────────────────────────────────────────────────


def test_search_finds_term(skill: SkillHandler) -> None:
    """Most skills mention 'kind' somewhere — sanity check fulltext."""
    out = skill.search(q="kind")
    assert "skill match" in out.body
    # Each hit should reference a real slug.
    assert "precis-" in out.body


def test_search_no_match(skill: SkillHandler) -> None:
    out = skill.search(q="xyzzy-no-such-token-anywhere")
    assert "no skills mention" in out.body


def test_search_requires_query(skill: SkillHandler) -> None:
    with pytest.raises(BadInput):
        skill.search()
    with pytest.raises(BadInput):
        skill.search(q="   ")


# ── packaging guarantees ─────────────────────────────────────────────


def test_skills_directory_has_overview(skill: SkillHandler) -> None:
    """The overview is the agent's entry point — it must exist."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert "precis-overview" in skills


def test_skill_count_reasonable(skill: SkillHandler) -> None:
    """Sanity check that we ship a handful of skills, not zero or 9000."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert 5 <= len(skills) <= 100


# ── synthesized precis-help skill ────────────────────────────────────


def test_precis_help_falls_back_without_registry(skill: SkillHandler) -> None:
    """Without bind_registry, precis-help still resolves but is a stub."""
    out = skill.get(id="precis-help")
    assert "precis-help" in out.body
    assert "registry not wired" in out.body


def test_precis_help_lists_active_kinds(skill: SkillHandler) -> None:
    """When the registry is bound, precis-help enumerates every kind."""

    class _FakeSpec:
        def __init__(
            self,
            kind: str,
            *,
            description: str = "",
            supports_get: bool = True,
            supports_search: bool = True,
            supports_put: bool = False,
        ) -> None:
            self.kind = kind
            self.description = description
            self.supports_get = supports_get
            self.supports_search = supports_search
            self.supports_put = supports_put

    class _FakeHandler:
        def __init__(self, spec: _FakeSpec) -> None:
            self.spec = spec

    class _FakeReg:
        def __init__(self, handlers: list[_FakeHandler]) -> None:
            self._h = {h.spec.kind: h for h in handlers}

        def kinds(self) -> list[str]:
            return sorted(self._h.keys())

        def get(self, kind: str) -> _FakeHandler:
            return self._h[kind]

    handlers = [
        _FakeHandler(_FakeSpec("calc", description="Math expressions")),
        _FakeHandler(
            _FakeSpec(
                "todo",
                description="Tasks with status tracking",
                supports_put=True,
            )
        ),
        _FakeHandler(_FakeSpec("paper", description="Research papers")),
    ]
    skill.bind_registry(_FakeReg(handlers))

    out = skill.get(id="precis-help")
    assert "calc" in out.body
    assert "todo" in out.body
    assert "paper" in out.body
    # Verbs surfaced.
    assert "get / search / put" in out.body  # todo has put
    assert "get / search" in out.body  # calc/paper don't
    assert "3 kinds active" in out.body


def test_precis_help_listed_in_index(skill: SkillHandler) -> None:
    out = skill.get()
    assert "precis-help" in out.body
    # Hint trailer should reference it.
    assert "precis-help" in out.body
    assert "active kinds" in out.body
