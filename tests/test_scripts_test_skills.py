"""scripts/test --skills — the skill-only selector (gr514383).

A skill-only change had no quick selector: the agent picked from 22
``tests/test_skill*.py`` files by hand. ``--skills`` expands to every
``tests/test_skill*.py`` host-side, so the missing-path preflight, the
auto ``-n0`` count and the DB bring-up see the same file list a
hand-typed selection would — and trailing pytest args still pass
through.

Runs the REAL script staged into a throwaway repo against the synthetic
``docker`` on PATH — the same rig as tests/test_scripts_test_env.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_scripts_test_env import _run, _run_line

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

SKILL_FILES = (
    "tests/test_skill_a.py",
    "tests/test_skill_b.py",
    "tests/test_skills_examples.py",  # the plural form matches too
)


def test_skills_expands_to_every_skill_file(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path,
        args=("--skills",),
        files=(*SKILL_FILES, "tests/test_other.py"),
    )
    assert rc == 0, stderr
    line = f" {_run_line(log)} "
    for f in SKILL_FILES:
        assert f" {f} " in line
    assert " tests/test_other.py " not in line
    assert " tests/x.py " not in line


def test_skills_is_a_file_selection_for_auto_n(tmp_path: Path) -> None:
    """All positional args are files — the container counts the collected
    items and picks -n0 at ≤500, same as a hand-typed list."""
    rc, log, stderr = _run(tmp_path, args=("--skills",), files=SKILL_FILES)
    assert rc == 0, stderr
    line = f" {_run_line(log)} "
    assert " -e PRECIS_AUTO_N=1 " in line
    assert " -n6 " not in line


def test_skills_combines_with_paths_and_pytest_args(tmp_path: Path) -> None:
    """--skills is a union, not a mode: an explicit file and a -k
    expression ride along as ordinary pytest args."""
    rc, log, stderr = _run(
        tmp_path,
        args=("--skills", "-k", "anchor", "tests/test_other.py"),
        files=(*SKILL_FILES, "tests/test_other.py"),
    )
    assert rc == 0, stderr
    line = f" {_run_line(log)} "
    for f in (*SKILL_FILES, "tests/test_other.py"):
        assert f" {f} " in line
    assert " -k anchor " in line


def test_skills_with_no_matching_files_is_a_named_error(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, args=("--skills",))
    assert rc == 2
    assert "--skills matched no tests/test_skill*.py files" in stderr
    assert not log  # refused before any docker call
