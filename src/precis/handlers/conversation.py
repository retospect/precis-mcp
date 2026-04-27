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
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section


class ConversationHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="conv",
        title="Conversation",
        description=(
            "Durable conversation transcript — slug-addressed, one "
            "block per message turn. Read-only in phase 5."
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
        lines = [f"# {len(hits)} turn match(es) for {q!r}"]
        for block, ref, score in hits:
            slug = ref.slug or "?"
            preview = (block.text[:160] + "…") if len(block.text) > 160 else block.text
            lines.append(f"\n## {slug}~{block.pos}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # ── render helpers ──────────────────────────────────────────────

    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="conv", limit=20)
        if not refs:
            return Response(body="no conversations recorded yet")
        lines = [f"# {len(refs)} conversation(s)"]
        for r in refs:
            preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
            lines.append(f"  {(r.slug or '?'):<30}  {preview}")
        return Response(body="\n".join(lines))

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
