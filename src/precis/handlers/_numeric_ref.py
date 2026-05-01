"""Shared base class for numeric-id ref kinds.

Memory was the first instance; phase 5 brings five more (todo, gripe,
fc, conv, quest) plus a couple of slug variants. The CRUD shape is
nearly identical for all of them — only the kind name, default tags,
landing/list views, and a small render hook differ.

Subclass contract:

    Required class attributes
        spec: ClassVar[KindSpec]   — kind metadata (name, description …)
        kind: ClassVar[str]         — duplicates spec.kind for terseness

    Optional overrides
        corpus_slug: ClassVar[str]                     — default 'default'
        default_tags_on_create: ClassVar[tuple[str,…]] — applied to every put-create
        sense:                                         — singular noun for messages

    Optional method overrides
        _render_one(ref, tags)             — body of a single-ref read
        _render_search_hit(ref, rank, …)   — line in search output
        _list_view(view)                   — handle path views like '/recent'

The base provides ``get`` / ``search`` / ``put`` exactly as v1's
MemoryHandler did. Subclasses that need fancier behaviour (e.g. `fc`'s
spaced-repetition scheduling, `quest`'s status transitions) override
the relevant hook.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import validate_relation
from precis.handlers._link_target import parse_link_target
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Link, Ref, Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits

# Views every numeric-ref kind picks up for free. Subclasses with
# additional views should override `get()` to layer their dispatch
# on top, or — when we land per-kind view registries — extend
# this tuple via a class-level hook.
_BASE_VIEWS: tuple[str, ...] = ("links",)

# The full set of ``mode=`` values numeric-ref kinds accept on put.
# Anything else (``mode='note'``, ``mode='untag'``, ``mode='unlink'``,
# typos like ``mode='deelete'``) must reject up front rather than
# silently falling through to the create / update branch — that was
# the MCP critic's CRITICAL #2 ("``mode='unlink'`` silently
# succeeds without removing anything"). The unlink/untag *operations*
# now have their own kwargs (``unlink=``, ``untags=``); ``mode=``
# is only kept around for the soft-delete path.
_SUPPORTED_PUT_MODES: tuple[str, ...] = ("delete",)


class NumericRefHandler(Handler):
    """Base class for numeric-id ref kinds (memory, todo, gripe, fc, …)."""

    spec: ClassVar[KindSpec]
    kind: ClassVar[str]
    corpus_slug: ClassVar[str] = "default"

    #: Tags applied automatically on `put`-create. e.g. for todo:
    #: ``("STATUS:open",)`` so every new todo starts open.
    default_tags_on_create: ClassVar[tuple[str, ...]] = ()

    #: Singular noun used in user-facing messages ("memory id=…",
    #: "todo id=…", …). Defaults to the kind name.
    sense: ClassVar[str] = ""

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError(f"{self.kind}: store required")
        self.store = hub.store

    # Convenience: callers / tests sometimes use `handler.sense` to
    # build messages — keep it cheap.
    @classmethod
    def _sense(cls) -> str:
        return cls.sense or cls.kind

    # ── get ─────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # `id='/recent'` and similar path views — subclasses may
        # implement custom list shapes (e.g. todo's open / done filters).
        if isinstance(id, str) and id.startswith("/"):
            list_resp = self._list_view(id[1:])
            if list_resp is not None:
                return list_resp
            # Enumerate the actual list views this handler supports
            # rather than pointing at a per-kind help skill that may
            # not exist (e.g. ``precis-fc-help``). The MCP critic
            # flagged the dangling reference as MINOR #5: a caller
            # who follows the "see precis-fc-help" hint dead-ends
            # because the skill file was never written. Always
            # spell the supported views inline so the agent has a
            # working recovery path. (Critic MINOR #5.)
            views = self._supported_list_views()
            view_list = ", ".join(f"/{v}" for v in views) if views else "(none)"
            raise Unsupported(
                f"unknown list view {id!r} for kind={self.kind!r}",
                options=list(views),
                next=f"list views available for {self.kind!r}: {view_list}",
            )
        if id is None and view is None:
            # Bare get → list recent, if subclass supports it.
            list_resp = self._list_view("recent")
            if list_resp is not None:
                return list_resp
            raise BadInput(
                f"{self._sense()} get requires id=",
                next=f"get(kind={self.kind!r}, id=<int>)",
            )

        ref_id = self._coerce_id(id)
        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        if ref is None:
            raise NotFound(
                f"{self._sense()} id={ref_id} not found",
                next=f"search(kind={self.kind!r}, q='...') to find existing",
            )
        if view is not None:
            if view not in _BASE_VIEWS:
                raise Unsupported(
                    f"unknown view {view!r} for kind={self.kind!r}",
                    options=list(_BASE_VIEWS),
                    next=(
                        f"views available for {self.kind!r}: {', '.join(_BASE_VIEWS)}"
                    ),
                )
            if view == "links":
                return self._render_links_view(ref)
        tags = self.store.tags_for(ref.id)
        return Response(body=self._render_one(ref, tags))

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next=f"search(kind={self.kind!r}, q='your query')",
            )
        # Validate at the agent boundary — symmetric with put(tags=...).
        # Pass kind= so per-kind axis enforcement catches
        # STATUS: filter queries against kinds that don't use STATUS.
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)
        hits = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=top_k
        )
        if not hits:
            tag_suffix = f" tagged {normalized_tags}" if normalized_tags else ""
            body = f"no {self._sense()} entries match {q!r}{tag_suffix}"
            # Empty searches should still teach the agent what to try
            # next — broaden the query, drop the tag filter (if any),
            # or fall back to the recent-list view.  Without this, a
            # small-model caller retries the same query, gives up, or
            # guesses at the wrong kind.  (MCP critic MINOR — empty-
            # result responses on search lack recovery hints.)
            nav: list[tuple[str, str]] = []
            nav.append(
                (
                    f"search(kind={self.kind!r}, q='broader term')",
                    "loosen the query",
                )
            )
            if normalized_tags:
                nav.append(
                    (
                        f"search(kind={self.kind!r}, q={q!r})",
                        "drop the tag filter",
                    )
                )
            nav.append(
                (
                    f"get(kind={self.kind!r}, id='/recent')",
                    f"list recent {self._sense()} entries",
                )
            )
            body += render_next_section(nav)
            return Response(body=body)

        # Total-hits header: a second COUNT(*) with the same WHERE
        # clause so the agent sees "10 of 1234 hits" when results are
        # capped by top_k. The MCP critic flagged the missing "of K"
        # readout as a pagination footgun (the agent couldn't tell
        # whether it had everything or just the first page).
        total = self.store.count_refs_lexical(q=q, kind=self.kind, tags=normalized_tags)
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun=f"{self._sense()} match",
                query=q,
            )
        ]
        for ref, rank in hits:
            lines.append(self._render_search_hit(ref, rank))
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Ref-level lexical search returned as ``SearchHit``s.

        Numeric-ref kinds search the ref title only — bodies tend
        to be short enough that one row per ref is the right
        granularity for cross-kind merge.  Subclasses with
        block-level bodies (none today) should override.
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)
        pairs = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=top_k
        )
        return ref_hits_to_search_hits(pairs, kind=self.kind)

    # ── put ────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Validate ``mode=`` up front. The MCP critic flagged
        # ``mode='unlink'`` / ``mode='untag'`` as silently succeeding
        # without doing anything — both are misuses now that ``unlink=``
        # and ``untags=`` are first-class kwargs, but the previous
        # code path treated any non-'delete' mode as a no-op. Reject
        # explicitly with the actual supported list so an agent
        # picking ``mode='note'`` from a stale doc gets a sharp error
        # rather than a phantom success.
        if mode is not None and mode not in _SUPPORTED_PUT_MODES:
            raise BadInput(
                f"unknown mode {mode!r} for kind={self.kind!r}",
                options=list(_SUPPORTED_PUT_MODES),
                next=(
                    "to remove tags use untags=[...]; to remove a link use "
                    "unlink='kind:identifier'; to soft-delete use "
                    f"put(kind={self.kind!r}, id=N, mode='delete')"
                ),
            )
        if mode == "delete":
            return self._delete(id)

        # ``link=`` and ``unlink=`` are mutually exclusive — they touch
        # the same row family but in opposite directions, and a single
        # call asking for both is almost always a misunderstanding.
        # Reject loudly rather than silently apply both.
        if link is not None and unlink is not None:
            raise BadInput(
                "link= and unlink= are mutually exclusive",
                next=(
                    "issue two put() calls if you want to remove one "
                    "link and add another"
                ),
            )

        # ``rel=`` only makes sense with link/unlink. A bare ``rel=``
        # is a no-op today, and silently swallowing it would let a
        # typo (``link='x', rel='cites'`` typed as ``rel='cites'``)
        # vanish into the void.
        if rel is not None and link is None and unlink is None:
            raise BadInput(
                "rel= requires link= or unlink=",
                next=(f"put(kind={self.kind!r}, id=N, link='paper:slug', rel='cites')"),
            )

        if id is None:
            if untags:
                # Untag-on-create has no meaning: there's nothing to
                # remove. Reject loudly rather than silently swallow
                # the kwarg.
                raise BadInput(
                    "untags= is not supported on create",
                    next=(
                        f"create first with put(kind={self.kind!r}, "
                        "text='...'), then put(id=N, untags=[...])"
                    ),
                )
            if unlink is not None:
                raise BadInput(
                    "unlink= is not supported on create",
                    next=(
                        f"create first with put(kind={self.kind!r}, "
                        "text='...'), then put(id=N, unlink=...)"
                    ),
                )
            return self._create(text=text, tags=tags, link=link, rel=rel)
        return self._update(
            self._coerce_id(id),
            text=text,
            tags=tags,
            untags=untags,
            link=link,
            unlink=unlink,
            rel=rel,
        )

    # ── private CRUD ───────────────────────────────────────────────

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
    ) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        # Resolve the link target *before* the insert, so a bad
        # ``link='paper:doesnotexist'`` doesn't leave a half-created
        # ref in the corpus. The parser hits the DB but doesn't
        # mutate; the insert + link both happen below.
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)

        # Pre-validate every tag *before* we touch the DB. The MCP
        # critic flagged a state-drift bug: a put-create that failed
        # tag validation still committed the ref insert, leaving a
        # ghost row behind. Now any BadInput from ``Tag.parse_strict``
        # is raised before ``insert_ref`` runs, so a rejected create
        # writes nothing. (Critic MAJOR #1.)
        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]

        # All validation done — now do every DB write inside a single
        # transaction so the ref + tags + link land atomically. If
        # any of the tag inserts trips a constraint we haven't
        # captured at validation time, the surrounding ``tx()`` will
        # roll back the ref insert too.
        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus(self.corpus_slug)
            ref = self.store.insert_ref(
                corpus_id=corpus_id,
                kind=self.kind,
                slug=None,
                title=text,
                meta={},
                conn=conn,
            )
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation,
                    conn=conn,
                )
        return self._render_create_ack(ref.id)

    def _update(
        self,
        ref_id: int,
        *,
        text: str | None,
        tags: list[str] | None,
        untags: list[str] | None = None,
        link: str | None,
        unlink: str | None = None,
        rel: str | None = None,
    ) -> Response:
        existing = self.store.get_ref(kind=self.kind, id=ref_id)
        if existing is None:
            raise NotFound(
                f"{self._sense()} id={ref_id} not found",
                next=f"check id with: get(kind={self.kind!r}, id={ref_id})",
            )
        if text is None and not tags and not untags and link is None and unlink is None:
            raise BadInput(
                "update requires at least one of text=, tags=, untags=, link=, unlink=",
                next=(
                    f"put(kind={self.kind!r}, id=N, text='new', "
                    "tags=['STATUS:done']) or unlink='paper:slug'"
                ),
            )
        # Resolve link/unlink targets and validate the relation up
        # front. If any of these are bad we want to reject before
        # touching the ref's title or tag rows.
        link_target = (
            parse_link_target(link, store=self.store) if link is not None else None
        )
        unlink_target = (
            parse_link_target(unlink, store=self.store) if unlink is not None else None
        )
        relation = validate_relation(rel)

        # Pre-validate tags + untags. The MCP critic flagged that a
        # rejected tag mid-update could leave partial state — half
        # the tags applied, the rest skipped. Validate everything
        # *before* the transaction; any BadInput is raised before
        # any DB write happens. (Critic MAJOR #1, write side of the
        # same fix as ``_create``.)
        parsed_tags: list[Tag] = (
            [Tag.parse_strict(t, kind=self.kind) for t in tags] if tags else []
        )
        parsed_untags: list[Tag] = (
            [Tag.parse_strict(t, kind=self.kind) for t in untags] if untags else []
        )

        # All validation done. Atomic update: every write joins a
        # single transaction so a downstream constraint violation
        # rolls the whole update back rather than leaving the ref
        # in a half-mutated state.
        with self.store.tx() as conn:
            if text is not None:
                self.store.update_ref(ref_id, title=text, conn=conn)
            for tag in parsed_tags:
                self.store.add_tag(
                    ref_id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            for tag in parsed_untags:
                self.store.remove_tag(ref_id, tag, conn=conn)
            if link_target is not None:
                self.store.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=link_target.ref_id,
                    dst_pos=link_target.pos,
                    relation=relation,
                    conn=conn,
                )
            if unlink_target is not None:
                # ``rel=`` on unlink is per-relation; absence means
                # "any link to this target at this position". The
                # store method's ``relation=None`` path does the
                # broad delete.
                self.store.remove_link(
                    src_ref_id=ref_id,
                    dst_ref_id=unlink_target.ref_id,
                    dst_pos=unlink_target.pos,
                    relation=relation if rel is not None else None,
                    conn=conn,
                )
        return Response(body=f"updated {self._sense()} id={ref_id}")

    def _delete(self, id: str | int | None) -> Response:
        if id is None:
            raise BadInput(
                "delete requires id=",
                next=f"put(kind={self.kind!r}, id=N, mode='delete')",
            )
        ref_id = self._coerce_id(id)
        self.store.soft_delete_ref(ref_id)
        return Response(body=f"deleted {self._sense()} id={ref_id}")

    def _apply_tags(self, ref_id: int, tags: list[str]) -> None:
        # Strict parse — rejects unknown closed-vocab values
        # (``STATUS:bogus``) and bare flags that collide with a closed
        # value (``'urgent'`` instead of ``'PRIO:urgent'``). Defaults
        # applied via ``default_tags_on_create`` are hard-coded
        # canonical values, so they pass validation unchanged.
        #
        # ``kind=`` is forwarded so per-kind axis enforcement catches
        # closed-axis tags on kinds that don't use that axis (e.g.
        # ``STATUS:open`` on a ``memory``).
        for s in tags:
            tag = Tag.parse_strict(s, kind=self.kind)
            self.store.add_tag(
                ref_id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
            )

    def _remove_tags(self, ref_id: int, untags: list[str]) -> None:
        """Remove a set of tags from ``ref_id``.

        Same strict validation as :meth:`_apply_tags` — the agent
        must spell each tag in canonical form. Closed-prefix tags
        require an explicit value (``STATUS:done``); the empty form
        ``STATUS:`` is rejected at parse time, so we don't risk a
        silent "remove all rows under this prefix" footgun.

        Removal is value-matched: ``untags=['STATUS:open']`` against
        a ref whose current STATUS is ``done`` is a no-op (the SQL
        DELETE finds no row). The store layer is silent on misses,
        which matches the closed-prefix overwrite contract on
        :meth:`_apply_tags` (where adding a different value
        replaces the old one without complaint).
        """
        for s in untags:
            # Per-kind axis enforcement matters on remove too: agents
            # that mistype the axis (``STATUS:`` on a memory) would
            # otherwise get a silent no-op via the value-match path.
            tag = Tag.parse_strict(s, kind=self.kind)
            self.store.remove_tag(ref_id, tag)

    # ── coercion ────────────────────────────────────────────────────

    @classmethod
    def _coerce_id(cls, id: str | int | None) -> int:
        if id is None:
            raise BadInput(
                f"{cls._sense()} operations require id=",
                next=f"put(kind={cls.kind!r}, id=<int>, ...)",
            )
        if isinstance(id, int):
            return id
        try:
            return int(id)
        except (ValueError, TypeError):
            raise BadInput(
                f"{cls._sense()} id must be an integer, got {id!r}",
                next=f"{cls._sense()} ids are integers — see search(kind={cls.kind!r}, q='...')",
            ) from None

    # ── rendering hooks (subclasses may override) ─────────────────

    def _render_links_view(self, ref: Ref) -> Response:
        """Render `view='links'`: outbound + inbound link graph for a ref.

        Outbound rows ("→") are stored on this ref as ``src_ref_id``.
        Inbound rows ("←") have this ref as ``dst_ref_id`` — they
        live on someone else's record. We render them as a separate
        section so the agent doesn't confuse the directions.

        Each link line names the other endpoint in canonical
        ``kind:identifier[~pos]`` form so it can be round-tripped
        back into a future ``put(link=…)`` call without any further
        translation.
        """
        out_links = self.store.links_for(ref.id, direction="out")
        in_links = self.store.links_for(ref.id, direction="in")

        lines = [f"# {self._sense()} {ref.id} — links"]
        if not out_links and not in_links:
            lines.append("")
            lines.append("(no links)")
            lines.append("")
            lines.append(
                f"add one with: put(kind={self.kind!r}, id={ref.id}, "
                "link='kind:identifier', rel='related-to')"
            )
            return Response(body="\n".join(lines))

        # Pre-fetch every distinct ref touched by either side so we
        # render kind:identifier rather than bare ref_ids. One round
        # trip per unique target keeps this O(N) on link count
        # rather than O(N) on (link × DB-query).
        endpoint_ids: set[int] = set()
        for link in out_links:
            endpoint_ids.add(link.dst_ref_id)
        for link in in_links:
            endpoint_ids.add(link.src_ref_id)
        endpoints = self._fetch_endpoints(endpoint_ids)

        if out_links:
            lines.append("")
            lines.append("## outbound")
            for link in out_links:
                lines.append(self._format_link_line(link, endpoints, "→"))
        if in_links:
            lines.append("")
            lines.append("## inbound")
            for link in in_links:
                lines.append(self._format_link_line(link, endpoints, "←"))
        return Response(body="\n".join(lines))

    def _fetch_endpoints(self, ref_ids: set[int]) -> dict[int, Ref]:
        """Bulk-fetch refs by id, returning ``{id: Ref}``.

        Soft-deleted refs are kept in the result so a link to a
        deleted ref still renders (with a deletion marker handled
        by the caller). Missing rows are simply absent from the
        dict — the caller renders an ``<unknown>`` placeholder.
        """
        if not ref_ids:
            return {}
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, corpus_id, kind, slug, title, provider, meta, "
                "       created_at, updated_at, deleted_at "
                "FROM refs WHERE id = ANY(%s)",
                (list(ref_ids),),
            ).fetchall()
        # Reuse the same Ref dataclass everywhere else in the
        # codebase uses; keep the projection in column order.
        from precis.store.store import _row_to_ref

        return {r[0]: _row_to_ref(r) for r in rows}

    @staticmethod
    def _format_link_line(
        link: Link,
        endpoints: dict[int, Ref],
        arrow: str,
    ) -> str:
        """Format one link row as ``arrow kind:id[~pos]  (relation)``.

        The arrow encodes direction:
          - ``→``  this ref's outbound link
          - ``←``  inbound — someone else's ref points here

        For inbound, we display the source ref's identifier so the
        agent can navigate "upstream" easily.
        """
        if arrow == "→":
            other_id, other_pos = link.dst_ref_id, link.dst_pos
        else:
            other_id, other_pos = link.src_ref_id, link.src_pos

        ref = endpoints.get(other_id)
        if ref is None:
            target = f"<unknown ref {other_id}>"
        else:
            handle = ref.slug if ref.slug is not None else str(ref.id)
            target = f"{ref.kind}:{handle}"
            if ref.deleted_at is not None:
                target += " (deleted)"
        if other_pos is not None:
            target += f"~{other_pos}"
        return f"{arrow} {target}  ({link.relation})"

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        """Default single-ref view: id header + body + tag line.

        Subclasses with richer body shape (e.g. fc's Q/A pair) override.
        """
        out = [f"# {self._sense()} {ref.id}", "", ref.title]
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(out)

    def _render_search_hit(self, ref: Ref, rank: float) -> str:
        preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
        return f"\n## {self._sense()} {ref.id}  (rank={rank:.2f})\n{preview}"

    def _render_create_ack(self, ref_id: int) -> Response:
        """Acknowledgement returned by `put` on create. Subclasses
        override to attach a Next: trailer with kind-specific hints."""
        return Response(body=f"created {self._sense()} id={ref_id}")

    def _supported_list_views(self) -> tuple[str, ...]:
        """Names of the list views this kind accepts via ``id='/<view>'``.

        Used by the unsupported-view error path to surface a working
        list of recovery options (the MCP critic flagged a dangling
        ``see precis-fc-help`` hint pointing at a skill file that
        doesn't exist; enumerating views inline avoids that whole
        class of bug).

        Subclasses extending ``_list_view`` should override this and
        include any kind-specific names they handle (todo: ``open``,
        ``done``, …; fc: ``due``). The base class only ships
        ``recent``.
        """
        return ("recent",)

    def _list_view(self, view: str) -> Response | None:
        """Handle ``id='/recent'`` and friends.

        Default returns the most recent 20 refs in reverse-chronological
        order. Subclasses with richer list semantics (todo's open /
        blocked / done filters; fc's due) override.

        Returning ``None`` means "I don't recognize this view" — the
        base then raises ``Unsupported``.
        """
        if view in ("", "recent"):
            refs = self.store.list_refs(kind=self.kind, limit=20)
            if not refs:
                # Even empty lists carry a Next: trailer so the agent
                # has a hint how to populate the kind. The MCP critic
                # flagged the silent empty-trailer response as a
                # consistency violation across kinds.
                body = f"no {self._sense()} entries yet"
                body += render_next_section(
                    [
                        (
                            f"put(kind={self.kind!r}, text='...')",
                            f"create your first {self._sense()}",
                        ),
                    ]
                )
                return Response(body=body)
            lines = [f"# recent {self._sense()} ({len(refs)})"]
            for r in refs:
                preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
                lines.append(f"  {r.id:>4}  {preview}")
            body = "\n".join(lines)
            body += render_next_section(
                [
                    (
                        f"get(kind={self.kind!r}, id=N)",
                        f"read full {self._sense()} text + tags",
                    ),
                    (
                        f"put(kind={self.kind!r}, text='...')",
                        f"capture a new {self._sense()}",
                    ),
                ]
            )
            return Response(body=body)
        return None
