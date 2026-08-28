"""Unit tests for the canonical author helper (precis.utils.authors).

Pure — no DB. Locks the shape-tolerance contract that the web display,
citation generation, provenance report and bib generation all now share.
"""

from __future__ import annotations

from precis.utils.authors import (
    author_display,
    author_names,
    build_byline,
    is_junk_author_name,
    normalize_authors,
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
