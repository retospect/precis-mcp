"""precis.mcp_supervisor: the port stays bound while server children come and go (gr459481)."""

from __future__ import annotations

import ast
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from precis import mcp_supervisor

SUPERVISOR = Path(mcp_supervisor.__file__)

# A stand-in server child: "boots" for BOOT_S, answers one connection with
# its generation marker, then exits with EXIT_CODE — the shape of a precis
# serve child that the checkout watchdog stops after a HEAD move.
CHILD = textwrap.dedent(
    """
    import os, socket, sys, time
    fd, boot_s, exit_code, marker = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    time.sleep(boot_s)
    sock = socket.socket(fileno=fd)
    conn, _ = sock.accept()
    conn.sendall(f"{marker} {os.getpid()}\\n".encode())
    conn.close()
    sock.detach()
    sys.exit(exit_code)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(
    tmp_path: Path, port: int, *child_args: str, prepare: str | None = None
) -> subprocess.Popen[str]:
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    # Run it the way the container does: the two files alone in a directory.
    # From src/precis directly, that directory heads sys.path and precis's own
    # modules (secrets.py, ...) shadow the standard library.
    run_dir = tmp_path / "supervisor"
    run_dir.mkdir(exist_ok=True)
    shutil.copy(SUPERVISOR, run_dir / "mcp_supervisor.py")
    shutil.copy(SUPERVISOR.with_name("mcp_liveness.py"), run_dir / "mcp_liveness.py")
    cmd = [
        sys.executable,
        str(run_dir / "mcp_supervisor.py"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if prepare:
        cmd += ["--prepare", prepare]
    cmd += ["--", sys.executable, str(child), "{fd}", *child_args]
    env = {
        k: v for k, v in os.environ.items() if k != "PRECIS_MCP_TOKEN"
    }  # liveness off
    return subprocess.Popen(
        cmd, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env
    )


def _connect_and_read(port: int, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
                return c.makefile(encoding="utf-8").readline().strip()
        except ConnectionRefusedError:
            # Only acceptable before the supervisor has bound the port at all.
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _wait_bound(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except ConnectionRefusedError:
            time.sleep(0.05)
    return False


def _stop(proc: subprocess.Popen[str]) -> str:
    proc.send_signal(signal.SIGTERM)
    try:
        _, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err


def test_connections_between_generations_queue_instead_of_being_refused(
    tmp_path: Path,
) -> None:
    """The gr459481 property: after the first bind, no connect is ever refused.

    Each child boots for 0.5 s, serves one connection and exits 0, so every
    connect after the first lands while the previous child is gone and the
    next one is still booting — the window that used to be dark.
    """
    port = _free_port()
    proc = _start(tmp_path, port, "0.5", "0", "gen")
    try:
        first = _connect_and_read(port)  # may retry until the bind happens
        pids = {first.split()[1]}
        for _ in range(3):
            # No retry loop here: a refusal now is the bug.
            with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
                line = c.makefile(encoding="utf-8").readline().strip()
            assert line.startswith("gen ")
            pids.add(line.split()[1])
        assert len(pids) == 4, "each connection should be served by a fresh generation"
    finally:
        err = _stop(proc)
    assert "generation 4 started" in err


def test_prepare_runs_before_every_generation(tmp_path: Path) -> None:
    port = _free_port()
    marks = tmp_path / "prepared"
    proc = _start(tmp_path, port, "0", "0", "gen", prepare=f"echo x >> {marks}")
    try:
        for _ in range(2):
            _connect_and_read(port)
        _connect_and_read(port)
    finally:
        _stop(proc)
    # three generations served, a fourth may have started; prepare ran once per start
    assert len(marks.read_text(encoding="utf-8").splitlines()) >= 3


def test_sigterm_is_forwarded_and_the_supervisor_exits(tmp_path: Path) -> None:
    port = _free_port()
    proc = _start(tmp_path, port, "60", "0", "gen")  # child sleeps, never accepts
    # Wait for the bind (the backlog accepts even though no child does): the
    # handlers are installed right after it, while the imports before it are slow.
    assert _wait_bound(port)
    time.sleep(0.5)
    start = time.monotonic()
    err = _stop(proc)
    assert time.monotonic() - start < 5
    assert "stopped by signal 15" in err
    assert proc.returncode == 128 + signal.SIGTERM


def test_a_crashing_child_is_restarted_after_a_backoff(tmp_path: Path) -> None:
    port = _free_port()
    proc = _start(tmp_path, port, "0", "3", "gen")
    try:
        _connect_and_read(port)
        _connect_and_read(port)  # served by the respawn after the 1 s backoff
    finally:
        err = _stop(proc)
    assert "crashed (exit 3)" in err
    assert "respawning in 1s" in err


@pytest.mark.parametrize(
    ("crashes", "delay"),
    [(0, 0.0), (1, 1.0), (2, 2.0), (3, 4.0), (5, 16.0), (6, 30.0), (40, 30.0)],
)
def test_crash_backoff_doubles_up_to_the_cap(crashes: int, delay: float) -> None:
    assert mcp_supervisor.crash_backoff_s(crashes) == delay


def test_child_argv_substitutes_the_fd() -> None:
    assert mcp_supervisor.child_argv(["precis", "serve", "--fd", "{fd}"], 3) == [
        "precis",
        "serve",
        "--fd",
        "3",
    ]


def test_the_child_command_must_carry_the_fd_placeholder() -> None:
    with pytest.raises(SystemExit):
        mcp_supervisor.main(["--", "precis", "serve"])


def test_a_wedge_verdict_sigkills_the_child_and_leaves_a_breadcrumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    detector = mcp_liveness_module().WedgeDetector()
    detector.failures = ["timeout", "timeout", "timeout"]
    sup = mcp_supervisor.Supervisor(socket.socket(), ["x"], None, detector)
    sup.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sup.on_wedge()
        assert sup.child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if sup.child.returncode is None:
            sup.child.kill()
    assert sup.wedge_killed
    crumb = tmp_path / "precis" / "server-state" / "last-exit.json"
    assert '"reason": "wedged"' in crumb.read_text(encoding="utf-8")


def test_a_wedge_verdict_on_an_already_exited_child_does_nothing() -> None:
    sup = mcp_supervisor.Supervisor(
        socket.socket(), ["x"], None, mcp_liveness_module().WedgeDetector()
    )
    sup.child = subprocess.Popen([sys.executable, "-c", "pass"])
    sup.child.wait()
    sup.on_wedge()
    assert not sup.wedge_killed


def mcp_liveness_module() -> Any:  # the module the supervisor bound
    return mcp_supervisor.mcp_liveness


def _top_level(name: str | None) -> str:
    return (name or "").split(".")[0]


def test_the_supervisor_imports_nothing_from_precis_outside_the_test_fallback() -> None:
    """It outlives /app re-copies: stdlib + the mcp_liveness sibling only, at module
    top; the package import is allowed solely as the ModuleNotFoundError fallback."""
    tree = ast.parse(SUPERVISOR.read_text(encoding="utf-8"))
    fallback = {
        id(n)
        for t in ast.walk(tree)
        if isinstance(t, ast.Try)
        for h in t.handlers
        for n in ast.walk(h)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
            )
            for name in names:
                top = _top_level(name)
                if top == "precis":
                    assert id(node) in fallback, (
                        "precis import outside the test fallback"
                    )
                    continue
                assert (
                    top in {"__future__", "mcp_liveness"}
                    or top in sys.stdlib_module_names
                ), name
    nested = [
        n
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef)
        for n in ast.walk(f)
        if isinstance(n, ast.Import | ast.ImportFrom)
    ]
    assert not nested, "no imports inside functions"
