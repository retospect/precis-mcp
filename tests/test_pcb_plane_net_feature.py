"""A batch's ``plane_net`` FEATURE reaches ``pcb_planes`` on the store path
(topo-route-geometry-truth root cause 4, slice G4).

A board script's ``b.plane(1, net=GND)`` lands in ``features`` as
``{"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"}``. The laptop
path (``pcb.local._design``) converted it into the rows
``apply_plane_assignments`` promotes; the batch put filed it as a plain
``pcb_features`` row with its ``net`` dropped and wrote no ``pcb_planes``
row at all — so the bench poured GND while every job run routed it as a
36-branch signal tree. Both paths now read the feature through
:func:`precis.pcb.planes.plane_rows_from_features`.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import local
from precis.pcb import session as pcb_session
from precis.pcb import session_store as pcb_session_store
from tests._pcb_outline import closed_outline

_P2: dict[str, Any] = {
    "name": "P2",
    "pads": [
        {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
    ],
    "pin_map": {"1": "1", "2": "2"},
}

#: What ``precis_geom.pcb.Board.plane(1, net=GND)`` emits (``priority``
#: omitted here: the converter defaults it to 0).
_PLANE: dict[str, Any] = {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"}


def _part(refdes: str, x: float, y: float) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "label": "r",
        "footprint": "P2",
        "x": x,
        "y": y,
        "pins": [{"name": "1"}, {"name": "2"}],
    }


def _batch(*features: dict[str, Any]) -> dict[str, Any]:
    return {
        "footprints": [_P2],
        "components": [_part("R1", 8.0, 10.0), _part("R2", 22.0, 10.0)],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R1", "pin": "2"},
            {"net": "S", "refdes": "R2", "pin": "1"},
        ],
        "features": [closed_outline(0.0, 0.0, 30.0, 20.0), *features],
    }


_GND_ROW = {
    "layer": "In1.Cu",
    "net": "GND",
    "region_hint": None,
    "source": "authored",
    "region": None,
    "priority": 0,
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _ref_id(store, slug: str) -> int:
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_plane_net_feature_lands_as_a_plane_row_not_a_feature_row(pcb, store):
    """One authored ``pcb_planes`` row, no ``pcb_features`` row of ftype
    ``plane_net``, the put counts it apart from the features, and
    ``view='planes'`` lists it. A re-put is idempotent (the assign is an
    upsert keyed by board/layer/net)."""
    resp = pcb.put(id="pnf-lands", args=_batch(_PLANE))
    assert "+1 plane(s)" in resp.body
    # The outline is the only placed feature; the plane is not one.
    assert "+1 feature(s)" in resp.body
    ref_id = _ref_id(store, "pnf-lands")
    assert store.pcb_planes_list(ref_id) == [_GND_ROW]
    assert [f["ftype"] for f in store.pcb_features_list(ref_id)] == ["outline"]

    planes = pcb.get(id="pnf-lands", view="planes").body
    assert "no plane assignments" not in planes
    assert "In1.Cu" in planes and "GND" in planes

    pcb.put(id="pnf-lands", args=_batch(_PLANE))
    assert store.pcb_planes_list(ref_id) == [_GND_ROW]
    assert [f["ftype"] for f in store.pcb_features_list(ref_id)] == ["outline"]


def test_plane_net_feature_priority_is_written(pcb, store):
    resp = pcb.put(id="pnf-prio", args=_batch({**_PLANE, "priority": 3}))
    assert "+1 plane(s)" in resp.body
    assert store.pcb_planes_list(_ref_id(store, "pnf-prio")) == [
        {**_GND_ROW, "priority": 3}
    ]


def test_job_ir_sees_the_plane(pcb, store):
    """The job side (``workers/job_types/pcb_place.py``): the snapshot's
    ``planes`` are the stored rows, and ``apply_plane_assignments``
    promotes GND onto In1.Cu of a fresh IR — the bitmask the topo router's
    ``_plane_nets`` reads. Before this change it was 0 on every stored
    board, so GND was routed as a signal tree."""
    pcb.put(id="pnf-ir", args=_batch(_PLANE))
    design = pcb_session_store.load_snapshot(store, _ref_id(store, "pnf-ir"))
    assert design.planes == [_GND_ROW]
    ir = pcb_session.build_ir(
        design.graph, outline=pcb_session.outline_from_features(design.features)
    )
    gnd = list(ir.net_name).index("GND")
    assert int(ir.net_plane_layers[gnd]) == 0
    pcb_session.apply_plane_assignments(ir, design.planes)
    in1 = [str(e["name"]) for e in ir.stackup].index("In1.Cu")
    assert int(ir.net_plane_layers[gnd]) & (1 << in1)
    s = list(ir.net_name).index("S")
    assert int(ir.net_plane_layers[s]) == 0


def test_plane_net_feature_naming_an_absent_net_refuses_the_put(pcb, store):
    """Same discipline as a stackup ``plane_net`` naming no net: refused
    with the ``op='plane_net'`` repair, and nothing of the put lands —
    the design is not even created."""
    with pytest.raises(BadInput, match="no net 'NOPE'") as exc:
        pcb.put(id="pnf-absent", args=_batch({**_PLANE, "net": "NOPE"}))
    assert "'op':'plane_net'" in str(exc.value.next)
    assert "'layer':'In1.Cu'" in str(exc.value.next)
    assert "'net':'NOPE'" in str(exc.value.next)
    assert store.get_ref(kind="pcb", id="pnf-absent") is None


def test_refused_plane_net_feature_leaves_an_existing_board_untouched(pcb, store):
    """On a board that already exists the refusal rolls back the WHOLE
    re-put: the good plane declaration beside the bad one is not written,
    and neither is the new feature the same batch carried."""
    pcb.put(id="pnf-partial", args=_batch(_PLANE))
    ref_id = _ref_id(store, "pnf-partial")
    before_planes = store.pcb_planes_list(ref_id)
    before_features = store.pcb_features_list(ref_id)
    assert before_planes == [_GND_ROW]

    with pytest.raises(BadInput, match="In2.Cu: no net 'NOPE'"):
        pcb.put(
            id="pnf-partial",
            args=_batch(
                _PLANE,
                {"ftype": "plane_net", "layer": "In2.Cu", "net": "S"},
                {"ftype": "plane_net", "layer": "In2.Cu", "net": "NOPE"},
                {"ftype": "testpoint", "x": 1.0, "y": 1.0, "layer": "F.Cu"},
            ),
        )
    assert store.pcb_planes_list(ref_id) == before_planes
    assert store.pcb_features_list(ref_id) == before_features


def test_local_design_yields_the_same_rows_as_the_store(pcb, store):
    """Shared-converter parity: the laptop path's ``design.planes`` and
    the stored ``pcb_planes_list`` rows are the same dicts, so the bench
    and the job pour the same board."""
    batch = _batch({**_PLANE, "priority": 2})
    pcb.put(id="pnf-parity", args=batch)
    local_rows = local._design(batch, None).planes
    assert local_rows == store.pcb_planes_list(_ref_id(store, "pnf-parity"))
    assert local_rows == [{**_GND_ROW, "priority": 2}]


def test_batch_without_a_plane_feature_counts_no_planes(pcb, store):
    resp = pcb.put(id="pnf-none", args=_batch())
    assert "plane(s)" not in resp.body
    assert store.pcb_planes_list(_ref_id(store, "pnf-none")) == []


def test_plane_net_on_a_layer_the_stackup_lacks_is_refused(pcb, store):
    """A layer outside the board's stackup was stored then silently skipped
    by ``apply_plane_assignments``; now it is refused, naming the layer and
    listing the stackup's. Judged against the batch's own ``meta.stackup``:
    a 2-layer preset has no In1.Cu even though the default does."""
    with pytest.raises(BadInput, match="'Nope.Cu'") as exc:
        pcb.put(
            id="pnf-layer",
            args=_batch({**_PLANE, "layer": "Nope.Cu"}),
        )
    assert "In1.Cu" in (exc.value.options or [])
    assert store.get_ref(kind="pcb", id="pnf-layer") is None

    with pytest.raises(BadInput, match="'In1.Cu'") as exc:
        pcb.put(
            id="pnf-layer-2l",
            args={**_batch(_PLANE), "meta": {"stackup_preset": "2l"}},
        )
    assert list(exc.value.options or []) == ["F.Cu", "B.Cu"]
    assert store.get_ref(kind="pcb", id="pnf-layer-2l") is None


def test_two_whole_board_nets_on_one_layer_are_refused(pcb, store):
    """Within a batch, and against a stored whole-board row; the same net
    re-put stays idempotent."""
    with pytest.raises(BadInput, match="one sheet of copper"):
        pcb.put(
            id="pnf-clash",
            args=_batch(_PLANE, {**_PLANE, "net": "S"}),
        )
    assert store.get_ref(kind="pcb", id="pnf-clash") is None

    pcb.put(id="pnf-clash-2", args=_batch(_PLANE))
    ref_id = _ref_id(store, "pnf-clash-2")
    with pytest.raises(BadInput, match="'S' and 'GND'"):
        pcb.put(id="pnf-clash-2", args=_batch({**_PLANE, "net": "S"}))
    assert store.pcb_planes_list(ref_id) == [_GND_ROW]
    pcb.put(id="pnf-clash-2", args=_batch(_PLANE))
    assert store.pcb_planes_list(ref_id) == [_GND_ROW]


@pytest.mark.parametrize("missing", ["layer", "net"])
def test_plane_net_feature_missing_layer_or_net_is_refused(pcb, store, missing):
    feature = {k: v for k, v in _PLANE.items() if k != missing}
    with pytest.raises(BadInput, match="needs a layer and a net") as exc:
        pcb.put(id="pnf-shape", args=_batch(feature))
    assert "'ftype':'plane_net'" in str(exc.value.next)
    assert store.get_ref(kind="pcb", id="pnf-shape") is None
    with pytest.raises(BadInput, match="needs a layer and a net"):
        local._design(_batch({**_PLANE, missing: "  "}), None)


@pytest.mark.parametrize("bad", [True, 2.5, "10"])
def test_plane_net_feature_non_integer_priority_is_refused(pcb, store, bad):
    with pytest.raises(BadInput, match="priority must be an integer"):
        pcb.put(id="pnf-prio-bad", args=_batch({**_PLANE, "priority": bad}))
    assert store.get_ref(kind="pcb", id="pnf-prio-bad") is None


def test_type_alias_plane_net_feature_is_a_plane_not_a_dead_row(pcb, store):
    """``_pcb_insert_feature`` reads ``ftype`` or ``type``; the plane test
    must too, or ``{"type": "plane_net"}`` files a dead feature row."""
    feature = {"type": "plane_net", "layer": "In1.Cu", "net": "GND"}
    resp = pcb.put(id="pnf-type", args=_batch(feature))
    assert "+1 plane(s)" in resp.body
    ref_id = _ref_id(store, "pnf-type")
    assert store.pcb_planes_list(ref_id) == [_GND_ROW]
    assert [f["ftype"] for f in store.pcb_features_list(ref_id)] == ["outline"]


def test_reput_with_priority_omitted_keeps_the_stored_priority(pcb, store):
    """As ``op='plane_net'`` does: an omitted priority is not a reset to 0."""
    pcb.put(id="pnf-keep", args=_batch({**_PLANE, "priority": 3}))
    ref_id = _ref_id(store, "pnf-keep")
    pcb.put(id="pnf-keep", args=_batch(_PLANE))
    assert store.pcb_planes_list(ref_id) == [{**_GND_ROW, "priority": 3}]
    pcb.put(id="pnf-keep", args=_batch({**_PLANE, "priority": 5}))
    assert store.pcb_planes_list(ref_id) == [{**_GND_ROW, "priority": 5}]
    # The laptop reader still gets a number.
    assert local._design(_batch(_PLANE), None).planes == [_GND_ROW]
