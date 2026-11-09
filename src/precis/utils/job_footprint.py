"""Per-job run footprint tracking (gr595536).

Every terminal ``STATUS:`` transition on a ``kind='job'`` ref stamps
``meta.footprint = {ru_maxrss_mb, wall_s, cpu_s}`` — the measured input
the footprint-admission table derives from
(``docs/backlog/cluster-scheduling.md`` Pillar 3b). The write side lives
in :func:`precis.store._tags_ops._stamp_job_completion`, the one funnel
every terminal path already passes through (executor success/fail/
cancel, the sweeper's reaps, a manual ``precis tag``), so a job
terminalised by a process that never claimed it still stamps — with the
unmeasurable fields null.

The measurement is scoped to *the job's execution*, not the worker:
a worker runs many jobs (``PRECIS_INPROC_CONCURRENCY`` pool threads,
sequential passes), so a worker-wide ``RUSAGE_SELF``/``RUSAGE_CHILDREN``
delta lets concurrent jobs count each other's CPU and lets a small job
inherit an earlier job's lifetime ``ru_maxrss`` peak — the review
findings this registry now answers (td611679). ``begin`` opens a segment
at every claim (fresh, yield re-claim, crash reclaim alike) baselined on
the calling thread; ``bind`` re-baselines it on the job's *execution*
thread at dispatch (claim and dispatch are different threads on the
pooled lanes, the same thread on synchronous ones) and marks this thread
so children reaped here attribute to this job; ``fold`` closes the open
segment into a per-job accumulator when the job leaves ``STATUS:running``
(checkpoint yield / park / requeue — never on ``cancel_requested``, a
request made while still running); ``take`` folds any open segment and
drains the accumulator at the terminal stamp.

Honesty of the numbers:

- ``cpu_s`` = the job's execution-thread CPU (``time.thread_time``
  deltas over the segments that thread ran — a fold on a *different*
  thread contributes nothing, another thread's counter is unreadable)
  PLUS each measured child's ``ru_utime + ru_stime``. Children are
  measured per-process: :class:`MeasuredPopen` reaps via ``os.wait4``
  and reports the child's own rusage — including its reaped descendants'
  — through :func:`record_child_rusage`, which attributes it to the job
  bound on the reaping thread. A child reaped with no job bound stays
  unattributed: nothing outside the job's execution can leak in.
- ``ru_maxrss_mb`` = max of (a) measured children's ``ru_maxrss`` —
  an exact per-process peak, so a small job never inherits an earlier
  job's high-water — and (b) the segment's *resident delta*: current
  RSS at fold minus RSS at baseline (the baseline follows a glibc
  ``malloc_trim``, so an earlier job's freed arenas don't inflate it).
  An in-proc transient peak that frees before the fold is still
  unobserved — cgroup ``memory.peak`` and per-run VRAM remain the
  follow-ups (gr595537+); until then this is the best number the job
  path can produce without cross-job inheritance.
- ``wall_s`` is stamped in ``_stamp_job_completion`` from
  ``meta.started_at`` — identical to the existing ``meta.wall_s`` stamp,
  so the two never disagree (it spans parked/yielded gaps).

Detached lanes (``claude_docker``, ``agent_container``, ``devin_api``,
``ssh_node``'s submit/poll path) run the compute off-process — remote
or in a container — and never ``bind``; their measured fields stamp
null rather than attributing thin launch/poll overhead as the job's
footprint.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

_MIB = 1024.0 * 1024.0


@dataclass(slots=True)
class _Segment:
    """One open run segment's baseline.

    ``thread`` is the thread the segment is bound to — only a fold on
    THAT thread can close the CPU delta (``time.thread_time`` reads the
    calling thread). ``rss0`` is the process's resident set at baseline;
    the fold's ``cur - rss0`` is the job's net retained memory over the
    segment — process-wide, never a lifetime max.
    """

    thread: int
    cpu0: float
    rss0: float | None


@dataclass(slots=True)
class _Accum:
    """Closed measurements per job (bytes for RSS; seconds for CPU)."""

    cpu_s: float = 0.0  # execution-thread CPU summed over closed segments
    child_cpu_s: float = 0.0  # reaped children's utime+stime
    child_rss_b: float = 0.0  # max reaped-child ru_maxrss, bytes
    self_rss_b: float = 0.0  # max resident delta over segments, bytes
    measured_cpu: bool = False
    measured_rss: bool = False


#: Open rusage segment per claimed job: ``ref_id -> _Segment``.
_RUN: dict[int, _Segment] = {}

#: Accumulated closed segments per job.
_ACCUM: dict[int, _Accum] = {}

#: The ref_id this thread is currently executing, set by :func:`bind` —
#: how a reaped child (:func:`record_child_rusage`) knows which job it
#: belongs to. Per-thread by construction (mirrors
#: :mod:`precis.utils.inproc_context`), so concurrent jobs on different
#: pool threads never cross-attribute.
_BOUND: ContextVar[int | None] = ContextVar("precis_job_footprint", default=None)


def _baseline() -> _Segment:
    return _Segment(
        thread=threading.get_ident(),
        cpu0=time.thread_time(),
        rss0=_self_rss_bytes(),
    )


def _self_rss_bytes() -> float | None:
    """This process's *current* resident set in bytes, or ``None``.

    Linux ``/proc/self/status`` ``VmRSS`` (KiB) first — a file read, no
    subprocess; ``ps -o rss= -p`` (KiB on both macOS and Linux) as the
    non-Linux fallback. Deliberately the live resident size, never the
    ``ru_maxrss`` lifetime high-water: the high-water is what made a
    small job inherit an earlier job's peak.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) * 1024.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        res = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(res.stdout.strip()) * 1024.0
    except ValueError:
        return None


def _malloc_trim() -> None:
    """Hand freed heap arenas back to the OS (glibc ``malloc_trim``).

    Called at :func:`bind` so the segment's resident baseline is the
    worker's real set, not a previous job's freed-but-retained heap —
    the delta the fold computes then belongs to this job. No-op off
    glibc and on any libc that lacks the symbol.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # pragma: no cover — non-glibc / locked-down ctypes
        pass


def ru_maxrss_mb(raw: float) -> float:
    """``ru_maxrss`` native units → MiB (KiB on Linux, bytes on macOS)."""
    return raw / _MIB if sys.platform == "darwin" else raw / 1024.0


def begin(ref_id: int) -> None:
    """Open a run segment for ``ref_id`` — called on every claim.

    The segment baselines on the calling thread; on the pooled lanes the
    dispatch thread :func:`bind`\\ s later and re-baselines (the claim
    thread's interim CPU is claim-loop overhead, not this job's).

    A no-op when a segment is already open: a same-process lease-expiry
    reclaim must not reset the baseline of a job that never stopped
    running (a yield/park fold already closed its segment, so a genuine
    re-claim still re-baselines here).
    """
    if ref_id in _RUN:
        return
    _RUN[ref_id] = _baseline()


def bind(ref_id: int) -> None:
    """Bind the calling thread to ``ref_id`` for its dispatch.

    The executor calls this at the top of the job's execution so the
    segment baselines on the thread that actually runs the work and so
    children reaped here (``MeasuredPopen`` / ``record_child_rusage``)
    attribute to this job. Re-baselines any open segment — safe to call
    on a re-claim or a second segment on the same thread.
    """
    _malloc_trim()
    _BOUND.set(ref_id)
    _RUN[ref_id] = _baseline()
    _ACCUM.setdefault(ref_id, _Accum())


def unbind() -> None:
    """Detach the calling thread from its bound job (dispatch end)."""
    _BOUND.set(None)


def record_child_rusage(ru: Any) -> None:
    """Attribute a reaped child's ``resource.struct_rusage`` to the job
    bound on THIS thread.

    ``ru_utime + ru_stime`` carries the child's reaped descendants
    already (the kernel folds them in at reap); ``ru_maxrss`` is that
    child's own peak — an exact per-process number, the answer to
    "small job inherits earlier jobs' RSS peaks". No-op with no job
    bound: the child then stays unattributed rather than landing on the
    wrong job.
    """
    ref_id = _BOUND.get()
    if ref_id is None:
        return
    acc = _ACCUM.setdefault(ref_id, _Accum())
    acc.child_cpu_s += float(ru.ru_utime + ru.ru_stime)
    rss_b = float(ru.ru_maxrss)
    if sys.platform != "darwin":
        rss_b *= 1024.0  # ru_maxrss is KiB on Linux, bytes on macOS
    acc.child_rss_b = max(acc.child_rss_b, rss_b)
    acc.measured_cpu = acc.measured_rss = True


def fold(ref_id: int) -> None:
    """Close the open segment into the accumulator, if any.

    Called when the job leaves ``STATUS:running`` short of terminal —
    a checkpoint yield, a park, a requeue — and inside :func:`take` for
    the terminal close. Only a fold on the segment's own thread counts
    the CPU delta (a foreign thread's ``thread_time`` says nothing about
    this job) and the resident delta (a foreign close spans work it
    can't attribute); the segment closes either way. No-op when the job
    has no open segment.
    """
    seg = _RUN.pop(ref_id, None)
    if seg is None:
        return
    if seg.thread != threading.get_ident():
        return
    acc = _ACCUM.setdefault(ref_id, _Accum())
    acc.cpu_s += max(0.0, time.thread_time() - seg.cpu0)
    acc.measured_cpu = True
    if seg.rss0 is not None:
        cur = _self_rss_bytes()
        if cur is not None:
            acc.self_rss_b = max(acc.self_rss_b, max(0.0, cur - seg.rss0))
            acc.measured_rss = True


def take(ref_id: int) -> dict[str, float | None]:
    """Fold any open segment, drain the accumulator, report the run.

    Called by the terminal stamp; returns ``None``-valued fields when
    this process never claimed the job (a swept orphan from a dead
    worker, a queued row cancelled before it ran) and per-field nulls
    when the job was claimed but nothing measurable ran on it (a
    detached lane that never bound, a segment closed on a foreign
    thread).
    """
    fold(ref_id)
    acc = _ACCUM.pop(ref_id, None)
    if acc is None:
        return {"ru_maxrss_mb": None, "cpu_s": None}
    rss_b = max(acc.child_rss_b, acc.self_rss_b)
    return {
        "ru_maxrss_mb": round(rss_b / _MIB, 1) if acc.measured_rss else None,
        "cpu_s": round(acc.cpu_s + acc.child_cpu_s, 1) if acc.measured_cpu else None,
    }


class MeasuredPopen(subprocess.Popen):
    """``subprocess.Popen`` whose reap captures the child's own rusage.

    ``wait``/``communicate``/``poll`` all ride :meth:`_try_wait`; this
    override prefers ``os.wait4`` — the only stdlib path to a dead
    child's ``struct_rusage`` — stashes it on :attr:`rusage`, and
    attributes it to the job bound on the reaping thread. Falls back to
    the plain ``waitpid`` path where ``wait4`` is unavailable or the
    child was already reaped.

    Removed from ``subprocess._active`` at construction: ``_cleanup``
    polls every tracked Popen from whichever thread next spawns one, so
    a foreign ``Popen()`` could otherwise win this child's reap and eat
    its rusage (or misattribute it onto that thread's bound job).
    """

    #: ``resource.struct_rusage`` captured at reap, else ``None``.
    rusage: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            getattr(subprocess, "_active", []).remove(self)
        except (AttributeError, ValueError):
            pass

    def _try_wait(self, wait_flags: int) -> tuple[int, int]:
        """``wait4``-backed ``_try_wait`` — same ``(pid, sts)`` contract.

        The callers (``_wait``/``_internal_poll``) run
        ``_handle_exitstatus`` on a matching pid exactly as with
        ``waitpid``; on top of that we keep the child's rusage and
        attribute it to the bound job.
        """
        try:
            pid, sts, ru = os.wait4(self.pid, wait_flags)
        except (AttributeError, ChildProcessError):
            sup: Any = super()
            return cast(tuple[int, int], sup._try_wait(wait_flags))
        if pid == self.pid:
            self.rusage = ru
            try:
                record_child_rusage(ru)
            except Exception:  # pragma: no cover — never break a reap
                pass
        return (pid, sts)


def _reset() -> None:
    """Clear both registries and the thread binding — test isolation only."""
    _RUN.clear()
    _ACCUM.clear()
    _BOUND.set(None)


__all__ = [
    "MeasuredPopen",
    "begin",
    "bind",
    "fold",
    "record_child_rusage",
    "ru_maxrss_mb",
    "take",
    "unbind",
]
