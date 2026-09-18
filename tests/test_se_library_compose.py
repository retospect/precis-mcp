"""Composition proposer — ``search(kind='se', compose={...})``
(:mod:`precis_se.compose`; docs/backlog/port-pose-and-composition-
search.md "New item — composition proposer").

Covers: the enumeration arithmetic incl. PSS scaling and the ``PSS
unknown`` mark; never-empty nearest-miss; the ``floppy`` flag and the
``stiffness unknown`` mark; a block without length rows is skipped and
counted; ``compose=`` reaches the handler over the MCP door (the verb
signature IS the schema); ``wants=`` keys still score on the switch
block; the parser's refusals.
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
from precis.handlers.material import MaterialHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools import core as tools_core
from precis_se import compose as se_compose
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


def _row_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln[:2].rstrip(".").isdigit()]


def _unit(
    handler: SeHandler,
    material: MaterialHandler,
    store: Store,
    slug: str,
    *,
    delta: float | None = None,
    length: float | None = None,
    pss: float | None = None,
    half_life: float | None = None,
    lp: float | None = None,
    roles: tuple[str, ...] = (),
    thermal: bool = False,
    requires: dict[str, Any] | None = None,
) -> None:
    """One library block ``<slug>#u`` whose facts live on a material the
    design is ``made-of`` (scoped to the block) — the star-schema path
    slice 4 resolves through."""
    mat = f"mat-{slug}"
    material.put(id=mat, title=mat)
    facts: list[tuple[str, float | None, str | None, dict[str, Any] | None]] = [
        (se_compose.DELTA_KEY, delta, "Å", None),
        (se_compose.LENGTH_KEY, length, "nm", None),
        (se_compose.PSS_KEY, pss, None, {"wavelength": "365 nm"}),
        (se_compose.HALF_LIFE_KEY, half_life, "s", None),
        (se_compose.LP_KEY, lp, "nm", None),
    ]
    for key, value, unit, conditions in facts:
        if value is not None:
            material.put(
                id=mat, property=key, value=value, unit=unit, conditions=conditions
            )
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}
    ]
    for i, role in enumerate(roles):
        ops.append({"op": "add_port", "block": "u", "name": f"p{i}", "roles": [role]})
    if delta is not None:
        ops.append(
            {
                "op": "declare_states",
                "block": "u",
                "states": [{"name": "trans"}, {"name": "cis"}],
            }
        )
        transitions: list[dict[str, Any]] = [
            {"from_state": "trans", "to_state": "cis", "driver_kind": "light"},
            {
                "from_state": "cis",
                "to_state": "trans",
                "driver_kind": "thermal" if thermal else "light",
            },
        ]
        if requires is not None:
            transitions[0]["requires"] = requires
        ops.append(
            {"op": "declare_transitions", "block": "u", "transitions": transitions}
        )
    handler.put(id=slug, text=json.dumps({"ops": ops}))
    design_ref = store.get_ref(kind="se", id=slug)
    mat_ref = store.get_ref(kind="material", id=mat)
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": "u"},
    )


# ── arithmetic, PSS scaling, floppy ─────────────────────────────────────


def test_series_arithmetic_with_pss_scaling_and_the_unknown_mark(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0, pss=0.8, half_life=172800.0,
        roles=("azide",), thermal=True,
    )  # fmt: skip
    _unit(handler, material, store, "nopss", delta=3.4, length=1.0, roles=("azide",))
    _unit(handler, material, store, "rod", length=10.0, lp=15.0, roles=("alkyne",))

    body = handler.search(compose={"delta": [8, 9], "span": [20, 30]}).body
    lines = _row_lines(body)
    top = lines[0]
    # 3 × 3.4 Å = 10.2 Å ideal, 8.16 Å at PSS 80 % → inside [8, 9];
    # span 3 × 1 nm + 2 × 10 nm = 23 nm → inside [20, 30].
    assert top.startswith("1. 3 × azo#u + 2 × rod#u  2/2")
    assert "✓delta: 10.2 Å (8.16 Å at PSS 80 % short; wavelength=365 nm)" in top
    assert "✓span: 23 nm" in top
    assert "floppy: span 23 nm > Lp 15 nm (rod#u)" in top
    assert "bistable ✗ (T-type (thermal reverse), τ½ 2 d)" in top
    assert "joining switch↔spacer: azide↔alkyne (CuAAC)" in top
    # The switch with no PSS row is scored on its ideal stroke and says so.
    nopss = next(ln for ln in lines if "3 × nopss#u + 2 × rod#u" in ln)
    assert "✗delta: 10.2 Å (PSS unknown)" in nopss
    assert "1/2" in nopss
    assert "bistable ✓ (2 states, no thermal path)" in nopss
    # Header honesty: counts + the Next ops script for the top row.
    assert "2 switch(es) × 1 spacer(s) from 3 library block(s)" in body
    assert "{'op': 'instance_block', 'name': 's1', 'template': 'azo#u'}" in body
    assert "{'op': 'connect', 'a': 's1.p0', 'b': 'p1.p0'}" in body


def test_never_empty_nearest_miss_shows_distances(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "azo", delta=3.4, length=1.0, roles=("azide",))
    body = handler.search(compose={"delta": [100, 120], "n_max": 4}).body
    lines = _row_lines(body)
    assert lines, body
    # Nothing reaches 100 Å; the nearest miss (n=4 → 13.6 Å) ranks first.
    assert lines[0].startswith("1. 4 × azo#u  0/1  ✗delta: 13.6 Å (PSS unknown)")
    assert "4 composition(s) ranked" in body


def test_stiffness_unknown_when_the_spacer_has_no_persistence_row(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)
    _unit(handler, material, store, "rod", length=10.0)
    body = handler.search(compose={"span": [10, 12], "n_max": 1, "m_max": 1}).body
    row = next(ln for ln in _row_lines(body) if "1 × azo#u + 1 × rod#u" in ln)
    assert "✓span: 11 nm" in row
    assert "stiffness unknown (rod#u: no persistence_length row)" in row
    assert "joining switch↔spacer: no complementary ports (no ports vs no ports)" in row


def test_blocks_without_length_rows_are_skipped_and_counted(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)
    handler.put(
        id="plain",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "b", "envelope": "sphere:r0.005"}]}
        ),
    )
    body = handler.search(compose={"delta": [3, 4], "n_max": 2}).body
    assert "1 switch(es) × 0 spacer(s) from 2 library block(s)" in body
    assert "1 block(s) carry no length facts — put(kind='material'" in body
    assert "plain#b" not in "\n".join(_row_lines(body))


def test_no_switch_in_the_library_says_nothing_to_compose(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "rod", length=10.0)
    body = handler.search(compose={"delta": [3, 4]}).body
    assert body.startswith("no library block carries a delta_length row")
    assert "1 spacer(s)" in body
    assert "property='delta_length'" in body


# ── wants= still scores on the switch block ────────────────────────────


def test_wants_keys_score_on_the_switch_block_and_bistable_carries_tau(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0, half_life=3600.0, roles=("azide",), thermal=True,
    )  # fmt: skip
    body = handler.search(
        compose={"delta": [3, 4], "n_max": 1},
        wants={"stimulus": "light", "bistable": True, "joining": "CuAAC"},
    ).body
    top = _row_lines(body)[0]
    assert top.startswith("1. 1 × azo#u  3/4")
    assert "✓stimulus: light" in top
    assert "✗bistable: T-type (thermal reverse), τ½ 1 h" in top
    assert "✓joining: azide (CuAAC)" in top
    assert "· bistable" not in top  # scored as an attr, not repeated as a note

    with pytest.raises(BadInput, match="collides with compose"):
        handler.search(compose={"delta": [3, 4]}, wants={"delta": 3.4})


# ── the MCP door ────────────────────────────────────────────────────────


@pytest.fixture
def mounted_runtime(
    runtime_with_store: PrecisRuntime, store: Store
) -> Iterator[PrecisRuntime]:
    _apply_se_migrations(store)
    # See tests/test_se_library_search.py::mounted_runtime for why the
    # plugin is registered directly when entry-point discovery lagged.
    if "se" not in runtime_with_store.hub.handlers:
        SeHandler(hub=runtime_with_store.hub)._register_with(runtime_with_store.hub)
    setattr(tools_core, "_runtime", runtime_with_store)  # noqa: B010
    try:
        yield runtime_with_store
    finally:
        tools_core._runtime = None


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def test_compose_kwarg_reaches_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime, store: Store
) -> None:
    handler = SeHandler(hub=Hub(store=store))
    material = MaterialHandler(hub=Hub(store=store))
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)

    out = tools_core.search(kind="se", compose={"delta": [3, 4], "n_max": 1})
    body = _body(out)
    assert "composition(s) ranked for compose=" in body
    assert "1 × azo#u" in body


# ── string form: compose='<design>#<block>' (Decision 3) ────────────────


def test_compose_string_form_scores_the_same_as_the_equivalent_dict(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0, pss=0.8, half_life=172800.0,
        roles=("azide",), thermal=True,
        requires={"delta": [8, 9], "span": [20, 30]},
    )  # fmt: skip
    _unit(handler, material, store, "rod", length=10.0, lp=15.0, roles=("alkyne",))

    # The string form always derives a `stimulus` wants key from the
    # edge's driver_kind — its true dict equivalent carries it explicitly.
    dict_body = handler.search(
        compose={"delta": [8, 9], "span": [20, 30]}, wants={"stimulus": "light"}
    ).body
    str_body = handler.search(compose="azo#u").body
    assert _row_lines(dict_body) == _row_lines(str_body)
    assert "box from se:azo#u trans->cis (light)" in str_body


def test_compose_string_form_derives_stimulus(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0, requires={"delta": [3, 4]},
    )  # fmt: skip
    top = _row_lines(handler.search(compose="azo#u").body)[0]
    assert "✓stimulus: light" in top


def test_compose_string_form_zero_addressable_edges_refused(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """Neither declared transition carries a ``requires=`` box — a
    pointer at ``declare_transitions``, not a silent empty result."""
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)
    with pytest.raises(BadInput, match="no transition with a requires= box") as exc:
        handler.search(compose="azo#u")
    assert "declare_transitions" in str(exc.value.next)


def test_compose_string_form_several_addressable_edges_need_the_selector(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)
    handler.edit(
        id="azo",
        ops=[
            {
                "op": "declare_transitions",
                "block": "u",
                "transitions": [
                    {
                        "from_state": "trans",
                        "to_state": "cis",
                        "driver_kind": "light",
                        "requires": {"delta": [3, 4]},
                    },
                    {
                        "from_state": "cis",
                        "to_state": "trans",
                        "driver_kind": "light",
                        "requires": {"span": [1, 2]},
                    },
                ],
            }
        ],
    )
    with pytest.raises(BadInput, match=r"azo#u/trans->cis"):
        handler.search(compose="azo#u")
    # the `/<from>-><to>` segment picks one.
    body = handler.search(compose="azo#u/trans->cis").body
    assert "box from se:azo#u trans->cis (light)" in body
    body = handler.search(compose="azo#u/cis->trans").body
    assert "box from se:azo#u cis->trans (light)" in body


def test_compose_string_form_wants_collision_and_derived_stimulus_override(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0,
        requires={"delta": [3, 4], "bistable": True},
    )  # fmt: skip
    # A declared (non-stimulus) requires key: the block's requirement owns it.
    with pytest.raises(BadInput, match="requirement owns that key"):
        handler.search(compose="azo#u", wants={"bistable": False})
    # stimulus is DERIVED, not declared — an explicit caller value wins.
    top = _row_lines(handler.search(compose="azo#u", wants={"stimulus": "light"}).body)[
        0
    ]
    assert "✓stimulus: light" in top


def test_compose_string_form_mcp_door_takes_a_string(
    mounted_runtime: PrecisRuntime, store: Store
) -> None:
    handler = SeHandler(hub=Hub(store=store))
    material = MaterialHandler(hub=Hub(store=store))
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=1.0, requires={"delta": [3, 4]},
    )  # fmt: skip
    out = tools_core.search(kind="se", compose="azo#u")
    body = _body(out)
    assert "composition(s) ranked for compose='azo#u'" in body
    assert "1 × azo#u" in body


# ── parser refusals (pure, no store) ────────────────────────────────────


@pytest.mark.parametrize(
    ("compose", "match"),
    [
        # The string form is `resolve_compose` (needs `store`), not this —
        # `parse_compose` stays dict-only and refuses a bare string like any
        # other non-dict.
        ("mydesign#box", "must be a JSON object"),
        ([1, 2], "must be a JSON object"),
        ({}, "needs at least one of"),
        ({"n_max": 3}, "needs at least one of"),
        ({"delta": [1, 2], "stroke": 1}, "unknown key"),
        ({"delta": "long"}, "must be a number"),
        ({"delta": True}, "must be a number"),
        ({"delta": [1, 2], "n_max": 0}, "n_max"),
        ({"delta": [1, 2], "m_max": 99}, "m_max"),
    ],
)
def test_parse_compose_refusals(compose: Any, match: str) -> None:
    with pytest.raises(BadInput, match=match):
        se_compose.parse_compose(compose)


def test_parse_compose_defaults_and_shapes() -> None:
    box = se_compose.parse_compose({"delta": 10, "span": [40, 50]})
    assert box.delta is not None and box.delta.target == 10
    assert box.span is not None and (box.span.min, box.span.max) == (40.0, 50.0)
    assert (box.n_max, box.m_max) == (6, 4)
    assert list(box.specs) == ["delta", "span"]
