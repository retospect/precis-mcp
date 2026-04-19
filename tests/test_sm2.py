"""Tests for SM-2 spaced repetition algorithm."""

from __future__ import annotations

from datetime import datetime

import pytest

from precis.handlers.sm2 import DEFAULT_EASINESS, MIN_EASINESS, SM2Result, update

NOW = datetime(2026, 4, 8, 12, 0, 0)


class TestSM2Update:
    """Core SM-2 algorithm tests."""

    def test_first_correct_review(self):
        r = update(DEFAULT_EASINESS, 0, 0, quality=4, now=NOW)
        assert r.reps == 1
        assert r.interval == 1
        assert r.next_review == datetime(2026, 4, 9, 12, 0, 0)

    def test_second_correct_review(self):
        r = update(DEFAULT_EASINESS, 1, 1, quality=4, now=NOW)
        assert r.reps == 2
        assert r.interval == 6
        assert r.next_review == datetime(2026, 4, 14, 12, 0, 0)

    def test_third_correct_review_uses_easiness(self):
        r = update(2.5, 6, 2, quality=4, now=NOW)
        assert r.reps == 3
        assert r.interval == pytest.approx(15.0, abs=0.5)
        assert r.easiness == pytest.approx(2.5, abs=0.1)

    def test_perfect_review_increases_easiness(self):
        r = update(2.5, 6, 2, quality=5, now=NOW)
        assert r.easiness > 2.5

    def test_quality_3_decreases_easiness(self):
        r = update(2.5, 6, 2, quality=3, now=NOW)
        assert r.easiness < 2.5

    def test_failed_review_resets(self):
        r = update(2.5, 30, 5, quality=2, now=NOW)
        assert r.reps == 0
        assert r.interval == 1
        assert r.next_review == datetime(2026, 4, 9, 12, 0, 0)

    def test_complete_blank_resets(self):
        r = update(2.5, 30, 5, quality=0, now=NOW)
        assert r.reps == 0
        assert r.interval == 1

    def test_easiness_never_below_minimum(self):
        r = update(MIN_EASINESS, 6, 2, quality=3, now=NOW)
        assert r.easiness >= MIN_EASINESS

    def test_repeated_failures_keep_min_easiness(self):
        e = DEFAULT_EASINESS
        for _ in range(20):
            r = update(e, 1, 0, quality=0, now=NOW)
            e = r.easiness
        assert e >= MIN_EASINESS

    def test_invalid_quality_raises(self):
        with pytest.raises(ValueError, match="quality must be 0-5"):
            update(2.5, 1, 0, quality=6, now=NOW)

    def test_negative_quality_raises(self):
        with pytest.raises(ValueError, match="quality must be 0-5"):
            update(2.5, 1, 0, quality=-1, now=NOW)

    def test_interval_grows_exponentially(self):
        """Verify intervals grow over repeated correct reviews."""
        e, interval, reps = DEFAULT_EASINESS, 0, 0
        intervals = []
        for _ in range(6):
            r = update(e, interval, reps, quality=4, now=NOW)
            e, interval, reps = r.easiness, r.interval, r.reps
            intervals.append(r.interval)
        # Each interval should be >= previous (monotonic growth)
        for i in range(1, len(intervals)):
            assert intervals[i] >= intervals[i - 1]
        # After 6 perfect reviews, interval should be months
        assert intervals[-1] > 60

    def test_result_is_dataclass(self):
        r = update(2.5, 1, 1, quality=4, now=NOW)
        assert isinstance(r, SM2Result)
        assert isinstance(r.easiness, float)
        assert isinstance(r.interval, (int, float))
        assert isinstance(r.reps, int)
        assert isinstance(r.next_review, datetime)
