"""Pure (no-DB) tests for the LaTeX→draft importer.

Covers the structural walker, the container-list model, inline de-macro
(cites/glossary/values), and the deferred cross-reference round-trip
(forward refs included). The DB writer (`run_import`) is exercised by the
integration gate.
"""

from __future__ import annotations

from precis.draftimport.demacro import (
    demacro,
    harvest_macros,
    harvest_param_macros,
    labels_in,
    resolve_deferred,
    strip_annotations,
)
from precis.draftimport.tex import Chunk, walk_document


def _kinds(node: Chunk) -> list[str]:
    out = []
    for c in node.children:
        out.append(c.kind)
        out.extend(_kinds(c))
    return out


def test_walk_nests_headings_and_lists() -> None:
    body = r"""
\section{Intro}
First para.

\subsection{Detail}
\begin{itemize}
\item one
\item two
\end{itemize}
"""
    tree = walk_document(body)
    # Intro(heading) > [para, Detail(heading) > [ulist > item,item]]
    intro = tree.children[0]
    assert intro.kind == "heading" and intro.text == "Intro"
    detail = intro.children[-1]
    assert detail.kind == "heading" and detail.text == "Detail"
    ulist = detail.children[0]
    assert ulist.kind == "ulist"
    assert [c.kind for c in ulist.children] == ["item", "item"]


def test_bare_tabular_becomes_table_not_paragraph() -> None:
    body = r"\section{S}\begin{tabular}{ll}a & b\\\end{tabular}"
    kinds = _kinds(walk_document(body))
    assert "table" in kinds
    assert "tabular" not in " ".join(kinds)


def test_bib_paths_in_reads_declared_bibs(tmp_path) -> None:
    """The bibliography is resolved from the document's own
    \\bibliography{}/\\addbibresource{} command (master-relative), not a
    directory glob — so a sibling references/ folder is found and a shared
    stray .bib isn't mis-grabbed."""
    from precis.draftimport.tex import bib_paths_in

    (tmp_path / "references").mkdir()
    real = tmp_path / "references" / "references.bib"
    real.write_text("@article{k, title={T}}")
    (tmp_path / "docs").mkdir()
    # \bibliography path is relative to the master dir (here tmp_path)
    body = r"text \cite{k}. \bibliography{references/references}"
    assert bib_paths_in(body, tmp_path) == [real.resolve()]
    # bare name + comma list + addbibresource all resolve; missing ones drop
    (tmp_path / "extra.bib").write_text("@book{b,}")
    body2 = r"\addbibresource{extra.bib}\bibliography{references/references,missing}"
    assert bib_paths_in(body2, tmp_path) == [
        (tmp_path / "extra.bib").resolve(),
        real.resolve(),
    ]


def test_resolve_key_matches_cite_key_alias() -> None:
    """A plain bib key (no DOI/arXiv shape) resolves by matching a paper's
    ``cite_key`` alias directly — not only via the .bib's DOI."""
    import types

    from precis.draftimport.resolve import resolve_key

    calls = {"ident": [], "cite_key": []}

    def find_paper_ref_by_identifier(value):
        calls["ident"].append(value)
        return None  # plain key detects as no scheme

    def find_ref_by_identifier(scheme, value, *, kind=None):
        calls["cite_key"].append((scheme, value, kind))
        return (
            5155
            if (scheme == "cite_key" and value == "zhao2024PdInCo" and kind == "paper")
            else None
        )

    def _ident_for_cite_key(ref_id, *a, **k):
        return [("cite_key", "zhao2024pdinco")]

    store = types.SimpleNamespace(
        find_paper_ref_by_identifier=find_paper_ref_by_identifier,
        find_ref_by_identifier=find_ref_by_identifier,
        ref_identifiers_for=_ident_for_cite_key,
        insert_ref_identifiers=lambda *a, **k: None,
        pool=types.SimpleNamespace(),
    )
    # _ref_cite_key reads the slug via the pool; stub it on the namespace path
    import precis.draftimport.resolve as R

    orig = R._ref_cite_key
    R._ref_cite_key = lambda s, rid: "zhao2024pdinco"
    try:
        slug, via, ref_id = resolve_key(store, "zhao2024PdInCo", None)
    finally:
        R._ref_cite_key = orig
    assert ref_id == 5155 and via == "cite_key"
    assert ("cite_key", "zhao2024PdInCo", "paper") in calls["cite_key"]


def test_demacro_cite_uses_keymap() -> None:
    out = demacro(r"see \cite{miller2012}.", keymap={"miller2012": "miller12"})
    assert "[§miller12]" in out
    assert "miller2012" not in out


def test_custom_cite_macro_keys_are_collected() -> None:
    """A custom macro that expands to a cite (methane's
    \\deepcite{key}{page}{quote}) must surface its key through the
    cite_resolver — that's how run_import builds the keymap, so a key missed
    here leaks as a dangling [§key]."""
    pmac = harvest_param_macros(
        r"\newcommand{\deepcite}[3]{``#3'' --- \cite{#1}, p.~#2}"
    )
    seen: list[str] = []

    def collect(c):  # mirrors run_import's key collector
        seen.extend(c.keys)
        return ""

    demacro(
        r"\deepcite{he2023methanotrophica}{1}{A quote.}",
        param_macros=pmac,
        cite_resolver=collect,
    )
    assert seen == ["he2023methanotrophica"]


def test_demacro_param_macro_from_definition() -> None:
    macros = harvest_macros("")
    pmac = harvest_param_macros(r"\newcommand{\POR}[1]{\textbf{Plan of Record:} #1}")
    out = demacro(r"\POR{ship it}", macros=macros, param_macros=pmac)
    assert out == "Plan of Record: ship it"


def test_forward_ref_round_trip() -> None:
    # \ref points *forward* to a label created later; resolve after the fact.
    raw = r"See Section~\ref{sec:later}. \label{sec:here}"
    assert labels_in(raw) == ["sec:here"]
    cleaned = demacro(raw)
    assert "[¶@sec:later]" in cleaned
    # writing pass builds the full label map across all chunks (dc<id> handles),
    # then resolves to the single-bracket [[handle]] form:
    label_map = {"sec:later": "dc742", "sec:here": "dc31"}
    missing: list[str] = []
    final = resolve_deferred(cleaned, labels=label_map, unresolved=missing)
    assert "[dc742]" in final
    assert not missing


def test_unresolved_ref_degrades_and_is_flagged() -> None:
    cleaned = demacro(r"see \cref{fig:dropped}.")
    missing: list[str] = []
    final = resolve_deferred(cleaned, labels={}, unresolved=missing)
    assert "[¶@" not in final  # no dangling token
    assert missing == ["fig:dropped"]


def test_limit_sections_truncates_to_n_headings() -> None:
    from precis.draftimport.build import _limit_sections

    body = r"Front matter.\section{A}a\section{B}b\section{C}c"
    tree = _limit_sections(walk_document(body), 2)
    headings = [c.text for c in tree.children if c.kind == "heading"]
    assert headings == ["A", "B"]  # C dropped; front matter kept


def test_extract_annotations_captures_notes_as_sentinels() -> None:
    from precis.draftimport.demacro import extract_annotations

    body = (
        r"Para. \mtechq{TQ-1}{Largest cage?}"
        "\n\n"
        r"More. \mrev{RIG-1}{MAJOR}{Overlaps logic-switching.}"
    )
    out, notes = extract_annotations(body)
    assert "⟦note:0⟧" in out and "⟦note:1⟧" in out
    assert notes[0] == {"type": "techq", "code": "TQ-1", "text": "Largest cage?"}
    assert notes[1] == {
        "type": "review",
        "code": "RIG-1",
        "sev": "major",
        "text": "Overlaps logic-switching.",
    }
    # the macros themselves are gone from the prose
    assert "mtechq" not in out and "mrev" not in out


def test_strip_annotations_handles_multiparagraph_arg() -> None:
    # a \mtechq whose 2nd arg spans a blank line must be removed whole,
    # not split mid-argument by the block splitter.
    raw = "Before.\n\n\\mtechq{TQ-1}{A question.\n\nWith two paragraphs.}\n\nAfter."
    out = strip_annotations(raw)
    assert "mtechq" not in out
    assert "Before." in out and "After." in out
