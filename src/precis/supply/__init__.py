"""Is this part actually **in stock**? (docs/backlog/
se-off-the-shelf-fabrication.md, "Stock as a selection signal".)

The `component` series registry makes M14×55 exactly as easy to specify as
M4×12, and only one of them is buyable. Reto's rule: *massive bias toward
massively stocked items at selection time*, the way
`precis-part-select-help` biases a PCB to JLCPCB Basic parts before it ever
looks at price.

**Two answers, and they are different kinds of fact.** The curated
`stocking` tier in `component_series.json` is an offline prior — a house
judgement that M4 socket caps are everywhere, good for ranking with no
network and stable for years. A **supplier quote** is a live number for one
SKU at one distributor at one moment. The prior ranks; the quote decides.

**A small adapter port.** Digi-Key Product Information v4, Farnell
Product Search and Mouser Search are self-serve account APIs with partial
hardware coverage. Farnell can identify inventory warehouses; another
supplier's destination locale cannot stand in for a stock origin. Unknown
origin remains unknown. ``StockQuote`` separates region/warehouse/origin
from destination; configurable home-region preference ranks stocked items
before quantity without comparing unlike currencies. Farnell and Mouser
published terms prohibit content storage: their adapters are attributed live
reads without result cache/import. Other distributors remain separate choices.

**No credentials ⇒ say so.** :func:`quote` returns :class:`QuoteResult`
with quotes and per-supplier outcomes; :func:`unavailable_reason` explains
which piece is missing. Failed suppliers retain a safe error beside other
suppliers' quotes: an outage is not evidence of catalogue absence. HTTP
errors expose stage/status, never credentials or arbitrary exception text.
Successful zero results name the exact keyword; known fastener series use
descriptive thread/length/family keywords, still matched by keyword. It never
invents a number and never silently falls back to the tier — the caller
decides what to show, and the part-select rule ("say so rather than
inventing a C-number") applies unchanged to hardware.

Single linked-component MCP reads fetch supplier records live, clearly dated
and attributed as not stored. Stored-only, list/search/BOM views stay offline;
web pages load the live block only on demand. Short request timeouts fail
honestly without mutating metadata. No content cache/memo is enabled.

``supplier-categories`` reads a dated Digi-Key tree; curated
``digikey_series.json`` records category mappings with source URLs. Supplier
IDs remain separate from local component categories. Missing thread/length
filter IDs refuse broadening.

Supplier links persist only identities/URLs and first-linked UTC. API-data
import was refused under the licence: the existing datasheet worker acquires
independent manufacturer evidence and links it to the component; ordinary
values cite that datasheet. Farnell/Mouser remain live-only. TME, RS and Nexar
were surveyed and declined by Reto on 2026-10-05; do not re-propose them.

"""

from __future__ import annotations

from precis.supply.base import (
    Adapter,
    QuoteResult,
    StockQuote,
    SupplierOutcome,
    adapters,
    quote,
    unavailable_reason,
)

__all__ = [
    "Adapter",
    "QuoteResult",
    "StockQuote",
    "SupplierOutcome",
    "adapters",
    "quote",
    "unavailable_reason",
]
