"""Unit tests for the canonical author helper (precis.utils.authors).

Pure — no DB. Locks the shape-tolerance contract that the web display,
citation generation, provenance report and bib generation all now share.
"""

from __future__ import annotations

from precis.utils.authors import author_display, author_names, to_name_dicts


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
