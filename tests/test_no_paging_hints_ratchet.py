"""Search no longer paginates: no emitted hint or skill may advertise it.

Pins (a) no ``page=N`` / ``more(cursor`` string constant in ``src/precis``
(docstrings and comments are documentation, not emitted text) or in the
skills, and (b) the paging refusals' ``next=`` names the caller's kind
rather than a hardcoded ``paper``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import precis.tools.core as tools_core
from tests.test_mcp_slim_schema import _call

_SRC = Path(__file__).resolve().parent.parent / "src" / "precis"
_BANNED = ("page=N", "more(cursor")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                ids.add(id(body[0].value))
    return ids


def test_no_paging_strings_in_emitted_python() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and any(b in node.value for b in _BANNED)
            ):
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert not offenders, offenders


def test_no_paging_strings_in_skills() -> None:
    offenders = [
        p.name
        for p in sorted((_SRC / "data" / "skills").glob("*.md"))
        if any(b in p.read_text(encoding="utf-8") for b in _BANNED)
    ]
    assert not offenders, offenders


@pytest.mark.parametrize("kind", ["memory", "gripe", "finding"])
def test_paging_refusal_next_names_callers_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    def unexpected(*_a: object, **_k: object) -> str:
        raise AssertionError("refusal reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    page = _call("search", {"kind": kind, "q": "x", "page": 2})
    nested = _call("search", {"kind": kind, "q": "x", "args": {"offset": 3}})
    top = tools_core.search(kind=kind, q="x", offset=5)
    for raw in (page, nested, top):
        out = raw if isinstance(raw, str) else raw.content[0].text
        assert "[error:BadInput]" in out, out
        assert f"kind='{kind}'" in out, out
        assert "kind='paper'" not in out, out
