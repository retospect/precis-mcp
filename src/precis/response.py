"""Response value type. Handlers return one; runtime renders to text.

The runtime appends collected hints (from HintBus) and the kind's cost
footer (if any) to produce the final agent-facing string."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Response:
    """What every handler returns from get/search/put/move.

    ``ref_id`` / ``reused`` are structured metadata for **in-process**
    callers (a handler calling a sibling handler via ``Hub.sibling``) —
    they must read these fields instead of regex-parsing ``body``.
    ``body`` is agent-facing prose, not an API: a copy-edit to the ack
    wording must never silently break a caller that scraped an id or
    an idempotency verdict out of it.
    """

    body: str
    cost: str | None = None
    #: The ref this verb created or resolved, when the handler knows
    #: it. ``None`` when not applicable (e.g. a search response).
    ref_id: int | None = None
    #: ``True`` when an idempotency hit returned an existing ref
    #: instead of creating one; ``False`` on a fresh create;
    #: ``None`` when the verb has no idempotency concept.
    reused: bool | None = None
