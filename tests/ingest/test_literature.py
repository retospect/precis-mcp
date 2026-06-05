"""Tests for shared literature helpers."""

from __future__ import annotations

from precis.ingest.literature import (
    SKIP_EMBED_TYPES,
    first_author_key,
    first_author_surname,
)


class TestSkipEmbedTypes:
    def test_is_frozenset(self):
        assert isinstance(SKIP_EMBED_TYPES, frozenset)

    def test_expected_members(self):
        assert (
            frozenset({"section_header", "title", "author", "equation", "junk"})
            == SKIP_EMBED_TYPES
        )


class TestFirstAuthorKey:
    def test_list_of_dicts_comma_first(self):
        assert first_author_key([{"name": "Smith, John"}]) == "Smith"

    def test_list_of_dicts_first_last(self):
        assert first_author_key([{"name": "Daniel S. Levine"}]) == "Daniel S. Levine"

    def test_semicolon_packed(self):
        authors = [{"name": "Daniel S. Levine; Nicholas Liesen; Lauren Chua"}]
        assert first_author_key(authors) == "Daniel S. Levine"

    def test_list_of_strings(self):
        assert first_author_key(["Zou, Jiawen"]) == "Zou"

    def test_json_string(self):
        assert first_author_key('[{"name": "Müller, Hans"}]') == "Müller"

    def test_empty_list(self):
        assert first_author_key([]) == ""

    def test_none(self):
        assert first_author_key(None) == ""

    def test_malformed_json(self):
        assert first_author_key("not-json") == ""

    def test_missing_name_key(self):
        assert first_author_key([{}]) == ""


class TestFirstAuthorSurname:
    def test_last_first(self):
        assert first_author_surname([{"name": "Smith, John"}]) == "Smith"

    def test_first_last(self):
        assert first_author_surname([{"name": "John Smith"}]) == "Smith"

    def test_first_middle_last(self):
        assert first_author_surname([{"name": "Daniel S. Levine"}]) == "Levine"

    def test_preserves_case(self):
        assert first_author_surname([{"name": "Müller, Hans"}]) == "Müller"

    def test_json_string_input(self):
        assert first_author_surname('[{"name": "Zou, Jiawen"}]') == "Zou"

    def test_empty(self):
        assert first_author_surname([]) == ""


# NOTE: The 17-test ``TestMakeSlug`` class that used to live here was
# removed in B3a. ``make_slug`` was dropped per ADR 0008 (slug retired;
# identifiers normalised into ref_identifiers). The behavioural
# equivalent — author/year/keyword folding — is now covered by
# ``tests/test_identity.py::test_cite_key_*`` which exercises
# ``precis.identity.make_cite_key`` (the ``miller23a``-style algorithm
# per ADR 0006).


# Tests for ``build_embedder`` / ``EmbedderUnavailableError`` were
# removed alongside the helper itself in 2026-06-05: the factory had
# no production callers (BgeM3Embedder is constructed directly) and
# the chroma path was dormant. ``test_identity.py`` covers the slug
# / cite-key helpers that replaced ``make_slug`` per ADR 0008.
