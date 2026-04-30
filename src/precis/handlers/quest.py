"""QuestHandler — request queue for inter-agent / inter-process work.

Slug-addressed ref kind. Each quest is a request for some work; the
slug is a human-meaningful handle (e.g. ``ingest-paper-acheson2026``).

This first phase implements the same CRUD shape as numeric kinds but
keyed by a user-supplied slug. Status transitions reuse the same
``STATUS:`` closed-prefix tag vocabulary as todos: open / doing /
blocked / done / won't-do.

Future work (deferred): atomic ``claim`` + ``complete`` operations
with worker identity, retries, and dead-letter routing.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store, Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits
from precis.utils.slug import slug_from_text


class QuestHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="quest",
        title="Quest",
        description=(
            "Request queue item — slug-addressed work unit. Status "
            "tracked via STATUS:open|doing|blocked|done|won't-do."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
    )

    _CORPUS_SLUG = "default"
    _DEFAULT_TAGS_ON_CREATE = ("STATUS:open",)

    def __init__(self, *, store: Store) -> None:
        self.store = store

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, str) and id.startswith("/"):
            return self._list_view(id[1:])
        if id is None:
            return self._list_view("recent")

        slug = str(id).strip()
        ref = self.store.get_ref(kind="quest", id=slug)
        if ref is None:
            raise NotFound(
                f"quest slug {slug!r} not found",
                next="search(kind='quest', q='...') to find existing",
            )
        tags = self.store.tags_for(ref.id)
        out = [f"# quest {slug}", "", ref.title]
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return Response(body="\n".join(out))

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='quest', q='your query')",
            )
        hits = self.store.search_refs_lexical(q=q, kind="quest", limit=top_k)
        if not hits:
            body = f"no quest entries match {q!r}"
            body += render_next_section(
                [
                    (
                        "search(kind='quest', q='broader term')",
                        "loosen the query",
                    ),
                    (
                        "get(kind='quest')",
                        "list recent quests",
                    ),
                ]
            )
            return Response(body=body)
        total = self.store.count_refs_lexical(q=q, kind="quest")
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="quest match",
                query=q,
            )
        ]
        for ref, rank in hits:
            preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
            lines.append(f"\n## quest {ref.slug}  (rank={rank:.2f})\n{preview}")
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ───────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Ref-level lexical search returned as ``SearchHit``s."""
        if not (q and q.strip()):
            return []
        pairs = self.store.search_refs_lexical(q=q, kind="quest", limit=top_k)
        return ref_hits_to_search_hits(pairs, kind="quest")

    # ── put ────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        if mode == "delete":
            return self._delete(id)
        if id is None:
            return self._create(text=text, tags=tags)
        return self._update(str(id), text=text, tags=tags)

    def _create(self, *, text: str | None, tags: list[str] | None) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "creating a quest requires text=",
                next="put(kind='quest', text='your request description')",
            )
        # Auto-derive a slug from the text — first 60 chars normalised.
        slug = slug_from_text(text, max_len=60) or "quest"
        # Ensure uniqueness with a numeric suffix.
        existing = self.store.get_ref(kind="quest", id=slug)
        if existing is not None:
            for n in range(2, 1000):
                candidate = f"{slug}-{n}"
                if self.store.get_ref(kind="quest", id=candidate) is None:
                    slug = candidate
                    break
        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus(self._CORPUS_SLUG)
            ref = self.store.insert_ref(
                corpus_id=corpus_id,
                kind="quest",
                slug=slug,
                title=text,
                meta={},
                conn=conn,
            )
        all_tags: list[str] = list(self._DEFAULT_TAGS_ON_CREATE)
        if tags:
            all_tags.extend(tags)
        for s in all_tags:
            tag = Tag.parse(s)
            self.store.add_tag(
                ref.id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
            )
        body = f"created quest {slug!r} (status: open)"
        body += render_next_section(
            [
                (f"put(kind='quest', id={slug!r}, tags=['STATUS:doing'])", "claim it"),
                (f"get(kind='quest', id={slug!r})", "read it back"),
                ("get(kind='quest', id='/open')", "list open quests"),
            ]
        )
        return Response(body=body)

    def _update(
        self,
        slug: str,
        *,
        text: str | None,
        tags: list[str] | None,
    ) -> Response:
        existing = self.store.get_ref(kind="quest", id=slug)
        if existing is None:
            raise NotFound(
                f"quest slug {slug!r} not found",
                next=f"check id with: get(kind='quest', id={slug!r})",
            )
        if text is not None:
            self.store.update_ref(existing.id, title=text)
        if tags:
            for s in tags:
                tag = Tag.parse(s)
                self.store.add_tag(
                    existing.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                )
        if text is None and not tags:
            raise BadInput(
                "update requires at least one of text=, tags=",
                next=f"put(kind='quest', id={slug!r}, tags=['STATUS:done'])",
            )
        return Response(body=f"updated quest {slug!r}")

    def _delete(self, id: str | int | None) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "delete requires id=",
                next="put(kind='quest', id='<slug>', mode='delete')",
            )
        slug = str(id).strip()
        existing = self.store.get_ref(kind="quest", id=slug)
        if existing is None:
            raise NotFound(
                f"quest slug {slug!r} not found",
                next="search(kind='quest', q='...') to find existing",
            )
        self.store.soft_delete_ref(existing.id)
        return Response(body=f"deleted quest {slug!r}")

    # ── list views ─────────────────────────────────────────────────

    def _list_view(self, view: str) -> Response:
        refs = self.store.list_refs(kind="quest", limit=100)
        wanted: frozenset[str] | None
        if view in ("", "recent"):
            wanted = None
        elif view == "open":
            wanted = frozenset({"open", "doing", "blocked"})
        elif view in ("doing", "blocked", "done"):
            wanted = frozenset({view})
        else:
            from precis.errors import Unsupported

            raise Unsupported(
                f"unknown quest list view {view!r}",
                options=["recent", "open", "doing", "blocked", "done"],
                next=(
                    "list views available for 'quest': "
                    "/recent, /open, /doing, /blocked, /done"
                ),
            )

        if wanted is None:
            kept = [(r.slug or "?", r.title, "?") for r in refs]
        else:
            kept = []
            for r in refs:
                tags = self.store.tags_for(r.id)
                status = _status_of(tags)
                if status in wanted:
                    kept.append((r.slug or "?", r.title, status))

        if not kept:
            # MCP critic MINOR m2: empty-list paths still emit a
            # Next: trailer with a concrete create-shape so the agent
            # has somewhere to go from a "no rows" reply.
            body = f"no quests in view {view!r}"
            body += render_next_section(
                [
                    (
                        "put(kind='quest', text='goal text')",
                        "open a new quest",
                    ),
                ]
            )
            return Response(body=body)
        label = view or "recent"
        lines = [f"# {len(kept)} quest ({label})"]
        for slug, title, status in kept:
            preview = (title[:60] + "…") if len(title) > 60 else title
            slug_col = (slug[:30] + "…") if len(slug) > 30 else slug
            if status == "?":
                lines.append(f"  {slug_col:<30}  {preview}")
            else:
                lines.append(f"  {slug_col:<30}  [{status:<7}]  {preview}")
        return Response(body="\n".join(lines))


def _status_of(tags: list) -> str:  # type: ignore[type-arg]
    for t in tags:
        if str(t).startswith("STATUS:"):
            return str(t)[len("STATUS:") :]
    return "open"
