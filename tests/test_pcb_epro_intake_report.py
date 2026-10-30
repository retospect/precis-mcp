"""The gr540839 intake census — ``import_epro(..., report=True)``.

Imports were confirmed blind: the only summary was whole-file counts.
The census lists the merge decisions (nets created/matched/merged), the
footprint verdict (resolved vs synthesized), orphaned pads, per-layer
source copper and the outline bbox delta — collected during the import,
never by a re-read.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

from precis.ingest import pcb_epro

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


def _zip(board: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_FIXTURE / "project2.json").read_bytes())
        zf.writestr("board.epru", board or (_FIXTURE / "board.epru").read_bytes())
    return buf.getvalue()


def _rec(head: dict, body: dict) -> str:
    """One ``<header>||<body>|`` epro record line."""
    return (
        json.dumps(head, separators=(",", ":"))
        + "||"
        + json.dumps(body, separators=(",", ":"))
        + "|"
    )


def _board_added() -> bytes:
    """A second .epru for the UPDATE census — same board uuid as
    ``board.epru``, a grown outline, and three added parts: R2 on the
    stored footprint (pin 1 on the existing GND net, pins 2+3 on the new
    NEWNET — two members so the net survives the dangling-net drop), X1
    pointing at a footprint the project does not carry, and TP1 on a new
    12-pad footprint left entirely unwired (12 orphan pads, over the
    10-name cap)."""
    src = _FIXTURE.joinpath("board.epru").read_text(encoding="utf-8").splitlines()
    new: list[str] = [
        _rec(
            {"type": "NET", "ticket": 900, "id": json.dumps(["NET", "NEWNET"])},
            {"netType": None},
        ),
        _rec(
            {"type": "COMPONENT", "ticket": 920, "id": "c3"},
            {
                "layerId": 1,
                "x": 700,
                "y": 150,
                "angle": 0,
                "attrs": {"Unique ID": "gge3"},
                "locked": False,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 921, "id": "c3e0"},
            {
                "parentId": "c3",
                "layerId": 3,
                "x": 700,
                "y": 120,
                "key": "Designator",
                "value": "R2",
                "valueVisible": True,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 922, "id": "c3e1"},
            {
                "parentId": "c3",
                "layerId": 3,
                "x": None,
                "y": None,
                "key": "Footprint",
                "value": "tiny000000000fp1",
                "valueVisible": False,
            },
        ),
        _rec(
            {
                "type": "PAD_NET",
                "ticket": 923,
                "id": json.dumps(["PAD_NET", "c3", "1", "e7"]),
            },
            {"padNet": "GND", "padLen": None, "attrsMap": {}},
        ),
        _rec(
            {
                "type": "PAD_NET",
                "ticket": 924,
                "id": json.dumps(["PAD_NET", "c3", "2", "e8"]),
            },
            {"padNet": "NEWNET", "padLen": None, "attrsMap": {}},
        ),
        _rec(
            {
                "type": "PAD_NET",
                "ticket": 925,
                "id": json.dumps(["PAD_NET", "c3", "3", "e9"]),
            },
            {"padNet": "NEWNET", "padLen": None, "attrsMap": {}},
        ),
        _rec(
            {"type": "COMPONENT", "ticket": 930, "id": "c4"},
            {
                "layerId": 1,
                "x": 900,
                "y": 400,
                "angle": 0,
                "attrs": {"Unique ID": "gge4"},
                "locked": False,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 931, "id": "c4e0"},
            {
                "parentId": "c4",
                "layerId": 3,
                "x": 900,
                "y": 370,
                "key": "Designator",
                "value": "X1",
                "valueVisible": True,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 932, "id": "c4e1"},
            {
                "parentId": "c4",
                "layerId": 3,
                "x": None,
                "y": None,
                "key": "Footprint",
                "value": "tiny000000000fp9",
                "valueVisible": False,
            },
        ),
        _rec(
            {"type": "COMPONENT", "ticket": 940, "id": "c5"},
            {
                "layerId": 1,
                "x": 100,
                "y": 400,
                "angle": 0,
                "attrs": {"Unique ID": "gge5"},
                "locked": False,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 941, "id": "c5e0"},
            {
                "parentId": "c5",
                "layerId": 3,
                "x": 100,
                "y": 370,
                "key": "Designator",
                "value": "TP1",
                "valueVisible": True,
            },
        ),
        _rec(
            {"type": "ATTR", "ticket": 942, "id": "c5e1"},
            {
                "parentId": "c5",
                "layerId": 3,
                "x": None,
                "y": None,
                "key": "Footprint",
                "value": "tiny00000000fpbig",
                "valueVisible": False,
            },
        ),
    ]
    fp_doc = [
        _rec(
            {"type": "DOCHEAD", "ticket": 970},
            {
                "docType": "FOOTPRINT",
                "uuid": "tiny00000000fpbig",
                "editVersion": "3.2.149",
            },
        ),
        _rec(
            {"type": "META", "ticket": 971, "id": "META"},
            {"title": "BIG12", "tags": ["Footprints"]},
        ),
        *(
            _rec(
                {"type": "PAD", "ticket": 972 + i, "id": f"b{i}"},
                {
                    "netName": "",
                    "layerId": 1,
                    "num": str(i),
                    "centerX": -200 + i * 40,
                    "centerY": 0,
                    "padAngle": 0,
                    "defaultPad": {"padType": "RECT", "width": 40, "height": 60},
                    "plated": True,
                    "padType": "NORMAL",
                },
            )
            for i in range(1, 13)
        ),
    ]

    out: list[str] = []
    inserted = False
    for line in src:
        head, _, body = line.partition("||")
        h = json.loads(head)
        if h.get("type") == "POLY" and h.get("id") == "outline1":
            # A grown outline: the update declines to move the stored
            # one, so the census shows the source/stored delta.
            b = json.loads(body[:-1] if body.endswith("|") else body)
            b["path"] = [0, 0, "L", 2000, 0, 2000, 1000, 0, 1000, 0, 0]
            line = head + "||" + json.dumps(b, separators=(",", ":")) + "|"
        if not inserted and '"docType":"FOOTPRINT"' in line:
            out.extend(new)
            inserted = True
        out.append(line)
    out.extend(fp_doc)
    return ("\n".join(out) + "\n").encode("utf-8")


def test_no_report_means_no_census(store) -> None:
    result = pcb_epro.import_epro(store, _zip(), slug="epro-norep", title="Tiny")
    assert result.intake is None


def test_fresh_import_census(store) -> None:
    result = pcb_epro.import_epro(
        store, _zip(), slug="epro-rep-1", title="Tiny", report=True
    )
    rep = result.intake
    assert rep is not None
    # A fresh import creates every net — nothing pre-existed to match.
    assert not rep.nets_matched and not rep.nets_merged
    assert len(rep.nets_created) == result.stats["nets"]
    # TEE reaches the board only through copper records — no NET row.
    assert rep.nets_copper_only == ["TEE"]
    # The fixture's parts all carry real source footprints.
    assert rep.parts_resolved and not rep.parts_synthesized
    assert rep.bbox_source is not None
    lines = rep.lines()
    assert lines[0] == "intake census:"
    assert "matches the source" in "\n".join(lines)
    assert len("\n".join(lines)) < 3000


def test_dry_run_reports_the_same_census(store) -> None:
    dry = pcb_epro.import_epro(
        store, _zip(), slug="epro-rep-dry", title="Tiny", report=True, dry_run=True
    )
    assert dry.intake is not None
    assert dry.intake.nets_created
    assert store.get_ref(kind="pcb", id="epro-rep-dry") is None


def test_update_census_marks_matched_and_added(store) -> None:
    pcb_epro.import_epro(store, _zip(), slug="epro-rep-up", title="Tiny", report=True)
    second = pcb_epro.import_epro(
        store,
        _zip(_board_added()),
        slug="epro-rep-up",
        title="Tiny",
        update=True,
        report=True,
    )
    rep = second.intake
    assert rep is not None
    # R2's pin 1 wires the batch into the existing GND net: matched AND
    # merged; its other two pins carry NEWNET, new to the board.
    assert rep.nets_matched == ["GND"]
    assert rep.nets_merged == ["GND"]
    assert rep.nets_created == ["NEWNET"]
    # The added-part verdict: R2 rides the stored R0603 footprint, TP1 the
    # file's new BIG12 doc; X1's footprint uuid is nowhere, so it
    # synthesizes.
    assert rep.parts_resolved == ["R2", "TP1"]
    assert rep.parts_synthesized == ["X1"]
    # TP1's 12 unwired pads are all named orphans — over the 10-cap.
    assert len(rep.pads_orphaned) == 12
    assert "TP1.7" in rep.pads_orphaned
    # The grown source outline does not move the stored one.
    assert rep.bbox_source is not None
    assert rep.bbox_stored != rep.bbox_source
    lines = "\n".join(rep.lines())
    # 12 orphan names over the 10-cap renders the "+N more" tail.
    assert "+2 more" in lines
    assert "differs from the source" in lines


def test_orphan_pads_are_named_not_just_counted(store) -> None:
    result = pcb_epro.import_epro(
        store, _zip(), slug="epro-rep-orphan", title="Tiny", report=True
    )
    rep = result.intake
    assert rep is not None
    # The fixture carries three pads whose pin has no net; the census
    # names each "REFDES.PIN", not just a count.
    assert rep.pads_orphaned == ["R1.2", "R1.3", "U1.GND_3"]
    for name in rep.pads_orphaned:
        assert "." in name  # "REFDES.PIN"
