"""Coherent all-block completion capture, without hydration or geometry work.

The existing load_tree uses several statements and may resolve foreign objects
later. Completion needs one captured inventory/binding/structure state instead.
Hidden blocks are included. Unsupported templates/mixed bindings remain unknown;
independent block evidence never means the physical assembly was relaxed.
"""

from __future__ import annotations

import struct
from contextlib import nullcontext
from typing import Any, cast

from psycopg import Connection
from psycopg.rows import dict_row

from precis.store._structure_completion import (
    COMPLETION_COLUMNS,
    completion_from_row,
    decode_json,
)
from precis.structure.completion import (
    completion_digest,
    completion_envelope,
    se_completion_envelope,
)
from precis.structure.completion_codec import CodecError, dumps_wire
from precis.structure.completion_identity import InvalidGeometry, exact_token, integer
from precis.structure.completion_types import SeCompletionSnapshot

MAX_BLOCKS = 256
SE_ARGS = frozenset({"shown_design_token"})


def se_completion_snapshot(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> SeCompletionSnapshot | None:
    integer(ref_id, minimum=1)
    sql = (
        """
    WITH owner AS (SELECT ref_id, meta FROM refs WHERE ref_id=%(ref_id)s
        AND kind='se' AND retired_at IS NULL),
    blocks AS (SELECT b.id, b.ref_id, b.uid, b.parent_block_id, b.name,
        b.bound_kind, b.bound_design, b.mode,
        ARRAY(SELECT encode(float8send(v), 'hex') FROM unnest(b.pose_xyz) v) AS pose_xyz_hex,
        ARRAY(SELECT encode(float8send(v), 'hex') FROM unnest(b.pose_rot) v) AS pose_rot_hex,
        b.template_ref, b.template_uid, b.array_spec
        FROM se_blocks b JOIN owner o ON o.ref_id=b.ref_id WHERE b.retired_at IS NULL
        ORDER BY b.id LIMIT %(block_fetch)s),
    ports AS (SELECT p.id, p.block_id, p.name, p.bound_design, p.bound_atom,
        p.axis_atom, p.phase_atom FROM se_ports p JOIN blocks b ON b.id=p.block_id
        WHERE p.retired_at IS NULL),
    bindings AS (SELECT bound_design AS slug FROM blocks WHERE bound_kind='structure'
        UNION SELECT bound_design AS slug FROM ports),
    structures AS (SELECT """
        + COMPLETION_COLUMNS
        + """ FROM refs r WHERE r.kind='structure'
        AND r.retired_at IS NULL AND EXISTS (SELECT 1 FROM ref_identifiers i
          JOIN bindings b ON b.slug=i.id_value WHERE i.ref_id=r.ref_id AND i.id_kind='cite_key'))
    SELECT o.ref_id, o.meta::text AS meta_json,
        COALESCE((SELECT jsonb_agg(to_jsonb(b) ORDER BY b.id)::text FROM blocks b), '[]') AS blocks_json,
        COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id)::text FROM ports p), '[]') AS ports_json,
        COALESCE((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.ref_id)::text FROM structures s), '[]') AS structures_json,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('slug', i.id_value, 'ref_id', i.ref_id)
            ORDER BY i.id_value)::text FROM ref_identifiers i JOIN structures s ON s.ref_id=i.ref_id
            WHERE i.id_kind='cite_key'), '[]') AS names_json
    FROM owner o
    """
    )
    params = {
        "ref_id": ref_id,
        "block_fetch": MAX_BLOCKS + 1,
        "run_fetch": 65,
        "request_fetch": 65,
        "selected_run_id": None,
    }
    with nullcontext(conn) if conn is not None else store.pool.connection() as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    if row is None:
        return None
    rows = decode_json(row["blocks_json"])
    for block in rows:
        for field in ("pose_xyz", "pose_rot"):
            block[field] = [
                struct.unpack("!d", bytes.fromhex(v))[0]
                for v in block.pop(field + "_hex")
            ]
    ports = decode_json(row["ports_json"])
    meta = decode_json(row["meta_json"])
    reasons: list[str] = []
    complete = len(rows) <= MAX_BLOCKS
    if not complete:
        reasons.append("block-inventory-exceeds-read-budget")
    uids: dict[int, int] = {}
    try:
        for block in rows:
            uid = integer(block["uid"], minimum=1)
            if uid in uids.values():
                raise InvalidGeometry("duplicate block UID")
            uids[block["id"]] = uid
    except InvalidGeometry:
        return {
            "format": 1,
            "latest_design": None,
            "design": None,
            "reason_codes": ["invalid-block-identity"],
            "complete_inventory": False,
            "snapshot_consistency": "single-statement",
            "blocks": [],
        }
    snapshots = {
        s["ref_id"]: completion_from_row(s) for s in decode_json(row["structures_json"])
    }
    names = {n["slug"]: n["ref_id"] for n in decode_json(row["names_json"])}
    blocks = []
    for block in rows:
        owned_ports = [p for p in ports if p["block_id"] == block["id"]]
        bound = block["bound_design"] if block["bound_kind"] == "structure" else None
        refs = {names.get(p["bound_design"]) for p in owned_ports if p["bound_design"]}
        if bound:
            refs.add(names.get(bound))
        candidate = next(iter(refs)) if len(refs) == 1 and None not in refs else None
        atomistic = (
            block["bound_kind"] == "structure"
            or bool(refs)
            or block["mode"] == "atomic"
        )
        unknown = bool(
            block["template_ref"] or block["template_uid"] or block["array_spec"]
        )
        non_atomistic = block["bound_kind"] in ("cad", "component", "part") and bool(
            block["bound_design"]
        )
        applicability = (
            "unknown"
            if unknown
            else "atomistic"
            if atomistic
            else "non-atomistic"
            if non_atomistic
            else "unknown"
        )
        snapshot = snapshots.get(candidate)
        geometry = (
            snapshot["geometry"]["identity"]
            if snapshot and snapshot["geometry"]
            else None
        )
        binding_status = (
            "not-applicable"
            if applicability == "non-atomistic"
            else "exact"
            if candidate and geometry and not unknown
            else "dangling"
            if None in refs or bound
            else "unknown"
        )
        port_bindings = [
            {
                "row_id": p["id"],
                "name": p["name"],
                "bound_design": p["bound_design"],
                "bound_atom": p["bound_atom"],
                "axis_atom": p["axis_atom"],
                "phase_atom": p["phase_atom"],
                "resolved_structure_id": names.get(p["bound_design"]),
            }
            for p in owned_ports
        ]
        binding_token = (
            exact_token(
                "se.structure-binding",
                {
                    "block": block,
                    "ports": port_bindings,
                    "resolved_structure_id": candidate,
                    "geometry": geometry,
                },
            )
            if binding_status == "exact"
            else None
        )
        blocks.append(
            {
                "block_uid": block["uid"],
                "row_id": block["id"],
                "name": block["name"],
                "parent_uid": uids.get(block["parent_block_id"]),
                "applicability": applicability,
                "applicability_reason": "template-or-array-not-expanded"
                if unknown
                else None,
                "bound_kind": block["bound_kind"],
                "bound_design": block["bound_design"],
                "port_bindings": port_bindings,
                "resolved_structure_id": candidate,
                "binding_status": binding_status,
                "binding_token": binding_token,
                "geometry": geometry,
                "completion": completion_envelope(snapshot)
                if snapshot and binding_status == "exact"
                else None,
                "hidden": None,
                "isolated": None,
            }
        )
    design = None
    if complete:
        try:
            token = exact_token(
                "se.design",
                {
                    "ref_id": ref_id,
                    "meta": meta,
                    "blocks": rows,
                    "ports": ports,
                    "bindings": [
                        {"uid": b["block_uid"], "binding": b["binding_token"]}
                        for b in blocks
                    ],
                },
            )
            revision = meta.get("version") if isinstance(meta, dict) else None
            if revision is not None:
                integer(revision, minimum=0)
            design = {
                "ref_id": ref_id,
                "revision": revision,
                "token": token,
                "source": "live-statement",
            }
        except (CodecError, InvalidGeometry):
            reasons.append("invalid-design-identity")
            complete = False
    return cast(
        SeCompletionSnapshot,
        {
            "format": 1,
            "latest_design": design,
            "design": design,
            "reason_codes": reasons,
            "complete_inventory": complete,
            "snapshot_consistency": "single-statement",
            "blocks": blocks,
        },
    )


def read_se_completion(
    store: Any, ref_id: int, args: dict[str, Any], *, digest: bool = False
) -> str | None:
    if set(args) - SE_ARGS:
        raise ValueError("unsupported SE completion read argument")
    shown = args.get("shown_design_token")
    if shown is not None and type(shown) is not str:
        raise ValueError("shown_design_token must be a string")
    snapshot = se_completion_snapshot(store, ref_id)
    if snapshot is None:
        return None
    envelope = se_completion_envelope(snapshot, shown_design_token=shown)
    return completion_digest(envelope) if digest else dumps_wire(envelope)


__all__ = ["read_se_completion", "se_completion_envelope", "se_completion_snapshot"]
