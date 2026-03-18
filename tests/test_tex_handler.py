"""Integration tests for TexHandler — parse, read, put, raw access."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.handlers.tex import TexHandler
from precis.protocol import PrecisError


@pytest.fixture
def handler():
    return TexHandler()


@pytest.fixture
def sample_tex(tmp_path):
    """Create a sample LaTeX project."""
    path = tmp_path / "main.tex"
    path.write_text(
        r"""\documentclass{article}
\begin{document}

\section{Introduction}
This is the introduction paragraph.
It spans multiple lines.

\section{Methods}
\subsection{Experimental Setup}
We prepared the samples using standard protocols.

\begin{table}[h]
\caption{Sample data}
\begin{tabular}{cc}
A & B \\
1 & 2 \\
\end{tabular}
\end{table}

\begin{figure}[h]
\caption{Overview of results}
\includegraphics{fig1.png}
\end{figure}

\begin{equation}
E = mc^2
\label{eq:einstein}
\end{equation}

\section{Results}
The results were significant.

\end{document}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def tex_with_bib(tmp_path):
    """Create LaTeX with bibliography."""
    tex_path = tmp_path / "main.tex"
    bib_path = tmp_path / "refs.bib"

    tex_path.write_text(
        r"""\documentclass{article}
\begin{document}
\section{Introduction}
Some text citing a reference.
\bibliography{refs}
\end{document}
""",
        encoding="utf-8",
    )

    bib_path.write_text(
        r"""@article{smith2024,
  title = {A Novel Approach},
  author = {Smith, John},
  year = {2024},
  journal = {Nature},
}

@article{jones2023,
  title = {Previous Work},
  author = {Jones, Alice},
  year = {2023},
  journal = {Science},
}
""",
        encoding="utf-8",
    )

    return tex_path


@pytest.fixture
def tex_with_input(tmp_path):
    """Create LaTeX with \\input files."""
    main = tmp_path / "main.tex"
    methods = tmp_path / "methods.tex"

    main.write_text(
        r"""\documentclass{article}
\begin{document}
\section{Introduction}
Intro text here.
\input{methods}
\section{Conclusion}
Final thoughts.
\end{document}
""",
        encoding="utf-8",
    )

    methods.write_text(
        r"""\section{Methods}
Detailed methods here.
\subsection{Materials}
We used many materials.
""",
        encoding="utf-8",
    )

    return main


@pytest.fixture
def empty_tex(tmp_path):
    path = tmp_path / "empty.tex"
    path.write_text(
        r"""\documentclass{article}
\begin{document}

\end{document}
""",
        encoding="utf-8",
    )
    return path


# ── Parse tests ─────────────────────────────────────────────────────


class TestParse:
    def test_parse_returns_nodes(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        assert len(nodes) > 0

    def test_parse_headings(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        assert len(headings) == 4  # 3 sections + 1 subsection
        assert headings[0].text == "Introduction"
        assert headings[1].text == "Methods"
        assert headings[2].text == "Experimental Setup"
        assert headings[3].text == "Results"

    def test_parse_subsections(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        sub = [h for h in headings if h.heading_level() == 2]
        assert len(sub) == 1
        assert sub[0].text == "Experimental Setup"

    def test_parse_paragraphs(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) >= 2

    def test_parse_table(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        tables = [n for n in nodes if n.node_type == "t"]
        assert len(tables) == 1
        assert "Sample data" in tables[0].precis

    def test_parse_figure(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        figs = [n for n in nodes if n.node_type == "f"]
        assert len(figs) == 1
        assert "Overview" in figs[0].precis

    def test_parse_equation(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        eqs = [n for n in nodes if n.node_type == "e"]
        assert len(eqs) == 1
        assert eqs[0].label == "eq:einstein"

    def test_parse_source_locations(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        for n in nodes:
            assert n.source_file == "main.tex"
            assert n.source_line_start > 0

    def test_parse_slugs_unique(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        slugs = [n.slug for n in nodes]
        assert len(slugs) == len(set(slugs))

    def test_parse_paths(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        assert str(headings[0].path) == "S1"
        assert str(headings[1].path) == "S2"

    def test_parse_empty(self, handler, empty_tex):
        nodes = handler.parse(empty_tex)
        assert len(nodes) == 0


# ── Multi-file tests ────────────────────────────────────────────────


class TestMultiFile:
    def test_resolve_input_files(self, handler, tex_with_input):
        files = handler.source_files(tex_with_input)
        names = [f.name for f in files]
        assert "main.tex" in names
        assert "methods.tex" in names

    def test_parse_input_files(self, handler, tex_with_input):
        nodes = handler.parse(tex_with_input)
        headings = [n for n in nodes if n.node_type == "h"]
        texts = [h.text for h in headings]
        assert "Introduction" in texts
        assert "Methods" in texts
        assert "Materials" in texts
        assert "Conclusion" in texts


# ── Bib tests ───────────────────────────────────────────────────────


class TestBib:
    def test_parse_bib_entries(self, handler, tex_with_bib):
        nodes = handler.parse(tex_with_bib)
        bib = [n for n in nodes if n.node_type == "b"]
        assert len(bib) == 2
        labels = [b.label for b in bib]
        assert "smith2024" in labels
        assert "jones2023" in labels

    def test_bib_precis(self, handler, tex_with_bib):
        nodes = handler.parse(tex_with_bib)
        bib = [n for n in nodes if n.node_type == "b"]
        smith = [b for b in bib if b.label == "smith2024"][0]
        assert "Novel Approach" in smith.precis

    def test_source_files_includes_bib(self, handler, tex_with_bib):
        files = handler.source_files(tex_with_bib)
        names = [f.name for f in files]
        assert "refs.bib" in names


# ── Read tests ──────────────────────────────────────────────────────


class TestRead:
    def test_read_toc(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), None, None, None, "", False, 0, 1
        )
        assert "main.tex" in result
        assert "Introduction" in result
        assert "Methods" in result

    def test_read_selector(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        intro = [n for n in nodes if n.text == "Introduction"][0]
        result = handler.read(
            str(sample_tex), intro.slug, None, None, "", False, 0, 1
        )
        assert "Introduction" in result

    def test_read_query(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), None, None, None, "significant", False, 0, 1
        )
        assert "significant" in result.lower() or "hit" in result.lower()

    def test_read_meta(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), None, "meta", None, "", False, 0, 1
        )
        assert "nodes:" in result

    def test_read_raw_file(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), "main.tex:1..5", None, None, "", False, 0, 1
        )
        assert "documentclass" in result
        assert "5" in result or "lines" in result


# ── Write tests ─────────────────────────────────────────────────────


class TestPut:
    def test_put_append(self, handler, sample_tex):
        result = handler.put(
            str(sample_tex), None, "A concluding remark.", "append"
        )
        assert "+" in result
        content = sample_tex.read_text(encoding="utf-8")
        assert "A concluding remark." in content

    def test_put_append_heading(self, handler, sample_tex):
        result = handler.put(
            str(sample_tex), None, "## | Discussion", "append"
        )
        assert "+" in result
        content = sample_tex.read_text(encoding="utf-8")
        assert r"\section{Discussion}" in content

    def test_put_replace(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) > 0
        para = paras[0]
        result = handler.put(
            str(sample_tex), para.slug, "Completely new text.", "replace",
            tracked=False,
        )
        assert "replace" in result.lower()
        content = sample_tex.read_text(encoding="utf-8")
        assert "Completely new text." in content

    def test_put_delete(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) > 0
        para = paras[-1]
        count_before = len(nodes)
        handler.put(str(sample_tex), para.slug, "", "delete")
        new_nodes = handler.parse(sample_tex)
        assert len(new_nodes) < count_before

    def test_put_insert_after(self, handler, sample_tex):
        nodes = handler.parse(sample_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        result = handler.put(
            str(sample_tex), headings[0].slug, "New paragraph after heading.", "after"
        )
        assert "+" in result

    def test_put_invalid_mode(self, handler, sample_tex):
        with pytest.raises(PrecisError, match="invalid mode"):
            handler.put(str(sample_tex), None, "text", "badmode")


# ── Raw file access ─────────────────────────────────────────────────


class TestRawAccess:
    def test_raw_read(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), "main.tex:1..3", None, None, "", False, 0, 1
        )
        assert "documentclass" in result

    def test_raw_write(self, handler, sample_tex):
        result = handler.put(
            str(sample_tex), "main.tex:$", "% appended comment\n", "replace"
        )
        assert "appended" in result.lower()
        content = sample_tex.read_text(encoding="utf-8")
        assert "% appended comment" in content

    def test_raw_read_whole_file(self, handler, sample_tex):
        result = handler.read(
            str(sample_tex), "main.tex", None, None, "", False, 0, 1
        )
        assert "lines" in result

    def test_raw_path_escape(self, handler, sample_tex):
        # ../../../etc/passwd doesn't match raw file regex, so falls through
        # to slug resolution which rejects it as invalid
        with pytest.raises(PrecisError, match="not a valid SLUG"):
            handler.read(
                str(sample_tex), "../../../etc/passwd", None, None, "", False, 0, 1
            )


# ── Bib write tests ────────────────────────────────────────────────


class TestLists:
    def test_parse_itemize(self, handler, tmp_path):
        """itemize environment should be parsed to markdown bullet list."""
        path = tmp_path / "list.tex"
        path.write_text(
            r"""\documentclass{article}
\begin{document}
\section{Shopping}
\begin{itemize}
  \item Apples
  \item Bananas
  \item Cherries
\end{itemize}
\end{document}
""",
            encoding="utf-8",
        )
        nodes = handler.parse(path)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) == 1
        assert "- Apples" in paras[0].text
        assert "- Bananas" in paras[0].text
        assert "- Cherries" in paras[0].text

    def test_parse_enumerate(self, handler, tmp_path):
        """enumerate environment should be parsed to markdown numbered list."""
        path = tmp_path / "enum.tex"
        path.write_text(
            r"""\documentclass{article}
\begin{document}
\section{Steps}
\begin{enumerate}
  \item First step
  \item Second step
\end{enumerate}
\end{document}
""",
            encoding="utf-8",
        )
        nodes = handler.parse(path)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) == 1
        assert "1. First step" in paras[0].text
        assert "1. Second step" in paras[0].text

    def test_append_bullet_list(self, handler, empty_tex):
        """Appending markdown bullet list should emit \\begin{itemize}."""
        handler.put(str(empty_tex), None, "- Alpha\n- Beta", "append")
        content = empty_tex.read_text(encoding="utf-8")
        assert r"\begin{itemize}" in content
        assert r"\item Alpha" in content
        assert r"\item Beta" in content
        assert r"\end{itemize}" in content

    def test_append_numbered_list(self, handler, empty_tex):
        """Appending markdown numbered list should emit \\begin{enumerate}."""
        handler.put(str(empty_tex), None, "1. First\n2. Second", "append")
        content = empty_tex.read_text(encoding="utf-8")
        assert r"\begin{enumerate}" in content
        assert r"\item First" in content
        assert r"\item Second" in content
        assert r"\end{enumerate}" in content

    def test_list_roundtrip(self, handler, tmp_path):
        """Write bullet list → parse → verify markdown prefix preserved."""
        path = tmp_path / "rt.tex"
        path.write_text(
            r"""\documentclass{article}
\begin{document}
\section{List}
\end{document}
""",
            encoding="utf-8",
        )
        handler.put(str(path), None, "- Item A\n- Item B", "append")
        nodes = handler.parse(path)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) == 1
        assert "- Item A" in paras[0].text
        assert "- Item B" in paras[0].text


class TestBibWrite:
    def test_append_bib_entry(self, handler, tex_with_bib):
        result = handler.put(
            str(tex_with_bib), None,
            "[@doe2025]: Doe, J. A great paper. 2025.",
            "append",
        )
        bib_path = tex_with_bib.parent / "refs.bib"
        content = bib_path.read_text(encoding="utf-8")
        assert "doe2025" in content
