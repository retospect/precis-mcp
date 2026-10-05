"""gr467885: refuse conflicting canonical pads without partial authoring."""

from __future__ import annotations

import copy
import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler


def _args():
    return {
        "nets": [],
        "footprints": [
            {
                "name": "PAD2",
                "pads": [
                    {
                        "pin": "2",
                        "x": x,
                        "y": 1,
                        "w": 0.6,
                        "h": 0.8,
                        "shape": "RECT",
                        "layer": "F.Cu",
                    }
                    for x in (0, 1)
                ],
                "pin_map": {"2": {"name": "CLK"}},
            }
        ],
        "components": [
            {
                "refdes": "J_UNPLACED",
                "footprint": "PAD2",
                "pins": [{"name": "CLK", "pad": "2"}, {"name": "OTHER", "pad": "2"}],
            }
        ],
        "connections": [
            {"refdes": "J_UNPLACED", "pin": "CLK", "net": "CLK_NET"},
            {"refdes": "J_UNPLACED", "pin": "OTHER", "net": "OTHER_NET"},
        ],
    }


def _snapshot(store):
    with store.pool.connection() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND (tablename LIKE 'pcb_%' OR tablename IN "
                "('refs', 'ref_identifiers', 'chunks', 'ref_tags', 'ref_links'))"
            ).fetchall()
        ]
        return {
            table: sorted(
                json.dumps(row[0], sort_keys=True, default=str)
                for row in conn.execute(
                    f'SELECT to_jsonb(t) FROM "{table}" t'
                ).fetchall()
            )
            for table in tables
        }


@pytest.fixture
def pcb(store, monkeypatch):
    handler = PcbHandler(hub=Hub(store=store))
    monkeypatch.setattr(handler, "_queue_datasheets", lambda *a, **k: 0)
    return handler


def _forbid_post_commit(pcb, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("refused authoring attempted a provider or post-commit job")

    monkeypatch.setattr(pcb, "_queue_datasheets", forbidden)
    monkeypatch.setattr("precis.handlers.pcb.fetch_footprint", forbidden)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("connections", ["different", "same", "none"])
def test_conflicting_pad_refuses_and_rolls_back_fresh_design(
    pcb, store, monkeypatch, reverse, connections
):
    args = _args()
    if reverse:
        args["components"][0]["pins"].reverse()
    if connections == "none":
        args["connections"] = []
    elif connections == "same":
        for connection in args["connections"]:
            connection["net"] = "SHARED"
    before = _snapshot(store)
    _forbid_post_commit(pcb, monkeypatch)
    with pytest.raises(BadInput) as error:
        pcb.put(id="pad-retention-regression", args=args)
    for evidence in (
        "J_UNPLACED",
        "2",
        "CLK",
        "OTHER",
        "one canonical name",
        "pins and connections",
        "correct",
        "pad number",
    ):
        assert evidence in str(error.value)
    assert _snapshot(store) == before
    assert store.get_ref(kind="pcb", id="pad-retention-regression") is None


@pytest.mark.parametrize("existing_refdes", [False, True])
def test_existing_board_rolls_back_all_earlier_writes(
    pcb, store, monkeypatch, existing_refdes
):
    valid = _args()
    valid["components"][0]["pins"] = [{"name": "CLK", "pad": "2"}]
    valid["connections"] = valid["connections"][:1]
    pcb.put(id="pad-retention-regression", title="Original", args=valid)
    before = _snapshot(store)
    invalid = _args()
    if not existing_refdes:
        invalid["components"][0]["refdes"] = "J_INVALID"
        for connection in invalid["connections"]:
            connection["refdes"] = "J_INVALID"
    invalid["components"].insert(
        0,
        {
            "refdes": "J_EARLIER",
            "pins": [{"name": "GOOD", "pad": "1"}],
        },
    )
    invalid["meta"] = {"new_annotation": "must roll back"}
    invalid["footprints"][0]["pads"][0]["x"] = 99
    invalid["nets"] = [{"name": "NEW_NET"}]
    invalid["net_classes"] = {"new": {"width_mm": 0.4}}
    _forbid_post_commit(pcb, monkeypatch)
    with pytest.raises(BadInput):
        pcb.put(id="pad-retention-regression", title="Changed", args=invalid)
    assert _snapshot(store) == before


@pytest.mark.parametrize("supplied_conn", [False, True])
def test_shared_store_authoring_rollback(store, supplied_conn):
    args = _args()
    args["components"].insert(0, {"refdes": "J_GOOD", "pins": [{"name": "A"}]})
    before = _snapshot(store)
    with pytest.raises(ValueError, match="one canonical name"):
        if supplied_conn:
            with store.tx() as conn:
                store.pcb_apply(
                    slug="direct-invalid", title="Invalid", conn=conn, **args
                )
        else:
            store.pcb_apply(slug="direct-invalid", title="Invalid", **args)
    assert _snapshot(store) == before


@pytest.mark.parametrize("pads", [("2", "3"), (None, "2")])
def test_same_name_contradictory_pads_refuse(pcb, store, pads):
    args = _args()
    args["components"][0]["pins"] = [
        {"name": " CLK ", "pad": pads[0]},
        {"name": "CLK", "pad": pads[1]},
    ]
    before = _snapshot(store)
    with pytest.raises(BadInput, match="contradictory pads") as error:
        pcb.put(id="contradiction", args=args)
    assert "J_UNPLACED" in str(error.value) and "CLK" in str(error.value)
    assert _snapshot(store) == before


def test_identical_declarations_reput_null_lazy_and_physical_duplicates(pcb, store):
    args = _args()
    args["components"][0]["pins"] = [
        {"name": "CLK", "pad": "2"},
        {"name": " CLK ", "pad": "2"},
        {"name": "UNBOUND_A"},
        {"name": "UNBOUND_A", "pad": None},
        {"name": "UNBOUND_B", "pad": None},
    ]
    args["connections"][1]["pin"] = "LAZY"
    pcb.put(id="valid-controls", args=args)
    # Successful re-put regenerates the intent card; canonical graph IDs/data
    # must remain stable. Refusal tests above compare cards as well.
    before = {k: v for k, v in _snapshot(store).items() if k.startswith("pcb_")}
    pcb.put(id="valid-controls", args=copy.deepcopy(args))
    assert {k: v for k, v in _snapshot(store).items() if k.startswith("pcb_")} == before
    ref = store.get_ref(kind="pcb", id="valid-controls")
    assert ref is not None
    neighbors = store.pcb_instance_neighbors(ref.id, "J_UNPLACED")
    assert neighbors is not None
    assert {p["pin"]: p["pad"] for p in neighbors["pins"]} == {
        "CLK": "2",
        "UNBOUND_A": None,
        "UNBOUND_B": None,
        "LAZY": None,
    }
    legacy = pcb.get(id="valid-controls#J_UNPLACED").body
    assert "CLK\t2" in legacy and "LAZY" in legacy
    pinout = pcb.get(id="valid-controls#J_UNPLACED", view="pinout").body
    assert "physical pads: 2" in pinout and "[1, 2]" in pinout
    assert "CLK_NET" in pinout and "unplaced/invalid pose" in pinout
    # Re-put remains create-or-extend, not rebinding/backfill.
    changed = copy.deepcopy(args)
    changed["components"][0]["pins"] = [{"name": "CLK", "pad": "3"}]
    changed["connections"] = []
    pcb.put(id="valid-controls", args=changed)
    assert {k: v for k, v in _snapshot(store).items() if k.startswith("pcb_")} == before


def test_database_pad_constraint_unwinds_before_error_translation(
    pcb, store, monkeypatch
):
    valid = _args()
    valid["components"][0]["pins"] = [{"name": "CLK", "pad": "2"}]
    valid["connections"] = valid["connections"][:1]
    pcb.put(id="db-authority", args=valid)
    ref = store.get_ref(kind="pcb", id="db-authority")
    assert ref is not None
    before = _snapshot(store)
    # Simulate validation missing a concurrent owner: the actual DB index
    # must still refuse, and diagnostic reads must run after savepoint unwind.
    monkeypatch.setattr(
        "precis.store._pcb_ops._validate_pin_declarations", lambda *a: None
    )
    with store.tx() as conn:
        component_id = conn.execute(
            "SELECT component_id FROM pcb_instances WHERE ref_id=%s",
            (ref.id,),
        ).fetchone()[0]
        with pytest.raises(ValueError, match="CLK.*OTHER.*one canonical name"):
            store._pcb_insert_pins(
                conn,
                component_id,
                [{"name": "OTHER", "pad": "2"}],
                refdes="J_UNPLACED",
            )
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert _snapshot(store) == before


def test_omitted_pad_keeps_footprint_mapping_without_backfill(pcb, store):
    args = _args()
    args["components"][0]["pins"] = [{"name": "CLK"}]
    args["connections"] = args["connections"][:1]
    pcb.put(id="omitted-pad", args=args)
    ref = store.get_ref(kind="pcb", id="omitted-pad")
    assert ref is not None
    neighborhood = store.pcb_instance_neighbors(ref.id, "J_UNPLACED")
    assert neighborhood is not None and neighborhood["pins"][0]["pad"] is None
    pinout = pcb.get(id="omitted-pad#J_UNPLACED", view="pinout").body
    assert "footprint-pin-map" in pinout and "CLK_NET" in pinout
    assert "explicit-pin-pad" not in pinout


def test_split_declarations_for_one_refdes_do_not_hide_conflict(pcb, store):
    args = _args()
    component = args["components"][0]
    component["pins"] = [{"name": "CLK", "pad": "2"}]
    args["components"].append(
        {
            "refdes": " J_UNPLACED ",
            "pins": [{"name": "OTHER", "pad": "2"}],
        }
    )
    before = _snapshot(store)
    with pytest.raises(BadInput, match="one canonical name"):
        pcb.put(id="split-declarations", args=args)
    assert _snapshot(store) == before
