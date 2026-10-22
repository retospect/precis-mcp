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
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from precis.errors import BadInput, Internal, NotFound, Unsupported, Upstream
from precis.protocol import Handler
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_ALIASES as _CROSS_KIND_ALIASES
from precis.runtime._shared import CROSS_KIND_SOURCE_ALIAS as _CROSS_KIND_SOURCE_ALIAS
from precis.runtime._shared import CROSS_KIND_SOURCE_KINDS as _CROSS_KIND_SOURCE_KINDS
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import (
    UNCITED_UNSUPPORTED_KINDS as _UNCITED_UNSUPPORTED_KINDS,
)
from precis.runtime._shared import RuntimeShape
from precis.store._chunks_ops import ANN_STATEMENT_TIMEOUT_MS
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.store.types import Tag
from precis.utils import handle_registry
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    flag_title_matches,
    merge_and_render,
)

log = logging.getLogger(__name__)

#: Cross-kind fan-out bounds (gr495190). Workers stay well under the DB pool
#: (default 10) so a wildcard search can't starve other requests. Held at 2
#: until the server-side cancel (ANN_STATEMENT_TIMEOUT_MS) is seen working on
#: prod; raise it then.
CROSS_KIND_MAX_WORKERS = 2
#: Kinds whose source-search hits are addressed by the parent ref handle (gr),
#: not the matching comment chunk (gc) — the status-browse kinds (gr495382).
_PARENT_HANDLE_KINDS = frozenset({"gripe", "todo", "quest"})
#: Wall-clock budget for the whole fan-out, not per kind.
CROSS_KIND_DEADLINE_S = 30.0

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


def _stamp_non_production_embedder(
    response: Response, embedder: Any | None
) -> Response:
    """Insert the non-production-embedder marker into ``response.body``.

    A no-op when ``embedder`` is ``None`` (mode='lexical' skipped the
    semantic leg entirely — nothing to warn about) or when the
    embedder doesn't carry ``is_production`` (an ad-hoc test double;
    default True keeps this silent rather than guessing). Matches the
    existing "insert a bracketed one-liner after the headline" visual
    convention used for the errored/omitted-kinds trailers in
    :meth:`SearchMixin._dispatch_cross_kind`.
    """
    if embedder is None or getattr(embedder, "is_production", True):
        return response
    backend = getattr(embedder, "backend", type(embedder).__name__)
    marker = _NON_PRODUCTION_EMBEDDER_MARKER.format(backend=backend)
    lines = response.body.splitlines()
    if lines:
        lines.insert(1, marker)
        return Response(
            body="\n".join(lines),
            cost=response.cost,
            result_count=response.result_count,
        )
    return Response(body=marker, cost=response.cost, result_count=response.result_count)


def _stub_state_line(row: dict[str, Any]) -> str:
    """The per-stub state line, with ``prio N`` and (when the ``stub_rank``
    pass's Tier-2 LLM band has labeled this stub) `` · <label>`` appended.

    ``row['prio']`` is ``None`` for an unranked stub (the ``stub_rank``
    pass hasn't scored it yet, or the whole pass is disabled) — omit the
    suffix entirely rather than rendering a noisy ``prio None``. Likewise
    ``row['llm_label']`` is empty for a stub outside the uncertain band
    (or the LLM step is disabled/hasn't reached it yet).
    """
    prio = row.get("prio")
    line = row["state"] if prio is None else f"{row['state']}  prio {prio}"
    label = row.get("llm_label")
    if label:
        line = f"{line}  · {label}"
    return line


class SearchMixin(RuntimeShape):
    """Cross-kind fan-out, tags-only sweep, folder scope, source search."""

    # Default backlog size for ``search(view='stubs')`` — enough to
    # scan in one prompt without dumping the whole queue.
    _STUBS_DEFAULT_N: int = 25

    def _dispatch_stubs(self, args: dict[str, Any]) -> Response:
        """The required-papers backlog: ``search(view='stubs')``.

        Lists ``paper`` refs with an external identifier (DOI / arXiv /
        S2) registered but no PDF yet — the "papers we still need to
        get" queue the chase worker and the dream ``acquire`` tool both
        feed (the stub surfaces). Paper-only; ``q=`` is
        ignored (the view *is* the filter). ``n=`` / ``page_size=`` cap
        the row count; ``prio``-ranked first (1=hottest, unranked
        last), oldest request the tiebreak. Read-only — surfacing the
        backlog does not touch salience or the fetch pipeline.
        """
        from precis.utils.next_block import render_next_section

        store = self.hub.store
        if store is None:
            raise Unsupported("view='stubs' needs a store-backed deployment")

        n = int(args.get("n") or args.get("page_size") or self._STUBS_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='stubs', n=25)")

        rows = store.stub_backlog(limit=n)
        if not rows:
            return Response(
                body=(
                    "no stub papers — every paper has a PDF or no external "
                    "identifier to fetch one with. Nothing to acquire."
                )
            )

        lines = [f"papers we still need to get ({len(rows)} shown):", ""]
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

    def _dispatch_chase_queue(self, args: dict[str, Any]) -> Response:
        """The DOI-only, never-tried-first chase queue:
        ``search(view='chase-queue')``.

        A tighter slice of the same stub backlog ``view='stubs'``
        surfaces: DOI-only (the
        identifier kind the fetcher cascade covers most reliably) and
        ordered never-tried-first rather than oldest-request-first —
        "what should I go find a PDF for right now" rather than "what's
        been waiting longest". Paper-only; ``q=`` is ignored (like
        ``view='stubs'``). ``n=`` / ``page_size=`` cap the row count.
        Read-only — surfacing the queue does not touch the fetch
        pipeline.
        """
        from precis.utils.next_block import render_next_section

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
        ingest.  Excluded handlers (calc, skill, python, perplexity,
        …) keep their single-kind ``search()`` contract; their
        absence from this list is by design.
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

        store = self.store
        if store is None:
            raise Unsupported("view='plan' needs a store-backed deployment")
        if kind is None or self._is_cross_kind_request(str(kind)):
            kinds = self._cross_kind_kinds()
        else:
            kinds = [self._expand_kind_code(str(kind))]
        mode = str(args.get("mode") or "hybrid").strip().lower()
        q = args.get("q")
        limit = int(args.get("page_size") or 20)
        ef = min(
            _ANN_EF_SEARCH_MAX,
            max(_ANN_EF_SEARCH_MIN, _ANN_EF_SEARCH_PER_LIMIT * limit),
        )
        embedder = getattr(self.hub, "embedder", None)
        lines = [
            "# search plan (no ranking executed)",
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
            return list(_CROSS_KIND_SOURCE_KINDS)

        requested = [tok.strip() for tok in kind.split(",")]
        requested = [t for t in requested if t]
        if not requested:
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

    @staticmethod
    def _parse_search_date(value: Any, field: str) -> datetime | None:
        """Parse a ``since=`` / ``until=`` bound into a tz-aware datetime.

        Accepts an ISO date (``2024-01-01``) or a full ISO timestamp; a
        naive value is assumed UTC (``refs.created_at`` is ``timestamptz``).
        Raises ``BadInput`` on an unparseable string.
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
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

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
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("source search needs a store-backed deployment")
        q = args.get("q")
        if not (isinstance(q, str) and q.strip()):
            raise BadInput(
                "source search (sort=/since=/until=) requires q=",
                next="search(kind='paper,patent', q='mof co2', sort='recency')",
            )
        expanded = (
            self._expand_kind_code(str(kind))
            if kind is not None
            else _CROSS_KIND_WILDCARD
        )
        kinds = self._resolve_cross_kind_request(expanded)
        since = self._parse_search_date(args.get("since"), "since")
        until = self._parse_search_date(args.get("until"), "until")
        sort = str(args.get("sort") or "relevance").strip().lower() or "relevance"
        top_k = int(args.get("page_size") or args.get("top_k") or 10)
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
                tags = [
                    t for t in (tags or []) if not str(t).strip().startswith("STATUS:")
                ]
                tags.append(f"STATUS:{status.strip().lower()}")
            if tags:
                tags = Tag.normalize_filter(tags, kind=None)
        mode = args.get("mode")
        mode_lexical = isinstance(mode, str) and mode.strip().lower() == "lexical"

        query_vec: list[float] | None = None
        semantic_degraded = False
        embedder = None if mode_lexical else getattr(self.hub, "embedder", None)
        if embedder is not None:
            try:
                query_vec = embedder.embed_one(q)
            except Upstream:
                semantic_degraded = True
            except Exception:
                log.exception("source search: query embed failed; lexical-only")

        exclude_ref_ids = args.get("exclude_ref_ids")
        include_ref_ids = args.get("include_ref_ids")
        results = store.chunks.search_chunks_across_kinds(
            kinds=kinds,
            q=q,
            query_vec=query_vec,
            mode=mode,
            tags=tags,
            exclude_tags=status_excluded or None,
            since=since,
            until=until,
            sort=sort,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
        )
        hits: list[SearchHit] = []
        seen_parents: set[int] = set()
        for block, ref, score in results:
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
                )
        return _stamp_non_production_embedder(response, embedder)

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
        if q is None or not (isinstance(q, str) and q.strip()):
            if args.get("exclude_ref_ids") is not None:
                raise BadInput(
                    "uncited= requires q= (it filters a ranked search, not "
                    "the tags-only sweep)",
                    next=f"search(kind={kind!r}, q='your query', uncited=…)",
                )
            if args.get("include_ref_ids") is not None:
                raise BadInput(
                    "cited= requires q= (it filters a ranked search, not "
                    "the tags-only sweep)",
                    next=f"search(kind={kind!r}, q='your query', cited=…)",
                )
            if tags_in:
                return self._dispatch_cross_kind_tags_only(kind, args)
            raise BadInput(
                "cross-kind search requires q= or tags=",
                next=(
                    f"search(kind={kind!r}, q='your query') - cross-kind merge "
                    "fans out via search_hits, which needs a non-empty query; "
                    f"or search(kind={kind!r}, tags=['<tag>']) for a tags-only sweep"
                ),
            )
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
                                "use sort=/since=/until= (the cross-kind "
                                "source-search primitive), which excludes "
                                "uniformly across every kind"
                            ),
                        )
                else:
                    raise Unsupported(
                        f"{facet_label} is not supported for kind(s) "
                        f"{unsupported_present!r} — no exclude/include-by-ref_id "
                        "wiring yet",
                        next=(
                            "drop these kinds from the comma-list, or use "
                            "sort=/since=/until= (the cross-kind "
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
        if embedder is not None:
            try:
                base_kwargs["query_vec"] = embedder.embed_one(q)
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

        streams: list[list[SearchHit]] = []
        per_kind_counts: list[tuple[str, int]] = []
        errored: dict[str, Exception] = {}
        fanned = self._cross_kind_fan_out(kinds, base_kwargs)
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
        # non-empty ``errored`` ⇒ nothing ran successfully.)
        if errored and not streams:
            names = ", ".join(
                f"{k} ({type(e).__name__})" for k, e in sorted(errored.items())
            )
            raise Internal(
                f"cross-kind search failed in every kind tried — {names} "
                "(see server log)"
            ) from next(iter(errored.values()))

        # When the embedder was unavailable for this turn, change the
        # empty-result wording from "no matches" to a partial-result
        # headline — semantic side genuinely couldn't run, so claiming
        # zero matches is overconfident. Broad-pass finding #13.
        scope_suffix = f" in folder {folder_label}" if folder_label else ""
        if semantic_degraded:
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
            )

        # Broad-pass finding #7: when cross-kind ran with the wildcard
        # (kind=None or kind='*'), surface the kinds that have search
        # but opt out of search_hits (skill, citation, finding, tag —
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
            )

        return _stamp_non_production_embedder(response, embedder)

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
        page_size = max(1, int(args.get("page_size") or 10))

        # ``folder=`` scope applies to the tags-only sweep too.
        folder_scope: set[int] | None = None
        folder_label: str | None = None
        if args.get("folder") is not None:
            folder_scope, folder_label = self._resolve_folder_scope(args["folder"])

        allowed_kinds = set(self._resolve_cross_kind_request(kind))
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
        return Response(body=body, result_count=len(refs))

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

    def _cross_kind_fan_out(
        self, kinds: list[str], base_kwargs: dict[str, Any]
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
        exist across all searches.
        """
        from concurrent.futures import wait

        from precis.store.pool import _PINNED

        jobs = [(k, h) for k in kinds if (h := self.hub.handler_for(k)) is not None]
        out: dict[str, list[SearchHit] | Exception] = {}
        if not jobs:
            return out

        def run(handler: Handler, k: str) -> list[SearchHit] | Exception:
            # A worker must not share a pinned (atomic-scope) connection.
            _PINNED.set(None)
            remaining_ms = int((deadline_at - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return TimeoutError("fan-out deadline passed before start")
            # Postgres cancels the ANN scan at the deadline; the client-side
            # wait alone would leave it running.
            ANN_STATEMENT_TIMEOUT_MS.set(remaining_ms)
            return self._cross_kind_invoke_search_hits(handler, k, base_kwargs)

        deadline_at = time.monotonic() + CROSS_KIND_DEADLINE_S
        pool = _cross_kind_executor()
        futures = {
            k: pool.submit(contextvars.copy_context().run, run, h, k) for k, h in jobs
        }
        wait(futures.values(), timeout=CROSS_KIND_DEADLINE_S)
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
        required = set(required_tag_strings)
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
            if required.issubset(have_strings):
                kept.append(hit)
        return kept
