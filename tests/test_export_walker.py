"""The export walker's :class:`~precis.export.walker.Renderer` protocol:
both backends implement every method, and each method emits output.

A method added to the protocol without a renderer (or without a sample
here) fails this test. The byte-identical check of real exports is
``tests/test_export_golden.py``."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

docx = pytest.importorskip("docx")

from precis.export import docx as dx
from precis.export import latex, walker

_PROTOCOL = sorted(
    name
    for name, member in vars(walker.Renderer).items()
    if inspect.isfunction(member) and not name.startswith("_")
)

#: One resolved argument per protocol method.
_SAMPLES: dict[str, Any] = {
    "cite_group": walker.CiteGroup(
        ("smith20", "jones21"), (1, 2), (False, False), (None, None)
    ),
}


def _latex_output(method: str) -> str:
    ctx = latex._Ctx(keymap={}, known_handles=set())
    sink: list[str] = []
    getattr(latex.LatexRenderer(ctx), method)(sink, _SAMPLES[method])
    return "".join(sink)


def _docx_output(method: str) -> str:
    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        library_label="x",
        library_search_url="x",
    )
    para = docx.Document().add_paragraph()
    getattr(dx.DocxRenderer(ctx), method)(para, _SAMPLES[method])
    return para.text


def test_protocol_has_methods() -> None:
    assert _PROTOCOL


@pytest.mark.parametrize("method", _PROTOCOL)
def test_every_protocol_method_has_a_sample(method: str) -> None:
    assert method in _SAMPLES, f"add a sample argument for Renderer.{method}"


@pytest.mark.parametrize("method", _PROTOCOL)
def test_both_backends_implement_and_emit(method: str) -> None:
    for cls in (latex.LatexRenderer, dx.DocxRenderer):
        assert callable(getattr(cls, method, None)), f"{cls.__name__}.{method}"
    assert _latex_output(method)
    assert _docx_output(method)


def test_cite_group_formats_only() -> None:
    assert _latex_output("cite_group") == r"\cite{smith20,jones21}"
    assert _docx_output("cite_group") == "[1, 2]"


def _g(*keys: str, si: bool = False) -> walker.CiteGroup:
    return walker.CiteGroup(
        keys, tuple(range(1, len(keys) + 1)), tuple(si for _ in keys)
    )


def test_merge_adjacent_merges_plain_runs_and_dedupes() -> None:
    out = walker.merge_adjacent(["a ", _g("x"), "", _g("x", "y"), " b"])
    assert out == ["a ", walker.CiteGroup(("x", "y"), (1, 2), (False, False)), " b"]


def test_merge_adjacent_keeps_si_and_text_separators() -> None:
    items: list[str | walker.CiteGroup] = [
        _g("x"),
        _g("p", si=True),
        _g("z"),
        " ",
        _g("w"),
    ]
    assert walker.merge_adjacent(items) == items


def test_cite_run_collapses_same_paper_across_whitespace_only() -> None:
    run = walker.CiteRun()
    assert run.add("a", 1, si=False, chunk_id=None) == []
    assert run.gap(" ") == walker.CiteGroup(("a",), (1,), (False,), (None,))
    assert run.add("a", 1, si=False, chunk_id=None) == []  # collapsed
    assert run.gap(" and ") is None
    assert run.add("a", 1, si=False, chunk_id=None) == []
    assert run.flush() == walker.CiteGroup(("a",), (1,), (False,), (None,))
