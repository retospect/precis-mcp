"""The stock port: one quote shape, one adapter protocol, one registry.

Kept deliberately small. An adapter's whole job is *designation in, quote
out* — it does not mint components, does not write, and does not decide
what a design should use. That separation is what lets a second supplier
land as one file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from precis.supply.catalog import FilterUnavailable

log = logging.getLogger(__name__)


class SupplierError(RuntimeError):
    """Safe adapter diagnostic; never includes request or credential data."""

    def __init__(self, stage: str, status: int) -> None:
        self.stage = stage
        self.status = status
        super().__init__(f"HTTP {status} at {stage}")


@dataclass(frozen=True)
class SupplierOutcome:
    """One supplier's successful quote count or safe error, for a keyword."""

    supplier: str
    keyword: str
    count: int | None = None
    error: str | None = None
    category_id: int | None = None
    parameters: dict[str, list[str]] | None = None

    def line(self) -> str:
        if self.error is not None:
            return f"{self.supplier}: {self.error} for keyword {self.keyword!r}"
        count = (
            "0 products with stock data" if self.count == 0 else f"{self.count} quotes"
        )
        filters = (
            f" · category {self.category_id}, parameters={self.parameters or {}}"
            if self.category_id
            else ""
        )
        return f"{self.supplier}: {count} for keyword {self.keyword!r}{filters}"


@dataclass(frozen=True)
class QuoteResult:
    quotes: list[StockQuote]
    outcomes: list[SupplierOutcome]


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
    region: str | None = None
    warehouse: str | None = None
    ships_from: str | None = None
    ships_to: str | None = None

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
        location = self.region or "origin unknown"
        if self.warehouse:
            location += f", warehouse {self.warehouse}"
        if self.ships_from and self.ships_from != self.region:
            location += f", ships from {self.ships_from}"
        if self.ships_to:
            location += f", destination {self.ships_to}"
        return (
            f"{self.supplier} ({location}) {self.sku}: {self.quantity} in stock · {price} · "
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
        Network errors raise; an empty list means this keyword returned no
        products with stock data, never that nobody stocks the part."""


def adapters() -> list[Adapter]:
    """Every adapter, configured or not. Import is local so a missing
    optional dependency narrows the list instead of breaking the import
    of anything that merely mentions stock."""
    out: list[Adapter] = []
    try:
        from precis.supply.digikey import DigiKeyAdapter
        from precis.supply.farnell import FarnellAdapter
        from precis.supply.mouser import MouserAdapter

        out.extend([DigiKeyAdapter(), FarnellAdapter(), MouserAdapter()])
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


def quote(
    designation: str,
    *,
    limit: int = 5,
    category_id: int | None = None,
    parameters: dict[str, list[str]] | None = None,
    criteria: dict[str, Any] | None = None,
) -> QuoteResult:
    """Ask every configured supplier, best-stocked first.

    Preserve each supplier's outcome beside successful quotes: an outage
    cannot establish catalogue absence, and must not hide other suppliers.
    Exception messages may contain request credentials; only typed adapter
    errors or exception class names are returned/logged.
    """
    out: list[StockQuote] = []
    outcomes: list[SupplierOutcome] = []
    for adapter in adapters():
        why = adapter.configured()
        if why is not None:
            outcomes.append(SupplierOutcome(adapter.name, designation, error=why))
            continue
        try:
            selected = parameters
            if category_id is not None:
                from precis.supply.digikey import DigiKeyAdapter

                if not isinstance(adapter, DigiKeyAdapter) and not criteria:
                    outcomes.append(
                        SupplierOutcome(
                            adapter.name,
                            designation,
                            error="Digi-Key filters unsupported by this supplier",
                        )
                    )
                    continue
                if not isinstance(adapter, DigiKeyAdapter):
                    # Curated automatic Digi-Key criteria do not exclude other
                    # suppliers; these quotes remain explicitly keyword matches.
                    found = adapter.search(designation, limit=limit)
                    out.extend(found)
                    outcomes.append(
                        SupplierOutcome(adapter.name, designation, count=len(found))
                    )
                    continue
                if selected is None and criteria:
                    selected = adapter.fastener_parameters(category_id, **criteria)
                found = adapter.search(
                    designation,
                    limit=limit,
                    category_id=category_id,
                    parameters=selected,
                )
            else:
                found = adapter.search(designation, limit=limit)
            out.extend(found)
            outcomes.append(
                SupplierOutcome(
                    adapter.name,
                    designation,
                    count=len(found),
                    category_id=category_id,
                    parameters=selected,
                )
            )
        except Exception as exc:  # retain partial success, without raw errors
            error = (
                str(exc)
                if isinstance(exc, (SupplierError, FilterUnavailable))
                else type(exc).__name__
            )
            outcomes.append(SupplierOutcome(adapter.name, designation, error=error))
            log.warning("supplier %s: %s", adapter.name, error)
    preference = [
        part.strip().upper().replace("UK", "GB")
        for part in os.environ.get(
            "PRECIS_SUPPLY_REGION_PREFERENCE", "IE,GB,DE,EU"
        ).split(",")
        if part.strip()
    ]

    def rank(item: StockQuote) -> tuple[bool, int, int]:
        region = (item.region or "").upper().replace("UK", "GB")
        home = preference.index(region) if region in preference else len(preference)
        return not item.in_stock, home, -item.quantity

    out.sort(key=rank)
    return QuoteResult(out[:limit], outcomes)


def now() -> datetime:
    return datetime.now(UTC)
