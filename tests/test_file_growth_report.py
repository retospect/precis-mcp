"""Measurement and report-only file growth policy."""

import json
import subprocess
import warnings
from pathlib import Path

import pytest

from tests._policy_scan import gitignore_matcher
from tests._script import load_script

ROOT = Path(__file__).resolve().parents[1]
report = load_script("file_growth_report", ROOT / "scripts/file-growth-report")


def test_size_and_outline(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/example.py").write_bytes(
        b"@decorate\nclass Example:\n    async def method(self):\n        pass"
    )
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "bytes").write_bytes(b"hello\0")
    rows = report.size_rows(
        tmp_path, ["tests/example.py", "empty", "bytes"], outlines=True
    )
    assert rows == {
        "tests/example.py": {
            "type": "test.py",
            "lines": 4,
            "largest_class": {"name": "Example", "lines": 4},
            "largest_function": {"name": "method", "lines": 2},
        },
        "empty": {"type": "(no extension)", "lines": 0},
        "bytes": {"size_unavailable": "binary"},
    }
    assert report.file_type(Path("script"), b"#!/usr/bin/env python\n") == ".py"
    assert report.file_type(Path("script"), b"#!/bin/sh\n") == ".sh"
    assert report.file_type(Path("page.HTML.j2"), b"") == ".html.j2"


def test_churn_excludes_split_and_earlier_paths(tmp_path: Path) -> None:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=tmp_path,
            timeout=10,
        )

    def commit(message: str) -> None:
        git("add", ".")
        git("commit", "--quiet", "-m", message)

    git("init", "--quiet")
    original = tmp_path / "original\tfile"
    sibling = tmp_path / "new file"
    other = tmp_path / "other"
    original.write_bytes(b"one\ntwo\n")
    other.write_bytes(b"before\n")
    commit("initial change")
    original.write_bytes(b"one\n")
    sibling.write_bytes(b"two\n")
    other.write_bytes(b"moved in same commit\n")
    commit("move code\n\nFile-Split: original\tfile\nFile-Split: new file")
    original.write_bytes(b"one\nthree\n")
    sibling.write_bytes(b"four\n")
    commit("change after split")
    commits, lines = report.churn(tmp_path)
    assert commits == {"original\tfile": 1, "new file": 1, "other": 1}
    assert lines == {"original\tfile": 1, "new file": 2, "other": 1}


def test_churn_binary_first_record(tmp_path: Path) -> None:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=tmp_path,
            timeout=10,
        )

    git("init", "--quiet")
    (tmp_path / "a-bin").write_bytes(b"\x00\x01")
    (tmp_path / "b-text").write_bytes(b"one\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "binary first")
    commits, lines = report.churn(tmp_path)
    assert commits == {"a-bin": 1, "b-text": 1}
    assert lines == {"b-text": 1}


def test_churn_skips_separator_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        b"\0commit\t\0\n-\t-\ta-bin\0"
        b"2\t1\tb-text\0"
        b"\0commit\t\0\n\0commit\t\0\n1\t0\tc-text"
    )
    monkeypatch.setattr(report, "git", lambda *args: payload)
    commits, lines = report.churn(tmp_path)
    assert commits == {"a-bin": 1, "b-text": 1, "c-text": 1}
    assert lines == {"b-text": 3, "c-text": 1}


def test_ratchet_reports_growth_and_shrinkage() -> None:
    assert report.ratchet(
        {"new.py": 11, "grown.py": 15, "smaller.py": 12, "same.py": 12},
        {"grown.py": 12, "smaller.py": 13, "gone.py": 11, "same.py": 12},
    ) == {
        "new_or_grown": {"new.py": 11, "grown.py": 15},
        "shrank_or_cleared": {"smaller.py": 12, "gone.py": 0},
    }


def test_file_growth_ratchet_report() -> None:
    if (ROOT / ".git").exists():
        paths = report.tracked_paths(ROOT)
    else:
        # The shipping gate can run from git archive.
        ignored = gitignore_matcher(ROOT)
        paths = [
            name
            for path in ROOT.rglob("*")
            if path.is_file() and not ignored(name := path.relative_to(ROOT).as_posix())
        ]
    assert paths, "file growth scan found no repository files"
    problems = report.ratchet_report(ROOT, paths)
    if any(problems.values()):
        warnings.warn(
            pytest.PytestWarning(
                f"File growth ratchet (report only): {json.dumps(problems, sort_keys=True)}"
            ),
            stacklevel=1,
        )
