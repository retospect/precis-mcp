"""Internal, SELECT-only PCB capture and transactional dev/test replay.

Public exports and content_hash deliberately project away raw geometry and router
checkpoints. A relational fixture preserves both without re-expanding a generator
or running authoring inference. IDs are local fixture keys; geometry is untouched.
No public op, schema, provider, or job is needed. Route parameters travel alongside
the copy, not as a runnable job. This is a source-version-bound diagnostic fixture,
not a general database backup or a promise of cross-version router determinism.
"""

from __future__ import annotations

import copy
import gzip
import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

# Dependency order also controls fresh identity allocation on replay.
TABLES = (
    "pcb_boards",
    "pcb_components",
    "pcb_pins",
    "pcb_instances",
    "pcb_nets",
    "pcb_netconns",
    "pcb_net_classes",
    "pcb_local_footprints",
    "pcb_generators",
    "pcb_features",
    "pcb_measures",
    "pcb_routes",
    "pcb_planes",
    "pcb_pin_swaps",
    "pcb_fixed_copper",
    "pcb_copper",
    "parts",
    "part_footprints",
)
IDENTITIES = {
    "pcb_boards": "board_id",
    "pcb_components": "component_id",
    "pcb_pins": "pin_id",
    "pcb_instances": "instance_id",
    "pcb_nets": "net_id",
    "pcb_netconns": "netconn_id",
    "pcb_net_classes": "class_id",
    "pcb_features": "feature_id",
    "pcb_measures": "measure_id",
    "pcb_routes": "route_id",
    "pcb_planes": "plane_id",
    "pcb_pin_swaps": "swap_id",
    "pcb_fixed_copper": "fixed_id",
    "pcb_copper": "copper_id",
}
VOLATILE = {"created_at", "updated_at", "generated_at", "fetched_at", "refreshed_at"}


def export_query(slug: str) -> str:
    """One SELECT for prod-psql --ro; no procedures, writes or session SETs."""
    literal = "'" + slug.replace("'", "''") + "'"
    fields = []
    for table in TABLES:
        if table == "pcb_pins":
            where = "component_id IN (SELECT component_id FROM pcb_components WHERE ref_id=s.ref_id)"
        elif table == "pcb_netconns":
            where = "net_id IN (SELECT net_id FROM pcb_nets WHERE ref_id=s.ref_id)"
        elif table in {"pcb_routes", "pcb_planes", "pcb_pin_swaps", "pcb_copper"}:
            where = (
                "board_id IN (SELECT board_id FROM pcb_boards WHERE ref_id=s.ref_id)"
            )
        elif table in {"parts", "part_footprints"}:
            where = (
                "lcsc IN (SELECT part_lcsc FROM pcb_components WHERE ref_id=s.ref_id)"
            )
        else:
            where = "ref_id=s.ref_id"
        order = IDENTITIES.get(
            table, "lcsc" if table in {"parts", "part_footprints"} else "name"
        )
        fields += [
            f"'{table}'",
            f"(SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY {order}), '[]'::jsonb) FROM {table} t WHERE {where})",
        ]
    return (
        "WITH s AS (SELECT r.ref_id,r.title,r.meta FROM refs r JOIN ref_identifiers i USING(ref_id) "
        f"WHERE r.kind='pcb' AND i.id_kind='cite_key' AND i.id_value={literal}) "
        "SELECT jsonb_build_object('design',to_jsonb(s),'tables',jsonb_build_object("
        + ",".join(fields)
        + "),'route_params',COALESCE((SELECT j.meta->'params' FROM refs j WHERE j.kind='job' "
        "AND j.meta->>'job_type'='pcb_route' AND j.meta->'params'->>'pcb_ref_id'=s.ref_id::text "
        "ORDER BY j.ref_id DESC LIMIT 1),'{}'::jsonb)) FROM s;"
    )


def _json_config(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(_json_config(k)): _json_config(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_config(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_json_config(v) for v in value]
    return value


def route_config(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Capture current worker defaults; dynamic constraints remain in raw rows.

    This binds the replay engine at capture time, not the unknown historical
    engine which produced the stored copper. The copy is read before rerouting.
    """
    from precis.pcb.capabilities import capability_for
    from precis.pcb.drc import process_for_stackup
    from precis.pcb.optimize import OptimizeConfig
    from precis.pcb.realize import RealizeConfig

    stackup = tables["pcb_boards"][0]["stackup"]
    realize = RealizeConfig(
        fab_caps=capability_for(process_for_stackup(stackup)),
        class_rules={
            r["name"]: r["rules"]
            for r in tables["pcb_net_classes"]
            if not r.get("retired_at")
        },
        negotiate_iterations=int(params.get("negotiate") or 0),
    )
    optimize = OptimizeConfig(
        iters=int(params.get("iters") or 3000), seed=int(params.get("seed") or 0)
    )
    return {
        "realize": _json_config(asdict(realize)),
        "optimize_defaults": _json_config(asdict(optimize)),
    }


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize relational identity only; never alter nested routing data."""
    result = copy.deepcopy(raw)
    tables = result["tables"]
    if set(tables) != set(TABLES) or len(tables["pcb_boards"]) != 1:
        raise ValueError(
            "snapshot requires exactly one board and the complete v1 table set"
        )
    maps = {"ref_id": {result["design"]["ref_id"]: 1}}
    for table, key in IDENTITIES.items():
        tables[table].sort(key=lambda r: r[key])
        maps[key] = {r[key]: i for i, r in enumerate(tables[table], 1)}
    for rows in tables.values():
        for row in rows:
            for key in list(row):
                if key in VOLATILE or key == "description_tsv":
                    del row[key]
                elif key in maps and row[key] is not None:
                    try:
                        row[key] = maps[key][row[key]]
                    except KeyError as exc:
                        raise ValueError(
                            f"external dependency: {key}={row[key]}"
                        ) from exc
    result["design"].pop("ref_id")
    params = result["route_params"]
    params.pop("pcb_ref_id", None)
    result["route_config"] = route_config(tables, params)
    result["format"] = "precis-pcb-replay"
    result["version"] = 1
    return result


def export_snapshot(
    conn: Connection,
    slug: str,
    *,
    route_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export at one statement snapshot; caller may additionally use READ ONLY."""
    rows = conn.execute(export_query(slug)).fetchall()
    if len(rows) != 1:
        raise ValueError("expected one existing PCB slug")
    raw = rows[0][0]
    if route_params is not None:
        raw["route_params"] = route_params
    return normalize(raw)


def load_snapshot(
    conn: Connection, fixture: dict[str, Any], slug: str
) -> tuple[int, dict[str, Any]]:
    """Insert a fresh dev/test design atomically; return its ID and replay params.

    Re-export with the returned params: no jobs are cloned/enqueued. Shared cached
    geometry must already equal the fixture or be absent, never overwritten.
    """
    if (
        not slug
        or fixture.get("format") != "precis-pcb-replay"
        or fixture.get("version") != 1
    ):
        raise ValueError("new slug and supported snapshot version required")
    if set(fixture.get("tables", {})) != set(TABLES):
        raise ValueError("incomplete or unknown snapshot tables")
    if len(fixture["tables"]["pcb_boards"]) != 1:
        raise ValueError("snapshot requires exactly one physical board")
    with conn.transaction():
        db_row = conn.execute("SELECT current_database()").fetchone()
        assert db_row is not None
        db = db_row[0]
        if db != "precis" and db != "precis_test" and not db.startswith("precis_test_"):
            raise ValueError(
                "replay is restricted to precis dev or precis_test databases"
            )
        if conn.execute(
            "SELECT 1 FROM ref_identifiers WHERE id_kind=%s AND id_value=%s",
            ("cite_key", slug),
        ).fetchone():
            raise ValueError(
                "target slug already exists; replay never replaces a design"
            )
        design = fixture["design"]
        ref_row = conn.execute(
            "INSERT INTO refs(kind,title,meta) VALUES ('pcb',%s,%s) RETURNING ref_id",
            (design["title"], Jsonb(design["meta"])),
        ).fetchone()
        assert ref_row is not None
        ref_id = int(ref_row[0])
        conn.execute(
            "INSERT INTO ref_identifiers(id_kind,id_value,ref_id) VALUES (%s,%s,%s)",
            ("cite_key", slug, ref_id),
        )
        maps: dict[str, dict[int, int]] = {"ref_id": {1: ref_id}}
        for table in TABLES:
            key = IDENTITIES.get(table)
            for original in fixture["tables"][table]:
                row = copy.deepcopy(original)
                old_id = row.pop(key) if key else None
                for column, mapping in maps.items():
                    if column in row and row[column] is not None:
                        row[column] = mapping[row[column]]
                if table in {"parts", "part_footprints"}:
                    existing = conn.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE lcsc=%s").format(
                            sql.Identifier(table)
                        ),
                        (row["lcsc"],),
                    ).fetchone()
                    if existing:
                        cached = {
                            k: v
                            for k, v in existing[0].items()
                            if k not in VOLATILE | {"description_tsv"}
                        }
                        if cached != row:
                            raise ValueError(
                                f"conflicting {table} cache for {row['lcsc']}; use an isolated test DB"
                            )
                        continue
                columns = list(row)
                statement = sql.SQL(
                    "INSERT INTO {table} ({columns}) SELECT {columns} FROM jsonb_populate_record(NULL::{table}, %s)"
                ).format(
                    table=sql.Identifier(table),
                    columns=sql.SQL(",").join(map(sql.Identifier, columns)),
                )
                if key:
                    statement += sql.SQL(" RETURNING {}").format(sql.Identifier(key))
                cursor = conn.execute(statement, (Jsonb(row),))
                if key:
                    inserted = cursor.fetchone()
                    assert inserted is not None and old_id is not None
                    maps.setdefault(key, {})[int(old_id)] = int(inserted[0])
        params = copy.deepcopy(fixture["route_params"])
        params["pcb_ref_id"] = ref_id
        return ref_id, params


def canonical_json(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def read_snapshot(path: Path) -> dict[str, Any]:
    """Read versioned JSON, optionally gzip-compressed to keep real fixtures small."""
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return json.loads(data)
