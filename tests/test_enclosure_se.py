"""Enclosure SE op: stable CAD bindings, deferred writes, and web dry runs."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.apply import HANDLER_LEVEL_OPS
from precis_se.enclosure.generator import enclosure_box
from precis_se.handler import SeHandler
from precis_se.ops import SeTree
from precis_web.design_turn import _SE_STORE_AWARE_SIGNATURES, dry_run_se

PARAMS: dict[str, Any] = {"x": 80, "y": 60, "z": 30}


@pytest.fixture
def handler(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _op(**overrides: Any) -> dict[str, Any]:
    return {"op": "enclosure_box", "params": {**PARAMS, **overrides}}


def _cad_slugs(store: Store, prefix: str) -> list[str]:
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT i.id_value FROM ref_identifiers i JOIN refs r "
            "ON r.ref_id = i.ref_id WHERE i.id_kind = 'cite_key' "
            "AND r.kind = 'cad' AND r.retired_at IS NULL "
            "AND i.id_value LIKE %s ORDER BY i.id_value",
            (prefix + "-%",),
        ).fetchall()
    return [r[0] for r in rows]


def _write_counts(store: Store) -> tuple[int, int]:
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT (SELECT count(*) FROM refs), (SELECT count(*) FROM chunks)"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_mints_two_bound_blocks_and_replayable_cad(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="enclosure", ops=[_op()])
    ref = store.get_ref(kind="se", id="enclosure")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert set(tree.blocks) == {"box", "lid"}
    assert _cad_slugs(store, "enclosure") == ["enclosure-box", "enclosure-lid"]
    for name, block in tree.blocks.items():
        assert block.bound_kind == "cad"
        assert block.bound == f"enclosure-{name}"
        assert block.local_pose == [0, 0, 0]
        assert block.local_rot == [0, 0, 0]
        cad = store.get_ref(kind="cad", id=block.bound)
        assert cad is not None
        spec, _ = store.cad_load(cad.id)
        result = enclosure_box(**spec.meta["enclosure"])
        assert spec.nodes == result.parts[name].nodes
        assert spec.components == [name]


def test_rerun_updates_existing_designs_without_suffixes(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="enclosure", ops=[_op()])
    before = {
        name: store.get_ref(kind="cad", id=f"enclosure-{name}")
        for name in ("box", "lid")
    }
    handler.edit(id="enclosure", ops=[_op(x=90, boss_count=2, ribs=False)])
    assert _cad_slugs(store, "enclosure") == ["enclosure-box", "enclosure-lid"]
    ref = store.get_ref(kind="se", id="enclosure")
    assert ref is not None
    assert set(persist.load_tree(store, ref.id).blocks) == {"box", "lid"}
    for name, old in before.items():
        updated = store.get_ref(kind="cad", id=f"enclosure-{name}")
        assert old is not None and updated is not None and updated.id == old.id
        spec, _ = store.cad_load(updated.id)
        assert spec.meta["enclosure"]["x"] == 90
        assert spec.meta["enclosure"]["boss_count"] == 2
        prefix = "pilot" if name == "box" else "clearance"
        holes = {n.name for n in spec.nodes if n.name.startswith(prefix + "-")}
        assert holes == {f"{prefix}-1", f"{prefix}-2"}
        assert not any(n.name.startswith("rib-") for n in spec.nodes)


def test_multiple_ops_keep_last_params_and_preserve_other_metadata(
    handler: SeHandler, store: Store
) -> None:
    handler.put(
        id="enclosure",
        text=json.dumps({"description": "sensor housing", "ops": [_op(), _op(x=90)]}),
    )
    for expected_x in (90, 100):
        if expected_x == 100:
            handler.edit(id="enclosure", ops=[_op(x=100)])
        for name in ("box", "lid"):
            cad = store.get_ref(kind="cad", id=f"enclosure-{name}")
            assert cad is not None
            spec, _ = store.cad_load(cad.id)
            assert spec.meta["enclosure"]["x"] == expected_x
    ref = store.get_ref(kind="se", id="enclosure")
    assert ref is not None
    assert ref.meta["description"] == "sensor housing"
    assert _cad_slugs(store, "enclosure") == ["enclosure-box", "enclosure-lid"]


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "enclosure_box"}],
        [_op(bogus=1)],
        [_op(x=-1)],
        [_op(counterbore=True, lid_thickness=3)],
        [{"op": "add_block", "name": "box"}, _op()],
        [_op(), {"op": "set_desc", "block": "missing", "desc": "bad"}],
        [_op(), _op(x=-1)],
    ],
)
def test_refused_put_leaves_no_design_or_cad_writes(
    handler: SeHandler, store: Store, ops: list[dict[str, Any]]
) -> None:
    before = _write_counts(store)
    with pytest.raises(BadInput):
        handler.put(id="refused", ops=ops)
    assert _write_counts(store) == before
    assert store.get_ref(kind="se", id="refused") is None
    assert _cad_slugs(store, "refused") == []


def test_later_invalid_edit_preserves_existing_tree_cad_and_params(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="enclosure", ops=[_op()])
    ref = store.get_ref(kind="se", id="enclosure")
    assert ref is not None
    original_meta = copy.deepcopy(ref.meta)
    original_tree = persist.load_tree(store, ref.id)
    original_cad = {}
    for name in ("box", "lid"):
        cad = store.get_ref(kind="cad", id=f"enclosure-{name}")
        assert cad is not None
        original_cad[name] = (cad.id, store.cad_load(cad.id)[0])
    counts = _write_counts(store)
    with pytest.raises(BadInput):
        handler.edit(
            id="enclosure",
            ops=[_op(x=90), {"op": "set_desc", "block": "missing", "desc": "bad"}],
        )
    assert _write_counts(store) == counts
    current = store.get_ref(kind="se", id="enclosure")
    assert current is not None and current.meta == original_meta
    assert persist.load_tree(store, ref.id) == original_tree
    for cad_id, spec in original_cad.values():
        assert store.cad_load(cad_id)[0] == spec


def test_web_dry_run_validates_without_writing_or_mutating_tree(store: Store) -> None:
    assert "enclosure_box" in HANDLER_LEVEL_OPS
    assert "enclosure_box" in _SE_STORE_AWARE_SIGNATURES
    tree = SeTree()
    before = _write_counts(store)
    assert dry_run_se(store, tree, [_op()], design_slug="preview") is None
    error = dry_run_se(store, tree, [_op(x=-1)], design_slug="preview")
    assert error is not None
    later_error = dry_run_se(
        store,
        tree,
        [_op(), {"op": "set_desc", "block": "missing", "desc": "bad"}],
        design_slug="preview",
    )
    assert later_error is not None and "missing" in later_error
    assert tree == SeTree()
    assert _write_counts(store) == before
    assert _cad_slugs(store, "preview") == []
