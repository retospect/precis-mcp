"""pin-prod-follow moves the prod checkout forward only, silently when idle.

Throwaway git repos under ``tmp_path``: an "origin" with a ``prod`` branch and
a clone standing in for the prod checkout, pointed at via ``PROD_CHECKOUT``.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell script"
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "host-ops" / "pin-prod-follow"


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PRECIS"))}
    env.update(
        HOME=str(tmp_path / "home"),
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    ).stdout.strip()


def _commit(repo: Path, name: str, env: dict[str, str]) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name, env=env)
    _git(repo, "commit", "-q", "-m", name, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.env = _env(tmp_path)
        self.origin = tmp_path / "origin"
        self.checkout = tmp_path / "prod-checkout"
        self.origin.mkdir()
        _git(self.origin, "init", "-q", "-b", "prod", env=self.env)
        self.c1 = _commit(self.origin, "one", self.env)
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.checkout)],
            check=True,
            env=self.env,
        )
        _git(self.checkout, "checkout", "-q", "--detach", "origin/prod", env=self.env)
        self.env["PROD_CHECKOUT"] = str(self.checkout)

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD", env=self.env)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=60,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_noop_when_equal_is_silent(rig: Rig) -> None:
    r = rig.run()
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert rig.head() == rig.c1


def test_forward_move_pins_and_prints_one_line(rig: Rig) -> None:
    c2 = _commit(rig.origin, "two", rig.env)
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"pin-prod-follow: {rig.checkout} {rig.c1[:12]} -> {c2[:12]}\n"
    assert rig.head() == c2
    assert (
        _git(rig.checkout, "rev-parse", "--abbrev-ref", "HEAD", env=rig.env) == "HEAD"
    )
    assert rig.run().stdout == ""  # now a no-op


def test_dirty_tracked_file_refuses(rig: Rig) -> None:
    _commit(rig.origin, "two", rig.env)
    (rig.checkout / "one").write_text("edited", encoding="utf-8")
    r = rig.run()
    assert r.returncode == 3
    assert str(rig.checkout) in r.stderr and "1 dirty" in r.stderr
    assert rig.head() == rig.c1
    assert (rig.checkout / "one").read_text(encoding="utf-8") == "edited"


def test_untracked_file_does_not_block(rig: Rig) -> None:
    c2 = _commit(rig.origin, "two", rig.env)
    (rig.checkout / "stray").write_text("x", encoding="utf-8")
    assert rig.run().returncode == 0
    assert rig.head() == c2


def test_backward_move_refuses(rig: Rig) -> None:
    c2 = _commit(rig.origin, "two", rig.env)
    assert rig.run().returncode == 0
    _git(rig.origin, "reset", "-q", "--hard", rig.c1, env=rig.env)
    r = rig.run()
    assert r.returncode == 4
    assert c2[:12] in r.stderr and rig.c1[:12] in r.stderr
    assert rig.head() == c2


def test_sideways_move_refuses(rig: Rig) -> None:
    c2 = _commit(rig.origin, "two", rig.env)
    assert rig.run().returncode == 0
    _git(rig.origin, "reset", "-q", "--hard", rig.c1, env=rig.env)
    d = _commit(rig.origin, "other", rig.env)
    r = rig.run()
    assert r.returncode == 4
    assert c2[:12] in r.stderr and d[:12] in r.stderr
    assert rig.head() == c2


def test_dry_run_changes_nothing(rig: Rig) -> None:
    c2 = _commit(rig.origin, "two", rig.env)
    r = rig.run("--dry-run")
    assert r.returncode == 0
    assert f"{rig.c1[:12]} -> {c2[:12]}" in r.stdout
    assert rig.head() == rig.c1


def test_unreachable_origin_exits_5(rig: Rig, tmp_path: Path) -> None:
    _git(
        rig.checkout,
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "gone"),
        env=rig.env,
    )
    r = rig.run()
    assert r.returncode == 5
    assert len(r.stderr.strip().splitlines()) == 1
    # git's own reason survives on that one line (gone repo -> "gone")
    assert "fetch of origin prod failed" in r.stderr and "gone" in r.stderr
    assert rig.head() == rig.c1


def test_missing_checkout_exits_2(rig: Rig, tmp_path: Path) -> None:
    rig.env["PROD_CHECKOUT"] = str(tmp_path / "nope")
    assert rig.run().returncode == 2


def test_print_checkout_prefers_prod_checkout_then_mcp_src(
    rig: Rig, tmp_path: Path
) -> None:
    assert rig.run("--print-checkout").stdout == f"{rig.checkout}\n"
    del rig.env["PROD_CHECKOUT"]
    rig.env["PRECIS_MCP_SRC"] = str(rig.checkout)
    assert rig.run("--print-checkout").stdout == f"{rig.checkout}\n"


def test_installer_without_apply_only_prints(rig: Rig, tmp_path: Path) -> None:
    """The default run shows the scheduler commands and installs nothing."""
    installer = SCRIPT.with_name("pin-prod-follow-install")
    r = subprocess.run(
        [str(installer)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=rig.env,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "launchctl bootstrap" in r.stdout or "crontab line:" in r.stdout
    assert str(rig.checkout) in r.stdout or "pin-prod-follow" in r.stdout
    home = Path(rig.env["HOME"])
    assert not list(home.rglob("*.plist"))
    assert not (home / ".cache" / "pin-prod-follow.log").exists()
