"""The `gated` and `prod` refs (scripts/lib/env-pointers.sh).

Two branches on origin that nobody commits to: scripts/ship moves `gated` to
the sha a full gate passed, scripts/deploy moves `prod` to the sha a deploy
converged on. They publish what `.ship-sha` and the deploy-state marker only
ever recorded on one machine.

The contract pinned here, against the real lib and a throwaway origin:

  - a move creates the ref, and fast-forwards it;
  - a move backward is refused and leaves the ref where it was, unless the
    caller says `force` (scripts/deploy --force-rollback);
  - it is best-effort: no origin, an unknown sha, or PRECIS_ENV_POINTERS=0
    return non-zero without killing a `set -e` caller's deploy;
  - both writers call it at their success point, and only there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib" / "env-pointers.sh"
SHIP = REPO_ROOT / "scripts" / "ship"
DEPLOY = REPO_ROOT / "scripts" / "deploy"


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    env.pop("PRECIS_ENV_POINTERS", None)
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A work repo with a bare origin and three commits on main, oldest first."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    shas: list[str] = []
    for n in range(3):
        (work / "f").write_text(f"{n}\n", encoding="utf-8")
        _git(work, "add", "f")
        _git(work, "commit", "-q", "-m", f"c{n}")
        shas.append(_git(work, "rev-parse", "HEAD"))
    _git(work, "push", "-q", "origin", "main")
    return work, origin, shas


def _move(
    work: Path, branch: str, sha: str, mode: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail; . "{LIB}"; env_pointer_move "{work}" "{branch}" "{sha}" {mode}'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(**env),
        check=False,
    )


def _ref(origin: Path, branch: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
        cwd=origin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
        check=False,
    )
    return out.stdout.strip()


def test_first_move_creates_the_ref(repo: tuple[Path, Path, list[str]]) -> None:
    work, origin, shas = repo
    assert _ref(origin, "prod") == ""
    assert _move(work, "prod", shas[0]).returncode == 0
    assert _ref(origin, "prod") == shas[0]


def test_forward_move_fast_forwards(repo: tuple[Path, Path, list[str]]) -> None:
    work, origin, shas = repo
    _move(work, "gated", shas[0])
    assert _move(work, "gated", shas[2]).returncode == 0
    assert _ref(origin, "gated") == shas[2]


def test_backward_move_is_refused_and_leaves_the_ref(
    repo: tuple[Path, Path, list[str]],
) -> None:
    work, origin, shas = repo
    _move(work, "prod", shas[2])
    res = _move(work, "prod", shas[0])
    assert res.returncode == 1
    assert "prod pointer not moved" in res.stderr
    assert _ref(origin, "prod") == shas[2]


def test_force_moves_backward_for_a_deliberate_rollback(
    repo: tuple[Path, Path, list[str]],
) -> None:
    work, origin, shas = repo
    _move(work, "prod", shas[2])
    assert _move(work, "prod", shas[0], "force").returncode == 0
    assert _ref(origin, "prod") == shas[0]


def test_switch_off_moves_nothing(repo: tuple[Path, Path, list[str]]) -> None:
    work, origin, shas = repo
    res = _move(work, "prod", shas[1], PRECIS_ENV_POINTERS="0")
    assert res.returncode == 1
    assert _ref(origin, "prod") == ""


def test_unknown_sha_and_short_sha_move_nothing(
    repo: tuple[Path, Path, list[str]],
) -> None:
    work, origin, shas = repo
    assert _move(work, "prod", "0" * 40).returncode == 1
    assert _move(work, "prod", shas[1][:8]).returncode == 1
    assert _move(work, "prod", "main").returncode == 1
    assert _ref(origin, "prod") == ""


def test_no_origin_is_a_quiet_no(tmp_path: Path) -> None:
    work = tmp_path / "solo"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "f").write_text("0\n", encoding="utf-8")
    _git(work, "add", "f")
    _git(work, "commit", "-q", "-m", "c0")
    res = _move(work, "prod", _git(work, "rev-parse", "HEAD"))
    assert res.returncode == 1
    assert res.stderr == ""


def test_a_refused_move_does_not_kill_a_set_e_caller(
    repo: tuple[Path, Path, list[str]],
) -> None:
    """The writers call it as `env_pointer_move … || true`; pin that the
    idiom survives `set -euo pipefail`, which both scripts run under."""
    work, _origin, shas = repo
    _move(work, "prod", shas[2])
    script = (
        f'set -euo pipefail; . "{LIB}"; '
        f'env_pointer_move "{work}" prod "{shas[0]}" || true; echo survived'
    )
    res = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "survived"


def test_ship_moves_gated_only_where_it_writes_the_pin() -> None:
    """`gated` means "the full gate passed this sha" — the same condition
    that writes `.ship-sha`. A quick or docs-only run must not move it."""
    text = SHIP.read_text(encoding="utf-8")
    assert text.count("env_pointer_move") == 1
    pin_write = text.index('printf \'%s\\n\' "$GATED_SHA" > "$SHIP_SHA_FILE"')
    docs_only_branch = text.index("docs-only lane — no deploy pin written")
    call = text.index("env_pointer_move")
    assert pin_write < call < docs_only_branch
    assert 'env_pointer_move "$WORKTREE" gated "$GATED_SHA"' in text


def test_deploy_moves_prod_from_both_success_writers() -> None:
    """scripts/deploy records success in two places (the rollout-converged
    graft and step 3); `prod` follows the marker from each, through one
    helper, and goes backward only under --force-rollback."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.count("_move_prod_pointer()") == 1
    assert text.count('_move_prod_pointer "$sha"') == 1
    assert text.count('_move_prod_pointer "$DEPLOYED_SHA"') == 1
    helper = text[text.index("_move_prod_pointer()") :]
    helper = helper[: helper.index("\n}\n")]
    assert "env_pointer_move" in helper
    assert "FORCE_ROLLBACK" in helper
    assert "force" in helper
