"""Script git transport stderr may not be discarded without a reviewed reason."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHELL_WORD = r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s;{}]+)"""
NETWORK_GIT = re.compile(
    rf"\bgit\s+(?:(?:-[Cc]\s*{SHELL_WORD})\s+)*"
    r"(?:push|fetch|ls-remote)(?=\s|$|[;&])"
)
DISCARD = re.compile(
    r"(?:2\s*>\s*/dev/null|2\s*>&\s*-|&>\s*/dev/null|>\s*/dev/null\s+2>&1)"
)
BRACE_GROUP = re.compile(r"\{([^{}]*)\}([^\n;]*)", re.DOTALL)
ALLOW = re.compile(r"#\s*git-error-silent: allow\s*—\s*\S.+")


def _silent_network_commands(source: str) -> list[str]:
    """Join shell continuations so a redirect on the next line cannot hide."""
    source = re.sub(r"\\\n", " ", source)
    lines = source.splitlines()
    bad: list[str] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if NETWORK_GIT.search(line) and DISCARD.search(line):
            if not ALLOW.search(line) and not (i and ALLOW.search(lines[i - 1])):
                bad.append(line.strip())
    for group in BRACE_GROUP.finditer(source):
        if not DISCARD.search(group[2]):
            continue
        start_line = source.count("\n", 0, group.start())
        if ALLOW.search(lines[start_line]) or (
            start_line and ALLOW.search(lines[start_line - 1])
        ):
            continue
        for line in group[1].splitlines():
            if not line.lstrip().startswith("#") and NETWORK_GIT.search(line):
                command = group[0].strip()
                if command not in bad:
                    bad.append(command)
                break
    return bad


def test_git_network_errors_are_not_silenced() -> None:
    bad: list[str] = []
    for path in sorted(SCRIPTS.rglob("*")):
        if not path.is_file() or path.suffix in {".pyc", ".md"}:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        bad.extend(
            f"{path.relative_to(SCRIPTS.parent)}: {command}"
            for command in _silent_network_commands(source)
        )
    assert not bad, (
        "Git network failures must retain stderr; reviewed exceptions need "
        "'# git-error-silent: allow — <reason>':\n" + "\n".join(bad)
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD 2>/dev/null",
        'git -C "$root" fetch origin >/dev/null 2>&1',
        "git ls-remote origin &>/dev/null",
        "git -C '/a path' -c core.askPass=false push origin HEAD 2>&-",
        "git -c core.askPass=false -C /repo fetch origin 2>&-",
        "{ git fetch origin; } 2>/dev/null",
        "{\n    git fetch origin\n} 2>/dev/null",
        'git fetch origin \\\n    "+refs/heads/*:refs/remotes/origin/*" 2> /dev/null',
    ],
)
def test_visibility_gate_catches_redirection_forms(command: str) -> None:
    assert _silent_network_commands(command)
    assert not _silent_network_commands(
        "# git-error-silent: allow — explicit reviewed fixture\n" + command
    )


def test_visibility_gate_allows_stdout_only_and_requires_reason() -> None:
    assert not _silent_network_commands("git fetch origin >/dev/null")
    assert _silent_network_commands(
        "# git-error-silent: allow —\ngit push origin HEAD 2>/dev/null"
    )


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add /tmp/fetch branch 2>/dev/null",
        "git cat-file -e HEAD:fetch 2>/dev/null",
        'git -C "$root" worktree add /tmp/push branch 2>/dev/null',
        "{\n    git cat-file -e HEAD:fetch\n} 2>/dev/null",
    ],
)
def test_visibility_gate_ignores_network_words_in_arguments(command: str) -> None:
    assert not _silent_network_commands(command)


def test_visibility_gate_handles_non_utf8_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "binary").write_bytes(b"\xff\xfe\n")
    monkeypatch.setitem(globals(), "SCRIPTS", tmp_path)
    test_git_network_errors_are_not_silenced()
