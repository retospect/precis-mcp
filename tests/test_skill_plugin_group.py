"""Skill plugin discovery — the ``precis.skills`` entry-point group lets
third-party packages contribute skill ``*.md`` roots to the skill index,
mirroring ``precis.handlers``. Built-ins win slug collisions.

``_walk_skill_root`` duck-types on ``iterdir``/``is_dir``/``name``/
``read_text`` so a ``pathlib.Path`` stands in for an importlib
``Traversable`` here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from precis.handlers import skill as skillmod


@pytest.fixture(autouse=True)
def _clear_skill_cache() -> Iterator[None]:
    skillmod._load_skills_map_cache_clear()
    yield
    skillmod._load_skills_map_cache_clear()


def test_plugin_skill_root_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "precis-zztest-help.md").write_text("# zztest\nbody", encoding="utf-8")
    monkeypatch.setattr(skillmod, "_plugin_skill_roots", lambda: [tmp_path])

    m = skillmod._load_skills_map()

    assert "precis-zztest-help" in m
    assert "precis-overview" in m  # built-ins still present alongside plugins


def test_builtin_wins_slug_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "precis-overview.md").write_text("PLUGIN OVERRIDE", encoding="utf-8")
    monkeypatch.setattr(skillmod, "_plugin_skill_roots", lambda: [tmp_path])

    m = skillmod._load_skills_map()

    assert "PLUGIN OVERRIDE" not in m["precis-overview"]


def test_no_plugins_is_builtins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # _plugin_skill_roots swallows its own discovery errors; with no
    # plugin roots the map is exactly the built-in corpus.
    monkeypatch.setattr(skillmod, "_plugin_skill_roots", lambda: [])
    m = skillmod._load_skills_map()
    assert "precis-overview" in m


def test_declared_plugin_skill_dirs_are_populated_and_unshadowed() -> None:
    """Each ``precis.skills`` package dir exists, ships a skill, and none of
    its slugs also lives under the built-in dir (built-ins win a clash, so a
    duplicate would be shadowed silently)."""
    from tests._skill_roots import BUILTIN_SKILLS_DIR, plugin_skill_dirs, skill_files

    builtin = {p.stem for p in BUILTIN_SKILLS_DIR.rglob("*.md")}
    for d in plugin_skill_dirs():  # asserts each dir exists
        slugs = {p.stem for p in d.rglob("*.md") if not p.name.startswith("__")}
        assert slugs, f"{d}: precis.skills package ships no skill .md"
        assert not slugs & builtin, f"{d}: shadowed by built-ins: {slugs & builtin}"
    assert "precis-pathway-help" in {p.stem for p in skill_files()}
