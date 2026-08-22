"""Tests for `precis.handlers._python_render` pure rendering helpers.

Unit-level — these take plain strings/dataclasses, no repo/handler setup
needed.
"""

from __future__ import annotations

from pathlib import Path

from precis.handlers._python_render import (
    _elide_signature,
    _split_top_level,
    render_symbol,
)
from precis.python_index import RepoIndex, Symbol

# ---------------------------------------------------------------------------
# _split_top_level
# ---------------------------------------------------------------------------


def test_split_top_level_ignores_commas_inside_brackets() -> None:
    parts = _split_top_level(
        "a: dict[str, Any], b: int = 1, c: tuple[int, int] = (1, 2)"
    )
    assert parts == [
        "a: dict[str, Any]",
        " b: int = 1",
        " c: tuple[int, int] = (1, 2)",
    ]


def test_split_top_level_no_commas() -> None:
    assert _split_top_level("x: int") == ["x: int"]


def test_split_top_level_empty() -> None:
    assert _split_top_level("") == [""]


# ---------------------------------------------------------------------------
# _elide_signature
# ---------------------------------------------------------------------------


def test_elide_signature_short_unchanged() -> None:
    sig = "def helper(x: int) -> int"
    assert _elide_signature(sig) == sig


def test_elide_signature_long_param_count_elided() -> None:
    params = ", ".join(f"p{i}: int" for i in range(1, 11))  # 10 params
    sig = f"def put({params}) -> None"
    out = _elide_signature(sig)
    assert out.startswith(
        "def put(p1: int, p2: int, p3: int, p4: int, p5: int, p6: int, "
    )
    assert "… +4 more" in out
    # Return annotation preserved.
    assert out.endswith(") -> None")
    # Only the kept 6 params should appear, not p7..p10.
    assert "p7" not in out


def test_elide_signature_preserves_return_annotation() -> None:
    params = ", ".join(f"p{i}: str" for i in range(1, 8))  # 7 params
    sig = f"def f({params}) -> dict[str, Any]"
    out = _elide_signature(sig)
    assert out.endswith("-> dict[str, Any]")
    assert "… +1 more" in out


def test_elide_signature_no_return_annotation() -> None:
    params = ", ".join(f"p{i}" for i in range(1, 9))  # 8 params, no -> ret
    sig = f"def g({params})"
    out = _elide_signature(sig)
    assert out.endswith("… +2 more)")


def test_elide_signature_commas_in_annotations_not_miscounted() -> None:
    """`dict[str, Any]` and tuple defaults shouldn't inflate the param count."""
    sig = (
        "def f(a: dict[str, Any], b: dict[str, Any], c: dict[str, Any], "
        "d: dict[str, Any], e: dict[str, Any], f: dict[str, Any]) -> None"
    )
    # Exactly 6 params despite each containing an internal comma — must
    # stay unchanged (<=6 params, and short enough).
    assert _elide_signature(sig, max_len=1000) == sig


def test_elide_signature_commas_in_defaults_not_miscounted() -> None:
    sig = "def f(a=(1, 2), b=(3, 4), c=(5, 6), d=(7, 8), e=(9, 10), f=(11, 12), g=1) -> None"
    out = _elide_signature(sig)
    # 7 params total, 6 kept, 1 elided — not inflated by the tuple commas.
    assert "… +1 more" in out


def test_elide_signature_no_parens_returned_unchanged() -> None:
    assert _elide_signature("not a signature") == "not a signature"


def test_elide_signature_unbalanced_parens_returned_unchanged() -> None:
    sig = "def f(a, b, c, d, e, f, g"  # missing close paren
    assert _elide_signature(sig) == sig


def test_elide_signature_param_count_ok_but_len_too_long_unchanged() -> None:
    """Under the param budget but over max_len: nothing to elide, left as-is."""
    sig = "def f(a: str = " + "'x' * 200" + ") -> None"
    out = _elide_signature(sig, max_len=10)
    assert out == sig


# ---------------------------------------------------------------------------
# render_symbol keeps the full (un-elided) header signature
# ---------------------------------------------------------------------------


def _make_symbol(**overrides: object) -> Symbol:
    defaults: dict[str, object] = dict(
        qualname="pkg.m.put",
        kind="function",
        file="pkg/m.py",
        start_line=1,
        end_line=10,
        parent="pkg.m",
        signature="def put("
        + ", ".join(f"p{i}: int" for i in range(1, 11))
        + ") -> None",
        docstring=None,
    )
    defaults.update(overrides)
    return Symbol(**defaults)  # type: ignore[arg-type]


def test_render_symbol_header_keeps_full_signature() -> None:
    sym = _make_symbol()
    idx = RepoIndex(root=Path("pkg"), modules={})
    out = render_symbol("r", sym, idx)
    # All 10 params present verbatim — drill-down is the "show me
    # everything" view, unlike outline/list views.
    assert "p10: int" in out
    assert "… +" not in out
