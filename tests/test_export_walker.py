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


def _chunk(kind: str, text: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        chunk_id=7,
        dc="dc7",
        handle="h7",
        chunk_kind=kind,
        depth=1,
        parent_chunk_id=None,
        text=text,
        meta=None,
    )


_TABLE = "| A | B |\n|---|---|\n| 1 | 2 |"

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
        (walker.Xref("dc2", None, live=True, label=("Section", "1")),),
    ),
    "back_matter": {
        "latex": ("acronyms", "\\printglossaries"),
        "docx": ("acronyms", True),
    },
    "title": dict.fromkeys(("latex", "docx"), (_chunk("heading", "Title"),)),
    "anchor": dict.fromkeys(("latex", "docx"), (_chunk("term", "metal"),)),
    "list_open": dict.fromkeys(("latex", "docx"), (_chunk("ulist", ""), "ulist", 1)),
    "list_close": dict.fromkeys(("latex", "docx"), ("olist",)),
    "list_item": dict.fromkeys(("latex", "docx"), (_chunk("item", "Item"), "olist", 2)),
    "heading": dict.fromkeys(
        ("latex", "docx"),
        (_chunk("heading", "Intro"), walker.HeadingInfo(0, "1", back_matter=False)),
    ),
    "figure": dict.fromkeys(("latex", "docx"), (_chunk("figure", "A caption."),)),
    "table": dict.fromkeys(("latex", "docx"), (_chunk("Table", _TABLE),)),
    "code": dict.fromkeys(("latex", "docx"), (_chunk("code", "x = 1"),)),
    "aside": dict.fromkeys(("latex", "docx"), (_chunk("aside", "Aside."),)),
    "equation": dict.fromkeys(
        ("latex", "docx"), (_chunk("paragraph", "$$x$$"), "x", 1, False)
    ),
    "paragraph": dict.fromkeys(("latex", "docx"), (_chunk("paragraph", "Plain."),)),
}

#: The ONLY renderer methods allowed to emit nothing (witty's ruling,
#: 2026-10-17); nothing joins this list without a new ruling.
_INTENTIONAL_NO_OPS: dict[tuple[str, str], str] = {
    ("docx", "anchor"): "docx has no phantom label",
    (
        "docx",
        "list_open",
    ): "docx styles each item from (kind, level); no container element",
    (
        "docx",
        "list_close",
    ): "docx styles each item from (kind, level); no container element",
}

_ABBREVS = {"MOF": "metal-organic framework"}


def _latex_output(method: str) -> str:
    ctx = latex._Ctx(
        keymap=latex._acronym_keymap(_ABBREVS),
        abbrevs=dict(_ABBREVS),
        known_handles=set(),
        withheld_figures=frozenset({"dc7"}),
    )
    sink: list[str] = []
    getattr(latex.LatexRenderer(ctx), method)(sink, *_SAMPLES[method]["latex"])
    return "".join(sink)


_BLOCK_METHODS = frozenset(
    {
        "back_matter",
        "title",
        "anchor",
        "list_open",
        "list_close",
        "list_item",
        "heading",
        "figure",
        "table",
        "code",
        "aside",
        "equation",
        "paragraph",
    }
)


def _docx_run(method: str) -> Any:
    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        abbrevs=dict(_ABBREVS),
        used_acr={"MOF"},
        withheld_figures=frozenset({"dc7"}),
        library_label="x",
        library_search_url="x",
    )
    doc = docx.Document()
    sink = doc if method in _BLOCK_METHODS else doc.add_paragraph()
    getattr(dx.DocxRenderer(ctx), method)(sink, *_SAMPLES[method]["docx"])
    return doc


def _docx_output(method: str) -> str:
    return "\n".join(p.text for p in _docx_run(method).paragraphs)


def _docx_emits(method: str) -> bool:
    doc = _docx_run(method)
    if method not in _BLOCK_METHODS:  # inline: runs in the sink paragraph
        return any(p.text for p in doc.paragraphs)
    return len(doc.element.body) > len(docx.Document().element.body)


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
    if ("docx", method) in _INTENTIONAL_NO_OPS:
        assert not _docx_emits(method), f"docx {method} is listed as a no-op"
    else:
        assert _docx_emits(method)


def test_no_op_list_names_protocol_methods_only() -> None:
    assert {m for _b, m in _INTENTIONAL_NO_OPS} <= set(_PROTOCOL)
    assert all(reason for reason in _INTENTIONAL_NO_OPS.values())


def test_cite_group_formats_only() -> None:
    assert _latex_output("cite_group") == r"\cite{smith20,jones21}"
    assert _docx_output("cite_group") == "[1, 2]"


def test_back_matter_formats_only() -> None:
    assert _latex_output("back_matter") == r"\printglossaries"
    assert _docx_output("back_matter") == "Acronyms\nMOF: metal-organic framework"


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
    nums = walker.XrefNumbers({"dc2": ("Section", "")}, set(), {"dc5": 1})
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


def test_merge_xref_runs_joins_refs_separated_by_connectors() -> None:
    items: list[str | tuple[str, ...]] = [
        "a ",
        ("x",),
        " ",
        ("y",),
        "",
        ("z",),
        ", and ",
        ("w",),
        " or ",
        ("v",),
    ]
    assert walker.merge_xref_runs(items) == ["a ", ("x", "y", "z", "w"), " or ", ("v",)]


def test_number_xrefs_reads_structure_only() -> None:
    from types import SimpleNamespace as C

    chunks = [
        C(dc="dc1", chunk_kind="heading", depth=0, text="T"),
        C(dc="dc2", chunk_kind="heading", depth=1, text="Intro"),
        C(dc="dc3", chunk_kind="paragraph", depth=2, text="$$x$$ [§a]"),
        C(dc="dc4", chunk_kind="paragraph", depth=2, text="$$y$$"),
        C(dc="dc5", chunk_kind="figure", depth=2, text="cap [§b]"),
        C(dc="dc6", chunk_kind="table", depth=2, text="| A |\n|---|\n| 1 |"),
        C(dc="dc7", chunk_kind="table", depth=2, text="| B |\n|---|\n| 2 |"),
    ]
    nums = walker.number_xrefs(chunks, "T", equation_of=latex._standalone_equation)
    assert nums.labels == {
        "dc2": ("Section", "1"),
        "dc3": ("Section", "1"),
        "dc4": ("Section", "1"),
        "dc5": ("Figure", "1"),
        "dc6": ("Table", "1"),
        "dc7": ("Table", "2"),
    }
    assert nums.numbered_headings == {"dc2"} and nums.equations == {"dc4": 1}
    # the section a target resolves to, by name — an equation never is one
    assert nums.names == {"dc2": "Intro", "dc3": "Intro"}
    assert nums.types["dc4"] == "equation"


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
    # An SI mark joins the run (gr725246); text between marks still splits it.
    assert walker.merge_adjacent(items) == [
        walker.CiteGroup(("x", "p", "z"), (1, 1, 1), (False, True, False)),
        " ",
        _g("w"),
    ]


def test_cite_run_collapses_same_paper_across_whitespace_only() -> None:
    run = walker.CiteRun()
    assert run.add("a", 1, si=False, chunk_id=None) == []
    assert run.gap(" ") == walker.CiteGroup(("a",), (1,), (False,), (None,))
    assert run.add("a", 1, si=False, chunk_id=None) == []  # collapsed
    assert run.gap(" and ") is None
    assert run.add("a", 1, si=False, chunk_id=None) == []
    assert run.flush() == walker.CiteGroup(("a",), (1,), (False,), (None,))


@pytest.mark.parametrize(
    ("prose", "want"),
    [
        ("(see \\cite{a} and ", "(see \\cite{a}"),
        ("(see [pa12] and ", "(see [pa12]"),
        ("A, B, and ", "A, B"),
        ("either X or ", "either X"),
        ("seen ", "seen"),
        ("brand and sand ", "brand and sand"),
        ("understand ", "understand"),
    ],
)
def test_trim_before_dropped_strips_stranded_joiner(prose: str, want: str) -> None:
    assert walker.trim_before_dropped(prose) == want


def _section_chunks() -> list[Any]:
    from types import SimpleNamespace as C

    return [
        C(dc="dc1", chunk_kind="heading", depth=0, text="One"),
        C(dc="dc2", chunk_kind="paragraph", depth=1, text="Setup."),
        C(dc="dc3", chunk_kind="heading", depth=1, text="Sub"),
        C(dc="dc4", chunk_kind="paragraph", depth=2, text="First."),
        C(dc="dc5", chunk_kind="paragraph", depth=2, text="Second."),
        C(dc="dc6", chunk_kind="figure", depth=2, text="cap"),
        C(dc="dc7", chunk_kind="paragraph", depth=2, text="$$x$$"),
        C(dc="dc8", chunk_kind="heading", depth=0, text="Two"),
        C(dc="dc9", chunk_kind="paragraph", depth=1, text="Other."),
        C(dc="dc10", chunk_kind="heading", depth=0, text="Acknowledgements"),
        C(dc="dc11", chunk_kind="paragraph", depth=1, text="Thanks."),
    ]


def test_relative_word_for_a_reference_into_the_own_section() -> None:
    """gr725243: a section reference to the citing chunk's own section or an
    ancestor is "above" / "below"; figures, equations, other sections and
    back matter keep their numbers."""
    nums = walker.number_xrefs(
        _section_chunks(), None, equation_of=latex._standalone_equation
    )

    def rel(target: str, citing: str) -> str | None:
        return walker.relative_word(target, citing, nums)

    assert rel("dc4", "dc5") == "above"  # same section, earlier
    assert rel("dc5", "dc4") == "below"  # same section, later
    assert rel("dc4", "dc4") == "above"  # the citing chunk itself
    assert rel("dc2", "dc5") == "above"  # an ancestor section (1 holds 1.1)
    assert rel("dc3", "dc5") == "above"  # the section's own heading
    assert rel("dc9", "dc4") is None  # another section
    assert rel("dc4", "dc2") is None  # a subsection of the citing section
    assert rel("dc6", "dc5") is None  # a figure
    assert rel("dc7", "dc5") is None  # a standalone equation
    assert rel("dc4", "dc11") is None  # citing chunk is in back matter
    assert rel("dc11", "dc4") is None  # target is in back matter
    assert walker.relative_word("dc4", "", nums) is None  # no citing chunk
    xr = walker.resolve_xref(
        "dc5", None, live=None, numbers=nums, warnings=[], citing="dc4"
    )
    assert xr.relative == "below" and xr.label is None
    assert (
        walker.resolve_xref(
            "dc5", "see", live=None, numbers=nums, warnings=[], citing="dc4"
        ).relative
        is None
    )  # an authored surface wins


# ── repeated citation marks (gr725253) ─────────────────────────────────


def _mark(
    *keys: str, si: bool = False, before: str = "a claim", after: str = ". More"
) -> walker.CiteGroup:
    return walker.CiteGroup(
        keys=tuple(keys),
        numbers=tuple(range(1, len(keys) + 1)),
        si=(si,) * len(keys),
        before=before,
        after=after,
    )


def test_repeat_marks_drop_an_equal_mark_until_another_one_prints() -> None:
    rep = walker.RepeatMarks()
    assert rep.admit(_mark("a")) is True
    assert rep.admit(_mark("a")) is False
    assert rep.admit(_mark("a")) is False  # compared with the printed one
    assert rep.admit(_mark("b")) is True  # a different set resets
    assert rep.admit(_mark("a")) is True
    # the set, not the order: [a, b] equals [b, a]
    assert rep.admit(_mark("a", "b")) is True
    assert rep.admit(_mark("b", "a")) is False


def test_repeat_marks_si_and_trust_and_switch() -> None:
    rep = walker.RepeatMarks()
    assert rep.admit(_mark("a")) is True
    assert rep.admit(_mark("a", si=True)) is True  # an SI postnote differs
    assert rep.admit(_mark("a", si=True), trusted=True) is True
    assert rep.admit(_mark("a", si=True)) is True  # not compared with it
    assert rep.admit(_mark("a", si=True)) is False
    off = walker.RepeatMarks(enabled=False)
    assert [off.admit(_mark("a")) for _ in range(3)] == [True] * 3


#: ``{}`` marks the repeated mark; True when the sentence leans on it (the
#: mark is kept), False when it may be dropped.
_LEAN_CASES = [
    ("B (see {}).", True),
    ("B ({}).", True),
    ("as in ref. {}.", True),
    ("(Fig. 2, {})", True),
    ("the model {}'s core", True),
    ("the model {}\u2019s core", True),
    ("... end. {} shows that", True),
    ("{} shows that", True),
    ("   {} shows that", True),
    ("first; {} second", True),
    ("as follows: {}", True),
    ("a dash \u2014 {}", True),
    ('he said "{}', True),
    ("he said \u201c{}", True),
    ("by {}", True),
    ("Refs. {}", True),
    ("(e.g. {})", True),
    ("(i.e., {}", True),
    ("Cf. {}", True),
    ("see also {}", True),
    ("the following {}", True),
    ("text {}. More", False),
    ("text {}, and", False),
    ("text {} more", False),
    ("text {})", False),
    ("text 2 {}.", False),
    ("a 50% {}.", False),
    ("(see x) {}.", False),
    ("as [3] {}.", False),
    ("quoted\u201d {}.", False),
    ('"quoted" {}.', False),
    ("**bold** {}.", False),
    ("NICS(1)$_{zz}$ {}.", False),
    ("a word-in {}.", False),
    ("a seeing {}.", False),  # not the word "see"
]


@pytest.mark.parametrize(("template", "keep"), _LEAN_CASES)
def test_leans_on_mark_decides_from_the_text_around_it(
    template: str, keep: bool
) -> None:
    ctx = walker.mark_context(*template.split("{}"))
    assert walker.leans_on_mark(*ctx) is keep
    # the same answer through the rule that drops marks
    rep = walker.RepeatMarks()
    assert rep.admit(_mark("a")) is True
    assert rep.admit(_mark("a", before=ctx[0], after=ctx[1])) is keep


def test_a_kept_repeat_is_the_mark_the_next_one_is_compared_with() -> None:
    rep = walker.RepeatMarks()
    assert rep.admit(_mark("a")) is True
    assert rep.admit(_mark("a", before="(see", after=")")) is True  # leaned on
    assert rep.admit(_mark("a")) is False  # equals the kept one
    assert rep.admit(_mark("b")) is True
    assert rep.admit(_mark("a", before="", after=" shows")) is True  # kept, and
    assert rep.admit(_mark("a")) is False  # compared with that one, not with b


def test_close_up_dropped_prose() -> None:
    assert walker.close_up_dropped("More ", ".") == ("More", ".")
    assert walker.close_up_dropped("shown ", ", and") == ("shown", ", and")
    assert walker.close_up_dropped("as ", " and") == ("as ", "and")
    assert walker.close_up_dropped("end ", "") == ("end", "")
    assert walker.close_up_dropped("see x and ", ".") == ("see x", ".")
    assert walker.close_up_dropped("a-", "b") == ("a-", "b")


# ── one number for two records of the same work (gr725253) ─────────────


class _WorkStore:
    """Papers by slug: ``(id, title, year, authors, doi)``."""

    def __init__(self, papers: dict[str, tuple]) -> None:
        from types import SimpleNamespace

        self._refs = {
            slug: SimpleNamespace(
                id=i,
                slug=slug,
                title=title,
                year=year,
                authors=authors,
                pdf_role=None,
            )
            for i, (slug, (title, year, authors, _doi)) in enumerate(
                papers.items(), start=1
            )
        }
        self._dois = {i: p[3] for i, p in enumerate(papers.values(), start=1)}

    def get_ref(self, *, kind: str, id: str) -> Any:
        return self._refs.get(id) if kind == "paper" else None

    def identifiers_for_refs(self, ids: list[int]) -> dict[int, dict[str, str]]:
        return {i: {"doi": self._dois[i]} for i in ids if self._dois.get(i)}


_SMITH = [{"family": "Smith", "given": "J."}]


def test_same_doi_shares_the_first_records_number_and_warns_once() -> None:
    store = _WorkStore(
        {
            "a1": ("Nanobuds", 2020, _SMITH, "10.1/X"),
            "b2": ("Nanobuds, preprint", 2019, _SMITH, "https://doi.org/10.1/x"),
            "c3": ("Other", 2021, _SMITH, "10.9/y"),
        }
    )
    cited: list[str] = []
    warnings: list[str] = []
    alias = walker.DoiAliases(store, cited, warnings)
    assert walker.register(cited, alias.canonical("a1")) == 1
    assert walker.register(cited, alias.canonical("b2")) == 1  # a1's number
    assert walker.register(cited, alias.canonical("b2")) == 1
    assert walker.register(cited, alias.canonical("c3")) == 2
    assert cited == ["a1", "c3"]
    assert warnings == ["references a1 and b2 share DOI 10.1/x: printed as one entry"]


def test_one_record_under_two_cite_keys_is_aliased_without_a_warning() -> None:
    # two cite keys (an alias) of one record: the same id, so the same DOI
    store = _WorkStore({"a1": ("Nanobuds", 2020, _SMITH, "10.1/X")})
    store._refs["a1alias"] = store._refs["a1"]
    cited: list[str] = []
    warnings: list[str] = []
    alias = walker.DoiAliases(store, cited, warnings)
    assert walker.register(cited, alias.canonical("a1")) == 1
    assert walker.register(cited, alias.canonical("a1alias")) == 1
    assert cited == ["a1"]
    assert warnings == []


def test_same_work_warning_for_unmergeable_records() -> None:
    store = _WorkStore(
        {
            "a1": ("Carbon  nanobuds!", 2020, _SMITH, "10.1/x"),
            "b2": ("carbon nanobuds", 2020, _SMITH, "10.2/other"),
            "c3": ("Carbon nanobuds", 2020, _SMITH, ""),
            "d4": ("Carbon nanobuds", 2021, _SMITH, ""),  # another year
            "e5": ("Carbon nanobuds", 2020, [{"family": "Jones"}], ""),
        }
    )
    cited = list(store._refs)
    alias = walker.DoiAliases(store, cited, [])
    assert alias.same_work_warnings() == [
        "references [1] and [2] look like the same work (a1, b2)",
        "references [1] and [3] look like the same work (a1, c3)",
        "references [2] and [3] look like the same work (b2, c3)",
    ]


# ── merged cross-reference runs ───────────────────────────────────────

_XREF_KINDS = {
    "s": ("section", "Section"),
    "u": ("subsection", "Section"),
    "w": ("subsubsection", "Section"),
    "f": ("figure", "Figure"),
    "t": ("table", "Table"),
    "e": ("equation", ""),
}


def _xr(spec: str) -> walker.Xref:
    """``f3`` a figure numbered 3, ``u2.1`` a subsection, ``e1`` an equation;
    ``s1/b`` another target (``b``) that prints section 1 too."""
    ref = spec.partition("/")[0]
    kind, noun = _XREF_KINDS[ref[0]]
    number = ref[1:]
    if kind == "equation":
        return walker.Xref(spec, None, live=True, equation=int(number), kind=kind)
    return walker.Xref(spec, None, live=True, label=(noun, number), kind=kind)


def _printed(parts: list[walker.XrefPart]) -> str:
    return "".join(p.text or p.number for p in parts)


#: ``(references in source order, cleveref's print, sentence start)``. The
#: prints are what ``pdflatex`` + cleveref (TeX Live 2026) gave for
#: ``\cref{...}`` / ``\Cref{...}`` over the same labels and the preamble's
#: ``\crefname`` lines.
_CLEVEREF = [
    (["s1"], "Section 1", False),
    (["s1", "s2"], "Sections 1 and 2", False),
    (["s1", "s2", "s4"], "Sections 1, 2 and 4", False),
    (["s1", "s2", "s3"], "Sections 1 to 3", False),
    (["f3", "f1", "f2"], "Figures 1 to 3", False),
    (["f4", "f2"], "Figures 2 and 4", False),
    (["f2", "f4", "f1"], "Figures 1, 2 and 4", False),
    (["f1", "f2", "f4", "f5"], "Figures 1, 2, 4 and 5", False),
    (["f10", "f9", "f2", "f1"], "Figures 1, 2, 9 and 10", False),
    (["s1", "s2", "s3", "s5"], "Sections 1 to 3 and 5", False),
    (["f1", "f2", "f3", "f5", "f7"], "Figures 1 to 3, 5 and 7", False),
    (["f1", "f2", "f3", "f5", "f6", "f7"], "Figures 1 to 3 and 5 to 7", False),
    (["s1", "s2", "f3"], "Sections 1 and 2 and Figure 3", False),
    (["f3", "s2", "s1"], "Figure 3 and Sections 1 and 2", False),
    (["f1", "s1", "f2", "s2"], "Figures 1 and 2 and Sections 1 and 2", False),
    (["t1", "t2"], "Tables 1 and 2", False),
    (["t1", "f1", "e1", "s1"], "Table 1, Figure 1, Eq. (1), and Section 1", False),
    (
        ["t1", "t2", "f1", "s1", "s2"],
        "Tables 1 and 2, Figure 1, and Sections 1 and 2",
        False,
    ),
    # the section counters are separate cleveref types, each with its name
    (["s2", "u2.1"], "Section 2 and Section 2.1", False),
    (["s2", "u2.1", "w2.1.1"], "Section 2, Section 2.1, and Section 2.1.1", False),
    (["u12.9", "u12.10", "u12.11"], "Sections 12.9 to 12.11", False),
    (["u1.2", "u1.3", "u2.1"], "Sections 1.2, 1.3 and 2.1", False),
    (["u1.1", "u1.2", "u1.3"], "Sections 1.1 to 1.3", False),
    (["e1", "e2"], "Eqs. (1) and (2)", False),
    (["e1", "e2", "e3"], "Eqs. (1) to (3)", False),
    (["e1", "e2", "e3", "e5"], "Eqs. (1) to (3) and (5)", False),
    (["e1", "e2", "e3"], "Equations (1) to (3)", True),
    (["e1"], "Equation (1)", True),
    (["s1", "e1"], "Section 1 and Eq. (1)", True),
    (["e1", "s1"], "Equation (1) and Section 1", True),
    (["f1", "e1", "e2"], "Figure 1 and Eqs. (1) and (2)", True),
    (["f1", "s1"], "Figure 1 and Section 1", True),
    # repeats: a target twice is one number; two targets printing one number
    # are one number too; every reference counts toward a three-long stretch
    (["s1", "s1"], "Section 1", False),
    (["s1", "s1/b"], "Section 1", False),
    (["s1", "s2", "s1"], "Sections 1 to 2", False),
    (["s1", "s1/b", "s2"], "Sections 1 to 2", False),
    (["s1", "s1/b", "s1/c"], "Section 1", False),
    (["s1", "s1/b", "s4"], "Sections 1 and 4", False),
    (["s1", "s1/b", "s2", "s2/b", "s5", "s6"], "Sections 1 to 2, 5 and 6", False),
]


@pytest.mark.parametrize(("specs", "printed", "sentence"), _CLEVEREF)
def test_format_xref_run_prints_what_cleveref_prints(
    specs: list[str], printed: str, sentence: bool
) -> None:
    parts = walker.format_xref_run([_xr(s) for s in specs], sentence_start=sentence)
    assert _printed(parts) == printed


def test_format_xref_run_numbers_are_fields_to_the_first_and_last_target() -> None:
    parts = walker.format_xref_run([_xr("f3"), _xr("f1"), _xr("f2")])
    assert [(p.dc, p.number, p.noun) for p in parts if p.dc] == [
        ("f1", "1", "Figure"),
        ("f3", "3", "Figure"),
    ]  # the middle figure gets none
    parts = walker.format_xref_run([_xr("s1"), _xr("s2"), _xr("s4"), _xr("f1")])
    assert [p.dc for p in parts if p.dc] == ["s1", "s2", "s4", "f1"]
    assert not [p for p in parts if p.dc and p.text]
    parts = walker.format_xref_run([_xr("e2"), _xr("e1")])
    assert [(p.dc, p.noun) for p in parts if p.dc] == [("e1", ""), ("e2", "")]


def test_xref_names_are_the_preambles_crefnames() -> None:
    pre = latex._template_text("preamble.tex")
    for kind, (one, many) in walker.XREF_NAMES.items():
        assert rf"\crefname{{{kind}}}{{{one}}}{{{many}}}" in pre
    for kind, (one, many) in walker.XREF_NAMES_CAPITAL.items():
        assert rf"\Crefname{{{kind}}}{{{one}}}{{{many}}}" in pre


def test_only_typed_numbered_live_references_may_merge() -> None:
    from types import SimpleNamespace as C

    chunks = [
        C(dc="dc1", chunk_kind="heading", depth=0, text="T"),
        C(dc="dc2", chunk_kind="heading", depth=1, text="One"),
        C(dc="dc3", chunk_kind="paragraph", depth=2, text="Setup."),
        C(dc="dc4", chunk_kind="heading", depth=2, text="Sub"),
        C(dc="dc5", chunk_kind="paragraph", depth=3, text="In the sub."),
        C(dc="dc6", chunk_kind="item", depth=3, text="an item"),
        C(dc="dc7", chunk_kind="listing", depth=3, text="code"),
        C(dc="dc8", chunk_kind="heading", depth=4, text="Run-in"),
        C(dc="dc9", chunk_kind="figure", depth=3, text="cap"),
        C(dc="dc10", chunk_kind="paragraph", depth=3, text="$$x$$"),
        C(dc="dc11", chunk_kind="heading", depth=0, text="Acknowledgements"),
    ]
    nums = walker.number_xrefs(chunks, "T", equation_of=latex._standalone_equation)
    kw: dict[str, Any] = {"live": None, "numbers": nums, "warnings": []}

    def kind(dc: str, **extra: Any) -> str | None:
        return walker.resolve_xref(dc, None, **kw, **extra).kind

    assert kind("dc2") == "section"
    assert kind("dc3") == "section"  # a paragraph takes its section's type
    assert kind("dc4") == "subsection" and kind("dc5") == "subsection"
    assert kind("dc9") == "figure" and kind("dc10") == "equation"
    assert kind("dc6") is None and kind("dc7") is None  # item, listing
    assert kind("dc8") is None  # a run-in heading (LaTeX: \nameref)
    assert kind("dc11") is None  # back matter prints its name
    assert kind("dc1") is None  # the title: no number
    assert walker.resolve_xref("dc2", "the intro", **kw).kind is None  # surface
    assert kind("dc3", placement=nums, citing="dc3") is None  # "above"
    dead = walker.resolve_xref("dc99", None, live={"dc2"}, numbers=nums, warnings=[])
    assert dead.kind is None


def test_merge_xref_runs_is_broken_by_prose_cites_and_own_section_words() -> None:
    items: list[str | tuple[str, ...]] = [
        ("a",),
        " ",
        ("b",),
        " or ",
        ("c",),
        r"\cite{k} ",
        ("d",),
        " \\precisrel{above} ",
        ("e",),
    ]
    assert walker.merge_xref_runs(items) == [
        ("a", "b"),
        " or ",
        ("c",),
        r"\cite{k} ",
        ("d",),
        " \\precisrel{above} ",
        ("e",),
    ]


# ── Word and PDF merge the same references ───────────────────────────

_PARITY_CHUNKS = [
    ("dc1", "heading", 0, "T"),
    ("dc2", "heading", 1, "A"),  # section 1
    ("dc3", "heading", 1, "B"),  # 2
    ("dc4", "heading", 2, "B1"),  # 2.1
    ("dc5", "heading", 1, "C"),  # 3
    ("dc6", "heading", 1, "D"),  # 4
    ("dc7", "figure", 2, "one"),  # Figure 1
    ("dc8", "figure", 2, "two"),
    ("dc9", "figure", 2, "three"),
    ("dc10", "table", 2, "| A |\n|---|\n| 1 |"),  # Table 1
    ("dc11", "paragraph", 2, "$$x$$"),  # Eq. (1)
    ("dc12", "paragraph", 2, "$$y$$"),  # Eq. (2)
    ("dc13", "heading", 1, "E"),  # 5
    ("dc14", "paragraph", 2, "The citing paragraph."),  # in section 5
]

#: ``(source, what Word prints)``; every run LaTeX merges must print the same
#: in Word, and what LaTeX leaves apart (``or``, a semicolon, a reference
#: into the citing chunk's own section) stays apart there too. Word prints
#: its default "short" label style (:data:`docx._SHORT_XREF_NAMES`); the
#: spellings differ from cleveref's ``\crefname`` table by design
#: (``xref_labels="full"`` opts back in) — the parity under test is the
#: grouping and order, so the closure below re-prints through the same
#: short names. Both backends locate a referenced section for the reader
#: (gr690475): the PDF's ``(\cpageref)`` page marker is Word's
#: ``(heading name)``.
_PARITY = [
    ("see [dc2] [dc3]", "see sections 1 (A) and 2 (B)"),
    ("see [dc2][dc3] [dc6]", "see sections 1 (A), 2 (B) and 4 (D)"),
    ("see [dc2] [dc3] [dc5]", "see sections 1 (A) to 3 (C)"),
    ("see [dc9] [dc7] [dc8]", "see figs. 1 to 3"),
    ("see [dc2] [dc3] [dc9]", "see sections 1 (A) and 2 (B) and fig. 3"),
    (
        "see [dc10] [dc7] [dc11] [dc3]",
        "see table 1, fig. 1, Eq. (1), and section 2 (B)",
    ),
    ("see [dc3] [dc4]", "see section 2 (B) and section 2.1 (B1)"),
    ("[dc11] [dc12] hold.", "Equations (1) and (2) hold."),
    ("by [dc11] [dc12]", "by Eqs. (1) and (2)"),
    ("see [dc2] [dc13] [dc3]", "see section 1 (A) above section 2 (B)"),
    ("see [dc7]", "see fig. 1"),
    ("see [dc2] and [dc3]", "see sections 1 (A) and 2 (B)"),
    ("see [dc2], [dc3]", "see sections 1 (A) and 2 (B)"),
    ("see [dc2], [dc3] and [dc6]", "see sections 1 (A), 2 (B) and 4 (D)"),
    ("see [dc2], [dc3], and [dc5]", "see sections 1 (A) to 3 (C)"),
    ("see [dc7] and [dc10]", "see fig. 1 and table 1"),
    ("[dc7] and [dc8] show", "Figs. 1 and 2 show"),
    ("see [dc2] or [dc3]", "see section 1 (A) or section 2 (B)"),
    ("see [dc2]; [dc3]", "see section 1 (A); section 2 (B)"),
    ("see [dc2] and [dc13]", "see section 1 (A) and above"),
    ("see [dc13] and [dc2]", "see above and section 1 (A)"),
    ("[dc2] [dc3] show it.", "Sections 1 (A) and 2 (B) show it."),
]


@pytest.mark.parametrize(("source", "printed"), _PARITY)
def test_word_and_pdf_merge_the_same_cross_references(
    source: str, printed: str
) -> None:
    import re
    from types import SimpleNamespace as C

    chunks = [C(dc=dc, chunk_kind=k, depth=d, text=t) for dc, k, d, t in _PARITY_CHUNKS]
    nums = walker.number_xrefs(chunks, "T", equation_of=latex._standalone_equation)
    handles = {c.dc for c in chunks}

    lctx = latex._Ctx(keymap={}, known_handles=handles, xref_pass=nums)
    lctx.cursor.dc = "dc14"
    tex = latex._render_inline(source, lctx)

    dctx = dx._Ctx(
        store=None,
        known_handles=set(),
        xref=nums.labels,
        xref_types=nums.types,
        xref_order=nums.order,
        xref_section=nums.section,
        xref_names=nums.names,
        numbered_headings=nums.numbered_headings,
        eq_numbers=nums.equations,
        dc_handles=handles,
    )
    dctx.cursor.dc = "dc14"
    paragraph = docx.Document().add_paragraph()
    dx._render_inline(source, dctx, paragraph)
    assert paragraph.text == printed

    # LaTeX's \cref arguments, printed by the one formatter: the same members
    # in the same groups and order as Word's runs, each section's number
    # followed by its name as Word prints it (the PDF's \cpageref mark is
    # checked separately below). Word's short label style names keep the
    # comparison about grouping, not spelling.
    def cleveref(m: re.Match[str]) -> str:
        run = [
            walker.resolve_xref(
                a.removeprefix("chunk:"), None, live=None, numbers=nums, warnings=[]
            )
            for a in m.group(2).split(",")
        ]
        return "".join(
            (p.text or p.number)
            + (f" ({nums.names[p.dc]})" if p.dc in nums.names else "")
            for p in walker.format_xref_run(
                run,
                sentence_start=m.group(1) == "C",
                names=dx._SHORT_XREF_NAMES,
                capital_names=dx._SHORT_XREF_CAPITAL,
            )
        )

    unmarked = re.sub(r"~?\(\\cpageref\{[^{}]*\}\)", "", tex)
    assert re.sub(r"\\([cC])ref\{([^{}]*)\}", cleveref, unmarked) == printed

    # Every \cpageref lists exactly its \cref mark's section-resolving
    # targets (gr690475).
    paged = {dc for dc, t in nums.types.items() if t in walker.XREF_SECTION_TYPES}
    marks = re.findall(r"\\[cC]ref\{([^{}]*)\}", tex)
    assert re.findall(r"\\cpageref\{([^{}]*)\}", tex) == [
        pages
        for pages in (
            ",".join(a for a in mark.split(",") if a.removeprefix("chunk:") in paged)
            for mark in marks
        )
        if pages
    ]


@pytest.mark.parametrize(
    ("gap", "joins"),
    [
        ("", True),
        (" ", True),
        ("\n", True),
        (",", True),
        (", ", True),
        (" and ", True),
        (", and ", True),
        (" ,  and  ", True),
        (" or ", False),
        (", or ", False),
        (" & ", False),
        ("; ", False),
        (" to ", False),
        (" through ", False),
        (" \u2013 ", False),
        (" and also ", False),
        (" and, ", False),
        ("and", False),
        (",and ", False),
        (" And ", False),
        (".", False),
        ("\n\n", False),
        (", and\n\n", False),
    ],
)
def test_xref_connectors_that_join_a_run(gap: str, joins: bool) -> None:
    out = walker.merge_xref_runs([("a",), gap, ("b",)])
    assert (out == [("a", "b")]) is joins
    assert walker.xref_connector(gap) is joins
