"""CadHandler end-to-end against a live store.

Exercises the full round-trip: author a design via put, read its node
tree, read a single node, run each probe / analysis view, and soft-delete.
Uses the same ``store`` fixture every DB-backed handler test uses.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.cad import CadHandler

_FLANGE = """
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
bolts     cut  cyl:r2.5h10  @18,0,-1  polar:n6r18
"""

_ASSEMBLY = """
component shaft
rod   add  cyl:r5h40   @0,0,-20
component hub
plate add  cyl:r20h10
bore  cut  cyl:r5.1h12 @0,0,-1
"""


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


def test_put_creates_and_lists(cad):
    resp = cad.put(id="flange", text=_FLANGE)
    assert "created" in resp.body
    assert "plate" in resp.body and "hub_bore" in resp.body
    # listing shows it
    lst = cad.get()
    assert "flange" in lst.body


def test_put_replace_updates(cad):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.put(id="flange", text="plate add cyl:r10h5")
    assert "updated" in resp.body
    tree = cad.get(id="flange")
    # the old bore/bolts are gone after replace
    assert "hub_bore" not in tree.body
    assert "plate" in tree.body


def test_put_bad_source_rejected(cad):
    with pytest.raises(BadInput):
        cad.put(id="bad", text="plate frobnicate cyl:r1h1")


def test_get_node_json(cad):
    cad.put(id="flange", text=_FLANGE)
    tree = cad.get(id="flange")
    # pull a node handle (ca<id>) out of the tree table
    handle = next(
        t for t in tree.body.split() if t.startswith("ca") and t[2:].isdigit()
    )
    node = cad.get(id=handle)
    assert "config" in node.body or "cyl" in node.body


def test_probe_ray(cad):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.get(id="flange", view="ray", args={"o": [-30, 0, 4], "d": [1, 0, 0]})
    assert "void" in resp.body  # the bore (and bolt holes) read as void
    assert "hub_bore" in resp.body


def test_probe_point_in_bore(cad):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.get(id="flange", view="point", args={"p": [0, 0, 4]})
    assert "empty" in resp.body
    assert "hub_bore" in resp.body


def test_section(cad):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.get(id="flange", view="section", args={"z": 4})
    assert "plate" in resp.body


def test_clearance_assembly(cad):
    cad.put(id="asm", text=_ASSEMBLY)
    resp = cad.get(id="asm", view="clearance", args={"a": "shaft", "b": "hub"})
    assert "clearance" in resp.body
    assert "clear" in resp.body  # 0.1 mm radial gap → not interfering


def test_volume(cad):
    cad.put(id="flange", text="plate add cyl:r10h10")
    resp = cad.get(id="flange", view="volume")
    assert "mm³" in resp.body and "sampled" in resp.body


# A hub (disc r5) + a rim (annulus r15..20) bridged by a spoke — hub and rim
# don't touch directly, only through the spoke.
_WHEEL = """
component hub
hdisc add cyl:r5h4
component rim
rdisc add cyl:r20h4
rhole cut cyl:r15h6 @0,0,-1
component spoke
sbar  add box:w20d2h4 @10,0,0
"""


def test_connectivity_view_reports_one_solid_and_path(cad):
    cad.put(id="wheel", text=_WHEEL)
    rep = cad.get(id="wheel", view="connectivity")
    assert "one connected solid" in rep.body
    # what touches the spoke → hub and rim
    nb = cad.get(id="wheel", view="connectivity", args={"of": "spoke"})
    assert "hub" in nb.body and "rim" in nb.body
    # a contact path hub → rim must route through the spoke
    p = cad.get(id="wheel", view="connectivity", args={"a": "hub", "b": "rim"})
    assert "spoke" in p.body and "→" in p.body


def test_connectivity_flags_disconnected_bodies(cad):
    text = "component hub\nh add cyl:r5h4\ncomponent rim\nr add cyl:r5h4 @100,0,0\n"
    resp = cad.put(id="split", text=text)
    assert "floating" in resp.body or "disconnected" in resp.body
    rep = cad.get(id="split", view="connectivity")
    assert "separate bodies" in rep.body
    # no contact path between the two lone parts
    p = cad.get(id="split", view="connectivity", args={"a": "hub", "b": "rim"})
    assert "separate bodies" in p.body


def test_delete(cad):
    cad.put(id="flange", text=_FLANGE)
    cad.delete(id="flange")
    with pytest.raises(NotFound):
        cad.get(id="flange")


def test_search_card_written(cad, store):
    cad.put(id="flange", text=_FLANGE)
    ref = store.get_ref(kind="cad", id="flange")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref.id,),
        ).fetchall()
    assert len(rows) == 1, "exactly one search card per design"
    card = rows[0][0]
    # the author's node names carry the searchable intent
    assert "hub_bore" in card and "bolts" in card
    assert "flange" in card.lower()


def test_replace_keeps_one_card_and_no_stale_nodes(cad, store):
    cad.put(id="flange", text=_FLANGE)
    cad.put(id="flange", text="plate add cyl:r10h5")
    ref = store.get_ref(kind="cad", id="flange")
    with store.pool.connection() as conn:
        ncards = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref.id,),
        ).fetchone()[0]
        nlive = conn.execute(
            "SELECT count(*) FROM cad_nodes WHERE ref_id = %s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()[0]
    assert ncards == 1
    assert nlive == 1  # only the new 'plate' node is live


def test_scad_view(cad):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.get(id="flange", view="scad")
    assert "difference()" in resp.body
    assert "cylinder(" in resp.body


_BRACKET = """
desc: L-shaped mounting bracket for a temperature sensor
use: bolts the sensor housing to the reactor backplate
component bracket
base  add  box:w40d40h5
hole  cut  cyl:r3h6  @10,10,-1
"""


def test_desc_use_parsed_into_card(cad, store):
    cad.put(id="bracket", text=_BRACKET)
    ref = store.get_ref(kind="cad", id="bracket")
    with store.pool.connection() as conn:
        card = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref.id,),
        ).fetchone()[0]
    assert "mounting bracket" in card
    assert "temperature sensor" in card
    assert "Used for:" in card and "backplate" in card


def test_search_finds_by_description(cad):
    cad.put(id="bracket", text=_BRACKET)
    cad.put(id="flange", text=_FLANGE)
    # a word that lives only in the bracket's description, not its geometry
    resp = cad.search(q="sensor", mode="lexical")
    assert "bracket" in resp.body
    assert "flange" not in resp.body  # the flange card has no 'sensor'


def test_search_hits_are_design_level(cad):
    cad.put(id="bracket", text=_BRACKET)
    hits = cad.search_hits(q="reactor backplate", mode="lexical")
    assert hits, "expected a cross-kind hit for the bracket"
    h = hits[0]
    assert h.kind == "cad"
    assert h.slug == "bracket"
    assert h.uhandle and h.uhandle.startswith("cd")  # design ref handle, not a node
    assert "backplate" in h.preview


def test_get_stl_view(cad, tmp_path):
    from precis.cad.export import manifold_available

    if not manifold_available():
        import pytest as _pt

        _pt.skip("manifold3d not installed")
    cad.put(id="flange", text=_FLANGE)
    out = tmp_path / "flange.stl"
    resp = cad.get(id="flange", view="stl", args={"path": str(out)})
    assert out.exists() and out.stat().st_size > 0
    assert "STL" in resp.body and str(out) in resp.body


def test_get_step_view(cad, tmp_path):
    from precis.cad.export import step_available

    if not step_available():
        import pytest as _pt

        _pt.skip("OpenCASCADE (cad-step) not installed")
    cad.put(id="flange", text=_FLANGE)
    out = tmp_path / "flange.step"
    resp = cad.get(id="flange", view="step", args={"path": str(out)})
    assert out.exists()
    assert "ISO-10303" in out.read_text(errors="replace", encoding="utf-8")[:200]
    assert "STEP" in resp.body


def test_derive_creates_new_design_with_lineage(cad, store):
    cad.put(id="flange", text=_FLANGE)
    resp = cad.derive(id="flange", to="flange-v2", text="plate add cyl:r30h10")
    assert "derived from flange" in resp.body
    # the derived design exists and is independent
    tree = cad.get(id="flange-v2")
    assert "plate" in tree.body
    # parent untouched
    assert cad.get(id="flange").body  # still resolves
    # lineage link points child -> parent
    child = store.get_ref(kind="cad", id="flange-v2")
    parent = store.get_ref(kind="cad", id="flange")
    links = store.links_for(child.id, direction="out", relation="derived-from")
    assert any(lnk.dst_ref_id == parent.id for lnk in links)


def test_derive_refuses_existing_slug(cad):
    cad.put(id="flange", text=_FLANGE)
    cad.put(id="taken", text="p add box:w4d4h4")
    with pytest.raises(BadInput):
        cad.derive(id="flange", to="taken", text="p add cyl:r1h1")


# ── sub-assembly instancing (`use <slug> as <name>`) ─────────────────────
_STANDOFF = """
component post
pillar add cyl:r3h20
"""


def test_use_instances_a_stored_design(cad):
    cad.put(id="standoff", text=_STANDOFF)
    resp = cad.put(
        id="deck",
        text=(
            "component base\n"
            "slab add box:w60d60h4\n"
            "use standoff as sw @-20,-20,4\n"
            "use standoff as se @20,-20,4\n"
        ),
    )
    # the head counts the *expanded* bodies, not the compact instance nodes
    assert "3 part(s)" in resp.body
    # the tree keeps the author's compact `use:` nodes
    tree = cad.get(id="deck")
    assert "use:standoff" in tree.body

    # probes see the inlined, namespaced bodies
    pt = cad.get(id="deck", view="point", args={"p": [-20, -20, 14]})
    assert "sw.pillar" in pt.body
    conn = cad.get(id="deck", view="connectivity")
    assert "sw.post" in conn.body and "se.post" in conn.body


def test_instanced_design_exports(cad):
    cad.put(id="standoff", text=_STANDOFF)
    cad.put(
        id="deck",
        text="component base\nslab add box:w60d60h4\nuse standoff as p @0,0,4\n",
    )
    scad = cad.get(id="deck", view="scad")
    # the export is meshable source, never the unresolved `use:` node
    assert "use:" not in scad.body
    assert "cylinder" in scad.body


def test_use_of_missing_design_is_bad_input(cad):
    with pytest.raises(BadInput, match="not found"):
        cad.put(id="deck", text="use nosuch as p\n")


def test_self_instancing_refused(cad):
    # otherwise the resolver hands back this slug's *previous* save and the
    # design quietly contains a frozen copy of itself
    cad.put(id="deck", text="component base\nslab add box:w60d60h4\n")
    with pytest.raises(BadInput, match="itself"):
        cad.put(
            id="deck", text="component base\nslab add box:w60d60h4\nuse deck as d\n"
        )


def test_use_of_retired_design_is_bad_input(cad):
    cad.put(id="standoff", text=_STANDOFF)
    cad.put(id="deck", text="use standoff as p\n")
    cad.delete(id="standoff")
    with pytest.raises(BadInput, match="not found"):
        cad.get(id="deck", view="volume")


# ── ports + mates (assembly by interface) ────────────────────────────────
_MOTOR = """
component body
case  add  box:w42d42h40
port shaft @0,0,40
"""


def test_ports_and_mates_round_trip_through_the_store(cad):
    cad.put(id="motor", text=_MOTOR)
    cad.put(
        id="rig",
        text="port deck @0,0,100\nuse motor as m\nmate m.shaft to deck\n",
    )
    # ports/mates live on refs.meta, so they must survive the save→load trip
    body = cad.get(id="rig").body
    assert "port deck @0,0,100" in body
    assert "mate m.shaft to deck" in body
    # ...and the mate must actually have placed the motor: its case runs from
    # z=60 up to the deck at z=100, so a point inside that span is material.
    hit = cad.get(id="rig", view="point", args={"p": [0, 0, 90]})
    assert "m.case" in hit.body


def test_mated_design_exports_without_ports(cad):
    cad.put(id="motor", text=_MOTOR)
    cad.put(id="rig", text="port deck @0,0,100\nuse motor as m\nmate m.shaft to deck\n")
    scad = cad.get(id="rig", view="scad").body
    # a port is a frame, never geometry — nothing named `port` reaches export
    assert "port" not in scad
    assert "cube" in scad


def test_ports_go_into_the_search_card(cad, store):
    cad.put(id="motor", text=_MOTOR)
    ref = store.get_ref(kind="cad", id="motor")
    with store.pool.connection() as conn:
        (card,) = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref.id,),
        ).fetchone()
    # designs are findable by the interfaces they advertise, not just shapes
    assert "Ports: shaft" in card


def test_over_constrained_mate_is_bad_input(cad):
    cad.put(id="motor", text=_MOTOR)
    with pytest.raises(BadInput, match="both mated and explicitly placed"):
        cad.put(
            id="rig",
            text="port deck @0,0,0\nuse motor as m @1,2,3\nmate m.shaft to deck\n",
        )


def test_mate_to_an_undeclared_port_is_bad_input(cad):
    cad.put(id="motor", text=_MOTOR)
    with pytest.raises(BadInput, match="has no port 'flange'"):
        cad.put(
            id="rig",
            text="port deck @0,0,0\nuse motor as m\nmate m.flange to deck\n",
        )


# ── joints + state + sweep + contains links (slice 3/4 + decision 4) ─────
_MOTOR_J = """
component body
case  add  box:w42d42h40
port shaft @0,0,40
"""

#: Crank on a motor shaft; the crank tip passes a post at some angles only.
_CRANK_RIG = """
component post
pillar add box:w6d6h20 @0,28,0
use motor_j as m
port base @0,0,-40
mate m.shaft to base
component arm
bar add box:w30d6h6 @15,0,3
port hub of:arm
joint arm revolute at:hub limits:-180..180
"""


def test_state_poses_a_probe(cad):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    # at neutral the bar reaches +x: material at (25, 0, 3)
    hit = cad.get(id="crank_rig", view="point", args={"p": [25, 0, 3]})
    assert "contains" in hit.body and "bar" in hit.body
    # posed at 90° that point is empty; the bar reaches +y instead
    posed = cad.get(
        id="crank_rig", view="point", args={"p": [25, 0, 3], "state": {"arm": 90}}
    )
    assert "empty" in posed.body.splitlines()[0]
    posed_y = cad.get(
        id="crank_rig", view="point", args={"p": [0, 25, 3], "state": {"arm": 90}}
    )
    assert "contains" in posed_y.body and "bar" in posed_y.body


def test_out_of_limits_state_is_bad_input(cad):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    with pytest.raises(BadInput, match="outside limits"):
        cad.get(
            id="crank_rig", view="point", args={"p": [0, 0, 0], "state": {"arm": 900}}
        )
    with pytest.raises(BadInput, match="must be a dict"):
        cad.get(id="crank_rig", view="point", args={"p": [0, 0, 0], "state": 5})


def test_sweep_reports_collision_and_envelope(cad):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    resp = cad.get(id="crank_rig", view="sweep", args={"n": 9})
    # the bar sweeps a circle of r 0..30 at z 0..6; the post pillar sits at
    # y=28 in that band, so somewhere near 90° they collide
    assert "colliding pair" in resp.body
    assert "arm" in resp.body and "post" in resp.body
    # swept envelope covers the full ±30 reach of the bar
    assert "swept envelope" in resp.body
    assert "-30" in resp.body


def test_sweep_without_joints_is_bad_input(cad):
    cad.put(id="plain", text="solo add cyl:r5h5")
    with pytest.raises(BadInput, match="declares no joints"):
        cad.get(id="plain", view="sweep")


def test_sweep_unknown_joint_arg(cad):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    with pytest.raises(BadInput, match="no joint 'ghost'"):
        cad.get(id="crank_rig", view="sweep", args={"joint": "ghost"})


def test_contains_links_track_use_lines(cad, store):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="rig_c", text="use motor_j as m @0,0,0\nsolo add cyl:r5h5")
    rig = store.get_ref(kind="cad", id="rig_c")
    sub = store.get_ref(kind="cad", id="motor_j")
    out = store.links_for(rig.id, direction="out", relation="contains")
    assert {lk.dst_ref_id for lk in out} == {sub.id}
    # dropping the use line prunes the link on the next save
    cad.put(id="rig_c", text="solo add cyl:r5h5")
    assert store.links_for(rig.id, direction="out", relation="contains") == []


def test_joints_reach_the_search_card(cad, store):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    ref = store.get_ref(kind="cad", id="crank_rig")
    with store.pool.connection() as conn:
        (card,) = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref.id,),
        ).fetchone()
    assert "Joints: arm (revolute)" in card


def test_jointed_tree_shows_interface_lines(cad):
    cad.put(id="motor_j", text=_MOTOR_J)
    cad.put(id="crank_rig", text=_CRANK_RIG)
    body = cad.get(id="crank_rig").body
    assert "joint arm revolute at:hub limits:-180..180" in body
    assert "port hub of:arm" in body


_GEARED_RIG = """
component post
pillar add box:w6d6h20 @0,28,0
component a1
bar1 add box:w20d6h6 @10,0,3
port h1 of:a1
component a2
bar2 add box:w30d6h6 @15,0,13
port h2 of:a2
joint a1 revolute at:h1 limits:-180..180
joint a2 revolute at:h2 limits:-180..180
gear a1 to a2 ratio:1
"""


def test_sweep_sees_gear_driven_collisions(cad):
    # finding-1 regression: sweeping the DRIVER must also score and
    # envelope the driven joint's parts — bar1 (r20) clears the post at
    # y=28, but the geared bar2 (r30) hits it near 90°.
    cad.put(id="geared_rig", text=_GEARED_RIG)
    resp = cad.get(id="geared_rig", view="sweep", args={"n": 9})
    assert "colliding pair" in resp.body
    assert "a2" in resp.body and "post" in resp.body
    # the driven arm's swept envelope is reported too
    env = resp.body.split("swept envelope", 1)[1]
    assert "a2" in env
