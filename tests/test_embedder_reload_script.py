"""``scripts/embedder-reload`` restarts the embedder only when its code moved.

gr458940: the dev-machine embedder runs editable from the primary checkout,
which ``scripts/ship`` fast-forwards on every merge, and it ran a 30-day-old
build because nothing restarted it. The script fixes that, and these tests
pin the three properties that make it safe to fire on every HEAD move:

- a merge that touches no embedder path does not restart (a qland burst must
  not bounce every session's embeddings);
- a merge that does touches restarts, and records the new sha only once
  ``/healthz`` answers;
- a restart that fails closed (the 2026-10-01 codesigning kill) retries with
  a plain kickstart and, still failing, leaves the old sha so the next HEAD
  move tries again.

``launchctl`` and ``curl`` are PATH stubs: the first logs its argv, the
second answers healthy or not per an env flag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "embedder-reload"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, path: str, body: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"touch {path}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "README.md", "x")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "launchctl.calls"
    (bin_dir / "launchctl").write_text(
        f'#!/bin/sh\necho "$*" >> {calls}\n', encoding="utf-8"
    )
    (bin_dir / "curl").write_text(
        '#!/bin/sh\n[ "$FAKE_HEALTHY" = 1 ]\n', encoding="utf-8"
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "EMBEDDER_RELOAD_REPO": str(repo),
        "EMBEDDER_RELOAD_STATE": str(tmp_path / "state"),
        "EMBEDDER_RELOAD_WAIT_S": "1",
        "FAKE_HEALTHY": "1",
        "CALLS": str(calls),
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _state(env: dict[str, str]) -> str:
    return Path(env["EMBEDDER_RELOAD_STATE"]).read_text(encoding="utf-8").strip()


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["CALLS"])
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _repo(env: dict[str, str]) -> Path:
    return Path(env["EMBEDDER_RELOAD_REPO"])


def test_first_run_baselines_without_restarting(env: dict[str, str]) -> None:
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert _state(env) == _git(_repo(env), "rev-parse", "HEAD")
    assert _calls(env) == []


def test_unrelated_merge_moves_baseline_without_restart(
    env: dict[str, str],
) -> None:
    _run(env)
    head = _commit(_repo(env), "src/precis/cad/x.py", "y")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "touches no embedder path" in result.stdout
    assert _calls(env) == []
    assert _state(env) == head


def test_embedder_change_restarts_and_records_on_health(
    env: dict[str, str],
) -> None:
    _run(env)
    head = _commit(_repo(env), "src/precis/embedder_service.py", "new")
    assert _run(env, "check").stdout.startswith("restart ")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert [c.split()[0] for c in _calls(env)] == ["kickstart"]
    assert "-k" in _calls(env)[0]
    assert _state(env) == head


def test_lock_change_restarts(env: dict[str, str]) -> None:
    _run(env)
    _commit(_repo(env), "uv.lock", "dep bump")
    assert _run(env, "check").stdout.startswith("restart ")


def test_fail_closed_restart_retries_then_keeps_old_sha(
    env: dict[str, str],
) -> None:
    _run(env)
    before = _state(env)
    _commit(_repo(env), "src/precis/embedder.py", "new")
    result = _run({**env, "FAKE_HEALTHY": "0"})
    assert result.returncode == 1
    assert "FAILED" in result.stdout
    # -k first, then the plain kickstart that recovered the codesigning kill.
    assert len(_calls(env)) == 2
    assert "-k" in _calls(env)[0]
    assert "-k" not in _calls(env)[1]
    # Not advanced, so the next HEAD move retries.
    assert _state(env) == before
    assert _run(env, "check").stdout.startswith("restart ")


def test_unknown_started_on_sha_restarts(env: dict[str, str]) -> None:
    Path(env["EMBEDDER_RELOAD_STATE"]).write_text("0" * 40, encoding="utf-8")
    assert _run(env, "check").stdout.startswith("restart ")
