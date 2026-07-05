"""End-to-end lock: the LaTeX importer no longer mints `equation` chunks.

Math — display environments, ``\\[..\\]``, and ``$$..$$`` — must land as
``paragraph`` chunks carrying KaTeX-renderable ``$$ … $$`` text (the reader's
delimiters), with no ``\\label`` and no ``chunk_kind='equation'`` row. Inline
``$…$`` stays verbatim inside its prose paragraph (unchanged behaviour).

DB-gated (uses the ``store`` fixture) — runs in the integration gate.
"""

from __future__ import annotations

from pathlib import Path

from precis.draftimport.build import run_import
from precis.store import Store

_TEX = r"""
\documentclass{article}
\begin{document}
\section{Intro}
The mass-energy relation is $E=mc^2$ in prose.

\begin{equation}
\label{eq:euler}
\chi = V - E + F
\end{equation}

A displayed system:
\[
a &= b \\ c &= d
\]
\end{document}
"""


def _write_tex(tmp_path: Path) -> Path:
    main = tmp_path / "main.tex"
    main.write_text(_TEX, encoding="utf-8")
    return main


def test_importer_emits_math_as_dollar_paragraphs(store: Store, tmp_path: Path) -> None:
    main = _write_tex(tmp_path)
    run_import(store, main, slug="euler-note", title="Euler Note")

    ref = store.get_ref(kind="draft", id="euler-note")
    assert ref is not None
    chunks = store.reading_order(ref.id)

    kinds = {c.chunk_kind for c in chunks}
    assert "equation" not in kinds, f"legacy equation kind leaked: {kinds}"

    bodies = [c.text or "" for c in chunks]
    math_blocks = [b for b in bodies if b.strip().startswith("$$")]
    # The equation env + the \[..\] display → two $$…$$ paragraphs.
    assert len(math_blocks) >= 2, bodies

    joined = "\n".join(math_blocks)
    assert "\\chi = V - E + F" in joined
    assert "\\label" not in joined  # KaTeX-hostile label stripped
    # The \[ a &= b \\ c &= d \] display carried alignment tokens → wrapped.
    assert "\\begin{aligned}" in joined

    # Inline $…$ math stays inside its prose paragraph, not split out into a
    # display block (demacro may re-space, so assert the dollar-math survives).
    prose = [b for b in bodies if "mass-energy relation" in b]
    assert prose, bodies
    assert not prose[0].strip().startswith("$$")  # not promoted to display
    assert "$" in prose[0] and "mc^2" in prose[0]
