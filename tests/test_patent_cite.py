"""Unit tests for patent-mode in-text citation formatting (slice 6).

See ``src/precis/export/_patent_cite.py`` and
``docs/design/patent-authoring-loop.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.export._patent_cite import format_patent_citation, paper_inline_citation


class TestFormatPatentCitation:
    def test_us_granted_patent(self) -> None:
        meta = {"country": "us", "doc_number": "2943737", "kind_code": "A"}
        assert format_patent_citation(meta) == "U.S. Patent No. 2,943,737"

    def test_us_granted_patent_b1(self) -> None:
        meta = {"country": "us", "doc_number": "6368648", "kind_code": "B1"}
        assert format_patent_citation(meta) == "U.S. Patent No. 6,368,648"

    def test_us_published_application(self) -> None:
        meta = {"country": "us", "doc_number": "20150101966", "kind_code": "A1"}
        assert (
            format_patent_citation(meta)
            == "U.S. Patent Application Publication No. 2015/0101966 A1"
        )

    def test_ep_granted(self) -> None:
        meta = {"country": "ep", "doc_number": "1234567", "kind_code": "B1"}
        assert format_patent_citation(meta) == "European Patent No. EP 1234567 B1"

    def test_ep_application(self) -> None:
        meta = {"country": "ep", "doc_number": "1234567", "kind_code": "A1"}
        assert (
            format_patent_citation(meta)
            == "European Patent Application Publication No. EP 1234567 A1"
        )

    def test_pct_wo_publication(self) -> None:
        meta = {"country": "wo", "doc_number": "2023123456", "kind_code": "A1"}
        assert (
            format_patent_citation(meta)
            == "PCT International Publication No. WO 2023/123456 A1"
        )

    def test_chinese_application(self) -> None:
        meta = {"country": "cn", "doc_number": "101787123", "kind_code": "A"}
        assert (
            format_patent_citation(meta)
            == "Chinese Patent Application Publication No. CN 101787123 A"
        )

    def test_unknown_authority_falls_back_to_docdb(self) -> None:
        meta = {"country": "zz", "doc_number": "1234567", "kind_code": "B1"}
        assert (
            format_patent_citation(meta, slug="zz1234567b1") == "Patent No. ZZ1234567B1"
        )

    def test_empty_meta_is_safe(self) -> None:
        assert format_patent_citation({}, slug="us5249511a") == "Patent No. US5249511A"


@dataclass
class _FakeRef:
    meta: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    slug: str | None = None


class TestPaperInlineCitation:
    def test_author_year(self) -> None:
        ref = _FakeRef(
            meta={
                "authors": [{"name": "Smith, Jane"}, {"name": "Doe, John"}],
                "publication_date": "2015-06-01",
            }
        )
        assert paper_inline_citation(ref) == "(Smith et al., 2015)"

    def test_given_surname_order(self) -> None:
        ref = _FakeRef(meta={"authors": ["Jane Smith"], "year": 2019})
        assert paper_inline_citation(ref) == "(Smith et al., 2019)"

    def test_falls_back_to_title(self) -> None:
        ref = _FakeRef(meta={}, title="A Study of Frying Oils")
        assert paper_inline_citation(ref) == "“A Study of Frying Oils”"

    def test_falls_back_to_slug(self) -> None:
        ref = _FakeRef(meta={}, slug="smith2015")
        assert paper_inline_citation(ref) == "smith2015"


class TestAssembleDocumentPatentMode:
    """A patent specification emits no bibliography (slice 6)."""

    def _assemble(self, doc_type: str) -> str:
        from precis.export.latex import assemble_document

        return assemble_document(
            title="A Frying-Oil System",
            author_block="\\author{precis}",
            body="Body text.",
            acronyms="",
            doc_type=doc_type,
        )

    def test_patent_mode_suppresses_bibliography(self) -> None:
        out = self._assemble("patent")
        assert "\\printbibliography" not in out
        assert "\\addbibresource" not in out

    def test_default_mode_keeps_bibliography(self) -> None:
        out = self._assemble("")
        assert "\\printbibliography" in out
        assert "\\addbibresource{refs.bib}" in out
