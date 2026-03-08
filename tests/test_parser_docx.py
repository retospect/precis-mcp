"""Tests for DOCX parser — parsing, writing, track changes."""

from pathlib import Path

import pytest

from precis.parser.docx import DocxParser


class TestDocxParse:
    def test_node_count(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        # 2 headings + 3 paragraphs + 1 table = 6
        assert len(nodes) == 6

    def test_heading_detection(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        headings = [n for n in nodes if n.node_type == "h"]
        assert len(headings) == 2
        assert headings[0].text == "Introduction"
        assert headings[1].text == "Methods"

    def test_heading_levels(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        headings = [n for n in nodes if n.node_type == "h"]
        assert headings[0].heading_level() == 1
        assert headings[1].heading_level() == 2

    def test_paragraph_paths(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        paras = [n for n in nodes if n.node_type == "p"]
        assert str(paras[0].path) == "H1.0.0.0p1"
        assert str(paras[1].path) == "H1.0.0.0p2"
        assert str(paras[2].path) == "H1.1.0.0p1"

    def test_table_detection(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        tables = [n for n in nodes if n.node_type == "t"]
        assert len(tables) == 1
        assert "Param" in tables[0].precis

    def test_slugs_unique(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        slugs = [n.slug for n in nodes]
        assert len(slugs) == len(set(slugs))

    def test_empty_docx(self, empty_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(empty_docx)
        assert nodes == []

    def test_formatted_runs(self, formatted_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(formatted_docx)
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) == 1
        assert "**bold text**" in paras[0].text
        assert "*italic text*" in paras[0].text

    def test_source_files(self, tmp_docx: Path):
        parser = DocxParser()
        assert parser.source_files(tmp_docx) == [tmp_docx]


class TestDocxWrite:
    def test_replace(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.write_node(tmp_docx, para, "Replaced paragraph text.")

        new_nodes = parser.parse(tmp_docx)
        new_para = [n for n in new_nodes if n.node_type == "p"][0]
        assert "Replaced" in new_para.text
        # Slug should change
        assert new_para.slug != para.slug

    def test_insert_after(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.insert_after(tmp_docx, para, "Inserted after paragraph.")

        new_nodes = parser.parse(tmp_docx)
        assert len(new_nodes) == len(nodes) + 1

    def test_insert_before(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.insert_before(tmp_docx, para, "Inserted before paragraph.")

        new_nodes = parser.parse(tmp_docx)
        assert len(new_nodes) == len(nodes) + 1

    def test_delete(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.delete_node(tmp_docx, para)

        new_nodes = parser.parse(tmp_docx)
        assert len(new_nodes) == len(nodes) - 1

    def test_append(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)

        parser.append_node(tmp_docx, "Appended paragraph.")

        new_nodes = parser.parse(tmp_docx)
        assert len(new_nodes) == len(nodes) + 1
        assert "Appended" in new_nodes[-1].text

    def test_append_heading(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)

        parser.append_node(tmp_docx, "New Section", heading_level=1)

        new_nodes = parser.parse(tmp_docx)
        new_headings = [n for n in new_nodes if n.node_type == "h"]
        assert len(new_headings) == 3
        assert new_headings[-1].text == "New Section"

    def test_move(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        paras = [n for n in nodes if n.node_type == "p"]
        # Move first para after the third
        parser.move_nodes(tmp_docx, [paras[0]], paras[2])

        new_nodes = parser.parse(tmp_docx)
        new_paras = [n for n in new_nodes if n.node_type == "p"]
        # Same count, different order
        assert len(new_paras) == len(paras)
        # First para's slug should now be after the third
        assert new_paras[0].slug != paras[0].slug or str(new_paras[0].path) != str(
            paras[0].path
        )


class TestDocxTrackChanges:
    def test_tracked_replace(self, tmp_docx: Path):
        parser = DocxParser()
        nodes = parser.parse(tmp_docx)
        para = [n for n in nodes if n.node_type == "p"][0]

        parser.write_tracked(tmp_docx, para, "Tracked replacement text.")

        # File should still be valid DOCX
        new_nodes = parser.parse(tmp_docx)
        assert len(new_nodes) >= len(nodes) - 1  # may differ due to track changes
