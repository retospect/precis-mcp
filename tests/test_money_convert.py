"""Convert-on-read for currency measures — slice 3 of
docs/backlog/measure-currency-units.md (gr510555, ruling 2026-10-28): a
currency value converts to EUR at the target year (the current UTC year) —
FX at the price year's ECB annual-average quote, then CPI deflation — and
renders with the original figure beside it. A missing (or too thin) rate
or index renders the original with a note naming it; a rate is never
guessed. Pure: fixed MoneyTables, no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.handlers import _measure_render as render
from precis.taxonomy.money import (
    MoneyConversion,
    MoneyRefusal,
    MoneyTables,
    convert_money,
    default_target_year,
)

_NOW = datetime.now(UTC).year

#: Fixed caches: per_eur is units of the currency per 1 EUR (as the ECB
#: publishes), n_days the quote days behind the annual average; CPI is the
#: wb:FP.CPI.TOTL:USA index.
_TABLES = MoneyTables(
    fx={
        ("USD", 2019): (1.12, 256),
        ("USD", _NOW): (1.08, 200),
        ("GBP", 2019): (0.89, 255),
    },
    cpi={2015: 100.0, 2019: 110.0, _NOW: 121.0},
)

#: The lagging cache: no CPI for the current year (the World Bank series
#: lags), so the target year falls back to 2019, the latest cached year.
_LAGGING = MoneyTables(fx=_TABLES.fx, cpi={2015: 100.0, 2019: 110.0})

#: 1300 USD(2019) at 1.12 USD/EUR deflated by 121/110.
_USD_2019_FACTOR = (1.0 / 1.12) * (121.0 / 110.0)


class TestConvertMoney:
    def test_usd_at_the_price_year_to_eur_at_the_target_year(self) -> None:
        got = convert_money(_TABLES, currency="USD", price_year=2019, target_year=_NOW)
        assert isinstance(got, MoneyConversion)
        assert got.factor == pytest.approx(_USD_2019_FACTOR)
        assert got.per_eur == 1.12 and got.fx_days == 256
        assert got.cpi_from == 110.0 and got.cpi_to == 121.0

    def test_eur_deflates_without_an_fx_leg(self) -> None:
        got = convert_money(_TABLES, currency="EUR", price_year=2015, target_year=_NOW)
        assert isinstance(got, MoneyConversion)
        assert got.factor == pytest.approx(121.0 / 100.0)
        assert got.per_eur is None and got.fx_days is None
        assert got.cpi_from == 100.0 and got.cpi_to == 121.0

    def test_eur_of_the_target_year_is_unchanged(self) -> None:
        got = convert_money(_TABLES, currency="EUR", price_year=_NOW, target_year=_NOW)
        assert isinstance(got, MoneyConversion)
        assert got.factor == 1.0
        assert got.per_eur is None and got.cpi_from is None and got.cpi_to is None

    def test_a_non_eur_row_of_the_target_year_priced_by_fx_only(self) -> None:
        got = convert_money(_TABLES, currency="USD", price_year=_NOW, target_year=_NOW)
        assert isinstance(got, MoneyConversion)
        assert got.factor == pytest.approx(1.0 / 1.08)

    def test_no_price_year_refuses(self) -> None:
        got = convert_money(_TABLES, currency="USD", price_year=None, target_year=_NOW)
        assert got == MoneyRefusal("price year unknown")

    def test_a_missing_fx_rate_refuses_naming_it(self) -> None:
        got = convert_money(_TABLES, currency="GBP", price_year=2020, target_year=_NOW)
        assert got == MoneyRefusal("no ECB rate for GBP in 2020")

    def test_a_thin_fx_year_refuses_naming_the_day_count(self) -> None:
        thin = MoneyTables(fx={("ISK", 2019): (150.0, 12)}, cpi=_TABLES.cpi)
        got = convert_money(thin, currency="ISK", price_year=2019, target_year=_NOW)
        assert got == MoneyRefusal("ECB rate for ISK in 2019 has only 12 quote days")

    def test_a_missing_target_index_refuses_with_the_covered_range(self) -> None:
        got = convert_money(
            _TABLES, currency="EUR", price_year=2019, target_year=_NOW + 1
        )
        assert got == MoneyRefusal(f"no CPI for {_NOW + 1} (have 2015–{_NOW})")

    def test_a_missing_price_index_refuses_naming_both_gaps(self) -> None:
        one_year = MoneyTables(fx=_TABLES.fx, cpi={2019: 110.0})
        got = convert_money(one_year, currency="EUR", price_year=2018, target_year=_NOW)
        assert got == MoneyRefusal(f"no CPI for 2018 or {_NOW} (have 2019)")


class TestDefaultTargetYear:
    def test_the_current_year_wins_when_it_has_cpi(self) -> None:
        assert default_target_year(_TABLES) == (_NOW, False)

    def test_the_latest_cached_year_wins_when_the_current_one_is_missing(self) -> None:
        assert default_target_year(_LAGGING) == (2019, True)

    def test_an_empty_cache_targets_the_current_year(self) -> None:
        assert default_target_year(MoneyTables(fx={}, cpi={})) == (_NOW, False)


class TestValueText:
    """The rendered reading a get/search line shows."""

    def test_usd_2019_renders_as_eur_at_the_target_year(self) -> None:
        row = {
            "value_num": 1300.0,
            "value_form": "point",
            "currency": "USD",
            "price_year": 2019,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"1277 EUR({_NOW})/t (1300 USD/t, 2019, FX: ECB annual mean 2019)"
        )

    def test_a_missing_current_year_falls_back_to_the_latest_cpi_year(self) -> None:
        # the World Bank series lags: no CPI for the current year, so the
        # target is 2019, the latest cached year, priced by the FX leg only
        row = {
            "value_num": 1300.0,
            "value_form": "point",
            "currency": "USD",
            "price_year": 2019,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_LAGGING) == (
            "1161 EUR(2019)/t (1300 USD/t, 2019, FX: ECB annual mean 2019, latest CPI year)"
        )

    def test_a_missing_fx_rate_renders_the_original_with_a_note(self) -> None:
        row = {
            "value_num": 100.0,
            "value_form": "point",
            "currency": "GBP",
            "price_year": 2020,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            "100 GBP(2020)/t (not converted: no ECB rate for GBP in 2020)"
        )

    def test_a_eur_value_of_the_target_year_is_unchanged(self) -> None:
        row = {
            "value_num": 100.0,
            "value_form": "point",
            "currency": "EUR",
            "price_year": _NOW,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"100 EUR({_NOW})/t (100 EUR/t, {_NOW})"
        )

    def test_a_missing_index_renders_the_original_with_a_note(self) -> None:
        row = {
            "value_num": 50.0,
            "value_form": "point",
            "currency": "EUR",
            "price_year": 2018,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"50 EUR(2018)/t (not converted: no CPI for 2018 (have 2015–{_NOW}))"
        )

    def test_a_yearless_value_renders_with_the_no_year_note(self) -> None:
        row = {
            "value_num": 500.0,
            "value_form": "point",
            "currency": "USD",
            "price_year": None,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            "500 USD/t (not converted: price year unknown)"
        )

    def test_an_interval_converts_end_to_end(self) -> None:
        row = {
            "value_low": 1.4,
            "value_high": 2.0,
            "value_form": "interval",
            "currency": "USD",
            "price_year": 2019,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"1.375–1.964 EUR({_NOW})/t (1.4–2 USD/t, 2019, FX: ECB annual mean 2019)"
        )

    def test_an_error_bar_scales_with_the_value(self) -> None:
        row = {
            "value_num": 9.6,
            "value_err": 1.7,
            "value_form": "point",
            "currency": "USD",
            "price_year": 2019,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"9.429 ± 1.67 EUR({_NOW})/t (9.6 ± 1.7 USD/t, 2019, FX: ECB annual mean 2019)"
        )

    def test_the_display_remainder_scales_before_the_conversion(self) -> None:
        # EUR/kg on a USD/t canonical: stored x1000, shown per kg, and the
        # conversion multiplies the displayed figure
        row = {
            "value_num": 1000.0,
            "value_form": "point",
            "currency": "EUR",
            "price_year": 2019,
            "canonical_unit": "USD/t",
            "display_unit": "EUR/kg",
        }
        assert render.value_text(row, money=_TABLES) == (
            f"1.1 EUR({_NOW})/kg (1 EUR/kg, 2019)"
        )

    def test_without_money_the_row_shows_as_reported(self) -> None:
        row = {
            "value_num": 1300.0,
            "value_form": "point",
            "currency": "USD",
            "price_year": 2019,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row) == "1300 USD(2019)/t"
