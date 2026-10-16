"""A tube-to-tube ``fuse{reducer}`` through the se generator: terminated,
geometry-judged, no clashes or out-of-band bonds."""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from precis_se.atomic.generators.hexfold_spec import build_hexfold

_SPEC = (
    "hexfold 0.2\norigin wide\n"
    "wide: tube(12,12, len=3)\nnarrow: tube(6,6, len=3)\n"
    "wide.out --fuse{reducer}--> narrow.in\n"
)


def _findings(block: Any) -> list[dict[str, Any]]:
    return block.topology["report"]["findings"]


def test_reducer_builds_through_se_without_geometry_findings() -> None:
    findings = _findings(build_hexfold({"spec": _SPEC}))
    codes = {f["code"] for f in findings}
    assert not [f for f in findings if f["severity"] == "ERROR"]
    for bad in (
        "geom.clash",
        "geom.contact.short",
        "termination.uncovered",
        "terminate.clash",
    ):
        assert bad not in codes
    (rings,) = [f for f in findings if f["code"] == "reducer.rings"]
    assert rings["data"]["pentagons"] == rings["data"]["heptagons"] == 12


def test_reducer_bond_and_angle_within_validator_bands() -> None:
    # geom.summary's n_over counts bonds / angles past the stick judge's own
    # tolerance; the clash and seed counters must be zero as well
    findings = _findings(build_hexfold({"spec": _SPEC}))
    (s,) = [f for f in findings if f["code"] == "geom.summary"]
    d = s["data"]
    assert d["bond_n_over"] == 0 and d["angle_n_over"] == 0
    assert d["clash_count"] == 0 and d["seed_clash_count"] == 0
    assert d["seed_short_bond_count"] == 0


def test_reducer_h_termination_closed_and_no_hh_clash() -> None:
    block = build_hexfold({"spec": _SPEC})
    term = block.topology["terminated"]
    assert term["clashes"] == 0 and term["dropped"] == 0
    # both far rims capped: 12*2 + 6*2 = 36 dangling atoms
    assert block.elements.count("H") == term["count"] == 36
    pos = np.asarray(block.coords)
    h = [i for i, e in enumerate(block.elements) if e == "H"]
    hh = min(
        float(np.linalg.norm(pos[i] - pos[j])) for i, j in itertools.combinations(h, 2)
    )
    assert hh > 1.5  # a bonded H2 would be 0.74 A
