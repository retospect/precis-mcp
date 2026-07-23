"""Cross-kind fan-out + the unified-item-view source-search primitive.

``SearchMixin`` carries ``search(kind='*')`` / comma-list fan-out (RRF
merge across handlers' ``search_hits``), the tags-only cross-kind sweep,
``folder=`` subtree scoping, the post-fan-out tag backstop filter, the
``sort=``/``since=``/``until=`` source-search primitive, and the
``view='stubs'`` backlog view. The angle spray and dreamable region live
in :mod:`precis.runtime.angle` — those also fan out across kinds but pick
their own seed rather than ranking a ``q=`` query.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from precis.errors import BadInput, NotFound, Unsupported, Upstream
from precis.protocol import Handler
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_ALIASES as _CROSS_KIND_ALIASES
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import RuntimeShape
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.store.types import Tag
from precis.utils import handle_registry
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    merge_and_render,
)

log = logging.getLogger(__name__)


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
        feed (docs/design/stubs-mcp-and-skill.md). Paper-only; ``q=`` is
        ignored (the view *is* the filter). ``n=`` / ``page_size=`` cap
        the row count; newest stub first. Read-only — surfacing the
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
            cite = r["cite_key"] or f"ref {r['ref_id']}"
            lines.append(f"  ref {r['ref_id']}  {ident}  [{cite}]")
            lines.append(f"      {r['state']}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='paper', id={rows[0]['ref_id']})",
                    "open a stub to see what links to it",
                ),
                (
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

    # ── cross-kind search ──────────────────────────────────────────────

    def _is_cross_kind_request(self, kind: Any) -> bool:
        """True iff ``kind`` asks for a cross-kind merge.

        Forms accepted (case-insensitive on aliases):

        - the wildcard ``'*'`` and its English aliases ``'all'`` /
          ``'any'`` (see :data:`~precis.runtime._shared.CROSS_KIND_ALIASES`);
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
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
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
            spec = self.hub.handler_for(k).spec
            if spec.supports_search and spec.supports_search_hits:
                out.append(k)
        return out

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
            spec = self.hub.handler_for(k).spec
            if spec.supports_search and not spec.supports_search_hits:
                out.append(k)
        return out

    def _resolve_cross_kind_request(self, kind: str) -> list[str]:
        """Expand ``kind`` into the concrete list of kinds to fan out to.

        Wildcard expands to every search-hits-capable kind.  Comma-
        lists are split, normalised (trim whitespace), and validated:
        unknown kinds and kinds that don't support cross-kind search
        raise ``BadInput`` with the recoverable list as ``options``.
        """
        eligible = self._cross_kind_kinds()
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
            return eligible

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

        The Slice-2 primitive (see
        :meth:`Store.search_chunks_across_kinds` +
        ``docs/proposals/unified-item-view.md``). Resolves the kind set
        (single / comma-list / wildcard / omitted → every cross-kind
        kind; kinds with no embedded chunks contribute nothing), embeds
        ``q`` once, runs the single store query, and renders the per-ref
        hits — each stamped with its own ``ref.kind`` so handles/labels
        stay correct — as one pre-ordered stream (RRF over a single
        stream preserves the store's relevance-or-recency order).
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

        results = store.search_chunks_across_kinds(
            kinds=kinds,
            q=q,
            query_vec=query_vec,
            mode=mode,
            tags=tags,
            since=since,
            until=until,
            sort=sort,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        hits: list[SearchHit] = []
        for block, ref, score in results:
            hits.extend(block_hits_to_search_hits([(block, ref, score)], kind=ref.kind))

        date_suffix = " in the given date window" if (since or until) else ""
        if semantic_degraded:
            empty_body = (
                f"no lexical matches across {', '.join(kinds)} for {q!r}"
                f"{date_suffix}; semantic search degraded to lexical-only this "
                "turn — retry in ~30s for the full ranked fan-out"
            )
        else:
            empty_body = f"no matches across {', '.join(kinds)} for {q!r}{date_suffix}"
        return merge_and_render(
            [hits],
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=empty_body,
        )

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        """Fan out a search across multiple kinds and RRF-fuse the streams.

        Each handler's ``search_hits(q=..., top_k=...)`` is called
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
        if q is None or not (isinstance(q, str) and q.strip()):
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
        top_k = int(args.get("top_k") or 10)
        tags = args.get("tags")
        exclude = args.get("exclude")
        mode = args.get("mode")
        mode_lexical = isinstance(mode, str) and mode.strip().lower() == "lexical"

        # ``folder=`` scope (ADR 0045): resolve the folder's live
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
        base_kwargs: dict[str, Any] = {"q": q, "top_k": top_k}
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

        streams: list[list[SearchHit]] = []
        per_kind_counts: list[tuple[str, int]] = []
        for k in kinds:
            handler = self.hub.handler_for(k)
            if handler is None:
                per_kind_counts.append((k, 0))
                continue
            hits = self._cross_kind_invoke_search_hits(handler, k, base_kwargs)
            if hits is None:
                per_kind_counts.append((k, 0))
                continue
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
            hits_list = list(hits)
            per_kind_counts.append((k, len(hits_list)))
            streams.append(hits_list)

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
        response = merge_and_render(
            streams,
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=empty_body,
            output_shape=output_shape,
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
                response = Response(body="\n".join(lines), cost=response.cost)

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
                response = Response(body="\n".join(lines), cost=response.cost)

        return response

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
        return Response(body=body)

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

    def _cross_kind_invoke_search_hits(
        self,
        handler: Handler,
        kind: str,
        base_kwargs: dict[str, Any],
    ) -> list[SearchHit] | None:
        """Call ``handler.search_hits`` with progressive-degradation retries.

        Handlers' ``search_hits`` signatures vary in which optional
        kwargs they accept (``tags=``, ``exclude=``, …). Rather than
        introspect the signature ahead of time, we try the full
        kwargs set first and drop unknown kwargs on ``TypeError``,
        most-recent-addition first (``exclude``, then ``tags``). Any
        non-TypeError exception is logged and degraded to ``None``
        so one slow / broken kind doesn't crash the whole query.
        """
        # Try the full set first.
        try:
            return list(handler.search_hits(**base_kwargs))
        except TypeError:
            pass
        except Exception:
            log.exception("cross-kind search_hits failed for %s", kind)
            return None

        # Drop ``exclude=`` (most recent kwarg addition) and retry.
        if "exclude" in base_kwargs:
            without_exclude = {k: v for k, v in base_kwargs.items() if k != "exclude"}
            try:
                return list(handler.search_hits(**without_exclude))
            except TypeError:
                pass
            except Exception:
                log.exception("cross-kind search_hits failed for %s", kind)
                return None
        else:
            without_exclude = base_kwargs

        # Drop ``tags=`` too (oracle doesn't filter by tag).
        if "tags" in without_exclude:
            minimal = {k: v for k, v in without_exclude.items() if k != "tags"}
            try:
                return list(handler.search_hits(**minimal))
            except Exception:
                log.exception("cross-kind search_hits failed for %s", kind)
                return None

        # Already minimal (q + top_k only) and still failing.
        log.exception("cross-kind search_hits failed for %s", kind)
        return None

    def _filter_hits_by_tags(
        self,
        hits: list[SearchHit],
        required_tag_strings: list[str],
    ) -> list[SearchHit]:
        """Drop hits whose refs don't carry every required tag.

        Correctness backstop for cross-kind fan-out. The fan-out
        passes ``tags=`` to each handler's ``search_hits``, but most
        handlers' signatures take ``**_kw`` and silently ignore
        unknown kwargs — so ``tags=['workspace']`` was effectively a
        no-op for every kind except numeric refs. That made the
        advertised ``search(tags=['workspace'])`` scope-to-workspace
        filter return hits from kinds (``think``, ``websearch``,
        …) that can't carry the tag at all.

        This method runs after every stream is collected: for each
        hit it resolves ``ref_id`` (looking up via ``slug`` when
        needed), fetches the ref-level tag set, and keeps only hits
        whose tags are a superset of the required ones. Comparison
        is on the canonical string form (``__str__``) so a flag
        ``workspace`` and an open tag ``workspace`` are treated as
        equivalent matches — there's no practical reason the agent
        should care about the namespace of the tag they're
        filtering by.

        Cost: one extra ``tags_for`` DB hit per surviving hit.
        Acceptable for this axis (tag filters are relatively rare
        in cross-kind search; correctness dwarfs throughput).
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
