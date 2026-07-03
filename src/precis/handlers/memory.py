"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Subclasses :class:`NumericRefHandler` — the shared
CRUD shape lives in one place across memory / todo / gripe / flashcard / conv.

Storage (migration 0050): a memory's **prose lives in a ``memory_body``
chunk**, and ``refs.title`` holds a short **title** — the header. This is
the ``gripe_body`` shape: the body chunk is embedded + keyworded by the
standard workers for free, and display reads a real title instead of
re-splitting the first line off the body every time. Dreams are memories,
so this is the "a dream lives in a chunk, not the ref header" model.

Semantics (`precis-memory-help`):
    - put(text=..., title=...)     — create; text=body prose, title=header
                                     (title derived from the first line when
                                     omitted). Emit the body, then the title.
    - edit(id=N, text=..., title=) — in-place body rewrite (+ optional title)
    - tag(id=N, add=[...])         — add/replace tags on memory N
    - link(id=N, target='kind:id') — cross-link memory N to another ref
    - delete(id=N)                 — soft-delete memory N
    - get(id=N)                    — read title + body + tags
    - get(id='/recent')            — list recent memories
    - search(q=...)                — lexical search over the body chunks
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.handlers._tag_redirect import redirect_long_tag_values
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import BlockInsert, Ref
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit

#: Max memories that one ``supersede`` call may fold into a survivor.
#: A guardrail, not a quota — the agent can do several small merges.
#: Bounds the blast radius / review cost of a single consolidation;
#: a 30-way merge is almost always over-eager.
_SUPERSEDE_MAX_MERGE = 10

#: Provenance tag forced onto every supersede survivor.
_DREAM_CONSOLIDATED = Tag.closed("DREAM", "consolidated")

#: Chunk kind holding a memory's body prose (migration 0050). The prose
#: moved off ``refs.title`` — the header is a short title, the body a chunk
#: (embedded + keyworded like ``gripe_body``).
_BODY_KIND = "memory_body"

#: Longest auto-derived title (first line, capped) when a writer doesn't
#: pass an explicit ``title=``. Matches the migration-0050 backfill rule.
_TITLE_MAX = 80


class MemoryHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="memory",
        title="Memory",
        description=(
            "Notes, decisions, ideas, questions. Numeric id assigned on "
            "create. Short title in the header, body prose in a chunk. "
            "Sub-kind via 'kind:' open tag."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        # In-place rewrite via edit(mode='replace', text='...') (broad-pass
        # finding #5). Same id, links stay attached, audit trail lands
        # in ref_events as a ``body_replaced`` row (view='log').
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "memory"
    sense: ClassVar[str] = "memory"

    # The body lives in a `memory_body` chunk (ord>=0), embedded + keyworded
    # by the standard workers — that chunk is the memory's single embed
    # source. No `card_combined` card any more (migration 0050): emitting
    # both would double-embed the same prose.
    emits_card: ClassVar[bool] = False

    # On create/edit, resolve `kind:ref` handles in the body and write
    # `related-to` links to them — so a memory is findable from the refs
    # it cites, not just by its own text. See `_sync_mention_links`.
    autolink_mentions: ClassVar[bool] = True

    # Reinforce first-line discipline at write time (filler-title nudge +
    # skill pointer in the create ack). See precis-firstline-help.
    firstline_discipline: ClassVar[bool] = True

    #: Set by :meth:`put` for the duration of one create so :meth:`_create`
    #: can pick up an explicit ``title=`` the base ``put`` signature drops.
    _pending_title: str | None = None

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        # /recent (base) + /sticky (union of sticky:thread and
        # sticky:global). Broad-pass finding #10. The sticky scope
        # distinction (per-thread vs global) lives on the tag; this
        # view shows the union so the agent can see "what's pinned
        # right now" at a glance.
        return ("recent", "sticky")

    def _list_view(self, view: str) -> Response | None:
        if view == "sticky":
            return self._render_sticky()
        return super()._list_view(view)

    def _render_sticky(self) -> Response:
        """Union of sticky:thread and sticky:global, recency-ordered."""
        thread = self.store.list_refs(kind=self.kind, tags=["sticky:thread"], limit=50)
        glob = self.store.list_refs(kind=self.kind, tags=["sticky:global"], limit=50)
        # Dedup by ref.id (a memory can carry both tags).
        seen: set[int] = set()
        refs = []
        for r in sorted(
            list(thread) + list(glob),
            key=lambda r: r.updated_at,
            reverse=True,
        ):
            if r.id in seen:
                continue
            seen.add(r.id)
            refs.append(r)
        if not refs:
            return Response(
                body=(
                    "no sticky memories. pin one: "
                    "tag(kind='memory', id=N, add=['sticky:thread'])"
                )
            )
        header = (
            f"# {len(refs)} sticky {self._sense()} "
            f"entr{'y' if len(refs) == 1 else 'ies'}"
        )
        return Response(body=f"{header}\n{self._render_hits_table(refs)}")

    # ── put / create: ref (title) + memory_body chunk ───────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a memory. ``text=`` is the body prose, ``title=`` the short
        header. Emit the body first, then a title once its point is clear.

        When ``title=`` is omitted it's derived from the body's first line
        (capped at 80 chars) — but an explicit title reads better and is
        easier to navigate, so pass one. The body lands in a ``memory_body``
        chunk (embedded + keyworded); ``refs.title`` carries the header.
        """
        self._pending_title = (
            title.strip() if isinstance(title, str) and title.strip() else None
        )
        try:
            return super().put(
                id=id,
                text=text,
                mode=mode,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
                auto_refresh_days=auto_refresh_days,
            )
        finally:
            self._pending_title = None

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
    ) -> Response:
        # Mirror NumericRefHandler._create, but the body prose goes into a
        # `memory_body` chunk (not `refs.title`) and the header gets a short
        # title. The body chunk picks up embeddings + keywords from the
        # standard workers automatically — no `card_combined` card.
        from precis.handlers._link_tag_ops import validate_relation
        from precis.handlers._link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text= (the body prose)",
                next=(
                    f"put(kind={self.kind!r}, text='the thought', title='short title')"
                ),
            )
        body = text
        title = self._pending_title or _derive_title(body)
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)

        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=title,
                meta={},
                auto_refresh_days=auto_refresh_days,
                conn=conn,
            )
            # Body → memory_body chunk at pos 0, written *before* the tag
            # redirect below (which appends any overflow chunk at the next
            # free ord), so the body always sits at ord 0.
            self.store.insert_blocks(
                ref.id,
                [BlockInsert(pos=0, text=body, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
            # Redirect long/whitespace ask-user:/halt: yields into an
            # overflow chunk before parse_strict's whitespace guard fires
            # (gripe #39254). On the create connection so it lands atomically.
            redirected, _redirected_chunks = redirect_long_tag_values(
                self.store, ref_id=ref.id, tags=all_tag_strs, conn=conn
            )
            parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in redirected]
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
            if self.autolink_mentions:
                self._sync_mention_links(ref.id, body, conn=conn)
        return self._with_first_line_nudge(self._render_create_ack(ref.id), title)

    def _create_ack_next_hints(self, ref_id: int) -> list[tuple[str, str]]:
        """Lead the create-ack trailer with the title/first-line convention,
        then the generic read/tag/delete recipes."""
        return [
            (
                "get(kind='skill', id='precis-firstline-help')",
                "title conventions (lead with the conclusion)",
            ),
            *super()._create_ack_next_hints(ref_id),
        ]

    # ── edit: in-place body rewrite (+ optional title) ──────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        mode: str = "replace",
        text: str | None = None,
        title: str | None = None,
        **_kw: Any,
    ) -> Response:
        """In-place rewrite of a memory's body prose.

        Only ``mode='replace'`` is supported. ``text=`` carries the new body;
        ``title=`` optionally updates the header (omit to keep the existing
        title). The old body is preserved in ``ref_events`` so the rewrite is
        recoverable / auditable via ``get(kind='memory', id=N, view='log')``.
        The ``memory_body`` chunk is delete+reinserted so semantic search +
        keywords re-derive from the new prose (an in-place UPDATE would leave
        a stale embedding).

        Distinct from ``supersede`` (the consolidate-into-new verb): replace
        keeps the same id and every inbound link — the "polish the wording"
        affordance.
        """
        if id is None:
            raise BadInput(
                "edit(kind='memory') requires id=",
                next="edit(kind='memory', id=N, mode='replace', text='new body')",
            )
        if mode != "replace":
            raise BadInput(
                f"edit(kind='memory') only supports mode='replace', got {mode!r}",
                next=("edit(kind='memory', id=N, mode='replace', text='new body')"),
            )
        if text is None or not text.strip():
            raise BadInput(
                "edit(kind='memory', mode='replace') requires text=",
                next="edit(kind='memory', id=N, mode='replace', text='new body')",
            )
        ref_id = self._coerce_id(id)
        # _resolve_live_ref raises NotFound/Gone with the right taxonomy if
        # the memory doesn't exist or was soft-deleted.
        ref = self._resolve_live_ref(ref_id)
        new_title = title.strip() if isinstance(title, str) and title.strip() else None
        with self.store.tx() as conn:
            old_body = self.store.replace_body_chunk(
                ref.id, text, chunk_kind=_BODY_KIND, source="agent", conn=conn
            )
            if new_title is not None:
                self.store.set_ref_title(ref.id, new_title, source="agent", conn=conn)
            # Re-sync auto-mention links to the rewritten body: drop the old
            # auto links, add the current ones. Hand-added links survive.
            self._sync_mention_links(ref.id, text, conn=conn, replace=True)
        nudge = self._first_line_nudge(new_title) if new_title is not None else None
        old_words = len((old_body or "").split())
        new_words = len(text.split())
        body = (
            f"replaced body of {self._sense()} id={ref.id} "
            f"({old_words} → {new_words} words)."
        )
        if new_title is not None:
            body += f" title now: {new_title!r}."
        body += " view='log' for the full diff."
        if nudge:
            body += f"\n\nhint: {nudge}"
        return Response(body=body)

    # ── read: title header + body chunk ─────────────────────────────

    def _body_text(self, ref: Ref) -> str:
        """The memory's body prose from its ``memory_body`` chunk.

        Falls back to ``refs.title`` for any pre-0050 row that never got a
        body chunk (defensive — the migration backfilled every live memory).
        """
        for block in self.store.list_blocks_for_ref(ref.id):
            if block.chunk_kind == _BODY_KIND:
                return block.text
        return ref.title or ""

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:  # type: ignore[override]
        """Single-ref view: ``# memory <id>: <title>`` + body + tag line."""
        title = ref.title or ""
        header = f"# {self._sense()} {ref.id}"
        if title:
            header += f": {title}"
        out = [header, "", self._body_text(ref)]
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(out)

    def _render_hits_table(self, refs: list[Ref]) -> str:  # type: ignore[override]
        """List view: ``refs.title`` is now a real title, so show it verbatim
        as the summary and count the body words hidden behind the id.

        Body-word counts come from one batched query over the ``memory_body``
        chunks (no N+1) rather than the base class's first-line split.
        """
        from datetime import UTC, datetime

        from precis.format import render_agent_table
        from precis.utils import handle_registry

        if not refs:
            return ""
        ref_ids = [r.id for r in refs]
        link_counts = self.store.count_links_for_refs(ref_ids)
        body_words = self.store.chunk_word_counts(ref_ids, chunk_kind=_BODY_KIND)
        now = datetime.now(UTC)
        rows: list[dict[str, str]] = []
        for r in refs:
            age_days = max(0, (now - r.updated_at).days)
            rows.append(
                {
                    "kind": self.kind,
                    "id": handle_registry.try_format(self.kind, r.id) or str(r.id),
                    "summary": r.title or "",
                    "body_words": str(body_words.get(r.id, 0)),
                    "links": str(link_counts.get(r.id, 0)),
                    "age": str(age_days),
                }
            )
        schema = ["kind", "id", "summary", "body_words", "links", "age"]
        return render_agent_table(rows, schema=schema)

    # ── search: query the body chunks and group by ref ──────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        """Lexical search over memory *body chunks*.

        The base class matches ``refs.title`` only, but the body now lives in
        a ``memory_body`` chunk — a title-only search would miss the prose.
        We route through ``search_blocks_lexical`` (mirrors gripe) and group
        by ref so each matching memory shows once with its best chunk teaser.
        Semantic ``like=`` retrieval is unaffected (it already keys on the
        embedded chunk). ``tags=`` without ``q=`` degrades to the base
        recency-ordered list view.
        """
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)
        if q is None or not q.strip():
            if normalized_tags:
                return self._list_by_tags(normalized_tags, page_size=page_size)
            raise BadInput(
                "search requires q= or tags=",
                next=(
                    f"search(kind={self.kind!r}, q='your query') or "
                    f"search(kind={self.kind!r}, tags=['<tag>'])"
                ),
            )
        raw = self.store.search_blocks_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=page_size * 5
        )
        best_by_ref: dict[int, tuple[Any, Ref, float]] = {}
        for block, ref, rank in raw:
            existing = best_by_ref.get(ref.id)
            if existing is None or rank > existing[2]:
                best_by_ref[ref.id] = (block, ref, rank)
        hits = sorted(best_by_ref.values(), key=lambda t: t[2], reverse=True)[
            :page_size
        ]
        # Heat salience on the hit refs' body chunks (recall reinforcement).
        self.store.bump_salience(
            self.store.card_chunk_ids([ref.id for _, ref, _ in hits])
        )
        if not hits:
            tag_suffix = f" tagged {normalized_tags}" if normalized_tags else ""
            body = f"no {self._sense()} entries match {q!r}{tag_suffix}"
            nav: list[tuple[str, str]] = [
                (f"search(kind={self.kind!r}, q='broader term')", "loosen the query")
            ]
            if normalized_tags:
                nav.append(
                    (f"search(kind={self.kind!r}, q={q!r})", "drop the tag filter")
                )
            nav.append(
                (
                    f"get(kind={self.kind!r}, id='/recent')",
                    f"list recent {self._sense()} entries",
                )
            )
            body += render_next_section(nav)
            return Response(body=body)

        total = len(best_by_ref)
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun=f"{self._sense()} match",
                query=q,
            )
        ]
        for block, ref, rank in hits:
            title = ref.title or ""
            label = f"{self._sense()} {ref.id}"
            if title:
                label += f": {title}"
            lines.append(f"\n## {label}  (rank={rank:.2f})\n{_snippet(block.text)}")
        return Response(body="\n".join(lines))

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Ref-grouped chunk-level hits for the cross-kind merge."""
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)
        raw = self.store.search_blocks_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=page_size * 5
        )
        best_by_ref: dict[int, tuple[Any, Ref, float]] = {}
        for block, ref, rank in raw:
            existing = best_by_ref.get(ref.id)
            if existing is None or rank > existing[2]:
                best_by_ref[ref.id] = (block, ref, rank)
        ordered = sorted(best_by_ref.values(), key=lambda t: t[2], reverse=True)[
            :page_size
        ]
        return [
            SearchHit(
                score=rank,
                kind=self.kind,
                title=ref.title or _snippet(block.text, max_chars=80),
                preview=_snippet(block.text),
                ref_id=ref.id,
            )
            for block, ref, rank in ordered
        ]

    # ── supersede: the one guarded destructive verb (dreaming) ──────

    def supersede(
        self,
        *,
        merge_ids: list[Any] | None = None,
        new_text: str | None = None,
        new_title: str | None = None,
        new_tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Consolidate >=2 near-duplicate memories into one survivor.

        The single guarded compress-only merge a dream uses instead of raw
        ``delete`` (docs/design/dreaming.md, §Consolidate). In one
        transaction: mint a new ``memory`` (title + ``memory_body`` chunk +
        merged tags), migrate every link off each original onto the survivor,
        add ``survivor --supersedes--> original`` edges, stamp
        ``meta.superseded_by`` and soft-delete the originals.

        Hard guards (enforced here, never the prompt):

        - ``merge_ids``: 2..10 *distinct* live ``memory`` ids. Papers (or any
          non-memory kind) are refused — papers are never merged or deleted.
        - ``new_text``: required, and **compress-only** — no longer than the
          combined original *bodies* (a merge may forget a nuance, never
          invent a claim).
        - ``new_title``: optional header; derived from ``new_text`` when
          omitted.

        A bad call raises a typed ``BadInput`` the agent can read and retry;
        it can never corrupt or hard-delete.
        """
        if not merge_ids or not isinstance(merge_ids, list):
            raise BadInput(
                "supersede requires merge_ids=[id, id, ...] (>= 2 memory ids)",
                next="supersede(merge_ids=[12, 47], new_text='merged wording')",
            )
        # Coerce + dedup preserving order; a repeated id is a mistake,
        # not a 2-way merge.
        seen_ids: set[int] = set()
        ids: list[int] = []
        for raw in merge_ids:
            mid = self._coerce_id(raw)
            if mid not in seen_ids:
                seen_ids.add(mid)
                ids.append(mid)
        if len(ids) < 2:
            raise BadInput(
                f"supersede needs >= 2 distinct memory ids, got {len(ids)}",
                next="pick two or more different memories to merge",
            )
        if len(ids) > _SUPERSEDE_MAX_MERGE:
            raise BadInput(
                f"supersede caps at {_SUPERSEDE_MAX_MERGE} memories per merge, "
                f"got {len(ids)}",
                next="split into smaller, reviewable merges",
            )
        if new_text is None or not new_text.strip():
            raise BadInput(
                "supersede requires new_text= (the consolidated memory)",
                next="supersede(merge_ids=[...], new_text='the merged wording')",
            )

        # Every id must resolve to a *live memory*. get_ref(kind='memory')
        # returns None for a wrong kind, a missing id, or a soft-deleted
        # row — all three are caller errors here.
        originals = []
        for mid in ids:
            ref = self.store.get_ref(kind="memory", id=mid)
            if ref is None:
                raise BadInput(
                    f"supersede: id={mid} is not a live memory "
                    "(wrong kind, missing, or already deleted)",
                    next=f"get(kind='memory', id={mid}) to check",
                )
            originals.append(ref)

        # Compress-only: the survivor body may not be longer than the sum of
        # the originals' bodies it absorbs. Forgetting a nuance is the
        # accepted loss; inventing new claims is not, and length is the cheap
        # proxy the tool can enforce.
        combined_len = sum(len(self._body_text(r)) for r in originals)
        if len(new_text) > combined_len:
            raise BadInput(
                f"supersede is compress-only: new_text ({len(new_text)} chars) "
                f"exceeds the combined originals ({combined_len} chars)",
                next="shorten new_text — a merge compresses, it never expands",
            )

        # Resolve the tag set before touching the DB so a bad explicit tag
        # fails before any write. Default = union of the originals' OPEN tags
        # (control/closed tags like STATUS:/DREAM: are dropped); the survivor
        # always carries DREAM:consolidated.
        if new_tags is not None:
            tag_objs = [Tag.parse_strict(t, kind="memory") for t in new_tags]
        else:
            tag_objs = []
            seen_tags: set[str] = set()
            for r in originals:
                for t in self.store.tags_for(r.id):
                    if t.namespace != "open":
                        continue
                    key = str(t)
                    if key not in seen_tags:
                        seen_tags.add(key)
                        tag_objs.append(t)
        if not any(str(t) == str(_DREAM_CONSOLIDATED) for t in tag_objs):
            tag_objs.append(_DREAM_CONSOLIDATED)

        title = (
            new_title.strip()
            if isinstance(new_title, str) and new_title.strip()
            else _derive_title(new_text)
        )
        with self.store.tx() as conn:
            survivor = self.store.insert_ref(
                kind="memory",
                slug=None,
                title=title,
                meta={"superseded": ids},
                conn=conn,
            )
            self.store.insert_blocks(
                survivor.id,
                [BlockInsert(pos=0, text=new_text, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
            for tag in tag_objs:
                self.store.add_tag(
                    survivor.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            for mid in ids:
                self.store.migrate_links(mid, survivor.id, conn=conn)
                self.store.add_link(
                    src_ref_id=survivor.id,
                    dst_ref_id=mid,
                    relation="supersedes",
                    set_by="agent",
                    conn=conn,
                )
                self.store.stamp_ref_meta(
                    mid, {"superseded_by": survivor.id}, conn=conn
                )
                self.store.soft_delete_ref(mid, conn=conn)

        merged = ", ".join(str(m) for m in ids)
        return Response(
            body=(
                f"superseded memories [{merged}] → new memory id={survivor.id} "
                f"(originals soft-deleted, links migrated, tagged "
                f"{_DREAM_CONSOLIDATED})"
            )
        )


def _derive_title(body: str, *, max_len: int = _TITLE_MAX) -> str:
    """Derive a short header title from body prose.

    The first non-empty line (the existing first-line-is-summary
    discipline), capped at ``max_len`` chars with an ellipsis. The graceful
    default when a writer doesn't emit an explicit ``title=`` — matches the
    migration-0050 backfill so old and new memories read the same.
    """
    first = ""
    for line in (body or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        first = (body or "").strip()
    if len(first) > max_len:
        return first[: max_len - 1].rstrip() + "…"
    return first


def _snippet(text: str, *, max_chars: int = 200) -> str:
    """Trim a chunk's text for inline display in search results."""
    flat = " ".join((text or "").split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + "…"
