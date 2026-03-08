"""Tests for LaTeX parser — parsing, writing, multi-file."""

from pathlib import Path

import pytest

from precis.parser.latex import LatexParser


class TestLatexParse:
    def test_node_count(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        # intro heading + intro para + methods heading + methods para + equation + training para = 6
        assert len(nodes) == 6

    def test_heading_detection(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        assert len(headings) == 2
        assert headings[0].text == "Introduction"
        assert headings[1].text == "Methods"

    def test_paragraph_nodes(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) == 3
        assert "introduction paragraph" in paras[0].text

    def test_equation_node(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        eqs = [n for n in nodes if n.node_type == "e"]
        assert len(eqs) == 1
        assert "\\sum" in eqs[0].text

    def test_label_captured(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        labeled = [n for n in nodes if n.label]
        assert len(labeled) >= 2  # sec:methods and eq:loss
        labels = {n.label for n in labeled}
        assert "sec:methods" in labels
        assert "eq:loss" in labels

    def test_source_file_tracking(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        # Methods heading should be in methods.tex
        methods_h = [n for n in nodes if n.text == "Methods"]
        assert len(methods_h) == 1
        assert methods_h[0].source_file == "methods.tex"

    def test_source_line_numbers(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        for n in nodes:
            assert n.source_line_start > 0
            assert n.source_line_end >= n.source_line_start

    def test_slugs_unique(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        slugs = [n.slug for n in nodes]
        assert len(slugs) == len(set(slugs))

    def test_paths_assigned(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        headings = [n for n in nodes if n.node_type == "h"]
        assert str(headings[0].path) == "H1.0.0.0"
        assert str(headings[1].path) == "H2.0.0.0"

    def test_empty_tex(self, empty_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(empty_tex)
        assert nodes == []

    def test_source_files(self, tmp_tex: Path):
        parser = LatexParser()
        files = parser.source_files(tmp_tex)
        names = {f.name for f in files}
        assert "main.tex" in names
        assert "methods.tex" in names


class TestLatexWrite:
    def test_replace(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.write_node(tmp_tex, para, "Replaced LaTeX paragraph.")

        new_nodes = parser.parse(tmp_tex)
        new_paras = [n for n in new_nodes if n.node_type == "p"]
        assert any("Replaced" in p.text for p in new_paras)

    def test_insert_after(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.insert_after(tmp_tex, para, "Inserted after.")

        new_nodes = parser.parse(tmp_tex)
        assert len(new_nodes) > len(nodes)

    def test_delete(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.delete_node(tmp_tex, para)

        new_nodes = parser.parse(tmp_tex)
        assert len(new_nodes) == len(nodes) - 1

    def test_append(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)

        parser.append_node(tmp_tex, "Appended text.")

        new_nodes = parser.parse(tmp_tex)
        assert len(new_nodes) > len(nodes)

    def test_append_heading(self, tmp_tex: Path):
        parser = LatexParser()
        nodes = parser.parse(tmp_tex)

        parser.append_node(tmp_tex, "Conclusion", heading_level=1)

        new_nodes = parser.parse(tmp_tex)
        headings = [n for n in new_nodes if n.node_type == "h"]
        assert any(h.text == "Conclusion" for h in headings)


# ---------------------------------------------------------------------------
# .bib file fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tex_with_bib(tmp_path: Path) -> Path:
    """LaTeX project with \\bibliography{refs} and a .bib file."""
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{smith2020,\n"
        "  author = {Smith, John},\n"
        "  title = {Chicken in Shoes},\n"
        "  journal = {J. Poultry},\n"
        "  year = {2020},\n"
        "  volume = {42},\n"
        "  pages = {1--15}\n"
        "}\n\n"
        "@inproceedings{jones2019,\n"
        "  author = {Jones, Alice},\n"
        "  title = {Quantum Poultry Dynamics},\n"
        "  booktitle = {Proc. Poultry Conf.},\n"
        "  year = {2019}\n"
        "}\n",
        encoding="utf-8",
    )

    root = tmp_path / "main.tex"
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n\n"
        "\\section{Introduction}\n"
        "Poultry is important \\cite{smith2020}.\n\n"
        "\\section{Methods}\n"
        "We follow \\cite{jones2019}.\n\n"
        "\\bibliography{refs}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def tex_with_biblatex(tmp_path: Path) -> Path:
    """LaTeX project with \\addbibresource{refs.bib}."""
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@book{lee2021,\n"
        "  author = {Lee, K.},\n"
        "  title = {Deep Poultry Networks},\n"
        "  publisher = {Springer},\n"
        "  year = {2021}\n"
        "}\n",
        encoding="utf-8",
    )

    root = tmp_path / "main.tex"
    root.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{biblatex}\n"
        "\\addbibresource{refs.bib}\n"
        "\\begin{document}\n\n"
        "\\section{Intro}\n"
        "See \\cite{lee2021}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def tex_no_bib(tmp_path: Path) -> Path:
    """LaTeX project with no .bib reference."""
    root = tmp_path / "main.tex"
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n\n"
        "\\section{Intro}\n"
        "No bibliography here.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# LaTeX bib tests
# ---------------------------------------------------------------------------


class TestLatexBibParse:
    def test_bib_entries_detected(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert len(bibs) == 2

    def test_bib_labels(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        labels = {b.label for b in bibs}
        assert labels == {"smith2020", "jones2019"}

    def test_bib_precis_has_title(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        smith = [b for b in bibs if b.label == "smith2020"][0]
        assert "smith2020:" in smith.precis
        assert "Chicken" in smith.precis

    def test_bib_style_is_entry_type(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        styles = {b.label: b.style for b in bibs}
        assert styles["smith2020"] == "@article"
        assert styles["jones2019"] == "@inproceedings"

    def test_bib_source_file(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert all(b.source_file == "refs.bib" for b in bibs)

    def test_bib_source_lines(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        for b in bibs:
            assert b.source_line_start > 0
            assert b.source_line_end >= b.source_line_start

    def test_bib_path_type(self, tex_with_bib: Path):
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert all("b" in str(b.path) for b in bibs)

    def test_cite_in_paragraph_text(self, tex_with_bib: Path):
        """\\cite{key} should appear in paragraph text (already content)."""
        parser = LatexParser()
        nodes = parser.parse(tex_with_bib)
        paras = [n for n in nodes if n.node_type == "p"]
        assert any("\\cite{smith2020}" in p.text for p in paras)
        assert any("\\cite{jones2019}" in p.text for p in paras)

    def test_biblatex_addbibresource(self, tex_with_biblatex: Path):
        """\\addbibresource{} should also find the .bib file."""
        parser = LatexParser()
        nodes = parser.parse(tex_with_biblatex)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert len(bibs) == 1
        assert bibs[0].label == "lee2021"

    def test_no_bib_no_crash(self, tex_no_bib: Path):
        """Project with no .bib reference should parse fine with zero 'b' nodes."""
        parser = LatexParser()
        nodes = parser.parse(tex_no_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert len(bibs) == 0

    def test_source_files_includes_bib(self, tex_with_bib: Path):
        parser = LatexParser()
        files = parser.source_files(tex_with_bib)
        names = {f.name for f in files}
        assert "refs.bib" in names
        assert "main.tex" in names


class TestLatexBibWrite:
    def test_append_bib_definition(self, tex_with_bib: Path):
        """[@key]: text appends to .bib file."""
        parser = LatexParser()
        parser.append_node(
            tex_with_bib,
            "[@miller2023]: Miller, B. (2023). Quantum Poultry Dynamics II.",
        )

        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert len(bibs) == 3
        labels = {b.label for b in bibs}
        assert "miller2023" in labels

    def test_append_bib_creates_misc_entry(self, tex_with_bib: Path):
        """New bib entry should be @misc with note field."""
        parser = LatexParser()
        parser.append_node(tex_with_bib, "[@new2024]: New Author (2024). Some Title.")

        bib_path = tex_with_bib.parent / "refs.bib"
        content = bib_path.read_text(encoding="utf-8")
        assert "@misc{new2024," in content
        assert "note = {New Author (2024). Some Title.}" in content

    def test_append_bib_dedup(self, tex_with_bib: Path):
        """Appending an existing key should NOT duplicate it."""
        parser = LatexParser()
        orig_nodes = parser.parse(tex_with_bib)
        orig_bibs = [n for n in orig_nodes if n.node_type == "b"]

        # Try to add smith2020 again — should be a no-op
        parser.append_node(
            tex_with_bib, "[@smith2020]: Smith, J. (2020). Different text."
        )

        new_nodes = parser.parse(tex_with_bib)
        new_bibs = [n for n in new_nodes if n.node_type == "b"]
        assert len(new_bibs) == len(orig_bibs)

    def test_append_bib_no_bib_file_errors(self, tex_no_bib: Path):
        """Appending [@key]: with no .bib reference should raise ValueError."""
        parser = LatexParser()
        with pytest.raises(ValueError, match="No .bib file found"):
            parser.append_node(tex_no_bib, "[@x2024]: Some reference.")

    def test_append_bib_creates_file(self, tmp_path: Path):
        """If .bib file doesn't exist yet, it should be created."""
        bib_path = tmp_path / "refs.bib"
        assert not bib_path.exists()

        root = tmp_path / "main.tex"
        root.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n\n"
            "\\section{Intro}\n"
            "Text.\n\n"
            "\\bibliography{refs}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )

        parser = LatexParser()
        parser.append_node(root, "[@alpha2020]: Alpha, A. (2020). First.")

        assert bib_path.exists()
        nodes = parser.parse(root)
        bibs = [n for n in nodes if n.node_type == "b"]
        assert len(bibs) == 1
        assert bibs[0].label == "alpha2020"

    def test_full_round_trip(self, tex_with_bib: Path):
        """Write bib + cite paragraph, read back, verify both."""
        parser = LatexParser()

        # Add a new bib entry
        parser.append_node(
            tex_with_bib,
            "[@wang2022]: Wang, X. (2022). Poultry Transformers.",
        )

        # Add a paragraph that cites it
        parser.append_node(
            tex_with_bib, "As shown by \\cite{wang2022}, the effect is clear."
        )

        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        paras = [n for n in nodes if n.node_type == "p"]

        assert any(b.label == "wang2022" for b in bibs)
        assert any("\\cite{wang2022}" in p.text for p in paras)

    def test_multiple_appends(self, tex_with_bib: Path):
        """Multiple bib appends get unique entries."""
        parser = LatexParser()
        parser.append_node(tex_with_bib, "[@a2020]: Alpha. (2020). Title A.")
        parser.append_node(tex_with_bib, "[@b2021]: Beta. (2021). Title B.")
        parser.append_node(tex_with_bib, "[@c2022]: Gamma. (2022). Title C.")

        nodes = parser.parse(tex_with_bib)
        bibs = [n for n in nodes if n.node_type == "b"]
        labels = {b.label for b in bibs}
        assert {"smith2020", "jones2019", "a2020", "b2021", "c2022"} == labels
