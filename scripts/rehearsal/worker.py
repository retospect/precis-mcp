"""The real worker, as a subprocess of the scratch world.

Started exactly as a deployed slice host runs it: ``python -m precis worker``
under the *host checkout's* interpreter, with ``--only`` naming the passes the
factory needs:

``heartbeat``
    advertises the host's capabilities (``repo_checkout``, ``git``, one
    ``<cli>_bin`` per lane binary) and capacity rows. The claim path
    refuses a job whose placement tokens the host does not advertise, so
    nothing claims before the first beat (:meth:`Worker.wait_ready`).
``job_ssh_node``
    claims and runs ``run_slice`` / ``land_slice`` (detached submit/poll);
    ``src/precis/workers/executors/ssh_node.py::run_ssh_node_pass``.
``job_inproc``
    claims and runs ``plan_dispatch`` jobs, the dispatcher tick
    (``executors/job_inproc.py``).
``sweeper``
    fails claim-orphaned jobs (optional).

A pass-gate does not apply under ``--only`` (``cli/worker.py::run``), so no
``service_config`` row is needed. ``wake_runner`` and ``scheduler`` are not
selectable with ``--only``; the driver runs a wake pass in-process before
each dispatcher tick (``driver.py::Driver.tick``).

The environment is built from nothing (:func:`.safety.worker_env`): scratch
``HOME`` and ``TMPDIR``, ``PATH`` led by the world's ``bin/`` (git url
wrapper, session wrappers, refusals for ``docker`` / ``gh`` / ``ssh``), the
database DSN of the scratch container, stand-in logins that are not
credentials, and every engine knob the rehearsal needs.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import proc as launch
from . import procguard
from .safety import assert_scratch_dsn, worker_env
from .world import FAKE_ORIGIN, World

#: The node name the world's host advertises and jobs pin to.
NODE = "rehearsal-host"

DEFAULT_PASSES = ("heartbeat", "job_ssh_node", "job_inproc")

#: Names a worker must be given a scratch value for, so the real one
#: (``~/.secrets``, the keychain, a CLI login) is never consulted.
FAKE_CLAUDE_TOKEN = "rehearsal-stand-in-not-a-credential"
FAKE_KIMI_KEY = "rehearsal-stand-in-not-a-credential"


def _path(world: World) -> str:
    """The world's ``bin`` first, then the caller's PATH minus every entry
    under ``/Users`` (a user's own CLI installs and ``~/.local/bin``)."""
    kept = [
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not p.startswith(("/Users/", str(Path.home())))
    ]
    for system in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if system not in kept:
            kept.append(system)
    return os.pathsep.join([str(world.bin), *kept])


def build_env(
    world: World,
    dsn: str,
    *,
    extra: dict[str, str] | None = None,
    uv_offline: bool = False,
) -> dict[str, str]:
    """The worker environment (see the module docstring)."""
    uv_cache = launch.run(["uv", "cache", "dir"], timeout=60).stdout.strip()
    uv_python = launch.run(["uv", "python", "dir"], timeout=60).stdout.strip()
    own: dict[str, str] = {
        "PATH": _path(world),
        "HOME": str(world.home),
        "TMPDIR": str(world.tmp),
        "PYTHONUNBUFFERED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Rehearsal Factory",
        "GIT_AUTHOR_EMAIL": "factory@rehearsal.invalid",
        "GIT_COMMITTER_NAME": "Rehearsal Factory",
        "GIT_COMMITTER_EMAIL": "factory@rehearsal.invalid",
        "PRECIS_SLICE_ROOT": str(world.host),
        "PRECIS_SLICE_OPS_DIR": str(world.ops),
        "PRECIS_NODE": NODE,
        # A deployed unit names its process; the boot-epoch reclaim of a
        # job whose worker was replaced needs it (heartbeat.py).
        "PRECIS_PROCESS": "rehearsal-worker",
        "PRECIS_HOST_NAME": NODE,
        "PRECIS_FACTORY_ORIGIN_URL": FAKE_ORIGIN,
        "PRECIS_CLAUDE_BIN": str(world.bin / "claude"),
        "PRECIS_DEVIN_BIN": str(world.bin / "devin"),
        "PRECIS_KIMI_BIN": str(world.bin / "kimi"),
        "PRECIS_CODEX_BIN": str(world.bin / "codex"),
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_CLAUDE_TOKEN,
        "KIMI_CODE_API_KEY": FAKE_KIMI_KEY,
        "DOCKER_HOST": "unix:///nonexistent-rehearsal.sock",
        "PRECIS_SLICE_STACK_SWEEP": "off",
        # No system gitconfig (its credential helper is the operator's
        # keychain): the world's own ``.gitconfig`` and the wrapper decide.
        "GIT_CONFIG_NOSYSTEM": "1",
        # ship's notify scripts would post to the production graph through
        # prod-precis; these name a stub that records the calls instead.
        "PRECIS_LAND_NOTIFY_CLI": str(world.bin / "notify-stub"),
        "PRECIS_GRIPE_NOTE_CLI": str(world.bin / "notify-stub"),
        "PRECIS_EMBEDDER": "mock",
        "PRECIS_HEARTBEAT_INTERVAL_SECONDS": "3",
        "PRECIS_LOG_LEVEL": "INFO",
        # ``scripts/ship --quick`` runs its lint through this host command
        # instead of the container gate (the test seam in scripts/ship that
        # ``tests/test_ship_*`` use): the command the gate would have run
        # arrives in PRECIS_SHIP_TEST_GATE_ARG and is run in the land folder.
        "PRECIS_SHIP_TEST_GATE_CMD": 'eval "$PRECIS_SHIP_TEST_GATE_ARG"',
        "UV_CACHE_DIR": uv_cache or str(world.run_dir / "uv-cache"),
        **({"UV_PYTHON_INSTALL_DIR": uv_python} if uv_python else {}),
        **({"UV_OFFLINE": "1"} if uv_offline else {}),
    }
    own.update(extra or {})
    return worker_env(os.environ, own=own, dsn=dsn)


@dataclass
class Worker:
    """One worker process."""

    world: World
    env: dict[str, str]
    passes: tuple[str, ...] = DEFAULT_PASSES
    idle_seconds: float = 0.5
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    @property
    def log_path(self) -> Path:
        return self.world.logs / "worker.log"

    def argv(self) -> list[str]:
        argv = [str(self.world.python), "-m", "precis", "worker"]
        for name in self.passes:
            argv += ["--only", name]
        argv += [
            "--database-url",
            self.env["PRECIS_DATABASE_URL"],
            "--embedder",
            "mock",
            "--idle-seconds",
            str(self.idle_seconds),
            "--batch-size",
            "4",
        ]
        return argv

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("worker already running")
        # The fence once more, on exactly what the child will be given.
        assert_scratch_dsn(self.env.get("PRECIS_DATABASE_URL", ""))
        self.world.logs.mkdir(parents=True, exist_ok=True)
        self.proc = launch.popen(
            self.argv(), cwd=self.world.run_dir, env=self.env, log=self.log_path
        )
        (self.world.run_dir / "worker.pid").write_text(
            str(self.proc.pid), encoding="utf-8"
        )

    def kill(self) -> str:
        """SIGKILL the worker's process group, as a crash or a power cut
        does: no shutdown, no release. The scripted sessions it launched
        run in sessions of their own and are not touched."""
        if self.proc is None or self.proc.poll() is not None:
            return "worker was not running"
        self._signal(signal.SIGKILL)
        self.proc.wait(timeout=30)
        return f"worker pid {self.proc.pid} killed"

    def _signal(self, sig: int) -> None:
        """Signal the worker's group when the process table shows it is this
        run's; otherwise only the child we started (a Popen's pid is ours)."""
        assert self.proc is not None
        if not procguard.signal_group(self.proc.pid, sig, self.world.run_dir):
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def restart(self) -> int:
        """Start a new worker (after :meth:`kill`); returns its pid."""
        self.start()
        assert self.proc is not None
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, lines: int = 25) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def stop(self, timeout: float = 30.0) -> str:
        """SIGTERM the worker's process group, SIGKILL after ``timeout``."""
        if self.proc is None:
            return "worker never started"
        if self.proc.poll() is not None:
            return f"worker had exited ({self.proc.returncode})"
        self._signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.2)
        if self.proc.poll() is None:
            self._signal(signal.SIGKILL)
            self.proc.wait(timeout=10)
            return "worker killed after the grace period"
        return f"worker stopped ({self.proc.returncode})"


def python_interpreter() -> str:
    return sys.executable
