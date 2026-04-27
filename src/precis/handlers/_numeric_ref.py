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

from precis.errors import BadInput, NotFound, Unsupported
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Ref, Store, Tag


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

    def __init__(self, *, store: Store) -> None:
        self.store = store

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
            raise Unsupported(
                f"unknown list view {id!r} for kind={self.kind!r}",
                next=f"see precis-{self.kind}-help for available list views",
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
        tags = self.store.tags_for(ref.id)
        return Response(body=self._render_one(ref, tags))

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
                next=f"search(kind={self.kind!r}, q='your query')",
            )
        hits = self.store.search_refs_lexical(q=q, kind=self.kind, limit=top_k)
        if not hits:
            return Response(body=f"no {self._sense()} entries match {q!r}")

        lines = [f"# {len(hits)} {self._sense()} match(es) for {q!r}"]
        for ref, rank in hits:
            lines.append(self._render_search_hit(ref, rank))
        return Response(body="\n".join(lines))

    # ── put ────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        **_kw: Any,
    ) -> Response:
        if mode == "delete":
            return self._delete(id)
        if id is None:
            return self._create(text=text, tags=tags, link=link)
        return self._update(self._coerce_id(id), text=text, tags=tags, link=link)

    # ── private CRUD ───────────────────────────────────────────────

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
    ) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
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
        # Apply default-on-create tags first, then user tags.
        all_tags: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tags.extend(tags)
        if all_tags:
            self._apply_tags(ref.id, all_tags)
        if link is not None:
            pass  # links CRUD lands later
        return self._render_create_ack(ref.id)

    def _update(
        self,
        ref_id: int,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
    ) -> Response:
        existing = self.store.get_ref(kind=self.kind, id=ref_id)
        if existing is None:
            raise NotFound(
                f"{self._sense()} id={ref_id} not found",
                next=f"check id with: get(kind={self.kind!r}, id={ref_id})",
            )
        if text is not None:
            self.store.update_ref(ref_id, title=text)
        if tags:
            self._apply_tags(ref_id, tags)
        if link is not None:
            pass
        if text is None and not tags and link is None:
            raise BadInput(
                "update requires at least one of text=, tags=, link=",
                next=f"put(kind={self.kind!r}, id=N, text='new', tags=['STATUS:done'])",
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
        for s in tags:
            tag = Tag.parse(s)
            self.store.add_tag(
                ref_id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
            )

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
                return Response(body=f"no {self._sense()} entries yet")
            lines = [f"# recent {self._sense()} ({len(refs)})"]
            for r in refs:
                preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
                lines.append(f"  {r.id:>4}  {preview}")
            return Response(body="\n".join(lines))
        return None
