"""OracleHandler — saved long-form prompts / authoritative reference nodes.

Slug-addressed, durable. Each `oracle` is a curated prompt that
agents can fetch verbatim — e.g. ``oracle:reviewer-rigor`` returns the
canonical reviewer rubric, ``oracle:cite-style`` returns the citation
style guide.

Shape mirrors :class:`ConversationHandler` (slug get + lexical search +
list view), minus the per-turn navigation. Phase 5 ships a read-only
handler; future ``put`` adds versioning.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store


class OracleHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="oracle",
        title="Oracle",
        description=(
            "Authoritative reference node — slug-addressed, curated "
            "prompt or rubric. Read-only in phase 5."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, store: Store) -> None:
        self.store = store

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_list()
        slug = str(id).strip()
        ref = self.store.get_ref(kind="oracle", id=slug)
        if ref is None:
            raise NotFound(
                f"oracle slug {slug!r} not found",
                next="search(kind='oracle', q='...') to find existing",
            )
        # The full body lives in the first block (or in title for short
        # oracles). We render whichever has content.
        blocks = self.store.list_blocks_for_ref(ref.id)
        body_text = "\n\n".join(b.text for b in blocks) if blocks else ref.title
        return Response(body=f"# oracle {slug}\n_{ref.title}_\n\n{body_text}")

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
                next="search(kind='oracle', q='your query')",
            )
        hits = self.store.search_refs_lexical(q=q, kind="oracle", limit=top_k)
        if not hits:
            return Response(body=f"no oracle entries match {q!r}")
        lines = [f"# {len(hits)} oracle match(es) for {q!r}"]
        for ref, rank in hits:
            preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
            lines.append(f"\n## oracle {ref.slug}  (rank={rank:.2f})\n{preview}")
        return Response(body="\n".join(lines))

    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="oracle", limit=50)
        if not refs:
            return Response(body="no oracles defined yet")
        lines = [f"# {len(refs)} oracle(s)"]
        for r in refs:
            preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
            lines.append(f"  {(r.slug or '?'):<30}  {preview}")
        return Response(body="\n".join(lines))
