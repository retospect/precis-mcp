"""Pure unit tests for the author-year short-cite (source-backfill slice 2)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from precis.utils.short_cite import short_cite


def _ref(**kw: object) -> NS:
    base: dict[str, object] = {
        "authors": None,
        "year": None,
        "slug": None,
        "title": None,
    }
    base.update(kw)
    return NS(**base)


def test_first_last_name_and_year() -> None:
    assert short_cite(_ref(authors=[{"name": "Jun Wang"}], year=2020)) == "Wang'20"


def test_comma_name() -> None:
    assert short_cite(_ref(authors=[{"name": "Wang, Jun"}], year=2019)) == "Wang'19"


def test_year_is_two_digit_padded() -> None:
    assert short_cite(_ref(authors=[{"name": "A Bee"}], year=2005)) == "Bee'05"


def test_surname_only_when_no_year() -> None:
    assert short_cite(_ref(authors=[{"name": "Kumar"}])) == "Kumar"


def test_fallback_to_slug_then_title_then_qmark() -> None:
    assert short_cite(_ref(slug="wang2020")) == "wang2020"
    long_title = "A Very Long Title About Garnet Electrolytes And Other Things"
    assert short_cite(_ref(title=long_title)).endswith("…")
    assert short_cite(_ref()) == "?"
