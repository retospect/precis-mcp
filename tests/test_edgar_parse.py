"""Tests for the submissions + filing-HTML parser of the ``edgar`` kind."""

from __future__ import annotations

import json

from precis.handlers._edgar_parse import (
    assemble_filing,
    extract_text_lines,
    find_filing,
    parse_filing_html,
    parse_submissions,
)

_SUBMISSIONS = json.dumps(
    {
        "cik": "320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-23-000106",
                    "0000320193-23-000077",
                    "0000320193-24-000001",
                ],
                "form": ["10-K", "10-Q", "8-K"],
                "filingDate": ["2023-11-03", "2023-08-04", "2024-01-02"],
                "reportDate": ["2023-09-30", "2023-07-01", ""],
                "primaryDocument": [
                    "aapl-20230930.htm",
                    "aapl-20230701.htm",
                    "ea0.htm",
                ],
                "items": ["", "", "5.02,7.01"],
            }
        },
    }
).encode()


class TestParseSubmissions:
    def test_company_and_tickers(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        assert subs.company == "Apple Inc."
        assert subs.tickers == ["AAPL"]
        assert subs.cik == "320193"

    def test_filing_rows(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        assert len(subs.filings) == 3
        tenk = subs.filings[0]
        assert tenk.form == "10-K"
        assert tenk.filed_date == "2023-11-03"
        assert tenk.report_date == "2023-09-30"
        assert tenk.primary_doc == "aapl-20230930.htm"

    def test_8k_items_split(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        eightk = subs.filings[2]
        assert eightk.items == ["5.02", "7.01"]
        assert eightk.report_date is None

    def test_find_filing(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        f = find_filing(subs, "0000320193-23-000077")
        assert f is not None
        assert f.form == "10-Q"
        assert find_filing(subs, "9999999999-99-999999") is None

    def test_malformed_json(self) -> None:
        subs = parse_submissions(b"not json")
        assert subs.filings == []
        assert subs.company == ""


_FILING_HTML = b"""
<html><head><style>.x{color:red}</style></head><body>
<p>Apple Inc. Annual Report</p>
<div>Table of Contents</div>
<p>Item 1. Business</p>
<p>The Company designs, manufactures and markets smartphones.</p>
<p>Item 1A. Risk Factors</p>
<p>The Company's business is subject to macroeconomic risk.</p>
<p>Supply chain disruptions could harm operations.</p>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Net sales decreased 3% year over year.</p>
<script>var a = 1;</script>
</body></html>
"""


class TestExtractTextLines:
    def test_drops_script_and_style(self) -> None:
        lines = extract_text_lines(_FILING_HTML)
        joined = " ".join(lines)
        assert "color:red" not in joined
        assert "var a" not in joined

    def test_plain_text_fallback(self) -> None:
        lines = extract_text_lines(b"line one\nline two\n")
        assert lines == ["line one", "line two"]


class TestParseFilingHtml:
    def test_sections_assigned(self) -> None:
        blocks = parse_filing_html(_FILING_HTML, form="10-K")
        by_text = {b.text: b.section.canonical_id for b in blocks}
        assert (
            by_text["The Company designs, manufactures and markets smartphones."]
            == "item-1"
        )
        assert (
            by_text["The Company's business is subject to macroeconomic risk."]
            == "item-1a"
        )
        assert by_text["Supply chain disruptions could harm operations."] == "item-1a"
        assert by_text["Net sales decreased 3% year over year."] == "item-7"

    def test_boilerplate_dropped(self) -> None:
        blocks = parse_filing_html(_FILING_HTML, form="10-K")
        texts = [b.text for b in blocks]
        assert "Table of Contents" not in texts

    def test_leading_text_is_body(self) -> None:
        blocks = parse_filing_html(_FILING_HTML, form="10-K")
        # First real block precedes any Item heading → body section.
        assert blocks[0].text == "Apple Inc. Annual Report"
        assert blocks[0].section.canonical_id == "body"


class TestAssembleFiling:
    def test_full_assembly(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        filing = find_filing(subs, "0000320193-23-000106")
        assert filing is not None
        parsed = assemble_filing(
            filing=filing,
            company=subs.company,
            cik=subs.cik,
            tickers=subs.tickers,
            primary_html=_FILING_HTML,
        )
        assert parsed.title == "Apple Inc. — 10-K (2023-09-30)"
        assert parsed.form == "10-K"
        assert parsed.period_of_report == "2023-09-30"
        # Discovered item codes surface in items.
        assert "1a" in parsed.items
        assert "7" in parsed.items
        assert len(parsed.blocks) > 0

    def test_8k_items_union(self) -> None:
        subs = parse_submissions(_SUBMISSIONS)
        filing = find_filing(subs, "0000320193-24-000001")
        assert filing is not None
        parsed = assemble_filing(
            filing=filing,
            company=subs.company,
            cik=subs.cik,
            tickers=subs.tickers,
            primary_html=b"<p>Item 5.02 Departure of Directors</p><p>CEO resigned.</p>",
        )
        # submissions-declared items merged with discovered.
        assert "5.02" in parsed.items
        assert "7.01" in parsed.items
