"""Unit tests for the canonical author helper (precis.utils.authors).

Pure — no DB. Locks the shape-tolerance contract that the web display,
citation generation, provenance report and bib generation all now share.
"""

from __future__ import annotations

from precis.utils.authors import (
    author_display,
    author_names,
    build_byline,
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
