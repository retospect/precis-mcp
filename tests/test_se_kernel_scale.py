"""se geometry at out-of-band scales — the kernel-unit normalization seam
(``precis_se.validate.kernel_scale``).

The cad kernel is unit-agnostic but its tolerances are absolute in the
numbers it is handed (``LINEAR_EPS = 1e-6``): the first real prod se design
(boxel-3nm, nanometre envelopes in metres, 2026-09-09) had every box face
culled as degenerate, making ``contains_local`` vacuously True and
``distance_local`` crash on ``min()`` over no planes — an internal
ValueError out of ``view='clearance'`` and ``view='validate'``. The seam
now normalizes out-of-band designs into O(100) kernel units and converts
results back to metres; in-band designs pass through with scale exactly
1.0 (bit-identical to the historical path).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.cad.primitives import PolyFrustum
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops
from precis_se.validate import envelope_overlaps, kernel_scale

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: The boxel-3nm trigger, minimized: a group parent without an envelope, a
#: nanometre panel, an overlapping vertex hub, a rotated instance, and an
#: array node — every feature the crashing prod design carried.
_BOXEL_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "cage"},
    {
        "op": "add_block",
        "name": "panel",
        "parent": "cage",
        "envelope": "box:w0.000000003d0.000000003h0.0000000003",
        "pose": [0, 0, -1.5e-09],
    },
    {
        "op": "add_block",
        "name": "vtx",
        "parent": "cage",
        "envelope": "box:w0.000000001d0.000000001h0.000000001",
        "pose": [-1e-09, -1e-09, -1.5e-09],
    },
    {
        "op": "instance_block",
        "name": "panel_xm",
        "template": "panel",
        "parent": "cage",
        "pose": [-1.35e-09, 0, -1.5e-09],
        "rot": [0, 90, 0],
    },
    {
        "op": "add_block",
        "name": "site",
        "parent": "panel",
        "envelope": "sphere:r0.0000000002",
        "pose": [-1e-09, 0, 0],
    },
    {
        "op": "array_block",
        "name": "site_row",
        "template": "site",
        "parent": "panel",
        "linear": {"count": 3, "pitch": 1e-09, "axis": [1, 0, 0]},
    },
]


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    h = SeHandler(hub=hub)
    h.put(id="boxel1", text=json.dumps({"ops": _BOXEL_OPS}))
    return h


def _nano_tree() -> SeTree:
    tree = SeTree()
    apply_ops(tree, _BOXEL_OPS)
    return tree


# ── kernel_scale itself ─────────────────────────────────────────────────


def _block(tree: SeTree, name: str):
    return tree.blocks[name]


def test_in_band_designs_scale_exactly_one() -> None:
    """A metre-scale (caster-sized) query passes through unscaled — the
    guarantee that pre-normalization behaviour is bit-identical."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "fork", "envelope": "box:w0.04d0.02h0.08"},
            {
                "op": "add_block",
                "name": "hub",
                "envelope": "cyl:r0.008h0.03",
                "pose": [0, 0, 0.05],
            },
        ],
    )
    scale = kernel_scale(
        ("box:w0.04d0.02h0.08", _block(tree, "fork")),
        ("cyl:r0.008h0.03", _block(tree, "hub")),
    )
    assert scale == 1.0


def test_nanoscale_normalizes_to_kernel_target() -> None:
    tree = _nano_tree()
    scale = kernel_scale(
        ("box:w0.000000003d0.000000003h0.0000000003", _block(tree, "panel")),
        ("box:w0.000000001d0.000000001h0.000000001", _block(tree, "vtx")),
    )
    # keyed off the SMALLEST block (the vtx's 1e-9 span), so a small
    # partner can never be left sub-epsilon by a bigger one.
    assert scale == pytest.approx(100.0 / 1e-09)


def test_mixed_scale_pair_is_refused_not_wrong() -> None:
    """A nano block paired with a macro one can't share one SDF query —
    kernel_scale must say so (None), never hand back a factor that lets
    the check silently return a wrong 'clear'."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "housing", "envelope": "box:w1d1h0.001"},
            {
                "op": "add_block",
                "name": "bolt",
                "envelope": "box:w0.000000002d0.000000002h0.000000002",
                "pose": [0, 0, 0.0005],
            },
        ],
    )
    scale = kernel_scale(
        ("box:w1d1h0.001", _block(tree, "housing")),
        ("box:w0.000000002d0.000000002h0.000000002", _block(tree, "bolt")),
    )
    assert scale is None
    # …and envelope_overlaps reports the pair as cross-scale rather than
    # silently dropping it.
    overlaps, cross = envelope_overlaps(tree)
    assert ("bolt", "housing") in cross or ("housing", "bolt") in cross
    assert not any({a, b} == {"bolt", "housing"} for a, b, _ in overlaps)


def test_planetary_scale_normalizes_down() -> None:
    tree = SeTree()
    apply_ops(
        tree,
        [{"op": "add_block", "name": "tether", "envelope": "cyl:r1h10000000"}],
    )
    scale = kernel_scale(("cyl:r1h10000000", _block(tree, "tether")))
    assert scale == pytest.approx(100.0 / 1e07)


# ── the crashing views, healed ──────────────────────────────────────────


def test_clearance_at_nm_scale_reports_metres(handler: SeHandler) -> None:
    """The exact prod crash: view='clearance' on nanometre envelopes. The
    panel and hub genuinely interpenetrate by ~0.3 nm — the gap must come
    back negative, in metres, at nanometre magnitude (not kernel units)."""
    resp = handler.get(id="boxel1", view="clearance", args={"a": "panel", "b": "vtx"})
    m = re.search(r"gap: (-?[\d.e-]+) m", resp.body)
    assert m, resp.body
    gap = float(m.group(1))
    assert gap == pytest.approx(-3e-10, rel=0.2)
    assert "interference" in resp.body


def test_validate_at_nm_scale_runs_and_finds_the_overlap(
    handler: SeHandler,
) -> None:
    """view='validate' (which rents the same kernel for the undeclared-
    interpenetration check) must not crash — and the panel/vtx overlap,
    unsanctioned by any connect here, is exactly its finding. The design
    also carries genuinely cross-scale pairs (the 0.2 nm site vs the 3 nm
    panel instance, 15×) — those must surface as unverifiable, not
    vanish."""
    resp = handler.get(id="boxel1", view="validate")
    assert "interpenetration" in resp.body
    assert "cross_scale_unverifiable" in resp.body


def test_clearance_view_refuses_cross_scale_pair(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="differ too much in size"):
        handler.get(id="boxel1", view="clearance", args={"a": "site", "b": "panel"})


def test_envelope_overlaps_gap_is_in_metres() -> None:
    tree = _nano_tree()
    overlaps, _cross = envelope_overlaps(tree)
    pairs = {frozenset((a, b)): gap for a, b, gap in overlaps}
    gap = pairs.get(frozenset(("panel", "vtx")))
    assert gap is not None, overlaps
    assert gap == pytest.approx(-3e-10, rel=0.2)


# ── edge coverage: unparseable envelopes, extent, dof probe ─────────────


def test_characteristic_and_scale_degrade_on_unparseable_envelope() -> None:
    """A stored envelope that no longer parses contributes no length —
    the query falls back to scale 1.0 and downstream handling owns it."""
    from precis_se.validate import _characteristic_length

    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "a"}])
    assert _characteristic_length("bogus:xyz") == 0.0
    assert kernel_scale(("bogus:xyz", _block(tree, "a"))) == 1.0


def test_design_extent_at_nano_returns_metres() -> None:
    """_design_extent must survive the nano regime (the construction guard
    would otherwise cull every primitive and silently report 0.0) and
    return the extent in metres, not kernel units."""
    from precis_se.drc import _design_extent

    tree = SeTree()
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "a",
                "envelope": "box:w0.000000003d0.000000003h0.000000003",
            },
            {
                "op": "add_block",
                "name": "b",
                "envelope": "box:w0.000000001d0.000000001h0.000000001",
                "pose": [0, 0, 4e-09],
            },
        ],
    )
    extent = _design_extent(tree)
    assert 1e-09 < extent < 1e-07


def _jointed_pair_ops(
    a_env: str, b_env: str, b_pose: list[float]
) -> list[dict[str, Any]]:
    return [
        {"op": "add_block", "name": "a", "envelope": a_env},
        {"op": "add_block", "name": "b", "envelope": b_env, "pose": b_pose},
        {"op": "add_port", "block": "a", "name": "p"},
        {"op": "add_port", "block": "b", "name": "p"},
        {"op": "connect", "a": "a.p", "b": "b.p"},
        {
            "op": "set_joint",
            "a": "a.p",
            "b": "b.p",
            "joint": {"class": "prismatic", "axis": [0, 0, 1]},
        },
    ]


def test_dof_probe_runs_at_nano_scale() -> None:
    """The axis-travel probe on a nanometre prismatic pair must run (not
    crash, not skip) and report travel in metres."""
    from precis_se import drc as se_drc

    tree = SeTree()
    apply_ops(
        tree,
        _jointed_pair_ops(
            "box:w0.000000003d0.000000003h0.000000001",
            "box:w0.000000001d0.000000001h0.000000001",
            [0, 0, 2e-09],
        ),
    )
    report = se_drc.drc(tree)
    probe = next(p for p in report.dof_probes if p.klass == "prismatic")
    assert "travel" in probe.outcome


def test_dof_probe_skips_cross_scale_pair() -> None:
    from precis_se import drc as se_drc

    tree = SeTree()
    apply_ops(
        tree,
        _jointed_pair_ops(
            "box:w1d1h0.001",
            "box:w0.000000002d0.000000002h0.000000002",
            [0, 0, 0.0005],
        ),
    )
    report = se_drc.drc(tree)
    probe = next(p for p in report.dof_probes if p.klass == "prismatic")
    assert "cross-scale" in probe.outcome


def test_clearance_view_names_the_degenerate_cause() -> None:
    """handler BadInput must carry the WHY: an envelope that parses fine
    in metres but whose scaled solid degenerates (a planetary-aspect box
    whose thin dimension lands sub-epsilon after normalizing down) names
    the degenerate-at-this-scale cause, not a bare 'invalid'."""
    from precis_se.handler import _render_clearance

    tree = SeTree()
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "sliver",
                "envelope": "box:w0.01d0.01h10000000",
            },
            {
                "op": "add_block",
                "name": "cube",
                "envelope": "box:w10000000d10000000h10000000",
                "pose": [0, 0, 0],
            },
        ],
    )
    with pytest.raises(BadInput, match="degenerate at this scale"):
        _render_clearance(tree, {"a": "sliver", "b": "cube"})


# ── kernel defense-in-depth ─────────────────────────────────────────────


def test_degenerate_frustum_fails_loud_at_construction() -> None:
    """A sub-epsilon polytope must raise a legible ValueError when built —
    never a vacuous contains_local plus a min()-over-nothing crash at
    read time."""
    nm = 1e-09
    with pytest.raises(ValueError, match="degenerate below the kernel tolerance"):
        PolyFrustum(
            bottom=[(0.0, 0.0), (nm, 0.0), (nm, nm), (0.0, nm)],
            top=[(0.0, 0.0), (nm, 0.0), (nm, nm), (0.0, nm)],
            h=nm,
        )
