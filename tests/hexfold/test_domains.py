"""``fit`` on domain sets and chain propagation from the pinned ends
(SPEC 12.1 0.2, SPEC 22.3; ``hexfold.domains``): a domain is a set of
roll-ups, every fuse/seam is an integer equality on rim ``N``, arc
consistency from the pinned ends resolves the middle, the survivors are
built and ranked, and an emptied domain is ``fit.unsolvable`` naming the
constraint that emptied it."""

from __future__ import annotations

import pytest

from hexfold.build import build
from hexfold.domains import (
    CATALOGUES,
    DomainError,
    domain_of,
    propagate,
    rim_n,
    set_value,
)
from hexfold.report import BuildError
from hexfold.text import parse, to_text

_CHAIN = """hexfold 0.2
a: cap(fit)
t: tube(fit in {(5,5),(6,6)}, len=3)
b: cap(5,5)
a.in --fuse k=0--> t.in
t.out --fuse k=0--> b.in
"""

_PINNED_TUBE = """hexfold 0.2
t: tube(6,0, len=3)
c: cap(fit)
t.out --fuse k=0--> c.in
"""

_CONFLICT = """hexfold 0.2
a: tube(6,0, len=3)
c: cap(fit)
b: tube(12,0, len=3)
a.out --fuse k=0--> c.in
c.in --fuse k=0--> b.in
"""

_BARE = """hexfold 0.2
a: tube(12,0, len=3)
t: tube(fit, len=3)
a.out --fuse k=0--> t.in
"""


def _codes(net) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in net.report.findings:
        out.setdefault(f.code, []).append(f)
    return out


# ---------- syntax ----------


def test_domain_set_literal_parses_as_one_argument_and_round_trips() -> None:
    spec = parse(_CHAIN)
    t = spec.instance("t")
    assert t is not None
    assert t.params == (("0", "fit in {(5,5),(6,6)}"), ("len", "3"))
    assert domain_of(t) == ((5, 5), (6, 6))
    a = spec.instance("a")
    assert a is not None
    assert domain_of(a) == CATALOGUES["cap"]
    b = spec.instance("b")
    assert b is not None
    assert domain_of(b) is None
    again = parse(to_text(spec))
    assert [(i.name, i.params) for i in again.instances] == [
        (i.name, i.params) for i in spec.instances
    ]


def test_catalogues_are_the_pure_rim_types_and_the_cap_family() -> None:
    assert len(CATALOGUES["tube"]) == 64
    assert all(m in (0, n) for n, m in CATALOGUES["tube"])
    assert CATALOGUES["cap"][0] == (5, 5)
    assert all(n % 6 == 0 and m == 0 for n, m in CATALOGUES["cap"][1:])


def test_rim_n_is_arithmetic() -> None:
    assert rim_n("tube", (5, 5), "in") == 10
    assert rim_n("tube", (7, 3), "out") == 10
    assert rim_n("tube", (5, 5), "hole") is None
    assert rim_n("cap", (5, 5), "in") == 10
    assert rim_n("cap", (12, 0), "in") == 12
    assert rim_n("cap", (7, 0), "in") is None  # not a lid
    assert rim_n("sheet", (1, 1), "rim") is None


def test_domain_on_a_sheet_is_refused_loudly() -> None:
    spec = parse("hexfold 0.2\ns: sheet(fit, 10)\n")
    with pytest.raises(DomainError, match="tube, cap"):
        domain_of(spec.instances[0])
    with pytest.raises(BuildError) as ei:
        build(spec)
    assert [f.code for f in ei.value.report.errors()] == ["fit.unsolvable"]


def test_set_value_replaces_the_domain_keeping_other_params() -> None:
    spec = set_value(parse(_CHAIN), "t", (6, 6))
    t = spec.instance("t")
    assert t is not None
    assert t.params == (("0", "6"), ("1", "6"), ("len", "3"))


# ---------- propagation ----------


def test_propagation_flows_both_directions_along_the_chain() -> None:
    """Pinned ``b`` (N=10) prunes ``t`` to (5,5); ``t`` then prunes ``a``
    to the one cap with N=10 -- the middle resolved from one pinned end
    through two hops."""
    prop = propagate(parse(_CHAIN), probe=lambda s: build(s, strict=False))
    assert prop.conflict is None
    assert prop.after["t"] == ((5, 5),)
    assert prop.after["a"] == ((5, 5),)
    assert prop.pruned_by["t"] == ["fuse t.out -> b.in"]
    assert prop.pruned_by["a"] == ["fuse a.in -> t.in"]
    assert prop.combinations == 1
    assert prop.unpinned == []


def test_propagation_without_a_probe_lists_unpinned_equalities() -> None:
    prop = propagate(parse(_PINNED_TUBE), probe=None)
    assert prop.after["c"] == CATALOGUES["cap"]
    assert prop.unpinned == ["fuse t.out -> c.in"]


def test_build_applies_the_propagated_value_and_reports_it() -> None:
    net = build(_PINNED_TUBE)
    c = net.spec.instance("c")
    assert c is not None
    assert dict(c.params) == {"0": "6", "1": "0"}
    codes = _codes(net)
    (prop,) = codes["fit.propagated"]
    d = dict(prop.data)
    assert d["before"] == [list(v) for v in CATALOGUES["cap"]]
    assert d["after"] == [[6, 0]]
    assert d["pruned_by"] == ["fuse t.out -> c.in"]
    (alts,) = codes["fit.alternatives"]
    da = dict(alts.data)
    assert da["param"] == "domain"
    assert da["applied"] == {"c": [6, 0]}
    assert da["alternatives"] == []
    # the lid on a zigzag (6,0) tube: six pentagons as seam rings
    assert dict(codes["seam.rings"][0].data)["rings"] == {5: 6}


def test_chain_builds_clean_and_resolves_every_domain() -> None:
    net = build(_CHAIN)
    assert net.report.ok
    resolved = {i.name: dict(i.params) for i in net.spec.instances}
    assert resolved["a"] == {"0": "5", "1": "5"}
    assert resolved["t"] == {"0": "5", "1": "5", "len": "3"}
    assert len(_codes(net)["fit.propagated"]) == 2
    assert len(net.atoms) == 30 + 100 + 30


def test_conflict_names_the_constraint_needs_and_offers() -> None:
    with pytest.raises(BuildError) as ei:
        build(_CONFLICT)
    report = ei.value.report
    (err,) = report.errors()
    assert err.code == "fit.unsolvable"
    d = dict(err.data)
    assert d["instance"] == "c"
    assert d["constraint"] == "fuse c.in -> b.in"
    assert d["needs"] == [12]
    assert d["offers"] == [6]  # what survived the first fuse
    assert err.span == (6, 1)
    # the propagation trace is still reported next to the refusal
    assert any(f.code == "fit.propagated" for f in report.findings)


def test_bare_fit_enumerates_and_ranks_the_catalogue_survivors() -> None:
    """``tube(fit)`` next to a pinned (12,0): the catalogue leaves (12,0)
    and (6,6) (both N=12); both build, the all-hexagon seam ranks first,
    the 30° adapter (5-7 rings) is the alternative."""
    net = build(_BARE)
    assert net.report.ok
    t = net.spec.instance("t")
    assert t is not None
    assert dict(t.params) == {"0": "12", "1": "0", "len": "3"}
    codes = _codes(net)
    d = dict(codes["fit.propagated"][0].data)
    assert d["after"] == [[12, 0], [6, 6]]
    da = dict(codes["fit.alternatives"][0].data)
    assert da["applied"] == {"t": [12, 0]}
    assert [a["value"] for a in da["alternatives"]] == [{"t": [6, 6]}]
    # the adapter's seam has 7-rings: cost (2) = 7 > the winner's 6
    assert da["alternatives"][0]["cost"][0] == 7.0


def test_domain_fit_composes_with_len_fit() -> None:
    """Domains resolve outside ``len=fit``: each surviving roll-up is a
    full spec whose own footprint solve runs inside; the winner carries
    both ``fit.alternatives`` families."""
    net = build(
        """hexfold 0.2
a: tube(20,0, len=3)
t: tube(fit in {(10,10),(20,0)}, len=fit) - hexagon@(7,0,A):0
a.in --fuse k=0--> t.out
"""
    )
    assert net.report.ok
    codes = _codes(net)
    assert dict(codes["fit.propagated"][0].data)["after"] == [[10, 10], [20, 0]]
    t = net.spec.instance("t")
    assert t is not None
    p = dict(t.params)
    assert (p["0"], p["1"]) in (("10", "10"), ("20", "0"))
    assert p["len"].isdigit() and int(p["len"]) >= 1
    params = {dict(f.data)["param"] for f in codes["fit.alternatives"]}
    assert params == {"domain", "len"}
