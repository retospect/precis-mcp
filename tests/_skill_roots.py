"""Skill corpus roots for test sweeps: built-ins plus ``precis.skills`` plugins.

Filesystem/pyproject-based on purpose (not ``importlib.metadata``): the
shared dev image's installed metadata can lag the tree, and a sweep that
silently misses a moved skill is worse than one that fails loudly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SKILLS_DIR = ROOT / "src" / "precis" / "data" / "skills"


def plugin_skill_dirs() -> list[Path]:
    """Dirs of every package declared under ``[project.entry-points."precis.skills"]``."""
    with (ROOT / "pyproject.toml").open("rb") as f:
        eps = tomllib.load(f)["project"]["entry-points"].get("precis.skills", {})
    dirs: list[Path] = []
    for name, target in sorted(eps.items()):
        d = ROOT / "src" / Path(*target.split("."))
        assert d.is_dir(), f"precis.skills entry {name!r} -> {target!r}: {d} missing"
        dirs.append(d)
    return dirs


def skill_roots() -> list[Path]:
    return [BUILTIN_SKILLS_DIR, *plugin_skill_dirs()]


def skill_files(*, recursive: bool = True) -> list[Path]:
    """Every skill ``*.md`` under every root, sorted (``__*`` names excluded)."""
    found: list[Path] = []
    for root in skill_roots():
        it = root.rglob("*.md") if recursive else root.glob("*.md")
        found.extend(p for p in it if not p.name.startswith("__"))
    return sorted(found)


def skill_key(path: Path) -> str:
    """Path relative to its own root, no suffix (``personas/x``, ``precis-get-help``)."""
    for root in skill_roots():
        if path.is_relative_to(root):
            return path.relative_to(root).with_suffix("").as_posix()
    raise ValueError(f"{path} is under no skill root")


def skill_path(slug: str) -> Path:
    """The ``<slug>.md`` file in whichever root holds it (built-in first)."""
    for root in skill_roots():
        p = root / f"{slug}.md"
        if p.exists():
            return p
    return BUILTIN_SKILLS_DIR / f"{slug}.md"  # nonexistent: callers assert on it
