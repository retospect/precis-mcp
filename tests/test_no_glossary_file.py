"""Tree gate: ``docs/glossary.md`` stays retired.

Reto retired the hand-written repo-dev glossary on 2026-11-03 (gripe
gr559340): every coined or overloaded term is a ``taxon`` under the prod root
``tn559460`` ("repo-dev vocabulary"), one hub node per memory subject, looked
up with ``search(kind='taxon', q='<term>', args={'under': 'tn559460'})``.

Two things make the forgetting stick across hundreds of worktrees and stale
briefs: this test (the file must not exist, and no tracked text file may
point at it) and the PreToolUse hook ``scripts/hooks/guard-glossary-file.py``
(a Write/Edit to that path is refused with the lookup as the next step).

Files are enumerated with ``git ls-files`` (tracked only). The gate container
has no Git metadata, so there we walk the source tree, excluding scratch,
caches and files matched by ``.gitignore``, like the other policy gates.
When ``.git`` is present, a Git failure still fails the gate loudly.
The hook, this test and the
``remember`` skill may name the retired path (they explain the rule); nothing
else may.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._policy_scan import ScanIncomplete, gitignore_matcher

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

# Only runtime directories: ``build`` and ``.claude`` contain shipped source,
# including paths explicitly re-included by .gitignore negations.
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".scratch",
    ".test-hang-stacks",
    ".venv",
    "__pycache__",
    "scratch",
    "worktrees",
}


def _tracked_files() -> list[Path]:
    if not (_REPO_ROOT / ".git").exists():
        ignored = gitignore_matcher(_REPO_ROOT)

        def fail_walk(exc: OSError) -> None:
            raise ScanIncomplete(exc, f"walking {_REPO_ROOT}") from exc

        files: list[Path] = []
        for directory, dirs, names in os.walk(_REPO_ROOT, onerror=fail_walk):
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            for name in names:
                path = Path(directory) / name
                if not ignored(path.relative_to(_REPO_ROOT).as_posix()):
                    files.append(path)
        return files
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.fail(
            f"Cannot enumerate tracked files with git ls-files: {exc}. "
            "Run this gate in a Git checkout with accessible Git metadata.",
            pytrace=False,
        )
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


@pytest.mark.parametrize("directory", ["docs", ".scratch"])
def test_glossary_gate_checks_only_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}._REPO_ROOT", tmp_path)
    clean = tmp_path / "README.md"
    clean.write_text("Terms live in the taxonomy.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    pointer = tmp_path / directory / "copied migration.sql"
    pointer.parent.mkdir()
    pointer.write_text(f"See {_RETIRED}\n", encoding="utf-8")

    test_no_tracked_file_points_at_the_glossary()

    subprocess.run(["git", "add", str(pointer)], cwd=tmp_path, check=True)
    with pytest.raises(AssertionError, match="1 pointer") as exc:
        test_no_tracked_file_points_at_the_glossary()
    assert f"{directory}/copied migration.sql:1" in str(exc.value)


@pytest.mark.parametrize(
    "directory", [".scratch", "scratch", ".venv", "__pycache__", "generated"]
)
def test_glossary_gate_without_git_checks_source_not_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    monkeypatch.setattr(f"{__name__}._REPO_ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    scratch = tmp_path / directory / "copied migration.sql"
    scratch.parent.mkdir()
    scratch.write_text(f"See {_RETIRED}\n", encoding="utf-8")

    with patch.object(subprocess, "run", side_effect=AssertionError("Git called")):
        test_no_tracked_file_points_at_the_glossary()

        pointer = tmp_path / "README.md"
        pointer.write_text(f"See {_RETIRED}\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="1 pointer") as exc:
            test_no_tracked_file_points_at_the_glossary()
    assert "README.md:1" in str(exc.value)


def test_glossary_gate_without_git_checks_reincluded_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{__name__}._REPO_ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text(
        "build/\n!src/precis_web/static/pdfjs/build/\n", encoding="utf-8"
    )
    pointer = tmp_path / "src/precis_web/static/pdfjs/build/pointer.js"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"See {_RETIRED}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="1 pointer") as exc:
        test_no_tracked_file_points_at_the_glossary()
    assert "src/precis_web/static/pdfjs/build/pointer.js:1" in str(exc.value)


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("git missing"), subprocess.CalledProcessError(128, "git")],
)
@pytest.mark.parametrize("metadata", ["directory", "file"])
def test_glossary_gate_fails_when_tracked_files_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, metadata: str
) -> None:
    monkeypatch.setattr(f"{__name__}._REPO_ROOT", tmp_path)
    if metadata == "directory":
        (tmp_path / ".git").mkdir()
    else:
        (tmp_path / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    with (
        patch.object(subprocess, "run", side_effect=error),
        pytest.raises(pytest.fail.Exception, match="Cannot enumerate tracked files"),
    ):
        test_no_tracked_file_points_at_the_glossary()
