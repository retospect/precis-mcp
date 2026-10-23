"""``scripts/claude-local`` castor-lane tree pinning (gr506125).

The gpt-oss re-pilot aimed every Edit at the main checkout's absolute path.
The launcher now exports ``PRECIS_CASTOR_LANE`` (the worktree-path hook's
strict mode) and pins the task tree in the system prompt: ``-w NAME``
resolves to ``<main>/.claude/worktrees/NAME``; without ``-w`` the session's
own toplevel is used. Tested end-to-end with stub ``claude``/``curl`` on a
throwaway git repo — no castor contact.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only launcher + stub shells"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude-local"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _lane_run(
    tmp_path: Path, args: list[str], cwd: Path
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # claude records its argv one-per-line; curl always healthy (both the
    # castor probe and the shim health check pass, so nothing is spawned).
    _stub(bin_dir, "claude", '#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    _stub(bin_dir, "curl", "#!/usr/bin/env bash\nexit 0\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NODE_HOST_CASTOR": "127.0.0.1",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


@pytest.mark.skipif(
    subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode != 0,
    reason="claude-local does not parse",
)
class TestTreePinning:
    def test_w_flag_pins_the_named_worktree(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        proc = _lane_run(tmp_path, ["-w", "mytree", "-p", "do the task"], repo)
        assert proc.returncode == 0, proc.stderr
        argv = proc.stdout.splitlines()
        i = argv.index("--append-system-prompt")
        pin = argv[i + 1]
        assert f"{repo}/.claude/worktrees/mytree" in pin
        assert "castor lane" in pin
        # the caller's own argv survives untouched after the injected flag
        assert argv[-4:] == ["-w", "mytree", "-p", "do the task"]

    def test_no_w_pins_the_current_toplevel(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        proc = _lane_run(tmp_path, ["-p", "hello"], repo)
        assert proc.returncode == 0, proc.stderr
        argv = proc.stdout.splitlines()
        pin = argv[argv.index("--append-system-prompt") + 1]
        assert str(repo) in pin

    def test_caller_system_prompt_is_not_doubled(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        proc = _lane_run(
            tmp_path,
            ["-w", "t2", "--append-system-prompt", "caller text", "-p", "x"],
            repo,
        )
        assert proc.returncode == 0, proc.stderr
        argv = proc.stdout.splitlines()
        assert argv.count("--append-system-prompt") == 1
        assert "caller text" in argv

    def test_castor_lane_env_marker_exported(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub(
            bin_dir,
            "claude",
            '#!/usr/bin/env bash\necho "lane=$PRECIS_CASTOR_LANE"\n',
        )
        _stub(bin_dir, "curl", "#!/usr/bin/env bash\nexit 0\n")
        repo = _repo(tmp_path)
        proc = subprocess.run(
            ["bash", str(SCRIPT), "-p", "hi"],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "NODE_HOST_CASTOR": "127.0.0.1",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "lane=1" in proc.stdout
