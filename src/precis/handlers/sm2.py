"""SM-2 spaced repetition algorithm.

Reference: https://supermemo.com/en/blog/application-of-a-computer-to-improve-the-results-obtained-in-working-with-the-supermemo-method

Tracks three values per item:
  - easiness (float, >=1.3, starts 2.5)
  - interval (float, days until next review)
  - reps (int, consecutive correct answers)

Quality scale 0-5:
  5 — perfect, instant
  4 — correct, slight hesitation
  3 — correct, hard effort
  2 — wrong but close
  1 — wrong, recognised correct answer
  0 — complete blank
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_EASINESS = 2.5
MIN_EASINESS = 1.3


@dataclass
class SM2Result:
    """Result of an SM-2 update."""

    easiness: float
    interval: float  # days
    reps: int
    next_review: datetime


def update(
    easiness: float,
    interval: float,
    reps: int,
    quality: int,
    now: datetime | None = None,
) -> SM2Result:
    """Run one SM-2 iteration.

    Args:
        easiness: Current easiness factor (>=1.3).
        interval: Current interval in days.
        reps: Consecutive correct repetitions.
        quality: Recall quality 0-5.
        now: Override current time (for testing).

    Returns:
        Updated SM2Result with new scheduling state.
    """
    if not 0 <= quality <= 5:
        raise ValueError(f"quality must be 0-5, got {quality}")

    now = now or datetime.now(UTC).replace(tzinfo=None)

    if quality >= 3:  # correct
        reps += 1
        if reps == 1:
            interval = 1
        elif reps == 2:
            interval = 6
        else:
            interval = interval * easiness
        easiness = max(
            MIN_EASINESS,
            easiness + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02),
        )
    else:  # failed
        reps = 0
        interval = 1
        # easiness unchanged on failure

    next_review = now + timedelta(days=interval)
    return SM2Result(
        easiness=round(easiness, 4),
        interval=round(interval, 1),
        reps=reps,
        next_review=next_review,
    )
