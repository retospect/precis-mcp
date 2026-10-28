"""``face=up|down`` on a bond or menu attachment (SPEC 11.1): the flat
host's face the part seeds on, per (host, part) pair, so a sheet can carry
one bud on each face (gr459567 family, round-2 review)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from hexfold.build import build
from hexfold.report import Severity
from hexfold.text import parse, spec_from_dict, to_text

BASE = "hexfold 0.2\norigin h\nh: sheet(12,12)\nb: fullerene(C60)\n"


def _dz(spec: str) -> dict[str, float]:
    """Each non-host instance's seed centroid height above the host plane."""
    net = build(spec, strict=False)
    pos = np.asarray(net.seed3)
    inst: dict[str, list[int]] = {}
    for a in net.atoms:
        inst.setdefault(a.instance, []).append(a.ord)
    host = pos[inst["h"], 2].mean()
    return {k: float(pos[v, 2].mean() - host) for k, v in inst.items() if k != "h"}


def test_face_parses_and_round_trips_on_menu_and_bond_lines() -> None:
    text = (
        BASE + "b @ h/(6,6,A):0 [2+2] face=down\n"
        "c: fullerene(C60)\nc/(0,0,A) --bond face=up--> h/(2,2,A)\n"
    )
    spec = parse(text)
    faces = {(c.src, c.verb): c.face for c in spec.connects}
    assert faces[("b", "menu")] == "down"
    assert faces[("c/(0,0,A)", "bond")] == "up"
    again = to_text(spec)
    assert "[2+2] face=down" in again and "--bond face=up-->" in again
    key = [(c.src, c.dst, c.verb, c.menu, c.k, c.face) for c in spec.connects]
    assert [
        (c.src, c.dst, c.verb, c.menu, c.k, c.face) for c in parse(again).connects
    ] == key
    d = json.loads(json.dumps(build(text, strict=False).to_dict()))
    assert {c.get("face") for c in d["connects"] if c.get("source") is None} == {
        "up",
        "down",
    }
    # the menu's generated bonds carry the face too
    assert all(c["face"] == "down" for c in d["connects"] if c.get("source"))
    assert spec_from_dict(d).connects[0].face in ("up", "down")


def test_bad_face_value_is_a_parse_error() -> None:
    from hexfold.report import HexfoldError

    with pytest.raises(HexfoldError):
        parse(BASE + "b @ h/(6,6,A):0 [2+2] face=left\n")


def test_face_down_puts_the_bud_under_the_sheet_and_up_above() -> None:
    plain = _dz(BASE + "b @ h/(6,6,A):0 [2+2]\n")["b"]
    up = _dz(BASE + "b @ h/(6,6,A):0 [2+2] face=up\n")["b"]
    down = _dz(BASE + "b @ h/(6,6,A):0 [2+2] face=down\n")["b"]
    assert plain > 3.0 and up == pytest.approx(plain, abs=1e-9)
    assert down < -3.0 and abs(down) == pytest.approx(plain, abs=0.2)


def test_two_buds_can_take_opposite_faces_of_one_sheet() -> None:
    spec = (
        BASE + "c: fullerene(C60)\n"
        "b @ h/(4,4,A):0 [2+2] face=up\n"
        "c @ h/(8,8,A):0 [2+2] face=down\n"
    )
    dz = _dz(spec)
    assert dz["b"] > 3.0 and dz["c"] < -3.0
    net = build(spec, strict=False)
    authored = [f for f in net.report.findings if f.code == "place.face_authored"]
    assert sorted(dict(f.data)["bud"] for f in authored) == ["b", "c"]
    assert {dict(f.data)["face"] for f in authored} == {"up", "down"}
    assert all(f.severity == Severity.INFO for f in authored)
    assert not [f for f in net.report.findings if f.code == "geom.clash"]


def test_face_on_a_tube_host_is_ignored_with_a_warning() -> None:
    spec = (
        "hexfold 0.2\norigin t\nt: tube(10,10, len=6)\nb: fullerene(C60)\n"
        "b @ t/(3,3,A):0 [2+2] face=down\n"
    )
    net = build(spec, strict=False)
    ignored = [f for f in net.report.findings if f.code == "place.face_ignored"]
    assert len(ignored) == 1 and ignored[0].severity == Severity.WARN
    assert not [f for f in net.report.findings if f.code == "place.face_authored"]
    # the tube's own normal still seats the bud outside
    pos = np.asarray(net.seed3)
    bud = [a.ord for a in net.atoms if a.instance == "b"]
    tube = [a.ord for a in net.atoms if a.instance == "t"]
    r_bud = np.hypot(*pos[bud, :2].mean(axis=0))
    r_tube = np.hypot(pos[tube, 0], pos[tube, 1]).mean()
    assert r_bud > r_tube + 3.0


def test_nanobud_menu_honours_an_authored_down_face() -> None:
    dz = _dz(BASE + "b @ h/(6,6,A) [9-6] face=down\n")
    assert dz["b"] < -3.0
