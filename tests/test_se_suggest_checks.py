"""gr521590 — ``view='suggest_checks'``: checks derived from the design's
own declarations. ``validate``/``drc`` run fixed rules and every
ray/measurand was hand-authored; the view proposes, read-only, the
``add_ray``/``set_measure`` ops (and transient ``view='ray'`` calls) the
declarations already imply — a ``bolt_axis`` ray per threaded port, a
region/void probe per shaped pocket and bound-cad cut feature, and a
checked same-unit measurand per ``measurand_unchecked`` measure. Nothing
is written; the snippets use exactly the syntax ``add_ray``/
``set_measure`` vet."""

from __future__ import annotations

import ast
import json
import re

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.measures import MeasureError
from precis_se.ops import SeTree, apply_ops
from precis_se.properties.measurand import Measurand
from precis_se.suggest_checks import render_suggest_checks


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _put(handler: SeHandler, slug: str, *ops: dict) -> None:
    handler.put(id=slug, text=json.dumps({"ops": list(ops)}))


def _suggested_ops(body: str) -> list[dict]:
    """Every op dict the rendered ``edit(kind='se', …, ops=[…])`` lines
    carry — parsed the way an agent lifts them to apply."""
    ops: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("edit(kind='se'"):
            continue
        m = re.search(r"ops=(\[.*\])\s*\)$", line)
        assert m is not None, line
        ops.extend(ast.literal_eval(m.group(1)))
    return ops


def test_threaded_port_proposes_a_bolt_axis_ray(handler: SeHandler) -> None:
    """The bracket fixture's bolt rays, generated: ``frame='port:h1'``,
    ``+z`` the port axis, starting a lead above it, ``kind='bolt_axis'``
    — plus the transient ``view='ray'`` args that run it without
    writing."""
    _put(
        handler,
        "d1",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {
            "op": "add_port",
            "block": "base",
            "name": "h1",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": "[15 0 10] mm",
            "annotations": {"thread": "M5x0.8", "depth": 0.008},
        },
    )
    body = handler.get(id="d1", view="suggest_checks").body
    assert "bolt axes" in body and "base.h1" in body and "M5x0.8" in body
    assert "'op': 'add_ray'" in body
    assert "'name': 'bolt-h1'" in body
    assert "'frame': 'port:h1'" in body
    assert "'kind': 'bolt_axis'" in body
    assert "'anchor': ['base.h1']" in body
    # the transient, write-free twin
    assert "view='ray'" in body and "'block': 'base'" in body
    # read-only: a second read neither accumulates nor mutates
    assert handler.get(id="d1", view="suggest_checks").body == body


def test_covered_port_is_not_reproposed(handler: SeHandler) -> None:
    """A port already carrying a ``bolt_axis`` ray on ``port:<name>`` is
    done — the view doesn't suggest a second one."""
    _put(
        handler,
        "d2",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {
            "op": "add_port",
            "block": "base",
            "name": "h1",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": "[15 0 10] mm",
            "annotations": {"thread": "M5x0.8", "depth": 0.008},
        },
        {
            "op": "add_ray",
            "block": "base",
            "name": "bolt-h1",
            "frame": "port:h1",
            "origin": [0, 0, -0.008],
            "direction": [0, 0, 1],
            "length": 0.036,
            "kind": "bolt_axis",
        },
    )
    body = handler.get(id="d2", view="suggest_checks").body
    assert "nothing to suggest" in body


def test_plain_design_reads_honest(handler: SeHandler) -> None:
    _put(handler, "d0", {"op": "add_block", "name": "b"})
    body = handler.get(id="d0", view="suggest_checks").body
    assert "nothing to suggest" in body


def test_unsized_thread_is_skipped_and_named(handler: SeHandler) -> None:
    """A ``thread`` annotation no diameter can be read from can't feed a
    bolt report — skipped, and the skip is named."""
    _put(
        handler,
        "d3",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {
            "op": "add_port",
            "block": "base",
            "name": "h9",
            "direction": [0, 0, -1],
            "pose": "[15 0 10] mm",
            "annotations": {"thread": "custom"},
        },
    )
    body = handler.get(id="d3", view="suggest_checks").body
    assert "base.h9" in body and "skipped" in body


def test_shaped_pocket_proposes_a_region_ray(handler: SeHandler) -> None:
    """A pocket with a cad-DSL shape gets a ``region``-field probe
    through its aabb's longest axis — the same call an agent would
    author by hand, in the pocket's own block frame."""
    _put(
        handler,
        "d4",
        {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"},
        {
            "op": "add_pocket",
            "block": "b",
            "name": "site",
            "shape": "box:w0.02d0.01h0.004",
        },
    )
    body = handler.get(id="d4", view="suggest_checks").body
    assert "pocket b.site" in body
    assert "'op': 'add_ray'" in body and "'name': 'region-site'" in body
    assert "'fields': ['region']" in body
    # longest aabb axis is x: the probe runs along it through the shape
    assert "'direction': [1.0, 0.0, 0.0]" in body


def test_unshaped_pocket_is_skipped_and_named(handler: SeHandler) -> None:
    _put(
        handler,
        "d5",
        {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"},
        {"op": "add_pocket", "block": "b", "name": "vague"},
    )
    body = handler.get(id="d5", view="suggest_checks").body
    assert "b.vague" in body and "unshaped" in body


def test_cut_feature_proposes_a_void_probe(handler: SeHandler, hub: Hub) -> None:
    """A ``Diff`` cutter in a cad-bound block's design gets a probe down
    its own axis — the ray's ``void <block>.<feature>`` event then proves
    the cut landed."""
    from precis.handlers.cad import CadHandler

    CadHandler(hub=hub).put(
        id="plate1",
        text=(
            "component plate\n"
            "plate add box:w40mmd40mmh10mm\n"
            "hole1 cut cyl:r2.75mmh8mm @15mm,0mm,3mm"
        ),
    )
    _put(
        handler,
        "d6",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {"op": "set_binding", "block": "base", "kind": "cad", "design": "plate1"},
    )
    body = handler.get(id="d6", view="suggest_checks").body
    assert "cut base.hole1" in body
    assert "'name': 'cut-hole1'" in body
    # a cyl's axis is +z: the probe runs down it, centred on the cutter
    assert "'direction': [0.0, 0.0, 1.0]" in body
    assert "'block': 'base'" in body


# ── round trips: parse the rendered call, apply it, the view goes quiet ──


def test_applied_bolt_ray_is_not_reproposed(handler: SeHandler) -> None:
    """The bolt round trip: the suggested op through ``apply_ops`` (the
    handler's edit path) covers the port — the view doesn't re-propose
    it under a ``-2`` name."""
    _put(
        handler,
        "r1",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {
            "op": "add_port",
            "block": "base",
            "name": "h1",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": "[15 0 10] mm",
            "annotations": {"thread": "M5x0.8", "depth": 0.008},
        },
    )
    ops = _suggested_ops(handler.get(id="r1", view="suggest_checks").body)
    assert [o["op"] for o in ops] == ["add_ray"]
    handler.edit(id="r1", ops=ops)
    body = handler.get(id="r1", view="suggest_checks").body
    assert "nothing to suggest" in body


def test_applied_pocket_probe_is_not_reproposed(handler: SeHandler) -> None:
    """The pocket round trip, under a hand-authored name — the dedupe is
    the ray's block+origin+direction (within 1e-9), not the generated
    name, so a manually-authored twin probe counts too."""
    _put(
        handler,
        "r2",
        {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"},
        {
            "op": "add_pocket",
            "block": "b",
            "name": "site",
            "shape": "box:w0.02d0.01h0.004",
        },
    )
    ops = _suggested_ops(handler.get(id="r2", view="suggest_checks").body)
    assert [o["op"] for o in ops] == ["add_ray"]
    ops[0]["name"] = "manual-probe"
    handler.edit(id="r2", ops=ops)
    body = handler.get(id="r2", view="suggest_checks").body
    assert "nothing to suggest" in body


def test_applied_cutter_probe_is_not_reproposed(handler: SeHandler, hub: Hub) -> None:
    """The cut-feature round trip: the applied ``void`` probe drops the
    suggestion."""
    from precis.handlers.cad import CadHandler

    CadHandler(hub=hub).put(
        id="plate2",
        text=(
            "component plate\n"
            "plate add box:w40mmd40mmh10mm\n"
            "hole1 cut cyl:r2.75mmh8mm @15mm,0mm,3mm"
        ),
    )
    _put(
        handler,
        "r3",
        {"op": "add_block", "name": "base", "envelope": "box:w0.04d0.04h0.01"},
        {"op": "set_binding", "block": "base", "kind": "cad", "design": "plate2"},
    )
    ops = _suggested_ops(handler.get(id="r3", view="suggest_checks").body)
    assert [o["op"] for o in ops] == ["add_ray"]
    handler.edit(id="r3", ops=ops)
    body = handler.get(id="r3", view="suggest_checks").body
    assert "nothing to suggest" in body


def test_dotted_cutter_label_is_sanitized() -> None:
    """A cutter spliced under an instance prefix is labelled
    ``<inst>.<node>`` — the generated ray name folds the ``.``
    ``add_ray`` reserves."""
    from precis.cad.dsl import build_config
    from precis.cad.graph import Design
    from precis.cad.vec import as_vec3, pose

    design = Design()
    at = pose(as_vec3([0.015, 0.0, 0.003]), as_vec3([0.0, 0.0, 0.0]))
    base = design.prim("plate", build_config("box:w0.04d0.04h0.01"))
    hole = design.prim("s.hole1", build_config("cyl:r0.00275h0.008"), at)
    design.add_component("plate", design.subtract(base, hole))

    tree = SeTree()
    tree.cad_designs = lambda _slug: (design, {})
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "base"},
            {"op": "set_binding", "block": "base", "kind": "cad", "design": "plate3"},
        ],
    )
    body = render_suggest_checks(tree, "r4")
    assert "cut base.s.hole1" in body
    assert "'name': 'cut-s-hole1'" in body
    assert "cut-s.hole1" not in body


def test_long_pocket_shape_is_capped(handler: SeHandler) -> None:
    """A shape string past the cap truncates with an ellipsis — the why
    line is a hint, not a dump."""
    shape = (
        "frustum:n6rb0.01234567890123456rt0.00987654321098765"
        "h0.00543210987654321rd0.00012345678901234"
    )
    assert len(shape) > 80
    _put(
        handler,
        "r5",
        {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"},
        {"op": "add_pocket", "block": "b", "name": "site", "shape": shape},
    )
    body = handler.get(id="r5", view="suggest_checks").body
    assert shape not in body
    assert shape[:80] + "…" in body


# ── measurands: pure tree with a stub resolver, and the seeded store ──

_STUB: dict[str, Measurand] = {
    "pore diameter": Measurand(910, "pore-diameter", "m"),
    "length": Measurand(904, "length", "m"),
    "surface charge density": Measurand(901, "surface-charge-density", "C/m^2"),
    "net partial charge": Measurand(902, "net-partial-charge", "e"),
}


def _stub(ref: object) -> Measurand:
    key = str(ref).replace("-", " ")
    if key not in _STUB:
        raise MeasureError(f"measurand {ref!r}: no taxon matches")
    return _STUB[key]


def _tree(*ops: dict) -> SeTree:
    tree = SeTree()
    tree.measurands = _stub
    apply_ops(tree, list(ops))
    return tree


def test_unchecked_measurand_proposes_a_checked_same_unit_swap() -> None:
    """'pore-diameter' [m] has no computer — 'length' does (the datum
    evaluator); the suggestion is the exact set_measure op."""
    tree = _tree(
        {"op": "add_block", "name": "b"},
        {
            "op": "add_measure",
            "block": "b",
            "name": "pore",
            "measurand": "pore diameter",
            "min": 1e-9,
            "max": 3e-9,
        },
    )
    body = render_suggest_checks(tree, "d")
    assert "b.pore" in body and "'pore-diameter'" in body
    assert "changes what is measured" in body
    assert "'op': 'set_measure'" in body
    assert "'measurand': 'length'" in body


def test_applied_measurand_swap_is_not_reproposed() -> None:
    """The measurand round trip: the suggested ``set_measure`` through
    ``apply_ops`` makes the measure checked — the next read drops it."""
    tree = _tree(
        {"op": "add_block", "name": "b"},
        {
            "op": "add_measure",
            "block": "b",
            "name": "pore",
            "measurand": "pore diameter",
            "min": 1e-9,
            "max": 3e-9,
        },
    )
    ops = _suggested_ops(render_suggest_checks(tree, "d"))
    assert [o["op"] for o in ops] == ["set_measure"]
    apply_ops(tree, ops)
    assert "nothing to suggest" in render_suggest_checks(tree, "d")


def test_unchecked_measurand_with_no_same_unit_candidate_is_honest() -> None:
    """'surface-charge-density' [C/m^2]: nothing registered computes in
    that unit — the line names it and lists what IS checked, rather
    than proposing a unit-breaking swap."""
    tree = _tree(
        {"op": "add_block", "name": "b"},
        {
            "op": "add_measure",
            "block": "b",
            "name": "q",
            "measurand": "surface charge density",
            "min": -1.0,
            "max": -0.5,
        },
    )
    body = render_suggest_checks(tree, "d")
    assert "b.q" in body and "'surface-charge-density'" in body
    assert "no checked measurand" in body and "C/m^2" in body
    assert "set_measure" not in body


@pytest.fixture
def taxonomy(store: Store) -> Store:
    """The measurand taxonomy as prod has it (0174 + 0182 + 0236) —
    same seed as ``test_se_regions``."""
    from tests.test_se_regions import _run_migration

    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0236_se_charge_dipole_si.sql")
    return store


def test_measurand_swap_through_the_store(handler: SeHandler, taxonomy: Store) -> None:
    """End to end: 'contact-angle' [deg] is measurand_unchecked; 'angle'
    is the seeded legacy node the suggestion swaps in."""
    _put(
        handler,
        "d7",
        {"op": "add_block", "name": "b"},
        {
            "op": "add_measure",
            "block": "b",
            "name": "wetting",
            "measurand": "contact angle",
            "min": 30.0,
            "max": 60.0,
        },
    )
    body = handler.get(id="d7", view="suggest_checks").body
    assert "b.wetting" in body and "measurands" in body
    assert "'measurand': 'angle'" in body
