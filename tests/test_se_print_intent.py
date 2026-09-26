"""Print groups with ``intent='model'`` (docs/backlog/structural-solution-
space.md §Slice 4 bridge, round B1; :mod:`precis_se.printgroup`).

A print group is an ancestor block in an fdm mode with a print intent
(``set_mode(block=, mode='fdm/<m>', intent='model')``); members are the
blocks below it. Pinned here: the one-row ``view='fab'`` collapse, the
one-3MF-many-objects export with a component-bound fastener printed as
its catalog stand-in, the one shared build frame, a SIMP member pinning
that frame (search skipped, said so), two SIMP members disagreeing, an
unbound purchase member's ``no_stand_in`` finding, an unknown intent
refused by name, and the regression that a group with no intent behaves
exactly as before (``intent='manufacture'`` is
``tests/test_se_print_manufacture.py``).

Fixtures: the seat-clamp (``tests.test_se_fasten_seatclamp``) re-parented
under an assembly block, and the SIMP cantilever
(``tests.test_se_simp_bridge``). Every design slug is unique per test (the
shared test-DB rule).
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.cad.export import _component_meshes, _scaled_for_export
from precis.cad.scene import part_spec
from precis.dispatch import Hub, _try
from precis.errors import BadInput
from precis.store import Store
from precis.workers import job_types as jt
from precis_se import persist, simp_bridge, simp_job
from precis_se import printgroup as se_printgroup
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops, pinned_down, print_intent
from tests.test_se_fasten_seatclamp import _ensure_fastener_specs
from tests.test_se_print_views import _mint_slug
from tests.test_se_simp_bridge import _job_params, _simp_op

# Print-intent resolution drives the same export kernel as
# test_se_print_manufacture — 33-37s per test in the 2026-09-26 gate profile.
# See the `slow` marker in tests/conftest.py.
pytestmark = pytest.mark.slow

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    _ensure_fastener_specs(store)
    h = _try(SeHandler, hub=hub)
    assert h is not None
    return h


@pytest.fixture
def register_se_simp() -> Any:
    """Inject the ``se_simp`` job_type into the registry (no entry-point
    discovery at test time — ``tests.test_se_simp_bridge``'s pattern)."""
    jt._REGISTRY[simp_bridge.JOB_TYPE] = simp_job.SPEC
    yield
    jt._REGISTRY.pop(simp_bridge.JOB_TYPE, None)


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _clamp_group_ops(screw_slug: str, *, intent: str | None = "model") -> list[dict]:
    """The seat clamp under an ``assy`` group root: ``clamp`` (fdm) and
    ``bolt`` (purchase, component-bound) are members; ``rail`` stays
    outside the group so the two-member acceptance case is exact."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "assy"},
        {
            "op": "add_block",
            "name": "rail",
            "envelope": "box:w0.03d0.02h0.004",
            "pose": [0, 0, 0.004],
        },
        {"op": "set_mode", "block": "rail", "mode": "fdm/asa"},
        {
            "op": "add_block",
            "name": "clamp",
            "parent": "assy",
            "envelope": "box:w0.03d0.02h0.012",
            "pose": [0, 0, 0.008],
        },
        {"op": "set_mode", "block": "clamp", "mode": "fdm/asa"},
        {"op": "add_block", "name": "bolt", "parent": "assy", "pose": [0, 0, 0.004]},
        {"op": "set_mode", "block": "bolt", "mode": "purchase"},
        {
            "op": "set_binding",
            "block": "bolt",
            "kind": "component",
            "design": screw_slug,
        },
        {"op": "add_port", "block": "bolt", "name": "thread"},
        {"op": "add_port", "block": "clamp", "name": "boss"},
        {
            "op": "connect",
            "a": "bolt.thread",
            "b": "clamp.boss",
            "joint": {
                "class": "rigid",
                "mechanism": "screw",
                "params": {"thread_strategy": "nut-trap"},
            },
        },
    ]
    mode_op: dict[str, Any] = {"op": "set_mode", "block": "assy", "mode": "fdm/pla"}
    if intent is not None:
        mode_op["intent"] = intent
    ops.append(mode_op)
    return ops


def _3mf_objects(path: Path) -> dict[str, np.ndarray]:
    """``{object name: (N, 3) vertices}`` read back from a 3MF package."""
    with zipfile.ZipFile(path) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    out: dict[str, np.ndarray] = {}
    for m in re.finditer(r'<object id="\d+" name="([^"]+)"[^>]*>(.*?)</object>', model):
        verts = re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"/>', m.group(2))
        out[m.group(1)] = np.array(verts, dtype=float)
    return out


def _extents(verts: np.ndarray) -> np.ndarray:
    return verts.max(axis=0) - verts.min(axis=0)


# ---------------------------------------------------------------------------
# the op: where intent lives, what it refuses
# ---------------------------------------------------------------------------


def test_intent_lives_on_the_build_frame_record_and_survives_pin_ops() -> None:
    tree = SeTree()
    apply_ops(
        tree, [{"op": "add_block", "name": "g", "envelope": "box:w0.01d0.01h0.01"}]
    )
    apply_ops(
        tree, [{"op": "set_mode", "block": "g", "mode": "fdm/pla", "intent": "model"}]
    )
    g = tree.blocks["g"]
    assert g.mode == "fdm/pla" and print_intent(g) == "model"
    assert g.build_frame == {"intent": "model"} and pinned_down(g) is None
    # a pin shares the record; clearing the pin keeps the intent
    apply_ops(tree, [{"op": "set_build_frame", "block": "g", "down": [0, 0, -1]}])
    assert pinned_down(g) == [0.0, 0.0, -1.0] and print_intent(g) == "model"
    apply_ops(tree, [{"op": "clear_build_frame", "block": "g"}])
    assert pinned_down(g) is None and g.build_frame == {"intent": "model"}
    # an intent-only record is not a pin, so there is nothing to clear
    with pytest.raises(ValueError, match="no pinned build frame"):
        apply_ops(tree, [{"op": "clear_build_frame", "block": "g"}])
    # set_mode without the key leaves the intent alone; intent=null clears
    apply_ops(tree, [{"op": "set_mode", "block": "g", "mode": "fdm/asa"}])
    assert print_intent(g) == "model"
    apply_ops(
        tree, [{"op": "set_mode", "block": "g", "mode": "fdm/asa", "intent": None}]
    )
    assert print_intent(g) is None and g.build_frame is None
    # no mode, no group: clearing the mode clears the intent too
    apply_ops(
        tree, [{"op": "set_mode", "block": "g", "mode": "fdm/pla", "intent": "model"}]
    )
    apply_ops(tree, [{"op": "set_mode", "block": "g", "mode": None}])
    assert g.mode is None and g.build_frame is None


def test_both_intents_are_accepted_and_the_rest_refused_by_name() -> None:
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "g"}])
    # round B2 built manufacture: the enum and the built set now agree
    apply_ops(
        tree,
        [{"op": "set_mode", "block": "g", "mode": "fdm/pla", "intent": "manufacture"}],
    )
    assert print_intent(tree.blocks["g"]) == "manufacture"
    apply_ops(tree, [{"op": "set_mode", "block": "g", "mode": None}])
    with pytest.raises(ValueError, match="unknown intent"):
        apply_ops(
            tree, [{"op": "set_mode", "block": "g", "mode": "fdm/pla", "intent": "toy"}]
        )
    with pytest.raises(ValueError, match="needs an fdm-family mode"):
        apply_ops(
            tree,
            [{"op": "set_mode", "block": "g", "mode": "purchase", "intent": "model"}],
        )
    assert tree.blocks["g"].mode is None and tree.blocks["g"].build_frame is None


def test_unknown_intent_refusal_reaches_the_handler(handler: SeHandler) -> None:
    handler.put(
        id="pi-manu", text=json.dumps({"ops": [{"op": "add_block", "name": "g"}]})
    )
    with pytest.raises(BadInput, match="unknown intent"):
        handler.edit(
            id="pi-manu",
            ops=[{"op": "set_mode", "block": "g", "mode": "fdm/pla", "intent": "toy"}],
        )


# ---------------------------------------------------------------------------
# the acceptance case: fdm block + component-bound fastener, intent model
# ---------------------------------------------------------------------------


def test_model_group_exports_one_3mf_with_the_fastener_stand_in_as_its_own_object(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(id="pi-model", text=json.dumps({"ops": _clamp_group_ops(screw_slug)}))
    handler.edit(
        id="pi-model", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    tree = _load(handler, "pi-model")
    assert se_printgroup.group_roots(tree) == ["assy"]
    assert se_printgroup.descendants(tree, "assy") == ["bolt", "clamp"]
    assert se_printgroup.grouped_blocks(tree) == {
        "assy": "assy",
        "bolt": "assy",
        "clamp": "assy",
    }

    report = se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store)
    assert report is not None and report.intent == "model"
    roles = {m.block: m.role for m in report.members}
    assert roles == {"bolt": se_printgroup.STAND_IN, "clamp": se_printgroup.PRINTED}
    bolt = next(m for m in report.members if m.block == "bolt")
    assert (
        bolt.stand_in is not None and "catalog part 'csk:m4x12'" in bolt.stand_in.source
    )
    # the mating hole in the printed member keeps its stamped compensation
    clamp = next(m for m in report.members if m.block == "clamp")
    assert clamp.printed is not None and clamp.printed.features
    # one frame for the whole group, chosen on the union, no pin
    assert report.chosen_down is not None and not report.pinned
    assert report.search_skipped is None and report.candidates
    assert not any(f.rule == "no_stand_in" for f in report.findings)

    # view='fab' collapses the group to ONE row naming intent/members/stand-ins
    fab = handler.get(id="pi-model", view="fab").body
    assert len(re.findall(r"^assy\t", fab, flags=re.M)) == 1
    assert not re.search(r"^(clamp|bolt)\t", fab, flags=re.M)
    assert "print group (intent model)" in fab
    assert "2 member(s), 1 stand-in(s)" in fab
    assert "view='print' args={'block': 'assy', 'fmt': '3mf'}" in fab
    assert re.search(r"^rail\t", fab, flags=re.M)  # outside the group: its own row

    # view='print' on the group: one frame line + per-member findings
    body = handler.get(id="pi-model", view="print", args={"block": "assy"}).body
    assert body.count("group build frame") == 1
    assert "print group, intent model" in body
    assert "- bolt: stand-in — catalog part 'csk:m4x12'" in body
    assert "- clamp: printed" in body
    summary = handler.get(id="pi-model", view="print").body
    assert "## assy — print group" in summary
    assert "## clamp" not in summary and "## rail — mode fdm/asa" in summary

    # pin the group frame to identity so the exported extents compare exactly
    handler.edit(
        id="pi-model",
        ops=[{"op": "set_build_frame", "block": "assy", "down": [0, 0, -1]}],
    )
    out = tmp_path / "assy.3mf"
    resp = handler.get(
        id="pi-model",
        view="print",
        args={"block": "assy", "fmt": "3mf", "path": str(out)},
    )
    assert out.exists() and str(out) in resp.body
    assert "2 object(s): bolt, clamp" in resp.body
    objects = _3mf_objects(out)
    assert set(objects) == {"bolt", "clamp"}
    (_name, cat_verts, _tris), *rest = _component_meshes(
        _scaled_for_export(part_spec("csk:m4x12"))
    )
    assert not rest
    assert np.allclose(_extents(objects["bolt"]), _extents(cat_verts), atol=1e-3)
    # world pose survives: the bolt sits at z=4 mm in the block tree, the
    # clamp at z=8 mm, and the group shares ONE bed offset — so the bolt's
    # lowest point is 4 mm BELOW the clamp's; nobody dropped either to the
    # bed on its own
    assert objects["clamp"][:, 2].min() - objects["bolt"][:, 2].min() == pytest.approx(
        4.0, abs=1e-3
    )
    assert objects["bolt"][:, 2].min() == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(BadInput, match="fmt='3mf' only"):
        handler.get(id="pi-model", view="print", args={"block": "assy", "fmt": "stl"})


def test_unbound_purchase_member_is_a_no_stand_in_finding(
    handler: SeHandler, hub: Hub
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    ops = _clamp_group_ops(screw_slug) + [
        {"op": "add_block", "name": "nut", "parent": "assy", "pose": [0, 0, 0.02]},
        {"op": "set_mode", "block": "nut", "mode": "purchase"},
    ]
    handler.put(id="pi-nostandin", text=json.dumps({"ops": ops}))
    handler.edit(
        id="pi-nostandin", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    tree = _load(handler, "pi-nostandin")
    report = se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store)
    assert report is not None
    finding = next(f for f in report.findings if f.rule == "no_stand_in")
    assert finding.subject == "nut" and finding.severity == "warn"
    assert "not bound to a component" in finding.detail
    assert "set_binding(block='nut', kind='component'" in (finding.suggested_fix or "")
    assert [m.block for m in report.exportable] == ["bolt", "clamp"]
    fab = handler.get(id="pi-nostandin", view="fab").body
    assert re.search(r"3 member\(s\), 1 stand-in\(s\), \d+ finding\(s\)", fab), fab


# ---------------------------------------------------------------------------
# a SIMP member pins the group frame
# ---------------------------------------------------------------------------


def _simp_group_ops(beams: list[str]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "add_block", "name": "frame"}]
    for i, name in enumerate(beams):
        ops += [
            {
                "op": "add_block",
                "name": name,
                "parent": "frame",
                "envelope": "box:w0.024d0.012h0.012",
                "pose": [0, 0.03 * i, 0],
            },
            {
                "op": "set_load",
                "block": name,
                "force": [0.0, 0.0, -10.0],
                "fixed": True,
            },
        ]
    ops.append(
        {"op": "set_mode", "block": "frame", "mode": "fdm/pla", "intent": "model"}
    )
    return ops


def _solve(
    handler: SeHandler, store: Store, slug: str, block: str, build_dir: str
) -> None:
    handler.edit(id=slug, ops=[_simp_op(block=block, build_dir=build_dir)])
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    params = next(p for p in _job_params(store, ref.id) if p["block"] == block)
    simp_bridge.run_simp(store, ref.id, simp_bridge.SimpRequest.from_params(params))


def test_a_simp_member_pins_the_group_frame_and_the_search_is_skipped(
    handler: SeHandler, store: Store, register_se_simp: Any
) -> None:
    handler.put(id="pi-simp", text=json.dumps({"ops": _simp_group_ops(["beam"])}))
    _solve(handler, store, "pi-simp", "beam", "z+")
    tree = _load(handler, "pi-simp")
    assert tree.blocks["beam"].build_frame is not None
    assert tree.blocks["beam"].build_frame["origin"] == "simp"
    assert print_intent(tree.blocks["frame"]) == "model"  # the root's intent survived
    report = se_printgroup.report_for(tree, "frame", cad_store_reader=store)
    assert report is not None
    assert report.candidates == [] and not report.pinned
    assert report.search_skipped is not None
    assert (
        "member 'beam' was SIMP-realized with build_dir 'z+'" in report.search_skipped
    )
    assert report.chosen_down is not None
    assert np.allclose(report.chosen_down, [0.0, 0.0, -1.0])
    assert not any(f.rule == "simp_frame_conflict" for f in report.findings)
    body = handler.get(id="pi-simp", view="print", args={"block": "frame"}).body
    assert "group build frame (simp)" in body
    assert "orientation search skipped" in body


def test_two_simp_members_disagreeing_is_a_finding_not_silence(
    handler: SeHandler, store: Store, register_se_simp: Any
) -> None:
    handler.put(id="pi-simp2", text=json.dumps({"ops": _simp_group_ops(["a", "b"])}))
    _solve(handler, store, "pi-simp2", "a", "z+")
    _solve(handler, store, "pi-simp2", "b", "x+")
    tree = _load(handler, "pi-simp2")
    report = se_printgroup.report_for(tree, "frame", cad_store_reader=store)
    assert report is not None
    conflict = next(f for f in report.findings if f.rule == "simp_frame_conflict")
    assert conflict.severity == "error" and conflict.subject == "frame"
    assert "'a' (z+" in conflict.detail and "'b' (x+" in conflict.detail
    # the first by name wins, and the report says whom it followed
    assert report.search_skipped is not None and "member 'a'" in report.search_skipped
    assert report.chosen_down is not None
    assert np.allclose(report.chosen_down, [0.0, 0.0, -1.0])


# ---------------------------------------------------------------------------
# regression: no intent, no group
# ---------------------------------------------------------------------------


def test_an_fdm_ancestor_without_intent_behaves_exactly_as_before(
    handler: SeHandler, hub: Hub
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(
        id="pi-nointent",
        text=json.dumps({"ops": _clamp_group_ops(screw_slug, intent=None)}),
    )
    handler.edit(
        id="pi-nointent", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    tree = _load(handler, "pi-nointent")
    assert se_printgroup.group_roots(tree) == []
    assert (
        se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store) is None
    )
    fab = handler.get(id="pi-nointent", view="fab").body
    assert "print group" not in fab
    for name in ("assy", "clamp", "bolt", "rail"):
        assert re.search(rf"^{name}\t", fab, flags=re.M), name
    summary = handler.get(id="pi-nointent", view="print").body
    assert "## clamp — mode fdm/asa" in summary and "## assy — mode fdm/pla" in summary
    assert "print group" not in summary
    body = handler.get(id="pi-nointent", view="print", args={"block": "assy"}).body
    assert "unrealized" in body


# ---------------------------------------------------------------------------
# nesting: a print group ends where the next group root begins
# ---------------------------------------------------------------------------


def _nested_ops() -> list[dict[str, Any]]:
    """``hub`` ⊃ ``arm`` ⊃ ``sub`` ⊃ ``leaf`` with ``hub`` and ``sub`` both
    intent roots and ``arm`` a plain fdm member of ``hub``."""
    return [
        {"op": "add_block", "name": "hub", "envelope": "cyl:r0.02h0.01"},
        {"op": "set_mode", "block": "hub", "mode": "fdm/pla", "intent": "model"},
        {
            "op": "add_block",
            "name": "arm",
            "parent": "hub",
            "envelope": "box:w0.03d0.004h0.004",
            "pose": [0.03, 0, 0],
        },
        {"op": "set_mode", "block": "arm", "mode": "fdm/pla"},
        {
            "op": "add_block",
            "name": "sub",
            "parent": "arm",
            "envelope": "box:w0.01d0.01h0.004",
            "pose": [0.06, 0, 0],
        },
        {"op": "set_mode", "block": "sub", "mode": "fdm/pla", "intent": "model"},
        {
            "op": "add_block",
            "name": "leaf",
            "parent": "sub",
            "envelope": "box:w0.004d0.004h0.01",
            "pose": [0.06, 0, 0.004],
        },
        {"op": "set_mode", "block": "leaf", "mode": "fdm/pla"},
    ]


def test_a_nested_group_root_owns_its_subtree(
    handler: SeHandler, tmp_path: Path
) -> None:
    handler.put(id="pi-nested", text=json.dumps({"ops": _nested_ops()}))
    for block in ("hub", "arm", "sub", "leaf"):
        handler.edit(
            id="pi-nested", ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}]
        )
    tree = _load(handler, "pi-nested")
    assert se_printgroup.group_roots(tree) == ["hub", "sub"]
    assert se_printgroup.descendants(tree, "hub") == ["arm"]
    assert se_printgroup.nested_roots(tree, "hub") == ["sub"]
    assert se_printgroup.descendants(tree, "sub") == ["leaf"]
    # every block maps to its NEAREST root — arm (not itself a root) to hub,
    # sub to itself, leaf to sub, never to the outer hub
    assert se_printgroup.grouped_blocks(tree) == {
        "hub": "hub",
        "arm": "hub",
        "sub": "sub",
        "leaf": "sub",
    }

    hub = se_printgroup.report_for(tree, "hub", cad_store_reader=handler.store)
    assert hub is not None
    assert {m.block: m.role for m in hub.members} == {
        "hub": se_printgroup.PRINTED,
        "arm": se_printgroup.PRINTED,
        "sub": se_printgroup.NESTED,
    }
    assert [m.block for m in hub.exportable] == ["hub", "arm"]
    assert hub.member_count == 2
    body = handler.get(id="pi-nested", view="print", args={"block": "hub"}).body
    assert "nested group 'sub' — printed separately, see its own row" in body
    assert "nested groups 1" in body
    assert "- leaf" not in body

    # fab: one row per root, nested included; no member rows
    fab = handler.get(id="pi-nested", view="fab").body
    assert len(re.findall(r"^hub\t", fab, flags=re.M)) == 1
    assert len(re.findall(r"^sub\t", fab, flags=re.M)) == 1
    assert not re.search(r"^(arm|leaf)\t", fab, flags=re.M)
    # print summary: one section per root, each covering only its own members
    summary = handler.get(id="pi-nested", view="print").body
    assert summary.count("## hub — print group") == 1
    assert summary.count("## sub — print group") == 1
    assert summary.count("- leaf: printed") == 1
    assert summary.count("- arm: printed") == 1

    # the 3MFs: hub's excludes sub + leaf, sub's has leaf
    hub_out = tmp_path / "hub.3mf"
    handler.get(
        id="pi-nested",
        view="print",
        args={"block": "hub", "fmt": "3mf", "path": str(hub_out)},
    )
    assert set(_3mf_objects(hub_out)) == {"hub", "arm"}
    sub_out = tmp_path / "sub.3mf"
    handler.get(
        id="pi-nested",
        view="print",
        args={"block": "sub", "fmt": "3mf", "path": str(sub_out)},
    )
    assert set(_3mf_objects(sub_out)) == {"sub", "leaf"}


# ---------------------------------------------------------------------------
# stand-in frame: a proud head is shifted, a countersunk one is not
# ---------------------------------------------------------------------------


def _stand_in_z(handler: SeHandler, slug: str) -> tuple[float, float]:
    """``(min z, max z)`` in mm of the bolt member's stand-in solid in the
    BLOCK frame (before any pose)."""
    tree = _load(handler, slug)
    report = se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store)
    assert report is not None
    bolt = next(m for m in report.members if m.block == "bolt")
    assert bolt.stand_in is not None
    (_n, verts, _t), *rest = _component_meshes(_scaled_for_export(bolt.stand_in.spec))
    assert not rest
    return float(verts[:, 2].min()), float(verts[:, 2].max())


def test_proud_head_stand_in_is_shifted_to_the_se_head_top_frame(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    cap_slug = _mint_slug(hub, "iso-4762", "M4x12")  # socket cap: head proud
    handler.put(id="pi-cap", text=json.dumps({"ops": _clamp_group_ops(cap_slug)}))
    handler.edit(
        id="pi-cap", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    tree = _load(handler, "pi-cap")
    specs = tree.blocks["bolt"].derived.specs
    head_h_mm = float(specs["head_height"]) * 1000.0
    assert head_h_mm > 0.0
    # the catalog draws the cap head at -head_height..0; the stand-in shifts
    # it so the head TOP sits at the block origin and the thread runs +z
    (_n, cat_verts, _t), *_ = _component_meshes(
        _scaled_for_export(part_spec("screw:m4x12"))
    )
    assert cat_verts[:, 2].min() == pytest.approx(-head_h_mm, abs=1e-6)
    lo, hi = _stand_in_z(handler, "pi-cap")
    assert lo == pytest.approx(0.0, abs=1e-6)
    assert hi == pytest.approx(cat_verts[:, 2].max() + head_h_mm, abs=1e-6)
    # and the exported object keeps that: the bolt's head top is at the
    # block pose (z = 4 mm), 4 mm below the clamp's bottom, cap or not
    handler.edit(
        id="pi-cap",
        ops=[{"op": "set_build_frame", "block": "assy", "down": [0, 0, -1]}],
    )
    out = tmp_path / "cap.3mf"
    handler.get(
        id="pi-cap",
        view="print",
        args={"block": "assy", "fmt": "3mf", "path": str(out)},
    )
    objects = _3mf_objects(out)
    assert objects["clamp"][:, 2].min() - objects["bolt"][:, 2].min() == pytest.approx(
        4.0, abs=1e-3
    )
    assert _extents(objects["bolt"])[2] == pytest.approx(12.0 + head_h_mm, abs=1e-3)

    # the countersunk case is unshifted: the catalog's flush face IS the top
    csk_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(id="pi-csk", text=json.dumps({"ops": _clamp_group_ops(csk_slug)}))
    lo, hi = _stand_in_z(handler, "pi-csk")
    (_n, csk_verts, _t), *_ = _component_meshes(
        _scaled_for_export(part_spec("csk:m4x12"))
    )
    assert csk_verts[:, 2].min() == pytest.approx(0.0, abs=1e-6)
    assert lo == pytest.approx(0.0, abs=1e-6)
    assert hi == pytest.approx(csk_verts[:, 2].max(), abs=1e-6)
