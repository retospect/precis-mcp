"""Primitive keyword vocabulary (gr454563): a keyword outside the
primitive's signature is a ParseError at its own column, not inert data
that lets ``len`` default and surfaces downstream as ``fit.unsolvable``."""

from __future__ import annotations

import pytest

from hexfold.check import check
from hexfold.report import ParseError
from hexfold.text import parse

_TYPO = """\
hexfold 0.2
lattice: element=C sigma=1.42

s: sheet(25A, 12)
t: tube(fit in {(5,5),(6,6)}, length=3)
c: cap(5,5)
t.out --fuse k=0--> c.in
"""


def test_unknown_keyword_is_a_parse_error_at_its_column() -> None:
    with pytest.raises(ParseError) as info:
        parse(_TYPO)
    msg = str(info.value)
    assert msg.startswith("5:")
    assert "unknown parameter 'length' for tube" in msg
    assert "known: hand, len, m, n" in msg
    # The column points at the offending key, not at the line start.
    assert info.value.span is not None
    _line, col = info.value.span
    assert col == _TYPO.splitlines()[4].index("length") + 1


def test_check_mode_reports_the_typo_not_fit_unsolvable() -> None:
    # Before the vocabulary check this spec produced ``fit.unsolvable``
    # (port.mismatch) pointing at a correct domain and fuse.
    with pytest.raises(ParseError, match="unknown parameter 'length'"):
        check(_TYPO)


@pytest.mark.parametrize(
    "line",
    [
        "t: tube(5,5,len=2,hand=+)",
        "t: tube(n=5,m=5,len=2)",
        "s: sheet(W=4,H=4)",
        "s: sheet(4,4,rim=z)",
        "c: cap(n=5,m=5)",
        "k: cone(P=1,rad=6)",
        "f: fullerene(C60,iso=1)",
    ],
)
def test_documented_keywords_still_parse(line: str) -> None:
    spec = parse(f"hexfold 0.2\nlattice: element=C sigma=1.42\n\n{line}\n")
    assert len(spec.instances) == 1


def test_positional_params_are_never_vocabulary_checked() -> None:
    spec = parse("hexfold 0.2\nlattice: element=C sigma=1.42\n\nt: tube(5,5,4)\n")
    assert dict(spec.instances[0].params) == {"0": "5", "1": "5", "2": "4"}
