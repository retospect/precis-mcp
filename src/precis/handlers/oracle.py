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
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    validate_link_args,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits


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
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='oracle', q='your query')",
            )
        hits = self.store.search_refs_lexical(q=q, kind="oracle", limit=top_k)
        if not hits:
            return Response(body=f"no oracle entries match {q!r}")
        total = self.store.count_refs_lexical(q=q, kind="oracle")
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="oracle match",
                query=q,
            )
        ]
        for ref, rank in hits:
            preview = (ref.title[:140] + "…") if len(ref.title) > 140 else ref.title
            lines.append(f"\n## oracle {ref.slug}  (rank={rank:.2f})\n{preview}")
        return Response(body="\n".join(lines))

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
        if not (q and q.strip()):
            return []
        pairs = self.store.search_refs_lexical(q=q, kind="oracle", limit=top_k)
        return ref_hits_to_search_hits(pairs, kind="oracle")

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

        validate_link_args(link=link, unlink=unlink, rel=rel, kind="oracle")
        if not any((link, unlink, tags, untags)):
            raise BadInput(
                "oracle put requires at least one of link=, unlink=, tags=, untags=",
                next=(f"put(kind='oracle', id={slug!r}, link='paper:other-slug')"),
            )

        n_links_added, n_links_removed = apply_link_ops(
            self.store, ref.id, link=link, unlink=unlink, rel=rel
        )
        n_tags_added, n_tags_removed = apply_tag_ops(
            self.store, "oracle", ref.id, tags=tags, untags=untags
        )
        return Response(
            body=format_link_tag_ack(
                kind="oracle",
                ref_label=slug,
                n_links_added=n_links_added,
                n_links_removed=n_links_removed,
                n_tags_added=n_tags_added,
                n_tags_removed=n_tags_removed,
            )
        )

    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="oracle", limit=50)
        if not refs:
            # Empty-list responses on read-only kinds still teach the
            # agent the next call shape — see-also the help skill
            # rather than just returning a bare sentence. (MCP critic
            # MINOR m2.)
            body = "no oracles defined yet"
            body += render_next_section(
                [
                    (
                        "get(kind='skill', id='precis-overview')",
                        "learn about the kind list",
                    ),
                ]
            )
            return Response(body=body)
        lines = [f"# {len(refs)} oracle(s)"]
        for r in refs:
            preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
            lines.append(f"  {(r.slug or '?'):<30}  {preview}")
        return Response(body="\n".join(lines))
