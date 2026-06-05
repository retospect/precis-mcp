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

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, Unsupported
from precis.handlers._link_tag_ops import apply_link_ops, apply_tag_ops
from precis.handlers._slug_ref_shared import (
    resolve_live_slug_ref,
    search_hits_slug_refs,
    search_slug_refs,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_merge import SearchHit
from precis.utils.slug import slug_from_text


class QuestHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="quest",
        title="Quest",
        description=(
            "Request queue item - slug-addressed work unit. Status "
            "tracked via STATUS:open|doing|blocked|done|won't-do."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
    )

    _CORPUS_SLUG = "default"
    _DEFAULT_TAGS_ON_CREATE = ("STATUS:open",)

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("quest: store required")
        self.store = hub.store

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
        ref = resolve_live_slug_ref(self.store, kind="quest", id=slug)
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
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        return search_slug_refs(
            self.store,
            kind="quest",
            q=q,
            page_size=page_size,
            noun="quest match",
            empty_next=[
                (
                    "search(kind='quest', q='broader term')",
                    "loosen the query",
                ),
                (
                    "get(kind='quest')",
                    "list recent quests",
                ),
            ],
        )

    # ── search_hits: structured form for cross-kind merge ───────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Ref-level lexical search returned as ``SearchHit``s."""
        return search_hits_slug_refs(self.store, kind="quest", q=q, page_size=page_size)

    # ── put: create-only ───────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a new quest (slug auto-derived from text).

        Quest mutation splits across the seven-verb surface:

        - tag(kind='quest', id=<slug>, add=[...], remove=[...])
        - link(kind='quest', id=<slug>, target=..., mode='add'|'remove')
        - delete(kind='quest', id=<slug>)
        """
        if id is not None:
            raise BadInput(
                f"put on existing quest id={id!r} is not supported",
                next=(
                    f"to mutate quest {id!r}: tag(kind='quest', id={id!r}, ...) / "
                    f"link(kind='quest', id={id!r}, ...) / delete(kind='quest', id={id!r})"
                ),
            )
        if mode is not None:
            raise BadInput(
                "mode= is not accepted on put for kind='quest'",
                next="put creates a new quest; for delete use delete(kind='quest', id=<slug>)",
            )
        return self._create(text=text, tags=tags)

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
            ref = self.store.insert_ref(
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
                (f"tag(kind='quest', id={slug!r}, add=['STATUS:doing'])", "claim it"),
                (f"get(kind='quest', id={slug!r})", "read it back"),
                ("get(kind='quest', id='/open')", "list open quests"),
            ]
        )
        return Response(body=body)

    def _delete(self, id: str | int | None) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "delete requires id=",
                next="delete(kind='quest', id='<slug>')",
            )
        slug = str(id).strip()
        existing = resolve_live_slug_ref(self.store, kind="quest", id=slug)
        self.store.soft_delete_ref(existing.id)
        return Response(body=f"deleted quest {slug!r}")

    # ── seven-verb surface ─────────────────────────────────────────

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
        """Soft-delete a quest by slug."""
        return self._delete(id)

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add and/or remove tags on an existing quest.

        Quest's primary use case is STATUS transitions
        (``add=['STATUS:doing']`` / ``add=['STATUS:done']``). Closed-
        prefix replacement semantics ensure only one STATUS at a time.
        """
        if not add and not remove:
            raise BadInput(
                "tag(kind='quest', id=...) requires add= or remove=",
                next=(
                    "tag(kind='quest', id='<slug>', add=['STATUS:doing']) or "
                    "tag(kind='quest', id='<slug>', remove=['draft'])"
                ),
            )
        slug = str(id).strip()
        existing = resolve_live_slug_ref(self.store, kind="quest", id=slug)
        # Reuse the shared tag-ops helper so the validate-then-write
        # transactional shape matches every other kind.
        apply_tag_ops(self.store, "quest", existing.id, tags=add, untags=remove)
        return Response(body=f"tagged quest {slug!r}")

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from an existing quest."""
        if target is None:
            raise BadInput(
                "link(kind='quest', id=...) requires target=",
                next="link(kind='quest', id='<slug>', target='paper:slug')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug = str(id).strip()
        existing = resolve_live_slug_ref(self.store, kind="quest", id=slug)
        if mode == "add":
            apply_link_ops(self.store, existing.id, link=target, unlink=None, rel=rel)
            return Response(body=f"linked quest {slug!r} → {target}")
        n_added, n_removed = apply_link_ops(
            self.store, existing.id, link=None, unlink=target, rel=rel
        )
        return Response(
            body=(
                f"unlinked quest {slug!r} ↛ {target} "
                f"({n_removed} edge{'s' if n_removed != 1 else ''} removed)"
            )
        )

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
