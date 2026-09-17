"""Stick fidelity, report determinism/sorting, CLI exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hexfold.build import build
from hexfold.canon import canonical_json
from hexfold.check import check
from hexfold.cli import main
from hexfold.fullerene import C60Data
from hexfold.report import Finding, Profile, Report, Severity
from hexfold.stick import stick

_C60 = "hexfold 0.1\norigin c\nc: fullerene(C60)\n"
_SHEET = "hexfold 0.1\norigin s\ns: sheet(6, 4)\n"


def _kabsch_rmsd(x: np.ndarray, ref: np.ndarray) -> float:
    X = x - x.mean(0)
    Y = ref - ref.mean(0)
    H = X.T @ Y
    U, _, Vt = np.linalg.svd(H)
    D = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, D]) @ U.T
    return float(np.sqrt(((X @ R - Y) ** 2).sum(1).mean()))


def test_stick_c60_rmsd() -> None:
    net = build(_C60, strict=False)
    data = C60Data()
    # ord -> C60 atom index via the fullerene site label (site.u)
    ref = np.array([data.atom_pos[data.order[a.path.site.u]] for a in net.atoms])
    b0 = data.bonds[0]
    ref *= 1.42 / np.linalg.norm(data.atom_pos[b0[0]] - data.atom_pos[b0[1]])
    assert _kabsch_rmsd(stick(net), ref) < 0.3


def test_stick_deterministic_and_shape() -> None:
    net = build(_SHEET, strict=False)
    a, b = stick(net), stick(net)
    assert a.shape == (len(net.atoms), 3)
    assert a.dtype == np.float64
    assert np.array_equal(a, b)


def test_build_canon_stick_repeatable() -> None:
    j = canonical_json(_SHEET)
    assert j == canonical_json(_SHEET)


def test_report_sorting_and_dict() -> None:
    fs = [
        Finding("b.code", Severity.WARN, "w", where="b"),
        Finding("a.code", Severity.ERROR, "e", where="a"),
        Finding("c.code", Severity.INFO, "i", where="c"),
    ]
    r = Report(tuple(fs)).sorted()
    seq = [f.code for f in r.findings]
    assert seq[0] == "a.code"  # ERROR first
    assert r.errors()[0].code == "a.code"
    assert r.warnings()[0].code == "b.code"
    assert r.infos()[0].code == "c.code"
    assert not r.ok
    assert json.dumps(r.to_dict()) == json.dumps(r.to_dict())


def test_report_ok_when_clean() -> None:
    assert Report(()).ok
    assert Report(()).render()


def _write(tmp_path: Path, text: str) -> str:
    p = tmp_path / "t.hx"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_cli_exit_codes(tmp_path: Path) -> None:
    good = _write(tmp_path, _SHEET)
    assert main(["check", good]) == 0
    bad = _write(tmp_path, "not hexfold at all\n")
    assert main(["check", bad]) == 2
    errd = _write(
        tmp_path,
        "hexfold 0.1\norigin s\n"
        "s: sheet(12, 12) + pentagon@(4,4,A):0 + pentagon@(4,4,A):1\n",
    )
    assert main(["check", errd]) == 1


def test_cli_xyz_provenance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = _write(tmp_path, _C60)
    assert main(["xyz", f]) == 0
    out = capsys.readouterr().out
    assert "fidelity=stick" in out
    assert out.splitlines()[0].strip() == "60"


def test_cli_canon_runs_twice_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = _write(tmp_path, _SHEET)
    assert main(["canon", f]) == 0
    a = capsys.readouterr().out
    assert main(["canon", f]) == 0
    assert capsys.readouterr().out == a


def test_geometry_report_byte_identical() -> None:
    rep1 = check(_C60, geometry=True)
    rep2 = check(_C60, geometry=True)
    assert rep1.render() == rep2.render()
    assert json.dumps(rep1.to_dict(), sort_keys=True) == json.dumps(
        rep2.to_dict(), sort_keys=True
    )


def test_geometry_ring_ideals() -> None:
    # flat sheet: no angle findings at all
    rep = check(_SHEET, geometry=True)
    assert not any(f.code == "geom.angle.dev" for f in rep.findings)
    # C60 pentagon corners are checked against 108 deg, not 120:
    # a closed-form C60 must produce zero angle findings
    rep = check(_C60, geometry=True)
    assert not any(f.code == "geom.angle.dev" for f in rep.findings)
    summary = [f for f in rep.findings if f.code == "geom.summary"]
    assert len(summary) == 1
    data = dict(summary[0].data)
    assert "bond_rms" in data and "angle_count" in data
    assert data["suppressed"] == 0


def test_geometry_angle_dev_sp3_uses_tetrahedral_ideal() -> None:
    # a bond across two small sheets drives both endpoints to 4 bonds
    # (hyb="sp3"); their ring-corner angle spring is now the sp3 ideal,
    # not the hexagon ring ideal.
    text = "hexfold 0.2\nh: sheet(6,6)\nf: sheet(6,6)\nh/(2,2,A) --bond--> f/(2,2,A)\n"
    net = build(text, strict=False)
    sp3 = {a.ord for a in net.atoms if a.hyb == "sp3"}
    assert sp3
    rep = check(text, geometry=True, profile=Profile(angle_tol_deg=1.0))
    ang = [f for f in rep.findings if f.code == "geom.angle.dev"]
    sp3_findings = [f for f in ang if f.where and int(f.where) in sp3]
    assert sp3_findings
    # every sp3-vertex finding uses the tetrahedral ideal, never a ring ideal
    assert {dict(f.data)["ideal_deg"] for f in sp3_findings} == {109.5}


def test_geometry_cap_and_suppressed() -> None:
    # a heavily defective sheet yields > 10 angle findings; cap + suppressed
    text = (
        "hexfold 0.1\norigin s\n"
        "s: sheet(14, 14) + sw@(6,6,A):0 + sw@(8,8,A):3 "
        "+ sw@(10,10,A):1\n"
    )
    rep = check(text, geometry=True)
    ang = [f for f in rep.findings if f.code == "geom.angle.dev"]
    assert len(ang) <= 10
    summary = next(f for f in rep.findings if f.code == "geom.summary")
    assert "suppressed" in dict(summary.data)


def _geom_stats(text: str) -> tuple[float, float]:
    rep = check(text, geometry=True)
    s = next(f for f in rep.findings if f.code == "geom.summary")
    d = dict(s.data)
    return float(d["bond_rms"]), float(d["angle_rms"])


def test_stick_tube_cylinder_quality() -> None:
    text = "hexfold 0.1\norigin t\nt: tube(5, 5, len=4)\n"
    bond_rms, angle_rms = _geom_stats(text)
    assert bond_rms < 0.02
    assert angle_rms < 3.0
    net = build(text, strict=False)
    pos = stick(net)
    x = pos - pos.mean(0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    axis = vt[0]  # longest direction = tube axis
    r = np.linalg.norm(x - (x @ axis)[:, None] * axis, axis=1)
    assert abs(float(r.mean()) - 3.39) / 3.39 < 0.03


def test_stick_cone_quality() -> None:
    bond_rms, angle_rms = _geom_stats("hexfold 0.1\norigin c\nc: cone(5, rad=12)\n")
    assert bond_rms < 0.05
    assert angle_rms < 6.0


def test_stick_sheet_sw_quality() -> None:
    bond_rms, _ = _geom_stats(
        "hexfold 0.1\norigin s\ns: sheet(12, 12) + sw@(4,4,A):0\n"
    )
    assert bond_rms < 0.05


def test_stick_coords_byte_identical() -> None:
    for text in (
        "hexfold 0.1\norigin t\nt: tube(5, 5, len=4)\n",
        "hexfold 0.1\norigin c\nc: cone(5, rad=12)\n",
        "hexfold 0.1\norigin s\ns: sheet(12, 12) + sw@(4,4,A):0\n",
        _C60,
    ):
        net = build(text, strict=False)
        assert np.array_equal(stick(net), stick(net))


def test_euler_residual_zero() -> None:
    for text in (
        "hexfold 0.1\norigin s\ns: sheet(12, 12) + sw@(4,4,A):0\n",
        "hexfold 0.1\norigin t\nt: tube(5, 5, len=4)\n",
        *[f"hexfold 0.1\norigin c\nc: cone({p}, rad=12)\n" for p in (1, 2, 3, 4, 5)],
    ):
        rep = check(text)
        res = [f for f in rep.findings if f.code == "euler.residual"]
        assert res and all(
            f.severity.name == "INFO" and dict(f.data)["residual"] == 0 for f in res
        )


def test_c60_hole_boundary_term() -> None:
    rep = check("hexfold 0.1\norigin c\nc: fullerene(C60) - pentagon@(0,0,A)\n")
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and res[0].severity.name == "INFO"
    assert res[0].data == (("residual", 0), ("sheet", "c"))
    net = build(
        "hexfold 0.1\norigin c\nc: fullerene(C60) - pentagon@(0,0,A)\n",
        strict=False,
    )
    port = dict(net.ports)["hole"]
    assert port.b == -5
    assert port.b_expected == -5
