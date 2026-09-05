"""`material <component> <slug>` + `view='mass'` — cited mass/CoM.

The design assigns each component a `material` kind slug; the mass view
joins the material's *sourced* density (canonical kg/m3) against sampled
per-component volume, carries the sampling error into the mass, and lists
unassigned components loudly rather than silently zeroing them.
"""

from __future__ import annotations

import pytest

from precis.cad.scene import SceneError, expand_instances, parse_source, spec_to_source
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler
from precis.handlers.material import MaterialHandler


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


@pytest.fixture
def alu(store):
    m = MaterialHandler(hub=Hub(store=store))
    m.put(id="6061-t6-m", title="Aluminium 6061-T6")
    m.put(id="6061-t6-m", property="density", value=2700, unit="kg/m3")
    return "6061-t6-m"


# ── kernel ───────────────────────────────────────────────────────────


def test_material_lines_round_trip_and_validate():
    spec = parse_source(
        "component frame\nslab add box:w60d40h10\nmaterial frame 6061-t6"
    )
    assert spec.meta["materials"] == {"frame": "6061-t6"}
    assert parse_source(spec_to_source(spec)) == spec
    with pytest.raises(SceneError, match="must name a component"):
        parse_source("component frame\nslab add box:w10d10h2\nmaterial nope alu")
    with pytest.raises(SceneError, match="already has material"):
        parse_source(
            "component frame\nslab add box:w10d10h2\nmaterial frame a\nmaterial frame b"
        )


def test_sub_design_materials_merge_namespaced():
    sub = parse_source("component core\nrod add cyl:r5h50\nmaterial core steel-4140")
    top = parse_source(
        "component frame\nslab add box:w60d40h10\nmaterial frame 6061-t6\n"
        "use shaft as sh @0,0,10"
    )
    ex = expand_instances(top, resolve=lambda s: {"shaft": sub}[s])
    assert ex.meta["materials"] == {"frame": "6061-t6", "sh.core": "steel-4140"}


# ── handler view='mass' ──────────────────────────────────────────────

_PLATE = """
component frame
slab add box:w100d100h10
material frame 6061-t6-m
component lug
tab add box:w20d10h5 @60,0,0
"""


def test_mass_view_is_cited_and_loud_about_unassigned(cad, alu):
    cad.put(id="mass_demo", text=_PLATE)
    body = cad.get(id="mass_demo", view="mass").body
    # 100x100x10 mm of 2700 kg/m3 aluminium ≈ 270 g (sampled volume)
    row = next(ln for ln in body.splitlines() if "frame" in ln and "6061" in ln)
    cells = row.split("\t")
    assert len(cells) >= 5, f"unexpected mass row shape: {row!r}"
    assert float(cells[4]) == pytest.approx(270.0, rel=0.05)
    assert "total:" in body and "CoM" in body
    # the unassigned lug is excluded LOUDLY, never silently zeroed
    assert "excluded" in body and "lug" in body
    # density arrives with its provenance column
    assert "source" in body


def test_mass_view_refuses_without_materials(cad):
    cad.put(id="mass_none", text="plate add box:w10d10h2")
    with pytest.raises(BadInput, match="material assignments"):
        cad.get(id="mass_none", view="mass")


def test_mass_view_refuses_unknown_material_and_missing_density(cad, store):
    cad.put(
        id="mass_bad",
        text="component p\nplate add box:w10d10h2\nmaterial p no-such-mat",
    )
    with pytest.raises(BadInput, match="not found"):
        cad.get(id="mass_bad", view="mass")
    MaterialHandler(hub=Hub(store=store)).put(id="bare-mat", title="no density yet")
    cad.put(
        id="mass_bad2",
        text="component p\nplate add box:w10d10h2\nmaterial p bare-mat",
    )
    with pytest.raises(BadInput, match="no density"):
        cad.get(id="mass_bad2", view="mass")
