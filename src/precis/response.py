"""Response value type. Handlers return one; runtime renders to text.

The runtime appends collected hints (from HintBus) and the kind's cost
footer (if any) to produce the final agent-facing string."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Response:
    """What every handler returns from get/search/put/move."""

    body: str
    cost: str | None = None
