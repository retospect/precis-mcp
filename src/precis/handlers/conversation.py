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

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import apply_link_tag_only_put
from precis.handlers._slug_ref_shared import render_slug_ref_list
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits


class ConversationHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="conv",
        title="Conversation",
        description=(
            "Durable conversation transcript — slug-addressed, one "
            "block per message turn. Body is capture-on-write only; "
            "put accepts link/tag ops only (cross-link to papers, "
            "memory, todos)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-8: cross-linking. Body remains capture-on-write
        # (transcripts arrive via the chat-bridge, not from agent
        # ``put``). The link/tag surface is the same shape as
        # paper/oracle.
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

        slug, chunk, path_view = _parse_conv_id(str(id))
        ref = self.store.get_ref(kind="conv", id=slug)
        if ref is None:
            raise NotFound(
                f"conv slug {slug!r} not found",
                next="search(kind='conv', q='...') to find existing",
            )

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
            scope_ref = self.store.get_ref(kind="conv", id=scope)
            if scope_ref is None:
                raise NotFound(
                    f"conv slug {scope!r} not found",
                    next="search(kind='conv', q='...')",
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

    # ── put: link/tag CRUD only (no body mutation) ─────────────────

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
        """Apply link/tag operations to an existing conversation ref.

        Conversations are *capture-on-write*: their turns arrive via
        the chat-bridge that captured the thread, not from agent
        ``put``. Editing transcript content from an agent would
        break the audit trail. Cross-linking the conversation to
        the todo it produced, or to the paper that prompted it,
        is a separate concern and lands here.

        Per-kind axis enforcement: conversations carry no closed-
        prefix tags (the workflow state lives on associated todos
        / quests). Only open tags are accepted; ``STATUS:`` /
        ``PRIO:`` raise ``BadInput`` at the agent boundary.

        ``id`` must be a bare slug — chunk selectors (``slug~12``)
        and path views (``slug/transcript``) are read-only-side
        addressing. Reject them so a misuse doesn't silently
        target the wrong row.
        """
        if text is not None:
            raise BadInput(
                "conv transcripts are capture-on-write — not editable from put",
                next=(
                    "transcripts arrive via the chat bridge; for "
                    "cross-links use put(kind='conv', id=<slug>, "
                    "link='paper:foo')"
                ),
            )
        if mode is not None:
            raise BadInput(
                f"mode={mode!r} not supported for kind='conv'",
                next=(
                    "conv put accepts only link/unlink/tags/untags — "
                    "no body modes. Drop the mode= kwarg."
                ),
            )
        if id is None:
            raise BadInput(
                "conv put requires id= (the conv slug)",
                next=(
                    "put(kind='conv', id='<slug>', link='paper:foo') "
                    "— find the slug via search(kind='conv', q='...')"
                ),
            )

        # Reject chunk selectors and path views — link/tag ops are
        # ref-level. Reuse ``_parse_conv_id`` so the error wording
        # matches the read-side parser's contract.
        slug, chunk, path_view = _parse_conv_id(str(id))
        if chunk is not None or path_view is not None:
            raise BadInput(
                "conv put operates at ref level — drop the turn "
                "selector / path view from id=",
                next=f"put(kind='conv', id={slug!r}, link=...)",
            )

        ref = self.store.get_ref(kind="conv", id=slug)
        if ref is None:
            raise NotFound(
                f"conv slug {slug!r} not found",
                next="search(kind='conv', q='...') to find existing slugs",
            )

        ack = apply_link_tag_only_put(
            self.store,
            kind="conv",
            ref_id=ref.id,
            ref_label=slug,
            link=link,
            unlink=unlink,
            tags=tags,
            untags=untags,
            rel=rel,
        )
        return Response(body=ack)

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
