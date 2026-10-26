"""The stock port: one quote shape, one adapter protocol, one registry.

Kept deliberately small. An adapter's whole job is *designation in, quote
out* — it does not mint components, does not write, and does not decide
what a design should use. That separation is what lets a second supplier
land as one file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from precis.supply.catalog import FilterUnavailable

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


class SupplierError(RuntimeError):
    """Safe adapter diagnostic; never includes request or credential data."""

    def __init__(
        self, stage: str, status: int | None, detail: str = "", *, reason: str = ""
    ) -> None:
        self.stage = stage
        #: ``None`` when there was no HTTP answer (transport error, breaker
        #: open); ``reason`` then carries the message.
        self.status = status
        self.detail = detail
        super().__init__(reason or f"HTTP {status} at {stage}")


class LookupUnsupported(SupplierError):
    """The vendor has no exact part-number endpoint wired.

    Deliberately distinct from a miss (the vendor answered and the SKU is
    absent — ``lookup`` returns ``None``, which a caller may
    negative-cache) and from a transport error: this means "ask
    differently", not "try again". It is a ``SupplierError`` — a caller
    distinguishing it catches ``LookupUnsupported`` BEFORE
    ``SupplierError`` in its ``except`` chain.
    """

    def __init__(self, reason: str) -> None:
        super().__init__("lookup", None, reason=reason)


@dataclass(frozen=True)
class SupplierOutcome:
    """One supplier's successful quote count or safe error, for a keyword."""

    supplier: str
    keyword: str
    count: int | None = None
    error: str | None = None
    category_id: int | None = None
    parameters: dict[str, list[str]] | None = None
    detail: str = ""

    def line(self) -> str:
        filters = (
            f" · category {self.category_id}, parameters={self.parameters or {}}"
            if self.category_id
            else ""
        )
        if self.error is not None:
            tail = f" — {self.detail}" if self.detail else ""
            return (
                f"{self.supplier}: {self.error} for keyword "
                f"{self.keyword!r}{filters}{tail}"
            )
        count = (
            "0 products with stock data" if self.count == 0 else f"{self.count} quotes"
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


@dataclass(frozen=True)
class PartRecord:
    """One vendor's exact answer about one part number.

    Unlike :class:`StockQuote` (a keyword search's best guess, with the
    match-confidence caveat), a record answers an exact SKU: ``supplier``
    and ``sku`` say who and which part number. The identity fields
    (``manufacturer`` … ``datasheet_url``, ``lifecycle``) may persist for
    any vendor; ``stock``/``price_breaks`` may persist only where
    ``Adapter.may_store`` — the licence ruling (Reto 2026-10-26, option
    3): JLCPCB may be stored, the rest are attributed live reads.

    ``price_breaks`` are ``(min_qty, unit_price)`` pairs ascending.
    ``extra`` holds vendor-only flags (JLCPCB's ``basic``) that don't
    deserve a column on every vendor's shape. ``retrieved`` is when we
    asked — a stock figure without a timestamp reads as a property of
    the part rather than a snapshot of a warehouse.
    """

    supplier: str
    sku: str
    manufacturer: str | None = None
    mpn: str | None = None
    description: str = ""
    package: str | None = None
    datasheet_url: str | None = None
    lifecycle: str | None = None
    stock: int | None = None
    price_breaks: tuple[tuple[int, float], ...] = ()
    currency: str | None = None
    retrieved: datetime = field(default_factory=lambda: datetime.now(UTC))
    extra: dict[str, Any] = field(default_factory=dict)

    def unit_price_at(self, quantity: int) -> float | None:
        """The unit price at ``quantity``: the largest break at or below
        it (the ``price@qty`` pull), or the first break's price when the
        quantity sits below every break — the lowest rung of a vendor's
        ladder is the only price it offers. ``None`` when there are no
        prices."""
        if not self.price_breaks:
            return None
        chosen = self.price_breaks[0][1]
        for qty, price in self.price_breaks:
            if qty > quantity:
                break
            chosen = price
        return chosen


@runtime_checkable
class Adapter(Protocol):
    """A supplier that can be asked about a standards designation."""

    name: str
    #: Per-vendor store policy (the licence ruling): ``True`` only where
    #: stock and price may be persisted — JLCPCB. ``False`` vendors are
    #: attributed live reads: fetched for a view or an export, never
    #: written. Identity fields may persist regardless.
    may_store: bool

    def configured(self) -> str | None:
        """``None`` when ready; otherwise the sentence naming what is
        missing (which env var, which account). The caller shows it — an
        adapter that is merely absent from the results looks like a part
        nobody stocks."""

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        """Quotes for a designation like ``'ISO 4762 M4x12'``, best first.
        Network errors raise; an empty list means this keyword returned no
        products with stock data, never that nobody stocks the part."""

    def lookup(self, sku: str) -> PartRecord | None:
        """An exact part number → its :class:`PartRecord`; ``None`` on a
        real miss (the vendor answered and this SKU is not in its
        catalogue). A vendor with no exact endpoint raises
        :class:`LookupUnsupported` — never ``None``, which a caller
        might cache as absence."""


def _vendor_adapters(store: Store | None) -> list[Adapter]:
    """One instance of every adapter. Import is local so a missing
    optional dependency narrows the list instead of breaking the import
    of anything that merely mentions stock."""
    from precis.supply.digikey import DigiKeyAdapter
    from precis.supply.farnell import FarnellAdapter
    from precis.supply.jlcpcb import JlcPcbAdapter
    from precis.supply.mouser import MouserAdapter

    return [
        DigiKeyAdapter(),
        FarnellAdapter(),
        MouserAdapter(),
        JlcPcbAdapter(store=store),
    ]


def adapters(*, store: Store | None = None) -> list[Adapter]:
    """Every adapter, configured or not.

    ``store`` is handed to adapters whose exact lookup persists rows —
    JLCPCB's ``live_lookup`` upserts into ``parts``, so it joins the list
    only when a caller (the table-refresh path) supplies one. Without it
    the adapter stays out: keyword aggregation is not its surface, and a
    store-less instance could only report "no store" on every search.
    :func:`vendor` resolves it by name when naming that gap is the point.
    """
    out: list[Adapter] = []
    try:
        out = _vendor_adapters(store)
    except ImportError:  # pragma: no cover — httpx is a core dep today
        pass
    if store is None:
        out = [a for a in out if not getattr(a, "needs_store", False)]
    return out


#: What a user types for a vendor → the adapter's ``name`` (compared
#: casefolded). A new vendor is one adapter class plus the alias lines.
VENDOR_ALIASES: dict[str, str] = {
    "digi-key": "digikey",
    "digikey": "digikey",
    "digikey.com": "digikey",
    "element14": "farnell",
    "farnell": "farnell",
    "farnell.com": "farnell",
    "newark": "farnell",
    "jlc": "jlcpcb",
    "jlcpcb": "jlcpcb",
    "jlcpcb.com": "jlcpcb",
    "lcsc": "jlcpcb",
    "lcsc.com": "jlcpcb",
    "mouser": "mouser",
    "mouser.com": "mouser",
}


def vendor(name: str, *, store: Store | None = None) -> Adapter | None:
    """Resolve what a user typed for a vendor — ``'LCSC.com'``,
    ``'lcsc'``, ``'JLCPCB'`` all reach the JLCPCB adapter — or ``None``
    for a name the alias map doesn't know. ``store`` reaches lookup
    adapters that persist rows; a store-less JLCPCB still resolves so
    ``configured()`` can name the gap."""
    canonical = VENDOR_ALIASES.get(name.strip().casefold())
    if canonical is None:
        return None
    for adapter in _vendor_adapters(store):
        if adapter.name == canonical:
            return adapter
    return None


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


def _digikey_ladder(
    adapter: Any,
    designation: str,
    limit: int,
    category_id: int,
    selected: dict[str, list[str]] | None,
    ladder: dict[str, Any],
) -> tuple[list[StockQuote], dict[str, list[str]] | None]:
    """Digi-Key fastener rungs; returns ``(quotes, parameters actually used)``.

    1. parametric (``ParameterFilterRequest`` only) + family words — Digi-Key
       refuses it with a 400 on a parent category, which falls through;
    2. keyword ``designation`` ("<family> M4X12") inside the category,
       keeping hits whose description has a ``match_words`` head/family
       marker (a wrong head form is not the match);
    3. keyword "<family> M4" keeping descriptions with the size token and
       the marker, else the hits labelled as near matches.
    """
    if selected:
        try:
            found = adapter.search(
                ladder["family"],
                limit=limit,
                category_id=category_id,
                parameters=selected,
            )
        except SupplierError as exc:
            if exc.status != 400:
                raise
        else:
            if found:
                return found, selected
    words = [w.casefold() for w in ladder.get("match_words") or []]

    def has_marker(q: StockQuote) -> bool:
        return not words or any(w in q.description.casefold() for w in words)

    found = adapter.search(designation, limit=limit, category_id=category_id)
    marked = [q for q in found if has_marker(q)]
    if marked:
        return marked, None
    wide = adapter.search(
        f"{ladder['family']} {ladder['thread']}", limit=50, category_id=category_id
    )
    needle = ladder["size"].casefold()
    exact = [q for q in wide if needle in q.description.casefold() and has_marker(q)]
    if exact:
        return exact[:limit], None

    def near(q: StockQuote) -> StockQuote:
        # Wrong head/family reads as the match otherwise; say which part failed.
        what = ladder["size"] if has_marker(q) else ladder["family"]
        return replace(q, description=f"[near match, not {what}] {q.description}")

    return [near(q) for q in (wide or found)[:limit]], None


def quote(
    designation: str,
    *,
    limit: int = 5,
    category_id: int | None = None,
    parameters: dict[str, list[str]] | None = None,
    criteria: dict[str, Any] | None = None,
    ladder: dict[str, Any] | None = None,
) -> QuoteResult:
    """Ask every configured supplier, best-stocked first.

    Preserve each supplier's outcome beside successful quotes: an outage
    cannot establish catalogue absence, and must not hide other suppliers.
    Exception messages may contain request credentials; only typed adapter
    errors or exception class names are returned/logged.

    ``ladder`` (``family``/``thread``/``size``) turns the Digi-Key lookup
    into parametric → keyword → near-match rungs; see :func:`_digikey_ladder`.
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
                    try:
                        selected = adapter.fastener_parameters(category_id, **criteria)
                    except FilterUnavailable:
                        if not ladder:
                            raise
                if ladder:
                    found, selected = _digikey_ladder(
                        adapter, designation, limit, category_id, selected, ladder
                    )
                else:
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
            outcomes.append(
                SupplierOutcome(
                    adapter.name,
                    designation,
                    error=error,
                    category_id=category_id,
                    parameters=selected,
                    detail=exc.detail if isinstance(exc, SupplierError) else "",
                )
            )
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
