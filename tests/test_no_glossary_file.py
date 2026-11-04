"""Tree gate: ``docs/glossary.md`` stays retired.

Reto retired the hand-written repo-dev glossary on 2026-11-03 (gripe
gr559340): every coined or overloaded term is a ``taxon`` under the prod root
``tn559460`` ("repo-dev vocabulary"), one hub node per memory subject, looked
up with ``search(kind='taxon', q='<term>', args={'under': 'tn559460'})``.

Two things make the forgetting stick across hundreds of worktrees and stale
briefs: this test (the file must not exist, and no tracked text file may
point at it) and the PreToolUse hook ``scripts/hooks/guard-glossary-file.py``
(a Write/Edit to that path is refused with the lookup as the next step).

Files are enumerated with ``git ls-files`` (tracked only) and fall back to a
filesystem walk when there is no checkout, the same shape as
``tests/test_deploy_tree_no_secrets.py``. The hook, this test and the
``remember`` skill may name the retired path (they explain the rule); nothing
else may.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RETIRED = "docs/glossary.md"
_LOOKUP = "search(kind='taxon', q='<term>', args={'under': 'tn559460'})"

#: Tracked files allowed to spell the retired path because they state the rule.
_ALLOWED: frozenset[str] = frozenset(
    {
        "tests/test_no_glossary_file.py",
        "scripts/hooks/guard-glossary-file.py",
        ".claude/skills/remember/SKILL.md",
        # Sealed migration (forward-only): its comment is history, never edited.
        "src/precis/migrations/0162_design_core.sql",
    }
)

_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "scratch",
    "venv",
    "worktrees",
}
_BINARY_SUFFIXES = {
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".so",
    ".whl",
    ".zip",
}


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in _REPO_ROOT.rglob("*")
            if p.is_file()
            and not (set(p.relative_to(_REPO_ROOT).parts[:-1]) & _SKIP_DIRS)
        ]
    return [_REPO_ROOT / rel for rel in out.split("\0") if rel]


def test_glossary_file_is_gone() -> None:
    assert not (_REPO_ROOT / _RETIRED).exists(), (
        f"{_RETIRED} is retired (gr559340, 2026-11-03): terms are taxa under "
        f"tn559460, {_LOOKUP}. Delete the file; add the term as a taxon."
    )


def test_no_tracked_file_points_at_the_glossary() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED or path.suffix in _BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _RETIRED in line:
                offenders.append(f"{rel}:{n}")
    assert not offenders, (
        f"{len(offenders)} pointer(s) to the retired {_RETIRED}; replace each with "
        f"the taxonomy lookup {_LOOKUP}: " + ", ".join(offenders[:20])
    )
