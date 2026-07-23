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
- ``search`` queries chunks (body + comments) and groups hits by
  gripe, so a search term that only appears in a comment surfaces
  the parent gripe — overriding ``NumericRefHandler.search`` which
  only indexes ``ref.title``. (Fix for the gap surfaced during the
  0005 e2e: the plan said comments would be searchable for free
  via the chunk substrate, but the verb itself never reached the
  chunk table.)
- ``tag`` / ``link`` / ``delete`` behave like every other
  first-class numeric ref kind via :class:`NumericRefHandler`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from psycopg.errors import ForeignKeyViolation

from precis.errors import BadInput, Upstream
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

    # Body + append-only comment timeline live in chunks, so search the
    # chunks grouped by ref (a term that only appears in a comment still
    # surfaces the parent gripe). Salience stays cold — bug reports must
    # not enter the dream frontier. Shared machinery in NumericRefHandler.
    search_body_chunks: ClassVar[bool] = True

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        # /recent (base) + /open and /wontfix (status-shorthand). The
        # full STATUS axis vocabulary lives in tags; these are the two
        # the agent reaches for daily ("what's still open?", "what did
        # I decide not to fix?"). Other STATUS values come via the
        # explicit tag filter: search(kind='gripe', tags=['STATUS:in_review']).
        return ("recent", "open", "wontfix")

    def _list_view(self, view: str) -> Response | None:
        if view == "open":
            return self._list_by_tags(["STATUS:open"], page_size=20)
        if view == "wontfix":
            return self._list_by_tags(["STATUS:wontfix"], page_size=20)
        return super()._list_view(view)

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
                    f"appending a comment to {self._sense()} id={id!r} requires text=",
                    next=(f"put(kind={self.kind!r}, id={id}, text='your comment')"),
                )
            # Tags / links / mode are not accepted on the append
            # path — they belong on tag() / link() against the
            # existing ref.
            if tags is not None or untags is not None:
                raise BadInput(
                    "tags=/untags= are not accepted when appending a "
                    f"{self._sense()} comment",
                    next=(
                        f"use tag(kind={self.kind!r}, id={id}, add=[...]/remove=[...])"
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
        # ``auto_refresh_days`` is propagated by NumericRefHandler.put
        # for cache-backed kinds (Model A relevance decay, migration
        # 0011). Gripe has no refresh policy, but accepting the kwarg
        # here keeps the override in lock-step with the base class so
        # any ``put(kind='gripe', ...)`` call doesn't raise TypeError.
        # (Broad-pass R3#14.)
        auto_refresh_days: int | None = None,
        **_kw: Any,
    ) -> Response:
        # The base insert (ref + gripe_body chunk + the STATUS:open
        # default tag) routes through ``file_gripe_readonly()``
        # (migration 0079) rather than hand-rolled insert_ref/
        # insert_blocks/add_tag calls. That SQL function is SECURITY
        # DEFINER — it runs with its owner's privileges regardless of
        # the calling role — so filing a gripe keeps working even from
        # an ``agent_ro`` connection (``write:none`` envelope,
        # ``envelope.py::db_role``), which Postgres would otherwise
        # refuse outright. Using it unconditionally (not just as an
        # agent_ro fallback) keeps one code path for both roles and
        # means the function is exercised on every gripe, not just the
        # rare read-only one. Any caller-supplied ``tags=``/``link=``
        # beyond the default are still applied in the same transaction,
        # same as before.
        from precis.handlers._link_tag_ops import validate_relation
        from precis.handlers._link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)
        parsed_extra_tags = [Tag.parse_strict(t, kind=self.kind) for t in (tags or [])]

        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)", (text,)
            ).fetchone()
            assert row is not None
            ref_id = int(row[0])
            for tag in parsed_extra_tags:
                self.store.add_tag(
                    ref_id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation,
                    conn=conn,
                )
        return self._render_create_ack(ref_id)

    def _append_comment(self, *, id: str | int, text: str) -> Response:
        ref_id = self._coerce_id(id)
        ref = self._resolve_live_ref(ref_id)
        # Next pos = current chunk count (body is pos=0, comments
        # follow). list_blocks_for_ref excludes synthetic cards
        # (ord<0) so the count is exactly the body + comment count.
        existing = self.store.list_blocks_for_ref(ref.id)
        next_pos = len(existing)
        try:
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
        except ForeignKeyViolation as e:
            # Most likely culprit: ``chunks.chunk_kind`` FK against
            # ``chunk_kinds.slug`` — i.e. the ``gripe_comment`` row
            # seeded by migration 0005 is missing on this DB. Surface
            # as Upstream (server-side schema drift, not user error)
            # with a copy-pasteable diagnostic so the operator knows
            # what to check. Broad-pass finding #3.
            raise Upstream(
                f"could not append {_COMMENT_KIND} chunk to "
                f"{self._sense()} id={ref.id} — likely missing "
                f"chunk_kind seed",
                next=[
                    f"verify: SELECT slug FROM chunk_kinds WHERE "
                    f"slug='{_COMMENT_KIND}'",
                    "re-apply migration "
                    "0005_gripe_first_class_and_jobs.sql against "
                    "this DB if the row is absent",
                ],
            ) from e
        return Response(
            body=(
                f"appended comment to {self._sense()} id={ref.id} "
                f"(now {next_pos + 1} chunk{'s' if next_pos else ''} total)"
            )
        )

    # ── search: chunk (body + comment) search grouped by ref lives in
    # NumericRefHandler via the search_body_chunks opt-in above. We only
    # override the per-hit renderer to surface comment context. ───────

    def _render_body_search_hit(self, ref: Ref, block: Any, rank: float) -> str:
        """One result line: header + chunk-kind context + matched text."""
        kind = block.chunk_kind
        if kind == _BODY_KIND:
            label = f"{self._sense()} {ref.id}"
        elif kind == _COMMENT_KIND:
            label = f"{self._sense()} {ref.id} (comment {block.pos})"
        else:
            label = f"{self._sense()} {ref.id} ({kind})"
        return f"\n## {label}  (rank={rank:.2f})\n{self._snippet(block.text)}"

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
        # Unified shape (broad-pass finding #9): kwarg spelling +
        # uppercase STATUS axis + TOON Next: trailer. Previous inline-
        # prose breadcrumb was harder to paste and bunched two
        # very different actions onto one line.
        from precis.utils.next_block import render_next_section

        body = f"created {self.kind} id={ref_id} (STATUS:open)."
        body += render_next_section(
            [
                (
                    f"put(kind={self.kind!r}, id={ref_id}, text='more details')",
                    "append a comment",
                ),
                (
                    f"put(kind='job', job_type='fix_gripe', "
                    f"link={self.kind!r}+':{ref_id}', rel='fixes')",
                    "hand off to an agent",
                ),
                (
                    f"link(kind={self.kind!r}, id={ref_id}, "
                    f"target={self.kind!r}+':N', rel='supersedes')",
                    "mark this as superseding/refining another",
                ),
                (
                    f"get(kind={self.kind!r}, id='/recent')",
                    f"recent {self._sense()} entries",
                ),
            ]
        )
        return Response(body=body)
