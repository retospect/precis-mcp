"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Subclasses :class:`NumericRefHandler` — the shared
CRUD shape lives in one place across memory / todo / gripe / anki / conv.

Storage (migration 0050): a memory's **prose lives in a ``memory_body``
chunk**, and ``refs.title`` holds a short **title** — the header. This is
the ``gripe_body`` shape: the body chunk is embedded + keyworded by the
standard workers for free, and display reads a real title instead of
re-splitting the first line off the body every time. Dreams are memories,
so this is the "a dream lives in a chunk, not the ref header" model.

Semantics (`precis-memory-help`):
    - put(text=..., title=...)     — create; text=body prose, title=header
                                     (title derived from the first line when
                                     omitted). Emit the body, then the title.
    - edit(id=N, text=..., title=) — in-place body rewrite (+ optional title)
    - edit(id=N, mode='find-replace'|'insert', find=..., text=...)
                                   — anchored edit of the body, same grammar
                                     as the file kinds (before/after/match/
                                     nth/where/dry_run)
    - edit(id=N, ops=[{find, text, ...}, ...])
                                   — several anchored ops in one call, applied
                                     in order and written in one transaction
                                     (a bad op refuses the whole call)
    - edit(id=N, mode='append', text=...)
                                   — tack text onto the end of the body
                                     (no anchor)
    - tag(id=N, add=[...])         — add/replace tags on memory N
    - link(id=N, target='kind:id') — cross-link memory N to another ref
    - delete(id=N)                 — soft-delete memory N
    - get(id=N)                    — read title + body + tags
    - get(id='/recent')            — list recent memories
    - get(id=N, view='fisheye'|'fisheye+1hop'|'fisheye+2hop'[+recall])
                                   — the walk: link rings via
                                     ``eye_render.render_eye``; ``+recall``
                                     fills k from the focus's ``SPACE:``
                                     tag inside the semantic search
    - search(q=...)                — hybrid search over the body chunks
    - search(q=..., view='index')  — recall: each hit as the session-start
                                     bullet (handle, mirror filename, hook),
                                     shared with ``precis memory index --q``;
                                     ``view='count'`` forwards to the
                                     generic search like no view

Attribution gate (``handlers/_attribution.py``): every body write
(create / edit / supersede) checks that a unit-bearing number next to a
citation token appears in the cited text. A miss tags the memory
``AUDIT:ungrounded-number`` (system-set, cleared by a clean rewrite) and
adds an advisory line to the ack; ``PRECIS_MEMORY_ATTRIBUTION_GATE=reject``
refuses the write instead. A lookup error fails open and changes no tag.
``scripts/memory-attribution-audit`` retro-applies the same check.

Anchored body edits reuse ``utils.edit_resolve``. All handler body writes
lock the owning ref; anchored writes also compare the previous body with
the resolver's snapshot inside the transaction. The comparison alone
would race between the store's read and delete; ``FOR NO KEY UPDATE``
serializes competing edits while allowing mention links' FK key-share
locks (``FOR UPDATE`` deadlocked reciprocal mentions). The mention
wrapper requires healthy ``INTRANS`` after the shared best-effort
helper: COMMIT can silently roll back an aborted transaction, and
psycopg can discard a closed/``UNKNOWN`` connection without raising.
Checking only ``INERROR`` missed that second false-success path. This
pre-commit check does not certify later commit-time connection failures.
Conflicts roll back the chunk replacement, derived-row cascade and audit
event rather than losing a concurrent writer's prose. Whole-body
replacement remains explicit. Creation fields (title, rule, warrant and
hook) travel in a call-local ``put_meta`` payload; staging them on the
shared handler let concurrent puts overwrite or clear each other's
metadata, even on failed requests. Edit writes additionally run inside
:func:`precis.utils.db_retry.retry_locked` (bounded, full jitter): a
``lock_timeout`` collision during an edit burst retries in-process
instead of costing the agent a turn (gr504486); an exhausted budget
refuses with ``Upstream`` + a ``next=`` naming the re-read.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.pq import TransactionStatus

from precis._result_budget import size_cell
from precis.errors import (
    BadInput,
    Gone,
    Internal,
    NotFound,
    Unsupported,
    Upstream,
)
from precis.handlers._argument_view import render_argument_view
from precis.handlers._attribution import (
    AUDIT_VALUE,
    UngroundedNumber,
    gate_mode,
    grounding_failures,
    ungrounded_cited_numbers,
)
from precis.handlers._eye import EYE_LADDER, is_eye_view
from precis.handlers._interpret import interpreted
from precis.handlers._mode_help import require_mode
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.handlers._tag_redirect import redirect_long_tag_values
from precis.handlers.plaintext import _require_find_and_text
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import ActorSlug, ChunkInsert, Ref
from precis.utils import handle_registry
from precis.utils.db_retry import retry_locked
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.match_all import fold_match_all_q
from precis.utils.memory_hubs import (
    link_hints,
    narrowing_hints,
    thread_dup_hint,
)
from precis.utils.next_block import render_next_section
from precis.utils.secret_scan import mask_secrets

#: Heading line that opens a memory-body section — the ``~<slug>``
#: selector grammar (``me<id>~<section>``) splits on these.
_SECTION_HEAD_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)

log = logging.getLogger(__name__)

#: Derived tag the attribution gate maintains (a number next to a citation
#: that the cited text does not carry). See :mod:`precis.handlers._attribution`.
_AUDIT_UNGROUNDED = Tag.closed("AUDIT", AUDIT_VALUE)

#: Max memories that one ``supersede`` call may fold into a survivor.
#: A guardrail, not a quota — the agent can do several small merges.
#: Bounds the blast radius / review cost of a single consolidation;
#: a 30-way merge is almost always over-eager.
_SUPERSEDE_MAX_MERGE = 10

#: Provenance tag forced onto every supersede survivor.
_DREAM_CONSOLIDATED = Tag.closed("DREAM", "consolidated")
# DREAM values only system code mints: ``consolidated`` (the supersede merge)
# and ``acquire`` (paper stubs). ``speculative`` stays agent-writable -- the
# dream agent itself marks its hunches with it through ``put``.
_DREAM_SYSTEM_VALUES = frozenset({"consolidated", "acquire"})


def _refuse_system_dream(tags: list[str] | None, *, verb: str) -> None:
    for t in tags or []:
        if not isinstance(t, str):
            continue
        prefix, _, value = t.partition(":")
        if prefix == "DREAM" and value in _DREAM_SYSTEM_VALUES:
            raise BadInput(
                f"{verb}(kind='memory') cannot write {t!r} -- "
                f"DREAM:{value} is system-set provenance, not author-writable",
                next=(
                    "DREAM:consolidated is stamped by "
                    "supersede merges; DREAM:acquire only by paper "
                    "stubs. Mark a low-confidence hunch with "
                    "tags=['DREAM:speculative'] instead"
                ),
            )


#: Chunk kind holding a memory's body prose (migration 0050). The prose
#: moved off ``refs.title`` — the header is a short title, the body a chunk
#: (embedded + keyworded like ``gripe_body``).
_BODY_KIND = "memory_body"

#: ``view=`` spellings agents reach for on a concrete ``get`` that mean
#: "just read the node" or the link neighbourhood (gr503547 — 7 refusals
#: across 3 gardening sessions). The plain read already is the complete
#: node (body + meta + hook), so ``'full'``/``'meta'``/``'body'`` fold to
#: the default view; ``'index'`` reads as the links view (the hub index).
#: Each alias runs the canonical call and returns with the
#: ``interpreted:`` reading line (gr494831's shape, via
#: :mod:`precis.handlers._interpret`).
_VIEW_ALIASES: dict[str, str | None] = {
    "full": None,
    "meta": None,
    "body": None,
    "index": "links",
}

# ── dream-write guards (2026-10 dream hygiene) ──────────────────
#
# The dream's Step-6c threads flooded recall (11k dreams, ~3% recalled in
# 30d), so the writer's discipline lives at the write boundary, not in the
# prompt: a dream-thread create is capped per day and refused when an
# embedding-nearest live thread memory already covers it.

#: Open-tag literals marking a dream thread memory: ``dream-thread``
#: (current — gr504795 moved it off the bare ``thread:``, an empty-valued
#: collision with the gripe ``thread:<slug>`` axis) plus the legacy
#: ``thread:`` pre-rename memories still carry. A ``thread:<slug>``
#: (non-empty value) is a gripe axis tag, never a dream thread.
_THREAD_TAGS = ("dream-thread", "thread:")


#: Max thread-tagged memories any agent may create per rolling 24h —
#: env ``PRECIS_THREAD_DAILY_CAP``, default 6 (twice the prompt's
#: per-cycle cap of three).
def _thread_daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("PRECIS_THREAD_DAILY_CAP", "6")))
    except ValueError:
        return 6


#: Cosine distance at which a new thread body is a near-duplicate of
#: the closest live thread memory's body embedding — tighter than the
#: corpus relevance floor (these are supposed to be *distinct* threads).
_THREAD_DUP_MAX_DISTANCE = 0.15

#: Default ``auto_refresh_days`` stamped on a DREAM-tagged memory when the
#: writer didn't set one — thread dreams decay slower (the
#: consolidation sweep gives them a 90-day horizon) than plain dreams (60d).
_DREAM_REFRESH_DAYS = 60
_DREAM_THREAD_REFRESH_DAYS = 90


def _dream_stamp_defaults(tags: list[str] | None) -> tuple[int | None, bool]:
    """``(auto_refresh_days, add_space_research)`` a DREAM-tagged create
    should default to. ``add_space_research`` is True when the tag list
    carries a ``DREAM:*`` but no ``SPACE:*`` — system writers pass
    ``default_tags=False`` and would otherwise land a dream unspaced."""
    if not tags or not any(t.startswith("DREAM:") for t in tags):
        return None, False
    days = (
        _DREAM_THREAD_REFRESH_DAYS
        if any(t in tags for t in _THREAD_TAGS)
        else _DREAM_REFRESH_DAYS
    )
    has_space = any(t.startswith("SPACE:") for t in tags)
    return days, not has_space


#: Longest auto-derived title (first line, capped) when a writer doesn't
#: pass an explicit ``title=``. Matches the migration-0050 backfill rule.
_TITLE_MAX = 80

#: ``meta=`` keys a caller may write on put/edit. ``hook`` is the one-line
#: index text of a ``SPACE:repo-dev`` memory (``precis memory index``).
_META_ALLOWED = ("hook", "type")

#: Keys one ``ops=[{...}]`` dict may carry on ``edit`` — the single-op
#: anchored vocabulary plus ``op`` for the op kind (the
#: ``ops=[{'op': ...}]`` convention the structure kind already uses).
_OPS_KEYS = frozenset(
    {"op", "find", "text", "before", "after", "where", "match", "nth"}
)
_OPS_KINDS = ("find-replace", "insert", "append")
_MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _validate_meta(meta: Any) -> dict[str, Any]:
    """Check a caller's ``meta=`` and return the cleaned patch.

    ``hook`` is a non-empty single-line ``str`` (stripped); ``type`` is one of
    user/feedback/project/reference (the mirror export's ``metadata.type``).
    Anything else raises :class:`BadInput` naming the allowed keys.
    """
    example = "meta={'hook': 'one-line index text'}"
    if not isinstance(meta, dict):
        raise BadInput(
            f"meta= must be a dict, got {type(meta).__name__}; "
            f"allowed keys: {list(_META_ALLOWED)}",
            next=example,
        )
    unknown = sorted(str(k) for k in meta if k not in _META_ALLOWED)
    if unknown:
        raise BadInput(
            f"meta= key(s) {unknown} not writable on kind='memory'; "
            f"allowed keys: {list(_META_ALLOWED)}",
            next=example,
        )
    out: dict[str, Any] = {}
    if "hook" in meta:
        hook = meta["hook"]
        if not isinstance(hook, str):
            raise BadInput(
                f"meta['hook'] must be a str, got {type(hook).__name__}",
                next=example,
            )
        hook = hook.strip()
        if not hook:
            raise BadInput("meta['hook'] must be non-empty", next=example)
        if "\n" in hook or "\r" in hook:
            raise BadInput("meta['hook'] must be one line (no newline)", next=example)
        out["hook"] = hook
    if "type" in meta:
        if meta["type"] not in _MEMORY_TYPES:
            raise BadInput(
                f"meta['type'] must be one of {list(_MEMORY_TYPES)}, "
                f"got {meta['type']!r}",
                # The repo-dev section taxonomy (threads/gotchas/…) is a
                # *tag* axis, not meta['type'] — agents conflate them
                # (gr484231). Name both spellings so the recovery lands.
                next=(
                    "meta={'type': 'reference'} — for the repo-dev node "
                    "type use the tag axis instead: "
                    "tags=['section:threads|runbooks|gotchas|workflow|reference']"
                ),
            )
        out["type"] = meta["type"]
    return out


class MemoryHandler(NumericRefHandler):
    #: Agent-authored assertions (graph-node-trust-state slice 1).
    trust_origin: ClassVar[str | None] = "generated"
    spec: ClassVar[KindSpec] = KindSpec(
        kind="memory",
        title="Memory",
        description=(
            "Notes, decisions, ideas, questions. Numeric id assigned on "
            "create. Short title in the header, body prose in a chunk. "
            "Sub-kind via 'kind:' open tag."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        # In-place rewrite via edit(mode='replace', text='...') (broad-pass
        # finding #5), or an anchored find-replace / insert. Same id, links stay attached, audit trail lands
        # in ref_events as a ``body_replaced`` row (view='log').
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
        # edit()'s accepted modes: 'replace' (whole body), the file-kind
        # anchored pair, and 'append' (tack text= onto the end, no anchor —
        # the word agents reach for when updating a thread/resume node;
        # gr484228). See todo.py's annotation (gr292913) for why an
        # omitted mode= still needs this declaration.
        edit_modes=("replace", "find-replace", "insert", "append", "delete"),
    )

    kind: ClassVar[str] = "memory"
    sense: ClassVar[str] = "memory"

    # Every memory lands in a space; a caller's own ``SPACE:`` tag replaces
    # this (closed tags add with ``replace_prefix``). ``repo-dev`` is the
    # dev-harness memory (docs/backlog/memory-native-authoring.md).
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("SPACE:research",)

    # The body lives in a `memory_body` chunk (ord>=0), embedded + keyworded
    # by the standard workers — that chunk is the memory's single embed
    # source. The body-text card is gone (migration 0050): emitting it too
    # would double-embed the same prose. ``emits_card`` stays False; the
    # separate *header* card (title + hook only, never the body — migration
    # 0198) is written by ``ChunksOps.sync_header_card`` on every write path
    # and opted into search via ``search_card_kinds``.
    emits_card: ClassVar[bool] = False
    search_card_kinds: ClassVar[tuple[str, ...] | None] = ("card_combined",)

    # On create/edit, resolve `kind:ref` handles in the body and write
    # `related-to` links to them — so a memory is findable from the refs
    # it cites, not just by its own text. See `_sync_mention_links`.
    autolink_mentions: ClassVar[bool] = True

    # Reinforce first-line discipline at write time (filler-title nudge +
    # skill pointer in the create ack). See precis-firstline-help.
    firstline_discipline: ClassVar[bool] = True

    # Prose lives in the `memory_body` chunk, so search the chunks grouped
    # by ref (base title-only search would miss the body). Heat salience on
    # the hits — memories are the dream frontier. Shared machinery in
    # NumericRefHandler._search_body_chunks / _body_search_hits.
    search_body_chunks: ClassVar[bool] = True
    heat_salience_on_body_search: ClassVar[bool] = True

    #: ``view='index'`` is this kind's complete-handle-list render —
    #: ``view='ids'``/``'list'`` are refused by ``_search_inner``, so the
    #: weak-match fold hint must name 'index' (gr538076).
    search_ids_view: ClassVar[str] = "index"
    # get/tag/link take id=[...]; get is overridden below to render each memory in
    # full (memories are short — the point of batching them is to read them).
    batch_ids: ClassVar[bool] = True
    #: ``edit(id=[...])`` runs the handler's own one-transaction batch.
    edit_id_list: ClassVar[str] = "native"

    # Carry call-local title/argument/index fields through the base's create
    # plumbing; shared handler instances must never stage request state.
    accepts_put_meta: ClassVar[bool] = True

    def link(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        meta: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Response:
        """Selection records keep candidate rejection reasons on their edges."""
        if id is None:
            raise BadInput(
                "memory link requires id=",
                next="link(kind='memory', id=<record>, target='kind:id', rel='...')",
            )
        if isinstance(id, list):
            if meta is not None:
                raise BadInput(
                    "link meta requires a single memory id",
                    next="link each selection record separately with its own meta.reason",
                )
            return self._link_batch(id, target=target, mode=mode, rel=rel)
        return self._link(id=id, target=target, mode=mode, rel=rel, meta=meta)

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        # /recent (base) + /sticky (union of sticky:thread and
        # sticky:global). Broad-pass finding #10. The sticky scope
        # distinction (per-thread vs global) lives on the tag; this
        # view shows the union so the agent can see "what's pinned
        # right now" at a glance.
        return ("recent", "sticky")

    def _list_view(self, view: str) -> Response | None:
        if view == "sticky":
            return self._render_sticky()
        return super()._list_view(view)

    def _render_sticky(self) -> Response:
        """Union of sticky:thread and sticky:global, recency-ordered."""
        thread = self.store.list_refs(kind=self.kind, tags=["sticky:thread"], limit=50)
        glob = self.store.list_refs(kind=self.kind, tags=["sticky:global"], limit=50)
        # Dedup by ref.id (a memory can carry both tags).
        seen: set[int] = set()
        refs = []
        for r in sorted(
            list(thread) + list(glob),
            key=lambda r: r.updated_at,
            reverse=True,
        ):
            if r.id in seen:
                continue
            seen.add(r.id)
            refs.append(r)
        if not refs:
            return Response(
                body=(
                    "no sticky memories. pin one: "
                    "tag(kind='memory', id=N, add=['sticky:thread'])"
                )
            )
        header = (
            f"# {len(refs)} sticky {self._sense()} "
            f"entr{'y' if len(refs) == 1 else 'ies'}"
        )
        return Response(body=f"{header}\n{self._render_hits_table(refs)}")

    # ── get: default single-ref + view='argument' ─────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        # ``view='diff'``'s sha (``args={'since': <sha>}``) — forwarded to
        # the base dispatch; declared so the extras gate accepts it here.
        since: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, str) and "~" in id:
            return self._get_section(id=id, view=view, q=q)
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        # view='argument' on a concrete id renders the kind-scoped argument
        # graph (proof tree + stale-premise + inherited-caveat flags).
        # Path-views (id='/recent', id='/sticky') and the base views
        # (links/log/raw) fall through to NumericRefHandler.get unchanged
        # — same dispatch shape as QuestHandler.get's view='tree'.
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if view == "argument" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return render_argument_view(self.store, ref)
        if concrete and is_eye_view(view):
            return self.eye(id=id, extent=view, q=q)
        if concrete and q is None and view in _VIEW_ALIASES:
            canon = _VIEW_ALIASES[view]
            shown = f"view={canon!r}" if canon is not None else "the default view"
            resp = super().get(id=id, view=canon, q=q, since=since, **_kw)
            return interpreted(resp, [f"view={view!r} read as {shown}"])
        if concrete and view is not None and view not in _BASE_VIEWS:
            legal = ["argument", *EYE_LADDER, *_BASE_VIEWS]
            close = difflib.get_close_matches(view, legal, n=1)
            raise Unsupported(
                f"unknown view {view!r} for kind='memory'",
                options=legal,
                next=(
                    (f"did you mean {close[0]!r}? " if close else "")
                    + "view='argument' (kind:lemma/kind:inference proof tree) "
                    f"· {'|'.join(EYE_LADDER)}, optionally +recall (the eye) "
                    "· links, log, raw (generic)"
                ),
            )
        resp = super().get(id=id, view=view, q=q, since=since, **_kw)
        if view is None and q is None and id is not None and not isinstance(id, list):
            resp = self._with_budget_outline(resp, id)
        return resp

    def _get_section(self, *, id: str, view: str | None, q: str | None) -> Response:
        """``get(id='<id>~<section>')`` — one headed part of the body.

        The targeted read an over-cap whole-node read advertises: the
        body's ``#``-headed sections each get a slug, and ``~body``
        always reads the body prose whole (no links/tags wrapper).
        """
        if view is not None or q is not None:
            raise BadInput(
                "a memory ~<section> read takes no view= or q=",
                next="get(kind='memory', id=<id>~<section>)",
            )
        base, _, selector = id.partition("~")
        sel = selector.strip()
        if not sel:
            raise BadInput(
                f"malformed memory id {id!r}: empty selector after '~'",
                next="get(kind='memory', id=<id>~<section>)",
            )
        ref = self._resolve_live_ref(self._coerce_id(base))
        return self._render_section(ref, sel)

    @staticmethod
    def _section_slug(title: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
        return s or "section"

    def _body_sections(self, body: str) -> list[tuple[str, str, str]]:
        """``(slug, title, text)`` per ``#``-headed section, body order."""
        matches = list(_SECTION_HEAD_RE.finditer(body))
        out: list[tuple[str, str, str]] = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            title = m.group(1).strip()
            out.append(
                (self._section_slug(title), title, body[m.start() : end].rstrip())
            )
        return out

    def _render_section(self, ref: Ref, sel: str) -> Response:
        body = self._body_text(ref)
        handle = handle_registry.try_format(self.kind, ref.id) or str(ref.id)
        sections = self._body_sections(body)
        if sel == "body":
            text = self._strip_title_echo(body, ref.title or "").strip()
            return Response(
                body=f"# memory {ref.id}~body\n\n{text}"
                + render_next_section(
                    [(f"get(kind='memory', id={ref.id})", "header + links too")]
                )
            )
        match = next((s for s in sections if s[0] == sel), None)
        if match is None:
            match = next((s for s in sections if s[1].lower() == sel.lower()), None)
        if match is None:
            legal = [s[0] for s in sections] or ["body"]
            raise NotFound(
                f"no section ~{sel!r} in {handle}",
                options=legal,
                next=f"get(kind='memory', id='{handle}~{legal[0]}')",
            )
        slug, title, text = match
        out = f"# memory {ref.id}~{slug} — {title}\n\n{text}"
        out += render_next_section(
            [
                (f"get(kind='memory', id={ref.id})", "the whole node"),
                (
                    f"get(kind='memory', id={ref.id}, view='fisheye+1hop')",
                    "read with linked neighbours",
                ),
            ]
        )
        return Response(body=out)

    def _with_budget_outline(self, resp: Response, id: Any) -> Response:
        """Attach the ``~<section>`` map an over-cap read prints — lazily.

        The thunk keeps the ref resolve + body load + section parse off
        every under-cap get; the fitter calls it only when a cap binds.
        The hint names ``~body`` — always a valid explicit read — so it
        too costs no body load here.
        """
        from dataclasses import replace

        return replace(
            resp,
            budget_outline=lambda: self._outline_text(id),
            result_budget_hint=resp.result_budget_hint
            or f"get(kind='memory', id='{id}~body') reads the body whole",
        )

    def _outline_text(self, id: Any) -> str | None:
        ref = self._resolve_live_ref(self._coerce_id(id))
        body = self._body_text(ref)
        handle = handle_registry.try_format(self.kind, ref.id) or str(ref.id)
        from precis.format import render_agent_table

        sections = self._body_sections(body)
        rows = [
            {
                "section": f"{handle}~{slug}",
                "size": size_cell(len(text.encode("utf-8"))),
                "title": title,
            }
            for slug, title, text in sections
        ]
        rows.append(
            {
                "section": f"{handle}~body",
                "size": size_cell(len(body.encode("utf-8"))),
                "title": "",
            }
        )
        return render_agent_table(rows, schema=["section", "size", "title"])

    def _get_batch(self, ids: list[Any], *, view: str | None) -> Response:
        """``get(id=[...])``: each memory in full, in the order given.

        Same body as a single ``get``, minus a ``Next:`` trailer per memory;
        one trailer at the end points at the fisheye neighbourhood. A missing
        or deleted id is a one-line block.
        """
        ref_ids = self._coerce_ids(ids)
        if view is not None:
            return self._get_batch_view(ref_ids, view=view)
        blocks: list[str] = []
        for rid in ref_ids:
            handle = handle_registry.try_format(self.kind, rid) or str(rid)
            try:
                blocks.append(NumericRefHandler.get(self, id=rid).body)
            except NotFound:
                blocks.append(f"{handle}  not found")
            except Gone:
                blocks.append(f"{handle}  deleted")
        body = f"# {len(ref_ids)} memory (batch)\n\n" + "\n\n---\n\n".join(blocks)
        body += render_next_section(
            [
                (
                    f"get(kind='memory', id={ref_ids[0]}, view='fisheye+1hop')",
                    "read one with its linked neighbours",
                )
            ]
        )
        return Response(body=body)

    # ── put / create: ref (title) + memory_body chunk ───────────────

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
        auto_refresh_days: int | None = None,
        rule: str | None = None,
        warrant: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a memory. ``text=`` is the body prose, ``title=`` the short
        header. Emit the body first, then a title once its point is clear.

        When ``title=`` is omitted it's derived from the body's first line
        (capped at 80 chars) — but an explicit title reads better and is
        easier to navigate, so pass one. The body lands in a ``memory_body``
        chunk (embedded + keyworded); ``refs.title`` carries the header.

        ``rule=`` / ``warrant=`` (the argument graph) label a
        ``kind:inference`` memory's reasoning step — ``rule`` the operator
        (e.g. ``'and-intro'``, ``'modus-ponens'``, free text allowed),
        ``warrant`` free-text prose for *why* the step holds. Author
        assertions; precis stores and surfaces them, never verifies
        validity. Meaningless (but harmless) on a non-inference memory.

        ``meta={'hook': '...'}`` sets the memory's one-line index text
        (``refs.meta.hook``); ``type`` (user/feedback/project/reference) is the other writable key.
        """
        if id is not None:
            return self._put_on_existing(
                id=id,
                text=text,
                title=title,
                mode=mode,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
                rule=rule,
                warrant=warrant,
                meta=meta,
            )
        # This fresh payload is passed down the stack, never staged on self.
        # Validate the public meta allowlist before adding internal fields.
        # Automated writers cannot rephrase: mask here. Agent verb calls are
        # refused at the dispatch boundary (runtime/dispatch.py) first.
        _refuse_system_dream(tags, verb="put")
        if tags and any(t in tags for t in _THREAD_TAGS):
            self._guard_thread_create(text or "")
        text, title = mask_secrets(text), mask_secrets(title)
        rule, warrant = mask_secrets(rule), mask_secrets(warrant)
        create_meta = _validate_meta(meta) if meta is not None else {}
        for key, value in (("title", title), ("rule", rule), ("warrant", warrant)):
            if isinstance(value, str) and value.strip():
                create_meta[key] = value.strip()
        if "hook" in create_meta:
            create_meta["hook"] = mask_secrets(create_meta["hook"])
        return super().put(
            id=id,
            text=text,
            mode=mode,
            tags=tags,
            untags=untags,
            link=link,
            unlink=unlink,
            rel=rel,
            auto_refresh_days=auto_refresh_days,
            meta=create_meta,
        )

    def _guard_thread_create(self, body: str) -> None:
        """Refuse a dream-thread create that busts the daily cap or
        duplicates a live thread memory by embedding (the write-boundary
        half of the dream's thread discipline — the prompt can only ask
        nicely). The thread set is ``dream-thread`` plus legacy ``thread:``."""
        with self.store.pool.connection() as conn:
            n_row = conn.execute(
                "SELECT count(*) FROM refs r "
                "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
                "AND r.created_at > now() - interval '24 hours' "
                "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
                "  ON t.tag_id = rt.tag_id WHERE rt.ref_id = r.ref_id "
                "  AND t.namespace = 'OPEN' AND t.value = ANY(%s))",
                (list(_THREAD_TAGS),),
            ).fetchone()
        n_today = int(n_row[0]) if n_row else 0
        cap = _thread_daily_cap()
        if n_today >= cap:
            raise BadInput(
                f"dream-thread memories are capped at {cap} per day "
                f"({n_today} already live today)",
                next="skip this thread, or fold it into an existing "
                "thread memory via edit(mode='append')",
            )
        embedder = getattr(self.hub, "embedder", None)
        if embedder is None or not body.strip():
            return
        try:
            vec = embedder.embed_one(body)
        except Exception:
            log.warning("memory: thread-dedup embed failed; skipping", exc_info=True)
            return
        with self.store.pool.connection() as conn:
            emb = conn.execute(
                "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
            ).fetchone()
            embedder_name = str(emb[0]) if emb else None
            if embedder_name is None:
                return
            row = conn.execute(
                "SELECT c.ref_id, ce.vector <=> %s::vector AS dist "
                "FROM chunks c "
                "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
                " AND ce.embedder = %s AND ce.status = 'ok' "
                "JOIN refs r ON r.ref_id = c.ref_id AND r.retired_at IS NULL "
                "WHERE c.chunk_kind = 'memory_body' AND c.retired_at IS NULL "
                "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
                "  ON t.tag_id = rt.tag_id WHERE rt.ref_id = r.ref_id "
                "  AND t.namespace = 'OPEN' AND t.value = ANY(%s)) "
                "ORDER BY ce.vector <=> %s::vector LIMIT 1",
                (
                    str(list(vec)),
                    embedder_name,
                    list(_THREAD_TAGS),
                    str(list(vec)),
                ),
            ).fetchone()
        if row is not None and float(row[1]) <= _THREAD_DUP_MAX_DISTANCE:
            raise BadInput(
                f"thread memory me{int(row[0])} already covers this "
                f"(embedding distance {float(row[1]):.3f} "
                f"<= {_THREAD_DUP_MAX_DISTANCE})",
                next=f"edit(kind='memory', id={int(row[0])}, mode='append', "
                "text='…') to add a follow-up, or skip the thread",
            )

    def _put_on_existing(
        self,
        *,
        id: str | int,
        text: str | None,
        title: str | None,
        mode: str | None,
        tags: list[str] | None,
        untags: list[str] | None,
        link: str | None,
        unlink: str | None,
        rel: str | None,
        rule: str | None,
        warrant: str | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        """``put(id=N, ...)`` on memory: delegate the link shape, refuse the rest.

        ``id`` + ``link`` (+``rel``/``tags``) is "attach an edge" and runs
        through ``link()``; anything else on an existing memory points at
        the verb that does it (gr495608).
        """
        n = self._coerce_id(id)
        if mode is not None or untags is not None or unlink is not None:
            raise BadInput(
                f"put creates new memories; memory id={n} already exists, "
                "and mode=/untags=/unlink= are not put arguments",
                next=(
                    f"link(kind='memory', id={n}, target='kind:slug', mode='remove') "
                    "to drop an edge / "
                    f"tag(kind='memory', id={n}, remove=['...']) to drop a tag / "
                    f"edit(kind='memory', id={n}, mode='replace', text='<new body>') "
                    "to rewrite the body"
                ),
            )
        has_body = any(
            v is not None and v != {} for v in (text, title, rule, warrant, meta)
        )
        if link is None or has_body:
            raise BadInput(
                f"put creates new memories; memory id={n} already exists",
                next=(
                    f"edit(kind='memory', id={n}, mode='replace', text='<new body>') / "
                    f"edit(kind='memory', id={n}, mode='find-replace', "
                    "find='<old>', text='<new>') / "
                    f"edit(kind='memory', id={n}, args={{'title': '<title>', "
                    "'hook': '<one-line index>'}}) to fix title/meta.hook / "
                    f"put(kind='memory', text='...', link='memory:{n}', rel='part-of')"
                ),
            )
        before = self.store.count_links_for_refs([n]).get(n, 0)
        out = self.link(id=n, target=link, rel=rel or "related-to")
        added = self.store.count_links_for_refs([n]).get(n, 0) > before
        if tags:
            self.tag(id=n, add=tags)
        body = out.body if added else f"already linked, nothing added: {out.body}"
        return Response(
            body=body
            + f"\nlinked via link(); next time use link(kind='memory', id={n}, "
            "target=..., rel=...)"
        )

    def _create(self, **kw: Any) -> Response:
        return self._create_with_id(**kw)[0]

    def _create_with_id(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
        put_meta: dict[str, Any] | None = None,
        set_by: ActorSlug = "agent",
        system_meta: dict[str, Any] | None = None,
        default_tags: bool = True,
    ) -> tuple[Response, int]:
        # Mirror NumericRefHandler._create, but the body prose goes into a
        # `memory_body` chunk (not `refs.title`) and the header gets a short
        # title. The body chunk picks up embeddings + keywords from the
        # standard workers automatically — no `card_combined` card.
        from precis.utils.link_tag_ops import (
            guard_taxon_hierarchy,
            validate_relation,
        )
        from precis.utils.link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text= (the body prose)",
                next=(
                    f"put(kind={self.kind!r}, text='the thought', title='short title')"
                ),
            )
        body = text
        create_meta = put_meta or {}
        title = create_meta.get("title") or _derive_title(body)
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel)
        if target is not None:
            # Taxon hierarchy rules (docs/backlog/term-taxonomy.md): this
            # create path writes its own link, so it calls the shared guard.
            guard_taxon_hierarchy(
                self.store, None, target, relation, src_kind=self.kind
            )

        # meta.rule / meta.warrant — stamped at create time
        # when the D3-shortcut kwargs were passed; omitted keys entirely
        # when absent (an inference gains them incrementally via edit()
        # too, so a rule-first / warrant-later authoring order works).
        meta: dict[str, Any] = {
            k: create_meta[k]
            for k in ("rule", "warrant", "hook", "type")
            if k in create_meta
        }
        if system_meta:
            meta.update(system_meta)

        all_tag_strs: list[str] = (
            list(self.default_tags_on_create) if default_tags else []
        )
        if tags:
            all_tag_strs.extend(tags)
        # Dream stamping (2026-10 hygiene): every agent-written
        # DREAM-tagged memory carries SPACE:research and an
        # auto_refresh_days floor unless the writer set one — decay is
        # what keeps the dream corpus self-limiting between consolidation
        # sweeps. ``default_tags=False`` writers (``write_system``) stamp
        # their own tags and keep their no-default-SPACE contract.
        if default_tags:
            dream_days, add_space = _dream_stamp_defaults(all_tag_strs)
            if add_space:
                all_tag_strs.append("SPACE:research")
            if auto_refresh_days is None:
                auto_refresh_days = dream_days

        # Attribution gate: before any write, so ``reject`` mode leaves nothing.
        misses = self._attribution_misses(body, reject=set_by == "agent")
        facts_deferred = 0

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=title,
                meta=meta,
                auto_refresh_days=auto_refresh_days,
                conn=conn,
            )
            # Body → memory_body chunk at pos 0, written *before* the tag
            # redirect below (which appends any overflow chunk at the next
            # free ord), so the body always sits at ord 0.
            body_blocks = self.store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=0, text=body, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
            # Redirect long/whitespace ask-user:/halt: yields into an
            # overflow chunk before parse_strict's whitespace guard fires
            # (gripe #39254). On the create connection so it lands atomically.
            redirected, _redirected_chunks = redirect_long_tag_values(
                self.store, ref_id=ref.id, tags=all_tag_strs, conn=conn
            )
            parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in redirected]
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by=set_by,
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
            if misses:
                self._apply_attribution_tag(ref.id, misses, conn=conn)
            if self.autolink_mentions:
                facts_deferred = self._sync_mention_links(ref.id, body, conn=conn)
            self.store.chunks.sync_header_card(ref.id, conn=conn)
        # Attribute the new memory's body chunk to the current agent run
        # (Slice B provenance) — a no-op unless PRECIS_CURRENT_AGENTLOG is
        # set (e.g. the dream pass), so a dream's own memories walk back to
        # the tick that wrote them. After the tx above commits, so the
        # attribution query sees the just-inserted chunk row.
        from precis import agentlog

        agentlog.touch_from_env(self.store, chunk_ids=[b.id for b in body_blocks])
        ack = self._with_first_line_nudge(self._render_create_ack(ref.id), title)
        ack = self._facts_note(ack, facts_deferred)
        return self._with_attribution_advisory(ack, misses), int(ref.id)

    # ── attribution gate (precis.handlers._attribution) ──

    def write_system(
        self,
        *,
        text: str,
        title: str | None = None,
        tags: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Create a memory on behalf of a system writer; return its ref id.

        The same path as an agent ``put`` (title short, body in a
        ``memory_body`` chunk, mention autolink, attribution audit), minus
        what only makes sense for an agent: tags are stamped
        ``set_by='system'``, ``meta`` may carry arbitrary keys (not just the
        public allowlist), and ``PRECIS_MEMORY_ATTRIBUTION_GATE=reject``
        never drops the write -- an ungrounded number is still tagged
        ``AUDIT:ungrounded-number``, the memory still lands. No default
        ``SPACE:`` tag: a system digest is not research recall.
        """
        text = mask_secrets(text) or ""
        put_meta = {"title": mask_secrets(title).strip()} if title else None
        _resp, ref_id = self._create_with_id(
            text=text,
            tags=tags,
            link=None,
            put_meta=put_meta,
            set_by="system",
            system_meta=meta,
            default_tags=False,
        )
        return ref_id

    def _attribution_misses(
        self, body: str, *, reject: bool = True
    ) -> list[UngroundedNumber] | None:
        """Numbers in ``body`` pinned to a citation whose text lacks them.

        Fails open: a lookup error never blocks a memory write and returns
        ``None`` ("not checked": callers change no tag). In
        ``PRECIS_MEMORY_ATTRIBUTION_GATE=reject`` mode a miss raises
        :class:`BadInput` (before the caller writes anything).
        """
        try:
            misses = grounding_failures(ungrounded_cited_numbers(self.store, body))
        except Exception:
            log.warning("memory attribution gate failed open", exc_info=True)
            return None
        if misses and reject and gate_mode() == "reject":
            raise BadInput(
                "\n".join(m.advisory() for m in misses),
                next=(
                    "drop the attribution (remove the handle from that "
                    "sentence) or mark the figure your own: write "
                    "'~10 nm (my estimate)'; then retry the write"
                ),
            )
        return misses

    def _apply_attribution_tag(
        self, ref_id: int, misses: list[UngroundedNumber] | None, *, conn: Connection
    ) -> None:
        """Set ``AUDIT:ungrounded-number`` on a miss, clear it on a clean body.

        Derived like ``STALE:retracted-premise``: the latest body decides.
        ``misses=None`` (gate failed open) changes nothing, and a clean body
        only clears a tag the gate itself set (``set_by='system'``): a
        hand-set flag survives.
        """
        if misses is None:
            return
        if misses:
            self.store.add_tag(ref_id, _AUDIT_UNGROUNDED, set_by="system", conn=conn)
            return
        row = conn.execute(
            "SELECT rt.set_by FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'AUDIT' AND t.value = %s",
            (ref_id, AUDIT_VALUE),
        ).fetchone()
        if row is not None and row[0] == "system":
            self.store.remove_tag(ref_id, _AUDIT_UNGROUNDED, conn=conn)

    @staticmethod
    def _with_attribution_advisory(
        ack: Response, misses: list[UngroundedNumber] | None
    ) -> Response:
        if not misses:
            return ack
        lines = "\n".join(m.advisory() for m in misses)
        return Response(body=f"{ack.body}\n\n{lines}", cost=ack.cost)

    def _create_ack_next_hints(self, ref_id: int) -> list[tuple[str, str]]:
        """Lead the create-ack trailer with the title/first-line convention,
        a duplicate-thread nudge when the new ``section:threads`` node's
        title prefix matches a live peer (gr485358 — one thread node per
        campaign, so a same-prefix ``put`` should have been an in-place
        rewrite), then the generic read/tag/delete recipes."""
        return [
            *thread_dup_hint(self.store, ref_id),
            (
                "get(kind='skill', id='precis-firstline-help')",
                "title conventions (lead with the conclusion)",
            ),
            *link_hints(self, ref_id),
            *_roundtrip_hint(self, ref_id),
            *super()._create_ack_next_hints(ref_id),
        ]

    # ── edit: in-place body rewrite (+ optional title) ──────────────

    def edit(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        mode: str = "replace",
        text: str | None = None,
        title: str | None = None,
        rule: str | None = None,
        warrant: str | None = None,
        meta: dict[str, Any] | None = None,
        hook: str | None = None,
        find: str | None = None,
        before: str = "",
        after: str = "",
        where: str | None = None,
        match: str = "unique",
        nth: int | None = None,
        ops: list[dict[str, Any]] | None = None,
        dry_run: bool | str = False,
        reason: str | None = None,
        **_kw: Any,
    ) -> Response:
        """In-place edit of a memory's body prose, and/or its argument-graph
        ``meta.rule`` / ``meta.warrant``.

        Three body modes:

        - ``mode='replace'`` (the default for a direct handler call):
          ``text=`` is the whole new body; ``title=`` optionally updates the
          header (omit to keep it).
        - ``mode='find-replace'``: ``find=`` locates literal text in the body
          and ``text=`` replaces it (``text=''`` deletes the span).
        - ``mode='insert'``: ``text=`` goes ``where='before'|'after'`` the
          ``find=`` match.
        - ``mode='append'``: ``text=`` is tacked onto the end of the body —
          no anchor (the "add an update" door).
        - ``ops=[{...}, ...]``: several anchored ops in one call. Each dict
          takes the anchored grammar — ``find`` / ``text`` / ``before`` /
          ``after`` / ``match`` / ``nth``, ``'op': 'insert'`` + ``'where'``
          switches op kind (default ``'find-replace'``), ``'op': 'append'``
          needs only ``text``. The ops apply in order, each against the
          previous op's result, and the batch lands in a single
          transaction — a bad op refuses the whole call with ``ops[i]``
          naming the failing one and nothing written. Top-level
          find=/text=/title=/rule=/warrant= are refused alongside ``ops=``;
          ``meta=``, ``reason=`` and ``dry_run=`` ride along.

        The anchored pair takes the file kinds' vocabulary — ``before=`` /
        ``after=`` anchors, ``match='unique'|'first'|'all'|'nth'`` + ``nth=``,
        ``dry_run=True`` (unified diff, nothing written) — resolved by the
        shared :mod:`precis.utils.edit_resolve`, so a one-word fix in a large
        body needs no re-send. ``title=`` / ``rule=`` / ``warrant=`` /
        ``meta=`` ride every mode's one transaction (gr506768) — a retitle
        or an argument-label fix pairs with an anchored body edit in one
        call; ``title=`` alone (with ``mode='replace'``) is the retitle
        door.

        Every body write keeps the old body in ``ref_events`` (recoverable /
        auditable via ``get(kind='memory', id=N, view='log')``) and
        delete+reinserts the ``memory_body`` chunk so semantic search +
        keywords re-derive from the new prose (an in-place UPDATE would leave
        a stale embedding). A body write also re-derives ``refs.title``
        from the new first line when the stored title still reads as the
        old body's derivation — a hand-set title is kept, and a same-call
        ``title=`` always wins (gr538501); the ack says
        ``title re-derived: '…'`` when it fires.

        ``rule=`` / ``warrant=`` patch a ``kind:inference`` memory's
        reasoning-step label / free-text justification without requiring a
        body rewrite — pass either (or both) alone to refine the warrant as
        understanding sharpens, no ``text=`` required. ``meta={'hook': '...'}``
        sets the one-line index text (``refs.meta.hook``), likewise alone or
        with a body edit; ``args={'hook': '...'}`` is the flat spelling of
        the same write (gr538472). At least one of ``text=``, ``title=``,
        ``rule=``, ``warrant=``, ``meta=``, ``hook=`` must be given.

        The MCP ``edit`` verb defaults ``mode`` to ``'find-replace'``, so an
        omitted ``mode=`` arrives as that. With ``find=`` it is an anchored
        edit; with neither ``find=`` nor ``text=`` it is the meta / rule /
        warrant patch — a metadata-only call (``title=`` / ``hook=`` /
        ``meta=`` / ``rule=`` / ``warrant=``, no ``find=``/``text=``)
        patches under either no-anchor mode (``'replace'`` or the MCP
        default ``'find-replace'``; gr538472), while the body modes
        ``'insert'`` / ``'append'`` / ``'delete'`` still demand their
        anchors. ``text=`` alone is refused — it would otherwise
        silently overwrite the whole body — and points at ``mode='replace'``.

        ``reason=`` is recorded first in the body edit's ``ref_events`` row, so
        ``view='log'`` shows it beside the diff: a recall that found the memory
        wrong writes ``reason='misled: <what was wrong, how found>'``.

        Distinct from ``supersede`` (the consolidate-into-new verb): edit
        keeps the same id and every inbound link — the "polish the wording"
        affordance.

        ``id=[...]`` (``batch_ids``) applies the same edit to every id in
        one transaction — :meth:`_edit_batch` (gr514482).
        """
        if isinstance(id, list):
            return self._edit_batch(
                id,
                mode=mode,
                text=text,
                title=title,
                rule=rule,
                warrant=warrant,
                meta=meta,
                hook=hook,
                find=find,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                ops=ops,
                dry_run=dry_run,
                reason=reason,
                **_kw,
            )
        if id is None:
            raise BadInput(
                "edit(kind='memory') requires id=",
                next="edit(kind='memory', id=N, mode='replace', text='new body')",
            )
        require_mode(spec=self.spec, verb="edit", mode=mode)
        delete_span = mode == "delete"
        if delete_span:
            # Explicit span deletion (gr503460): several MCP clients
            # mangle an empty ``text=''`` argument before the call ever
            # reaches the server, so the delete-by-empty-text shape got
            # a first-class mode. Alias of find-replace with text=''.
            if text is not None and text != "":
                raise BadInput(
                    "mode='delete' takes find= only",
                    next=(
                        "edit(kind='memory', id=N, mode='delete', "
                        "find='exact span to remove') — or mode='find-replace' "
                        "with text='' where the client passes it through"
                    ),
                )
            if not find:
                raise BadInput(
                    "mode='delete' requires find= (the exact span to remove)",
                    next="edit(kind='memory', id=N, mode='delete', find='exact text')",
                )
            mode, text = "find-replace", ""
        text, title = mask_secrets(text), mask_secrets(title)
        rule, warrant = mask_secrets(rule), mask_secrets(warrant)
        hook = mask_secrets(hook)
        reason = mask_secrets(reason)
        has_text = text is not None and text.strip()
        rule_clean = rule.strip() if isinstance(rule, str) and rule.strip() else None
        warrant_clean = (
            warrant.strip() if isinstance(warrant, str) and warrant.strip() else None
        )
        meta_clean = _validate_meta(meta) if meta is not None else {}
        if "hook" in meta_clean:
            meta_clean["hook"] = mask_secrets(meta_clean["hook"])
        if hook is not None:
            if "hook" in meta_clean:
                raise BadInput(
                    "the hook was passed twice — hook= and meta={'hook': …}",
                    next="edit(kind='memory', id=N, args={'hook': 'one-line index text'})",
                )
            meta_clean["hook"] = _validate_meta({"hook": hook})["hook"]
        new_title = title.strip() if isinstance(title, str) and title.strip() else None
        dry_mode = normalize_dry_run(dry_run)
        if ops is not None:
            if mode not in ("find-replace", "replace"):
                raise BadInput(
                    f"ops= cannot combine with mode={mode!r} — each op "
                    "dict carries its own 'op': 'find-replace'|'insert'|"
                    "'append' (default 'find-replace')",
                    next=(
                        f"edit(kind='memory', id={id}, ops=[{{'find': 'old', "
                        "'text': 'new'}}, ...])"
                    ),
                )
            mixed = [
                name
                for name, val in (
                    ("find", find),
                    ("text", text),
                    ("title", new_title),
                    ("rule", rule_clean),
                    ("warrant", warrant_clean),
                    ("before", before or None),
                    ("after", after or None),
                    ("where", where),
                    ("nth", nth),
                    ("match", match if match != "unique" else None),
                )
                if val is not None
            ]
            if mixed:
                raise BadInput(
                    f"{'/'.join(mixed)}= is a single-edit argument — with "
                    "ops= it belongs inside each op dict",
                    next=(
                        f"edit(kind='memory', id={id}, ops=[{{'find': 'old', "
                        "'text': 'new'}}, ...]) — meta=, reason= and "
                        "dry_run= may ride along at top level"
                    ),
                )
            return self._edit_ops(
                ref_id=self._coerce_id(id),
                ops=ops,
                dry_mode=dry_mode,
                meta_patch=meta_clean,
                reason=reason,
            )
        # Only the MCP default with neither body argument is a metadata
        # patch. Explicit insert/append and even empty text must validate
        # anchors (append's check is its own "no anchor" rule).
        if mode in ("insert", "append") or (
            mode == "find-replace" and (find is not None or text is not None)
        ):
            # ``title=`` / ``rule=`` / ``warrant=`` ride the anchored
            # edit's one transaction the way ``meta=`` always did
            # (gr506768 — agents repeatedly pair a span fix with a
            # retitle; refusing split one intent into two calls).
            anchored_meta = dict(meta_clean)
            if rule_clean is not None:
                anchored_meta["rule"] = rule_clean
            if warrant_clean is not None:
                anchored_meta["warrant"] = warrant_clean
            return self._edit_anchored(
                ref_id=self._coerce_id(id),
                mode=mode,
                find=find,
                text=text,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                dry_mode=dry_mode,
                meta_patch=anchored_meta,
                new_title=new_title,
                reason=reason,
                delete_span=delete_span,
            )
        if dry_mode is not None:
            raise BadInput(
                "dry_run= previews mode='find-replace' / 'insert' / "
                "'append' edits only",
                next=(
                    "edit(kind='memory', id=N, mode='find-replace', "
                    "find='old', text='new', dry_run=True)"
                ),
            )
        if (
            not has_text
            and new_title is None
            and rule_clean is None
            and warrant_clean is None
            and not meta_clean
        ):
            raise BadInput(
                "edit(kind='memory') requires text=, title=, hook=, rule=, "
                "warrant=, or meta=",
                next=(
                    "edit(kind='memory', id=N, mode='replace', text='new body') "
                    "or edit(kind='memory', id=N, mode='find-replace', "
                    "find='old', text='new') "
                    "or edit(kind='memory', id=N, args={'title': 'new title', "
                    "'hook': 'one-line index text'}) "
                    "or edit(kind='memory', id=N, mode='replace', "
                    "args={'warrant': 'updated justification'})"
                ),
            )
        ref_id = self._coerce_id(id)
        # _resolve_live_ref raises NotFound/Gone with the right taxonomy if
        # the memory doesn't exist or was soft-deleted.
        ref = self._resolve_live_ref(ref_id)
        meta_patch: dict[str, Any] = {}
        if rule_clean is not None:
            meta_patch["rule"] = rule_clean
        if warrant_clean is not None:
            meta_patch["warrant"] = warrant_clean
        meta_patch.update(meta_clean)
        if not has_text:
            # Meta-only patch (rule=/warrant=/meta=, no body rewrite) — skip the
            # chunk delete+reinsert and mention re-sync entirely; nothing
            # in the prose changed.
            def _patch_meta() -> None:
                with self.store.tx() as conn:
                    self.store.update_ref(ref.id, meta_patch=meta_patch, conn=conn)
                    if new_title is not None:
                        self.store.chunks.set_ref_title(
                            ref.id, new_title, source="agent", conn=conn
                        )
                    self.store.chunks.sync_header_card(ref.id, conn=conn)

            self._write_retried(ref.id, "meta edit", _patch_meta)
            changed = ", ".join(
                k for k in ("rule", "warrant", "hook", "type") if k in meta_patch
            )
            out = f"updated {self._sense()} id={ref.id} meta: {changed}"
            if new_title is not None:
                out += f". title now: {new_title!r}"
            return Response(body=out + render_next_section(link_hints(self, ref.id)))

        assert text is not None
        misses = self._attribution_misses(text)
        deferred: list[int] = []
        derived_title: list[str] = []
        old_body = self._write_body(
            ref.id,
            text,
            new_title=new_title,
            meta_patch=meta_patch,
            misses=misses,
            reason=reason,
            deferred_out=deferred,
            derived_title_out=derived_title,
        )
        nudge = self._first_line_nudge(new_title) if new_title is not None else None
        old_words = len((old_body or "").split())
        new_words = len(text.split())
        body = (
            f"replaced body of {self._sense()} id={ref.id} "
            f"({old_words} → {new_words} words)."
        )
        if new_title is not None:
            body += f" title now: {new_title!r}."
        elif derived_title:
            body += f" title re-derived: {derived_title[0]!r}."
        body += " view='log' for the full diff."
        if nudge:
            body += f"\n\nhint: {nudge}"
        lead = self._thread_lead_hint(ref.id, old_body, text)
        if lead:
            body += f"\n\nhint: {lead}"
        body += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=body), deferred[0] if deferred else 0),
            misses,
        )

    def _thread_lead_hint(
        self, ref_id: int, old_body: str | None, new_body: str
    ) -> str | None:
        """Reminder for a ``section:threads`` node whose lead line survived a body edit.

        The first body line of a thread node is its ``Left: …`` state line
        (what the session-start render shows); an edit that leaves it
        untouched probably left it stale. ``None`` for non-thread nodes or a
        changed first line.
        """

        def first(text: str | None) -> str:
            return next(
                (ln.strip() for ln in (text or "").splitlines() if ln.strip()), ""
            )

        lead = first(new_body)
        if not lead or lead != first(old_body):
            return None
        tags = self.store.ref_tags_bulk([ref_id]).get(ref_id, [])
        if "section:threads" not in {v for _ns, v in tags}:
            return None
        shown = lead if len(lead) <= 120 else lead[:119].rstrip() + "…"
        anchor = lead if len(lead) <= 120 else lead[:60]
        if new_body.count(anchor) == 1:
            action = (
                f"edit(kind='memory', id={ref_id}, mode='find-replace', "
                f"find={anchor!r}, text='Left: …')"
            )
        else:
            action = f"edit(kind='memory', id={ref_id}, mode='replace', text='…')"
        return (
            f"lead line still reads {shown!r} — still true? update: "
            f"{action} · finished: "
            f"delete(kind='memory', id={ref_id})"
        )

    def _write_retried[T](self, ref_id: int, what: str, fn: Callable[[], T]) -> T:
        """Run one memory write under bounded lock-contention retry.

        Prod roles run ``lock_timeout='5s'``; a burst of edits against one
        node (a memory-graph hygiene pass, a thread node hit by several
        sessions) surfaced as ``Upstream LockNotAvailable`` — 18 lost
        turns in the 2026-10-21 22:00Z hour (gr504486). Each attempt
        re-invokes ``fn`` so it opens a fresh ``store.tx()`` — the contract
        :func:`retry_locked` requires (an aborted transaction can never be
        continued). A retry that reaches the lock but finds the body
        changed by the holder still refuses through ``expected_body`` as
        before — only the lock-wait itself is replayed. When the budget
        runs out the call refuses with a ``next=`` instead of leaking the
        bare psycopg error.
        """
        try:
            return retry_locked(fn, label=f"memory {what} ref_id={ref_id}")
        except (LockNotAvailable, DeadlockDetected) as exc:
            raise Upstream(
                f"memory {ref_id} is still locked by another writer "
                "(lock-retry budget exhausted)",
                next=(
                    f"get(kind='memory', id={ref_id}) to re-read the "
                    "current body, then retry the edit — contention that "
                    "outlasts the retry budget usually means another "
                    "session holds a long write; if it repeats, file a "
                    "gripe naming the id"
                ),
            ) from exc

    def _write_body(
        self,
        ref_id: int,
        new_text: str,
        *,
        new_title: str | None,
        meta_patch: dict[str, Any],
        expected_body: str | None = None,
        misses: list[UngroundedNumber] | None = None,
        reason: str | None = None,
        op: str | None = None,
        deferred_out: list[int] | None = None,
        derived_title_out: list[str] | None = None,
    ) -> str | None:
        """The one body-write path shared by every edit mode.

        One transaction: delete+reinsert the ``memory_body`` chunk (old body
        to ``ref_events``; never an in-place UPDATE, so embeddings
        re-derive), patch meta, optionally retitle, and re-sync the
        auto-mention links to the new prose. Lock the ref before reading
        the previous body so simultaneous handler writes serialize. An
        anchored edit must still match its snapshot; a conflict rolls back
        the replacement and its derived-row cascade. ``misses`` is the
        attribution gate's verdict on ``new_text`` (``None`` = not checked):
        it sets or clears ``AUDIT:ungrounded-number`` in the same
        transaction. Returns the old body.

        Title derivation (gr538501): when no explicit ``new_title`` was
        given and the stored title still reads as the old body's derived
        title — ``title == _derive_title(old_body)``, i.e. it tracks the
        first line — a body write re-derives it from the new prose the
        same way ``put`` does, so a lead-line edit doesn't leave a stale
        header. A hand-set title (one that never was the derivation) is
        left alone; the re-derived value goes to ``derived_title_out``
        for the caller's ack.
        """

        def _do() -> str | None:
            with self.store.tx() as conn:
                row = conn.execute(
                    "SELECT title, retired_at FROM refs "
                    "WHERE ref_id = %s AND kind = 'memory' FOR NO KEY UPDATE",
                    (ref_id,),
                ).fetchone()
                if row is None:
                    raise NotFound(
                        f"memory {ref_id} not found",
                        next="search(kind='memory', q='<topic>') for the live id",
                    )
                if row[1] is not None:
                    raise Gone(
                        f"memory {ref_id} has been deleted",
                        next="search(kind='memory', q='<topic>') for a live "
                        "memory on the topic",
                    )
                old_body = self.store.chunks.replace_body_chunk(
                    ref_id,
                    new_text,
                    chunk_kind=_BODY_KIND,
                    source="agent",
                    reason=reason.strip() if reason and reason.strip() else None,
                    op=op,
                    conn=conn,
                )
                previous = old_body if old_body is not None else (row[0] or "")
                if expected_body is not None and previous != expected_body:
                    raise BadInput(
                        f"memory {ref_id} changed during the edit; nothing written",
                        next=(
                            f"get(kind='memory', id={ref_id}), then retry the "
                            "anchored edit against the current body"
                        ),
                    )
                applied_title = new_title
                if (
                    applied_title is None
                    and old_body is not None
                    and (row[0] or "") == _derive_title(old_body)
                ):
                    derived = _derive_title(new_text)
                    if derived != (row[0] or ""):
                        applied_title = derived
                        if derived_title_out is not None:
                            # ``retry_locked`` can re-invoke this closure —
                            # assign, never append (same rule as
                            # ``deferred_out``).
                            derived_title_out[:] = [derived]
                if meta_patch:
                    self.store.update_ref(ref_id, meta_patch=meta_patch, conn=conn)
                if applied_title is not None:
                    self.store.chunks.set_ref_title(
                        ref_id, applied_title, source="agent", conn=conn
                    )
                if meta_patch or applied_title is not None:
                    self.store.chunks.sync_header_card(ref_id, conn=conn)
                # Re-sync auto-mention links to the rewritten body: drop the
                # old auto links, add the current ones. Hand-added links
                # survive.
                deferred = self._sync_mention_links(
                    ref_id, new_text, conn=conn, replace=True
                )
                if deferred_out is not None:
                    # ``retry_locked`` can re-invoke this closure — assign,
                    # never append, so the sink stays single-valued.
                    deferred_out[:] = [deferred]
                if misses is not None:
                    self._apply_attribution_tag(ref_id, misses, conn=conn)
            return old_body

        return self._write_retried(ref_id, "body edit", _do)

    def _sync_mention_links(
        self,
        ref_id: int,
        text: str,
        *,
        conn: Connection,
        replace: bool = False,
    ) -> int:
        """Keep best-effort resolution, but require a healthy caller transaction.

        The shared helper catches SQL errors as well as resolution failures.
        PostgreSQL rolls back an aborted transaction; psycopg may silently
        discard a closed connection (UNKNOWN). Both callers own an explicit
        transaction, so only INTRANS may reach their commit boundary. This
        check does not guarantee success if the connection fails later.
        """
        added = super()._sync_mention_links(ref_id, text, conn=conn, replace=replace)
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise Internal(
                "memory mention synchronization left an unhealthy write transaction; "
                "write not acknowledged",
                next="get the current memory, then retry the write",
            )
        return added

    def _edit_anchored(
        self,
        *,
        ref_id: int,
        mode: str,
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_mode: str | None,
        meta_patch: dict[str, Any],
        new_title: str | None = None,
        reason: str | None = None,
        delete_span: bool = False,
    ) -> Response:
        """``mode='find-replace'`` / ``'insert'`` / ``'append'`` on the body prose.

        Resolves the edit over the current body with the shared pure
        resolver (same vocabulary and error shapes as the file kinds), then
        writes the result through :meth:`_write_body`. ``'append'`` needs
        no anchor — it tacks ``text=`` onto the end of the body (the file
        kinds' append semantics: one blank line between old tail and new
        text), which is the call agents reach for when appending an update
        to a thread/resume node (gr484228).
        """
        if mode == "append":
            return self._edit_append(
                ref_id=ref_id,
                find=find,
                text=text,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                dry_mode=dry_mode,
                meta_patch=meta_patch,
                new_title=new_title,
                reason=reason,
            )
        if not find:
            # text= alone on the MCP-default mode: refuse rather than guess
            # a whole-body overwrite.
            raise BadInput(
                f"mode={mode!r} requires find= (the exact text to locate)",
                next=(
                    (
                        f"edit(kind='memory', id={ref_id}, mode='insert', "
                        "find='exact text', text='text to insert', where='after')"
                    )
                    if mode == "insert"
                    else "edit(kind='memory', id=N, mode='find-replace', "
                    "find='exact text', text='replacement') — or, to overwrite "
                    "the whole body, edit(kind='memory', id=N, mode='replace', "
                    "text='new body')"
                ),
            )
        op_kind = (
            "delete-span"
            if delete_span
            else "edit"
            if mode == "find-replace"
            else "insert"
        )
        _require_find_and_text(
            op_kind=op_kind,
            kind=self.kind,
            slug=str(ref_id),
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
        )
        assert text is not None
        ref = self._resolve_live_ref(ref_id)
        label = f"{self.kind} {ref.id}"
        # where/match arrive as plain str from the wire; EditOp's
        # __post_init__ validates both against the exact Literal sets and
        # raises BadInput on a bad value, so the casts are sound at runtime.
        op = EditOp(
            op="insert" if op_kind == "insert" else "edit",
            find=find,
            text=text,
            before=before,
            after=after,
            where=cast("Any", where),
            match=cast("Any", match),
            nth=nth,
            region_label=label,
        )
        old_body = self._body_text(ref)
        result = apply_edit(old_body, op)
        new_body = result.new_buffer
        if not new_body.strip():
            raise BadInput(
                f"edit would leave {label} with an empty body",
                next=(
                    f"delete(kind='memory', id={ref.id}) to remove the memory, "
                    "or edit with replacement text"
                ),
            )
        if dry_mode is not None:
            header = render_dry_run_header(
                region_label=label,
                edited_spans=result.edited_spans,
                match_policy=op.match,
            )
            if dry_mode == "full":
                body = render_dry_run_full(
                    new_body, edited_spans=result.edited_spans, region_label=label
                )
            else:
                diff = format_unified_diff(old_body, new_body, file_label=label)
                body = diff.rstrip("\n") or "(no diff - pre and post are identical)"
            if new_title is not None:
                body += f"\n(title would become {new_title!r})"
            return Response(body="\n".join([*header, "", body]))
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        self._write_body(
            ref.id,
            new_body,
            new_title=new_title,
            meta_patch=meta_patch,
            expected_body=old_body,
            misses=misses,
            reason=reason,
            op=op_kind if op_kind == "delete-span" else None,
            deferred_out=deferred,
            derived_title_out=derived_title,
        )
        verb = (
            "edited"
            if op_kind == "edit"
            else "deleted from"
            if op_kind == "delete-span"
            else "inserted into"
        )
        n_spans = len(result.edited_spans)
        ack = (
            f"{verb} body of {self._sense()} id={ref.id} "
            f"({len(old_body.split())} → {len(new_body.split())} words, "
            f"{n_spans} span{'s' if n_spans != 1 else ''}). "
            "view='log' for the full diff."
        )
        if new_title is not None:
            ack += f" title now: {new_title!r}."
            nudge = self._first_line_nudge(new_title)
            if nudge:
                ack += f"\n\nhint: {nudge}"
        elif derived_title:
            ack += f" title re-derived: {derived_title[0]!r}."
        lead = self._thread_lead_hint(ref.id, old_body, new_body)
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
        )

    def _edit_append(
        self,
        *,
        ref_id: int,
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_mode: str | None,
        meta_patch: dict[str, Any],
        new_title: str | None = None,
        reason: str | None = None,
    ) -> Response:
        """``mode='append'`` — tack ``text=`` onto the end of the body.

        No anchor: the append point is the end of the body. The file
        kinds' convention carries over — old tail rstripped, one blank
        line, then the new text rstripped.
        """
        if not text or not text.strip():
            raise BadInput(
                "mode='append' requires text= (the text to tack on)",
                next=(f"edit(kind='memory', id={ref_id}, mode='append', text='...')"),
            )
        anchors = [
            name
            for name, val in (
                ("find", find),
                ("before", before or None),
                ("after", after or None),
                ("where", where),
                ("nth", nth),
                ("match", match if match != "unique" else None),
            )
            if val is not None
        ]
        if anchors:
            raise BadInput(
                f"mode='append' takes no anchors ({'/'.join(anchors)}= given) — "
                "it always writes at the end of the body",
                next=(
                    f"edit(kind='memory', id={ref_id}, mode='append', "
                    "text='...') — or mode='insert' with find= to anchor "
                    "inside the body"
                ),
            )
        ref = self._resolve_live_ref(ref_id)
        label = f"{self.kind} {ref.id}"
        old_body = self._body_text(ref)
        new_body = (
            f"{old_body.rstrip()}\n\n{text.strip()}"
            if old_body.strip()
            else text.strip()
        )
        if dry_mode is not None:
            start = old_body.count("\n") + 1
            end = new_body.count("\n") + 1
            header = render_dry_run_header(
                region_label=label,
                edited_spans=((start, end),),
                match_policy="append",
            )
            if dry_mode == "full":
                body = render_dry_run_full(
                    new_body, edited_spans=((start, end),), region_label=label
                )
            else:
                diff = format_unified_diff(old_body, new_body, file_label=label)
                body = diff.rstrip("\n") or "(no diff - pre and post are identical)"
            return Response(body="\n".join([*header, "", body]))
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        self._write_body(
            ref.id,
            new_body,
            new_title=new_title,
            meta_patch=meta_patch,
            expected_body=old_body,
            misses=misses,
            reason=reason,
            deferred_out=deferred,
            derived_title_out=derived_title,
        )
        ack = (
            f"appended to body of {self._sense()} id={ref.id} "
            f"({len(old_body.split())} → {len(new_body.split())} words). "
            "view='log' for the full diff."
        )
        if new_title is not None:
            ack += f" title now: {new_title!r}."
        elif derived_title:
            ack += f" title re-derived: {derived_title[0]!r}."
        lead = self._thread_lead_hint(ref.id, old_body, new_body)
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
        )

    def _edit_ops(
        self,
        *,
        ref_id: int,
        ops: Any,
        dry_mode: str | None,
        meta_patch: dict[str, Any],
        reason: str | None = None,
    ) -> Response:
        """``ops=[{find, text, ...}]`` — several anchored edits in one call.

        Each dict is one anchored edit with the single-op vocabulary —
        ``find``/``text``/``before``/``after``/``match``/``nth`` plus
        ``'op'`` for the op kind (``'find-replace'`` default, ``'insert'``
        with ``'where'``, ``'append'`` needing only ``text`` — the
        ``ops=[{'op': ...}]`` convention the structure kind already
        uses). Ops resolve in order, each against the previous op's
        result; the final buffer then lands through the single
        :meth:`_write_body` transaction (retried under
        :func:`retry_locked` like every body write). Any bad op refuses
        the whole call with ``ops[i]`` naming the failing index — nothing
        is written, so a batch can't leave a half-applied body.
        """
        if not isinstance(ops, list) or not ops:
            raise BadInput(
                "ops= must be a non-empty list of op dicts",
                next=(
                    "edit(kind='memory', id=N, ops=[{'find': 'old', "
                    "'text': 'new'}, ...])"
                ),
            )
        ref = self._resolve_live_ref(ref_id)
        label = f"{self.kind} {ref.id}"
        old_body = self._body_text(ref)
        new_body = old_body
        spans: list[tuple[int, int]] = []
        for i, raw in enumerate(ops):
            if not isinstance(raw, dict):
                raise BadInput(
                    f"ops[{i}]: each op must be a dict, got {type(raw).__name__}",
                    next="ops=[{'find': 'exact text', 'text': 'replacement'}]",
                )
            unknown = sorted(str(k) for k in raw if k not in _OPS_KEYS)
            if unknown:
                raise BadInput(
                    f"ops[{i}]: unknown key(s) {unknown}; allowed: {sorted(_OPS_KEYS)}",
                    next="ops=[{'find': 'exact text', 'text': 'replacement'}]",
                )
            op_kind = raw.get("op", "find-replace")
            if op_kind not in _OPS_KINDS:
                raise BadInput(
                    f"ops[{i}]: unknown 'op' {op_kind!r}",
                    options=list(_OPS_KINDS),
                    next="drop 'op' (default 'find-replace') or set 'insert'/'append'",
                )
            try:
                if op_kind == "append":
                    new_body, span = self._op_append(raw, new_body)
                else:
                    new_body, span = self._op_anchored(
                        raw, new_body, op_kind=op_kind, label=label
                    )
            except BadInput as exc:
                raise BadInput(
                    f"ops[{i}]: {exc.cause}", next=exc.next, options=exc.options
                ) from exc
            spans.extend(span)
        if not new_body.strip():
            raise BadInput(
                f"ops= would leave {label} with an empty body",
                next=(
                    f"delete(kind='memory', id={ref.id}) to remove the "
                    "memory, or keep some body text"
                ),
            )
        if dry_mode is not None:
            header = render_dry_run_header(
                region_label=label,
                edited_spans=tuple(spans),
                match_policy=f"{len(ops)} ops",
            )
            if dry_mode == "full":
                body = render_dry_run_full(
                    new_body, edited_spans=tuple(spans), region_label=label
                )
            else:
                diff = format_unified_diff(old_body, new_body, file_label=label)
                body = diff.rstrip("\n") or "(no diff - pre and post are identical)"
            return Response(body="\n".join([*header, "", body]))
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        self._write_retried(
            ref.id,
            "body edit",
            lambda: self._write_body(
                ref.id,
                new_body,
                new_title=None,
                meta_patch=meta_patch,
                expected_body=old_body,
                misses=misses,
                reason=reason,
                deferred_out=deferred,
                derived_title_out=derived_title,
            ),
        )
        ack = (
            f"edited body of {self._sense()} id={ref.id} "
            f"({len(old_body.split())} → {len(new_body.split())} words, "
            f"{len(ops)} ops). view='log' for the full diff."
        )
        if derived_title:
            ack += f" title re-derived: {derived_title[0]!r}."
        lead = self._thread_lead_hint(ref.id, old_body, new_body)
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
        )

    @staticmethod
    def _op_append(
        raw: dict[str, Any], buffer: str
    ) -> tuple[str, tuple[tuple[int, int], ...]]:
        """One ``'op': 'append'`` dict — tack ``text`` onto the body end."""
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BadInput(
                "'op': 'append' requires 'text' (the text to tack on)",
                next="edit(kind='memory', id=N, args={'ops': [{'op': 'append', 'text': '...'}]})",
            )
        text = mask_secrets(text)
        anchored = [
            k for k in ("find", "before", "after", "where", "nth") if raw.get(k)
        ] + (["match"] if raw.get("match") not in (None, "unique") else [])
        if anchored:
            raise BadInput(
                f"'op': 'append' takes no anchors ({'/'.join(anchored)} "
                "given) — it always writes at the end of the body",
                next=(
                    "drop the anchors, or use 'op': 'insert' with 'find' and "
                    "'where' to write at a position"
                ),
            )
        new_buffer = (
            f"{buffer.rstrip()}\n\n{text.strip()}" if buffer.strip() else text.strip()
        )
        span = ((buffer.count("\n") + 1, new_buffer.count("\n") + 1),)
        return new_buffer, span

    @staticmethod
    def _op_anchored(
        raw: dict[str, Any],
        buffer: str,
        *,
        op_kind: str,
        label: str,
    ) -> tuple[str, tuple[tuple[int, int], ...]]:
        """One ``'find-replace'``/``'insert'`` dict through ``apply_edit``."""
        find = raw.get("find")
        if not isinstance(find, str) or not find:
            raise BadInput(
                f"'op': {op_kind!r} requires 'find' (the exact text to locate)",
                next=(
                    "edit(kind='memory', id=N, args={'ops': [{'op': 'find-replace', "
                    "'find': 'old text', 'text': 'new text'}]})"
                ),
            )
        text = raw.get("text")
        if not isinstance(text, str):
            raise BadInput(
                f"'op': {op_kind!r} requires 'text' — pass 'text': '' to "
                "delete the matched span",
                next=(
                    "edit(kind='memory', id=N, args={'ops': [{'op': 'find-replace', "
                    "'find': 'old text', 'text': 'new text'}]})"
                ),
            )
        before = raw.get("before", "")
        after = raw.get("after", "")
        where = raw.get("where")
        match = raw.get("match", "unique")
        nth = raw.get("nth")
        for key, val in (
            ("before", before),
            ("after", after),
            ("where", where),
            ("match", match),
        ):
            if val is not None and not isinstance(val, str):
                raise BadInput(
                    f"'{key}' must be a string",
                    next=f"pass '{key}' as a string, or leave it out",
                )
        if nth is not None and not isinstance(nth, int):
            raise BadInput(
                "'nth' must be an int",
                next="pass 'nth' as an int with 'match': 'nth', or leave it out",
            )
        op = EditOp(
            op="edit" if op_kind == "find-replace" else "insert",
            find=mask_secrets(find),
            text=mask_secrets(text),
            before=mask_secrets(cast(str, before)),
            after=mask_secrets(cast(str, after)),
            where=cast("Any", mask_secrets(where) if isinstance(where, str) else where),
            match=cast("Any", match or "unique"),
            nth=nth,
            region_label=label,
        )
        result = apply_edit(buffer, op)
        return result.new_buffer, result.edited_spans

    # ── tag: refuse author add/remove of the system-set STALE: axis ──

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove tags — same as the base, except ``STALE:`` is refused.

        ``STALE:retracted-premise`` is a system-set,
        derived flag: the retraction-ripple hook recomputes and
        sets/clears it on every retraction-edge add or remove. An author
        `tag(add=['STALE:...'])` or `tag(remove=['STALE:...'])` would fight
        that recompute (the next ripple silently overwrites it anyway), so
        reject at the write boundary with a pointer at the real mechanism —
        mirrors how ``SRC:`` / ``CACHE:`` / ``DENSITY:`` are system-only
        elsewhere in the corpus.
        """
        _refuse_system_dream(add, verb="tag")
        for t in (*(add or []), *(remove or [])):
            if isinstance(t, str) and t.split(":", 1)[0] == "STALE":
                raise BadInput(
                    f"tag(kind='memory') cannot add/remove {t!r} — "
                    "STALE: is system-set, not author-writable",
                    next=(
                        "STALE:retracted-premise is derived and recomputed "
                        "automatically by the retraction-ripple hook "
                        " whenever a retracts/raises-concern-about "
                        "edge is added or removed — see get(kind='memory', "
                        "id=N, view='argument') to read it, not tag() to set it"
                    ),
                )
        return super().tag(id=id, add=add, remove=remove, **_kw)

    # ── read: title header + body chunk ─────────────────────────────

    def _body_text(self, ref: Ref) -> str:
        """The memory's body prose from its ``memory_body`` chunk.

        Falls back to ``refs.title`` for any pre-0050 row that never got a
        body chunk (defensive — the migration backfilled every live memory).
        """
        for block in self.store.chunks.list_chunks_for_ref(ref.id):
            if block.chunk_kind == _BODY_KIND:
                return block.text
        return ref.title or ""

    def _hit_text(self, ref: Ref, block: Any) -> str:
        """Search-hit text: the body prose, even when the best-ranked chunk
        was the title+hook header card (so a card hit still shows a snippet)."""
        if getattr(block, "chunk_kind", None) == "card_combined":
            return self._body_text(ref)
        return str(block.text or "")

    def _strip_title_echo(self, body: str, title: str) -> str:
        """Drop the body's leading line when it only repeats ``refs.title``.

        ``_derive_title`` copies the first non-empty body line into the
        title, so digest-style bodies (``# Heading`` opener) and
        single-line memories would otherwise print the same text twice —
        once in the ``# memory <id>: <title>`` header, once as the body's
        first line (gr504805). The stored chunks are untouched; this is
        display-only.
        """
        if not title:
            return body
        lines = body.split("\n")
        first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        if first is None or lines[first].strip() != title.strip():
            return body
        rest = lines[first + 1 :]
        if rest and not rest[0].strip():
            rest = rest[1:]
        return "\n".join(rest)

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        """Single-ref view: ``# memory <id>: <title>`` + body + tag line."""
        title = ref.title or ""
        header = f"# {self._sense()} {ref.id}"
        if title:
            header += f": {title}"
        out = [header]
        body = self._strip_title_echo(self._body_text(ref), title)
        if body:
            out += ["", body]
        # meta.rule / meta.warrant — a kind:inference memory's
        # reasoning-step label + free-text justification, rendered right
        # after the body so a proof-tree reader sees the operator + the
        # "why" without a separate call.
        meta = ref.meta or {}
        rule = meta.get("rule")
        warrant = meta.get("warrant")
        if rule or warrant:
            out.append("")
            if rule:
                out.append(f"rule: {rule}")
            if warrant:
                out.append(f"warrant: {warrant}")
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
            if any(str(t) == str(_AUDIT_UNGROUNDED) for t in tags):
                out.append(
                    f"{_AUDIT_UNGROUNDED}: a number next to a citation is not "
                    "in the cited text; check it before trusting it"
                )
        return "\n".join(out)

    def _render_hits_table(self, refs: list[Ref]) -> str:
        """List view: ``refs.title`` is now a real title, so show it verbatim
        as the summary and count the body words hidden behind the id.

        Body-word counts come from one batched query over the ``memory_body``
        chunks (no N+1) rather than the base class's first-line split.
        """

        from precis.format import render_agent_table
        from precis.utils import handle_registry

        if not refs:
            return ""
        ref_ids = [r.id for r in refs]
        link_counts = self.store.count_links_for_refs(ref_ids)
        body_words = self.store.chunks.chunk_word_counts(ref_ids, chunk_kind=_BODY_KIND)
        sizes = self.store.chunks.body_byte_counts(ref_ids)
        now = datetime.now(UTC)
        rows: list[dict[str, str]] = []
        for r in refs:
            age_days = max(0, (now - r.updated_at).days)
            rows.append(
                {
                    "kind": self.kind,
                    "id": handle_registry.try_format(self.kind, r.id) or str(r.id),
                    "summary": r.title or "",
                    "body_words": str(body_words.get(r.id, 0)),
                    "links": str(link_counts.get(r.id, 0)),
                    "age": str(age_days),
                    "size": size_cell(sizes.get(r.id, 0)),
                }
            )
        schema = ["kind", "id", "summary", "body_words", "links", "age", "size"]
        return render_agent_table(rows, schema=schema)

    # ── search: body-chunk search + cross-kind hits come from the base
    # (NumericRefHandler._search_body_chunks / _body_search_hits) via the
    # search_body_chunks opt-in above. ``view='index'`` is the one addition:
    # the hit *is* the session-start index bullet. ──────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        page: int = 1,
        mode: str | None = None,
        view: str | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        scope: str | None = None,
        **kw: Any,
    ) -> Response:
        """Memory search. An unscoped query whose hits cluster in one hub or
        ``section:<type>`` gets a one-line narrowing hint appended
        (:func:`~precis.utils.memory_hubs.narrowing_hints`; ``under=`` itself
        is the dispatcher's generic part-of scope). The parameters are spelled
        out because dispatch routes by this signature."""
        resp = self._search_inner(
            q=q,
            tags=tags,
            page_size=page_size,
            page=page,
            mode=mode,
            view=view,
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
            scope=scope,
            **kw,
        )
        if not (q and q.strip()):
            return resp
        try:
            ids = [
                int(m)
                for m in re.findall(r"(?:^## memory |\bme)(\d+)\b", resp.body, re.M)
            ]
            hints = narrowing_hints(self.store, ids, q)
        except Exception:
            return resp
        if not hints:
            return resp
        return replace(resp, body=resp.body.rstrip() + "\n\n" + "\n".join(hints))

    def _search_inner(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        page: int = 1,
        mode: str | None = None,
        view: str | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        scope: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Body-chunk search; ``view='index'`` renders hits as index bullets.

        ``scope=`` picks where ``q`` matches: ``'title'`` = only the header
        card (title + hook), ``'body'`` = only the body prose, omitted =
        both. Anything else is refused.

        ``view='index'`` returns one ``- <Title> (me<id>[, <filename>]) —
        <hook>`` line per best-ranked memory (the session-start index shape;
        ``<filename>`` is ``meta.file_mirror.filename`` when mirrored,
        ``<hook>`` is ``meta.hook`` else the body's first line). Any other
        ``view`` is refused; ``None`` is the unchanged default render.
        """
        # ``q='*'`` is the match-all spelling (gr538071) — fold before
        # the index view's tag-filtered listing path runs; the
        # count/default path normalizes the same way inside
        # super().search.
        q = fold_match_all_q(q)
        chunk_scope = scope.strip().lower() if isinstance(scope, str) else None
        if chunk_scope == "":
            chunk_scope = None
        if chunk_scope not in (None, "title", "body"):
            raise BadInput(
                f"unknown scope {scope!r} for search(kind='memory')",
                options=["title", "body"],
                next="search(kind='memory', q='<words>', scope='title')",
            )
        if view is None or view == "count":
            return super().search(
                q=q,
                tags=tags,
                page_size=page_size,
                page=page,
                mode=mode,
                view=view,
                exclude_ref_ids=exclude_ref_ids,
                include_ref_ids=include_ref_ids,
                chunk_scope=chunk_scope,
                **_kw,
            )
        if view != "index":
            raise BadInput(
                f"unknown view {view!r} for search(kind='memory')",
                options=["index", "count"],
                next=(
                    "search(kind='memory', q='<task>', tags=['SPACE:repo-dev'], "
                    "view='index')"
                ),
            )
        if not (q and q.strip()):
            return self._index_listing(
                tags, page_size, page, exclude_ref_ids, include_ref_ids
            )
        from precis.cli.memory import bullet_line

        normalized = Tag.normalize_filter(tags, kind=self.kind)
        # gr514956: the index view is the no-human-reads recall path
        # (session-start recall and the ``score_recall`` weekly scorer
        # both rank off this render) — the kind's fold distance applies
        # as a hard cut, with no hint line and no cursor tail.
        hits, _total, _fold_info = self._best_body_hits(
            q,
            normalized,
            page_size,
            page=page,
            mode=mode,
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
            chunk_scope=chunk_scope,
            hard_cut=True,
        )
        if not hits:
            tag_suffix = f" tagged {normalized}" if normalized else ""
            return Response(body=f"no {self._sense()} entries match {q!r}{tag_suffix}")
        self.store.chunks.bump_salience(
            self.store.chunks.card_chunk_ids([ref.id for _, ref, _ in hits])
        )
        lines = []
        for _block, ref, _rank in hits:
            body_text = self._body_text(ref)
            first = next(
                (ln.strip() for ln in body_text.splitlines() if ln.strip()),
                "",
            )
            lines.append(
                bullet_line(
                    ref,
                    cut_hooks=False,
                    filename=True,
                    fallback_hook=first,
                    size=size_cell(len(body_text.encode("utf-8"))),
                )
            )
        return Response(body="\n".join(lines))

    def _index_listing(
        self,
        tags: list[str] | None,
        page_size: int,
        page: int,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
    ) -> Response:
        """``view='index'`` without ``q=``: the tag-filtered memories as index
        bullets, most recently touched first (``GREATEST(last_viewed_at,
        last_recalled_at, updated_at)``)."""

        from precis.cli.memory import bullet_line
        from precis.utils.memory_hubs import recency_stamps

        normalized = Tag.normalize_filter(tags, kind=self.kind)
        refs = self.store.list_refs(kind=self.kind, tags=normalized, limit=5000)
        if include_ref_ids is not None:
            keep = set(include_ref_ids)
            refs = [r for r in refs if r.id in keep]
        if exclude_ref_ids:
            drop = set(exclude_ref_ids)
            refs = [r for r in refs if r.id not in drop]
        if not refs:
            tag_suffix = f" tagged {normalized}" if normalized else ""
            return Response(body=f"no {self._sense()} entries{tag_suffix}")
        stamps = recency_stamps(self.store, [r.id for r in refs])
        oldest = datetime.min.replace(tzinfo=UTC)
        refs = sorted(refs, key=lambda r: stamps.get(r.id) or oldest, reverse=True)
        start = (max(page, 1) - 1) * page_size
        lines = []
        for ref in refs[start : start + page_size]:
            body_text = self._body_text(ref)
            first = next(
                (ln.strip() for ln in body_text.splitlines() if ln.strip()),
                "",
            )
            lines.append(
                bullet_line(
                    ref,
                    cut_hooks=False,
                    filename=True,
                    fallback_hook=first,
                    size=size_cell(len(body_text.encode("utf-8"))),
                )
            )
        if not lines:
            return Response(body=f"no {self._sense()} entries on page {page}")
        return Response(body="\n".join(lines))

    # ── delete: soft-delete + edge cleanup (gr503031) ───────────────

    def _delete(self, id: str | int | None, *, reason: str | None = None) -> Response:
        """Soft-delete a memory and drop its link edges in one transaction.

        The base ``retire_ref`` leaves the ref's inbound and outbound
        links pointing at a tombstone — ``fisheye`` renders them as dead
        ends and ``memory-lint`` counts them (2026-10-21: 17 unlinked by
        hand). When ``meta.superseded_by`` names a live memory the edges
        are retargeted onto it instead (``migrate_links``, the same move
        ``supersede`` makes); otherwise they're deleted outright. The ack
        reports what happened to the edges.
        """
        if id is None:
            raise BadInput(
                "delete requires id=",
                next=f"delete(kind={self.kind!r}, id=N)",
            )
        ref_id = self._coerce_id(id)
        ref = self._resolve_ref_for_delete(ref_id)
        successor: int | None = None
        raw_sup = (ref.meta or {}).get("superseded_by")
        if raw_sup is not None:
            try:
                cand = int(raw_sup)
            except (TypeError, ValueError):
                cand = -1
            if (
                cand != ref_id
                and self.store.get_ref(kind="memory", id=cand) is not None
            ):
                successor = cand
        if successor is None and reason:
            # The gardening idiom ``delete(reason='merged into meNNN')``:
            # the named handle IS the survivor — stamp the canonical
            # ``meta.superseded_by`` so edges migrate and the tombstone's
            # Gone points at it (gr503546).
            named = self._reason_named_ref({"reason": reason})
            if (
                named is not None
                and named != ref_id
                and self.store.get_ref(kind="memory", id=named) is not None
            ):
                successor = named
        with self.store.tx() as conn:
            if successor is not None and raw_sup is None:
                self.store.stamp_ref_meta(
                    ref_id, {"superseded_by": successor}, conn=conn
                )
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE dst_ref_id = %s"
                "   AND relation <> 'supersedes')::int,"
                "       count(*) FILTER (WHERE src_ref_id = %s)::int,"
                "       count(*) FILTER (WHERE dst_ref_id = %s"
                "   AND relation = 'supersedes')::int"
                "  FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
                (ref_id, ref_id, ref_id, ref_id, ref_id),
            ).fetchone()
            n_in, n_out, n_sup = (
                (int(counts[0]), int(counts[1]), int(counts[2]))
                if counts
                else (0, 0, 0)
            )
            if successor is not None:
                moved = self.store.migrate_links(ref_id, successor, conn=conn)
            else:
                # Inbound ``supersedes`` edges stay: "new supersedes old"
                # IS the tombstone trail (dangling_links skips them by
                # design); the Gone reads it back (gr503546).
                cur = conn.execute(
                    "DELETE FROM links WHERE (src_ref_id = %s OR dst_ref_id = %s)"
                    " AND NOT (dst_ref_id = %s AND relation = 'supersedes')",
                    (ref_id, ref_id, ref_id),
                )
                removed = int(cur.rowcount or 0)
            self._record_delete_reason(ref_id, reason, conn=conn)
            self.store.retire_ref(ref_id, conn=conn)
        if successor is not None:
            detail = (
                f"{moved} link(s) migrated to memory id={successor} "
                f"(meta.superseded_by)"
            )
        else:
            kept = f"; {n_sup} supersedes edge(s) kept" if n_sup else ""
            detail = (
                f"{n_in} inbound + {n_out} outbound link(s) removed{kept}"
                if (n_in or n_out or removed or n_sup)
                else "no links to remove"
            )
        return Response(body=f"deleted {self._sense()} id={ref_id} — {detail}")

    # ── supersede: the one guarded destructive verb (dreaming) ──────

    def supersede(
        self,
        *,
        merge_ids: list[Any] | None = None,
        new_text: str | None = None,
        new_title: str | None = None,
        new_tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Consolidate >=2 near-duplicate memories into one survivor.

        The single guarded compress-only merge a dream uses instead of raw
        ``delete`` (docs/backlog/dreaming.md, §Consolidate). In one
        transaction: mint a new ``memory`` (title + ``memory_body`` chunk +
        merged tags), migrate every link off each original onto the survivor,
        add ``survivor --supersedes--> original`` edges, stamp
        ``meta.superseded_by`` and soft-delete the originals.

        Hard guards (enforced here, never the prompt):

        - ``merge_ids``: 2..10 *distinct* live ``memory`` ids. Papers (or any
          non-memory kind) are refused — papers are never merged or deleted.
        - ``new_text``: required, and **compress-only** — no longer than the
          combined original *bodies* (a merge may forget a nuance, never
          invent a claim).
        - ``new_title``: optional header; derived from ``new_text`` when
          omitted.

        A bad call raises a typed ``BadInput`` the agent can read and retry;
        it can never corrupt or hard-delete.
        """
        if not merge_ids or not isinstance(merge_ids, list):
            raise BadInput(
                "supersede requires merge_ids=[id, id, ...] (>= 2 memory ids)",
                next="supersede(merge_ids=[12, 47], new_text='merged wording')",
            )
        # Coerce + dedup preserving order; a repeated id is a mistake,
        # not a 2-way merge.
        seen_ids: set[int] = set()
        ids: list[int] = []
        for raw in merge_ids:
            mid = self._coerce_id(raw)
            if mid not in seen_ids:
                seen_ids.add(mid)
                ids.append(mid)
        if len(ids) < 2:
            raise BadInput(
                f"supersede needs >= 2 distinct memory ids, got {len(ids)}",
                next="pick two or more different memories to merge",
            )
        if len(ids) > _SUPERSEDE_MAX_MERGE:
            raise BadInput(
                f"supersede caps at {_SUPERSEDE_MAX_MERGE} memories per merge, "
                f"got {len(ids)}",
                next="split into smaller, reviewable merges",
            )
        if new_text is None or not new_text.strip():
            raise BadInput(
                "supersede requires new_text= (the consolidated memory)",
                next="supersede(merge_ids=[...], new_text='the merged wording')",
            )

        new_text, new_title = mask_secrets(new_text), mask_secrets(new_title)

        # Every id must resolve to a *live memory*. get_ref(kind='memory')
        # returns None for a wrong kind, a missing id, or a soft-deleted
        # row — all three are caller errors here.
        originals = []
        for mid in ids:
            ref = self.store.get_ref(kind="memory", id=mid)
            if ref is None:
                raise BadInput(
                    f"supersede: id={mid} is not a live memory "
                    "(wrong kind, missing, or already deleted)",
                    next=f"get(kind='memory', id={mid}) to check",
                )
            originals.append(ref)

        # Compress-only: the survivor body may not be longer than the sum of
        # the originals' bodies it absorbs. Forgetting a nuance is the
        # accepted loss; inventing new claims is not, and length is the cheap
        # proxy the tool can enforce.
        combined_len = sum(len(self._body_text(r)) for r in originals)
        if len(new_text) > combined_len:
            raise BadInput(
                f"supersede is compress-only: new_text ({len(new_text)} chars) "
                f"exceeds the combined originals ({combined_len} chars)",
                next="shorten new_text — a merge compresses, it never expands",
            )

        # Resolve the tag set before touching the DB so a bad explicit tag
        # fails before any write. Default = union of the originals' OPEN tags
        # (control/closed tags like STATUS:/DREAM: are dropped); the survivor
        # always carries DREAM:consolidated.
        if new_tags is not None:
            _refuse_system_dream(new_tags, verb="supersede")
            tag_objs = [Tag.parse_strict(t, kind="memory") for t in new_tags]
        else:
            tag_objs = []
            seen_tags: set[str] = set()
            for r in originals:
                for t in self.store.tags_for(r.id):
                    if t.namespace != "open":
                        continue
                    key = str(t)
                    if key not in seen_tags:
                        seen_tags.add(key)
                        tag_objs.append(t)
        if not any(str(t) == str(_DREAM_CONSOLIDATED) for t in tag_objs):
            tag_objs.append(_DREAM_CONSOLIDATED)
        # SPACE: is closed, so the open-tag union above drops it. The
        # survivor keeps the originals' space; merging across spaces would
        # leak one partition into another, so it is refused.
        if not any(t.namespace == "closed" and t.prefix == "SPACE" for t in tag_objs):
            spaces = {
                str(t)
                for r in originals
                for t in self.store.tags_for(r.id)
                if t.namespace == "closed" and t.prefix == "SPACE"
            }
            if len(spaces) > 1:
                raise BadInput(
                    f"supersede across spaces {sorted(spaces)} — merge memories "
                    "of one SPACE: only",
                    next="pass new_tags=['SPACE:<one>', …] to choose explicitly",
                )
            space = spaces.pop() if spaces else self.default_tags_on_create[0]
            tag_objs.append(Tag.parse_strict(space, kind="memory"))

        title = (
            new_title.strip()
            if isinstance(new_title, str) and new_title.strip()
            else _derive_title(new_text)
        )
        misses = self._attribution_misses(new_text)
        with self.store.tx() as conn:
            survivor = self.store.insert_ref(
                kind="memory",
                slug=None,
                title=title,
                meta={"superseded": ids},
                conn=conn,
            )
            self.store.chunks.insert_chunks(
                survivor.id,
                [ChunkInsert(ord=0, text=new_text, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
            self.store.chunks.sync_header_card(survivor.id, conn=conn)
            for tag in tag_objs:
                self.store.add_tag(
                    survivor.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if misses:
                self._apply_attribution_tag(survivor.id, misses, conn=conn)
            for mid in ids:
                self.store.migrate_links(mid, survivor.id, conn=conn)
                self.store.add_link(
                    src_ref_id=survivor.id,
                    dst_ref_id=mid,
                    relation="supersedes",
                    set_by="agent",
                    conn=conn,
                )
                self.store.stamp_ref_meta(
                    mid, {"superseded_by": survivor.id}, conn=conn
                )
                self.store.retire_ref(mid, conn=conn)

        merged = ", ".join(str(m) for m in ids)
        return self._with_attribution_advisory(
            Response(
                body=(
                    f"superseded memories [{merged}] → new memory id={survivor.id} "
                    f"(originals soft-deleted, links migrated, tagged "
                    f"{_DREAM_CONSOLIDATED})"
                )
            ),
            misses,
        )


def _roundtrip_hint(handler: Any, ref_id: int) -> list[tuple[str, str]]:
    """The create-ack recall check (gr520279): ``view='index'`` recall
    applies the hard :data:`SEMANTIC_ONLY_FOLD_DISTANCE` cut, so a memory
    whose title/hook lacks the task words silently never comes back. One
    prefilled line telling the writer to verify — in every space, not
    just hub-shaped ones. Never raises, like ``link_hints``."""
    try:
        ref = handler.store.get_ref(kind="memory", id=ref_id)
        title = " ".join(str(ref.title or "").split())[:80] if ref else ""
        if not title:
            return []
        from precis.store._mappers import SEMANTIC_ONLY_FOLD_DISTANCE
        from precis.utils import handle_registry

        me = handle_registry.try_format("memory", ref_id) or f"me{ref_id}"
        cut = SEMANTIC_ONLY_FOLD_DISTANCE.get("memory", 0.42)
        return [
            (
                f"search(kind='memory', q={title!r}, view='index')",
                f"round-trip: if {me} is not listed, the {cut} recall "
                "cut hides it — put the task words in title=/meta.hook",
            )
        ]
    except Exception:
        log.debug("memory round-trip hint skipped for %s", ref_id, exc_info=True)
        return []


def _derive_title(body: str, *, max_len: int = _TITLE_MAX) -> str:
    """Derive a short header title from body prose.

    The first non-empty line (the existing first-line-is-summary
    discipline), capped at ``max_len`` chars with an ellipsis. The graceful
    default when a writer doesn't emit an explicit ``title=`` — matches the
    migration-0050 backfill so old and new memories read the same.
    """
    first = ""
    for line in (body or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        first = (body or "").strip()
    if len(first) > max_len:
        return first[: max_len - 1].rstrip() + "…"
    return first
