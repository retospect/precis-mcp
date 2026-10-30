"""PCB builder entities link back to their construction frames (gr521581/6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis_geom import trace, trace_schema
from precis_geom.pcb import Board, Net, Part, PcbBuilderError, module


def test_part_and_net_makers_resolve_to_creation_frames(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        board = Board("makers")
        part = board.part("U1")
        net = board.net("VCC")
        frames = {f["id"]: f for f in tr.dump()["frames"]}

    assert {"parts", "nets"} <= trace_schema.ENTITY_TYPES
    assert part.maker != net.maker
    assert frames[part.maker]["qual"] == "pcb.Board.part"
    assert frames[part.maker]["args"]["refdes"] == "U1"
    assert frames[part.maker]["made"] == {"parts": 1}
    assert frames[net.maker]["qual"] == "pcb.Board.net"
    assert frames[net.maker]["args"]["name"] == "VCC"
    assert frames[net.maker]["made"] == {"nets": 1}


def test_wire_counts_its_new_net_once_and_preserves_reused_maker(
    tmp_path: Path,
) -> None:
    with trace.run(work_root=tmp_path) as tr:
        board = Board("wire-makers")
        a, b = board.part("U1"), board.part("U2")
        for pin in (a["1"], b["1"], a["2"], b["2"]):
            pin.etype("bidi", fmax_hz=0.0, assume="signal pin")
        net = board.wire(a["1"], b["1"], name="signal")
        maker = net.maker
        assert board.wire(a["2"], b["2"], name="signal") is net
        frames = tr.dump()["frames"]

    assert net.maker == maker
    assert sum(f["made"].get("nets", 0) for f in frames) == 1
    creator = next(f for f in frames if f["id"] == maker)
    parent = next(f for f in frames if f["key"] == creator["parent"])
    assert creator["qual"] == "pcb.Board.net"
    assert parent["qual"] == "pcb.Board.wire"
    assert parent["made"] == {}


def test_rejected_declarations_do_not_count_phantom_entities(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        board = Board("duplicate-makers")
        part = board.part("U1")
        net = board.net("VCC")
        with pytest.raises(PcbBuilderError):
            board.part("U1")
        with pytest.raises(PcbBuilderError):
            board.net("VCC")
        frames = tr.dump()["frames"]

    assert sum(f["made"].get("parts", 0) for f in frames) == 1
    assert sum(f["made"].get("nets", 0) for f in frames) == 1
    assert all(f["made"] == {} for f in frames if "error" in f)
    assert {part.maker, net.maker} <= {f["id"] for f in frames}


def test_each_traced_primitive_counts_what_it_made(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        board = Board("primitive-makers")
        board.net_class("pwr", clearance_mm=0.3)
        board.role("3v3", voltage=3.3)
        net = board.net("VCC", cls="pwr")
        a, b = board.part("U1"), board.part("U2")
        board.outline({"path": [[0, 0], [10, 0], [10, 10], [0, 10]]})
        board.hole(1, 1, drill=2.0, ring=3.0)
        board.near(a, b, within_mm=20)
        board.generate("g1", generator="copper_pour")
        frames = tr.dump()["frames"]

    assert {
        "footprints",
        "net_classes",
        "roles",
        "features",
        "measures",
        "generators",
    } <= trace_schema.ENTITY_TYPES
    by_qual: dict[str, list[dict[str, Any]]] = {}
    for f in frames:
        by_qual.setdefault(f["qual"], []).append(f)
    assert by_qual["pcb.Board.net_class"][0]["made"] == {"net_classes": 1}
    assert by_qual["pcb.Board.role"][0]["made"] == {"roles": 1}
    assert by_qual["pcb.Board.net"][0]["made"] == {"nets": 1}
    assert by_qual["pcb.Board.outline"][0]["made"] == {"features": 1}
    assert by_qual["pcb.Board.hole"][0]["made"] == {"features": 1}
    assert by_qual["pcb.Board.measure"][0]["made"] == {"measures": 1}
    assert by_qual["pcb.Board.generate"][0]["made"] == {"generators": 1}
    # untyped wrappers (near) create no frame of their own; the measure
    # child counts the entity once — the wire/net precedent.
    assert "pcb.Board.near" not in by_qual
    assert sum(f["made"].get("features", 0) for f in frames) == 2
    assert net.maker == by_qual["pcb.Board.net"][0]["id"]


def test_module_entities_keep_primitive_makers_without_changing_batch(
    tmp_path: Path,
) -> None:
    @module
    def cell(board: Board) -> tuple[Part, Net]:
        return board.part("U1"), board.net("signal")

    plain = Board("module-makers")
    plain_part, plain_net = plain.use(cell, prefix="amp")
    with trace.run(work_root=tmp_path) as tr:
        traced = Board("module-makers")
        part, net = traced.use(cell, prefix="amp")
        frames = {f["id"]: f for f in tr.dump()["frames"]}

    assert plain_part.maker is None and plain_net.maker is None
    assert part.refdes == "amp.U1" and net.name == "amp/signal"
    assert frames[part.maker]["made"] == {"parts": 1}
    assert frames[net.maker]["made"] == {"nets": 1}
    # Traced to_batch stamps each entity row's `frame` (gr521596 slice 3);
    # authored content still matches the untraced batch exactly.
    tb = traced.to_batch()
    assert tb["components"][0]["frame"] == part.maker
    assert tb["nets"][0]["frame"] == net.maker
    for row in (*tb["components"], *tb["nets"]):
        row.pop("frame", None)
    assert tb == plain.to_batch()
