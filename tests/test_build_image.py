"""scripts/build-image safety — gr544137.

Two builds that ran out of disk at the final unpack left ``precis-dev:latest``
on a half-unpacked image and the last good one untagged, breaking every fresh
gate container. ``scripts/build-image`` now refuses below a free-disk floor,
builds to a temp tag, smoke-runs ``import autocatpath`` in it and retags
``:latest`` only on success. These tests run the shipped bash against a fake
``docker`` on PATH that records its calls, so no real image is built,
tagged or removed.

The second half pins the preflight cache key in ``scripts/test``: it must
change when ``uv.lock`` does, so lock-pin drift is checked again instead of
hiding behind a stamp.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash scripts + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent

_FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "${1:-}" in
  ps) exit 0 ;;
  image) exit 0 ;;
  tag|rmi) exit 0 ;;
  compose)
    case " $* " in
      *" config "*)
        echo '{"services": {"precis-dev": {"image": "precis-dev"}}}'
        exit 0
        ;;
      *" build "*)
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ]; then
            case "$a" in
              *override.yaml) sed 's/^/override: /' "$a" >> "$FAKE_DOCKER_LOG" ;;
            esac
          fi
          prev="$a"
        done
        exit "${FAKE_BUILD_RC:-0}"
        ;;
    esac
    ;;
  run)
    prev=""
    for a in "$@"; do
      if [ "$prev" = "--entrypoint" ]; then
        case "$a" in
          df)
            printf 'Filesystem 1K-blocks Used Available Use%% Mounted\n'
            printf '/dev/vda 999999999 1 %s 1%% /\n' "${FAKE_FREE_KB:-134217728}"
            exit 0
            ;;
          python) exit "${FAKE_SMOKE_RC:-0}" ;;
        esac
      fi
      prev="$a"
    done
    exit 0
    ;;
esac
exit 0
"""


def _shim(fakebin: Path, name: str, body: str) -> None:
    path = fakebin / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _stage(tmp_path: Path) -> tuple[Path, Path, Path]:
    tree = tmp_path / "tree"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "build-image", tree / "scripts" / "build-image")
    for lib in (
        "autocatpath-wheel.sh",
        "docker-disk.sh",
        "git-network.sh",
        "git_network.py",
    ):
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    shutil.copy2(REPO / "uv.lock", tree / "uv.lock")
    compose = tree / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(
            ["git", *cmd], cwd=tree, env=git_env, check=True, capture_output=True
        )
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    if shutil.which("shasum") is None:
        _shim(fakebin, "shasum", 'exec sha256sum "$@"')
    if shutil.which("hostname") is None:
        _shim(fakebin, "hostname", "echo testhost")
    return tree, compose, fakebin


def _run_build(
    tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    tree, compose, fakebin = _stage(tmp_path)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        FAKE_DOCKER_LOG=str(log),
        PRECIS_COMPOSE=str(compose),
        GH_TOKEN="not-a-token",
    )
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "build-image"), "precis-dev"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return proc, log.read_text(encoding="utf-8").splitlines()


def _calls(log: list[str], verb: str) -> list[str]:
    return [line for line in log if line.startswith(f"docker {verb} ")]


def test_passing_smoke_retags_latest_from_the_temp_tag(tmp_path: Path) -> None:
    proc, log = _run_build(tmp_path)
    assert proc.returncode == 0, proc.stderr
    builds = _calls(log, "compose")
    build = next(line for line in builds if " build " in line)
    assert "override.yaml" in build
    override = [line for line in log if line.startswith("override: ")]
    temp = next(
        line.split('image: "')[1].rstrip('"') for line in override if "image:" in line
    )
    assert temp.startswith("precis-dev:build-")
    smoke = [line for line in _calls(log, "run") if "--entrypoint python" in line]
    assert len(smoke) == 1
    assert temp in smoke[0]
    assert smoke[0].endswith("-c import autocatpath")
    assert f"docker tag {temp} precis-dev" in log
    assert log.index(f"docker tag {temp} precis-dev") > log.index(smoke[0])


def test_failed_smoke_leaves_latest_alone_and_exits_nonzero(tmp_path: Path) -> None:
    proc, log = _run_build(tmp_path, {"FAKE_SMOKE_RC": "1"})
    assert proc.returncode != 0
    assert _calls(log, "tag") == []
    assert _calls(log, "rmi") == []
    assert "left untouched" in proc.stderr
    assert "import autocatpath" in proc.stderr


def test_failed_build_never_smokes_or_retags(tmp_path: Path) -> None:
    proc, log = _run_build(tmp_path, {"FAKE_BUILD_RC": "7"})
    assert proc.returncode == 7
    assert _calls(log, "tag") == []
    assert not [line for line in _calls(log, "run") if "--entrypoint python" in line]


def test_disk_floor_refuses_before_any_build(tmp_path: Path) -> None:
    free_30_gb = str(30 * 1024 * 1024)
    proc, log = _run_build(tmp_path, {"FAKE_FREE_KB": free_30_gb})
    assert proc.returncode == 3
    assert "30 GB free" in proc.stderr
    assert "PRECIS_BUILD_MIN_FREE_GB" in proc.stderr
    assert not [line for line in log if " build " in line]
    assert _calls(log, "tag") == []


def test_disk_floor_env_override_lets_a_small_disk_build(tmp_path: Path) -> None:
    free_30_gb = str(30 * 1024 * 1024)
    proc, log = _run_build(
        tmp_path, {"FAKE_FREE_KB": free_30_gb, "PRECIS_BUILD_MIN_FREE_GB": "20"}
    )
    assert proc.returncode == 0, proc.stderr
    assert any(line.startswith("docker tag ") for line in log)


def _preflight_key(tmp_path: Path, lock_text: str) -> str:
    """Run the real key computation lifted out of ``scripts/test``."""
    src = (REPO / "scripts" / "test").read_text(encoding="utf-8")
    start = src.index("_sha256() {")
    end = src.index("# Artifact-writing tests")
    block = src[start:end]
    work = tmp_path / "keywork"
    work.mkdir(parents=True)
    (work / "uv.lock").write_text(lock_text, encoding="utf-8")
    (work / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    fakebin = tmp_path / "keybin"
    fakebin.mkdir(parents=True)
    _shim(fakebin, "docker", "echo sha256:fixedimageid")
    if shutil.which("shasum") is None:
        _shim(fakebin, "shasum", 'exec sha256sum "$@"')
    env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    env.pop("UV_WITH", None)
    proc = subprocess.run(
        ["bash", "-c", f'{block}\nprintf "%s" "$PRECIS_PREFLIGHT_KEY"'],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_preflight_key_changes_with_uv_lock(tmp_path: Path) -> None:
    """gr544137: a lock-pin bump (e.g. autocatpath 0.43 -> 0.44) must change
    the key, so a stamp from before the bump cannot hide the drift check."""
    a = _preflight_key(tmp_path / "a", 'name = "autocatpath"\nversion = "0.43.0"\n')
    a_again = _preflight_key(
        tmp_path / "a2", 'name = "autocatpath"\nversion = "0.43.0"\n'
    )
    b = _preflight_key(tmp_path / "b", 'name = "autocatpath"\nversion = "0.44.0"\n')
    assert len(a) == 64
    assert a == a_again
    assert a != b
