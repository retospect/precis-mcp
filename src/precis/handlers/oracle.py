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

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
)
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    search_hits_slug_refs,
    search_slug_refs,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils.search_merge import SearchHit


class OracleHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="oracle",
        title="Oracle",
        description=(
            "Authoritative reference node — slug-addressed, curated "
            "prompt or rubric. Read-only body; use tag / link to "
            "cross-link to papers, memory, etc."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-9 / seven-verb cutover: oracle bodies are curated —
        # set externally via the corpus seeding pipeline, never
        # written from the agent surface. Cross-linking and tag
        # classification ride on the dedicated tag/link verbs;
        # ``put`` is therefore not exposed.
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("oracle: store required")
        self.store = hub.store

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

    # ── seven-verb surface ─────────────────────────────────────────

    def _resolve_oracle_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair."""
        slug = str(id).strip()
        ref = self.store.get_ref(kind="oracle", id=slug)
        if ref is None:
            raise NotFound(
                f"oracle slug {slug!r} not found",
                next="search(kind='oracle', q='...') to find existing slugs",
            )
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove oracle tags. Open-tag only (no closed prefixes)."""
        if not add and not remove:
            raise BadInput(
                "tag(kind='oracle', id=...) requires add= or remove=",
                next="tag(kind='oracle', id='<slug>', add=['topic-eval'])",
            )
        slug, ref_id = self._resolve_oracle_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "oracle", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="oracle",
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from this oracle to another ref."""
        if target is None:
            raise BadInput(
                "link(kind='oracle', id=...) requires target=",
                next="link(kind='oracle', id='<slug>', target='paper:slug')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug, ref_id = self._resolve_oracle_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="oracle",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

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
