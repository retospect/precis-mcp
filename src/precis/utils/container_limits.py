"""Optional resource limits for spawned job children (containers + bare
subprocesses).

CPU pinning and weighting are advisory (`container_limit_flags`), so is the
container user (`container_user_flags`) — read env at call time so
``monkeypatch.setenv`` works in tests. On the scheduler side of a dispatch
the values are rendered by ansible out of ``worker_limits``/``precis_lane_memory``
(``deploy/roles/hardening/``) and land in the worker unit's ``EnvironmentFile``.
Operators pick the values by host shape — same policy knob the cgroup slices
or cgroup-only lane modes set.

The memory half (gr595537) is the per-task hard cap: a spawned job that
eats the box used to sit under only the unit-level ``MemoryMax``, which
kills the whole worker. Now every ``docker``/``podman run`` argv built here
can carry ``--memory <m> --memory-swap <m>`` (equal — no swap on top of the
cap; swap is exactly the host-wedging path this fence exists to close), and
a bare ``subprocess.Popen`` child gets the same cap as an rlimit through
:func:`mem_limit_preexec`.

The cap resolves per call from three layers (weakest → strongest):

* the footprint estimate the caller passes as ``mem_mb`` (predicted peak +
  margin — ``meta.footprint_req`` once gr595539 stamps it, sized off the
  ``meta.footprint`` rows gr595536 measures);
* ``_JOB_MEM_DEFAULT_MB[job_type]`` — a static per-job_type default for
  when no estimate exists yet;
* ``PRECIS_JOB_MEM_LIMIT`` — the host-level operator ceiling, a docker size
  string (``"8g"``, ``"40960m"``, bytes). It *clamps* either source down
  and supplies the cap when neither exists; ``0``/``none``/``off`` disables
  capping outright (the escape hatch — nothing overrides an explicit
  disable, and an operator ceiling can never be raised by an estimate).

A capped container dies by SIGKILL from the cgroup OOM-killer — exit 137 /
``-9`` / ``.State.OOMKilled`` — which :func:`memory_cap_killed` turns into
the recognisable :data:`KILLED_FOR_MEMORY` outcome the requeue policy reads
(gr595538).
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable

log = logging.getLogger(__name__)

_MIB = 1024 * 1024

#: Host-level memory cap for spawned job children, a docker size string —
#: the same family of operator knob as ``PRECIS_JOB_CPUSET`` below.
_MEM_LIMIT_ENV = "PRECIS_JOB_MEM_LIMIT"

#: Spellings that mean "explicitly uncapped" for the env knob — an operator
#: disable wins over every other source, including a footprint estimate.
_DISABLE_VALUES = frozenset({"0", "none", "off", "unlimited"})

#: The outcome a memory-cap kill stamps on the job's ``meta.outcome`` —
#: the recognisable flag the requeue policy reads (gr595538).
KILLED_FOR_MEMORY = "killed-for-memory"

#: Exit codes a SIGKILL surfaces as: 137 = 128+9 (shell/docker encoding),
#: -9 = ``subprocess``'s raw signal encoding.
MEMORY_KILL_RETURNCODES = frozenset({137, -9})

#: Static per-job_type memory defaults (MiB) — the cold-start layer for the
#: cap when the caller carries no footprint estimate. Only job types the
#: gripe named gain an entry, and every value is GENEROUS (fences a
#: runaway, doesn't constrain normal work) until
#: ``Store.job_footprint_summary``'s p95s size them for real. struct_relax
#: and fold are deliberately absent: their caps are operator-sized opt-in
#: knobs already (``PRECIS_DFT_MEM_LIMIT`` / ``PRECIS_FOLD_MEM_LIMIT``,
#: gr489395).
_JOB_MEM_DEFAULT_MB: dict[str, int] = {
    # A `claude -p` agent container — the sandbox lane books 8 GiB per
    # container for the same class of work (roles/hardening
    # precis_lane_memory.sandbox.container_reserve).
    "agent_container": 8192,
    # precis-tts render (kokoro + ffmpeg): a few GiB in normal work; 8 GiB
    # fences a runaway without touching a healthy episode.
    "tts_render": 8192,
    # retrosynth containers (the aizynth planner and the LinChemIn
    # normalizer share the job_type) — no measured budget yet; the same
    # generous 8 GiB class.
    "retrosynth": 8192,
}

#: Docker size-string suffixes → MiB multiplier, longest first so ``"8mb"``
#: doesn't parse as ``"8m" + "b"``. Docker's own convention: single letters
#: are powers of two, the ``*b`` spellings powers of ten, a bare number is
#: bytes.
_SIZE_UNITS: tuple[tuple[str, float], ...] = (
    ("kb", 1e3 / _MIB),
    ("mb", 1e6 / _MIB),
    ("gb", 1e9 / _MIB),
    ("tb", 1e12 / _MIB),
    ("b", 1.0 / _MIB),
    ("k", 1024.0),
    ("m", 1.0),
    ("g", 1024.0),
    ("t", 1024.0 * 1024.0),
)


def _size_to_mb(raw: str) -> float | None:
    """A docker size string → MiB (``"40g"`` → 40960, ``"512m"`` → 512).
    A bare number is bytes (docker's convention). ``None`` when
    unparseable."""
    s = raw.strip().lower()
    num, mult = s, 1.0 / _MIB  # bare number = bytes
    for suffix, unit_mult in _SIZE_UNITS:
        if s.endswith(suffix):
            num, mult = s[: -len(suffix)], unit_mult
            break
    try:
        return float(num) * mult
    except ValueError:
        return None


def job_mem_limit_mb(
    job_type: str | None = None, mem_mb: float | None = None
) -> float | None:
    """The effective per-task memory cap in MiB, or ``None`` (uncapped).

    ``mem_mb`` is the footprint estimate (predicted peak + margin) — the
    per-task source. The static ``_JOB_MEM_DEFAULT_MB[job_type]`` fills in
    when no estimate exists. ``PRECIS_JOB_MEM_LIMIT`` is the host ceiling:
    it clamps either source down, supplies the cap when neither exists, and
    a disable spelling wins over both. An unreadable env value falls back
    to the estimate/default rather than silently uncapping.
    """
    if mem_mb is not None and mem_mb > 0:
        base: float | None = float(mem_mb)
    else:
        default = _JOB_MEM_DEFAULT_MB.get(job_type or "", 0)
        base = float(default) if default > 0 else None
    raw = os.environ.get(_MEM_LIMIT_ENV, "").strip()
    if not raw:
        return base
    if raw.lower() in _DISABLE_VALUES:
        return None
    ceiling = _size_to_mb(raw)
    if ceiling is None or ceiling <= 0:
        log.warning(
            "%s=%r unreadable — falling back to the estimate/default",
            _MEM_LIMIT_ENV,
            raw,
        )
        return base
    return min(ceiling, base) if base is not None else ceiling


def container_memory_flags(
    job_type: str | None = None, mem_mb: float | None = None
) -> list[str]:
    """``--memory <m> --memory-swap <m>`` for the resolved cap (gr595537).

    ``--memory-swap`` equal to ``--memory`` means no swap on top of the
    cap — a runaway child hits its own ceiling instead of pushing the host
    into swap (docker sizes ``--memory-swap`` as memory+swap combined, so
    equal values give zero swap). Empty list when no cap resolves so
    callers splice it unconditionally and keep no-env-no-flag behaviour.
    """
    mb = job_mem_limit_mb(job_type, mem_mb)
    if mb is None:
        return []
    size = f"{math.ceil(mb)}m"
    return ["--memory", size, "--memory-swap", size]


def container_limit_flags(
    *,
    job_type: str | None = None,
    mem_mb: float | None = None,
) -> list[str]:
    """CPU + memory ``run`` flags for a spawned job container.

    CPU side as before: ``PRECIS_JOB_CPUSET`` (empty/unset ⇒ no flag) is a
    docker/podman ``--cpuset-cpus`` spec like ``"0-7"`` pinning the
    container's cpus; ``PRECIS_JOB_CPU_SHARES`` (empty/unset ⇒ no flag) is
    a cgroup v1 ``--cpu-shares`` weight (default 1024) — relative priority
    among siblings, meaningful only under contention. Both are operator
    knobs, rendered per host.

    Memory side: ``job_type``/``mem_mb`` feed
    :func:`job_mem_limit_mb`; both default to nothing so a bare call is
    byte-identical to before (CPU-only flags, no ``--memory``).
    """
    flags: list[str] = []
    cpuset = os.environ.get("PRECIS_JOB_CPUSET", "").strip()
    if cpuset:
        flags += ["--cpuset-cpus", cpuset]
    shares = os.environ.get("PRECIS_JOB_CPU_SHARES", "").strip()
    if shares:
        flags += ["--cpu-shares", shares]
    flags += container_memory_flags(job_type, mem_mb)
    return flags


def mem_limit_preexec(
    mem_mb: float | None = None,
    *,
    job_type: str | None = None,
    address_space: bool = False,
) -> Callable[[], None] | None:
    """A ``preexec_fn`` capping a bare subprocess's memory (gr595537).

    Resolves the same cap as the container flags (estimate →
    ``PRECIS_JOB_MEM_LIMIT`` → per-job_type default) and applies it as
    ``RLIMIT_DATA`` — on Linux ≥ 4.7 it counts a task's writable private
    mappings plus brk, which is the footprint that wedges a host.
    ``RLIMIT_AS`` is opt-in (``address_space=True``): CUDA/torch reserve
    hundreds of GiB of *virtual* space never resident, so an address-space
    cap would kill GPU jobs outright —
    :func:`precis_pathway.runner._apply_memory_cap` made the same call.
    The soft limit clamps under an inherited hard one — a child never
    *raises* its ceiling.

    Over the cap, allocations fail: Python raises ``MemoryError`` (which a
    child like pathway's maps to its classified "memory limit exceeded"
    failure); a child that can't handle it dies to SIGKILL, the exit the
    requeue policy reads (:func:`memory_cap_killed`, exit 137).

    ``None`` when no cap resolves or the platform lacks POSIX rlimits —
    pass the return straight to ``subprocess.Popen(preexec_fn=...)``.
    """
    mb = job_mem_limit_mb(job_type, mem_mb)
    if mb is None or os.name != "posix":
        return None
    import resource

    cap = math.ceil(mb * _MIB)
    limits: list[int] = []
    if hasattr(resource, "RLIMIT_DATA"):
        limits.append(resource.RLIMIT_DATA)
    if address_space and hasattr(resource, "RLIMIT_AS"):
        limits.append(resource.RLIMIT_AS)
    if not limits:
        return None

    def _apply() -> None:
        for what in limits:
            try:
                _soft, hard = resource.getrlimit(what)
                soft = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
                resource.setrlimit(what, (soft, hard))
            except (ValueError, OSError):
                pass

    return _apply


def container_user_flags() -> list[str]:
    """``--user <uid:gid>`` for a spawned job container — the sandbox's
    container user so bind-mount writes don't land root-owned.
    ``PRECIS_CONTAINER_USER`` (empty/unset ⇒ no flag), e.g. ``"1000:1000"``.
    Operator knob, rendered per host into the worker's env file; on
    rootless podman the mapping also shifts which host uid owns writes.
    """
    spec = os.environ.get("PRECIS_CONTAINER_USER", "").strip()
    return ["--user", spec] if spec else []


def memory_cap_killed(
    returncode: int | None, *, oom_killed: bool | None = None
) -> bool:
    """Whether a spawned child's exit reads as the memory cap's kill.

    ``oom_killed`` is the runtime's ``.State.OOMKilled`` when the caller
    inspected the container — authoritative: the cgroup OOM-killer fired.
    Absent that, exit 137 / signal -9 under a cap is the signature
    (docker/podman OOM-kill delivers SIGKILL to the container's leader).
    It is ambiguous with an external SIGKILL — a deploy bounce, ``docker
    kill``, the executor's own escalate — so the caller weighs context; a
    job's own kill path stamps its reason itself. A bare child that caught
    its failed allocation reports a classified failure instead (pathway's
    "memory limit exceeded" envelope); its launcher folds that into
    :data:`KILLED_FOR_MEMORY` itself.
    """
    if oom_killed is not None:
        return oom_killed
    return returncode in MEMORY_KILL_RETURNCODES


__all__ = [
    "KILLED_FOR_MEMORY",
    "MEMORY_KILL_RETURNCODES",
    "container_limit_flags",
    "container_memory_flags",
    "container_user_flags",
    "job_mem_limit_mb",
    "mem_limit_preexec",
    "memory_cap_killed",
]
