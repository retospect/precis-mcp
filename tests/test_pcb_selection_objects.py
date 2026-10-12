"""Pads, vias, silkscreen, outline, fiducials and nets as selectable objects
(gr479844) on a seeded fixture board — extends test_pcb_design_chat.py."""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET

import pytest

from precis.design import history
from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis_web import design_turn, pcb_design


def seed(store, slug="object_fixture"):
    hub = Hub(store=store)
    PcbHandler(hub=hub).put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": r,
                    "label": "resistor",
                    "footprint": "local-R",
                    "pins": [{"name": "1", "pad": "1"}],
                }
                for r in ("R1", "R2")
            ],
            "nets": [{"name": "SIG"}],
            "connections": [{"net": "SIG", "refdes": "R1", "pin": "1"}],
            "footprints": [
                {
                    "name": "local-R",
                    "pads": [
                        {
                            "number": "1",
                            "x": 0,
                            "y": 0,
                            "w": 0.6,
                            "h": 0.6,
                            "shape": "rect",
                        }
                    ],
                }
            ],
            "features": [
                {
                    "ftype": "outline",
                    "geom": {"path": [[0, 0], [40, 0], [40, 30], [0, 30]]},
                }
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    store.pcb_move_instance(ref.id, "R1", x=15, y=15, rot=0)
    store.pcb_move_instance(ref.id, "R2", x=25, y=15, rot=0)
    board_id = store.pcb_ensure_board(ref.id)
    store.pcb_fixed_copper_put(
        ref.id,
        board_id,
        "VIA_GEN",
        "fixture",
        "v1",
        [
            {
                "ctype": "via",
                "layer": "F.Cu",
                "net": "SIG",
                "geom": {
                    "x": 20.0,
                    "y": 5.0,
                    "dia_mm": 0.45,
                    "drill_mm": 0.2,
                    "span": ["F.Cu", "B.Cu"],
                },
            }
        ],
    )
    return hub, ref


def view_of(store, ref):
    with store.pcb_view_tx() as conn:
        return pcb_design.capture_view(store, ref, conn=conn)


def catalogue(store, ref):
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        return view, pcb_design.selection_objects(store, view)


def context_for(view, item):
    return {**view.context, "selected": [item]}


def by_key(objects, prefix):
    return [o for o in objects if o["key"].startswith(prefix)]


def test_catalogue_covers_every_kind_on_the_fixture_board(store):
    _, ref = seed(store)
    view, objects = catalogue(store, ref)
    kinds = {o["kind"] for o in objects}
    assert {"instance", "pad", "via", "silk", "outline", "fiducial", "net"} <= kinds
    keys = [o["key"] for o in objects]
    assert len(keys) == len(set(keys))
    r1_pad = next(
        o for o in objects if o["key"].startswith("pad:") and o["refdes"] == "R1"
    )
    assert r1_pad["instance_id"] == next(
        i["instance_id"] for i in view.instances if i["refdes"] == "R1"
    )
    assert (r1_pad["net"], r1_pad["pin"]) == ("SIG", "1")
    assert r1_pad["pin_id"] is not None and r1_pad["net_id"] is not None
    (via,) = by_key(objects, "via:")
    assert via["key"] == "via:20.0000:5.0000" and via["fixed"] is True
    assert [o["index"] for o in objects if o["kind"] == "outline"] == [0, 1, 2, 3]
    assert not any(o["kind"] == "via" and o["x"] != 20.0 for o in objects)
    json.dumps(objects, allow_nan=False)


def test_shown_svg_carries_object_hits_and_default_render_is_unchanged(store):
    hub, ref = seed(store)
    handler = PcbHandler(hub=hub)
    plain = handler._render_fab_svg(ref.id, ref.slug).body
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        marked = handler._render_fab_svg(
            ref.id, ref.slug, selection_manifest=pcb_design.selection_manifest(view)
        ).body
        objects = pcb_design.selection_objects(store, view)
    assert handler._render_fab_svg(ref.id, ref.slug).body == plain
    assert "pcb-select" not in plain and "pcb-objects" not in plain
    root = ET.fromstring(marked)
    meta = next(e for e in root.iter() if e.get("id") == "pcb-objects")
    assert json.loads(meta.text or "") == [
        json.loads(json.dumps(o, sort_keys=True)) for o in objects
    ]
    for o in objects:
        if o["kind"] in {"pad", "via", "outline", "silk", "fiducial"}:
            assert f'data-pcb-object="{o["key"]}"' in marked, o["key"]
    # the overlay never carries a handle: pcb-argue walks past it
    overlay = marked[marked.index('<g class="pcb-select">') :]
    overlay = overlay[: overlay.index("</g>")]
    assert "data-handle" not in overlay
    kinds = re.findall(r'data-pcb-kind="(\w+)"', overlay)
    order = ["instance", "net", "outline", "silk", "pad", "via", "fiducial"]
    assert [order.index(k) for k in kinds] == sorted(order.index(k) for k in kinds)


def test_every_identity_echoed_from_svg_metadata_resolves(store):
    # The client sends JSON.parse of the <metadata> text back verbatim; the
    # float/list/None shapes must survive that round trip and still match.
    hub, ref = seed(store)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        marked = (
            PcbHandler(hub=hub)
            ._render_fab_svg(
                ref.id, ref.slug, selection_manifest=pcb_design.selection_manifest(view)
            )
            .body
        )
    meta = next(e for e in ET.fromstring(marked).iter() if e.get("id") == "pcb-objects")
    echoed = json.loads(meta.text or "")
    assert {o["kind"] for o in echoed} >= {"instance", "pad", "outline", "fiducial"}
    for item in echoed:
        ctx = json.loads(json.dumps(context_for(view, item)))
        assert pcb_design.resolve_selection(view, ctx, store)["selected"][0] == item


def test_resolve_accepts_exact_pad_and_refuses_tampering(store):
    _, ref = seed(store)
    view, objects = catalogue(store, ref)
    pad = next(o for o in objects if o["kind"] == "pad" and o["refdes"] == "R1")
    ctx = context_for(view, copy.deepcopy(pad))
    assert pcb_design.resolve_selection(view, ctx, store)["selected"] == [pad]
    for field, value in (
        ("net_id", pad["net_id"] + 1),
        ("pin_id", 123456789),
        ("net", "GND"),
    ):
        forged = copy.deepcopy(ctx)
        forged["selected"][0][field] = value
        with pytest.raises(ValueError, match="forged"):
            pcb_design.resolve_selection(view, forged, store)
    unknown = copy.deepcopy(ctx)
    unknown["selected"][0]["key"] = "pad:999:9"
    with pytest.raises(ValueError, match="absent, ambiguous or forged"):
        pcb_design.resolve_selection(view, unknown, store)
    # non-instance kinds cannot be resolved without a store to rebuild from
    with pytest.raises(ValueError, match="forged"):
        pcb_design.resolve_selection(view, ctx)


def test_changed_board_refuses_a_pad_selection(store):
    _, ref = seed(store)
    view, objects = catalogue(store, ref)
    pad = next(o for o in objects if o["kind"] == "pad")
    store.pcb_move_instance(ref.id, "R2", x=26, y=15, rot=0)
    with store.pcb_view_tx() as conn:
        now = pcb_design.capture_view(store, ref, conn=conn)
    with pytest.raises(ValueError, match="changed"):
        pcb_design.resolve_selection(now, context_for(view, pad), store)


def test_legacy_instance_without_kind_still_resolves(store):
    _, ref = seed(store)
    view = view_of(store, ref)
    legacy = {k: v for k, v in view.instances[0].items() if k not in {"kind", "key"}}
    out = pcb_design.resolve_selection(view, context_for(view, legacy), store)
    assert out["selected"] == [view.instances[0]]
    assert view.instances[0]["kind"] == "instance"
    assert view.instances[0]["key"] == f"instance:{view.instances[0]['instance_id']}"


def test_digest_names_the_selected_object(store):
    _, ref = seed(store)
    view, objects = catalogue(store, ref)
    via = by_key(objects, "via:")[0]
    body = json.loads(
        pcb_design.digest(store, ref.slug, context=context_for(view, via))
    )
    assert body["selected"] == via
    assert "selected" not in json.loads(pcb_design.digest(store, ref.slug))


def test_move_with_a_pad_selected_is_refused_but_discussion_is_recorded(store):
    hub, ref = seed(store)
    view, objects = catalogue(store, ref)
    pad = next(o for o in objects if o["kind"] == "pad" and o["refdes"] == "R1")
    ctx = context_for(view, pad)
    before = store.pcb_load(ref.id)
    refused = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="lock it",
        context=ctx,
        model_call=lambda _: json.dumps(
            {"ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}], "rationale": "x"}
        ),
    )
    assert not refused.applied and not refused.valid
    assert "select the component" in (refused.error or "")
    applied = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=refused.turn,
        shown=ctx["shown"]["snapshot"],
    )
    assert not applied.applied
    assert store.pcb_load(ref.id) == before
    assert history.list_revisions(store, ref.id) == []
    answered = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="what net is this pad on?",
        context=ctx,
        model_call=lambda _: json.dumps({"ops": [], "rationale": "It is on SIG."}),
    )
    assert not answered.applied and not answered.proposal
    assert answered.rationale == "It is on SIG."
    assert history.list_revisions(store, ref.id) == []


def test_selecting_the_component_of_a_pad_still_proposes_a_move(store):
    hub, ref = seed(store)
    view, objects = catalogue(store, ref)
    pad = next(o for o in objects if o["kind"] == "pad" and o["refdes"] == "R1")
    inst = next(i for i in view.instances if i["instance_id"] == pad["instance_id"])
    ctx = context_for(view, inst)
    turn = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="lock resistor",
        context=ctx,
        model_call=lambda _: json.dumps(
            {"ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}], "rationale": "x"}
        ),
    )
    assert turn.valid and turn.proposal and not turn.applied


def test_vet_move_blocks_ops_for_non_instance_kinds_only():
    op = {"op": "move", "refdes": "R1", "fixed": "xy"}
    ctx = {"selected": [{"kind": "via", "key": "via:1:1", "refdes": "R1"}]}
    assert "select the component" in (pcb_design.vet_move([op], ctx) or "")
    assert pcb_design.vet_move([], ctx) is None
    legacy = {"selected": [{"refdes": "R1"}]}
    assert pcb_design.vet_move([op], legacy) is None
