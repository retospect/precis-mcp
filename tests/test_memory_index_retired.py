"""Retirement test 4a — the graph serves ``MEMORY.md``'s index role.

docs/backlog/memory-native-authoring.md test 4a / AC 3: seed the synthetic
``tests/fixtures/file_mirror/`` tree through the importer, run the real
``scripts/hooks/session-start-memory.sh`` against the test DB, and the output
must carry the fixture's ``## Section`` headers and, for each fixture
``- [Title](slug.md) — hook`` bullet, ``- Title (me<id>) — hook`` (the graph
node is the truth now, addressed by handle), in order. A green test is the condition for
deleting the ``MEMORY.md`` index by hand.

``PRECIS_MEMORY_CLI`` is the seam: it points the hook at this interpreter's
CLI entry point instead of ``uv run precis`` (no uv inside the gate container).
``PRECIS_MEMORY_CACHE`` keeps the hook's last-good copy under ``tmp_path``, and
``PRECIS_HARNESS_MEMORY_MD`` points the hook's cutover gate at a fixture
``MEMORY.md`` (absent by default, which counts as cut over).
"""

from __future__ import annotations

import os
import re
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


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "memory-index.md"


def _run_hook(
    tmp_path: Path,
    dsn: str,
    *,
    claude_md_bytes: int = 100,
    memory_md: Path | None = None,
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
            "PRECIS_MEMORY_CACHE": str(_cache(tmp_path)),
            "PRECIS_HARNESS_MEMORY_MD": str(
                memory_md or tmp_path / "absent" / "MEMORY.md"
            ),
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


def _assert_matches_fixture(got: list[str]) -> None:
    """``got`` = the header + bullet lines; each fixture bullet renders as
    ``- Title (me<digits>) — hook`` under the same headers, in order."""
    want = _fixture_index_lines()
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        if w.startswith("## "):
            assert g == w
            continue
        m = re.fullmatch(r"- \[(?P<title>[^\]]+)\]\([^)]+\)(?: — (?P<hook>.*))?", w)
        assert m is not None, w
        pat = rf"- {re.escape(m['title'])} \(me\d+\)"
        if m["hook"]:
            pat += rf" — {re.escape(m['hook'])}"
        assert re.fullmatch(pat, g), (g, pat)


def test_hook_output_equals_the_fixture_index(store: Store, tmp_path: Path) -> None:
    import_memory_dir(store, FIXTURE)
    proc = _run_hook(tmp_path, _active_dsn())
    assert proc.returncode == 0, proc.stderr
    got = [ln for ln in proc.stdout.splitlines() if ln.startswith(("## ", "- "))]
    _assert_matches_fixture(got)
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
    alpha = next(ln for ln in lines if ln.startswith("- Alpha campaign (me"))
    assert alpha.endswith("…") and len(alpha.split(" — ", 1)[1]) == 60


def test_hook_dead_dsn_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    proc = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1
    assert out[0].startswith("memory index unavailable:")
    assert "postgresql://" not in out[0]


def test_hook_dead_dsn_prints_the_last_good_copy(store: Store, tmp_path: Path) -> None:
    import_memory_dir(store, FIXTURE)
    good = _run_hook(tmp_path, _active_dsn())
    assert good.returncode == 0, good.stderr
    assert _cache(tmp_path).read_text(encoding="utf-8") == good.stdout
    assert not list(_cache(tmp_path).parent.glob("*.tmp.*"))

    proc = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    assert proc.stdout.startswith(good.stdout)
    _assert_matches_fixture([ln for ln in lines if ln.startswith(("## ", "- "))])
    assert lines[-1].startswith("(memory index: precis memory index exited")
    assert "showing the cached copy from " in lines[-1]
    assert "postgresql://" not in proc.stdout


def test_hook_unrunnable_cli_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    env_proc = subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PRECIS_DATABASE_URL": "postgresql://nobody@localhost:1/none",
            "PRECIS_MEMORY_CLI": "/nonexistent/precis-cli",
            "CLAUDE_PROJECT_DIR": str(tmp_path),
            "PRECIS_MEMORY_CACHE": str(_cache(tmp_path)),
            "PRECIS_HARNESS_MEMORY_MD": str(tmp_path / "absent" / "MEMORY.md"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert env_proc.returncode == 0
    out = env_proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1 and out[0].startswith("memory index unavailable:")


def test_hook_is_silent_while_memory_md_is_still_the_index(tmp_path: Path) -> None:
    """Before the cutover MEMORY.md (no graph marker) is the index; printing
    the graph render too would double it in every session."""
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(
        "# Memory index\n\n## Threads\n\n- [A](a.md) — x\n", encoding="utf-8"
    )
    proc = _run_hook(
        tmp_path, "postgresql://nobody@localhost:1/none", memory_md=memory_md
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not _cache(tmp_path).exists()


def test_hook_prints_once_memory_md_carries_the_graph_marker(
    store: Store, tmp_path: Path
) -> None:
    import_memory_dir(store, FIXTURE)
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(
        "<!-- memory-index: graph -->\n# Memory index\n", encoding="utf-8"
    )
    proc = _run_hook(tmp_path, _active_dsn(), memory_md=memory_md)
    assert proc.returncode == 0, proc.stderr
    _assert_matches_fixture(
        [ln for ln in proc.stdout.splitlines() if ln.startswith(("## ", "- "))]
    )
