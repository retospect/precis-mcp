"""Retirement test 4a — the graph serves ``MEMORY.md``'s index role.

docs/backlog/memory-native-authoring.md test 4a / AC 3: seed the synthetic
``tests/fixtures/file_mirror/`` tree through the importer, run the real
``scripts/hooks/session-start-memory.sh`` against the test DB, and the output
must carry the fixture's ``## Section`` headers and ``- [Title](slug.md) —
hook`` bullets, exact strings, in order. A green test is the condition for
deleting the ``MEMORY.md`` index by hand; the hook is deliberately not wired
into ``.claude/settings.json`` yet.

``PRECIS_MEMORY_CLI`` is the seam: it points the hook at this interpreter's
CLI entry point instead of ``uv run precis`` (no uv inside the gate container).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from precis.cli.memory import import_memory_dir
from precis.store import Store
from tests.conftest import _active_dsn

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the session-start hook is a POSIX bash script"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "hooks" / "session-start-memory.sh"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "file_mirror"


def _run_hook(
    tmp_path: Path, dsn: str, *, claude_md_bytes: int = 100
) -> subprocess.CompletedProcess[str]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "CLAUDE.md").write_text("x" * claude_md_bytes, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "PRECIS_DATABASE_URL": dsn,
            "PRECIS_MEMORY_CLI": f"{sys.executable} -m precis.cli.main",
            "CLAUDE_PROJECT_DIR": str(project),
        }
    )
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _fixture_index_lines() -> list[str]:
    text = (FIXTURE / "MEMORY.md").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith(("## ", "- ["))]


def test_hook_output_equals_the_fixture_index(store: Store, tmp_path: Path) -> None:
    import_memory_dir(store, FIXTURE)
    proc = _run_hook(tmp_path, _active_dsn())
    assert proc.returncode == 0, proc.stderr
    got = [ln for ln in proc.stdout.splitlines() if ln.startswith(("## ", "- "))]
    assert got == _fixture_index_lines()
    # the hook adds nothing but the index itself
    assert proc.stdout.startswith("# Memory index\n")
    assert "over budget" not in proc.stdout


def test_hook_over_budget_prints_cut_hooks_and_the_overage_line(
    store: Store, tmp_path: Path
) -> None:
    import_memory_dir(store, FIXTURE)
    # CLAUDE.md of 32000 bytes = the whole 8000-tok preamble budget -> budget 0.
    proc = _run_hook(tmp_path, _active_dsn(), claude_md_bytes=32000)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    assert lines[-1].startswith("(memory index over budget:")
    assert "budget 0 tok" in lines[-1]
    alpha = next(ln for ln in lines if "[Alpha campaign]" in ln)
    assert alpha.endswith("…") and len(alpha.split(" — ", 1)[1]) == 60


def test_hook_dead_dsn_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    proc = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1
    assert out[0].startswith("memory index unavailable:")
    assert "postgresql://" not in out[0]


def test_hook_unrunnable_cli_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    env_proc = subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PRECIS_DATABASE_URL": "postgresql://nobody@localhost:1/none",
            "PRECIS_MEMORY_CLI": "/nonexistent/precis-cli",
            "CLAUDE_PROJECT_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert env_proc.returncode == 0
    out = env_proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1 and out[0].startswith("memory index unavailable:")
