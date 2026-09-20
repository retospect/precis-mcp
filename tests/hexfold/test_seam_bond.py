"""``<seam>/s<i>`` seam atoms resolvable by ``--bond-->`` (SPEC 9, [spec
0.2]): seams are minted after menu/fuse but before bond connects, so a
``--bond-->`` can name a seam atom the same as any lattice atom.  A k=3
seam atom's 3 ring bonds are its own sp2 floor (SPEC 11.3), same as a
lattice atom's 3 ring bonds: a `--bond-->` is its 4th bond, sp3, no
error; a second `--bond-->` overflows to valence.over exactly as for a
lattice atom.
"""

from __future__ import annotations

from pathlib import Path

from hexfold.build import Net, build
from hexfold.check import check
from hexfold.stick import stick

ROOT = Path(__file__).resolve().parents[2] / "hexfold"
_EXAMPLE = (ROOT / "examples" / "sheet_pill_bump.hx").read_text(encoding="utf-8")
_DOUGHNUT_OH = (ROOT / "examples" / "flanged_doughnut_oh.hx").read_text(
    encoding="utf-8"
)


def _deg(net: Net) -> dict[int, int]:
    deg: dict[int, int] = {}
    for i, j, _order in net.bonds:
        deg[i] = deg.get(i, 0) + 1
        deg[j] = deg.get(j, 0) + 1
    return deg


def test_bond_onto_seam_atom_is_sp3_no_error() -> None:
    text = _EXAMPLE + "\nfrag: oh = smiles(O)\noh.1 --bond--> foot/s0\n"
    rep = check(text)
    assert not any(f.severity.name == "ERROR" for f in rep.findings)
    assert not any(f.code == "op.dangling" for f in rep.findings)

    # a real (non-frag) bond does add a 4th bond and promotes to sp3,
    # exactly like a lattice atom (test_phase2.test_bond_sp3_and_valence_over)
    net = build(_EXAMPLE + "\nfoot/s0 --bond--> foot/s3\n", strict=False)
    deg = _deg(net)
    s0 = next(a for a in net.atoms if a.instance == "foot" and a.path.label == "s0")
    assert deg[s0.ord] == 4
    assert s0.hyb == "sp3"


def test_second_bond_onto_seam_atom_is_valence_over() -> None:
    text = _EXAMPLE + "\nfoot/s0 --bond--> foot/s3\nfoot/s0 --bond--> foot/s4\n"
    net = build(text, strict=False)
    deg = _deg(net)
    s0 = next(a for a in net.atoms if a.instance == "foot" and a.path.label == "s0")
    assert deg[s0.ord] == 5
    assert s0.hyb == "sp2"
    rep = check(text)
    over = [f for f in rep.findings if f.code == "valence.over"]
    assert len(over) >= 1
    assert any(f.severity.name == "ERROR" for f in over)


def test_bond_to_nonexistent_seam_atom_is_dangling() -> None:
    text = _EXAMPLE + "\nfoot/s0 --bond--> foot/s99\n"
    rep = check(text)
    dangling = [f for f in rep.findings if f.code == "op.dangling"]
    assert len(dangling) == 1
    assert "foot/s99" in dangling[0].message


def test_flanged_doughnut_oh_checks_clean_with_six_pendants() -> None:
    rep = check(_DOUGHNUT_OH)
    assert not any(f.severity.name == "ERROR" for f in rep.findings)
    assert not any(f.code == "op.dangling" for f in rep.findings)
    unrealized = [f for f in rep.findings if f.code == "frag.unrealized"]
    assert len(unrealized) == 6


def test_flanged_doughnut_oh_stick_shape() -> None:
    net = build(_DOUGHNUT_OH, strict=False)
    pos = stick(net)
    assert pos.shape == (len(net.atoms), 3)
