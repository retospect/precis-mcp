"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Stored in `refs` (kind='memory') with the memory
text in `refs.title` (postgres TEXT has no length limit, so short and
medium memories fit fine; long memories will get block-split in a
future phase).

Semantics from the `precis-memory-help` skill:
    - put(text=...)            — create new memory, return its id
    - put(id=N, text=...)      — replace memory N's text
    - put(id=N, mode='delete') — soft-delete memory N
    - put(id=N, tags=[...])    — add/replace tags on memory N
    - get(id=N)                — read memory text + tags
    - search(q=...)            — lexical search over memories
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store, Tag


class MemoryHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="memory",
        title="Memory",
        description=(
            "Notes, decisions, ideas, questions. Numeric id assigned on "
            "create. Sub-kind via 'kind:' open tag."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        is_numeric=True,
        id_required=False,
    )

    _CORPUS_SLUG = "default"

    def __init__(self, *, store: Store) -> None:
        self.store = store

    # -- get -----------------------------------------------------------------

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        ref_id = self._coerce_id(id)
        ref = self.store.get_ref(kind="memory", id=ref_id)
        if ref is None:
            raise NotFound(
                f"memory id={ref_id} not found",
                next="search(kind='memory', q='...') to find existing",
            )
        tags = self.store.tags_for(ref.id)
        return Response(body=self._render_one(ref.id, ref.title, tags))

    # -- search --------------------------------------------------------------

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
                next="search(kind='memory', q='your query')",
            )
        hits = self.store.search_refs_lexical(q=q, kind="memory", limit=top_k)
        if not hits:
            return Response(body=f"no memories match {q!r}")

        lines = [
            f"# {len(hits)} memor{'y' if len(hits) == 1 else 'ies'} matching {q!r}"
        ]
        for ref, rank in hits:
            preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
            lines.append(f"\n## memory {ref.id}  (rank={rank:.2f})")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # -- put -----------------------------------------------------------------

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

    # -- private impls -------------------------------------------------------

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
    ) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "creating a memory requires text=",
                next="put(kind='memory', text='your note')",
            )

        with self.store.tx() as conn:
            corpus_id = self.store.ensure_corpus(self._CORPUS_SLUG)
            ref = self.store.insert_ref(
                corpus_id=corpus_id,
                kind="memory",
                slug=None,
                title=text,
                meta={},
                conn=conn,
            )
        if tags:
            self._apply_tags(ref.id, tags)
        # Phase 2: link= deferred until links CRUD lands.
        if link is not None:
            # silently ignored; phase 2 doesn't wire links yet
            pass
        return Response(body=f"created memory id={ref.id}")

    def _update(
        self,
        ref_id: int,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
    ) -> Response:
        # Verify it exists and isn't soft-deleted
        existing = self.store.get_ref(kind="memory", id=ref_id)
        if existing is None:
            raise NotFound(
                f"memory id={ref_id} not found",
                next=f"check id with: get(kind='memory', id={ref_id})",
            )

        if text is not None:
            self.store.update_ref(ref_id, title=text)

        if tags:
            self._apply_tags(ref_id, tags)

        if link is not None:
            pass  # deferred to phase with links CRUD

        if text is None and not tags and link is None:
            raise BadInput(
                "update requires at least one of text=, tags=, link=",
                next="put(kind='memory', id=N, text='new', tags=['kind:note'])",
            )
        return Response(body=f"updated memory id={ref_id}")

    def _delete(self, id: str | int | None) -> Response:
        if id is None:
            raise BadInput(
                "delete requires id=",
                next="put(kind='memory', id=N, mode='delete')",
            )
        ref_id = self._coerce_id(id)
        self.store.soft_delete_ref(ref_id)
        return Response(body=f"deleted memory id={ref_id}")

    def _apply_tags(self, ref_id: int, tags: list[str]) -> None:
        """Apply a list of tag strings. Closed-prefix tags replace any
        previous tag with the same prefix (per skill semantics)."""
        for s in tags:
            tag = Tag.parse(s)
            self.store.add_tag(
                ref_id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
            )

    @staticmethod
    def _coerce_id(id: str | int | None) -> int:
        if id is None:
            raise BadInput(
                "memory operations require id=",
                next="put(kind='memory', id=<int>, ...)",
            )
        if isinstance(id, int):
            return id
        try:
            return int(id)
        except (ValueError, TypeError):
            raise BadInput(
                f"memory id must be an integer, got {id!r}",
                next="memory ids are integers — see search(kind='memory', q='...')",
            ) from None

    @staticmethod
    def _render_one(ref_id: int, body: str, tags: list[Tag]) -> str:
        out = [f"# memory {ref_id}", "", body]
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(out)
