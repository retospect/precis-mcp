"""SkillHandler graph surface — linked-tag/kind/sibling footers and wikilink expansion (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from tests._skill_corpus import _install_corpus, _md


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


def test_graph_footer_shows_linked_tags_kinds_siblings(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "a": _md(
                front=("flavor: reference\ntags:\n  - orientation\nkinds:\n  - paper"),
                body="see [[b]]",
            ),
            "b": _md(body="body"),
        },
    )
    out = skill.get(id="a")
    assert "linked: b" in out.body
    assert "tags: orientation" in out.body
    assert "kinds: paper (pa)" in out.body


def test_graph_footer_tree_siblings() -> None:
    """Tree siblings come from ``_SKILL_CATEGORIES`` — use a real
    category pair rather than the synthetic corpus (categories are a
    fixed, shipped mapping)."""
    from precis.handlers.skill import _tree_siblings

    siblings = _tree_siblings("precis-get-help")
    assert "precis-get-help" not in siblings
    assert "precis-search-help" in siblings  # same "Core verbs" bucket


def test_graph_footer_caps_inbound_with_search_pointer(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers.skill import _FOOTER_LINK_CAP

    hub_slug = "hub"
    files = {hub_slug: _md(body="hub page")}
    for i in range(_FOOTER_LINK_CAP + 2):
        leaf = f"leaf{i}"
        files[leaf] = _md(body=f"see [[{hub_slug}]]")
    _install_corpus(monkeypatch, files)

    out = skill.get(id=hub_slug)
    assert f"search(kind='skill', q='{hub_slug}')" in out.body
    assert "more)" in out.body


def test_graph_footer_empty_for_persona(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {"p": _md(front="flavor: persona\ntags:\n  - orientation", body="body")},
    )
    out = skill.get(id="p")
    assert "tags: orientation" not in out.body


def test_wikilink_expands_in_full_serve(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {"a": _md(body="see [[b]] for more"), "b": _md(body="body")},
    )
    out = skill.get(id="a")
    assert "[[b]]" not in out.body
    assert "get(kind='skill', id='b')" in out.body


def test_wikilink_expands_in_chunk_serve(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No H1/H2 heading: ``chunk_by_h2`` returns a single whole-body
    # chunk at index 0 (a leading H1 would instead become its own
    # empty-heading "head chunk" at ~0, pushing the wikilinked content
    # to ~1) — see the chunker's head-chunk docstring.
    _install_corpus(
        monkeypatch,
        {
            "a": "---\nflavor: reference\n---\nsee [[b]]\n",
            "b": _md(body="body"),
        },
    )
    out = skill.get(id="a~0")
    assert "[[b]]" not in out.body
    assert "get(kind='skill', id='b')" in out.body
