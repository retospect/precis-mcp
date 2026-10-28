"""D1 run records for ``pcb_<op>`` jobs (design-rerun-pull S1, gr512900).

Every terminal ``pcb_place`` run — committed OR gate-refused — leaves a
``meta.run_record`` on its job ref: schema + code-version tags, the input
digest it was enqueued under (the idem key's ``content_hash`` tail), the
post-commit result digest, the outcome, and the payload (pose map +
scores, or the refusal verdict). The carrier is the terminal job ref
itself — the 2026-10-26 ruling picked "a query convention over job refs"
over a new table, so the archive is this one meta key and the lookups
below; no DDL.

Two digest match arms, because ``content_hash`` covers poses:

* **input** — the resubmit digests to what the run READ (a board stored
  and recovered to its pre-run state). Replay writes the recorded poses,
  side flips and fixed-copper rip-ups verbatim: bit-identical by
  construction, never by re-execution luck.
* **result** — the resubmit digests to what the run WROTE (the common
  "re-run an unchanged board" case). The stored poses are already the
  board's; replay applies them idempotently and names the source run.

A record whose ``schema``/``code_version`` this build cannot read — or
whose ``placer_code_version`` (the placer's OWN version, not the record
format's) differs from this build's — is skipped with a logged reason:
code changes cost the cache, never the run (the research's layer-1
caveat). A ``refused`` record replays as a refusal, never as a result.

Retention: ``meta.run_record`` is deliberately OUTSIDE the meta-key
families the sweeper's ``_gc_transcripts`` strips (transcript*,
assembled_context*) — those reaping passes must never touch the archive —
and :meth:`precis.handlers.job.JobHandler._delete` refuses a job carrying
one, so "job GC" cannot reap a record.

``apply_record`` is the one write path both consumers share:
``PcbHandler._enqueue_op`` today, the pcb workspace importer's
request-cache door (pcb-workspace-code §3) when it lands.

This module is store-side only — excluded from the precis-geom wheel
exactly like ``session_store.py`` (``packaging/geom/pyproject.toml``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: ``meta.run_record``'s schema tag — bump on any shape change.
RUN_RECORD_SCHEMA = "precis.pcb-run/1"
#: The record-format version replay validates against. Bump when a stored
#: record's meaning changes; older records are SKIPPED, never migrated
#: (the event store refits, it does not pickle).
RUN_RECORD_CODE_VERSION = 1

#: A record is only consultable on a TERMINAL job — same tuple
#: ``precis.workers._mint.JOB_TERMINAL_STATUSES`` keeps (duplicated here:
#: ``precis.pcb`` may not reach up into ``precis.workers``).
_TERMINAL = ("succeeded", "failed", "cancelled")


def placer_code_version(placer: str) -> int | None:
    """The placer's OWN code version as this build knows it — the topo
    kernel's ``topoplace.runstore.CODE_VERSION`` for ``'topo'``, the
    anneal's ``optimize.ANNEAL_CODE_VERSION`` for ``'anneal'``. NOT the
    record-format :data:`RUN_RECORD_CODE_VERSION`: a placer-code change
    invalidates stored poses even when the record shape is unchanged.
    ``None`` = a placer name this build does not know — the record is
    unreadable either way. Imported lazily: this module is store-side
    while both versions live kernel-side."""
    if placer == "topo":
        from precis.pcb.topoplace.runstore import CODE_VERSION

        return int(CODE_VERSION)
    if placer == "anneal":
        from precis.pcb.optimize import ANNEAL_CODE_VERSION

        return int(ANNEAL_CODE_VERSION)
    return None


def _part_meta(i: dict[str, Any]) -> dict[str, Any]:
    part = i.get("part")
    return part if isinstance(part, dict) else {}


def footprint_fingerprint(graph: dict[str, Any]) -> str:
    """The part-identity half of the lookup key — a digest over the
    sorted ``(refdes, part, footprint, value)`` tuples. ``content_hash``
    deliberately does not cover them (gr512900 fix 2): a footprint or
    part swap on the same refdes leaves the pose digest unchanged but
    must NOT hit the old record — the stored poses were computed for
    different geometry. ``part`` is the catalog SKU (``part_lcsc``,
    falling back to the epro part blob's mpn); ``value`` the part blob's
    value, else the component label — the de-facto value string for
    non-catalog parts (``"100nF 0402"``). Stored on the record and
    compared in :func:`lookup`; a mismatch — or a record written before
    the field existed — is a miss, never a wrong replay."""
    rows = sorted(
        (
            str(i.get("refdes") or ""),
            str(
                i.get("part_lcsc")
                or _part_meta(i).get("mpn")
                or _part_meta(i).get("name")
                or ""
            ),
            str(i.get("footprint") or ""),
            str(_part_meta(i).get("value") or i.get("label") or ""),
        )
        for i in graph.get("instances") or []
    )
    return hashlib.blake2b(
        json.dumps(rows, default=str).encode("utf-8"), digest_size=12
    ).hexdigest()


@dataclass(frozen=True)
class RunHit:
    """A readable stored run matching a resubmit's digest."""

    job_id: int
    record: dict[str, Any]
    #: ``"input"`` — the resubmit equals the run's read state (replay
    #: writes the stored result); ``"result"`` — it equals the run's
    #: post-commit state (the result is already on the board).
    matched: str


def committed_record(
    *,
    op: str,
    placer: str,
    params: dict[str, Any],
    input_digest: str,
    result_digest: str,
    footprint_digest: str,
    poses: dict[str, dict[str, Any]],
    scores: dict[str, Any],
    ripped_fixed_ids: list[int],
    placement_only: bool,
    elites: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The record a gate-clean run leaves. ``poses`` is
    ``{refdes: {x, y, rot, side}}``; ``scores`` the run's
    ``meta.last_place`` payload verbatim; ``elites`` a pointer at the
    placer's stored elite set (topo's ``meta.topo_run.files``
    ``elites.json``) — S2's warm-start reads it, replay never does.
    ``placement_only`` flags a run that had no route stage (the anneal
    always; topo without ``route=True``) so replay can say
    "placement replayed; route not replayed" — a routed run's record is
    stamped only after that stage SUCCEEDED (gr512900 fix 4)."""
    record: dict[str, Any] = {
        "schema": RUN_RECORD_SCHEMA,
        "code_version": RUN_RECORD_CODE_VERSION,
        "op": op,
        "placer": placer,
        "placer_code_version": placer_code_version(placer),
        "params": dict(params),
        "input_digest": str(input_digest),
        "result_digest": str(result_digest),
        "footprint_digest": str(footprint_digest),
        "outcome": "committed",
        "poses": poses,
        "scores": dict(scores),
        "ripped_fixed_ids": [int(i) for i in ripped_fixed_ids],
        "placement_only": bool(placement_only),
    }
    if elites is not None:
        record["elites"] = dict(elites)
    return record


def refused_record(
    *,
    op: str,
    placer: str,
    params: dict[str, Any],
    input_digest: str,
    footprint_digest: str,
    verdict: str,
    failure_class: str | None,
) -> dict[str, Any]:
    """The record a refused run leaves — evidence of what did NOT work.
    Carries the gate verdict so a resubmit on the same input replays the
    refusal instead of paying for the same anneal."""
    return {
        "schema": RUN_RECORD_SCHEMA,
        "code_version": RUN_RECORD_CODE_VERSION,
        "op": op,
        "placer": placer,
        "placer_code_version": placer_code_version(placer),
        "params": dict(params),
        "input_digest": str(input_digest),
        "result_digest": None,
        "footprint_digest": str(footprint_digest),
        "outcome": "refused",
        "refusal": {"verdict": verdict, "failure_class": failure_class},
    }


def _validate(
    job_id: int, record: Any, op: str, matched: str, footprint: str
) -> dict[str, Any] | None:
    """The record iff this build can read it AND its part identity still
    matches the resubmit's, else ``None`` with a logged reason — a
    corrupt, stale-version or BOM-drifted record costs the hit, never
    the resubmit."""
    if not isinstance(record, dict):
        log.warning("run_record: job %d record is %r, not an object", job_id, record)
        return None
    if record.get("schema") != RUN_RECORD_SCHEMA:
        log.warning(
            "run_record: job %d record schema %r unreadable (want %r); skipped",
            job_id,
            record.get("schema"),
            RUN_RECORD_SCHEMA,
        )
        return None
    if record.get("code_version") != RUN_RECORD_CODE_VERSION:
        log.warning(
            "run_record: job %d record code_version %r unreadable (want %r); skipped",
            job_id,
            record.get("code_version"),
            RUN_RECORD_CODE_VERSION,
        )
        return None
    if record.get("op") != op or record.get("outcome") not in ("committed", "refused"):
        log.warning(
            "run_record: job %d record op/outcome corrupt (%r/%r); skipped",
            job_id,
            record.get("op"),
            record.get("outcome"),
        )
        return None
    # The placer's OWN code version, not the record format's: an anneal or
    # kernel change invalidates stored poses even when the record still
    # parses — replay it and the "verbatim" result was computed by code
    # that no longer exists.
    want_placer = placer_code_version(str(record.get("placer") or ""))
    if want_placer is None or record.get("placer_code_version") != want_placer:
        log.warning(
            "run_record: job %d record placer %r code version %r "
            "(this build: %r); skipped",
            job_id,
            record.get("placer"),
            record.get("placer_code_version"),
            want_placer,
        )
        return None
    # Part identity rides alongside the pose digest (gr512900 fix 2): a
    # footprint/part/value swap on the same refdes does NOT change
    # ``content_hash``, but the stored poses were computed for the old
    # part's geometry — replaying them is the wrong-result failure this
    # key exists to prevent. Records predating the field miss the same
    # way.
    if record.get("footprint_digest") != footprint:
        log.warning(
            "run_record: job %d record footprint digest %r != resubmit "
            "%r (part/footprint/value changed); skipped",
            job_id,
            record.get("footprint_digest"),
            footprint,
        )
        return None
    if matched == "input" and record.get("input_digest") is None:
        log.warning("run_record: job %d record has no input_digest; skipped", job_id)
        return None
    if record["outcome"] == "committed" and not isinstance(record.get("poses"), dict):
        log.warning(
            "run_record: job %d committed record has no pose map; skipped", job_id
        )
        return None
    return record


def lookup(
    store: Store, ref_id: int, op: str, digest: str, footprint: str
) -> RunHit | None:
    """The newest TERMINAL ``pcb_<op>`` job on ``ref_id`` whose run record
    matches ``digest`` — on its idem key (the run's INPUT) or on the
    record's ``result_digest`` (the run's OUTPUT) — AND whose part
    identity still matches the resubmit's ``footprint`` digest
    (:func:`footprint_fingerprint`). Unreadable records are skipped
    newest-first, so a corrupt entry never shadows a good older one;
    ``None`` = ordinary enqueue."""
    idem_key = f"pcb_{op}:{ref_id}:{digest}"
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.meta->'run_record', "
            "       (r.meta->>'idem_key' = %s) AS on_input "
            "  FROM refs r "
            " WHERE r.kind = 'job' AND r.retired_at IS NULL "
            "   AND r.parent_id = %s "
            "   AND r.meta ? 'run_record' "
            "   AND (r.meta->>'idem_key' = %s "
            "        OR r.meta->'run_record'->>'result_digest' = %s) "
            "   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "                WHERE rt.ref_id = r.ref_id "
            "                  AND t.namespace = 'STATUS' "
            "                  AND t.value = ANY(%s)) "
            " ORDER BY r.ref_id DESC",
            (idem_key, ref_id, idem_key, digest, list(_TERMINAL)),
        ).fetchall()
    for job_id, record, on_input in rows:
        matched = "input" if on_input else "result"
        valid = _validate(int(job_id), record, op, matched, footprint)
        if valid is not None:
            return RunHit(job_id=int(job_id), record=valid, matched=matched)
    return None


def poses_of(record: dict[str, Any]) -> dict[str, tuple[float, float, float]]:
    """The record's pose map as ``pcb_set_pose`` takes it — ``{refdes:
    (x, y, rot)}``; ``side`` rides :func:`sides_of` separately."""
    return {
        str(refdes): (float(p["x"]), float(p["y"]), float(p.get("rot") or 0.0))
        for refdes, p in (record.get("poses") or {}).items()
    }


def sides_of(record: dict[str, Any]) -> dict[str, str]:
    """``{refdes: 'top'|'bottom'}`` for entries whose pose carries a side."""
    return {
        str(refdes): str(p["side"])
        for refdes, p in (record.get("poses") or {}).items()
        if isinstance(p, dict) and p.get("side") in ("top", "bottom")
    }


def apply_record(
    store: Store,
    ref_id: int,
    record: dict[str, Any],
    *,
    board_id: int | None,
    current_layers: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """Write a committed record's result onto the design — ``(poses
    written, side flips applied, fixed-copper rows retired)``.

    The same write-back calls the job itself used: ``pcb_set_pose``
    (fixed-respecting) for ``(x, y, rot)`` plus the recorded
    ``meta.last_place`` verbatim, ``pcb_move_instance(layer=)`` for the
    side flips (skipped where the stored layer already agrees), and
    ``pcb_fixed_copper_retire_ids`` for the rip-up. One transaction — a
    pose write without its rip-up is the half-state the job's own commit
    guards against. Idempotent by construction: on a ``result``-digest
    hit every write lands the value already stored.
    """
    poses = poses_of(record)
    sides = sides_of(record)
    current_layers = current_layers or {}
    ripped = [int(i) for i in record.get("ripped_fixed_ids") or []]
    scores = record.get("scores")
    with store.tx() as conn:
        moved = store.pcb_set_pose(
            ref_id,
            poses,
            meta={"last_place": scores} if isinstance(scores, dict) else None,
            conn=conn,
        )
        flips = 0
        for refdes, side in sides.items():
            if current_layers.get(refdes) == side:
                continue
            store.pcb_move_instance(ref_id, refdes, layer=side, conn=conn)
            flips += 1
        n_ripped = (
            store.pcb_fixed_copper_retire_ids(int(board_id), ripped, conn=conn)
            if ripped and board_id is not None
            else 0
        )
    return moved, flips, n_ripped


__all__ = [
    "RUN_RECORD_CODE_VERSION",
    "RUN_RECORD_SCHEMA",
    "RunHit",
    "apply_record",
    "committed_record",
    "footprint_fingerprint",
    "lookup",
    "placer_code_version",
    "poses_of",
    "refused_record",
    "sides_of",
]
