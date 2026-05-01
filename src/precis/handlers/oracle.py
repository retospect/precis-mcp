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
from precis.handlers._link_tag_ops import apply_link_tag_only_put
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    search_hits_slug_refs,
    search_slug_refs,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.search_merge import SearchHit


class OracleHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="oracle",
        title="Oracle",
        description=(
            "Authoritative reference node — slug-addressed, curated "
            "prompt or rubric. Read-only body; put accepts link/tag "
            "ops only (cross-link to papers, memory, etc.)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-8: cross-linking. Body is curated — set externally
        # via the corpus seeding pipeline. Put surface is link/tag
        # only, same shape as paper.
        supports_put=True,
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
        return search_slug_refs(
            self.store,
            kind="oracle",
            q=q,
            top_k=top_k,
            noun="oracle match",
        )

    # ── search_hits: structured form for cross-kind merge ───────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Title-level lexical search returned as ``SearchHit``s.

        Oracle bodies live in blocks but the canonical search
        surface today indexes the title only — cross-kind merge
        therefore stays consistent with single-kind ``search()``.
        Block-level search is a follow-up.
        """
        return search_hits_slug_refs(self.store, kind="oracle", q=q, top_k=top_k)

    # ── put: link/tag CRUD only (no body mutation) ────────────────

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
        """Apply link/tag operations to an existing oracle ref.

        Oracle bodies are curated content seeded externally — the
        agent shouldn't edit them. Cross-linking (oracle → paper
        that motivates the rubric, oracle → todo that tracks
        revisions) and open-tag classification are useful
        though, so a narrow link/tag put surface is exposed
        here, mirroring :class:`PaperHandler.put`.
        """
        if text is not None:
            raise BadInput(
                "oracle bodies are curated and not writable from put",
                next=(
                    "edit the source oracle in the corpus seed pipeline; "
                    "for cross-links use put(kind='oracle', id=<slug>, "
                    "link='paper:foo')"
                ),
            )
        if mode is not None:
            raise BadInput(
                f"mode={mode!r} not supported for kind='oracle'",
                next=(
                    "oracle put accepts only link/unlink/tags/untags — "
                    "no body modes. Drop the mode= kwarg."
                ),
            )
        if id is None:
            raise BadInput(
                "oracle put requires id= (the oracle slug)",
                next=(
                    "put(kind='oracle', id='<slug>', link='paper:foo') "
                    "— find the slug via search(kind='oracle', q='...')"
                ),
            )
        slug = str(id).strip()
        ref = self.store.get_ref(kind="oracle", id=slug)
        if ref is None:
            raise NotFound(
                f"oracle slug {slug!r} not found",
                next="search(kind='oracle', q='...') to find existing slugs",
            )

        ack = apply_link_tag_only_put(
            self.store,
            kind="oracle",
            ref_id=ref.id,
            ref_label=slug,
            link=link,
            unlink=unlink,
            tags=tags,
            untags=untags,
            rel=rel,
        )
        return Response(body=ack)

    def _render_list(self) -> Response:
        # Empty-list responses on read-only kinds still teach the
        # agent the next call shape — see-also the help skill
        # rather than just returning a bare sentence. (MCP critic
        # MINOR m2.)
        return render_slug_ref_list(
            self.store,
            kind="oracle",
            label_plural="oracle(s)",
            empty_body="no oracles defined yet",
            empty_next=[
                (
                    "get(kind='skill', id='precis-overview')",
                    "learn about the kind list",
                ),
            ],
        )
