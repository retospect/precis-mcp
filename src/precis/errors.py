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

There is no general auto-fill: every `next=`/`options=` is hand-written
at the raise site. A couple of narrow dispatcher-boundary hooks
(`runtime.dispatch.DispatchMixin._maybe_add_skill_hint` /
`_maybe_add_schema_drift_hint`) append a specific extra `next:` line for
their one recognized signal (missing skill / schema drift) — they are
not a general backstop, so a raise site with no `next=` still ships a
hint-less error in the common case.

Non-Precis exceptions caught at the dispatcher boundary are wrapped
into `Internal(...)` or `Upstream(...)` with `__cause__` chained.
"""

from __future__ import annotations

from collections.abc import Sequence


class PrecisError(Exception):
    """Base for all precis-raised errors.

    Args:
        cause: Human-readable reason. Always required.
        next: One copy-pasteable next action (the breaking hint). Not
              auto-filled — leave it unset only when there's genuinely
              no recovery action to suggest.
        options: Allowed values for parameter errors. Not auto-filled;
                 set it explicitly for closed vocabularies.
    """

    def __init__(
        self,
        cause: str,
        *,
        next: str | Sequence[str] | None = None,
        options: Sequence[str] | None = None,
    ) -> None:
        self.cause = cause
        # F12: ``next=`` accepts either a single string (legacy
        # one-line hint) or a list of strings (multiple hints, each
        # rendered on its own ``next:`` line by ``runtime.render_error``).
        # Existing single-string callsites keep working unchanged.
        self.next: str | list[str] | None
        if next is None:
            self.next = None
        elif isinstance(next, str):
            self.next = next
        else:
            self.next = list(next)
        self.options = list(options) if options is not None else None
        super().__init__(cause)

    def __str__(self) -> str:
        """``cause`` plus the structured hints, inline.

        ``str(exc)`` in a test must name the refusal's vocabulary without
        reaching for ``exc.options``/``exc.next`` (gr514481, gr514782);
        ``runtime.error``'s ``render_error`` keeps the multi-line
        ``[error:Kind]`` envelope for the wire.
        """
        parts = [self.cause]
        if self.options:
            parts.append("options: " + ", ".join(map(str, self.options)))
        nexts = [self.next] if isinstance(self.next, str) else (self.next or [])
        parts.extend(f"next: {hint}" for hint in nexts)
        return "; ".join(parts)


class NotFound(PrecisError):
    """Identifier or path does not exist."""


class BadInput(PrecisError, ValueError):
    """Parameter invalid or unparseable.

    Also a :class:`ValueError` (gr514202): callers that wrap precis calls
    in ``except ValueError`` — e.g. code that also calls hexfold, which
    raises plain ``ValueError`` — catch this too. ``PrecisError`` stays
    first on the MRO, so ``except PrecisError`` and the dispatcher's
    typed-error path are unchanged.
    """


class Conflict(BadInput):
    """The write collides with live state another ref already owns.

    Distinct from plain :class:`BadInput` (a malformed parameter): the
    call was well-formed but cannot land without stealing something a
    live ref holds — e.g. a ``cite_key`` slug claimed by another ref,
    where the silent outcome would be a slugless orphan (gr517405).
    Subclasses ``BadInput`` so callers catching parameter errors keep
    working, and the envelope renders ``[error:Conflict]``.
    """


class Gone(PrecisError):
    """Ref existed and was soft-deleted.

    Distinct envelope from :class:`NotFound` so the LLM can tell the
    two apart: ``NotFound`` means "try a different id / spelling",
    while ``Gone`` means "the row was here, it was deleted, the MCP
    surface can't resurrect it." The row is still recoverable at the
    SQL layer (``retired_at IS NOT NULL`` filter) but no MCP verb
    undoes the delete.

    Added as a response to MCP critic finding MINOR-C: ``delete
    memory id=3676`` then ``get memory id=3676`` previously returned
    the same ``[error:NotFound]`` as a never-existed id, leaving the
    caller unable to tell whether they hit a typo or a tombstone.
    """


class Unsupported(PrecisError):
    """Mode or view not supported by this kind."""


class Denied(PrecisError):
    """The caller's permission envelope forbids this verb."""


class Upstream(PrecisError):
    """A downstream system (DB, paid tool, network) failed."""


class RateLimited(PrecisError):
    """A provider throttled this caller."""


class Internal(PrecisError):
    """Unhandled server-side bug."""
