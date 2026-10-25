"""Canonical-checkout guard (gr515239) for the mcp-http ensure script and
``scripts/mcp-http-install --apply``.

2026-10-24: a run whose ``PRECIS_MCP_REPO`` — or the built-in default
(then a nonexistent plural-"projects" path) — resolved to a stale SECOND
copy of this repo recreated the live backend, which then mounted and
served that copy's tree. The ensure script now records the canonical
main checkout in ``$STATE_DIR/main-checkout`` on its first pass, refuses
(exit 3) when a later run resolves a different physical path OR a path
that is not a checkout at all (gr515239 follow-up: a moved marker target
crash-looped a standby backend on 2026-10-25), and never silently falls
back to the default; ``mcp-http-install --apply`` refuses to install
from a tree that is not the recorded one.

No docker needed here: the guard runs under the lock before the first
docker call, so a refusal is exit 3 and a passing run next stops at the
image check (exit 2 for a guaranteed-absent rig image). The docker side —
the ``precis.invoker`` label and the ``PRECIS_MCP_{SRC,MAIN}_HOST`` env
vars reaching the container spec — is covered by the proxy rig
(tests/test_mcp_http_proxy_rig.py, ``PRECIS_MCP_PROXY_RIG=1``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ENSURE = _ROOT / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"
_INSTALL = _ROOT / "scripts" / "mcp-http-install"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the bash scripts"
)


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _main_checkout() -> Path | None:
    """This repo's main checkout via --git-common-dir, like the scripts do."""
    common = _git("-C", str(_ROOT), "rev-parse", "--git-common-dir")
    if not common:
        return None
    p = Path(common)
    if not p.is_absolute():
        p = _ROOT / p
    return p.parent.resolve()


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    """A rig-shaped env: own NAME/STATE/HOME/TMPDIR and an absent image tag."""
    (tmp_path / "home" / "work" / "corpus").mkdir(parents=True)
    (tmp_path / "tmp").mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PRECIS_MCP_HTTP_NAME": f"precis-mcp-guard-{uuid.uuid4().hex[:8]}",
        "PRECIS_MCP_HTTP_STATE": str(tmp_path / "state"),
        # A tag nothing pulls: a run that PASSES the guard stops at the
        # image check with exit 2 and never creates a container.
        "PRECIS_MCP_IMAGE": "precis-mcp-guard-absent:tag",
        "PRECIS_MCP_DB_HOST": "dbnode",
        "PRECIS_MCP_DB_IP": "203.0.113.10",
    }


def _ensure(
    env: dict[str, str], *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_ENSURE), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_INSTALL), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


# --- the ensure script --------------------------------------------------------


def test_refuses_when_repo_differs_from_the_recorded_checkout(
    env: dict[str, str], tmp_path: Path
) -> None:
    canon = tmp_path / "canon"
    (canon / ".git").mkdir(parents=True)
    other = tmp_path / "other"
    # A stale second copy IS a real checkout — the not-a-checkout refusal
    # runs first, so the non-canonical path needs .git to reach it.
    (other / ".git").mkdir(parents=True)
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    env["PRECIS_MCP_REPO"] = str(other)
    r = _ensure(env)
    assert r.returncode == 3
    assert "non-canonical" in r.stderr and "next=" in r.stderr
    assert str(canon) in r.stderr and str(other) in r.stderr


def test_first_pass_records_the_env_checkout_then_enforces_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)  # a marker target must have .git
    env["PRECIS_MCP_REPO"] = str(repo)
    r = _ensure(env)
    # Past the guard: stops at the image check (the rig image is absent).
    assert r.returncode == 2, r.stderr
    marker = Path(env["PRECIS_MCP_HTTP_STATE"]) / "main-checkout"
    assert marker.read_text(encoding="utf-8").strip() == os.path.realpath(repo)
    # A later run resolving the same checkout still passes …
    assert _ensure(env).returncode == 2
    # … and one resolving elsewhere refuses.
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)  # a stale copy is a real checkout
    env["PRECIS_MCP_REPO"] = str(other)
    r = _ensure(env)
    assert r.returncode == 3 and "non-canonical" in r.stderr


def test_recorded_marker_is_the_repo_when_env_is_unset(
    env: dict[str, str], tmp_path: Path
) -> None:
    canon = tmp_path / "canon"
    (canon / ".git").mkdir(parents=True)
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    r = _ensure(env)
    assert r.returncode == 2, r.stderr
    assert "non-canonical" not in r.stderr


def _rig_checkout(tmp_path: Path) -> Path:
    """A throwaway precis-mcp-named clone the invoker probe can resolve:
    invoker_main_checkout needs a real git dir (for --git-common-dir) and
    an origin remote naming precis-mcp — both work container and host,
    unlike _main_checkout() whose host path the gate container can't cd."""
    repo = tmp_path / "invoker"
    _git("init", "-b", "main", str(repo))
    _git(
        "-C",
        str(repo),
        "remote",
        "add",
        "origin",
        "https://example.invalid/precis-mcp.git",
    )
    return repo


def test_invokers_checkout_is_recorded_and_the_bare_default_refused(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The incident shape: env unset, no marker, invoked from inside a
    precis-mcp tree. The invoker's MAIN checkout is recorded as canonical
    and the unresolved built-in default refuses — it has no .git dir
    under the rig's fake HOME — instead of silently serving it."""
    want = os.path.realpath(_rig_checkout(tmp_path))
    r = _ensure(env, cwd=Path(want))
    assert r.returncode == 3, r.stderr
    assert want in r.stderr and "built-in default" in r.stderr
    marker = Path(env["PRECIS_MCP_HTTP_STATE"]) / "main-checkout"
    assert marker.read_text(encoding="utf-8").strip() == want


def test_marker_naming_a_missing_path_refuses_then_recovers(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The crash-loop shape (2026-10-25): the marker names a checkout that
    moved or was deleted. The run must refuse with a next= instead of
    falling back to the default; with the invoker's checkout recoverable
    the marker is re-recorded, so the NEXT run passes the guard."""
    want = os.path.realpath(_rig_checkout(tmp_path))
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    gone = tmp_path / "moved-away"
    (state / "main-checkout").write_text(str(gone) + "\n", encoding="utf-8")
    r = _ensure(env, cwd=Path(want))
    assert r.returncode == 3, r.stderr
    assert "not a git checkout" in r.stderr and "next=" in r.stderr
    assert str(gone) in r.stderr
    # The marker was re-recorded to the invoker's main checkout …
    assert (state / "main-checkout").read_text(encoding="utf-8").strip() == want
    # … so the next run resolves a real checkout and reaches the image check.
    assert _ensure(env, cwd=Path(want)).returncode == 2


# --- scripts/mcp-http-install --apply ------------------------------------------


def test_mcp_http_install_apply_refuses_from_a_noncanonical_tree(
    env: dict[str, str], tmp_path: Path
) -> None:
    if _main_checkout() is None:
        pytest.skip("not a git checkout")
    canon = tmp_path / "canon"
    canon.mkdir()
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    inst = tmp_path / "inst-ensure.sh"
    inst.write_text("old\n", encoding="utf-8")
    env["PRECIS_MCP_ENSURE"] = str(inst)
    r = _install(env, "--apply")
    assert r.returncode == 3
    assert "refusing --apply" in r.stderr and str(canon) in r.stderr
    assert inst.read_text(encoding="utf-8") == "old\n"  # untouched


def test_mcp_http_install_apply_runs_from_the_canonical_checkout(
    env: dict[str, str], tmp_path: Path
) -> None:
    main = _main_checkout()
    if main is None:
        pytest.skip("not a git checkout")
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(main) + "\n", encoding="utf-8")
    inst = tmp_path / "inst-ensure.sh"
    inst.write_text("old\n", encoding="utf-8")
    env["PRECIS_MCP_ENSURE"] = str(inst)
    r = _install(env, "--apply")
    assert r.returncode == 0, r.stderr
    assert inst.read_text(encoding="utf-8") == _ENSURE.read_text(encoding="utf-8")
    assert list(tmp_path.glob("inst-ensure.sh.bak-*"))  # backup kept


def test_mcp_http_install_check_mode_never_refuses(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The check/--for-hook path runs on every SessionStart from every
    worktree: it reports DIFFERS but must not gate on the marker."""
    canon = tmp_path / "canon"
    canon.mkdir()
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    env["PRECIS_MCP_ENSURE"] = str(tmp_path / "inst-ensure.sh")  # absent
    r = _install(env)
    assert r.returncode == 0
    assert "refusing" not in r.stderr and "DIFFERS" in r.stdout
