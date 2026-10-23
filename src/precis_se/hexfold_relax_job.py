"""``hexfold_relax`` job_type — relax an assembled hexfold block on the
compute group and store per-seam/per-mouth strain before and after
(hexfold H3, docs/backlog/hexfold-nanoreactor-tube.md). A ``precis_se``
plugin spec (``precis.job_types`` entry point), so the core never
imports the hexfold atomic pipeline it dispatches.

The ``struct_relax`` job relaxes one structure at one rung into the
run-cube; an assembled tunnel wants the pipeline of
docs/backlog/hexfold-relax-pipeline.md instead — a cheap whole-block
polish (``ml``/``emt``/``xtb``, whichever the relax ladder supports —
GFN-FF has no backend in the tree) plus GFN2 on H-capped, buffer-frozen
junction fragments — and its own write-back: the relaxed geometry
becomes the structure's next version, the strain findings land on the
ref's ``meta["generated"]["relax"]``, and every compute pass records a
``struct_runs`` row like any other relax. All of it runs through
:func:`precis_se.atomic.relax_block.relax_block`, which in turn calls
the one ladder, :func:`precis.structure.relax.relax` — same rungs, same
calculators, no second stack.

Minted like every compute-lane job: ``put(kind='job',
job_type='hexfold_relax', executor='ssh_node',
parent_id=<structure ref>, params={'structure_ref_id': N, ...})`` —
``parent_id`` is the bound structure design the job builds on (the
compute lane's owner). ``params.target_node`` pins the claim the same
way ``struct_relax`` does; unset, any ``ssh_node`` worker may claim it —
a node missing tblite (the fragment rung's backend) fails the job
``infra``, naming the extra.

The runner's outer dispatch bound and initial lease read the type's
``wall_seconds`` callable: ``_relax_timeout_s`` is configured on the
claiming host, not stamped into the job's ``params.resources``. Without
that declaration the runner could fail at 7200s while the relaxer's
internal cap was still 14400s, abandoning a live in-process compute.

The compute runs in a supervised child process, not on the dispatch
thread (gr506395): a 5105-atom relax overran its bound in the worker's
daemon thread, the job was failed, and the thread kept ~44 GB of GPU
memory and the worker's shared, non-thread-safe MACE calculator for
hours. The child enforces the cap itself (the runner's outer bound is the
cap plus a margin, so it does not fire first), polls cancel and operator
kill, and is killed with its process group — confirmed gone — before the
job turns terminal. It also gives each job its own MACE calculator; the
cost is a model load per job, small against an hours-long relax.

Step checkpoints (gr508493, Reto 2026-10-23 "Each step snapshot. Do
that."): the child emits the current geometry after every optimiser
step and the parent keeps the latest on the job's
``meta.relax_checkpoint`` slot — one slot, each write overwriting the
last, fragment done-state riding along. A worker restart or deploy
re-claims the same job, which resumes from the checkpoint instead of
starting over (a deploy costs at most one step); on timeout, cancel or
kill the checkpoint becomes the structure's next version with
``converged=false`` and the step count, so a re-step job
(``on_version``) continues from it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from precis.workers.child_compute import (
    ChildDied,
    ChildStopped,
    job_stop_reason,
    run_in_child,
)
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.struct_relax import _relax_timeout_s

log = logging.getLogger(__name__)

#: Supervisor cadence (seconds) for the relax child: deadline / cancel /
#: kill checks, and the SIGTERM→SIGKILL grace.
_CHILD_POLL_S = 5.0
_CHILD_GRACE_S = 30.0

#: The per-job checkpoint POINTER (gr508493, review of 3e6c2ef3e): the
#: checkpoint payload itself is a node-local FILE — ~150 KB per step for
#: a 5k-atom block would be ~1 GB of jsonb/WAL churn over a cone job,
#: and every claim/reclaim/sweeper poll that selects ``refs.meta`` would
#: detoast it. Meta carries only ``{path, sha256, stage, step,
#: base_version, job_id}``, refreshed on a stage change or every
#: :data:`_POINTER_MIN_INTERVAL_S`; the file is still written every
#: optimiser step (tmp → fsync → ``os.replace``), as ruled.
_CHECKPOINT_META_KEY = "relax_checkpoint"
_POINTER_MIN_INTERVAL_S = 60.0


def _ckpt_dir(ref_id: int) -> Path:
    """The job's node-local checkpoint dir. The job pins to its
    ``target_node``, so a reclaim re-dispatches where the file lives;
    a missing file just means a fresh run, never a failure."""
    root = os.environ.get("PRECIS_RELAX_CKPT_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "precis", "hexfold-relax"
    )
    return Path(root) / f"job-{ref_id}"


class _CkptWriter:
    """Child-side per-step checkpoint writer (gr508493).

    Every optimiser step's payload lands on disk through
    tmp → fsync → ``os.replace`` (a torn write can't produce a
    half-file), then a small ``checkpoint`` event carries
    ``{path, sha256, stage, stage_steps}`` to the parent. An exclusive
    lock file held for the whole run makes it impossible for two
    children (a reclaimed job's orphan and its successor) to write the
    same checkpoint.
    """

    def __init__(
        self,
        dir_path: str,
        *,
        job_id: int,
        base_version: int,
        resume_n: int = 0,
    ) -> None:
        import fcntl

        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.path = self._dir / "checkpoint.json"
        self._tmp = self._dir / f"checkpoint.json.{os.getpid()}.tmp"
        self._job_id = job_id
        self._base_version = base_version
        # A resumed run keeps counting upward — a reset to 0 would make
        # its first writes read as OLDER than the prior attempt's
        # pointer and trip the staleness guard.
        self._n = resume_n
        self._lock_fd = os.open(
            self._dir / "checkpoint.json.lock", os.O_RDWR | os.O_CREAT
        )
        # Raises BlockingIOError while another child holds it — the
        # dispatch records infra and the job retries on its next claim
        # rather than two writers racing one file.
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def write(self, payload: dict[str, Any]) -> None:
        """One overwrite: the full payload to the file, a small pointer
        event to the parent."""
        from precis.workers.child_compute import emit

        self._n += 1
        payload = {
            **payload,
            "job_id": self._job_id,
            "base_version": self._base_version,
            "n": self._n,
        }
        blob = json.dumps(payload).encode("utf-8")
        with open(self._tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(self._tmp, self.path)
        emit(
            "checkpoint",
            {
                "v": 1,
                "path": str(self.path),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "stage": payload.get("stage"),
                "stage_steps": payload.get("stage_steps"),
                "fragment": payload.get("fragment"),
                "n": self._n,
            },
        )


_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The bound structure design minted by a hexfold generate — the
        # job relaxes it and writes the strain record onto its
        # meta['generated'].
        "structure_ref_id": {"type": "integer"},
        # The design version to relax (absent ⇒ the live version).
        "on_version": {"type": "integer"},
        # Whole-block polish rung: 'ml' (default, the T1 tier) | 'emt' |
        # 'xtb' | 'geo' | 'clean' — anything the relax ladder accepts.
        "polish": {"type": "string"},
        "model": {"type": ["string", "null"]},
        # Junction-fragment rung: 'xtb' (GFN2, the relaxer of record) or
        # 'none' (polish only).
        "fragment": {"type": "string"},
        "buffer_A": {"type": "number"},
        "core_A": {"type": "number"},
        "steps": {"type": "integer"},
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["structure_ref_id"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Satisfied by EXECUTOR_PROVIDES['ssh_node'] == {'has_gpaw'} — the
#: compute-group marker, same as ``struct_relax``.
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "Relax an assembled hexfold block (whole-block polish + GFN2 junction "
    "fragments) and store per-seam/mouth strain before/after on the scene."
)

#: Polish rungs accepted here, mirrored from relax_block.POLISH_RUNGS —
#: a rung outside the ladder is refused at submit, not on the node.
_POLISH_RUNGS = ("ml", "emt", "xtb", "geo", "clean")
_FRAGMENT_RUNGS = ("xtb", "none")


def _validate_submit(
    store: Any, *, gripe_id: Any = None, params: Any = None
) -> str | None:
    """Refuse a mint the dispatch could only fail: unknown structure, no
    persisted strain bookkeeping, or a rung the ladder doesn't carry."""
    params = params or {}
    ref_id = params.get("structure_ref_id")
    if not isinstance(ref_id, int) or isinstance(ref_id, bool):
        return (
            "hexfold_relax needs params.structure_ref_id — the ref id of "
            "the structure design bound to the hexfold block"
        )
    polish = str(params.get("polish") or "ml")
    if polish not in _POLISH_RUNGS:
        return (
            f"hexfold_relax polish must be one of {list(_POLISH_RUNGS)}; got {polish!r}"
        )
    fragment = str(params.get("fragment") or "xtb")
    if fragment not in _FRAGMENT_RUNGS:
        return (
            f"hexfold_relax fragment must be one of {list(_FRAGMENT_RUNGS)}; "
            f"got {fragment!r}"
        )
    ref = store.get_ref(kind="structure", id=ref_id)
    if ref is None:
        return f"hexfold_relax: no structure ref {ref_id}"
    generated = (ref.meta or {}).get("generated") or {}
    if not generated.get("junctions"):
        return (
            f"structure ref {ref_id} has no generated.junctions "
            "bookkeeping — hexfold_relax scores blocks minted with the "
            "H3 strain record (tunnel-reservoirs scenes, fuse/opening "
            "builds); regenerate it first"
        )
    return None


def _relax_child(
    scene: Any,
    generated: dict[str, Any],
    kwargs: dict[str, Any],
    cuda_requested: bool,
    resume: dict[str, Any] | None,
    ckpt: dict[str, Any],
) -> tuple[Any, Any]:
    """The compute, run in the supervised child: report the MLIP device
    (``ml`` polish only, and only when the record will pass
    ``relax_block``'s own checks, so a bad record still fails as params
    before a model loads), then relax. Returns the relaxed scene with the
    report — the child's mutation of ``scene`` does not reach the parent.

    ``resume`` is the checkpoint a killed attempt left; ``relax_block``
    continues from it (completed stages skipped, the in-flight one on its
    remaining steps budget). Every optimiser step's payload lands on the
    node's checkpoint file through ``ckpt``'s :class:`_CkptWriter`
    (``{dir, job_id, base_version}``), which also emits the small
    ``checkpoint`` pointer event."""
    from precis.workers.child_compute import emit
    from precis_se.atomic.relax_block import relax_block, strain_bookkeeping

    if kwargs["polish"] == "ml" and strain_bookkeeping(generated) is not None:
        from precis.structure.relax import ml_device_check

        device = ml_device_check(
            kwargs["model"] or "mace_mp", cuda_requested=cuda_requested
        )
        if device is not None:
            emit("ml_device", device)
    writer = _CkptWriter(
        str(ckpt["dir"]),
        job_id=int(ckpt["job_id"]),
        base_version=int(ckpt["base_version"]),
        resume_n=int((resume or {}).get("n") or 0),
    )
    report = relax_block(
        scene,
        generated,
        on_checkpoint=writer.write,
        resume=resume,
        **kwargs,
    )
    return scene, report


def _on_child_event(ctx: Any, kind: str, payload: Any) -> None:
    """Record the child's MLIP device on ``meta.ml_device``; a CPU fallback
    also lands as a ``job_event`` warning. ``checkpoint`` events are the
    small file pointers handled by the dispatch loop's throttle, not
    here."""
    if kind != "ml_device":
        return
    ctx.set_meta(ml_device=payload)
    warning = payload.get("warning")
    if warning:
        log.warning("hexfold_relax job %s: %s", ctx.ref_id, warning)
        ctx.append_chunk("job_event", f"hexfold_relax: warning — {warning}")


def _read_ckpt_file(
    path: str, *, job_id: int, base_version: int
) -> dict[str, Any] | None:
    """Parse + verify a checkpoint file: it must decode, name THIS job
    and THIS base version. ``None`` — with a warning — on any mismatch;
    a bad file means a fresh run, never a crash."""
    try:
        payload = json.loads(Path(path).read_bytes())
    except Exception as exc:
        log.warning(
            "hexfold_relax: checkpoint file %s unreadable (%s) — starting fresh",
            path,
            exc,
        )
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        log.warning(
            "hexfold_relax: checkpoint file %s is not a v1 payload — starting fresh",
            path,
        )
        return None
    if int(payload.get("job_id", job_id)) != job_id:
        log.warning(
            "hexfold_relax: checkpoint file %s names job %s, this is job "
            "%s — starting fresh",
            path,
            payload.get("job_id"),
            job_id,
        )
        return None
    if int(payload.get("base_version", -1)) != base_version:
        log.warning(
            "hexfold_relax: checkpoint file %s was taken on base v%s, "
            "this run loads v%s — starting fresh",
            path,
            payload.get("base_version"),
            base_version,
        )
        return None
    return payload


def _resume_from_file(
    job_meta: dict[str, Any], version_loaded: int, job_id: int
) -> dict[str, Any] | None:
    """Same-job reclaim resume: follow the meta pointer to the
    node-local file and return its payload after the job/base/sha
    checks. The pointer lags the file by up to the throttle interval —
    a NEWER file than the pointer is normal (the sha only has to match
    when the write counters agree); an older file is a clobbered write.
    Staleness is judged on the monotonic write counter ``n``, not
    ``stage_steps`` — a step count resets at every stage boundary and
    would wrongly discard a file that just crossed into a new stage."""
    ptr = job_meta.get(_CHECKPOINT_META_KEY)
    if not isinstance(ptr, dict) or int(ptr.get("v") or 0) != 1:
        return None
    if int(ptr.get("base_version", -1)) != version_loaded:
        log.warning(
            "hexfold_relax: %s names base version %s but the job loads "
            "v%s — starting fresh",
            _CHECKPOINT_META_KEY,
            ptr.get("base_version"),
            version_loaded,
        )
        return None
    if int(ptr.get("job_id", job_id)) != job_id:
        return None
    path = ptr.get("path")
    if not path or not os.path.exists(str(path)):
        log.warning(
            "hexfold_relax: checkpoint file %s missing on this node — starting fresh",
            path,
        )
        return None
    payload = _read_ckpt_file(str(path), job_id=job_id, base_version=version_loaded)
    if payload is None:
        return None
    file_n = int(payload.get("n") or 0)
    ptr_n = int(ptr.get("n") or 0)
    if file_n < ptr_n:
        log.warning(
            "hexfold_relax: checkpoint file is older (write %s) than the "
            "meta pointer (write %s) — starting fresh",
            file_n,
            ptr_n,
        )
        return None
    if (
        file_n == ptr_n
        and ptr_n > 0
        and ptr.get("sha256")
        and hashlib.sha256(Path(str(path)).read_bytes()).hexdigest() != ptr["sha256"]
    ):
        log.warning(
            "hexfold_relax: checkpoint file sha256 does not match the "
            "meta pointer — starting fresh"
        )
        return None
    return payload


def _resume_state(
    job_meta: dict[str, Any],
    generated: dict[str, Any],
    version_loaded: int,
    job_id: int,
) -> dict[str, Any] | None:
    """The checkpoint this dispatch continues from, or ``None``.

    Two shapes, one ``relax_block`` payload:

    * **Same-job reclaim** (worker restart/deploy): the job's
      ``meta.relax_checkpoint`` pointer → node-local file.
    * **Re-step job** (``on_version`` naming a checkpoint version): the
      interrupted run's ``resume`` seed on the checkpoint version's
      ``generated.relax`` record — the geometry already IS the
      checkpoint's, so the seed carries no positions.
    """
    payload = _resume_from_file(job_meta, version_loaded, job_id)
    if payload is not None:
        return payload
    record = generated.get("relax")
    if isinstance(record, dict) and record.get("interrupted"):
        seed = record.get("resume")
        if isinstance(seed, dict) and int(seed.get("version", -1)) == version_loaded:
            return dict(seed)
    return None


def _wait_pid_gone(pid: int, timeout_s: float) -> bool:
    """True once ``pid`` no longer exists (or ``timeout_s`` elapses)."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _proc_start_epoch(pid: int) -> float | None:
    """The process's start time as epoch seconds, from /proc (Linux
    worker nodes). ``None`` where /proc is absent (macOS dev) — the
    caller then falls back to the pgid check alone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Field 22 (starttime, jiffies since boot): parse past the
        # comm field's closing ')' — comm may itself contain spaces.
        fields = stat.rsplit(")", 1)[1].split()
        start_jiffies = int(fields[19])
        btime = 0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime"):
                btime = int(line.split()[1])
                break
        if not btime:
            return None
        return btime + start_jiffies / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _proc_identity_matches(pid: int, handle: dict[str, Any]) -> bool:
    """True only when the live ``pid`` really is the child the handle
    recorded — pid reuse must never let the reaper kill an innocent
    (review item B). Checks the recorded process group, then the
    process's real start time against the handle's ``started_at``
    (30 s of slack for clock/boot jitter)."""
    pgid = handle.get("pgid")
    if isinstance(pgid, int) and pgid > 0:
        try:
            if os.getpgid(pid) != pgid:
                return False
        except (ProcessLookupError, PermissionError):
            return False
    started = _proc_start_epoch(pid)
    if started is None:
        return True  # nothing more verifiable on this platform
    try:
        from datetime import datetime

        recorded = datetime.fromisoformat(str(handle.get("started_at"))).timestamp()
    except (ValueError, TypeError):
        return False  # handle names no start time to verify against
    return abs(started - recorded) <= 30.0


def _reap_prior_child(ctx: Any) -> bool:
    """Reap an orphan relax child a dead worker generation left behind
    — ``meta.compute_child`` records its pid/pgid — before a new child
    takes the checkpoint-file lock (review item 6). ``False`` when a
    verified survivor refuses to die: the caller fails infra rather
    than risk two writers on one file (the child's own lock would also
    refuse). A pid that fails the identity check is somebody else's —
    it is left alone (review item B)."""
    handle = (ctx.meta or {}).get("compute_child")
    if not isinstance(handle, dict):
        return True
    if handle.get("stopped") or handle.get("exitcode") is not None:
        return True  # a finished handle, already accounted for
    pid = handle.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    host = str(handle.get("host") or "")
    this_host = os.environ.get("PRECIS_NODE") or socket.gethostname()
    if not host or host != this_host:
        return True  # missing or foreign host — never ours to reap
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True  # already gone — the common path
    except PermissionError:
        pass
    if not _proc_identity_matches(pid, handle):
        ctx.append_chunk(
            "job_event",
            f"hexfold_relax: stale compute_child pid {pid} is a reused "
            "pid, not the orphan — leaving it alone",
        )
        return True
    ctx.append_chunk(
        "job_event",
        f"hexfold_relax: reaping orphaned relax child pid {pid} "
        "from a prior worker generation",
    )
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if _wait_pid_gone(pid, 10.0):
            return True
    ctx.append_chunk(
        "job_event",
        f"hexfold_relax: orphan child pid {pid} survived SIGKILL — "
        "refusing to start a second checkpoint writer",
    )
    return False


def _clear_ckpt(ctx: Any, path: str | None) -> None:
    """Drop the meta pointer and delete the checkpoint file — on a
    clean finish and after a checkpoint was written back as a version."""
    ctx.set_meta(**{_CHECKPOINT_META_KEY: None})
    if path:
        for stale in (str(path), f"{path}.lock"):
            try:
                os.unlink(stale)
            except OSError:
                pass


def _write_checkpoint_version(
    ctx: Any,
    *,
    structure_ref_id: int,
    version_loaded: int,
    polish: str,
    fragment: str,
    checkpoint: dict[str, Any] | None,
    reason: str,
) -> tuple[int, int] | None:
    """The last checkpoint becomes the structure's next version
    (``converged=false`` + step count), so a stopped run's geometry is
    never thrown away (gr508493). Returns ``(version, total_steps)``, or
    ``None`` when there is nothing newer to save.

    The new version is ``structure_version + 1`` — never
    ``version_loaded + 1``: an ``on_version`` job relaxing an OLDER
    design must not collide with versions saved since (review item 3)."""
    if not isinstance(checkpoint, dict):
        return None
    if int(checkpoint.get("base_version", -1)) != version_loaded:
        return None
    positions = checkpoint.get("positions")
    if not isinstance(positions, dict) or not positions:
        return None
    ref = ctx.store.get_ref(kind="structure", id=structure_ref_id)
    if ref is None:
        return None
    generated = (ref.meta or {}).get("generated") or {}
    scene, _handles = ctx.store.structure_load(structure_ref_id, version=version_loaded)
    applied = 0
    for label, frac in positions.items():
        atom = scene.atoms.get(label)
        if atom is None or not isinstance(frac, (list, tuple)) or len(frac) != 3:
            continue
        atom.frac = scene.cell.wrap(np.asarray(frac, dtype=float))
        applied += 1
    if not applied:
        return None
    version = int(ctx.store.structure_version(structure_ref_id)) + 1
    polish_rec = (
        dict(checkpoint["polish"])
        if isinstance(checkpoint.get("polish"), dict)
        else None
    )
    stage = checkpoint.get("stage")
    stage_steps = int(checkpoint.get("stage_steps") or 0)
    fragments = [f for f in (checkpoint.get("fragments") or []) if isinstance(f, dict)]
    total_steps = (
        stage_steps
        + (int(polish_rec.get("n_steps") or 0) if polish_rec else 0)
        + sum(int(f.get("n_steps") or 0) for f in fragments)
    )
    polish_steps = (
        int(polish_rec.get("n_steps") or 0)
        if polish_rec
        else stage_steps
        if stage == "polish"
        else 0
    )
    relax_record = {
        "job_id": int(ctx.ref_id),
        "interrupted": {
            "reason": reason,
            "stage": stage,
            "fragment": checkpoint.get("fragment"),
            "steps": total_steps,
        },
        "polish": {
            "rung": str((polish_rec or {}).get("rung") or polish),
            "model": (polish_rec or {}).get("model"),
            "converged": bool((polish_rec or {}).get("converged"))
            if polish_rec
            else False,
            "n_steps": polish_steps,
            "energy": (polish_rec or {}).get("energy"),
            "max_force": (polish_rec or {}).get("max_force"),
        },
        "fragments": [
            {
                "junction": str(f.get("name")),
                "rung": f.get("rung"),
                "atoms": f.get("atoms"),
                "h_caps": f.get("h_caps"),
                "frozen": f.get("frozen"),
                "converged": bool(f.get("converged")),
                "n_steps": int(f.get("n_steps") or 0),
                "energy": f.get("energy"),
                "max_force": f.get("max_force"),
            }
            for f in fragments
        ],
        "strain": {"before": checkpoint.get("before") or []},
        # The seed a re-step job (on_version=this save) resumes from —
        # the geometry lives in the version itself, so no positions.
        "resume": {
            "version": version,
            "stage": stage,
            "fragment": checkpoint.get("fragment"),
            "stage_steps": stage_steps,
            "curve": checkpoint.get("curve") or [],
            "polish": polish_rec,
            "fragments": fragments,
            "before": checkpoint.get("before") or [],
        },
    }
    generated_next = dict(generated)
    generated_next["relax"] = relax_record
    title = str(ref.title or ref.slug)
    ctx.store.structure_save(
        slug=str(ref.slug),
        title=title,
        scene=scene,
        version=version,
        card_text=(
            f"{title} (atomistic structure). hexfold_relax checkpoint "
            f"({reason}, stopped at step {total_steps}): "
            f"{len(scene.atoms)} atoms, {len(scene.bonds)} bonds."
        ),
        description=str((ref.meta or {}).get("description") or ""),
        relax_summary={
            "rung": f"hexfold:{polish}+{fragment}",
            "converged": False,
            "n_steps": total_steps,
            "energy": (polish_rec or {}).get("energy"),
            "max_force": (polish_rec or {}).get("max_force"),
            "model": (polish_rec or {}).get("model"),
            "interrupted": reason,
        },
        meta_extra={"generated": generated_next},
    )
    # The checkpointed steps cost real compute — record the passes like
    # any other relax run ('interrupted' for the in-flight stage).
    if polish_steps:
        ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=polish,
            on_version=version_loaded,
            converged=bool((polish_rec or {}).get("converged")),
            n_steps=polish_steps,
            max_disp=float((polish_rec or {}).get("max_disp") or 0.0),
            energy=(polish_rec or {}).get("energy"),
            max_force=(polish_rec or {}).get("max_force"),
            model=(polish_rec or {}).get("model"),
            curve=(polish_rec or {}).get("curve")
            if polish_rec
            else checkpoint.get("curve"),
            status="succeeded" if polish_rec else "interrupted",
            params={"job": int(ctx.ref_id), "part": "hexfold_relax:polish"},
        )
    for f in fragments:
        ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=str(f.get("rung") or fragment),
            on_version=version_loaded,
            converged=bool(f.get("converged")),
            n_steps=int(f.get("n_steps") or 0),
            max_disp=float(f.get("max_disp") or 0.0),
            energy=f.get("energy"),
            max_force=f.get("max_force"),
            model=f.get("model"),
            curve=f.get("curve"),
            status="succeeded",
            params={
                "job": int(ctx.ref_id),
                "part": f"hexfold_relax:fragment:{f.get('name')}",
                "atoms": f.get("atoms"),
                "h_caps": f.get("h_caps"),
                "frozen": f.get("frozen"),
            },
        )
    if stage == "fragment" and stage_steps:
        ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=fragment,
            on_version=version_loaded,
            converged=False,
            n_steps=stage_steps,
            max_disp=0.0,
            curve=checkpoint.get("curve"),
            status="interrupted",
            params={
                "job": int(ctx.ref_id),
                "part": f"hexfold_relax:fragment:{checkpoint.get('fragment')}",
            },
        )
    ctx.append_chunk(
        "job_summary",
        f"hexfold_relax stopped at step {total_steps} ({reason}); "
        f"checkpoint written back as version {version} "
        "(converged=false) — resume with put(kind='job', "
        "job_type='hexfold_relax', params={'structure_ref_id': "
        f"{structure_ref_id}, 'on_version': {version}}})",
    )
    return version, total_steps


def _settled_note(saved: tuple[int, int] | None) -> str:
    """The ``stopped at step k, checkpoint vN`` tail the ruling asks the
    terminal record to carry; empty when nothing was checkpointed."""
    if saved is None:
        return ""
    version, total_steps = saved
    return f"; stopped at step {total_steps} — checkpoint saved as version {version}"


def _terminalize_stopped(
    ctx: Any,
    stop: ChildStopped,
    cap: float,
    settle: Callable[[str], tuple[int, int] | None],
) -> None:
    """Terminal state for a child the supervisor stopped. Runs after the
    kill, so the job never reads terminal while its compute still runs; an
    unverified kill is named in the reason. ``settle`` first writes the
    run's last checkpoint back as the structure's next version — the
    partial geometry is never lost (gr508493)."""
    pid = stop.handle.get("pid")
    killed = (
        f"relax process pid {pid} killed"
        if stop.handle.get("kill_verified")
        else f"relax process pid {pid} signalled but NOT confirmed gone "
        "(survived SIGKILL)"
    )
    if stop.reason == "cancel":
        saved = settle("cancel")
        ctx.append_chunk(
            "job_event",
            f"hexfold_relax: cancel requested — {killed}{_settled_note(saved)}",
        )
        ctx.set_status("cancelled")
    elif stop.reason == "kill":
        saved = settle("kill")
        ctx.record_failure(
            f"hexfold_relax: killed by operator — {killed}{_settled_note(saved)}"
        )
    else:
        saved = settle("timeout")
        ctx.record_failure(
            f"hexfold_relax: relax exceeded its {cap:.0f}s cap "
            "(PRECIS_DFT_RELAX_TIMEOUT_S) — " + killed + _settled_note(saved),
            failure_class="timeout",
        )


def _dispatch(ctx: Any, spec: Any) -> None:
    """Load the bound structure, run the relax pipeline in a supervised
    child process, and write the relaxed geometry + strain record back.

    No container and no staging — the rungs compute on the claiming host,
    like ``struct_relax``'s in-process path, but in a child
    (:func:`precis.workers.child_compute.run_in_child`) whose pid lands on
    ``meta.compute_child``. Past the cap, on cancel or on an operator kill
    the child's process group is killed and confirmed gone before the job
    turns terminal. Backend-missing and a child that dies without a result
    record ``infra`` honestly (the geometry is fine; the host is not
    provisioned or the rung is too big), a cap overrun records
    ``timeout``, a record with no junction bookkeeping fails as a params
    error."""
    from precis.structure.relax import RelaxUnsupported

    params = (ctx.meta or {}).get("params") or {}
    try:
        structure_ref_id = int(params["structure_ref_id"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"hexfold_relax: malformed params ({exc})", failure_class="infra"
        )
        return
    polish = str(params.get("polish") or "ml")
    model = params.get("model") or None
    fragment = str(params.get("fragment") or "xtb")
    buffer_A = float(params.get("buffer_A") or 12.0)
    core_A = float(params.get("core_A") or 4.0)
    steps = int(params.get("steps") or 200)
    on_version = params.get("on_version")

    ref = ctx.store.get_ref(kind="structure", id=structure_ref_id)
    if ref is None:
        ctx.record_failure(
            f"hexfold_relax: no structure ref {structure_ref_id}",
            failure_class="infra",
        )
        return
    generated = (ref.meta or {}).get("generated") or {}
    try:
        scene, _handles = ctx.store.structure_load(
            structure_ref_id,
            version=int(on_version) if on_version is not None else None,
        )
    except Exception as exc:
        ctx.record_failure(
            f"hexfold_relax: structure_load failed: {exc}", failure_class="infra"
        )
        return
    loaded_version = int((ref.meta or {}).get("version", 0))
    version_loaded = int(on_version) if on_version is not None else loaded_version
    resume = _resume_state(ctx.meta or {}, generated, version_loaded, int(ctx.ref_id))
    if resume is not None:
        ctx.append_chunk(
            "job_event",
            "hexfold_relax: resuming from checkpoint — stage "
            f"{resume.get('stage')}"
            + (f"/{resume.get('fragment')}" if resume.get("fragment") else "")
            + f", {int(resume.get('stage_steps') or 0)} stage steps done",
        )
    if not _reap_prior_child(ctx):
        ctx.record_failure(
            "hexfold_relax: a prior compute child is still alive — not "
            "starting a second checkpoint writer",
            failure_class="infra",
        )
        return

    cap = _relax_timeout_s()
    ctx.append_chunk(
        "job_event",
        f"hexfold_relax[{polish}+{fragment}] on "
        f"{os.environ.get('PRECIS_NODE') or 'this host'}: "
        f"{len(scene.atoms)} atoms, buffer={buffer_A:g}A core={core_A:g}A "
        f"steps={steps} cap={cap:.0f}s",
    )
    kwargs = {
        "polish": polish,
        "model": model,
        "fragment": fragment,
        "buffer_A": buffer_A,
        "core_A": core_A,
        "steps": steps,
    }
    cuda_requested = bool(((ctx.meta or {}).get("requires") or {}).get("gpu"))
    ckpt_dir = _ckpt_dir(int(ctx.ref_id))
    ckpt = {
        "dir": str(ckpt_dir),
        "job_id": int(ctx.ref_id),
        "base_version": version_loaded,
    }
    latest: dict[str, Any] = {}

    def _event(kind: str, payload: Any) -> None:
        # The child's per-step event is only the small file pointer; the
        # ~150KB payload itself is already on disk (review item 1).
        if kind == "checkpoint" and isinstance(payload, dict):
            ptr = {
                **payload,
                "job_id": int(ctx.ref_id),
                "base_version": version_loaded,
                "step": int(payload.get("stage_steps") or 0),
            }
            latest["ptr"] = ptr
            now = time.monotonic()
            if (
                ptr.get("stage") != latest.get("ptr_stage")
                or now - float(latest.get("ptr_at") or 0.0) >= _POINTER_MIN_INTERVAL_S
                or ptr.get("stage") == "strain"
            ):
                ctx.set_meta(**{_CHECKPOINT_META_KEY: ptr})
                latest["ptr_at"] = now
                latest["ptr_stage"] = ptr.get("stage")
            return
        _on_child_event(ctx, kind, payload)

    def _settle(reason: str) -> tuple[int, int] | None:
        """Save the child's last checkpoint as the next structure
        version; never masks the job's own terminal transition."""
        ptr = latest.get("ptr")
        ckpt_path: str | None = None
        if isinstance(ptr, dict) and ptr.get("path"):
            ckpt_path = str(ptr["path"])
        else:
            # A child killed early may have written its file without
            # the pointer event reaching the parent (SIGKILL races the
            # pipe) — check the slot directly before giving up.
            direct = ckpt_dir / "checkpoint.json"
            if direct.exists():
                ckpt_path = str(direct)
        payload = (
            _read_ckpt_file(
                ckpt_path,
                job_id=int(ctx.ref_id),
                base_version=version_loaded,
            )
            if ckpt_path
            else None
        )
        if payload is None:
            return None
        try:
            out = _write_checkpoint_version(
                ctx,
                structure_ref_id=structure_ref_id,
                version_loaded=version_loaded,
                polish=polish,
                fragment=fragment,
                checkpoint=payload,
                reason=reason,
            )
        except Exception:  # pragma: no cover - defensive
            log.warning("hexfold_relax: checkpoint write-back raised", exc_info=True)
            return None
        if out is not None:
            _clear_ckpt(ctx, ckpt_path)
        return out

    try:
        scene, report = run_in_child(
            _relax_child,
            (scene, generated, kwargs, cuda_requested, resume, ckpt),
            label=f"hexfold_relax job {ctx.ref_id}",
            timeout_s=cap,
            should_stop=lambda: job_stop_reason(ctx),
            on_start=lambda handle: ctx.set_meta(compute_child=handle),
            on_event=_event,
            on_end=lambda handle: ctx.set_meta(compute_child=handle),
            poll_s=_CHILD_POLL_S,
            grace_s=_CHILD_GRACE_S,
        )
    except ChildStopped as stop:
        _terminalize_stopped(ctx, stop, cap, _settle)
        return
    except ChildDied as exc:
        code = exc.handle.get("exitcode")
        ctx.record_failure(
            f"hexfold_relax: relax process {exc} — no result"
            + _settled_note(_settle("child died")),
            failure_class="infra",
            open_tag=(
                "infra:child-killed" if isinstance(code, int) and code < 0 else None
            ),
        )
        return
    except RelaxUnsupported as exc:
        # Backend/params/pipeline failures are not kills — the partial
        # geometry is not written back (review item 8).
        ctx.record_failure(
            f"hexfold_relax: a rung has no backend on this host ({exc}) — "
            "install the extra that carries it on the node ([dft-ml] for "
            "'ml', [estimate] for 'xtb')",
            failure_class="infra",
        )
        return
    except ValueError as exc:
        ctx.record_failure(f"hexfold_relax: {exc}", failure_class="params")
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("hexfold_relax: relax pipeline raised", exc_info=True)
        ctx.record_failure(
            f"hexfold_relax: relax pipeline failed: {exc}",
            failure_class="infra",
        )
        return

    # Write-back: the relaxed geometry is the scene's next version; the
    # before/after strain lands on the generated record; each compute
    # pass is a struct_runs row like any other relax. The new version
    # is live+1 — an on_version job relaxing an older design appends
    # above everything saved since, never colliding (gr508967).
    version = int(ctx.store.structure_version(structure_ref_id)) + 1
    relax_record = {
        "job_id": int(ctx.ref_id),
        "polish": {
            "rung": report.polish.rung,
            "model": report.polish.model,
            "converged": report.polish.converged,
            "n_steps": report.polish.n_steps,
            "energy": report.polish.energy,
            "max_force": report.polish.max_force,
        },
        "fragments": [
            {
                "junction": f.name,
                "rung": f.result.rung,
                "atoms": f.atoms,
                "h_caps": f.h_caps,
                "frozen": f.frozen,
                "converged": f.result.converged,
                "n_steps": f.result.n_steps,
                "energy": f.result.energy,
                "max_force": f.result.max_force,
            }
            for f in report.fragments
        ],
        "strain": {"before": report.before, "after": report.after},
    }
    generated_next = dict(generated)
    generated_next["relax"] = relax_record
    title = str(ref.title or ref.slug)
    ctx.store.structure_save(
        slug=str(ref.slug),
        title=title,
        scene=scene,
        version=version,
        card_text=(
            f"{title} (atomistic structure). hexfold_relax "
            f"polish={polish} fragment={fragment}: {len(scene.atoms)} "
            f"atoms, {len(scene.bonds)} bonds."
        ),
        description=str((ref.meta or {}).get("description") or ""),
        relax_summary={
            "rung": f"hexfold:{polish}+{fragment}",
            "converged": report.polish.converged
            and all(f.result.converged for f in report.fragments),
            "n_steps": report.polish.n_steps
            + sum(f.result.n_steps for f in report.fragments),
            "max_disp": report.polish.max_disp,
            "energy": report.polish.energy,
            "max_force": report.polish.max_force,
            "model": report.polish.model,
        },
        meta_extra={"generated": generated_next},
    )
    ctx.store.structure_record_run(
        structure_ref_id,
        fidelity=polish,
        on_version=version_loaded,
        converged=report.polish.converged,
        n_steps=report.polish.n_steps,
        max_disp=report.polish.max_disp,
        energy=report.polish.energy,
        max_force=report.polish.max_force,
        model=report.polish.model,
        curve=report.polish.curve,
        params={"job": int(ctx.ref_id), "part": "hexfold_relax:polish"},
    )
    for f in report.fragments:
        ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=f.result.rung,
            on_version=version_loaded,
            converged=f.result.converged,
            n_steps=f.result.n_steps,
            max_disp=f.result.max_disp,
            energy=f.result.energy,
            max_force=f.result.max_force,
            model=f.result.model,
            curve=f.result.curve,
            params={
                "job": int(ctx.ref_id),
                "part": f"hexfold_relax:fragment:{f.name}",
                "atoms": f.atoms,
                "h_caps": f.h_caps,
                "frozen": f.frozen,
            },
        )
    ctx.set_meta(
        hexfold_relax={
            "structure_ref_id": structure_ref_id,
            "version": version,
            "polish": polish,
            "fragment": fragment,
        }
    )
    n_after = len(report.after)
    ctx.append_chunk(
        "job_summary",
        f"hexfold_relax[{polish}+{fragment}] → version {version}: polish "
        f"{'converged' if report.polish.converged else 'NOT converged'} in "
        f"{report.polish.n_steps} steps"
        + (
            f"; {len(report.fragments)} junction fragment(s) relaxed "
            f"({sum(1 for f in report.fragments if f.result.converged)} converged)"
            if report.fragments
            else ""
        )
        + f"; {n_after} strain finding(s) re-scored on the relaxed atoms — "
        "before/after on the ref's meta.generated.relax.",
    )
    # Clear before the terminal transition: a sweeper that sees a
    # succeeded job must not find a live pointer or a stale child
    # handle (review items B and C).
    _ptr = latest.get("ptr")
    _clear_ckpt(
        ctx,
        str(_ptr["path"]) if isinstance(_ptr, dict) and _ptr.get("path") else None,
    )
    ctx.set_meta(compute_child=None)
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="hexfold_relax",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    validate_submit=_validate_submit,
    wall_seconds=_relax_timeout_s,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
