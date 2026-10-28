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
adds an error stores nothing. ``add``/``remove``/``hole``/``drill`` are
slice-2+ and refuse with ``next=`` naming the note.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
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


def _seed(pcb, slug: str = "pad-op", **design_kw):
    pcb.put(id=slug, args=_design(**design_kw))
    pcb.store.part_footprint_put(_LCSC, _fp())
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


@pytest.mark.parametrize("key", ["add", "remove", "hole", "drill"])
def test_op_pad_refuses_slice2_args_with_next_naming_the_note(pcb, key):
    _seed(pcb)
    with pytest.raises(BadInput) as exc:
        pcb.put(
            id="pad-op",
            args={"op": "pad", "refdes": "U1", "pad": "1", key: True, "dx": 0.1},
        )
    assert "pcb-pad-overrides.md" in (exc.value.next or "")


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
