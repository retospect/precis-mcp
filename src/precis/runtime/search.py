"""Cross-kind fan-out + the source-search primitive.

``SearchMixin`` carries ``search(kind='*')`` / comma-list fan-out (RRF
merge across handlers' ``search_hits``), the tags-only cross-kind sweep,
``folder=`` subtree scoping, the post-fan-out tag backstop filter, the
``sort=``/``since=``/``until=`` source-search primitive, and the
``view='stubs'`` backlog view and its ``view='chase-queue'`` DOI-only,
never-tried-first sibling. The angle spray and dreamable region live
in :mod:`precis.runtime.angle` — those also fan out across kinds but pick
their own seed rather than ranking a ``q=`` query.

``kind='source'`` (unrelated naming collision with the paragraph above,
despite both saying "source"): a fixed cross-kind alias — exactly
``paper`` + ``finding`` — not a new search shape. It rides the same
``_resolve_cross_kind_request`` expansion the wildcard and comma-list
forms use, so it composes with every existing entry point (``folder=``,
``view='keywords'``, the ``sort=``/``since=``/``until=`` primitive above,
and the plain fan-out) for free. See
:data:`~precis.runtime._shared.CROSS_KIND_SOURCE_ALIAS`.

Domain fence (gr572720): wherever the *default* expansion runs — an
omitted ``kind=`` or a wildcard alias, on the fan-out, the tags-only
sweep and the source-search primitive alike — the concrete kind list
intersects the resolved domain's ``kinds`` (``runtime.scope``), the
footer names the skipped count and the door. An explicit ``kind=`` or
comma list outside the domain still runs; ``'all'`` is byte-identical
to the unfenced fan-out.

Fan-out isolation (gr664954): the wildcard fan-out shares one call
deadline across ~34 kinds on a capped worker pool, so a slow kind must
not starve the queue — every pool checkout a worker makes is bounded
by :data:`~precis.store.pool.CHECKOUT_BUDGET` (the wait itself and,
server-side, every statement, not just the ANN leg), and a kind
dequeued inside the min-start floor still gets a lexical-only attempt
rather than a bare TimeoutError. Degraded kinds are named in a footer;
errored ones in the errored tip.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from precis.errors import (
    BadInput,
    Internal,
    NotFound,
    ResultBudgetExceeded,
    Unsupported,
    Upstream,
)
from precis.protocol import Handler
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_ALIASES as _CROSS_KIND_ALIASES
from precis.runtime._shared import CROSS_KIND_SOURCE_ALIAS as _CROSS_KIND_SOURCE_ALIAS
from precis.runtime._shared import CROSS_KIND_SOURCE_KINDS as _CROSS_KIND_SOURCE_KINDS
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import (
    RESOLVED_FACETS_KEY,
    SOURCE_SEARCH_TRIGGER_LABEL,
    RuntimeShape,
    refuse_unapplied_args,
)
from precis.runtime._shared import (
    UNCITED_UNSUPPORTED_KINDS as _UNCITED_UNSUPPORTED_KINDS,
)
from precis.runtime.scope import DOMAINS, Domain, current_domain
from precis.store._chunks_ops import ANN_STATEMENT_TIMEOUT_MS
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.store.types import Tag
from precis.utils import handle_registry, search_trace
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    flag_title_matches,
    merge_and_render,
    ref_hits_to_search_hits,
)

log = logging.getLogger(__name__)

#: Payload keys :meth:`SearchMixin._dispatch_cross_kind` actually reads.
#: Anything else the caller supplied silently did nothing on the fan-out
#: — the per-kind kwargs gate (gr334695) never runs for a fan-out — so
#: :func:`refuse_unapplied_args` refuses it up front (gr524663).
#: ``view`` joins the set only when it is ``'keywords'`` (the one view the
#: fan-out renders); every other ``view=`` is refused.
#: ``domain`` joins the set for gr572720: ``args={'domain': '<name>'}``
#: is the per-call override the skipped-kinds footer names — the fence
#: resolves it against the DOMAINS registry instead of the connection's
#: bound domain.
_FAN_OUT_CONSUMED: frozenset[str] = frozenset(
    {
        "q",
        "tags",
        "exclude",
        "mode",
        "folder",
        "page_size",
        "top_k",
        "exclude_ref_ids",
        "include_ref_ids",
        "domain",
    }
)

#: Same for :meth:`SearchMixin._dispatch_cross_kind_tags_only` — much
#: narrower than the fan-out's: the sweep is a flat ``list_refs`` tag
#: query, so the fan-out's ranking knobs (``mode``, ``exclude``, the
#: ref-id facets, ``view``) are all unapplied on it. ``q`` stays in the
#: set only as the shape discriminator — a non-blank ``q=`` takes the
#: real fan-out instead; a blank one is not a filter. ``n``/``top_k``
#: are read as the page cap, matching angle/dreamable.
_TAGS_ONLY_CONSUMED: frozenset[str] = frozenset(
    {"q", "tags", "folder", "page_size", "top_k", "n", "domain"}
)

#: Same for :meth:`SearchMixin._dispatch_source_search`.
_SOURCE_SEARCH_CONSUMED: frozenset[str] = frozenset(
    {
        "q",
        "sort",
        "since",
        "until",
        "tags",
        "status",
        "mode",
        "page",
        "offset",
        "page_size",
        "top_k",
        "exclude",
        "queries",
        "answers",
        "serves",
        "exclude_ref_ids",
        "include_ref_ids",
        "domain",
    }
)

#: No-op spellings the source search accepts without applying: the
#: paper-search ``view='index'``/``'ids'`` aliases for the default ranked
#: render, and ``per_paper=1`` — exactly the one-chunk-per-ref collapse
#: the primitive already applies. Any other value is refused by
#: :func:`refuse_unapplied_args` (a view the path can't render, or a cap
#: that would break its per-ref shape).
_SOURCE_SEARCH_TOLERATED: dict[str, tuple[Any, ...]] = {
    "view": ("index", "ids"),
    "per_paper": (1,),
}

#: Same for :meth:`SearchMixin._dispatch_search_plan`. The plan accepts
#: the real search's filters — echoed in the output and used to name the
#: dispatch path the same call would take; it no longer refuses them.
_PLAN_CONSUMED: frozenset[str] = frozenset(
    {
        "view",
        "mode",
        "q",
        "page_size",
        "top_k",
        "n",
        "tags",
        "status",
        "link",
        "folder",
        "exclude",
        "sort",
        "since",
        "until",
        "under",
        "depth",
        "axis",
        "queries",
        "answers",
        "exclude_ref_ids",
        "include_ref_ids",
    }
)

#: The subset of :data:`_PLAN_CONSUMED` echoed as ``filters:`` in the plan
#: output — everything the caller supplied that is a filter rather than a
#: size/display knob (mode/q already ride their own output line).
_PLAN_FILTER_KEYS: frozenset[str] = _PLAN_CONSUMED - {
    "view",
    "mode",
    "q",
    "page_size",
    "top_k",
    "n",
}

#: Same for :meth:`SearchMixin._dispatch_stubs` /
#: :meth:`SearchMixin._dispatch_chase_queue`.
_STUBS_CONSUMED: frozenset[str] = frozenset({"view", "n", "page_size"})

#: The filters ``view='stubs'`` applies on top of the backlog
#: (gr608580): ``q=`` narrows to stubs whose title / cite_key /
#: identifier match (a stub has no body chunks to rank — ``q=`` is a
#: metadata match, not a search); ``since=`` / ``until=`` bound
#: ``refs.created_at``; the ref-id channels carry the dispatcher's
#: ``uncited=`` / ``cited=`` / ``hubbed=`` resolutions (the facets
#: themselves are popped before this view runs). ``view='chase-queue'``
#: keeps the narrow base — a tighter slice is not a filtered browse.
_STUBS_VIEW_CONSUMED: frozenset[str] = _STUBS_CONSUMED | {
    "q",
    "since",
    "until",
    "include_ref_ids",
    "exclude_ref_ids",
}

#: ``page=1`` is the verb wrapper's always-present default — the value
#: below is what flags it as caller intent; a real ``page=N`` is refused
#: on these paths too (search returns a bounded top-N set, not pages).
_PAGE_TOLERATED: dict[str, tuple[Any, ...]] = {"page": (1,)}

#: Cross-kind fan-out bounds (gr495190, gr513663). Workers stay under the DB
#: pool (default 10) so a wildcard search can't starve other requests. 2
#: workers could not drain ~34 kinds inside the deadline — any per-kind
#: latency over ~1.7 s left most of the queue to TimeoutError (gr513663,
#: the 2026-10-24 fan-out collapse). Raised to 4 now that the server-side
#: cancel (ANN_STATEMENT_TIMEOUT_MS) has a shipped regression test.
CROSS_KIND_MAX_WORKERS = 4
#: A kind dequeued with less budget than this (or <10% of the whole
#: deadline, whichever is smaller) cannot finish the full hybrid legs —
#: it falls to the lexical floor below rather than reporting the
#: timeout outright (gr664954).
CROSS_KIND_MIN_START_MS = 1_500
#: Smaller floor for a lexical-only attempt (gr664954). A kind dequeued
#: inside ``CROSS_KIND_MIN_START_MS`` still answers a lexical leg in far
#: less budget — the 2026-11-13 collapse starved cheap kinds (skill,
#: todo) to TimeoutError purely on queue position while early kinds
#: held the workers, and the merge lost hits a small retry slice would
#: have found. Applies on the default expansion only, and only when
#: the call's mode is the default/``'hybrid'``: an explicit kind list
#: and any explicit ``mode=`` (``'semantic'``, or the exact-term
#: ``'verbatim'``/``'literal'`` a lexical rewrite would silently
#: broaden) are caller intent and keep the timeout.
CROSS_KIND_LEXICAL_MIN_START_MS = 500
#: Kinds whose source-search hits are addressed by the parent ref handle (gr),
#: not the matching comment chunk (gc) — the status-browse kinds (gr495382).
_PARENT_HANDLE_KINDS = frozenset({"gripe", "todo", "quest"})
#: Wall-clock budget for the ANN route probe plus the whole fan-out,
#: not per kind. Sized so the whole ``search/-`` call stays inside the
#: 15 s tool-call latency budget (``health_checks.TOOL_LATENCY_BUDGET_MS``)
#: with headroom for the once-per-call query embed and the merge/render
#: tail — the old 30 s deadline could never fit it (gr628278: search/-
#: p50 measured 17.5 s on 2026-11-10, kinds still cancelling past the
#: budget). td649559: the probe draws from this same deadline — its
#: pool checkout and gate read used to run before the fan-out's clock
#: started, so pool contention or a stalled probe could spend the
#: budget before the first kind was even submitted.
CROSS_KIND_DEADLINE_S = 12.0
#: Slice of that deadline the ANN route probe may spend (gr664970).
#: The probe is an up-front optimization whose whole cost is one pool
#: checkout plus the cached gate reads; letting it wait the entire
#: remaining budget meant a contended pool (or a stalled read) left the
#: fan-out nothing — every kind came back TimeoutError and the whole
#: call raised Internal (the 2026-11-13 all-kinds collapse). The slice
#: is ONE absolute deadline covering checkout and reads together — a
#: per-step slice lets a waited checkout stack full-slice reads on top.
#: Over this slice it fails open — no leg skip is claimed — and the
#: kinds keep the rest of the deadline.
CROSS_KIND_PROBE_BUDGET_S = 1.5
#: Bound on the probe watchdog's ``cancel_safe`` send — and so on the
#: ``join`` that reaps a fired watchdog before the probe's connection
#: goes back to the pool (gr664970 review: ``Timer.cancel`` stops only
#: a callback that has not started; one already running must finish
#: first, else its cancel lands on the next borrower's statement — but
#: an unbounded send, the 30 s default, would hold the checkout for it).
CROSS_KIND_PROBE_REAP_S = 1.0
#: Per-kind server-side statement cap inside the fan-out (gr519679).
#: Without it one kind's runaway query (a global-route ANN scan over the
#: paper-dominated index measures 10-30 s on prod) can hold a pooled
#: worker for the entire deadline while cheap kinds starve behind it.
#: Postgres cancels the statement at this bound, the kind lands in the
#: errored footer by name, and the worker moves on. Sized well above any
#: healthy kind's latency (a ready partial-index kind answers in <1 s)
#: and well below the whole-fan-out deadline.
CROSS_KIND_PER_KIND_S = 4.0

_cross_kind_pool: ThreadPoolExecutor | None = None
_cross_kind_pool_lock = threading.Lock()


def _cross_kind_executor() -> ThreadPoolExecutor:
    """Process-wide fan-out executor: one cap on DB connections held by
    cross-kind searches, including stragglers that outlive their deadline."""
    global _cross_kind_pool
    with _cross_kind_pool_lock:
        if _cross_kind_pool is None:
            _cross_kind_pool = ThreadPoolExecutor(
                max_workers=CROSS_KIND_MAX_WORKERS, thread_name_prefix="cross-kind"
            )
        return _cross_kind_pool


#: gr249198: ``MockEmbedder`` never raises, so the existing
#: ``semantic_degraded`` paths below — which key off catching
#: ``Upstream`` from a failing embed — never fire when the wired
#: backend is the "mock" default. A search that ran the semantic leg
#: against mock still gets a vector back and still executes the store
#: query; it just compares against deterministic hash noise, so a
#: real "no matches" is indistinguishable from "the semantic leg was
#: meaningless". This marker fires independent of embed success/
#: failure — a mock embed usually SUCCEEDS — whenever the wired
#: embedder is non-production, so the ambiguity above is never silent.
_NON_PRODUCTION_EMBEDDER_MARKER = (
    "_(⚠ embedder={backend!r} is non-production — the semantic leg ran "
    "against a deterministic mock vector, not a trained model; treat any "
    "apparent absence here as unproven, not confirmed. See "
    "get(kind='skill', id='precis-status') for the active backend.)_"
)

#: Variant for the cross-kind fan-out's up-front leg skip (gr628278):
#: under a non-production embedder the fan-out never runs the semantic
#: leg at all, so "ran against a mock vector" would claim a noise
#: ranking that isn't there.
_NON_PRODUCTION_EMBEDDER_SKIPPED_MARKER = (
    "_(⚠ embedder={backend!r} is non-production — the semantic leg was "
    "skipped up front (a mock vector can only rank hash noise); results "
    "are lexical-only. See get(kind='skill', id='precis-status') for "
    "the active backend.)_"
)


def _stamp_non_production_embedder(
    response: Response, embedder: Any | None, *, leg_skipped: bool = False
) -> Response:
    """Insert the non-production-embedder marker into ``response.body``.

    A no-op when ``embedder`` is ``None`` (mode='lexical' skipped the
    semantic leg entirely — nothing to warn about) or when the
    embedder doesn't carry ``is_production`` (an ad-hoc test double;
    default True keeps this silent rather than guessing). ``leg_skipped``
    picks the variant naming the up-front skip the cross-kind fan-out
    applies under a non-production embedder (gr628278), rather than the
    ran-anyway warning. Matches the existing "insert a bracketed
    one-liner after the headline" visual convention used for the
    errored/omitted-kinds trailers in
    :meth:`SearchMixin._dispatch_cross_kind`.
    """
    if embedder is None or getattr(embedder, "is_production", True):
        return response
    backend = getattr(embedder, "backend", type(embedder).__name__)
    marker = (
        _NON_PRODUCTION_EMBEDDER_SKIPPED_MARKER
        if leg_skipped
        else _NON_PRODUCTION_EMBEDDER_MARKER
    ).format(backend=backend)
    lines = response.body.splitlines()
    if lines:
        lines.insert(1, marker)
        return Response(
            body="\n".join(lines),
            cost=response.cost,
            result_count=response.result_count,
            result_ids=response.result_ids,
        )
    return Response(
        body=marker,
        cost=response.cost,
        result_count=response.result_count,
        result_ids=response.result_ids,
    )


def _search_call_text(kind: str | None, args: dict[str, Any], drop: set[str]) -> str:
    """Render ``search(…)`` over ``args`` minus ``drop``.

    Builds the literal ``next=`` hint — a copy-pasteable call that works
    (mcp-authoring rule 2), not prose. ``kind`` is the kind the
    interception ran under (popped off ``args`` before dispatch — ``None``
    when the call omitted it).

    Only the slim-advertised core params
    (:data:`~precis.runtime.dispatch.CORE_PARAMS` for ``search``) render
    top-level; every other key — a dispatch-payload key, a nested
    ``args=`` key or a resolved-facet origin alike — renders inside one
    ``args={…}`` dict (gr608575). The slim wrapper deprecates the
    top-level spelling of every non-core param, so a hint that
    re-rendered them top-level aimed the caller back at the deprecated
    form its own note named.

    Two things never render: ``__``-prefixed dispatch-internal keys and
    the ``exclude_ref_ids``/``include_ref_ids`` channel when a facet
    resolution wrote it — the origins recorded under
    :data:`RESOLVED_FACETS_KEY` render as the caller's own keys
    (``uncited=``/``cited=``/``hubbed=``/``under=``) instead. Tolerated
    wrapper defaults (``page=1``) are dropped too — they are not caller
    intent.
    """
    from precis.runtime.dispatch import CORE_PARAMS

    top_level = frozenset(CORE_PARAMS["search"]) - {"kind", "args"}
    resolved = args.get(RESOLVED_FACETS_KEY)
    resolved_map = resolved if isinstance(resolved, dict) else {}
    parts = [f"kind={kind!r}"] if kind is not None else []
    nested: dict[str, Any] = {}

    def _emit(k: str, v: Any) -> None:
        if k in top_level:
            parts.append(f"{k}={v!r}")
        else:
            nested[k] = v

    for k, v in args.items():
        if (
            k in drop
            or k.startswith("__")
            or k in resolved_map
            or v is None
            or v in _PAGE_TOLERATED.get(k, ())
        ):
            continue
        _emit(k, v)
    for channel, origins in resolved_map.items():
        if channel in drop:
            continue
        for ok, ov in origins:
            if ok in drop or ov is None:
                continue
            _emit(ok, ov)
    extras = args.get("__extras__")
    if isinstance(extras, dict):
        for k, v in extras.items():
            if k not in drop and v is not None:
                _emit(k, v)
    if nested:
        parts.append(f"args={nested!r}")
    return f"search({', '.join(parts)})"


#: A hit row on the markdown render starts ``\n## <rank>. `` — the
#: row-granular boundary a source-search ``budget_fit`` cuts on.
_HIT_ROW_RE = re.compile(r"\n## (\d+)\.\s")


def _hit_row_bounds(body: str) -> tuple[str, list[str]] | None:
    """Split a rendered hit page into ``(head, row blocks)``.

    ``head`` is everything before the first ``## 1.`` mark (headline plus
    any note/marker lines); each row block runs to the next sequential
    ``## <rank+1>.`` mark — the rank check keeps a ``## `` line inside a
    hit's own preview from splitting mid-row. ``None`` when the body has
    no numbered hit rows at all.
    """
    marks = list(_HIT_ROW_RE.finditer(body))
    bounds: list[tuple[int, int]] = []
    for i, m in enumerate(marks):
        if int(m.group(1)) != i + 1:
            break
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        bounds.append((m.start(), end))
    if not bounds:
        return None
    return body[: bounds[0][0]], [body[s:e] for s, e in bounds]


def _stub_state_line(row: dict[str, Any]) -> str:
    """The per-stub state line, with ``prio N (name)`` and (when the
    ``stub_rank`` pass's Tier-2 LLM band has labeled this stub)
    `` · <label>`` appended.

    ``row['prio']`` is ``None`` for an unranked stub (the ``stub_rank``
    pass hasn't scored it yet, or the whole pass is disabled) — omit the
    suffix entirely rather than rendering a noisy ``prio None``. Likewise
    ``row['llm_label']`` is empty for a stub outside the uncertain band
    (or the LLM step is disabled/hasn't reached it yet). The stub's prio
    is the same ``refs.prio`` ladder the todo/quest reads render, so it
    gets the same ``N (name)`` spelling (gr592664).
    """
    # Lazy — the handler import stays off the runtime/worker import path.
    from precis.handlers._prio_tag import prio_display

    prio = row.get("prio")
    line = (
        row["state"] if prio is None else f"{row['state']}  prio {prio_display(prio)}"
    )
    label = row.get("llm_label")
    if label:
        line = f"{line}  · {label}"
    return line


class SearchMixin(RuntimeShape):
    """Cross-kind fan-out, tags-only sweep, folder scope, source search."""

    # Default backlog size for ``search(view='stubs')`` — enough to
    # scan in one prompt without dumping the whole queue.
    _STUBS_DEFAULT_N: int = 25

    def _dispatch_stubs(self, kind: Any, args: dict[str, Any]) -> Response:
        """The required-documents backlog: ``search(view='stubs')``.

        Lists ``paper`` refs with an external identifier (DOI / arXiv /
        S2) registered but no PDF yet — the "papers we still need to
        get" queue the chase worker and the dream ``acquire`` tool both
        feed (the stub surfaces). ``kind='datasheet'`` instead lists the
        manual-fill stubs ``put(kind='datasheet', args={'part': …,
        'stub': True})`` mints (gr520099); any other kind is refused —
        nothing else has a stub backlog. ``q=`` narrows by metadata
        match (title / cite_key / identifier — a stub has no body
        chunks to rank, so there is no relevance ordering; gr608580),
        ``since=`` / ``until=`` bound ``refs.created_at``, and the
        ``uncited=`` / ``cited=`` / ``hubbed=`` facets apply through the
        include/exclude ref-id channel like on every other search
        shape. ``n=`` / ``page_size=``
        cap the row count;
        ``prio``-ranked first (1=hottest, unranked last), oldest request
        the tiebreak. Read-only — surfacing the backlog does not touch
        salience or the fetch pipeline.
        """
        from precis.utils.next_block import render_next_section

        refuse_unapplied_args(
            args,
            consumed=_STUBS_VIEW_CONSUMED,
            shape="a view='stubs' backlog listing",
            next="search(kind='paper', view='stubs')",
            tolerated=_PAGE_TOLERATED,
        )
        store = self.hub.store
        if store is None:
            raise Unsupported("view='stubs' needs a store-backed deployment")

        q = str(args.get("q") or "").strip() or None
        since = self._parse_search_date(args.get("since"), "since")
        until = self._parse_search_date(args.get("until"), "until")
        stub_filters: dict[str, Any] = {
            "q": q,
            "since": since,
            "until": until,
            "include_ref_ids": args.get("include_ref_ids"),
            "exclude_ref_ids": args.get("exclude_ref_ids"),
        }

        normalized = str(kind or "").strip().lower()
        if normalized == "datasheet":
            return self._datasheet_stubs(args, store=store, stub_filters=stub_filters)
        if normalized != "paper" and normalized not in _CROSS_KIND_ALIASES:
            raise BadInput(
                f"view='stubs' has no {normalized!r} backlog",
                options=["paper", "datasheet"],
                next="search(kind='datasheet', view='stubs')",
            )

        n = int(args.get("n") or args.get("page_size") or self._STUBS_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='stubs', page_size=25)")

        rows = store.stub_backlog(limit=n, **stub_filters)
        filtered = (
            q is not None
            or since is not None
            or until is not None
            or (
                stub_filters["include_ref_ids"] is not None
                or bool(stub_filters["exclude_ref_ids"])
            )
        )
        if not rows:
            if filtered:
                return Response(
                    body=(
                        "no stub papers match those filters — the "
                        "unfiltered backlog may still be non-empty "
                        "(search(view='stubs'))."
                    )
                )
            return Response(
                body=(
                    "no stub papers — every paper has a PDF or no external "
                    "identifier to fetch one with. Nothing to acquire."
                )
            )

        qualifiers: list[str] = []
        if q:
            qualifiers.append(f"q={q!r}")
        if since is not None:
            qualifiers.append(f"since {since.date().isoformat()}")
        if until is not None:
            qualifiers.append(f"until {until.date().isoformat()}")
        qual = f" matching {', '.join(qualifiers)}" if qualifiers else ""
        lines = [f"papers we still need to get{qual} ({len(rows)} shown):", ""]
        for r in rows:
            ident = r["identifier"] or "(no external id)"
            # Universal handle, not the bare ref_id: a bare number in
            # ``get(id=...)`` only resolves via the fallback that fires
            # the ``bare_numeric_hint`` WARNING — printing it here taught
            # the exact habit the system warns against one turn later.
            handle = handle_registry.format_handle("paper", r["ref_id"])
            cite = r["cite_key"] or handle
            lines.append(f"  {handle}  {ident}  [{cite}]")
            lines.append(f"      {_stub_state_line(r)}")
            reason = (r.get("llm_reason") or "").strip()
            if reason:
                if len(reason) > 80:
                    reason = reason[:79].rstrip() + "…"
                lines.append(f"      {reason}")
        body = "\n".join(lines)
        top_handle = handle_registry.format_handle("paper", rows[0]["ref_id"])
        body += render_next_section(
            [
                (
                    f"get(kind='paper', id='{top_handle}')",
                    "open a stub to see what links to it",
                ),
                (
                    # gr311340: single-kind ``search(kind='paper',
                    # tags=[...])`` with no q= now has its own
                    # recency-ordered tags-only path (PaperHandler.
                    # _list_by_tags), so this doesn't need the
                    # cross-kind wildcard fan-out anymore —
                    # ``DREAM:acquire`` is only ever set on paper stubs
                    # (PaperHandler.acquire) anyway.
                    "search(kind='paper', tags=['DREAM:acquire'])",
                    "just the papers a dream wanted",
                ),
                (
                    "get(kind='skill', id='precis-stubs-help')",
                    "how the backlog works",
                ),
            ]
        )
        return Response(body=body)

    def _datasheet_stubs(
        self,
        args: dict[str, Any],
        *,
        store: Any,
        stub_filters: dict[str, Any],
    ) -> Response:
        """``search(kind='datasheet', view='stubs')`` — bodiless sheets to fill.

        The datasheet counterpart of :meth:`_dispatch_stubs` (gr520099):
        a ``datasheet`` stub is minted on request (
        ``put(kind='datasheet', args={'part': …, 'stub': True})``) or
        when a pull leaves nothing fetchable, and is filled by a human
        dropping ``da<N>.pdf`` into ``<inbox>/datasheets/`` — there is
        no fetcher, so the row's actionable bit is the named-drop
        instruction, not a fetch state. Renders the ``da<N>`` handle,
        the part it documents, the cite_key slug and title. The same
        ``q=`` / ``since=`` / ``until=`` / ref-id filters apply
        (gr608580) — ``q=`` additionally matches the part number.
        """
        from precis.utils.next_block import render_next_section

        n = int(args.get("n") or args.get("page_size") or self._STUBS_DEFAULT_N)
        if n < 1:
            raise BadInput(
                "n must be >= 1",
                next="search(kind='datasheet', view='stubs', page_size=25)",
            )

        rows = store.stub_backlog(kind="datasheet", limit=n, **stub_filters)
        filtered = any(
            v is not None and (k != "exclude_ref_ids" or bool(v))
            for k, v in stub_filters.items()
        )
        if not rows:
            if filtered:
                return Response(
                    body=(
                        "no datasheet stubs match those filters — the "
                        "unfiltered backlog may still be non-empty "
                        "(search(kind='datasheet', view='stubs'))."
                    )
                )
            return Response(
                body=(
                    "no datasheet stubs — every datasheet has a body. Nothing to fill."
                )
            )

        lines = [f"datasheets still needing a PDF ({len(rows)} shown):", ""]
        for r in rows:
            handle = handle_registry.format_handle("datasheet", r["ref_id"])
            cite = r["cite_key"] or handle
            head = f"  {handle}"
            if r["part_lcsc"]:
                head += f"  {r['part_lcsc']}"
            head += f"  [{cite}]"
            if r["title"]:
                head += f"  {r['title']}"
            lines.append(head)
            state = _stub_state_line(r) if r["last_event"] else "awaiting fill"
            lines.append(f"      {state} — drop <inbox>/datasheets/{handle}.pdf")
        body = "\n".join(lines)
        top_handle = handle_registry.format_handle("datasheet", rows[0]["ref_id"])
        top_part = rows[0]["part_lcsc"]
        hints = [
            (
                f"get(kind='datasheet', id='{top_handle}')",
                "open a stub",
            ),
        ]
        if top_part:
            hints.append(
                (
                    f"get(kind='part', id='{top_part}')",
                    "the part it documents",
                )
            )
        hints.append(
            (
                "get(kind='skill', id='precis-datasheet-help')",
                "how datasheet ingest works",
            )
        )
        body += render_next_section(hints)
        return Response(body=body)

    def _dispatch_chase_queue(self, kind: Any, args: dict[str, Any]) -> Response:
        """The DOI-only, never-tried-first chase queue:
        ``search(view='chase-queue')``.

        A tighter slice of the same stub backlog ``view='stubs'``
        surfaces: DOI-only (the
        identifier kind the fetcher cascade covers most reliably) and
        ordered never-tried-first rather than oldest-request-first —
        "what should I go find a PDF for right now" rather than "what's
        been waiting longest". Paper-only (gr520077: any other kind is
        refused — nothing else has a DOI-fetcher queue to list); ``q=``
        is ignored (unlike ``view='stubs'``, which filters by it —
        gr608580 — a documented no-op here).
        ``n=`` / ``page_size=`` cap
        the row count. Read-only — surfacing the queue does not touch
        the fetch pipeline.
        """
        from precis.utils.next_block import render_next_section

        refuse_unapplied_args(
            args,
            consumed=_STUBS_CONSUMED,
            ignored={"q"},
            shape="a view='chase-queue' listing",
            next="search(kind='paper', view='chase-queue')",
            tolerated=_PAGE_TOLERATED,
        )
        normalized = str(kind or "").strip().lower()
        if normalized != "paper" and normalized not in _CROSS_KIND_ALIASES:
            raise BadInput(
                f"view='chase-queue' is paper-only (kind={normalized!r})",
                options=["paper"],
                next="search(kind='paper', view='chase-queue')",
            )

        store = self.hub.store
        if store is None:
            raise Unsupported("view='chase-queue' needs a store-backed deployment")

        n = int(args.get("n") or args.get("page_size") or self._STUBS_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='chase-queue', n=25)")

        rows = store.stub_backlog(id_kinds=("doi",), sort="last-tried", limit=n)
        if not rows:
            return Response(
                body=(
                    "no DOI-only stubs — nothing waiting in the chase queue. "
                    "Nothing to acquire."
                )
            )

        lines = [f"chase queue — DOI-only, never-tried first ({len(rows)} shown):", ""]
        for r in rows:
            ident = r["identifier"] or "(no external id)"
            # See ``_dispatch_stubs`` above — universal handle, not the
            # bare ref_id.
            handle = handle_registry.format_handle("paper", r["ref_id"])
            cite = r["cite_key"] or handle
            lines.append(f"  {handle}  {ident}  [{cite}]")
            lines.append(f"      {_stub_state_line(r)}")
        body = "\n".join(lines)
        top_handle = handle_registry.format_handle("paper", rows[0]["ref_id"])
        body += render_next_section(
            [
                (
                    f"get(kind='paper', id='{top_handle}')",
                    "open a stub to see what links to it",
                ),
                (
                    "search(kind='paper', view='stubs')",
                    "the full acquisition backlog (every id kind)",
                ),
                (
                    "get(kind='skill', id='precis-stubs-help')",
                    "how the backlog works",
                ),
            ]
        )
        return Response(body=body)

    # ── cross-kind search ──────────────────────────────────────────────

    def _is_cross_kind_request(self, kind: Any) -> bool:
        """True iff ``kind`` asks for a cross-kind merge.

        Forms accepted (case-insensitive on aliases):

        - the wildcard ``'*'`` and its English aliases ``'all'`` /
          ``'any'`` (see :data:`~precis.runtime._shared.CROSS_KIND_ALIASES`);
        - ``'source'`` — the fixed paper+finding-hub alias (see
          :data:`~precis.runtime._shared.CROSS_KIND_SOURCE_ALIAS`);
        - any comma-list (``'paper,memory'`` or ``'paper, memory'``);
        - an explicit empty string (``''``) is treated like the
          wildcard for symmetry with MCP clients that send ``kind=""``.

        ``None`` does NOT count here — it goes through the
        single-kind defaulting path so callers that forgot
        ``kind=`` get the friendly "what were you working on"
        nudge before being escalated to cross-kind merge.
        """
        if not isinstance(kind, str):
            return False
        normalized = kind.strip().lower()
        if normalized in _CROSS_KIND_ALIASES:
            return True
        if normalized == _CROSS_KIND_SOURCE_ALIAS:
            return True
        if "," in kind:
            return True
        return False

    def _cross_kind_kinds(self) -> list[str]:
        """Active kinds whose ``KindSpec.supports_search_hits`` is True.

        These are the kinds the cross-kind merge knows how to
        ingest.  Excluded handlers (calc, python, md, tag, …) keep
        their single-kind ``search()`` contract; their absence from
        this list is by design.
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            spec = handler.spec
            if spec.supports_search and spec.supports_search_hits:
                out.append(k)
        return out

    def _dispatch_search_plan(self, kind: Any, args: dict[str, Any]) -> Response:
        """``search(view='plan')`` — the route a kind-scoped search *would*
        take, without running it (gr503028).

        Per kind: the resolved mode, the embedding model, the ANN index
        (or exact-scan / global-fallback route) and *why*, the lexical
        leg, and the candidate caps. Read-only: the only SQL is the ANN
        state probe + an extension-version read, so the call can never
        rank — which is the point (an agent can't tell a slow search
        from a wrong one when the route is invisible).
        """
        from precis.store._chunks_ops import (
            _ANN_EF_SEARCH_MAX,
            _ANN_EF_SEARCH_MIN,
            _ANN_EF_SEARCH_PER_LIMIT,
            _ANN_MAX_SCAN_TUPLES,
            ann_route_plan,
        )

        refuse_unapplied_args(
            args,
            consumed=_PLAN_CONSUMED,
            shape="a view='plan' render (no ranking runs)",
            next="run the search itself — view='plan' only describes the route",
            tolerated=_PAGE_TOLERATED,
        )
        store = self.store
        if store is None:
            raise Unsupported("view='plan' needs a store-backed deployment")
        if kind is None or self._is_cross_kind_request(str(kind)):
            kinds = self._cross_kind_kinds()
        else:
            kinds = [self._expand_kind_code(str(kind))]
        mode = str(args.get("mode") or "hybrid").strip().lower()
        q = args.get("q")
        limit = int(args.get("page_size") or args.get("top_k") or args.get("n") or 20)
        ef = min(
            _ANN_EF_SEARCH_MAX,
            max(_ANN_EF_SEARCH_MIN, _ANN_EF_SEARCH_PER_LIMIT * limit),
        )
        embedder = getattr(self.hub, "embedder", None)
        # The real search's filters are accepted, echoed, and used to
        # name the path the same call would take without view='plan' —
        # they decide the dispatch route (folder= and sort=/since=/
        # until= divert to interceptions), so naming the single-kind
        # path unconditionally would claim a route the call never takes.
        sim = {k: v for k, v in args.items() if k != "view"}
        lines = [
            "# search plan (no ranking executed)",
            f"path: {self._plan_path_label(kind, sim)}",
            f"mode: {mode}"
            + (f" · q={str(q)[:40]!r}" if q is not None else "")
            + (
                f" · embedder={getattr(embedder, 'model', None) or type(embedder).__name__}"
                if embedder is not None
                else " · embedder=none (lexical-only)"
            ),
            f"candidates: hnsw.ef_search={ef} (clamp {_ANN_EF_SEARCH_MIN}..{_ANN_EF_SEARCH_MAX}"
            f" of {_ANN_EF_SEARCH_PER_LIMIT}×limit {limit})"
            f" · hnsw.max_scan_tuples={_ANN_MAX_SCAN_TUPLES}",
        ]
        supplied = [
            f"{k}={args[k]!r}"
            for k in sorted(_PLAN_FILTER_KEYS)
            if args.get(k) is not None
        ]
        if supplied:
            lines.append("filters: " + ", ".join(supplied))
        for k in kinds:
            handler = self.hub.handler_for(k)
            with store.pool.connection() as conn:
                plan = ann_route_plan(conn, k, None)
            mro = {c.__name__ for c in type(handler).__mro__} if handler else set()
            if "NumericRefHandler" in mro:
                if getattr(handler, "search_body_chunks", False):
                    lex = "chunks.tsv FTS (body chunks, grouped by ref)"
                else:
                    ck = getattr(handler, "search_card_kinds", None)
                    lex = "refs.title FTS + chunk leg"
                    lex += f" (incl. cards: {', '.join(ck)})" if ck else ""
            elif "PaperHandler" in mro:
                lex = "chunks.tsv FTS (lex leg of the RRF fusion)"
            else:
                lex = "kind-specific path (see the handler's search)"
            lines += [
                f"kind={k}:",
                f"  index: {plan['index'] or 'none (exact scan)'}",
                f"  route: {plan['route']} — {plan['reason']}",
                f"  lexical: {lex}",
            ]
        return Response(body="\n".join(lines))

    def _plan_path_label(self, kind: Any, args: dict[str, Any]) -> str:
        """The dispatch path this call (``view='plan'`` removed) would take.

        Mirrors the interception order in
        :meth:`~precis.runtime.dispatch.DispatchMixin._dispatch_inner_core`:
        ``folder=`` post-filters through the fan-out first, then the
        ``sort=``/``since=``/``until=`` source-search primitive (with its
        numeric-browse, count-view, owned-``under=`` and queryless
        ``kind='job'`` carve-outs inside :meth:`_routes_to_source_search`),
        then the
        plain cross-kind merge, then the single-kind handler path.
        """
        if args.get("folder") is not None:
            return "cross-kind fan-out (folder= subtree post-filter)"
        if self._routes_to_source_search("search", kind, args):
            return f"cross-kind source search ({SOURCE_SEARCH_TRIGGER_LABEL} primitive)"
        if kind is None or self._is_cross_kind_request(str(kind)):
            return "cross-kind fan-out (RRF merge)"
        return f"single-kind search on kind={self._expand_kind_code(str(kind))!r}"

    def _cross_kind_excluded_kinds(self) -> list[str]:
        """Active kinds searchable per-kind but opted out of cross-kind.

        These are kinds with ``supports_search=True`` but
        ``supports_search_hits=False`` — they carry per-kind result
        shapes (TOON tables with kind-specific columns, score-
        annotated skill rows, tag rows from a different table) that
        the cross-kind SearchHit substrate would have to flatten and
        lose information from. The wildcard cross-kind footer names
        them so the agent knows the kinds exist and where to look.
        Broad-pass finding #7.
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            spec = handler.spec
            if spec.supports_search and not spec.supports_search_hits:
                out.append(k)
        return out

    def _resolve_cross_kind_request(self, kind: str) -> list[str]:
        """Expand ``kind`` into the concrete list of kinds to fan out to.

        Wildcard expands to every search-hits-capable kind.
        ``'source'`` expands to exactly
        :data:`~precis.runtime._shared.CROSS_KIND_SOURCE_KINDS`
        (``paper`` + ``finding``) — raises ``BadInput`` if either isn't
        search-hits-capable in this build, same as an explicit comma-list
        naming an unsupported kind. Comma-lists are split, normalised
        (trim whitespace), and validated: unknown kinds and kinds that
        don't support cross-kind search raise ``BadInput`` with the
        recoverable list as ``options``.
        """
        eligible = self._cross_kind_kinds()
        normalized = kind.strip().lower()
        if normalized in _CROSS_KIND_ALIASES:
            search_trace.resolved_kinds(eligible)
            return eligible
        if normalized == _CROSS_KIND_SOURCE_ALIAS:
            eligible_set = set(eligible)
            missing = [k for k in _CROSS_KIND_SOURCE_KINDS if k not in eligible_set]
            if missing:
                raise BadInput(
                    f"kind='source' needs {list(_CROSS_KIND_SOURCE_KINDS)!r} "
                    f"search-hits-capable in this build — missing {missing!r}",
                    options=eligible,
                    next=(
                        "use kind='paper' or kind='finding' directly, or "
                        "kind='*' for the full cross-kind fan-out"
                    ),
                )
            search_trace.resolved_kinds(list(_CROSS_KIND_SOURCE_KINDS))
            return list(_CROSS_KIND_SOURCE_KINDS)

        requested = [tok.strip() for tok in kind.split(",")]
        requested = [t for t in requested if t]
        if not requested:
            search_trace.resolved_kinds(eligible)
            return eligible

        bad = [t for t in requested if t not in eligible]
        if bad:
            registered = self.hub.kinds
            unknown = [t for t in bad if t not in registered]
            unsupported = [t for t in bad if t in registered]
            if unknown:
                raise BadInput(
                    f"unknown kind(s) in cross-kind request: {unknown!r}",
                    options=eligible,
                    next=(
                        "drop the unknown kind(s); cross-kind merge accepts "
                        f"the listed kinds, or use kind={_CROSS_KIND_WILDCARD!r} for all"
                    ),
                )
            if unsupported:
                raise BadInput(
                    (f"kind(s) do not support cross-kind search: {unsupported!r}"),
                    options=eligible,
                    next=(
                        "the listed kinds opt into the merge via "
                        "supports_search_hits; the others keep their "
                        "single-kind search() contract - call them one at a time"
                    ),
                )
        # Preserve caller order (first-occurrence) so the output
        # rendering deterministically reflects what the agent
        # asked for.  Dedup while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for t in requested:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _search_domain(self, args: dict[str, Any]) -> Domain:
        """The domain fencing this call's default kind expansion.

        ``domain=`` (top-level, or hoisted from ``args={'domain': …}``)
        overrides the connection/env resolution for the one call — the
        escape door the skipped-kinds footer names (gr572720). An
        unknown name refuses loud with the registry as ``options`` —
        never silently widens to ``'all'``, the same contract
        ``current_domain`` gives a ``PRECIS_DOMAIN`` typo.
        """
        name = args.get("domain")
        if name is None or not str(name).strip():
            return current_domain()
        key = str(name).strip()
        try:
            return DOMAINS[key]
        except KeyError:
            raise BadInput(
                f"domain={key!r} names no registered domain",
                options=sorted(DOMAINS),
                next="pass args={'domain': 'all'} for the unrestricted surface",
            ) from None

    def _domain_fan_out_fence(
        self, requested: str, kinds: list[str], args: dict[str, Any]
    ) -> tuple[list[str], str | None]:
        """Intersect a *default-expansion* fan-out with the domain's kinds.

        ``requested`` is the spelling that reached
        :meth:`_resolve_cross_kind_request` — the fence fires only when
        the expansion was the default (a wildcard alias, which is also
        what an omitted ``kind=`` becomes upstream). An explicit
        ``kind=`` or comma list outside the domain still runs, and
        ``kind='source'`` counts as explicit. Returns ``(kinds,
        footer)`` — ``footer`` names the skipped kinds and the door,
        ``None`` when nothing was fenced (``'all'`` included: its
        ``kinds`` axis is unrestricted, so the output stays
        byte-identical to the unfenced fan-out).
        """
        if requested.strip().lower() not in _CROSS_KIND_ALIASES:
            return kinds, None
        domain = self._search_domain(args)
        if domain.kinds is None:
            return kinds, None
        kept = [k for k in kinds if k in domain.kinds]
        skipped = [k for k in kinds if k not in domain.kinds]
        if not skipped:
            return kinds, None
        if not kept:
            raise BadInput(
                f"domain {domain.name!r} leaves no cross-kind-searchable "
                f"kinds — all {len(skipped)} eligible kinds sit outside it",
                next=(
                    'args={"domain": "all"} for the unrestricted fan-out, '
                    "or kind=<list> to name kinds directly"
                ),
            )
        search_trace.resolved_kinds(kept)
        footer = (
            f"{len(skipped)} kinds outside domain {domain.name} not searched; "
            'args={"domain": "all"} or '
            f"kind='{','.join(skipped)}'"
        )
        return kept, footer

    @staticmethod
    def _is_source_search_request(args: dict[str, Any]) -> bool:
        """True when a search opts into the Slice-2 source primitive.

        Triggered by an explicit ``sort=`` (``relevance`` / ``recency``)
        or any ``since=`` / ``until=`` date bound. None of these args
        exist on the legacy single-kind / fan-out paths, so the check
        never hijacks an existing call shape.
        """
        sort = str(args.get("sort") or "").strip().lower()
        if sort in ("relevance", "recency"):
            return True
        return args.get("since") is not None or args.get("until") is not None

    def _routes_to_source_search(
        self, verb: str, kind: Any, args: dict[str, Any]
    ) -> bool:
        """Whether this call takes the cross-kind source-search primitive.

        The interception condition shared by
        :meth:`~precis.runtime.dispatch.DispatchMixin._dispatch_inner_core`
        and :meth:`_dispatch_search_plan` (which names the route a real
        call would take — the label must not drift from the routing).
        A few shapes keep their own path even with trigger keys present:

        * ``numeric_browse`` — a queryless status/tag browse on
          gripe/todo/quest is the kind's own listing, not a dated search;
        * ``view='count'`` — keeps ``since=``/``until=`` for its own
          ``created_at`` filter;
        * an ``under=`` the kind OWNS (``owns_under`` — todo/taxon trees,
          job's hierarchical scope) — source search would silently drop
          the scope;
        * a queryless ``kind='job'`` without an explicit ``sort=`` —
          ``since=``/``until=`` on a plain job listing are the kind's own
          ``created_at`` bounds, so a bare window stays on the job
          listing path. A ``q=`` or an explicit ``sort=`` asks for
          ranking and still routes here.
        """
        if verb != "search" or not self._is_source_search_request(args):
            return False
        # The memory inventory owns its sort vocabulary and rejects date
        # windows; source routing must not turn a bad inventory option into
        # an unrelated ranked search.
        if (
            kind is not None
            and self._expand_kind_code(str(kind)) == "memory"
            and args.get("view") == "reads"
        ):
            return False
        from precis.utils.browse_sort import numeric_browse

        if numeric_browse(kind, args):
            return False
        if str(args.get("view") or "").strip() == "count":
            return False
        if kind is not None and not self._is_cross_kind_request(str(kind)):
            resolved = self._expand_kind_code(str(kind))
            if args.get("under") is not None and bool(
                (h := self.hub.handler_for(resolved)) and h.owns_under
            ):
                return False
            if resolved == "quest" and (
                args.get("scope") is not None
                or (
                    isinstance(args.get("__extras__"), dict)
                    and args["__extras__"].get("within") is not None
                )
            ):
                # gr592279: a within-one-quest logbook search is the
                # kind's own scope= path — source search has no way to
                # apply it and would silently search the whole corpus.
                # Any since=/until=/sort= the call also carries reaches
                # the handler, which refuses what it cannot apply.
                return False
            if resolved == "job" and (
                args.get("job_type") is not None
                or (
                    isinstance(args.get("__extras__"), dict)
                    and args["__extras__"].get("job_type") is not None
                )
            ):
                # gr608734: job_type is the kind's own meta filter —
                # the cross-kind primitive has no way to apply it and
                # refused the call outright. The handler's job_type path
                # applies q=/since=/until= itself and refuses what it
                # cannot (sort=/mode=).
                return False
            q = args.get("q")
            if (
                resolved == "job"
                and args.get("sort") is None
                and not (isinstance(q, str) and q.strip())
            ):
                return False
        return True

    @staticmethod
    def _parse_search_date(value: Any, field: str) -> datetime | None:
        """Parse a ``since=`` / ``until=`` bound into a tz-aware datetime.

        Accepts an ISO date (``2024-01-01``) or a full ISO timestamp; a
        naive value is assumed UTC (``refs.created_at`` is ``timestamptz``)
        and an offset-aware value is normalized to UTC so a ``Z``-labelled
        echo can't print the offset hour as UTC. Raises ``BadInput`` on an
        unparseable string.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError as exc:
                raise BadInput(
                    f"{field}= must be an ISO date/timestamp, got {value!r}",
                    next=f"{field}='2024-01-01' or '2024-01-01T00:00:00'",
                ) from exc
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    def _handle_query_hit(
        self,
        q: Any,
        *,
        kinds: Any = None,
        include_ref_ids: Any = None,
        exclude_ref_ids: Any = None,
    ) -> SearchHit | None:
        """The pinned identity hit for a ``q`` that *is* a universal handle.

        A handle names one object, so its card ranks ahead of every
        lexical/semantic hit (``pinned`` — gr517806): a record handle
        (``me5``, ``gr123``) yields its ref card; a chunk handle
        (``dc4417588``, ``pc10`` — every chunk code the registry knows)
        yields the containing ref's card showing that chunk's text, so
        the caller sees the doc the chunk lives in rather than a bare
        chunk row. For the status kinds (``_PARENT_HANDLE_KINDS``) the
        card is the parent ref (``gr123``, not ``gc456``), matching the
        parent-handle collapse the source search already applies.

        ``kinds`` is the caller's requested kind set — an explicit
        ``kind='paper,patent'`` search must not pin a todo card as row 1
        of a paper-scoped result. ``None`` means unrestricted (the
        wildcard/omitted-``kind`` request — a handle is an address, so a
        wildcard search pins any kind, including kinds like draft that
        opt out of the fan-out itself). ``include_ref_ids`` /
        ``exclude_ref_ids`` (the cited=/uncited= facets) still apply to
        the pinned hit — an identity card that
        the caller's own filter excludes must not surface. Tag and
        folder filters do NOT apply: a handle is an address, not a
        match. ``None`` for anything that is not a well-formed handle,
        resolves to nothing (unknown/retired), or has no store —
        the caller falls through to the ordinary fan-out untouched.

        A non-handle ``q`` that *is* another identifier form — a DOI
        (bare or pasted doi.org URL) or a cite_key — resolves through
        the same probe the web /drive box redirects on
        (:func:`precis.handlers._query_identifier.resolve_query_identifier`)
        and pins the same record card (gr537783); before this, a bare
        DOI/cite-key ``search`` fanned out to chunk text the id never
        appears in and returned nothing.
        """
        store = self.hub.store
        if not isinstance(q, str) or store is None:
            return None
        text = q.strip()
        if not handle_registry.is_well_formed(text):
            # A DOI (bare or pasted doi.org URL) or a cite_key names one
            # ref too — resolve it through the same identifier probe the
            # web /drive box redirects on
            # (:func:`precis.handlers._query_identifier.resolve_query_identifier`)
            # so both surfaces share the one matcher (gr537783). The
            # pinned card is the record-handle card's shape; the same
            # kind/include/exclude gates apply (a handle is an address —
            # so is a DOI or cite-key).
            from precis.handlers._query_identifier import (
                resolve_query_identifier,
            )

            try:
                ident_ref = resolve_query_identifier(store, text).ref
            except Exception:  # a pin is a relevance aid, never fatal
                log.warning(
                    "cross-kind: identifier pin probe failed for %r",
                    text,
                    exc_info=True,
                )
                return None
            if ident_ref is None:
                return None
            return self._pinned_ref_hit(
                ident_ref,
                store=store,
                kinds=kinds,
                include_ref_ids=include_ref_ids,
                exclude_ref_ids=exclude_ref_ids,
            )
        resolved = store.resolve_handle(text)
        if resolved is None:
            return None
        if kinds is not None and resolved.kind not in kinds:
            return None
        if exclude_ref_ids and resolved.ref_id in exclude_ref_ids:
            return None
        if include_ref_ids is not None and resolved.ref_id not in include_ref_ids:
            return None
        ref = store.fetch_refs_by_ids([resolved.ref_id]).get(resolved.ref_id)
        if resolved.kind == "profile":
            # owner-private rows (gr490266): another owner's profile
            # pins nothing — the card would be an existence oracle plus
            # a title leak. Same filter the tags-only sweep applies.
            from precis import profiles

            if ref is None or not profiles.visible_to(
                ref, profiles.acting_login(store)
            ):
                return None
        title = (getattr(ref, "title", "") or "").strip() if ref is not None else ""
        retraction = getattr(ref, "retraction_status", None)
        handle = handle_registry.normalize(text)
        excerpt = 200

        if resolved.chunk_id is None:
            # Record handle → the ref card — the same pinned identity hit
            # the identifier (DOI/cite_key) branch emits.
            if ref is None:
                return None
            return self._pinned_ref_hit(
                ref,
                store=store,
                kinds=kinds,
                include_ref_ids=include_ref_ids,
                exclude_ref_ids=exclude_ref_ids,
                uhandle=handle,
            )

        # Chunk handle → the containing ref's card at that chunk.
        chunk = store.drafts.universal_chunk(text) or {}
        body = str(chunk.get("text") or "").strip()
        preview = body if len(body) <= excerpt else body[: excerpt - 1] + "…"
        is_parent_kind = resolved.kind in _PARENT_HANDLE_KINDS
        slug = (
            resolved.public_id if resolved.public_id != str(resolved.ref_id) else None
        )
        return SearchHit(
            score=1.0,
            kind=resolved.kind,
            title=title,
            preview=preview,
            # The TOON table renders only the summary cell — put the
            # ref's name + the chunk's own text there so the pinned row
            # shows what the handle pointed at, not just the doc title.
            summary=(
                f"{title} — {preview}"
                if title and preview
                else (title or preview) or None
            ),
            slug=slug,
            pos=None if is_parent_kind else resolved.chunk_ord,
            ref_id=resolved.ref_id,
            dedupe_key=(
                f"{resolved.kind}:{resolved.ref_id}"
                if is_parent_kind
                else f"{resolved.kind}:{resolved.public_id}~{resolved.chunk_ord}"
            ),
            uhandle=(
                handle_registry.try_format(resolved.kind, resolved.ref_id)
                if is_parent_kind
                else handle
            ),
            pinned=True,
            retraction_status=retraction,
        )

    def _pinned_ref_hit(
        self,
        ref: Any,
        *,
        store: Any,
        kinds: Any = None,
        include_ref_ids: Any = None,
        exclude_ref_ids: Any = None,
        uhandle: str | None = None,
    ) -> SearchHit | None:
        """The pinned record card for a ref resolved by identifier —
        a record handle, DOI, doi.org URL or cite_key
        (``_handle_query_hit``). Same gates as the handle path: the
        caller's kind set scopes it, the cited=/uncited= ref-id facets
        apply, and another owner's ``profile`` row pins nothing. A DOI /
        cite-key is an address, not a match, so tag/folder filters do
        NOT apply — mirroring the handle rule. Dedupe keyed like the
        ref-level producers (``kind:slug`` / ``kind:#id``) so a lexical
        hit on the same ref folds into this card. ``uhandle`` overrides
        the computed handle only for the record-handle branch (the
        normalized query handle); ``None`` asks for
        ``try_format`` — itself ``None`` for a code-less kind, where the
        renderers fall back to ``slug~pos``/``#ref_id``.
        """
        if kinds is not None and ref.kind not in kinds:
            return None
        if exclude_ref_ids and ref.id in exclude_ref_ids:
            return None
        if include_ref_ids is not None and ref.id not in include_ref_ids:
            return None
        if ref.kind == "profile":
            # owner-private rows (gr490266): another owner's profile
            # pins nothing — the card would be an existence oracle plus
            # a title leak. Same filter the tags-only sweep applies.
            from precis import profiles

            if not profiles.visible_to(ref, profiles.acting_login(store)):
                return None
        title = (getattr(ref, "title", "") or "").strip()
        excerpt = 200
        preview = title if len(title) <= excerpt else title[: excerpt - 1] + "…"
        public_id = getattr(ref, "slug", None) or str(ref.id)
        slugish = public_id != str(ref.id)
        return SearchHit(
            score=1.0,
            kind=ref.kind,
            title=title,
            preview=preview,
            slug=public_id if slugish else None,
            ref_id=ref.id,
            dedupe_key=(
                f"{ref.kind}:{public_id}" if slugish else f"{ref.kind}:#{ref.id}"
            ),
            uhandle=uhandle or handle_registry.try_format(ref.kind, ref.id),
            pinned=True,
            retraction_status=getattr(ref, "retraction_status", None),
        )

    def _dispatch_source_search(
        self, kind: str | None, args: dict[str, Any]
    ) -> Response:
        """Cross-kind chunk search — RRF-fused, per-ref best chunk, dated.

        The source-search primitive (see
        :meth:`Store.search_chunks_across_kinds`). Resolves the kind set
        (single/comma-list/wildcard/omitted → every cross-kind kind;
        kinds with no embedded chunks contribute nothing), embeds ``q``
        once, runs the single store query, renders per-ref hits (each
        stamped with its own ``ref.kind``) as one pre-ordered stream.

        ``args['exclude_ref_ids']``/``args['include_ref_ids']``
        (``_dispatch_inner``'s ``uncited=``/``cited=``/``hubbed=``
        resolution) thread straight into
        :meth:`~precis.store._chunks_ops.BlocksMixin.search_chunks_across_kinds`,
        applied in the ONE underlying SQL query across every kind — the
        only search path with no per-kind support gap (unlike
        :meth:`_dispatch_cross_kind`'s per-handler fan-out), so this is
        the recommended route for ``uncited=``/``cited=`` when the target
        kinds include patent/edgar.

        ``exclude=`` resolves slug/handle/container entries per kind into
        the same channel (gr608576); ``args={'serves': 'quest:<id>'}``
        resolves the quest's serving-refs cohort into the include channel
        (gr608733 — the paper path's ``serves=`` filter, honoured here so
        the dated call is not refused at the unapplied-args gate);
        ``queries=``/``answers=`` add
        broad-retrieval legs fused with ``q`` (same shape as the paper
        handler's broad path); ``page=``/``args={'offset': N}`` page the
        fused per-ref list with a one-row probe for the continuation
        hint — ``offset`` is the exact primitive a truncated page's
        ``next:`` call names (td610130). A pinned identity card counts
        as a page row — it fills row 1 of the head window and shifts
        every later window's fused offset back one.

        Queryless calls (``q=`` absent/blank/``'*'`` — folded upstream)
        do not rank: when the call narrows to a named cohort — ``serves=``
        resolved, or a ``cited=``/``under=`` facet wrote the include
        channel — :meth:`_source_filtered_listing` enumerates the
        cohort's live refs (td628495 review — a ``paper + since +
        serves`` call used to die here with "requires q=" before
        ``serves`` ever resolved). With no cohort the refusal stands:
        a bare date window over the whole corpus is a browse, not a
        listing.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("source search needs a store-backed deployment")
        # ``args={…}`` extras: hoist the keys this path consumes (plus the
        # no-op spellings it tolerates) to top level — paper search takes
        # the same knobs nested, so callers reuse that spelling here
        # (gr608576). ``args={'page': N}`` rides the same hoist — the
        # paging spelling on this path (gr608577); the verb wrapper's own
        # ``page=1`` default must not shadow it. Anything left in extras
        # is still refused below.
        extras = args.get("__extras__")
        if isinstance(extras, dict):
            hoistable = _SOURCE_SEARCH_CONSUMED - {
                "exclude_ref_ids",
                "include_ref_ids",
            }
            for key in list(extras):
                val = extras.get(key)
                if val is None:
                    continue
                if key not in hoistable and val not in _SOURCE_SEARCH_TOLERATED.get(
                    key, ()
                ):
                    continue
                if args.get(key) is not None and not (
                    key == "page" and args.get("page") == 1
                ):
                    raise BadInput(
                        f"{key}= was passed twice — top-level and inside args=",
                        next=f"pass {key}= once, at the top level",
                    )
                args[key] = extras.pop(key)
        refuse_unapplied_args(
            args,
            consumed=_SOURCE_SEARCH_CONSUMED,
            shape=f"a source search ({SOURCE_SEARCH_TRIGGER_LABEL})",
            next=lambda strays: self._source_search_next(kind, args, strays),
            tolerated=_SOURCE_SEARCH_TOLERATED,
        )
        q = args.get("q")
        has_q = isinstance(q, str) and bool(q.strip())
        expanded = (
            self._expand_kind_code(str(kind))
            if kind is not None
            else _CROSS_KIND_WILDCARD
        )
        kinds = self._resolve_cross_kind_request(expanded)
        # Domain fence (gr572720): an omitted kind expands to the
        # wildcard, which intersects the resolved domain's kinds; an
        # explicit kind=/comma list outside the set still runs.
        kinds, domain_footer = self._domain_fan_out_fence(expanded, kinds, args)
        since = self._parse_search_date(args.get("since"), "since")
        until = self._parse_search_date(args.get("until"), "until")
        sort = str(args.get("sort") or "relevance").strip().lower() or "relevance"
        page_raw = args.get("page")
        try:
            page = int(page_raw) if page_raw is not None else 1
        except (TypeError, ValueError):
            page = 0
        if page < 1 or isinstance(page_raw, bool):
            raise BadInput(
                f"page= must be a positive integer, got {page_raw!r}",
                next=self._source_search_page_call(kind, args, page_size=None, page=1),
            )
        # ``args={'offset': N}`` (td610130): the exact resume primitive a
        # truncated page's ``next:`` call names — ``page`` can only place
        # a window at a multiple of ``page_size``, so a mid-page cut
        # needs the absolute row offset to avoid a gap/overlap.
        offset_raw = args.get("offset")
        try:
            resume_offset = int(offset_raw) if offset_raw is not None else None
        except (TypeError, ValueError):
            resume_offset = -1
        if isinstance(offset_raw, bool) or (
            resume_offset is not None and resume_offset < 0
        ):
            raise BadInput(
                f"offset= must be a non-negative integer, got {offset_raw!r}",
                next=self._source_search_page_call(
                    kind, args, page_size=None, offset=0
                ),
            )
        if resume_offset is not None and page > 1:
            raise BadInput(
                "page= and offset= both place the window — pass one",
                next=self._source_search_page_call(
                    kind, args, page_size=None, offset=resume_offset
                ),
            )
        top_k = int(args.get("page_size") or args.get("top_k") or 10)
        # Broad-retrieval legs (gr608576): ``queries=`` are extra
        # reformulations, ``answers=`` hypothetical-answer passages (HyDE)
        # — each fuses its own leg via search_chunks_multi, same as the
        # paper handler's broad path. Re-check the leg cap here: the
        # agentic tier bypasses the MCP tool boundary's validation.
        from precis.handlers._paper_search import _BROAD_LEG_CAP

        extra_queries = self._str_list(args.get("queries"))
        hyde_answers = self._str_list(args.get("answers"))
        if len(extra_queries) > _BROAD_LEG_CAP:
            raise BadInput(
                f"queries= has {len(extra_queries)} entries, max {_BROAD_LEG_CAP}",
                next="pass up to 8 distinct rephrasings; merge the rest",
            )
        if len(hyde_answers) > _BROAD_LEG_CAP:
            raise BadInput(
                f"answers= has {len(hyde_answers)} entries, max {_BROAD_LEG_CAP}",
                next="pass up to 8 hypothetical-answer passages (HyDE)",
            )
        tags = args.get("tags")
        # gr495382: ``status=`` used to be dropped on this path, so a
        # ``sort=`` search of the status kinds returned every status. It is
        # a ref-level filter: fold it into the STATUS tag, which the store
        # applies to the parent ref (a ref carries one STATUS value, so an
        # explicit status= replaces any STATUS: tag the caller also passed).
        status = args.get("status")
        status_excluded: list[str] = []
        if isinstance(status, str) and status.strip():
            if isinstance(tags, str):
                tags = [tags]
            only = self.hub.handler_for(kinds[0]) if len(kinds) == 1 else None
            if only is not None and hasattr(only, "_apply_status_default"):
                # One status kind: its own rules (alert-state, aliases, '*').
                tags, _note, status_excluded = only._apply_status_default(status, tags)
            elif status.strip().lower() not in ("*", "any", "all"):
                # Drop caller STATUS: constraints of either polarity —
                # '!STATUS:x' is a STATUS constraint too (gr568354).
                tags = [
                    t
                    for t in (tags or [])
                    if not str(t)
                    .strip()
                    .removeprefix("!")
                    .lstrip()
                    .startswith("STATUS:")
                ]
                tags.append(f"STATUS:{status.strip().lower()}")
            if tags:
                tags = Tag.normalize_filter(tags, kind=None)
        mode = args.get("mode")
        mode_lexical = isinstance(mode, str) and mode.strip().lower() == "lexical"

        # One batch embed covers q + every reformulation + every HyDE
        # answer — the semantic legs fuse them in search_chunks_multi.
        # Degrade contract is unchanged: an Upstream failure marks the
        # semantic leg degraded and the lexical legs still answer.
        query_vecs: list[list[float]] = []
        semantic_degraded = False
        embedder = None if mode_lexical else getattr(self.hub, "embedder", None)
        if embedder is not None and has_q:
            try:
                query_vecs = [
                    v
                    for v in embedder.embed([q, *extra_queries, *hyde_answers])
                    if v is not None
                ]
            except Upstream:
                semantic_degraded = True
            except Exception:
                log.exception("source search: query embed failed; lexical-only")

        exclude_ref_ids = args.get("exclude_ref_ids")
        include_ref_ids = args.get("include_ref_ids")
        exclude_notice = ""
        serves_note = ""
        serves_qhandle: str | None = None
        if args.get("serves") is not None:
            # ``args={'serves': 'quest:<id>'}`` (gr608733): the paper
            # path's serving-quest cohort — resolved into the same
            # include channel the cite facets write, so the ONE
            # underlying query applies it across the kind set. An empty
            # cohort means zero hits, never an unfiltered search.
            from precis.handlers._serves_scope import serves_quest_id

            token = args.pop("serves")
            qid = serves_quest_id(store, token)
            serving = {
                int(ln.src_ref_id)
                for ln in store.links_for(qid, direction="in", relation="serves")
            }
            self._note_channel_value(args, "include_ref_ids")
            self._note_facet_origin(args, "include_ref_ids", "serves", token)
            self._intersect_include_ref_ids(args, serving)
            include_ref_ids = args["include_ref_ids"]
            qhandle = handle_registry.try_format("quest", qid) or f"quest:{qid}"
            serves_qhandle = qhandle
            serves_note = (
                f"_(serves={qhandle}: restricted to {len(serving)} "
                f"serving ref{'s' if len(serving) != 1 else ''})_"
            )
        if args.get("exclude"):
            # ``exclude=`` (gr608576): slug/handle/container entries resolve
            # per requested kind into the same exclude_ref_ids channel
            # uncited= uses, so the one SQL query drops them uniformly.
            resolved_ids, dead_handles = self._resolve_source_exclude(
                args["exclude"], kinds, store
            )
            if resolved_ids:
                exclude_ref_ids = sorted(set(exclude_ref_ids or ()) | resolved_ids)
            if dead_handles:
                n_dead = len(dead_handles)
                listed = ", ".join(repr(h) for h in dead_handles)
                exclude_notice = (
                    f"\n\n⚠ exclude=: {n_dead} handle"
                    f"{'s' if n_dead != 1 else ''} "
                    f"not found, so not excluded: {listed}"
                )
        if not has_q:
            # The queryless filtered listing (td628495 review — a
            # ``paper + since + serves`` call used to die here with
            # "requires q=" before ``serves`` ever resolved): a named
            # cohort — ``serves=``, or the include channel a
            # ``cited=``/``under=`` facet resolved — replaces the ranking
            # query. With no cohort the call is unfilterable and keeps
            # the old refusal.
            if include_ref_ids is None:
                raise BadInput(
                    f"source search ({SOURCE_SEARCH_TRIGGER_LABEL}) requires "
                    "q= — or a named cohort (args={'serves': 'quest:<id>'}) "
                    "for a filtered listing",
                    next=[
                        "search(kind='paper,patent', q='mof co2', sort='recency')",
                        "search(kind='paper', args={'since': '2026-01-01', "
                        "'serves': 'quest:<id>'})",
                    ],
                )
            ranking_only = [
                f"{name}=" for name in ("queries", "answers", "mode") if args.get(name)
            ]
            if ranking_only:
                raise BadInput(
                    f"{', '.join(ranking_only)} rank against q= — the "
                    "filtered listing has no query to rank with",
                    next="add q= to rank the cohort, or drop them for the listing",
                )
            return self._source_filtered_listing(
                kind,
                args,
                store=store,
                kinds=kinds,
                domain_footer=domain_footer,
                since=since,
                until=until,
                sort=sort,
                page=page,
                resume_offset=resume_offset,
                top_k=top_k,
                tags=tags,
                status_excluded=status_excluded,
                include_ref_ids=include_ref_ids,
                exclude_ref_ids=exclude_ref_ids,
                serves_note=serves_note,
                serves_qhandle=serves_qhandle,
                exclude_notice=exclude_notice,
            )
        # ``has_q`` is not a TypeGuard — the door above returned or
        # refused every queryless shape, so q is a non-blank str here.
        assert isinstance(q, str) and q.strip()
        # The pinned identity card (a handle/identifier ``q``) is part of
        # the paginated stream, not decoration on top of it: it occupies
        # row 1 of the head window, so every deeper window's fused offset
        # shifts back one row — otherwise the fetched row it displaces is
        # render-dropped at the head while the next window skips past it
        # for good (td609888). It resolves on every window, not just the
        # head, so that accounting stays consistent; its ref leaves the
        # store query so no window can render it twice (the pin *is* that
        # ref's row).
        handle_hit = self._handle_query_hit(
            q,
            kinds=(None if expanded.strip().lower() in _CROSS_KIND_ALIASES else kinds),
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
        )
        pin_rows = 1 if handle_hit is not None else 0
        fetch_exclude_ref_ids = exclude_ref_ids
        if handle_hit is not None and handle_hit.ref_id is not None:
            fetch_exclude_ref_ids = sorted(
                set(fetch_exclude_ref_ids or ()) | {handle_hit.ref_id}
            )
        # Probe one row past the page so the continuation hint is exact
        # — the same trick the byline/stamp listings run; ``offset``
        # pages the one fused, per-ref-collapsed list. An explicit
        # ``args={'offset': N}`` places the window at an exact result
        # offset (td610130); ``page`` is sugar for the ``top_k``
        # multiples, shifted back one when the pin fills the head
        # window's row 1.
        offset = (
            resume_offset
            if resume_offset is not None
            else max(0, (page - 1) * top_k - pin_rows)
        )
        at_head = resume_offset == 0 if resume_offset is not None else page == 1
        page_rows = top_k - pin_rows if at_head else top_k
        results = store.chunks.search_chunks_across_kinds(
            kinds=kinds,
            q=q,
            q_texts=[q, *extra_queries],
            query_vecs=query_vecs,
            mode=mode,
            tags=tags,
            exclude_tags=status_excluded or None,
            since=since,
            until=until,
            sort=sort,
            limit=page_rows + 1,
            offset=offset,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=fetch_exclude_ref_ids,
            include_ref_ids=include_ref_ids,
        )
        has_more = len(results) > page_rows
        results = results[:page_rows]
        hits: list[SearchHit] = []
        # The pinned card fills row 1 of the head window only — a deeper
        # window neither repeats it nor reserves a row for it.
        if at_head and handle_hit is not None:
            hits.append(handle_hit)
        seen_parents: set[int] = set()
        # Indices into ``results`` that produced no rendered row (a
        # parent-handle collapse, an empty chunk render). The
        # budget_fit's resume offset must count them as consumed — the
        # rendered row count alone would restart mid-span and re-show a
        # result (td610130).
        dropped: list[int] = []
        for idx, (block, ref, score) in enumerate(results):
            before = len(hits)
            for hit in block_hits_to_search_hits([(block, ref, score)], kind=ref.kind):
                # gr495382: gripe/todo/quest comments are chunks, but the
                # thing to act on is the parent (gr123), not the chunk handle
                # of the comment that matched (gc456): return the parent, once.
                if ref.kind in _PARENT_HANDLE_KINDS:
                    if ref.id in seen_parents:
                        continue
                    seen_parents.add(ref.id)
                    parent = handle_registry.try_format(ref.kind, ref.id, chunk=False)
                    if parent is not None:
                        hit = replace(
                            hit, uhandle=parent, dedupe_key=f"{ref.kind}:{ref.id}"
                        )
                hits.append(hit)
            if len(hits) == before:
                dropped.append(idx)

        date_suffix = " in the given date window" if (since or until) else ""
        if semantic_degraded:
            empty_body = (
                f"no lexical matches across {', '.join(kinds)} for {q!r}"
                f"{date_suffix}; semantic search degraded to lexical-only this "
                "turn — retry in ~30s for the full ranked fan-out"
            )
        else:
            empty_body = f"no matches across {', '.join(kinds)} for {q!r}{date_suffix}"
        response = merge_and_render(
            [hits],
            # The pinned card is a page row, not an extra — it fills
            # row 1 of page 1 inside the same ``top_k`` window whose
            # fetched share ``page_rows`` already reserved.
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=empty_body,
        )
        # gr503440: the date-ordered render had no "N of K" cue — the
        # store over-fetches a relevance-qualified pool past top_k and
        # date-sorts it, so a bare "# N matches" hid how much further the
        # query reaches. Count the same lexical universe the relevance
        # "N of K" headers count (``count_chunks_lexical`` — the shared
        # helper, ``distinct_refs`` to match the per-ref collapse), per
        # kind; semantic/verbatim have no honest count here and keep the
        # bare headline.
        if (
            sort != "relevance"
            and hits
            and (mode or "").strip().lower() not in ("semantic", "verbatim")
        ):
            per_kind: list[tuple[str, int]] = []
            total = 0
            for k in kinds:
                n = store.chunks.count_chunks_lexical(
                    q=q,
                    kind=k,
                    tags=tags,
                    exclude_tags=status_excluded or None,
                    since=since,
                    until=until,
                    exclude_ref_ids=exclude_ref_ids,
                    include_ref_ids=include_ref_ids,
                    distinct_refs=True,
                )
                per_kind.append((k, n))
                total += n
            if total:
                shown = response.result_count or 0
                head = f"{shown} of {total}" if total > shown else f"{total} total"
                breakdown = ", ".join(f"{k}: {n}" for k, n in per_kind)
                lines = response.body.splitlines()
                lines.insert(1, f"_({head} · {breakdown})_")
                response = Response(
                    body="\n".join(lines),
                    cost=response.cost,
                    result_count=response.result_count,
                    result_ids=response.result_ids,
                )
        response = _stamp_non_production_embedder(response, embedder)
        if serves_note:
            # Leads the body like the dispatch-level facet notes — and
            # lands inside ``head`` so a budget-fitted page keeps it.
            response = replace(response, body=f"{serves_note}\n\n{response.body}")
        # gr608577: a page that fits still names its continuation, and a
        # page larger than the result budget gets a ``budget_fit`` whose
        # trailer names the exact resume call — every row stays reachable.
        split = _hit_row_bounds(response.body)
        body = response.body
        if has_more:
            # Re-render the call verbatim — dropping
            # queries=/answers=/exclude=/since= would silently change the
            # ordering and window (same contract as the paper broad path's
            # continuation hint). The continuation keeps the caller's own
            # window spelling: ``args={'page': N+1}`` for a page window,
            # ``args={'offset': M}`` for an offset window — both survive
            # the MCP boundary inside ``args={}`` (gr608577, td610130).
            from precis.utils.next_block import render_next_section

            cont_label = (
                f"see the next {top_k} matches (offset {offset + page_rows})"
                if resume_offset is not None
                else f"see the next {top_k} matches (page {page + 1})"
            )
            body += render_next_section(
                [
                    (
                        self._source_search_page_call(
                            kind,
                            args,
                            page_size=top_k,
                            page=None if resume_offset is not None else page + 1,
                            offset=(
                                offset + page_rows
                                if resume_offset is not None
                                else None
                            ),
                        ),
                        cont_label,
                    )
                ]
            )
        # The post-split notices are not hit rows, so they ride into the
        # fit as the trailer it re-appends past the cut — a budget-fitted
        # page keeps the exclude= dead-handle warning and the fence's
        # skipped-kinds footer + door (td611710 review: the appends used
        # to land outside the fitted render and were dropped).
        trailer = exclude_notice
        if domain_footer is not None:
            trailer += f"\n_({domain_footer})_"
        body += trailer
        return replace(
            response,
            body=body,
            budget_fit=(
                self._source_search_fit(
                    kind,
                    args,
                    body=body,
                    head=split[0],
                    rows=split[1],
                    offset=offset,
                    page_size=top_k,
                    has_more=has_more,
                    pinned=at_head and handle_hit is not None,
                    dropped=dropped,
                    trailer=trailer,
                )
                if split is not None
                else None
            ),
        )

    def _source_filtered_listing(
        self,
        kind: str | None,
        args: dict[str, Any],
        *,
        store: Any,
        kinds: list[str],
        domain_footer: str | None,
        since: datetime | None,
        until: datetime | None,
        sort: str,
        page: int,
        resume_offset: int | None,
        top_k: int,
        tags: list[str] | None,
        status_excluded: list[str],
        include_ref_ids: list[int] | None,
        exclude_ref_ids: list[int] | None,
        serves_note: str,
        serves_qhandle: str | None,
        exclude_notice: str,
    ) -> Response:
        """The queryless filtered listing — a named cohort stands in for q=.

        Reached from :meth:`_dispatch_source_search` when the caller gave
        no ``q`` but the call narrows to an explicit set: ``serves=``
        resolved the quest's serving refs, or a ``cited=``/``under=``
        facet (or a literal ``include_ref_ids``) wrote the include
        channel upstream. There is no match-all chunk leg —
        :meth:`~precis.store._chunks_ops.BlocksMixin.search_chunks_lexical`
        requires ``c.tsv @@ qq.qq`` — so a queryless call can only ever
        enumerate *refs*. This lists the cohort through
        :meth:`~precis.store._refs_ops.RefsMixin.list_refs` (live refs
        only), bounded by the same ``since=``/``until=``/``tags=``/
        ``status=``/``exclude=`` filters, ordered by ``sort`` (a date
        order is the only honest one with no query to rank against —
        ``relevance`` reads as ``created_desc`` here), and paged by the
        same ``page=``/``offset=`` primitive as the ranked path. Rows are
        ref-level hits in the same ``## N.`` shape, so
        :func:`_hit_row_bounds`, ``budget_fit`` and the continuation
        replay all work unchanged. An empty cohort (the include channel
        is ``[]``) lists zero refs — it never falls through unfiltered,
        the same contract the ranked path gives ``serves=``.
        """
        order_by = {
            "modified": "updated_desc",
            "oldest": "created_asc",
        }.get(sort, "created_desc")
        offset = resume_offset if resume_offset is not None else (page - 1) * top_k
        refs = store.list_refs(
            kind=kinds,
            tags=tags,
            exclude_tags=status_excluded or None,
            created_after=since,
            created_before=until,
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
            order_by=order_by,
            limit=top_k + 1,
            offset=offset,
        )
        has_more = len(refs) > top_k
        hits: list[SearchHit] = []
        for ref in refs[:top_k]:
            hits.extend(ref_hits_to_search_hits([(ref, 1.0)], kind=ref.kind))
        date_suffix = " in the given date window" if (since or until) else ""
        response = merge_and_render(
            [hits],
            page_size=top_k,
            query=(
                f"refs serving {serves_qhandle}"
                if serves_qhandle is not None
                else "the filtered cohort"
            ),
            header_noun="match",
            mode="rrf",
            empty_body=(
                f"no matches across {', '.join(kinds)} for the filtered "
                f"cohort{date_suffix}"
            ),
        )
        # The "N of K" line is exact here — ``count_refs`` shares every
        # filter, not the ranked path's lexical-lower-bound estimate.
        per_kind: list[tuple[str, int]] = []
        total = 0
        for k in kinds:
            n = store.count_refs(
                kind=k,
                tags=tags,
                exclude_tags=status_excluded or None,
                created_after=since,
                created_before=until,
                include_ref_ids=include_ref_ids,
                exclude_ref_ids=exclude_ref_ids,
            )
            per_kind.append((k, n))
            total += n
        if total and hits:
            shown = response.result_count or 0
            head = f"{shown} of {total}" if total > shown else f"{total} total"
            breakdown = ", ".join(f"{k}: {n}" for k, n in per_kind)
            lines = response.body.splitlines()
            lines.insert(1, f"_({head} · {breakdown})_")
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )
        if serves_note:
            response = replace(response, body=f"{serves_note}\n\n{response.body}")
        split = _hit_row_bounds(response.body)
        body = response.body
        if has_more:
            # Same continuation contract as the ranked path — the
            # replay keeps the caller's own window spelling and every
            # resolved filter re-renders in its own spelling (serves=,
            # uncited=, …) via _search_call_text.
            from precis.utils.next_block import render_next_section

            cont_label = (
                f"see the next {top_k} matches (offset {offset + top_k})"
                if resume_offset is not None
                else f"see the next {top_k} matches (page {page + 1})"
            )
            body += render_next_section(
                [
                    (
                        self._source_search_page_call(
                            kind,
                            args,
                            page_size=top_k,
                            page=None if resume_offset is not None else page + 1,
                            offset=(
                                offset + top_k if resume_offset is not None else None
                            ),
                        ),
                        cont_label,
                    )
                ]
            )
        trailer = exclude_notice
        if domain_footer is not None:
            trailer += f"\n_({domain_footer})_"
        body += trailer
        return replace(
            response,
            body=body,
            budget_fit=(
                self._source_search_fit(
                    kind,
                    args,
                    body=body,
                    head=split[0],
                    rows=split[1],
                    offset=offset,
                    page_size=top_k,
                    has_more=has_more,
                    pinned=False,
                    dropped=[],
                    trailer=trailer,
                )
                if split is not None
                else None
            ),
        )

    def _source_search_page_call(
        self,
        kind: str | None,
        args: dict[str, Any],
        *,
        page_size: int | None,
        page: int | None = None,
        offset: int | None = None,
    ) -> str:
        """The ``args={'page': N}``/``args={'offset': N}`` continuation
        spelling (gr608577, td610130).

        Replays every caller arg verbatim via ``_search_call_text`` (the
        resolved ``uncited=``/``hubbed=`` facets re-render in their own
        spelling); the window rides inside ``args={}`` — the shape that
        survives the MCP boundary, where ``page``/``offset`` are not
        advertised params — and ``page_size`` overrides so a resume call
        names the size that actually fit. ``offset`` is the exact
        primitive: it places the window at any result-row boundary,
        which a mid-page cut needs and ``page`` cannot express. A
        first-window continuation (``offset=0``/``page=1``) renders no
        window arg at all — both are the wrapper's own defaults.
        """
        replay = dict(args)
        replay["page"] = None
        replay["offset"] = None
        if page_size is not None:
            replay["page_size"] = page_size
        extras = dict(replay.get("__extras__") or {})
        extras.pop("page", None)
        extras.pop("offset", None)
        if offset:
            extras["offset"] = offset
        elif page and page > 1:
            extras["page"] = page
        replay["__extras__"] = extras
        return _search_call_text(kind, replay, set())

    def _source_search_fit(
        self,
        kind: str | None,
        args: dict[str, Any],
        *,
        body: str,
        head: str,
        rows: list[str],
        offset: int,
        page_size: int,
        has_more: bool,
        pinned: bool,
        dropped: list[int],
        trailer: str,
    ) -> Callable[[int], str]:
        """The ``budget_fit`` for an over-cap source-search page.

        Cuts at whole hit rows; the tail names the resume call that
        continues the same ordering where the cut fell — ``args=
        {'offset': <M>}`` places the window at the exact underlying
        result offset, ``page_size`` keeps the caller's own (gr608577:
        the generic line-fit left the unshown rows unreachable;
        td610130: the page*size arithmetic could not express a mid-page
        offset at all). ``offset`` counts consumed *results*, not
        rendered rows — the pinned identity card is a rendered
        head-window row but consumes no result slot (its ref never
        reaches ``results``), while a result whose row was suppressed
        (a parent-handle collapse — the ``dropped`` indices) still
        consumes its slot: a resume that re-fetched it would re-show
        the last displayed row.

        ``trailer`` is the post-split notices (the ``exclude=``
        dead-handle warning, the domain fence's skipped-kinds footer +
        door — td611710): required reading, not hit rows, so the tail
        re-appends it past the cut and the row fit counts it against
        the cap.
        """
        from precis._result_budget import body_bytes

        total = body_bytes(body)
        pin_rows = 1 if pinned else 0

        def consumed(k: int) -> int:
            # ``results`` entries the first ``k`` rendered rows account
            # for: the shown result rows, plus every suppressed index
            # interleaved inside that span (fixpoint — each drop inside
            # the span pulls it one wider).
            shown_results = max(0, k - pin_rows)
            m = shown_results
            while True:
                wider = shown_results + sum(1 for j in dropped if j < m)
                if wider == m:
                    return m
                m = wider

        def tail(k: int) -> str:
            if k >= len(rows) and not has_more:
                return trailer
            shown = body_bytes(head + "".join(rows[:k]))
            return (
                f"\n\n… truncated: showing {shown} of {total} bytes\nnext: "
                + self._source_search_page_call(
                    kind,
                    args,
                    page_size=page_size,
                    offset=offset + consumed(k),
                )
                + trailer
            )

        def fit(cap: int) -> str:
            if total <= cap:
                return body
            lo, hi = 0, len(rows)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if body_bytes(head + "".join(rows[:mid]) + tail(mid)) <= cap:
                    lo = mid
                else:
                    hi = mid - 1
            if lo == 0:
                raise ResultBudgetExceeded(
                    f"search: result is {total} bytes and even one hit row "
                    f"does not fit under the {cap}-byte result budget",
                    next=self._source_search_page_call(
                        kind, args, page_size=5, offset=offset
                    ),
                )
            return head + "".join(rows[:lo]) + tail(lo)

        return fit

    @staticmethod
    def _str_list(value: Any) -> list[str]:
        """Coerce a list-or-scalar arg to a list of non-blank strings."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [s for s in (str(v).strip() for v in value) if s]

    def _resolve_source_exclude(
        self, entries: Any, kinds: list[str], store: Any
    ) -> tuple[set[int], list[str]]:
        """``exclude=`` on a source search → ref_ids to drop + dead handles.

        Each entry resolves under the semantics of the kind it names —
        the same grammar that kind's own ``search_hits`` applies on the
        per-handler fan-out:

        * paper-family kinds (``PaperHandler`` and its subclasses —
          cfp/datasheet) use
          :func:`precis.handlers._exclude_closure.resolve_exclude_paper_ids`,
          which covers bare slugs, DOIs, record/chunk handles and the
          ``dr…``/``dc…`` draft-container cite closures;
        * every other kind resolves a universal handle to its ref (the
          handle already encodes the ref's kind) and a bare entry as a
          slug via ``Store.fetch_ref_ids_by_slugs``.

        Dead handle entries are returned separately so the caller can
        surface the same ``⚠ exclude=`` notice the paper path renders
        (gr340059) — a named id that resolves nowhere must not silently
        skip.
        """
        from precis.handlers._exclude_closure import resolve_exclude_paper_ids

        normalized = self._str_list(entries)
        ids: set[int] = set()
        dead: list[str] = []
        other_kinds: list[str] = []
        for k in kinds:
            handler = self.hub.handler_for(k)
            mro = {c.__name__ for c in type(handler).__mro__} if handler else set()
            if "PaperHandler" in mro:
                resolved = resolve_exclude_paper_ids(normalized, store=store, kind=k)
                ids.update(int(i) for i in resolved)
                dead.extend(resolved.dead_handles)
            else:
                other_kinds.append(k)
        if other_kinds:
            slug_entries: list[str] = []
            for entry in normalized:
                if handle_registry.parse(entry) is not None:
                    resolved_handle = store.resolve_handle(entry)
                    if resolved_handle is not None:
                        if resolved_handle.kind in other_kinds:
                            ids.add(resolved_handle.ref_id)
                    else:
                        dead.append(entry)
                else:
                    slug_entries.append(entry)
            for k in other_kinds:
                ids.update(
                    int(i) for i in store.fetch_ref_ids_by_slugs(slug_entries, kind=k)
                )
        return ids, dead

    def _kind_search_takes(
        self, kind: str, args: dict[str, Any], drop: set[str]
    ) -> bool:
        """Whether ``search`` minus ``drop`` lands wholly on ``kind``'s
        own search — every remaining top-level key an accepted kwarg,
        every ``args={…}`` key a declared ``args`` key or kwarg.

        Gates the "drop the interception trigger" ``next=`` hints: the
        rewrite claims the kind's own search applies the keys, so it is
        only offered when it provably does — an unverifiable hint
        violates the ``next=``-that-works rule (mcp-authoring rule 2).
        """
        from precis.runtime.dispatch import (
            _WRAPPER_DEFAULTED_KWARGS,
            _handler_accepted_kwargs,
        )

        handler: Handler | None = self.hub.handler_for(self._expand_kind_code(kind))
        if handler is None:
            return False
        accepted = _handler_accepted_kwargs(type(handler), "search") | (
            _WRAPPER_DEFAULTED_KWARGS.get("search") or frozenset()
        )
        extras_keys = (getattr(type(handler), "ARGS_KEYS", None) or {}).get(
            "search"
        ) or frozenset()
        extras = args.get("__extras__")
        for k, v in args.items():
            # ``__``-prefixed keys are dispatcher-internal sentinels
            # (``__extras__``, the resolved-facets marker), never caller
            # args — they don't reach the handler kwargs gate.
            if k in drop or v is None or k.startswith("__"):
                continue
            if k not in accepted:
                return False
        if isinstance(extras, dict):
            for k, v in extras.items():
                if k in drop or v is None:
                    continue
                if k not in extras_keys and k not in accepted:
                    return False
        return True

    def _fan_out_next(
        self, kind: str, args: dict[str, Any], strays: list[str]
    ) -> list[str]:
        """The corrected call(s) for a refused fan-out/tags-only call.

        ``next=`` must be a call that works (mcp-authoring rule 2), and
        "narrow to one kind" is wrong when the caller already named one —
        ``folder=`` routes a single-kind call through here. Two working
        recoveries, built from the actual call: drop the refused keys
        (the interception still applies what's left), or drop the
        interception trigger (``folder=``/``view='keywords'``) so the
        kind's own search sees them — the second only when the kind's
        search actually accepts the refused keys.
        """
        if self._is_cross_kind_request(kind):
            return [
                "narrow to one kind for per-kind filters: "
                "search(kind='<one kind>', q='…', <key>=…); a kind's own "
                "args= keys apply there too"
            ]
        hints = ["drop the refused keys: " + _search_call_text(kind, args, set(strays))]
        for trigger in ("folder", "view"):
            if args.get(trigger) is not None and self._kind_search_takes(
                kind, args, {trigger}
            ):
                hints.append(
                    f"or drop {trigger}= so kind={kind!r}'s own search "
                    f"applies them: " + _search_call_text(kind, args, {trigger})
                )
        return hints

    def _source_search_next(
        self, kind: str | None, args: dict[str, Any], strays: list[str]
    ) -> list[str]:
        """The corrected call(s) for a refused source-search call.

        Same contract as :meth:`_fan_out_next`: the triggers here are
        ``sort=``/``since=``/``until=`` — dropping them returns the call
        to the kind's own search path, which may consume the refused
        keys.
        """
        if kind is None or self._is_cross_kind_request(kind):
            return [
                "narrow to one kind for per-kind filters: "
                "search(kind='<one kind>', q='…', <key>=…); a kind's own "
                "args= keys apply there too"
            ]
        hints = ["drop the refused keys: " + _search_call_text(kind, args, set(strays))]
        triggers = {t for t in ("sort", "since", "until") if args.get(t) is not None}
        if triggers:
            names = "/".join(f"{t}=" for t in sorted(triggers))
            hints.append(
                f"or drop {names} so kind={kind!r}'s own search applies "
                f"them: " + _search_call_text(kind, args, triggers)
            )
        return hints

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        """Fan out a search across multiple kinds and RRF-fuse the streams.

        Each handler's ``search_hits(q=..., page_size=...)`` is called
        with the same arguments; per-handler exceptions degrade to
        empty streams (logged) so one slow / broken kind doesn't
        crash the whole query.  Final ranking is reciprocal-rank
        fusion via ``merge_and_render(mode='rrf')``.
        """
        q = args.get("q")
        tags_in = args.get("tags")
        # Tags-only cross-kind path (R2#9 — finding "every throwaway
        # across kinds" used to force 4 single-kind calls). The lexical
        # leg doesn't need an embedder, so we can answer this in one
        # store query — list_refs accepts a kind=None tag filter and
        # returns a kind-mixed result set the renderer trivially flattens.
        # ``_dispatch_cross_kind_tags_only`` has no exclude_ref_ids/
        # include_ref_ids wiring at all (``list_refs`` isn't a ranked chunk
        # search), so a ``uncited=``/``cited=`` resolved into
        # ``args['exclude_ref_ids']``/``args['include_ref_ids']`` above
        # would silently ride through unfiltered — raise instead of
        # falling through to that path.
        queryless = q is None or not (isinstance(q, str) and q.strip())
        if queryless:
            if args.get("exclude_ref_ids") is not None:
                raise BadInput(
                    "uncited= requires q= (it filters a ranked search, not "
                    "the tags-only sweep)",
                    next=f"search(kind={kind!r}, q='your query', args={{'uncited': '…'}})",
                )
            if args.get("include_ref_ids") is not None:
                raise BadInput(
                    "cited= requires q= (it filters a ranked search, not "
                    "the tags-only sweep)",
                    next=f"search(kind={kind!r}, q='your query', args={{'cited': '…'}})",
                )
            if not tags_in:
                # The missing filter is the first problem, not any stray
                # keys — refusing them here would mislabel the call "a
                # tags-only sweep" and offer a drop-keys hint whose
                # rewritten call still has no q=/tags= to apply.
                single_kind_folder = (
                    not self._is_cross_kind_request(kind)
                    and args.get("folder") is not None
                )
                if single_kind_folder:
                    # ``folder=`` routed a named single kind through the
                    # fan-out — name that shape, and keep ``folder=`` in
                    # the corrected call (the fan-out applies it). Other
                    # supplied keys stay dropped: only each shape's
                    # consumed set survives its gate.
                    fan_out_args = {
                        k: v
                        for k, v in args.items()
                        if k in _FAN_OUT_CONSUMED and k not in {"q", "tags"}
                    }
                    sweep_args = {
                        k: v
                        for k, v in args.items()
                        if k in _TAGS_ONLY_CONSUMED and k not in {"q", "tags"}
                    }
                    message = "folder-scoped search requires q= or tags="
                    nexts = [
                        _search_call_text(
                            kind, {**fan_out_args, "q": "your query"}, set()
                        )
                        + " - the folder scope post-filters a fan-out via "
                        "search_hits, which needs a non-empty query; or "
                        + _search_call_text(
                            kind, {**sweep_args, "tags": ["<tag>"]}, set()
                        )
                        + " for a tags-only sweep"
                    ]
                else:
                    message = "cross-kind search requires q= or tags="
                    nexts = [
                        f"search(kind={kind!r}, q='your query') - cross-kind merge "
                        "fans out via search_hits, which needs a non-empty query; "
                        f"or search(kind={kind!r}, tags=['<tag>']) for a tags-only sweep"
                    ]
                if single_kind_folder and self._kind_search_takes(
                    kind, args, {"folder"}
                ):
                    nexts.append(
                        f"or drop folder= so kind={kind!r}'s own search "
                        "applies the filters: "
                        + _search_call_text(kind, args, {"folder"})
                    )
                raise BadInput(message, next=nexts)
            # ``n``/``top_k`` are the sweep's page cap (read in
            # ``_dispatch_cross_kind_tags_only``), not a kind's args=
            # key — nested they died at the unapplied-args gate below
            # as ``args['top_k']``, which is the only spelling an MCP
            # caller has (the verb exposes ``n=`` but not ``top_k=``).
            # Hoist like the spray knobs; a key at both levels stays
            # nested and still refuses.
            extras = args.get("__extras__")
            if isinstance(extras, dict):
                for key in ("n", "top_k"):
                    if extras.get(key) is not None and args.get(key) is None:
                        args[key] = extras.pop(key)
        # ``args={'domain': …}`` is the per-call domain override the
        # skipped-kinds footer names (gr572720) — hoist it beside the
        # sweep's ``n``/``top_k`` so the gate sees a consumed key, not a
        # stray. The fence below resolves it per call.
        extras = args.get("__extras__")
        if (
            isinstance(extras, dict)
            and extras.get("domain") is not None
            and args.get("domain") is None
        ):
            args["domain"] = extras.pop("domain")
        refuse_unapplied_args(
            args,
            consumed=(
                _TAGS_ONLY_CONSUMED
                if queryless
                else (
                    _FAN_OUT_CONSUMED
                    | ({"view"} if args.get("view") == "keywords" else set())
                )
            ),
            shape=(
                "a tags-only cross-kind sweep" if queryless else "a cross-kind search"
            ),
            next=lambda strays: self._fan_out_next(kind, args, strays),
            tolerated=_PAGE_TOLERATED,
        )
        if queryless:
            return self._dispatch_cross_kind_tags_only(kind, args)
        assert isinstance(q, str)  # non-blank — queryless returned/raised above
        top_k = int(args.get("page_size") or args.get("top_k") or 10)
        tags = args.get("tags")
        exclude = args.get("exclude")
        mode = args.get("mode")
        mode_lexical = isinstance(mode, str) and mode.strip().lower() == "lexical"

        # ``folder=`` scope: resolve the folder's live
        # placement subtree once; hits outside it are dropped after
        # each kind's stream returns (handlers stay scope-blind — the
        # SearchHit.ref_id is enough to filter at the dispatch layer).
        folder_scope: set[int] | None = None
        folder_label: str | None = None
        if args.get("folder") is not None:
            folder_scope, folder_label = self._resolve_folder_scope(args["folder"])

        kinds = self._resolve_cross_kind_request(kind)
        if not kinds:
            raise BadInput(
                "no kinds available for cross-kind search",
                next=(
                    "this build has no kinds that opt into cross-kind merge; "
                    "use single-kind search() against the kind you want"
                ),
            )
        # Domain fence (gr572720): the default expansion — omitted kind
        # or a wildcard alias — intersects the resolved domain's kinds;
        # an explicit kind=/comma list outside the set still runs.
        kinds, domain_footer = self._domain_fan_out_fence(kind, kinds, args)

        # ``uncited=``/``cited=`` (resolved by ``_dispatch_inner`` into
        # ``args['exclude_ref_ids']``/``args['include_ref_ids']``): every
        # per-handler ``search_hits`` accepts arbitrary kwargs (``**_kw``),
        # so an unsupported kind would otherwise silently swallow the
        # filter and contribute unfiltered (possibly already-cited, or
        # not-actually-cited) hits — the failure mode the feature must
        # never produce. Kinds in ``_UNCITED_UNSUPPORTED_KINDS`` have no
        # real SQL-level exclude/include wiring: an EXPLICIT request for
        # one of them raises; the default wildcard fan-out instead drops
        # them (footer-noted below) so the common unscoped
        # ``search(q=..., uncited=...)``/``cited=...`` call still works.
        exclude_ref_ids = args.get("exclude_ref_ids")
        include_ref_ids = args.get("include_ref_ids")
        # Name every facet that is live, not just the first — a drop caused
        # by the exclusion half of a ``cited=`` + ``exclude=`` call must not
        # read as a ``cited=`` problem.
        _facets = [
            label
            for label, live in (
                ("cited=", include_ref_ids is not None),
                ("uncited=", exclude_ref_ids is not None),
            )
            if live
        ]
        facet_label = "/".join(_facets) if _facets else None
        uncited_dropped_kinds: list[str] = []
        if facet_label is not None:
            unsupported_present = [k for k in kinds if k in _UNCITED_UNSUPPORTED_KINDS]
            if unsupported_present:
                if kind.strip().lower() in _CROSS_KIND_ALIASES:
                    kinds = [k for k in kinds if k not in _UNCITED_UNSUPPORTED_KINDS]
                    uncited_dropped_kinds = unsupported_present
                    if not kinds:
                        raise Unsupported(
                            f"{facet_label} leaves no searchable kinds — every "
                            f"eligible kind {unsupported_present!r} lacks "
                            "exclude/include-by-ref_id wiring",
                            next=(
                                f"use {SOURCE_SEARCH_TRIGGER_LABEL} (the "
                                "cross-kind source-search primitive), which "
                                "excludes uniformly across every kind"
                            ),
                        )
                else:
                    raise Unsupported(
                        f"{facet_label} is not supported for kind(s) "
                        f"{unsupported_present!r} — no exclude/include-by-ref_id "
                        "wiring yet",
                        next=(
                            "drop these kinds from the comma-list, or use "
                            f"{SOURCE_SEARCH_TRIGGER_LABEL} (the cross-kind "
                            "source-search primitive) which excludes "
                            "uniformly across every kind"
                        ),
                    )

        # Canonicalise the tag filter once at the dispatch boundary.
        # ``Tag.normalize_filter`` round-trips each tag through
        # ``parse_strict`` (validates vocabulary, rejects typos) and
        # returns its canonical string form so the post-filter below
        # can match by string equality regardless of namespace.
        # ``kind=None`` because cross-kind doesn't know which axes
        # apply where — per-kind axis enforcement lives on writes.
        normalized_tags: list[str] | None = None
        if tags:
            normalized_tags = Tag.normalize_filter(tags, kind=None)

        # Build the kwargs dict once so the per-kind retry chain
        # below can drop unknown kwargs without re-listing them.
        # ``exclude=`` is fanned out to every kind (the per-handler
        # ``fetch_ref_ids_by_slugs`` filters by kind, so a paper
        # slug in the list silently no-ops on memory etc.). Kinds
        # that don't accept the kwarg fall through to the
        # ``TypeError`` retry below.
        #
        # ``query_vec=`` is computed once here and threaded into every
        # block-level handler that opts in. Without this the cross-
        # kind fan-out paid one embed_one(q) per kind — for kind='*'
        # over seven block-level handlers that's seven identical
        # transformer forward passes on the same query string. Kinds
        # whose ``search_hits`` signature doesn't accept ``query_vec=``
        # fall through the same TypeError-degradation chain as
        # ``exclude=`` / ``tags=``.
        # ``page_size`` is the name every handler's ``search_hits`` takes;
        # passing ``top_k`` was swallowed by ``**_kw`` so each kind returned
        # its default 10 whatever the caller asked for (gr495190).
        base_kwargs: dict[str, Any] = {"q": q, "page_size": top_k}
        if mode is not None:
            base_kwargs["mode"] = mode
        semantic_degraded = False
        # ``mode='lexical'`` skips the embed entirely — the deterministic
        # keyword fan-out (and the right move when the embedder is down).
        embedder = None if mode_lexical else getattr(self.hub, "embedder", None)
        # The default expansion — omitted kind or a wildcard alias — is
        # where up-front leg skips apply (gr628278, same gate the domain
        # fence and the uncited= drop use); an explicit kind=/comma
        # list is caller intent and keeps every leg.
        default_expansion = kind.strip().lower() in _CROSS_KIND_ALIASES
        # gr628278: a non-production embedder's vector is deterministic
        # hash noise — its semantic leg can only mislead (the marker at
        # the end of this method already tells the caller so), yet every
        # kind still pays the ANN scan the 15 s tool budget can't absorb
        # (the anki/paper QueryCanceled stragglers that blew it). Skip
        # the leg up front: force lexical for the whole fan-out and
        # don't embed at all. An explicit ``mode='semantic'`` still
        # runs it — the caller asked for the noise knowingly.
        mock_leg_skipped = False
        _hybridish = mode is None or str(mode).strip().lower() == "hybrid"
        if (
            default_expansion
            and _hybridish
            and embedder is not None
            and not getattr(embedder, "is_production", True)
        ):
            base_kwargs["mode"] = "lexical"
            mock_leg_skipped = True
            query_embedder = None
        else:
            query_embedder = embedder
        if query_embedder is not None:
            try:
                base_kwargs["query_vec"] = query_embedder.embed_one(q)
            except Upstream as exc:
                # Embedder is warming (or upstream is unavailable).
                # Falling back to lexical-only is the right runtime
                # move, but we must SURFACE the degraded state so the
                # agent doesn't read "no matches" as a definitive
                # answer. Round-2 picky finding R2#2: silent fallback
                # made `search(q='photocatalysis')` return zero hits
                # while `search(kind='paper', q='...')` raised
                # Upstream — same underlying state, two different
                # signals.
                from precis.hints import Hint

                self.hub.emit_hint(
                    Hint(
                        text=(
                            f"cross-kind semantic search degraded to "
                            f"lexical-only: {exc.cause}. Some matches "
                            "may be missing; retry shortly for the "
                            "full semantic fan-out."
                        ),
                        topic="search.embedder_warming",
                        cooldown=3,
                    )
                )
                log.info(
                    "cross-kind: embedder unavailable; falling back to lexical (%s)",
                    exc.cause,
                )
                semantic_degraded = True
            except Exception:
                # An embed failure here shouldn't kill the whole
                # cross-kind search — fall back to per-kind embed
                # (or lex-only when the kind's embedder is also
                # unavailable).
                log.exception("cross-kind: query embed failed; falling back per-kind")
        if tags:
            base_kwargs["tags"] = tags
        if exclude:
            base_kwargs["exclude"] = exclude
        if exclude_ref_ids:
            # Real SQL-level exclusion for the kinds that support it
            # (paper/cfp/datasheet — the ``PaperHandler`` family); every
            # other kind's ``search_hits`` accepts and ignores this via
            # ``**_kw``, which is safe (never a false match) since ref_ids
            # are globally unique and the exclusion set only ever contains
            # citeable-kind refs (see ``_UNCITED_UNSUPPORTED_KINDS`` above
            # for the two exceptions, already excluded from ``kinds``).
            base_kwargs["exclude_ref_ids"] = exclude_ref_ids
        if include_ref_ids is not None:
            # ``is not None`` (not truthy): an empty ``include_ref_ids``
            # means "restricted to zero sources", which must fan out as
            # zero hits from every wired kind, not fall through unfiltered
            # like an empty ``exclude_ref_ids`` correctly would.
            base_kwargs["include_ref_ids"] = include_ref_ids

        # gr628278 (cont.): on the default wildcard expansion, a kind
        # whose ANN leg can only route over the paper-dominated global
        # index — its partial index missing, invalid or fill-incomplete —
        # faces the 10-30 s post-filter scan, so inside the deadline it
        # can only die at the per-kind statement cap and land in the
        # errored footer anyway. Skip that leg up front: the kind still
        # contributes its lexical hits, the footer below names the skip,
        # and an explicit kind list keeps the full legs (caller intent).
        # td649559: the probe and the fan-out share one call deadline —
        # the probe's checkout and gate reads draw from a capped slice
        # of it (one absolute probe deadline — the checkout wait, the
        # statement cap and the cancel watchdog all measure against the
        # same instant, gr664970) instead of running unbounded before
        # the fan-out's clock starts.
        route_skipped: list[str] = []
        deadline_at = time.monotonic() + CROSS_KIND_DEADLINE_S
        if (
            not mock_leg_skipped
            and "query_vec" in base_kwargs
            and _hybridish
            and default_expansion
            and self.hub.store is not None
        ):
            route_skipped = self._cross_kind_slow_semantic_kinds(kinds, deadline_at)
        kind_overrides = (
            {k: {"mode": "lexical"} for k in route_skipped} if route_skipped else None
        )

        streams: list[list[SearchHit]] = []
        per_kind_counts: list[tuple[str, int]] = []
        errored: dict[str, Exception] = {}
        # gr664954: on the default expansion (same gate as the up-front
        # skip — an explicit kind list is caller intent), a kind a
        # worker can only reach inside the deadline's last slice runs
        # lexical-only instead of timing out. The set collects those
        # kinds so the footer below can name the degrade.
        lexical_fallback: set[str] | None = set() if default_expansion else None
        fanned = self._cross_kind_fan_out(
            kinds,
            base_kwargs,
            kind_overrides,
            deadline_at=deadline_at,
            lexical_fallback=lexical_fallback,
        )
        for k in kinds:
            if k not in fanned:
                per_kind_counts.append((k, 0))
                continue
            hits_or_exc = fanned[k]
            if isinstance(hits_or_exc, Exception):
                errored[k] = hits_or_exc
                per_kind_counts.append((k, 0))
                continue
            hits = hits_or_exc
            # Defensive post-filter: handler search_hits
            # implementations have inconsistent ``tags=`` support
            # (numeric-ref kinds honour the filter via
            # ``Tag.normalize_filter`` + SQL; most slug-ref and
            # block-level handlers accept it via ``**_kw`` and
            # silently ignore it). The dispatcher re-applies the
            # filter here so a caller who passed
            # ``tags=['workspace']`` never sees cross-kind hits
            # from kinds that can't carry that tag.
            if normalized_tags:
                hits = self._filter_hits_by_tags(list(hits), normalized_tags)
            # ``folder=`` subtree filter: a hit without a ref_id can't
            # prove membership, so it drops when a scope is set.
            if folder_scope is not None:
                hits = [
                    h for h in hits if h.ref_id is not None and h.ref_id in folder_scope
                ]
            hits_list = flag_title_matches(list(hits), q)
            per_kind_counts.append((k, len(hits_list)))
            streams.append(hits_list)

        # Total failure must be loud: when every handler-bearing kind
        # errored, "0 matches" is a false negative — during the
        # gr281493 schema-drift outage the fan-out attested quiet while
        # every single-kind call raised, misleading anyone using it as
        # a health check. (``streams`` gets an entry for every kind
        # that *ran*, even with zero hits, so empty ``streams`` +
        # non-empty ``errored`` ⇒ nothing ran successfully.) This check
        # runs BEFORE the pinned identity card is inserted — a lone pin
        # on top of an all-errored fan-out must not read as a normal
        # result.
        if errored and not streams:
            names = ", ".join(
                f"{k} ({type(e).__name__})" for k, e in sorted(errored.items())
            )
            raise Internal(
                f"cross-kind search failed in every kind tried — {names} "
                "(see server log)"
            ) from next(iter(errored.values()))

        # gr517806: a ``q`` that is itself a universal handle resolves to
        # a pinned identity card ahead of every lexical/semantic hit —
        # a chunk handle shows its containing ref's card with that
        # chunk's text. The cited=/uncited= ref-id facets still apply to
        # it; tags=/folder= do not (a handle is an address, not a match).
        handle_hit = self._handle_query_hit(
            q,
            kinds=None if kind.strip().lower() in _CROSS_KIND_ALIASES else kinds,
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
        )
        if handle_hit is not None:
            streams.insert(0, [handle_hit])

        # gr513663: when at least half the fanned kinds timed out or
        # errored, a confident "# N matches" (or worse, "no matches")
        # reads as an answer when most of the corpus was never searched —
        # the 2026-10-24 fan-out collapse hid existing material from three
        # sessions this way. The result must lead with the incompleteness,
        # not the ranking.
        attempted = len(fanned)
        majority_failed = attempted > 0 and len(errored) * 2 >= attempted

        # When the embedder was unavailable for this turn, change the
        # empty-result wording from "no matches" to a partial-result
        # headline — semantic side genuinely couldn't run, so claiming
        # zero matches is overconfident. Broad-pass finding #13.
        scope_suffix = f" in folder {folder_label}" if folder_label else ""
        if majority_failed:
            empty_body = (
                f"# ⚠ search incomplete — {len(errored)} of {attempted} kinds "
                f"timed out or errored; nothing matched {q!r}{scope_suffix} in "
                f"the {attempted - len(errored)} kinds that answered. "
                "Retry, or narrow by kind "
                f"(search(kind='quest,todo,memory', q={q!r}), …)."
                + (
                    " Semantic leg also degraded to lexical-only this turn."
                    if semantic_degraded
                    else ""
                )
            )
        elif semantic_degraded:
            empty_body = (
                f"no lexical matches across {', '.join(kinds)} for "
                f"{q!r}{scope_suffix}; semantic search degraded to "
                "lexical-only this turn — retry in ~30s for ranked "
                "semantic fan-out"
            )
        else:
            empty_body = f"no matches across {', '.join(kinds)} for {q!r}{scope_suffix}"
        # ``view='keywords'`` swaps the renderer for a compact
        # id|kind|keywords TOON table — no preview text. Same fan-out
        # / dedup / RRF; only the projection differs.
        view = str(args.get("view") or "").strip()
        output_shape: Literal["keywords", "toon"] = (
            "keywords" if view == "keywords" else "toon"
        )
        # Change A: batch the ref-to-ref link summary across every stream
        # so the cross-kind TOON table's ``links`` column doesn't need a
        # per-hit query — one round-trip covers the whole rendered page.
        # ``hub.store`` can be None in a store-less deployment (in-memory
        # fixture); degrade to no link data rather than crash.
        ref_ids = sorted({h.ref_id for s in streams for h in s if h.ref_id is not None})
        link_summary = (
            self.hub.store.link_rel_summary_for_refs(ref_ids)
            if ref_ids and self.hub.store is not None
            else {}
        )
        response = merge_and_render(
            streams,
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=empty_body,
            output_shape=output_shape,
            link_summary=link_summary,
        )

        # Round-2 picky F-8: prepend a per-kind hit-count line under the
        # headline so the agent can see which kinds contributed and
        # which returned empty. Without this, a comma-list call
        # ``search(kind='paper,memory', q='...')`` that surfaces only
        # paper hits looks identical to a single-kind paper call —
        # the agent has no way to know memory was searched and empty.
        if len(per_kind_counts) >= 2:
            breakdown = ", ".join(f"{k}: {n}" for k, n in per_kind_counts)
            lines = response.body.splitlines()
            if lines:
                lines.insert(1, f"_(per kind: {breakdown})_")
                response = Response(
                    body="\n".join(lines),
                    cost=response.cost,
                    result_count=response.result_count,
                    result_ids=response.result_ids,
                )

        # Partial failure is not silence either: a kind that errored is
        # indistinguishable from a kind with zero matches in the per-kind
        # counts, so name it — the caller may be missing exactly the hits
        # they wanted. (Total failure raised above.)
        if errored:
            tip = (
                "_(⚠ errored, omitted from this merge: "
                + ", ".join(
                    f"{k} ({type(e).__name__})" for k, e in sorted(errored.items())
                )
                + " — results may be incomplete; see server log)_"
            )
            lines = response.body.splitlines()
            if lines:
                lines.insert(1, tip)
            else:
                lines.append(tip)
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        # gr628278: kinds whose semantic leg was skipped up front (their
        # ANN route falls back to the global index — a scan that cannot
        # fit the per-kind budget) keep their lexical hits but must not
        # read as fully searched — name them, with the door back to the
        # full hybrid leg.
        if route_skipped:
            tip = (
                "_(semantic leg skipped for "
                + ", ".join(sorted(route_skipped))
                + " — their ANN leg falls back to the global index "
                "(partial index missing or not ready), a scan that "
                "cannot fit the fan-out's per-kind budget; search each "
                "kind explicitly for the full hybrid leg)_"
            )
            lines = response.body.splitlines()
            if lines:
                lines.insert(1, tip)
            else:
                lines.append(tip)
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        # gr664954: kinds that only got a worker inside the deadline's
        # last slice ran lexical-only — their hits stay merged, but the
        # semantic leg they skipped must not read as fully searched.
        # (Kinds that errored anyway are already named by the errored
        # tip; don't name them twice.)
        if lexical_fallback:
            fell_back = sorted(lexical_fallback - set(errored))
            if fell_back:
                tip = (
                    "_(semantic leg skipped for "
                    + ", ".join(fell_back)
                    + " — the fan-out deadline left too little for the "
                    "full hybrid legs, so they ran lexical-only this "
                    "turn; search each kind explicitly for the full "
                    "leg)_"
                )
                lines = response.body.splitlines()
                if lines:
                    lines.insert(1, tip)
                else:
                    lines.append(tip)
                response = Response(
                    body="\n".join(lines),
                    cost=response.cost,
                    result_count=response.result_count,
                    result_ids=response.result_ids,
                )

        # The domain fence's skipped-kinds footer (gr572720) — the same
        # ``_(…)_`` convention as the errored/excluded trailers, naming
        # what was fenced and the door back to the full surface.
        if domain_footer is not None:
            lines = response.body.splitlines()
            tip = f"_({domain_footer})_"
            if lines:
                lines.insert(1, tip)
            else:
                lines.append(tip)
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        # Broad-pass finding #7: when cross-kind ran with the wildcard
        # (kind=None or kind='*'), surface the kinds that have search
        # but opt out of search_hits (python, md, citation, tag —
        # each by design, because their per-kind renderer carries
        # structure the SearchHit shape would flatten). Lets the agent
        # know those kinds exist + were skipped on purpose, and where
        # to look. Only emit for wildcards; an explicit comma-list is
        # a deliberate choice and doesn't need the breadcrumb.
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
            excluded = self._cross_kind_excluded_kinds()
            if excluded:
                lines = response.body.splitlines()
                tip = (
                    "_(not included: "
                    + ", ".join(sorted(excluded))
                    + " — search each kind explicitly for the "
                    "richer per-kind view)_"
                )
                if len(per_kind_counts) >= 2 and len(lines) >= 2:
                    lines.insert(2, tip)
                elif lines:
                    lines.insert(1, tip)
                else:
                    lines.append(tip)
                response = Response(
                    body="\n".join(lines),
                    cost=response.cost,
                    result_count=response.result_count,
                    result_ids=response.result_ids,
                )

        if uncited_dropped_kinds:
            lines = response.body.splitlines()
            tip = (
                f"_({facet_label}: skipped "
                + ", ".join(sorted(uncited_dropped_kinds))
                + " — no exclude/include-by-ref_id wiring yet, so a hit "
                "there might already be (mis)cited; search that kind "
                "explicitly to check)_"
            )
            if lines:
                lines.insert(1, tip)
            else:
                lines.append(tip)
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        # gr485250: a C-number query is almost certainly a part lookup.
        # ``part`` opts out of the cross-kind merge (per-kind TOON table),
        # so the fan-out never even tries it — name the door.
        from precis.pcb.catalog import lcsc_id

        lcsc_q = lcsc_id(q)
        if lcsc_q is not None:
            lines = response.body.splitlines()
            lines.append(
                f"_({lcsc_q} looks like an LCSC/JLCPCB part number — "
                f"search(kind='part', q='{lcsc_q}') or "
                f"get(kind='part', id='{lcsc_q}') resolves it, live on a "
                "catalog miss)_"
            )
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        # Majority failure leads, not trails: the ranked list stays (the
        # hits are real), but it may not open with a confident headline
        # when most kinds never answered (gr513663).
        if majority_failed and response.result_count:
            lines = response.body.splitlines()
            lines.insert(
                0,
                f"# ⚠ search incomplete — {len(errored)} of {attempted} kinds "
                "timed out or errored; the list below covers only the kinds "
                "that answered. Retry, or narrow by kind "
                f"(search(kind='quest,todo,memory', q={q!r}), …).",
            )
            response = Response(
                body="\n".join(lines),
                cost=response.cost,
                result_count=response.result_count,
                result_ids=response.result_ids,
            )

        return _stamp_non_production_embedder(
            response, embedder, leg_skipped=mock_leg_skipped
        )

    def _dispatch_cross_kind_tags_only(
        self, kind: Any, args: dict[str, Any]
    ) -> Response:
        """Tags-only fan-out — one store query, no embedder needed.

        Use case: "find every ref tagged ``topic:exercise-mcp-throwaway``
        across all the kinds I created throwaways on" — previously
        forced four single-kind calls. Now a single
        ``search(tags=['<tag>'])`` (kind omitted) returns the kind-mixed
        set in one call. Restricted to live numeric-ref kinds (memory,
        todo, gripe, anki, conv, finding, job, pres) — the slug-ref and
        cache-backed kinds don't share the same tag-indexing path.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported(
                "tags-only cross-kind search needs a store-backed deployment"
            )
        tags = args.get("tags") or []
        # ``Tag.normalize_filter(kind=None)`` validates each tag against
        # the registered vocabulary (typo-rejection on closed axes) and
        # returns canonical string form so the store filter matches by
        # string equality. Mirrors the validation in the q= path above.
        normalized = Tag.normalize_filter(tags, kind=None)
        page_size = max(
            1, int(args.get("n") or args.get("top_k") or args.get("page_size") or 10)
        )

        # ``folder=`` scope applies to the tags-only sweep too.
        folder_scope: set[int] | None = None
        folder_label: str | None = None
        if args.get("folder") is not None:
            folder_scope, folder_label = self._resolve_folder_scope(args["folder"])

        allowed_list, domain_footer = self._domain_fan_out_fence(
            str(kind), self._resolve_cross_kind_request(kind), args
        )
        allowed_kinds = set(allowed_list)
        # Pull enough to cover the requested page after the kind filter.
        # 5x oversample is generous — the store's tag filter is fast and
        # the typical "find my throwaways" call returns < 20 rows.
        raw = store.list_refs(
            kind=None,
            tags=normalized,
            limit=page_size * 5,
        )
        refs = [r for r in raw if r.kind in allowed_kinds]
        if any(r.kind == "profile" for r in refs):
            # owner-private rows (gr490266): drop other owners' profiles
            from precis import profiles

            login = profiles.acting_login(store)
            refs = [r for r in refs if profiles.visible_to(r, login)]
        if folder_scope is not None:
            refs = [r for r in refs if r.id in folder_scope]
        refs = refs[:page_size]

        if not refs:
            scope_suffix = f" in folder {folder_label}" if folder_label else ""
            body = (
                f"no refs match tags={normalized!r} across "
                f"{sorted(allowed_kinds)}{scope_suffix}"
            )
        else:
            head = (
                f"# {len(refs)} ref{'s' if len(refs) != 1 else ''} "
                f"tagged {normalized!r} (kind-mixed, by recency)"
            )
            lines = [head]
            for r in refs:
                title = r.title or r.slug or f"#{r.id}"
                lines.append(f"{r.kind}:{r.id}  {title}")
            body = "\n".join(lines)
        if domain_footer is not None:
            body += f"\n_({domain_footer})_"
        return Response(
            body=body,
            result_count=len(refs),
            result_ids=[int(r.id) for r in refs],
        )

    def _resolve_folder_scope(self, value: Any) -> tuple[set[int], str]:
        """Resolve a ``folder=`` argument to ``(subtree_ref_ids, label)``.

        Accepts the folder's numeric id, a ``folder:N`` target string,
        the ``fo<N>`` universal handle, or the folder's *name*
        (case-insensitive; must be unique). Raises ``BadInput`` /
        ``NotFound`` with recovery hints — an empty subtree is never
        silently searched as unscoped.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("folder= scoping needs a store-backed deployment")

        ref_id: int | None = None
        raw = value
        if isinstance(raw, int):
            ref_id = raw
        else:
            s = str(raw).strip()
            if s.startswith("folder:"):
                s = s[len("folder:") :]
            try:
                ref_id = int(s)
            except ValueError:
                parsed = handle_registry.parse(s)
                if parsed is not None and parsed[0] == "folder":
                    ref_id = parsed[2]
        if ref_id is None:
            # Fall back to name resolution.
            matches = store.folder_ref_ids_by_title(str(raw).strip())
            if len(matches) == 1:
                ref_id = matches[0]
            elif len(matches) > 1:
                raise BadInput(
                    f"folder name {raw!r} is ambiguous ({len(matches)} folders match)",
                    next=(
                        "pass the id instead: "
                        + " or ".join(f"folder={m}" for m in matches[:5])
                    ),
                )
            else:
                raise NotFound(
                    f"no folder named {raw!r}",
                    next="get(kind='folder') lists folders with their ids",
                )
        ref = store.get_ref(kind="folder", id=ref_id)
        if ref is None:
            raise NotFound(
                f"folder id={ref_id} not found",
                next="get(kind='folder') lists folders with their ids",
            )
        label = f"{ref.title!r} (folder:{ref.id})"
        return store.folder_subtree_ids(ref.id), label

    def _cross_kind_slow_semantic_kinds(
        self, kinds: list[str], deadline_at: float
    ) -> list[str]:
        """Fanned kinds whose semantic leg cannot fit the per-kind budget.

        A kind in ``_ANN_PARTIAL_KINDS`` whose partial index is missing,
        invalid/unready or fill-incomplete routes its ANN leg over the
        paper-dominated global index — the filtered post-filter scan that
        measures 10-30 s on prod (gr519679), so inside the deadline it
        can only die at the per-kind statement cap and land in the
        errored footer (gr628278). Skipping the leg up front keeps the
        kind's lexical hits in the merge instead of paying a worker for
        a guaranteed cancel. One probe covers every kind:
        :func:`ann_route_plan`'s gate read is cached per database.

        td649559: the probe shares ``deadline_at`` with the fan-out —
        bounded to :data:`CROSS_KIND_PROBE_BUDGET_S`, not the whole
        remainder (gr664970: a checkout waiting the full remainder on a
        contended pool, or a gate read running it, left the fan-out
        nothing — every kind came back TimeoutError). The slice is ONE
        absolute deadline for the whole probe, not a per-step budget:
        the checkout waits only what the slice has left, the statement
        cap is what is left after that (a cap reusing the whole slice
        lets a waited checkout plus two stalled reads spend ~3x it),
        and a watchdog cancels whatever is still in flight when the
        deadline lands — ``statement_timeout`` is per-statement, so a
        second stalled read could otherwise spend a whole new cap past
        the deadline. The per-kind loop stops issuing reads past it.
        A fired watchdog is joined before the connection returns to
        the pool: ``Timer.cancel`` stops only a callback that has not
        started, and one already running would deliver its
        ``cancel_safe`` to the NEXT borrower of the connection
        (gr664970 review). Fails open to ``[]`` — an exhausted slice or
        probe error keeps the old cancel-at-the-cap path rather than
        refusing a whole search.
        """
        from precis.store._chunks_ops import _ANN_GLOBAL_KINDS, ann_route_plan

        store = self.hub.store
        if store is None:
            return []
        probe_deadline = min(deadline_at, time.monotonic() + CROSS_KIND_PROBE_BUDGET_S)
        wait_s = probe_deadline - time.monotonic()
        if wait_s <= 0:
            return []
        try:
            with store.pool.connection(timeout=wait_s) as conn:

                def cancel_in_flight() -> None:
                    # A cancel delivered on an idle backend is dropped and
                    # a dead conn has nothing to cancel — the statement
                    # cap remains the backstop, so a watchdog error must
                    # not mask the probe's own failure mode. The send is
                    # bounded (the 30 s default is not): the join below
                    # waits on this callback before the checkout ends.
                    try:
                        conn.cancel_safe(timeout=CROSS_KIND_PROBE_REAP_S)
                    except Exception:
                        pass

                watchdog = threading.Timer(
                    max(probe_deadline - time.monotonic(), 0.0), cancel_in_flight
                )
                watchdog.start()
                try:
                    with conn.transaction():
                        # Transaction-local, so the capped setting cannot
                        # leak to the next checkout of this pooled
                        # connection. The cap is what the slice has LEFT
                        # after the checkout, not the whole slice again.
                        stmt_ms = max(
                            int((probe_deadline - time.monotonic()) * 1000), 1
                        )
                        conn.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (str(stmt_ms),),
                        )
                        out = []
                        for k in kinds:
                            if time.monotonic() >= probe_deadline:
                                raise TimeoutError("cross-kind probe deadline spent")
                            plan = ann_route_plan(conn, k, None)
                            if (
                                plan["route"] == "global"
                                and plan["kind"] is not None
                                and plan["kind"] not in _ANN_GLOBAL_KINDS
                            ):
                                out.append(k)
                        return out
                finally:
                    watchdog.cancel()
                    # … and reap it: cancel() stops only a callback that
                    # has not started — a fired watchdog keeps running
                    # and its cancel_safe() could land AFTER the pool
                    # return, on the next borrower's unrelated statement
                    # (gr664970 review). The callback's own bounded send
                    # caps this wait; the join's timeout states that bound.
                    watchdog.join(timeout=2 * CROSS_KIND_PROBE_REAP_S)
        except Exception:
            log.exception("cross-kind ANN route probe failed; full fan-out kept")
            return []

    def _cross_kind_fan_out(
        self,
        kinds: list[str],
        base_kwargs: dict[str, Any],
        kind_overrides: dict[str, dict[str, Any]] | None = None,
        *,
        deadline_at: float | None = None,
        lexical_fallback: set[str] | None = None,
    ) -> dict[str, list[SearchHit] | Exception]:
        """Run every kind's ``search_hits`` on at most
        :data:`CROSS_KIND_MAX_WORKERS` threads, giving up on stragglers
        after :data:`CROSS_KIND_DEADLINE_S` seconds in total.

        gr495190: the fan-out used to be a serial loop, and one kind's
        filtered HNSW scan alone takes 10-30 s on prod (the global index
        is almost all papers), so 34 kinds ran past 120 s. A kind that
        misses the deadline comes back as a ``TimeoutError`` value, so the
        caller names it in the "errored, omitted" footer instead of
        reading it as zero matches. Kinds without a handler are absent
        from the result. Threads cannot be killed: a straggler finishes in
        the background, but the process-wide executor caps how many can
        exist across all searches — and each job pins its pool checkouts
        to ``deadline_at`` (``POOL_CHECKOUT_DEADLINE``), so a straggler
        can't hold a shared worker queueing for connections after its
        results were discarded (gr664970).

        ``kind_overrides`` maps a kind to ``search_hits`` kwargs merged
        over ``base_kwargs`` — the wildcard fan-out uses it to run a
        predictably-slow kind lexical-only (gr628278).

        ``deadline_at`` defaults to ``now + CROSS_KIND_DEADLINE_S``;
        :meth:`_dispatch_cross_kind` passes the deadline its ANN route
        probe already drew from, so probe and fan-out together fit the
        one budget (td649559).

        gr664954: two isolations keep one slow kind from starving the
        queue. :data:`~precis.store.pool.CHECKOUT_BUDGET` bounds every
        pool checkout the kind makes — the wait itself and every
        statement server-side, not just the ANN leg — so a stalled
        checkout or an uncapped query can no longer hold a worker past
        the call deadline. And a kind dequeued inside
        :data:`CROSS_KIND_MIN_START_MS` but above the smaller
        :data:`CROSS_KIND_LEXICAL_MIN_START_MS` floor runs lexical-only
        instead of timing out: ``lexical_fallback``, when a set is
        passed (the default expansion — an explicit kind list is
        caller intent and keeps the timeout), collects those kinds so
        the caller can name the degrade. The degrade itself only
        applies to the default/``'hybrid'`` mode: an explicit
        ``mode='semantic'`` or an exact-term mode (``'verbatim'``,
        ``'literal'``) would have its match semantics silently changed
        by a lexical rewrite, so those keep the timeout too.
        """
        from concurrent.futures import wait

        from precis.store.pool import (
            _PINNED,
            CHECKOUT_BUDGET,
            POOL_CHECKOUT_DEADLINE,
        )

        jobs = [(k, h) for k in kinds if (h := self.hub.handler_for(k)) is not None]
        out: dict[str, list[SearchHit] | Exception] = {}
        if not jobs:
            return out
        if deadline_at is None:
            deadline_at = time.monotonic() + CROSS_KIND_DEADLINE_S

        def run(handler: Handler, k: str) -> list[SearchHit] | Exception:
            # A worker must not share a pinned (atomic-scope) connection.
            _PINNED.set(None)
            # … and its pool checkouts may not outwait the call deadline:
            # the default 30 s checkout would let a straggler hold this
            # shared worker through repeated waits long after its results
            # were discarded, starving the next call's fan-out (gr664970).
            POOL_CHECKOUT_DEADLINE.set(deadline_at)
            remaining_ms = int((deadline_at - time.monotonic()) * 1000)
            min_start = min(CROSS_KIND_MIN_START_MS, int(CROSS_KIND_DEADLINE_S * 100))
            kw = (
                {**base_kwargs, **kind_overrides[k]}
                if kind_overrides is not None and k in kind_overrides
                else base_kwargs
            )
            if remaining_ms < min_start:
                mode = str(kw.get("mode") or "").strip().lower()
                lexical_floor = min(
                    CROSS_KIND_LEXICAL_MIN_START_MS,
                    int(CROSS_KIND_DEADLINE_S * 100),
                )
                if (
                    lexical_fallback is not None
                    and mode in ("", "hybrid")
                    and remaining_ms >= lexical_floor
                ):
                    # Too late for the full legs but not for a
                    # lexical-only attempt — a merged lexical result
                    # beats an omitted kind (gr664954). Only the
                    # default/hybrid mode degrades: an exact-term mode
                    # ('verbatim', 'literal') or 'semantic' is caller
                    # intent a lexical rewrite would silently change.
                    kw = {**kw, "mode": "lexical"}
                    lexical_fallback.add(k)
                elif not (mode == "lexical" and remaining_ms >= lexical_floor):
                    return TimeoutError(
                        f"fan-out deadline left {remaining_ms}ms — too late to start"
                    )
                # An already-lexical kind needs no degrade — the smaller
                # floor still fits its legs.
            # Postgres cancels the ANN scan at the deadline; the client-side
            # wait alone would leave it running. The per-kind cap (gr519679)
            # stops a single runaway query from holding its worker for the
            # whole deadline while the rest of the queue starves.
            ANN_STATEMENT_TIMEOUT_MS.set(
                min(remaining_ms, int(CROSS_KIND_PER_KIND_S * 1000))
            )
            # gr664954: the same bound on every pool checkout this kind
            # makes — a saturated pool or an uncapped non-ANN statement
            # can no longer hold a worker past the call deadline.
            CHECKOUT_BUDGET.set((deadline_at, CROSS_KIND_PER_KIND_S * 1000))
            return self._cross_kind_invoke_search_hits(handler, k, kw)

        pool = _cross_kind_executor()
        futures = {
            k: pool.submit(contextvars.copy_context().run, run, h, k) for k, h in jobs
        }
        wait(futures.values(), timeout=max(0.0, deadline_at - time.monotonic()))
        for k, fut in futures.items():
            if fut.done():
                try:
                    out[k] = fut.result()
                except Exception as exc:
                    log.exception("cross-kind search_hits crashed for %s", k)
                    out[k] = exc
            else:
                # Queued work is dropped; a running straggler finishes in the
                # background but holds one of the shared workers, so stragglers
                # across all searches stay at CROSS_KIND_MAX_WORKERS.
                fut.cancel()
                log.error(
                    "cross-kind search_hits for %s exceeded %.0fs; omitted",
                    k,
                    CROSS_KIND_DEADLINE_S,
                )
                out[k] = TimeoutError(f"no answer within {CROSS_KIND_DEADLINE_S:.0f}s")
        return out

    def _cross_kind_invoke_search_hits(
        self,
        handler: Handler,
        kind: str,
        base_kwargs: dict[str, Any],
    ) -> list[SearchHit] | Exception:
        """Call ``handler.search_hits`` with progressive-degradation
        retries. Handlers' ``search_hits`` signatures vary in which
        optional kwargs they accept (``tags=``, ``exclude=``, …); rather
        than introspect ahead of time, try the full kwargs set first and
        drop unknown kwargs on ``TypeError``, most-recent-addition first
        (``exclude``, then ``tags``). Any non-TypeError exception is
        logged and returned as a value — the caller drops the kind from
        the merge but records *that it errored*, so one broken kind
        neither crashes the whole query nor hides inside a "0 matches"
        count (the gr281493 false-quiet).

        ``exclude_ref_ids=`` is deliberately NOT in the drop list: every
        handler's ``search_hits`` today declares ``**_kw`` (silently
        ignores unknown kwargs), so this never triggers the retry path —
        dropping it here would be the exact silent-degrade the feature
        must avoid. The caller (``_dispatch_cross_kind``) instead
        pre-filters ``kinds`` to exclude the two known to ignore it
        (:data:`~precis.runtime._shared.UNCITED_UNSUPPORTED_KINDS`); for
        every remaining kind, ignoring the kwarg is provably harmless
        (globally-unique ref_ids) or actually honoured. If a future
        handler ever *does* raise ``TypeError`` on it, the final fallback
        returns that ``TypeError`` — no hits from that kind — rather
        than proceeding without the filter.
        """
        # Try the full set first.
        try:
            return list(handler.search_hits(**base_kwargs))
        except TypeError as exc:
            first_type_error: Exception = exc
        except Exception as exc:
            log.exception("cross-kind search_hits failed for %s", kind)
            return exc

        # Drop ``exclude=`` (most recent kwarg addition) and retry.
        if "exclude" in base_kwargs:
            without_exclude = {k: v for k, v in base_kwargs.items() if k != "exclude"}
            try:
                return list(handler.search_hits(**without_exclude))
            except TypeError:
                pass
            except Exception as exc:
                log.exception("cross-kind search_hits failed for %s", kind)
                return exc
        else:
            without_exclude = base_kwargs

        # Drop ``tags=`` too (oracle doesn't filter by tag).
        if "tags" in without_exclude:
            minimal = {k: v for k, v in without_exclude.items() if k != "tags"}
            try:
                return list(handler.search_hits(**minimal))
            except Exception as exc:
                log.exception("cross-kind search_hits failed for %s", kind)
                return exc

        # Already minimal (q + top_k only) and still failing on the
        # signature itself.
        log.error("cross-kind search_hits failed for %s: %s", kind, first_type_error)
        return first_type_error

    def _filter_hits_by_tags(
        self,
        hits: list[SearchHit],
        required_tag_strings: list[str],
    ) -> list[SearchHit]:
        """Drop hits whose refs don't carry every required tag.

        Correctness backstop for cross-kind fan-out: it passes ``tags=``
        to each handler's ``search_hits``, but most handlers'
        signatures take ``**_kw`` and silently ignore unknown kwargs —
        so ``tags=['workspace']`` was effectively a no-op for every kind
        except numeric refs, letting kinds that can't carry the tag
        (``think``, ``websearch``, …) leak into a scoped search.

        Runs after every stream is collected: resolves ``ref_id`` (via
        ``slug`` when needed), fetches the ref-level tag set, keeps only
        hits whose tags superset the required ones. Compares on the
        canonical string form so a flag ``workspace`` and an open tag
        ``workspace`` match equivalently.

        Cost: one extra ``tags_for`` DB hit per surviving hit —
        acceptable since tag filters are rare in cross-kind search and
        correctness dwarfs throughput here.
        """
        if self.hub.store is None or not required_tag_strings:
            return hits
        # ``'!<tag>'`` entries (gr568354) exclude rather than require —
        # same split the SQL path applies in ``build_tag_filter``.
        required = {t for t in required_tag_strings if not t.startswith("!")}
        excluded = {t[1:] for t in required_tag_strings if t.startswith("!")}
        kept: list[SearchHit] = []
        for hit in hits:
            ref_id = hit.ref_id
            if ref_id is None and hit.slug:
                ref = self.hub.store.get_ref(kind=hit.kind, id=hit.slug)
                if ref is None:
                    continue
                ref_id = ref.id
            if ref_id is None:
                # Producer provided neither ref_id nor slug — can't
                # check tags. Drop rather than leak an unfiltered
                # hit.
                continue
            tags_have = self.hub.store.tags_for(ref_id)
            have_strings = {str(t) for t in tags_have}
            if required.issubset(have_strings) and excluded.isdisjoint(have_strings):
                kept.append(hit)
        return kept
