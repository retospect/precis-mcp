"""Blocktree slice 4 — ranked library search (``search(kind='se',
wants=...)``), docs/backlog/blocktree-library-build-plan.md §Slice 4 +
docs/backlog/port-pose-and-composition-search.md Decision 2.

The rule under test everywhere here: every attribute is optional, results
are ranked (never a strict filter), every row shows its per-attribute
match/miss with the actual value, and the result set is empty only when
the library itself is empty.

Fixture shape lifted from ``test_se_port_pose.py`` — the shared test DB
template carries only core migrations, so the plugin's own are seeded here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.component import ComponentHandler
from precis.handlers.material import MaterialHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools import core as tools_core
from precis_se import library as se_library
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


def _apply_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _apply_se_migrations(store)
    return SeHandler(hub=hub)


@pytest.fixture
def material(store: Store) -> MaterialHandler:
    return MaterialHandler(hub=Hub(store=store))


@pytest.fixture
def component(store: Store) -> ComponentHandler:
    return ComponentHandler(hub=Hub(store=store))


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> Any:
    return handler.put(id=slug, text=json.dumps({"ops": ops}))


def _row_lines(body: str) -> list[str]:
    """The numbered result lines only (skip header/notes/Next)."""
    return [ln for ln in body.splitlines() if ln[:2].rstrip(".").isdigit()]


# ── (a) ranked near-misses, all-miss row still present ──────────────────


def test_four_attribute_query_ranks_near_misses_and_keeps_the_all_miss_row(
    handler: SeHandler, component: ComponentHandler
) -> None:
    component.put(id="azo-comp-a", title="azo dye a", category="switch")
    component.put(id="azo-comp-a", spec="delta_length", value=1.0, unit="nm")

    _put(
        handler,
        "switch1",
        [
            {"op": "add_block", "name": "dyeA", "envelope": "sphere:r0.005"},
            {"op": "add_port", "block": "dyeA", "name": "p1", "roles": ["azide"]},
            {
                "op": "declare_states",
                "block": "dyeA",
                "states": [{"name": "trans"}, {"name": "cis"}],
            },
            {
                "op": "declare_transitions",
                "block": "dyeA",
                "transitions": [
                    {"from_state": "trans", "to_state": "cis", "driver_kind": "light"},
                    {"from_state": "cis", "to_state": "trans", "driver_kind": "light"},
                ],
            },
            {
                "op": "set_binding",
                "block": "dyeA",
                "kind": "component",
                "design": "azo-comp-a",
            },
            {"op": "add_block", "name": "blank", "envelope": "sphere:r0.005"},
        ],
    )

    wants = {
        "stimulus": "light",
        "bistable": True,
        "joining": "CuAAC",
        "delta_length": 1.0,
    }
    body = handler.search(wants=wants).body

    assert "switch1#dyeA" in body
    assert "switch1#blank" in body
    lines = _row_lines(body)
    idx_a = next(i for i, ln in enumerate(lines) if "switch1#dyeA" in ln)
    idx_blank = next(i for i, ln in enumerate(lines) if "switch1#blank" in ln)
    assert idx_a < idx_blank  # the near-miss/near-hit outranks the all-miss

    a_line = lines[idx_a]
    assert "4/4" in a_line
    assert "✓stimulus: light" in a_line
    assert "✓bistable: 2 states, no thermal path" in a_line
    assert "✓joining: azide (CuAAC)" in a_line
    assert "✓delta_length: 1" in a_line

    blank_line = lines[idx_blank]
    assert "0/4" in blank_line
    assert "✗stimulus: no transitions" in blank_line
    assert "✗bistable: 0 states" in blank_line
    assert "✗joining: no ports" in blank_line
    assert "✗delta_length: no value row" in blank_line


# ── (b) star-schema join order ───────────────────────────────────────────


def test_component_binding_reaches_its_own_spec_value(
    handler: SeHandler, component: ComponentHandler
) -> None:
    component.put(id="hose-a", title="hose a", category="switch")
    component.put(id="hose-a", spec="delta_length", value=2.0, unit="nm")
    _put(
        handler,
        "d1",
        [
            {"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "b1",
                "kind": "component",
                "design": "hose-a",
            },
        ],
    )
    body = handler.search(wants={"delta_length": 2.0}).body
    line = next(ln for ln in _row_lines(body) if "d1#b1" in ln)
    assert "✓delta_length: 2" in line
    assert "component:hose-a" in line


def test_component_made_of_material_reaches_the_property_value(
    handler: SeHandler, component: ComponentHandler, material: MaterialHandler
) -> None:
    material.put(id="azobenzene", title="Azobenzene")
    material.put(
        id="azobenzene",
        property="quantum_yield",
        value=0.8,
        unit="frac",
        conditions={"solvent": "MeCN"},
    )
    component.put(id="dye-comp", title="dye component", category="switch")
    component.put(id="dye-comp", made_of="material:azobenzene")
    _put(
        handler,
        "d2",
        [
            {"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "b1",
                "kind": "component",
                "design": "dye-comp",
            },
        ],
    )
    body = handler.search(wants={"quantum_yield": 0.8}).body
    line = next(ln for ln in _row_lines(body) if "d2#b1" in ln)
    assert "✓quantum_yield: 0.8 frac" in line
    assert "material:azobenzene" in line
    assert "solvent=MeCN" in line


def test_design_level_made_of_applies_to_every_block_without_a_scope(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="peek", title="PEEK")
    material.put(id="peek", property="quantum_yield", value=0.4, unit="frac")
    _put(
        handler,
        "d3",
        [
            {"op": "add_block", "name": "arm1", "envelope": "sphere:r0.005"},
            {"op": "add_block", "name": "arm2", "envelope": "sphere:r0.005"},
        ],
    )
    design_ref = store.get_ref(kind="se", id="d3")
    material_ref = store.get_ref(kind="material", id="peek")
    assert design_ref is not None and material_ref is not None
    store.add_link(
        src_ref_id=design_ref.id, dst_ref_id=material_ref.id, relation="made-of"
    )

    body = handler.search(wants={"quantum_yield": 0.4}).body
    arm1_line = next(ln for ln in _row_lines(body) if "d3#arm1" in ln)
    arm2_line = next(ln for ln in _row_lines(body) if "d3#arm2" in ln)
    assert "✓quantum_yield" in arm1_line
    assert "✓quantum_yield" in arm2_line


def test_design_level_made_of_scoped_to_one_block_via_meta(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="peek2", title="PEEK 2")
    material.put(id="peek2", property="quantum_yield", value=0.4, unit="frac")
    _put(
        handler,
        "d4",
        [
            {"op": "add_block", "name": "arm1", "envelope": "sphere:r0.005"},
            {"op": "add_block", "name": "arm2", "envelope": "sphere:r0.005"},
        ],
    )
    design_ref = store.get_ref(kind="se", id="d4")
    material_ref = store.get_ref(kind="material", id="peek2")
    assert design_ref is not None and material_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=material_ref.id,
        relation="made-of",
        meta={"block": "arm1"},
    )

    body = handler.search(wants={"quantum_yield": 0.4}).body
    arm1_line = next(ln for ln in _row_lines(body) if "d4#arm1" in ln)
    arm2_line = next(ln for ln in _row_lines(body) if "d4#arm2" in ln)
    assert "✓quantum_yield" in arm1_line
    assert "✗quantum_yield: no value row" in arm2_line


def test_structure_bound_block_reports_no_value_rows_with_its_binding(
    handler: SeHandler,
) -> None:
    _put(
        handler,
        "d5",
        [
            {"op": "add_block", "name": "core", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "core",
                "kind": "structure",
                "design": "some-structure",
            },
        ],
    )
    body = handler.search(wants={"delta_length": 1.0}).body
    line = next(ln for ln in _row_lines(body) if "d5#core" in ln)
    assert "bound to structure some-structure: no value rows" in line


# ── (c) interval / dict forms, weights, tolerance boundary ───────────────


def test_interval_form_matches_a_value_inside_the_band(
    handler: SeHandler, component: ComponentHandler
) -> None:
    component.put(id="comp-int", title="c", category="switch")
    component.put(id="comp-int", spec="delta_length", value=0.9, unit="nm")
    _put(
        handler,
        "d6",
        [
            {"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "b1",
                "kind": "component",
                "design": "comp-int",
            },
        ],
    )
    body = handler.search(wants={"delta_length": [0.8, 1.0]}).body
    line = next(ln for ln in _row_lines(body) if "d6#b1" in ln)
    assert "✓delta_length" in line


def test_interval_form_misses_a_value_outside_the_band(
    handler: SeHandler, component: ComponentHandler
) -> None:
    component.put(id="comp-int2", title="c", category="switch")
    component.put(id="comp-int2", spec="delta_length", value=1.5, unit="nm")
    _put(
        handler,
        "d7",
        [
            {"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "b1",
                "kind": "component",
                "design": "comp-int2",
            },
        ],
    )
    body = handler.search(wants={"delta_length": [0.8, 1.0]}).body
    line = next(ln for ln in _row_lines(body) if "d7#b1" in ln)
    assert "✗delta_length" in line


def test_tolerance_boundary_is_inclusive(
    handler: SeHandler, component: ComponentHandler
) -> None:
    """diff == tol * |target| matches; a hair beyond it does not."""
    component.put(id="comp-tol-hit", title="c", category="switch")
    component.put(id="comp-tol-hit", spec="delta_length", value=0.9, unit="nm")
    component.put(id="comp-tol-miss", title="c", category="switch")
    component.put(id="comp-tol-miss", spec="delta_length", value=0.89, unit="nm")
    _put(
        handler,
        "d8",
        [
            {"op": "add_block", "name": "hit", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "hit",
                "kind": "component",
                "design": "comp-tol-hit",
            },
            {"op": "add_block", "name": "miss", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "miss",
                "kind": "component",
                "design": "comp-tol-miss",
            },
        ],
    )
    body = handler.search(wants={"delta_length": 1.0}).body
    hit_line = next(ln for ln in _row_lines(body) if "d8#hit" in ln)
    miss_line = next(ln for ln in _row_lines(body) if "d8#miss" in ln)
    assert "✓delta_length" in hit_line
    assert "✗delta_length" in miss_line


def test_dict_form_weight_changes_the_ranking(
    handler: SeHandler, component: ComponentHandler
) -> None:
    component.put(id="comp-w1", title="c", category="switch")
    component.put(id="comp-w1", spec="delta_length", value=1.0, unit="nm")
    _put(
        handler,
        "d9",
        [
            {"op": "add_block", "name": "onlylength", "envelope": "sphere:r0.005"},
            {
                "op": "set_binding",
                "block": "onlylength",
                "kind": "component",
                "design": "comp-w1",
            },
            {"op": "add_block", "name": "onlystim", "envelope": "sphere:r0.005"},
            {
                "op": "declare_states",
                "block": "onlystim",
                "states": [{"name": "a"}, {"name": "b"}],
            },
            {
                "op": "declare_transitions",
                "block": "onlystim",
                "transitions": [
                    {"from_state": "a", "to_state": "b", "driver_kind": "light"},
                ],
            },
        ],
    )
    wants = {
        "delta_length": {"target": 1.0, "weight": 5.0},
        "stimulus": {"target": "light", "weight": 1.0},
    }
    body = handler.search(wants=wants).body
    lines = _row_lines(body)
    idx_len = next(i for i, ln in enumerate(lines) if "d9#onlylength" in ln)
    idx_stim = next(i for i, ln in enumerate(lines) if "d9#onlystim" in ln)
    assert idx_len < idx_stim  # the heavier-weighted match outranks the lighter one


# ── (d) bistable derivation ───────────────────────────────────────────────


def test_bistable_is_false_when_a_thermal_reverse_path_exists(
    handler: SeHandler,
) -> None:
    _put(
        handler,
        "d10",
        [
            {"op": "add_block", "name": "sw", "envelope": "sphere:r0.005"},
            {
                "op": "declare_states",
                "block": "sw",
                "states": [{"name": "trans"}, {"name": "cis"}],
            },
            {
                "op": "declare_transitions",
                "block": "sw",
                "transitions": [
                    {"from_state": "trans", "to_state": "cis", "driver_kind": "light"},
                    {
                        "from_state": "cis",
                        "to_state": "trans",
                        "driver_kind": "thermal",
                    },
                ],
            },
        ],
    )
    body = handler.search(wants={"bistable": True}).body
    line = next(ln for ln in _row_lines(body) if "d10#sw" in ln)
    assert "✗bistable: T-type (thermal reverse)" in line


# ── (e) joining via a role half ───────────────────────────────────────────


def test_joining_matches_either_half_of_a_named_chemistry(handler: SeHandler) -> None:
    _put(
        handler,
        "d11",
        [
            {"op": "add_block", "name": "azideblock", "envelope": "sphere:r0.005"},
            {"op": "add_port", "block": "azideblock", "name": "p", "roles": ["azide"]},
            {"op": "add_block", "name": "alkyneblock", "envelope": "sphere:r0.005"},
            {
                "op": "add_port",
                "block": "alkyneblock",
                "name": "p",
                "roles": ["alkyne"],
            },
            {"op": "add_block", "name": "donorblock", "envelope": "sphere:r0.005"},
            {"op": "add_port", "block": "donorblock", "name": "p", "roles": ["donor"]},
        ],
    )
    body = handler.search(wants={"joining": "CuAAC"}).body
    azide_line = next(ln for ln in _row_lines(body) if "d11#azideblock" in ln)
    alkyne_line = next(ln for ln in _row_lines(body) if "d11#alkyneblock" in ln)
    donor_line = next(ln for ln in _row_lines(body) if "d11#donorblock" in ln)
    assert "✓joining: azide (CuAAC)" in azide_line
    assert "✓joining: alkyne (CuAAC)" in alkyne_line
    assert "✗joining" in donor_line


# ── (f) unknown key: miss + header note, never a refusal ─────────────────


def test_unknown_key_scores_as_a_miss_and_is_noted_once(handler: SeHandler) -> None:
    _put(
        handler, "d12", [{"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"}]
    )
    body = handler.search(wants={"not_a_real_attribute": 5}).body
    assert "not a registered property/spec" in body
    assert "not_a_real_attribute" in body
    line = next(ln for ln in _row_lines(body) if "d12#b1" in ln)
    assert "✗not_a_real_attribute" in line


# ── (g) empty library ─────────────────────────────────────────────────────


def test_empty_library_says_so_and_is_never_a_hard_empty_result(
    handler: SeHandler,
) -> None:
    body = handler.search(wants={"stimulus": "light"}).body
    assert "the se library is empty" in body


# ── (h) q narrows candidates; a zero-match q falls back ───────────────────


def test_q_narrows_candidates_and_zero_match_falls_back_to_the_whole_library(
    handler: SeHandler,
) -> None:
    handler.put(
        id="narrow-hit",
        text=json.dumps(
            {
                "description": "a distinctive zeptobolt",
                "ops": [
                    {"op": "add_block", "name": "onlyhere", "envelope": "sphere:r0.005"}
                ],
            }
        ),
    )
    _put(
        handler,
        "narrow-miss",
        [{"op": "add_block", "name": "elsewhere", "envelope": "sphere:r0.005"}],
    )

    hit_body = handler.search(
        q="zeptobolt", wants={"stimulus": "light"}, mode="lexical"
    ).body
    assert "narrowed to" in hit_body
    assert "narrow-hit#onlyhere" in hit_body
    assert "narrow-miss#elsewhere" not in hit_body

    miss_body = handler.search(
        q="wordnotanywhereinthecorpus", wants={"stimulus": "light"}, mode="lexical"
    ).body
    assert "showing the whole library" in miss_body
    assert "narrow-hit#onlyhere" in miss_body
    assert "narrow-miss#elsewhere" in miss_body


# ── (i) bad wants shapes → BadInput ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_wants",
    [
        "not a dict",
        {},
        {"k": [1, 2, 3]},
        {"k": [2, 1]},
        {"k": [1, "a"]},
        {"k": {"bogus": 1}},
        {"k": {"min": 5, "max": 1}},
        {"k": None},
        {"k": {"conditions": "not-a-dict"}},
        {"k": {"conditions": {}}},
        {"k": {"conditions": {"salt": [1, 2]}}},
        {"k": {"conditions": {"salt": None}}},
    ],
)
def test_bad_wants_shapes_raise_bad_input(handler: SeHandler, bad_wants: Any) -> None:
    with pytest.raises(BadInput):
        handler.search(wants=bad_wants)


# ── (j) instances excluded ────────────────────────────────────────────────


def test_instances_are_excluded_from_the_library(handler: SeHandler) -> None:
    _put(
        handler,
        "d13",
        [
            {"op": "add_block", "name": "tmpl", "envelope": "sphere:r0.005"},
            {"op": "add_port", "block": "tmpl", "name": "p", "roles": ["azide"]},
            {"op": "instance_block", "name": "inst1", "template": "tmpl"},
        ],
    )
    body = handler.search(wants={"joining": "azide"}).body
    assert "d13#tmpl" in body
    assert "d13#inst1" not in body


# ── (k) the verb reaches the handler over the MCP door ────────────────────


@pytest.fixture
def mounted_runtime(
    runtime_with_store: PrecisRuntime, store: Store
) -> Iterator[PrecisRuntime]:
    _apply_se_migrations(store)
    # `se` is a Route-B plugin, registered via the `precis.handlers`
    # entry-point group (pyproject.toml) — discovered by
    # `dispatch._load_plugins` at `boot()` time from the INSTALLED
    # package's dist-info, which can lag a container image build (same
    # staleness `test_skill_ingest.py::test_shipped_skill_corpus_has_
    # zero_gate_findings` works around for plugin kind codes). Register
    # it directly on this runtime's hub rather than depending on that
    # metadata being fresh — the same `_register_with` call `_load_
    # plugins` itself makes once entry-point discovery succeeds. Skipped
    # when discovery DID succeed — registering twice is a boot-time bug
    # the hub refuses (DuplicateRegistration).
    if "se" not in runtime_with_store.hub.handlers:
        SeHandler(hub=runtime_with_store.hub)._register_with(runtime_with_store.hub)
    # ``_runtime`` is declared ``= None`` at module level (mypy infers
    # ``None``); the other runtime-swapping fixtures go through setattr too.
    setattr(tools_core, "_runtime", runtime_with_store)  # noqa: B010
    try:
        yield runtime_with_store
    finally:
        tools_core._runtime = None


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def test_wants_kwarg_reaches_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime, store: Store
) -> None:
    handler = SeHandler(hub=Hub(store=store))
    _put(
        handler, "d14", [{"op": "add_block", "name": "b1", "envelope": "sphere:r0.005"}]
    )

    out = tools_core.search(kind="se", wants={"stimulus": "light"})
    body = _body(out)
    assert "library block(s) ranked" in body
    assert "d14#b1" in body


# ── library.py unit-level coverage (pure functions, no store) ────────────


def test_parse_wants_scalar_interval_dict_shapes() -> None:
    specs = se_library.parse_wants(
        {
            "a": "light",
            "b": True,
            "c": [1.0, 2.0],
            "d": {"target": 5, "tol": 0.2, "weight": 3.0},
        }
    )
    assert specs["a"].target == "light"
    assert specs["b"].target is True
    assert specs["c"].min == 1.0 and specs["c"].max == 2.0
    assert specs["d"].tol == 0.2 and specs["d"].weight == 3.0


# ── read cache + the instance-only narrow ─────────────────────────────────


def test_a_two_key_query_reads_each_blocks_transitions_once(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stimulus`` and ``bistable`` both need a block's transitions; the
    per-search cache makes that one read per block, not one per key."""
    ops: list[dict[str, Any]] = []
    for name in ("b1", "b2", "b3"):
        ops += [
            {"op": "add_block", "name": name, "envelope": "sphere:r0.005"},
            {
                "op": "declare_states",
                "block": name,
                "states": [{"name": "trans"}, {"name": "cis"}],
            },
            {
                "op": "declare_transitions",
                "block": name,
                "transitions": [
                    {"from_state": "trans", "to_state": "cis", "driver_kind": "light"}
                ],
            },
        ]
    _put(handler, "cache-d", ops)

    from precis.design import states as design_states

    real = design_states.transitions_for
    calls: list[tuple[int, int]] = []

    def counting(store_: Any, ref_id: int, block_uid: int, **kw: Any) -> Any:
        calls.append((ref_id, block_uid))
        return real(store_, ref_id, block_uid, **kw)

    monkeypatch.setattr(design_states, "transitions_for", counting)
    body = handler.search(wants={"stimulus": "light", "bistable": True}).body
    assert "cache-d#b1" in body
    cache_ref = store.get_ref(kind="se", id="cache-d")
    assert cache_ref is not None
    mine = [c for c in calls if c[0] == cache_ref.id]
    assert len(mine) == 3, calls


def test_q_narrowing_to_instance_only_designs_falls_back_and_says_so(
    handler: SeHandler,
) -> None:
    _put(
        handler,
        "narrow-src",
        [{"op": "add_block", "name": "tmpl", "envelope": "sphere:r0.005"}],
    )
    handler.put(
        id="narrow-inst-only",
        text=json.dumps(
            {
                "description": "a distinctive quuxwidget",
                "ops": [
                    {
                        "op": "instance_block",
                        "name": "inst",
                        "template": "narrow-src#tmpl",
                    }
                ],
            }
        ),
    )
    body = handler.search(
        q="quuxwidget", wants={"stimulus": "light"}, mode="lexical"
    ).body
    assert "with no library blocks of their own — showing the whole library" in body
    assert "narrowed to" not in body
    assert "narrow-src#tmpl" in body
    assert "narrow-inst-only#inst" not in body


# ── (k) multi-row material property pick (gr346735) ────────────────────────


def _design_made_of(store: Store, slug: str, block: str, mat_ref_id: int) -> None:
    design_ref = store.get_ref(kind="se", id=slug)
    assert design_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref_id,
        relation="made-of",
        meta={"block": block},
    )


def test_multi_row_property_picks_newest_and_lists_the_others_passed_over(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="pick-mat-1", title="pick mat 1")
    material.put(id="pick-mat-1", property="persistence_length", value=10.0, unit="nm")
    material.put(id="pick-mat-1", property="persistence_length", value=20.0, unit="nm")
    material.put(id="pick-mat-1", property="persistence_length", value=30.0, unit="nm")
    _put(
        handler,
        "pick-d1",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-1")
    assert mat_ref is not None
    _design_made_of(store, "pick-d1", "u", mat_ref.id)

    body = handler.search(wants={"persistence_length": 30.0}).body
    line = next(ln for ln in _row_lines(body) if "pick-d1#u" in ln)
    assert "✓persistence_length: 30 nm" in line
    assert "sample 1 of 3 (newest)" in line
    assert "others:" in line
    assert "20 nm" in line
    assert "10 nm" in line


def test_single_row_property_provenance_unchanged(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """The single-row case must render exactly as before gr346735 — no
    ``sample`` / ``others`` note."""
    material.put(id="pick-mat-solo", title="pick mat solo")
    material.put(
        id="pick-mat-solo", property="persistence_length", value=15.0, unit="nm"
    )
    _put(
        handler,
        "pick-d-solo",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-solo")
    assert mat_ref is not None
    _design_made_of(store, "pick-d-solo", "u", mat_ref.id)

    body = handler.search(wants={"persistence_length": 15.0}).body
    line = next(ln for ln in _row_lines(body) if "pick-d-solo#u" in ln)
    assert "sample" not in line
    assert "others:" not in line


def test_band_row_wins_over_point_rows(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="pick-mat-2", title="pick mat 2")
    material.put(id="pick-mat-2", property="persistence_length", value=10.0, unit="nm")
    material.put(
        id="pick-mat-2",
        property="persistence_length",
        value_low=18.0,
        value_high=22.0,
        unit="nm",
    )
    _put(
        handler,
        "pick-d2",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-2")
    assert mat_ref is not None
    _design_made_of(store, "pick-d2", "u", mat_ref.id)

    body = handler.search(wants={"persistence_length": 20.0}).body
    line = next(ln for ln in _row_lines(body) if "pick-d2#u" in ln)
    assert "18.0–22.0 nm" in line
    assert "sample 1 of 2 (band)" in line
    assert "others:" in line and "10 nm" in line


def test_conditions_filter_selects_the_matching_row(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="pick-mat-3", title="pick mat 3")
    material.put(
        id="pick-mat-3",
        property="persistence_length",
        value=33.0,
        unit="nm",
        conditions={"salt": "250 mM NaCl"},
    )
    material.put(
        id="pick-mat-3",
        property="persistence_length",
        value=10.0,
        unit="nm",
        conditions={"salt": "50 mM NaCl"},
    )
    _put(
        handler,
        "pick-d3",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-3")
    assert mat_ref is not None
    _design_made_of(store, "pick-d3", "u", mat_ref.id)

    wants = {
        "persistence_length": {"target": 33, "conditions": {"salt": "250 mM NaCl"}}
    }
    body = handler.search(wants=wants).body
    line = next(ln for ln in _row_lines(body) if "pick-d3#u" in ln)
    assert "✓persistence_length: 33 nm" in line
    assert "conditions match" in line


def test_conditions_filter_falls_back_when_nothing_matches(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    material.put(id="pick-mat-4", title="pick mat 4")
    material.put(
        id="pick-mat-4",
        property="persistence_length",
        value=15.0,
        unit="nm",
        conditions={"salt": "50 mM NaCl"},
    )
    material.put(
        id="pick-mat-4",
        property="persistence_length",
        value=25.0,
        unit="nm",
        conditions={"salt": "100 mM NaCl"},
    )
    _put(
        handler,
        "pick-d4",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-4")
    assert mat_ref is not None
    _design_made_of(store, "pick-d4", "u", mat_ref.id)

    wants = {"persistence_length": {"target": 25, "conditions": {"salt": "9 M NaCl"}}}
    body = handler.search(wants=wants).body
    line = next(ln for ln in _row_lines(body) if "pick-d4#u" in ln)
    assert "no sample matches conditions" in line
    assert "sample 1 of 2 (newest)" in line


def test_conditions_filter_narrows_then_band_still_wins_over_newest(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """Reviewer follow-up: a ``conditions`` filter that narrows to more
    than one survivor must still run the unique-band tie-break over
    those survivors, not just take the newest matching row — the three
    rows are (oldest→newest) a non-matching point, a matching band, and
    a matching newer point; the band wins."""
    material.put(id="pick-mat-5", title="pick mat 5")
    material.put(
        id="pick-mat-5",
        property="persistence_length",
        value=5.0,
        unit="nm",
        conditions={"wavelength_nm": 436},
    )
    material.put(
        id="pick-mat-5",
        property="persistence_length",
        value_low=28.0,
        value_high=32.0,
        unit="nm",
        conditions={"wavelength_nm": 313},
    )
    material.put(
        id="pick-mat-5",
        property="persistence_length",
        value=10.0,
        unit="nm",
        conditions={"wavelength_nm": 313},
    )
    _put(
        handler,
        "pick-d5",
        [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}],
    )
    mat_ref = store.get_ref(kind="material", id="pick-mat-5")
    assert mat_ref is not None
    _design_made_of(store, "pick-d5", "u", mat_ref.id)

    wants = {"persistence_length": {"target": 30, "conditions": {"wavelength_nm": 313}}}
    body = handler.search(wants=wants).body
    line = next(ln for ln in _row_lines(body) if "pick-d5#u" in ln)
    assert "28.0–32.0 nm" in line
    assert "conditions match, band" in line
    assert "✓persistence_length" in line
