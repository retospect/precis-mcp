"""The step-4 interface claim across the se boundary (SPEC 28.4,
``docs/backlog/hexfold-integration.md`` ruling step 4): a block the
hexfold generator resolved becomes a pinned chain part from nothing but
its typed port payloads and its length anchor, and the chain solver
composes it with stub parts through the ports alone -- no geometry, no
second build."""

from __future__ import annotations

import pytest

from hexfold.chain import Part, Rim, part_from_payloads, solve
from precis_se.atomic.generators import GENERATORS

_TUBE = """\
hexfold 0.1

lattice: element=C sigma=1.42

origin post
post: tube(5,5,len=4)
"""


def test_generated_block_is_a_pinned_chain_part_through_its_ports() -> None:
    block = GENERATORS["hexfold"]({"spec": _TUBE})
    ports = {p.name: p for p in block.ports}
    assert set(ports) == {"in", "out"}
    assert ports["in"].lattice == "sp2-hex"
    (length,) = [m for m in block.measures if m.name == "post_len"]

    part = part_from_payloads(
        "post", ports["in"].payload, ports["out"].payload, length.value_A
    )
    assert part.rims == {"in": Rim(10, "a"), "out": Rim(10, "a")}

    # don't-care caps at both ends: the block's rims pin them
    res = solve([Part("bottom", "cap"), part, Part("top", "cap")])
    assert res.ok
    assert res.after == {"bottom": ((5, 5),), "top": ((5, 5),)}
    best = res.best
    assert best is not None
    assert [a.value for a in best.assignments] == [(5, 5), None, (5, 5)]
    cap_height = 2 * best.assignments[0].length_A
    assert best.total_A == pytest.approx(length.value_A + cap_height)
    assert best.adapters == ()


def test_generated_block_against_a_zigzag_wish_is_the_adapter_not_a_refusal() -> None:
    block = GENERATORS["hexfold"]({"spec": _TUBE})
    ports = {p.name: p for p in block.ports}
    (length,) = [m for m in block.measures if m.name == "post_len"]
    part = part_from_payloads(
        "post", ports["in"].payload, ports["out"].payload, length.value_A
    )
    # a zigzag neighbour of the same N joins through the 30° adapter
    res = solve([part, Part("next", "tube", domain=((10, 0), (5, 5)), periods=2)])
    assert res.ok
    assert [(s.assignments[1].value, len(s.adapters)) for s in res.solutions] == [
        ((5, 5), 0),
        ((10, 0), 1),
    ]
