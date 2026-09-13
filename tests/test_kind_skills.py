"""Tests for :mod:`precis.skill_index.kind_skills` — the kind-scoped
skill breadcrumb consumed outside ``handlers.skill`` (docs/backlog/
skill-graph.md slice 4).

Corpus-shaped unit tests: monkeypatch ``handlers.skill.skill_corpus_texts``
to a small fixed ``{slug: raw}`` map so behaviour doesn't depend on the
real shipped corpus, and clear the module's own graph cache around every
test (it's process-wide, same shape as ``handlers.skill``'s own cache).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from precis.skill_index import kind_skills


def _skill(body: str, *, front: str) -> str:
    return f"---\n{front}\n---\n# T\n## op\n{body}\n"


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    kind_skills._cache_clear()
    yield
    kind_skills._cache_clear()


def _patch_corpus(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    import precis.handlers.skill as skill_mod

    monkeypatch.setattr(skill_mod, "skill_corpus_texts", lambda: dict(files))


def test_hint_lists_kind_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "precis-se-help": _skill("body", front="flavor: reference\nkinds:\n  - se"),
        "precis-se-design-help": _skill(
            "body", front="flavor: reference\nkinds:\n  - se"
        ),
    }
    _patch_corpus(monkeypatch, files)
    hint = kind_skills.kind_skill_hint("se")
    assert hint == (
        "skills for kind='se': get(kind='skill', id='precis-se-design-help') "
        "· get(kind='skill', id='precis-se-help')"
    )


def test_hint_none_for_kind_with_no_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "precis-se-help": _skill("body", front="flavor: reference\nkinds:\n  - se"),
    }
    _patch_corpus(monkeypatch, files)
    assert kind_skills.kind_skill_hint("nm") is None


def test_hint_excludes_personas(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "precis-se-help": _skill("body", front="flavor: reference\nkinds:\n  - se"),
        "precis-se-persona": _skill("body", front="flavor: persona\nkinds:\n  - se"),
    }
    _patch_corpus(monkeypatch, files)
    hint = kind_skills.kind_skill_hint("se")
    assert hint == "skills for kind='se': get(kind='skill', id='precis-se-help')"
    assert "persona" not in (hint or "")


def test_hint_caps_at_four(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        f"precis-se-help-{i}": _skill("body", front="flavor: reference\nkinds:\n  - se")
        for i in range(6)
    }
    _patch_corpus(monkeypatch, files)
    hint = kind_skills.kind_skill_hint("se")
    assert hint is not None
    assert hint.count("get(kind='skill'") == 4


def test_hint_degrades_silently_on_graph_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken graph build must never raise out of ``kind_skill_hint`` —
    both consumer seams (kind-help read, error-path hint) treat this as
    a strictly additive layer."""

    def _boom(files: dict[str, str]) -> None:
        raise RuntimeError("corpus scan blew up")

    monkeypatch.setattr(kind_skills, "build_skill_graph", _boom)
    _patch_corpus(monkeypatch, {"a": _skill("body", front="flavor: reference")})
    assert kind_skills.kind_skill_hint("se") is None


def test_hint_result_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The corpus is scanned once per process, not once per call."""
    calls = {"n": 0}

    def _texts() -> dict[str, str]:
        calls["n"] += 1
        return {
            "precis-se-help": _skill("body", front="flavor: reference\nkinds:\n  - se")
        }

    import precis.handlers.skill as skill_mod

    monkeypatch.setattr(skill_mod, "skill_corpus_texts", _texts)
    kind_skills.kind_skill_hint("se")
    kind_skills.kind_skill_hint("se")
    assert calls["n"] == 1
