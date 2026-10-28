"""GripeHandler — the project's bug tracker.

Numeric-id ref kind, first-class as of migration 0005. File a
complaint, find existing ones, comment to add context, hand off
to a ``fix_gripe`` job for an agent to prepare a candidate branch,
retire via ``delete``.

Surface (see ``precis-gripe-help``):

- ``put(kind='gripe', text=...)`` creates a new gripe with a
  ``gripe_body`` chunk and ``STATUS:open`` tag. The create path also
  runs the recurring-theme registry (:mod:`._gripe_themes`): a filing
  matching exactly one theme folds into that theme's live
  ``theme:<slug>`` keeper as a ``+1`` comment — or becomes the keeper
  itself when none exists — so filers never have to dedup first.
  ``args={'new': True}`` bypasses matching; several matches file
  normally (the ack lists them). ``args={'observation': True}`` files
  the gripe already closed (``STATUS:done`` + ``observation`` tag) via
  the ordinary resolved-comment + tag path.
- ``put(kind='gripe', id=N, text=...)`` appends a ``gripe_comment``
  chunk to the existing gripe (id-present routes to append; same
  verb, no separate ``comment=`` field). ``link=``/``rel=`` and
  ``tags=``/``untags=`` ride along in the same transaction, so
  comment-then-close is one call.
- ``get(kind='gripe', id=N)`` is the title card plus the body
  (gr499040's card, gr514351's body): header + tags + ``title:`` +
  a comment-count line + the newest-comment gist + a ``refs in text:``
  line naming the universal handles the body quotes (gr514629 — an
  intake reader needs to know ``get(id='<handle>')`` reads a ``pc<N>``
  chunk's text or a ``jo<N>`` job's card, no kind= needed), then the
  body —
  head+tail-clipped at :data:`_BODY_HEAD_CHARS`, the marker naming
  ``view='body'`` for the uncapped read. The ruling's measured cost
  was the *timeline* (no pagination then), which stays behind
  ``view='comments'``; the selectors ``args={'comments': K}`` (the
  newest K), ``args={'comments': 'A-B'}`` (an ord window — gr515093)
  and ``args={'comment': K}`` (one comment uncapped) work inside
  ``view='comments'`` and ``view='full'``. Both views page whole
  comments — never a mid-text cut — under
  :data:`_comment_timeline.COMMENT_PAGE_CHARS`; what doesn't fit names
  the ``args={'comments': 'A-B'}`` call that reaches it in one fetch
  (gr514350: ``view='full'`` clips the body at :data:`_BODY_HEAD_CHARS`
  naming ``view='body'`` and elides the remaining comments by ord, all
  under :data:`_FULL_VIEW_CHARS`, so nothing needs a cursor to reach).
  ``log`` stays audit events. ``delete(id=N, args={'comment': K})`` hides one.
- ``search`` queries chunks (body + comments) and groups hits by
  gripe, so a search term that only appears in a comment surfaces
  the parent gripe — overriding ``NumericRefHandler.search`` which
  only indexes ``ref.title``. (Fix for the gap surfaced during the
  0005 e2e: the plan said comments would be searchable for free
  via the chunk substrate, but the verb itself never reached the
  chunk table.)
- Queryless browse (``get(id='/open')``, ``search(tags=['STATUS:open'])``)
  sorts by PRIO, oldest first within a PRIO, under an
  ``N open · M older than 3d · oldest grX (D days)`` line
  (:func:`age_summary`, also the session-start digest line).
- ``tag`` / ``link`` / ``delete`` behave like every other
  first-class numeric ref kind via :class:`NumericRefHandler`.
- The gripe-fix loop's working state lives on the gripe (gr519361,
  :mod:`._gripe_fix`): ``edit(id=N|[ids], mode='fix', args={...})`` sets
  the batch tag, host/session/tree/landed in ``meta.fix`` and appends
  brief / handoff / review chunks; the card shows a ``fix:`` line,
  ``view='fix'`` the texts, ``get(id='/queue')`` the derived queues
  (filter by queue/host/batch) and ``get(id='/batch')`` the census or
  one batch's dossier. A gripe nobody picked up for 14d (no ``meta.fix``,
  STATUS never past ``triaged``, no live job/todo link) is marked stale:
  a ``stale: unpicked Nd`` card line, an ``unpicked Nd`` column in lists
  and search hits, and an ``N unpicked ≥14d`` clause in the age summary
  (gr520909; derived per read, pickup clears it).
"""

from __future__ import annotations

import difflib
import logging
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from precis.errors import BadInput, Denied, Gone, NotFound, Unsupported
from precis.handlers import _comment_timeline as tl
from precis.handlers import _gripe_fix as gf
from precis.handlers._gripe_themes import THEME_TAG_PREFIX, match_themes
from precis.handlers._interpret import interpreted
from precis.handlers._mode_help import require_mode
from precis.handlers._numeric_ref import (
    _BASE_VIEWS,
    NumericRefHandler,
    plain_listing_view,
)
from precis.handlers._prio_tag import (
    PRIO_TAG_TO_INT,
    alias_prio_tags,
    split_prio,
    validate_prio,
)
from precis.handlers._request_state import RequestState
from precis.handlers._status_tag import alias_status_tags
from precis.protocol import ArgsSpec, KindSpec
from precis.response import Response
from precis.store import Store, Tag
from precis.store.types import ChunkInsert, Ref
from precis.utils.secret_scan import mask_secrets

# Chunk-kind slugs we own. Match the seed in 0005.
log = logging.getLogger(__name__)

_BODY_KIND = "gripe_body"
_RESOLVED_PREFIX = "resolved:"
_TERMINAL_STATUS_TAGS = frozenset({"STATUS:done", "STATUS:wontfix"})
_COMMENT_KIND = "gripe_comment"

#: ``get`` view spellings with one reading on this kind (gr494831 step 2;
#: prod 2026-10-15..17 sent view='full' x6, 'default' x1). ``'full'`` is a
#: first-class view since gr499040 (the complete read — body + every
#: comment); ``None`` is the default title-card read. ``'abstract'`` is
#: the paper/patent spelling generalised onto gripes (gr503549) — it
#: reads as ``'body'``; ``'tags'`` reads as the title card, which
#: already is that. Applied to a single-id get without q= only; the
#: reply names it.
_VIEW_ALIASES: dict[str, str | None] = {
    "default": None,
    "abstract": "body",
    "tags": None,
}

#: ``title:`` line cap on the bare get (gr499040's title card) so a
#: gripe filed with a long first line can't re-inflate the small read.
_TITLE_CARD_CHARS = 240

#: Bare-get body budget (gr514351): the body belongs to the default
#: read — a gripe's point is its text, and fetching it cost a second
#: call on every single get. Past the budget the head and tail render
#: around a cut marker naming ``view='body'`` (the uncapped read), so a
#: pasted-log body can't re-inflate the card either.
_BODY_HEAD_CHARS = 1500

#: ``view='full'`` total budget (gr514350): the complete read stays
#: inside the agent-context budget (~4 KB), so no continuation is needed —
#: the body clips at
#: :data:`_BODY_HEAD_CHARS` naming ``view='body'``, and the timeline
#: renders the whole comments that fit the remainder, then names the
#: ``args={'comments': 'A-B'}`` window holding the rest.
_FULL_VIEW_CHARS = 3500

#: Below this much leftover inside :data:`_FULL_VIEW_CHARS`, a comments
#: section would squeeze in a fraction of one comment — 'full' lists
#: the windowed read for all of them instead (never a partial comment).
_MIN_COMMENT_PAGE = 400

#: Open-view age cut: a gripe filed before this is "older than 3d" in the
#: list summary (2026-10-18 audit: the newest-20 page hid the 44 oldest).
_STALE_AFTER = timedelta(days=3)


#: A create-time ``prio=`` plumbed from ``put`` into ``_create`` (which the
#: base class calls with a fixed arg set). Request-scoped, not an instance
#: attribute: one handler instance serves concurrent calls.
_PUT_PRIO: RequestState[int | None] = RequestState("precis_gripe_put_prio", None)

#: The ``agent:<name>`` label grammar ``file_gripe_readonly`` validates
#: (migration 0244) — the source derivation below stays inside it so a
#: weird env value can't turn a gripe filing into a refused call.
_SOURCE_LABEL = re.compile(r"[a-z0-9_.:-]{1,48}")

#: Per-process run label, minted once at import (``agent-<8 hex>`` —
#: inside the SQL label grammar by construction). The fallback when no
#: agentlog run id is in env. ``PRECIS_PROCESS`` is deliberately NOT the
#: fallback: it names the process *type*, shared by every run a
#: long-lived serve handles, which would collapse them into one 3/hour
#: ``agent:`` bucket (gr516603 review). A per-process mint keeps each
#: spawned-serve run in its own bucket — the spawned ``precis serve``
#: lives for one run — and the 20/hour ``source:agent_ro`` cap still
#: bounds the total however the labels slice themselves.
_PROCESS_RUN_LABEL = f"agent-{uuid.uuid4().hex[:8]}"


def _ro_gripe_source() -> str:
    """The ``agent:<name>`` label a read-only run's gripe is stamped with.

    The agentlog run id when present (``run-<id>`` — per-run unique,
    which is what makes the 3-per-run SQL cap actually per-run), else
    this process's minted :data:`_PROCESS_RUN_LABEL` (per-serve ≈
    per-run). An env value outside the label grammar is skipped rather
    than truncated into a lie; the minted label is grammar-safe by
    construction, so a fallback can't fail the SQL check.
    """
    import os

    from precis import agentlog

    run = os.environ.get(agentlog.ENV_VAR, "").strip()
    label = f"run-{run}" if run else ""
    if label and _SOURCE_LABEL.fullmatch(label):
        return label
    return _PROCESS_RUN_LABEL


def _filing_refusal(exc: BaseException) -> Denied | BadInput | None:
    """Map a ``file_gripe_readonly`` refusal to a typed error with a next
    step — the FRICTION-block fallback (gr516603, mcp-authoring rule 2).

    ``None`` for anything that isn't one of its documented refusals (the
    caller re-raises; a source-label rejection means our own derivation
    produced garbage — a server bug, not caller misuse)."""
    msg = str(exc)
    if "file_gripe_readonly:" not in msg:
        return None
    cause = msg.split("file_gripe_readonly: ", 1)[1].splitlines()[0]
    fallback = (
        "end the run's output with a FRICTION block instead "
        "(at most 3 — 'FRICTION' / 'title: <one line>' / "
        "'body: <what blocked you>' / 'END')"
    )
    if "exceeds the 4000-char limit" in cause:
        return BadInput(
            f"a read-only gripe is capped at 4000 chars ({cause})",
            next=f"shorten it and retry put(kind='gripe', text='...'), or {fallback}",
        )
    if "run cap" in cause or "hourly cap" in cause:
        return Denied(cause, next=fallback)
    return None


#: Universal-handle tokens (``pc5155615``, ``jo512227``) inside a gripe's
#: text — the ``refs in text:`` card line names them. Case-insensitive
#: (``PC5155615``); ``handle_registry.normalize`` folds the prefix.
_HANDLE_TOKEN_RE = re.compile(r"\b([a-z]{2}\d+)\b", re.IGNORECASE)

#: Cap on handles the card line lists — a log-paste gripe can quote dozens.
_REF_LINE_CAP = 12


def _referenced_handles(text: str) -> list[str]:
    """Universal handles a gripe's text quotes, first-appearance order.

    ``handle_registry.parse`` keeps only the decimal codes that decode
    to a row-backed ref or chunk — a ``pa5`` paper and a ``pc10`` chunk
    count, a file-backed ``sk``/``py`` token or a coincidence like
    ``ab123`` (no such code) does not. Chunk handles of the numeric
    kinds (``gc<N>``, ``jc<N>`` — :func:`._numeric_ref._no_chunk_handle_read_kinds`)
    are dropped: they have no ``get(id=…)`` read, so naming them under
    "each reads via get(id='<handle>')" would promise a dead path.
    """
    from precis.handlers._numeric_ref import _no_chunk_handle_read_kinds
    from precis.utils import handle_registry

    no_chunk = _no_chunk_handle_read_kinds()
    seen: dict[str, None] = {}
    for m in _HANDLE_TOKEN_RE.finditer(text):
        tok = m.group(1)
        parsed = handle_registry.parse(tok)
        if tok in seen or parsed is None:
            continue
        kind, is_chunk, _pk = parsed
        if is_chunk and kind in no_chunk:
            continue
        seen[tok] = None
    return list(seen)


class _CreatedGripe(NamedTuple):
    """A raw gripe insert, pre-ack. Create variants that take over the
    reply (theme-keeper tag line, observation close) need the ``ref_id``
    :meth:`_render_create_ack` alone would not hand back."""

    ref_id: int
    readings: list[str]
    has_thread: bool
    short_title: str | None
    title_src_len: int | None


class _Window(NamedTuple):
    """A validated comment selector: one comment, the newest K, or an
    ord range ``lo``-``hi`` (``args={'comments': 'A-B'}``)."""

    only: int | None = None
    last: int | None = None
    lo: int | None = None
    hi: int | None = None


def age_summary(
    store: Store,
    tags: list[str],
    *,
    exclude_tags: list[str] | None = None,
    total: int | None = None,
    id_min: int | None = None,
    id_max: int | None = None,
) -> str:
    """``N open · M older than 3d · K unpicked ≥14d · oldest grX (D days)``.

    Shared by the queryless list header and ``precis gripes --summary``
    (the session-start digest line). ``total`` skips the count when the
    caller already has it. The label is the single ``STATUS:`` value in
    ``tags`` (``open``), else ``live`` when terminal statuses are excluded,
    else ``listed``. The ``unpicked`` clause only appears when at least
    one old-enough gripe has no fix state, no live job/todo link and no
    STATUS past ``triaged`` (gr520909).
    """
    from precis.utils.handle_registry import format_handle

    filters: dict[str, Any] = {"exclude_tags": exclude_tags} if exclude_tags else {}
    if id_min is not None:
        filters["id_min"] = id_min
    if id_max is not None:
        filters["id_max"] = id_max
    if total is None:
        total = store.count_refs(kind="gripe", tags=tags, **filters)
    statuses = [t.split(":", 1)[1] for t in tags if t.startswith("STATUS:")]
    label = statuses[0] if len(statuses) == 1 else "live" if exclude_tags else "listed"
    now = datetime.now(UTC)
    stale = store.count_refs(
        kind="gripe", tags=tags, created_before=now - _STALE_AFTER, **filters
    )
    parts = [f"{total} {label}", f"{stale} older than {_STALE_AFTER.days}d"]
    # gr520909: age alone isn't "unpicked" — count only the old-enough
    # gripes with no fix state, no live job/todo edge and no STATUS past
    # triaged. Derived each read, so a pickup clears it on next render.
    unpicked = gf.stale_unpicked(
        store,
        store.list_refs(
            kind="gripe",
            tags=tags,
            created_before=now - gf.UNPICKED_AFTER,
            limit=5000,
            **filters,
        ),
        now=now,
    )
    if unpicked:
        parts.append(f"{len(unpicked)} unpicked ≥{gf.UNPICKED_AFTER.days}d")
    oldest = store.list_refs(
        kind="gripe", tags=tags, order_by="created_asc", limit=1, **filters
    )
    if oldest:
        days = (now - oldest[0].created_at).days
        handle = format_handle("gripe", oldest[0].id)
        parts.append(f"oldest {handle} ({days} day{'' if days == 1 else 's'})")
    return " · ".join(parts)


class GripeHandler(NumericRefHandler):
    #: Agent-authored assertions (graph-node-trust-state slice 1).
    trust_origin: ClassVar[str | None] = "generated"
    spec: ClassVar[KindSpec] = KindSpec(
        kind="gripe",
        title="Gripe",
        description=(
            "The project's bug tracker. Numeric id assigned on "
            "create. Body + append-only comment timeline live as "
            "chunks. Status tracked via the STATUS: tag (one of: open, "
            "triaged, ready_for_fix, in_review, verify, done, wontfix)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        supports_edit=True,
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
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "delete": ArgsSpec(tl.DELETE_ARGS_KEYS),
    }
    #: ``put(id=N, text=...)`` appends a comment (the unknown-args= refusal
    #: points at it, gr478557).
    supports_comment: ClassVar[bool] = True

    kind: ClassVar[str] = "gripe"
    sense: ClassVar[str] = "gripe"
    #: get/tag/link take ``id=[...]`` (summary get, one-transaction tag/fan-in link).
    batch_ids: ClassVar[bool] = True
    #: ``edit(id=[...])`` runs the handler's own one-transaction batch.
    edit_id_list: ClassVar[str] = "native"
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:open",)

    # The everyday gripe search is "what's still open?" — so default the
    # STATUS filter to open. Overridable: pass an explicit status= (e.g.
    # status='wontfix'), pin a STATUS: tag, or status='*' for every
    # status regardless. Mirrors finding's status-axis default (there:
    # 'established'). The default surfaces a one-line hint in the header
    # so it's never a silent filter.
    default_search_status: ClassVar[str] = "open"

    # Queryless browse is a triage queue: PRIO first, oldest first within a
    # PRIO, so the long-waiting gripe is on page 1 instead of past it.
    # sort='recency' still gives the newest-touched order.
    browse_default_sort: ClassVar[str] = "prio"
    prio_order_by: ClassVar[str] = "prio_age"

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
        # /queue and /batch are the gripe-fix loop views (gr519361),
        # rendered in get() before the base list-view route.
        return ("recent", "open", "wontfix", "queue", "batch")

    paged_list_views: ClassVar[frozenset[str]] = frozenset({"open", "wontfix"})

    def _list_view(
        self, view: str, *, page: int = 1, page_size: int | None = None
    ) -> Response | None:
        size = 20 if page_size is None else page_size
        offset = (page - 1) * size
        if view not in self.paged_list_views:
            return super()._list_view(view)
        tags = ["STATUS:open"] if view == "open" else ["STATUS:wontfix"]
        # The next-page call must repeat a non-default page_size or the
        # continuation silently shrinks back to 20 rows.
        more_args = "'page': {next}"
        if page_size is not None:
            more_args += f", 'page_size': {size}"
        resp = self._list_by_tags(
            tags,
            page_size=size,
            page=page,
            offset=offset,
            sort="prio" if view == "open" else "recency",
            more_call=f"get(kind='gripe', id='/{view}', args={{{{{more_args}}}}})",
        )
        if "next page:" in resp.body:
            resp = replace(
                resp,
                body=resp.body
                + f"\nfallback: search(kind='gripe', status='{view}', page_size=100)",
            )
        return replace(
            resp,
            body=resp.body
            + "\nbatch-read: get(kind='gripe', id=[<ids>], view='body'|'full')",
        )

    def _list_summary(
        self,
        tags: list[str],
        *,
        exclude_tags: list[str] | None,
        total: int,
        past_page: int,
        id_min: int | None = None,
        id_max: int | None = None,
    ) -> str:
        """``N open · M older than 3d · oldest grX (D days) · K more past this page``."""
        line = age_summary(
            self.store,
            tags,
            exclude_tags=exclude_tags,
            total=total,
            id_min=id_min,
            id_max=id_max,
        )
        if past_page > 0:
            line += f" · {past_page} more past this page (page=N)"
        return line

    def _list_row_marks(self, refs: list[Ref]) -> dict[int, str]:
        """gr520909: an old unpicked gripe's row shows ``unpicked Nd``."""
        return {
            rid: f"unpicked {d}d"
            for rid, d in gf.stale_unpicked(self.store, refs).items()
        }

    # ── put: create or append-comment ───────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        # ``body=`` shares the prose slot with ``text=`` the same way
        # todo put documents the split (gr515699): ``text=`` is the
        # title / first line, ``body=`` the details — joined below so
        # the todo shape files identically here. ``body=`` alone is the
        # whole prose (the old body→text alias behaviour).
        body: str | None = None,
        title: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        prio: int | None = None,
        # ``args={'new': True}`` bypasses theme merging; ``args=
        # {'observation': True}`` files the gripe already closed. Both are
        # create-only keys — the dispatcher flattens args= into kwargs.
        new: bool | None = None,
        observation: bool | None = None,
        **_kw: Any,
    ) -> Response:
        # ``text=`` + ``body=`` join as first line + details (the todo
        # split, gr515699); ``body=`` alone is the whole prose.
        if isinstance(body, str) and body.strip():
            body = body.strip()
            head = (text or "").strip()
            text = f"{head}\n\n{body}" if head else body
        # A gripe's title is the first line of its body, so ``title=`` is
        # sugar for prepending it (skipped when text already leads with it).
        # An over-limit title= files like text=-only: the stored title is
        # the same ellipsis-marked derived cut, never a refusal (gr528151).
        if title is not None and id is None and title.strip():
            title = title.strip()
            existing = (text or "").strip()
            if not existing.startswith(title):
                text = f"{title}\n\n{existing}" if existing else title
        # automated writers cannot rephrase: mask here; agent verb calls are
        # refused at the dispatch boundary (runtime/dispatch.py)
        text = mask_secrets(text)
        # Near-miss PRIO spellings (PRIO:medium) are read as canonical and
        # named on the reply's first line (gr494831 step 2); same for
        # near-miss STATUS values ('STATUS:closed' → 'STATUS:done',
        # gr508686). Runs here so create and comment paths share it —
        # the comment path's tag leg re-aliases harmlessly on the
        # already-canonical list.
        tags, readings = alias_prio_tags(tags)
        tags, status_readings = alias_status_tags(tags, kind=self.kind)
        readings += status_readings
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
            if self._is_ro():
                # The dispatch carve-out is create-only; a direct call
                # under agent_ro would die as Internal on the chunk
                # INSERT — name the one thing a ro run may do (gr516603).
                raise Denied(
                    "a read-only run can't comment on or tag an existing "
                    f"{self._sense()} — its gripe channel is create-only",
                    next=f"put(kind={self.kind!r}, text='your friction report')",
                )
            if new or observation:
                raise BadInput(
                    "args={'new': …}/{'observation': …} only apply when filing "
                    f"a new {self._sense()}, not commenting on id={id}",
                    next=f"put(kind={self.kind!r}, id={id}, text='your comment')",
                )
            if prio is not None:
                # Reject rather than silently drop — priority mutation on
                # an existing gripe goes through tag().
                raise BadInput(
                    f"prio= is not accepted when appending a {self._sense()} comment",
                    next=f"use tag(kind={self.kind!r}, id={id}, prio={prio})",
                )
            no_text = text is None or not text.strip()
            edges = (link, unlink, tags, untags)
            if no_text and mode is None and any(e is not None for e in edges):
                # put(id=N, link=…/tags=…) with no text= has one reading:
                # the edge/tag edit alone (prod 2026-10-15..16: three
                # link-only comment puts refused). Run exactly that, named.
                readings.append(
                    self._edges_only_reading(
                        id, link=link, unlink=unlink, rel=rel, tags=tags, untags=untags
                    )
                )
                resp = self._append_then_link(
                    None,
                    id=id,
                    link=link,
                    unlink=unlink,
                    rel=rel,
                    tags=tags,
                    untags=untags,
                    what="link/tag edit",
                )
                return interpreted(resp, readings)
            if text is None or not text.strip():
                raise BadInput(
                    f"appending a comment to {self._sense()} id={id!r} requires text=",
                    next=(f"put(kind={self.kind!r}, id={id}, text='your comment')"),
                )
            # link=/unlink=/rel= and tags=/untags= ride along: comment +
            # edge/tag edit in one transaction (comment-then-close is one
            # put, not reject + re-put + tag(); gr484232). The tag edit is
            # this kind's own tag() — the same STATUS guards apply.
            resp = self._append_then_link(
                lambda: self._append_comment(id=id, text=text),
                id=id,
                link=link,
                unlink=unlink,
                rel=rel,
                tags=tags,
                untags=untags,
                what=f"{self._sense()} comment",
            )
            return interpreted(resp, readings)
        # Read-scoped create (write:none, --read-only, agent_ro — the
        # dispatch carve-out is what let this put reach the handler): the
        # channel is text-only. Refuse — don't strip — every
        # write-shaping arg: the run should learn the one call shape it
        # can make, and "filed" must never mean "filed with different
        # tags than you asked" (gr516603 review; since gr512233 every
        # read scope gets this channel, not only the env deny — and
        # ``_is_ro`` adds the per-call grants probe, so an
        # ``agent_ro`` run that never recorded a boot verdict is refused
        # here too instead of failing downstream).
        if self._is_ro():
            non_text = [
                name
                for name, val in (
                    ("tags", tags),
                    ("untags", untags),
                    ("link", link),
                    ("unlink", unlink),
                    ("rel", rel),
                    ("prio", prio),
                    ("new", new),
                    ("observation", observation),
                )
                if val
            ]
            if non_text:
                raise Denied(
                    "a read-only run's gripe channel takes text= only — "
                    f"{', '.join(non_text)} needs a write-capable run",
                    next=f"put(kind={self.kind!r}, text='your friction report')",
                )
        # Observation (gr514477 batch): file already closed. The auto-close
        # goes through the same resolved-comment + tag path as a manual
        # close, so the close gate applies unchanged.
        if observation:
            self._reject_mutating_put(
                id=None, mode=None, untags=untags, unlink=unlink, rel=rel, link=link
            )
            with _PUT_PRIO.bind(validate_prio(prio)):
                resp = self._create_observation(
                    text=text, tags=tags, link=link, rel=rel
                )
            return interpreted(resp, readings)
        # Recurring-theme merge (gripe-ux batch, Reto 2026-10-24): a filing
        # hitting exactly one registered theme joins that theme's live
        # ``theme:<slug>`` keeper as a '+1' comment instead of minting a
        # duplicate ref; with no live keeper the new gripe takes the tag
        # and becomes it. Filers never search first — matching lives here.
        # A read-only filing skips the merge entirely: the '+1' would be a
        # comment INSERT on someone else's ref — exactly the write
        # ``agent_ro`` lacks (gr516603; filers file, triage dedups).
        matched: list[str] = [] if (new or self._is_ro()) else match_themes(text or "")
        note = ""
        if len(matched) == 1:
            slug = matched[0]
            keeper = self._theme_keeper(slug)
            if keeper is not None:
                resp = self._merge_into_keeper(
                    keeper,
                    slug,
                    text=text,
                    prio=prio,
                    link=link,
                    unlink=unlink,
                    rel=rel,
                    tags=tags,
                    untags=untags,
                )
                return interpreted(resp, readings)
            tags = [*(tags or []), f"{THEME_TAG_PREFIX}{slug}"]
            note = (
                f"tagged {THEME_TAG_PREFIX}{slug} — no live keeper existed, "
                "so this gripe is now the theme keeper; later matching "
                "filings merge into it as '+1' comments"
            )
        elif len(matched) > 1:
            note = f"matched themes: {', '.join(matched)} — filed separately (no merge)"
        # Create path: plumb a create-time ``prio=`` into ``_create`` via a
        # request-scoped slot (``_PUT_PRIO``, as todo does — the base put's
        # signature stays untouched). The ``PRIO:`` tag alias keeps working;
        # the explicit kwarg wins.
        with _PUT_PRIO.bind(validate_prio(prio)):
            resp = super().put(
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
        if note:
            resp = replace(resp, body=f"{resp.body}\n{note}")
        return interpreted(resp, readings)

    def _theme_keeper(self, slug: str) -> int | None:
        """The oldest live gripe tagged ``theme:<slug>``, or ``None``.

        "Live" = not carrying a terminal STATUS — a triaged/in-review
        keeper still collects ``+1``s; once it closes, the next matching
        filing becomes the new keeper.
        """
        refs = self.store.list_refs(
            kind=self.kind,
            tags=[f"{THEME_TAG_PREFIX}{slug}"],
            exclude_tags=sorted(_TERMINAL_STATUS_TAGS),
            order_by="created_asc",
            limit=1,
        )
        return refs[0].id if refs else None

    @staticmethod
    def _merge_comment(text: str | None) -> str:
        """``+1 (<first line>): <filed text>`` — the merge comment shape.

        The parenthetical is the gist for a multi-line filing; on a
        single-line filing it would repeat the whole text verbatim, so
        it is dropped (gr515447).
        """
        from precis.handlers._gripe_title import first_line

        body = (text or "").strip()
        src = first_line(body)
        if src == body:
            return f"+1: {body}"
        if len(src) > 100:
            src = src[:99].rstrip() + "…"
        return f"+1 ({src}): {body}"

    def _merge_into_keeper(
        self,
        keeper: int,
        slug: str,
        *,
        text: str | None,
        prio: int | None,
        link: str | None,
        unlink: str | None,
        rel: str | None,
        tags: list[str] | None,
        untags: list[str] | None,
    ) -> Response:
        """Fold a theme-matched filing into its keeper as a ``+1`` comment.

        Same semantics as ``put(id=N, …)``: caller ``link=``/``tags=``/
        ``untags=`` ride onto the keeper in one transaction with every
        guard intact. ``prio=`` has no comment carrier — refuse it naming
        the direct ``tag()`` door.
        """
        if prio is not None:
            raise BadInput(
                f"this filing merges into {self._sense()} id={keeper}; prio= "
                "is not accepted on a +1 comment",
                next=f"tag(kind={self.kind!r}, id={keeper}, prio={prio})",
            )
        resp = self._append_then_link(
            lambda: self._append_comment(id=keeper, text=self._merge_comment(text)),
            id=keeper,
            link=link,
            unlink=unlink,
            rel=rel,
            tags=tags,
            untags=untags,
            what=f"{self._sense()} comment",
        )
        from precis.utils.handle_registry import format_handle

        return replace(
            resp,
            body=(
                f"{resp.body}\nmerged into {format_handle(self.kind, keeper)} "
                f"(theme {slug}); to file separately anyway pass "
                "args={'new': True}"
            ),
        )

    def _create_observation(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None,
    ) -> Response:
        """File a gripe already closed: ``observation`` tag + ``STATUS:done``.

        The close is *not* a gate bypass: after the insert a real
        ``resolved:`` comment is appended and ``STATUS:done`` goes through
        this kind's own ``tag()`` — ``_require_resolved_comment`` sees its
        comment exactly as it would on a manual close.
        """
        if self._is_ro():
            raise BadInput(
                "a read-only run can't file an already-closed gripe — the "
                "resolved comment + STATUS:done writes need a write-capable role",
                next=(
                    f"put(kind={self.kind!r}, text='...') — it files open; "
                    "triage closes it"
                ),
            )
        created = self._insert_gripe(
            text=text, tags=[*(tags or []), "observation"], link=link, rel=rel
        )
        resp = self._append_then_link(
            lambda: self._append_comment(
                id=created.ref_id,
                text="resolved: observation, auto-closed at filing",
            ),
            id=created.ref_id,
            link=None,
            unlink=None,
            rel=None,
            tags=["STATUS:done"],
            untags=None,
            what=f"{self._sense()} comment",
        )
        return interpreted(
            Response(
                body=(
                    f"created {self.kind} id={created.ref_id} (STATUS:done, "
                    "observation) — auto-closed at filing.\n"
                    f"{resp.body}\n"
                    f"list: search(kind={self.kind!r}, tags=['observation'], "
                    "status='*'); reopen: "
                    f"tag(kind={self.kind!r}, id={created.ref_id}, "
                    "add=['STATUS:open'], remove=['STATUS:done'])"
                )
            ),
            created.readings,
        )

    def _edges_only_reading(
        self,
        id: str | int,
        *,
        link: str | None,
        unlink: str | None,
        rel: str | None,
        tags: list[str] | None,
        untags: list[str] | None,
    ) -> str:
        """``put(kind='gripe', id=N) with no text= read as link(…) + tag(…)``."""
        calls: list[str] = []
        target = link if link is not None else unlink
        if target is not None:
            remove = ", mode='remove'" if link is None else ""
            rel_part = f", rel={rel!r}" if rel is not None else ""
            calls.append(
                f"link(kind={self.kind!r}, id={id!r}, target={target!r}{remove}{rel_part})"
            )
        if tags is not None or untags is not None:
            ops = [f"add={tags!r}"] if tags is not None else []
            ops += [f"remove={untags!r}"] if untags is not None else []
            calls.append(f"tag(kind={self.kind!r}, id={id!r}, {', '.join(ops)})")
        return (
            f"put(kind={self.kind!r}, id={id!r}) with no text= read as "
            + " + ".join(calls)
        )

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

    # ── tag: sync a PRIO: alias into the canonical prio column ───────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        prio: int | None = None,
        reason: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Near-miss PRIO spellings are read as canonical, named in the reply.
        add, readings = alias_prio_tags(add)
        # Same for near-miss STATUS values — 'STATUS:closed'/'resolved'
        # read as 'STATUS:done' (gr508686's top caller-error family), so
        # the refusal that still fires is the real protocol one (the
        # ``resolved:``-comment guard).
        add, status_readings = alias_status_tags(add, kind=self.kind)
        readings += status_readings
        readings = [*readings, *self._check_thread_tags(add, id=id)]
        if isinstance(id, list):
            return interpreted(
                self._tag_batch(
                    id, add=add, remove=remove, prio=prio, reason=reason, **_kw
                ),
                readings,
            )
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
        self._require_resolved_comment(id, add)
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
                # The prio write above committed on its own connection; the
                # gr504803 audit row lands right after it.
                self._record_tag_reason(ref_id, reason)
                return interpreted(
                    Response(body=f"set {shown} on {self._sense()} id={ref_id}"),
                    readings,
                )
        return interpreted(
            super().tag(id=id, add=add, remove=remove, reason=reason, **_kw), readings
        )

    def _check_thread_tags(
        self, tags: list[str] | None, *, id: object = "<id>", create: bool = False
    ) -> list[str]:
        """Refuse a ``thread:<slug>`` tag naming no thread file. The closed
        list is :func:`precis.utils.thread_files.thread_slugs`; a process
        without the thread files passes the tag unchecked and says so (the
        returned reading). ``create=True`` (the ``put`` create path) makes
        the refusal's ``next=`` a corrected put — ``tag()`` on an id the
        refused put never minted is a dead end (gr503548)."""
        from precis.utils.thread_files import (
            THREAD_TAG_PREFIX,
            thread_slugs,
            threads_dir,
        )

        named = sorted(
            {
                t.lower()[len(THREAD_TAG_PREFIX) :]
                for t in tags or ()
                if t.lower().startswith(THREAD_TAG_PREFIX)
            }
        )
        if not named:
            return []
        slugs = thread_slugs()
        if slugs is None:
            return ["thread tag not checked: no docs/backlog/threads in this checkout"]
        unknown = [s for s in named if s not in slugs]
        if unknown:
            slug = unknown[0]
            sorted_slugs = sorted(slugs)
            close = difflib.get_close_matches(slug, sorted_slugs, n=3, cutoff=0.6)
            if not close:
                # Nothing near at the usual cutoff — still name the top
                # candidates so the refusal always lists live slugs
                # (gr506766: 'graph-gardener' had no >=0.6 neighbour and the
                # agent saw only the bare "unknown thread" text).
                close = difflib.get_close_matches(slug, sorted_slugs, n=3, cutoff=0.0)
            cause = (
                f"unknown thread {slug!r}: a thread tag names a "
                "docs/backlog/threads/<slug>.md file"
            )
            if close:
                cause += f"; nearest: {', '.join(close)}"
            backlog_dir = threads_dir().parent
            item = slug if (backlog_dir / f"{slug}.md").is_file() else None
            if item is None and backlog_dir.is_dir():
                # Same door, fuzzier match — agents tag docs/backlog/<slug>.md
                # *item* names, often clipped ('graph-gardener' for
                # 'graph-gardener-value', gr506766).
                near_item = difflib.get_close_matches(
                    slug,
                    [p.stem for p in backlog_dir.glob("*.md")],
                    n=1,
                    cutoff=0.6,
                )
                if near_item:
                    item = near_item[0]
            if item is not None:
                # docs/backlog/<slug>.md is a planned-work item, not a
                # thread — agents tag item slugs (gr503548: graph-gardener,
                # memory-graph, gardener-value). Name the link door with a
                # target that resolves (gr516697): the old
                # 'md:main/docs/backlog/<item>.md' hint could never — md is
                # a DB-free index kind, its ids are get/search-only.
                cause += (
                    f"; {item!r} is a backlog item, not a thread — "
                    "threads are docs/backlog/threads/*.md; "
                    f"{self._backlog_item_link_hint(backlog_dir, item)} "
                    "and pick a thread from options"
                )
            fix = close[0] if close else "<slug>"
            if create:
                next_call = (
                    f"put(kind={self.kind!r}, title='<title>', text='...', "
                    f"tags=['{THREAD_TAG_PREFIX}{fix}'])"
                )
            else:
                shown = id if not isinstance(id, list) else "<id>"
                next_call = (
                    f"tag(kind={self.kind!r}, id={shown}, "
                    f"add=['{THREAD_TAG_PREFIX}{fix}'])"
                )
            raise BadInput(cause, options=sorted(slugs), next=next_call)
        return []

    def _backlog_item_link_hint(self, backlog_dir: Path, item: str) -> str:
        """The ``link=`` target that resolves for a backlog item file.

        ``docs/backlog/<item>.md`` is reachable through the DB-free
        ``md`` index kind for get/search — never for link (that kind
        carries no refs rows). The linkable form is the store-backed
        ``markdown`` kind's file slug: ``docs--backlog--<item>`` under
        the workspace root, ``<alias>/<slug>`` under a ``PRECIS_ROOTS``
        mirror. :func:`precis.utils.link_target.mint_lazy_link_target`
        mints the file's ref on first link, so the named call works in
        one step. Falls back to naming the rule when no live markdown
        handler can see the file (kind not mounted, or the file outside
        every configured root).
        """
        from precis.utils.md_parse import file_slug_from_path

        path = (backlog_dir / f"{item}.md").resolve()
        hub = getattr(self, "hub", None)
        md = hub.handler_for("markdown") if hub is not None else None
        if md is not None:
            roots: list[tuple[str | None, Any]] = [(None, md.root)]
            roots += [(alias, m.path) for alias, m in md.mirrors.items()]
            for alias, root in roots:
                try:
                    rel = path.relative_to(root.resolve())
                except ValueError:
                    continue
                try:
                    slug = file_slug_from_path(str(rel))
                except ValueError:
                    break
                target = f"markdown:{f'{alias}/' if alias else ''}{slug}"
                return f"link the item with link={target!r}"
        return (
            f"the item file docs/backlog/{item}.md is a file, not a "
            "corpus ref — where the markdown file kind is mounted its "
            "ref links as link='markdown:<file-slug>'"
        )

    def _comments(self, ref_id: int, ref: Ref | None = None) -> list[Any]:
        """Visible comments (hidden ones, per ``refs.meta``, are skipped)."""
        if ref is None:
            ref = self.store.get_ref(kind=self.kind, id=ref_id, include_deleted=True)
        hidden = tl.hidden_comments(ref) if ref is not None else set()
        return [
            c
            for c in self.store.chunks.list_chunks_for_ref(ref_id)
            if c.chunk_kind == _COMMENT_KIND and c.ord not in hidden
        ]

    def _require_resolved_comment(self, id: str | int, add: list[str] | None) -> None:
        """A terminal STATUS needs a newest comment starting ``resolved:``."""
        if not any(t in _TERMINAL_STATUS_TAGS for t in add or ()):
            return
        ref_id = self._coerce_id(id)
        self._resolve_live_ref(ref_id)
        comments = self._comments(ref_id)
        newest = max(comments, key=lambda c: c.ord).text if comments else ""
        if newest.lstrip().startswith(_RESOLVED_PREFIX):
            return
        wontfix = "STATUS:wontfix" in (add or ())
        example = (
            "resolved: wontfix — <reason>"
            if wontfix
            else "resolved: <sha> (r<N>) — <how verified>"
        )
        raise BadInput(
            f"closing a {self._sense()} needs a newest comment starting "
            f"{_RESOLVED_PREFIX!r}",
            next=(
                f"put(kind={self.kind!r}, id={ref_id}, text={example!r}) first, "
                f"then repeat this tag (or pass tags={list(add or ())!r} on that put)"
            ),
        )

    def _after_tag_mutation(
        self,
        ref_id: int,
        added: list[Tag],
        removed: list[Tag],
        *,
        conn: Any,
    ) -> None:
        """gr520237: a terminal STATUS cascades to everything that
        referenced or waited on this gripe — before this hook nothing
        heard about the close, and ``wontfix`` gripes kept blocking
        todos forever."""
        terminal = next(
            (str(t).split(":", 1)[1] for t in added if str(t) in _TERMINAL_STATUS_TAGS),
            None,
        )
        if terminal is not None:
            gripe_close_cascade(self.store, ref_id, terminal, conn=conn)

    def _render_tombstone(self, ref_id: int) -> Response:
        """Read-only view of a soft-deleted gripe: title, last STATUS, last comment."""
        ref = self.store.get_ref(kind=self.kind, id=ref_id, include_deleted=True)
        if ref is None:
            raise NotFound(
                f"{self._sense()} id={ref_id} not found",
                next=f"search(kind='{self.kind}', q='<keywords>') for the live id",
            )
        statuses = [
            str(t) for t in self.store.tags_for(ref_id) if str(t).startswith("STATUS:")
        ]
        comments = self._comments(ref_id)
        last = max(comments, key=lambda c: c.ord) if comments else None
        lines = [
            f"{self._sense()} id={ref_id} [soft-deleted, read-only]",
            f"title: {ref.title}",
            f"last status: {', '.join(statuses) or '(none)'}",
            f"last comment: {last.text if last else '(none)'}",
        ]
        return Response(body="\n".join(lines))

    # ── create: ref + body chunk + default tags + (optional) link ──

    def _is_ro(self) -> bool:
        """Whether this filing must take the read-only channel —
        ``runtime.scope.current_scope() == 'read'`` (gr512233): the
        envelope deny naming ``put`` (``PRECIS_MCP_DENY_VERBS``; the
        dispatch carve-out is what let the call reach here), ``serve
        --read-only``, ``PRECIS_MCP_READ_ONLY``, or the boot probe's
        ``agent_ro``/read-only-DSN verdict.

        The env half matters because tier-2 is dark by default
        (``pool._db_role_enforced``): a ``write:none`` run usually holds
        an *rw* connection, so the grants probe alone would take the
        uncapped writer path — the deny env is the signal, not the
        grants (gr516603 review).

        The per-call grants probe is the second check for the scope's
        holes (gr512233 round-2 review): a process that never ran
        ``dispatch.boot``, booted ``boot_writes=False``, or whose boot
        probe wobbled would scope ``write`` on a genuinely read-only
        store and take the uncapped writer path. One cheap query per
        filing recovers the per-call probe the boot cache replaced;
        detection still fails *open* (scope says write when unknown —
        the DB grants are the enforcement), this just catches the case
        where we know better.
        """
        from precis.dispatch import _probe_read_only
        from precis.runtime.scope import current_scope

        if current_scope() == "read":
            return True
        store = getattr(self, "store", None)
        return store is not None and _probe_read_only(store)

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
        created = self._insert_gripe(text=text, tags=tags, link=link, rel=rel)
        return interpreted(
            self._render_create_ack(
                created.ref_id,
                has_thread=created.has_thread,
                short_title=created.short_title,
                title_src_len=created.title_src_len,
            ),
            created.readings,
        )

    def _insert_gripe(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
    ) -> _CreatedGripe:
        """Validate + insert a gripe (ref, body chunk, tags, link).

        The ack wording stays with the caller so create variants (theme
        keeper, observation close) answer in their own words — every
        guard (empty text, terminal-STATUS refusal, thread-tag check,
        prio sync) stays in this one place.
        """
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
        import psycopg.errors

        from precis.handlers._gripe_title import first_line
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
        if any(t in _TERMINAL_STATUS_TAGS for t in tags or ()):
            raise BadInput(
                f"a new {self._sense()} cannot be filed already closed",
                next=(
                    f"file it, then put(kind={self.kind!r}, id=<new id>, "
                    "text='resolved: …', tags=['STATUS:done'])"
                ),
            )
        # Read-only filing (gr516603/gr512233): ``put``'s text-only gate
        # refused every write-shaping arg under ``_is_ro`` already, so
        # ``ro`` here means a bare ``text=`` create — the definer stamps
        # it ``source:agent_ro`` + ``friction`` + ``agent:<run>`` and the
        # reading says so. (Caller ``tags=``/``link=``/``prio=`` can no
        # longer reach this point under ro; they get Denied upstream.)
        ro = self._is_ro()
        if link is not None:
            mint_lazy_link_target(link, store=self.store, hub=self.hub)
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
        # explicit ``put(prio=)`` (plumbed via ``_PUT_PRIO``) wins.
        tags, prio_from_tag = split_prio(tags)
        readings = self._check_thread_tags(tags, create=True)
        if (put_prio := _PUT_PRIO.get()) is not None:
            prio_from_tag = put_prio
        if ro:
            readings.append(
                "read-only run: stamped source:agent_ro + friction + "
                "agent:<run> by file_gripe_readonly"
            )
        parsed_extra_tags = [Tag.parse_strict(t, kind=self.kind) for t in (tags or [])]

        with self.store.tx() as conn:
            try:
                row = conn.execute(
                    "SELECT public.file_gripe_readonly(%s, %s)",
                    (text, _ro_gripe_source() if ro else None),
                ).fetchone()
            except psycopg.errors.RaiseException as exc:
                refusal = _filing_refusal(exc)
                if refusal is not None:
                    raise refusal from exc
                raise
            assert row is not None
            ref_id = int(row[0])
            short = self._shorten_title(conn, ref_id, text)
            if not ro and prio_from_tag is not None:
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
        return _CreatedGripe(
            ref_id=ref_id,
            readings=readings,
            has_thread=not ro
            and any(t.lower().startswith("thread:") for t in tags or ()),
            short_title=short,
            title_src_len=len(first_line(text)) if short is not None else None,
        )

    @staticmethod
    def _shorten_title(conn: Any, ref_id: int, text: str) -> str | None:
        """Store the derived short title (:mod:`._gripe_title`) on a new
        gripe whose first line is long; the body keeps the full text.

        ``file_gripe_readonly`` stores the full text as the title. The
        rewrite runs in a savepoint: an ``agent_ro`` connection cannot
        UPDATE refs, and its gripe must still file, so a refused rewrite
        rolls back alone and the full-text title stays. Returns the stored
        short title, or ``None``.

        Uses the lenient :func:`._gripe_title.short_title` (gr524014
        supersedes gr520270's refusal): a first clause that fits no
        boundary inside ``TITLE_MAX`` stores its mid-phrase cut with an
        ellipsis (gr528151), inside the cap — take the gripe anyway,
        triage can fix the title."""
        import psycopg

        from precis.handlers._gripe_title import short_title

        title = short_title(text, mark_truncation=True)
        if title is None:
            return None
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE refs SET title = %s WHERE ref_id = %s", (title, ref_id)
                )
        except psycopg.Error:
            log.info("gripe: title not shortened for id=%d (write refused)", ref_id)
            return None
        return title

    def edit(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        title: str | None = None,
        text: str | None = None,
        body: str | None = None,
        dry_run: bool | str | None = None,
        # ``mode='fix'`` keys (gr519361, :mod:`._gripe_fix`) — named here
        # so the dispatcher's args= gate admits them; read only under that
        # mode, refused with the title door otherwise.
        batch: str | None = None,
        host: str | None = None,
        session: str | None = None,
        tree: str | None = None,
        brief: str | None = None,
        handoff: str | None = None,
        review: str | None = None,
        findings: str | None = None,
        landed: str | None = None,
        **kw: Any,
    ) -> Response:
        """Rewrite a gripe's title (``refs.title``), or — ``mode='fix'`` —
        record gripe-fix loop state (:func:`._gripe_fix.apply_patch`). The
        body and the comment timeline are append-only: add context with
        ``put(id=N, text=...)``. ``text=``/``body=`` are declared only so
        they are refused here, with that recovery named. ``id=[...]``
        (``batch_ids``) applies the same edit to every id in one
        transaction — :meth:`_edit_batch` (gr514482)."""
        fix_kw = {
            "batch": batch,
            "host": host,
            "session": session,
            "tree": tree,
            "brief": brief,
            "handoff": handoff,
            "review": review,
            "findings": findings,
            "landed": landed,
        }
        if isinstance(id, list):
            return self._edit_batch(
                id, title=title, text=text, body=body, dry_run=dry_run, **fix_kw, **kw
            )
        # The edit verb injects ``verdict='approved'`` on every call
        # (``_WRAPPER_DEFAULTED_KWARGS``); drop that default so the
        # refusal below doesn't fire on a kwarg the caller never sent
        # (gr500645). A caller-supplied verdict still lists as unaccepted.
        if kw.get("verdict") == "approved":
            kw.pop("verdict")
        if kw.get("mode") == "fix":
            return self._edit_fix(
                id=id, dry_run=dry_run, title=title, text=text, body=body, **fix_kw
            )
        fix_given = sorted(k for k, v in fix_kw.items() if v is not None)
        if fix_given:
            raise BadInput(
                f"{', '.join(fix_given)} are gripe-fix loop keys — they need "
                "mode='fix'",
                next=(
                    f"edit(kind={self.kind!r}, id={id or '<id>'}, mode='fix', "
                    f"args={{{', '.join(f'{k!r}: ...' for k in fix_given)}}})"
                ),
            )
        extra = sorted(
            [k for k, v in kw.items() if v is not None and k != "mode"]
            + [k for k, v in (("text", text), ("body", body)) if v is not None]
        )
        if id is None or title is None or extra:
            next_hint = (
                f"edit(kind={self.kind!r}, id={id or '<id>'}, "
                "args={'title': 'Area: what is wrong'}) — add context with "
                f"put(kind={self.kind!r}, id=N, text='...')"
            )
            if {"verdict", "review"} & set(extra):
                # gr501173: ``verdict=``/``review=`` are the draft
                # chunk sign-off params (edit(kind='draft',
                # review='human')); an agent generalising them onto a
                # gripe is trying to record its ruling — name the
                # resolution recipe, not the title door.
                next_hint = (
                    f"{self.kind} takes no verdict — that sign-off "
                    "shape is draft-only. Record a resolution with "
                    f"put(kind={self.kind!r}, id={id or '<id>'}, "
                    "text='resolved: <sha> — <how verified>', "
                    "tags=['STATUS:done'])"
                )
            raise BadInput(
                f"edit(kind={self.kind!r}) rewrites the title only"
                + (f"; {', '.join(extra)} not accepted" if extra else ""),
                next=next_hint,
            )
        new = " ".join(title.split())
        if not new:
            raise BadInput(
                "title= is empty",
                next=(
                    f"edit(kind={self.kind!r}, id={id}, "
                    "args={'title': 'Area: what is wrong'})"
                ),
            )
        ref = self._resolve_live_ref(self._coerce_id(id))
        if dry_run:
            return Response(
                body=f"dry run: {self._sense()} id={ref.id} title "
                f"{ref.title!r} → {new!r} (nothing written)"
            )
        self.store.update_ref(ref.id, title=new)
        return Response(
            body=f"{self._sense()} id={ref.id} title {ref.title!r} → {new!r}"
        )

    def _edit_fix(
        self,
        *,
        id: str | int | None,
        dry_run: bool | str | None,
        title: str | None,
        text: str | None,
        body: str | None,
        **raw: Any,
    ) -> Response:
        """``edit(mode='fix')``: validate the whole patch, then write it in
        one transaction (:func:`._gripe_fix.apply_patch`). The ack's first
        line lists every change and the derived queue before → after, so
        :meth:`_edit_batch`'s per-id line carries the effect."""
        if id is None:
            raise BadInput(
                "edit(kind='gripe', mode='fix') requires id=",
                next="get(kind='gripe', id='/queue') lists the loop; then "
                "edit(kind='gripe', id=<id>, mode='fix', args={...})",
            )
        prose = [k for k, v in (("title", title), ("text", text), ("body", body)) if v]
        if prose:
            raise BadInput(
                f"mode='fix' takes no {', '.join(prose)}= — the brief, handoff and "
                "review texts are args keys",
                next=f"edit(kind='gripe', id={id}, mode='fix', args={{'brief': '...'}})",
            )
        patch = gf.validate_patch(id, raw)
        ref = self._resolve_live_ref(self._coerce_id(id))
        tags = self.store.tags_for(ref.id)
        return Response(
            body=gf.apply_patch(self.store, ref, tags, patch, dry_run=bool(dry_run))
        )

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
            raise BadInput(
                f"delete(kind={self.kind!r}) requires id=",
                next=(
                    f"search(kind='{self.kind}', q='<keywords>') for the id, then "
                    f"delete(kind={self.kind!r}, id=<N>)"
                ),
            )
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

    def _render_body_search_hit(
        self,
        ref: Ref,
        block: Any,
        rank: float,
        dist: float | None = None,
        chips: str = "",
    ) -> str:
        """One result line: header + chunk-kind context + matched text.

        ``dist`` rides along for signature compatibility with the base
        fold render (gr514956) — gripe is not in
        ``SEMANTIC_ONLY_FOLD_DISTANCE``, so callers never pass it today.
        ``chips`` is the ref's pre-rendered tag chips (gr520321).
        """
        kind = block.chunk_kind
        if kind == _BODY_KIND:
            label = f"{self._sense()} {ref.id}"
        elif kind == _COMMENT_KIND:
            label = f"{self._sense()} {ref.id} (comment {block.ord})"
        else:
            label = f"{self._sense()} {ref.id} ({kind})"
        metric = f"({dist:.2f})" if dist is not None else f"(rank={rank:.2f})"
        suffix = f"  {chips}" if chips else ""
        return f"\n## {label}  {metric}{suffix}\n{self._snippet(block.text)}"

    # ── rendering: body + comment timeline ──────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        page: int | None = None,
        # ``args={'page_size': N}`` — the list views' canonical row cap
        # (gr514469), same extras channel as ``page``.
        page_size: int | None = None,
        # ``args={'comment': K}`` / ``{'comments': K}`` — the timeline
        # selectors inside ``view='comments'``/``view='full'`` (gr499040,
        # gr503545; flattened to kwargs by the dispatcher's extras
        # handling).
        comment: int | str | None = None,
        comments: int | str | None = None,
        # ``get(id='/queue', args={'queue', 'host', 'batch'})`` filters and
        # ``get(id='/batch', args={'batch': slug})`` (gr519361).
        queue: str | None = None,
        host: str | None = None,
        batch: str | None = None,
        **_kw: Any,
    ) -> Response:
        # ``args={'page': N}`` pages the id='/open' / '/wontfix' lists; the
        # base reads it back out of the forwarded kwargs (gr497175).
        if page is not None:
            _kw["page"] = page
        if page_size is not None:
            _kw["page_size"] = page_size
        view = plain_listing_view(view, ())
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        if id in ("/queue", "/batch"):
            if _kw.get("page") not in (None, 1) or _kw.get("page_size") is not None:
                raise BadInput(
                    f"id={id!r} renders the whole loop on one page; it takes no "
                    "page/page_size",
                    next=f"get(kind='gripe', id={id!r}, args={{'queue': 'in-build'}})"
                    if id == "/queue"
                    else f"get(kind='gripe', id={id!r}, args={{'batch': '<slug>'}})",
                )
            return self._fix_list_view(
                str(id), view=view, queue=queue, host=host, batch=batch
            )
        if queue is not None or host is not None or batch is not None:
            raise BadInput(
                "args={'queue'|'host'|'batch'} filter the gripe-fix list views only",
                next=[
                    "get(kind='gripe', id='/queue', args={'queue': 'in-build', "
                    "'host': '<host>'})",
                    "get(kind='gripe', id='/batch', args={'batch': '<slug>'})",
                ],
            )
        if view == "fix" and id is not None and not isinstance(id, list):
            ref = self._resolve_live_ref(self._coerce_id(id))
            tags = self.store.tags_for(ref.id)
            state = gf.fix_state(self.store, ref, tags)
            head = "\n".join(self._header_lines(ref, tags))
            return Response(body=gf.render_fix_view(ref, state, head))
        if isinstance(id, list):
            # Batch form: the base class renders it (summary-shaped) or
            # refuses the view= combination.
            return super().get(id=id, view=view, q=q, **_kw)
        readings: list[str] = []
        single = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if view in _VIEW_ALIASES and single and q is None:
            canon = _VIEW_ALIASES[view]
            shown = f"view={canon!r}" if canon is not None else "the default view"
            readings.append(f"view={view!r} read as {shown}")
            view = canon
        if comment is not None or comments is not None:
            # The selectors only mean something inside the timeline
            # reads — anywhere else they would be a silent drop
            # (mcp-authoring rule 6). On a *bare* get they have exactly
            # one reading — view='comments' — so run it and name the
            # reading (gr523120, the interpreted convention gr494831);
            # prod kept re-issuing the refused call. view='full' is
            # legal since gr503545: "body + the newest K" is a sensible
            # ask, so the full renderer windows its timeline the same
            # way the comments view does. An explicit other view still
            # refuses.
            if view is None:
                view = "comments"
                readings.append(
                    "args={'comment': K} / {'comments': K} on a bare get "
                    "read as view='comments'"
                )
            elif view not in ("comments", "full"):
                raise BadInput(
                    "args={'comment': K} / args={'comments': K} select inside "
                    "view='comments' or view='full'",
                    next=[
                        f"get(kind='gripe', id={id}, view='comments', "
                        "args={'comments': 5})",
                        f"get(kind='gripe', id={id}, view='full', "
                        "args={'comments': 5})",
                    ],
                )
        if view in ("comments", "body", "full"):
            ref = self._resolve_live_ref(self._coerce_id(id))
            return interpreted(
                Response(
                    body=self._render_ref_view(
                        ref,
                        self.store.tags_for(ref.id),
                        view=view,
                        comment=comment,
                        comments=comments,
                    ),
                    # When the transport fold still cuts (one comment or
                    # the body alone over the budget), name the windowed
                    # reads that reach the rest without a cursor.
                    result_budget_hint=(
                        f"get(kind='gripe', id={ref.id}, view='comments', "
                        "args={'comments': '<A>-<B>'}) reads the timeline "
                        "in windows; view='body' reads the body uncapped"
                    ),
                ),
                readings,
            )
        if view is not None and view not in _BASE_VIEWS:
            views = [*_BASE_VIEWS, "body", "comments", "full", "fix"]
            near = difflib.get_close_matches(view, views, n=1)
            lead = f"nearest is view={near[0]!r}. " if near else ""
            raise Unsupported(
                f"unknown view {view!r} for kind='gripe'",
                options=views,
                next=(
                    lead + "get(kind='gripe', id=<id>, view='comments') — every "
                    "comment, paged whole (args={'comments': K} newest K, "
                    "{'comments': 'A-B'} a window, {'comment': K} one); "
                    "view='body' the body; the plain get is title + tags + count"
                ),
            )
        try:
            resp = super().get(id=id, view=view, q=q, **_kw)
        except Gone:
            # Soft-deleted: keep past closures readable instead of a bare Gone.
            if view is not None or id is None:
                raise
            resp = self._render_tombstone(self._coerce_id(id))
        return interpreted(resp, readings)

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        """The bare get: title card plus the body (gr514351).

        Card = header + tags + ``title:`` + comment count + newest gist;
        the body follows, head+tail-clipped at :data:`_BODY_HEAD_CHARS`
        (the marker names ``view='body'``, the uncapped read). The
        comment timeline stays behind ``view='comments'`` — the
        unbounded timeline was gr499040's measured cost, not the body.
        """
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        comments = [b for b in blocks if b.chunk_kind == _COMMENT_KIND]
        body = next((b.text for b in blocks if b.chunk_kind == _BODY_KIND), ref.title)
        # ``refs.title`` is the whole body text when the first line already
        # fits (:mod:`._gripe_title` only derives a short title for a long
        # one) — the card shows the first line, never the stored blob.
        from precis.handlers._gripe_title import first_line

        title = first_line(ref.title or "")
        if len(title) > _TITLE_CARD_CHARS:
            title = title[: _TITLE_CARD_CHARS - 1].rstrip() + "…"
        lines = [
            *self._header_lines(ref, tags),
            f"title: {title}",
            *self._dup_lines(ref),
            f"comments: {len(comments)} · body: view='body' · "
            "timeline: view='comments'",
        ]
        # gr519361: a gripe in the fix loop shows its batch, derived queue,
        # who (session@host), tree, the newest brief/handoff/review ords
        # and the landed sha — view='fix' reads the texts.
        fix_line = gf.card_line(gf.build_state(ref, tags, blocks))
        if fix_line:
            lines.append(fix_line)
        elif (d := gf.stale_unpicked(self.store, [ref]).get(ref.id)) is not None:
            # gr520909: a gripe nobody picked up for UNPICKED_AFTER shows
            # why and how to clear it (a fix edit or a job/todo link).
            lines.append(
                f"stale: unpicked {d}d — no fix state, "
                f"STATUS:{gf.status_of(tags) or 'none'}, no live job/todo "
                "link (gr520909); pick up: edit(kind='gripe', "
                "mode='fix', args={'tree': '<tree>'}) or link a job/todo"
            )
        # One-line gist of the newest visible comment (~80 B): a closer
        # verifying a close (or a skimmer checking state) shouldn't have
        # to open the timeline for the latest word.
        newest = next(
            (
                c
                for c in sorted(comments, key=lambda c: c.ord, reverse=True)
                if c.ord not in tl.hidden_comments(ref)
            ),
            None,
        )
        if newest is not None:
            gist = " ".join(newest.text.split())[:60]
            lines.append(f"newest: comment {newest.ord} · {tl._stamp(newest)} · {gist}")
        # gr514629: a gripe's text routinely quotes universal handles —
        # a ``pc<N>`` chunk, a ``jo<N>`` job, a ``pa<N>`` paper. Name
        # the read path up front (``get(id='<handle>')`` resolves each,
        # no kind= needed) so an intake reader doesn't hunt for a
        # chunk-text verb that isn't there.
        handles = _referenced_handles(body or "")
        if handles:
            shown = ", ".join(handles[:_REF_LINE_CAP])
            more = (
                f", +{len(handles) - _REF_LINE_CAP} more"
                if len(handles) > _REF_LINE_CAP
                else ""
            )
            lines.append(
                f"refs in text: {shown}{more} — each reads via "
                "get(id='<handle>'): pc<N> is the chunk's text, "
                "jo<N> the job card (status + summary stats)"
            )
        lines += [
            "",
            tl.clip_middle(
                body,
                _BODY_HEAD_CHARS,
                f"get(kind='gripe', id={ref.id}, view='body')",
            ),
        ]
        return "\n".join(lines)

    def _fix_list_view(
        self,
        which: str,
        *,
        view: str | None,
        queue: str | None,
        host: str | None,
        batch: str | None,
    ) -> Response:
        """``/queue`` (derived queues, filterable) and ``/batch`` (census,
        or one batch's dossier with ``args={'batch': slug}``) — gr519361.

        The loop's population is every live gripe with a ``meta.fix``
        key (``list_refs(meta_has='fix')``); terminal gripes drop out of
        the queues by derivation, so nothing has to be un-tagged on close.
        """
        if view is not None:
            raise BadInput(
                f"id={which!r} takes no view= (got {view!r})",
                next=f"get(kind='gripe', id={which!r})",
            )
        if queue is not None and queue not in gf.QUEUES:
            raise BadInput(
                f"unknown queue {queue!r}",
                options=list(gf.QUEUES),
                next="get(kind='gripe', id='/queue', args={'queue': 'in-build'})",
            )
        refs = self.store.list_refs(
            kind=self.kind,
            meta_has=gf.META_KEY,
            exclude_tags=sorted(_TERMINAL_STATUS_TAGS),
            order_by="updated_desc",
            limit=2000,
        )
        states = gf.bulk_states(self.store, refs)
        if which == "/queue":
            titles = {r.id: r.title for r in refs}
            return Response(
                body=gf.render_queue(
                    states, titles, queue=queue, host=host, batch=batch
                )
            )
        if queue is not None or host is not None:
            raise BadInput(
                "id='/batch' takes args={'batch': '<slug>'} only; queue/host "
                "filters belong to '/queue'",
                next="get(kind='gripe', id='/queue', args={'queue': ..., 'host': ...})",
            )
        if batch is None:
            return Response(body=gf.render_batches(states))
        members = [s for s in states if s.batch == batch]
        bodies: dict[int, str] = {}
        comments: dict[int, list[tuple[int, str]]] = {}
        by_id = {r.id: r for r in refs}
        for s in members:
            ref = by_id[s.ref_id]
            blocks = self.store.chunks.list_chunks_for_ref(ref.id)
            bodies[ref.id] = next(
                (b.text for b in blocks if b.chunk_kind == _BODY_KIND), ref.title
            )
            hidden = tl.hidden_comments(ref)
            comments[ref.id] = [
                (b.ord, b.text)
                for b in blocks
                if b.chunk_kind == _COMMENT_KIND and b.ord not in hidden
            ]
        return Response(body=gf.render_batch(batch, states, bodies, comments))

    def _dup_lines(self, ref: Ref) -> list[str]:
        """Pipeline v2 slice 2 card lines (``docs/backlog/
        gripe-pipeline-v2.md`` §Data model): ``duplicates: x N`` =
        OUTBOUND ``supersedes`` links — this keeper supersedes N parked
        duplicates (the repo convention is source supersedes target) —
        and ``potential duplicate parents: gr<id>(<sim>)`` off
        ``meta.dup_parents`` (written by ``workers/gripe_parents``;
        absent/empty/malformed shows nothing — a bad entry never takes
        the card down)."""
        out: list[str] = []
        n = len(self.store.links_for(ref.id, direction="out", relation="supersedes"))
        if n:
            out.append(f"duplicates: x {n}")
        parents = (ref.meta or {}).get("dup_parents")
        if isinstance(parents, list) and parents:
            from precis.utils.handle_registry import try_format

            shown: list[str] = []
            for p in parents:
                try:
                    handle = try_format("gripe", int(p["id"])) or str(p["id"])
                    shown.append(f"{handle}({float(p['sim']):.2f})")
                except (KeyError, TypeError, ValueError):
                    continue
            if shown:
                out.append(f"potential duplicate parents: {' '.join(shown)}")
        return out

    def _header_lines(self, ref: Ref, tags: list[Tag]) -> list[str]:
        lines = [f"# {self._sense()} {ref.id}"]
        if ref.set_by:
            lines.append(f"filed by: {ref.set_by}")
        if tags:
            lines.append("tags: " + " ".join(str(t) for t in tags))
        return lines

    def _render_ref_view(
        self,
        ref: Ref,
        tags: list[Tag],
        *,
        view: str,
        comment: int | str | None,
        comments: int | str | None,
    ) -> str:
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        body = next((b.text for b in blocks if b.chunk_kind == _BODY_KIND), ref.title)
        cblocks = [b for b in blocks if b.chunk_kind == _COMMENT_KIND]
        head = "\n".join(self._header_lines(ref, tags))
        if view == "body":
            return f"{head}\n\n{body}"
        if view == "full":
            # The bounded complete read (gr514350): the body clips
            # naming view='body'; the timeline fills the rest of
            # _FULL_VIEW_CHARS with whole comments and names the
            # args={'comments': 'A-B'} window holding what didn't fit.
            body_shown = tl.clip_middle(
                body,
                _BODY_HEAD_CHARS,
                f"get(kind='gripe', id={ref.id}, view='body')",
            )
            budget = _FULL_VIEW_CHARS - len(head) - len(body_shown)
            w = self._comment_window(ref, cblocks, comment, comments)
            if w.only is not None:
                section = tl.render_comment_view(
                    ref, cblocks, kind=self.kind, only=w.only
                )
            elif budget >= _MIN_COMMENT_PAGE:
                section = tl.render_comment_view(
                    ref,
                    cblocks,
                    kind=self.kind,
                    last=w.last,
                    lo=w.lo,
                    hi=w.hi,
                    budget=budget,
                )
            else:
                section = self._comments_elided_line(ref, cblocks, w)
            if (comment is not None or comments is not None) and not section:
                section = (
                    f"no comments — put(kind='gripe', id={ref.id}, text='...') adds one"
                )
            return f"{head}\n\n{body_shown}" + (f"\n\n{section}" if section else "")
        # view == "comments"
        w = self._comment_window(ref, cblocks, comment, comments)
        section = tl.render_comment_view(
            ref, cblocks, kind=self.kind, last=w.last, only=w.only, lo=w.lo, hi=w.hi
        )
        if not section:
            section = (
                f"no comments — put(kind='gripe', id={ref.id}, text='...') adds one"
            )
        return f"{head}\n\n{section}"

    @staticmethod
    def _comments_elided_line(ref: Ref, cblocks: list[Any], w: _Window) -> str:
        """The 'full'-page-overflow line: which comments, which call."""
        ords = sorted(
            c.ord
            for c in cblocks
            if (w.lo is None or c.ord >= w.lo) and (w.hi is None or c.ord <= w.hi)
        )
        if w.last is not None:
            ords = ords[-w.last :]
        if not ords:
            return ""
        span = f"{ords[0]}-{ords[-1]}"
        return (
            f"comments {span} elided — the body used the page; one call "
            f"reads them: get(kind='gripe', id={ref.id}, view='comments', "
            f"args={{'comments': '{span}'}})"
        )

    def _comment_window(
        self,
        ref: Ref,
        cblocks: list[Any],
        comment: int | str | None,
        comments: int | str | None,
    ) -> _Window:
        """Validate the ``comment``/``comments`` selectors into a window."""
        if comment is not None and comments is not None:
            raise BadInput(
                "pass args={'comment': K} or args={'comments': K}, not both",
                next=(
                    f"get(kind='gripe', id={ref.id}, view='comments', "
                    "args={'comments': 5})"
                ),
            )
        ords = sorted(c.ord for c in cblocks)
        if comment is not None:
            try:
                number = int(comment)
            except (TypeError, ValueError):
                raise BadInput(
                    f"args['comment'] must be a comment number, got {comment!r}",
                    next=(
                        f"comments on {self._sense()} {ref.id}: {ords}"
                        if ords
                        else f"{self._sense()} {ref.id} has no comments"
                    ),
                ) from None
            if number not in ords:
                raise BadInput(
                    f"{self._sense()} {ref.id} has no comment {number}",
                    next=(
                        f"comments on {self._sense()} {ref.id}: {ords}"
                        if ords
                        else f"{self._sense()} {ref.id} has no comments"
                    ),
                )
            return _Window(only=number)
        if comments is not None:
            text = str(comments).strip()
            lo_s, sep, hi_s = text.partition("-")
            # 'A-B' / 'A-' is an ord window (gr515093): the middle
            # comments a folded page hid, reachable in one call.
            if sep and lo_s.isdigit() and (hi_s.isdigit() or hi_s == ""):
                lo, hi = int(lo_s), int(hi_s) if hi_s else None
                if lo < 1 or (hi is not None and hi < lo):
                    raise BadInput(
                        "args['comments'] range must be 'A-B' with "
                        f"1 <= A <= B, got {comments!r}",
                        next=(
                            f"comments on {self._sense()} {ref.id}: {ords}"
                            if ords
                            else f"{self._sense()} {ref.id} has no comments"
                        ),
                    )
                if not any(
                    c.ord >= lo and (hi is None or c.ord <= hi) for c in cblocks
                ):
                    raise BadInput(
                        f"{self._sense()} {ref.id} has no comments in range {text!r}",
                        next=(
                            f"comments on {self._sense()} {ref.id}: {ords}"
                            if ords
                            else f"{self._sense()} {ref.id} has no comments"
                        ),
                    )
                return _Window(lo=lo, hi=hi)
            try:
                count = int(text)
            except (TypeError, ValueError):
                raise BadInput(
                    f"args['comments'] must be a count or 'A-B' range, "
                    f"got {comments!r}",
                    next=(
                        f"get(kind='gripe', id={ref.id}, view='comments', "
                        "args={'comments': 5})"
                    ),
                ) from None
            if count < 1:
                raise BadInput(
                    f"args['comments'] must be >= 1, got {count}",
                    next=(
                        f"get(kind='gripe', id={ref.id}, view='comments', "
                        "args={'comments': 5})"
                    ),
                )
            return _Window(last=count)
        return _Window()

    def _render_create_ack(
        self,
        ref_id: int,
        *,
        has_thread: bool = False,
        short_title: str | None = None,
        title_src_len: int | None = None,
    ) -> Response:
        # Unified shape (broad-pass finding #9): kwarg spelling +
        # uppercase STATUS axis + TOON Next: trailer. Previous inline-
        # prose breadcrumb was harder to paste and bunched two
        # very different actions onto one line.
        from precis.utils.next_block import render_next_section

        body = f"created {self.kind} id={ref_id} (STATUS:open)."
        thread_hint: list[tuple[str, str]] = (
            []
            if has_thread
            else [
                (
                    f"tag(kind={self.kind!r}, id={ref_id}, add=['thread:<slug>'])",
                    "name its owning thread (docs/backlog/threads/<slug>.md); "
                    "a wrong slug is refused with the list",
                )
            ]
        )
        if short_title is not None:
            # Name the cut (gr514360): a truncation the filer can't see is
            # a fragment they can't fix — the size of the lost tail says
            # how much was dropped. The body keeps the full text either way.
            body += (
                f'\ntitle shortened to "{short_title}" '
                f"({title_src_len}→{len(short_title)} chars; the body keeps "
                f"the full text); edit(kind={self.kind!r}, id={ref_id}, "
                "args={'title': '<new title>'}) to change it."
            )
        body += render_next_section(
            [
                *thread_hint,
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


#: Comment chunk_kind per referrer kind — only kinds with a comment
#: timeline get the close notice; others are evaluated (unblock)
#: without a comment write.
_CLOSE_COMMENT_KIND: dict[str, str] = {
    "todo": "todo_comment",
    "gripe": "gripe_comment",
}

#: Inbound relations the close cascades to (gr520237 ask 2).
_CLOSE_CASCADE_RELS = ("blocked-by", "related-to", "supersedes", "fixes")


def gripe_close_cascade(
    store: Store, gripe_id: int, status_value: str, *, conn: Any
) -> None:
    """Notify everything that referenced or waited on a gripe that just
    closed (gr520237).

    Runs inside the closing ``tag`` transaction so the notes land
    atomically with the close. For each referrer — an inbound
    ``blocked-by``/``related-to``/``supersedes``/``fixes`` link, or an
    OPEN ``waiting-for:gr<id>`` tag — append a comment naming the close
    and the next step; a todo whose last live blocked-by edge cleared
    flips ``STATUS:blocked`` → ``STATUS:open`` (doable picks it up).
    Quest need-gripes need nothing here: ``open_tool_gripes`` filters
    live statuses, so they drop out on the next tick automatically.
    Idempotent per referrer: a referrer with ANY comment already
    carrying this close note is skipped, so a ``done`` → ``wontfix``
    re-close doesn't double-post even after an intervening comment.
    A referrer already at a terminal STATUS (per
    ``BLOCKER_TERMINAL_STATUSES`` — done/won't-do todo, closed gripe,
    abandoned quest) is settled and gets no note.
    """
    row = conn.execute(
        "SELECT c.text FROM chunks c WHERE c.ref_id = %s "
        "AND c.chunk_kind = 'gripe_comment' ORDER BY c.ord DESC LIMIT 1",
        (gripe_id,),
    ).fetchone()
    gist = " ".join((row[0] if row else "").split())[:120]
    close_note = f"gr{gripe_id} closed ({gist or status_value})"

    referrers = conn.execute(
        "SELECT l.src_ref_id, l.relation, r.kind FROM links l "
        "JOIN refs r ON r.ref_id = l.src_ref_id "
        "WHERE l.dst_ref_id = %s AND l.relation = ANY(%s) "
        "AND r.retired_at IS NULL",
        (gripe_id, list(_CLOSE_CASCADE_RELS)),
    ).fetchall()
    waiters = conn.execute(
        "SELECT rt.ref_id, 'waiting-for', r.kind FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "JOIN refs r ON r.ref_id = rt.ref_id "
        "WHERE t.namespace = 'OPEN' AND t.value = %s "
        "AND r.retired_at IS NULL",
        (f"waiting-for:gr{gripe_id}",),
    ).fetchall()

    from precis.todo import BLOCKER_TERMINAL_STATUSES, blocker_terminal_sql

    for src_id, relation, kind in {tuple(r) for r in referrers + waiters}:
        cur = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t "
            "ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s "
            "AND t.namespace = 'STATUS' LIMIT 1",
            (src_id,),
        ).fetchone()
        referrer_status = str(cur[0]) if cur is not None else None
        if referrer_status in BLOCKER_TERMINAL_STATUSES:
            continue
        # Next step: a blocked-by referrer with no live blockers left is
        # unblocked; everything else is a re-evaluation note.
        unblocked = False
        if relation == "blocked-by":
            live = conn.execute(
                "SELECT count(*) FROM links l JOIN refs b "
                " ON b.ref_id = l.dst_ref_id "
                "WHERE l.src_ref_id = %s AND l.relation = 'blocked-by' "
                " AND b.retired_at IS NULL "
                " AND COALESCE((SELECT t.value FROM ref_tags rt "
                "   JOIN tags t ON t.tag_id = rt.tag_id "
                "   WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' "
                "   LIMIT 1), 'open') NOT IN " + blocker_terminal_sql(),
                (src_id,),
            ).fetchone()
            unblocked = live is not None and int(live[0]) == 0
        comment_kind = _CLOSE_COMMENT_KIND.get(kind)
        if comment_kind is not None:
            already = conn.execute(
                "SELECT 1 FROM chunks c WHERE c.ref_id = %s "
                "AND c.chunk_kind = %s AND c.text LIKE %s LIMIT 1",
                (src_id, comment_kind, f"gr{gripe_id} closed%"),
            ).fetchone()
            if already is None:
                nxt = "unblocked" if unblocked else "re-evaluate"
                (ord_,) = conn.execute(
                    "SELECT coalesce(max(ord), 0) + 1 FROM chunks WHERE ref_id = %s",
                    (src_id,),
                ).fetchone()
                store.chunks.insert_chunks(
                    src_id,
                    [
                        ChunkInsert(
                            ord=int(ord_),
                            text=f"{close_note} — next: {nxt}",
                            meta={"chunk_kind": comment_kind},
                        )
                    ],
                    conn=conn,
                )
        if unblocked and kind == "todo" and referrer_status == "blocked":
            store.add_tag(
                src_id,
                Tag.parse_strict("STATUS:open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
