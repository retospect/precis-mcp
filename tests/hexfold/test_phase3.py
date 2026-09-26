"""Phase 3: hex(r) holes, caps, terminate, len=fit, registry, view."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.canon import canonical_json
from hexfold.check import check
from hexfold.text import spec_from_dict, to_text

ROOT = Path(__file__).resolve().parents[2] / "hexfold"


def _net(text: str) -> Net:
    return build(text, strict=False)


def _codes(net: Net) -> list[str]:
    return [f.code for f in net.report.findings]


# ---------- hex(r) sheet openings ----------


def test_hex_hole_dangling_table() -> None:
    # dangling = 3|S| - 2 e_S; hex(r) removes the ring cluster of radius r
    expected = {0: 6, 1: 12, 2: 18}
    for r, want in expected.items():
        net = _net(
            f"""hexfold 0.1
origin s
s: sheet(40,40) - hex({r})@(20,20,A):0
"""
        )
        hole = dict(net.ports)["hole"]
        assert len(hole.dangling) == want, (r, len(hole.dangling))


def test_hex_hole_canonical_roundtrip() -> None:
    text = """hexfold 0.1
origin s
s: sheet(20,20) - hex(1)@(9,9,A):0
"""
    _net(text)
    d = json.loads(canonical_json(text))
    spec2 = spec_from_dict(d)
    assert canonical_json(to_text(spec2)) == canonical_json(text)


# ---------- caps ----------


def test_cap55_fuse_all_hexagon_seam() -> None:
    net = _net(
        """hexfold 0.1
origin a
a: tube(5,5,len=4)
b: cap(5,5)
a.out --fuse k=0--> b.in
"""
    )
    seam = [f for f in net.report.findings if f.code == "seam.rings"]
    assert seam and seam[0].data == (("k", 2), ("rings", {6: 10}))
    pn = Counter(len(r) for r in net.rings)
    assert pn.get(5, 0) == 6  # six pentagons in the hemisphere
    # chi = 1: one rim (a.in) remains
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 1
    assert dict(net.ports).keys() == {"a.in"}


def test_cap90_rejected() -> None:
    # cap(9,0)'s hemisphere is not a cap: it leaves the end over-curved
    # (3 pentagons + a {5:3,7:6} seam -> euler.residual -6); 9 is not a
    # multiple of 6 either, so it is not a flat-lid family member
    net = _net(
        """hexfold 0.1
origin b
b: cap(9,0)
"""
    )
    assert "build.kind" in _codes(net)
    kind = next(f for f in net.report.findings if f.code == "build.kind")
    assert "(5,5)" in kind.message and "(6k,0)" in kind.message


def test_cap_other_chiralities_rejected() -> None:
    net = _net(
        """hexfold 0.1
origin b
b: cap(7,0)
"""
    )
    assert "build.kind" in _codes(net)


# ---------- terminate ----------


def test_terminate_tube_fully_passivated() -> None:
    net = _net(
        """hexfold 0.1
origin t
t: tube(5,5,len=3)
terminate: t.* = H
"""
    )
    elems = Counter(a.element for a in net.atoms)
    assert elems == {"C": 60, "H": 20}
    deg: Counter[int] = Counter()
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    for a in net.atoms:
        assert deg[a.ord] == (1 if a.element == "H" else 3)
        if a.element == "H":
            assert a.hyb == "s" and "/H/" in str(a.path)
    assert not net.ports  # both rims consumed
    assert "terminate.done" in _codes(net)
    d = net.to_dict()
    assert d["lattice"]["sigma_CH_A"] == "1.09"
    assert d["terminate"] == [["t.*", "H"]]


def test_terminated_rims_keep_b_and_valence_clean() -> None:
    rep = check(
        """hexfold 0.1
origin t
t: tube(5,5,len=3)
terminate: t.* = H
"""
    )
    codes = [f.code for f in rep.findings]
    assert "valence.under" not in codes  # terminated rims still rims
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and res[0].severity.name == "INFO"  # B retained -> residual 0
    assert res[0].data == (("residual", 0), ("sheet", "t"))
    chi = [f for f in rep.findings if f.code == "euler.chi"]
    assert chi and "rims=2" in chi[0].message


# ---------- len=fit + registry ----------


def test_len_fit_picks_smallest_clean_len() -> None:
    # a mid-tube hole needs clearance: len < 8 clips the end rims
    net = _net(
        """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""
    )
    assert net.report.ok
    inst = net.spec.instance("t")
    assert inst is not None
    got = int(dict(inst.params)["len"])
    assert got == 7


def test_len_fit_unsolvable_reports_tried() -> None:
    from hexfold.report import BuildError

    with pytest.raises(BuildError) as ei:
        _net(
            """hexfold 0.1
origin t
t: tube(6,0,len=fit) - hexagon@(0,0,A)
"""
        )
    # hexagon on a zigzag tube always spans the circumference
    f = ei.value.report.findings[0]
    assert f.code == "fit.unsolvable" and f.data[0][0] == "tried"


def test_registry_is_redundant_info() -> None:
    net = _net(
        """hexfold 0.1
origin a
a: tube(5,5,len=3)
b: tube(5,5,len=3)
a.out --fuse k=0--> b.in
registry: a.out == b.in
"""
    )
    reg = [f for f in net.report.findings if f.code == "registry.redundant"]
    assert reg and reg[0].severity.name == "INFO"


# ---------- pillar end-to-end ----------


PILLAR = """hexfold 0.1
lattice: element=C sigma=1.42

origin substrate
substrate: sheet(20,20) - hex(0)@(9,9,A):0
post: tube(6,0,len=4)

substrate.hole --fuse k=0--> post.in

registry: post.in == post.out
terminate: substrate.rim = H
terminate: post.out = H
"""


COLLARED_PILLAR = PILLAR.replace(
    "post: tube(6,0,len=4)", "post: tube(6,0,len=4)"
).replace(
    "substrate.hole --fuse k=0--> post.in",
    "substrate.hole --fuse{7x6 @fit} k=0--> post.in",
)


@pytest.mark.slow  # 324s in the 2026-09-26 gate profile
def test_collared_pillar_is_over_curved() -> None:
    # the seam already supplies six heptagons; the collar doubles them:
    # euler.residual -6 (WARN), and the collar lands far from the hole
    rep = check(COLLARED_PILLAR)
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and res[0].severity.name == "WARN"
    assert res[0].data == (("residual", -6), ("sheet", "sheet0"))
    far = [f for f in rep.findings if f.code == "collar.far"]
    assert far and far[0].severity.name == "WARN"


def test_pillar_builds_clean() -> None:
    net = _net(PILLAR)
    assert net.report.ok
    assert not net.ports
    rep = check(PILLAR)
    assert rep.ok
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and res[0].data == (("residual", 0), ("sheet", "sheet0"))
    seam = [f for f in rep.findings if f.code == "seam.rings"]
    assert seam and seam[0].data == (("k", 2), ("rings", {7: 6}))
    # canon byte-stable
    assert canonical_json(PILLAR) == canonical_json(PILLAR)


def test_examples_residual_is_zero() -> None:
    # every shipped example is well-formed: residual 0 (INFO) on every
    # sheet (0.2: euler.residual is per sheet, SPEC 6.3; data carries the
    # sheet name alongside residual, so this checks the pair rather than
    # the full tuple, which varies by sheet)
    for f in sorted((ROOT / "examples").glob("*.hx")):
        rep = check(f.read_text(encoding="utf-8"))
        res = [x for x in rep.findings if x.code == "euler.residual"]
        assert res, f.name
        for x in res:
            assert (x.severity.name, dict(x.data)["residual"]) == (
                "INFO",
                0,
            ), (f.name, x.message)
            assert "sheet" in dict(x.data)


# ---------- view ----------


def test_view_writes_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    out = tmp_path / "nb.png"
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hexfold.cli",
            "view",
            str(ROOT / "examples" / "nanobud_96.hx"),
            "-o",
            str(out),
        ],
        check=False,
    ).returncode
    assert rc == 0 and out.stat().st_size > 1024
