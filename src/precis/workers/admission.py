"""The single admission entry point (gr647996, load-based scheduling).

:func:`admit` reserves a whole :class:`Footprint` on one host or nothing;
:func:`release` gives it back. Two callers shapes sit on top:
:func:`spawn` — the one admitted detached launcher — admits, launches and
babysits a child process (gr657245), and :func:`hold` brackets one
synchronous heavy call. S2 (gr657245) routes the builder-editable launch
sites (the run_slice/land_slice/dep_check/lane_health/boundary_probe
detached launcher, the codex/claude subprocess runners, the moot test
probe) through them; the release and gate tooling and the claim path are
deliberately still outside (S3 / Reto's patch). Fresh run_slice claims use
:func:`launch_fits` as a preflight only; spawn retains hold ownership.

**Lock order.** Every multi-row writer of ``resource_slots`` (this
reserve, the heartbeat's capability and capacity syncs, release) touches
rows in :func:`precis.store._resource_slots_ops.lock_order`, sorted by
resource name. One global order means a multi-resource :func:`admit` and
a concurrent heartbeat resync can block each other but never deadlock.

**Hold TTLs.** A hold lives ``ttl_s`` from admission; a long-running
:func:`spawn` child renews its holds on schedule
(:func:`precis.store._resource_slots_ops.renew_slot_holds`) so the
heartbeat sweep never refunds a live launch, and the babysit releases at
the child's exit — the exit check comes first, so a finished child frees
its footprint promptly instead of holding it to the TTL (review
td702053). A synchronous :func:`hold` sizes the TTL from the predicted
``wall_s`` and releases at block exit; the sweep is only the crash
backstop.

**Spawned holds outlive the launcher.** A :func:`spawn` child is
detached: it survives the launching worker's shutdown and restart, so
its holds carry NO ``holder_identity`` — the reaper's epoch arm must not
refund them the moment the worker's generation is replaced while the
child still runs, and the restarted worker's babysit (gone with the old
process) cannot renew them anyway. TTL-only reclaim preserves the
accounting through the child's exit: the babysit renews while the parent
lives; once the parent is gone the last renewed TTL is the bound, the
same crash-backstop discipline as every other hold. A synchronous
:func:`hold`'s holder IS the worker, so its holds keep the identity —
epoch reclaim on restart is correct there.

**A job's spawn holds follow the job (gr762788).** A launch made for a
job (``spawn(job_id=…)``) is held as :func:`job_holder` —
``job:<job_id>``, then ``job:<job_id>:<pgid>:<ttl_s>`` once the child
exists — never the launching worker's ``host:pid``, so the worker-exit
hook (``local_serving.release_process_holds``) cannot refund a
footprint whose child still runs. Whichever worker polls the job on this
host (the launcher, or the one that re-adopted it after a restart) calls
:func:`tend_job_holds` each pass: a hold whose child group is gone is
released at once, a live one renewed to its footprint's own TTL while
the job runs. A terminal job's remaining holds — a child that may still
be exiting — wait out the orphan reaper's grace
(``_common.ORPHAN_REAP_GRACE_S``), then the heartbeat's
:func:`reclaim_settled_job_holds` refunds them, along with holds of jobs
terminalized by paths no poll saw or of jobs gone. A job that left
``running`` without going terminal (reclaimed to ``queued`` after its
launcher died, then claimed elsewhere) is polled on this host by no one:
the heartbeat's :func:`reap_gone_job_child_holds` refunds every hold on
this host whose named child group is gone, whatever the job's status
(gr763200). The babysit stays the fast path while the launcher lives; every release
deletes by row, so whichever lands second finds nothing and refunds
nothing. A :func:`spawn` with a pooled store but no ``job_id`` warns
once per caller: its holds stay the worker's.

**Refused vs unheld.** :func:`spawn`/:func:`hold` raise
:class:`Refused` only when the host publishes capacity rows but cannot
fit the footprint right now. A process with no usable store (CLI, the
allowlisted dep_check child) or a host that publishes no rows for the
footprint's resources runs unheld with a warning — admission is
best-effort scheduling, never a launch veto on an unadvertised host.

**No second reservation store.** Admission is a thin layer over
:mod:`precis.store._resource_slots_ops`: the all-or-nothing conditional
decrement (:func:`reserve_resource_slots`) and the TTL lease ledger
(:func:`insert_slot_hold`, swept by the heartbeat's expired-hold reclaim).
Each footprint dimension becomes one hold row carrying its units, so a
crashed holder's memory is refunded by the same sweep that already
reclaims leaked ``llm:*`` units. No migration: per-resource hold rows
already hold everything a refund needs, and the holder string ties the
rows of one admission together for an operator.

**Multi-unit host rows.** ``mem_mb``, ``cpu_milli`` and ``vram_mb`` are hard
``resource_slots`` rows whose capacity is the host's measured total minus
its reserves (:func:`precis.workers.capability_probe.probe_host_capacity`,
published by the heartbeat). A 2048 MiB footprint takes 2048 units of
``mem_mb``. Token dimensions (``gpu``, ``llm:<model>``,
``llm-api:<provider>``, ``account:<lane>``) are counted the same way. The
default OS memory reserve is 10 GiB
(:data:`~precis.workers.capability_probe.DEFAULT_MEM_RESERVE_MB`), so a host
with 10 GiB or less publishes ``mem_mb = 0``, which retracts the row: no
memory footprint admits there until ``PRECIS_HOST_MEM_RESERVE_MB`` is set
lower.

``off_host`` work (a cloud session, a GitHub call) costs the launching
host nothing but its tokens, so host dimensions are dropped from its
requirements.

All functions take the caller's ``conn`` and run inside its transaction:
an exception between reserve and hold rolls both back.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from psycopg import Connection

from precis.store import _resource_slots_ops as _ops

log = logging.getLogger(__name__)

#: Hard ``resource_slots`` row names for the multi-unit host dimensions.
MEM_MB = "mem_mb"
CPU_MILLI = "cpu_milli"
VRAM_MB = "vram_mb"
#: Per-card VRAM ceiling — the largest single card's ``memory.total`` in
#: MiB (``capability_probe`` publishes it alongside ``vram_mb``). NOT a
#: reserved dimension: footprint admission *checks* a model's footprint
#: against it (a model must fit one card, not the host's summed VRAM —
#: td655172) while the ``gpu`` token count serializes the cards. Hence it
#: stays out of :data:`_HOST_DIMENSIONS` and :class:`Footprint`.
VRAM_CARD_MB = "vram_card_mb"
GPU = "gpu"

#: Host-local dimensions an ``off_host`` footprint does not reserve.
_HOST_DIMENSIONS = (CPU_MILLI, MEM_MB, GPU, VRAM_MB)

#: Hold TTL when the footprint predicts no wall time. Matches the
#: local-serving default (``local_serving._HOLD_TTL_S``): an hour bounds a
#: crashed holder's leak without reclaiming under a live long call.
DEFAULT_TTL_S = 3600.0

#: Slack over a predicted ``wall_s`` before the sweep may reclaim the hold.
TTL_MARGIN_S = 60.0

#: Holder prefix of a job's spawn holds (gr762788, :func:`job_holder`).
JOB_HOLDER_PREFIX = "job:"


def job_holder(job_id: int, pgid: int | None = None, ttl_s: float | None = None) -> str:
    """The holder string of ``job_id``'s spawn holds: ``job:<job_id>`` at
    admission, ``job:<job_id>:<pgid>[:<ttl_s>]`` once the child's process
    group is known — the pgid is what :func:`tend_job_holds` and
    :func:`reap_gone_job_child_holds` check for liveness, ``ttl_s`` (whole
    seconds, rounded up; only with a pgid) the footprint's own hold TTL
    :func:`tend_job_holds` renews to (gr763200). The holder is the one
    place a hold row can carry it: ``expires_at - acquired_at`` stops
    being the TTL at the first renewal."""
    bare = f"{JOB_HOLDER_PREFIX}{int(job_id)}"
    if pgid is None:
        return bare
    named = f"{bare}:{int(pgid)}"
    return named if ttl_s is None else f"{named}:{math.ceil(ttl_s)}"


def _holder_fields(holder: str) -> list[str] | None:
    """The numeric fields after ``job:`` of a :func:`job_holder` string
    (``[job_id]``, ``[job_id, pgid]`` or ``[job_id, pgid, ttl_s]``), else
    ``None``."""
    parts = holder.split(":")
    if parts[0] + ":" != JOB_HOLDER_PREFIX or not 2 <= len(parts) <= 4:
        return None
    if not all(p.isdigit() for p in parts[1:]):
        return None
    return parts[1:]


def _holder_pgid(holder: str) -> int | None:
    """The child pgid a :func:`job_holder` string names, else ``None``."""
    fields = _holder_fields(holder)
    return int(fields[1]) if fields is not None and len(fields) >= 2 else None


def _holder_ttl_s(holder: str) -> float | None:
    """The footprint hold TTL a :func:`job_holder` string carries, else
    ``None`` (a bare holder, or one stamped before gr763200)."""
    fields = _holder_fields(holder)
    if fields is None or len(fields) != 3 or int(fields[2]) <= 0:
        return None
    return float(fields[2])


@dataclass(frozen=True, slots=True)
class Footprint:
    """What one launch needs. Unset fields mean 0 or none.

    ``llm`` maps ``llm:<model>`` / ``llm-api:<provider>`` to units;
    ``account`` maps ``account:<lane>`` to units. ``wall_s`` is the
    predicted run time and sizes the default hold TTL.
    """

    cpu_milli: int = 0
    mem_mb: int = 0
    gpu: int = 0
    vram_mb: int = 0
    llm: dict[str, int] = field(default_factory=dict)
    account: dict[str, int] = field(default_factory=dict)
    wall_s: float = 0.0
    off_host: bool = False

    def requirements(self) -> dict[str, int]:
        """``{resource: units}`` to reserve, zero dimensions omitted, in
        :func:`~precis.store._resource_slots_ops.lock_order`."""
        req: dict[str, int] = {}
        if not self.off_host:
            for name in _HOST_DIMENSIONS:
                units = int(getattr(self, name))
                if units > 0:
                    req[name] = units
        for tokens in (self.llm, self.account):
            for resource, n in tokens.items():
                if int(n) > 0:
                    req[resource] = req.get(resource, 0) + int(n)
        return dict(_ops.lock_order(req))

    def hold_ttl_s(self) -> float:
        """Default hold TTL: predicted wall time plus margin, else an hour."""
        return self.wall_s + TTL_MARGIN_S if self.wall_s > 0 else DEFAULT_TTL_S


@dataclass(frozen=True, slots=True)
class Hold:
    """One reserved dimension and the TTL lease row that backs it."""

    resource: str
    units: int
    hold_id: int


@dataclass(frozen=True, slots=True)
class Admission:
    """A granted footprint on ``host``. Pass it to :func:`release`."""

    host: str
    holder: str
    footprint: Footprint
    holds: tuple[Hold, ...]


def admit(
    conn: Connection,
    host: str,
    footprint: Footprint,
    holder: str,
    *,
    ttl_s: float | None = None,
    holder_identity: tuple[str | None, str | None, str | None] | None = None,
) -> Admission | None:
    """Reserve every dimension of ``footprint`` on ``host``, or none.

    ``None`` when any dimension is short or the host offers no such row;
    nothing stays reserved then. On success each dimension gets a TTL hold
    (``ttl_s``, default :meth:`Footprint.hold_ttl_s`) so the heartbeat
    sweep refunds a holder that dies before :func:`release`.
    ``holder`` (``host:pid``) and ``holder_identity`` (the
    :func:`precis.liveness.worker_identity` triple) are stamped on every
    hold, as :func:`insert_slot_hold` documents.
    """
    req = footprint.requirements()
    if not _ops.reserve_resource_slots(conn, host, req):
        return None
    ttl = footprint.hold_ttl_s() if ttl_s is None else ttl_s
    holds = tuple(
        Hold(
            resource,
            units,
            _ops.insert_slot_hold(
                conn,
                host,
                resource,
                units,
                holder,
                ttl,
                holder_identity=holder_identity,
            ),
        )
        for resource, units in req.items()
    )
    return Admission(host=host, holder=holder, footprint=footprint, holds=holds)


def release(conn: Connection, admission: Admission) -> int:
    """Close ``admission``'s holds and refund their units.

    A hold the sweep already reclaimed (expired, refunded) is skipped, so
    a late or repeated release never double-refunds. Returns the number of
    holds this call closed.
    """
    closed = 0
    for hold in admission.holds:
        if _ops.delete_slot_hold(conn, hold.hold_id):
            _ops.release_resource_slots(
                conn, admission.host, {hold.resource: hold.units}
            )
            closed += 1
    return closed


class Refused(Exception):
    """The host publishes capacity rows but cannot fit the footprint right
    now. Transient by design: the caller retries later, attempt-free — a
    capacity refusal is the host's, not the task's (review td702053)."""


def local_host() -> str:
    """This node's name — the key the heartbeat publishes capacity rows
    under (``PRECIS_HOST_NAME`` then the hostname, the flagless
    ``heartbeat.resolve_host`` precedence)."""
    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


#: The detached child's wrapping shell: argv rides as positional args,
#: stdin from ``$RS_IN``, output to ``$RS_LOG``, the exit code to
#: ``$RS_EXIT`` — so a restarted worker, no longer the parent, still
#: reads the verdict from the file. Its first act writes its own pid to
#: ``$RS_PID``: a caller that dies before persisting the returned handle
#: leaves the child's own launch proof on disk (gr708457).
_SPAWN_SCRIPT = (
    'echo $$ > "$RS_PID"; "$@" < "$RS_IN" > "$RS_LOG" 2>&1; echo $? > "$RS_EXIT"'
)

#: Ceiling on the babysit's renewal cadence (half the default TTL): a
#: long-running launch re-checks the child at least this often.
_RENEW_INTERVAL_CAP_S = 1800.0


def _worker_identity() -> tuple[str | None, str | None, str | None] | None:
    try:
        from precis.liveness import worker_identity

        return worker_identity()
    except Exception:  # pragma: no cover — identity is best-effort garnish
        return None


def _host_offers(conn: Connection, host: str, footprint: Footprint) -> bool:
    """Whether ``host`` publishes ANY row the footprint reserves against —
    ``False`` reads as an unadvertised host (run unheld), never as full."""
    resources = list(footprint.requirements())
    if not resources:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM resource_slots WHERE host = %s AND resource = ANY(%s)",
            (host, resources),
        ).fetchone()
        is not None
    )


def launch_fits(conn: Connection, host: str, footprint: Footprint) -> bool:
    """Check spawn capacity without retaining a second reservation.

    A successful probe refunds immediately; spawn owns the detached hold.
    A race after commit is handled by run_slice’s attempt-free lane wait.
    No advertised rows means unheld, matching the launch fallback.
    """
    requirements = footprint.requirements()
    if _ops.reserve_resource_slots(conn, host, requirements):
        _ops.release_resource_slots(conn, host, requirements)
        return True
    return not _host_offers(conn, host, footprint)


def _try_admit(
    store: Any,
    host: str,
    footprint: Footprint,
    holder: str,
    *,
    ttl_s: float | None = None,
    detached: bool = False,
) -> Admission | None:
    """Admit for a launch, or decide it runs unheld; raise :class:`Refused`
    only when the host advertises the footprint's resources but cannot
    fit it right now.

    ``detached`` (a :func:`spawn` launch) admits with no ``holder_identity``:
    the child outlives the launching worker, so the epoch arm must not
    reclaim its holds on the worker's restart — TTL-only reclaim (see the
    module docstring). A synchronous :func:`hold` keeps the worker's
    identity.

    A store without a pool (unit-test fakes, the allowlisted dep_check
    child) and a store fault both mean unheld: admission must never veto
    a launch it cannot measure (the launch sites ran unaccounted for
    years; a silent regression to that is worse than a visible warning,
    and the heartbeat keeps the rows honest)."""
    if getattr(store, "pool", None) is None:
        return None
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                adm = admit(
                    conn,
                    host,
                    footprint,
                    holder,
                    ttl_s=ttl_s,
                    holder_identity=None if detached else _worker_identity(),
                )
                if adm is not None:
                    return adm
                known = _host_offers(conn, host, footprint)
    except Exception:
        log.warning(
            "admission: admit for %s on %s failed; launching unheld",
            holder,
            host,
            exc_info=True,
        )
        return None
    if not known:
        log.warning(
            "admission: %s publishes no rows for %s; launching unheld",
            host,
            sorted(footprint.requirements()),
        )
        return None
    raise Refused(
        f"{host} cannot fit {sorted(footprint.requirements())} for {holder} right now"
    )


def _renew(store: Any, adm: Admission, ttl_s: float) -> None:
    """Push a live launch's holds back out to ``ttl_s`` — best-effort; a
    renewal that misses (DB hiccup) leaves the last TTL as the backstop."""
    ids = [h.hold_id for h in adm.holds]
    if not ids:
        return
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                _ops.renew_slot_holds(conn, ids, ttl_s)
    except Exception:
        log.warning(
            "admission: hold renewal for %s failed; the TTL sweep is the backstop",
            adm.holder,
            exc_info=True,
        )


def _release_quietly(store: Any, adm: Admission) -> None:
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                release(conn, adm)
    except Exception:
        log.warning(
            "admission: release for %s failed; the TTL sweep reclaims",
            adm.holder,
            exc_info=True,
        )


def _name_child(store: Any, adm: Admission, holder: str) -> Admission:
    """Stamp the child's pgid and the footprint's hold TTL on a job
    launch's holds (``holder`` from :func:`job_holder`) — best-effort: a
    miss leaves the bare ``job:<id>`` holder, which :func:`tend_job_holds`
    renews (to its ``ttl_s`` fallback) while the job runs. Having no pgid
    to judge, no pass can prove its child gone: once the job is terminal
    it is left to :func:`reclaim_settled_job_holds` after the orphan
    reaper's grace, else to the TTL."""
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                _ops.retag_slot_holds(conn, [h.hold_id for h in adm.holds], holder)
    except Exception:
        log.warning(
            "admission: naming the child on %s's holds failed", holder, exc_info=True
        )
        return adm
    return replace(adm, holder=holder)


def _group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` still exists on this host. A pgid
    this process cannot judge (``<= 1``, EPERM) reads as alive: the hold
    is renewed, never refunded under a child that may still run."""
    if pgid <= 1:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def tend_job_holds(
    store: Any,
    job_id: int,
    *,
    host: str | None = None,
    ttl_s: float = DEFAULT_TTL_S,
    renew: bool = True,
) -> tuple[int, int]:
    """Keep a job's spawn holds on ``host`` (default this one) honest from
    its poll (gr762788): release each hold whose named child group is
    gone, and (``renew``, the job still running) push the rest out to
    their footprint's own hold TTL from now — the one its holder carries
    (:func:`job_holder`), so dep_check's 1860 s backstop stays 1860 s
    (gr763200); ``ttl_s`` only for a holder that carries none (bare, or
    stamped before the TTL rode the holder). A hold with no pgid named
    (the stamp missed) counts as live. A terminal job's poll passes
    ``renew=False``: a gone child's hold is refunded at once, a
    possibly-live one is left to :func:`reclaim_settled_job_holds` after
    the orphan reaper's grace. The poll runs on the job's own host, so a
    worker that re-adopted the job after a restart does what the dead
    launcher's babysit no longer can. Returns ``(renewed, released)``; a
    store fault returns ``(0, 0)`` and leaves the TTL as the backstop."""
    if not job_id or getattr(store, "pool", None) is None:
        return 0, 0
    where = host or local_host()
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                held = _ops.job_slot_holds(conn, job_id, where)
                dead = [
                    hold_id
                    for hold_id, holder in held
                    if (pgid := _holder_pgid(holder)) is not None
                    and not _group_alive(pgid)
                ]
                released = _ops.delete_job_slot_holds(conn, job_id, dead)
                renewed = 0
                if renew:
                    by_ttl: dict[float, list[int]] = {}
                    for hold_id, holder in held:
                        if hold_id not in dead:
                            own = _holder_ttl_s(holder)
                            by_ttl.setdefault(own or ttl_s, []).append(hold_id)
                    for own_ttl, ids in by_ttl.items():
                        renewed += _ops.renew_slot_holds(conn, ids, own_ttl)
    except Exception:
        log.warning(
            "admission: tending job %s's holds failed; the TTL sweep is the backstop",
            job_id,
            exc_info=True,
        )
        return 0, 0
    return renewed, released


def reclaim_settled_job_holds(store: Any) -> int:
    """Refund every ``job:`` hold whose job is gone, or has been terminal
    longer than the orphan reaper's grace
    (``_common.ORPHAN_REAP_GRACE_S``): the release of a terminal job's
    footprint whose child the poll could not prove gone, and the backstop
    for terminal transitions no poll saw (the sweeper, a direct STATUS
    tag). The heartbeat's pass; one statement. Returns holds deleted, 0
    for a store without a pool. Raises on a store fault (the heartbeat
    logs and moves on)."""
    from precis.workers.executors._common import ORPHAN_REAP_GRACE_S, TERMINAL

    if getattr(store, "pool", None) is None:
        return 0
    with store.pool.connection() as conn:
        with conn.transaction():
            return _ops.reclaim_settled_job_holds(
                conn, list(TERMINAL), ORPHAN_REAP_GRACE_S
            )


def reap_gone_job_child_holds(store: Any, *, host: str | None = None) -> int:
    """Refund every ``job:<id>:<pgid>`` hold on ``host`` (default this
    one) whose process group is gone, whatever its job's status
    (gr763200). Closes the leak no job-scoped path sees: a launcher
    SIGKILLed with its child, the job reclaimed to ``queued`` and claimed
    on another host — that host's poll tends only its own holds, and the
    settled-job sweep skips a job that is not terminal. Liveness is
    :func:`_group_alive`'s (a group this process cannot judge reads as
    alive, so the hold stays); a bare ``job:<id>`` holder names no group
    and is not touched. Deletes by row, so a racing poll, babysit or
    sweep that closed a hold first leaves nothing to refund twice. The
    heartbeat's pass; must run on the host whose holds it judges, since
    the pgid means nothing elsewhere. Returns holds deleted, 0 for a
    store without a pool. Raises on a store fault (the heartbeat logs and
    moves on)."""
    if getattr(store, "pool", None) is None:
        return 0
    where = host or local_host()
    with store.pool.connection() as conn:
        with conn.transaction():
            held = _ops.job_child_slot_holds(conn, where)
            alive: dict[int, bool] = {}
            dead = []
            for hold_id, holder in held:
                pgid = _holder_pgid(holder)
                if pgid is None:
                    continue
                if pgid not in alive:
                    alive[pgid] = _group_alive(pgid)
                if not alive[pgid]:
                    dead.append(hold_id)
            return _ops.delete_job_child_slot_holds(conn, where, dead)


#: Call sites already warned about a jobless pooled :func:`spawn` — one
#: line per site per process, not one per launch.
_jobless_warned: set[str] = set()


def _spawn_caller() -> str:
    """``module.function:line`` of the first frame outside this module and
    the ``_launch`` wrappers that forward to :func:`spawn` — the site that
    launched without a ``job_id``."""
    frame: Any = sys._getframe(1)
    while frame is not None and (
        frame.f_globals.get("__name__") == __name__ or frame.f_code.co_name == "_launch"
    ):
        frame = frame.f_back
    if frame is None:
        return "<unknown>"
    module = frame.f_globals.get("__name__", "?")
    return f"{module}.{frame.f_code.co_name}:{frame.f_lineno}"


def _warn_jobless_spawn(holder: str) -> None:
    """A pooled :func:`spawn` with no ``job_id``: its holds stay the
    launching worker's (``holder``), so a worker crash leaks them to the
    TTL and a clean exit refunds them under a live child (gr762788)."""
    caller = _spawn_caller()
    if caller in _jobless_warned:
        return
    _jobless_warned.add(caller)
    log.warning(
        "admission: spawn from %s has no job_id; its holds stay with worker %s "
        "and outlive or undercount the child (gr762788)",
        caller,
        holder,
    )


def _babysit(store: Any, proc: Any, adm: Admission, ttl_s: float) -> None:
    """Watch one admitted child: renew its holds on schedule, release at
    exit. Runs on a daemon thread started by :func:`spawn`.

    The exit check comes FIRST — :meth:`Popen.wait` bounds every pass by
    the renewal interval and returns the moment the child exits — so a
    finished launch frees its footprint promptly instead of holding it
    to the TTL (review td702053: a sleep-then-check loop retained dead
    launches' CPU/memory for up to a whole TTL)."""
    interval = max(1.0, min(ttl_s / 2, _RENEW_INTERVAL_CAP_S))
    try:
        while True:
            try:
                proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                _renew(store, adm, ttl_s)
    finally:
        _release_quietly(store, adm)


def spawn(
    store: Any,
    host: str,
    footprint: Footprint,
    holder: str,
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: str | Path | None = None,
    log_path: str | Path,
    exit_file: str | Path,
    pid_file: str | Path | None = None,
    pass_fds: Sequence[int] = (),
    preexec_fn: Callable[[], object] | None = None,
    popen: Callable[..., Any] | None = None,
    job_id: int | None = None,
) -> int:
    """The one admitted detached launcher (gr657245).

    Admit ``footprint`` for ``holder`` on ``host``, then Popen ``argv``
    detached in its own session: stdin from ``stdin`` (``None`` →
    ``/dev/null``), stdout+stderr to ``log_path``, the wrapping shell
    writing the exit code to ``exit_file`` so a restarted worker still
    reads the verdict. A daemon thread babysits the child — renewing the
    holds while it lives (:func:`_babysit`) and releasing at its exit.

    ``None`` from :func:`admit` raises :class:`Refused` when the host
    publishes rows but is full; an unadvertised host or an unusable store
    launches unheld with a warning (see :func:`_try_admit`). The admitted
    holds carry no worker identity — the detached child outlives this
    worker, so its footprint is accounted until the child's exit (babysit
    release) or the TTL backstop, never refunded early by the launching
    worker's restart. A :func:`popen` failure releases the just-admitted
    holds before the error propagates — no hold leaks to its TTL for a
    child that never started (review td702053). ``pid_file`` is where the
    child's *first* act writes its own pid — launch proof that survives
    a caller dead between ``spawn`` returning and the caller persisting
    the handle it built (gr708457); ``None`` writes it to ``/dev/null``.
    ``job_id`` (a launch made for a job) replaces ``holder`` with
    :func:`job_holder` and, once the child exists, stamps its pgid and the
    footprint's hold TTL on the holds, so the job's poll owns them past
    this worker's life (gr762788, gr763200; module docstring); a pooled store without one warns once per call
    site, naming it. Returns the process group pid. ``popen`` is the
    test seam for the fake child.
    """
    popen = subprocess.Popen if popen is None else popen
    if job_id:
        holder = job_holder(job_id)
    elif getattr(store, "pool", None) is not None:
        _warn_jobless_spawn(holder)
    adm = _try_admit(store, host, footprint, holder, detached=True)
    full_env = {
        **(dict(env) if env is not None else {}),
        "RS_IN": str(stdin) if stdin is not None else os.devnull,
        "RS_LOG": str(log_path),
        "RS_EXIT": str(exit_file),
        "RS_PID": str(pid_file) if pid_file is not None else os.devnull,
    }
    # Fixed script; argv rides as positional args, never interpolated.
    try:
        proc = popen(
            [
                "/bin/sh",
                "-c",
                _SPAWN_SCRIPT,
                str(pid_file) if pid_file else "admitted",
                *argv,
            ],
            cwd=str(cwd) if cwd is not None else None,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=tuple(pass_fds),
            preexec_fn=preexec_fn,
        )
    except Exception:
        if adm is not None:
            _release_quietly(store, adm)
        raise
    if adm is not None:
        ttl = footprint.hold_ttl_s()
        if job_id:
            adm = _name_child(store, adm, job_holder(job_id, int(proc.pid), ttl))
        try:
            threading.Thread(
                target=_babysit,
                args=(store, proc, adm, ttl),
                name=f"admission-babysit-{int(proc.pid)}",
                daemon=True,
            ).start()
        except Exception:
            # The child runs but nobody will ever know its pid (the caller
            # sees only the raise) and no babysitter renews or releases its
            # holds: stop its process group (``start_new_session``: the group
            # is the child's own) and release the holds before the error
            # propagates, like a Popen failure does.
            try:
                os.killpg(int(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            _release_quietly(store, adm)
            raise
    return int(proc.pid)


@contextlib.contextmanager
def hold(
    store: Any,
    host: str,
    footprint: Footprint,
    holder: str,
    *,
    ttl_s: float | None = None,
) -> Iterator[Admission | None]:
    """Admit ``footprint`` for one synchronous block; release on exit.

    The synchronous sibling of :func:`spawn` for heavy calls that run in
    the caller's process (an LLM CLI subprocess, a pytest probe): the
    footprint's ``wall_s`` sizes the TTL and no renewal is needed —
    :func:`release` fires at block exit and the sweep is the crash
    backstop. Yields the :class:`Admission`, or ``None`` on the unheld
    dark paths (no store, unadvertised host, store fault — see
    :func:`_try_admit`); raises :class:`Refused` when the host advertises
    rows but cannot fit the footprint now.
    """
    adm = _try_admit(store, host, footprint, holder, ttl_s=ttl_s)
    try:
        yield adm
    finally:
        if adm is not None:
            _release_quietly(store, adm)


__all__ = [
    "CPU_MILLI",
    "DEFAULT_TTL_S",
    "GPU",
    "JOB_HOLDER_PREFIX",
    "MEM_MB",
    "TTL_MARGIN_S",
    "VRAM_CARD_MB",
    "VRAM_MB",
    "Admission",
    "Footprint",
    "Hold",
    "Refused",
    "admit",
    "hold",
    "job_holder",
    "launch_fits",
    "local_host",
    "reap_gone_job_child_holds",
    "reclaim_settled_job_holds",
    "release",
    "spawn",
    "tend_job_holds",
]
