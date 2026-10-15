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
  verb, no separate ``comment=`` field). ``link=``/``rel=`` and
  ``tags=``/``untags=`` ride along in the same transaction, so
  comment-then-close is one call.
- ``get(kind='gripe', id=N)`` composes a body head (``_BODY_HEAD_CHARS``,
  with a ``+N chars`` marker when cut) + the newest comment head
  (older ones as one-liners; per-kind policy in
  :mod:`_comment_timeline`) alongside header / tags.
  ``view='comments'`` returns the full body + the full timeline;
  ``log`` stays audit events. ``delete(id=N, args={'comment': K})`` hides one.
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

from precis.errors import BadInput, Unsupported
from precis.handlers import _comment_timeline as tl
from precis.handlers._mode_help import require_mode
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.handlers._prio_tag import PRIO_TAG_TO_INT, split_prio, validate_prio
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import Ref
from precis.utils.secret_scan import mask_secrets

# Chunk-kind slugs we own. Match the seed in 0005.
_BODY_KIND = "gripe_body"
_COMMENT_KIND = "gripe_comment"

#: Bare-get body head budget (render diet 2026-10-14: the default read
#: measured p95 6.7 KB, half of it the body). Over-budget bodies cut at
#: a line boundary and get a ``+N chars`` marker pointing at
#: ``view='comments'``, the complete read.
_BODY_HEAD_CHARS = 1500


class GripeHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="gripe",
        title="Gripe",
        description=(
            "The project's bug tracker. Numeric id assigned on "
            "create. Body + append-only comment timeline live as "
            "chunks. Status tracked via "
            "STATUS:open|triaged|ready_for_fix|in_review|done|wontfix."
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
        # id-presence alone dispatches create-vs-comment; mode= is never
        # required. gr338441/gr292913: an explicit mode= is accepted only
        # as a redundant-but-unambiguous confirmation of what id= already
        # selected — 'create' with no id=, 'comment' with id=<n> — and
        # rejected otherwise (see put()'s mode validation).
        modes=("create", "comment"),
    )

    #: args= keys each verb reads out of ``args`` (gr475332).
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "delete": tl.DELETE_ARGS_KEYS,
    }
    #: ``put(id=N, text=...)`` appends a comment (the unknown-args= refusal
    #: points at it, gr478557).
    supports_comment: ClassVar[bool] = True

    kind: ClassVar[str] = "gripe"
    sense: ClassVar[str] = "gripe"
    #: get/tag/link take ``id=[...]`` (summary get, one-transaction tag/fan-in link).
    batch_ids: ClassVar[bool] = True
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:open",)

    # The everyday gripe search is "what's still open?" — so default the
    # STATUS filter to open. Overridable: pass an explicit status= (e.g.
    # status='wontfix'), pin a STATUS: tag, or status='*' for every
    # status regardless. Mirrors finding's status-axis default (there:
    # 'established'). The default surfaces a one-line hint in the header
    # so it's never a silent filter.
    default_search_status: ClassVar[str] = "open"

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

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        prio: int | None = None,
        **_kw: Any,
    ) -> Response:
        # A gripe's title is the first line of its body, so ``title=`` is
        # sugar for prepending it (skipped when text already leads with it).
        if title is not None and id is None and title.strip():
            title = title.strip()
            body = (text or "").strip()
            if not body.startswith(title):
                text = f"{title}\n\n{body}" if body else title
        # automated writers cannot rephrase: mask here; agent verb calls are
        # refused at the dispatch boundary (runtime/dispatch.py)
        text = mask_secrets(text)
        # id-presence is what actually dispatches create-vs-comment; an
        # explicit mode= is accepted only as a redundant-but-unambiguous
        # confirmation of that ('create' <-> no id=, 'comment' <-> id=<n>)
        # and rejected as a mismatch otherwise (gr338441). Validated once,
        # up front, so both branches below can treat mode= as already
        # settled and never forward it past this point.
        if mode is not None:
            self._validate_put_mode(id=id, mode=mode)
        # ``put(id=N, text='...')`` appends a gripe_comment chunk —
        # the comment-append idiom for this kind. The base
        # NumericRefHandler.put rejects id-presence unconditionally
        # so we intercept before delegating.
        if id is not None:
            if prio is not None:
                # Reject rather than silently drop — priority mutation on
                # an existing gripe goes through tag().
                raise BadInput(
                    f"prio= is not accepted when appending a {self._sense()} comment",
                    next=f"use tag(kind={self.kind!r}, id={id}, prio={prio})",
                )
            if text is None or not text.strip():
                raise BadInput(
                    f"appending a comment to {self._sense()} id={id!r} requires text=",
                    next=(f"put(kind={self.kind!r}, id={id}, text='your comment')"),
                )
            # link=/unlink=/rel= and tags=/untags= ride along: comment +
            # edge/tag edit in one transaction (comment-then-close is one
            # put, not reject + re-put + tag(); gr484232). The tag edit is
            # this kind's own tag() — the same STATUS guards apply.
            return self._append_then_link(
                lambda: self._append_comment(id=id, text=text),
                id=id,
                link=link,
                unlink=unlink,
                rel=rel,
                tags=tags,
                untags=untags,
                what=f"{self._sense()} comment",
            )
        # Create path: plumb a create-time ``prio=`` into ``_create`` via a
        # per-call slot (the todo pending-slot pattern — the base put's
        # signature stays untouched). The ``PRIO:`` tag alias keeps working;
        # the explicit kwarg wins.
        self._pending_prio = validate_prio(prio)
        try:
            return super().put(
                id=id,
                text=text,
                # Already validated above (mode='create' or None here — a
                # mismatch raised before we got this far); the base put()
                # rejects any non-None mode= outright, so don't forward it.
                mode=None,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
            )
        finally:
            self._pending_prio = None

    def _validate_put_mode(self, *, id: str | int | None, mode: str) -> None:
        """Check an explicit ``put`` ``mode=`` against reality.

        ``mode=`` is never required — id-presence alone dispatches
        create-vs-comment. When a caller passes it anyway, it must agree
        with what id= already selected: ``'create'`` pairs with no id=,
        ``'comment'`` pairs with id=<n>. Anything else (an unknown value,
        or a value that names the wrong branch) is ``BadInput`` naming
        both accepted forms and the real dispatch rule.
        """
        require_mode(spec=self.spec, verb="put", mode=mode)
        expected = "create" if id is None else "comment"
        if mode != expected:
            raise BadInput(
                f"put(kind={self.kind!r}, mode={mode!r}) does not match "
                f"id={id!r} — id-presence selects the branch, not mode=: "
                "mode='create' pairs with no id= (files a new gripe), "
                "mode='comment' pairs with id=<n> (appends a comment to it)",
                next=(
                    f"put(kind={self.kind!r}, text='...')"
                    if id is None
                    else f"put(kind={self.kind!r}, id={id}, text='...')"
                ),
            )

    #: Per-call slot plumbing a create-time ``prio=`` from ``put`` into
    #: ``_create`` (which the base class calls with a fixed arg set).
    _pending_prio: int | None = None

    # ── tag: sync a PRIO: alias into the canonical prio column ───────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        prio: int | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, list):
            return self._tag_batch(id, add=add, remove=remove, prio=prio, **_kw)
        # ``PRIO:`` → the canonical prio column (which the backlog groomer
        # inherits onto the minted fix_gripe todo, so a human tagging a gripe
        # PRIO:high actually hastens its fix), stripped from the tag set like
        # quest/todo. A bare ``PRIO:*`` in remove clears the column back to
        # the sort-time default. An explicit ``prio=`` kwarg (declared at the
        # verb level alongside todo's) wins over the tag alias. Mirrors
        # QuestHandler.tag.
        prio = validate_prio(prio)
        # Every gripe carries exactly one STATUS (migration 0176's commit-time
        # trigger), so a bare STATUS remove would fail at commit as a raw
        # check_violation. Refuse it here with the replacement idiom instead.
        if any(t.startswith("STATUS:") for t in remove or ()) and not any(
            t.startswith("STATUS:") for t in add or ()
        ):
            raise BadInput(
                f"a {self._sense()} always has one STATUS; it cannot be removed",
                next=(
                    f"replace it: tag(kind={self.kind!r}, id={id}, "
                    "add=['STATUS:wontfix']) — a STATUS add displaces the old value"
                ),
            )
        add, prio_from_tag = split_prio(add)
        clear_prio = False
        if remove:
            kept = [t for t in remove if t not in PRIO_TAG_TO_INT]
            clear_prio = len(kept) != len(remove)
            remove = kept or None
        if prio is None:
            prio = prio_from_tag
        if prio is not None or clear_prio:
            ref_id = self._coerce_id(id)
            self._resolve_live_ref(ref_id)
            self.store.set_prio(ref_id, prio)
            if not add and not remove:
                # Only a prio write happened — the base tag() would reject an
                # otherwise-empty call.
                shown = f"prio={prio}" if prio is not None else "prio=cleared"
                return Response(body=f"set {shown} on {self._sense()} id={ref_id}")
        return super().tag(id=id, add=add, remove=remove, **_kw)

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
        # insert_chunks/add_tag calls. That SQL function is SECURITY
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
        from precis.utils.link_tag_ops import (
            guard_taxon_hierarchy,
            validate_relation,
        )
        from precis.utils.link_target import (
            mint_lazy_link_target,
            parse_link_target,
        )

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        if link is not None:
            mint_lazy_link_target(link, store=self.store)
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)
        if target is not None:
            # Taxon hierarchy rules (docs/backlog/term-taxonomy.md): this
            # create path writes its own link, so it calls the shared guard.
            guard_taxon_hierarchy(
                self.store, None, target, relation, src_kind=self.kind
            )
        # A create-time ``PRIO:`` alias syncs to the canonical prio column
        # (which the backlog groomer inherits onto the fix todo), not a
        # decorative tag row. Strip it before strict tag parsing. An
        # explicit ``put(prio=)`` (plumbed via the pending slot) wins.
        tags, prio_from_tag = split_prio(tags)
        if self._pending_prio is not None:
            prio_from_tag = self._pending_prio
        parsed_extra_tags = [Tag.parse_strict(t, kind=self.kind) for t in (tags or [])]

        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)", (text,)
            ).fetchone()
            assert row is not None
            ref_id = int(row[0])
            if prio_from_tag is not None:
                self.store.set_prio(ref_id, prio_from_tag, conn=conn)
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
        ref = self._resolve_live_ref(self._coerce_id(id))
        return tl.append_comment(
            self.store, ref, kind=self.kind, comment_kind=_COMMENT_KIND, text=text
        )

    # ── delete: soft-delete the gripe, or hide one comment ──────────

    def delete(
        self,
        *,
        id: str | int | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput(f"delete(kind={self.kind!r}) requires id=")
        if args and "comment" in args:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return tl.delete_comment(
                self.store, ref, kind=self.kind, comment_kind=_COMMENT_KIND, args=args
            )
        if args:
            raise BadInput(
                f"unknown delete args {sorted(args)!r}",
                next=f"delete(kind={self.kind!r}, id={id}, args={{'comment': <K>}})",
            )
        return super().delete(id=id, **_kw)

    # ── search: chunk (body + comment) search grouped by ref lives in
    # NumericRefHandler via the search_body_chunks opt-in above. We only
    # override the per-hit renderer to surface comment context. ───────

    def _render_body_search_hit(self, ref: Ref, block: Any, rank: float) -> str:
        """One result line: header + chunk-kind context + matched text."""
        kind = block.chunk_kind
        if kind == _BODY_KIND:
            label = f"{self._sense()} {ref.id}"
        elif kind == _COMMENT_KIND:
            label = f"{self._sense()} {ref.id} (comment {block.ord})"
        else:
            label = f"{self._sense()} {ref.id} ({kind})"
        return f"\n## {label}  (rank={rank:.2f})\n{self._snippet(block.text)}"

    # ── rendering: body + comment timeline ──────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        if isinstance(id, list):
            # Batch form: the base class renders it (summary-shaped) or
            # refuses the view= combination.
            return super().get(id=id, view=view, q=q, **_kw)
        if view == "comments":
            ref = self._resolve_live_ref(self._coerce_id(id))
            return Response(
                body=self._render_timeline(ref, self.store.tags_for(ref.id), full=True)
            )
        if view is not None and view not in _BASE_VIEWS:
            views = [*_BASE_VIEWS, "comments"]
            raise Unsupported(
                f"unknown view {view!r} for kind='gripe'",
                options=views,
                next="get(kind='gripe', id=<id>, view='comments') for the full timeline",
            )
        return super().get(id=id, view=view, q=q, **_kw)

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        return self._render_timeline(ref, tags, full=False)

    def _render_timeline(self, ref: Ref, tags: list[Tag], *, full: bool) -> str:
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        lines = [f"# {self._sense()} {ref.id}"]
        if ref.set_by:
            lines.append(f"filed by: {ref.set_by}")
        if tags:
            lines.append("tags: " + " ".join(str(t) for t in tags))
        body = next((b.text for b in blocks if b.chunk_kind == _BODY_KIND), ref.title)
        lines.extend(["", body if full else self._body_head(body, ref.id)])
        comments = [b for b in blocks if b.chunk_kind == _COMMENT_KIND]
        section = tl.render_comments(ref, comments, kind=self.kind, full=full)
        return "\n".join(lines) + (f"\n\n{section}" if section else "")

    @staticmethod
    def _body_head(body: str, ref_id: int) -> str:
        """Bare-get body head: line-boundary cut inside the budget, then
        a marker naming the view that carries the rest. The pointer is
        inline (not in a footer) because a comment-free gripe renders no
        comments footer to point at it."""
        if len(body) <= _BODY_HEAD_CHARS:
            return body
        cut = body.rfind("\n", 0, _BODY_HEAD_CHARS)
        if cut < _BODY_HEAD_CHARS // 2:
            cut = _BODY_HEAD_CHARS
        return (
            body[:cut].rstrip()
            + f"\n[+{len(body) - cut} chars — "
            + f"get(kind='gripe', id={ref_id}, view='comments')]"
        )

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
                    f"link='{self.kind}:{ref_id}', rel='fixes')",
                    "hand off to an agent",
                ),
                (
                    f"link(kind={self.kind!r}, id={ref_id}, "
                    "target='<gripe-handle>', rel='supersedes')",
                    "mark this as superseding/refining another (the other gripe's id)",
                ),
            ]
        )
        return Response(body=body)
