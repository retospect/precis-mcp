"""gr520149 — ``put(op='pad')``: per-design pad overrides on a placed part.

Slice 1 of docs/backlog/pcb-pad-overrides.md: ``dx``/``dy`` move one pad's
centre and ``w``/``h`` resize it to an absolute extent, in footprint-local
mm, stored as a delta on ``pcb_instances.meta['pad_overrides']`` — never
on the shared ``part_footprints`` row. ``revert`` drops one pad's
override or the part's whole map. The delta is applied at the one
footprint join (:func:`precis.pcb.session.footprints_by_refdes`), so
``view='pinout'``, DRC and the fab path all see the overridden geometry
while other designs and other instances of the part do not; the op runs
inside ``_judged_mutation`` at its default refuse=True, so a delta that
adds an error stores nothing. Slices 2–3: ``drill`` re-drills a plated
pad; ``add``/``remove`` + ``pad`` author or suppress a catalog pad;
``hole='add'``/``hole='<n>'`` edit non-plated footprint holes by the
effective ``hole_index`` pinout prints, and ``restore_hole``
un-suppresses a removed catalog hole by its catalog index.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import epro as pcb_epro
from precis.pcb import padplace as pcb_padplace
from precis.pcb import session as pcb_session

_LCSC = "C639448"


def _fp() -> dict:
    """A two-pad cached catalog footprint: pads ``"1"``/``"2"`` 0.5mm
    square at (0,0) and (1,0) — a 0.5mm edge gap, comfortably over the
    house clearance, so the board is clean until an override closes it."""
    return {
        "pads": [
            {
                "number": "1",
                "shape": "RECT",
                "x": 0.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            },
            {
                "number": "2",
                "shape": "RECT",
                "x": 1.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            },
        ],
        "pin_map": {
            "1": {"name": "A", "tags": []},
            "2": {"name": "B", "tags": []},
        },
        "courtyard": {"bbox": [-0.5, -0.5, 1.5, 0.5]},
        "centroid": {"x": 0.5, "y": 0.0},
        "source": "easyeda:test",
        "raw": {},
    }


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _component(refdes: str, **extra) -> dict:
    return {
        "refdes": refdes,
        "label": "part under test",
        "part": _LCSC,
        "pins": [{"name": "A"}, {"name": "B"}],
        **extra,
    }


def _design(*, placed: bool = False, two: bool = False) -> dict:
    """U1 on ``part=_LCSC`` with pads wired to two nets. ``placed`` gives
    U1 a pose (the judged gate ignores a board with no placed instance);
    ``two`` adds a second instance of the same C-number at x=20."""
    components = [_component("U1", **({"x": 0.0, "y": 0.0} if placed else {}))]
    connections = [
        {"net": "N1", "refdes": "U1", "pin": "A"},
        {"net": "N2", "refdes": "U1", "pin": "B"},
    ]
    if two:
        components.append(_component("U2", x=20.0, y=0.0))
        connections += [
            {"net": "N1", "refdes": "U2", "pin": "A"},
            {"net": "N2", "refdes": "U2", "pin": "B"},
        ]
    return {
        "components": components,
        "nets": [{"name": "N1"}, {"name": "N2"}],
        "connections": connections,
    }


def _seed(pcb, slug: str = "pad-op", fp=None, **design_kw):
    pcb.put(id=slug, args=_design(**design_kw))
    pcb.store.part_footprint_put(_LCSC, fp if fp is not None else _fp())
    return pcb.store.get_ref(kind="pcb", id=slug)


def _seed_pad2_free(pcb, slug: str = "pad-op", fp=None):
    """U1 whose pin B never binds a net — pad 2 is removable (a pad
    carrying a net refuses ``remove``)."""
    pcb.put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "part under test",
                    "part": _LCSC,
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "A"}],
                }
            ],
            "nets": [{"name": "N1"}],
            "connections": [{"net": "N1", "refdes": "U1", "pin": "A"}],
        },
    )
    pcb.store.part_footprint_put(_LCSC, fp if fp is not None else _fp())
    return pcb.store.get_ref(kind="pcb", id=slug)


def _instance(pcb, ref, refdes: str) -> dict:
    graph = pcb.store.pcb_graph(ref.id)
    return next(i for i in graph["instances"] if i["refdes"] == refdes)


# ── move / resize reach the read side and DRC ────────────────────────────


def test_op_pad_move_shifts_pinout_and_marks_the_pad(pcb):
    ref = _seed(pcb)

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.2, "dy": -0.1},
    )
    assert "override stored" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"dx": 0.2, "dy": -0.1}}

    view = pcb.get(id="pad-op#U1", view="pinout")
    assert "1 authored pad override(s)" in view.body
    assert "authored pad override" in view.body
    assert "0.2000" in view.body  # pad 1's local_x_mm after dx
    assert "-0.1000" in view.body  # and local_y_mm after dy

    # The shared cache row is never rewritten.
    assert pcb.store.part_footprint_get(_LCSC)["pads"][0]["x"] == 0.0

    # Re-sending the same override is idempotent.
    again = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.2, "dy": -0.1},
    )
    assert "no change" in again.body


def test_op_pad_patch_merges_onto_the_stored_override(pcb):
    ref = _seed(pcb)
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.2})
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "w": 0.8})

    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"dx": 0.2, "w": 0.8}}


def test_op_pad_resize_reaches_the_drc_pad_set(pcb):
    """``w``/``h`` replace the pad's extent absolutely, and the override
    rides the one seam (:meth:`_drc_pads` → ``footprints_by_refdes``) DRC
    reads — the OTHER pad of the same part is untouched."""
    ref = _seed(pcb, placed=True)
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "w": 0.9, "h": 0.3},
    )
    graph = pcb.store.pcb_graph(ref.id)
    layers = [str(layer["name"]) for layer in graph["board"]["stackup"]]
    pads = pcb._drc_pads(ref.id, layers)
    by_pin = {p["pin"]: p for p in pads if p["refdes"] == "U1"}
    assert by_pin["B"]["w"] == pytest.approx(0.9)
    assert by_pin["B"]["h"] == pytest.approx(0.3)
    assert by_pin["A"]["w"] == pytest.approx(0.5)
    assert by_pin["A"]["h"] == pytest.approx(0.5)


def test_op_pad_is_per_instance_and_never_the_shared_row(pcb):
    ref = _seed(pcb, two=True)
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.4})

    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"dx": 0.4}}
    assert not _instance(pcb, ref, "U2").get("pad_overrides")

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    assert fps["U1"]["pads"][0]["x"] == pytest.approx(0.4)
    assert fps["U2"]["pads"][0]["x"] == 0.0  # same C-number, catalog geometry

    view = pcb.get(id="pad-op#U2", view="pinout")
    assert "authored pad override" not in view.body
    # The shared cache row itself is untouched either way.
    assert pcb.store.part_footprint_get(_LCSC)["pads"][0]["x"] == 0.0


def test_board_pads_flashes_the_pad_at_its_overridden_position(pcb, store):
    """The fab path's own seam: gerber pads come out of
    :func:`precis.pcb.padplace.board_pads`, which applies the instance's
    stored delta to a COPY of the cache row before placing — the flash
    lands at the moved centre while a second instance of the same
    C-number still gets catalog geometry."""
    ref = _seed(pcb, placed=True, two=True)
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.3})

    # The same arguments the gerber/fab view passes (design["instances"]
    # carries the hoisted pad_overrides; footprints are the raw cache).
    design = store.pcb_load(ref.id)
    graph = store.pcb_graph(ref.id)
    layers = [str(layer["name"]) for layer in graph["board"]["stackup"]]
    pads, _drills = pcb_padplace.board_pads(
        design["instances"],
        store.pcb_footprints_for(ref.id),
        layers=layers,
    )
    by_pad = {(p["refdes"], p["pin"]): p for p in pads if p.get("layer") == "F.Cu"}
    assert by_pad[("U1", "A")]["x"] == pytest.approx(0.3)
    assert by_pad[("U2", "A")]["x"] == pytest.approx(20.0)


def test_view_footprints_lists_the_overridden_pad_numbers(pcb):
    _seed(pcb)
    before = pcb.get(id="pad-op", view="footprints")
    u1_row = next(line for line in before.body.splitlines() if line.startswith("U1"))
    assert u1_row.split()[-1] == "-"  # overrides column: none yet

    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "2", "dx": 0.1})
    after = pcb.get(id="pad-op", view="footprints")
    assert "overrides" in after.body
    u1_row = next(line for line in after.body.splitlines() if line.startswith("U1"))
    assert u1_row.split()[-1] == "2"


# ── revert ───────────────────────────────────────────────────────────────


def test_op_pad_revert_restores_catalog_geometry(pcb):
    ref = _seed(pcb)
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.2})
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "2", "w": 0.9})

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "revert": True},
    )
    assert "pad 1 override reverted" in resp.body
    # Per-pad revert keeps the other pad's override.
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"2": {"w": 0.9}}

    resp = pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "revert": True})
    assert "all pad overrides reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")

    view = pcb.get(id="pad-op#U1", view="pinout")
    assert "authored pad override" not in view.body
    assert "0.0000" in view.body  # pad 1's local_x_mm back to catalog

    # Reverting a part with no overrides left is a no-op, not an error.
    resp = pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "revert": True})
    assert "no change" in resp.body


# ── refusals and the judged gate ─────────────────────────────────────────


def test_op_pad_add_refuses_an_existing_number(pcb):
    """Pad numbers are unique per footprint — adding one that is already
    there (live or patched) refuses; a REMOVED catalog number is the
    re-add/replace path tested below."""
    _seed(pcb)
    with pytest.raises(BadInput, match="already has a pad '1'"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "pad": "1",
                "add": True,
                "x": 2.0,
                "y": 0.0,
                "w": 0.6,
            },
        )
    with pytest.raises(BadInput, match="exclusive"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "pad": "9",
                "add": True,
                "remove": True,
                "x": 2.0,
                "y": 0.0,
                "w": 0.6,
            },
        )


def test_op_pad_refuses_an_unknown_pad_and_lists_the_real_numbers(pcb):
    _seed(pcb)
    with pytest.raises(BadInput, match="no pad '9'"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "9", "dx": 0.1},
        )


def test_op_pad_arg_validation(pcb):
    _seed(pcb)
    with pytest.raises(BadInput, match="refdes"):
        pcb.put(id="pad-op", args={"op": "pad", "pad": "1", "dx": 0.1})
    with pytest.raises(BadInput, match="args.pad"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "dx": 0.1})
    with pytest.raises(BadInput, match="dx / dy / w / h"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1"})
    with pytest.raises(BadInput, match="not both"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.1, "revert": True},
        )
    with pytest.raises(BadInput, match="positive"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "w": 0})
    with pytest.raises(NotFound, match="U9"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U9", "pad": "1", "dx": 0.1})


def test_op_pad_overlap_is_refused_by_the_judge_and_stores_nothing(pcb):
    """The judged-mutation gate at its default refuse=True: pad 1 slid
    +0.9mm lands ON pad 2 — an error between pads on different nets — so
    the whole call refuses and the override is never stored."""
    ref = _seed(pcb, placed=True)
    with pytest.raises(BadInput, match="invalid board"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.9},
        )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def test_op_pad_move_onto_router_copper_rips_the_net_and_stores(pcb, store):
    """The spec's other judged half: a move that lands a pad on STORED
    routed copper is not refused like pad-vs-pad — the colliding router
    net yields, is ripped and reported, and the override IS stored. Pad 1
    (net N1) slides +1.0mm in y onto a realized N2 track; the clearance
    finding names N2's derived copper, which yields."""
    ref = _seed(pcb, placed=True)
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    nets = store.pcb_net_ids(ref.id)
    store.pcb_routes_write(ref.id, board_id, {"N2": {"status": "realized"}})
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": nets["N2"],
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [-3.0, 1.0], "end": [3.0, 1.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    assert store.pcb_nets_with_router_copper(board_id) == {"N2"}

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "dy": 1.0},
    )
    assert "N2 ripped: clearance after this change" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == set()
    status = {r["name"]: r["status"] for r in store.pcb_route_status(ref.id)}
    assert status["N2"] != "realized"
    # The override stored — the collision was answered by ripping, not refusing.
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"dy": 1.0}}


def _fp_tht() -> dict:
    """A two-pad PLATED through-hole variant: 1.6mm round pads drilled
    0.8mm (a 0.4mm annular ring, clear of the fab floor and the house
    margin) at (0,0) and (3,0) — a 1.4mm edge gap, comfortably clean
    until a re-drill eats the ring."""
    fp = _fp()
    for pad, x in zip(fp["pads"], (0.0, 3.0), strict=True):
        pad.update({"shape": "CIRCLE", "x": x, "w": 1.6, "h": 1.6, "drill": 0.8})
    fp["courtyard"] = {"bbox": [-1.0, -1.0, 4.0, 1.0]}
    fp["centroid"] = {"x": 1.5, "y": 0.0}
    return fp


# ── drill: re-drill a plated pad (slice 2) ───────────────────────────────


def test_op_pad_drill_redrills_a_plated_pad_and_reaches_drc(pcb):
    """Slice 2: ``drill`` is an absolute plated-hole diameter on the pad
    — it rides the same seam, so the DRC's drilled-pad model sees the
    new bore while the other pad and the shared cache row keep 0.8."""
    ref = _seed(pcb, placed=True)
    pcb.store.part_footprint_put(_LCSC, _fp_tht())

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "drill": 1.0},
    )
    assert "override stored" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"drill": 1.0}}

    graph = pcb.store.pcb_graph(ref.id)
    layers = [str(layer["name"]) for layer in graph["board"]["stackup"]]
    pads = pcb._drc_pads(ref.id, layers)
    drilled = {(p["refdes"], p["pin"]): p["drill"] for p in pads if p.get("drill")}
    assert drilled[("U1", "A")] == pytest.approx(1.0)
    assert drilled[("U1", "B")] == pytest.approx(0.8)
    # The shared cache row is never rewritten.
    assert pcb.store.part_footprint_get(_LCSC)["pads"][0]["drill"] == 0.8

    # Per-pad revert restores the catalog bore.
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "revert": True},
    )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def test_op_pad_drill_refuses_on_an_smd_pad(pcb):
    """``drill`` on a surface pad — no plated hole to resize."""
    _seed(pcb)
    with pytest.raises(BadInput, match="no plated hole"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", "drill": 0.6},
        )


def test_op_pad_drill_past_the_annular_floor_is_judged_and_stores_nothing(pcb):
    """Judged like move/resize: re-drilling 0.8→1.4 leaves a 0.10mm
    annular ring, under the fab floor — the call refuses and the
    override is never stored."""
    ref = _seed(pcb, placed=True)
    pcb.store.part_footprint_put(_LCSC, _fp_tht())
    with pytest.raises(BadInput, match="invalid board"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", "drill": 1.4},
        )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def _fp_holed() -> dict:
    """``_fp()`` plus two catalog non-plated holes (``holes[0]``/``[1]`` —
    ``view='pinout'``'s ``hole_index`` 1 and 2)."""
    fp = _fp()
    fp["holes"] = [
        {"x": -1.0, "y": 0.0, "dia_mm": 1.0},
        {"x": 2.0, "y": 0.0, "dia_mm": 1.0},
    ]
    return fp


# ── non-plated holes: add / remove / revert (slice 3) ───────────────────


def test_op_pad_hole_add_rides_the_same_seam(pcb):
    """``hole='add'`` stores under the map's ``#holes`` meta key and the
    resolved footprint carries it: the pinout holes table, the DRC's
    unplated drills and the footprints-view token all see it — the
    shared cache row does not."""
    ref = _seed(pcb, placed=True)
    pcb.store.part_footprint_put(_LCSC, _fp_holed())

    resp = pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    assert "non-plated hole added" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {
        "#holes": {"add": [{"x": 0.5, "y": 1.0, "dia_mm": 0.6}]}
    }
    # The shared cache row is never rewritten.
    assert len(pcb.store.part_footprint_get(_LCSC)["holes"]) == 2

    pinout = pcb.get(id="pad-op#U1", view="pinout")
    assert "footprint holes (non-plated)" in pinout.body
    assert "0.6000" in pinout.body  # the added hole's dia_mm row

    graph = pcb.store.pcb_graph(ref.id)
    ir = pcb._build_ir(ref.id, graph)
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(round(d["x"], 4), round(d["y"], 4), d["dia_mm"]) for d in drills} == {
        (-1.0, 0.0, 1.0),
        (2.0, 0.0, 1.0),
        (0.5, 1.0, 0.6),
    }

    view = pcb.get(id="pad-op", view="footprints")
    u1_row = next(line for line in view.body.splitlines() if line.startswith("U1"))
    assert u1_row.split()[-1] == "holes+1"

    # Re-sending the same add is a no-op, not a second hole.
    again = pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    assert "no change" in again.body


def test_op_pad_hole_remove_drops_catalog_and_added_holes(pcb):
    """``hole='<n>'`` + ``remove=true`` — ``<n>`` is the resolved
    ``hole_index``: a CATALOG index records ``remove`` against the
    catalog list; one naming an ADDED hole pops it back off."""
    ref = _seed(pcb, placed=True)
    pcb.store.part_footprint_put(_LCSC, _fp_holed())

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "2", "remove": True},
    )
    assert "hole 2 removed" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"#holes": {"remove": [2]}}

    graph = pcb.store.pcb_graph(ref.id)
    ir = pcb._build_ir(ref.id, graph)
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {(-1.0, 1.0)}

    # An index naming an added hole pops it off the add list: after the
    # add the resolved holes are [catalog#1, added#2].
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "2", "remove": True},
    )
    assert "hole 2 removed" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"#holes": {"remove": [2]}}


def test_op_pad_hole_edits_revert_with_the_whole_map(pcb):
    """Whole-map ``revert`` drops hole overrides with the pad ones; the
    catalog holes come back."""
    ref = _seed(pcb, placed=True)
    pcb.store.part_footprint_put(_LCSC, _fp_holed())
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "1", "remove": True},
    )
    resp = pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "revert": True})
    assert "all pad overrides reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")

    graph = pcb.store.pcb_graph(ref.id)
    ir = pcb._build_ir(ref.id, graph)
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {
        (-1.0, 1.0),
        (2.0, 1.0),
    }


def test_op_pad_hole_edits_are_per_instance(pcb, store):
    """A second instance of the same C-number keeps catalog holes —
    the override lives on U1's instance row only."""
    ref = _seed(pcb, placed=True, two=True)
    pcb.store.part_footprint_put(_LCSC, _fp_holed())
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    assert not _instance(pcb, ref, "U2").get("pad_overrides")

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    assert len(fps["U1"]["holes"]) == 3
    assert len(fps["U2"]["holes"]) == 2
    u2_view = pcb.get(id="pad-op#U2", view="pinout")
    assert "0.6000" not in u2_view.body


def test_op_pad_hole_onto_router_copper_rips_the_net_and_stores(pcb, store):
    """Judged like the pad half: an added NPTH that lands on stored
    routed copper does not refuse — the colliding net yields, is ripped
    and reported, and the override IS stored. The new hole's edge and
    the N2 track's edge overlap (a negative gap, under the 0.45mm NPTH
    floor)."""
    ref = _seed(pcb, placed=True)
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    nets = store.pcb_net_ids(ref.id)
    store.pcb_routes_write(ref.id, board_id, {"N2": {"status": "realized"}})
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": nets["N2"],
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [-3.0, 1.0], "end": [3.0, 1.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    assert store.pcb_nets_with_router_copper(board_id) == {"N2"}

    resp = pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    assert "N2 ripped" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == set()
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {
        "#holes": {"add": [{"x": 0.5, "y": 1.0, "dia_mm": 0.6}]}
    }


def test_op_pad_hole_arg_validation(pcb):
    _seed(pcb)
    with pytest.raises(BadInput, match="needs remove=true"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "hole": "1"})
    with pytest.raises(BadInput, match="name pad OR hole"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "hole": "add",
                "x": 0.5,
                "y": 1.0,
                "drill": 0.6,
                "pad": "1",
            },
        )
    with pytest.raises(BadInput, match="revert does not combine"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "hole": "add",
                "x": 0.5,
                "y": 1.0,
                "drill": 0.6,
                "revert": True,
            },
        )
    with pytest.raises(BadInput, match="out of range"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "hole": "9", "remove": True},
        )
    with pytest.raises(BadInput, match="'remove' belongs"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "hole": "add", "remove": True},
        )
    with pytest.raises(BadInput, match="remove needs 'pad'"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "remove": True})


# ── dead overrides (slice 2) ─────────────────────────────────────────────


def test_dead_override_is_marked_in_both_views_and_revert_drops_it(pcb):
    """Slice 2, docs/backlog/pcb-pad-overrides.md: an override whose pad
    number no longer exists on the resolved footprint (an
    ``op='footprint'``-style cache-row swap renumbered the pads) must not
    drop silently — ``view='pinout'`` and ``view='footprints'`` say
    "override on pad 1: pad not on footprint" and ``revert`` still drops
    the stored entry."""
    ref = _seed(pcb)
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.2})
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"1": {"dx": 0.2}}

    # The cache row is re-pulled renumbered: pads '1'/'2' become '9'/'10',
    # so the stored override on pad 1 applies to nothing.
    renumbered = _fp()
    for pad, number in zip(renumbered["pads"], ("9", "10"), strict=True):
        pad["number"] = number
    renumbered["pin_map"] = {
        "9": {"name": "A", "tags": []},
        "10": {"name": "B", "tags": []},
    }
    pcb.store.part_footprint_put(_LCSC, renumbered)

    pinout = pcb.get(id="pad-op#U1", view="pinout")
    assert "override on pad 1: pad not on footprint" in pinout.body
    assert "1 dead" in pinout.body

    view = pcb.get(id="pad-op", view="footprints")
    assert "override on pad 1: pad not on footprint" in view.body
    u1_row = next(line for line in view.body.splitlines() if line.startswith("U1"))
    assert u1_row.split()[-1] == "1(dead)"

    # Revert removes the dead entry like any other.
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "1", "revert": True},
    )
    assert "pad 1 override reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")
    view = pcb.get(id="pad-op", view="footprints")
    assert "pad not on footprint" not in view.body


def test_op_pad_authored_add_conflicts_with_a_repulled_catalog_pad(pcb):
    """The other re-pull collision: an authored-only ``add`` whose
    number a LATER cache refresh puts on the catalog must not emit two
    pad 9s — the catalog pad wins and the entry reports where dead
    overrides do, spelled "override on pad 9: catalog now has this
    pad"; ``revert`` still drops it."""
    ref = _seed(pcb, placed=True)
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "9",
            "x": 5.0,
            "y": 5.0,
            "w": 0.8,
        },
    )

    # The cache row is re-pulled and the catalog now carries pad 9 too.
    grown = _fp()
    grown["pads"].append(
        {
            "number": "9",
            "shape": "RECT",
            "x": 1.0,
            "y": 1.0,
            "w": 0.5,
            "h": 0.5,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        }
    )
    grown["pin_map"]["9"] = {"name": "C", "tags": []}
    pcb.store.part_footprint_put(_LCSC, grown)

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    nines = [p for p in fps["U1"]["pads"] if str(p["number"]) == "9"]
    assert len(nines) == 1
    assert nines[0]["x"] == pytest.approx(1.0)  # the catalog pad wins

    pinout = pcb.get(id="pad-op#U1", view="pinout")
    assert "1 dead" in pinout.body
    assert "override on pad 9: catalog now has this pad" in pinout.body
    assert "pad not on footprint" not in pinout.body
    view = pcb.get(id="pad-op", view="footprints")
    assert "override on pad 9: catalog now has this pad" in view.body

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "9", "revert": True},
    )
    assert "pad 9 override reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def test_apply_pad_overrides_reports_the_unclaimed_keys():
    """The seam itself: keys no pad on the row claims land on
    ``dead_pad_overrides``; geometry is untouched."""
    out = pcb_padplace.apply_pad_overrides(_fp(), {"7": {"dx": 0.5}, "2": {"dy": 0.1}})
    assert out["dead_pad_overrides"] == ["7"]
    assert out["pads"][0]["x"] == 0.0  # pad '1' has no override
    assert out["pads"][1]["y"] == pytest.approx(0.1)
    # A clean map reports no dead keys.
    assert (
        pcb_padplace.apply_pad_overrides(_fp(), {"1": {"dx": 1.0}})[
            "dead_pad_overrides"
        ]
        == []
    )


def test_apply_pad_overrides_repulled_catalog_wins_an_authored_add():
    """The seam's conflict case: an authored-only ``add`` whose number
    the row now carries is NOT appended — the catalog pad survives and
    the key lands on ``dead_pad_overrides`` (the views spell it
    "catalog now has this pad"). A remove+add on a live number is the
    replace — it still emits its authored pad and never counts dead."""
    authored = {"x": 9.0, "y": 9.0, "w": 0.4, "h": 0.4, "shape": "rect"}
    out = pcb_padplace.apply_pad_overrides(_fp(), {"1": {"add": authored}})
    ones = [p for p in out["pads"] if str(p["number"]) == "1"]
    assert len(ones) == 1 and ones[0]["x"] == 0.0
    assert out["dead_pad_overrides"] == ["1"]

    out = pcb_padplace.apply_pad_overrides(
        _fp(), {"1": {"remove": True, "add": authored}}
    )
    ones = [p for p in out["pads"] if str(p["number"]) == "1"]
    assert len(ones) == 1 and ones[0]["x"] == pytest.approx(9.0)
    assert out["dead_pad_overrides"] == []


# ── the pure seam ────────────────────────────────────────────────────────


def test_apply_pad_overrides_scales_a_polygon_ring_and_copies():
    """``dx``/``dy`` translate a polygon pad's ring; ``w``/``h`` then scale
    it about its own bbox centre so the outline and ``w``/``h`` stay one
    fact — all on a COPY: the source row and unoverridden pads pass
    through untouched."""
    fp = {
        "pads": [
            {
                "number": "9",
                "shape": "polygon",
                "x": 1.0,
                "y": 0.5,
                "w": 2.0,
                "h": 1.0,
                "poly": [[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [0.0, 1.0]],
            },
            {
                "number": "10",
                "shape": "RECT",
                "x": 5.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
            },
        ]
    }
    assert pcb_padplace.apply_pad_overrides(fp, None) is fp

    out = pcb_padplace.apply_pad_overrides(fp, {"9": {"dx": 1.0, "w": 4.0}})
    assert out is not fp
    pad = out["pads"][0]
    assert pad["w"] == pytest.approx(4.0)
    assert pad["h"] == pytest.approx(1.0)
    assert pad["x"] == pytest.approx(2.0)  # ring bbox centre after dx + scale
    xs = [v[0] for v in pad["poly"]]
    assert min(xs) == 0.0 and max(xs) == 4.0
    assert out["pads"][1] is fp["pads"][1]  # unoverridden pad passes through
    assert fp["pads"][0]["x"] == 1.0  # the source row is not mutated
    assert out["pad_overrides"] == {"9": {"dx": 1.0, "w": 4.0}}


# ── pad add / remove (slice 2 port) ─────────────────────────────────────


def test_op_pad_add_pad_shows_in_pinout_and_drc_not_the_cache(pcb):
    """An authored pad appends to the effective pad list: ``view='pinout'``
    marks it ``authored pad add``, DRC's pad set carries it, the
    footprints view shows ``+9``, and the shared row is untouched."""
    ref = _seed(pcb, placed=True)

    resp = pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "9",
            "x": 0.5,
            "y": 3.0,
            "w": 0.8,
            "h": 0.4,
        },
    )
    assert "pad 9: pad added" in resp.body
    added = _instance(pcb, ref, "U1")["pad_overrides"]["9"]["add"]
    assert added["x"] == pytest.approx(0.5)
    assert added["y"] == pytest.approx(3.0)
    assert added["w"] == pytest.approx(0.8)
    assert added["h"] == pytest.approx(0.4)

    view = pcb.get(id="pad-op#U1", view="pinout")
    assert "1 authored pad override(s)" in view.body
    assert "authored pad add" in view.body
    assert "3.0000" in view.body  # the new pad's local_y_mm

    graph = pcb.store.pcb_graph(ref.id)
    layers = [str(layer["name"]) for layer in graph["board"]["stackup"]]
    pads = pcb._drc_pads(ref.id, layers)
    u1_pads = [p for p in pads if p["refdes"] == "U1"]
    assert any(
        p["x"] == pytest.approx(0.5) and p["y"] == pytest.approx(3.0) for p in u1_pads
    )

    fp_view = pcb.get(id="pad-op", view="footprints")
    u1_row = next(line for line in fp_view.body.splitlines() if line.startswith("U1"))
    assert u1_row.split()[-1] == "+9"

    assert pcb.store.part_footprint_get(_LCSC)["pads"] == _fp()["pads"]


def test_op_pad_add_then_patch_then_revert_the_added_pad(pcb):
    ref = _seed(pcb, placed=True)
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "9",
            "x": 0.5,
            "y": 3.0,
            "w": 0.8,
        },
    )
    # A later patch merges onto the same entry — the added pad nudges.
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "9", "dx": 0.25})
    ov = _instance(pcb, ref, "U1")["pad_overrides"]["9"]
    assert ov["dx"] == 0.25 and "add" in ov

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    added = next(p for p in fps["U1"]["pads"] if str(p["number"]) == "9")
    assert added["x"] == pytest.approx(0.75)

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "9", "revert": True},
    )
    assert "pad 9 override reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")
    view = pcb.get(id="pad-op#U1", view="pinout")
    assert "authored pad add" not in view.body


def test_op_pad_remove_pad_then_revert_restores_it(pcb):
    ref = _seed(pcb, placed=True)
    # Pad 1 carries net N1 (pin A binds it) — removing it must refuse.
    with pytest.raises(BadInput) as exc:
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", "remove": True},
        )
    assert "carries net" in str(exc.value)
    assert "pin_remove" in (exc.value.next or "")
    assert not _instance(pcb, ref, "U1").get("pad_overrides")

    # An unconnected pad removes fine.
    ref2 = _seed_pad2_free(pcb, slug="pad-op2")
    resp = pcb.put(
        id="pad-op2",
        args={"op": "pad", "refdes": "U1", "pad": "2", "remove": True},
    )
    assert "pad 2: pad removed" in resp.body
    assert _instance(pcb, ref2, "U1")["pad_overrides"] == {"2": {"remove": True}}

    view = pcb.get(id="pad-op2#U1", view="pinout")
    assert "1 authored pad override(s)" in view.body
    assert "physical pads: 1" in view.body  # pad 2's row is gone

    resp = pcb.put(
        id="pad-op2",
        args={"op": "pad", "refdes": "U1", "pad": "2", "revert": True},
    )
    assert "pad 2 override reverted" in resp.body
    assert not _instance(pcb, ref2, "U1").get("pad_overrides")


def test_op_pad_readd_replaces_the_removed_catalog_pad(pcb, store):
    """Remove pad 2, then add pad 2: the add stores the REPLACE
    ``{"remove": true, "add": …}`` — exactly one pad 2 with the authored
    geometry everywhere (pinout, DRC, board_pads), and ``revert`` brings
    the catalog pad back."""
    ref = _seed_pad2_free(pcb)
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "remove": True},
    )
    resp = pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "2",
            "x": 2.0,
            "y": 1.0,
            "w": 0.7,
            "h": 0.4,
        },
    )
    assert "pad 2: pad replaced" in resp.body
    ov = _instance(pcb, ref, "U1")["pad_overrides"]["2"]
    assert ov["remove"] is True and ov["add"]["x"] == pytest.approx(2.0)

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    twos = [p for p in fps["U1"]["pads"] if str(p["number"]) == "2"]
    assert len(twos) == 1
    assert twos[0]["x"] == pytest.approx(2.0)
    assert twos[0]["y"] == pytest.approx(1.0)

    graph = pcb.store.pcb_graph(ref.id)
    layers = [str(layer["name"]) for layer in graph["board"]["stackup"]]
    drc_twos = [
        p
        for p in pcb._drc_pads(ref.id, layers)
        if p["refdes"] == "U1" and p["pin"] == "B"
    ]
    assert len(drc_twos) == 1

    # The fab seam: one flash for pad 2, at the authored spot.
    design = store.pcb_load(ref.id)
    pads, _ = pcb_padplace.board_pads(
        design["instances"],
        store.pcb_footprints_for(ref.id),
        layers=layers,
    )
    fab_twos = [p for p in pads if p["refdes"] == "U1" and p["pin"] == "B"]
    assert len(fab_twos) == 1
    assert fab_twos[0]["x"] == pytest.approx(2.0)

    # Revert drops the whole entry — catalog pad 2 is back at (1, 0).
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "revert": True},
    )
    assert "pad 2 override reverted" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")
    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    twos = [p for p in fps["U1"]["pads"] if str(p["number"]) == "2"]
    assert len(twos) == 1 and twos[0]["x"] == pytest.approx(1.0)


def test_op_pad_reremove_keeps_the_replaced_catalog_pad_suppressed(pcb):
    """remove → add → remove on the SAME catalog number: the second
    remove must drop the authored pad yet keep the catalog pad
    suppressed — the entry becomes ``{"remove": true}``, it is not
    deleted (deleting it resurrected the catalog pad while the call
    said "pad removed", and skipped the net-carrying refusal). A later
    ``revert`` restores the catalog pad."""
    ref = _seed_pad2_free(pcb)
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "remove": True},
    )
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "2",
            "x": 2.0,
            "y": 1.0,
            "w": 0.7,
            "h": 0.4,
        },
    )
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "remove": True},
    )
    assert "pad 2: pad removed" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"2": {"remove": True}}

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    assert [str(p["number"]) for p in fps["U1"]["pads"]] == ["1"]

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "pad": "2", "revert": True},
    )
    assert "pad 2 override reverted" in resp.body
    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    fps = pcb_session.footprints_by_refdes(ir, pcb.store.pcb_footprints_for(ref.id))
    twos = [p for p in fps["U1"]["pads"] if str(p["number"]) == "2"]
    assert len(twos) == 1 and twos[0]["x"] == pytest.approx(1.0)


def test_op_pad_add_pad_worsening_clearance_is_refused(pcb):
    """An added pad that lands ON pad 2 is a pad-to-copper error the
    board did not have — the gate refuses and stores nothing."""
    ref = _seed(pcb, placed=True)
    with pytest.raises(BadInput, match="invalid board"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "add": True,
                "pad": "9",
                "x": 1.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
            },
        )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


# ── hole restore + effective-index revert (slice 2 port) ─────────────────


def _fp_holed3() -> dict:
    """``_fp_holed`` plus a third catalog hole — enough that removing
    the LAST one makes the effective-vs-catalog indexing ambiguity
    real."""
    fp = _fp_holed()
    fp["holes"].append({"x": 2.0, "y": 2.0, "dia_mm": 2.8})
    return fp


def test_op_pad_remove_and_restore_a_catalog_hole(pcb):
    """``hole='<n>'`` + ``remove`` suppresses the catalog hole by its
    effective index; ``restore_hole`` then names it by its CATALOG
    index — printed by pinout's removed-catalog-holes table — and the
    override map collapses to nothing."""
    ref = _seed(pcb, placed=True, fp=_fp_holed())

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "2", "remove": True},
    )
    assert "hole 2 removed" in resp.body
    assert _instance(pcb, ref, "U1")["pad_overrides"] == {"#holes": {"remove": [2]}}

    view = pcb.get(id="pad-op#U1", view="pinout")
    effective, _, removed = view.body.partition("removed catalog holes")
    assert "2.0000" not in effective  # gone from the footprint holes table
    assert "2.0000" in removed  # listed for restore_hole, by catalog index
    assert "restore_hole" in view.body
    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {(-1.0, 1.0)}

    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "restore_hole": 2},
    )
    assert "catalog hole 2 restored" in resp.body
    assert "effective hole 2" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")

    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {(-1.0, 1.0), (2.0, 1.0)}


def test_op_pad_remove_an_added_hole_by_effective_index(pcb):
    ref = _seed(pcb, placed=True, fp=_fp_holed())
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": 1.0,
            "drill": 0.6,
        },
    )
    # Effective list is [catalog 1, catalog 2, added] — the authored
    # hole is index 3; removing it drops the add entry entirely.
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "3", "remove": True},
    )
    assert "hole 3 removed" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def test_op_pad_revert_hole_is_effective_indexed_not_catalog(pcb):
    """The review's ambiguity case (gr520149 slice-2 review): catalog
    holes c1..c3, c3 removed, one hole added — pinout prints the added
    hole as effective index 3, so ``revert`` + ``hole:'3'`` must drop
    THAT added hole, not resurrect c3. The removed c3 answers only to
    ``restore_hole:<catalog index>``."""
    ref = _seed(pcb, placed=True, fp=_fp_holed3())
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "3", "remove": True},
    )
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": -1.0,
            "drill": 1.6,
        },
    )
    # Effective list is [c1, c2, a1] — the added hole prints as hole 3.
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "3", "revert": True},
    )
    assert "hole 3 override reverted" in resp.body
    holes_doc = _instance(pcb, ref, "U1")["pad_overrides"]["#holes"]
    assert holes_doc["remove"] == [3]  # c3 stays suppressed
    assert not holes_doc.get("add")  # the added hole is what reverted
    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {(-1.0, 1.0), (2.0, 1.0)}

    # hole:3 is now off the end of the effective list — the refusal
    # names restore_hole for the removed catalog hole it cannot reach.
    with pytest.raises(BadInput, match="out of range") as exc:
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "hole": "3", "revert": True},
        )
    assert "restore_hole" in (exc.value.next or "")

    # A standing catalog hole has no override to undo — unchanged, not
    # an error.
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "1", "revert": True},
    )
    assert "no change" in resp.body

    # restore_hole:<catalog index> brings c3 back.
    resp = pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "restore_hole": 3},
    )
    assert "catalog hole 3 restored" in resp.body
    assert not _instance(pcb, ref, "U1").get("pad_overrides")
    ir = pcb._build_ir(ref.id, pcb.store.pcb_graph(ref.id))
    drills = [d for d in pcb._drc_drills(ir) if not d.get("plated")]
    assert {(d["x"], d["dia_mm"]) for d in drills} == {
        (-1.0, 1.0),
        (2.0, 1.0),
        (2.0, 2.8),
    }


def test_op_pad_restore_hole_refusals(pcb):
    ref = _seed(pcb, placed=True, fp=_fp_holed())
    with pytest.raises(BadInput, match="not removed"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "restore_hole": 1})
    with pytest.raises(BadInput, match="no catalog hole 5"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "restore_hole": 5})
    with pytest.raises(BadInput, match="restore_hole must be a positive"):
        pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "restore_hole": "x"})
    with pytest.raises(BadInput, match="its own edit"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "restore_hole": 1, "hole": 1},
        )
    with pytest.raises(BadInput, match="its own edit"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "restore_hole": 1, "pad": "1"},
        )
    with pytest.raises(BadInput, match="its own edit"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "restore_hole": 1, "add": True},
        )
    with pytest.raises(BadInput, match="its own edit"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "restore_hole": 1,
                "hole": "1",
                "remove": True,
            },
        )
    with pytest.raises(BadInput, match="its own edit"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "restore_hole": 1, "revert": True},
        )
    with pytest.raises(BadInput, match="patch fields"):
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "restore_hole": 1, "dx": 0.1},
        )
    for field, value in (("x", 0.1), ("y", 0.1), ("shape", "rect")):
        with pytest.raises(BadInput, match="patch fields"):
            pcb.put(
                id="pad-op",
                args={
                    "op": "pad",
                    "refdes": "U1",
                    "restore_hole": 1,
                    field: value,
                },
            )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


def test_op_pad_add_hole_inside_another_parts_body_is_refused(pcb):
    """The judged gate at default refuse=True: a new mounting hole under
    U2's body is a courtyard_hole error the board did not have — the
    call refuses and stores nothing."""
    ref = _seed(pcb, placed=True, two=True, fp=_fp_holed())
    with pytest.raises(BadInput, match="invalid board"):
        pcb.put(
            id="pad-op",
            args={
                "op": "pad",
                "refdes": "U1",
                "hole": "add",
                "x": 20.0,
                "y": 0.0,
                "drill": 1.6,
            },
        )
    assert not _instance(pcb, ref, "U1").get("pad_overrides")


# ── cross-design isolation, the sugar address, epro (slice 2 port) ───────


def test_op_pad_edits_are_design_local(pcb):
    """A second design placing the same C-number keeps catalog geometry —
    the overrides live on THIS design's instance meta."""
    ref = _seed(pcb, placed=True, fp=_fp_holed())
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "add": True,
            "pad": "9",
            "x": 0.5,
            "y": 3.0,
            "w": 0.8,
        },
    )
    pcb.put(
        id="pad-op",
        args={"op": "pad", "refdes": "U1", "hole": "1", "remove": True},
    )

    pcb.put(id="pad-op2", args=_design(placed=True))
    view = pcb.get(id="pad-op2#U1", view="pinout")  # same cache row
    assert "authored pad override" not in view.body
    assert "footprint hole(s)" not in view.body
    graph2 = pcb.store.pcb_graph(pcb.store.get_ref(kind="pcb", id="pad-op2").id)
    assert not next(i for i in graph2["instances"] if i["refdes"] == "U1").get(
        "pad_overrides"
    )
    assert pcb.store.part_footprint_get(_LCSC)["holes"] == _fp_holed()["holes"]
    assert _instance(pcb, ref, "U1")["pad_overrides"]  # this design kept them


def test_op_pad_id_selector_sugar(pcb):
    """``<slug>#<refdes>.pad:<id>`` and ``… .hole:<n>`` fill
    refdes/pad/hole from the id — the address form the brief names; a
    disagreeing explicit arg refuses."""
    _seed(pcb, placed=True, fp=_fp_holed())

    resp = pcb.put(id="pad-op#U1.pad:2", args={"op": "pad", "dx": 0.15})
    assert "# U1 pad 2" in resp.body
    resp = pcb.put(id="pad-op#U1.hole:2", args={"op": "pad", "remove": True})
    assert "hole 2 removed" in resp.body
    # The same facts spelled in args still work, and must not fight.
    resp = pcb.put(
        id="pad-op#U1.pad:2",
        args={"op": "pad", "refdes": "U1", "pad": "2", "revert": True},
    )
    assert "pad 2 override reverted" in resp.body
    with pytest.raises(BadInput, match="must agree"):
        pcb.put(
            id="pad-op#U1.pad:1",
            args={"op": "pad", "pad": "2", "dx": 0.1},
        )


def test_op_pad_overrides_split_the_shared_epro_footprint_doc(pcb):
    """gr520149 slice-2 review, item 3: Pro shares ONE FOOTPRINT document
    per distinct footprint, but ``footprint_holes`` is modelled per
    refdes — two instances of one cache row with different per-instance
    overrides must not collapse onto one document. ``_footprint_groups``
    keys on the un-placed pad set + hole set, so an override on ONE
    instance writes it its own document. Verified for an added hole
    AND a moved pad (same key, same outcome)."""
    ref = _seed(pcb, placed=True, two=True, fp=_fp_holed())

    def footprint_docs():
        build = pcb.epro_build(ref.id)
        stream = build.exported.files["pad-op.epru"]
        return [
            d for d in pcb_epro.split_documents(stream) if d.doc_type == "FOOTPRINT"
        ]

    # Identical instances share one document; an added hole on U1 splits it.
    assert len(footprint_docs()) == 1
    pcb.put(
        id="pad-op",
        args={
            "op": "pad",
            "refdes": "U1",
            "hole": "add",
            "x": 0.5,
            "y": -1.0,
            "drill": 1.6,
        },
    )
    docs = footprint_docs()
    assert len(docs) == 2
    # Each document carries ITS instance's hole set — never a merged
    # document that gives U2 U1's extra hole.
    assert sorted(len(d.bodies("FILL")) for d in docs) == [2, 3]

    # A moved pad on U1 alone also splits the document (the group key's
    # pad canon differs).
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "revert": True})
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "pad": "1", "dx": 0.4})
    assert len(footprint_docs()) == 2
    pcb.put(id="pad-op", args={"op": "pad", "refdes": "U1", "revert": True})
    assert len(footprint_docs()) == 1


# ── the pure seam (slice 2 port) ─────────────────────────────────────────


def test_apply_pad_overrides_add_remove_holes_and_pads():
    """The map's slice-2 shape on a COPY: ``remove`` drops the catalog
    pad/hole, ``add`` appends the authored one after the survivors, and
    the source row is never mutated."""
    fp: dict = {
        "pads": [
            {"number": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
            {"number": "2", "shape": "rect", "x": 1.0, "y": 0.0, "w": 0.5, "h": 0.5},
        ],
        "holes": [
            {"x": 0.0, "y": 2.0, "dia_mm": 2.2},
            {"x": 1.0, "y": 2.0, "dia_mm": 3.2},
        ],
    }
    out = pcb_padplace.apply_pad_overrides(
        fp,
        {
            "2": {"remove": True},
            "9": {
                "add": {
                    "x": 0.5,
                    "y": 3.0,
                    "w": 0.8,
                    "h": 0.4,
                    "shape": "rect",
                    "layer": "F.Cu",
                },
                "dx": 0.25,
            },
            "#holes": {"add": [{"x": 0.5, "y": -1.0, "dia_mm": 1.6}], "remove": [1]},
        },
    )
    assert [p["number"] for p in out["pads"]] == ["1", "9"]
    added = out["pads"][1]
    assert added["x"] == pytest.approx(0.75) and added["w"] == pytest.approx(0.8)
    assert out["holes"] == [
        {"x": 1.0, "y": 2.0, "dia_mm": 3.2},
        {"x": 0.5, "y": -1.0, "dia_mm": 1.6},
    ]
    # Neither entry counts as dead — the authored pad is live geometry.
    assert out["dead_pad_overrides"] == []
    assert [p["number"] for p in fp["pads"]] == ["1", "2"]
    assert len(fp["holes"]) == 2


def test_pad_hole_targets_numbers_effective_then_added():
    fp = {"holes": [{"x": 0, "y": 0, "dia_mm": 1}, {"x": 1, "y": 0, "dia_mm": 1}]}
    ov = {"#holes": {"add": [{"x": 2, "y": 0, "dia_mm": 1}], "remove": [1]}}
    assert pcb_padplace.pad_hole_targets(fp, ov) == [
        ("catalog", 2),
        ("added", 0),
    ]
    assert pcb_padplace.pad_hole_targets(None, None) == []
