"""No decision logic outside the export walker (export-latex-docx-parity,
decisions 6 and 7).

``export/latex.py`` and ``export/docx.py`` are renderers: they format what
:mod:`precis.export.walker` decides. This test scans their syntax trees
for the decisions the walker owns, so one cannot drift back into a single
backend: the legacy-handle mapping, cross-ref / equation numbering, cite
registration and merging, list nesting, chunk-kind dispatch, abbreviation
first use and back-matter selection. Dispatch on format details inside a
renderer is fine; dispatch on the chunk kind is not."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_EXPORT = Path(__file__).resolve().parents[1] / "src" / "precis" / "export"
_BACKENDS = ("latex.py", "docx.py")

#: Functions allowed to compare a chunk's kind, with the reason and the
#: gripe that removes the allowance. CLOSED: nothing joins this without a
#: ruling (witty, 2026-10-17).
_KIND_ALLOWED = {
    # a poster logo is a figure REF; its own chunks (figure_node / blob)
    # are an asset lookup, not the draft's structure
    "_figure_logo_asset": (
        "gr495810: logo asset lookup inside a figure ref; move it out of "
        "the renderer and drop this entry"
    ),
}


def _functions(tree: ast.AST) -> dict[ast.AST, str]:
    """node → name of the innermost enclosing function."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else fn
            )
            owner[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _attr(node: ast.AST | None) -> str:
    return node.attr if isinstance(node, ast.Attribute) else ""


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return _attr(node)


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    owner = _functions(tree)
    out: list[str] = []

    def hit(node: ast.AST, what: str) -> None:
        out.append(f"line {getattr(node, 'lineno', '?')} ({owner.get(node)}): {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            # legacy-handle mapping: legacy_to_dc.get(...)
            if (
                _attr(fn) == "get"
                and _attr(getattr(fn, "value", None)) == "legacy_to_dc"
            ):
                hit(node, "legacy-handle lookup")
            # cite registration: cited.append / si_cited.append, cited.index
            if _attr(fn) in ("append", "index") and _name(
                getattr(fn, "value", None)
            ) in ("cited", "si_cited"):
                hit(node, f"cite registration ({_attr(fn)})")
            # abbreviation first use: _abbrev.expand, seen_acr.add
            if _attr(fn) == "expand" and _name(getattr(fn, "value", None)) == "_abbrev":
                hit(node, "abbreviation first-use decision")
            if _attr(fn) == "add" and _name(getattr(fn, "value", None)) == "seen_acr":
                hit(node, "abbreviation first-use decision")
            # back-matter selection
            if _name(fn) == "ordered_blocks":
                hit(node, "back-matter selection")
        elif isinstance(node, ast.DictComp):
            # legacy-handle map construction {c.handle: c.dc ...}
            if _attr(node.key) == "handle" and _attr(node.value) == "dc":
                hit(node, "legacy-handle map")
        elif isinstance(node, ast.Name | ast.Attribute):
            name = _name(node)
            if name in ("BACK_MATTER_ORDER",):
                hit(node, "back-matter order")
            if name in ("counters", "list_stack", "last_cite", "_ADJ_CITES"):
                hit(node, f"walker decision state ({name})")
            if name == "parent_chunk_id":
                hit(node, "list nesting")
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_attr(o) == "chunk_kind" for o in operands):
                if owner.get(node) not in _KIND_ALLOWED:
                    hit(node, "chunk-kind dispatch")
            if any(_attr(o) in ("numbered_headings", "eq_numbers") for o in operands):
                hit(node, "xref / equation numbering")
        elif isinstance(node, ast.AugAssign):
            if _name(node.target) in ("figs", "counters"):
                hit(node, "xref numbering")
    return out


@pytest.mark.parametrize("backend", _BACKENDS)
def test_backend_holds_no_walker_decision(backend: str) -> None:
    bad = _violations((_EXPORT / backend).read_text(encoding="utf-8"))
    assert not bad, f"{backend} decides what the walker owns:\n" + "\n".join(bad)


def test_detector_catches_each_category() -> None:
    """The scan is not vacuous: each forbidden shape is caught."""
    sample = "\n".join(
        [
            "def f(ctx, c, chunks):",
            "    ctx.legacy_to_dc.get('h')",
            "    {c.handle: c.dc for c in chunks}",
            "    ctx.cited.append('a')",
            "    _abbrev.expand('t', {}, set())",
            "    ordered_blocks({})",
            "    counters = [0]",
            "    c.parent_chunk_id",
            "    c.chunk_kind == 'heading'",
            "    'x' in ctx.numbered_headings",
        ]
    )
    found = {v.split(": ", 1)[1] for v in _violations(sample)}
    assert found >= {
        "legacy-handle lookup",
        "legacy-handle map",
        "cite registration (append)",
        "abbreviation first-use decision",
        "back-matter selection",
        "walker decision state (counters)",
        "list nesting",
        "chunk-kind dispatch",
        "xref / equation numbering",
    }


def test_allowed_kind_dispatch_is_named_and_exists() -> None:
    source = (_EXPORT / "latex.py").read_text(encoding="utf-8")
    assert set(_KIND_ALLOWED) == {"_figure_logo_asset"}  # closed list
    for fn, reason in _KIND_ALLOWED.items():
        assert reason.startswith("gr") and f"def {fn}(" in source
