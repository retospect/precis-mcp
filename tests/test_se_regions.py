"""se region property layer, slice A (docs/backlog/se-region-property-layer.md):
taxonomy measurands on measures, the region selectors (``patch:``/``ring:``/
``sites:``/``atoms:``), pockets, the ``measurand_unchecked`` advisory, and the
ops-export round trip.

Pure tests run on bare :class:`~precis_se.ops.SeTree`s with a stub measurand
resolver; the store tests seed the taxonomy (0174 + 0182) into the test DB —
``refs`` is truncated between tests, so the seed fixture re-runs both files.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.reading.concepts import normalize_name
from precis.store import Store
from precis.taxonomy.nodes import slugify, taxon_card_text, validate_taxon_meta
from precis_se import persist
from precis_se.datums import (
    VOCABULARY,
    Selector,
    evaluate_measure,
    parse_selector,
    resolve,
    same_region,
)
from precis_se.drc import drc
from precis_se.handler import SeHandler, _render_pockets
from precis_se.measures import MeasureError, stackup
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.ops_export import NOT_CARRIED, design_ops
from precis_se.persist import tree_to_json
from precis_se.properties import COMPUTERS, is_checked
from precis_se.properties.measurand import Measurand, measurand_from_meta

# ── stub resolver for the pure tests ────────────────────────────────────────

_STUB: dict[str, Measurand] = {
    "surface charge density": Measurand(901, "surface-charge-density", "C/m^2"),
    "net partial charge": Measurand(902, "net-partial-charge", "e"),
    "hydrophobicity index": Measurand(
        903,
        "hydrophobicity-index",
        "",
        categorical=True,
        allowed_values=("hydrophilic", "amphiphilic", "hydrophobic"),
    ),
    "length": Measurand(904, "length", "m"),
    "contact angle": Measurand(905, "contact-angle", "deg"),
}


def _stub(ref: Any) -> Measurand:
    key = str(ref).replace("-", " ")
    if key not in _STUB:
        raise MeasureError(f"measurand {ref!r}: no taxon matches")
    return _STUB[key]


def _tree(*ops: dict[str, Any], resolver: bool = True) -> SeTree:
    tree = SeTree()
    if resolver:
        tree.measurands = _stub
    apply_ops(tree, list(ops))
    return tree


_BOX = {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"}
_PIN = {"op": "add_block", "name": "pin", "envelope": "cyl:r0.01h0.02"}
_PATCH = "patch:b.top@0.02,-0.01+0.01x0.01"


# ── selector grammar: every new form, good and malformed ────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (
            "patch:b.top@0.02,-0.01+0.01x0.005",
            Selector(
                kind="patch", instance="b", tag="top", u=0.02, v=-0.01, w=0.01, h=0.005
            ),
        ),
        (
            "patch:b.side0@1e-9,-2.5E-10+4e-10x1e+0",
            Selector(
                kind="patch",
                instance="b",
                tag="side0",
                u=1e-9,
                v=-2.5e-10,
                w=4e-10,
                h=1.0,
            ),
        ),
        ("ring:pin.top", Selector(kind="ring", instance="pin", tag="top")),
        (
            "sites:cnt/seamA/s3..s7",
            Selector(kind="sites", instance="cnt", seam="seamA", lo=3, hi=7),
        ),
        (
            "atoms:cnt[9,0,3,5-7,5]",
            Selector(kind="atoms", instance="cnt", indices=(0, 3, 5, 6, 7, 9)),
        ),
    ],
)
def test_new_selector_forms_parse_and_round_trip(text: str, want: Selector) -> None:
    from precis_se.datums import _selector_text

    got = parse_selector(text)
    assert got == want
    # the canonical text parses back to the same selector
    assert parse_selector(_selector_text(got)) == got


@pytest.mark.parametrize(
    "text",
    [
        "patch:b.top",  # no rectangle
        "patch:top@0,0+1x1",  # no instance
        "patch:b.top@0,0+1",  # no height
        "patch:b.top@0;0+1x1",  # bad separator
        "patch:b.top@0,0+0x1",  # zero extent
        "patch:b.top@0,0+-1x1",  # negative extent
        "patch:b.top@a,0+1x1",  # not a number
        "ring:top",  # no instance
        "ring:b.top@0,0+1x1",  # a ring takes no rectangle
        "sites:cnt/s0..s3",  # no seam
        "sites:cnt/seam/s3",  # no range
        "sites:cnt/seam/s5..s3",  # backwards
        "sites:cnt/seam/3..5",  # no s prefix
        "atoms:cnt",  # no brackets
        "atoms:cnt[]",  # empty
        "atoms:cnt[1,,2]",  # empty item
        "atoms:cnt[a]",  # not an ordinal
        "atoms:cnt[5-3]",  # backwards
        "atoms:[1]",  # no block
        "atoms:cnt[0-200000]",  # past the expansion cap
    ],
)
def test_malformed_new_selectors_are_refused(text: str) -> None:
    with pytest.raises(MeasureError):
        parse_selector(text)


def test_shape_errors_name_the_whole_vocabulary() -> None:
    for text in ("patch:b.top", "sites:x", "atoms:x", "nope", "zap:x"):
        with pytest.raises(MeasureError) as exc:
            parse_selector(text)
        assert VOCABULARY in str(exc.value), text
    for form in ("patch:", "ring:", "sites:", "atoms:"):
        assert form in VOCABULARY


def test_same_region_compares_parsed_selectors() -> None:
    assert same_region("patch:b.top@0,0+1e-3x1e-3", "patch:b.top@0.0,0.000+0.001x0.001")
    assert not same_region("patch:b.top@0,0+1e-3x1e-3", "patch:b.top@0,0+2e-3x1e-3")
    assert same_region("ring:b.top", " ring:b.top ")
    assert not same_region("ring:b.top", None)


# ── resolution on a cad envelope ─────────────────────────────────────────────


def test_patch_resolves_to_its_centre_on_the_face_plane() -> None:
    tree = _tree(_BOX)
    top = resolve(tree, tree.blocks["b"], _PATCH)
    assert top.error is None and top.kind == "patch"
    # top face centre is (0, 0, h); u = +x, v = n × u = +y
    np.testing.assert_allclose(top.point, [0.02, -0.01, 0.01], atol=1e-12)
    np.testing.assert_allclose(top.normal, [0, 0, 1], atol=1e-12)
    # a side face ⊥ y: u = +x still, v = (-y) × x = +z
    side = resolve(tree, tree.blocks["b"], "patch:b.side0@0.01,0.002+0.01x0.004")
    np.testing.assert_allclose(side.point, [0.01, -0.025, 0.007], atol=1e-12)
    np.testing.assert_allclose(side.normal, [0, -1, 0], atol=1e-12)
    # a side face ⊥ x: u falls back to +y
    east = resolve(tree, tree.blocks["b"], "patch:b.side1@0.01,0+0.001x0.001")
    np.testing.assert_allclose(east.point, [0.05, 0.01, 0.005], atol=1e-12)


def test_patch_moves_with_the_block_pose() -> None:
    tree = _tree({**_BOX, "pose": [1.0, 2.0, 3.0], "rot": [0.0, 0.0, float(np.pi / 2)]})
    r = resolve(tree, tree.blocks["b"], _PATCH)
    # Rz(90°) takes (0.02, -0.01, 0.01) to (0.01, 0.02, 0.01)
    np.testing.assert_allclose(r.point, [1.01, 2.02, 3.01], atol=1e-12)
    np.testing.assert_allclose(r.normal, [0, 0, 1], atol=1e-12)


def test_patch_off_its_face_or_on_a_missing_face_is_a_lenient_miss() -> None:
    tree = _tree(_BOX)
    off = resolve(tree, tree.blocks["b"], "patch:b.top@0.2,0+0.01x0.01")
    assert off.error is not None and "lies off face b.top" in off.error
    gone = resolve(tree, tree.blocks["b"], "patch:b.nope@0,0+1x1")
    assert gone.error is not None and "no face tagged 'nope'" in gone.error


def test_ring_resolves_to_the_loop_centre_and_axis() -> None:
    tree = _tree(_BOX, _PIN)
    rim = resolve(tree, tree.blocks["b"], "ring:pin.top")
    assert rim.error is None and rim.resolved == "ring:pin.top"
    np.testing.assert_allclose(rim.point, [0, 0, 0.02], atol=1e-12)
    np.testing.assert_allclose(rim.normal, [0, 0, 1], atol=1e-12)
    base = resolve(tree, tree.blocks["b"], "ring:b.bottom")
    np.testing.assert_allclose(base.normal, [0, 0, -1], atol=1e-12)


def test_sites_and_atoms_parse_but_resolve_to_a_named_note() -> None:
    tree = _tree(_BOX)
    missing = resolve(tree, tree.blocks["b"], "atoms:ghost[1]")
    assert missing.error is not None and "no block 'ghost'" in missing.error
    unbound = resolve(tree, tree.blocks["b"], "sites:b/seamA/s0..s3")
    assert unbound.error is not None and "binds no structure" in unbound.error
    tree.blocks["b"].bound_kind = "structure"
    tree.blocks["b"].bound = "cnt-55"
    bound = resolve(tree, tree.blocks["b"], "atoms:b[0-3]")
    assert bound.error is not None
    assert "'cnt-55'" in bound.error and "does not load" in bound.error


def test_a_patch_datum_measures_a_feature_distance() -> None:
    tree = _tree(
        _BOX,
        {
            "op": "add_measure",
            "block": "b",
            "name": "h",
            "datum": _PATCH,
            "relation": {"feature": "face:b.bottom"},
            "min": 0.009,
            "max": 0.011,
        },
    )
    mv = evaluate_measure(tree, tree.measures[0])
    assert mv.value == pytest.approx(0.01)
    assert mv.datum_resolved == _PATCH
    assert not mv.notes


# ── measurand on measures ────────────────────────────────────────────────────


def _q(**kw: Any) -> dict[str, Any]:
    return {
        "op": "add_measure",
        "block": "b",
        "name": "q",
        "measurand": "surface charge density",
        "datum": _PATCH,
        "min": -1.0,
        "max": -0.5,
        "strength": "hard",
        **kw,
    }


def test_measurand_snapshots_slug_ref_and_unit() -> None:
    tree = _tree(_BOX, _q())
    (m,) = tree.measures
    assert (m.measurand, m.measurand_ref, m.unit) == (
        "surface-charge-density",
        901,
        "C/m^2",
    )
    assert (m.min_value, m.max_value, m.strength) == (-1.0, -0.5, "hard")


def test_a_bare_tree_refuses_a_measurand_rather_than_guessing() -> None:
    with pytest.raises(OpError, match="no measurand resolver"):
        _tree(_BOX, _q(), resolver=False)


def test_unknown_measurand_is_refused_with_the_resolver_message() -> None:
    with pytest.raises(OpError, match="no taxon matches"):
        _tree(_BOX, _q(measurand="phlogiston"))


def test_a_contradicting_unit_is_refused_and_an_agreeing_one_accepted() -> None:
    with pytest.raises(OpError, match="contradicts measurand"):
        _tree(_BOX, _q(unit="m"))
    tree = _tree(
        _BOX,
        {"op": "add_measure", "block": "b", "name": "a", "measurand": "contact angle"},
        {
            "op": "add_measure",
            "block": "b",
            "name": "a2",
            "measurand": "contact angle",
            "unit": "deg",
        },
    )
    assert [m.unit for m in tree.measures] == ["deg", "deg"]


def test_a_categorical_measurand_takes_no_numbers() -> None:
    tree = _tree(
        _BOX,
        {
            "op": "add_measure",
            "block": "b",
            "name": "phob",
            "measurand": "hydrophobicity index",
            "datum": "ring:b.top",
        },
    )
    assert tree.measures[0].unit == ""
    with pytest.raises(OpError, match="categorical"):
        apply_ops(
            tree, [{"op": "set_measure", "block": "b", "name": "phob", "min": 1.0}]
        )
    with pytest.raises(OpError, match="categorical"):
        _tree(
            _BOX,
            {
                "op": "add_measure",
                "block": "b",
                "name": "phob",
                "measurand": "hydrophobicity index",
                "value": 2.0,
            },
        )


def test_set_measure_measurand_and_unit_rules() -> None:
    tree = _tree(_BOX, {"op": "add_measure", "block": "b", "name": "w", "value": 0.1})
    m = tree.measures[0]
    assert (m.measurand, m.unit) == (None, "m")  # legacy form, unchanged
    apply_ops(
        tree,
        [
            {
                "op": "set_measure",
                "block": "b",
                "name": "w",
                "measurand": "contact angle",
            }
        ],
    )
    assert (m.measurand, m.measurand_ref, m.unit) == ("contact-angle", 905, "deg")
    with pytest.raises(OpError, match="takes its unit from measurand"):
        apply_ops(tree, [{"op": "set_measure", "block": "b", "name": "w", "unit": "m"}])
    with pytest.raises(OpError, match="cannot clear measurand"):
        apply_ops(
            tree, [{"op": "set_measure", "block": "b", "name": "w", "measurand": None}]
        )


def test_relations_compare_the_measurand_unit_snapshot() -> None:
    tree = _tree(
        _BOX,
        _q(name="q0", min=None, max=None, value=-0.8, strength="gauge"),
        _q(
            name="q1",
            min=None,
            max=None,
            strength="gauge",
            relation={"source": "b.q0", "offset": 0.0, "tol": 0.1},
        ),
        {
            "op": "add_measure",
            "block": "b",
            "name": "dq",
            "measurand": "net partial charge",
            "relation": {"source": "b.q0", "tol": 0.1},
        },
    )
    by = {r.measure: r for r in stackup(tree.measures)}
    assert by["b.q1"].problem is None
    assert by["b.q1"].derived == pytest.approx(-0.8)
    assert by["b.dq"].problem_kind == "unit_mismatch"
    assert "'e' (measurand net-partial-charge)" in (by["b.dq"].problem or "")
    assert "'C/m^2' (measurand surface-charge-density)" in (by["b.dq"].problem or "")


# ── declared-but-unchecked is loud ───────────────────────────────────────────


def test_measurand_unchecked_names_measure_and_measurand() -> None:
    assert COMPUTERS == {}  # slice A registers no computer
    tree = _tree(
        _BOX,
        _q(),
        {"op": "add_measure", "block": "b", "name": "legacy", "value": 0.01},
        {"op": "add_measure", "block": "b", "name": "len", "measurand": "length"},
    )
    rows = [f for f in drc(tree).findings if f.rule == "measurand_unchecked"]
    assert [f.subject for f in rows] == ["b.q"]
    (row,) = rows
    assert row.severity == "warn"
    assert "'surface-charge-density'" in row.detail and "tn901" in row.detail
    assert _PATCH in row.detail
    assert is_checked(None) and is_checked("length") and is_checked("angle")
    assert not is_checked("contact-angle")


# ── pockets ──────────────────────────────────────────────────────────────────


def _pocket(**kw: Any) -> dict[str, Any]:
    return {
        "op": "add_pocket",
        "block": "b",
        "name": "site",
        "shape": "sphere:r0.004",
        "regions": [
            {
                "selector": _PATCH,
                "measures": [
                    {
                        "name": "q",
                        "measurand": "surface charge density",
                        "min": -1.0,
                        "max": -0.5,
                        "strength": "hard",
                    }
                ],
            },
            {"selector": "ring:b.top"},
        ],
        **kw,
    }


def test_add_pocket_writes_inline_measures_through_add_measure() -> None:
    tree = _tree(_BOX, _pocket())
    pocket = tree.blocks["b"].pockets["site"]
    assert pocket.shape == "sphere:r0.004"
    assert pocket.regions == [_PATCH, "ring:b.top"]
    (m,) = tree.measures
    assert (m.block, m.name, m.datum, m.measurand) == (
        "b",
        "q",
        _PATCH,
        "surface-charge-density",
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"shape": "blob:r1"}, "bad envelope"),
        ({"regions": [{"selector": "patch:b.top"}]}, "patch selector"),
        (
            {"regions": [{"selector": "ring:b.top"}, {"selector": " ring:b.top"}]},
            "listed twice",
        ),
        (
            {
                "regions": [
                    {
                        "selector": "ring:b.top",
                        "measures": [{"name": "x", "datum": "frame"}],
                    }
                ]
            },
            "datum",
        ),
        ({"regions": [{"selector": "ring:b.top", "extra": 1}]}, "unknown key"),
        ({"regions": "ring:b.top"}, "must be a list"),
        ({"name": "a.b"}, "must not contain"),
    ],
)
def test_add_pocket_refusals(change: dict[str, Any], match: str) -> None:
    with pytest.raises(OpError, match=match):
        _tree(_BOX, _pocket(**change))


def test_a_failing_inline_measure_leaves_no_partial_write() -> None:
    tree = _tree(_BOX)
    bad = _pocket()
    bad["regions"][1]["measures"] = [
        {"name": "ok", "measurand": "contact angle"},
        {"name": "q", "measurand": "phlogiston"},
    ]
    with pytest.raises(OpError, match="add_pocket"):
        apply_ops(tree, [bad])
    assert tree.measures == [] and tree.blocks["b"].pockets == {}


def test_set_and_remove_pocket() -> None:
    tree = _tree(_BOX, _pocket())
    apply_ops(
        tree,
        [
            {
                "op": "set_pocket",
                "block": "b",
                "name": "site",
                "shape": "hull",
                "regions": [
                    {
                        "selector": "ring:b.bottom",
                        "measures": [{"name": "rim", "measurand": "contact angle"}],
                    }
                ],
            }
        ],
    )
    pocket = tree.blocks["b"].pockets["site"]
    assert (pocket.shape, pocket.regions) == ("hull", ["ring:b.bottom"])
    assert {m.name for m in tree.measures} == {"q", "rim"}
    with pytest.raises(OpError, match="cannot clear shape"):
        apply_ops(
            tree, [{"op": "set_pocket", "block": "b", "name": "site", "shape": None}]
        )
    with pytest.raises(OpError, match="no pocket 'nope'"):
        apply_ops(tree, [{"op": "remove_pocket", "block": "b", "name": "nope"}])
    apply_ops(tree, [{"op": "remove_pocket", "block": "b", "name": "site"}])
    assert tree.blocks["b"].pockets == {}
    assert {m.name for m in tree.measures} == {"q", "rim"}  # measures stay


def test_pockets_live_on_the_template() -> None:
    tree = _tree(_BOX, {"op": "instance_block", "name": "b2", "template": "b"})
    with pytest.raises(OpError, match="pockets live on the template"):
        apply_ops(tree, [_pocket(block="b2")])


def test_pockets_view_lists_regions_and_derived_members() -> None:
    tree = _tree(
        _BOX,
        _pocket(),
        # written later, standalone — joins the region its datum names
        {
            "op": "add_measure",
            "block": "b",
            "name": "phob",
            "measurand": "hydrophobicity index",
            "datum": "ring:b.top",
        },
        {"op": "add_measure", "block": "b", "name": "far", "datum": "ring:b.bottom"},
    )
    body = _render_pockets(tree)
    assert body.startswith("# se pockets — 1 pocket(s)")
    assert "## b.site" in body
    assert "shape: sphere:r0.004 · regions: 2" in body
    assert f"- region {_PATCH} — 1 measure(s)" in body
    assert "- region ring:b.top — 1 measure(s)" in body
    q_line = next(line for line in body.splitlines() if "b.q ·" in line)
    for part in (
        "measurand surface-charge-density (tn901)",
        "unit C/m^2",
        "band [-1 C/m^2, -0.5 C/m^2]",
        "hard",
        "measurand_unchecked",
    ):
        assert part in q_line, part
    assert "unit (categorical)" in body
    assert "b.far" not in body  # not in any region


def test_pockets_view_on_an_empty_design_is_an_empty_list() -> None:
    body = _render_pockets(SeTree())
    assert body.startswith("# se pockets — 0 pocket(s)")
    assert "(no pockets declared)" in body


# ── ops export round trip (pure) ─────────────────────────────────────────────


def test_measurand_and_pockets_round_trip_through_the_ops_export() -> None:
    original = _tree(
        _BOX,
        _pocket(),
        {"op": "add_measure", "block": "b", "name": "legacy", "value": 0.01},
    )
    exported = design_ops(original)
    measure_ops = [op for op in exported if op["op"] == "add_measure"]
    assert len(measure_ops) == 2  # the region measure is emitted ONCE
    q = next(op for op in measure_ops if op["name"] == "q")
    assert q["measurand"] == "surface-charge-density" and "unit" not in q
    legacy = next(op for op in measure_ops if op["name"] == "legacy")
    assert legacy["unit"] == "m" and "measurand" not in legacy
    (pocket_op,) = [op for op in exported if op["op"] == "add_pocket"]
    assert pocket_op["regions"] == [{"selector": _PATCH}, {"selector": "ring:b.top"}]
    replay = SeTree()
    replay.measurands = _stub
    apply_ops(replay, exported)
    assert _norm(tree_to_json(replay)) == _norm(tree_to_json(original))
    assert not any(op["op"] == "remove_pocket" for op in exported)
    assert any("remove_pocket" in gap for gap in NOT_CARRIED)


def _norm(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out["blocks"] = sorted(out["blocks"], key=lambda b: b["name"])
    out["measures"] = sorted(out["measures"], key=lambda m: (m["block"], m["name"]))
    return out


def test_measurand_from_meta_unit_rule() -> None:
    assert (
        measurand_from_meta(
            1,
            {
                "slug": "length",
                "dimension_kind": "si",
                "si_vector": "1,0,0,0,0,0,0",
                "canonical_unit": "mm",
            },
        ).unit
        == "m"
    )
    assert (
        measurand_from_meta(
            2,
            {
                "slug": "x",
                "dimension_kind": "si",
                "si_vector": "0,0,1,1,0,0,0",
                "canonical_unit": "e",
            },
        ).unit
        == "e"
    )
    assert (
        measurand_from_meta(3, {"slug": "p", "dimension_kind": "dimensionless"}).unit
        == "ratio"
    )
    assert (
        measurand_from_meta(4, {"slug": "n", "dimension_kind": "count"}).unit == "count"
    )
    cat = measurand_from_meta(
        5,
        {
            "slug": "h",
            "dimension_kind": "categorical",
            "value_type": "categorical",
            "allowed_values": ["a", "b"],
        },
    )
    assert (cat.unit, cat.categorical, cat.allowed_values) == ("", True, ("a", "b"))
    with pytest.raises(MeasureError, match="no canonical_unit"):
        measurand_from_meta(6, {"slug": "usd", "dimension_kind": "currency"})


# ── through the store: the seed migration and the handler ────────────────────


def _run_migration(name: str) -> None:
    import psycopg

    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


_SEEDED = (
    "length",
    "angle",
    "count",
    "ratio",
    "contact-angle",
    "surface-charge-density",
    "net-partial-charge",
    "dipole-moment",
    "h-bond-donor-count",
    "h-bond-acceptor-count",
    "electric-field-magnitude",
    "absorption-maximum-wavelength",
    "hydrophobicity-index",
)


@pytest.fixture
def taxonomy(store: Store) -> Store:
    """The taxonomy as prod has it: 0174's registry seed, then 0182."""
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    return store


def _taxa(store: Store) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'slug' = ANY(%s) ORDER BY ref_id",
            (list(_SEEDED),),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2])) for r in rows]


def test_seed_inserts_each_measurand_once_and_is_idempotent(taxonomy: Store) -> None:
    store = taxonomy
    first = _taxa(store)
    assert sorted(m["slug"] for _i, _t, m in first) == sorted(_SEEDED)
    _run_migration("0182_se_measurand_seed.sql")
    assert _taxa(store) == first  # replay inserts nothing

    def _slug(rid: int) -> str | None:
        ref = store.get_ref(kind="taxon", id=rid)
        return None if ref is None else (ref.meta or {}).get("slug")

    measurand_root = next(
        rid
        for rid in store.taxon_start_nodes_reached(first[0][0])
        if _slug(rid) == "measurand"
    )
    for rid, title, meta in first:
        if meta.get("legacy_source"):
            continue  # 0174's Length — not this file's to shape
        assert meta["status"] == "proposed"
        assert validate_taxon_meta(meta) == meta
        assert meta["slug"] == slugify(title)
        assert meta["norm_name"] == normalize_name(title)
        assert meta.get("dimension_kind"), title
        assert [p for p, _ax in store.taxon_parents(rid)] == [measurand_root]
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1", (rid,)
            ).fetchone()
        assert row is not None
        assert row[0] == taxon_card_text(title, meta["definition"], meta["aliases"])
    units = {m["slug"]: m.get("canonical_unit") for _i, _t, m in first}
    assert units["surface-charge-density"] == "C/m^2"
    assert units["absorption-maximum-wavelength"] == "m"
    hyd = next(m for _i, _t, m in first if m["slug"] == "hydrophobicity-index")
    assert (hyd["value_type"], hyd["dimension_kind"]) == ("categorical", "categorical")


def test_seed_is_a_noop_without_the_measurand_root(store: Store) -> None:
    """A database whose taxonomy is gone (the truncated test DB the
    migrator catches up) inserts nothing — never unrooted nodes, never a
    failed chain."""
    _run_migration("0182_se_measurand_seed.sql")
    assert _taxa(store) == []


@pytest.fixture
def handler(hub: Hub, taxonomy: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> None:
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def _loaded(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


_A1 = {
    "op": "add_measure",
    "block": "b",
    "name": "q",
    "measurand": "surface charge density",
    "datum": _PATCH,
    "min": -1.0,
    "max": -0.5,
    "strength": "hard",
}


def test_acceptance_1_measurand_round_trips_and_shows_in_its_pocket(
    handler: SeHandler,
) -> None:
    _put(
        handler,
        "reg-src",
        [
            _BOX,
            _A1,
            {
                "op": "add_pocket",
                "block": "b",
                "name": "site",
                "regions": [{"selector": _PATCH}],
            },
        ],
    )
    src = _loaded(handler, "reg-src")
    (m,) = src.measures
    assert m.measurand_ref is not None
    node = handler.store.get_ref(kind="taxon", id=m.measurand_ref)
    assert node is not None and node.meta["slug"] == "surface-charge-density"
    assert (m.measurand, m.unit, m.datum) == ("surface-charge-density", "C/m^2", _PATCH)
    assert src.blocks["b"].pockets["site"].regions == [_PATCH]

    pockets = handler.get(id="reg-src", view="pockets").body
    assert f"- region {_PATCH} — 1 measure(s)" in pockets
    assert "measurand surface-charge-density" in pockets

    body = handler.get(id="reg-src", view="ops").body
    exported = json.loads(body.split("```json")[1].split("```")[0])["ops"]
    _put(handler, "reg-copy", exported)
    copy = _loaded(handler, "reg-copy")

    def strip_uids(payload: dict[str, Any]) -> dict[str, Any]:
        out = _norm(payload)
        out["blocks"] = [
            {k: v for k, v in b.items() if k != "uid"} for b in out["blocks"]
        ]
        return out

    assert strip_uids(tree_to_json(copy)) == strip_uids(tree_to_json(src))


def test_measurand_resolves_by_handle_and_path(handler: SeHandler) -> None:
    (rid,) = [i for i, _t, m in _taxa(handler.store) if m["slug"] == "dipole-moment"]
    _put(
        handler,
        "reg-forms",
        [
            _BOX,
            {"op": "add_measure", "block": "b", "name": "d1", "measurand": f"tn{rid}"},
            {
                "op": "add_measure",
                "block": "b",
                "name": "d2",
                "measurand": "measurand/dipole-moment",
            },
            {"op": "add_measure", "block": "b", "name": "d3", "measurand": rid},
            {"op": "add_measure", "block": "b", "name": "len", "measurand": "length"},
        ],
    )
    by = {m.name: m for m in _loaded(handler, "reg-forms").measures}
    assert {by[n].measurand_ref for n in ("d1", "d2", "d3")} == {rid}
    assert by["d1"].unit == "D"
    # the seeded Length node quotes mm; se stores lengths in metres
    assert (by["len"].measurand, by["len"].unit) == ("length", "m")


def test_unknown_and_non_measurand_references_are_refused(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="phlogiston"):
        _put(handler, "reg-bad", [_BOX, {**_A1, "measurand": "phlogiston"}])
    with pytest.raises(BadInput, match="not under the 'measurand' start node"):
        _put(handler, "reg-bad", [_BOX, {**_A1, "measurand": "subject"}])
    assert handler.store.get_ref(kind="se", id="reg-bad") is None


def test_acceptance_6_pockets_view_on_a_design_without_pockets(
    handler: SeHandler,
) -> None:
    _put(handler, "reg-empty", [_BOX])
    body = handler.get(id="reg-empty", view="pockets").body
    assert body.startswith("# se pockets — 0 pocket(s)")


def test_drc_view_reports_measurand_unchecked(handler: SeHandler) -> None:
    _put(handler, "reg-drc", [_BOX, _A1])
    body = handler.get(id="reg-drc", view="drc").body
    assert "measurand_unchecked" in body and "b.q" in body


def test_pockets_survive_edit_and_remove_through_the_store(handler: SeHandler) -> None:
    _put(
        handler,
        "reg-edit",
        [
            _BOX,
            {
                "op": "add_pocket",
                "block": "b",
                "name": "p",
                "shape": "hull",
                "regions": [{"selector": "ring:b.top"}],
            },
        ],
    )
    handler.edit(
        id="reg-edit",
        ops=[
            {
                "op": "add_measure",
                "block": "b",
                "name": "rim",
                "measurand": "contact angle",
                "datum": "ring:b.top",
                "max": 30,
            }
        ],
    )
    tree = _loaded(handler, "reg-edit")
    assert tree.blocks["b"].pockets["p"].shape == "hull"
    assert "b.rim" in handler.get(id="reg-edit", view="pockets").body
    handler.edit(
        id="reg-edit", ops=[{"op": "remove_pocket", "block": "b", "name": "p"}]
    )
    tree = _loaded(handler, "reg-edit")
    assert tree.blocks["b"].pockets == {}
    assert [m.name for m in tree.measures] == ["rim"]
