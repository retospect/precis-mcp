"""The feature-graph recipe layer (se-flow-kernel slice 4b §Design
representation item 1, recipe half). The ``build``/``Built``/``shape_sha``
half is in ``tests/test_precis_geom_build.py``."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis_geom.design import Recipe, Snapshot
from precis_geom.params import Continuous, Space


def _base() -> tuple[Recipe, str]:
    r = Recipe()
    base = r.add("box", x=0.1, y=0.05, z=0.01)
    return r, base


def test_add_mints_op_prefixed_ids() -> None:
    r, base = _base()
    hole = r.add("drill", parents=(base,), x=0.02, d=0.003)
    assert base == "b1" and hole == "h2"
    assert r.feature(hole).parents == (base,)
    assert r.feature(hole).param_map() == {"x": 0.02, "d": 0.003}


def test_add_refuses_missing_parent() -> None:
    r = Recipe()
    with pytest.raises(BadInput):
        r.add("drill", parents=("nope",))


def test_modify_merges_and_recanonicalises() -> None:
    r, base = _base()
    hole = r.add("drill", parents=(base,), x=0.02, d=0.003)
    r.modify(hole, x=0.04)
    assert r.feature(hole).param_map() == {"x": 0.04, "d": 0.003}
    # params stay sorted by name whatever the modify order
    assert [name for name, _ in r.feature(hole).params] == ["d", "x"]


def test_pinned_refuses_modify_and_remove() -> None:
    r, base = _base()
    pin = r.add("drill", parents=(base,), pinned=True, x=0.02, d=0.003)
    with pytest.raises(BadInput):
        r.modify(pin, x=0.01)
    with pytest.raises(BadInput):
        r.remove(pin)


def test_remove_with_child_refuses_naming_it() -> None:
    r, base = _base()
    child = r.add("drill", parents=(base,), x=0.0, d=0.002)
    with pytest.raises(BadInput) as exc:
        r.remove(base)
    assert child in str(exc.value)


def test_remove_then_restore_round_trips() -> None:
    r, base = _base()
    r.add("drill", parents=(base,), x=0.02, d=0.003)
    snap = r.snapshot()
    before = r.features
    leaf = r.add("drill", parents=(base,), x=0.05, d=0.003)
    r.remove(leaf)
    r.restore(snap)
    assert r.features == before


def test_restore_does_not_free_the_id_counter() -> None:
    r, base = _base()
    snap = r.snapshot()
    r.add("drill", parents=(base,), x=0.02, d=0.003)  # mints h2
    r.restore(snap)
    # The id counter is untouched by restore, so the next add is never h2.
    again = r.add("drill", parents=(base,), x=0.02, d=0.003)
    assert again == "h3"


def test_snapshot_is_detached() -> None:
    r, base = _base()
    snap = r.snapshot()
    r.add("drill", parents=(base,), x=0.02, d=0.003)
    assert snap.features == (r.feature(base),)
    assert len(r.features) == 2


def test_region_stores_selector_and_refuses_empty() -> None:
    r, base = _base()
    r.region("inlet", f"patch:{base}.x0@0,0+0.2x0.1")
    assert dict(r.regions)["inlet"].startswith("patch:")
    with pytest.raises(BadInput):
        r.region("outlet", "  ")


def test_to_json_from_json_round_trips() -> None:
    r, base = _base()
    r.add("drill", parents=(base,), x=0.02, d=0.003, group="holes")
    r.region("inlet", f"patch:{base}.x0@0,0+0.2x0.1")
    doc = r.to_json()
    back = Recipe.from_json(doc)
    assert back.features == r.features
    assert back.regions == r.regions
    # A reload keeps minting past every stored id.
    assert back.add("box") == "b3"


def test_from_json_advances_counter_past_stored_ids() -> None:
    doc = {"features": [{"id": "h9", "op": "drill", "params": {}, "parents": []}]}
    back = Recipe.from_json(doc)
    assert back.add("box") == "b10"


def test_space_apply_drives_modify() -> None:
    r, base = _base()
    hole = r.add("drill", parents=(base,), x=0.02, d=0.003)
    space = Space({"w": Continuous(1e-3, 5e-3)}, bindings={"w": f"{hole}.d"})
    params = space.clip({"w": 4e-3})
    space.apply(r, params)
    assert r.feature(hole).param_map()["d"] == pytest.approx(4e-3)


def test_snapshot_is_a_plain_frozen_copy() -> None:
    r, base = _base()
    snap = r.snapshot()
    assert isinstance(snap, Snapshot)
    assert [f.id for f in snap.features] == [base]


def test_nested_inputs_cannot_change_a_recipe_outside_edits() -> None:
    r = Recipe()
    points = {"xy": [[0.01, 0.02], [0.03, 0.04]]}
    fid = r.add("polygon", points=points)
    points["xy"][0][0] = 9.0
    assert r.feature(fid).param_map()["points"]["xy"][0][0] == 0.01

    replacement = {"xy": [[0.05, 0.06]]}
    r.modify(fid, points=replacement)
    replacement["xy"][0][0] = 9.0
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.05, 0.06]]


def test_feature_reads_cannot_bypass_pinning_with_nested_values() -> None:
    r = Recipe()
    fid = r.add("polygon", pinned=True, points={"xy": [[0.01, 0.02]]})
    feat = r.feature(fid)
    params = feat.param_map()
    params["points"]["xy"][0][0] = 9.0
    assert feat.param_map()["points"]["xy"] == [[0.01, 0.02]]

    dict(feat.params)["points"]["xy"][0][0] = 8.0
    dict(r.features[0].params)["points"]["xy"][0][0] = 7.0
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.01, 0.02]]


def test_nested_snapshot_values_and_restores_are_detached() -> None:
    r = Recipe()
    fid = r.add("polygon", points={"xy": [[0.01, 0.02]]})
    snap = r.snapshot()
    dict(snap.features[0].params)["points"]["xy"][0][0] = 0.03
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.01, 0.02]]

    r.restore(snap)
    dict(snap.features[0].params)["points"]["xy"][0][0] = 0.04
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.03, 0.02]]
    r.modify(fid, points={"xy": [[0.05, 0.06]]})
    r.restore(snap)
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.04, 0.02]]


def test_nested_json_values_are_detached_on_export_and_import() -> None:
    r = Recipe()
    fid = r.add("polygon", points={"xy": [[0.01, 0.02]]})
    doc = r.to_json()
    back = Recipe.from_json(doc)
    doc["features"][0]["params"]["points"]["xy"][0][0] = 9.0
    assert r.feature(fid).param_map()["points"]["xy"] == [[0.01, 0.02]]
    assert back.feature(fid).param_map()["points"]["xy"] == [[0.01, 0.02]]
