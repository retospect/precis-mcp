"""Every non-Python file under ``scripts/`` is ruff-excluded (gr514997).

ruff parses ANY path handed to it explicitly — ``ruff check
scripts/prod-precis`` reported 138 invalid-syntax "errors" on a bash
script. ``[tool.ruff] force-exclude`` plus an ``extend-exclude`` list
covering every non-Python file under ``scripts/`` makes the explicit call
a skip. This test keeps the list honest in both directions:

- a new bash script / data file that no pattern covers → uncovered failure;
- an exclude pattern matching nothing → stale-entry failure;
- a pattern that swallows a Python file (e.g. ``scripts/*``) → lint loss
  caught by the python-coverage assertion.

"Python" means ``*.py``/``*.pyi`` or an extensionless file whose shebang
names python (mirrors ``tests/test_scripts_use_uv.py``'s inventory rule).

The judged set is TRACKED content only. The first version globbed the
patterns on disk and ``rglob``'d every directory match: ``.claude``
expanded through ``.claude/worktrees/<sibling>/`` — nested repo copies,
~27k files locally — and the pattern evaluation alone outlasted the
per-test timeout on ``--selected`` runs (gr537677). ``git ls-files``
enumerates the same tracked content without descending anywhere; pattern
coverage is computed by matching against that set, never the filesystem,
so no pattern shape can grow the walk back. With no checkout (the gate
container runs on ``git archive HEAD``) a filesystem walk prunes ignored
directories BEFORE descending and filters the rest through
``.gitignore`` — the shape ``tests/test_deploy_tree_no_secrets.py`` uses.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import tomllib
from pathlib import Path

from tests._policy_scan import _ignore_glob, gitignore_matcher

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_PREFIX = "scripts/"

# Directory names pruned before descending in the no-checkout walk.
# Every name is provably untracked (``.git`` can never be, the rest are
# runtime trees ``.gitignore`` covers) — pruning can't drop judged
# content. ``build``/``dist`` stay OUT: tracked files live under
# ``src/precis_web/static/pdfjs/build/``. ``worktrees`` is the one that
# matters — sibling worktrees under ``.claude/worktrees/`` are the gr537677
# walk; ``.claude`` itself must still be descended (its skills/agents/
# commands trees are tracked), so the matcher can't drive pruning: a
# ``.claude`` dir query reads "ignored" because its direct children are,
# while the negated subdirs it holds are not.
_PRUNE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".test-hang-stacks",
    ".venv",
    "__pycache__",
    "dist",
    "htmlcov",
    "node_modules",
    "venv",
    "worktrees",
}


def _is_python(path: Path) -> bool:
    if path.suffix in (".py", ".pyi"):
        return True
    if path.suffix:
        return False
    try:
        first = path.read_text(encoding="utf-8").partition("\n")[0]
    except UnicodeDecodeError:
        return False
    return first.startswith("#!") and "python" in first


def _ruff_config() -> tuple[list[str], bool]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "ruff"
    ]
    return cfg["extend-exclude"], cfg.get("force-exclude", False)


def _git_tracked() -> set[str] | None:
    """Tracked files relative to ROOT, or ``None`` without a checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "-c", "safe.directory=*", "ls-files", "-z"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {rel for rel in out.split("\0") if rel}


def _walked_files() -> set[str]:
    """No-checkout stand-in for ``git ls-files``.

    ``os.walk`` with ``dirnames`` pruned before descent; files then drop
    through the ``.gitignore`` matcher so runtime scratch a checkout
    wouldn't track stays out of the judged set.
    """
    ignored = gitignore_matcher(ROOT)
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        out.update(prefix + name for name in filenames if not ignored(prefix + name))
    return out


@functools.cache
def _repo_files() -> set[str]:
    """Judged files, ``/``-separated paths relative to ROOT."""
    tracked = _git_tracked()
    return _walked_files() if tracked is None else tracked


def _glob_covers(rx: re.Pattern[str], rel: str) -> bool:
    """``rx`` fullmatches ``rel`` itself or one of its ancestor dirs — a
    pattern matching a directory excludes the whole tree below it."""
    parts = rel.split("/")
    return any(rx.fullmatch("/".join(parts[:i])) for i in range(1, len(parts) + 1))


@functools.cache
def _covered_files() -> dict[str, set[str]]:
    """Map each extend-exclude pattern to the judged files it covers.

    Literal patterns cover their own path or — naming a directory — the
    paths under it; glob patterns go through the repo's ``**``-aware
    glob→regex translation (``tests._policy_scan._ignore_glob``), which
    matches ruff's globset shape: ``**/`` swallows zero or more
    directories and ``*`` stays inside a component. Matching runs on the
    file set, so dotfiles the old (dot-shy) glob missed are now judged
    the way ruff really treats them — e.g. ``scripts/**/*.json`` covers
    ``scripts/.doi_status.json`` — and no ``**`` placement can re-open a
    ``.claude`` descent.
    """
    files = _repo_files()
    patterns, _ = _ruff_config()
    covered: dict[str, set[str]] = {}
    for pat in patterns:
        cut = min((pat.find(c) for c in "*?[" if c in pat), default=-1)
        if cut == -1:
            covered[pat] = {f for f in files if f == pat or f.startswith(pat + "/")}
            continue
        stem = pat[:cut]
        stem = stem.rsplit("/", 1)[0] + "/" if "/" in stem else ""
        rx = re.compile(_ignore_glob(pat))
        covered[pat] = {f for f in files if f.startswith(stem) and _glob_covers(rx, f)}
    return covered


def test_force_exclude_is_on() -> None:
    """Excludes only bind explicit paths when force-exclude is set; a
    refactor that drops it silently re-opens gr514997."""
    _, force = _ruff_config()
    assert force is True, "[tool.ruff] force-exclude must be true"


def test_nonpython_scripts_files_are_excluded() -> None:
    files = _repo_files()
    covered = _covered_files()
    uncovered = [
        rel
        for rel in sorted(files)
        if rel.startswith(SCRIPTS_PREFIX)
        and (ROOT / rel).is_file()
        and "__pycache__" not in rel.split("/")  # bytecode dropped mid-run
        and not _is_python(ROOT / rel)
        and not any(rel in hits for hits in covered.values())
    ]
    assert not uncovered, (
        "non-Python files under scripts/ that ruff would parse as Python "
        "when passed explicitly; add them to [tool.ruff] extend-exclude: "
        f"{uncovered}"
    )


def test_exclude_patterns_are_not_stale() -> None:
    stale = [pat for pat, hits in _covered_files().items() if not hits]
    assert not stale, f"extend-exclude entries matching nothing: {stale}"


def test_excludes_never_cover_python() -> None:
    swallowed = [
        f"{pat} -> {sorted(hits)}"
        for pat, hits in _covered_files().items()
        if any((ROOT / h).is_file() and _is_python(ROOT / h) for h in hits)
    ]
    assert not swallowed, (
        f"extend-exclude patterns covering linted Python files: {swallowed}"
    )
