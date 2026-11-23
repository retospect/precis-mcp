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
hint-less error in the common case. One exception
(`runtime.error._ARG_SHAPE_GRIPE_HINT`, gr631696): argument-shaped
refusals (`BadInput`/`Unsupported`) get the gripe standing-order line
appended at render time.

Non-Precis exceptions caught at the dispatcher boundary are wrapped
into `Internal(...)` or `Upstream(...)` with `__cause__` chained.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


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


class ResultBudgetExceeded(BadInput):
    """The rendered result body exceeded the byte budget.

    Raised only by :func:`precis._result_budget.fit_result_body`, as its
    last resort when not even a head fits under the cap in force
    (a fitted head+outline result is not an error). Subclasses :class:`BadInput` so callers
    catching parameter errors keep working, and the envelope renders
    ``[error:ResultBudgetExceeded]`` — the ``tool_calls.error_type``
    the sessions roll-up counts as a budget refusal (block-navigation
    slice 2).
    """


class DraftExportRefused(BadInput):
    """The PDF is precis's own draft export, not an ingestable paper (gr634048).

    Subclasses :class:`BadInput` (hence ``ValueError``) so the CLI
    ``precis add`` error path and any ``except ValueError`` callers keep
    catching it; the watcher catches it by name to route the file to
    ``errors/`` without a stack-trace log.
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

    #: ``semantic_unavailable_upstream`` (``utils/embed_query.py``) sets
    #: this on the instance it raises for an explicit ``mode='semantic'``
    #: embed-leg failure: dispatch retries that search once as
    #: ``mode='hybrid'`` — degraded and disclosed — instead of surfacing
    #: the refusal (gr714744). Class-level ``False`` keeps every other
    #: Upstream out of the retry.
    embedder_semantic_down: bool = False


class RateLimited(PrecisError):
    """A provider throttled this caller."""


class Internal(PrecisError):
    """Unhandled server-side bug."""


# ── corrected-call helpers (gr613469) ────────────────────────────────────
#
# A refusal's ``next=`` should name the one call that would have worked —
# the caller's own call with the bad key/view swapped for the nearest
# accepted one — not just the vocabulary. These two helpers build it;
# ``tool_ledger.next_call_summary`` reads the same ``verb(`` shape back
# out of rendered bodies to measure hint uptake.


def nearest_option(name: str, choices: Iterable[str]) -> str | None:
    """difflib's closest of ``name`` in ``choices``, else ``None``."""
    near = difflib.get_close_matches(name, [str(c) for c in choices], n=1)
    return near[0] if near else None


def render_corrected_call(
    verb: str,
    args: Mapping[str, Any],
    *,
    extras: Mapping[str, Any] | None = None,
    top_level: Iterable[str] | None = None,
) -> str:
    """``verb(k=v, …, args={…})`` — the caller's call re-rendered.

    Non-``None`` args render ``key=value`` (repr values, so strings get
    quotes and the call stays copy-pasteable — mcp-authoring's "next= is
    a call that works"). ``__``-prefixed dispatch-internal keys never
    render. ``top_level`` narrows which keys render ``key=value``: keys
    outside it fold into a trailing ``args={…}`` dict, merged with
    ``extras`` — the slim-schema spelling (only the verb's
    ``CORE_PARAMS`` go top-level; ``runtime.search._search_call_text``'s
    rule). ``top_level=None`` renders every key top-level — right for
    the full-signature verbs (``get``/``link``/…) and for extras the
    caller already nested under ``args=``. A mapping-valued ``args``
    key always merges into the one trailing ``args={…}`` dict — two
    ``args=`` kwargs would not be a runnable call.
    """
    top = set(top_level) if top_level is not None else None
    parts: list[str] = []
    nested: dict[str, Any] = {}
    for k, v in args.items():
        ks = str(k)
        if ks.startswith("__") or v is None:
            continue
        if ks == "args" and isinstance(v, Mapping):
            # The caller's own ``args={…}`` dict merges into the ONE
            # trailing ``args={…}`` with the folded keys — emitting a
            # second ``args=`` kwarg would not be a runnable call.
            nested.update(v)
        elif top is None or ks in top:
            parts.append(f"{ks}={v!r}")
        else:
            nested[ks] = v
    if extras:
        for k, v in extras.items():
            nested[str(k)] = v
    if nested:
        parts.append(f"args={nested!r}")
    return f"{verb}({', '.join(parts)})"
