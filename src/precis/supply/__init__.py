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

**An adapter, not an integration.** Surveyed 2026-09-15: no fastener
distributor publishes an open stock API. Digi-Key's Product Information
v4 is the one free self-serve key (register a developer app on a My
DigiKey account) and it carries small metric hardware — machine screws,
standoffs, inserts — thinning out above M5. JLCMC, the mechanical arm of
the JLCPCB group, has the catalogue we want but gates API access behind an
application. Würth/Fabory/RS/Misumi are commercial arrangements. So this
package is a *port*: one :class:`StockQuote`, one adapter protocol, and as
many adapters as end up being reachable.

**No credentials ⇒ say so.** :func:`quote` returns ``None`` and
:func:`unavailable_reason` explains which piece is missing. It never
invents a number and never silently falls back to the tier — the caller
decides what to show, and the part-select rule ("say so rather than
inventing a C-number") applies unchanged to hardware.

Nothing here is on a read path by default: a quote costs a network round
trip, so it happens when someone asks for it (``view='stock'``), not while
rendering a design.
"""

from __future__ import annotations

from precis.supply.base import (
    Adapter,
    StockQuote,
    adapters,
    quote,
    unavailable_reason,
)

__all__ = [
    "Adapter",
    "StockQuote",
    "adapters",
    "quote",
    "unavailable_reason",
]
