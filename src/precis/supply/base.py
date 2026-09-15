"""The stock port: one quote shape, one adapter protocol, one registry.

Kept deliberately small. An adapter's whole job is *designation in, quote
out* — it does not mint components, does not write, and does not decide
what a design should use. That separation is what lets a second supplier
land as one file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StockQuote:
    """One supplier's answer about one part, at one moment.

    ``quantity`` is units on hand — the number the whole exercise is for.
    ``unit_price`` is in ``currency`` at the smallest break, or ``None``
    when the supplier gives none. ``retrieved`` is when we asked, because
    a stock figure without a timestamp reads as a property of the part
    rather than a snapshot of a warehouse.

    ``match_confidence`` is the honest half: a fastener is looked up by
    keyword ("ISO 4762 M4x12"), not by an exact part number we own, so a
    hit may be the wrong grade, the wrong finish or a 100-pack. Never
    present a quote without it.
    """

    supplier: str
    sku: str
    description: str
    quantity: int
    unit_price: float | None
    currency: str
    url: str | None
    retrieved: datetime
    match_confidence: str = "keyword"

    @property
    def in_stock(self) -> bool:
        return self.quantity > 0

    def line(self) -> str:
        """One display line — supplier, SKU, quantity, price, and the
        caveat, in that order."""
        price = (
            f"{self.unit_price:.4g} {self.currency}"
            if self.unit_price is not None
            else "no price"
        )
        return (
            f"{self.supplier} {self.sku}: {self.quantity} in stock · {price} · "
            f"{self.description} (matched by {self.match_confidence}, "
            f"{self.retrieved:%Y-%m-%d %H:%M} UTC)"
        )


@runtime_checkable
class Adapter(Protocol):
    """A supplier that can be asked about a standards designation."""

    name: str

    def configured(self) -> str | None:
        """``None`` when ready; otherwise the sentence naming what is
        missing (which env var, which account). The caller shows it — an
        adapter that is merely absent from the results looks like a part
        nobody stocks."""

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        """Quotes for a designation like ``'ISO 4762 M4x12'``, best first.
        Network errors raise; an empty list means *asked and nobody has
        one*, which is itself the answer."""


def adapters() -> list[Adapter]:
    """Every adapter, configured or not. Import is local so a missing
    optional dependency narrows the list instead of breaking the import
    of anything that merely mentions stock."""
    out: list[Adapter] = []
    try:
        from precis.supply.digikey import DigiKeyAdapter

        out.append(DigiKeyAdapter())
    except ImportError:  # pragma: no cover — httpx is a core dep today
        pass
    return out


def unavailable_reason() -> str | None:
    """Why no live quote is possible right now, or ``None`` when at least
    one adapter is ready."""
    reasons = []
    for adapter in adapters():
        why = adapter.configured()
        if why is None:
            return None
        reasons.append(f"{adapter.name}: {why}")
    if not reasons:
        return "no supplier adapters are installed"
    return "; ".join(reasons)


def quote(designation: str, *, limit: int = 5) -> list[StockQuote]:
    """Ask every configured supplier, best-stocked first.

    A supplier that errors is skipped rather than allowed to sink the
    whole answer — one distributor being down is not a reason to refuse to
    report the others. An empty result with no
    :func:`unavailable_reason` means the part really is unlisted.
    """
    out: list[StockQuote] = []
    for adapter in adapters():
        if adapter.configured() is not None:
            continue
        try:
            out.extend(adapter.search(designation, limit=limit))
        except Exception:  # one supplier's outage is not the caller's
            continue
    out.sort(key=lambda q: (-q.quantity, q.unit_price or float("inf")))
    return out[:limit]


def now() -> datetime:
    return datetime.now(UTC)
