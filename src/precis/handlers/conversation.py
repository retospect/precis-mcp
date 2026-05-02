"""ConversationHandler — durable chat-thread refs.

Slug-addressed kind. Each `conv` ref is a captured conversation;
turns live as ``blocks`` (one block per message in chronological
order). The ref title is a short summary; metadata holds participants
and any thread-level context.

Phase 5 ships a read-only handler — get an overview, get a specific
turn (`~N`), get the whole transcript (`/transcript`), search across
turns. Capture-on-write (a `put` interface that appends messages) is
deferred until the chat-bridge work that produces these threads is
final.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
)
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits


class ConversationHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="conv",
        title="Conversation",
        description=(
            "Durable conversation transcript — slug-addressed, one "
            "block per message turn. Body is capture-on-write; use "
            "tag / link to cross-link to papers, memory, todos."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-9 / seven-verb cutover: conv transcripts are
        # capture-on-write (arrive via the chat-bridge, never written
        # by agents). Cross-linking and tagging ride on the dedicated
        # tag/link verbs; ``put`` is therefore not exposed.
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("conv: store required")
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

        slug, chunk, path_view = _parse_conv_id(str(id))
        ref = resolve_live_slug_ref(self.store, kind="conv", id=slug)

        effective_view = path_view or view
        if chunk is not None:
            return self._render_turn(slug, ref.id, chunk)
        if effective_view == "transcript":
            return self._render_transcript(slug, ref)
        if effective_view is not None:
            raise Unsupported(
                f"unknown conv view {effective_view!r}",
                next="see precis-conv-help — try '/transcript' or '~N'",
            )
        # Default: overview.
        return self._render_overview(slug, ref)

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='conv', q='your query')",
            )
        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="conv",
                id=scope,
                next_hint="search(kind='conv', q='...')",
            )
            scope_ref_id = scope_ref.id
        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=None,  # phase 5 — lexical only for state kinds
            kind="conv",
            scope_ref_id=scope_ref_id,
            limit=top_k,
        )
        if not hits:
            return Response(body=f"no conv turns match {q!r}")
        total = self.store.count_blocks_lexical(
            q=q, kind="conv", scope_ref_id=scope_ref_id
        )
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="turn match",
                query=q,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "?"
            preview = (block.text[:160] + "…") if len(block.text) > 160 else block.text
            lines.append(f"\n## {slug}~{block.pos}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level lexical search returned as ``SearchHit``s.

        State kinds (incl. ``conv``) keep semantic search off in
        phase 5; the lexical path is the only stream that goes
        into the cross-kind merge.
        """
        if not (q and q.strip()):
            return []
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=None,
            kind="conv",
            limit=top_k,
        )
        return block_hits_to_search_hits(triples, kind="conv", excerpt=160)

    # ── seven-verb surface ─────────────────────────────────────────

    def _resolve_conv_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair.

        Rejects chunk selectors / path views — link/tag ops are
        ref-level only.
        """
        slug, chunk, path_view = _parse_conv_id(str(id))
        if chunk is not None or path_view is not None:
            raise BadInput(
                "conv ops operate at ref level — drop the turn "
                "selector / path view from id=",
                next=f"tag(kind='conv', id={slug!r}, ...) or link(kind='conv', id={slug!r}, ...)",
            )
        ref = resolve_live_slug_ref(
            self.store,
            kind="conv",
            id=slug,
            next_hint="search(kind='conv', q='...') to find existing slugs",
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
        """Add/remove conversation tags. Open-tag only (no closed prefixes)."""
        if not add and not remove:
            raise BadInput(
                "tag(kind='conv', id=...) requires add= or remove=",
                next="tag(kind='conv', id='<slug>', add=['topic-debug'])",
            )
        slug, ref_id = self._resolve_conv_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "conv", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="conv",
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
        """Add or remove a link from this conversation to another ref."""
        if target is None:
            raise BadInput(
                "link(kind='conv', id=...) requires target=",
                next="link(kind='conv', id='<slug>', target='paper:slug')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug, ref_id = self._resolve_conv_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="conv",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── render helpers ──────────────────────────────────────────────

    def _render_list(self) -> Response:
        # MCP critic MINOR m2: empty-list paths on read-only kinds
        # still want a Next: trailer so the agent gets a concrete
        # recovery call shape.
        return render_slug_ref_list(
            self.store,
            kind="conv",
            label_plural="conversation(s)",
            limit=20,
            empty_body="no conversations recorded yet",
            empty_next=[
                (
                    "get(kind='skill', id='precis-overview')",
                    "see what kinds this server has",
                ),
            ],
        )

    def _render_overview(self, slug: str, ref: Any) -> Response:
        n_blocks = self.store.count_blocks(ref.id)
        meta = ref.meta or {}
        participants = meta.get("participants") or []
        lines = [f"# {slug}", f"_{ref.title}_"]
        if participants:
            lines.append("participants: " + ", ".join(map(str, participants)))
        lines.append("")
        lines.append(f"{n_blocks} turn{'s' if n_blocks != 1 else ''}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='conv', id='{slug}/transcript')",
                    "read the whole transcript",
                ),
                (f"get(kind='conv', id='{slug}~0')", "read the first turn"),
                (
                    f"search(kind='conv', q='...', scope='{slug}')",
                    "search this thread",
                ),
            ]
        )
        return Response(body=body)

    def _render_transcript(self, slug: str, ref: Any) -> Response:
        blocks = self.store.list_blocks_for_ref(ref.id)
        if not blocks:
            return Response(body=f"{slug}: no turns")
        lines = [f"# {slug} — transcript", f"_{ref.title}_", ""]
        for b in blocks:
            lines.append(f"## turn ~{b.pos}")
            lines.append(b.text)
            lines.append("")
        return Response(body="\n".join(lines).rstrip())

    def _render_turn(self, slug: str, ref_id: int, pos: int) -> Response:
        blocks = self.store.list_blocks_for_ref(ref_id, pos_range=(pos, pos))
        if not blocks:
            raise NotFound(
                f"no turn at ~{pos} in conv {slug!r}",
                next=f"get(kind='conv', id='{slug}/transcript')",
            )
        b = blocks[0]
        return Response(body=f"# {slug}~{pos}\n{b.text}")


def _parse_conv_id(raw: str) -> tuple[str, int | None, str | None]:
    """Parse conv ids: ``slug``, ``slug~N``, ``slug/transcript``."""
    if "~" in raw:
        slug, _, sel = raw.partition("~")
        try:
            pos = int(sel.split("/", 1)[0])
        except ValueError as exc:
            raise BadInput(
                f"unparseable turn selector after ~: {sel!r}",
                next="use '~N' for a single turn",
            ) from exc
        return slug, pos, None
    if "/" in raw:
        slug, _, view = raw.partition("/")
        return slug, None, view
    return raw, None, None
