"""gr514397: the autocatpath skip gate must NAME the missing extra.

* ``needs_engine``'s reason tells the reader *which install* fixes the
  skip — a bare ``SKIPPED autocatpath`` reads as "the test is inert" on a
  venv without the optional engine.
* No test module keeps a bare ``pytest.importorskip("autocatpath…")``
  *call site*: every new gate goes through ``tests._autocatpath`` so the
  silent form can't drift back in. (Prose mentions don't count — the walk
  is AST call sites only.)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._autocatpath import needs_engine

_TESTS = Path(__file__).resolve().parent


def test_needs_engine_reason_names_the_catalyst_extra() -> None:
    with pytest.raises(pytest.skip.Exception, match="catalyst"):
        needs_engine("autocatpath.definitely_absent_module")


def test_needs_engine_note_carries_the_version_gate() -> None:
    with pytest.raises(pytest.skip.Exception, match="engine >= 9.99"):
        needs_engine("autocatpath.definitely_absent_module", note="engine >= 9.99")


def _bare_autocatpath_gates(path: Path) -> list[int]:
    """Line numbers of ``pytest.importorskip("autocatpath…")`` call sites."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "importorskip"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.split(".")[0] == "autocatpath"
        ):
            continue
        hits.append(node.lineno)
    return hits


def test_no_bare_autocatpath_importorskip_call_sites() -> None:
    bad = {
        p.name: hits
        for p in sorted(_TESTS.rglob("test_*.py"))
        if (hits := _bare_autocatpath_gates(p))
    }
    assert not bad, (
        "bare autocatpath importorskip hides why the test skips — use "
        f"tests._autocatpath.needs_engine: {bad}"
    )
