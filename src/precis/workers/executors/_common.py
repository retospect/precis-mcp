"""Shared substrate for the job executors and the wake_runner.

The two executors (:mod:`claude_inproc`, :mod:`coordinator`) and the
:mod:`precis.workers.wake_runner` pass all speak the same closed
``STATUS:*`` tag namespace, claim ``kind='job'`` rows with the same
SQL shape, and manipulate job rows with the same handful of helpers.
This module is the single home for that substrate so the three
modules stop re-declaring the constants and reaching into each
other's privates (the previous arrangement had ``coordinator`` and
``wake_runner`` importing helpers straight out of ``claude_inproc``,
and all three re-stating the STATUS values "to avoid a circular
import" that module-level constants can't actually cause).

The executors import these under their existing ``_name`` aliases so
the bare-name references in their bodies — and the tests that
``monkeypatch.setattr(module, "_set_status", ...)`` — keep working.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.handlers._todo_views import _doable_exclusion_clause
from precis.store._resource_slots_ops import (
    release_resource_slots,
    reserve_resource_slots,
)
from precis.store.types import BlockInsert
from precis.workers.registry import SERVICES_BY_NAME

log = logging.getLogger(__name__)

# ── STATUS:* closed-namespace tag values ──────────────────────────
STATUS_NAMESPACE = "STATUS"
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"

# STATUS:waiting_* values written by a coordinator Yield; each maps to
# one ``WakeWhen.kind`` so the wake_runner's selectivity stays cheap
# (exact match on a closed-status value, not a LIKE).
WAITING_CHILDREN = "waiting_children"
WAITING_TIME = "waiting_time"
WAITING_ASK_USER = "waiting_ask_user"
WAITING_MANUAL_KICK = "waiting_manual_kick"

# Terminal STATUS values — a row carrying any of these is not
# claimable. Waiting statuses are NOT terminal; they're paused.
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

# Chunk kinds the executors write.
JOB_EVENT_KIND = "job_event"
JOB_SUMMARY_KIND = "job_summary"

#: Claim-ordering weight for a job whose ``refs.prio`` is unset (NULL).
#: The mid-point of the 1..10 ``refs.prio`` scale, matching
#: ``service_config``'s ``DEFAULT_PRIO`` — so an all-unset queue orders by
#: age alone (``ref_id`` ASC), byte-identical to the pre-6a FIFO claim.
_DEFAULT_JOB_PRIO = 5

#: Candidate over-fetch factor for the scarcity re-rank (6d-deferred, §5.3).
#: The SQL fetches ``limit × this`` rows in prio/age order, then the scarcity
#: term re-ranks in Python and the top ``limit`` are reserved — so a rare-
#: capability job can surface ahead of the prio/age head without an unbounded
#: ``FOR UPDATE`` lock set. 1 (or no ``resource_slots``) ⇒ pre-6d behaviour.
_CLAIM_OVERFETCH = 3


def _scarcity(requires: dict[str, int], host_count: dict[str, int]) -> float:
    """A job's capability-scarcity score — the first claim-order key (§5.3).

    Rarer capability → higher score → claimed first, because the scarce
    resource is the bottleneck the schedule should fill before commodity work.
    Score = the max over the job's required resources of ``1 / (hosts
    advertising it)``; a resource no host advertises contributes 0 (it can't be
    reserved here anyway — it self-gates to the ``target_node`` pin). A job that
    requires nothing scores 0, so a queue with no ``requires`` collapses to the
    prio/age order (pre-6d, byte-identical).
    """
    best = 0.0
    for res in requires:
        n = host_count.get(res, 0)
        if n > 0:
            best = max(best, 1.0 / n)
    return best


def reserve_host() -> str:
    """This host's identity for resource reservation (slice 6c).

    Must match the key the ``heartbeat`` self-probe writes ``resource_slots``
    under (``PRECIS_HOST_NAME`` or the hostname) — the reservation host is
    the *heartbeat* identity, not the ``PRECIS_NODE`` claim-gate identity,
    so a decrement lands on the same row the probe advertised.
    """
    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


def effective_requires(meta: dict[str, Any]) -> dict[str, int]:
    """The resource requirements of a job (slice 6d).

    An explicit ``meta.requires`` (``{resource: units}``) wins; otherwise
    the requirement is *derived* from the job's ``job_type`` via the
    registry — a ``struct_relax``/``fold`` job matches a ``ServiceSpec``
    whose ``requires={"gpu"}``, so it needs ``{"gpu": 1}`` without any mint
    change. A job_type with no matching spec (or an empty ``requires``)
    needs nothing — the common path. The vocabulary is the counted
    ``resource_slots`` tokens (``gpu``/``podman``/``tts``), not the
    executor-capability tokens the dispatcher validates.
    """
    explicit = meta.get("requires")
    if explicit:
        return {str(k): int(v) for k, v in dict(explicit).items()}
    job_type = meta.get("job_type")
    spec = SERVICES_BY_NAME.get(str(job_type)) if job_type else None
    if spec is not None and spec.requires:
        return {tok: 1 for tok in spec.requires}
    return {}


def _advertised_by_host(conn: Connection) -> dict[str, set[str]]:
    """``host -> {resources it currently advertises}`` from ``resource_slots``.

    The self-gating map (slice 6d): a required resource that no host — or
    not *this* host — advertises is not reserved (it falls back to the
    ``target_node`` pin), so activating ``requires`` can't strand a job in
    the window before the heartbeat self-probe has populated the table.
    """
    rows = conn.execute("SELECT host, resource FROM resource_slots").fetchall()
    out: dict[str, set[str]] = {}
    for host, resource in rows:
        out.setdefault(str(host), set()).add(str(resource))
    return out


def _mem_pressured_hosts(conn: Connection) -> set[str]:
    """Hosts under measured memory pressure (6d-deferred, soft veto).

    A host whose soft ``mem`` gauge (:meth:`Store.sync_soft_signal`) has hit
    ``free = 0`` is out of headroom; the claim skips reserving *heavy*
    (requires-bearing) jobs there so a jetsam-prone box (macOS) isn't handed
    another GPU/container/TTS job while it's thrashing. Dark until the
    heartbeat writes a ``mem`` row — no rows ⇒ empty set ⇒ no veto.
    """
    rows = conn.execute(
        "SELECT host FROM resource_slots WHERE resource = 'mem' "
        "AND kind = 'soft' AND free <= 0"
    ).fetchall()
    return {str(r[0]) for r in rows}


def release_job_reservation(conn: Connection, ref_id: int) -> None:
    """Refund + clear a job's ``meta.reserved`` slots (idempotent).

    A no-op for a job that reserved nothing (no ``meta.reserved``). Clears
    the key after refunding so a second terminal transition — e.g. the
    sweeper racing an executor — can't double-refund (the capped release
    also guards the counter). Called at every terminal transition.
    """
    row = conn.execute(
        "SELECT meta->'reserved' FROM refs WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    reserved = row[0] if row and row[0] else None
    if not reserved:
        return
    host = reserved.get("host")
    slots = reserved.get("slots") or {}
    if host and slots:
        release_resource_slots(conn, str(host), dict(slots))
    conn.execute(
        "UPDATE refs SET meta = meta - 'reserved' WHERE ref_id = %s", (ref_id,)
    )


# ── Claim ─────────────────────────────────────────────────────────


def claim_executor_jobs(
    conn: Connection,
    *,
    executor: str,
    limit: int,
    exclude_paused: bool = False,
    node: str | None = None,
    parent_not_paused: bool = False,
    reserve_host_id: str | None = None,
    reclaim_stale_running: bool = False,
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable jobs for ``executor``.

    Claimable = ``kind='job'``, ``meta.executor`` matches,
    ``STATUS:queued``, not terminal, lease expired or absent.

    **Crash recovery (``reclaim_stale_running``).** Off by default, so
    ``claude_inproc`` / ``coordinator`` behaviour is unchanged. When on
    (``ssh_node`` only), a ``STATUS:running`` row whose lease has
    *provably* expired (``lease_until`` non-null and ``< now()``) is also
    claimable — its worker died mid-dispatch (e.g. a deploy restart) and
    the lease, sized to outlive the job plus margin, is the death-
    presumption signal. Stealing re-runs the job (the ssh_node caller
    bumps ``meta.attempts`` and poison-guards past a cap); a stolen row's
    stale ``meta.reserved`` slots are refunded here before it re-reserves,
    so a crash can't leak resource slots. A running job with a *live*
    (unexpired) lease is never stolen — the lease clause excludes it.

    **Claim ordering (slice 6a).** ``ORDER BY COALESCE(prio, 5) DESC,
    ref_id ASC`` — highest ``refs.prio`` first (the dispatcher propagates
    the parent todo's prio onto the job, so a high-prio quest/project has
    its compute claimed ahead of commodity work), oldest-first
    (``ref_id``) as the within-prio tiebreak / anti-starvation term. An
    all-unset queue collapses to ``ref_id`` ASC — the pre-6a FIFO. The
    capability-rarity term (§5.3) is layered on in 6d.

    **Reserve-at-claim (slices 6c/6d).** A job's resource requirements —
    explicit ``meta.requires`` or *derived* from its ``job_type`` via the
    registry (:func:`effective_requires`; ``struct_relax``/``fold`` →
    ``{"gpu": 1}``) — are reserved in this transaction before the job is
    handed back: the conditional decrement is the lock. The reservation
    host is the job's ``target_node`` if pinned (the resource lives where
    the job runs — an ssh_node GPU job reserves on the GPU box), else this
    host (``reserve_host_id`` / :func:`reserve_host`). Reservation
    *self-gates* on what that host advertises (slice 6d): an unadvertised
    requirement is left to the ``target_node`` node-gate rather than
    blocking, so activating ``requires`` can't strand a job before the
    heartbeat probe has populated ``resource_slots``. A job whose reserved
    slot is full is dropped from the batch (lock frees at commit → waits
    for capacity). What was actually reserved is stamped on
    ``meta.reserved`` for :func:`release_job_reservation` to refund at
    terminal. Jobs needing nothing are unaffected — the common path.

    When ``exclude_paused`` is True, also exclude rows carrying an
    open-namespace pause tag (``ask-user:*`` / ``halt:*`` /
    ``child-failed:*``) via the shared
    :func:`_doable_exclusion_clause` so the vocabulary stays in sync
    with the dispatcher's candidate query.

    **Node gate (ADR 0043 §23 #3).** A job may pin itself to a node via
    ``meta.params.target_node`` (``struct_relax`` sets it so the GPU
    relax is claimed by the node that ssh+stages it, keeping the NFS
    bind paths consistent). A worker passes its own ``node`` (from
    ``PRECIS_NODE``; ``None`` when unset): an un-pinned job is claimable
    by anyone, a pinned job only by the matching node. A node-less
    worker therefore claims only un-pinned jobs — the ``= NULL`` compare
    is never true, so it can't grab a job meant for a specific box.

    **Parent gate (§23 #3).** When ``parent_not_paused`` is True, skip a
    job whose *parent* todo carries an open-namespace pause tag — a
    halted / asking-user / child-failed project must not burn heavy
    compute until the owner unblocks it.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    exclusion_sql = ""
    if exclude_paused:
        exclusion_sql = f"""
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'OPEN'
                    AND {_doable_exclusion_clause()}
               )"""

    parent_sql = ""
    if parent_not_paused:
        parent_sql = f"""
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.parent_id
                    AND t.namespace = 'OPEN'
                    AND {_doable_exclusion_clause()}
               )"""

    # Status/lease predicate. Fresh queued work (``put`` sets no lease) is
    # always claimable; with ``reclaim_stale_running`` an expired-lease
    # ``STATUS:running`` row is too (crash recovery — see the docstring). A
    # live-lease running row is excluded either way.
    status_lease_sql = """
           AND (
             (
               EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = %s
               )
               AND (
                    (r.meta->>'lease_until') IS NULL
                 OR (r.meta->>'lease_until')::timestamptz < now()
               )
             )"""
    status_params: list[Any] = [STATUS_NAMESPACE, QUEUED]
    if reclaim_stale_running:
        status_lease_sql += """
             OR (
               EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = %s
               )
               AND (r.meta->>'lease_until') IS NOT NULL
               AND (r.meta->>'lease_until')::timestamptz < now()
             )"""
        status_params += [STATUS_NAMESPACE, RUNNING]
    status_lease_sql += """
           )"""

    rows = conn.execute(
        f"""
        SELECT r.ref_id, r.title, r.meta, r.prio
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND r.meta->>'executor' = %s
           AND (
                (r.meta->'params'->>'target_node') IS NULL
             OR (r.meta->'params'->>'target_node') = %s
           ){status_lease_sql}
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = ANY(%s)
               ){exclusion_sql}{parent_sql}
         ORDER BY COALESCE(r.prio, %s) DESC, r.ref_id ASC
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (
            executor,
            node,
            *status_params,
            STATUS_NAMESPACE,
            list(TERMINAL),
            _DEFAULT_JOB_PRIO,
            limit * _CLAIM_OVERFETCH,
        ),
    ).fetchall()

    default_host = reserve_host_id or reserve_host()
    advertised = _advertised_by_host(conn)

    # Scarcity re-rank (6d-deferred, §5.3): capability-rarity is the FIRST
    # claim-order key, then prio, then age. ``host_count[res]`` = how many hosts
    # advertise ``res`` (rarer → higher scarcity). The SQL already returned rows
    # in prio/age order and stably; re-sorting by (-scarcity, -prio, ref_id)
    # keeps that order within a scarcity tier — so a queue with no ``requires``
    # (scarcity 0 everywhere) is byte-identical to the pre-6d prio/age claim.
    host_count: dict[str, int] = {}
    for _h, res_set in advertised.items():
        for res in res_set:
            host_count[res] = host_count.get(res, 0) + 1

    def _order_key(r: Any) -> tuple[float, int, int]:
        prio = int(r[3]) if r[3] is not None else _DEFAULT_JOB_PRIO
        scarcity = _scarcity(effective_requires(dict(r[2] or {})), host_count)
        return (-scarcity, -prio, int(r[0]))

    ranked = sorted(rows, key=_order_key)
    pressured = _mem_pressured_hosts(conn)

    claimed: list[tuple[int, str, dict[str, Any]]] = []
    for r in ranked:
        if len(claimed) >= limit:
            break  # scarcity-ranked top ``limit`` reserved; leave the rest locked-free
        ref_id, title, meta = int(r[0]), str(r[1]), dict(r[2] or {})
        if reclaim_stale_running and meta.get("reserved"):
            # A stolen (crash-recovered) job still carries the dead worker's
            # reservation — refund it before this claim re-reserves, so slots
            # don't leak on the reserving host. No-op for fresh queued work.
            release_job_reservation(conn, ref_id)
            meta.pop("reserved", None)
        requires = effective_requires(meta)
        if not requires:
            claimed.append((ref_id, title, meta))
            continue
        # The resource lives where the job runs: its target_node (an
        # ssh_node GPU job reserves on the GPU box, not the claimer), else
        # this host. Self-gate to what that host actually advertises — an
        # unadvertised requirement falls back to the node-gate/pin (no
        # stall in the window before the probe populates the slot map).
        params = meta.get("params") or {}
        res_host = str(params.get("target_node") or default_host)
        # Soft memory-pressure veto (6d-deferred): a heavy job's reservation
        # host is out of RAM headroom → skip it this round (the lock frees at
        # commit; it retries once pressure clears). Dark until a ``mem`` row
        # with free=0 exists.
        if res_host in pressured:
            continue
        reservable = {
            res: units
            for res, units in requires.items()
            if res in advertised.get(res_host, set())
        }
        if reservable and not reserve_resource_slots(conn, res_host, reservable):
            # A live slot is full → drop it; the lock frees at commit and
            # the job waits for capacity on that host.
            continue
        if reservable:
            reserved = {"host": res_host, "slots": reservable}
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object('reserved', %s::jsonb) WHERE ref_id = %s",
                (json.dumps(reserved), ref_id),
            )
            meta["reserved"] = reserved
        claimed.append((ref_id, title, meta))
    return claimed


# ── Status / chunk / meta helpers ─────────────────────────────────


def set_status(
    store: Any, ref_id: int, value: str, *, conn: Connection | None = None
) -> None:
    """Replace the current ``STATUS:`` tag with ``value`` on ``ref_id``.

    A terminal value also refunds any resource reservation the job holds
    (slice 6c) — release rides the same status write so a job's slots come
    back the instant it stops running, in the caller's transaction.
    """
    from precis.store import Tag

    tag = Tag.parse_strict(f"STATUS:{value}")
    store.add_tag(
        ref_id,
        tag,
        set_by="agent",
        replace_prefix=True,
        conn=conn,
    )
    if value in TERMINAL:
        if conn is not None:
            release_job_reservation(conn, ref_id)
        else:
            with store.pool.connection() as c:
                with c.transaction():
                    release_job_reservation(c, ref_id)


def is_cancel_requested(conn: Connection, ref_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND t.namespace = %s
           AND t.value = %s
         LIMIT 1
        """,
        (ref_id, STATUS_NAMESPACE, CANCEL_REQUESTED),
    ).fetchone()
    return row is not None


def current_status(conn: Connection, ref_id: int) -> str | None:
    """Return the ref's current ``STATUS:`` value, or ``None`` if unset.

    There is one ``STATUS:`` tag per ref at a time (the handler writes
    with ``replace_prefix=True``), so this is an unambiguous read. Used
    to tell whether a job has already reached a terminal state before
    the executor applies its own transition.
    """
    row = conn.execute(
        """
        SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND t.namespace = %s
         LIMIT 1
        """,
        (ref_id, STATUS_NAMESPACE),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def append_chunk(
    store: Any,
    ref_id: int,
    chunk_kind: str,
    text: str,
    *,
    conn: Connection | None = None,
) -> None:
    """Append a chunk at the next ``ord`` for the ref.

    When ``conn`` is provided we count via that connection so back-to-
    back appends inside the same tx see each other's INSERTs. The
    previous implementation called ``store.list_blocks_for_ref`` which
    opens its own pool connection — uncommitted INSERTs in ``conn``
    were invisible, leading to two calls computing the same
    ``next_pos`` and a unique-constraint violation on ``(ref_id, ord)``.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT COALESCE(MAX(ord) + 1, 0) FROM chunks "
            "WHERE ref_id = %s AND ord >= 0",
            (ref_id,),
        ).fetchone()
        next_pos = int(row[0]) if row and row[0] is not None else 0
    else:
        blocks = store.list_blocks_for_ref(ref_id)
        next_pos = len(blocks)
    store.insert_blocks(
        ref_id,
        [BlockInsert(pos=next_pos, text=text, meta={"chunk_kind": chunk_kind})],
        conn=conn,
    )


def set_meta(conn: Connection, ref_id: int, **fields: Any) -> None:
    """Merge ``fields`` into ``refs.meta``."""
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
        (Jsonb(fields), ref_id),
    )


def record_failure(
    store: Any,
    ref_id: int,
    reason: str,
    *,
    gripe_rollback: int | None,
    failure_class: str | None = None,
) -> None:
    """Tag a job ``STATUS:failed`` with a reason event chunk.

    ``failure_class`` (optional) distinguishes *why* the job failed — e.g.
    ``"infra"`` (the runner/container/executor itself died: subprocess
    exception, non-zero container exit, malformed/missing output) vs
    ``"non-convergence"`` (the compute actually ran and reported a genuine
    physical/numeric failure) — stamped onto ``refs.meta.failure_class`` so a
    downstream harvest can tell "couldn't run" apart from "ran and failed"
    instead of laundering both into the same bare ``STATUS:failed``.
    """
    with store.pool.connection() as conn:
        append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
        set_status(store, ref_id, FAILED, conn=conn)
        if failure_class is not None:
            set_meta(conn, ref_id, failure_class=failure_class)
        if gripe_rollback is not None:
            set_status(store, gripe_rollback, "open", conn=conn)
        # Slice-5 failure-bubble.
        from precis.handlers._job_bubble import bubble_job_failure

        bubble_job_failure(store, ref_id, conn=conn)
        conn.commit()


__all__ = [
    "CANCELLED",
    "CANCEL_REQUESTED",
    "FAILED",
    "JOB_EVENT_KIND",
    "JOB_SUMMARY_KIND",
    "QUEUED",
    "RUNNING",
    "STATUS_NAMESPACE",
    "SUCCEEDED",
    "TERMINAL",
    "WAITING_ASK_USER",
    "WAITING_CHILDREN",
    "WAITING_MANUAL_KICK",
    "WAITING_TIME",
    "append_chunk",
    "claim_executor_jobs",
    "current_status",
    "effective_requires",
    "is_cancel_requested",
    "record_failure",
    "release_job_reservation",
    "reserve_host",
    "set_meta",
    "set_status",
]
