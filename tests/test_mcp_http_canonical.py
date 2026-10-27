"""Canonical-checkout guard (gr515239) for the mcp-http ensure script and
``scripts/mcp-http-install --apply``.

2026-10-24: a run whose ``PRECIS_MCP_REPO`` — or the built-in default
(then a nonexistent plural-"projects" path) — resolved to a stale SECOND
copy of this repo recreated the live backend, which then mounted and
served that copy's tree. The ensure script now records the canonical
main checkout in ``$STATE_DIR/main-checkout`` on its first pass (and
uses it in that pass when only the default had resolved), refuses
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
import re
import subprocess
import sys
import textwrap
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


def test_invokers_checkout_is_recorded_and_adopted_over_the_default(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The incident shape: env unset, no marker, invoked from inside a
    precis-mcp tree. The invoker's MAIN checkout is recorded as canonical
    and that same run uses it — the built-in default (no .git under the
    rig's fake HOME) is never served. 2026-10-26: this run refused on the
    default and only a plain re-run passed."""
    want = os.path.realpath(_rig_checkout(tmp_path))
    r = _ensure(env, cwd=Path(want))
    # Past the guard: stops at the image check (the rig image is absent).
    assert r.returncode == 2, r.stderr
    assert f"recorded as {want}" in r.stderr
    marker = Path(env["PRECIS_MCP_HTTP_STATE"]) / "main-checkout"
    assert marker.read_text(encoding="utf-8").strip() == want


def test_bare_default_refuses_and_records_no_marker(
    env: dict[str, str], tmp_path: Path
) -> None:
    """Env unset, no marker, invoked outside any checkout: the built-in
    default refuses, and no marker naming that missing path is written —
    one would make every later run refuse until someone deleted it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    r = _ensure(env, cwd=outside)
    assert r.returncode == 3, r.stderr
    assert "built-in default" in r.stderr and "next=" in r.stderr
    assert not (Path(env["PRECIS_MCP_HTTP_STATE"]) / "main-checkout").exists()


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


# --- --check-path: the strays-cleanup guard (gr516960 ask 4) ------------------
#
# The probe is read-only and runs before the lock — no docker needed beyond
# a stub on PATH. Exit 0 "IN USE" when a marker or a container mount
# references the path (or sits under it), 1 "free", 2 on a missing argument.


def test_check_path_flags_the_recorded_checkout(
    env: dict[str, str], tmp_path: Path
) -> None:
    canon = tmp_path / "canon"
    (canon / ".git").mkdir(parents=True)
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    state.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    r = _ensure(env, "--check-path", str(canon))
    assert r.returncode == 0
    assert "IN USE" in r.stdout and "main-checkout" in r.stdout


def test_check_path_flags_a_container_mount_under_the_path(
    env: dict[str, str], tmp_path: Path
) -> None:
    """Deleting a parent dir of a mounted path breaks it the same way —
    the probe must match a mount source sitting UNDER the path."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    mounted = tmp_path / "root" / "checkout"
    mounted.mkdir(parents=True)
    (fakebin / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = ps ]; then echo c1; '
        'elif [ "$1" = inspect ] && [ "$2" = "-f" ] && [ "$3" = "{{.Name}}" ]; '
        "then echo /c1; else printf '%s\\n' " + str(mounted) + "; fi\n",
        encoding="utf-8",
    )
    (fakebin / "docker").chmod(0o755)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    r = _ensure(env, "--check-path", str(tmp_path / "root"))
    assert r.returncode == 0
    assert "IN USE" in r.stdout and "mounts" in r.stdout


def test_check_path_free_when_nothing_references_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    free = tmp_path / "elsewhere"
    free.mkdir()
    r = _ensure(env, "--check-path", str(free))
    assert r.returncode == 1
    assert "free" in r.stdout


def test_check_path_requires_an_argument(env: dict[str, str]) -> None:
    r = _ensure(env, "--check-path")
    assert r.returncode == 2


# --- the cli python-kind root (gr520147) --------------------------------------
#
# The image check exits (2) before the PY_ROOTS assembly runs, so these
# exercise the resolution block directly: lifted verbatim out of the script
# and run with stub log/note and a fake installed CLI on PATH.


def _cli_block() -> str:
    text = _ENSURE.read_text(encoding="utf-8")
    m = re.search(r'^case ",\$\{PY_ROOTS\}," in.*?^esac\n', text, re.S | re.M)
    assert m, "cli-root resolution block not found in ensure script"
    return m.group(0)


def _fake_cli(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal installed-CLI rig: console script + site-packages package.

    The console script's shebang names THIS python so the resolver's
    `import precis` runs under PYTHONPATH against the fake site-packages.
    """
    site = tmp_path / "site-packages"
    pkg = site / "precis"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    console = bindir / "precis"
    console.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    console.chmod(0o755)
    return bindir, pkg


def _run_cli_block(tmp_path: Path, env_extra: dict[str, str]) -> str:
    block = tmp_path / "block.sh"
    block.write_text(_cli_block(), encoding="utf-8")
    runner = tmp_path / "run.sh"
    runner.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            STATE_DIR="{tmp_path}/state"
            LOG="$STATE_DIR/ensure.log"
            log() {{ mkdir -p "$STATE_DIR"; printf '%s\\n' "$*" >> "$LOG"; }}
            note() {{ log "$*"; printf 'precis-mcp: %s\\n' "$*" >&2; }}
            PY_ROOTS="${{PY_ROOTS:-precis:/app,main:/main}}"
            EXTRA_MOUNTS=()
            PY_ROOT_CHECKS=()
            . "{block}"
            printf 'PY_ROOTS=%s\\n' "$PY_ROOTS"
            printf 'MOUNT=%s\\n' "${{EXTRA_MOUNTS[@]+"${{EXTRA_MOUNTS[@]}}"}}"
            """
        ),
        encoding="utf-8",
    )
    env = {"PATH": os.environ["PATH"], **env_extra}
    r = subprocess.run(
        ["bash", str(runner)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_cli_root_resolves_the_installed_package(tmp_path: Path) -> None:
    """Discovery asks the console script's shebang interpreter for the
    package dir, copies it into the ensure state dir — the discovered
    path can sit outside the docker VM's host share, where the bind
    mount reads EMPTY inside the container (gr520147) — and mounts the
    copy one level down so qualnames stay precis.*."""
    bindir, pkg = _fake_cli(tmp_path)
    out = _run_cli_block(
        tmp_path,
        {"PATH": f"{bindir}:{os.environ['PATH']}", "PYTHONPATH": str(pkg.parent)},
    )
    assert ",cli:/roots/cli" in out
    assert f"{tmp_path}/state/cli-python-root:/roots/cli:ro" in out
    assert (tmp_path / "state" / "cli-python-root" / "precis" / "__init__.py").is_file()


def _cli_log(tmp_path: Path) -> str:
    return (tmp_path / "state" / "ensure.log").read_text(encoding="utf-8")


def test_cli_root_auto_mode_logs_why_nothing_found(tmp_path: Path) -> None:
    """gr520147: auto discovery used to fall through with no log line at
    all — a missing `precis` console script must say so in ensure.log."""
    out = _run_cli_block(tmp_path, {"PATH": "/usr/bin:/bin"})
    assert ",cli:" not in out
    log = _cli_log(tmp_path)
    assert "no cli root" in log and "console script on PATH" in log


def test_cli_root_off_logs_disabled(tmp_path: Path) -> None:
    out = _run_cli_block(tmp_path, {"PRECIS_MCP_CLI_PYTHON": "off"})
    assert ",cli:" not in out
    assert "PRECIS_MCP_CLI_PYTHON=off" in _cli_log(tmp_path)


def test_cli_root_names_a_non_executable_interpreter(tmp_path: Path) -> None:
    """PRECIS_MCP_CLI_PYTHON=<missing python>: the note says why."""
    out = _run_cli_block(
        tmp_path, {"PRECIS_MCP_CLI_PYTHON": str(tmp_path / "no-python")}
    )
    assert ",cli:" not in out
    assert "not executable" in _cli_log(tmp_path)


def test_cli_root_copy_refreshes_when_the_source_changes(tmp_path: Path) -> None:
    """A `precis` upgrade in the venv must reach the mounted copy: the
    content signature moves and the next run re-copies."""
    bindir, pkg = _fake_cli(tmp_path)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHONPATH": str(pkg.parent),
    }
    _run_cli_block(tmp_path, env)
    state_pkg = tmp_path / "state" / "cli-python-root" / "precis"
    assert (state_pkg / "__init__.py").is_file()
    added = pkg / "newmod.py"
    added.write_text("X = 1\n", encoding="utf-8")
    _run_cli_block(tmp_path, env)
    assert (state_pkg / "newmod.py").read_text(encoding="utf-8") == "X = 1\n"
    added.unlink()
    _run_cli_block(tmp_path, env)
    assert not (state_pkg / "newmod.py").exists()


def test_cli_root_falls_back_to_direct_mount_when_copy_fails(
    tmp_path: Path,
) -> None:
    """The copy is the fix, not a new failure mode: when it cannot write
    the state dir the block still mounts the discovered path, loudly."""
    bindir, pkg = _fake_cli(tmp_path)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "cp").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fakebin / "cp").chmod(0o755)
    out = _run_cli_block(
        tmp_path,
        {
            "PATH": f"{fakebin}:{bindir}:{os.environ['PATH']}",
            "PYTHONPATH": str(pkg.parent),
        },
    )
    assert ",cli:/roots/cli" in out
    assert f"{pkg}:/roots/cli/precis:ro" in out
    assert "mounting the host path directly" in _cli_log(tmp_path)


def test_cli_root_off_disables_discovery(tmp_path: Path) -> None:
    bindir, _pkg = _fake_cli(tmp_path)
    out = _run_cli_block(
        tmp_path,
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "PRECIS_MCP_CLI_PYTHON": "off",
        },
    )
    assert ",cli:" not in out


def test_cli_root_override_names_the_interpreter(tmp_path: Path) -> None:
    """PRECIS_MCP_CLI_PYTHON=<python> resolves without `precis` on PATH."""
    _bindir, _pkg = _fake_cli(tmp_path)
    out = _run_cli_block(
        tmp_path,
        {
            "PRECIS_MCP_CLI_PYTHON": sys.executable,
            "PYTHONPATH": str(_pkg.parent),
        },
    )
    assert ",cli:/roots/cli" in out
    assert f"{tmp_path}/state/cli-python-root:/roots/cli:ro" in out


def test_cli_root_operator_entry_wins(tmp_path: Path) -> None:
    """A `cli` line in python-roots is never shadowed by auto-discovery."""
    bindir, pkg = _fake_cli(tmp_path)
    out = _run_cli_block(
        tmp_path,
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "PYTHONPATH": str(pkg.parent),
            "PY_ROOTS": "precis:/app,main:/main,cli:/roots/cli",
        },
    )
    assert out.count(",cli:") == 1
    assert "MOUNT=" not in out or f"{pkg}:/roots" not in out


def test_cli_root_falls_back_to_direct_mount_when_sig_fails(
    tmp_path: Path,
) -> None:
    """An empty signature — the hasher missing or the pipeline failing
    under pipefail — is the direct mount with a note, never an abort of
    the ensure."""
    bindir, pkg = _fake_cli(tmp_path)
    fakebin = tmp_path / "nosigbin"
    fakebin.mkdir()
    (fakebin / "xargs").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fakebin / "xargs").chmod(0o755)
    out = _run_cli_block(
        tmp_path,
        {
            "PATH": f"{fakebin}:{bindir}:{os.environ['PATH']}",
            "PYTHONPATH": str(pkg.parent),
        },
    )
    assert ",cli:/roots/cli" in out
    assert f"{pkg}:/roots/cli/precis:ro" in out
    log = _cli_log(tmp_path)
    assert "cannot sign" in log and "mounting the host path directly" in log


def test_cli_root_cleans_stale_staging_dirs(tmp_path: Path) -> None:
    """A run killed mid-copy leaves cli-python-root.new.*; the next run
    reaps it before staging its own."""
    bindir, pkg = _fake_cli(tmp_path)
    stale = tmp_path / "state" / "cli-python-root.new.99999"
    (stale / "precis").mkdir(parents=True)
    out = _run_cli_block(
        tmp_path,
        {"PATH": f"{bindir}:{os.environ['PATH']}", "PYTHONPATH": str(pkg.parent)},
    )
    assert ",cli:/roots/cli" in out
    assert not stale.exists()


# --- the signature reaching the container spec -------------------------------
#
# The refresh replaces cli-python-root's inode, so without the signature in
# the container spec a `precis` upgrade leaves the running backend bind-mounted
# on the deleted directory (EMPTY on virtiofs — gr520147 again). These run the
# cli block and the ENVS/RUN_ARGS/ENV_HASH section together, with a stub
# `docker` for the image id.


def _args_block() -> str:
    text = _ENSURE.read_text(encoding="utf-8")
    m = re.search(r"^ENVS=\(\n.*?^ENV_HASH=\"\$\(\n.*?\n\)\"\n", text, re.S | re.M)
    assert m, "ENVS..ENV_HASH section not found in ensure script"
    return m.group(0)


def _run_cli_and_args(tmp_path: Path, env_extra: dict[str, str]) -> str:
    block = tmp_path / "block.sh"
    block.write_text(_cli_block(), encoding="utf-8")
    args = tmp_path / "args.sh"
    args.write_text(_args_block(), encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker = fakebin / "docker"
    docker.write_text("#!/bin/sh\nprintf 'sha256:fakeimage\\n'\n", encoding="utf-8")
    docker.chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            STATE_DIR="{tmp_path}/state"
            LOG="$STATE_DIR/ensure.log"
            HOME="{tmp_path}/home"
            mkdir -p "$HOME"
            log() {{ mkdir -p "$STATE_DIR"; printf '%s\\n' "$*" >> "$LOG"; }}
            note() {{ log "$*"; printf 'precis-mcp: %s\\n' "$*" >&2; }}
            PY_ROOTS="${{PY_ROOTS:-precis:/app,main:/main}}"
            cli_sig=""
            EXTRA_MOUNTS=()
            PY_ROOT_CHECKS=()
            . "{block}"
            TOOL_CONCURRENCY=12 POOL_MIN=4 POOL_MAX=16 SCHEMA_GATE=warn
            WT_ENVS=() TOPO_ENVS=()
            MAIN_CHECKOUT="{tmp_path}/checkout" SRC_REPO="{tmp_path}/checkout"
            NET=test-net DB_HOST=db DB_IP=203.0.113.10 BACKEND_PORT=8765
            SECRETS_OUT="{tmp_path}/secrets" CACHE_DIR="{tmp_path}/cache"
            UV_CACHE_VOLUME=test-uv ENTRYPOINT=/entry
            TOKEN=test-token IMAGE=test-image RUN_CMD='true'
            . "{args}"
            printf 'ENV_HASH=%s\\n' "$ENV_HASH"
            printf 'ARGS=%s\\n' "${{RUN_ARGS[*]}}"
            """
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        **env_extra,
    }
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    r = subprocess.run(
        ["bash", str(runner)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _env_hash(out: str) -> str:
    m = re.search(r"^ENV_HASH=(\w+)$", out, re.M)
    assert m, out
    return m.group(1)


def test_cli_sig_label_recreates_the_backend_on_upgrade(tmp_path: Path) -> None:
    """precis.cli_sig rides in RUN_ARGS, so a changed package signature is
    a changed ENV_HASH — a `precis` upgrade recreates the backend instead
    of leaving it mounted on the deleted copy."""
    bindir, pkg = _fake_cli(tmp_path)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHONPATH": str(pkg.parent),
    }
    out = _run_cli_and_args(tmp_path, env)
    first = _env_hash(out)
    assert re.search(r"precis\.cli_sig=[0-9a-f]{8,}", out)
    (pkg / "newmod.py").write_text("X = 1\n", encoding="utf-8")
    out = _run_cli_and_args(tmp_path, env)
    assert _env_hash(out) != first
    # An unchanged package keeps the hash: no recreate per session start.
    assert _env_hash(_run_cli_and_args(tmp_path, env)) != first


def test_cli_sig_follows_symlinked_files(tmp_path: Path) -> None:
    """find -L signs the same tree cp -RL copies: a symlink's target
    content. Re-pointing a symlink inside the package must move the
    signature (find -type f alone never sees it)."""
    bindir, pkg = _fake_cli(tmp_path)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHONPATH": str(pkg.parent),
    }
    outside = tmp_path / "outside"
    outside.mkdir()
    a = outside / "a.py"
    a.write_text("A = 1\n", encoding="utf-8")
    b = outside / "b.py"
    b.write_text("B = 2\n", encoding="utf-8")
    link = pkg / "linked.py"
    link.symlink_to(a)
    first = _env_hash(_run_cli_and_args(tmp_path, env))
    link.unlink()
    link.symlink_to(b)
    assert _env_hash(_run_cli_and_args(tmp_path, env)) != first


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
