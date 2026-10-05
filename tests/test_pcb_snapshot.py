"""Faithful relational replay, observable route-status, and atomic refusal."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb.snapshot import (
    TABLES,
    export_query,
    export_snapshot,
    load_snapshot,
    read_snapshot,
)


def _fixture():
    tables: dict[str, list[dict[str, Any]]] = {table: [] for table in TABLES}
    tables.update(
        {
            "pcb_boards": [
                {
                    "board_id": 1,
                    "ref_id": 1,
                    "stackup": [
                        {"name": "F.Cu", "role": "signal"},
                        {"name": "B.Cu", "role": "signal"},
                    ],
                }
            ],
            "pcb_components": [
                {"component_id": 1, "ref_id": 1, "label": "ASYM", "footprint": "ASYM"}
            ],
            "pcb_pins": [{"pin_id": 1, "component_id": 1, "name": "CLK", "pad": "2"}],
            "pcb_instances": [
                {
                    "instance_id": 1,
                    "ref_id": 1,
                    "board_id": 1,
                    "component_id": 1,
                    "refdes": "J1",
                    "x": 1.23456789,
                    "y": -2,
                    "rot": 90,
                    "layer": "bottom",
                    "fixed": "both",
                }
            ],
            "pcb_nets": [
                {
                    "net_id": 1,
                    "ref_id": 1,
                    "name": "CLOCK",
                    "net_class": "signal",
                    "working_voltage_v": 250,
                }
            ],
            "pcb_netconns": [
                {
                    "netconn_id": 1,
                    "net_id": 1,
                    "instance_id": 1,
                    "component_id": 1,
                    "pin_id": 1,
                }
            ],
            "pcb_net_classes": [
                {
                    "class_id": 1,
                    "ref_id": 1,
                    "name": "signal",
                    "rules": {"clearance_mm": 0.099, "layers": ["B.Cu"]},
                }
            ],
            "pcb_local_footprints": [
                {
                    "ref_id": 1,
                    "name": "ASYM",
                    "pads": [
                        {
                            "number": "2",
                            "x": 0,
                            "y": 1,
                            "w": 0.6,
                            "h": 0.8,
                            "polygon": [[0, 0], [1, 0], [1, 1]],
                        }
                    ],
                    "pin_map": {"2": {"name": "CLK"}},
                }
            ],
            "pcb_generators": [
                {
                    "ref_id": 1,
                    "name": "G1",
                    "generator": "ewod_pad_array",
                    "params": {"grid": [2, 3]},
                    "refdes": "J1",
                    "ledger": {"terminal": "CLK"},
                }
            ],
            "pcb_features": [
                {
                    "feature_id": 1,
                    "board_id": 1,
                    "ref_id": 1,
                    "ftype": "outline",
                    "geom": {"polygon": [[-5, -5], [5, -5], [5, 5], [-5, 5]]},
                }
            ],
            "pcb_measures": [
                {
                    "measure_id": 1,
                    "ref_id": 1,
                    "metric": "separation",
                    "operands": [{"refdes": "J1"}],
                    "meta": {"snapped": True},
                }
            ],
            "pcb_routes": [
                {
                    "route_id": 1,
                    "board_id": 1,
                    "net_id": 1,
                    "status": "failed",
                    "fail": {"reason": "no_path"},
                    "tree": {"a": "J1.CLK"},
                    "topology": {},
                    "layer_assign": {},
                }
            ],
            "pcb_planes": [
                {
                    "plane_id": 1,
                    "board_id": 1,
                    "net_id": 1,
                    "layer": "B.Cu",
                    "meta": {"source": "authored"},
                }
            ],
            "pcb_pin_swaps": [
                {
                    "swap_id": 1,
                    "board_id": 1,
                    "net_id": 1,
                    "instance_id": 1,
                    "component_id": 1,
                    "pin_id": 1,
                    "meta": {"source": "derived"},
                }
            ],
            "pcb_fixed_copper": [
                {
                    "fixed_id": 1,
                    "board_id": 1,
                    "ref_id": 1,
                    "net_id": 1,
                    "generator_name": "G1",
                    "generator": "ewod_pad_array",
                    "ctype": "via",
                    "layer": "B.Cu",
                    "geom": {
                        "x": 0,
                        "y": 0,
                        "terminal": {"refdes": "J1", "pin": "CLK"},
                    },
                }
            ],
            "pcb_copper": [
                {
                    "copper_id": 1,
                    "board_id": 1,
                    "net_id": 1,
                    "route_id": 1,
                    "ctype": "track",
                    "layer": "B.Cu",
                    "geom": {"points": [[0, 0], [1, 2]], "width_mm": 0.2},
                }
            ],
        }
    )
    return {
        "format": "precis-pcb-replay",
        "version": 1,
        "design": {"title": "Synthetic replay", "meta": {"last_route": {"failed": 1}}},
        "tables": tables,
        "route_params": {"seed": 7, "iters": 100, "negotiate": 0},
    }


def test_all_surfaces_roundtrip_and_readback(store):
    with store.pool.connection() as conn:
        source_id, params = load_snapshot(conn, _fixture(), "snapshot-source")
        first = export_snapshot(conn, "snapshot-source", route_params=params)
        # Sequence allocation differs, but relations, exact geometry and state do not.
        copy_id, copy_params = load_snapshot(conn, first, "snapshot-copy")
        assert copy_id != source_id
        second = export_snapshot(conn, "snapshot-copy", route_params=copy_params)
        assert second == first
        assert second["tables"]["pcb_local_footprints"][0]["pads"][0]["w"] == 0.6
        assert second["tables"]["pcb_instances"][0]["x"] == 1.23456789
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind='job'").fetchone()[0]
            == 0
        )
    response = PcbHandler(hub=Hub(store=store)).get(
        id="snapshot-copy", view="route-status"
    )
    assert "1 failed" in response.body and "CLOCK" in response.body


def test_existing_slug_refused_without_changes(store):
    with store.pool.connection() as conn:
        _, params = load_snapshot(conn, _fixture(), "snapshot-source")
        before = export_snapshot(conn, "snapshot-source", route_params=params)
        with pytest.raises(ValueError, match="already exists"):
            load_snapshot(conn, _fixture(), "snapshot-source")
        assert export_snapshot(conn, "snapshot-source", route_params=params) == before


def test_late_cache_conflict_rolls_back_every_design_row(store):
    fixture = _fixture()
    fixture["tables"]["part_footprints"] = [
        {"lcsc": "C123", "pads": [{"number": "1", "w": 1}]}
    ]
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO part_footprints(lcsc,pads) VALUES (%s,%s)", ("C123", Jsonb([]))
        )
        conn.commit()
        with pytest.raises(ValueError, match="conflicting part_footprints"):
            load_snapshot(conn, fixture, "snapshot-refused")
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind='pcb'").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM pcb_copper").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT pads FROM part_footprints WHERE lcsc=%s", ("C123",)
            ).fetchone()[0]
            == []
        )


@pytest.mark.parametrize("cache", ["parts", "part_footprints"])
def test_source_absent_referenced_cache_refuses_atomically(store, cache):
    fixture = read_snapshot(Path("tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz"))
    assert "C639448" in {
        c.get("part_lcsc") for c in fixture["tables"]["pcb_components"]
    }
    # Actual source lacks parts[C639448]. The second case also makes its
    # referenced footprint uncached; target geometry must not fill that gap.
    fixture["tables"][cache] = []
    with store.pool.connection() as conn:
        if cache == "parts":
            conn.execute(
                "INSERT INTO parts(lcsc,basic) VALUES (%s,false)", ("C639448",)
            )
        else:
            conn.execute(
                "INSERT INTO part_footprints(lcsc,pads) VALUES (%s,%s)",
                ("C639448", Jsonb([{"number": "1", "w": 9, "h": 8}])),
            )
        conn.commit()
        tables = ("refs", "ref_identifiers", *TABLES)
        before = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        target_cache = conn.execute(
            f"SELECT to_jsonb(t) FROM {cache} t WHERE lcsc=%s", ("C639448",)
        ).fetchone()[0]
        with pytest.raises(
            ValueError, match=f"source-absent {cache} cache for C639448"
        ):
            load_snapshot(conn, fixture, "snapshot-negative-cache")
        assert {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        } == before
        assert (
            conn.execute(
                "SELECT 1 FROM ref_identifiers WHERE id_kind='cite_key' AND id_value=%s",
                ("snapshot-negative-cache",),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                f"SELECT to_jsonb(t) FROM {cache} t WHERE lcsc=%s", ("C639448",)
            ).fetchone()[0]
            == target_cache
        )


@pytest.mark.parametrize("part_lcsc", [None, "C123"])
def test_unreferenced_target_caches_do_not_block_replay(store, part_lcsc):
    fixture = _fixture()
    fixture["tables"]["pcb_components"][0]["part_lcsc"] = part_lcsc
    # NULL components and source-absent references do not claim unrelated keys.
    with store.pool.connection() as conn:
        _, params = load_snapshot(conn, fixture, "snapshot-cache-source")
        fixture = export_snapshot(conn, "snapshot-cache-source", route_params=params)
        conn.execute("INSERT INTO parts(lcsc,basic) VALUES ('C999',false)")
        conn.execute(
            "INSERT INTO part_footprints(lcsc,pads) VALUES (%s,%s)",
            ("C999", Jsonb([])),
        )
        conn.commit()
        _, params = load_snapshot(conn, fixture, "snapshot-unrelated-cache")
        assert (
            export_snapshot(conn, "snapshot-unrelated-cache", route_params=params)
            == fixture
        )


@pytest.mark.parametrize("change", ["version", "table"])
def test_invalid_format_refused(store, change):
    fixture = copy.deepcopy(_fixture())
    if change == "version":
        fixture["version"] = 99
    else:
        fixture["tables"]["refs"] = []
    with store.pool.connection() as conn:
        with pytest.raises(ValueError):
            load_snapshot(conn, fixture, "snapshot-invalid")
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind='pcb'").fetchone()[0]
            == 0
        )


def test_export_query_quotes_slug_and_is_one_select():
    query = export_query("single'board")
    assert "single''board" in query
    assert query.startswith("WITH s AS (SELECT")
    assert query.count(";") == 1
    assert not any(
        word in query.upper() for word in ("INSERT ", "UPDATE ", "DELETE ", "CALL ")
    )


def test_exact_ewod_copy_matches_snapshot_and_stored_route_summary(store):
    fixture = read_snapshot(
        Path(
            os.environ.get(
                "PRECIS_PCB_REPLAY_FIXTURE",
                "tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz",
            )
        )
    )
    with store.pool.connection() as conn:
        _, params = load_snapshot(conn, fixture, "ewod-dogfood-6-replay")
        assert (
            export_snapshot(conn, "ewod-dogfood-6-replay", route_params=params)
            == fixture
        )
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind='job'").fetchone()[0]
            == 0
        )
    handler = PcbHandler(hub=Hub(store=store))
    response = handler.get(id="ewod-dogfood-6-replay", view="route-status")
    assert "22 routed, 33 failed, 3 dangling" in response.body
    ref = store.get_ref(kind="pcb", id="ewod-dogfood-6-replay")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    assert len(graph["instances"]) == 2 and len(graph["nets"]) == 58


def test_production_target_denied_before_any_write():
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("precis_prod",)
    with pytest.raises(ValueError, match="restricted"):
        load_snapshot(conn, _fixture(), "dogfood-copy")
    conn.execute.assert_called_once_with("SELECT current_database()")
