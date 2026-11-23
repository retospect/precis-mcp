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
watchdog, a drain request) and is replaced at once, as is a child killed
by SIGTERM or SIGHUP — someone asked it to go (uvicorn re-raises SIGTERM
after its shutdown, and a child without the drain handler dies of
SIGHUP). Any other exit is a crash, replaced after an exponential backoff
that resets once a child has stayed up for a while.

Bounce: SIGHUP to the supervisor turns the generation over without
touching the listener — it forwards :data:`BOUNCE_SIGNAL` to the live
child (a prompt cutover drain, no quiet-window deferral) and respawns on
the clean exit. A deploy whose install has already been swapped uses this
to land the new code while the port stays bound the whole time (gr660009);
``deploy/tasks/reload_launchd_draining.yml`` is the Ansible side. A bounce
during a stop, or a second bounce before the first reached the child, is
ignored.

Self-supervision: a ``precis serve`` over a network transport that launchd
started directly (``ppid == 1`` on macOS — the mcp-public gateway daemon)
re-execs into this module via :func:`relaunch_as_supervisor` instead of
binding its port in the serve process, so the launchd-tracked process IS
the socket holder and a deploy's SIGHUP bounce reaches it. The gate is
:func:`wants_self_supervision`; every other launch shape (``--fd``
children, ``--uds`` callbacks, stdio sessions, terminals, tests, the
container fallback) keeps its plain single process. The self-supervised
run passes ``--no-liveness``: the probe's token can outlive its
acceptance (a credential-class rotation deletes the token file only after
the new child already answers it 401), and three failed probes would
SIGKILL healthy generations.

Stopping: SIGTERM (``docker stop``) is forwarded to the child as
:data:`DRAIN_SIGNAL`, so it finishes its in-flight calls before exiting
— forwarded as SIGTERM, uvicorn's shutdown would cancel them (gr460711).
The listening socket stays open meanwhile, so new connections queue
rather than being refused. ``docker stop -t`` bounds the drain. SIGINT is
forwarded as is. The supervisor exits with the child's status.

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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

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

#: What the child gets when the supervisor is asked to stop with SIGTERM:
#: drain in-flight calls, then exit. Mirrors
#: ``precis.install_watchdog.DRAIN_SIGNAL``, which this file cannot import.
DRAIN_SIGNAL = signal.SIGHUP

#: What a bounce (SIGHUP to the supervisor) forwards to the child: finish
#: in-flight calls promptly — no quiet-window deferral — and exit, so the
#: next generation starts on the swapped install at once (gr660009).
BOUNCE_SIGNAL = getattr(signal, "SIGUSR1", None)

#: Child deaths by these signals were requested, not crashes.
REQUESTED_DEATHS = frozenset({-signal.SIGTERM, -signal.SIGHUP})

#: Opt-out for the launchd self-supervision (see :func:`wants_self_supervision`).
_SELF_SUPERVISE_ENV = "PRECIS_SERVE_SUPERVISOR"


def _mcp_token() -> str | None:
    """The bearer token from env, else from the mounted secrets directory.

    Mirrors :func:`precis.secrets.mounted_secret`, which this file cannot
    import: as PID 1 it runs from a standalone copy (gr458350)."""
    token = os.environ.get("PRECIS_MCP_TOKEN")
    if token:
        return token
    secrets_dir = os.environ.get("PRECIS_SECRETS_FILE_DIR")
    if not secrets_dir:
        return None
    try:
        path = os.path.join(secrets_dir, "PRECIS_MCP_TOKEN")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


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


def wants_self_supervision(
    *,
    transport: str,
    fd: int | None,
    uds: str | None,
    platform: str,
    ppid: int,
    environ: Mapping[str, str],
) -> bool:
    """Should a direct-bind network ``precis serve`` exec into this supervisor?

    Yes when launchd started it: on macOS a LaunchDaemon's program is
    reparented to launchd (``ppid == 1``), and the mcp-public daemon plist
    binds the port in the serve process itself — so a deploy's restart used
    to kill the listener and every in-flight call with it (gr660009).
    Relaunching as ``python -m precis.mcp_supervisor -- <self> --fd {fd}``
    makes the launchd-tracked process the socket holder: deploys signal
    SIGHUP, the child drains, and the next generation boots on the swapped
    install while connections wait in the listen backlog.

    Every other shape is excluded: ``--fd`` children already have a
    supervisor, ``--uds`` serves are short-lived per-run callbacks, stdio
    serves are per-session, and anything not launched by launchd (terminals,
    tests, the container's fallback direct serve) keeps its plain process.
    ``PRECIS_SERVE_SUPERVISOR=0`` opts out.
    """
    return (
        transport in {"sse", "streamable-http"}
        and fd is None
        and uds is None
        and platform == "darwin"
        and ppid == 1
        and environ.get(_SELF_SUPERVISE_ENV, "1") != "0"
    )


def supervisor_command(
    *,
    python: str,
    transport: str,
    host: str,
    port: int,
    token: str | None,
    migrate: bool,
    read_only: bool,
) -> list[str]:
    """The ``python -m precis.mcp_supervisor`` argv a launchd-bound serve
    execs into (gr660009) — pure so the shape is testable.

    The child re-runs this same serve on the supervisor's socket; every
    respawn re-resolves ``precis.cli`` from whatever install is current, so
    a generation automatically picks up the deploy's venv swap.
    """
    child = [
        python,
        "-m",
        "precis.cli",
        "serve",
        "--transport",
        transport,
        "--fd",
        FD_PLACEHOLDER,
    ]
    if token:
        child += ["--token", token]
    if migrate:
        child += ["--migrate"]
    if read_only:
        child += ["--read-only"]
    return [
        python,
        "-m",
        "precis.mcp_supervisor",
        # Liveness stays off on this path: the probe token can outlive its
        # acceptance (a credential-class rotation deletes the file only
        # after the new child already answers it 401), and three failed
        # probes would SIGKILL healthy generations.
        "--no-liveness",
        "--host",
        host,
        "--port",
        str(port),
        "--",
        *child,
    ]


def relaunch_as_supervisor(
    *,
    transport: str,
    host: str,
    port: int,
    token: str | None,
    migrate: bool,
    read_only: bool,
) -> NoReturn:
    """Replace this process with its self-supervision argv; never returns."""
    os.execvpe(
        sys.executable,
        supervisor_command(
            python=sys.executable,
            transport=transport,
            host=host,
            port=port,
            token=token,
            migrate=migrate,
            read_only=read_only,
        ),
        os.environ,
    )


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
        self._bounce_pending = False  # a SIGHUP not yet forwarded to a child

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
        self._forward_stop()

    def _forward_stop(self) -> None:
        """Pass the pending stop signal to the live child, as a drain for SIGTERM.

        Called from the handler and again right after each spawn: a signal
        that lands while Popen runs finds ``self.child`` still pointing at the
        reaped generation, and would otherwise never reach the new one. A
        repeat is harmless — the serve child ignores DRAIN_SIGNAL mid-drain.
        """
        signum, child = self.stopping, self.child
        if signum is None:
            return
        # os.kill, not Popen.send_signal: send_signal polls first, which can
        # reap the child behind _wait_child's back. Until _wait_child reaps
        # it, the pid is ours (at worst a zombie), so signalling it is safe.
        if child is not None and child.returncode is None:
            forward = DRAIN_SIGNAL if signum == signal.SIGTERM else signum
            try:
                os.kill(child.pid, forward)
            except ProcessLookupError:
                pass

    def _on_bounce(self, _signum: int, _frame: object) -> None:
        """SIGHUP: turn the generation over (cutover drain + respawn), stay alive.

        The launchd deploy path signals the long-lived supervisor instead of
        booting it out, so the listen socket never goes dark (gr660009). A
        bounce during a stop, or a second bounce before the first one
        reached a live child, is ignored.
        """
        if self.stopping is not None or self._bounce_pending:
            return
        self._bounce_pending = True
        self._forward_bounce()

    def _forward_bounce(self) -> None:
        """Pass a pending bounce to the live child (see _forward_stop).

        A bounce that lands while no child is running (mid-prepare or
        between generations) waits for the next spawn. The flag clears only
        once the signal reached a live child, so a bounce never fans out to
        more than one generation.
        """
        child = self.child
        if (
            not self._bounce_pending
            or BOUNCE_SIGNAL is None
            or child is None
            or child.returncode is not None
        ):
            return
        try:
            os.kill(child.pid, BOUNCE_SIGNAL)
        except ProcessLookupError:
            return
        self._bounce_pending = False

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
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._on_bounce)
        fd = self.sock.fileno()
        crashes = 0
        generation = 0
        while self.stopping is None:
            if not self._run_prepare():
                crashes += 1
                time.sleep(crash_backoff_s(crashes))
                continue
            if self.stopping is not None:
                # Asked to stop while the prepare step ran (gr462133).
                _log(
                    f"stopped by signal {self.stopping} before generation {generation + 1}"
                )
                return 0
            generation += 1
            argv = child_argv(self.command, fd)
            started = time.monotonic()
            self.child = subprocess.Popen(argv, pass_fds=(fd,))
            self._forward_stop()
            self._forward_bounce()
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
            if code == 0 or code in REQUESTED_DEATHS:
                crashes = 0
                how = "exited cleanly" if code == 0 else f"was killed by signal {-code}"
                _log(f"generation {generation} {how} after {uptime:.0f}s; respawning")
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
    parser.add_argument(
        "--no-liveness",
        action="store_true",
        help="Skip the wedge detector even when a token is resolvable. The "
        "launchd self-supervision passes this: its probe token can outlive "
        "its acceptance, and three failed probes would SIGKILL healthy "
        "generations (gr660009).",
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
    # No install_log_scrub() here: the supervisor imports stdlib only (it
    # outlives /app re-copies) and logs nothing but its own child lifecycle.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sock = bind_listener(args.host, args.port)
    _log(f"listening on {args.host}:{args.port} (fd {sock.fileno()})")
    token = None if args.no_liveness else _mcp_token()
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
        _log(
            "liveness probe off (--no-liveness)"
            if args.no_liveness
            else "liveness probe off (no PRECIS_MCP_TOKEN)"
        )
    try:
        return supervisor.run()
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
