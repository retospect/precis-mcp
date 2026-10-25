"""``taxonomy/money.py`` and the currency branch of ``make_converter`` —
slice 1 of docs/backlog/measure-currency-units.md (gr510555): a currency is
recorded as reported, pint scales only the remainder, and nothing converts
between currencies. Pure (no DB).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.errors import BadInput
from precis.taxonomy.measure_units import (
    make_converter,
    pint_unit_text,
    to_canonical,
)
from precis.taxonomy.money import (
    CurrencyUnit,
    row_currency,
    split_currency,
    taxon_is_currency,
)

_NOW = datetime.now(UTC).year


class TestSplitCurrency:
    @pytest.mark.parametrize(
        ("unit", "want"),
        [
            ("USD", CurrencyUnit("USD", None, "")),
            ("EUR/t", CurrencyUnit("EUR", None, "/t")),
            ("EUR/kg", CurrencyUnit("EUR", None, "/kg")),
            ("USD2015", CurrencyUnit("USD", 2015, "")),
            ("USD 2015", CurrencyUnit("USD", 2015, "")),
            ("USD(2015)", CurrencyUnit("USD", 2015, "")),
            ("2015 USD", CurrencyUnit("USD", 2015, "")),
            ("2015USD", CurrencyUnit("USD", 2015, "")),
            ("EUR2015/kg", CurrencyUnit("EUR", 2015, "/kg")),
            # a year not next to the code is not a price year: it is part of
            # the remainder (and will refuse downstream like '2015')
            ("USD/t 2015", CurrencyUnit("USD", None, "/t 2015")),
            ("GBP/yr", CurrencyUnit("GBP", None, "/yr")),
            ("JPY", CurrencyUnit("JPY", None, "")),
            ("ISK2020/kg", CurrencyUnit("ISK", 2020, "/kg")),
            ("$", CurrencyUnit("USD", None, "")),
            ("$/t", CurrencyUnit("USD", None, "/t")),
            ("US$/kg", CurrencyUnit("USD", None, "/kg")),
            ("€", CurrencyUnit("EUR", None, "")),
            ("€2015/t", CurrencyUnit("EUR", 2015, "/t")),
            ("£/kg", CurrencyUnit("GBP", None, "/kg")),
            ("USD / kg", CurrencyUnit("USD", None, "/kg")),
        ],
    )
    def test_forms(self, unit: str, want: CurrencyUnit) -> None:
        assert split_currency(unit) == want

    @pytest.mark.parametrize("unit", ["kg", "mV", "kg/m³", "%", "t CO2-eq", "HK$/t"])
    def test_no_currency(self, unit: str) -> None:
        got = split_currency(unit)
        assert got.currency is None and got.price_year is None
        assert got.remainder == unit

    def test_empty(self) -> None:
        assert split_currency(None) == CurrencyUnit(None, None, "")
        assert split_currency("  ") == CurrencyUnit(None, None, "")

    @pytest.mark.parametrize(
        ("unit", "codes"),
        [("¥/kg", "JPY or CNY"), ("A$", "AUD"), ("C$/t", "CAD")],
    )
    def test_ambiguous_symbols_refused_naming_the_iso_code(
        self, unit: str, codes: str
    ) -> None:
        with pytest.raises(BadInput, match="ambiguous"):
            split_currency(unit)
        try:
            split_currency(unit)
        except BadInput as exc:
            assert codes in str(exc.next)

    @pytest.mark.parametrize("year", [1899, _NOW + 1, _NOW + 100])
    def test_a_year_out_of_range_is_refused(self, year: int) -> None:
        with pytest.raises(BadInput, match="price year"):
            split_currency(f"USD{year}")

    @pytest.mark.parametrize(
        "unit",
        ["1000 USD/yr", "USD 1000/t", "USD1899", "USD2200", "EUR(1024)/kg"],
    )
    def test_a_4_digit_number_outside_1900_2100_is_a_scale_factor(
        self, unit: str
    ) -> None:
        # 1000 USD/yr is a thousands scale, not a year: refused as an
        # unsupported scale factor, with next= naming the plain unit
        with pytest.raises(BadInput, match="scale factor") as err:
            split_currency(unit)
        assert "plain unit" in str(err.value.next)

    def test_the_year_token_bounds(self) -> None:
        # inside 1900..2100 the number is read as a year — and then the
        # 1900..current-year check still applies, so 2100 refuses as a
        # year, not as a scale factor
        assert split_currency("USD1900").price_year == 1900
        assert split_currency(f"USD{_NOW}").price_year == _NOW
        with pytest.raises(BadInput, match="price year 2100"):
            split_currency("USD2100")

    def test_a_short_digit_run_is_not_a_year(self) -> None:
        assert split_currency("USD120") == CurrencyUnit("USD", None, "120")

    @pytest.mark.parametrize("year", [1900, 1999, _NOW])
    def test_year_bounds(self, year: int) -> None:
        assert split_currency(f"USD{year}").price_year == year

    def test_two_different_years_refused(self) -> None:
        with pytest.raises(BadInput, match="two different price years"):
            split_currency("2015 USD 2016")

    def test_the_same_year_twice_is_fine(self) -> None:
        assert split_currency("2015 USD 2015").price_year == 2015

    def test_a_second_currency_is_refused(self) -> None:
        with pytest.raises(BadInput, match="more than one currency"):
            split_currency("USD/t EUR")


class TestMathDollars:
    """A ``$...$`` pair whose contents carry TeX is LaTeX math, never USD
    (gr510555 review): ``mA$^{-1}$`` is per-milliampere, not ambiguous
    ``A$``, and a lone ``$`` before a TeX command is an unclosed math
    span. A lone ``$`` elsewhere is still a dollar — and a pair whose
    contents hold no TeX mark is two dollars, not math."""

    @pytest.mark.parametrize(
        ("unit", "remainder"),
        [
            ("$\\mu m$", "\\mu m"),
            ("$\\mu m", "\\mu m"),  # unclosed span before a TeX command
            ("m$^2$", "m^2"),
            ("mA$^{-1}$", "mA^{-1}"),
            ("°C$^{-1}$", "°C^{-1}"),
            ("K$^{-1}$", "K^{-1}"),
            ("W m$^{-2}$", "W m^{-2}"),
        ],
    )
    def test_math_delimiters_are_not_currency(self, unit: str, remainder: str) -> None:
        assert split_currency(unit) == CurrencyUnit(None, None, remainder)

    def test_a_lone_dollar_is_still_usd(self) -> None:
        assert split_currency("$/t") == CurrencyUnit("USD", None, "/t")
        assert split_currency("$") == CurrencyUnit("USD", None, "")

    @pytest.mark.parametrize(
        ("unit", "want"),
        [
            ("$/kWh ($2015)", CurrencyUnit("USD", 2015, "/kWh")),
            ("$/t (2015$)", CurrencyUnit("USD", 2015, "/t")),
            ("$/t ($2015)", CurrencyUnit("USD", 2015, "/t")),
        ],
    )
    def test_a_pair_without_tex_is_two_dollars(
        self, unit: str, want: CurrencyUnit
    ) -> None:
        # '($2015)' is a price year in dollars, not a math span — the pair's
        # contents carry no TeX mark, so both $ read as currency and the
        # second token merges back (same currency, same or no year)
        assert split_currency(unit) == want

    def test_two_dollars_that_disagree_refuse(self) -> None:
        with pytest.raises(BadInput, match="more than one currency"):
            split_currency("$/t (EUR2015)")
        with pytest.raises(BadInput, match="two different price years"):
            split_currency("USD2015/t ($2016)")

    @pytest.mark.parametrize("unit", ["USD/USD", "$/$", "USD (USD2015)"])
    def test_a_currency_twice_and_no_unit_refuses(self, unit: str) -> None:
        # the merged remainder is empty or only '/': there is no unit at all
        with pytest.raises(BadInput, match="only the currency"):
            split_currency(unit)

    @pytest.mark.parametrize(
        ("unit", "want"),
        [
            ("$\\mu m$", "µ m"),
            ("$\\mu m", "µ m"),
            ("$\\mu$m", "µm"),
            ("$\\mu$g/L", "µg/L"),
            ("m$^2$", "m^2"),
            ("mA$^{-1}$", "mA^-1"),
            ("°C$^{-1}$", "°C^-1"),
            ("W m$^{-2}$", "W m^-2"),
        ],
    )
    def test_pint_unit_text_folds_math_first(self, unit: str, want: str) -> None:
        assert pint_unit_text(unit) == (want, None)

    @pytest.mark.parametrize(
        ("reported", "canonical"),
        [
            ("m$^2$", "m^2"),
            ("mA$^{-1}$", "mA^-1"),
            ("K$^{-1}$", "K^-1"),
            ("°C$^{-1}$", "°C^-1"),
            ("W m$^{-2}$", "W m^-2"),
        ],
    )
    def test_make_converter_reads_math_units(
        self, reported: str, canonical: str
    ) -> None:
        assert make_converter(reported, canonical).scale == pytest.approx(1.0)

    def test_make_converter_reads_a_mu_priced_unit(self) -> None:
        # the common LaTeX form: the math span reaches pint through
        # markup_unit, so '$\mu$g/L' converts like 'µg/L'
        assert make_converter("$\\mu$g/L", "kg/m^3").scale == pytest.approx(1e-6)

    def test_make_converter_reads_two_dollars_as_usd_year(self) -> None:
        assert make_converter("$/t (2015$)", "USD/t").scale == pytest.approx(1.0)
        assert make_converter("$/kWh ($2015)", "USD/kWh").scale == pytest.approx(1.0)


class TestMoneyConverter:
    def test_remainder_scales_through_pint(self) -> None:
        # the acceptance case: EUR/kg on a USD/t measurand stores x1000 in EUR
        conv = make_converter("EUR/kg", "USD/t")
        assert conv.value(1.0) == pytest.approx(1000.0)
        assert conv.scale == pytest.approx(1000.0)

    def test_same_remainder_keeps_the_amount(self) -> None:
        conv = make_converter("EUR/t", "USD/t")
        assert conv.value(1000.0) == pytest.approx(1000.0)
        assert conv.scale == pytest.approx(1.0)

    def test_a_year_token_is_not_part_of_the_conversion(self) -> None:
        conv = make_converter("EUR2015/kg", "USD/t")
        assert conv.value(1.0) == pytest.approx(1000.0)

    def test_a_symbol_is_its_code(self) -> None:
        assert make_converter("$/t", "USD/t").scale == pytest.approx(1.0)
        assert make_converter("€/kg", "USD/t").scale == pytest.approx(1000.0)

    def test_a_bare_currency_matches_a_bare_currency(self) -> None:
        assert make_converter("EUR", "USD").scale == pytest.approx(1.0)

    def test_an_unparseable_remainder_must_match_textually(self) -> None:
        conv = make_converter("EUR/nozzle", "USD/nozzle")
        assert conv.scale == pytest.approx(1.0)
        with pytest.raises(BadInput, match="no dimension match"):
            make_converter("EUR/nozzle", "USD/t")

    @pytest.mark.parametrize(
        ("reported", "canonical"),
        [
            ("EUR/t", "kg"),  # currency against a non-currency canonical
            ("kg", "USD/t"),  # a non-currency unit on a currency measurand
            ("EUR", "USD/kg"),  # bare currency against a per-mass canonical
            ("EUR/t", "USD"),  # per-mass against a bare currency
        ],
    )
    def test_mismatched_dimensions_keep_the_refusal(
        self, reported: str, canonical: str
    ) -> None:
        with pytest.raises(BadInput, match="no dimension match"):
            make_converter(reported, canonical)

    def test_a_currency_bound_converts_the_remainder_only(self) -> None:
        # search bounds: 100 EUR/t is 100 in the stored EUR/t numbers; a
        # EUR/kg bound is scaled to the per-t remainder
        assert to_canonical(100, "EUR/t", "USD/t") == pytest.approx(100.0)
        assert to_canonical(0.1, "EUR/kg", "USD/t") == pytest.approx(100.0)
        with pytest.raises(BadInput):
            to_canonical(1, "EUR/nozzle", "USD/t")


class TestEffectiveCurrency:
    def test_a_stored_currency_wins(self) -> None:
        row = {"currency": "EUR", "price_year": 2015, "reported_unit": "EUR2015/kg"}
        assert row_currency(row) == ("EUR", 2015)

    def test_a_legacy_row_reads_its_reported_unit(self) -> None:
        row = {"currency": None, "price_year": None, "reported_unit": "USD"}
        assert row_currency(row) == ("USD", None)

    def test_the_canonical_token_is_the_fallback(self) -> None:
        row = {"currency": None, "reported_unit": None, "canonical_unit": "USD/t"}
        assert row_currency(row) == ("USD", None)

    def test_no_currency_anywhere(self) -> None:
        row = {"currency": None, "reported_unit": "kg", "canonical_unit": "kg"}
        assert row_currency(row) == (None, None)

    def test_taxon_is_currency(self) -> None:
        assert taxon_is_currency({"dimension_kind": "currency"}, None)
        assert taxon_is_currency({}, "USD/t")
        assert not taxon_is_currency({}, "kg")
        assert not taxon_is_currency({"dimension_kind": "si"}, "m")
