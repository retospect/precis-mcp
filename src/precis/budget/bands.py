"""Cost-band affordance — the model's *sense* of expensive.

Two qualitative axes, deliberately word-valued (not dollar figures in the
agent's face) so even a lesser model gets a feel for which lane it's in
without doing arithmetic:

* **cost** — ``free`` · ``cheap`` · ``expensive`` (decision: ``expensive``,
  not ``steep`` — unambiguous to small models).
* **pace** — ``fast`` · ``slow``.

The bands are *information + permission-when-needful*, never prohibition:
prefer the cheapest tier that does the job, but escalate freely when the
cheap tier stalls or the question is high-value. Only the global breaker
(:mod:`precis.budget.breaker`) ever refuses, and only at the catastrophe cap.

A dollar figure maps to a cost band via :func:`cost_from_usd`; a router
:class:`~precis.utils.llm.router.Tier` maps to a full band via
:func:`band_for_tier`. ``expensive``-band work is what the breaker gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from precis.utils.llm.router import Tier


class Cost(StrEnum):
    """Qualitative cost lane. Ordered free < cheap < expensive."""

    FREE = "free"
    CHEAP = "cheap"
    EXPENSIVE = "expensive"


class Pace(StrEnum):
    """Qualitative latency lane."""

    FAST = "fast"
    SLOW = "slow"


@dataclass(frozen=True, slots=True)
class Band:
    """A ``(cost, pace)`` pair with a compact word label."""

    cost: Cost
    pace: Pace

    def label(self) -> str:
        """``'free · fast'`` / ``'expensive · slow'`` — the affordance string."""
        return f"{self.cost.value} \u00b7 {self.pace.value}"


#: The tier → band table. The router's :class:`Tier` already encodes the
#: cost/speed ladder implicitly; this makes it explicit and uniform. A
#: totality assert (below) guarantees every tier has a band, so adding a
#: tier without classifying it is a load-time failure, not a KeyError.
_TIER_BANDS: dict[Tier, Band] = {
    Tier.LOCAL_SMALL: Band(Cost.FREE, Pace.FAST),
    Tier.LOCAL_BIG: Band(Cost.FREE, Pace.SLOW),
    Tier.CLOUD_SMALL: Band(Cost.CHEAP, Pace.FAST),
    Tier.CLOUD_MID: Band(Cost.CHEAP, Pace.SLOW),
    Tier.CLOUD_SUPER: Band(Cost.EXPENSIVE, Pace.SLOW),
}

assert set(_TIER_BANDS) == set(Tier), "budget.bands: tier band table is not total"


#: Upper bound (USD) for the ``cheap`` band. At or below → cheap; above →
#: expensive. A guess until the read-only meter shows the real distribution;
#: tunable via ``PRECIS_BUDGET_CHEAP_MAX_USD``.
DEFAULT_CHEAP_MAX_USD = 0.02


def _cheap_max_usd() -> float:
    """The cheap/expensive threshold.

    A Tier-1 config field (``budget_cheap_max_usd``), so it is read through
    :func:`~precis.config.load_config` — not a raw ``os.environ`` read — per the
    env-var policy (``PRECIS_BUDGET_CHEAP_MAX_USD`` maps to the field via the
    ``PRECIS_`` env prefix). Defaults to :data:`DEFAULT_CHEAP_MAX_USD`."""
    from precis.config import load_config

    return load_config().budget_cheap_max_usd


def cost_from_usd(usd: float | None) -> Cost:
    """Classify a dollar figure into a cost band.

    ``None`` / ``<= 0`` (free providers, cache hits) → ``FREE``; at or below
    the cheap threshold → ``CHEAP``; above it → ``EXPENSIVE``.
    """
    if usd is None or usd <= 0:
        return Cost.FREE
    if usd <= _cheap_max_usd():
        return Cost.CHEAP
    return Cost.EXPENSIVE


def band_for_tier(tier: Tier) -> Band:
    """The ``(cost, pace)`` band for a router tier."""
    return _TIER_BANDS[tier]


def is_expensive(tier: Tier) -> bool:
    """True when ``tier`` rides the ``expensive`` lane — what the breaker gates."""
    return _TIER_BANDS[tier].cost is Cost.EXPENSIVE


__all__ = [
    "DEFAULT_CHEAP_MAX_USD",
    "Band",
    "Cost",
    "Pace",
    "band_for_tier",
    "cost_from_usd",
    "is_expensive",
]
