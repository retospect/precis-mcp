"""Shared base class for numeric-id ref kinds.

Memory was the first instance; phase 5 brings four more (todo, gripe,
flashcard, conv) plus a couple of slug variants. The CRUD shape is
nearly identical for all of them — only the kind name, default tags,
landing/list views, and a small render hook differ.

Subclass contract:

    Required class attributes
        spec: ClassVar[KindSpec]   — kind metadata (name, description …)
        kind: ClassVar[str]         — duplicates spec.kind for terseness

    Optional overrides
        corpus_slug: ClassVar[str]                     — default 'default'
        default_tags_on_create: ClassVar[tuple[str,…]] — applied to every put-create
        sense:                                         — singular noun for messages

    Optional method overrides
        _render_one(ref, tags)             — body of a single-ref read
        _render_search_hit(ref, rank, …)   — line in search output
        _list_view(view)                   — handle path views like '/recent'

The base provides ``get`` / ``search`` / ``put`` exactly as v1's
MemoryHandler did. Subclasses that need fancier behaviour (e.g. `flashcard`'s
spaced-repetition scheduling) override the relevant hook.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, Gone, NotFound, PrecisError, Unsupported
from precis.handlers._link_tag_ops import (
    require_link_target,
    require_tag_ops,
    validate_link_mode,
    validate_relation,
)
from precis.handlers._link_target import parse_link_target
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Link, Ref, Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits

log = logging.getLogger(__name__)

# Filler first-line shapes the first-line-discipline nudge flags — a
# leading "Notes on…" / "Re:" / "Memory about…" means the writer led
# with the topic instead of the conclusion. See the precis-firstline-help
# skill. Anchored at start-of-string; matched against the first body line.
_FILLER_FIRST_LINE = re.compile(
    r"^\s*(notes?\s+on\b|memory\s+about\b|re:|thoughts?\s+on\b"
    r"|about\s+the\b|summary\s+of\b)",
    re.IGNORECASE,
)

# Views every numeric-ref kind picks up for free. Subclasses with
# additional views should override `get()` to layer their dispatch
# on top, or — when we land per-kind view registries — extend
# this tuple via a class-level hook.
_BASE_VIEWS: tuple[str, ...] = ("links", "log")

# Summary budget for the TOON list view's first-line cell. Memories
# written before the SOUL's first-line discipline landed are often a
# single ~400-word paragraph with no newlines; without a cap the
# whole body lands in the summary cell and the table becomes
# unreadable. Cap at the first sentence terminator within budget;
# fall back to a hard char cut + ellipsis.
_SUMMARY_MAX_CHARS = 180
_SENTENCE_TERMINATORS = (". ", "! ", "? ")


def _extract_summary(body: str) -> tuple[str, int]:
    """Return ``(summary, remaining_words)`` for the list-view TOON row.

    The summary is the natural author-written first line when one
    exists (i.e. there's a ``\\n`` within budget). Failing that, the
    first sentence within ``_SUMMARY_MAX_CHARS`` of the start. Failing
    that, a hard cut + ``…``. ``remaining_words`` counts the words in
    everything past the summary so the budget signal stays honest.
    """
    if not body:
        return "", 0
    first_line, sep, rest = body.partition("\n")
    first_line = first_line.rstrip()
    if sep and len(first_line) <= _SUMMARY_MAX_CHARS:
        return first_line, len(rest.split())
    head = body[:_SUMMARY_MAX_CHARS]
    cut = -1
    for term in _SENTENCE_TERMINATORS:
        idx = head.rfind(term)
        if idx > cut:
            cut = idx
    if cut > 0:
        summary = body[: cut + 1].rstrip()
        tail = body[cut + 1 :]
    elif len(body) > _SUMMARY_MAX_CHARS:
        summary = body[:_SUMMARY_MAX_CHARS].rstrip() + "…"
        tail = body[_SUMMARY_MAX_CHARS:]
    else:
        return body.rstrip(), 0
    return summary, len(tail.split())


class NumericRefHandler(Handler):
    """Base class for numeric-id ref kinds (memory, todo, gripe, flashcard, …)."""

    spec: ClassVar[KindSpec]
    kind: ClassVar[str]
    corpus_slug: ClassVar[str] = "default"

    #: Tags applied automatically on `put`-create. e.g. for todo:
    #: ``("STATUS:open",)`` so every new todo starts open.
    default_tags_on_create: ClassVar[tuple[str, ...]] = ()

    #: Singular noun used in user-facing messages ("memory id=…",
    #: "todo id=…", …). Defaults to the kind name.
    sense: ClassVar[str] = ""

    #: When True, put-create emits a synthetic ``card_combined`` chunk
    #: (``ord=-1``) holding the ref's text so the embed worker
    #: vectorizes it and semantic search finds neighbours. Scoped to
    #: ``memory`` for the dreaming capability (see
    #: docs/design/dreaming.md); widen to other note-like kinds later.
    emits_card: ClassVar[bool] = False

    #: When True, put-create (and the subclass's ``edit``) resolves every
    #: ``kind:ref`` handle in the body and materialises ``related-to``
    #: links to those refs — so the note becomes a graph node reachable
    #: from its targets, not just visually at read time. Scoped to
    #: ``memory`` for now. Links carry ``meta={'auto': 'mention'}`` so
    #: an edit can re-sync them without touching hand-curated links.
    autolink_mentions: ClassVar[bool] = False

    #: When True, create/edit acks reinforce first-line discipline: a
    #: filler-looking first line ("Notes on…", "Re:") gets a one-line
    #: nudge toward the precis-firstline-help skill. The convention lands
    #: at the point of writing, no pre-fetch required. Scoped to memory.
    firstline_discipline: ClassVar[bool] = False

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError(f"{self.kind}: store required")
        self.store = hub.store

    # Convenience: callers / tests sometimes use `handler.sense` to
    # build messages — keep it cheap.
    @classmethod
    def _sense(cls) -> str:
        return cls.sense or cls.kind

    # ── ref resolution with soft-delete distinction ────────────────

    def _resolve_live_ref(self, ref_id: int) -> Ref:
        """Fetch a ref by id, raising ``Gone`` if soft-deleted and
        ``NotFound`` if it never existed.

        MCP critic MINOR-C (round 1): before this split, both cases
        returned the same ``[error:NotFound]`` envelope, so the LLM
        couldn't tell whether it hit a typo (try a different id) or
        a tombstone (the row was deleted, no MCP undo). Distinct
        envelopes mean the recovery vocabulary is sharp.

        The ``Gone`` path uses ``include_deleted=True`` on
        ``get_ref`` so soft-deleted rows surface for detection;
        they're still excluded from every other read path.
        """
        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        if ref is not None:
            return ref
        # Second probe: does the row exist but is soft-deleted?
        tombstone = self.store.get_ref(kind=self.kind, id=ref_id, include_deleted=True)
        if tombstone is not None:
            raise Gone(
                f"{self._sense()} id={ref_id} was soft-deleted "
                "(row retained for audit; no MCP undo)",
                next=(
                    "post-mortem only - soft-deleted refs are "
                    "recoverable at the SQL layer by setting "
                    "deleted_at=NULL on the row"
                ),
            )
        raise NotFound(
            f"{self._sense()} id={ref_id} not found",
            next=f"search(kind={self.kind!r}, q='...') to find existing",
        )

    # ── get ─────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # `id='/recent'` and similar path views — subclasses may
        # implement custom list shapes (e.g. todo's open / done filters).
        if isinstance(id, str) and id.startswith("/"):
            list_resp = self._list_view(id[1:])
            if list_resp is not None:
                return list_resp
            # Enumerate the actual list views this handler supports
            # rather than pointing at a per-kind help skill that may
            # not exist (e.g. ``precis-flashcard-help``). The MCP
            # critic flagged the dangling reference as MINOR #5: a
            # caller who follows the "see precis-flashcard-help"
            # hint dead-ends because the skill file was never written.
            # Always spell the supported views inline so the agent has
            # a working recovery path. (Critic MINOR #5.)
            views = self._supported_list_views()
            view_list = ", ".join(f"/{v}" for v in views) if views else "(none)"
            raise Unsupported(
                f"unknown list view {id!r} for kind={self.kind!r}",
                options=list(views),
                next=f"list views available for {self.kind!r}: {view_list}",
            )
        if id is None and view is None:
            # Bare get → list recent, if subclass supports it.
            list_resp = self._list_view("recent")
            if list_resp is not None:
                return list_resp
            raise BadInput(
                f"{self._sense()} get requires id=",
                next=f"get(kind={self.kind!r}, id=<int>)",
            )

        ref_id = self._coerce_id(id)
        ref = self._resolve_live_ref(ref_id)
        if view is not None:
            if view not in _BASE_VIEWS:
                raise Unsupported(
                    f"unknown view {view!r} for kind={self.kind!r}",
                    options=list(_BASE_VIEWS),
                    next=(
                        f"views available for {self.kind!r}: {', '.join(_BASE_VIEWS)}"
                    ),
                )
            if view == "links":
                return self._render_links_view(ref)
            if view == "log":
                from precis.handlers._event_log_render import render_event_log

                # Per-kind source filter: chase-driven kinds (finding)
                # narrow to source='chase' so the log is the chase
                # decision trail rather than every event ever logged
                # against the ref. Subclasses override
                # ``_event_log_source()`` to customise.
                return render_event_log(
                    self.store, ref.id, source=self._event_log_source()
                )
        tags = self.store.tags_for(ref.id)
        body = self._render_one(ref, tags)
        # F8: surface the link graph for this ref so the agent's
        # recall step actually sees connections the write step made.
        body += self._render_links_section(ref)
        return Response(body=body)

    def _event_log_source(self) -> str | None:
        """Subsystem to filter ``view='log'`` to, or ``None`` for all.

        Default: no filter (show every event). Subclasses with a
        natural per-subsystem identity override (e.g. ``FindingHandler``
        returns ``'chase'``).
        """
        return None

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        # Validate at the agent boundary — symmetric with put(tags=...).
        # Pass kind= so per-kind axis enforcement catches
        # STATUS: filter queries against kinds that don't use STATUS.
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)

        # ``q=`` is optional when ``tags=`` is supplied — broad
        # usability pass 2026-05-30 (#7 / #13): an agent looking for
        # "everything I tagged ``foo``" had to pass an arbitrary
        # ``q='a'`` to make the filter fire, which then *ranked* the
        # hits by lexical match to ``'a'``. With ``tags=`` set we
        # degrade to a recency-ordered list, which is what the user
        # wanted in the first place.
        if q is None or not q.strip():
            if normalized_tags:
                return self._list_by_tags(normalized_tags, page_size=page_size)
            raise BadInput(
                "search requires q= or tags=",
                next=(
                    f"search(kind={self.kind!r}, q='your query') or "
                    f"search(kind={self.kind!r}, tags=['<tag>'])"
                ),
            )

        hits = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=page_size
        )
        if not hits:
            tag_suffix = f" tagged {normalized_tags}" if normalized_tags else ""
            body = f"no {self._sense()} entries match {q!r}{tag_suffix}"
            # Empty searches should still teach the agent what to try
            # next — broaden the query, drop the tag filter (if any),
            # or fall back to the recent-list view.  Without this, a
            # small-model caller retries the same query, gives up, or
            # guesses at the wrong kind.  (MCP critic MINOR — empty-
            # result responses on search lack recovery hints.)
            nav: list[tuple[str, str]] = []
            nav.append(
                (
                    f"search(kind={self.kind!r}, q='broader term')",
                    "loosen the query",
                )
            )
            if normalized_tags:
                nav.append(
                    (
                        f"search(kind={self.kind!r}, q={q!r})",
                        "drop the tag filter",
                    )
                )
            nav.append(
                (
                    f"get(kind={self.kind!r}, id='/recent')",
                    f"list recent {self._sense()} entries",
                )
            )
            body += render_next_section(nav)
            return Response(body=body)

        # Salience: heat the entries this page surfaced. Ref-level kinds
        # carry their salience on the card_combined chunk (ord=-1); kinds
        # without a card contribute nothing. No-op for dream-actor reads.
        self.store.bump_salience(self.store.card_chunk_ids([r.id for r, _ in hits]))

        # Total-hits header: a second COUNT(*) with the same WHERE
        # clause so the agent sees "10 of 1234 hits" when results are
        # capped by page_size. The MCP critic flagged the missing "of K"
        # readout as a pagination footgun (the agent couldn't tell
        # whether it had everything or just the first page).
        total = self.store.count_refs_lexical(q=q, kind=self.kind, tags=normalized_tags)
        header = format_search_headline(
            n_returned=len(hits),
            total=total,
            noun=f"{self._sense()} match",
            query=q,
        )
        table = self._render_hits_table([ref for ref, _ in hits])
        return Response(body=f"{header}\n{table}")

    def _list_by_tags(self, tags: list[str], *, page_size: int) -> Response:
        """Recency-ordered list of refs matching ``tags``, no ranking.

        Reached when ``search(kind=K, tags=[...])`` is called without
        ``q=`` — the right shape for "show me everything I tagged X".
        Always emits a ``Next:`` trailer pointing at the ranked search
        path for callers who realize they wanted ranking.
        """
        refs = self.store.list_refs(kind=self.kind, tags=tags, limit=page_size)
        if not refs:
            body = f"no {self._sense()} entries tagged {tags}"
            body += render_next_section(
                [
                    (
                        f"get(kind={self.kind!r}, id='/recent')",
                        f"recent {self._sense()} entries (no tag filter)",
                    ),
                    (
                        f"search(kind={self.kind!r}, q='topic', tags={tags!r})",
                        "rank within the tagged set",
                    ),
                ]
            )
            return Response(body=body)
        header = (
            f"# {len(refs)} {self._sense()} entr"
            f"{'y' if len(refs) == 1 else 'ies'} tagged {tags} "
            f"(by recency)"
        )
        table = self._render_hits_table(refs)
        return Response(body=f"{header}\n{table}")

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Ref-level lexical search returned as ``SearchHit``s.

        Numeric-ref kinds search the ref title only — bodies tend
        to be short enough that one row per ref is the right
        granularity for cross-kind merge.  Subclasses with
        block-level bodies (none today) should override.
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind=self.kind)
        pairs = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=normalized_tags, limit=page_size
        )
        # Salience bump (card chunks); no-op for cardless kinds / dreamer.
        self.store.bump_salience(self.store.card_chunk_ids([r.id for r, _ in pairs]))
        return ref_hits_to_search_hits(pairs, kind=self.kind)

    # ── put: create-only on numeric-ref kinds ──────────────────────

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
        auto_refresh_days: int | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a new numeric ref.

        Per the seven-verb surface (D6), ``put`` is creation-only on
        numeric-ref kinds. Mutating an existing ref splits across
        dedicated verbs:

        - text body: not exposed (numeric-ref bodies are immutable
          once created — capture the new wording as a fresh ref or
          use ``delete`` + ``put`` to replace).
        - tags:  ``tag(kind, id, add=[...], remove=[...])``
        - links: ``link(kind, id, target=..., mode='add'|'remove', rel=...)``
        - delete: ``delete(kind, id)`` (soft-delete)

        ``id=``, ``mode=``, ``untags=``, ``unlink=`` are all rejected
        with a pointer at the right verb so an agent stuck on the
        old shape gets a sharp recovery hint rather than a silent
        no-op. ``tags=`` / ``link=`` / ``rel=`` are accepted on
        creation as the D3 shortcut.
        """
        if id is not None:
            raise BadInput(
                f"put on existing {self._sense()} id={id!r} is not supported",
                next=(
                    f"to mutate id={id}: tag(kind={self.kind!r}, id=N, add=[...]/remove=[...]) / "
                    f"link(kind={self.kind!r}, id=N, target=..., mode='add'|'remove') / "
                    f"delete(kind={self.kind!r}, id=N)"
                ),
            )
        if mode is not None:
            raise BadInput(
                f"mode= is not accepted on put for kind={self.kind!r}",
                next=(
                    "put creates a new ref; for delete use "
                    f"delete(kind={self.kind!r}, id=N)"
                ),
            )
        if untags is not None:
            raise BadInput(
                "untags= is not accepted on put",
                next=f"use tag(kind={self.kind!r}, id=N, remove=[...])",
            )
        if unlink is not None:
            raise BadInput(
                "unlink= is not accepted on put",
                next=(
                    f"use link(kind={self.kind!r}, id=N, target='kind:slug', "
                    "mode='remove')"
                ),
            )
        if rel is not None and link is None:
            raise BadInput(
                "rel= requires link= on create",
                next=(
                    f"put(kind={self.kind!r}, text='...', "
                    "link='paper:slug', rel='cites')"
                ),
            )
        # ``put(tags=...)`` and ``put(link=...)`` are the D3 shortcut
        # for the standalone ``tag``/``link`` verbs; if the kind
        # doesn't expose those verbs at all (e.g. gripe — write-only
        # by design) the put-create shortcut must reject too, otherwise
        # the documented "no tags, no links" guarantee from the help
        # skill is silently violated. Broad usability pass 2026-05-30
        # (#4).
        if tags is not None and not self.spec.supports_tag:
            raise BadInput(
                f"tags= is not accepted on put for kind={self.kind!r}",
                next=(
                    f"kind={self.kind!r} does not support tagging; "
                    f"omit tags= or see "
                    f"get(kind='skill', id='precis-{self.kind}-help')"
                ),
            )
        if link is not None and not self.spec.supports_link:
            raise BadInput(
                f"link= is not accepted on put for kind={self.kind!r}",
                next=(
                    f"kind={self.kind!r} does not support linking; "
                    f"omit link= or see "
                    f"get(kind='skill', id='precis-{self.kind}-help')"
                ),
            )
        return self._create(
            text=text,
            tags=tags,
            link=link,
            rel=rel,
            auto_refresh_days=auto_refresh_days,
        )

    # ── seven-verb surface (delegates to the same private helpers) ─

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
        """Soft-delete a numeric ref by id.

        Mirrors the legacy ``put(id=N, mode='delete')`` shape one-
        for-one — same store call, same response wording. The
        seven-verb surface promotes deletion to a first-class verb
        so agents don't have to rummage through ``put`` modes to
        find it.
        """
        return self._delete(id)

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add and/or remove tags on an existing numeric ref.

        Both ``add`` and ``remove`` apply atomically inside one
        transaction. An empty call (no ``add`` / no ``remove``) is
        rejected — the caller almost certainly meant something
        specific and a silent no-op would mask the typo.
        """
        require_tag_ops(self.kind, add, remove)
        ref_id = self._coerce_id(id)
        existing = self._resolve_live_ref(ref_id)
        # Pre-validate every tag *before* touching the DB so a
        # rejected tag mid-call doesn't leave partial state. Mirrors
        # the contract on ``_create`` / ``_update``.
        parsed_add: list[Tag] = (
            [Tag.parse_strict(s, kind=self.kind) for s in add] if add else []
        )
        parsed_remove: list[Tag] = (
            [Tag.parse_strict(s, kind=self.kind) for s in remove] if remove else []
        )
        with self.store.tx() as conn:
            for t in parsed_add:
                self.store.add_tag(
                    ref_id,
                    t,
                    set_by="agent",
                    replace_prefix=(t.namespace == "closed"),
                    conn=conn,
                )
            for t in parsed_remove:
                self.store.remove_tag(ref_id, t, conn=conn)
        return Response(body=f"tagged {self._sense()} id={ref_id}")

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from an existing numeric ref.

        ``mode='add'`` (default) creates the edge; ``mode='remove'``
        deletes it. With ``rel=`` on remove, removes only that
        (target, relation) pair; without ``rel=``, removes every
        link to the target at that selector.
        """
        target = require_link_target(self.kind, target)
        validate_link_mode(mode)
        ref_id = self._coerce_id(id)
        existing = self._resolve_live_ref(ref_id)
        # Collect both target-resolution and rel-vocabulary errors so a
        # caller with both wrong gets one round trip's worth of feedback,
        # not two. (MCP broad-pass usability finding #10.)
        target_err: PrecisError | None = None
        rel_err: BadInput | None = None
        link_target = None
        relation = None
        try:
            link_target = parse_link_target(target, store=self.store)
        except (NotFound, BadInput) as exc:
            target_err = exc
        try:
            relation = validate_relation(rel)
        except BadInput as exc:
            rel_err = exc
        if target_err is not None and rel_err is not None:
            raise BadInput(
                "link validation failed: "
                f"(target) {target_err.cause}; "
                f"(rel) {rel_err.cause}",
                options=rel_err.options,
                next=[
                    n
                    for n in (
                        target_err.next if isinstance(target_err.next, str) else None,
                        rel_err.next if isinstance(rel_err.next, str) else None,
                    )
                    if n
                ]
                or None,
            )
        if target_err is not None:
            raise target_err
        if rel_err is not None:
            raise rel_err
        # mypy: link_target and relation are guaranteed non-None below.
        assert link_target is not None and relation is not None
        if mode == "add":
            self.store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=link_target.ref_id,
                dst_pos=link_target.pos,
                relation=relation,
            )
            return Response(body=f"linked {self._sense()} id={ref_id} → {target}")
        # mode == "remove"
        n_removed = self.store.remove_link(
            src_ref_id=ref_id,
            dst_ref_id=link_target.ref_id,
            dst_pos=link_target.pos,
            relation=relation if rel is not None else None,
        )
        return Response(
            body=(
                f"unlinked {self._sense()} id={ref_id} ↛ {target} "
                f"({n_removed} edge{'s' if n_removed != 1 else ''} removed)"
            )
        )

    # ── private CRUD ───────────────────────────────────────────────

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
    ) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        # Resolve the link target *before* the insert, so a bad
        # ``link='paper:doesnotexist'`` doesn't leave a half-created
        # ref in the corpus. The parser hits the DB but doesn't
        # mutate; the insert + link both happen below.
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)

        # Pre-validate every tag *before* we touch the DB. The MCP
        # critic flagged a state-drift bug: a put-create that failed
        # tag validation still committed the ref insert, leaving a
        # ghost row behind. Now any BadInput from ``Tag.parse_strict``
        # is raised before ``insert_ref`` runs, so a rejected create
        # writes nothing. (Critic MAJOR #1.)
        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]

        # All validation done — now do every DB write inside a single
        # transaction so the ref + tags + link land atomically. If
        # any of the tag inserts trips a constraint we haven't
        # captured at validation time, the surrounding ``tx()`` will
        # roll back the ref insert too.
        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text,
                meta={},
                auto_refresh_days=auto_refresh_days,
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
            if self.emits_card:
                # Emit the embeddable card in the same tx as the ref
                # insert so the embed worker can vectorize it lazily.
                self.store.upsert_card_combined(ref.id, text, conn=conn)
            if self.autolink_mentions:
                self._sync_mention_links(ref.id, text, conn=conn)
        return self._with_first_line_nudge(self._render_create_ack(ref.id), text)

    def _first_line_nudge(self, text: str | None) -> str | None:
        """A one-line nudge when the body's first line reads as filler.

        Gated on ``firstline_discipline``. Returns ``None`` when the
        flag is off, the text is empty, or the first line already leads
        with substance. See the ``precis-firstline-help`` skill.
        """
        if not self.firstline_discipline or not text or not text.strip():
            return None
        first_line = text.strip().splitlines()[0]
        if _FILLER_FIRST_LINE.match(first_line):
            return (
                "first line reads as a topic, not a conclusion - lead with "
                "what you'd say if asked. "
                "See get(kind='skill', id='precis-firstline-help')."
            )
        return None

    def _with_first_line_nudge(self, ack: Response, text: str | None) -> Response:
        """Append the first-line nudge to an ack Response, if one fires."""
        nudge = self._first_line_nudge(text)
        if nudge is None:
            return ack
        return Response(body=f"{ack.body}\n\nhint: {nudge}", cost=ack.cost)

    def _sync_mention_links(
        self,
        ref_id: int,
        text: str,
        *,
        conn: Any,
        replace: bool = False,
    ) -> int:
        """Materialise ``related-to`` links to every live ref ``text`` names.

        Resolves the same ``kind:ref`` handles the web References panel
        surfaces and writes a real link per live target, so the note is
        reachable from the *other* side of the graph. Best-effort: a
        resolution failure must never roll back the note write, so the
        whole pass is wrapped and only ever logs.

        ``replace=True`` (the edit path) first drops the previous
        auto-mention links (those carrying ``meta={'auto': 'mention'}``)
        so a handle removed from the body loses its link; hand-added
        ``related-to`` links are left untouched. Returns the number of
        links added.
        """
        from precis.utils import mentions

        try:
            if replace:
                for link in self.store.links_for(
                    ref_id, direction="out", relation="related-to"
                ):
                    if (link.meta or {}).get("auto") == "mention":
                        self.store.remove_link(
                            src_ref_id=ref_id,
                            dst_ref_id=link.dst_ref_id,
                            dst_pos=link.dst_pos,
                            relation="related-to",
                            conn=conn,
                        )
            added = 0
            for tgt in mentions.resolve_link_targets(
                self.store, text, exclude_ref_id=ref_id
            ):
                self.store.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=tgt.dst_ref_id,
                    dst_pos=tgt.dst_pos,
                    relation="related-to",
                    set_by="agent",
                    meta={"auto": "mention"},
                    conn=conn,
                )
                added += 1
            return added
        except Exception:
            log.warning(
                "%s: autolink mentions failed for ref %s",
                self.kind,
                ref_id,
                exc_info=True,
            )
            return 0

    def _delete(self, id: str | int | None) -> Response:
        if id is None:
            raise BadInput(
                "delete requires id=",
                next=f"delete(kind={self.kind!r}, id=N)",
            )
        ref_id = self._coerce_id(id)
        self.store.soft_delete_ref(ref_id)
        return Response(body=f"deleted {self._sense()} id={ref_id}")

    # ── coercion ────────────────────────────────────────────────────

    @classmethod
    def _coerce_id(cls, id: str | int | None) -> int:
        if id is None:
            raise BadInput(
                f"{cls._sense()} operations require id=",
                next=f"put(kind={cls.kind!r}, id=<int>, ...)",
            )
        if isinstance(id, int):
            return id
        # Accept the canonical link-target form (`<kind>:<int>`) too —
        # an LLM that copy-pastes a link-target string into id= should
        # not have to strip the kind prefix by hand. Mirrors paper's
        # transparent DOI resolution and youtube's URL-form acceptance.
        s = id.strip()
        prefix = f"{cls.kind}:"
        if s.startswith(prefix):
            s = s[len(prefix) :]
        try:
            return int(s)
        except (ValueError, TypeError):
            raise BadInput(
                f"{cls._sense()} id must be an integer, got {id!r}",
                next=f"{cls._sense()} ids are integers - see search(kind={cls.kind!r}, q='...')",
            ) from None

    # ── rendering hooks (subclasses may override) ─────────────────

    def _render_links_view(self, ref: Ref) -> Response:
        """Render `view='links'`: outbound + inbound link graph for a ref.

        Outbound rows ("→") are stored on this ref as ``src_ref_id``.
        Inbound rows ("←") have this ref as ``dst_ref_id`` — they
        live on someone else's record. We render them as a separate
        section so the agent doesn't confuse the directions.

        Each link line names the other endpoint in canonical
        ``kind:identifier[~pos]`` form so it can be round-tripped
        back into a future ``link(target=…)`` call without any further
        translation.
        """
        out_links = self.store.links_for(ref.id, direction="out")
        in_links = self.store.links_for(ref.id, direction="in")

        lines = [f"# {self._sense()} {ref.id} - links"]
        if not out_links and not in_links:
            lines.append("")
            lines.append("(no links)")
            lines.append("")
            # MCP critic MINOR-C (round 2, deep pass): the recovery
            # hint used to suggest ``put(link='kind:identifier', rel=…)``.
            # ``put(link=)`` survives on numeric-ref kinds as a
            # create-and-link-in-one shortcut (D3), but the canonical
            # add-link verb for an existing ref is ``link(...)``. Teach
            # that here so the LLM doesn't mix the two idioms when
            # later adding links to refs the caller already has.
            lines.append(
                f"add one with: link(kind={self.kind!r}, id={ref.id}, "
                "target='kind:identifier', rel='related-to')"
            )
            return Response(body="\n".join(lines))

        # Pre-fetch every distinct ref touched by either side so we
        # render kind:identifier rather than bare ref_ids. One round
        # trip per unique target keeps this O(N) on link count
        # rather than O(N) on (link × DB-query).
        endpoint_ids: set[int] = set()
        for link in out_links:
            endpoint_ids.add(link.dst_ref_id)
        for link in in_links:
            endpoint_ids.add(link.src_ref_id)
        endpoints = self._fetch_endpoints(endpoint_ids)

        if out_links:
            lines.append("")
            lines.append("## outbound")
            for link in out_links:
                lines.append(self._format_link_line(link, endpoints, "→"))
        if in_links:
            lines.append("")
            lines.append("## inbound")
            for link in in_links:
                lines.append(self._format_link_line(link, endpoints, "←"))
        return Response(body="\n".join(lines))

    def _fetch_endpoints(self, ref_ids: set[int]) -> dict[int, Ref]:
        """Bulk-fetch refs by id, returning ``{id: Ref}``.

        Thin delegate to :meth:`Store.fetch_refs_by_ids`. Kept as a
        method here (instead of inlining at the call site) because
        subclasses / tests may want to override it with a canned
        endpoint dict. Soft-deleted refs are retained so a link
        to a tombstoned ref still renders with a deletion marker.
        """
        return self.store.fetch_refs_by_ids(ref_ids)

    @staticmethod
    def _format_link_line(
        link: Link,
        endpoints: dict[int, Ref],
        arrow: str,
    ) -> str:
        """Format one link row as ``arrow kind:id[~pos]  (relation)``.

        The arrow encodes direction:
          - ``→``  this ref's outbound link
          - ``←``  inbound — someone else's ref points here

        For inbound, we display the source ref's identifier so the
        agent can navigate "upstream" easily.
        """
        if arrow == "→":
            other_id, other_pos = link.dst_ref_id, link.dst_pos
        else:
            other_id, other_pos = link.src_ref_id, link.src_pos

        ref = endpoints.get(other_id)
        if ref is None:
            target = f"<unknown ref {other_id}>"
        else:
            handle = ref.slug if ref.slug is not None else str(ref.id)
            target = f"{ref.kind}:{handle}"
            if ref.deleted_at is not None:
                target += " (deleted)"
        if other_pos is not None:
            target += f"~{other_pos}"
        return f"{arrow} {target}  ({link.relation})"

    # F8: rel-name → inbound-passive-form. Symmetric rels (no
    # passive form) map to themselves; unknown rels fall through to
    # the ``<-`` prefix rendering in :meth:`_render_links_section`.
    _INVERSE_REL: dict[str, str] = {
        "related-to": "related-to",
        "cites": "cited by",
        "refutes": "refuted by",
        "supersedes": "superseded by",
        "supports": "supported by",
        "contradicts": "contradicted by",
        "cited-by": "cites",
        "retracted-by": "retracts",
    }

    def _render_links_section(self, ref: Ref) -> str:
        """F8: render the Links: TOON sub-section for a single-ref get.

        Three columns: ``{related to	keywords	how to get}``.
        Column 1 holds ``<rel-marker> <target>`` — ``--`` for default
        ``related-to`` (no semantic relation specified), the literal
        rel name otherwise. Inbound rows use the passive form via
        ``_INVERSE_REL`` (``cites`` → ``cited by``); unknown inbound
        rels fall back to a ``<- <rel>`` prefix so direction stays
        visible.

        Returns an empty string when the ref has no links in either
        direction — the caller appends unconditionally, so the empty
        case must produce no output (not even a trailing newline).

        Teaser column = first ~60 chars of the target's title. The
        F8 design called for "keywords" but the project doesn't yet
        expose a ``Store.page_sizeeywords_for_ref`` helper; title is the
        portable fallback. Upgrade path: swap the call here when a
        keyword API lands.
        """
        out_links = self.store.links_for(ref.id, direction="out")
        in_links = self.store.links_for(ref.id, direction="in")
        if not out_links and not in_links:
            return ""

        endpoint_ids: set[int] = set()
        for link in out_links:
            endpoint_ids.add(link.dst_ref_id)
        for link in in_links:
            endpoint_ids.add(link.src_ref_id)
        endpoints = self._fetch_endpoints(endpoint_ids)

        rows: list[dict[str, str]] = []
        combined = [(lnk, "out") for lnk in out_links] + [
            (lnk, "in") for lnk in in_links
        ]
        combined.sort(key=lambda pair: pair[0].id)
        for link, direction in combined:
            if direction == "out":
                other_id, other_pos = link.dst_ref_id, link.dst_pos
                rel_marker = self._format_outbound_rel(link.relation)
            else:
                other_id, other_pos = link.src_ref_id, link.src_pos
                rel_marker = self._format_inbound_rel(link.relation)
            target = self._format_target_handle(other_id, other_pos, endpoints)
            teaser = self._teaser_for(endpoints.get(other_id))
            get_call = self._get_call_for(endpoints.get(other_id), other_id)
            rows.append(
                {
                    "related to": f"{rel_marker} {target}".strip(),
                    "keywords": teaser,
                    "how to get": get_call,
                }
            )

        from precis.format import render_agent_table

        return "\n\nLinks:\n" + render_agent_table(
            rows, schema=["related to", "keywords", "how to get"]
        )

    @classmethod
    def _format_outbound_rel(cls, relation: str) -> str:
        """``--`` for default ``related-to``; literal rel name otherwise."""
        if relation == "related-to":
            return "--"
        return relation

    @classmethod
    def _format_inbound_rel(cls, relation: str) -> str:
        """Inverse-form for known rels; ``<- <rel>`` fallback."""
        if relation == "related-to":
            return "--"
        inv = cls._INVERSE_REL.get(relation)
        if inv is not None:
            return inv
        return f"<- {relation}"

    @staticmethod
    def _format_target_handle(
        ref_id: int, pos: int | None, endpoints: dict[int, Ref]
    ) -> str:
        """Build ``kind:identifier[~pos]`` for the link row."""
        ref = endpoints.get(ref_id)
        if ref is None:
            handle = f"<unknown ref {ref_id}>"
        else:
            ident = ref.slug if ref.slug is not None else str(ref.id)
            handle = f"{ref.kind}:{ident}"
            if ref.deleted_at is not None:
                handle += " (deleted)"
        if pos is not None:
            handle += f"~{pos}"
        return handle

    @staticmethod
    def _teaser_for(ref: Ref | None) -> str:
        """First ~60 chars of the target's title — the keyword stand-in."""
        if ref is None or not ref.title:
            return ""
        title = ref.title.strip().replace("\n", " ")
        if len(title) > 60:
            return title[:60].rstrip() + "…"
        return title

    @staticmethod
    def _get_call_for(ref: Ref | None, fallback_id: int) -> str:
        """Render the exact ``get(...)`` call to retrieve the link target."""
        if ref is None:
            return f"get(id={fallback_id})"
        ident = ref.slug if ref.slug is not None else ref.id
        ident_repr = repr(ident) if isinstance(ident, str) else str(ident)
        return f"get(kind={ref.kind!r}, id={ident_repr})"

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        """Default single-ref view: id header + body + tag line.

        Subclasses with richer body shape (e.g. flashcard's Q/A pair) override.
        """
        out = [f"# {self._sense()} {ref.id}", "", ref.title]
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(out)

    def _render_hits_table(self, refs: list[Ref]) -> str:
        """Render a list of refs as a single TOON table.

        Columns: ``kind | id | summary | remaining_words | links``.
        Columns:
          - ``kind``: the ref kind (constant per single-kind list, useful
            cross-kind once that path adopts the same shape).
          - ``id``: ref.id as string.
          - ``summary``: the body's first line in full (no truncation —
            scannable in one glance is the whole point).
          - ``remaining_words``: words *after* the first line, so the
            agent knows how much body is hiding behind the id.
          - ``links``: total edges touching this ref (in + out),
            batched into one COUNT across the page.
          - ``age``: integer days since ``updated_at``. Answers "is
            this stale?" at a glance; a re-tag/edit bumps it back to 0.

        Body source is ``ref.title`` for numeric-ref kinds (memory,
        todo, gripe-body, flashcard-question). Side chunks (gripe comments,
        flashcard answers) aren't included — they show up on the get= fetch.
        2026-06-13 redesign per "scan in one glance" SOUL guidance.
        """
        from precis.format import render_agent_table

        if not refs:
            return ""
        link_counts = self.store.count_links_for_refs([r.id for r in refs])
        now = datetime.now(UTC)
        rows: list[dict[str, str]] = []
        for r in refs:
            body = r.title or ""
            summary, remaining_words = _extract_summary(body)
            age_days = max(0, (now - r.updated_at).days)
            rows.append(
                {
                    "kind": self.kind,
                    "id": str(r.id),
                    "summary": summary,
                    "remaining_words": str(remaining_words),
                    "links": str(link_counts.get(r.id, 0)),
                    "age": str(age_days),
                }
            )
        schema = ["kind", "id", "summary", "remaining_words", "links", "age"]
        return render_agent_table(rows, schema=schema)

    def _render_create_ack(self, ref_id: int) -> Response:
        """Acknowledgement returned by `put` on create.

        Default shape: ``created <kind> id=N.`` + TOON Next: trailer
        listing one or two useful follow-ups. Uses ``self.kind`` (the
        kwarg spelling, e.g. ``flashcard``) — *not* ``self._sense()`` (the
        prose noun, e.g. ``flashcard``) — so the header matches the
        kwarg agents pass on put/tag/link/get. Broad-pass finding #9.

        Subclasses override to add kind-specific hints (e.g. todo's
        STATUS:doing transition, gripe's append-comment recipe).
        """
        body = f"created {self.kind} id={ref_id}."
        body += render_next_section(self._create_ack_next_hints(ref_id))
        return Response(body=body)

    def _create_ack_next_hints(self, ref_id: int) -> list[tuple[str, str]]:
        """Default TOON Next: rows for the create-ack trailer.

        Generic recipes that work on every numeric-ref kind. Subclasses
        prepend their own (status transitions, comment appends, etc.).
        """
        return [
            (
                f"get(kind={self.kind!r}, id={ref_id})",
                f"read this {self._sense()}",
            ),
            (
                f"tag(kind={self.kind!r}, id={ref_id}, add=[...])",
                "add tags",
            ),
            (
                f"delete(kind={self.kind!r}, id={ref_id})",
                f"delete this {self._sense()}",
            ),
        ]

    def _supported_list_views(self) -> tuple[str, ...]:
        """Names of the list views this kind accepts via ``id='/<view>'``.

        Used by the unsupported-view error path to surface a working
        list of recovery options (the MCP critic flagged a dangling
        ``see precis-flashcard-help`` hint pointing at a skill file that
        doesn't exist; enumerating views inline avoids that whole
        class of bug).

        Subclasses extending ``_list_view`` should override this and
        include any kind-specific names they handle (todo: ``open``,
        ``done``, …; flashcard: ``due``). The base class only ships
        ``recent``.
        """
        return ("recent",)

    def _list_view(self, view: str) -> Response | None:
        """Handle ``id='/recent'`` and friends.

        Default returns the most recent 20 refs in reverse-chronological
        order. Subclasses with richer list semantics (todo's open /
        blocked / done filters; flashcard's due) override.

        Returning ``None`` means "I don't recognize this view" — the
        base then raises ``Unsupported``.
        """
        if view in ("", "recent"):
            refs = self.store.list_refs(kind=self.kind, limit=20)
            if not refs:
                # Even empty lists carry a Next: trailer so the agent
                # has a hint how to populate the kind. The MCP critic
                # flagged the silent empty-trailer response as a
                # consistency violation across kinds.
                body = f"no {self._sense()} entries yet"
                body += render_next_section(
                    [
                        (
                            f"put(kind={self.kind!r}, text='...')",
                            f"create your first {self._sense()}",
                        ),
                    ]
                )
                return Response(body=body)
            # F14: render as TOON, with an adaptive ``tags`` column.
            # When at least one ref carries tags, surface them so the
            # agent sees the classification on recall; when no ref has
            # tags, drop the column entirely to avoid noise on the
            # common "all-default" case.
            tags_per_ref = {r.id: self.store.tags_for(r.id) for r in refs}
            any_tagged = any(tags_per_ref[r.id] for r in refs)
            rows: list[dict[str, str]] = []
            for r in refs:
                preview = (r.title[:80] + "…") if len(r.title) > 80 else r.title
                row: dict[str, str] = {"id": str(r.id), "preview": preview}
                if any_tagged:
                    row["tags"] = " ".join(str(t) for t in tags_per_ref[r.id])
                rows.append(row)
            from precis.format import render_agent_table

            schema = ["id", "preview", "tags"] if any_tagged else ["id", "preview"]
            head = f"# recent {self._sense()} ({len(refs)})"
            body = f"{head}\n\n" + render_agent_table(rows, schema=schema)
            body += render_next_section(
                [
                    (
                        f"get(kind={self.kind!r}, id=N)",
                        f"read full {self._sense()} text + tags",
                    ),
                    (
                        f"put(kind={self.kind!r}, text='...')",
                        f"capture a new {self._sense()}",
                    ),
                ]
            )
            return Response(body=body)
        return None
