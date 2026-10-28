"""Detached-seed scratch-dir plumbing — log tail, cleanup, wall-clock kill.

Deliberately free of any ``autocatpath`` import: the ``ssh_node`` kill/reap
hooks (``seed_job._kill`` / ``_reap``) and the completion-stamp reaper run on
hosts that lack the catalyst extra, and the dir removal must still happen
there. :mod:`precis_pathway.runner` re-exports these names.
"""

from __future__ import annotations

import logging

from .types import DetachedHandle

log = logging.getLogger("precis_pathway.runner")


def _tail_logs(scratch_dir: str, limit: int = 4000) -> str:
    """Tail of the detached child's captured streams — the diagnosis a
    blocking run gets for free from ``subprocess.run``'s ``capture_output``
    (:func:`run_seed_partial_subprocess`), reconstructed here from the files
    :func:`submit_seed_partial_detached` redirected the child into (a
    detached ``Popen`` can't be waited on for output).

    The budget is split PER STREAM and each is labelled, rather than tailing
    one concatenation of both. Concatenating and slicing once looks
    equivalent and isn't: stderr carries the traceback and the import
    warnings, stdout carries hours of ``print()`` progress, and a single
    trailing window over ``stderr + stdout`` returns the end of *stdout*
    only — silently dropping the very traceback that says why the run died.
    That was latent while stdout was always empty (``_child_cmd`` ran the
    child buffered, so nothing survived a kill); making the child unbuffered
    is exactly what would have armed it.

    ``limit`` bounds the log CONTENT, not the returned string: each stream's
    label adds a few dozen chars on top. Callers that re-truncate to a hard
    chunk budget may therefore clip a leading label — cosmetic, since every
    stream was already sliced to its own share before the labels went on.
    """
    import os

    streams: list[tuple[str, str]] = []
    for name in ("stderr.log", "stdout.log"):
        path = os.path.join(scratch_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if text.strip():
            streams.append((name, text))
    if not streams:
        return ""
    per = max(1, limit // len(streams))
    return "\n".join(
        f"--- {name} (last {per} chars) ---\n{text[-per:]}" for name, text in streams
    )


def _cleanup_detached(scratch_dir: str, *, failed: bool = False) -> None:
    """Best-effort scratch-dir removal — a detached submit's dir is a bare
    ``tempfile.mkdtemp`` (not context-managed, unlike the blocking sibling's
    ``TemporaryDirectory``), so nothing else reclaims it. Only resolved
    immediate children of the temporary root with the submit's
    ``autocatpath-seed-`` prefix are deletable; traversal paths and a symlink
    final component are refused.

    Set ``PRECIS_PATHWAY_KEEP_FAILED_SCRATCH=1`` to RETAIN the dir of a run
    that failed, so its full ``stdout.log`` / ``stderr.log`` outlive the
    terse tail lifted into the job event. Off by default deliberately: these
    dirs live in ``/tmp`` on a compute node, and a job type that can fail
    ~100 times a week would turn unconditional retention into a disk-filling
    failure mode of its own — trading one silent outage for another. Turn it
    on while actively diagnosing, off the rest of the time.
    """
    import os
    import shutil
    import tempfile

    resolved = os.path.realpath(scratch_dir)
    temp_root = os.path.realpath(tempfile.gettempdir())
    if (
        ".." in scratch_dir.split(os.sep)
        or os.path.islink(scratch_dir)
        or os.path.join(
            os.path.realpath(os.path.dirname(scratch_dir)),
            os.path.basename(scratch_dir),
        )
        != resolved
        or os.path.dirname(resolved) != temp_root
        or not os.path.basename(resolved).startswith("autocatpath-seed-")
    ):
        log.warning(
            "autocatpath_seed: refusing unsafe scratch directory %r", scratch_dir
        )
        return
    if failed and os.environ.get("PRECIS_PATHWAY_KEEP_FAILED_SCRATCH") == "1":
        log.warning(
            "autocatpath_seed: retaining failed scratch dir %s "
            "(PRECIS_PATHWAY_KEEP_FAILED_SCRATCH=1)",
            scratch_dir,
        )
        return
    shutil.rmtree(resolved, ignore_errors=True)


def finalize_seed_partial_detached(
    handle: DetachedHandle, *, failed: bool = False
) -> None:
    """Remove a DONE job's scratch dir — call ONLY after persisting.

    The ``"done"`` branch of :func:`poll_seed_partial_detached` deliberately
    leaves the scratch dir (and its ``result.json`` envelope) on disk so
    that a failed DB persist retries on the next poll instead of discarding
    a finished run. Once the caller's persist has succeeded, this reclaims
    the disk. Best-effort and idempotent (missing dir is a no-op).

    ``failed=True`` reclaims the dir of a failed/killed run under
    ``PRECIS_PATHWAY_KEEP_FAILED_SCRATCH`` rules (see
    :func:`_cleanup_detached`); ``seed_job`` calls it only once the job's
    completion stamp has landed (gr496403).
    """
    _cleanup_detached(handle["dir"], failed=failed)


def kill_seed_partial_detached(
    handle: DetachedHandle, *, keep_scratch: bool = False
) -> bool:
    """Best-effort SIGKILL of a detached submit's whole process group — the
    ssh_node wall-clock kill hook (§H piece 2), invoked once
    ``meta.deadline`` passes. ``os.killpg`` reaches every descendant the
    child may have spawned (MACE/CUDA workers), not just its own PID — the
    same reason :func:`submit_seed_partial_detached` starts a new session.
    Guards ``ProcessLookupError``/``PermissionError`` (already dead is the
    common case: this only fires once the compute has overrun its own wall
    budget by a full margin) and removes the scratch dir regardless of
    whether the kill landed. Also reaps the killed PID (``os.waitpid``,
    best-effort) when this happens to be the process that submitted it —
    same zombie concern :func:`_process_alive` explains: a killed child
    that's never reaped would otherwise sit defunct in this worker process
    for the rest of its life. Unsafe or missing process groups return False
    without signalling, waiting or deleting scratch; accepted groups return
    True (the signal remains best-effort)."""
    import os
    import signal

    value = handle.get("pgid")
    try:
        pgid = int(value) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        pgid = 0
    if pgid <= 1 or pgid == os.getpgrp():
        log.warning("autocatpath_seed: refusing unsafe process group %r", value)
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    pid = handle.get("pid")
    if pid is not None:
        try:
            os.waitpid(int(pid), 0)
        except (ChildProcessError, OSError):
            pass
    scratch = handle.get("dir")
    if scratch:
        # Log the tail before wiping: the executor terminalizes from its
        # own side, so unlike the poll branches there is no channel to
        # hand the child's output back on —
        # and it deleted the dir without ever reading it. Every wall-clock
        # kill therefore explained itself with nothing but the handle dict.
        # SIGKILL leaves no in-process buffer to flush, but with an
        # unbuffered child (:func:`_child_cmd`) the progress is already ON
        # DISK, so there is now something real to capture here.
        tail = _tail_logs(scratch)
        if tail:
            log.warning(
                "autocatpath_seed: wall-clock kill of pid=%s — child output:\n%s",
                handle.get("pid"),
                tail,
            )
        if not keep_scratch:
            _cleanup_detached(scratch, failed=True)
    return True
