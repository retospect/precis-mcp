"""se-print-implementer.md rung 1 — the widened `fdm` capability rows,
`capabilities.resolve()`/`orientation_policy()`, and the
`set_process_override`/`clear_process_override` ops + their migration
(``0011_se_process_overrides.sql``).

Fixture shape lifted from ``test_se_plugin.py``/``test_se_block_uid.py``
— the shared test DB template carries only core migrations, so the
plugin's own are seeded here for the one persistence-round-trip test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_se
from precis.blocktree.types import OpError
from precis.dispatch import Hub
from precis.store import Store
from precis_se import capabilities as se_caps
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeBlock, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: Every field the rung-1 widening adds, plus the three rung-3c fields —
#: the full roster every fdm row must answer for (a value, or an explicit
#: null with a source), per the acceptance criterion.
_ALL_FIELDS = (
    "hole_diameter_compensation",
    "min_wall",
    "min_boss_wall",
    "layer_height",
    "line_width",
    "max_overhang",
    "max_bridge",
    "min_bed_contact",
    "min_feature",
    "min_hole",
    "strength_z_ratio",
    "max_build",
)

_FDM_MODES = ("fdm/pla", "fdm/petg", "fdm/abs", "fdm/asa", "fdm/tpu")


def _row(mode: str) -> dict:
    rows = se_caps._rows()
    return rows[mode]


# ── every fdm row resolves every field, or is an explicit null with a source ──


@pytest.mark.parametrize("mode", _FDM_MODES)
def test_every_field_present_with_a_source(mode: str) -> None:
    row = _row(mode)
    fields = row.get("fields") or {}
    assert set(_ALL_FIELDS) <= set(fields), (
        f"{mode} is missing {set(_ALL_FIELDS) - set(fields)}"
    )
    for name in _ALL_FIELDS:
        raw = fields[name]
        source = raw.get("source")
        assert source, f"{mode}.{name} has no source"
        # A null field is null with a source saying so — never a bare
        # number-free entry, and never silently absent.
        if raw.get("house") is None:
            assert raw.get("physical") is None, (
                f"{mode}.{name} has a physical figure but no house one — "
                "the two-tier convention wants both or neither published"
            )


@pytest.mark.parametrize("mode", _FDM_MODES)
def test_every_field_resolves_or_is_none_via_capability(mode: str) -> None:
    for name in _ALL_FIELDS:
        cap = se_caps.capability(mode, name)
        row_field = _row(mode)["fields"][name]
        if row_field.get("house") is None:
            assert cap is None
        else:
            assert cap is not None
            assert cap.source
            assert cap.mode == mode
            assert cap.field == name


def test_no_numeric_field_lacks_a_source() -> None:
    for mode in _FDM_MODES:
        for name, raw in _row(mode)["fields"].items():
            if raw.get("house") is not None or raw.get("physical") is not None:
                assert raw.get("source"), f"{mode}.{name} has a number but no source"


def test_unit_less_fields_default_to_mm() -> None:
    cap = se_caps.capability("fdm/pla", "min_wall")
    assert cap is not None
    assert cap.unit == "mm"


def test_deg_and_ratio_units_carried() -> None:
    overhang = se_caps.capability("fdm/pla", "max_overhang")
    assert overhang is not None
    assert overhang.unit == "deg"
    # min_bed_contact is null in every row (no published figure) — the
    # unit still parses off the raw field even though capability()
    # reports it as uncharacterized.
    assert _row("fdm/pla")["fields"]["min_bed_contact"].get("unit") == "ratio"


def test_house_mm_raises_on_a_deg_field() -> None:
    overhang = se_caps.capability("fdm/pla", "max_overhang")
    assert overhang is not None
    with pytest.raises(ValueError, match="not millimetres"):
        _ = overhang.house_mm


def test_house_mm_still_works_on_an_mm_field() -> None:
    wall = se_caps.capability("fdm/pla", "min_wall")
    assert wall is not None
    assert wall.house_mm == pytest.approx(0.86)
    assert wall.house_m == pytest.approx(0.00086)


def test_unknown_mode_and_field_are_none() -> None:
    assert se_caps.capability("fdm/unobtainium", "max_overhang") is None
    assert se_caps.capability("fdm/pla", "no_such_field") is None
    assert se_caps.capability(None, "max_overhang") is None


# ── orientation_policy ──────────────────────────────────────────────────


def test_orientation_policy_fdm_returns_the_family_block() -> None:
    policy = se_caps.orientation_policy("fdm/asa")
    assert policy is not None
    weights = policy["weights"]
    assert weights["overhang"] == pytest.approx(1.0)
    assert weights["bed_contact"] == pytest.approx(0.5)
    assert weights["height"] == pytest.approx(0.2)
    assert weights["bridges"] == pytest.approx(0.8)
    assert weights["load_vs_layer"] == pytest.approx(1.0)
    assert policy["sweep_deg"] == 30
    assert "house judgment" in policy["source"]


def test_orientation_policy_unknown_family_is_none() -> None:
    assert se_caps.orientation_policy("laser/acrylic") is None
    assert se_caps.orientation_policy(None) is None


# ── known_fields (family-wide, used by the ops' write-time gate) ────────


def test_known_fields_is_family_wide_not_per_material() -> None:
    # max_bridge is null on fdm/petg's own row, but it IS a field the
    # fdm family defines (other rows carry a figure) — an override may
    # fill the gap.
    assert "max_bridge" in se_caps.known_fields("fdm/petg")
    assert se_caps.known_fields("laser/acrylic") == frozenset()
    assert se_caps.known_fields(None) == frozenset()


# ── resolve() ────────────────────────────────────────────────────────────


def _block(name: str = "clamp", *, mode: str | None = "fdm/pla", **kw) -> SeBlock:
    return SeBlock(name=name, mode=mode, **kw)


def test_resolve_house_tier_with_no_override() -> None:
    tree = SeTree()
    block = _block()
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "max_overhang")
    assert resolved is not None
    assert resolved.tier == "house"
    assert resolved.value == pytest.approx(40.0)
    assert resolved.unit == "deg"
    assert resolved.clamped is False
    assert resolved.capability is not None


def test_resolve_override_tier_wins_over_house() -> None:
    tree = SeTree()
    block = _block(process_overrides={"max_overhang": 30.0})
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "max_overhang")
    assert resolved is not None
    assert resolved.tier == "override"
    assert resolved.value == pytest.approx(30.0)
    assert resolved.clamped is False


def test_resolve_clamps_a_sub_floor_override() -> None:
    # The write path (`set_process_override`) refuses this value outright
    # — exercised below — so the only way to observe the read-time clamp
    # is a tree built directly, bypassing ops.
    tree = SeTree()
    block = _block(process_overrides={"max_overhang": 60.0})
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "max_overhang")
    assert resolved is not None
    assert resolved.tier == "override"
    assert resolved.value == pytest.approx(45.0)
    assert resolved.clamped is True
    assert "physical" in resolved.note


def test_resolve_min_field_clamps_upward() -> None:
    tree = SeTree()
    block = _block(process_overrides={"min_wall": 0.1})
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "min_wall")
    assert resolved is not None
    assert resolved.clamped is True
    assert resolved.value == pytest.approx(0.42)


def test_resolve_unprefixed_field_does_not_clamp() -> None:
    # line_width has a physical figure (0.3) but no min_/max_ prefix, so
    # resolve() never guesses a clamp direction for it.
    tree = SeTree()
    block = _block(process_overrides={"line_width": 0.1})
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "line_width")
    assert resolved is not None
    assert resolved.value == pytest.approx(0.1)
    assert resolved.clamped is False


def test_resolve_unknown_field_is_none() -> None:
    tree = SeTree()
    block = _block()
    tree.blocks[block.name] = block
    assert se_caps.resolve(tree, block, "no_such_field") is None


def test_resolve_modeless_block_is_none() -> None:
    tree = SeTree()
    block = _block(mode=None)
    tree.blocks[block.name] = block
    assert se_caps.resolve(tree, block, "max_overhang") is None


# ── set_process_override / clear_process_override (ops, in-memory) ──────


def _apply(mode: str | None) -> SeTree:
    tree = apply_ops(
        SeTree(),
        [{"op": "add_block", "name": "clamp", "envelope": "box:w0.01d0.01h0.01"}],
    )
    if mode is not None:
        tree = apply_ops(tree, [{"op": "set_mode", "block": "clamp", "mode": mode}])
    return tree


def test_set_process_override_write_then_resolve() -> None:
    tree = _apply("fdm/pla")
    apply_ops(
        tree,
        [
            {
                "op": "set_process_override",
                "block": "clamp",
                "field": "max_overhang",
                "value": 30,
            }
        ],
    )
    assert tree.blocks["clamp"].process_overrides == {"max_overhang": 30.0}
    resolved = se_caps.resolve(tree, tree.blocks["clamp"], "max_overhang")
    assert resolved is not None
    assert resolved.tier == "override"
    assert resolved.value == pytest.approx(30.0)


def test_set_process_override_rejects_beyond_physical_floor() -> None:
    tree = _apply("fdm/pla")
    with pytest.raises(OpError, match="physical"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_process_override",
                    "block": "clamp",
                    "field": "max_overhang",
                    "value": 60,
                }
            ],
        )
    assert tree.blocks["clamp"].process_overrides is None


def test_set_process_override_rejects_unknown_field() -> None:
    tree = _apply("fdm/pla")
    with pytest.raises(OpError, match="accepted fields"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_process_override",
                    "block": "clamp",
                    "field": "not_a_real_field",
                    "value": 1,
                }
            ],
        )


def test_set_process_override_rejects_modeless_block() -> None:
    tree = _apply(None)
    with pytest.raises(OpError, match="no mode"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_process_override",
                    "block": "clamp",
                    "field": "max_overhang",
                    "value": 30,
                }
            ],
        )


def test_set_process_override_accepts_a_field_null_on_this_material() -> None:
    """max_bridge is null on fdm/petg's own row but is a real fdm-family
    field — an override fills the gap rather than being rejected as
    unknown."""
    tree = _apply("fdm/petg")
    apply_ops(
        tree,
        [
            {
                "op": "set_process_override",
                "block": "clamp",
                "field": "max_bridge",
                "value": 8,
            }
        ],
    )
    assert tree.blocks["clamp"].process_overrides == {"max_bridge": 8.0}


def test_clear_process_override_round_trip() -> None:
    tree = _apply("fdm/pla")
    apply_ops(
        tree,
        [
            {
                "op": "set_process_override",
                "block": "clamp",
                "field": "max_overhang",
                "value": 35,
            }
        ],
    )
    apply_ops(
        tree,
        [{"op": "clear_process_override", "block": "clamp", "field": "max_overhang"}],
    )
    assert tree.blocks["clamp"].process_overrides is None


def test_clear_process_override_unknown_field_raises() -> None:
    tree = _apply("fdm/pla")
    with pytest.raises(OpError, match="no override"):
        apply_ops(
            tree,
            [
                {
                    "op": "clear_process_override",
                    "block": "clamp",
                    "field": "max_overhang",
                }
            ],
        )


def test_set_process_override_unknown_block_raises() -> None:
    tree = _apply("fdm/pla")
    with pytest.raises(OpError, match="no such block|not.*found|unknown"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_process_override",
                    "block": "ghost",
                    "field": "max_overhang",
                    "value": 30,
                }
            ],
        )


# ── persistence round trip, through the real handler/store ──────────────


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


def test_process_overrides_persist_round_trip(handler: SeHandler, store: Store) -> None:
    resp = handler.put(
        id="clamp1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "clamp",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {"op": "set_mode", "block": "clamp", "mode": "fdm/pla"},
                    {
                        "op": "set_process_override",
                        "block": "clamp",
                        "field": "max_overhang",
                        "value": 35,
                    },
                ]
            }
        ),
    )
    assert "created" in resp.body
    ref_id = _ref_id(store, "clamp1")
    tree = persist.load_tree(store, ref_id)
    assert tree.blocks["clamp"].process_overrides == {"max_overhang": 35.0}
    resolved = se_caps.resolve(tree, tree.blocks["clamp"], "max_overhang")
    assert resolved is not None
    assert resolved.tier == "override"
    assert resolved.value == pytest.approx(35.0)

    # clear it (edit = apply-on-top, unlike put's full replace), reload —
    # the column round-trips back to NULL.
    handler.edit(
        id="clamp1",
        ops=[
            {
                "op": "clear_process_override",
                "block": "clamp",
                "field": "max_overhang",
            }
        ],
    )
    tree2 = persist.load_tree(store, ref_id)
    assert tree2.blocks["clamp"].process_overrides is None
