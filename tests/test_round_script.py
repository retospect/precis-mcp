"""The peer-round tally (scripts/round).

A round's bookkeeping moved from cross-session messages into a file-backed
table under the common git dir. The contract pinned here, against the real
script and a throwaway repo with linked worktrees:

  - `open` numbers rounds upward and refuses while one is open;
  - a peer is its worktree directory, and marks itself `in <sha>` (repeatable),
    `none`, or `eta <text>`; a mark with no round open is refused;
  - `in` refuses a sha that is not a commit, so a typo never reads as a land;
  - a mark from an earlier round reads as no mark;
  - `pending` lists worktrees without a final mark — `eta` is not final;
  - the primary checkout is not a peer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs the shebang'd scripts/round against git worktrees",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "round"


def _env() -> dict[str, str]:
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
    env.pop("PRECIS_ROUND_PEER", None)
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


def _round(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """A primary checkout with two linked worktrees, `alpha` and `beta`."""
    primary = tmp_path / "primary"
    _git(tmp_path, "init", "-q", "-b", "main", str(primary))
    (primary / "f").write_text("0\n", encoding="utf-8")
    _git(primary, "add", "f")
    _git(primary, "commit", "-q", "-m", "c0")
    sha = _git(primary, "rev-parse", "HEAD")
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _git(primary, "worktree", "add", "-q", "-b", "alpha", str(alpha))
    _git(primary, "worktree", "add", "-q", "-b", "beta", str(beta))
    return primary, alpha, beta, sha


def _peers(cwd: Path) -> dict[str, dict[str, object]]:
    out = _round(cwd, "status", "--json")
    assert out.returncode == 0, out.stderr
    return {row["peer"]: row for row in json.loads(out.stdout)["peers"]}


def test_mark_without_an_open_round_is_refused(
    trees: tuple[Path, Path, Path, str],
) -> None:
    _primary, alpha, _beta, sha = trees
    out = _round(alpha, "in", sha)
    assert out.returncode != 0
    assert "no round is open" in out.stderr


def test_open_numbers_upward_and_refuses_a_second_open(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, _alpha, _beta, sha = trees
    assert "round 1 open" in _round(primary, "open", "--base", sha).stdout
    again = _round(primary, "open")
    assert again.returncode != 0
    assert "still open" in again.stderr
    assert _round(primary, "close").returncode == 0
    assert "round 2 open" in _round(primary, "open").stdout


def test_peers_mark_from_their_own_tree_and_the_table_shows_it(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, alpha, beta, sha = trees
    _round(primary, "open")
    assert _round(alpha, "in", sha[:10], "placer", "fix").returncode == 0
    assert _round(beta, "none").returncode == 0
    peers = _peers(primary)
    assert set(peers) == {"alpha", "beta"}  # the primary checkout is not a peer
    assert peers["alpha"]["status"] == "in"
    assert peers["alpha"]["shas"] == [sha]  # a short sha is stored in full
    assert peers["alpha"]["note"] == "placer fix"
    assert peers["beta"]["status"] == "none"
    assert _round(primary, "pending").stdout == ""


def test_in_is_repeatable_and_does_not_duplicate_a_sha(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, alpha, _beta, sha = trees
    _round(primary, "open")
    (alpha / "f").write_text("1\n", encoding="utf-8")
    _git(alpha, "commit", "-q", "-am", "c1")
    second = _git(alpha, "rev-parse", "HEAD")
    for value in (sha, second, sha):
        assert _round(alpha, "in", value).returncode == 0
    assert _peers(primary)["alpha"]["shas"] == [sha, second]


def test_in_refuses_a_sha_that_is_not_a_commit(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, alpha, _beta, _sha = trees
    _round(primary, "open")
    for bad in ("deadbeefdead", "not-a-sha"):
        out = _round(alpha, "in", bad)
        assert out.returncode != 0
    assert _peers(primary)["alpha"]["status"] == ""


def test_eta_needs_text_and_keeps_the_peer_pending(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, alpha, beta, _sha = trees
    _round(primary, "open")
    assert _round(alpha, "eta").returncode != 0
    assert _round(alpha, "eta", "two", "tests", "left").returncode == 0
    _round(beta, "none")
    assert _round(primary, "pending").stdout.split() == ["alpha"]
    assert _peers(primary)["alpha"]["note"] == "two tests left"


def test_a_mark_from_an_earlier_round_reads_as_no_mark(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, alpha, _beta, sha = trees
    _round(primary, "open")
    _round(alpha, "in", sha)
    _round(primary, "close")
    _round(primary, "open")
    assert _peers(primary)["alpha"]["status"] == ""
    assert _round(primary, "pending").stdout.split() == ["alpha", "beta"]


def test_peer_name_override(trees: tuple[Path, Path, Path, str]) -> None:
    primary, alpha, _beta, _sha = trees
    _round(primary, "open")
    env = _env()
    env["PRECIS_ROUND_PEER"] = "EWOD"
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "none"],
        cwd=alpha,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert _peers(primary)["EWOD"]["status"] == "none"


def test_status_with_no_round_is_quiet_success(
    trees: tuple[Path, Path, Path, str],
) -> None:
    primary, _alpha, _beta, _sha = trees
    out = _round(primary, "status")
    assert out.returncode == 0
    assert "no round open" in out.stdout
