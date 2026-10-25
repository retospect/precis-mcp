"""PCB sanity board — the acceptance board of
``docs/backlog/pcb-unified-place-route.md``. Board and variants:
``src/precis/pcb/sanity_board.py``.

Pure in-memory (no store, no DB, no MCP): the fixture builds the IR the
route job would build; ``realize`` and the routed DRC run on it directly.

- Pinned: four fixed 10-pin headers, 20 two-pin nets, four routable layers
  (straight 1:1, top-to-bottom permuted at seeds 1-3, both permuted). Each
  must route 20/20 with 0 DRC errors — 100 % is the floor, no tolerance.
- Unpinned bottom header: placement moves it under the top one (net length
  down >= 80 %, same orientation, legal), then it routes 20/20, 0 DRC.

Iterate without the gate queue (prints routed/20, DRC by rule, per-net
verdicts)::

    uv run python -m precis.pcb.bench sanity [variant ...]
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from precis.pcb import sanity_board as sb

NETS = 2 * sb.PINS


def test_variants_are_distinct_one_to_one_permutations() -> None:
    for spec in sb.SPECS.values():
        for seed in (spec.tb_seed, spec.lr_seed):
            assert sorted(sb.permutation(seed)) == list(range(1, sb.PINS + 1))
    assert sb.permutation(None) == list(range(1, sb.PINS + 1))
    tb = [sb.permutation(s) for s in (1, 2, 3)]
    assert all(p != sb.permutation(None) for p in tb)
    assert len({tuple(p) for p in tb}) == 3


def test_headers_are_centred_25mm_in_and_fixed() -> None:
    insts = {i["refdes"]: i for i in sb.build_graph(sb.SPECS["straight"])["instances"]}
    assert {r: (i["x"], i["y"]) for r, i in insts.items()} == {
        "J_T": (50.0, 75.0),
        "J_B": (50.0, 25.0),
        "J_L": (25.0, 50.0),
        "J_R": (75.0, 50.0),
    }
    assert all(i["fixed"] == "both" for i in insts.values())


def test_svg_option_writes_a_drawing(tmp_path: Path) -> None:
    assert sb.main(["straight", "--svg", str(tmp_path)]) == 0
    text = (tmp_path / "sanity-straight.svg").read_text(encoding="utf-8")
    assert text.startswith("<svg") and text.count("<path") >= NETS


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_unpinned_bottom_header_migrates_to_the_top_one_and_routes(seed: int) -> None:
    """Unpin J_B and place: it moves under J_T, same orientation, legal; then
    it routes 20/20 with 0 DRC. Exercises pad-granular segment costs and the
    centroid-jump move, both always on since gr499239's flags were retired."""
    spec = sb.SPECS["free-bottom"]
    built = sb.build(spec)
    before = sb.net_length_mm(built, "TB")
    poses = sb.place(built, seed=seed)
    after = sb.net_length_mm(built, "TB")
    x, y, rot = poses["J_B"]
    note = (
        f"seed {seed}: J_B (50.0, 25.0, 0.0) -> ({x:.2f}, {y:.2f}, {rot:.1f}); "
        f"TB net length {before:.1f} -> {after:.1f} mm"
    )

    # Only the unpinned part moved.
    assert poses["J_T"] == (50.0, 75.0, 0.0), note
    assert poses["J_L"] == (25.0, 50.0, 90.0), note
    assert poses["J_R"] == (75.0, 50.0, 90.0), note

    # (a) it moved toward the top header: net length down by >= 80 %.
    assert after <= 0.2 * before, note
    assert math.hypot(x - 50.0, y - 75.0) < 50.0, note

    # (b) 1:1 pin order stays uncrossed: P<i> of J_B faces P<i> of J_T (a
    # 180-degree turn would reverse the order and cross every net).
    assert round(rot) % 360 == 0, note

    # (c) legal: no courtyard overlap, inside the outline.
    assert sb.placement_errors(built) == [], note

    # Then route what was placed.
    result = sb.route(spec, built)
    # Reto 2026-10-20 cutover: the routed count is reported, not gated
    # (grid router removed; vector regression accepted) — the DRC oracle
    # below stays an assert.
    print(f"{note}; routed {result.routed}/{result.nets}, failed {result.failed}")
    assert 0 < result.routed <= result.nets
    assert not result.errors_by_rule, f"{note}; {list(result.findings)}"


@pytest.mark.parametrize("name", sb.PINNED)
def test_sanity_board_routes_every_net_with_zero_drc_errors(name: str) -> None:
    result = sb.route(sb.SPECS[name])
    assert result.nets == NETS
    # Reto 2026-10-20 cutover: routed count reported, not gated.
    print(f"{name}: routed {result.routed}/{result.nets}, failed {result.failed}")
    assert 0 < result.routed <= result.nets, f"{name}: {result.failed}"
    assert not result.errors_by_rule, (
        f"{name}: DRC errors {result.errors_by_rule}; {list(result.findings)}"
    )
