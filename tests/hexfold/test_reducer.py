"""``wide.out --fuse{reducer}--> narrow.in``: two coaxial tube ends of
different size joined through a flat annulus washer, a coaxial ring of
pentagons + heptagons (menus.reducer_washer / _expand_reducer)."""

from __future__ import annotations

import json

import pytest

from hexfold.build import build
from hexfold.canon import canonical_json, text_of
from hexfold.check import check
from hexfold.report import BuildError
from hexfold.text import parse, to_text


def _spec(wide: str, narrow: str, *, ln: int = 3) -> str:
    return (
        "hexfold 0.2\norigin wide\n"
        f"wide: tube({wide}, len={ln})\n"
        f"narrow: tube({narrow}, len={ln})\n"
        "wide.out --fuse{reducer}--> narrow.in\n"
    )


_BASE = _spec("12,12", "6,6")


def _find(net, code: str) -> list:
    return [f for f in net.report.findings if f.code == code]


def _errors(net) -> list:
    return [f for f in net.report.findings if f.severity.name == "ERROR"]


def test_12_12_to_6_6_builds_clean() -> None:
    net = build(_BASE)
    assert _errors(net) == []
    (f,) = _find(net, "reducer.rings")
    d = dict(f.data)
    assert d["pentagons"] == d["heptagons"] == 12
    assert d["other"] == []


def test_12_12_to_6_6_is_one_component() -> None:
    (f,) = [f for f in check(_BASE).findings if f.code == "net.components"]
    assert f.severity.name == "INFO"
    assert dict(f.data)["n"] == 1


def test_seam_rings_only_5_6_7() -> None:
    net = build(_BASE)
    seams = _find(net, "seam.rings")
    assert seams
    for f in seams:
        assert set(dict(f.data)["rings"]) <= {5, 6, 7}


def test_euler_residual_zero() -> None:
    (f,) = [f for f in check(_BASE).findings if f.code == "euler.residual"]
    assert f.severity.name == "INFO"
    assert f.message == "open net residual 0"


@pytest.mark.parametrize(
    ("wide", "narrow", "n"),
    [("18,18", "6,6", 18), ("24,0", "12,0", 6)],
    ids=["armchair-18-to-6", "zigzag-24-to-12"],
)
def test_other_ratios_balance(wide: str, narrow: str, n: int) -> None:
    net = build(_spec(wide, narrow))
    assert _errors(net) == []
    (f,) = _find(net, "reducer.rings")
    d = dict(f.data)
    assert d["pentagons"] == d["heptagons"] == n
    assert d["other"] == []


@pytest.mark.parametrize(
    ("wide", "narrow"),
    [
        ("15,15", "9,9"),  # n % 6 != 0
        ("12,12", "12,12"),  # same size
        ("6,6", "6,6"),
        ("12,12", "12,0"),  # mixed armchair / zigzag
    ],
    ids=["n-not-multiple-of-6", "same-12", "same-6", "mixed-chirality"],
)
def test_bad_ends_refused_with_reducer_ends(wide: str, narrow: str) -> None:
    with pytest.raises(BuildError) as ei:
        build(_spec(wide, narrow))
    assert "reducer.ends" in {f.code for f in ei.value.report.findings}


def test_text_round_trip_keeps_authored_connect_only() -> None:
    spec = parse(_BASE)
    out = to_text(spec)
    assert "--fuse{reducer}-->" in out
    assert out.count("--fuse") == 1  # no generated washer/fuse line emitted
    assert "cap(" not in out and "hex(" not in out
    again = parse(out)
    assert to_text(again) == out
    assert len(again.connects) == len(spec.connects) == 1
    assert again.connects[0].src == spec.connects[0].src
    assert again.connects[0].dst == spec.connects[0].dst


def test_canonical_json_round_trip_rebuilds_same_net() -> None:
    j1 = canonical_json(_BASE)
    assert json.loads(j1)
    t1 = text_of(j1)
    assert "reducer" in t1
    assert canonical_json(t1) == j1
    a, b = build(_BASE), build(t1)
    assert len(a.atoms) == len(b.atoms)
    assert len(a.bonds) == len(b.bonds)
    assert _errors(b) == []
