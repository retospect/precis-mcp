"""ConversationHandler — durable chat-thread refs.

Slug-addressed kind. Each `conv` ref is a captured conversation;
turns live as ``blocks`` (one block per message in chronological
order). The ref title is a short summary; metadata holds participants
and any thread-level context.

Read surface: get an overview, get a specific turn (`~N`), get the
whole transcript (`/transcript`), search across turns.

Capture-on-write (``put``) is intended for the chat-bridge — Hermes'
Discord adapter calls it once per inbound user message and once per
outbound assistant reply. ``msg_id`` makes the append idempotent so
a bridge replay (or a retry storm) does not duplicate turns.
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
    reject_chunk_or_path_view,
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits


class ConversationHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="conv",
        title="Conversation",
        description=(
            "Durable conversation transcript - slug-addressed, one "
            "block per message turn. Body is capture-on-write via "
            "put(id=<slug>, text=..., author=..., msg_id=...) from "
            "the chat-bridge; use tag / link to cross-link to papers, "
            "memory, todos."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
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
                next="see precis-conv-help - try '/transcript' or '~N'",
            )
        # Default: overview.
        return self._render_overview(slug, ref)

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        page_size: int = 10,
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
            limit=page_size,
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
        page_size: int = 10,
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
            limit=page_size,
        )
        return block_hits_to_search_hits(triples, kind="conv", excerpt=160)

    # ── seven-verb surface ─────────────────────────────────────────

    def _resolve_conv_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair.

        Rejects chunk selectors / path views — link/tag ops are
        ref-level only.
        """
        slug, chunk, path_view = _parse_conv_id(str(id))
        reject_chunk_or_path_view(
            kind="conv",
            slug=slug,
            sel=chunk,
            path_view=path_view,
            selector_noun="turn selector",
        )
        ref = resolve_live_slug_ref(
            self.store,
            kind="conv",
            id=slug,
            next_hint="search(kind='conv', q='...') to find existing slugs",
        )
        return slug, ref.id

    # ── put: capture-on-write turn append ──────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        author: str | None = None,
        msg_id: str | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        ref_meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Append a turn to a conv ref (chat-bridge entry point).

        First call with a given ``id`` slug mints the ref using
        ``title`` (or a slug-derived fallback) and ``ref_meta`` as
        the ref-level metadata (platform / guild / channel /
        thread). Subsequent calls just append a block.

        Idempotency: if ``msg_id`` is set and any existing block on
        the ref already carries ``meta.msg_id == msg_id``, the call
        is a no-op. This is what makes a Discord-adapter replay safe
        — the bridge can re-emit the same message and we won't
        duplicate the turn. Discord msg ids are 64-bit snowflakes;
        we store as a string to also fit Slack ``ts`` values
        verbatim if/when that bridge is added.

        Block-level ``meta`` carries per-turn provenance the
        renderer surfaces: ``author``, ``msg_id``, ``ts``,
        ``edited_at``. The block ``text`` is the raw message body.
        Embeddings are populated asynchronously by the existing
        ``embed:bge-m3`` worker; keyword extraction by the
        ``chunk_keywords`` worker.
        """
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='conv') requires id= (the conv slug)",
                next=(
                    "put(kind='conv', id='discord/<guild>/<channel>/"
                    "<thread>', text='...', author='<user>', "
                    "msg_id='<platform-id>')"
                ),
            )
        if text is None or not str(text).strip():
            raise BadInput(
                "put(kind='conv') requires text= (the message body)",
                next=(
                    "put(kind='conv', id='<slug>', text='hello', "
                    "author='alice', msg_id='1234')"
                ),
            )
        slug = str(id).strip()
        body = str(text)
        author_s = (author or "unknown").strip() or "unknown"
        msg_id_s = str(msg_id).strip() if msg_id is not None else None

        ref = self.store.get_ref(kind="conv", id=slug)
        created = False
        if ref is None:
            ref_title = (title or f"conversation {slug}").strip() or slug
            ref_meta_payload = dict(ref_meta or {})
            if msg_id_s is not None and "first_msg_id" not in ref_meta_payload:
                ref_meta_payload["first_msg_id"] = msg_id_s
            ref = self.store.insert_ref(
                kind="conv",
                slug=slug,
                title=ref_title,
                meta=ref_meta_payload,
            )
            created = True

        # Idempotency: a Discord bridge may replay the same message id
        # after a reconnect. Cheap per-ref scan — turns counts cap in
        # the low thousands per conv even for long threads.
        if msg_id_s is not None:
            existing = self.store.list_blocks_for_ref(ref.id)
            for b in existing:
                if (b.meta or {}).get("msg_id") == msg_id_s:
                    return Response(
                        body=(
                            f"{slug}~{b.pos}: already captured "
                            f"(msg_id={msg_id_s!r}); no-op"
                        )
                    )
            next_pos = (existing[-1].pos + 1) if existing else 0
        else:
            next_pos = self.store.count_blocks(ref.id)

        block_meta: dict[str, Any] = dict(meta or {})
        block_meta["author"] = author_s
        if msg_id_s is not None:
            block_meta["msg_id"] = msg_id_s
        # chunk_kind tags the block as a chat turn so cross-kind
        # search renderers can distinguish it from paper paragraphs
        # at hit time. ``conv_message`` is the seeded vocabulary slug
        # (0001_initial.sql line 1578).
        block_meta.setdefault("chunk_kind", "conv_message")

        inserted = self.store.insert_blocks(
            ref.id,
            [BlockInsert(pos=next_pos, text=body, meta=block_meta)],
        )
        assert inserted, "insert_blocks returned no rows"
        verb = "created + appended" if created else "appended"
        return Response(
            body=(
                f"{verb} {slug}~{inserted[0].pos} "
                f"(author={author_s!r}"
                + (f", msg_id={msg_id_s!r}" if msg_id_s else "")
                + ")"
            )
        )

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
        lines = [f"# {slug} - transcript", f"_{ref.title}_", ""]
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
