"""The symbolic chain solver with a stub geometry backend (SPEC 22.3,
SPEC 28 step 4; ``hexfold.chain``): parts are abstract records, every
adjacency is an integer equality on rim ``N``, pin either end and
propagation resolves the middle, spacers and free periods are the
slack, and the stub backend answers rims and rough lengths from tables
with no build."""

from __future__ import annotations

import math

import pytest

from hexfold import domains, lattice
from hexfold.chain import (
    MAX_COMBINATIONS,
    ChainError,
    Part,
    Rim,
    StubBackend,
    part_from_payloads,
    solve,
)
from hexfold.report import Severity

_A = math.sqrt(3.0) * 1.42
_PITCH_ARM = _A  # |T| of an (n,n) tube is one lattice constant
_PITCH_ZIG = math.sqrt(3.0) * _A  # |T| of an (n,0) tube
_R55 = _A * math.sqrt(75) / (2 * math.pi)


def _codes(result) -> list[str]:
    return [f.code for f in result.findings]


# ---------- the stub agrees with the real lattice arithmetic ----------


@pytest.mark.parametrize("kind", ["tube", "cap"])
def test_stub_rims_match_domains_rim_n(kind: str) -> None:
    sb = StubBackend()
    for value in domains.CATALOGUES[kind]:
        for port in ("in", "out"):
            rim = sb.rim(kind, value, port)
            assert (None if rim is None else rim.N) == domains.rim_n(kind, value, port)
    assert sb.catalogue(kind) == domains.CATALOGUES[kind]


def test_stub_pitch_and_cap_height_match_lattice() -> None:
    sb = StubBackend()
    lat = lattice.Lattice()
    for value in domains.CATALOGUES["tube"]:
        t = lattice.translation_vector(*value)
        period = lat.a * math.sqrt(lattice._lattice_metric_dot(t, t))
        assert sb.pitch_A("tube", value) == pytest.approx(period)
    assert sb.pitch_A("tube", (5, 5)) == pytest.approx(_PITCH_ARM)
    assert sb.pitch_A("tube", (12, 0)) == pytest.approx(_PITCH_ZIG)
    assert sb.fixed_length_A("cap", (5, 5)) == pytest.approx(lattice.tube_radius(5, 5))
    assert sb.fixed_length_A("cap", (12, 0)) == 0.0
    assert sb.pitch_A("cap", (5, 5)) is None


def test_stub_rim_types_follow_spec_10() -> None:
    sb = StubBackend()
    assert sb.rim("tube", (12, 0), "in") == Rim(12, "z")
    assert sb.rim("tube", (6, 6), "out") == Rim(12, "a")
    assert sb.rim("tube", (8, 4), "in") == Rim(12, None)
    assert sb.rim("cap", (5, 5), "in") == Rim(10, "a")
    assert sb.rim("cap", (12, 0), "in") == Rim(12, "z")
    assert sb.rim("cap", (5, 5), "out") is None
    assert sb.rim("cap", (7, 0), "in") is None


# ---------- rims: pin either end, propagation resolves the middle ----------


def test_pinned_top_cap_resolves_the_tube_and_the_bottom_cap() -> None:
    res = solve(
        [
            Part("a", "cap"),
            Part("t", "tube", domain=((5, 5), (6, 6)), periods=3),
            Part("b", "cap", value=(5, 5)),
        ]
    )
    assert res.ok
    assert res.after == {"t": ((5, 5),), "a": ((5, 5),)}
    assert res.pruned_by == {"t": ["t.out == b.in"], "a": ["a.in == t.in"]}
    assert _codes(res) == ["chain.propagated", "chain.propagated", "chain.solved"]
    best = res.best
    assert best is not None
    assert [a.value for a in best.assignments] == [(5, 5), (5, 5), (5, 5)]
    # the bottom cap was written first, so its one rim faces up
    assert best.assignments[0].bottom is None
    assert best.assignments[0].top == Rim(10, "a")
    assert best.assignments[-1].top is None
    assert best.total_A == pytest.approx(2 * _R55 + 3 * _PITCH_ARM)
    assert best.deviation_A == 0.0


def test_pinned_ends_rank_same_type_seams_before_adapters() -> None:
    res = solve([Part("t", "tube", periods=2)], bottom=Rim(12, "z"), top=12)
    assert res.ok
    assert [(s.assignments[0].value, len(s.adapters)) for s in res.solutions] == [
        ((12, 0), 0),
        ((6, 6), 1),
    ]
    # the adapter is against the typed pinned end; a bare N pins no type
    (adapter,) = res.solutions[1].adapters
    assert adapter[:2] == ("chain.bottom", "t")


def test_emptied_domain_is_chain_unsolvable_with_needs_and_offers() -> None:
    res = solve(
        [
            Part("a", "cap", value=(5, 5)),
            Part("t", "tube", domain=((6, 6), (7, 7)), periods=1),
        ]
    )
    assert not res.ok
    assert res.conflict is not None
    assert res.conflict.part == "t"
    assert res.conflict.needs == (10,)
    assert res.conflict.offers == (12, 14)
    assert res.conflict.constraint == "a.in == t.in"
    (finding,) = res.findings
    assert finding.code == "chain.unsolvable"
    assert finding.severity == Severity.ERROR
    assert (
        "needs N in [10]" in finding.message and "offered [12, 14]" in finding.message
    )
    assert res.solutions == []


def test_two_pinned_rims_that_differ_are_a_mismatch() -> None:
    res = solve(
        [Part("a", "cap", value=(5, 5)), Part("t", "tube", value=(6, 6), periods=1)]
    )
    assert not res.ok
    assert _codes(res) == ["chain.mismatch"]
    assert "10 != 12" in res.findings[0].message


def test_a_cap_in_the_middle_has_no_rim_on_one_side() -> None:
    res = solve(
        [
            Part("a", "cap", value=(5, 5)),
            Part("m", "cap", value=(5, 5)),
            Part("t", "tube", value=(5, 5), periods=1),
        ]
    )
    assert not res.ok
    assert _codes(res) == ["chain.no_rim"]
    assert res.findings[0].where == "m"


def test_two_dont_care_tubes_are_too_many_combinations() -> None:
    res = solve([Part("t1", "tube", periods=1), Part("t2", "tube", periods=1)])
    assert not res.ok
    assert _codes(res) == ["chain.too_many"]
    assert dict(res.findings[0].data)["combinations"] == 64 * 64 > MAX_COMBINATIONS
    # the propagated domains are still reported
    assert len(res.after["t1"]) == 64


def test_adapter_between_parts_is_reported_on_the_best_solution() -> None:
    res = solve(
        [
            Part("z", "tube", value=(12, 0), periods=1),
            Part("a", "tube", value=(6, 6), periods=1),
        ]
    )
    assert res.ok
    assert _codes(res) == ["chain.adapter", "chain.solved"]
    assert "z12 onto a12" in res.findings[0].message
    assert res.findings[0].where == "a"


# ---------- lengths: free periods and spacers absorb the wish ----------


def test_free_periods_land_nearest_the_wish() -> None:
    res = solve(
        [
            Part("a", "cap", value=(5, 5)),
            Part("t", "tube", value=(5, 5)),
            Part("b", "cap", value=(5, 5)),
        ],
        wish_A=(30.0, 2.0),
    )
    assert res.ok
    best = res.best
    assert best is not None
    tube = best.assignments[1]
    assert tube.periods == 9  # 2R + 9|T| = 28.9 A, nearer 30 than 10 periods
    assert best.total_A == pytest.approx(2 * _R55 + 9 * _PITCH_ARM)
    assert abs(best.deviation_A) <= 2.0
    assert "len=9" in res.findings[-1].message


def test_spacer_takes_the_slack_exactly() -> None:
    parts = [
        Part("t1", "tube", value=(5, 5), periods=2),
        Part("s", "spacer", length_A=(5.0, 10.0)),
        Part("t2", "tube", value=(5, 5), periods=2),
    ]
    res = solve(parts, wish_A=(17.0, 0.5))
    assert res.ok
    best = res.best
    assert best is not None
    spacer = best.assignments[1]
    assert spacer.length_A == pytest.approx(17.0 - 4 * _PITCH_ARM)
    assert best.total_A == pytest.approx(17.0)
    assert best.deviation_A == pytest.approx(0.0)
    # a spacer is transparent to the rim pass
    assert spacer.bottom is None and spacer.top is None


def test_unreachable_wish_is_chain_length_with_the_nearest_total() -> None:
    parts = [
        Part("t1", "tube", value=(5, 5), periods=2),
        Part("s", "spacer", length_A=(5.0, 10.0)),
        Part("t2", "tube", value=(5, 5), periods=2),
    ]
    res = solve(parts, wish_A=(100.0, 1.0))
    assert not res.ok
    (finding,) = res.findings
    assert finding.code == "chain.length"
    assert finding.severity == Severity.ERROR
    nearest = dict(finding.data)["nearest"]
    assert nearest["total_A"] == pytest.approx(4 * _PITCH_ARM + 10.0, abs=1e-3)
    assert nearest["assignments"][1]["length_A"] == 10.0
    assert "spacer" in finding.fix


def test_no_wish_means_minimum_periods_and_no_deviation() -> None:
    res = solve([Part("t", "tube", value=(5, 5), periods_range=(3, 7))])
    assert res.ok
    best = res.best
    assert best is not None
    assert best.assignments[0].periods == 3
    assert best.deviation_A == 0.0


# ---------- the interface: a resolved block is a pinned part ----------


def test_block_from_port_payloads_pins_its_neighbour() -> None:
    blk = part_from_payloads("blk", {"kind": "rim", "N": 10, "type": "a10"}, None, 7.0)
    assert blk.kind == "block"
    assert blk.rims == {"in": Rim(10, "a")}
    res = solve([Part("t", "tube", periods=2), blk])
    assert res.ok
    assert [(s.assignments[0].value, len(s.adapters)) for s in res.solutions] == [
        ((5, 5), 0),
        ((10, 0), 1),
    ]
    best = res.best
    assert best is not None
    assert best.total_A == pytest.approx(2 * _PITCH_ARM + 7.0)
    # the block was written last, so its one rim faces down
    assert best.assignments[1].bottom == Rim(10, "a")
    assert best.assignments[1].top is None


def test_rim_from_payload_rejects_a_non_rim_payload() -> None:
    with pytest.raises(ChainError, match="not a rim payload"):
        Rim.from_payload({"kind": "facet", "hkl": [1, 1, 1]})
    assert Rim.from_payload({"kind": "rim", "N": 12, "type": None}) == Rim(12, None)


# ---------- malformed chains are ChainError, never findings ----------


@pytest.mark.parametrize(
    ("parts", "match"),
    [
        ([], "empty chain"),
        (
            [Part("s", "spacer", length_A=(1.0, 2.0)), Part("t", "tube", value=(5, 5))],
            "end",
        ),
        (
            [
                Part("t", "tube", value=(5, 5)),
                Part("s", "spacer", length_A=(1.0, 2.0)),
                Part("u", "spacer", length_A=(1.0, 2.0)),
                Part("v", "tube", value=(5, 5)),
            ],
            "adjacent spacers",
        ),
        (
            [Part("t", "tube", value=(5, 5)), Part("t", "tube", value=(5, 5))],
            "duplicate",
        ),
        ([Part("b", "block", rims={"in": Rim(10, "a")})], "fixed length_A"),
        ([Part("x", "widget")], "unknown kind"),
        ([Part("t", "tube", value=(5, 5), periods_range=(0, 3))], "empty"),
        ([Part("t", "tube", domain=())], "empty domain"),
    ],
)
def test_malformed_chains_raise(parts: list[Part], match: str) -> None:
    with pytest.raises(ChainError, match=match):
        solve(parts)


def test_result_to_dict_is_json_shaped() -> None:
    res = solve([Part("a", "cap"), Part("t", "tube", value=(5, 5), periods=1)])
    d = res.to_dict()
    assert d["ok"] is True
    assert d["domains"] == {"a": [[5, 5]]}
    assert d["solutions"][0]["assignments"][0] == {
        "part": "a",
        "value": [5, 5],
        "periods": None,
        "length_A": round(_R55, 4),
        "bottom": None,
        "top": "a10",
    }
    assert {f["code"] for f in d["findings"]} == {"chain.propagated", "chain.solved"}
