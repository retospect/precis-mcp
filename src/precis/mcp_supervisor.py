"""PID-1 supervisor for the shared session MCP server: hold the port, respawn the server.

The shared ``precis-mcp-http`` container serves every Claude Code session
on the machine. Its checkout watchdog (``install_watchdog``) makes the
server exit when the source checkout moves, so the next server runs the
new code. Without this module the exit takes the whole container down,
and the port is dark for ~13 s while Docker restarts it and the server
boots. An idle interactive client holds a long-lived ``GET /mcp`` event
stream; when that stream dies it retries, but only for a budget measured
between 16 s and 24 s (gr459481), so one slow restart or two close
together strand every idle session until a human reconnects it.

A wedge detector (:mod:`precis.mcp_liveness`) probes the port from a
thread; on its verdict the supervisor SIGKILLs the child and starts a new
one at once — a wedge kill skips the crash backoff, since the detector's
own kill cooldown already paces kills. It runs only when
``PRECIS_MCP_TOKEN`` is set (the probe speaks real MCP and needs it).

This supervisor binds the port **once** and keeps it bound for the life
of the container. Each server generation is a child process that serves
on the inherited listening socket (``precis serve --fd``). Between
generations the socket stays open: new connections wait in the kernel's
listen backlog instead of being refused, and the next child accepts them.
A client whose session id belonged to the previous generation gets a 404
from the new one and re-initializes — the path the client already
handles.

Before each generation the optional ``--prepare`` command runs (the
container uses it to re-copy ``/src`` into ``/app``, so the child imports
the code the watchdog saw move).

Exit-code policy: a child exiting 0 is a deliberate restart (the
watchdog) and is replaced at once; a non-zero exit is a crash, replaced
after an exponential backoff that resets once a child has stayed up for
a while. SIGTERM/SIGINT are forwarded to the child, and the supervisor
exits with the child's status — ``docker stop`` keeps working.

**Standard library plus the sibling ``mcp_liveness`` (which itself imports
only the venv's ``mcp``), no ``precis.*`` imports, all imports at the top.**
The supervisor runs for the life of the container while ``--prepare``
wipes and re-copies the tree it may have been loaded from; a lazy import
of anything under ``/app`` could load the wrong generation's code or
fail mid-copy. The container copies this file to a stable path and runs
it from there. A change to this file therefore takes a container
recreate, not a watchdog restart.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis import mcp_liveness
else:
    try:
        # In the container this file and mcp_liveness.py are copied side by
        # side to a stable directory, so the sibling import resolves there.
        mcp_liveness = importlib.import_module("mcp_liveness")
    except ModuleNotFoundError:
        # Only the in-repo tests should get here. As PID 1 in the container a
        # fallback to the package would re-import from /app — the tree every
        # child start wipes — so refuse instead of degrading quietly.
        if os.getpid() == 1:
            raise
        from precis import mcp_liveness

#: Kernel listen backlog. Connections that arrive while no child is
#: accepting queue here; every Claude Code session reconnecting at once
#: after a restart is a few dozen at most.
LISTEN_BACKLOG = 1024

#: A child that stayed up at least this long resets the crash backoff.
STABLE_AFTER_S = 60.0

#: Backoff cap between crash restarts.
MAX_BACKOFF_S = 30.0

#: Placeholder in the child command replaced by the listening fd number.
FD_PLACEHOLDER = "{fd}"


def _log(msg: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"{stamp} precis-supervisor: {msg}", file=sys.stderr, flush=True)


def crash_backoff_s(consecutive_crashes: int) -> float:
    """Delay before restarting after the n-th consecutive crash (n >= 1)."""
    if consecutive_crashes <= 0:
        return 0.0
    return min(MAX_BACKOFF_S, float(2 ** (consecutive_crashes - 1)))


def bind_listener(host: str, port: int) -> socket.socket:
    """Bind and listen once; the socket is inheritable so children can serve on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(LISTEN_BACKLOG)
    sock.set_inheritable(True)
    return sock


def child_argv(command: list[str], fd: int) -> list[str]:
    """Substitute the listening fd into the child command."""
    return [part.replace(FD_PLACEHOLDER, str(fd)) for part in command]


class Supervisor:
    def __init__(
        self,
        sock: socket.socket,
        command: list[str],
        prepare: str | None,
        detector: mcp_liveness.WedgeDetector | None = None,
    ) -> None:
        self.sock = sock
        self.command = command
        self.prepare = prepare
        self.detector = detector
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping: int | None = None  # signal number once asked to stop
        self.wedge_killed = False

    def on_wedge(self) -> None:
        """Liveness verdict: kill the child; run() respawns it without backoff."""
        child = self.child
        # returncode, not poll(): _wait_child must stay the only reaper.
        if child is None or child.returncode is not None or self.detector is None:
            return
        mcp_liveness.record_wedge_kill(
            self.detector, pid=child.pid, now=time.monotonic()
        )
        self.wedge_killed = True
        try:
            os.kill(child.pid, signal.SIGKILL)  # a wedged process may not honour TERM
        except ProcessLookupError:
            pass

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.stopping = signum
        child = self.child
        # os.kill, not Popen.send_signal: send_signal polls first, which can
        # reap the child behind _wait_child's back. Until _wait_child reaps
        # it, the pid is ours (at worst a zombie), so signalling it is safe.
        if child is not None and child.returncode is None:
            try:
                os.kill(child.pid, signum)
            except ProcessLookupError:
                pass

    def _run_prepare(self) -> bool:
        if not self.prepare:
            return True
        r = subprocess.run(["bash", "-c", self.prepare], check=False)
        if r.returncode != 0:
            _log(f"prepare failed (exit {r.returncode})")
        return r.returncode == 0

    def _wait_child(self, child: subprocess.Popen[bytes]) -> int:
        """Wait for our child, reaping any orphan reparented to us (we are PID 1)."""
        while True:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                # No children left: the child was reaped elsewhere.
                return child.wait()
            if pid == child.pid:
                child.returncode = os.waitstatus_to_exitcode(status)
                return child.returncode

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        fd = self.sock.fileno()
        crashes = 0
        generation = 0
        while self.stopping is None:
            if not self._run_prepare():
                crashes += 1
                time.sleep(crash_backoff_s(crashes))
                continue
            generation += 1
            argv = child_argv(self.command, fd)
            started = time.monotonic()
            self.child = subprocess.Popen(argv, pass_fds=(fd,))
            if self.detector is not None:
                self.detector.child_started(time.monotonic())
            _log(f"generation {generation} started (pid {self.child.pid})")
            code = self._wait_child(self.child)
            uptime = time.monotonic() - started
            if self.stopping is not None:
                _log(f"stopped by signal {self.stopping}; child exited {code}")
                # A signal death reports -signum; the shell convention is 128+signum.
                return code if code >= 0 else 128 - code
            if self.wedge_killed:
                self.wedge_killed = False
                crashes = 0
                _log(
                    f"generation {generation} killed as wedged after {uptime:.0f}s; respawning"
                )
                continue
            if code == 0:
                crashes = 0
                _log(
                    f"generation {generation} exited cleanly after {uptime:.0f}s; respawning"
                )
                continue
            crashes = 1 if uptime >= STABLE_AFTER_S else crashes + 1
            delay = crash_backoff_s(crashes)
            _log(
                f"generation {generation} crashed (exit {code}) after {uptime:.0f}s; "
                f"respawning in {delay:.0f}s"
            )
            time.sleep(delay)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold a TCP port and respawn a server child on the inherited socket.",
        usage="%(prog)s [--host H] [--port P] [--prepare CMD] -- CHILD ... {fd} ...",
    )
    parser.add_argument("--host", default="0.0.0.0")  # container-internal bind
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--prepare",
        default=None,
        help="Shell command run before every child start (e.g. re-copy /src to /app).",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing child command after --")
    if not any(FD_PLACEHOLDER in part for part in command):
        parser.error(
            f"child command must contain {FD_PLACEHOLDER} for the listening fd"
        )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sock = bind_listener(args.host, args.port)
    _log(f"listening on {args.host}:{args.port} (fd {sock.fileno()})")
    token = os.environ.get("PRECIS_MCP_TOKEN")
    detector = mcp_liveness.WedgeDetector() if token else None
    supervisor = Supervisor(sock, command, args.prepare, detector)
    stop = threading.Event()
    if token and detector is not None:
        threading.Thread(
            target=mcp_liveness.run_liveness_loop,
            kwargs={
                "url": f"http://127.0.0.1:{args.port}/mcp",
                "token": token,
                "on_wedge": supervisor.on_wedge,
                "stop": stop,
                "detector": detector,
            },
            daemon=True,
            name="mcp-liveness",
        ).start()
        _log("liveness probe armed")
    else:
        _log("liveness probe off (no PRECIS_MCP_TOKEN)")
    try:
        return supervisor.run()
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
