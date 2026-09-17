"""``seam`` -- a k>=3 rim identification (SPEC 11.3) and its consequences:
per-sheet Euler accounting (SPEC 6.3) and the ``sheets`` partition (SPEC
6.3/18).  ``sheet_pill_bump.hx`` is the 0.2 stand-in for SPEC 6.4's
capped-pill acceptance example (see its header comment: caps that size
need the roadmap ``cap(n,m)`` flat-lid family, not in 0.2).
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.check import check
from hexfold.report import ParseError, Report
from hexfold.text import Seam, parse, to_text

ROOT = Path(__file__).resolve().parents[2] / "hexfold"
_EXAMPLE = (ROOT / "examples" / "sheet_pill_bump.hx").read_text(encoding="utf-8")


def _net(text: str) -> Net:
    return build(text, strict=False)


# ---------- parse / emit / round-trip ----------


def test_seam_line_parses() -> None:
    spec = parse(
        "hexfold 0.2\n"
        "s: sheet(6,6) - hex(0)@(3,3,A):0\n"
        "up: tube(6,0,len=3)\n"
        "down: tube(6,0,len=3)\n"
        "seam foot: s.hole == up.in == down.in\n"
    )
    assert len(spec.seams) == 1
    s = spec.seams[0]
    assert (s.name, s.rims, s.k, s.atoms) == (
        "foot",
        ("s.hole", "up.in", "down.in"),
        0,
        "sp2",
    )


def test_seam_line_parses_k_and_atoms() -> None:
    spec = parse("hexfold 0.2\nseam foot: a.r1 == b.r1 == c.r1 k=2 atoms=sp2\n")
    assert len(spec.seams) == 1
    s = spec.seams[0]
    assert (s.name, s.rims, s.k, s.atoms) == (
        "foot",
        ("a.r1", "b.r1", "c.r1"),
        2,
        "sp2",
    )


def test_seam_line_k_order_before_atoms_does_not_matter() -> None:
    spec1 = parse("hexfold 0.2\nseam x: a.r == b.r == c.r k=3 atoms=sp2\n")
    spec2 = parse("hexfold 0.2\nseam x: a.r == b.r == c.r atoms=sp2 k=3\n")
    assert spec1.seams == spec2.seams


def test_two_rims_is_a_parse_error_pointing_at_fuse() -> None:
    with pytest.raises(ParseError, match="fuse"):
        parse("hexfold 0.2\nseam x: a.r == b.r\n")


def test_bad_atoms_value_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="sp2"):
        parse("hexfold 0.2\nseam x: a.r == b.r == c.r atoms=sp3\n")


def test_to_text_emits_seam_line() -> None:
    spec = parse("hexfold 0.2\nseam foot: s.hole == up.in == down.in k=2\n")
    text = to_text(spec)
    assert "seam foot: s.hole == up.in == down.in k=2" in text


def test_to_text_omits_default_k_and_atoms() -> None:
    spec = parse("hexfold 0.2\nseam foot: a.r == b.r == c.r\n")
    text = to_text(spec)
    assert "seam foot: a.r == b.r == c.r\n" in text
    assert "k=" not in text
    assert "atoms=" not in text


def test_seam_round_trips_via_canonical_json() -> None:
    import json

    from hexfold.canon import canonical_json
    from hexfold.text import spec_from_dict

    spec = parse(_EXAMPLE)
    j = canonical_json(spec)
    spec2 = spec_from_dict(json.loads(j))
    assert spec2.seams == (Seam("foot", ("s.hole", "up.in", "down.in"), 0, "sp2"),)
    assert to_text(spec2) == to_text(spec_from_dict(json.loads(canonical_json(spec2))))


# ---------- port.mismatch: generalised to k rims ----------


def test_mismatched_dangling_count_is_port_mismatch() -> None:
    text = (
        "hexfold 0.2\n"
        "lattice: element=C sigma=1.42\n"
        "s: sheet(6,6) - hex(0)@(3,3,A):0\n"
        "up: tube(5,0,len=3)\n"
        "down: tube(6,0,len=3)\n"
        "seam foot: s.hole == up.in == down.in\n"
    )
    rep = check(text)
    assert not rep.ok
    bad = [f for f in rep.findings if f.code == "port.mismatch"]
    assert len(bad) == 1
    assert bad[0].severity.name == "ERROR"
    assert "up.in" in bad[0].message
    assert dict(bad[0].data)["counts"] == {"s.hole": 6, "up.in": 5, "down.in": 6}


def test_unresolved_rim_is_port_unknown() -> None:
    text = (
        "hexfold 0.2\n"
        "a: tube(6,0,len=3)\n"
        "b: tube(6,0,len=3)\n"
        "c: tube(6,0,len=3)\n"
        "seam x: a.in == b.in == c.nosuchport\n"
    )
    rep = check(text)
    assert not rep.ok
    bad = [f for f in rep.findings if f.code == "port.unknown"]
    assert len(bad) == 1
    assert "c.nosuchport" in bad[0].message


# ---------- k=4: valence.over, still sp2 ----------


def test_k4_seam_atoms_are_over_valent_but_stay_sp2() -> None:
    text = (
        "hexfold 0.2\n"
        "a: tube(6,0,len=3)\n"
        "b: tube(6,0,len=3)\n"
        "c: tube(6,0,len=3)\n"
        "d: tube(6,0,len=3)\n"
        "seam x: a.in == b.in == c.in == d.in\n"
    )
    net = _net(text)
    seam_ords = {o for rec in net.seams for o in rec.atoms}
    assert len(seam_ords) == 6
    for o in seam_ords:
        a = next(at for at in net.atoms if at.ord == o)
        assert a.hyb == "sp2"
    rep = check(text)
    over = [f for f in rep.findings if f.code == "valence.over"]
    assert len(over) == 6
    assert all(f.severity.name == "ERROR" for f in over)
    assert not rep.ok
    seam_findings = [f for f in rep.findings if f.code == "seam.rings"]
    assert len(seam_findings) == 1
    assert dict(seam_findings[0].data)["k"] == 4


# ---------- the acceptance example ----------


def test_example_has_no_error() -> None:
    rep = check(_EXAMPLE)
    assert rep.ok


def test_example_has_three_sheets() -> None:
    net = _net(_EXAMPLE)
    assert {name for name, _ in net.sheet_atoms} == {"s", "up", "down"}
    assert len(net.sheet_atoms) == 3


def test_example_sheets_key_in_to_dict() -> None:
    net = _net(_EXAMPLE)
    d = net.to_dict()
    assert set(d["sheets"]) == {"s", "up", "down"}
    for faces in d["sheets"].values():
        assert faces  # every sheet has at least one face


def test_example_per_sheet_chi_and_residual() -> None:
    # a disc with one hole (a hex(0) opening) is an annulus: chi=0; each
    # open tube is a cylinder: chi=0.  B_expected balances exactly (SPEC
    # 6.3: the seam is invisible to each sheet's own law) -> residual 0
    # on all three, computed by running check() over the built net (the
    # hex(0) hole's rim is an 18-atom walk with 6 dangling, not a plain
    # hexagon, so its arc lengths are not the sort one derives by eye;
    # this is the algorithm's actual output, cross-checked against the
    # spec formulas in the report, not a guess).
    rep = check(_EXAMPLE)
    chi = {f.where: dict(f.data)["chi"] for f in rep.findings if f.code == "euler.chi"}
    res = {
        f.where: dict(f.data)["residual"]
        for f in rep.findings
        if f.code == "euler.residual"
    }
    assert chi == {"s": 0, "up": 0, "down": 0}
    assert res == {"s": 0, "up": 0, "down": 0}


def test_example_seam_rings_finding() -> None:
    # census computed by running the built seam against the real rims:
    # the hex(0) hole is an 18-atom/6-dangling rim, each tube end a
    # 12-atom/6-dangling rim; the AB/CA families (hole-tube) arc 4+3
    # atoms per period (a nonagon with the two seam atoms), the BC
    # family (tube-tube) arcs 3+3 (an octagon) -- 6 periods each.
    rep = check(_EXAMPLE)
    seam = [f for f in rep.findings if f.code == "seam.rings"]
    assert len(seam) == 1
    data = dict(seam[0].data)
    assert data["k"] == 3
    assert data["rings"] == {8: 6, 9: 12}


def test_example_seam_atoms_six_with_three_bonds_each() -> None:
    net = _net(_EXAMPLE)
    assert len(net.seams) == 1
    rec = net.seams[0]
    assert rec.name == "foot"
    assert rec.rims == ("s.hole", "up.in", "down.in")
    assert len(rec.atoms) == 6
    deg: dict[int, int] = {}
    for i, j, _ in net.bonds:
        deg[i] = deg.get(i, 0) + 1
        deg[j] = deg.get(j, 0) + 1
    for o in rec.atoms:
        assert deg[o] == 3


def test_example_generated_atoms_paths() -> None:
    net = _net(_EXAMPLE)
    d = net.to_dict()
    paths = sorted(
        a["path"] for a in d["generated"]["atoms"] if a["instance"] == "foot"
    )
    assert paths == [f"foot/s{i}" for i in range(6)]


def test_example_registry_closure_absent() -> None:
    # the part graph via one seam is a path (sheet -> up, up -> down),
    # not a triangle -- no cycle, so neither registry.closure nor
    # registry.redundant fires (no `registry:` line is authored either)
    rep = check(_EXAMPLE)
    codes = {f.code for f in rep.findings}
    assert "registry.closure" not in codes
    assert "registry.redundant" not in codes


# ---------- fuse-only nets: exactly one sheet, unchanged Euler findings ----


# Snapshot of every pre-slice-E example's (report.ok, euler-code
# signature) taken before this slice touched check.py -- codes and
# residuals/chi must be unchanged; only the new `sheet` data key differs.
_PRE_SEAM_EULER_SNAPSHOT: dict[str, tuple[bool, list[tuple[str, str, int]]]] = {
    "c60_hole.hx": (True, [("euler.chi", "INFO", 1), ("euler.residual", "INFO", 0)]),
    "capped_tube.hx": (True, [("euler.chi", "INFO", 1), ("euler.residual", "INFO", 0)]),
    "capped_tube_da_neck.hx": (
        True,
        [("euler.chi", "INFO", 1), ("euler.residual", "INFO", 0)],
    ),
    "cone5.hx": (True, [("euler.chi", "INFO", 1), ("euler.residual", "INFO", 0)]),
    "nanobud_22.hx": (
        True,
        [
            ("euler.chi", "INFO", 2),
            ("euler.chi", "INFO", 0),
            ("euler.residual", "INFO", 0),
            ("euler.residual", "INFO", 0),
        ],
    ),
    "nanobud_87.hx": (
        True,
        [
            ("euler.chi", "INFO", 1),
            ("euler.chi", "INFO", 0),
            ("euler.residual", "INFO", 0),
        ],
    ),
    "nanobud_96.hx": (
        True,
        [
            ("euler.chi", "INFO", 1),
            ("euler.chi", "INFO", 0),
            ("euler.residual", "INFO", 0),
        ],
    ),
    "nanobud_da_neck.hx": (
        True,
        [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)],
    ),
    "nanobud_db_neck.hx": (
        True,
        [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)],
    ),
    "pillar.hx": (True, [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)]),
    "sheet_bud_22.hx": (
        True,
        [
            ("euler.chi", "INFO", 2),
            ("euler.chi", "INFO", 1),
            ("euler.residual", "INFO", 0),
            ("euler.residual", "INFO", 0),
        ],
    ),
    "sheet_sw.hx": (True, [("euler.chi", "INFO", 1), ("euler.residual", "INFO", 0)]),
    "tube55.hx": (True, [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)]),
    "tube_fuse.hx": (True, [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)]),
    "tube_ring_closure.hx": (
        True,
        [("euler.chi", "INFO", 0), ("euler.residual", "INFO", 0)],
    ),
}


def _euler_signature(rep: Report) -> tuple[bool, list[tuple[str, str, int | None]]]:
    sig: list[tuple[str, str, int | None]] = []
    for f in rep.sorted().findings:
        if f.code in (
            "euler.chi",
            "euler.residual",
            "internal.euler",
            "euler.closed_unreachable",
        ):
            d = dict(f.data)
            sig.append((f.code, f.severity.name, d.get("residual", d.get("chi"))))
    return rep.ok, sig


@pytest.mark.parametrize("name", sorted(_PRE_SEAM_EULER_SNAPSHOT))
def test_pre_seam_examples_euler_findings_unchanged(name: str) -> None:
    text = (ROOT / "examples" / name).read_text(encoding="utf-8")
    rep = check(text)
    assert _euler_signature(rep) == _PRE_SEAM_EULER_SNAPSHOT[name]


@pytest.mark.parametrize(
    "name",
    sorted(
        Path(p).name
        for p in glob.glob(str(ROOT / "examples" / "*.hx"))
        if Path(p).name in _PRE_SEAM_EULER_SNAPSHOT
    ),
)
def test_pre_seam_fuse_only_examples_are_one_sheet(name: str) -> None:
    text = (ROOT / "examples" / name).read_text(encoding="utf-8")
    net = _net(text)
    if net.attach:
        # bond-attach examples (the nanobud menus) are already
        # multi-sheet pre-0.2 -- this guard is for the fuse-only ones.
        pytest.skip("bond-attach example, not fuse-only")
    assert len(net.sheet_atoms) == 1


def test_all_pre_seam_examples_covered_by_snapshot() -> None:
    names = {Path(p).name for p in glob.glob(str(ROOT / "examples" / "*.hx"))}
    names.discard("sheet_pill_bump.hx")
    assert names == set(_PRE_SEAM_EULER_SNAPSHOT)


def test_seam_registry_edges_phase_only_on_primary_pair() -> None:
    # the bonding loop offsets only rim 0 by k (every other rim uses the
    # same (k - i) % n index), so rim0-rim1 carries phase k and rim1-rim2
    # is in direct register: the part-graph edges must say so, else a
    # cycle closed through the second seam edge gets a bogus residual
    text = _EXAMPLE.replace(
        "seam foot: s.hole == up.in == down.in",
        "seam foot: s.hole == up.in == down.in k=2",
    )
    net = build(text)
    seam_edges = [e for e in net.registry_edges if e[3] == 6]
    assert ("s", "up", 2, 6) in seam_edges
    assert ("up", "down", 0, 6) in seam_edges
    assert ("up", "down", 2, 6) not in seam_edges


def test_seam_atom_path_round_trips() -> None:
    from hexfold.ids import AtomPath

    net = build(_EXAMPLE)
    seam_atoms = [a for a in net.atoms if a.instance == "foot"]
    assert len(seam_atoms) == 6
    for a in seam_atoms:
        assert AtomPath.parse(str(a.path)) == a.path
        assert str(a.path).startswith("foot/s")
