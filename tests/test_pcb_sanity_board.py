"""PCB sanity board — the acceptance board of
``docs/backlog/pcb-unified-place-route.md``. Board and variants:
``tests/fixtures/pcb/sanity_board.py``.

Pure in-memory (no store, no DB, no MCP): the fixture builds the IR the
route job would build; ``realize`` and the routed DRC run on it directly.

- Pinned: four fixed 10-pin headers, 20 two-pin nets, four routable layers
  (straight 1:1, top-to-bottom permuted at seeds 1-3, both permuted). Each
  must route 20/20 with 0 DRC errors — 100 % is the floor, no tolerance.
Iterate without the gate queue (prints routed/20, DRC by rule, per-net
verdicts)::

    uv run python -m tests.fixtures.pcb.sanity_board [variant ...]
"""

from __future__ import annotations

import pytest

from tests.fixtures.pcb import sanity_board as sb

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


@pytest.mark.parametrize("name", sb.PINNED)
def test_sanity_board_routes_every_net_with_zero_drc_errors(name: str) -> None:
    result = sb.route(sb.SPECS[name])
    assert result.nets == NETS
    assert (result.routed, result.failed) == (NETS, {}), (
        f"{name}: routed {result.routed}/{result.nets}, failed {result.failed}"
    )
    assert not result.errors_by_rule, (
        f"{name}: DRC errors {result.errors_by_rule}; {list(result.findings)}"
    )
