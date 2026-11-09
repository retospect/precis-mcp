"""Offline side of the venue-metrics feeds (gr606422): Scimago CSV
parsing, OpenAlex source row shaping, ISSN normalisation. The DB-backed
half (upsert, overview, search filter) is in
``tests/test_paper_venue_metrics.py``."""

from __future__ import annotations

import pytest

from precis.ingest.venue_metrics import (
    normalise_issn,
    openalex_source_rows,
    openalex_venue_rows,
    parse_scimago_csv,
    scimago_year_from_name,
)

# A scimagojr export is semicolon-delimited with decimal-comma SJR.
_SC = """\
Rank;Sourceid;Title;Type;Issn;Publisher;Coverage;Categories;Area;SJR;SJR Best Quartile;H index;Total Docs.
1;1385;Chemical Engineering Journal;journal;1385-8947, 1873-2444;Elsevier;1996-2024;Chemical Engineering;Chemical Engineering;2,709;Q1;244;30000
2;1234;Regional Letters;journal;0001-0001;Some Pub;2000-2024;Engineering;Engineering;0,412;Q3;40;900
3;9999;Venue Sans Quartile;journal;9999-9999;X;2010-2024;All;All;-;-;10;5
"""

# The same columns comma-delimited with US decimals also parses.
_SC_COMMA = (
    "Rank,Sourceid,Title,Issn,SJR,SJR Best Quartile\n"
    "1,1385,Chemical Engineering Journal,1385-8947,2.709,Q1\n"
)


def test_normalise_issn() -> None:
    assert normalise_issn("1385-8947") == "13858947"
    assert normalise_issn("13858947") == "13858947"
    assert normalise_issn("0001-000X") == "0001000X"
    assert normalise_issn(" 2049-3630 ") == "20493630"
    assert normalise_issn("12-34") == ""
    assert normalise_issn("") == ""
    assert normalise_issn(None) == ""


def test_scimago_year_from_name() -> None:
    assert scimago_year_from_name("scimagojr 2024.csv") == 2024
    assert scimago_year_from_name("/tmp/scimagojr2023.csv") == 2023
    assert scimago_year_from_name("sjr.csv") is None


def test_parse_scimago_csv() -> None:
    rows = parse_scimago_csv(_SC, year=2024)
    by_issn = {r["issn"]: r for r in rows}
    # The multi-ISSN cell fans out to one row per ISSN.
    assert set(by_issn) == {"13858947", "18732444", "00010001", "99999999"}
    cej = by_issn["13858947"]
    assert cej["year"] == 2024
    assert cej["sjr"] == pytest.approx(2.709)
    assert cej["sjr_quartile"] == "Q1"
    assert cej["title"] == "Chemical Engineering Journal"
    # Sibling ISSN carries the same metrics.
    assert by_issn["18732444"]["sjr_quartile"] == "Q1"
    assert by_issn["00010001"]["sjr_quartile"] == "Q3"
    # A '-' quartile/SJR parses to None, not a bogus row or a crash.
    assert by_issn["99999999"]["sjr_quartile"] is None
    assert by_issn["99999999"]["sjr"] is None


def test_parse_scimago_csv_comma_dialect() -> None:
    rows = parse_scimago_csv(_SC_COMMA, year=2023)
    assert len(rows) == 1
    assert rows[0]["issn"] == "13858947"
    assert rows[0]["sjr"] == pytest.approx(2.709)


def test_openalex_source_rows() -> None:
    source = {
        "id": "https://openalex.org/S4210173458",
        "display_name": "Chemical Engineering Journal",
        "issn_l": "1385-8947",
        "issn": ["1385-8947", "1873-2444"],
        "summary_stats": {"2yr_mean_citedness": 11.4, "h_index": 244},
    }
    rows = openalex_source_rows(source, year=2026)
    assert {r["issn"] for r in rows} == {"13858947", "18732444"}
    for r in rows:
        assert r["year"] == 2026
        assert r["openalex_source_id"] == "S4210173458"
        assert r["openalex_2yr_mean_citedness"] == pytest.approx(11.4)
        assert r["openalex_h_index"] == 244
        assert r["title"] == "Chemical Engineering Journal"
        # OA rows carry no SJR fields — the upsert's COALESCE keeps them
        # off the shared (issn, year) row rather than writing NULLs.
        assert "sjr" not in r and "sjr_quartile" not in r


def test_openalex_source_rows_no_stats() -> None:
    rows = openalex_source_rows({"id": "https://openalex.org/S1"}, year=2026)
    assert rows == []  # no ISSN → nothing keyed


def test_openalex_venue_rows_injected_fetch() -> None:
    def fake_fetch(issn: str, *, email: str = "") -> list[dict]:
        if issn == "13858947":
            return [
                {
                    "id": "https://openalex.org/S1",
                    "display_name": "J",
                    "issn_l": "1385-8947",
                    "summary_stats": {"2yr_mean_citedness": 2.0, "h_index": 9},
                }
            ]
        return []

    rows = list(
        openalex_venue_rows(["13858947", "00010001"], year=2026, fetch=fake_fetch)
    )
    assert [r["issn"] for r in rows] == ["13858947"]
    assert rows[0]["openalex_h_index"] == 9


def test_openalex_venue_rows_skip_and_limit() -> None:
    def boom(issn: str, *, email: str = "") -> list[dict]:
        raise RuntimeError("upstream dead")

    # A failing ISSN logs-and-skips — it never sinks the sweep.
    good = lambda i, *, email="": [
        {
            "id": "https://openalex.org/S2",
            "display_name": "J2",
            "issn_l": i,
            "summary_stats": {},
        }
    ]
    calls: list[str] = []

    def mixed(issn: str, *, email: str = "") -> list[dict]:
        calls.append(issn)
        return boom(issn) if issn == "00010001" else good(issn)

    rows = list(
        openalex_venue_rows(
            ["00010001", "13858947", "20493630"],
            year=2026,
            fetch=mixed,
            limit=2,
        )
    )
    # limit=2 stops before the third ISSN; the failed first one yielded
    # nothing but didn't stop the second.
    assert calls == ["00010001", "13858947"]
    assert [r["issn"] for r in rows] == ["13858947"]
