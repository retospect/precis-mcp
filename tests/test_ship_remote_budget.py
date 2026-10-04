"""scripts/ship --remote sets its race budget by risk (2026-10-04).

At ~33 pushes/h one land had cost three green CI runs. A tree that adds no
migration and leaves safe_fetch.py alone now gets one CI run, then a lost race
lands forward-merged; a tree that adds a migration or touches safe_fetch.py
gets one CI retry. An explicit PRECIS_REMOTE_CI_RETRIES keeps the old budget.

The function is cut out of the real script and run in a throwaway repo whose
`origin/main` is a plain ref, so no network or remote is involved.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

SHIP = Path(__file__).resolve().parent.parent / "scripts" / "ship"


def _function() -> str:
    text = SHIP.read_text(encoding="utf-8")
    m = re.search(r"^_set_remote_budget\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "_set_remote_budget not found in scripts/ship"
    return m.group(0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n", encoding="utf-8")
    sf = repo / "src" / "precis" / "utils" / "safe_fetch.py"
    sf.parent.mkdir(parents=True)
    sf.write_text("A = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    for rel, body in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _budget(repo: Path, **env: str) -> tuple[str, str]:
    script = (
        'set -euo pipefail\nsay() { echo "SAY $*"; }\nMAX_ATTEMPTS=4\n'
        "REMOTE_RETRY_BUDGET=${PRECIS_REMOTE_CI_RETRIES:-2}\n"
        + _function()
        + '_set_remote_budget\necho "MAX=$MAX_ATTEMPTS RETRIES=$REMOTE_RETRY_BUDGET"\n'
    )
    full_env = {k: v for k, v in os.environ.items() if k != "PRECIS_REMOTE_CI_RETRIES"}
    full_env.update(env)
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        env=full_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    last = proc.stdout.strip().splitlines()[-1]
    return last, proc.stdout


def test_ordinary_tree_gets_one_ci_run(tmp_path: Path) -> None:
    last, out = _budget(_repo(tmp_path, {"src/precis/x.py": "X = 1\n"}))
    assert last == "MAX=1 RETRIES=0"
    assert "1 CI run" in out


def test_added_migration_gets_one_retry(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/precis_se/migrations/0019_x.sql": "SELECT 1;\n"})
    last, out = _budget(repo)
    assert last == "MAX=2 RETRIES=1"
    assert "adds a migration" in out


def test_safe_fetch_change_gets_one_retry(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/precis/utils/safe_fetch.py": "A = 2\n"})
    assert _budget(repo)[0] == "MAX=2 RETRIES=1"


def test_explicit_env_keeps_the_old_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/precis/x.py": "X = 1\n"})
    last, out = _budget(repo, PRECIS_REMOTE_CI_RETRIES="2")
    assert last == "MAX=4 RETRIES=2"
    assert "SAY" not in out
