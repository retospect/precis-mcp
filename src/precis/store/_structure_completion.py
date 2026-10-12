"""One-statement bounded current geometry/run/request read for completion.

No writer is trusted in v1. Raw params.completion JSON remains unqualified,
including self-declared proof fields. Explicit selection is independent of the
history window. Legacy on_version is not an input/output identity. This seam
neither hydrates historical cells nor reads trajectories/force arrays.
"""

from __future__ import annotations

import json
import struct
from contextlib import nullcontext
from decimal import Decimal
from typing import Any, cast

from psycopg import Connection
from psycopg.rows import dict_row

from precis.structure.completion import validate_selector
from precis.structure.completion_codec import CodecError
from precis.structure.completion_identity import (
    InvalidGeometry,
    geometry_snapshot,
    integer,
)
from precis.structure.completion_types import CompletionSnapshot, DisplaySelector

# r is the live structure ref in both standalone and SE statements. Metadata
# numerics stay decimal; binary columns use their exact PostgreSQL send value.
GEOMETRY_COLUMNS = """
 r.ref_id, r.meta::text AS meta_json,
 COALESCE((SELECT jsonb_agg(jsonb_build_object(
 'id', a.id, 'ref_id', a.ref_id, 'label', a.label, 'element', a.element,
 'fa', encode(float8send(a.fa), 'hex'), 'fb', encode(float8send(a.fb), 'hex'),
 'fc', encode(float8send(a.fc), 'hex'), 'fixed', a.fixed,
 'magmom', encode(float8send(a.magmom), 'hex'), 'oxidation', a.oxidation,
 'charge', a.charge, 'hybridization', a.hybridization)
 ORDER BY a.id)::text FROM struct_atoms a
 WHERE a.ref_id=r.ref_id AND a.retired_version IS NULL), '[]') AS atoms_json,
 COALESCE((SELECT jsonb_agg(jsonb_build_object(
 'id', b.id, 'ref_id', b.ref_id, 'i', b.i, 'j', b.j, 'image', b.image,
 'bond_order', encode(float8send(b.bond_order::double precision), 'hex'),
 'kind', b.kind, 'provenance', b.provenance) ORDER BY b.id)::text
 FROM struct_bonds b WHERE b.ref_id=r.ref_id AND b.retired_version IS NULL), '[]') AS bonds_json
"""
RUN_COLUMNS = """
 s.id, s.ref_id, s.fidelity, s.status, s.model, s.on_version,
 s.converged, s.n_steps, encode(float8send(s.max_force), 'hex') AS max_force_hex,
 s.created_at, s.provenance, s.params->'completion' AS raw_completion
"""
# Fixed SQL field projection: intentionally excludes POSCAR, order, forces,
# charges, frames and final_geometry. No per-run cache lookup loop.
COMPLETION_COLUMNS = (
    GEOMETRY_COLUMNS
    + """,
 COALESCE((SELECT jsonb_agg(to_jsonb(run) ORDER BY run.created_at DESC, run.id DESC)::text
 FROM (SELECT """
    + RUN_COLUMNS
    + """ FROM struct_runs s WHERE s.ref_id=r.ref_id
 ORDER BY s.created_at DESC, s.id DESC LIMIT %(run_fetch)s) run), '[]') AS runs_json,
 (SELECT s.ref_id FROM struct_runs s WHERE s.id=%(selected_run_id)s) AS selected_owner,
 (SELECT to_jsonb(run)::text FROM (SELECT """
    + RUN_COLUMNS
    + """ FROM struct_runs s
 WHERE s.id=%(selected_run_id)s AND s.ref_id=r.ref_id) run) AS selected_json,
 COALESCE((SELECT jsonb_agg(to_jsonb(job) ORDER BY job.created_at DESC, job.ref_id DESC)::text
 FROM (SELECT j.ref_id, j.parent_id, j.created_at,
 j.meta->'struct_run_id' AS result_run_id,
 j.meta->>'failure_reason' AS failure_reason,
 (SELECT jsonb_agg(t.value ORDER BY t.value) FROM ref_tags rt JOIN tags t USING(tag_id)
 WHERE rt.ref_id=j.ref_id AND t.namespace='STATUS') AS statuses
 FROM refs j WHERE j.kind='job' AND j.retired_at IS NULL
 AND j.parent_id=r.ref_id AND j.meta->>'job_type'='struct_relax'
 AND j.meta->'params'->'structure_ref_id'=to_jsonb(r.ref_id)
 ORDER BY j.created_at DESC, j.ref_id DESC LIMIT %(request_fetch)s) job), '[]') AS requests_json
"""
)


def decode_json(value: str | None) -> Any:
    return (
        json.loads(value, parse_float=Decimal, parse_int=int)
        if value is not None
        else None
    )


def _binary(value: str | None) -> float | None:
    return struct.unpack("!d", bytes.fromhex(value))[0] if value is not None else None


def decode_geometry(row: dict[str, Any]) -> dict[str, Any]:
    meta = decode_json(row["meta_json"])
    atoms, bonds = decode_json(row["atoms_json"]), decode_json(row["bonds_json"])
    for atom in atoms:
        for key in ("fa", "fb", "fc", "magmom"):
            atom[key] = _binary(atom[key])
    for bond in bonds:
        bond["bond_order"] = _binary(bond["bond_order"])
    return {
        "ref_id": row["ref_id"],
        "meta": meta,
        "version": meta.get("version") if isinstance(meta, dict) else None,
        "lattice": meta.get("lattice") if isinstance(meta, dict) else None,
        "pbc": meta.get("pbc") if isinstance(meta, dict) else None,
        "atoms": atoms,
        "bonds": bonds,
    }


def _run(raw: dict[str, Any]) -> dict[str, Any]:
    receipt = raw["raw_completion"]
    return {
        "ref_id": raw["ref_id"],
        "run_id": raw["id"],
        "job_id": None,
        "request_id": None,
        "on_version": raw["on_version"],
        "created_at": raw["created_at"],
        "raw_status": raw["status"],
        "raw_fidelity": raw["fidelity"],
        "raw_model": raw["model"],
        "raw_converged": raw["converged"],
        "raw_max_force": _binary(raw["max_force_hex"]),
        "raw_n_steps": raw["n_steps"],
        "raw_provenance": raw["provenance"],
        "raw_completion": receipt,
        "receipt_assessment": {
            "status": "unavailable",
            "verified": None,
            "reason_codes": [
                "no-trusted-producer"
                if receipt is not None
                else "legacy-receipt-missing"
            ],
        },
    }


def _window(size: int, limit: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "returned": min(size, limit),
        "truncated": size > limit,
        "coverage": "bounded" if size > limit else "complete",
        "next_cursor": None,
    }


def completion_from_row(
    row: dict[str, Any],
    *,
    requested_display: DisplaySelector | None = None,
    selected_run_id: int | None = None,
    history_limit: int = 64,
    request_limit: int = 64,
) -> CompletionSnapshot:
    """Pure decoding/validation of one captured SQL row; never a second read."""
    raw = decode_geometry(row)
    geom = None
    reason = None
    try:
        geom = geometry_snapshot(raw)
    except (InvalidGeometry, CodecError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc)
    requested = validate_selector(
        requested_display if requested_display is not None else {"kind": "current"}
    )
    requested_run = requested.get("run_id")
    conflict = (
        selected_run_id is not None
        and requested_run is not None
        and selected_run_id != requested_run
    )
    selected_id = selected_run_id if selected_run_id is not None else requested_run
    if selected_id is not None:
        integer(selected_id, minimum=1)
    resolution: dict[str, Any] = {
        "status": "exact" if geom else "invalid",
        "geometry": geom["identity"] if geom else None,
        "artifact": None,
        "role": None,
        "reason": reason,
    }
    if requested["kind"] == "persisted":
        # Current rows can match the requested identity but cannot establish
        # their role in an unverified run. v1 has no trusted producer/loader.
        resolution.update(
            status="unsupported",
            geometry=None,
            role=requested["role"],
            reason="role-provenance-unavailable"
            if geom and requested["geometry"] == geom["identity"]
            else "historic-snapshot-unavailable",
        )
    elif requested["kind"] == "artifact":
        resolution.update(
            status="unsupported",
            geometry=None,
            artifact=requested["artifact"],
            role="unadopted-artifact",
            reason="artifact-rendering-unsupported",
        )
    if conflict:
        resolution.update(
            status="conflict", geometry=None, reason="conflicting-run-selection"
        )
    raw_runs = decode_json(row["runs_json"])
    runs = [_run(run) for run in raw_runs[:history_limit]]
    selected = {
        "requested_run_id": selected_id,
        "status": "not-requested",
        "record": None,
    }
    if selected_id is not None:
        owner = row["selected_owner"]
        selected["status"] = (
            "missing"
            if owner is None
            else "wrong-owner"
            if owner != row["ref_id"]
            else "found"
        )
        if selected["status"] == "found":
            selected["record"] = _run(decode_json(row["selected_json"]))
    jobs = decode_json(row["requests_json"])
    requests = []
    for job in jobs[:request_limit]:
        statuses = job["statuses"] or []
        status = (
            statuses[0]
            if len(statuses) == 1
            and statuses[0] in ("queued", "running", "succeeded", "failed", "cancelled")
            else "unknown"
        )
        result = job["result_run_id"]
        requests.append(
            {
                "request_id": job["ref_id"],
                "job_id": job["ref_id"],
                "owner_ref_id": row["ref_id"],
                "policy_id": None,
                "policy_digest": None,
                "input": None,
                "created_at": job["created_at"],
                "state": status,
                "failure_reason": job["failure_reason"],
                "result_run_id": result if type(result) is int else None,
                "lineage": None,
            }
        )
    meta = raw["meta"] if isinstance(raw["meta"], dict) else {}
    return cast(
        CompletionSnapshot,
        {
            "format": 1,
            "ref_id": row["ref_id"],
            "requested_display": requested,
            "display_resolution": resolution,
            "geometry": geom,
            "selected_geometry": None,
            "selected_geometry_role": None,
            "geometry_reason": reason,
            "raw_generated": meta.get("generated"),
            "raw_last_relax": meta.get("last_relax"),
            "runs": runs,
            "requests": requests,
            "selected_run": selected,
            "run_window": _window(len(raw_runs), history_limit),
            "request_window": _window(len(jobs), request_limit),
            "snapshot_consistency": "single-statement",
        },
    )


def read_completion_snapshot(
    store: Any,
    ref_id: int,
    *,
    conn: Connection | None = None,
    requested_display: DisplaySelector | None = None,
    selected_run_id: int | None = None,
    history_limit: int = 64,
    request_limit: int = 64,
) -> CompletionSnapshot | None:
    integer(ref_id, minimum=1)
    for limit in (history_limit, request_limit):
        integer(limit, minimum=1)
        if limit > 64:
            raise ValueError("completion histories are bounded at 64")
    requested = validate_selector(
        requested_display if requested_display is not None else {"kind": "current"}
    )
    effective_run = (
        selected_run_id if selected_run_id is not None else requested.get("run_id")
    )
    if effective_run is not None:
        integer(effective_run, minimum=1)
    params = {
        "ref_id": ref_id,
        "run_fetch": history_limit + 1,
        "request_fetch": request_limit + 1,
        "selected_run_id": effective_run,
    }
    with nullcontext(conn) if conn is not None else store.pool.connection() as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT "
                + COMPLETION_COLUMNS
                + " FROM refs r WHERE r.ref_id=%(ref_id)s AND r.kind='structure' AND r.retired_at IS NULL",
                params,
            )
            row = cur.fetchone()
    return (
        None
        if row is None
        else completion_from_row(
            row,
            requested_display=requested,
            selected_run_id=selected_run_id,
            history_limit=history_limit,
            request_limit=request_limit,
        )
    )
