"""Unit tests for the canonical author helper (precis.utils.authors).

Pure — no DB. Locks the shape-tolerance contract that the web display,
citation generation, provenance report and bib generation all now share.
"""

from __future__ import annotations

from typing import cast

import pytest

from precis.utils.authors import (
    author_display,
    author_line,
    author_links,
    author_names,
    author_row_from_entry,
    build_byline,
    entry_from_author_row,
    is_junk_author_name,
    normalize_authors,
    normalize_orcid,
    paper_scholar_link,
    split_middle,
    to_author_dicts,
    to_name_dicts,
)


class TestAuthorDisplay:
    def test_family_given_natural_order(self) -> None:
        a = {"family": "Smith", "given": "Jane"}
        assert author_display(a) == "Jane Smith"

    def test_family_given_sortable_order(self) -> None:
        a = {"family": "Smith", "given": "Jane"}
        assert author_display(a, order="sortable") == "Smith, Jane"

    def test_name_shape_returned_as_is(self) -> None:
        # Semantic Scholar / Crossref ingest shape — can't be reordered.
        assert author_display({"name": "Jane Smith"}) == "Jane Smith"
        assert (
            author_display({"name": "Smith, Jane"}, order="sortable") == "Smith, Jane"
        )

    def test_family_only_and_given_only(self) -> None:
        assert author_display({"family": "Aristotle"}) == "Aristotle"
        assert author_display({"given": "Cher"}) == "Cher"

    def test_bare_string_and_empty(self) -> None:
        assert author_display("Plato") == "Plato"
        assert author_display({}) == ""
        assert author_display(None) == ""


class TestScrubName:
    """Character-level hygiene at both funnels — write (normalize_authors)
    and read (author_display) — so known-garbage legacy rows (refs 258,
    3023, ryder14's thin space) render clean without a prod sweep."""

    def test_exotic_spaces_collapse_to_plain(self) -> None:
        # ryder14: a thin space (U+2009) in "Matthew R." became `\,`
        # in the .bib, which biber's name parser read as a suffix →
        # runaway .bbl. NBSP gets the same treatment.
        assert author_display({"name": "Ryder, Matthew R."}) == "Ryder, Matthew R."
        assert author_display({"family": "Doe", "given": "A. B."}) == "A. B. Doe"

    def test_zero_widths_deleted(self) -> None:
        assert author_display({"name": "Jane​Smith﻿"}) == "JaneSmith"

    def test_trailing_backslash_stripped_at_write(self) -> None:
        # refs 258: PDF-extraction debris "DIFFUSION MODELS\" — the
        # trailing backslash goes; the junk guard then judges the rest.
        out = normalize_authors(["Weber, Max\\"])
        assert out == [{"given": "Max", "family": "Weber"}]

    def test_write_path_scrubs_every_leg(self) -> None:
        out = normalize_authors(
            [
                {"family": "Ryder", "given": "Matthew R."},
                {"name": "Nellia​ Dzhubaeva"},
                "Plato\\",
            ]
        )
        assert out == [
            {"given": "Matthew R.", "family": "Ryder"},
            {"name": "Nellia Dzhubaeva"},
            {"name": "Plato"},
        ]

    def test_packed_string_scrubbed(self) -> None:
        assert author_names("Smith, J.; Doe, A.\\") == ["Smith, J.", "Doe, A."]


class TestAuthorNames:
    def test_mixed_shapes_in_one_list(self) -> None:
        raw = [
            {"name": "Jane Smith"},
            {"family": "Doe", "given": "Alice"},
            "Plato",
            {},  # dropped
        ]
        assert author_names(raw) == ["Jane Smith", "Alice Doe", "Plato"]

    def test_semicolon_packed_string(self) -> None:
        assert author_names("Smith, J.; Doe, A.") == ["Smith, J.", "Doe, A."]

    def test_none_and_garbage(self) -> None:
        assert author_names(None) == []
        assert author_names(123) == []


class TestToNameDicts:
    def test_canonicalises_every_shape_to_name(self) -> None:
        raw = [{"family": "Doe", "given": "Alice"}, "Smith, Jane", {"name": "X"}]
        assert to_name_dicts(raw) == [
            {"name": "Doe, Alice"},
            {"name": "Smith, Jane"},
            {"name": "X"},
        ]

    def test_empty(self) -> None:
        assert to_name_dicts(None) == []
        assert to_name_dicts([]) == []


class TestToAuthorDicts:
    def test_preserves_affiliation_and_ror(self) -> None:
        raw = [
            {"family": "Doe", "given": "Alice", "affiliation": "MIT", "ror": "r1"},
            {"name": "Smith, Jane"},  # no affiliation
        ]
        assert to_author_dicts(raw) == [
            {"name": "Doe, Alice", "affiliation": "MIT", "ror": "r1"},
            {"name": "Smith, Jane"},
        ]

    def test_drops_blank_affiliation_keys(self) -> None:
        raw = [{"name": "X", "affiliation": "  ", "ror": ""}]
        assert to_author_dicts(raw) == [{"name": "X"}]

    def test_string_and_empty(self) -> None:
        assert to_author_dicts("Smith, J.; Doe, A.") == [
            {"name": "Smith, J."},
            {"name": "Doe, A."},
        ]
        assert to_author_dicts(None) == []


class TestBuildByline:
    def test_distinct_affiliations_get_marks(self) -> None:
        raw = [
            {"name": "Jane Doe", "affiliation": "MIT", "ror": "r1"},
            {"family": "Roe", "given": "John", "affiliation": "Caltech"},
        ]
        b = build_byline(raw)
        assert b["multi"] is True
        assert [a["sup"] for a in b["authors"]] == ["1", "2"]
        assert [a["name"] for a in b["authors"]] == ["Jane Doe", "John Roe"]
        assert b["affiliations"] == [
            {"index": 1, "org": "MIT", "ror": "r1"},
            {"index": 2, "org": "Caltech", "ror": ""},
        ]

    def test_shared_affiliation_deduped_and_unnumbered(self) -> None:
        # Same ROR → one affiliation, no superscripts (reads better).
        raw = [
            {"name": "A B", "affiliation": "MIT", "ror": "r1"},
            {"name": "C D", "affiliation": "Massachusetts Inst. Tech.", "ror": "r1"},
        ]
        b = build_byline(raw)
        assert b["multi"] is False
        assert len(b["affiliations"]) == 1
        assert [a["sup"] for a in b["authors"]] == ["", ""]

    def test_dedup_falls_back_to_org_when_no_ror(self) -> None:
        raw = [
            {"name": "A B", "affiliation": "MIT"},
            {"name": "C D", "affiliation": "mit"},  # case-insensitive match
        ]
        b = build_byline(raw)
        assert len(b["affiliations"]) == 1
        assert b["multi"] is False

    def test_no_affiliations_is_plain_name_list(self) -> None:
        b = build_byline(["X Y", "Z W"])
        assert b["multi"] is False
        assert b["affiliations"] == []
        assert [a["name"] for a in b["authors"]] == ["X Y", "Z W"]
        assert all(a["sup"] == "" for a in b["authors"])

    def test_empty(self) -> None:
        assert build_byline(None)["authors"] == []
        assert build_byline([])["affiliations"] == []


class TestIsJunkAuthorName:
    def test_email_is_junk(self) -> None:
        assert is_junk_author_name("j.smith@example.com") is True

    def test_section_heading_is_junk(self) -> None:
        assert is_junk_author_name("REFERENCES") is True
        assert is_junk_author_name("Abstract") is True
        assert is_junk_author_name("introduction.") is True

    def test_lone_all_caps_token_is_junk(self) -> None:
        assert is_junk_author_name("OECD") is True

    def test_over_long_string_is_junk(self) -> None:
        assert is_junk_author_name("This is way too many words to be a name") is True

    def test_empty_is_junk(self) -> None:
        assert is_junk_author_name("") is True
        assert is_junk_author_name("   ") is True

    def test_genuine_short_names_pass(self) -> None:
        assert is_junk_author_name("Aristotle") is False
        assert is_junk_author_name("Dellago, Christoph") is False
        assert is_junk_author_name("Bryan R. Goldsmith") is False


class TestNormalizeAuthors:
    def test_structured_passes_through(self) -> None:
        raw = [{"family": "Smith", "given": "Jane"}]
        assert normalize_authors(raw) == [{"given": "Jane", "family": "Smith"}]

    def test_family_only_and_given_only(self) -> None:
        assert normalize_authors([{"family": "Aristotle"}]) == [{"family": "Aristotle"}]
        assert normalize_authors([{"given": "Cher"}]) == [{"given": "Cher"}]

    def test_single_comma_string_splits(self) -> None:
        assert normalize_authors(["Smith, Jane"]) == [
            {"given": "Jane", "family": "Smith"}
        ]
        assert normalize_authors([{"name": "Dellago, Christoph"}]) == [
            {"given": "Christoph", "family": "Dellago"}
        ]

    def test_ambiguous_natural_string_stays_name(self) -> None:
        # No comma — a middle name / multi-word surname can't be told
        # apart without a real parser, so no heuristic reordering.
        assert normalize_authors(["Christoph Dellago"]) == [
            {"name": "Christoph Dellago"}
        ]
        assert normalize_authors(["Aristotle"]) == [{"name": "Aristotle"}]

    def test_junk_entries_dropped(self) -> None:
        raw = ["REFERENCES", "j.smith@example.com", "Smith, Jane"]
        assert normalize_authors(raw) == [{"given": "Jane", "family": "Smith"}]

    def test_junk_guard_applies_to_structured_display_name(self) -> None:
        # A junk family/given pair (mis-parsed section heading) is
        # rejected on its rendered display name, same as a flat string.
        raw = [{"given": "", "family": "REFERENCES"}]
        assert normalize_authors(raw) == []

    def test_optional_keys_carried_through(self) -> None:
        raw = [
            {
                "family": "Smith",
                "given": "Jane",
                "orcid": "0000-0000-0000-0001",
                "affiliation": "MIT",
                "ror": "r1",
            }
        ]
        assert normalize_authors(raw) == [
            {
                "given": "Jane",
                "family": "Smith",
                "orcid": "0000-0000-0000-0001",
                "affiliation": "MIT",
                "ror": "r1",
            }
        ]

    def test_jammed_initials_get_spaced(self) -> None:
        # The dominant Semantic Scholar byline style — dotted initials
        # jammed against the next capital — is repaired on every write
        # path: flat strings, {"name"} dicts, and structured ``given``.
        assert normalize_authors(["A.K. Geim"]) == [{"name": "A. K. Geim"}]
        assert normalize_authors([{"name": "J.R.R. Tolkien"}]) == [
            {"name": "J. R. R. Tolkien"}
        ]
        assert normalize_authors([{"given": "A.K.", "family": "Geim"}]) == [
            {"given": "A. K.", "family": "Geim"}
        ]

    def test_initials_tidy_leaves_edge_cases_alone(self) -> None:
        # Hyphenated initials, multi-letter abbreviations, and already
        # correct spacing are untouched; doubled whitespace collapses.
        assert normalize_authors(["A.-K. Geim"]) == [{"name": "A.-K. Geim"}]
        assert normalize_authors(["K. S.  Novoselov"]) == [{"name": "K. S. Novoselov"}]
        assert normalize_authors([{"name": "St. John Smith"}]) == [
            {"name": "St. John Smith"}
        ]

    def test_semicolon_packed_string(self) -> None:
        assert normalize_authors("Smith, Jane; Dellago, Christoph") == [
            {"given": "Jane", "family": "Smith"},
            {"given": "Christoph", "family": "Dellago"},
        ]

    def test_empty_and_garbage(self) -> None:
        assert normalize_authors(None) == []
        assert normalize_authors([]) == []
        assert normalize_authors(123) == []

    def test_crossref_and_s2_shapes_display_identically(self) -> None:
        """Acceptance: the same person via Crossref's structured shape and
        S2's natural unstructured string renders the same natural-order
        display string, even though the stored shapes differ (Crossref
        carries the split, S2's ambiguous flat string doesn't)."""
        crossref_style = normalize_authors([{"family": "Smith", "given": "John"}])
        s2_style = normalize_authors([{"name": "John Smith"}])
        assert author_names(crossref_style) == author_names(s2_style) == ["John Smith"]


class TestSplitMiddle:
    """``paper_authors.middle`` derivation — the rule decided 2026-09-18."""

    def test_trailing_initials_peel_off(self) -> None:
        assert split_middle("Bryan R.") == ("Bryan", "R.")
        assert split_middle("John T. J.") == ("John", "T. J.")
        assert split_middle("Mary A") == ("Mary", "A")

    def test_leading_initial_is_a_first_name(self) -> None:
        assert split_middle("J. Robert") == ("J. Robert", "")
        # the first token is never consumed, so "K. S." keeps K. as given
        assert split_middle("K. S.") == ("K.", "S.")

    def test_multi_word_given_and_hyphenated_initials_stay(self) -> None:
        assert split_middle("Mary Anne") == ("Mary Anne", "")
        assert split_middle("A.-K.") == ("A.-K.", "")
        assert split_middle("Ronggang") == ("Ronggang", "")

    def test_empty_never_none(self) -> None:
        assert split_middle("") == ("", "")
        assert split_middle(cast(str, None)) == ("", "")


class TestNormalizeOrcid:
    def test_strips_url_and_validates(self) -> None:
        assert (
            normalize_orcid("https://orcid.org/0000-0002-1825-0097")
            == "0000-0002-1825-0097"
        )
        assert normalize_orcid("0000-0002-1825-009x") == "0000-0002-1825-009X"

    def test_rejects_garbage(self) -> None:
        assert normalize_orcid("") is None
        assert normalize_orcid(None) is None
        assert normalize_orcid("junk") is None
        assert normalize_orcid("0000-0002-1825") is None


class TestAuthorRowMapping:
    """jsonb element ↔ ``paper_authors`` row, both directions."""

    def test_canonical_entry_splits_middle_and_carries_orcid(self) -> None:
        e = {
            "given": "Bryan R.",
            "family": "Goldsmith",
            "orcid": "https://orcid.org/0000-0002-1825-0097",
        }
        row = author_row_from_entry(e, 2, source="crossref")
        assert row == {
            "position": 2,
            "given": "Bryan",
            "middle": "R.",
            "family": "Goldsmith",
            "name_raw": "Bryan R. Goldsmith",
            "orcid": "0000-0002-1825-0097",
            "openalex_author_id": None,
            "source": "crossref",
        }
        # projection back re-absorbs middle into given (CSL shape)
        assert entry_from_author_row(row) == {
            "given": "Bryan R.",
            "family": "Goldsmith",
            "orcid": "0000-0002-1825-0097",
        }

    def test_single_comma_name_splits(self) -> None:
        row = author_row_from_entry({"name": "Zywucka, N."}, 1, source="legacy")
        assert row is not None
        assert (row["given"], row["middle"], row["family"]) == ("N.", "", "Zywucka")
        assert row["name_raw"] == "Zywucka, N."
        assert entry_from_author_row(row) == {"given": "N.", "family": "Zywucka"}

    def test_ambiguous_name_keeps_name_raw_only(self) -> None:
        row = author_row_from_entry({"name": "A.K. Geim"}, 1, source="pdf")
        assert row is not None
        assert (row["given"], row["middle"], row["family"]) == ("", "", "")
        assert row["name_raw"] == "A.K. Geim"  # the received string, untouched
        # the projection renders the tidied {name} shape
        assert entry_from_author_row(row) == {"name": "A. K. Geim"}

    def test_junk_gets_no_row(self) -> None:
        assert author_row_from_entry("REFERENCES", 1, source="pdf") is None
        assert author_row_from_entry({"name": ""}, 1, source="pdf") is None

    def test_openalex_author_id_rides_along(self) -> None:
        e = {"given": "Jane", "family": "Smith", "openalex_author_id": "A123"}
        row = author_row_from_entry(e, 1, source="openalex")
        assert row is not None
        assert row["openalex_author_id"] == "A123"
        assert entry_from_author_row(row)["openalex_author_id"] == "A123"

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown author source"):
            author_row_from_entry({"name": "Jane Smith"}, 1, source="magic")

    def test_round_trip_matches_normalize_authors(self) -> None:
        """Acceptance (S1): projecting a row back yields exactly what
        ``normalize_authors`` would have stored for the same entry."""
        fixtures: list[object] = [
            {"given": "Bryan R.", "family": "Goldsmith"},
            {"name": "Zywucka, N."},
            {"name": "Christoph Dellago"},
            "Smith, Jane",
            {"family": "Aristotle"},
            {"name": "A.K. Geim"},
        ]
        for e in fixtures:
            row = author_row_from_entry(e, 1, source="legacy")
            assert row is not None
            assert entry_from_author_row(row) == normalize_authors([e])[0], e

    def test_row_shape_entry_reabsorbs_middle(self) -> None:
        """S1's own row shape (given/middle/family as separate keys, as
        an ``edit(authors=[...])`` caller could pass) round-trips through
        the same given+middle merge / split as a plain string does — the
        ``middle`` key is not silently dropped."""
        e = {
            "given": "Bryan",
            "middle": "R.",
            "family": "Goldsmith",
            "orcid": "0000-0002-1825-0097",
            "openalex_author_id": "A123",
        }
        row = author_row_from_entry(e, 1, source="human")
        assert row is not None
        assert (row["given"], row["middle"], row["family"]) == (
            "Bryan",
            "R.",
            "Goldsmith",
        )
        assert row["orcid"] == "0000-0002-1825-0097"
        assert row["openalex_author_id"] == "A123"
        assert row["name_raw"] == "Bryan R. Goldsmith"


class TestOrcidBracketParsing:
    """Web textarea grammar: a trailing bracketed ORCID iD on a name
    string is stripped and carried as ``orcid`` (docs/backlog S4)."""

    def test_bracketed_orcid_stripped_and_carried(self) -> None:
        row = author_row_from_entry(
            "Goldsmith, Bryan R. [0000-0002-1825-0097]", 1, source="human"
        )
        assert row is not None
        assert (row["given"], row["middle"], row["family"]) == (
            "Bryan",
            "R.",
            "Goldsmith",
        )
        assert row["orcid"] == "0000-0002-1825-0097"

    def test_bracketed_full_orcid_url_accepted(self) -> None:
        row = author_row_from_entry(
            "Goldsmith, Bryan R. [https://orcid.org/0000-0002-1825-0097]",
            1,
            source="human",
        )
        assert row is not None
        assert row["orcid"] == "0000-0002-1825-0097"

    def test_invalid_bracket_dropped_silently_name_kept(self) -> None:
        row = author_row_from_entry(
            "Goldsmith, Bryan R. [not-an-orcid]", 1, source="human"
        )
        assert row is not None
        assert row["orcid"] is None
        assert (row["given"], row["middle"], row["family"]) == (
            "Bryan",
            "R.",
            "Goldsmith",
        )

    def test_name_shape_dict_also_accepts_bracket(self) -> None:
        entry = normalize_authors(["Zywucka, N. [0000-0002-1825-0097]"])[0]
        assert entry == {
            "given": "N.",
            "family": "Zywucka",
            "orcid": "0000-0002-1825-0097",
        }

    def test_ambiguous_natural_string_with_bracket_keeps_name_shape(self) -> None:
        # No comma → stays ``{"name"}``, same ambiguity rule as always;
        # the bracket is still stripped and carried.
        assert normalize_authors(["Christoph Dellago [0000-0002-1825-0097]"]) == [
            {"name": "Christoph Dellago", "orcid": "0000-0002-1825-0097"}
        ]


class TestAuthorLinks:
    def test_all_three_link_kinds(self) -> None:
        row = {
            "given": "Bryan",
            "middle": "R.",
            "family": "Goldsmith",
            "name_raw": "Bryan R. Goldsmith",
            "orcid": "0000-0002-1825-0097",
            "openalex_author_id": "A123",
        }
        links = author_links(row)
        assert links["orcid"] == "https://orcid.org/0000-0002-1825-0097"
        assert links["openalex"] == "https://openalex.org/A123"
        assert links["scholar"] == (
            "https://scholar.google.com/scholar?q=%22Bryan%20R.%20Goldsmith%22"
        )

    def test_missing_identity_links_absent(self) -> None:
        row = {"given": "N.", "family": "Zywucka", "name_raw": "Zywucka, N."}
        links = author_links(row)
        assert "orcid" not in links
        assert "openalex" not in links
        assert links["scholar"].startswith("https://scholar.google.com/scholar?q=")

    def test_unsplit_row_falls_back_to_name_raw(self) -> None:
        row = {"given": "", "middle": "", "family": "", "name_raw": "Aabid Hamid"}
        links = author_links(row)
        assert links["scholar"] == (
            "https://scholar.google.com/scholar?q=%22Aabid%20Hamid%22"
        )


class TestPaperScholarLink:
    def test_doi_preferred(self) -> None:
        assert paper_scholar_link("10.1/xyz", "Some Title") == (
            "https://scholar.google.com/scholar_lookup?doi=10.1%2Fxyz"
        )

    def test_title_fallback(self) -> None:
        assert paper_scholar_link(None, "Some Title") == (
            "https://scholar.google.com/scholar?q=Some%20Title"
        )

    def test_neither_is_none(self) -> None:
        assert paper_scholar_link(None, None) is None
        assert paper_scholar_link("", "") is None


class TestAuthorLine:
    def test_round_trips_bracketed_orcid(self) -> None:
        row = {
            "given": "Bryan",
            "middle": "R.",
            "family": "Goldsmith",
            "name_raw": "Bryan R. Goldsmith",
            "orcid": "0000-0002-1825-0097",
        }
        line = author_line(row)
        assert line == "Goldsmith, Bryan R. [0000-0002-1825-0097]"
        row2 = author_row_from_entry(line, 1, source="human")
        assert row2 is not None
        assert (row2["given"], row2["middle"], row2["family"], row2["orcid"]) == (
            "Bryan",
            "R.",
            "Goldsmith",
            "0000-0002-1825-0097",
        )

    def test_no_orcid_no_bracket(self) -> None:
        row = {"given": "N.", "middle": "", "family": "Zywucka", "orcid": None}
        assert author_line(row) == "Zywucka, N."

    def test_unsplit_row_renders_name_raw(self) -> None:
        row = {"given": "", "middle": "", "family": "", "name_raw": "Aabid Hamid"}
        assert author_line(row) == "Aabid Hamid"
