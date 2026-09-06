"""Catalog atoms (`part <name> <family>:<code>`) + the realization edge.

Kernel half: parts parse/round-trip, expand from the built-in catalog with
no store resolver, mate by port with zero world coordinates, and refuse
unknown codes at parse with a line number. Handler half: `view='bom'`
rolls quantities up through patterns and nesting to procurable `component`
refs, and `cad_save` syncs `realized-by` links (0156) for catalog parts —
managing only its own rows (links.meta.catalog), never hand-authored ones.
"""

from __future__ import annotations

import pytest

from precis.cad import catalog
from precis.cad.scene import (
    SceneError,
    build_design,
    expand_instances,
    parse_source,
    spec_to_source,
)
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler

# --- kernel ---------------------------------------------------------------


def test_every_catalog_family_resolves_and_parses():
    """Each family's sample code yields a valid, buildable envelope with at
    least one port — the catalog's own sources must never be the thing that
    breaks at expansion time."""
    samples = [
        "bearing:6202",
        "bolt:m6x20",
        "nut:m6",
        "washer:m6",
        "extrusion:2020x400",
        "rail:mgn12x200",
        "nema:17",
        "gear:m1z20",
    ]
    assert sorted({s.split(":")[0] for s in samples}) == sorted(
        catalog.known_families()
    )
    for code in samples:
        info = catalog.resolve_part(code)
        assert info.code == code and info.part_slug and info.designation
        spec = parse_source(info.source)
        assert spec.nodes, code
        assert spec.meta.get("ports"), f"{code} declares no ports"
        build_design(spec)  # buildable geometry


def test_part_round_trips_and_expands_without_resolver():
    src = (
        "component block\n"
        "base add box:w60d40h20\n"
        "port seat @0,0,20 of:block\n"
        "part b1 bearing:6202\n"
        "mate b1.face to seat\n"
    )
    spec = parse_source(src)
    rt = spec_to_source(spec)
    assert "part b1 bearing:6202" in rt
    assert parse_source(rt).nodes == spec.nodes
    ex = expand_instances(spec)  # no resolver: parts-only must not need one
    ring = next(n for n in ex.nodes if n.name == "b1.ring")
    assert ring.config == "cyl:r17.5h11"
    # face port (z=B=11) mated to the seat at z=20 → ring base at 9
    assert ring.loc[2] == pytest.approx(9.0)


def test_part_pattern_multiplies_copies():
    spec = parse_source("part bolts bolt:m6x20 @20,0,0 polar:n4r24\n")
    ex = expand_instances(spec)
    assert sum(1 for n in ex.nodes if n.name.endswith(".shank")) == 4


def test_unknown_code_and_family_refuse_at_parse_with_line_number():
    with pytest.raises(SceneError, match="line 2: unknown bearing code '9999'"):
        parse_source("component a\npart x bearing:9999")
    with pytest.raises(SceneError, match="unknown catalog family 'gizmo'"):
        parse_source("part x gizmo:1")
    with pytest.raises(SceneError, match="expected 'part <name> <family>:<code>"):
        parse_source("part lonely\n")


def test_part_name_collisions_refused():
    with pytest.raises(SceneError, match="duplicate name"):
        parse_source("component b1\nbase add box:w9d9h9\npart b1 nut:m6\n")


def test_mate_onto_missing_part_port_names_the_code():
    src = "component c\nbase add box:w9d9h9\nport p @0,0,9 of:c\npart b1 bearing:608\nmate b1.nope to p\n"
    with pytest.raises(SceneError, match="'bearing:608'.*no port 'nope'"):
        expand_instances(parse_source(src))


# --- handler --------------------------------------------------------------


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


_ASSY = (
    "component plate\n"
    "base add box:w120d80h10\n"
    "part b1 bearing:6202 @0,0,10\n"
    "part bolts bolt:m6x20 @40,0,10 polar:n4r30\n"
)


def test_bom_rolls_up_patterns_nesting_and_component_refs(cad, store):
    comp, _ = store.component_entity_upsert(
        slug="bearing-6202", title="6202 bearing", meta_patch={}
    )
    store.component_value_insert(
        component_ref_id=comp.id, spec_id="unit_cost", value_num=1.5
    )
    cad.put(id="cat_sub", text=_ASSY)
    cad.put(
        id="cat_top",
        text="use cat_sub as left @0,0,0\nuse cat_sub as right @0,200,0\npart m1 nema:17 @200,0,0\n",
    )
    body = cad.get(id="cat_top", view="bom").body
    # 2× the sub-assembly: 2 bearings, 8 bolts, plus the motor
    assert "2\tbearing:6202\t" in body
    assert "8\tbolt:m6x20\t" in body
    assert "1\tnema:17\t" in body
    assert "bearing-6202" in body  # resolved component ref
    assert "unit_cost total: 3 — priced: 1 of 3 part(s)" in body
    assert "⚠ unsourced" in body and "'bolt-m6x20'" in body and "'nema-17'" in body


def test_bom_empty_says_so(cad):
    cad.put(id="cat_plain", text="component a\nbase add box:w9d9h9\n")
    assert "no catalog parts" in cad.get(id="cat_plain", view="bom").body


def _realized_dst_ids(store, ref_id):
    return {
        lk.dst_ref_id
        for lk in store.links_for(ref_id, direction="out", relation="realized-by")
    }


def test_realized_by_sync_adds_and_prunes_only_catalog_links(cad, store):
    comp, _ = store.component_entity_upsert(
        slug="bearing-6202", title="6202 bearing", meta_patch={}
    )
    other, _ = store.component_entity_upsert(
        slug="hand-picked", title="manual candidate", meta_patch={}
    )
    cad.put(id="cat_links", text=_ASSY)
    ref = store.get_ref(kind="cad", id="cat_links")
    assert _realized_dst_ids(store, ref.id) == {comp.id}
    # a hand-authored candidate realization (no meta.catalog) must survive
    store.add_link(src_ref_id=ref.id, dst_ref_id=other.id, relation="realized-by")
    cad.put(id="cat_links", text="component plate\nbase add box:w120d80h10\n")
    assert _realized_dst_ids(store, ref.id) == {other.id}


def test_part_with_no_component_ref_stays_unsourced_without_links(cad, store):
    cad.put(id="cat_unsourced", text="part g1 gear:m2z30\n")
    ref = store.get_ref(kind="cad", id="cat_unsourced")
    assert _realized_dst_ids(store, ref.id) == set()
    body = cad.get(id="cat_unsourced", view="bom").body
    assert "expected component 'gear-m2z30'" in body


def test_bad_part_code_refuses_at_put(cad):
    with pytest.raises(BadInput, match="unknown bolt code"):
        cad.put(id="cat_bad", text="part x bolt:m7x20\n")


def test_manual_realized_by_link_verb(cad, store):
    comp, _ = store.component_entity_upsert(
        slug="alt-bearing", title="alternate sourcing", meta_patch={}
    )
    cad.put(
        id="cat_verb",
        text=(
            "component plate\nbase add box:w120d80h10\n"
            "port seat @0,0,10 of:plate\n"
            "part b1 bearing:6202\nmate b1.face to seat\n"
        ),
    )
    ref = store.get_ref(kind="cad", id="cat_verb")
    body = cad.link(
        id="cat_verb", target="component:alt-bearing", rel="realized-by"
    ).body
    assert "realized-by: cat_verb" in body
    assert comp.id in _realized_dst_ids(store, ref.id)
    # the links view surfaces the edge (and the printed-joint lint walks a
    # design whose mates involve catalog parts without tripping)
    assert "alt-bearing" in cad.get(id="cat_verb", view="links").body
    cad.link(
        id="cat_verb", target="component:alt-bearing", rel="realized-by", mode="remove"
    )
    assert comp.id not in _realized_dst_ids(store, ref.id)
    finding = store.insert_ref(kind="finding", slug=None, title="not a part", meta={})
    with pytest.raises(BadInput, match="must be a component"):
        cad.link(id="cat_verb", target=f"finding:{finding.id}", rel="realized-by")
