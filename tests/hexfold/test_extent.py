"""Angstrom sheet extents snap to whole cells and are *reported*
(``extent.snap``, SPEC 7); ``measures(net)`` returns the block's length
anchors with the snap cell as band (``hexfold.extent``)."""

from __future__ import annotations

import math

import pytest

from hexfold.build import build
from hexfold.canon import content_hash
from hexfold.extent import measures, parse_length_A, snap_cells
from hexfold.lattice import Lattice

_A = Lattice().a  # sqrt(3) * 1.42


def test_parse_length_accepts_both_angstrom_spellings_only() -> None:
    assert parse_length_A("24.6A") == 24.6
    assert parse_length_A(" 24.6 Å ") == 24.6
    assert parse_length_A("12") is None
    assert parse_length_A("fit") is None
    assert parse_length_A("24.6nm") is None


def test_snap_cells_rounds_to_nearest_and_never_below_one() -> None:
    assert snap_cells(24.6, _A) == 10
    assert snap_cells(0.1, _A) == 1
    assert snap_cells(1.5 * _A, _A) == 2


def test_sheet_extent_in_angstrom_snaps_and_reports() -> None:
    net = build("hexfold 0.2\ns: sheet(25A, 25Å)\n")
    s = net.spec.instance("s")
    assert s is not None
    assert dict(s.params) == {"0": "10", "1": "10"}
    snaps = sorted(
        (f for f in net.report.findings if f.code == "extent.snap"),
        key=lambda f: dict(f.data)["param"],
    )
    assert [dict(f.data)["param"] for f in snaps] == ["H", "W"]
    d = dict(snaps[1].data)
    assert d["requested_A"] == 25.0
    assert d["cells"] == 10
    assert d["realised_A"] == pytest.approx(10 * _A, abs=1e-3)
    assert d["delta_A"] == pytest.approx(10 * _A - 25.0, abs=1e-3)
    assert d["period_A"] == pytest.approx(_A, abs=1e-3)
    assert snaps[1].where == "s"
    assert len(net.atoms) == 200  # the same net sheet(10,10) builds


def test_exact_extent_has_zero_delta_and_named_keys_work() -> None:
    net = build(f"hexfold 0.2\ns: sheet(W={10 * _A:.4f}A, H=12)\n")
    (snap,) = [f for f in net.report.findings if f.code == "extent.snap"]
    assert dict(snap.data)["delta_A"] == pytest.approx(0.0, abs=1e-3)
    s = net.spec.instance("s")
    assert s is not None
    assert dict(s.params) == {"W": "10", "H": "12"}


def test_snap_findings_survive_a_len_fit_solve() -> None:
    net = build(
        """hexfold 0.2
s: sheet(25A, 20) - hex(0)@(4,9,A):0
t: tube(6,0, len=fit)
s.hole --fuse k=0--> t.in
"""
    )
    codes = {f.code for f in net.report.findings}
    assert {"extent.snap", "fit.alternatives"} <= codes


def test_angstrom_text_hashes_as_its_resolved_cells() -> None:
    assert content_hash("hexfold 0.2\ns: sheet(25A, 12)\n") == content_hash(
        "hexfold 0.2\ns: sheet(10, 12)\n"
    )


def test_measures_sheet_and_tube_anchors() -> None:
    net = build(
        """hexfold 0.2
s: sheet(25A, 12)
t: tube(6,0, len=4)
"""
    )
    got = {m.name: m for m in measures(net)}
    assert list(got) == ["s_W", "s_H", "t_len", "t_R"]
    w = got["s_W"]
    assert w.value_A == pytest.approx(10 * _A)
    assert (w.min_A, w.max_A) == (pytest.approx(9.5 * _A), pytest.approx(10.5 * _A))
    h = got["s_H"]
    assert h.value_A == pytest.approx(12 * _A)
    # zigzag (6,0): |T| = sqrt(3) a
    period = math.sqrt(3) * _A
    ln = got["t_len"]
    assert ln.value_A == pytest.approx(4 * period)
    assert (ln.min_A, ln.max_A) == (
        pytest.approx(3.5 * period),
        pytest.approx(4.5 * period),
    )
    r = got["t_R"]
    assert r.value_A == pytest.approx(_A * 6 / (2 * math.pi))
    assert r.min_A is None and r.max_A is None
    assert "snap cell" in w.reason and "snap period" in ln.reason
    d = w.to_dict()
    assert set(d) == {"name", "value_A", "min_A", "max_A", "reason"}
