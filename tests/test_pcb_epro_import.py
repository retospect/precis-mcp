"""pcb-epro-import slice 1b — the Store-facing half, against a real DB.

Acceptance criterion 2 from ``docs/backlog/pcb-epro-import.md``: after an
import the instance and connection counts equal the source's, the derived
stackup lands, plane assignments resolve, ``view='drc'`` runs and
``op='route'`` enqueues without refusing. Criterion 3 (a named pin reaches
real copper through ``pin_map``) is pinned on a synthetic footprint in
``test_pcb_footprint_pin_map.py`` and re-checked here on the imported one,
because the synthetic case cannot catch the reader handing over a map the
store then drops.

The pure reader's own output is pinned in ``test_pcb_epro_design.py``;
nothing here re-asserts it. What is here is what needs a database: did the
rows arrive, and does the rest of the engine accept them.
"""

from __future__ import annotations

import io
import os
import pathlib
import zipfile
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.ingest import pcb_epro
from precis.pcb import epro, padplace

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


def _zip(board: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_FIXTURE / "project2.json").read_bytes())
        zf.writestr("board.epru", board or (_FIXTURE / "board.epru").read_bytes())
    return buf.getvalue()


@pytest.fixture
def pcb(store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


@pytest.fixture
def imported(store):
    return pcb_epro.import_epro(store, _zip(), slug="epro-import-1", title="Tiny")


# ── the rows arrived ─────────────────────────────────────────────────────
def test_counts_match_the_source(store, imported) -> None:
    """The source's own record counts, not a plausible-looking subset.
    Two COMPONENTs and three live PAD_NET rows -> two instances and three
    connections."""
    assert imported.created is True
    assert imported.stats["components"] == 2
    assert imported.stats["connections"] == 3

    graph = store.pcb_graph(imported.ref_id)
    assert len(graph["instances"]) == 2
    members = [(m["refdes"], m["pin"]) for n in graph["nets"] for m in n["members"]]
    assert len(members) == 3
    assert set(members) == {("R1", "1"), ("U1", "VDD"), ("U1", "GND")}


def test_the_outline_and_the_mounting_hole_land_as_features(store, imported) -> None:
    features = store.pcb_features_list(imported.ref_id)
    ftypes = [f["ftype"] for f in features]
    assert ftypes.count("outline") == 1
    # The board's free-pad hole and R1's footprint hole.
    assert ftypes.count("mounting_hole") == 2
    assert {
        f["geom"].get("part") for f in features if f["ftype"] == "mounting_hole"
    } == {
        None,
        "R1",
    }

    outline = next(f for f in features if f["ftype"] == "outline")
    xs = [p[0] for p in outline["geom"]["path"]]
    # The board's own 1000 mil width, not a bbox around the two parts.
    assert max(xs) - min(xs) == pytest.approx(1000 * 0.0254, abs=1e-6)

    hole = next(
        f for f in features if f["ftype"] == "mounting_hole" and "part" not in f["geom"]
    )
    assert hole["geom"]["dia_mm"] == pytest.approx(6.0, abs=1e-5)
    # NOT marked fixed: pcb_features.fixed is unconstrained text that
    # nothing reads, so a value there would claim a freeze that does not
    # exist. Asserted so a later "let's freeze the mechanicals" change
    # has to make the column mean something first.
    assert hole["fixed"] is None


def test_provenance_is_stamped_on_the_ref(store, imported) -> None:
    """A stored editor version and source hash is what lets a future
    format failure be dated instead of guessed at."""
    ref = store.get_ref(kind="pcb", id="epro-import-1")
    assert ref is not None
    prov = (ref.meta or {})["epro"]
    assert prov["editor_version"] == "3.2.149"
    assert len(prov["source_sha256"]) == 64
    assert prov["board_uuid"] == "tiny0000000000pcb"
    assert prov["reader_version"] == 1


# ── the stackup + planes ─────────────────────────────────────────────────
def test_the_stackup_comes_from_the_board_not_the_default(store, imported) -> None:
    """``DEFAULT_STACKUP`` names In1.Cu a GND plane; the fixture's inner
    layers are named Inner1/Inner2 in the source and carry no inner pour,
    so a board that kept the default would be claiming a plane the file
    does not have."""
    assert [s["name"] for s in imported.stackup] == [
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
    ]
    graph = store.pcb_graph(imported.ref_id)
    assert [s["name"] for s in graph["board"]["stackup"]] == [
        s["name"] for s in imported.stackup
    ]


def test_an_outer_pour_does_not_become_a_plane(imported) -> None:
    """The fixture's only POUR is GND on the BOTTOM layer. An outer pour
    is local fill around parts; calling it a plane would tell the router
    that whole side is unavailable."""
    assert imported.planes == {}
    b_cu = next(s for s in imported.stackup if s["name"] == "B.Cu")
    assert b_cu["role"] == "signal"
    assert any("outer layer B.Cu carries a pour" in w for w in imported.warnings)


def test_a_single_net_inner_pour_becomes_a_plane(store) -> None:
    """The right precis model for a pour: the far side re-pours it, and
    ``pcb_fixed_copper``'s CHECK (``track|via``) has no room for it
    anyway."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    inner = stream.replace(
        '"netName":"GND","layerId":2,"width":0.2',
        '"netName":"GND","layerId":15,"width":0.2',
    )
    assert inner != stream
    result = pcb_epro.import_epro(
        store, _zip(inner.encode("utf-8")), slug="epro-plane-1"
    )
    assert result.planes == {"In1.Cu": "GND"}
    in1 = next(s for s in result.stackup if s["name"] == "In1.Cu")
    assert in1["role"] == "plane"
    assert in1["plane_net"] == "GND"
    # ...and the assignment is a real row, not just a stackup label: the
    # router reads pcb_planes, not the stackup's plane_net hint.
    assert {(p["layer"], p["net"]) for p in store.pcb_planes_list(result.ref_id)} == {
        ("In1.Cu", "GND")
    }
    # The fixture also routes TEE on In1.Cu, so the plane stays routable:
    # a real board did exactly this on both inner layers, and stacking them
    # as pure planes halved the author's routing layers.
    assert in1.get("routable") is True
    stored = store.pcb_graph(result.ref_id)["board"]["stackup"]
    assert next(s for s in stored if s["name"] == "In1.Cu").get("routable") is True


def test_an_inner_pour_with_no_foreign_tracks_is_a_pure_plane(store) -> None:
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    inner = stream.replace(
        '"netName":"GND","layerId":2,"width":0.2',
        '"netName":"GND","layerId":16,"width":0.2',
    )
    assert inner != stream
    result = pcb_epro.import_epro(
        store, _zip(inner.encode("utf-8")), slug="epro-plane-2"
    )
    in2 = next(s for s in result.stackup if s["name"] == "In2.Cu")
    assert (in2["role"], in2.get("routable")) == ("plane", None)


def test_an_inner_pour_on_two_nets_stays_a_signal_layer(store) -> None:
    """Guessing which net "wins" would silently connect copper."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    two = stream.replace(
        '"netName":"GND","layerId":2,"width":0.2',
        '"netName":"GND","layerId":15,"width":0.2',
    ).replace(
        '{"type":"COMPONENT","ticket":60,"id":"c1"}',
        '{"type":"POUR","ticket":51,"id":"p2"}||{"netName":"SIG","layerId":15,'
        '"width":0.2,"name":"POUR2","order":1,"path":[["R",0,500,1000,500,0,0]],'
        '"pourType":{"pourType":"SOLID","fineness":8},"keepIsland":false}|\n'
        '{"type":"COMPONENT","ticket":60,"id":"c1"}',
    )
    result = pcb_epro.import_epro(store, _zip(two.encode("utf-8")), slug="epro-2net")
    assert result.planes == {}
    in1 = next(s for s in result.stackup if s["name"] == "In1.Cu")
    assert in1["role"] == "signal"
    assert any("2 nets" in w for w in result.warnings)


# ── the named pin reaches real copper ────────────────────────────────────
def test_an_imported_named_pin_reaches_real_copper(store, imported) -> None:
    """Acceptance criterion 3 on the IMPORTED footprint. ``U1`` pad 1 is
    named ``VDD`` by the schematic; the pad it addresses has to be the
    one at the source file's coordinates."""
    graph = store.pcb_graph(imported.ref_id)
    pin_to_net = {
        (m["refdes"], m["pin"]): n["name"] for n in graph["nets"] for m in n["members"]
    }
    pads, _drills = padplace.board_pads(
        graph["instances"],
        {},
        layers=[s["name"] for s in imported.stackup],
        pin_to_net=pin_to_net,
        local_footprints=store.pcb_local_footprints_for(imported.ref_id),
    )
    u1 = {p["pin"]: p for p in pads if p["refdes"] == "U1"}
    assert set(u1) == {"VDD", "GND", "GND_3"}
    assert u1["VDD"]["net"] == "SIG"
    assert u1["GND"]["net"] == "GND"
    # GND_3 is the renamed duplicate: a real pad, simply unconnected.
    assert u1["GND_3"].get("net") in (None, "")

    # ...and it is where the source put it, via padplace's own transform.
    design = epro.build_design(epro.read_archive(_zip()))[0]
    fp = next(f for f in design.footprints if f["name"] == "SOT-23-3_L2.9-W1.3-P0.95")
    local = next(p for p in fp["pads"] if p["pin"] == "1")
    inst = next(i for i in graph["instances"] if i["refdes"] == "U1")
    want = padplace.place_pad_point(local, inst)
    assert (u1["VDD"]["x"], u1["VDD"]["y"]) == pytest.approx(want, abs=1e-6)


def test_the_pad_numbers_survive_the_round_trip_into_the_db(store, imported) -> None:
    """The export half writes pad NUMBERS back out. If naming the pins
    had cost them, the file could not be regenerated."""
    fps = store.pcb_local_footprints_for(imported.ref_id)
    sot = fps["SOT-23-3_L2.9-W1.3-P0.95"]
    assert sorted(p["number"] for p in sot["pads"]) == ["1", "2", "3"]
    assert sot["pin_map"]["1"]["name"] == "VDD"


# ── the rest of the engine accepts the board ─────────────────────────────
def test_drc_runs_on_the_imported_board(pcb, imported) -> None:
    """Not "is clean" — an imported board with no routing has real
    findings. That the check RUNS is the criterion: a malformed feature or
    pad would raise instead."""
    body = pcb.get(id=imported.slug, view="drc").body
    assert "unrouted" in body or "clean" in body.lower()


def test_svg_renders_the_imported_board(pcb, imported) -> None:
    body = pcb.get(id=imported.slug, view="svg").body
    assert "<svg" in body
    # The board outline's own size, not a bbox around the two parts.
    assert "25.4" in body or "1000" in body


def test_route_enqueues_without_refusing(pcb, imported) -> None:
    """``_enqueue_op`` hard-refuses any stackup that is not exactly 4
    layers. The import derives a 4-layer stackup from the file, so this
    is the check that the two agree."""
    res = pcb.put(id=imported.slug, args={"op": "route"})
    assert "refus" not in res.body.lower()


# ── refusals ─────────────────────────────────────────────────────────────
def test_a_second_import_onto_the_same_slug_is_refused(store, imported) -> None:
    """``pcb_apply`` EXTENDS: an existing refdes keeps its old position
    (so a moved part silently does not move) and features have no dedup
    key at all (so the outline and every hole would be duplicated). The
    refusal names both ways forward."""
    with pytest.raises(pcb_epro.EproImportError) as exc:
        pcb_epro.import_epro(store, _zip(), slug=imported.slug)
    msg = str(exc.value)
    assert "already exists" in msg
    assert "--update" in msg


def test_a_two_layer_board_is_refused_at_import_not_at_route(store) -> None:
    """``op='place'``/``op='route'`` refuse a non-4-layer stackup, so
    importing one produces a board that cannot do the thing the import
    exists for. Refusing here names the layer count; refusing later names
    an op the user did not think they were choosing."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    two_layer = "\n".join(
        ln for ln in stream.splitlines() if '"layerType":"SIGNAL"' not in ln
    )
    with pytest.raises(pcb_epro.EproImportError) as exc:
        pcb_epro.import_epro(store, _zip(two_layer.encode("utf-8")), slug="epro-2layer")
    msg = str(exc.value)
    assert "2 copper layers" in msg
    assert "pcb-engine-plan" in msg


def test_a_failure_after_the_design_writes_leaves_no_board(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design, the stackup and the planes land in ONE transaction.

    Without that, a failure between them leaves a board that got its
    instances and kept ``DEFAULT_STACKUP`` — which declares a GND plane on
    In1.Cu. So the half-done state is not "missing a stackup", it is a
    board actively claiming a plane the source file does not have, and
    nothing downstream could tell it apart from an authored one.
    """
    boom = RuntimeError("stackup write failed")

    def _explode(*_a: object, **_k: object) -> None:
        raise boom

    monkeypatch.setattr(type(store), "pcb_set_stackup", _explode)
    with pytest.raises(RuntimeError, match="stackup write failed"):
        pcb_epro.import_epro(store, _zip(), slug="epro-atomic")

    assert store.get_ref(kind="pcb", id="epro-atomic") is None, (
        "the design survived a failed stackup write — the board now claims "
        "DEFAULT_STACKUP's In1.Cu GND plane"
    )


# ── --update (slice 1c, AC6) ─────────────────────────────────────────────
_R2 = (
    '{"type":"COMPONENT","ticket":300,"id":"c3"}||{"layerId":1,"x":100,"y":100,'
    '"angle":0,"attrs":{"Unique ID":"gge3"},"locked":false}|\n'
    '{"type":"ATTR","ticket":301,"id":"c3e0"}||{"parentId":"c3","layerId":3,'
    '"x":100,"y":80,"key":"Designator","value":"R2","valueVisible":true}|\n'
    '{"type":"ATTR","ticket":302,"id":"c3e1"}||{"parentId":"c3","layerId":3,'
    '"x":null,"y":null,"key":"Footprint","value":"tiny000000000fp1",'
    '"valueVisible":false}|\n'
    '{"type":"ATTR","ticket":303,"id":"c3e2"}||{"parentId":"c3","layerId":3,'
    '"x":null,"y":null,"key":"Device","value":"tiny000000000dv1",'
    '"valueVisible":false}|\n'
    '{"type":"PAD_NET","ticket":304,"id":"[\\"PAD_NET\\",\\"c3\\",\\"1\\",'
    '\\"e7\\"]"}||{"padNet":"SIG","padLen":null,"attrsMap":{}}|\n'
)


def _edited(*, move_r1: bool = False, add_r2: bool = False, drop_u1: bool = False):
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    if move_r1:
        stream = stream.replace(
            '"layerId":1,"x":300,"y":150,"angle":90',
            '"layerId":1,"x":340,"y":150,"angle":0',
        )
    if drop_u1:
        stream = "".join(
            line
            for line in stream.splitlines(keepends=True)
            if '"c2' not in line and '\\"c2\\"' not in line
        )
    if add_r2:
        # Inside the PCB document (right after R1's records): the stream
        # holds seven documents, and appending at the end would put R2 in
        # a symbol.
        anchor = '"key":"Device","value":"tiny000000000dv1","valueVisible":false}|\n'
        assert stream.count(anchor) == 1
        stream = stream.replace(anchor, anchor + _R2)
    return _zip(stream.encode("utf-8"))


def _pose(store, ref_id: int) -> dict[str, tuple]:
    return {
        i["refdes"]: (round(i["x"], 4), round(i["y"], 4), i["rot"], i["layer"])
        for i in store.pcb_graph(ref_id)["instances"]
    }


def test_update_moves_adds_and_reports_without_duplicating_features(
    store, imported
) -> None:
    """AC6: moving a part updates exactly that pose, a new part is added,
    a removed one is REPORTED and kept, and the outline and hole are not
    duplicated."""
    before = _pose(store, imported.ref_id)
    n_features = len(store.pcb_features_list(imported.ref_id))

    result = pcb_epro.import_epro(
        store,
        _edited(move_r1=True, add_r2=True, drop_u1=True),
        slug=imported.slug,
        update=True,
    )
    plan = result.update
    assert plan is not None
    assert [m[0] for m in plan.moved] == ["R1"]
    assert plan.added == ["R2"]
    assert plan.removed == ["U1"]

    after = _pose(store, imported.ref_id)
    assert after["U1"] == before["U1"], "a part missing from the source moved"
    assert after["R1"] != before["R1"]
    assert after["R1"][2] == 0.0
    assert set(after) == {"R1", "U1", "R2"}
    assert len(store.pcb_features_list(imported.ref_id)) == n_features
    members = {
        (m["refdes"], m["pin"]): n["name"]
        for n in store.pcb_graph(imported.ref_id)["nets"]
        for m in n["members"]
    }
    assert members[("R2", "1")] == "SIG"
    assert ("U1", "VDD") in members, "a removed part's connections were dropped"


def test_an_unchanged_update_changes_nothing(store, imported) -> None:
    before = _pose(store, imported.ref_id)
    result = pcb_epro.import_epro(store, _zip(), slug=imported.slug, update=True)
    plan = result.update
    assert plan is not None
    assert (plan.moved, plan.added, plan.removed, plan.rewired) == ([], [], [], [])
    assert (plan.features_added, plan.features_missing) == (0, 0)
    # The stored footprints round-trip through JSONB unchanged, so a
    # re-read of the same source refreshes none of them.
    assert (plan.footprints_refreshed, plan.footprints_differ) == ([], [])
    assert _pose(store, imported.ref_id) == before


def _set_stored_footprint(store, ref_id: int, name: str, column: str, value) -> None:
    from psycopg.types.json import Jsonb

    with store.pool.connection() as conn:
        n = conn.execute(
            f"UPDATE pcb_local_footprints SET {column} = %s "
            "WHERE ref_id = %s AND name = %s",
            (Jsonb(value), ref_id, name),
        ).rowcount
        conn.commit()
    assert n == 1


def test_update_refreshes_a_footprint_whose_geometry_changed(store, imported) -> None:
    """heater-base-test: C27-29 kept the pad-extent courtyard an older
    reader stored, because --update never rewrote an existing footprint."""
    stored = store.pcb_local_footprints_for(imported.ref_id)
    name = sorted(stored)[0]
    fresh = stored[name]["courtyard"]
    _set_stored_footprint(
        store, imported.ref_id, name, "courtyard", {"bbox": [-0.1, -0.1, 0.1, 0.1]}
    )
    result = pcb_epro.import_epro(store, _zip(), slug=imported.slug, update=True)
    assert result.update is not None
    assert result.update.footprints_refreshed == [name]
    assert f"  refreshed footprint {name}" in result.update.lines()
    assert store.pcb_local_footprints_for(imported.ref_id)[name]["courtyard"] == fresh


def test_update_does_not_apply_a_footprint_whose_pin_map_changed(
    store, imported
) -> None:
    stored = store.pcb_local_footprints_for(imported.ref_id)
    name = sorted(stored)[0]
    renamed = {
        pad: {**entry, "name": f"X{pad}"}
        for pad, entry in stored[name]["pin_map"].items()
    }
    _set_stored_footprint(store, imported.ref_id, name, "pin_map", renamed)
    result = pcb_epro.import_epro(store, _zip(), slug=imported.slug, update=True)
    assert result.update is not None
    assert result.update.footprints_differ == [name]
    assert result.update.footprints_refreshed == []
    assert store.pcb_local_footprints_for(imported.ref_id)[name]["pin_map"] == renamed


def test_a_dry_run_update_reports_and_writes_nothing(store, imported) -> None:
    before = _pose(store, imported.ref_id)
    result = pcb_epro.import_epro(
        store,
        _edited(move_r1=True, add_r2=True),
        slug=imported.slug,
        update=True,
        dry_run=True,
    )
    assert result.update is not None
    assert [m[0] for m in result.update.moved] == ["R1"]
    assert result.update.added == ["R2"]
    assert _pose(store, imported.ref_id) == before


def test_update_keeps_the_first_import_date_and_stamps_the_update(
    store, imported
) -> None:
    first = store.get_ref(kind="pcb", id=imported.slug).meta["epro"]
    pcb_epro.import_epro(store, _zip(), slug=imported.slug, update=True)
    meta = store.get_ref(kind="pcb", id=imported.slug).meta["epro"]
    assert meta["imported_at"] == first["imported_at"]
    assert meta["updated_at"]


def test_update_onto_a_missing_slug_is_refused(store) -> None:
    """A typo'd slug must not quietly become a second board."""
    with pytest.raises(pcb_epro.EproImportError, match="without --update"):
        pcb_epro.import_epro(store, _zip(), slug="epro-nope", update=True)


def test_update_onto_a_board_that_was_not_imported_is_refused(store) -> None:
    store.pcb_apply(
        slug="epro-hand", title="hand", components=[], nets=[], connections=[]
    )
    with pytest.raises(pcb_epro.EproImportError, match="not imported"):
        pcb_epro.import_epro(store, _zip(), slug="epro-hand", update=True)


def test_update_from_a_different_board_is_refused(store, imported) -> None:
    """Matching parts by refdes across two boards would move R1 of one
    onto R1 of the other."""
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{epro,board_uuid}', "
            "'\"other\"') WHERE ref_id = %s",
            (imported.ref_id,),
        )
    with pytest.raises(pcb_epro.EproImportError, match="other"):
        pcb_epro.import_epro(store, _zip(), slug=imported.slug, update=True)


def test_a_board_with_no_outline_is_refused(store) -> None:
    """Every geometry consumer otherwise falls back to
    ``export.board_bbox``'s rectangle around the placed parts, which is
    not the board. Pinned in the reader too; re-checked here because the
    import is where a user meets it."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    no_outline = stream.replace(
        '"layerId":11,"width":10,"path"', '"layerId":13,"width":10,"path"'
    )
    with pytest.raises(epro.EproError, match="outline"):
        pcb_epro.import_epro(store, _zip(no_outline.encode("utf-8")), slug="epro-noout")


# ── dry run ──────────────────────────────────────────────────────────────
def test_dry_run_reports_everything_and_writes_nothing(store) -> None:
    """A user needs to see the warnings and counts BEFORE committing a
    board — and a dry run that reported different numbers than the import
    would be worse than none."""
    dry = pcb_epro.import_epro(store, _zip(), slug="epro-dry", dry_run=True)
    assert dry.ref_id == 0
    assert dry.created is False
    assert store.get_ref(kind="pcb", id="epro-dry") is None

    wet = pcb_epro.import_epro(store, _zip(), slug="epro-dry-2")
    assert dry.stats == wet.stats
    assert dry.stackup == wet.stackup
    assert dry.planes == wet.planes
    assert dry.warnings == wet.warnings


# ── the real board, when someone points at one ───────────────────────────
@pytest.mark.skipif(
    not os.environ.get("PRECIS_EPRO_FIXTURE"),
    reason="set PRECIS_EPRO_FIXTURE=/path/to/real.epro2 to run",
)
def test_a_real_board_imports_at_full_scale(store) -> None:
    """Not in CI: a real board is large and usually proprietary. This is
    the only check that the write path survives real scale — 140
    components, 20 footprints and 599 connections in one ``pcb_apply``,
    where the fixture has 2/2/3.

    Asserts shape, never the board's own numbers: it is Reto's file, it
    changes, and a test pinned to its component count would fail on the
    next export for no useful reason.
    """
    data = pathlib.Path(os.environ["PRECIS_EPRO_FIXTURE"]).read_bytes()
    project = epro.read_archive(data)
    boards = project.by_type("PCB")
    result = pcb_epro.import_epro(
        store,
        data,
        slug="epro-real",
        board_uuid=boards[-1].uuid if len(boards) > 1 else None,
    )

    assert result.stats["components"] > 50, "a real board, not a fragment"
    assert result.stats["connections"] > result.stats["components"]

    # Slice 1c at real scale: the report runs, measures copper on most
    # nets, and sees the pads (no "gaps are partial" note). Printed under
    # -s because reading it is the point of running this test at all.
    from precis.pcb import copper_report

    assert result.copper is not None
    assert result.copper.notes == []
    assert len(result.copper.nets) > 10
    print("\n" + "\n".join(copper_report.render(result.copper)))

    graph = store.pcb_graph(result.ref_id)
    assert len(graph["instances"]) == result.stats["components"]

    # Referential integrity at scale: pcb_apply would happily create a
    # pin row for a name nothing else uses, so a connection that missed
    # its pin becomes a net nobody can route rather than an error.
    members = [(m["refdes"], m["pin"]) for n in graph["nets"] for m in n["members"]]
    assert len(members) == result.stats["connections"]
    assert len(set(members)) == len(members), "a duplicated connection"

    # Every pad of every instance resolves through the imported pin maps.
    pads, _drills = padplace.board_pads(
        graph["instances"],
        {},
        layers=[s["name"] for s in result.stackup],
        local_footprints=store.pcb_local_footprints_for(result.ref_id),
    )
    assert pads, "no imported pad reached board copper"
    placed = {p["refdes"] for p in pads}
    assert len(placed) == result.stats["components"]

    # ...and every one of them lands inside the board outline. A frame
    # bug shows up here and nowhere else.
    outline = next(
        f for f in store.pcb_features_list(result.ref_id) if f["ftype"] == "outline"
    )
    xs = [p[0] for p in outline["geom"]["path"]]
    ys = [p[1] for p in outline["geom"]["path"]]
    for p in pads:
        assert min(xs) - 1.0 <= p["x"] <= max(xs) + 1.0, f"{p['refdes']} off board"
        assert min(ys) - 1.0 <= p["y"] <= max(ys) + 1.0, f"{p['refdes']} off board"


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("PRECIS_EPRO_FIXTURE"),
    reason="set PRECIS_EPRO_FIXTURE=/path/to/real.epro2 to run",
)
@pytest.mark.parametrize(
    ("freeze_all", "iters", "cells_per_axis"),
    [(False, 200, None), (True, 200, None), (True, 2000, None), (True, 200, 2000)],
    ids=["as-imported-200", "frozen-200", "frozen-2000", "frozen-200-fine-grid"],
)
def test_a_real_board_routes_at_all(
    store, monkeypatch, freeze_all: bool, iters: int, cells_per_axis: int | None
) -> None:
    """Reto, 2026-09-30: "it's routed already, but yea do the needful."

    Three configs (Reto approved 2026-10-01) because ``pcb_route`` is a
    JOINT place+route anneal and the import locks only the parts EasyEDA
    had locked: as imported, the anneal re-places the rest before routing.
    ``frozen-*`` sets every instance ``fixed='both'`` so the number is the
    router alone on the author's placement; ``frozen-2000`` asks whether
    budget alone moves it. Each run prints copper per layer (are the inner
    layers used at all?), how many instances moved (did the freeze hold?)
    and the top cumulative-time functions. ``fine-grid`` raises
    ``maze.grid_for``'s 400-cells-per-axis cap: across a 200 mm board that
    cap is a 0.5 mm routing pitch, coarse enough that neighbouring
    fine-pitch pads plus clearance can wall a pin in at any width — which
    is exactly what ``no_path`` reports.

    The board arrives ALREADY routed in EasyEDA — what is unmeasured is
    whether precis' own router can do it at this size. 140 components on
    200 x 75 mm is an order of magnitude past anything the engine has been
    run on, and `op='place'`/`op='route'` were written against boards of a
    few dozen parts. If the answer is "not in any usable time", that is a
    fact the re-route depends on and it is much cheaper to learn here than
    during the annotation work.

    Deliberately weak assertions: this measures, it does not gate. A
    routing-quality claim at this size would be a claim about an anneal's
    seed.
    """
    import cProfile
    import pstats
    import time
    from collections import Counter

    from precis.workers.job_types import pcb_route

    data = pathlib.Path(os.environ["PRECIS_EPRO_FIXTURE"]).read_bytes()
    project = epro.read_archive(data)
    boards = project.by_type("PCB")
    result = pcb_epro.import_epro(
        store,
        data,
        slug=(
            f"epro-route-trial-{'frozen' if freeze_all else 'free'}-{iters}"
            f"-{cells_per_axis or 'default'}"
        ),
        board_uuid=boards[-1].uuid if len(boards) > 1 else None,
    )

    if freeze_all:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE pcb_instances SET fixed = 'both' "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (result.ref_id,),
            )

    def _poses() -> dict[str, tuple[float, float, float, str]]:
        return {
            str(i["refdes"]): (
                float(i["x"] or 0.0),
                float(i["y"] or 0.0),
                float(i["rot"] or 0.0),
                str(i["layer"]),
            )
            for i in store.pcb_graph(result.ref_id)["instances"]
        }

    before = _poses()
    locked = sum(
        1 for i in store.pcb_graph(result.ref_id)["instances"] if i.get("fixed")
    )

    if cells_per_axis is not None:
        from precis.pcb import maze

        real_grid_for = maze.grid_for

        def _fine_grid_for(*a: Any, **k: Any) -> maze.GridSpec:
            k.setdefault("target_cells_per_axis", cells_per_axis)
            return real_grid_for(*a, **k)

        monkeypatch.setattr(maze, "grid_for", _fine_grid_for)

    profiler = cProfile.Profile()
    started = time.monotonic()
    ctx = _FakeRouteCtx(store, {"pcb_ref_id": result.ref_id, "iters": iters, "seed": 1})
    profiler.enable()
    pcb_route._dispatch(cast(Any, ctx), pcb_route.SPEC)
    profiler.disable()
    elapsed = time.monotonic() - started

    after = _poses()
    moved = sorted(
        r
        for r, (x, y, rot, side) in after.items()
        if r in before
        and (
            abs(x - before[r][0]) > 1e-3
            or abs(y - before[r][1]) > 1e-3
            or abs(rot - before[r][2]) > 1e-3
            or side != before[r][3]
        )
    )

    rows = store.pcb_route_status(result.ref_id)
    by_status = Counter(str(r["status"]) for r in rows)
    tag = (
        f"[route {'frozen' if freeze_all else 'as-imported'} iters={iters}"
        f"{f' cells={cells_per_axis}' if cells_per_axis else ''}]"
    )
    print(
        f"\n{tag} {result.stats['components']} components ({locked} locked), "
        f"{len(rows)} nets: {elapsed:.1f}s, {len(ctx.failures)} failure(s)"
    )
    print(f"{tag} status: {dict(by_status)}")
    print(f"{tag} instances moved: {len(moved)} {moved[:8]}")
    board_id = store.pcb_ensure_board(result.ref_id)
    copper = store.pcb_copper_list(board_id)
    per_layer = Counter((str(c["ctype"]), str(c.get("layer"))) for c in copper)
    print(f"{tag} copper rows by (ctype, layer): {dict(sorted(per_layer.items()))}")
    notes = Counter(
        str(r["note"])[:120] for r in rows if r["status"] != "realized" and r["note"]
    )
    for note, n in notes.most_common(3):
        print(f"{tag} {n} net(s): {note}")
    for f in ctx.failures[:3]:
        print(f"{tag} failure: {str(f)[:300]}")
    out = io.StringIO()
    pstats.Stats(profiler, stream=out).sort_stats("cumulative").print_stats(25)
    print(f"{tag} profile (cumulative, top 25):\n{out.getvalue()}")

    assert rows, "the router reported on no nets at all"


class _FakeRouteCtx:
    """The minimum the ``pcb_route`` job dispatch reads — the same shape
    ``tests/workers/test_pcb_route.py::_FakeCtx`` uses, restated rather
    than imported across test modules (importing another module's private
    helper couples two suites that have no reason to move together)."""

    def __init__(self, store, params: dict[str, object]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="epro route trial",
            meta={"executor": "job_inproc", "job_type": "pcb_route"},
        )
        self.ref_id = int(ref.id)
        self.title = "epro route trial"
        self.meta: dict[str, object] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:
        pass

    def set_meta(self, **_kw: object) -> None:
        pass

    def is_cancel_requested(self) -> bool:
        return False


def test_an_import_measures_the_source_copper_against_its_pads(imported) -> None:
    """Slice 1c: the import's copper is measured, not kept. Read back from
    the DB, so the pads closing the gaps are the ones every later reader
    sees — and with pads present there is no "gaps are partial" note."""
    rep = imported.copper
    assert rep is not None
    assert rep.nets, "the fixture's tracks and via produced no measurement"
    assert rep.notes == []
    measured = {c.measured.net for c in rep.nets}
    assert "SIG" in measured


def test_a_dry_run_reports_the_same_copper_as_the_real_import(store) -> None:
    """``--dry-run`` promises the real import's report. It measured gaps
    without pads (there is no written board to read them from) until
    2026-10-02, when the real board's dry run on prod said so in a note
    and the real import did not; it now places the pads in memory."""
    from precis.pcb import copper_report

    dry = pcb_epro.import_epro(store, _zip(), slug="epro-dry-copper", dry_run=True)
    real = pcb_epro.import_epro(store, _zip(), slug="epro-real-copper")
    assert dry.copper is not None and real.copper is not None
    assert dry.copper.notes == []
    assert copper_report.render(dry.copper) == copper_report.render(real.copper)


def test_the_copper_report_follows_the_current_spec(store, imported) -> None:
    """The point of re-running it: annotate a net, and the rule the report
    compares against moves with it."""
    before = pcb_epro.report_copper(store, _zip(), slug=imported.slug)
    sig = next(c for c in before.nets if c.measured.net == "SIG")
    assert sig.rules is not None
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_nets SET est_current_a = 5.0 "
            "WHERE ref_id = %s AND name = 'SIG'",
            (imported.ref_id,),
        )
    after = pcb_epro.report_copper(store, _zip(), slug=imported.slug)
    sig_after = next(c for c in after.nets if c.measured.net == "SIG")
    assert sig_after.rules is not None
    assert sig_after.rules.track_width_mm > sig.rules.track_width_mm


def test_a_copper_report_on_a_missing_slug_is_refused(store) -> None:
    with pytest.raises(pcb_epro.EproImportError, match="import the board first"):
        pcb_epro.report_copper(store, _zip(), slug="never-imported")


def test_dry_run_still_refuses(store) -> None:
    """A refusal a dry run did not report would be a nasty surprise on
    the real import."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    two_layer = "\n".join(
        ln for ln in stream.splitlines() if '"layerType":"SIGNAL"' not in ln
    )
    with pytest.raises(pcb_epro.EproImportError):
        pcb_epro.import_epro(
            store, _zip(two_layer.encode("utf-8")), slug="epro-dry-3", dry_run=True
        )
