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
import os
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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
    Returns the process group pid. ``popen`` is the test seam for the
    fake child.
    """
    popen = subprocess.Popen if popen is None else popen
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
        threading.Thread(
            target=_babysit,
            args=(store, proc, adm, ttl),
            name=f"admission-babysit-{int(proc.pid)}",
            daemon=True,
        ).start()
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
    "launch_fits",
    "local_host",
    "release",
    "spawn",
]
