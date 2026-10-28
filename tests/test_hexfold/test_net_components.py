"""``net.components`` -- is the built net one physical object, or a set of
parts nobody joined (gr458713)?

The whole value of this check is the distinction it draws, and it is not
the ``sheets`` partition: a sheet breaks at every bond-verb attachment
(SPEC 6.3), so a ``[2+2]`` bud is *two* sheets in *one* object.  Wiring
this finding to ``len(net.sheet_atoms)`` would therefore warn on the most
ordinary nanobud in the library, which is why the bonded-bud case below
is the load-bearing test rather than the disconnected one.

Found by Reto reading a prod design in the viewer as "a sheet plus a blob
of carbons": its spec fused a tube to a cap and never joined the sheet to
anything, and every finding came back INFO.
"""

from __future__ import annotations

from pathlib import Path

from hexfold.build import build
from hexfold.check import check

ROOT = Path(__file__).resolve().parents[2] / "hexfold"

_HEAD = "hexfold 0.2\nlattice: element=C sigma=1.42\n\n"


def _components(text: str):
    (finding,) = [f for f in check(text).findings if f.code == "net.components"]
    return finding


def test_two_primitives_nobody_joined_is_a_warning_naming_both_sizes() -> None:
    """The shape Reto hit: declare a sheet and a tube, join neither."""
    f = _components(_HEAD + "origin s\ns: sheet(10,10)\nt: tube(6,0,len=3)\n")
    assert f.severity.name == "WARN"
    assert dict(f.data)["n"] == 2
    # Both pieces are named by size, largest first, so the reader can tell
    # which primitive was left behind without re-reading the spec.
    sizes = [int(s) for s in str(dict(f.data)["sizes"]).split(",")]
    assert len(sizes) == 2
    assert sizes == sorted(sizes, reverse=True)


def test_a_fused_chain_is_one_piece() -> None:
    f = _components(
        _HEAD
        + "origin t\n"
        + "t: tube(5,5,len=3)\n"
        + "c: cap(5,5)\n"
        + "t.out --fuse k=0--> c.in\n"
    )
    assert f.severity.name == "INFO"
    assert dict(f.data)["n"] == 1


def test_a_bonded_bud_is_one_piece_though_it_is_two_sheets() -> None:
    """``sheet_bud_22.hx``: a C60 held on a sheet by two ``bond`` lines.

    The sheets partition splits at the attachment, so a check keyed on
    ``len(net.sheet_atoms)`` would call the library's own nanobud
    disconnected.  Physical connectivity must follow ``net.bonds``, which
    carries the attach edges.
    """
    text = (ROOT / "examples" / "sheet_bud_22.hx").read_text(encoding="utf-8")
    net = build(text, strict=False)
    assert len(net.sheet_atoms) > 1, "fixture no longer exercises the trap"
    f = _components(text)
    assert f.severity.name == "INFO"
    assert dict(f.data)["n"] == 1


def test_every_shipped_example_is_one_connected_piece() -> None:
    """A library example that is secretly in pieces is a library bug; this
    is also the regression net for the finding itself, since a wrong
    connectivity rule would light up most of these."""
    broken = {}
    for path in sorted((ROOT / "examples").glob("*.hx")):
        f = _components(path.read_text(encoding="utf-8"))
        if f.severity.name != "INFO":
            broken[path.name] = dict(f.data)["sizes"]
    assert broken == {}
