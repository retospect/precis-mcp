"""Per-slice process and memory containment for a build session (gr762463).

A run_slice session is an agent CLI with a shell tool. Its shell commands
lead process groups of their own, so the session's group (the launch's
``start_new_session``) does not hold them: on 2026-11-22 a builder's
pytest-xdist experiment in ``/tmp`` grew to 5604 processes in another
group, 1040 of them orphaned to pid 1, and starved the build host until
sshd died. A process group cannot bound that; a cgroup can.

* **Linux with a lingering user systemd manager** (the managed builder
  account, ``docker_rootless.yml``): the session's argv runs under
  ``systemd-run --user --scope`` with ``TasksMax``, ``MemoryMax`` and
  ``MemorySwapMax=0``. Every descendant, orphans included, stays in the
  scope's cgroup and counts against its limits; stopping the unit
  (:func:`stop_scope`) kills all of it. ``systemd-run --scope`` execs the
  command in its own process, so the launch's pid, process group and
  identity are unchanged. The scope's name rides the run handle as
  ``slice_scope``; only a name of this module's shape is ever stopped.
* **macOS, or Linux without a usable user manager**: no cgroup. With
  ``PRECIS_SLICE_NPROC`` set the child gets ``RLIMIT_NPROC`` (soft and
  hard, so no descendant can raise it). The limit counts every process of
  the account, not of the slice, so the ceiling must leave room for the
  host's concurrent slices; Linux counts threads against it as well. The
  kill on job end stays the process group's.

Every knob is off in code: a lane keeps today's launch unless its unit
sets them. Only the builder-host role does
(``deploy/roles/precis_builder_host/defaults/main.yml``): a scope leaves
the worker unit's cgroup, so it is not counted against the unit's
``MemoryMax`` or the host's memory budget, and a lane with no lingering
user manager has no scope to give.

* ``PRECIS_SLICE_SCOPE`` — ``auto`` uses a scope when the probe succeeds;
  anything else (the default: unset) never does.
* ``PRECIS_SLICE_TASKS_MAX`` — the scope's ``TasksMax`` (tasks are threads
  as well as processes), default 2048; ``0``/``off`` drops it.
* ``PRECIS_SLICE_MEMORY_MAX`` — the scope's ``MemoryMax``, a systemd size
  (``16G``) or a percentage of host RAM (``25%``, the default);
  ``off`` drops it, and ``MemorySwapMax=0`` with it.
* ``PRECIS_SLICE_NPROC`` — the ``RLIMIT_NPROC`` ceiling without a scope;
  unset (the default), ``0`` or ``off`` sets none.

A cached positive probe is dropped (:func:`forget_probe`) when the runtime
dir is gone at launch, or when a session dies of systemd-run's own bus or
scope error (:func:`scope_launch_failed`, which the poll treats as the
host's failure, not the task's).

Docker containers a session starts (``scripts/test``'s gate) run under the
docker daemon's own cgroup, not the scope: their limits are the gate's.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCOPE_ENV = "PRECIS_SLICE_SCOPE"
TASKS_MAX_ENV = "PRECIS_SLICE_TASKS_MAX"
MEMORY_MAX_ENV = "PRECIS_SLICE_MEMORY_MAX"
NPROC_ENV = "PRECIS_SLICE_NPROC"

DEFAULT_TASKS_MAX = 2048
DEFAULT_MEMORY_MAX = "25%"

#: logind's lingering flags, one empty file per account.
_LINGER_DIR = "/var/lib/systemd/linger"
#: systemd-run's own failures before it execs the session: no bus to the
#: user manager, or the manager refused the scope.
_SCOPE_FAIL_RE = re.compile(
    r"^Failed to (?:connect to\b.*\bbus|create bus connection"
    r"|start transient scope unit|add PIDs to scope)",
    re.MULTILINE,
)
#: A systemd-run failure leaves only its own line or two in the log.
_SCOPE_FAIL_MAX_LINES = 5

#: ``env(1)`` sets the user manager's runtime dir for systemd-run only.
_ENV = "/usr/bin/env"

#: Seconds systemd waits between the stop's SIGTERM and its SIGKILL.
STOP_TIMEOUT_S = 15

_OFF = frozenset({"0", "off", "none", "no", "false", "infinity", "unlimited"})
#: A systemd size (bytes, or a K/M/G/T suffix, 1024-based) or a percentage
#: of physical memory — nothing else reaches ``-p MemoryMax=``.
_MEMORY_RE = re.compile(r"(?:\d+[KMGT]?|\d{1,2}(?:\.\d+)?%|100%)")
#: The only unit names :func:`stop_scope` stops: a handle value naming any
#: other unit (the docker daemon's, say) is ignored.
_UNIT_RE = re.compile(r"precis-slice-[A-Za-z0-9_-]{1,80}\.scope")
_LABEL_DROP = re.compile(r"[^A-Za-z0-9_-]+")

#: How long a probe's answer is reused before the next launch probes again.
_PROBE_TTL_S = 600.0
_probe: tuple[float, str | None, str] | None = None


def _env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def tasks_max() -> int | None:
    """The scope's ``TasksMax``, ``None`` when switched off."""
    raw = _env_value(TASKS_MAX_ENV)
    if not raw:
        return DEFAULT_TASKS_MAX
    if raw.lower() in _OFF:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r unreadable; using %d", TASKS_MAX_ENV, raw, DEFAULT_TASKS_MAX)
        return DEFAULT_TASKS_MAX
    return value if value > 0 else None


def memory_max() -> str | None:
    """The scope's ``MemoryMax`` as systemd spells it, ``None`` when off."""
    raw = _env_value(MEMORY_MAX_ENV)
    if not raw:
        return DEFAULT_MEMORY_MAX
    if raw.lower() in _OFF:
        return None
    value = raw.upper() if raw[-1:].isalpha() else raw
    if not _MEMORY_RE.fullmatch(value) or value.rstrip("%KMGT").strip("0.") == "":
        log.warning(
            "%s=%r unreadable; using %s", MEMORY_MAX_ENV, raw, DEFAULT_MEMORY_MAX
        )
        return DEFAULT_MEMORY_MAX
    return value


def nproc_limit() -> int | None:
    """The ``RLIMIT_NPROC`` ceiling for a launch without a scope; ``None``
    (no rlimit) unless the unit sets one."""
    raw = _env_value(NPROC_ENV)
    if not raw or raw.lower() in _OFF:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r unreadable; no RLIMIT_NPROC", NPROC_ENV, raw)
        return None
    return value if value > 0 else None


def scope_props() -> list[str]:
    """The ``-p`` properties of a slice scope — the probe passes the same
    set, so a value this host's systemd rejects fails the probe, not every
    launch."""
    props = [f"TimeoutStopSec={STOP_TIMEOUT_S}"]
    tasks = tasks_max()
    if tasks is not None:
        props.append(f"TasksMax={tasks}")
    memory = memory_max()
    if memory is not None:
        props += [f"MemoryMax={memory}", "MemorySwapMax=0"]
    return props


def _account() -> str | None:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return None


def _lingering(user: str) -> bool:
    """Whether ``user``'s manager outlives its sessions: logind's flag file,
    else ``loginctl show-user`` (a host that keeps the flag elsewhere)."""
    if os.path.exists(os.path.join(_LINGER_DIR, user)):
        return True
    if shutil.which("loginctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "Linger=yes"


def _runtime_dir() -> str | None:
    """Where the user manager's sockets live: ``XDG_RUNTIME_DIR``, else
    ``/run/user/<uid>`` when it exists (a system unit for the builder
    account may not export it)."""
    found = os.environ.get("XDG_RUNTIME_DIR")
    if found:
        return found
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    candidate = f"/run/user/{getuid()}"
    return candidate if os.path.isdir(candidate) else None


def _systemd_env() -> dict[str, str]:
    env = dict(os.environ)
    runtime = _runtime_dir()
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    return env


def _scope_probe() -> tuple[str | None, str]:
    """``(systemd-run path, why)``: the path when this account can start a
    transient user scope right now, ``None`` and the reason otherwise.
    The account must linger (a manager that dies with the last login would
    take every running slice's scope with it). Then a real one-shot
    ``systemd-run --user --scope`` with the launch's own properties is the
    probe: the worker runs in a system unit, and only the user manager
    (asking the system manager to move the pid) can say whether that
    works."""
    if sys.platform != "linux":
        return None, f"no cgroups on {sys.platform}"
    exe = shutil.which("systemd-run")
    if exe is None:
        return None, "no systemd-run on PATH"
    if _runtime_dir() is None:
        return None, "no user runtime dir (user manager not running; lingering off?)"
    user = _account()
    if user is None or not _lingering(user):
        return None, f"lingering is off for {user or 'this account'}"
    # The launch's env is slice_env's allowlist, not the worker's: probe
    # with no more than the launch will have (no DBUS_SESSION_BUS_ADDRESS).
    probe_env = {
        k: v
        for k, v in _systemd_env().items()
        if k in ("PATH", "HOME", "XDG_RUNTIME_DIR")
    }
    props = [arg for prop in scope_props() for arg in ("-p", prop)]
    try:
        # A literal head: the PATH lookup is the one shutil.which just made.
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", *props]
            + ["--", "true"],
            env=probe_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"systemd-run probe failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        err = " ".join((proc.stderr or proc.stdout or "").split())[:200]
        return None, f"systemd-run --user --scope exit {proc.returncode}: {err}"
    return exe, "user scope available"


def scope_runner() -> tuple[str | None, str]:
    """:func:`_scope_probe`, reused for :data:`_PROBE_TTL_S`. Only
    ``auto`` in :data:`SCOPE_ENV` probes; unset or anything else is off."""
    global _probe
    mode = _env_value(SCOPE_ENV).lower()
    if mode != "auto":
        return None, f"{SCOPE_ENV}={mode or 'unset'}"
    now = time.monotonic()
    if _probe is not None and now - _probe[0] < _PROBE_TTL_S:
        return _probe[1], _probe[2]
    exe, why = _scope_probe()
    if exe is None:
        log.warning("run_slice: no per-slice scope: %s", why)
    _probe = (now, exe, why)
    return exe, why


def forget_probe() -> None:
    """Drop the cached probe answer: the next launch probes again."""
    global _probe
    _probe = None


def scope_launch_failed(scope: object, log_text: str, exit_code: int | None) -> bool:
    """Whether a scoped session died of systemd-run itself (no bus to the
    user manager, the scope refused) before the agent ran: a non-zero exit,
    a log of a few lines, one of them systemd-run's own failure. The host's
    failure, never the task's."""
    if not isinstance(scope, str) or not scope or exit_code in (None, 0):
        return False
    lines = log_text.strip().splitlines()
    if not lines or len(lines) > _SCOPE_FAIL_MAX_LINES:
        return False
    return _SCOPE_FAIL_RE.search(log_text.strip()) is not None


def scope_unit(label: str) -> str:
    """A fresh unit name for one launch: a relaunch of the same job must not
    collide with an earlier scope whose orphans are still being stopped."""
    clean = _LABEL_DROP.sub("-", label).strip("-")[:60] or "run"
    return f"precis-slice-{clean}-{secrets.token_hex(4)}.scope"


def _chain(
    first: Callable[[], object] | None, second: Callable[[], object] | None
) -> Callable[[], object] | None:
    if first is None:
        return second
    if second is None:
        return first

    def _both() -> bool:
        a = first()
        b = second()
        return a is not False and b is not False

    return _both


def nproc_preexec(limit: int | None) -> Callable[[], object] | None:
    """A ``preexec_fn`` setting ``RLIMIT_NPROC`` soft and hard to ``limit``
    (clamped under an inherited hard limit), so no descendant can raise
    it. ``None`` without a limit or without POSIX rlimits."""
    if limit is None or os.name != "posix":
        return None
    import resource

    what = getattr(resource, "RLIMIT_NPROC", None)
    if what is None:
        return None

    def _apply() -> bool:
        try:
            _soft, hard = resource.getrlimit(what)
            cap = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(what, (cap, cap))
        except (ValueError, OSError):
            # Runs after fork: no logging (its locks may be held).
            return False
        return True

    return _apply


@dataclass(frozen=True)
class Containment:
    """How one launch is bounded: ``scope`` is the transient unit's name
    when the session runs in one (the handle's ``slice_scope``), ``prefix``
    the argv that starts it, ``nproc`` the rlimit when there is no scope,
    ``note`` a one-line account for the log."""

    scope: str | None
    prefix: tuple[str, ...]
    nproc: int | None
    note: str

    def wrap(self, argv: list[str]) -> list[str]:
        return [*self.prefix, *argv] if self.prefix else list(argv)

    def preexec(
        self, base: Callable[[], object] | None = None
    ) -> Callable[[], object] | None:
        return _chain(base, nproc_preexec(self.nproc))


def containment(label: str) -> Containment:
    """The bounds for one session launch labelled ``label`` (``td<T>-j<J>``)."""
    exe, why = scope_runner()
    runtime = _runtime_dir() if exe is not None else None
    if exe is not None and runtime is None:
        # The probe said yes, but the user manager's runtime dir is gone
        # now (the manager stopped): no scope, and probe again next time.
        forget_probe()
        exe, why = None, "user runtime dir gone since the probe"
        log.warning("run_slice: no per-slice scope: %s", why)
    if exe is not None:
        unit = scope_unit(label)
        props = scope_props()
        # systemd-run finds the user manager through XDG_RUNTIME_DIR, which
        # slice_env's allowlist drops; the session's own env stays as
        # slice_env built it (the name is unset again after ``--``).
        prefix: list[str] = [_ENV, f"XDG_RUNTIME_DIR={runtime}"]
        prefix += [exe, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
        for prop in props:
            prefix += ["-p", prop]
        prefix += ["--", _ENV, "-u", "XDG_RUNTIME_DIR"]
        limits = ", ".join(props[1:]) or "no limits"
        return Containment(unit, tuple(prefix), None, f"scope {unit} ({limits})")
    nproc = nproc_limit()
    if nproc is not None:
        return Containment(None, (), nproc, f"RLIMIT_NPROC={nproc} ({why})")
    return Containment(None, (), None, f"process group only ({why})")


def stop_scope(unit: object) -> bool:
    """Stop the slice's scope without waiting: systemd TERMs every process
    in it and KILLs what remains after :data:`STOP_TIMEOUT_S`. ``True``
    when systemd took the job. Anything but a name of this module's shape
    is ignored, so a forged handle cannot stop another unit."""
    if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
        return False
    if sys.platform != "linux":
        return False
    if shutil.which("systemctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "stop", "--no-block", unit],
            env=_systemd_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("run_slice: stopping %s failed", unit, exc_info=True)
        return False
    return proc.returncode == 0


__all__ = [
    "Containment",
    "containment",
    "forget_probe",
    "memory_max",
    "nproc_limit",
    "nproc_preexec",
    "scope_launch_failed",
    "scope_props",
    "scope_runner",
    "scope_unit",
    "stop_scope",
    "tasks_max",
]
