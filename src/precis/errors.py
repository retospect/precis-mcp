"""Exception hierarchy for precis.

The base class carries the one breaking hint (`next=`) — the single
copy-pasteable next action the agent should take to recover. Distinct
from ambient hints, which are deduplicated tips collected via the
HintBus and rendered post-success.

Usage:
    raise BadInput(
        "missing kind=",
        next="add kind=<one of: calc, paper, todo>",
    )

`ErrorModel.enrich()` (in `precis.runtime`) auto-fills `next` and
`options` at the dispatcher boundary when the raise site doesn't.

Non-Precis exceptions caught at the dispatcher boundary are wrapped
into `Internal(...)` or `Upstream(...)` with `__cause__` chained.
"""

from __future__ import annotations

from collections.abc import Sequence


class PrecisError(Exception):
    """Base for all precis-raised errors.

    Args:
        cause: Human-readable reason. Always required.
        next: One copy-pasteable next action (the breaking hint).
              Auto-filled by `ErrorModel.enrich()` if None.
        options: Allowed values for parameter errors. Auto-filled
                 for closed vocabularies.
    """

    def __init__(
        self,
        cause: str,
        *,
        next: str | None = None,
        options: Sequence[str] | None = None,
    ) -> None:
        self.cause = cause
        self.next = next
        self.options = list(options) if options is not None else None
        super().__init__(cause)


class NotFound(PrecisError):
    """Identifier or path does not exist."""


class BadInput(PrecisError):
    """Parameter invalid or unparseable."""


class Unsupported(PrecisError):
    """Mode or view not supported by this kind."""


class Upstream(PrecisError):
    """A downstream system (DB, paid tool, network) failed."""


class RateLimited(PrecisError):
    """A provider throttled this caller."""


class Internal(PrecisError):
    """Unhandled server-side bug."""
