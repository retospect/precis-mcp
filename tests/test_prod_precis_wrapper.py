"""The production CLI wrapper forwards configuration using synthetic files only."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only bash wrapper and executable shell fixture",
)


@pytest.mark.parametrize(
    "provided,file_url,expected",
    [
        (None, "https://fixture.invalid/v1", "https://fixture.invalid/v1"),
        ("", "https://fixture.invalid/v1", ""),
        (
            "https://override.invalid/v1",
            "https://fixture.invalid/v1",
            "https://override.invalid/v1",
        ),
        (None, None, "unset"),
    ],
)
def test_cloud_url_child_environment(
    tmp_path: Path, provided: str | None, file_url: str | None, expected: str
) -> None:
    secret_dir = tmp_path / "synthetic-home" / ".secrets" / "pw"
    secret_dir.mkdir(parents=True)
    (secret_dir / "PRECIS_DATABASE_URL").write_text(
        "postgresql://fixture@host.docker.internal:1/fixture\n", encoding="utf-8"
    )
    if file_url is not None:
        (secret_dir / "PRECIS_LLM_BASE_URL").write_text(
            file_url + "\n", encoding="utf-8"
        )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "url=${PRECIS_LLM_BASE_URL-unset}" "dsn=${PRECIS_DATABASE_URL-unset}" "args=$*"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(secret_dir.parents[1]),
        "PATH": str(fake_bin) + ":" + os.environ["PATH"],
        # This test is about env forwarding, not the stale-tree check (own
        # tests below): skip the git fetch against the real origin.
        "PRECIS_ALLOW_STALE_PROD_TREE": "1",
    }
    env.pop("PRECIS_LLM_BASE_URL", None)
    if provided is not None:
        env["PRECIS_LLM_BASE_URL"] = provided
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "prod-precis"
    result = subprocess.run(
        ["bash", str(wrapper), "tools", "get", "--kind", "gripe", "--id", "fixture"],
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert f"url={expected}\n" in result.stdout
    assert "dsn=postgresql://fixture@127.0.0.1:1/fixture\n" in result.stdout
    assert "args=run precis tools get --kind gripe --id fixture" in result.stdout
    assert result.stderr == ""
    assert expected not in result.args


_WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "prod-precis"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )


def _stale_tree_fixture(tmp_path: Path, *, stale: bool) -> tuple[Path, dict[str, str]]:
    """A clone whose origin/prod is, or is not, an ancestor of its HEAD.

    When ``stale``, origin's ``prod`` gains a commit the clone lacks. The
    wrapper is copied into the clone so its ``dirname "$0"/..`` lands there.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "prod")
    (origin / "f").write_text("1", encoding="utf-8")
    _git(origin, "add", "f")
    _git(origin, "commit", "-q", "-m", "base")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "-b", "work")
    if stale:
        _git(origin, "commit", "-q", "--allow-empty", "-m", "prod moves on")
    (clone / "scripts").mkdir()
    shutil.copy(_WRAPPER, clone / "scripts" / "prod-precis")
    secret_dir = tmp_path / "home" / ".secrets" / "pw"
    secret_dir.mkdir(parents=True)
    (secret_dir / "PRECIS_DATABASE_URL").write_text(
        "postgresql://fixture@host.docker.internal:1/fixture\n", encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/bin/sh\necho "ran $*"\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": str(fake_bin) + ":" + os.environ["PATH"],
    }
    env.pop("PRECIS_ALLOW_STALE_PROD_TREE", None)
    return clone, env


def _run(
    clone: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(clone / "scripts" / "prod-precis"), *args],
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )


def test_fresh_tree_runs_writes_silently(tmp_path: Path) -> None:
    clone, env = _stale_tree_fixture(tmp_path, stale=False)
    r = _run(clone, env, "quest", "redispatch", "1")
    assert r.returncode == 0, r.stderr
    assert "ran run precis quest redispatch 1" in r.stdout
    assert r.stderr == ""


def test_stale_tree_refuses_write_with_next_step(tmp_path: Path) -> None:
    clone, env = _stale_tree_fixture(tmp_path, stale=True)
    r = _run(clone, env, "quest", "redispatch", "1")
    assert r.returncode == 3
    assert "ran" not in r.stdout
    assert "REFUSED" in r.stderr and "not a descendant of origin/prod" in r.stderr
    assert "git rebase origin/prod" in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("tools", "get", "--kind", "gripe", "--id", "1"),
        ("tools", "search", "--kind", "gripe", "--q", "x"),
        ("quest", "status", "1"),
        ("quest", "redispatch", "1", "--dry-run"),
    ],
)
def test_stale_tree_warns_but_runs_reads(tmp_path: Path, args: tuple[str, ...]) -> None:
    clone, env = _stale_tree_fixture(tmp_path, stale=True)
    r = _run(clone, env, *args)
    assert r.returncode == 0, r.stderr
    assert "ran run precis" in r.stdout
    assert "WARNING" in r.stderr and "not a descendant of origin/prod" in r.stderr


def test_stale_tree_override_runs_write(tmp_path: Path) -> None:
    clone, env = _stale_tree_fixture(tmp_path, stale=True)
    r = _run(
        clone, {**env, "PRECIS_ALLOW_STALE_PROD_TREE": "1"}, "quest", "redispatch", "1"
    )
    assert r.returncode == 0, r.stderr
    assert "ran run precis quest redispatch 1" in r.stdout
