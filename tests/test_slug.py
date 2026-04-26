"""Tests for the slug minter."""

from __future__ import annotations

import pytest

from precis.utils.slug import mint_slug

# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------


class TestBasicShape:
    def test_simple_three_part(self) -> None:
        s = mint_slug(authors=["Wang, Q."], year=2020, title="State of the art")
        assert s == "wang2020state"

    def test_first_name_last_name(self) -> None:
        s = mint_slug(
            authors=["Quentin Wang"],
            year=2020,
            title="State of the art",
        )
        assert s == "wang2020state"

    def test_skips_stopwords(self) -> None:
        s = mint_slug(
            authors=["Kim"],
            year=2024,
            title="The Electrocatalytic Reduction",
        )
        assert s == "kim2024electrocatalytic"

    def test_first_word_when_all_stopwords(self) -> None:
        s = mint_slug(authors=["Lee"], year=2021, title="The of the")
        assert s == "lee2021the"

    def test_year_none_falls_back(self) -> None:
        s = mint_slug(authors=["Wang"], year=None, title="State")
        assert s == "wang0000state"


# ---------------------------------------------------------------------------
# Author edge cases
# ---------------------------------------------------------------------------


class TestAuthors:
    def test_no_authors_uses_anon(self) -> None:
        s = mint_slug(authors=[], year=2020, title="State")
        assert s == "anon2020state"

    def test_single_token_surname(self) -> None:
        s = mint_slug(authors=["Curie"], year=1903, title="Radium")
        assert s == "curie1903radium"

    def test_diacritics_folded(self) -> None:
        s = mint_slug(authors=["Müller, A."], year=2024, title="X")
        assert s == "muller2024x"

    def test_compound_surname_letters_only(self) -> None:
        s = mint_slug(authors=["Marques-Silva, J."], year=1999, title="Grasp")
        # hyphen folds out; we keep both parts
        assert s == "marquessilva1999grasp"

    def test_surname_long_capped_at_30(self) -> None:
        long = "A" * 50
        s = mint_slug(authors=[long], year=2024, title="X")
        assert s.startswith("a" * 30)
        assert s == "a" * 30 + "2024x"


# ---------------------------------------------------------------------------
# Title edge cases
# ---------------------------------------------------------------------------


class TestTitle:
    def test_empty_title_uses_untitled(self) -> None:
        s = mint_slug(authors=["Wang"], year=2020, title="")
        assert s == "wang2020untitled"

    def test_non_latin_title_hashes(self) -> None:
        s = mint_slug(authors=["Wang"], year=2020, title="\u4e2d\u6587\u9898\u76ee")
        # surname + year + 6-hex hash chunk
        assert s.startswith("wang2020")
        assert len(s) == len("wang2020") + 6

    def test_keyword_capped(self) -> None:
        long_word = "x" * 50
        s = mint_slug(authors=["Wang"], year=2020, title=long_word)
        assert s == "wang2020" + "x" * 20


# ---------------------------------------------------------------------------
# Collision handling
# ---------------------------------------------------------------------------


class TestCollision:
    def test_no_predicate_returns_base(self) -> None:
        s = mint_slug(authors=["Wang"], year=2020, title="State")
        assert s == "wang2020state"

    def test_first_collision_appends_2(self) -> None:
        taken = {"wang2020state"}
        s = mint_slug(
            authors=["Wang"],
            year=2020,
            title="State",
            existing=lambda x: x in taken,
        )
        assert s == "wang2020state-2"

    def test_chain_of_collisions(self) -> None:
        taken = {f"wang2020state{suf}" for suf in ("", "-2", "-3", "-4")}
        s = mint_slug(
            authors=["Wang"],
            year=2020,
            title="State",
            existing=lambda x: x in taken,
        )
        assert s == "wang2020state-5"

    def test_runaway_predicate_raises(self) -> None:
        with pytest.raises(RuntimeError):
            mint_slug(
                authors=["Wang"],
                year=2020,
                title="State",
                existing=lambda x: True,
            )


# ---------------------------------------------------------------------------
# Determinism — pure function, same inputs → same output
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_output(self) -> None:
        for _ in range(5):
            s = mint_slug(authors=["Wang, Q."], year=2020, title="State of the art")
            assert s == "wang2020state"
