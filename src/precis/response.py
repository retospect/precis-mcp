"""Response value type. Handlers return one; runtime renders to text.

The runtime appends collected hints (from HintBus) and the kind's cost
footer (if any) to produce the final agent-facing string."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResponseImage:
    """A binary payload a :class:`Response` delivers alongside ``body``.

    Currently only the draft figure render check's PNG (gr592765):
    ``view='render'`` was specified to hand the caller the picture, and
    a byte count plus a blob path is a report, not a delivery. The MCP
    tool surface emits each item as an ``ImageContent`` block (the
    per-call sink in :mod:`precis.runtime.dispatch` carries them past
    the text-only ``(body, is_error)`` contract); surfaces that cannot
    carry binary — the CLI, in-process ``runtime.dispatch`` — keep just
    ``body``, which still reports the byte count and blob path.
    """

    data: bytes
    mime: str


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
    #: Concrete targeted-read call(s) offered when the complete rendered
    #: body exceeds the result budget. No partial body or continuation is
    #: returned; this hint is the recovery path.
    result_budget_hint: str | None = None

    #: Selector-bearing outline the budget fitter appends after a
    #: truncated head — one ``<selector>\t<size>`` line per addressable
    #: part (memory ``me<id>~<section>``, skill ``slug~N``), so the
    #: agent can re-read the rest piece by piece. Set only on
    #: open-ended whole-ref renders; part-reads need no map. A thunk is
    #: resolved by the fitter only when a cap actually binds, so the
    #: outline's body load doesn't tax every under-cap get.
    budget_outline: str | Callable[[], str | None] | None = None

    #: Live licensed data is never retained for later continuation.
    transient: bool = False

    #: How many items the body enumerates when the response is a result
    #: set — search hits / list rows shown on this page, the batch size
    #: on a multi-id ``get``, ``1`` for a single-ref read, ``0`` for an
    #: empty result set. ``None`` when the verb doesn't enumerate a
    #: collection (put/edit/delete acks, errors). The dispatch
    #: chokepoint records it into ``tool_calls.result_count`` (the
    #: "reserved" column of migration 0133) so the surface-economy pass
    #: can separate one huge render from a hundred small hits. Set it
    #: where the count is already in hand — never an extra COUNT query
    #: for the ledger's sake.
    result_count: int | None = None

    #: Fit indivisible content units to the supplied UTF-8 byte budget,
    #: or raise ResultBudgetExceeded if none fit. Runtime reserves its
    #: hint/cost suffix first. The callback owns the continuation and
    #: must use the already-loaded content, without another store read.
    budget_fit: Callable[[int], str] | None = None

    #: Binary payloads delivered alongside ``body`` — see
    #: :class:`ResponseImage`. Empty on nearly every response. Kept last
    #: so positional construction is unaffected.
    images: tuple[ResponseImage, ...] = ()

    #: The ref ids the body enumerates — the recall-audit half of
    #: ``result_count`` (``tool_calls.result_ids``, migration 0304):
    #: a search that surfaces memories records WHICH ones, not just how
    #: many. ``None`` where the response doesn't enumerate a result
    #: set. Set it where the ids are already in hand — never an extra
    #: query for the ledger's sake. Last so positional construction is
    #: unaffected.
    result_ids: list[int] | None = None
