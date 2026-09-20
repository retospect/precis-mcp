"""Composition proposer — ``search(kind='se', compose={...})``
(:mod:`precis_se.compose`; docs/backlog/port-pose-and-composition-
search.md "New item — composition proposer").

Covers: the enumeration arithmetic incl. PSS scaling and the ``PSS
unknown`` mark; never-empty nearest-miss; the ``floppy`` flag and the
``stiffness unknown`` mark; a block without length rows is skipped and
counted; ``compose=`` reaches the handler over the MCP door (the verb
signature IS the schema); ``wants=`` keys still score on the switch
block; the parser's refusals; R3's lever family (a rotary unit + arm
units, ``swing`` box key, docs/backlog/port-rotation-and-lever-
composition.md "Slice R3") ranked beside — or, for a swing box, instead
of — the linear chains.
"""

from __future__ import annotations

import json
import math
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


#: The hinge's own envelope arm — a sub-nm box (``2e-10`` m per side, an
#: Å-scale round number) so R2's metre-scale envelope arithmetic and R3's
#: Å-scale ``delta`` box land in the same ballpark as a real switch's
#: stroke. ``p1`` sits centred in x/y at half height, exactly R2's own
#: hinge fixture (``tests/test_se_kinematics.py::_hinge_ops``) scaled
#: down, so the in-plane arm is the same ``half-width·√2`` shape.
_HINGE_HALF_WIDTH_M = 1e-10
ARM0_M = _HINGE_HALF_WIDTH_M * math.sqrt(2.0)


def _hinge(
    handler: SeHandler,
    slug: str,
    *,
    block: str = "u",
    roles: tuple[str, ...] = (),
    driver_kind: str = "mechanical",
    requires: dict[str, Any] | None = None,
) -> None:
    """One library block ``<slug>#<block>`` whose port ``p1`` swings 90°
    about z between two declared states (a
    ``port_pose_overrides`` rot delta on a posed port — R2 derives the
    swing from exactly this shape). ``roles`` lets a fixture give the
    rotating port a complementary half for the ops-script test; ``requires``
    rides on the one declared transition for the ``compose='<slug>#<block>'``
    string-form tests."""
    transition: dict[str, Any] = {
        "from_state": "trans",
        "to_state": "cis",
        "driver_kind": driver_kind,
    }
    if requires is not None:
        transition["requires"] = requires
    ops: list[dict[str, Any]] = [
        {
            "op": "add_block",
            "name": block,
            "envelope": "box:w2e-10d2e-10h2e-10",
        },
        {
            "op": "add_port",
            "block": block,
            "name": "p1",
            "pose": [0.0, 0.0, _HINGE_HALF_WIDTH_M],
            "roles": list(roles),
        },
        {
            "op": "declare_states",
            "block": block,
            "states": [
                {"name": "trans"},
                {
                    "name": "cis",
                    "port_pose_overrides": {"p1": {"rot": [0.0, 0.0, math.pi / 2.0]}},
                },
            ],
        },
        {"op": "declare_transitions", "block": block, "transitions": [transition]},
    ]
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def _link_step_angle(
    handler: SeHandler,
    material: MaterialHandler,
    store: Store,
    slug: str,
    block: str,
    rad: float,
) -> None:
    mat = f"mat-{slug}-step"
    material.put(id=mat, title=mat)
    material.put(id=mat, property="step_angle", value=rad, unit="rad")
    design_ref = store.get_ref(kind="se", id=slug)
    mat_ref = store.get_ref(kind="material", id=mat)
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": block},
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


# ── derived n_max/m_max bounds (gr356739) ───────────────────────────────


def test_span_box_derives_m_max_from_the_spacer_unit_length(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """The gripe's own case: a 20-30 nm span box over a 0.34 nm/bp spacer
    needs m in the 50s-80s — the old fixed default (4) could never reach
    it (best 1.36 nm). With no ``m_max`` given, each spacer derives its
    own from ``ceil(span_hi / unit_length)``; the switch (``length=0`` —
    all the span comes from the spacer here) still derives its own
    ``n_max`` from ``ceil(delta_hi / delta_length)`` so the PSS-scaled
    delta band is reachable too."""
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=0.0, pss=0.8, roles=("azide",),
    )  # fmt: skip
    _unit(handler, material, store, "dsdna-bp", length=0.34, roles=("alkyne",))

    body = handler.search(compose={"delta": [8, 9], "span": [20, 30]}).body
    lines = _row_lines(body)
    assert lines, body
    assert "unreachable" not in body
    matches = [ln for ln in lines if "✓span" in ln and "3 × azo#u" in ln]
    assert matches, body
    for ln in matches:
        m = int(ln.split("× dsdna-bp#u")[0].rsplit("+", 1)[1].strip().split()[0])
        assert 59 <= m <= 88, ln
    assert "8.16 Å at PSS 80 % short" in matches[0]


def test_explicit_m_max_below_the_derived_bound_says_unreachable(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """The same box, but the caller pins ``m_max=4`` explicitly — derived
    bounds never override an explicit one. The header names the bound
    that IS the problem, the best span actually reached under it, and the
    ``m_max`` that would reach the box's own lower edge (20 nm / 0.34
    nm/bp = 59 — computed, never hard-coded in the handler)."""
    _unit(
        handler, material, store, "azo",
        delta=3.4, length=0.0, pss=0.8, roles=("azide",),
    )  # fmt: skip
    _unit(handler, material, store, "dsdna-bp", length=0.34, roles=("alkyne",))

    body = handler.search(compose={"delta": [8, 9], "span": [20, 30], "m_max": 4}).body
    assert (
        "span unreachable at m_max=4 (max 1.36 nm with dsdna-bp#u) — "
        "pass m_max=59 or larger" in body
    )
    assert all("✗span" in ln for ln in _row_lines(body) if "3 × azo#u" in ln)


def test_derived_m_max_clamped_to_the_hard_ceiling_says_so(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """A spacer with a tiny ``unit_length`` against a huge span box would
    derive an ``m_max`` in the hundreds of thousands — clamped to
    :data:`~precis_se.compose._HARD_MAX` (200). Nothing in the library
    reaches the box even at the ceiling, so the header says the bound
    IS the ceiling instead of naming an ``m_max`` the caller could never
    actually pass (the same 200 ceiling caps an explicit value too)."""
    _unit(handler, material, store, "sw", delta=3.4, length=0.0)
    _unit(handler, material, store, "tiny", length=0.001)

    body = handler.search(compose={"span": [500, 600], "n_max": 1}).body
    assert (
        "span unreachable at m_max=200 (hard ceiling; max 0.2 nm with "
        "tiny#u) — no m_max within the 200 hard ceiling reaches the "
        "box's span" in body
    )
    assert "pass m_max=" not in body


def test_derived_n_max_accounts_for_pss_scaling(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """A low-PSS switch needs MORE ``n`` to clear the box, not fewer —
    ``delta_eff = n * delta_length * pss`` is what the box is scored
    against (:func:`~precis_se.compose._delta_attr`), so the derivation
    must divide by ``pss`` the same way :func:`~precis_se.compose.
    _delta_reachability_note`'s own suggestion already does. Unscaled,
    ``ceil(9 / 3.4) = 3`` would cap the sweep at n=3 (best 3.06 Å) and
    the box would wrongly read as unreachable; scaled by pss=0.3 the
    true n=8 (8.16 Å) is in range and reachable."""
    _unit(handler, material, store, "lowpss", delta=3.4, pss=0.3, roles=("azide",))
    body = handler.search(compose={"delta": [8, 9]}).body
    assert "unreachable" not in body
    matches = [ln for ln in _row_lines(body) if "✓delta" in ln]
    assert matches, body
    assert "8.16 Å at PSS 30 % short" in matches[0]


# ── R3: lever family — rotary unit + arm units, swing box key ───────────
# docs/backlog/port-rotation-and-lever-composition.md "Slice R3"


def test_lever_row_ranks_a_rotary_unit_plus_an_arm_by_tip_stroke(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _hinge(handler, "hinge", roles=("azide",))
    _unit(handler, material, store, "spacer", length=10.0, roles=("alkyne",))
    # A plain switch chain lives in the SAME library — the merged ranked
    # list (R3 review: "add a switch+spacer so both families produce
    # rows") must show BOTH families and order them by whichever is
    # actually closer to the box.
    _unit(handler, material, store, "azo", delta=200.0)
    spacer_m = 10.0 * 1e-9
    tip_m = 2.0 * (ARM0_M + spacer_m) * math.sin(math.pi / 4.0)
    tip_a = tip_m * 1e10

    # The lever wins: the box brackets the lever's tip stroke, nowhere
    # near azo's 200 Å chain. n_max/m_max=1 keeps the row set small and
    # the ranking unambiguous (no tied-distance combinatorics to reason
    # through).
    body = handler.search(
        compose={"delta": [tip_a - 1.0, tip_a + 1.0], "n_max": 1, "m_max": 1}
    ).body
    lines = _row_lines(body)
    top = lines[0]
    assert "hinge#u + 1 × spacer#u" in top
    assert f"✓delta: {tip_a:g} Å" in top
    assert "arm" in top and "nm (envelope)" in top
    assert "+ 1 × spacer 10 nm (spacer#u)" in top
    assert "family: lever" in top
    assert "; 1 rotary unit(s)" in body
    # The chain still shows — never hidden — ranked behind, as a miss.
    chain_row = next(ln for ln in lines if "1 × azo#u" in ln and "spacer#u" not in ln)
    assert "✗delta" in chain_row
    assert "family: chain" in chain_row
    assert lines.index(chain_row) > 0
    # The Next ops script: rotary + arm, connected at the rotating port.
    assert "{'op': 'instance_block', 'name': 'r1', 'template': 'hinge#u'}" in body
    assert "{'op': 'instance_block', 'name': 'a1', 'template': 'spacer#u'}" in body
    assert "{'op': 'connect', 'a': 'r1.p1', 'b': 'a1.p0'}" in body

    # The chain wins: the box brackets azo's 200 Å, nowhere near any
    # lever's tip stroke over this library's tiny sub-nm envelope.
    body = handler.search(
        compose={"delta": [199.0, 201.0], "n_max": 1, "m_max": 1}
    ).body
    lines = _row_lines(body)
    top = lines[0]
    assert top.startswith("1. 1 × azo#u")
    assert "✓delta: 200 Å" in top
    assert "family: chain" in top
    lever_row = next(ln for ln in lines if "hinge#u + 1 × spacer#u" in ln)
    assert "✗delta" in lever_row
    assert "family: lever" in lever_row
    assert lines.index(lever_row) > 0


def test_swing_box_ranks_a_rotary_series_by_total_angle(handler: SeHandler) -> None:
    _hinge(handler, "hinge_series")
    body = handler.search(compose={"swing": [170, 190]}).body
    top = _row_lines(body)[0]
    assert top.startswith("1. 2 × hinge_series#u")
    assert "✓swing: 180°" in top
    assert "family: series" in top
    # n=1 (90°) and n=3 (270°) both miss the window — never empty/hidden.
    misses = [
        ln for ln in _row_lines(body) if "hinge_series#u" in ln and "✗swing" in ln
    ]
    assert misses


def test_sourced_step_angle_disagreeing_with_the_derived_swing_shows_both(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _hinge(handler, "hinge_disagree", roles=("azide",))
    _unit(handler, material, store, "spacer_d", length=10.0, roles=("alkyne",))
    # 57 % off the derived pi/2 — same fixture pi/2 vs 1.0 rad R2's own
    # kinematics test uses (tests/test_se_kinematics.py).
    _link_step_angle(handler, material, store, "hinge_disagree", "u", 1.0)

    body = handler.search(compose={"delta": [0.0, 1e12]}).body
    top = _row_lines(body)[0]
    assert "hinge_disagree#u" in top
    step_deg = f"{math.degrees(1.0):g}"
    assert f"sourced step_angle {step_deg}° disagrees" in top
    assert "(derived from trans → cis on port p1)" in top


def test_lever_row_with_no_complementary_role_says_joining_none_and_skips_connect(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """The rotating port ``p1`` carries no role at all here — no half
    complements the arm's ``alkyne`` — so :func:`~precis_se.compose.
    _rotating_port_pair` must not fabricate a ``'<port>'`` connect: the
    row still lists (never empty/hidden) with a ``joining: none`` note,
    and its ops script drops the connect for a comment line instead."""
    _hinge(handler, "hinge_bare")  # p1 carries no roles
    _unit(handler, material, store, "spacer_bare", length=10.0, roles=("alkyne",))
    spacer_m = 10.0 * 1e-9
    tip_m = 2.0 * (ARM0_M + spacer_m) * math.sin(math.pi / 4.0)
    tip_a = tip_m * 1e10

    body = handler.search(
        compose={"delta": [tip_a - 1.0, tip_a + 1.0], "n_max": 1, "m_max": 1}
    ).body
    top = _row_lines(body)[0]
    assert "hinge_bare#u + 1 × spacer_bare#u" in top
    assert (
        "joining: none (rotating port p1 has no role complementary to "
        "spacer_bare#u's ports)" in top
    )
    # never a fabricated '<port>' connect — the ops script skips it and
    # leaves a comment instead.
    assert "{'op': 'connect'" not in body
    assert "no connect: rotating port p1" in body


def test_swing_and_delta_together_refused() -> None:
    with pytest.raises(BadInput, match="one stroke measure"):
        se_compose.parse_compose({"delta": [1, 2], "swing": [10, 20]})


def test_declare_transitions_requires_delta_and_swing_together_refused(
    handler: SeHandler,
) -> None:
    """The same refusal, exercised through the WRITE path
    (``declare_transitions … requires=``), not just ``parse_compose``
    directly — ``precis_se/ops.py`` catches ``parse_requires``'s
    ``BadInput`` and re-raises it as an ``OpError``, which
    ``apply_ops_with_atomic`` (the handler's own op-walking layer) then
    re-wraps back into a ``BadInput`` — so the message survives both
    hops unchanged, naming 'one stroke measure' the same way at write
    time as at ``compose=`` read time."""
    ops = [
        {"op": "add_block", "name": "u", "envelope": "sphere:r0.005"},
        {
            "op": "declare_states",
            "block": "u",
            "states": [{"name": "trans"}, {"name": "cis"}],
        },
        {
            "op": "declare_transitions",
            "block": "u",
            "transitions": [
                {
                    "from_state": "trans",
                    "to_state": "cis",
                    "driver_kind": "light",
                    "requires": {"delta": [1, 2], "swing": [10, 20]},
                }
            ],
        },
    ]
    with pytest.raises(BadInput, match="one stroke measure"):
        handler.put(id="bad_requires", text=json.dumps({"ops": ops}))


def test_swing_via_compose_string_form_reads_requires(handler: SeHandler) -> None:
    _hinge(handler, "hinge_requires", requires={"swing": [80, 100]})
    body = handler.search(compose="hinge_requires#u").body
    top = _row_lines(body)[0]
    assert top.startswith("1. 1 × hinge_requires#u")
    assert "✓swing:" in top
    assert "box from se:hinge_requires#u trans->cis (mechanical)" in body


def test_no_rotary_unit_in_the_library_says_nothing_to_compose(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "rod", length=10.0)
    body = handler.search(compose={"swing": [10, 20]}).body
    assert body.startswith("no library block")
    assert "step_angle" in body


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


# ── conditions box key (gr346735) ────────────────────────────────────────


def test_compose_conditions_selects_the_313nm_pss_row_over_a_newer_436nm_one(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    """The gripe's own case: two ``pss_short_fraction`` rows on one
    switch — the newer at 436 nm (~0.1), the older at 313 nm (0.8).
    Without a filter the newest (436 nm) wins; ``conditions=
    {'wavelength_nm': 313}`` picks the older, matching row instead."""
    mat = "mat-pss-cond"
    material.put(id=mat, title=mat)
    material.put(id=mat, property=se_compose.DELTA_KEY, value=3.4, unit="Å")
    material.put(id=mat, property=se_compose.LENGTH_KEY, value=1.0, unit="nm")
    material.put(
        id=mat,
        property=se_compose.PSS_KEY,
        value=0.8,
        conditions={"wavelength_nm": 313},
    )
    material.put(
        id=mat,
        property=se_compose.PSS_KEY,
        value=0.1,
        conditions={"wavelength_nm": 436},
    )
    handler.put(
        id="pss-cond",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "u", "envelope": "sphere:r0.005"}]}
        ),
    )
    design_ref = store.get_ref(kind="se", id="pss-cond")
    mat_ref = store.get_ref(kind="material", id=mat)
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": "u"},
    )

    default_top = _row_lines(
        handler.search(compose={"delta": [3, 4], "n_max": 1}).body
    )[0]
    assert "PSS 10 % short" in default_top  # newest (436 nm) by default

    filtered_top = _row_lines(
        handler.search(
            compose={
                "delta": [3, 4],
                "n_max": 1,
                "conditions": {"wavelength_nm": 313},
            }
        ).body
    )[0]
    assert "PSS 80 % short" in filtered_top
    assert "wavelength_nm=313" in filtered_top


def test_compose_conditions_malformed_rejected(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    _unit(handler, material, store, "azo", delta=3.4, length=1.0)
    with pytest.raises(BadInput):
        handler.search(compose={"delta": [3, 4], "conditions": "nope"})
    with pytest.raises(BadInput):
        handler.search(compose={"delta": [3, 4], "conditions": {}})


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
        ({"delta": [1, 2], "m_max": 999}, "m_max"),
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
