"""CadHandler end-to-end against a live store (ADR 0041 §11, §12).

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
    assert "ISO-10303" in out.read_text(errors="replace")[:200]
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
