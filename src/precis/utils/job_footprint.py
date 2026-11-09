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

The measurement is a *process* rusage: only the worker that claimed the
job can see it, so this registry is in-process memory, never the row.
``begin`` opens a segment at every claim (fresh, yield re-claim, crash
reclaim alike — a re-claim measures only what it can see from there);
``fold`` closes the open segment into a per-job accumulator when the job
leaves ``STATUS:running`` (checkpoint yield / park / requeue — never on
``cancel_requested``, a request made while still running); ``take``
folds any open segment and drains the accumulator at the terminal stamp.

Honesty of the numbers:

- ``cpu_s`` sums (utime+stime) deltas of ``RUSAGE_SELF`` +
  ``RUSAGE_CHILDREN`` over the segments this process ran — covers both
  child-process lanes (``claude -p``) and in-proc lanes
  (``embed_batch``). Upper bound: unrelated work the same worker did
  inside a segment counts too (cheap to run, wrong only upward).
- ``ru_maxrss_mb`` is a process-lifetime max per rusage scope and cannot
  be diffed; a segment reads the scope it actually exercised (children
  when a reaped child consumed CPU inside it, else self). An upper
  bound for the job's own peak whenever an earlier job on the same
  scope peaked higher — conservative for the admission maths.
  cgroup ``memory.peak`` and per-run VRAM are the follow-up steps in
  the same order (gr595537+); until then this is the best number the
  job path can produce.
- ``wall_s`` is stamped in ``_stamp_job_completion`` from
  ``meta.started_at`` — identical to the existing ``meta.wall_s`` stamp,
  so the two never disagree (it spans parked/yielded gaps).
"""

from __future__ import annotations

import sys
from typing import Any

#: Open rusage segment per claimed job: ``ref_id -> (self, children)``
#: ``resource.struct_rusage`` pair taken at claim.
_RUN: dict[int, tuple[Any, Any]] = {}

#: Accumulated closed segments per job: ``{cpu_s: float, rss: raw}`` —
#: cpu sums, rss keeps the max native-unit ``ru_maxrss`` reading.
_ACCUM: dict[int, dict[str, float]] = {}


def _snapshot() -> tuple[Any, Any] | None:
    """``(RUSAGE_SELF, RUSAGE_CHILDREN)`` pair, or ``None`` off Unix.

    Lazy import keeps this module importable on a platform without
    ``resource``; every worker host is Linux/macOS so the ``None``
    branch is defence, not a lane.
    """
    try:
        import resource

        return (
            resource.getrusage(resource.RUSAGE_SELF),
            resource.getrusage(resource.RUSAGE_CHILDREN),
        )
    except Exception:  # pragma: no cover — non-Unix / sandboxed rusage
        return None


def _cpu_s(*rus: Any) -> float:
    """utime+stime summed over the given rusage scopes."""
    return float(sum(ru.ru_utime + ru.ru_stime for ru in rus))


def ru_maxrss_mb(raw: float) -> float:
    """``ru_maxrss`` native units → MiB (KiB on Linux, bytes on macOS)."""
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def begin(ref_id: int) -> None:
    """Open a run segment for ``ref_id`` — called on every claim.

    A no-op when a segment is already open: a same-process lease-expiry
    reclaim must not reset the baseline of a job that never stopped
    running (a yield/park fold already closed its segment, so a genuine
    re-claim still re-baselines here).
    """
    if ref_id in _RUN:
        return
    snap = _snapshot()
    if snap is not None:
        _RUN[ref_id] = snap


def fold(ref_id: int) -> None:
    """Close the open segment into the accumulator, if any.

    Called when the job leaves ``STATUS:running`` short of terminal —
    a checkpoint yield, a park, a requeue — and inside :func:`take` for
    the terminal close. No-op when the job has no open segment.
    """
    seg = _RUN.pop(ref_id, None)
    if seg is None:
        return
    snap = _snapshot()
    if snap is None:
        return
    self0, child0 = seg
    self1, child1 = snap
    acc = _ACCUM.setdefault(ref_id, {"cpu_s": 0.0, "rss": 0.0})
    acc["cpu_s"] += _cpu_s(self1, child1) - _cpu_s(self0, child0)
    # ru_maxrss is an undiffable lifetime max per scope — read the scope
    # the segment actually exercised: children when a reaped child spent
    # CPU inside it (claude -p, an ssh poll), else self (in-proc work
    # like embed_batch).
    rss = child1.ru_maxrss if _cpu_s(child1) > _cpu_s(child0) else self1.ru_maxrss
    acc["rss"] = max(acc["rss"], float(rss))


def take(ref_id: int) -> dict[str, float | None]:
    """Fold any open segment, drain the accumulator, report the run.

    Called by the terminal stamp; returns ``None``-valued fields when
    this process never claimed the job (a swept orphan from a dead
    worker, a queued row cancelled before it ran).
    """
    fold(ref_id)
    acc = _ACCUM.pop(ref_id, None)
    if acc is None:
        return {"ru_maxrss_mb": None, "cpu_s": None}
    return {
        "ru_maxrss_mb": round(ru_maxrss_mb(acc["rss"]), 1),
        "cpu_s": round(acc["cpu_s"], 1),
    }


def _reset() -> None:
    """Clear both registries — test isolation only."""
    _RUN.clear()
    _ACCUM.clear()


__all__ = ["begin", "fold", "ru_maxrss_mb", "take"]
