"""GripeHandler — the project's bug tracker.

Numeric-id ref kind, first-class as of migration 0005. File a
complaint, find existing ones, comment to add context, hand off
to a ``fix_gripe`` job for an agent to prepare a candidate branch,
retire via ``delete``.

Surface (see ``precis-gripe-help``):

- ``put(kind='gripe', text=...)`` creates a new gripe with a
  ``gripe_body`` chunk and ``STATUS:open`` tag.
- ``put(kind='gripe', id=N, text=...)`` appends a ``gripe_comment``
  chunk to the existing gripe (id-present routes to append; same
  verb, no separate ``comment=`` field).
- ``get(kind='gripe', id=N)`` composes the body + ordered comment
  timeline alongside the standard ref header / tags / links view.
- ``search`` / ``tag`` / ``link`` / ``delete`` behave like every
  other first-class numeric ref kind via :class:`NumericRefHandler`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import BlockInsert, Ref


# Chunk-kind slugs we own. Match the seed in 0005.
_BODY_KIND = "gripe_body"
_COMMENT_KIND = "gripe_comment"


class GripeHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="gripe",
        title="Gripe",
        description=(
            "The project's bug tracker. Numeric id assigned on "
            "create. Body + append-only comment timeline live as "
            "chunks. Status tracked via "
            "STATUS:open|triaged|ready_for_fix|in_review|wontfix."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "gripe"
    sense: ClassVar[str] = "gripe"
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:open",)

    # ── put: create or append-comment ───────────────────────────────

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
        # ``put(id=N, text='...')`` appends a gripe_comment chunk —
        # the comment-append idiom for this kind. The base
        # NumericRefHandler.put rejects id-presence unconditionally
        # so we intercept before delegating.
        if id is not None:
            if text is None or not text.strip():
                raise BadInput(
                    f"appending a comment to {self._sense()} id={id!r} "
                    "requires text=",
                    next=(
                        f"put(kind={self.kind!r}, id={id}, "
                        "text='your comment')"
                    ),
                )
            # Tags / links / mode are not accepted on the append
            # path — they belong on tag() / link() against the
            # existing ref.
            if tags is not None or untags is not None:
                raise BadInput(
                    "tags=/untags= are not accepted when appending a "
                    f"{self._sense()} comment",
                    next=(
                        f"use tag(kind={self.kind!r}, id={id}, "
                        "add=[...]/remove=[...])"
                    ),
                )
            if link is not None or unlink is not None or rel is not None:
                raise BadInput(
                    "link=/unlink=/rel= are not accepted when appending "
                    f"a {self._sense()} comment",
                    next=(
                        f"use link(kind={self.kind!r}, id={id}, "
                        "target=..., mode='add'|'remove')"
                    ),
                )
            if mode is not None:
                raise BadInput(
                    f"mode= is not accepted on {self._sense()} put",
                    next=f"delete(kind={self.kind!r}, id={id})",
                )
            return self._append_comment(id=id, text=text)
        return super().put(
            id=id,
            text=text,
            mode=mode,
            tags=tags,
            untags=untags,
            link=link,
            unlink=unlink,
            rel=rel,
        )

    # ── create: ref + body chunk + default tags + (optional) link ──

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
    ) -> Response:
        # Mirror NumericRefHandler._create but add the body-chunk
        # write in the same transaction so the gripe + its body
        # land atomically. The body chunk picks up embeddings +
        # keywords from the standard workers automatically, which
        # is what makes the comment timeline searchable.
        from precis.handlers._link_tag_ops import validate_relation
        from precis.handlers._link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        target = (
            parse_link_target(link, store=self.store) if link is not None else None
        )
        relation = validate_relation(rel)

        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text,
                meta={},
                conn=conn,
            )
            self.store.insert_blocks(
                ref.id,
                [BlockInsert(pos=0, text=text, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation,
                    conn=conn,
                )
        return self._render_create_ack(ref.id)

    def _append_comment(self, *, id: str | int, text: str) -> Response:
        ref_id = self._coerce_id(id)
        ref = self._resolve_live_ref(ref_id)
        # Next pos = current chunk count (body is pos=0, comments
        # follow). list_blocks_for_ref excludes synthetic cards
        # (ord<0) so the count is exactly the body + comment count.
        existing = self.store.list_blocks_for_ref(ref.id)
        next_pos = len(existing)
        with self.store.tx() as conn:
            self.store.insert_blocks(
                ref.id,
                [
                    BlockInsert(
                        pos=next_pos,
                        text=text,
                        meta={"chunk_kind": _COMMENT_KIND},
                    )
                ],
                conn=conn,
            )
        return Response(
            body=(
                f"appended comment to {self._sense()} id={ref.id} "
                f"(now {next_pos + 1} chunk{'s' if next_pos else ''} total)"
            )
        )

    # ── rendering: body + comment timeline ──────────────────────────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:  # type: ignore[override]
        blocks = self.store.list_blocks_for_ref(ref.id)
        lines = [f"# {self._sense()} {ref.id}"]
        if ref.set_by:
            lines.append(f"filed by: {ref.set_by}")
        if tags:
            lines.append("tags: " + " ".join(str(t) for t in tags))
        lines.append("")
        # Walk chunks in pos order. Body is pos=0, comments follow.
        body_rendered = False
        for block in blocks:
            kind = block.chunk_kind
            if kind == _BODY_KIND and not body_rendered:
                lines.append(block.text)
                body_rendered = True
                continue
            if kind == _COMMENT_KIND:
                lines.append("")
                lines.append(f"## comment {block.pos}")
                lines.append(block.text)
        if not body_rendered:
            # Pre-migration gripes had no body chunk; fall back to
            # the ref title so old rows still render coherently.
            lines.insert(-1 if lines[-1] == "" else len(lines), ref.title)
        return "\n".join(lines)

    def _render_create_ack(self, ref_id: int) -> Response:
        return Response(
            body=(
                f"created {self._sense()} id={ref_id} (STATUS:open). "
                f"add context: put(kind={self.kind!r}, id={ref_id}, "
                "text='more details'). "
                f"hand off to an agent: put(kind='job', "
                f"job_type='fix_gripe', link='gripe:{ref_id}', "
                "rel='fixes')."
            )
        )
