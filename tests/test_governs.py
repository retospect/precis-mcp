"""Tests for :mod:`precis.plans.governs` (gr657066 — gr657041 slice 3).

The ``governs: factory`` fence resolves twice: from the filesystem for
advisory routing, and from the land base ref for the write gates — the
base's declaration is what counts, so a branch cannot un-tag the fence.
The at-ref tests build their own ``git init`` repo under ``tmp_path``
(no ``needs_repo_git`` guard needed — that fixture exists for tests
that read THIS checkout's metadata).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from precis.plans.governs import (
    governed_skill_paths,
    governed_skill_paths_at_ref,
)

_PYPROJECT = (
    '[project]\nname = "x"\nversion = "0"\n'
    '[project.entry-points."precis.skills"]\n'
    'plug = "precis_plug.skills"\n'
)

_SKILL = "---\nid: {slug}\nflavor: reference\n{extra}---\n# {slug}\n## op\nbody\n"


def _repo(root: Path) -> Path:
    """A minimal checkout layout: pyproject + built-in + plugin skills."""
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "src" / "precis" / "data" / "skills").mkdir(parents=True)
    (root / "src" / "precis_plug" / "skills").mkdir(parents=True)
    return root


def _skill(
    root: Path, slug: str, *, governs: str | None = None, plug: bool = False
) -> Path:
    extra = f"governs: [{governs}]\n" if governs else ""
    d = (
        root
        / "src"
        / ("precis_plug" if plug else "precis")
        / ("skills" if plug else "data/skills")
    )
    p = d / f"{slug}.md"
    p.write_text(_SKILL.format(slug=slug, extra=extra), encoding="utf-8")
    return p


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git_repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "init",
    )
    return root


# ── filesystem variant (advisory routing) ──────────────────────────────


def test_governed_skill_paths_finds_tagged_builtins_and_plugins(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _skill(root, "a-governed", governs="factory")
    _skill(root, "b-plain")
    _skill(root, "c-plug-governed", governs="factory", plug=True)
    got = governed_skill_paths(root)
    assert got == (
        "src/precis/data/skills/a-governed.md",
        "src/precis_plug/skills/c-plug-governed.md",
    )


def test_governed_skill_paths_empty_when_none_tagged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _skill(root, "a-plain")
    assert governed_skill_paths(root) == ()


def test_governed_skill_paths_ignores_other_domains(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _skill(root, "a-other", governs="mordor")
    assert governed_skill_paths(root) == ()
    assert governed_skill_paths(root, governs="mordor") == (
        "src/precis/data/skills/a-other.md",
    )


# ── at-ref variant (the write gates) ───────────────────────────────────


def test_at_ref_reads_the_ref_not_the_worktree(tmp_path: Path) -> None:
    """The fence is the base's declaration: a tag added only in the
    working tree (an uncommitted un-tag/add) does not move the ref's set,
    and a tag removed in the worktree does not lift it."""
    root = _git_repo(_repo(tmp_path))
    tagged = _skill(root, "a-governed", governs="factory")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "add",
        "-A",
    )
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "tag it",
    )
    assert governed_skill_paths_at_ref(root, "HEAD") == (
        "src/precis/data/skills/a-governed.md",
    )
    # First commit (before the tag) sees nothing — the ref decides.
    first = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert governed_skill_paths_at_ref(root, first) == ()
    # Worktree edits don't move the fence either way.
    tagged.write_text(_SKILL.format(slug="a-governed", extra=""), encoding="utf-8")
    assert governed_skill_paths_at_ref(root, "HEAD") == (
        "src/precis/data/skills/a-governed.md",
    )
    _skill(root, "b-new-tag", governs="factory")
    assert governed_skill_paths_at_ref(root, "HEAD") == (
        "src/precis/data/skills/a-governed.md",
    )


def test_at_ref_empty_corpus(tmp_path: Path) -> None:
    root = _git_repo(_repo(tmp_path))
    _skill(root, "a-plain")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "add",
        "-A",
    )
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "plain",
    )
    assert governed_skill_paths_at_ref(root, "HEAD") == ()


def test_at_ref_fails_closed_on_bad_ref(tmp_path: Path) -> None:
    root = _git_repo(_repo(tmp_path))
    assert governed_skill_paths_at_ref(root, "refs/does-not-exist") is None
