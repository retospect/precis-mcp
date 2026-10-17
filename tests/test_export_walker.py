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

from precis.export import _abbrev, latex, walker
from precis.export import docx as dx

_PROTOCOL = sorted(
    name
    for name, member in vars(walker.Renderer).items()
    if inspect.isfunction(member) and not name.startswith("_")
)

#: Resolved arguments per protocol method, per backend (the back-matter
#: payload is each backend's own: pre-rendered TeX, or a docx marker).
_SAMPLES: dict[str, dict[str, tuple[Any, ...]]] = {
    "cite_group": dict.fromkeys(
        ("latex", "docx"),
        (
            walker.CiteGroup(
                ("smith20", "jones21"), (1, 2), (False, False), (None, None)
            ),
        ),
    ),
    "term": dict.fromkeys(("latex", "docx"), (_abbrev.Use("MOF", mode="first"),)),
    "xref": dict.fromkeys(
        ("latex", "docx"),
        (walker.Xref("dc2", None, live=True, label=("section", "1")),),
    ),
    "back_matter": {
        "latex": ("acronyms", "\\printglossaries"),
        "docx": ("acronyms", True),
    },
}

_ABBREVS = {"MOF": "metal-organic framework"}


def _latex_output(method: str) -> str:
    ctx = latex._Ctx(
        keymap=latex._acronym_keymap(_ABBREVS),
        abbrevs=dict(_ABBREVS),
        known_handles=set(),
    )
    sink: list[str] = []
    getattr(latex.LatexRenderer(ctx), method)(sink, *_SAMPLES[method]["latex"])
    return "".join(sink)


def _docx_output(method: str) -> str:
    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        abbrevs=dict(_ABBREVS),
        used_acr={"MOF"},
        library_label="x",
        library_search_url="x",
    )
    doc = docx.Document()
    sink = doc if method == "back_matter" else doc.add_paragraph()
    getattr(dx.DocxRenderer(ctx), method)(sink, *_SAMPLES[method]["docx"])
    return "\n".join(p.text for p in doc.paragraphs)


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


def test_back_matter_formats_only() -> None:
    assert _latex_output("back_matter") == r"\printglossaries"
    assert _docx_output("back_matter") == "Acronyms\nMOF — metal-organic framework"


def test_back_matter_blocks_follow_mode_and_order() -> None:
    blocks: dict[str, str | None] = {
        n: n for n in ("published", "references", "acronyms", "unverified")
    }
    blocks["data_package"] = None
    names = [n for n, _ in walker.back_matter(blocks, patent=False)]
    assert names == ["unverified", "references", "acronyms", "published"]
    patent = [
        n for n, _ in walker.back_matter(blocks, patent=True, claim_appendix=False)
    ]
    assert patent == ["unverified", "acronyms"]


def test_claim_keys_never_mint_a_citation() -> None:
    seen: list[str] = []
    walker.record_claim_keys(seen, ["b", "x", "a", "b"], ["a", "b"], str.lower)
    assert seen == ["b", "a"]
    assert walker.claim_source_numbers(seen, ["a", "b"]) == [1, 2]
    assert walker.acronym_list({"dft", "MOF", "zz"}, {"MOF": "m", "dft": "d"}) == [
        "dft",
        "MOF",
    ]


def test_term_formats_only() -> None:
    assert _latex_output("term") == r"\gls{mof}"
    assert _docx_output("term") == "metal-organic framework (MOF)"


def test_xref_formats_only() -> None:
    assert _latex_output("xref") == r"\cref{chunk:dc2}"
    assert _docx_output("xref") == "Section 1"  # paragraph start


def test_resolve_xref_downgrades_dead_and_skips_unnumbered() -> None:
    nums = walker.XrefNumbers({"dc2": ("section", "")}, set(), {"dc5": 1})
    warnings: list[str] = []
    dead = walker.resolve_xref(
        "dc9", None, live={"dc2", "dc5"}, numbers=nums, warnings=warnings
    )
    assert not dead.live and warnings == [
        "cross-ref dc9: no such live chunk — downgraded"
    ]
    kw: dict[str, Any] = {"live": None, "numbers": nums, "warnings": warnings}
    assert walker.resolve_xref("dc5", None, **kw).equation == 1
    assert walker.resolve_xref("dc2", None, **kw).label is None
    assert walker.resolve_xref("dc2", "see", **kw).surface == "see"


def test_merge_xref_runs_joins_whitespace_separated_refs() -> None:
    items: list[str | tuple[str, ...]] = [
        "a ",
        ("x",),
        " ",
        ("y",),
        "",
        ("z",),
        ", ",
        ("w",),
    ]
    assert walker.merge_xref_runs(items) == ["a ", ("x", "y", "z"), ", ", ("w",)]


def test_number_xrefs_reads_structure_only() -> None:
    from types import SimpleNamespace as C

    chunks = [
        C(dc="dc1", chunk_kind="heading", depth=0, text="T"),
        C(dc="dc2", chunk_kind="heading", depth=1, text="Intro"),
        C(dc="dc3", chunk_kind="paragraph", depth=2, text="$$x$$ [§a]"),
        C(dc="dc4", chunk_kind="paragraph", depth=2, text="$$y$$"),
        C(dc="dc5", chunk_kind="figure", depth=2, text="cap [§b]"),
    ]
    nums = walker.number_xrefs(chunks, "T", equation_of=latex._standalone_equation)
    assert nums.labels == {
        "dc2": ("section", "1"),
        "dc3": ("section", "1"),
        "dc4": ("section", "1"),
        "dc5": ("fig.", "1"),
    }
    assert nums.numbered_headings == {"dc2"} and nums.equations == {"dc4": 1}


def test_term_segments_defines_once_and_never_in_a_heading() -> None:
    seen: set[str] = set()
    used: set[str] = set()
    head = walker.term_segments("MOF", _ABBREVS, seen, used, preceding="", heading=True)
    assert head == [_abbrev.Use("MOF", mode="bare")] and not seen
    body = walker.term_segments(
        "a MOF and MOF", _ABBREVS, seen, used, preceding="", heading=False
    )
    assert [s.mode for s in body if isinstance(s, _abbrev.Use)] == ["first", "later"]
    assert seen == used == {"MOF"}


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
