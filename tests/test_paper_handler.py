"""Tests for paper handler date/tag/filter features."""

from datetime import datetime, timedelta, timezone

import pytest

from precis.handlers._ref_base import (
    _parse_date_value,
    _parse_filters,
    _parse_year_value,
    _relative_date,
)


class TestParseDateValue:
    def test_today(self):
        result = _parse_date_value("today")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert result is not None
        assert result.hour == 0 and result.minute == 0
        assert result.date() == now.date()

    def test_yesterday(self):
        result = _parse_date_value("yesterday")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert result is not None
        assert result.date() == (now - timedelta(days=1)).date()

    def test_this_week(self):
        result = _parse_date_value("this-week")
        assert result is not None
        assert result.weekday() == 0  # Monday

    def test_this_month(self):
        result = _parse_date_value("this-month")
        assert result is not None
        assert result.day == 1

    def test_iso_date(self):
        result = _parse_date_value("2025-03-15")
        assert result == datetime(2025, 3, 15)

    def test_non_date_returns_none(self):
        assert _parse_date_value("MOF") is None
        assert _parse_date_value("quantum") is None
        assert _parse_date_value("/regex/i") is None

    def test_case_insensitive(self):
        assert _parse_date_value("TODAY") is not None
        assert _parse_date_value("This-Week") is not None


class TestParseYearValue:
    def test_single_year(self):
        assert _parse_year_value("2024") == (2024, 2024)

    def test_range(self):
        assert _parse_year_value("2020-2024") == (2020, 2024)

    def test_open_range(self):
        assert _parse_year_value("2020-") == (2020, None)

    def test_invalid(self):
        assert _parse_year_value("abc") == (None, None)
        assert _parse_year_value("") == (None, None)


class TestParseFilters:
    def test_plain_grep(self):
        result = _parse_filters("quantum dots")
        assert result == {"grep": "quantum dots"}

    def test_ingested_only(self):
        result = _parse_filters("ingested:today")
        assert result == {"ingested": "today", "grep": ""}

    def test_year_only(self):
        result = _parse_filters("year:2020-2024")
        assert result == {"year": "2020-2024", "grep": ""}

    def test_tag_only(self):
        result = _parse_filters("tag:review")
        assert result == {"tag": "review", "grep": ""}

    def test_combined(self):
        result = _parse_filters("ingested:today tag:review MOF")
        assert result == {"ingested": "today", "tag": "review", "grep": "MOF"}

    def test_year_and_grep(self):
        result = _parse_filters("year:2020- quantum")
        assert result == {"year": "2020-", "grep": "quantum"}

    def test_empty(self):
        result = _parse_filters("")
        assert result == {"grep": ""}

    def test_unknown_prefix_stays_in_grep(self):
        result = _parse_filters("foo:bar baz")
        assert result == {"grep": "foo:bar baz"}

    def test_no_value_after_colon(self):
        result = _parse_filters("tag: something")
        assert result == {"grep": "tag: something"}


class TestRelativeDate:
    def _utcnow(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def test_today(self):
        assert _relative_date(self._utcnow()) == "today"

    def test_yesterday(self):
        assert _relative_date(self._utcnow() - timedelta(days=1)) == "yesterday"

    def test_days_ago(self):
        assert _relative_date(self._utcnow() - timedelta(days=3)) == "3d ago"

    def test_weeks_ago(self):
        result = _relative_date(self._utcnow() - timedelta(days=14))
        assert result == "2w ago"

    def test_months_ago(self):
        result = _relative_date(self._utcnow() - timedelta(days=60))
        assert result == "2mo ago"

    def test_old_date(self):
        result = _relative_date(datetime(2020, 1, 15))
        assert result == "2020-01-15"

    def test_none(self):
        assert _relative_date(None) == ""
