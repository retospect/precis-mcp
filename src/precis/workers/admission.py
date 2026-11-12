"""The single admission entry point (gr647996, load-based scheduling).

:func:`admit` reserves a whole :class:`Footprint` on one host or nothing;
:func:`release` gives it back. Later slices route every launch through it
(the claim path, the detached-spawn helper, the bash-gate CLI). S1 ships
it inert: no live caller yet. S3 moves the claim path and the two LLM
holds (``utils/llm/local_serving.py::_reserve_one`` and the outbound D2
hold in ``utils/llm/openai_tools.py``) onto it. ``spawn()``, the one
admitted detached launcher, is deferred to S4.

**Lock order.** Every multi-row writer of ``resource_slots`` (this
reserve, the heartbeat's capability and capacity syncs, release) touches
rows in :func:`precis.store._resource_slots_ops.lock_order`, sorted by
resource name. One global order means a multi-resource :func:`admit` and
a concurrent heartbeat resync can block each other but never deadlock.

**Hold TTLs are not renewed yet.** A hold lives ``ttl_s`` from admission
and the heartbeat sweep refunds it on expiry, live holder or not. That is
safe for today's bounded LLM calls only. Before long-running jobs admit,
S3 must add TTL renewal or tie the hold to the holder's liveness epoch
(``holder_identity``), or the sweep frees units a running job still uses.

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

from dataclasses import dataclass, field

from psycopg import Connection

from precis.store import _resource_slots_ops as _ops

#: Hard ``resource_slots`` row names for the multi-unit host dimensions.
MEM_MB = "mem_mb"
CPU_MILLI = "cpu_milli"
VRAM_MB = "vram_mb"
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


__all__ = [
    "CPU_MILLI",
    "DEFAULT_TTL_S",
    "GPU",
    "MEM_MB",
    "TTL_MARGIN_S",
    "VRAM_MB",
    "Admission",
    "Footprint",
    "Hold",
    "admit",
    "release",
]
