"""Migration 0221 data path: ``pcb_pin_swaps`` rows become one snapshot.

The test DB is fully migrated, so ``pcb_pin_swaps`` is already gone; each
test recreates it from 0141's own DDL, seeds a board, and replays the 0221
file text on a dedicated connection.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.store import Store
from tests.conftest import MIGRATIONS_DIR, _active_dsn

pytestmark = pytest.mark.db


def _swaps_ddl() -> str:
    text = (MIGRATIONS_DIR / "0141_pcb_pin_swaps.sql").read_text(encoding="utf-8")
    start = text.index("CREATE TABLE IF NOT EXISTS pcb_pin_swaps")
    end = text.index("COMMIT;", start)
    return text[start:end]


@pytest.fixture
def conn(store: Store) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    c = psycopg.connect(_active_dsn(), autocommit=True, row_factory=dict_row)
    try:
        c.execute("DROP TABLE IF EXISTS pcb_pin_swaps")
        c.execute(_swaps_ddl())
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DROP TABLE IF EXISTS pcb_pin_swaps")
        c.execute("DELETE FROM pcb_netlist_snapshots")
        c.close()


_SEED = """
INSERT INTO refs (kind,title) VALUES ('pcb','mig-0221');
INSERT INTO pcb_boards (ref_id, stackup) SELECT ref_id,'[]' FROM refs WHERE kind='pcb';
INSERT INTO pcb_components (ref_id,label) SELECT ref_id,'SUP' FROM refs WHERE kind='pcb';
INSERT INTO pcb_components (ref_id,label) SELECT ref_id,'CLI' FROM refs WHERE kind='pcb';
INSERT INTO pcb_pins (component_id,name)
 SELECT component_id,n FROM pcb_components, unnest(ARRAY['HV1','HV2','HV3']) n WHERE label='SUP';
INSERT INTO pcb_pins (component_id,name)
 SELECT component_id,n FROM pcb_components, unnest(ARRAY['E1','E2']) n WHERE label='CLI';
INSERT INTO pcb_instances (ref_id,component_id,refdes,board_id)
 SELECT c.ref_id,c.component_id,CASE c.label WHEN 'SUP' THEN 'U1' ELSE 'ARR1' END,
        (SELECT board_id FROM pcb_boards) FROM pcb_components c;
INSERT INTO pcb_nets (ref_id,name) SELECT ref_id,n FROM refs, unnest(ARRAY['N1','N2']) n WHERE kind='pcb';
INSERT INTO pcb_nets (ref_id,name,retired_at) SELECT ref_id,'NR',now() FROM refs WHERE kind='pcb';
INSERT INTO pcb_instances (ref_id,component_id,refdes,board_id,retired_at)
 SELECT ref_id,component_id,'ARR2',(SELECT board_id FROM pcb_boards),now()
 FROM pcb_components WHERE label='CLI';
CREATE TEMP VIEW pp AS
 SELECT i.refdes, p.name pin, i.instance_id, p.pin_id, i.component_id
 FROM pcb_instances i JOIN pcb_pins p USING (component_id);
INSERT INTO pcb_netconns (net_id,instance_id,pin_id,component_id)
 SELECT n.net_id,pp.instance_id,pp.pin_id,pp.component_id FROM pp
 JOIN pcb_nets n ON (n.name,pp.refdes,pp.pin)
   IN (('N1','ARR1','E1'),('N1','U1','HV1'),('N2','ARR1','E2'),('N2','U1','HV2'));
"""

# (refdes, pin, net, retired)
_SWAPS = [
    ("U1", "HV1", "N2", False),  # mapped cross swap
    ("U1", "HV2", "N1", False),  # mapped cross swap
    ("ARR1", "E1", "NR", False),  # live, retired net -> unmapped
    ("ARR2", "E2", "N1", False),  # live, retired instance -> unmapped
    ("U1", "HV3", "N1", True),  # three retired rows
    ("U1", "HV3", "N2", True),
    ("U1", "HV1", "N1", True),
]


def _seed(c: psycopg.Connection[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    c.execute(_SEED)
    ids: dict[tuple[str, str, str], int] = {}
    for refdes, pin, net, retired in _SWAPS:
        row = c.execute(
            "INSERT INTO pcb_pin_swaps"
            " (board_id,instance_id,pin_id,component_id,net_id,meta,retired_at)"
            " SELECT (SELECT board_id FROM pcb_boards),pp.instance_id,pp.pin_id,"
            '  pp.component_id,n.net_id,\'{"source":"derived"}\','
            "  CASE WHEN %s THEN now() END"
            " FROM pp JOIN pcb_nets n ON (pp.refdes,pp.pin,n.name)=(%s,%s,%s)"
            " RETURNING swap_id",
            (retired, refdes, pin, net),
        ).fetchone()
        assert row is not None
        ids[(refdes, pin, net)] = row["swap_id"]
    return ids


def _migration() -> str:
    return (MIGRATIONS_DIR / "0221_pcb_netlist_snapshots.sql").read_text(
        encoding="utf-8"
    )


def _scalar(c: psycopg.Connection[dict[str, Any]], q: str) -> Any:
    row = c.execute(q).fetchone()
    assert row is not None
    return next(iter(row.values()))


def test_0221_migrates_live_unmapped_and_retired_rows(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    ids = _seed(conn)
    conn.execute(_migration())

    assert _scalar(conn, "SELECT to_regclass('pcb_pin_swaps')") is None
    rows = conn.execute("SELECT * FROM pcb_netlist_snapshots").fetchall()
    assert len(rows) == 1
    snap = rows[0]
    meta = snap["meta"]
    assert snap["content_hash"] is None
    assert meta["source"] == "pcb_pin_swaps"
    assert meta["swap_rows"] == 4
    assert meta["retired_rows"] == 3
    assert len(meta["unmapped"]) == 2
    assert [u["swap_id"] for u in meta["unmapped"]] == sorted(
        [ids[("ARR1", "E1", "NR")], ids[("ARR2", "E2", "N1")]]
    )
    assert len(meta["retired"]) == 3
    assert all(r["retired_at"] for r in meta["retired"])
    assert {r["swap_id"] for r in meta["retired"]} == {
        ids[("U1", "HV3", "N1")],
        ids[("U1", "HV3", "N2")],
        ids[("U1", "HV1", "N1")],
    }
    pins = {(p["refdes"], p["pin"]): p for p in snap["pins"]}
    assert pins[("U1", "HV1")]["net"] == "N2"
    assert pins[("U1", "HV1")]["declared"] == "N1"
    assert pins[("U1", "HV2")]["net"] == "N1"
    assert pins[("U1", "HV2")]["declared"] == "N2"
    # unmapped rows are not applied: ARR1.E1 keeps its declared net.
    assert pins[("ARR1", "E1")]["net"] == "N1"
    assert ("ARR2", "E2") not in pins
    # retired rows are not applied: U1.HV3 has no declared net, no pin entry.
    assert ("U1", "HV3") not in pins


def test_0221_guard_a_refuses_and_rolls_back(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    _seed(conn)
    board = _scalar(conn, "SELECT board_id FROM pcb_boards")
    conn.execute(
        "INSERT INTO pcb_netlist_snapshots (board_id, meta) VALUES (%s, %s)",
        (
            board,
            Jsonb({"source": "pcb_pin_swaps", "swap_rows": 99, "retired_rows": 0}),
        ),
    )
    with pytest.raises(
        psycopg.errors.RaiseException,
        match=re.escape("pcb_pin_swaps rows do not match the migrated snapshots"),
    ):
        conn.execute(_migration())
    conn.execute("ROLLBACK")
    assert _scalar(conn, "SELECT to_regclass('pcb_pin_swaps')") is not None
    assert _scalar(conn, "SELECT count(*) FROM pcb_pin_swaps") == 7
    # only the pre-seeded bogus snapshot survives the rollback
    assert _scalar(conn, "SELECT count(*) FROM pcb_netlist_snapshots") == 1


def test_0221_guard_b_refuses_and_rolls_back(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    # The data path is consistent by construction (one live swap per pin),
    # so inject the fault guard (b) exists for: the migrated pins lose the
    # applied swaps while the meta counts that guard (a) checks stay right.
    _seed(conn)
    conn.execute(
        "CREATE FUNCTION pg_temp.strip_pins() RETURNS trigger"
        " LANGUAGE plpgsql AS $$ BEGIN NEW.pins := '[]'::jsonb; RETURN NEW; END $$"
    )
    conn.execute(
        "CREATE TRIGGER strip_pins BEFORE INSERT ON pcb_netlist_snapshots"
        " FOR EACH ROW EXECUTE FUNCTION pg_temp.strip_pins()"
    )
    try:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match=re.escape("a live pcb_pin_swaps row is neither applied nor carried"),
        ):
            conn.execute(_migration())
        conn.execute("ROLLBACK")
    finally:
        conn.execute("DROP TRIGGER strip_pins ON pcb_netlist_snapshots")
    assert _scalar(conn, "SELECT count(*) FROM pcb_pin_swaps") == 7
    assert _scalar(conn, "SELECT count(*) FROM pcb_netlist_snapshots") == 0
