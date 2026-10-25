"""Synthetic skill-corpus installer shared by the test_skill_*.py files (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

# ── slice 1 surfacing: graph footer, wikilink expansion, toc/search
# filters, availability-gate cutover, serve ledger ─────────────────────
#
# docs/backlog/skill-graph.md slice 1. These use a synthetic corpus
# (monkeypatched onto the module-level caches) rather than the real
# shipped skills — slice 2 hasn't swept ``tags:``/``kinds:`` onto real
# files yet, so exercising the surfacing logic needs files that
# actually carry the axes.


def _install_corpus(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    """Replace the module-wide skill corpus + derived graph for one test."""
    import precis.handlers.skill as skill_mod

    monkeypatch.setattr(skill_mod, "_SKILLS_MAP_CACHE", dict(files))
    monkeypatch.setattr(skill_mod, "_SKILL_GRAPH_CACHE", None)


def _md(*, front: str = "flavor: reference", body: str = "body") -> str:
    return f"---\n{front}\n---\n# T\n{body}\n"
