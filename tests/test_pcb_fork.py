"""``op='fork'`` — the lossless board copy (gr517199).

Covers the round-trip the ``.epro2`` re-import loses: part/LCSC fields,
single-pin nets, routed copper (``topology``/``layer_assign``) and the
board's own provenance stamp.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc as pcb_drc
from precis.pcb.snapshot import VOLATILE, load_snapshot
from precis.store._pcb_ops import _FORK_SCOPES, _FORK_TABLES
from tests.test_pcb_snapshot import _fixture

#: Columns a copy re-issues or re-points — every key the fork remaps,
#: plus the jsonb columns and derived keys that embed remapped ids
#: (``operands``/``objects`` carry feature/fixed ids; ``finding_key``
#: hashes them). Field-level equality asserts on every column NOT in
#: this set; the embedded ids are checked through ``_natural_state``.
_REMAPPED = {
    "ref_id",
    "board_id",
    "component_id",
    "pin_id",
    "instance_id",
    "net_id",
    "netconn_id",
    "class_id",
    "feature_id",
    "room_id",
    "measure_id",
    "role_id",
    "route_id",
    "plane_id",
    "snapshot_id",
    "fixed_id",
    "copper_id",
    "finding_id",
    "id",
    "operands",
    "objects",
    "finding_key",
}

#: FK columns each fork table carries -> the surrogate key they name
#: (``room_id`` is the one alias: it names ``pcb_features.feature_id``).
#: The verbatim test asserts none of the copy's values points back at a
#: source-owned row, and all of them land inside the copy.
_FORK_FKS: dict[str, dict[str, str]] = {
    "pcb_pins": {"component_id": "component_id"},
    "pcb_features": {"board_id": "board_id"},
    "pcb_instances": {
        "board_id": "board_id",
        "component_id": "component_id",
        "room_id": "feature_id",
    },
    "pcb_netconns": {
        "net_id": "net_id",
        "instance_id": "instance_id",
        "component_id": "component_id",
        "pin_id": "pin_id",
    },
    "pcb_routes": {"board_id": "board_id", "net_id": "net_id"},
    "pcb_planes": {"board_id": "board_id", "net_id": "net_id"},
    "pcb_netlist_snapshots": {"board_id": "board_id"},
    "pcb_fixed_copper": {"board_id": "board_id", "net_id": "net_id"},
    "pcb_copper": {
        "board_id": "board_id",
        "net_id": "net_id",
        "route_id": "route_id",
    },
    "pcb_drc_runs": {"board_id": "board_id"},
    "pcb_drc_findings": {"board_id": "board_id"},
}

#: ``design_*`` versioning tables the fork deliberately leaves behind —
#: a fork is a new independent design, not a branch of its source
#: (GRIPE_FIX ruling on the gr517199 review).
_DESIGN_HISTORY = {
    "design_block_state",
    "design_branches",
    "design_checkpoints",
    "design_envelope_revisions",
    "design_load_case_exemptions",
    "design_revisions",
    "design_scenario_link",
    "design_situations",
    "design_states",
    "design_transitions",
}


def _source(store: Any, slug: str = "fork-src") -> int:
    """A snapshot-replay source with LCSC part, single-pin + floating nets,
    routed copper, a via, a room-bound instance, a feature-operand measure,
    a role, an argue-ledger note and a DRC run with a fixed-id finding."""
    fixture = _fixture()
    fixture["tables"]["pcb_components"][0]["part_lcsc"] = "C123"
    tables = fixture["tables"]
    tables["pcb_features"].append(
        {
            "feature_id": 2,
            "board_id": 1,
            "ref_id": 1,
            "ftype": "room",
            "geom": {"polygon": [[-1, -1], [1, -1], [1, 1], [-1, 1]]},
            "x": 0.0,
            "y": 0.0,
        }
    )
    tables["pcb_instances"][0]["room_id"] = 2
    tables["pcb_pins"].append(
        {"pin_id": 2, "component_id": 1, "name": "SIG", "pad": "5"}
    )
    tables["pcb_nets"] += [
        {"net_id": 2, "ref_id": 1, "name": "SINGLE", "net_class": "signal"},
        {"net_id": 3, "ref_id": 1, "name": "FLOAT", "note": "no pins"},
    ]
    tables["pcb_netconns"].append(
        {
            "netconn_id": 2,
            "net_id": 2,
            "instance_id": 1,
            "component_id": 1,
            "pin_id": 2,
        }
    )
    tables["pcb_routes"][0]["topology"] = {"terminals": ["J1.2"]}
    tables["pcb_routes"][0]["layer_assign"] = {"J1.2": "B.Cu"}
    tables["pcb_copper"].append(
        {
            "copper_id": 2,
            "board_id": 1,
            "net_id": 1,
            "route_id": 1,
            "ctype": "via",
            "layer": "B.Cu",
            "geom": {"x": 3, "y": 4, "drill": 0.3},
        }
    )
    with store.pool.connection() as conn:
        ref_id, _params = load_snapshot(conn, fixture, slug)
        # Rows whose values must name the design's REAL allocated ids go
        # in after the replay: the align measure's feature operand and
        # the via_pad_keepout finding's fixed-copper id.
        feature_id = conn.execute(
            "SELECT feature_id FROM pcb_features WHERE ref_id = %s AND ftype = 'room'",
            (ref_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pcb_measures "
            "(ref_id, metric, direction, strength, operands, reason) "
            "VALUES (%s, 'align', 'target', 'soft', %s, 'J1 to room edge')",
            (ref_id, Jsonb([{"refdes": "J1"}, {"feature_id": feature_id}])),
        )
        fixed_id = conn.execute(
            "SELECT fixed_id FROM pcb_fixed_copper WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()[0]
        objects = [
            {
                "via_fixed_id": fixed_id,
                "via_net": "CLOCK",
                "pad_net": "CLOCK",
                "pad_refdes": "J1",
                "pad_pin": "CLK",
                "pad_layer": "B.Cu",
            }
        ]
        where = "via[CLOCK] @ (0,0) <-> pad J1/CLK on B.Cu"
        finding_key = pcb_drc.finding_key(
            pcb_drc.DrcFinding(
                rule="via_pad_keepout",
                severity="error",
                where=where,
                detail="via in pad",
                objects=tuple(objects),
            )
        )
        conn.execute(
            "INSERT INTO pcb_roles (ref_id, name, voltage_v) VALUES (%s, 'vcc', 3.3)",
            (ref_id,),
        )
        conn.execute(
            "INSERT INTO pcb_notes (ref_id, name, kind, body) "
            "VALUES (%s, 'n1', 'decision', 'keep the guard ring')",
            (ref_id,),
        )
        conn.execute(
            "INSERT INTO pcb_drc_runs (board_id, run_id, n_error, n_warn) "
            "SELECT board_id, 'r1', 0, 1 FROM pcb_boards WHERE ref_id = %s",
            (ref_id,),
        )
        conn.execute(
            "INSERT INTO pcb_drc_findings "
            "(board_id, run_id, rule, severity, objects, detail, "
            " finding_key, location, margin_mm) "
            "SELECT board_id, 'r1', 'via_pad_keepout', 'error', %s, "
            "       'via in pad', %s, %s, -0.5 "
            "FROM pcb_boards WHERE ref_id = %s",
            (Jsonb(objects), finding_key, where, ref_id),
        )
        conn.execute(
            "INSERT INTO pcb_drc_findings "
            "(board_id, run_id, rule, severity, objects, detail) "
            "SELECT board_id, 'r1', 'clearance', 'warn', '{}'::jsonb, 'tight' "
            "FROM pcb_boards WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()
    return ref_id


def _project(conn: Any, table: str, scope_sql: str, ref_id: int) -> list[str]:
    """Rows of ``table`` for ``ref_id`` with every remapped/volatile column
    stripped — what a lossless copy must reproduce exactly."""
    rows = conn.execute(
        f"SELECT to_jsonb(t) FROM {table} t WHERE {scope_sql}",
        (ref_id,),
    ).fetchall()
    projected = []
    for (row,) in rows:
        projected.append(
            json.dumps(
                {
                    k: v
                    for k, v in row.items()
                    if k not in _REMAPPED and k not in VOLATILE
                },
                sort_keys=True,
                default=str,
            )
        )
    return sorted(projected)


def _owned_ids(conn: Any, ref_id: int) -> dict[str, set[int]]:
    """Every surrogate id ``ref_id`` owns, keyed by column name — built
    from the fork inventory itself."""
    owned: dict[str, set[int]] = {}
    for _table, scope, key, _map in _FORK_TABLES:
        if key is None:
            continue
        ids = {
            int(r[0])
            for r in conn.execute(
                f"SELECT {key} FROM {_table} WHERE {_FORK_SCOPES[scope]}",
                (ref_id,),
            ).fetchall()
        }
        owned.setdefault(key, set()).update(ids)
    return owned


def _natural_state(conn: Any, ref_id: int) -> dict[str, Any]:
    """The design's relations keyed by what they MEAN — refdes, pin and
    net names, feature/fixed-copper content — never by surrogate id. A
    remap that points a copy row at the wrong parent (or back at the
    source) changes this projection even when every row copied."""
    feats = {
        int(r[0]): f"{r[1]}|{r[2]}"
        for r in conn.execute(
            "SELECT feature_id, ftype, geom::text FROM pcb_features WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    }
    fixed = {
        int(r[0]): f"{r[1]}|{r[2]}|{r[3]}"
        for r in conn.execute(
            "SELECT fixed_id, ctype, layer, geom::text "
            "FROM pcb_fixed_copper WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    }
    netconns = conn.execute(
        "SELECT i.refdes, p.name, n.name FROM pcb_netconns nc "
        "JOIN pcb_instances i ON i.instance_id = nc.instance_id "
        "JOIN pcb_pins p ON p.pin_id = nc.pin_id "
        "JOIN pcb_nets n ON n.net_id = nc.net_id "
        "WHERE nc.net_id IN (SELECT net_id FROM pcb_nets WHERE ref_id = %s) "
        "ORDER BY 1, 2, 3",
        (ref_id,),
    ).fetchall()
    routes = conn.execute(
        "SELECT n.name, r.status, r.tree::text, r.topology::text, "
        "r.layer_assign::text "
        "FROM pcb_routes r JOIN pcb_boards b USING (board_id) "
        "JOIN pcb_nets n ON n.net_id = r.net_id "
        "WHERE b.ref_id = %s ORDER BY 1",
        (ref_id,),
    ).fetchall()
    # Copper keeps its net AND proves its route FK lands on the copy's
    # own route row (named by that route's net).
    copper = conn.execute(
        "SELECT n.name, c.ctype, c.layer, c.geom::text, rn.name "
        "FROM pcb_copper c JOIN pcb_boards b USING (board_id) "
        "LEFT JOIN pcb_nets n ON n.net_id = c.net_id "
        "LEFT JOIN pcb_routes r ON r.route_id = c.route_id "
        "LEFT JOIN pcb_nets rn ON rn.net_id = r.net_id "
        "WHERE b.ref_id = %s ORDER BY 1, 2, 3, 4",
        (ref_id,),
    ).fetchall()
    planes = conn.execute(
        "SELECT n.name, p.layer, p.meta::text FROM pcb_planes p "
        "JOIN pcb_boards b USING (board_id) "
        "LEFT JOIN pcb_nets n ON n.net_id = p.net_id "
        "WHERE b.ref_id = %s ORDER BY 1, 2",
        (ref_id,),
    ).fetchall()
    rooms = conn.execute(
        "SELECT i.refdes, f.ftype, f.geom::text FROM pcb_instances i "
        "LEFT JOIN pcb_features f ON f.feature_id = i.room_id "
        "WHERE i.ref_id = %s ORDER BY i.refdes",
        (ref_id,),
    ).fetchall()
    measures = []
    for (operands,) in conn.execute(
        "SELECT operands FROM pcb_measures WHERE ref_id = %s ORDER BY metric",
        (ref_id,),
    ).fetchall():
        resolved = [
            (
                {**o, "feature_id": feats.get(o["feature_id"], o["feature_id"])}
                if isinstance(o, dict) and "feature_id" in o
                else o
            )
            for o in (operands or [])
        ]
        measures.append(json.dumps(resolved, sort_keys=True, default=str))
    findings = []
    for rule, severity, objects, where in conn.execute(
        "SELECT f.rule, f.severity, f.objects, f.location "
        "FROM pcb_drc_findings f JOIN pcb_boards b USING (board_id) "
        "WHERE b.ref_id = %s ORDER BY f.rule, f.severity",
        (ref_id,),
    ).fetchall():
        resolved = []
        entries = objects if isinstance(objects, list) else [objects]
        for o in entries:
            if isinstance(o, dict):
                o = {
                    k: (fixed.get(v, v) if str(k).endswith("fixed_id") else v)
                    for k, v in o.items()
                }
            resolved.append(o)
        findings.append(
            (rule, severity, where, json.dumps(resolved, sort_keys=True, default=str))
        )
    return {
        "netconns": netconns,
        "routes": routes,
        "copper": copper,
        "planes": planes,
        "rooms": rooms,
        "measures": measures,
        "findings": findings,
    }


def _handler(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def test_fork_copies_every_table_verbatim(store: Any) -> None:
    src_id = _source(store)
    src_ref = store.get_ref(kind="pcb", id="fork-src")
    assert src_ref is not None
    with store.pool.connection() as conn:
        rev_before = store.pcb_design_revision(src_id)
        updated_before = conn.execute(
            "SELECT updated_at FROM refs WHERE ref_id = %s", (src_id,)
        ).fetchone()[0]
        counts_before = store.pcb_fork_counts(conn, src_id)

    resp = _handler(store).put(
        id="fork-dst",
        title="Forked copy",
        args={"op": "fork", "from": "fork-src"},
    )
    assert "copied" in resp.body and "fork-src" in resp.body

    dst_ref = store.get_ref(kind="pcb", id="fork-dst")
    assert dst_ref is not None and dst_ref.id != src_id
    assert dst_ref.title == "Forked copy"
    assert dst_ref.meta["forked_from"]["slug"] == "fork-src"
    assert dst_ref.meta["forked_from"]["ref_id"] == src_id

    with store.pool.connection() as conn:
        # Every table carries the same row count; the copy owns one more
        # link than the source — the fresh derived-from provenance edge.
        dst_counts = store.pcb_fork_counts(conn, dst_ref.id)
        assert {k: v for k, v in dst_counts.items() if k != "links"} == {
            k: v for k, v in counts_before.items() if k != "links"
        }
        assert dst_counts["links"] == counts_before["links"] + 1
        for table, scope, _key, _map in _FORK_TABLES:
            assert _project(conn, table, _FORK_SCOPES[scope], dst_ref.id) == (
                _project(conn, table, _FORK_SCOPES[scope], src_id)
            ), table
        # ids really were remapped, not shared.
        src_inst = conn.execute(
            "SELECT instance_id, board_id FROM pcb_instances WHERE ref_id = %s",
            (src_id,),
        ).fetchone()
        dst_inst = conn.execute(
            "SELECT instance_id, board_id FROM pcb_instances WHERE ref_id = %s",
            (dst_ref.id,),
        ).fetchone()
        assert src_inst[0] != dst_inst[0] and src_inst[1] != dst_inst[1]
        # the copy's copper points at the copy's route, not the source's.
        dst_route = conn.execute(
            "SELECT route_id FROM pcb_routes r JOIN pcb_boards b "
            "USING (board_id) WHERE b.ref_id = %s",
            (dst_ref.id,),
        ).fetchone()
        dst_copper_route = conn.execute(
            "SELECT DISTINCT route_id FROM pcb_copper c JOIN pcb_boards b "
            "USING (board_id) WHERE b.ref_id = %s",
            (dst_ref.id,),
        ).fetchone()
        assert dst_copper_route[0] == dst_route[0]
        # provenance edge carried.
        derived_dst = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'derived-from'",
            (dst_ref.id,),
        ).fetchone()
        assert derived_dst[0] == src_id
        # the card chunk regenerated for the new ref.
        assert (
            conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'card_combined'",
                (dst_ref.id,),
            ).fetchone()[0]
            == 1
        )
        # single-pin and floating nets carried (the epro round-trip drops them).
        dst_nets = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM pcb_nets WHERE ref_id = %s", (dst_ref.id,)
            ).fetchall()
        }
        assert {"CLOCK", "SINGLE", "FLOAT"} <= dst_nets
        # the source is untouched: same revision hash, same updated_at.
        assert store.pcb_design_revision(src_id) == rev_before
        assert (
            conn.execute(
                "SELECT updated_at FROM refs WHERE ref_id = %s", (src_id,)
            ).fetchone()[0]
            == updated_before
        )


def test_fork_preserves_natural_relations(store: Any) -> None:
    """FK remap proof: compare the two designs on natural keys, then
    assert no FK in the copy points back at a source-owned row."""
    src_id = _source(store)
    _handler(store).put(id="fork-nat", args={"op": "fork", "from": "fork-src"})
    dst = store.get_ref(kind="pcb", id="fork-nat")
    assert dst is not None
    with store.pool.connection() as conn:
        assert _natural_state(conn, dst.id) == _natural_state(conn, src_id)
        src_owned = _owned_ids(conn, src_id)
        dst_owned = _owned_ids(conn, dst.id)
        scopes = {t: s for t, s, _k, _m in _FORK_TABLES}
        for table, fks in _FORK_FKS.items():
            for column, target in fks.items():
                vals = {
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT DISTINCT {column} FROM {table} "
                        f"WHERE {_FORK_SCOPES[scopes[table]]} "
                        f"AND {column} IS NOT NULL",
                        (dst.id,),
                    ).fetchall()
                }
                assert not (vals & src_owned.get(target, set())), (
                    f"{table}.{column} still points at a source row"
                )
                assert vals <= dst_owned.get(target, set()), (
                    f"{table}.{column} points outside the copy"
                )
        # Embedded ids: the copied align measure names the copy's room
        # feature; the copied finding names the copy's fixed copper and
        # carries the finding_key a fresh DRC run on the copy produces.
        feat_ids = {
            int(r[0])
            for r in conn.execute(
                "SELECT feature_id FROM pcb_features WHERE ref_id = %s",
                (dst.id,),
            ).fetchall()
        }
        ops = conn.execute(
            "SELECT operands FROM pcb_measures WHERE ref_id = %s AND metric = 'align'",
            (dst.id,),
        ).fetchone()[0]
        assert ops[1]["feature_id"] in feat_ids
        assert ops[1]["feature_id"] not in src_owned["feature_id"]
        fixed_ids = {
            int(r[0])
            for r in conn.execute(
                "SELECT fixed_id FROM pcb_fixed_copper WHERE ref_id = %s",
                (dst.id,),
            ).fetchall()
        }
        row = conn.execute(
            "SELECT f.rule, f.severity, f.objects, f.location, f.detail, "
            "f.finding_key FROM pcb_drc_findings f "
            "JOIN pcb_boards b USING (board_id) "
            "WHERE b.ref_id = %s AND f.rule = 'via_pad_keepout'",
            (dst.id,),
        ).fetchone()
        assert row[2][0]["via_fixed_id"] in fixed_ids
        assert row[2][0]["via_fixed_id"] not in src_owned["fixed_id"]
        assert row[5] == pcb_drc.finding_key(
            pcb_drc.DrcFinding(
                rule=row[0],
                severity=row[1],
                where=row[3] or "",
                detail=row[4] or "",
                objects=tuple(row[2]),
            )
        )


def test_fork_slug_held_by_other_kind_refused(store: Any) -> None:
    """cite_key is a global namespace: a slug a paper owns is just as
    taken — refuse before any write (the gr517405 orphan shape)."""
    _source(store)
    store.insert_ref(kind="paper", slug="fork-paper-slug", title="A study")
    with pytest.raises(BadInput, match="taken"):
        _handler(store).put(
            id="fork-paper-slug", args={"op": "fork", "from": "fork-src"}
        )
    with store.pool.connection() as conn:
        # the paper is untouched and no orphan pcb ref/rows exist.
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind = 'pcb'").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM ref_identifiers ri JOIN refs r "
                "ON r.ref_id = ri.ref_id WHERE r.kind = 'paper' "
                "AND ri.id_value = 'fork-paper-slug'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM pcb_instances").fetchone()[0] == 1


def test_fork_cross_design_fk_aborts_and_rolls_back(store: Any) -> None:
    """A child row naming a parent outside the design aborts the copy:
    typed error with a retry hint, and the one transaction leaves
    nothing behind."""
    src_id = _source(store)
    other_id = _source(store, slug="fork-b")
    with store.pool.connection() as conn:
        own_net = conn.execute(
            "SELECT net_id FROM pcb_nets WHERE ref_id = %s LIMIT 1",
            (src_id,),
        ).fetchone()[0]
        foreign = conn.execute(
            "WITH p AS ("
            "  INSERT INTO pcb_pins (component_id, name, pad) "
            "  SELECT component_id, 'XTRA-B', '9' FROM pcb_components "
            "  WHERE ref_id = %s RETURNING pin_id, component_id"
            ") SELECT i.instance_id, i.component_id, p.pin_id "
            "FROM pcb_instances i JOIN p ON p.component_id = i.component_id "
            "WHERE i.ref_id = %s LIMIT 1",
            (other_id, other_id),
        ).fetchone()
        # In scope (A's net) but every physical-parent FK names the OTHER
        # design's rows — FK-valid, semantically corrupt.
        conn.execute(
            "INSERT INTO pcb_netconns "
            "(net_id, instance_id, component_id, pin_id) "
            "VALUES (%s, %s, %s, %s)",
            (own_net, *foreign),
        )
        conn.commit()
    with pytest.raises(Upstream, match="points outside the design"):
        _handler(store).put(id="fork-abort", args={"op": "fork", "from": "fork-src"})
    with store.pool.connection() as conn:
        assert store.get_ref(kind="pcb", id="fork-abort") is None
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind = 'pcb'").fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM pcb_boards").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM links WHERE relation = 'derived-from'"
            ).fetchone()[0]
            == 0
        )


def test_fork_from_pb_handle(store: Any) -> None:
    src_id = _source(store)
    _handler(store).put(id="fork-by-handle", args={"op": "fork", "from": f"pb{src_id}"})
    assert store.get_ref(kind="pcb", id="fork-by-handle") is not None


def test_fork_dry_run_writes_nothing(store: Any) -> None:
    src_id = _source(store)
    resp = _handler(store).put(
        id="fork-dry", args={"op": "fork", "from": "fork-src", "dry_run": True}
    )
    assert "would copy" in resp.body and "pcb_components: 1" in resp.body
    assert store.get_ref(kind="pcb", id="fork-dry") is None
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM pcb_instances WHERE ref_id != %s",
                (src_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM links WHERE dst_ref_id = %s "
                "AND relation = 'derived-from'",
                (src_id,),
            ).fetchone()[0]
            == 0
        )


def test_fork_taken_slug_refused(store: Any) -> None:
    _source(store)
    _source(store, slug="fork-other")
    with pytest.raises(BadInput, match="taken"):
        _handler(store).put(id="fork-other", args={"op": "fork", "from": "fork-src"})


def test_fork_source_is_target_refused(store: Any) -> None:
    _source(store)
    with pytest.raises(BadInput, match="its source"):
        _handler(store).put(id="fork-src", args={"op": "fork", "from": "fork-src"})


def test_fork_missing_source_not_found(store: Any) -> None:
    with pytest.raises(NotFound):
        _handler(store).put(id="fork-dst", args={"op": "fork", "from": "no-such-board"})


def test_fork_missing_from_refused(store: Any) -> None:
    with pytest.raises(BadInput, match="from="):
        _handler(store).put(id="fork-dst", args={"op": "fork"})


def test_fork_second_copy_chains_provenance(store: Any) -> None:
    _source(store)
    handler = _handler(store)
    handler.put(id="fork-mid", args={"op": "fork", "from": "fork-src"})
    handler.put(id="fork-leaf", args={"op": "fork", "from": "fork-mid"})
    leaf = store.get_ref(kind="pcb", id="fork-leaf")
    mid = store.get_ref(kind="pcb", id="fork-mid")
    assert leaf is not None and mid is not None
    assert leaf.meta["forked_from"]["slug"] == "fork-mid"
    with store.pool.connection() as conn:
        # a fork of a fork keeps the copied derived-from edge AND gains its
        # own — the provenance chain back to the source survives.
        dsts = {
            r[0]
            for r in conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
                "AND relation = 'derived-from'",
                (leaf.id,),
            ).fetchall()
        }
    src_ref = store.get_ref(kind="pcb", id="fork-src")
    assert src_ref is not None
    assert {src_ref.id, mid.id} <= dsts


def test_fork_tables_cover_every_design_fk(store: Any) -> None:
    """Drift guard against schema truth, not the snapshot fixture list:
    every table FK-ing a design anchor (``refs`` or a ``pcb_`` surrogate
    key) must be a fork table or an explicit exclusion — a new ``pcb_*``
    table that forgets to join ``_FORK_TABLES`` fails here."""
    fork_tables = {table for table, _s, _k, _m in _FORK_TABLES}
    anchors = sorted(fork_tables) + ["refs"]
    with store.pool.connection() as conn:
        children = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT kcu.table_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND ccu.table_name = ANY(%s)",
                (anchors,),
            ).fetchall()
        }
    owned = {t for t in children if t.startswith(("pcb_", "design_"))}
    assert owned <= fork_tables | _DESIGN_HISTORY, (
        "design-owned tables absent from _FORK_TABLES and the exclusion "
        f"list: {sorted(owned - fork_tables - _DESIGN_HISTORY)}"
    )
    assert {t for t in owned if t.startswith("pcb_")} <= fork_tables
    assert not ({"parts", "part_footprints"} & fork_tables)


def test_fork_table_scopes_are_known() -> None:
    for _table, scope, _key, _map in _FORK_TABLES:
        assert scope in _FORK_SCOPES


def test_fork_through_dispatch(runtime_with_store: Any, store: Any) -> None:
    """R1 regression: run the fork through the real dispatch path, where
    the write verb's ``revision_context`` ``set_config`` opens the
    transaction on checkout — a ``SET TRANSACTION`` first statement in
    ``pcb_fork`` would fail outright there (gr517199 re-review)."""
    src_id = _source(store)
    body, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "pcb",
            "id": "fork-rt",
            "title": "Dispatch fork",
            "args": {"op": "fork", "from": "fork-src"},
        },
    )
    assert not is_error, body
    assert "copied" in body
    dst = store.get_ref(kind="pcb", id="fork-rt")
    assert dst is not None and dst.id != src_id
    assert dst.meta["forked_from"]["slug"] == "fork-src"


def test_fork_lock_timeout_maps_to_upstream(store: Any) -> None:
    """The fork never waits on a lock — ``lock_timeout = '300ms'`` turns
    a writer holding the source rows into a retryable ``Upstream``, not
    a block and not a raw DB error (gr517199 re-review)."""
    src_id = _source(store)
    with store.pool.connection() as locker:
        # The fork's first lock is refs FOR SHARE; a writer holding the
        # ref FOR UPDATE must bounce it inside 300ms.
        locker.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE", (src_id,)
        )
        with pytest.raises(Upstream, match="could not lock"):
            _handler(store).put(
                id="fork-locked", args={"op": "fork", "from": "fork-src"}
            )
    assert store.get_ref(kind="pcb", id="fork-locked") is None
