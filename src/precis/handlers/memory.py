"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Subclasses :class:`NumericRefHandler` — the shared
CRUD shape lives in one place across memory / todo / gripe / anki / conv.

Fisheye batches live in ``eye`` because the dispatcher routes ``extent=``
there before ``get``. They reuse scalar eyes and the plain batch ID policy,
keeping each memory's own context and whole blocks in caller order.

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
    - get(id=N, view='processing') — current-revision processing evidence;
                                     check before testing recall
    - get(id='/recent')            — list recent memories
    - get(id=N, view='fisheye'|'fisheye+1hop'|'fisheye+2hop'[+recall])
                                   — the walk: link rings via
                                     ``eye_render.render_eye``; ``+recall``
                                     fills k from the focus's ``SPACE:``
                                     tag inside the semantic search
    - get(id=N, view='environment', args={'hops': 2})
                                   — the graph neighbourhood (gr570722):
                                     per-node stats + edges + computed
                                     shape smells + session co-read pairs;
                                     ``args={'mermaid': true}`` adds a sketch
                                     (:mod:`precis.handlers._memory_environment`)
    - search(view='shape', args={'under': 'me<hub>'})
                                   — the same smells rolled up over a scope
    - search(q=...)                — hybrid search over the body chunks
    - search(q=..., view='index')  — recall: each hit as the session-start
                                     bullet (handle, mirror filename, hook),
                                     shared with ``precis memory index --q``;
                                     ``view='count'`` forwards to the
                                     generic search like no view
    - search(view='reads')         — paged lifetime read/size inventory;
                                     one SELECT, no read-counter updates
                                     (:mod:`precis.handlers._memory_reads`)
    - search(tags=['ruling'], view='rulings')
                                   — ruling nodes grouped by status
                                     (``utils.memory_rulings``; gr548835);
                                     absorbed/archived/retired rulings drop
                                     out of the default and index recall

Attribution gate (``handlers/_attribution.py``): every body write
(create / edit / supersede) checks that a unit-bearing number next to a
citation token appears in the cited text. A miss tags the memory
``AUDIT:ungrounded-number`` (system-set, cleared by a clean rewrite) and
adds an advisory line to the ack; ``PRECIS_MEMORY_ATTRIBUTION_GATE=reject``
refuses the write instead. A lookup error fails open and changes no tag.
``scripts/memory-attribution-audit`` retro-applies the same check.

Shape checks (``handlers/_memory_shape.py``) run alongside attribution on
agent put/edit, but are always advisory; non-agent puts skip them. Each
checked code maintains its own system-owned ``AUDIT`` tag; a clean rewrite
clears it. Hand-set flags retain their owner even on a matching finding.
Preview responses
include proposed titles for grab-bags. Unavailable checks leave flags
untouched; tag maintenance has a savepoint so failures cannot poison the
body write.

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
refuses with ``Upstream`` naming the live lock holder(s) resolved by
the lock manager's own wait queue (gr631568, td632689) + a ``next=``
naming the re-read.

Retirement scans live memory prose inside its transaction, independently
of mention edges. Current mentions require a successor or explicit force;
dated history is reported but exempt. The capped receipt anchors edits on
sentences, since replacing a handle alone cannot correct an obsolete claim.
Successors are full handles, including file-backed ``md:path~section``;
the runtime follows those addresses while retired view preserves the record.

Retitles preserve the legacy echo decision in fingerprinted ref metadata.
Rows retitled before this fix without a stamp still show their old prefix.
Full replacements store exact prose, including a lead equal to the new header. Complete
displayed-body anchors replace the hidden prefix too, while explicit stored
title anchors remain compatible. Raw snapshots still guard concurrent writes
and populate audit history; generated entity footers never enter edit buffers.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from precis._result_budget import result_context_cap, row_size_cell, size_cell
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
from precis.handlers._memory_link_receipt import (
    link_effect_receipt,
    preview_link_effects,
    unresolved_mention_notice,
)
from precis.handlers._memory_shape import ShapeAudit, audit_shape
from precis.handlers._memory_smells import (
    count_smell_nodes,
    render_memory_smells,
    render_smell_rollup,
)
from precis.handlers._mode_help import require_mode
from precis.handlers._numeric_ref import (
    _BASE_VIEWS,
    BATCH_VIEW_PAGE_CHARS,
    NumericRefHandler,
    _tag_names_axis,
)
from precis.handlers._tag_redirect import redirect_long_tag_values
from precis.handlers.plaintext import _require_find_and_text
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store._search_cut import semantic_fold_distance
from precis.store.types import ActorSlug, ChunkInsert, Ref
from precis.utils import handle_registry, search_trace
from precis.utils.db_retry import retry_locked
from precis.utils.edit_resolve import (
    EditOp,
    EditResult,
    apply_edit,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.link_tag_ops import merge_link_reason
from precis.utils.link_target import parse_link_target
from precis.utils.match_all import fold_match_all_q
from precis.utils.memory_fusion import GOTCHAS_TAG, FusionFlags
from precis.utils.memory_fusion import rerank as rerank_fused
from precis.utils.memory_hubs import (
    NEW_MEMORY,
    MemoryDraft,
    band_factors,
    draft_link_hints,
    draft_tag_values,
    draft_thread_dup_hint,
    link_hints,
    narrowing_hints,
    sub_hub_hints,
    thread_dup_hint,
)
from precis.utils.memory_rulings import (
    META_KEYS as _RULING_META_KEYS,
)
from precis.utils.memory_rulings import (
    RULING_TAG,
    count_rulings,
    drop_legacy_status_tags,
    hidden_ids,
    render_rulings,
    validate_ruling_meta,
)
from precis.utils.memory_spec import spec_root_advisory, spec_root_slug
from precis.utils.next_block import render_next_section
from precis.utils.search_merge import SearchHit
from precis.utils.secret_scan import mask_secrets
from precis.utils.timeutil import as_utc
from precis.workers.embed import nudge_fresh_embed

#: Heading line that opens a memory-body section — the ``~<slug>``
#: selector grammar (``me<id>~<section>``) splits on these.
_SECTION_HEAD_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)

log = logging.getLogger(__name__)

# Batch editing keeps only each child's first receipt line. Collect advisory
# ids call-locally so its outer receipt retains one processing hint, without
# repeating the status queries or sharing mutable state between requests.
_PROCESSING_HINT_IDS: ContextVar[list[int] | None] = ContextVar(
    "memory_processing_hint_ids", default=None
)

#: Derived tag the attribution gate maintains (a number next to a citation
#: that the cited text does not carry). See :mod:`precis.handlers._attribution`.
_AUDIT_UNGROUNDED = Tag.closed("AUDIT", AUDIT_VALUE)

#: Bytes paged listings (``view='members'``, ``view='index'`` pages)
#: reserve for the header + ``next:``/note lines the dispatcher may
#: prepend, so the emitted page itself always fits the binding cap.
_PAGED_FOOTER_BYTES = 400

#: Write-retry budget for a memory write (gr631568): a fresh or
#: just-edited memory is processed in the background — embed / keywords /
#: summary, each claiming at the head of the interactive lanes the write
#: itself nudged — and those stages cycle short write locks on the same
#: rows for tens of seconds. ``retry_locked``'s default 4 attempts
#: (~15-20 s under prod's ``lock_timeout=5s``) lost that race outright on
#: a node only minutes old; 8 attempts roughly doubles the window an
#: interactive edit rides out before the refusal is earned.
_WRITE_LOCK_ATTEMPTS = 8

#: Row-lock probes replaying the row locks a memory body/meta write
#: takes on this node's rows, in the order the write reaches them
#: (gr631568, reworked for td632689). Granted row locks never reach
#: ``pg_locks`` — they live in the tuple header, and a raw ``xmax``
#: cannot say whether the number it holds is an xid or a multixact:
#: that flag lives in ``t_infomask``, which no SQL-visible column
#: exposes, and the two ID spaces overlap, so guessing by lookup
#: success can name a backend that has nothing to do with the row.
#: Instead a dedicated connection asks for each lock itself: ``FOR
#: UPDATE`` conflicts with every row-lock mode including the ``FOR KEY
#: SHARE`` a worker's foreign-key write takes, and when it waits, the
#: lock manager itself resolves the holder — a shared lock's multixact
#: expands to its live member xids, a stale ``xmax`` from a finished
#: transaction blocks nobody — so every backend the wait names really
#: holds the lock.
_ROW_LOCK_PROBES: tuple[tuple[str, str], ...] = (
    ("refs", "SELECT 1 FROM refs WHERE ref_id = %(ref)s FOR UPDATE"),
    ("chunks", "SELECT 1 FROM chunks WHERE ref_id = %(ref)s FOR UPDATE"),
    ("ref_tags", "SELECT 1 FROM ref_tags WHERE ref_id = %(ref)s FOR UPDATE"),
    (
        "links",
        "SELECT 1 FROM links WHERE src_ref_id = %(ref)s OR dst_ref_id = %(ref)s "
        "FOR UPDATE",
    ),
    (
        "chunk_claims",
        "SELECT 1 FROM chunk_claims cc "
        "JOIN chunks c ON c.chunk_id = cc.chunk_id "
        "WHERE c.ref_id = %(ref)s FOR UPDATE OF cc",
    ),
    (
        "chunk_embeddings",
        "SELECT 1 FROM chunk_embeddings ce "
        "JOIN chunks c ON c.chunk_id = ce.chunk_id "
        "WHERE c.ref_id = %(ref)s FOR UPDATE OF ce",
    ),
    (
        "chunk_summaries",
        "SELECT 1 FROM chunk_summaries cs "
        "JOIN chunks c ON c.chunk_id = cs.chunk_id "
        "WHERE c.ref_id = %(ref)s FOR UPDATE OF cs",
    ),
)

#: The table-level half of the same probe: ROW EXCLUSIVE is the mode
#: the write's INSERT/DELETE takes, so only the modes that genuinely
#: conflict with it (SHARE and above) can hold it up — a
#: ``ShareUpdateExclusive`` holder such as a vacuum never makes the
#: probe wait and can no longer be reported as the blocker (td632689).
_TABLE_LOCK_PROBE_SQL = (
    "LOCK TABLE refs, chunks, ref_tags, links, ref_events, "
    "chunk_claims, chunk_embeddings, chunk_summaries, tags "
    "IN ROW EXCLUSIVE MODE"
)

#: Per-statement wait bound on the lock-taker: long enough for the
#: observer's poll to see the queued request, short enough that
#: replaying every lock the write needed stays a bounded diagnostic
#: on a path that has already burned its retry budget.
_LOCK_PROBE_TIMEOUT_MS = 400

#: Observer cadence and total budget for the probe pair.
_LOCK_PROBE_POLL_S = 0.025
_LOCK_PROBE_BUDGET_S = 8.0

#: What the lock-taker backend is currently waiting on, joined to the
#: backends blocking it. ``pg_blocking_pids`` walks the wait queue with
#: the lock manager's own conflict matrix — for a shared row lock it
#: reports the live members of the multixact, which is the resolution
#: reading ``xmax`` could only guess at.
_LOCK_WAIT_SQL = """
    SELECT l.locktype, l.mode, l.relation::regclass::text AS rel,
           l.transactionid::text AS xid,
           unnest(pg_blocking_pids(l.pid)) AS blocker
      FROM pg_locks l
     WHERE l.pid = %(pid)s AND NOT l.granted
"""

#: backend pid → its live session row. ``query`` resolves for same-role
#: backends, so a worker stage's claim/write SQL shows verbatim.
_BLOCKER_BACKEND_SQL = """
    SELECT a.pid, a.usename,
           a.application_name AS app, a.backend_type AS btype,
           a.state::text AS state,
           COALESCE(a.wait_event_type || ':' || a.wait_event, '-') AS wait,
           EXTRACT(epoch FROM now() - a.xact_start)::int AS age_s,
           left(a.query, 120) AS query
      FROM pg_stat_activity a
     WHERE a.pid = ANY(%(pids)s)
"""

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
#: index text of a ``SPACE:repo-dev`` memory (``precis memory index``);
#: ``expires`` is the node's declared expiry instant (the garden's
#: review-due signal — and on ``SPACE:scratch`` the auto-retire override,
#: gr568398).
_META_ALLOWED = ("hook", "type", "expires")

#: ``meta.expires`` duration grammar — mirrors the alias handler's
#: ``expires=`` (``'7d'``/``'12h'``/``'30m'``/``'2w'``), resolved at write
#: into the absolute instant the stored meta carries.
_EXPIRES_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")
_EXPIRES_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

#: ``args={'scratch_for': '<owner>'}`` on ``put`` — the task-scratch pair
#: (gr605669): ``SPACE:scratch`` plus the ``scratch-for:<owner>`` tag a
#: ring ``tags`` filter or ``search(tags=[…])`` scopes to one task. The
#: owner is a handle-shaped id (``td575121``, ``gr605669`` — the task
#: whose agents share the scratch); a ``scratch-for:`` prefix is
#: tolerated and stripped, and case folds the way open tags do.
_SCRATCH_OWNER_RE = re.compile(r"^(?:scratch-for:)?([a-z]{2}\d+)$")


def _stored_tag_form(tag: Any) -> str:
    """The canonical string ``Tag.parse_strict`` stores for ``tag`` — the
    ``OPEN:`` display sentinel stripped, open tags case-folded — or the
    raw spelling when it won't parse (the downstream yield redirect may
    still rewrite it, and the same ``BadInput`` surfaces there)."""
    try:
        return str(Tag.parse_strict(tag, kind="memory"))
    except BadInput:
        return tag if isinstance(tag, str) else str(tag)


def _scratch_for_tags(
    tags: list[str] | None,
    untags: list[str] | None,
    scratch_for: Any,
) -> list[str]:
    """``tags`` plus the task-scratch pair ``scratch_for`` spells —
    ``SPACE:scratch`` and ``scratch-for:<owner>`` — deduped. The pair is
    one partition: a supplied ``SPACE:`` other than scratch, a different
    ``scratch-for:`` owner, or an ``untags`` touching either contradicts
    it and is refused. The checks compare each tag's *stored* form, not
    its raw spelling — ``OPEN:scratch-for:<other>`` or
    ``Scratch-For:<other>`` would otherwise slip a second owner past a
    raw-string test and land it beside ``scratch_for``'s (td607021)."""
    m = _SCRATCH_OWNER_RE.match(str(scratch_for).strip().lower())
    if m is None:
        raise BadInput(
            f"args['scratch_for'] must be a handle-shaped owner like "
            f"'td575121' or 'gr605669', got {scratch_for!r}",
            next="put(kind='memory', text='…', args={'scratch_for': 'td<N>'})",
        )
    owner = m.group(1)
    out = list(tags or [])
    stored = [_stored_tag_form(t) for t in out]
    foreign_space = [
        s for s in stored if s.startswith("SPACE:") and s != "SPACE:scratch"
    ]
    if foreign_space:
        raise BadInput(
            f"args['scratch_for'] sets SPACE:scratch — it can't combine "
            f"with the {foreign_space} tag(s) already passed",
            next="drop the SPACE tag (the scratch pair is the partition), "
            "or pass tags=[...] without scratch_for",
        )
    other_owner = [
        s
        for s in stored
        if s.startswith("scratch-for:") and s != f"scratch-for:{owner}"
    ]
    if other_owner:
        raise BadInput(
            f"args['scratch_for'] sets scratch-for:{owner} — the tags "
            f"already name {other_owner}; one task owns a scratch node",
            next="drop the conflicting scratch-for tag, or drop scratch_for",
        )
    if untags:
        stored_untags = [_stored_tag_form(t) for t in untags]
        clash = [
            s
            for s in stored_untags
            if s == "SPACE:scratch" or s.startswith("scratch-for:")
        ]
        if clash:
            raise BadInput(
                f"args['scratch_for'] sets the task-scratch pair; untags "
                f"{clash} contradicts it",
                next="drop scratch_for, or drop the clashing untags",
            )
    for tag in ("SPACE:scratch", f"scratch-for:{owner}"):
        if tag not in stored:
            out.append(tag)
            stored.append(tag)
    return out


def _expires_meta(value: Any) -> str:
    """Normalize a writable ``meta.expires`` to ``YYYY-MM-DDTHH:MM:SSZ`` —
    an ISO date/datetime, a ``datetime``, or a ``<n>[mhdw]`` duration from
    now. Anything else is a BadInput: a stored value the sweep and garden
    cannot parse would silently act as no expiry at all."""
    when = as_utc(value)
    if when is None and isinstance(value, str):
        m = _EXPIRES_DURATION_RE.match(value.strip().lower())
        if m:
            when = datetime.now(UTC) + timedelta(
                **{_EXPIRES_DURATION_UNITS[m.group(2)]: int(m.group(1))}
            )
    if when is None:
        raise BadInput(
            f"meta['expires'] must be an ISO date or a duration like '30d', "
            f"got {value!r}",
            next="meta={'expires': '7d'} or meta={'expires': '2026-12-31'}",
        )
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


#: Keys one ``ops=[{...}]`` dict may carry on ``edit`` — the single-op
#: anchored vocabulary plus ``op`` for the op kind (the
#: ``ops=[{'op': ...}]`` convention the structure kind already uses).
_OPS_KEYS = frozenset(
    {"op", "find", "text", "before", "after", "where", "match", "nth"}
)
_OPS_KINDS = ("find-replace", "insert", "append")
_MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _validate_meta(
    meta: Any,
    *,
    ruling: bool = False,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check a caller's ``meta=`` and return the cleaned patch.

    ``hook`` is a non-empty single-line ``str`` (stripped); ``type`` is one of
    user/feedback/project/reference (the mirror export's ``metadata.type``).
    A node tagged ``ruling`` (``ruling=True``) also takes ``ruled_by`` /
    ``ruled_on`` / ``status`` / ``review_by``
    (:func:`precis.utils.memory_rulings.validate_ruling_meta`; ``existing`` =
    the node's meta on an edit, ``None`` on a create, which stamps the
    defaults). Anything else raises :class:`BadInput` naming the allowed keys.
    """
    example = "meta={'hook': 'one-line index text'}"
    ruling_note = f"; a node tagged {RULING_TAG!r} also takes {list(_RULING_META_KEYS)}"
    if not isinstance(meta, dict):
        raise BadInput(
            f"meta= must be a dict, got {type(meta).__name__}; "
            f"allowed keys: {list(_META_ALLOWED)}{ruling_note}",
            next=example,
        )
    unknown = sorted(
        str(k) for k in meta if k not in _META_ALLOWED and k not in _RULING_META_KEYS
    )
    if unknown:
        raise BadInput(
            f"meta= key(s) {unknown} not writable on kind='memory'; "
            f"allowed keys: {list(_META_ALLOWED)}{ruling_note}",
            next=example,
        )
    ruling_keys = {k: meta[k] for k in _RULING_META_KEYS if k in meta}
    if ruling_keys and not ruling:
        raise BadInput(
            f"meta= key(s) {sorted(ruling_keys)} only apply to a node tagged "
            f"{RULING_TAG!r}, and this one is not",
            next="put(kind='memory', tags=['ruling'], text='…', "
            "meta={'ruled_by': 'Reto'}) for a new ruling, or "
            "tag(kind='memory', id=N, add=['ruling']) first",
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
    if "expires" in meta:
        out["expires"] = _expires_meta(meta["expires"])
    if ruling:
        out.update(validate_ruling_meta(ruling_keys, existing=existing))
    return out


def _ruling_ack(meta: dict[str, Any]) -> str:
    """The create ack's line naming the ruling keys the write stamped."""
    stamped = ", ".join(f"{k}={meta[k]}" for k in _RULING_META_KEYS if k in meta)
    return f"\nruling meta: {stamped}" if stamped else ""


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
        modes=("process",),
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
    # Recall fusion (gr548265): rescue, recency prior, gotcha quota over the
    # fused pool — :meth:`_rerank_fused`, ``precis.utils.memory_fusion``.
    fused_rerank: ClassVar[bool] = True

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
        reason: str | None = None,
        **kw: Any,
    ) -> Response:
        """Selection records keep candidate rejection reasons on their edges.

        ``reason=`` is the kwarg spelling of ``meta={'reason': …}`` —
        folded in by :func:`merge_link_reason` (gr626896: the refusal
        that suggests ``reason='.'`` names a call that now works).
        """
        if id is None:
            raise BadInput(
                "memory link requires id=",
                next="link(kind='memory', id=<record>, target='kind:id', rel='...')",
            )
        if isinstance(id, list):
            if meta is not None or reason is not None:
                raise BadInput(
                    "link meta requires a single memory id",
                    next="link each selection record separately with its own meta.reason",
                )
            response = self._link_batch(id, target=target, mode=mode, rel=rel)
        else:
            response = self._link(
                id=id,
                target=target,
                mode=mode,
                rel=rel,
                meta=merge_link_reason(meta, reason, mode=mode),
            )
        try:
            # Inspect both ends after the write (and after a batch commits):
            # adding a part-of edge grows the target's child collection.
            ref_ids = {
                self._coerce_id(i) for i in (id if isinstance(id, list) else [id])
            }
            if target is not None:
                resolved = parse_link_target(
                    target, store=self.store, include_retired=mode == "remove"
                )
                if resolved.kind == "memory":
                    ref_ids.add(resolved.ref_id)
            hints = [
                hint for rid in sorted(ref_ids) for hint in sub_hub_hints(self, rid)
            ]
            body = response.body + render_next_section(hints)
            advisories = [
                adv
                for rid in sorted(ref_ids)
                if (adv := spec_root_advisory(self.store, rid)) is not None
            ]
            if advisories:
                body += "\n\n" + "\n".join(advisories)
            return replace(response, body=body)
        except Exception:
            log.debug("memory link ack hints skipped", exc_info=True)
            return response

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
        # ``view='members'``'s cursor args (``args={'offset': N,
        # 'depth': D}``) — declared so the extras gate accepts them.
        offset: int | str | None = None,
        depth: int | str | None = None,
        limit: int | str | None = None,
        event_id: int | str | None = None,
        # ``view='environment'``'s neighbourhood args
        # (``args={'hops': N, 'mermaid': true}``) — declared so the
        # extras gate accepts them (gr570722).
        hops: int | str | None = None,
        mermaid: bool | str | None = None,
        **_kw: Any,
    ) -> Response:
        # Log offsets count events, or payload characters when event_id
        # selects one event. Keep members' independent cursor contract.
        if depth is not None and (view != "members" or q is not None):
            raise BadInput(
                "depth= only applies to get(kind='memory', view='members')",
                next="get(kind='memory', id=N, view='members', args={'depth': 1})",
            )
        if any(v is not None for v in (hops, mermaid)) and (
            view != "environment" or q is not None
        ):
            raise BadInput(
                "hops=/mermaid= only apply to get(kind='memory', view='environment')",
                next="get(kind='memory', id=N, view='environment', args={'hops': 2})",
            )
        if offset is not None and (view not in {"members", "log"} or q is not None):
            raise BadInput(
                "offset= can only apply to memory view='members' or view='log'",
                next="get(kind='memory', id=N, view='log', args={'offset': 0})",
            )
        if any(v is not None for v in (limit, event_id)) and (
            view != "log" or q is not None
        ):
            raise BadInput(
                "limit=/event_id= only apply to memory view='log'",
                next="get(kind='memory', id=N, view='log')",
            )
        if view == "log" and any(v is not None for v in (limit, offset, event_id)):
            if (
                id is None
                or isinstance(id, list)
                or (isinstance(id, str) and (id.startswith("/") or "~" in id))
            ):
                raise BadInput(
                    "log arguments require one whole memory id",
                    next="get(kind='memory', id=N, view='log')",
                )
        if view == "log" and id is not None and not isinstance(id, list) and q is None:
            from precis.handlers._event_log_render import log_integer, render_event_log

            ref = self._resolve_live_ref(self._coerce_id(id))
            count = (
                50 if limit is None else log_integer(limit, "limit", ref.id, minimum=1)
            )
            off = (
                0
                if offset is None
                else log_integer(offset, "offset", ref.id, minimum=0)
            )
            selected = (
                None
                if event_id is None
                else log_integer(event_id, "event_id", ref.id, minimum=1)
            )
            return render_event_log(
                self.store,
                ref.id,
                newest_first=True,
                limit=min(count, 100),
                offset=off,
                event_id=selected,
            )
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
        if view == "processing":
            if not concrete or isinstance(id, list):
                raise BadInput(
                    "view='processing' requires one memory id",
                    next="get(kind='memory', id=N, view='processing')",
                )
            from precis.handlers._memory_processing import processing_report

            ref = self._resolve_live_ref(self._coerce_id(id))
            return Response(body=processing_report(self.store, ref.id).render())
        if view == "smells" and concrete:
            if isinstance(id, list):
                return self._get_batch(id, view=view)
            return render_memory_smells(
                self.store, self._resolve_live_ref(self._coerce_id(id))
            )
        if view == "environment" and concrete:
            if isinstance(id, list):
                raise BadInput(
                    "view='environment' requires one memory id",
                    next="get(kind='memory', id=N, view='environment', "
                    "args={'hops': 2})",
                )
            from precis.handlers import _memory_environment as env

            try:
                n_hops = int(str(hops)) if hops is not None else env._HOPS_DEFAULT
            except (TypeError, ValueError):
                n_hops = 0
            if not 1 <= n_hops <= env._HOPS_MAX:
                raise BadInput(
                    f"hops= must be an integer in 1..{env._HOPS_MAX}, got {hops!r}",
                    next="get(kind='memory', id=N, view='environment', "
                    "args={'hops': 3})",
                )
            ref = self._resolve_live_ref(self._coerce_id(id))
            return env.render_environment(
                self.store,
                ref,
                hops=n_hops,
                mermaid=mermaid not in (None, False, "false", "0", ""),
            )
        if view == "members" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_members(ref, offset=offset, depth=depth)
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
            legal = [
                "argument",
                "environment",
                "members",
                "processing",
                *EYE_LADDER,
                *_BASE_VIEWS,
            ]
            close = difflib.get_close_matches(view, legal, n=1)
            raise Unsupported(
                f"unknown view {view!r} for kind='memory'",
                options=legal,
                next=(
                    (f"did you mean {close[0]!r}? " if close else "")
                    + "view='argument' (kind:lemma/kind:inference proof tree) "
                    f"· {'|'.join(EYE_LADDER)}, optionally +recall (the eye) "
                    "· members (paged part-of listing) · processing (before recall) "
                    "· environment (graph neighbourhood + shape smells) "
                    "· links, log, raw (generic)"
                ),
            )
        resp = super().get(id=id, view=view, q=q, since=since, **_kw)
        if view is None and q is None and id is not None and not isinstance(id, list):
            resp = self._with_budget_outline(resp, id)
        return resp

    def eye(
        self,
        *,
        id: Any = None,
        extent: Any = "fisheye",
        q: str | None = None,
        ring: Any = None,
    ) -> Response:
        """Eye render; on a ``section:index`` hub the over-cap trailer also
        names the paged member listing (``view='members'``) as the way to
        read every member in cap-sized chunks (gr538449/gr538490)."""
        if isinstance(id, list):
            return self._eye_batch(id, extent=extent, q=q, ring=ring)
        ref = self._resolve_live_ref(self._coerce_id(id))
        resp = super().eye(id=int(ref.id), extent=extent, q=q, ring=ring)
        from precis.utils.eye_render import _is_hub

        if _is_hub(self.store, ref.id):
            hint = f"get(kind='memory', id={ref.id}, view='members')"
            resp = replace(resp, result_budget_hint=resp.result_budget_hint or hint)
        return resp

    def _eye_batch(
        self,
        ids: list[Any],
        *,
        extent: Any,
        q: str | None,
        ring: Any,
    ) -> Response:
        """Bounded scalar eyes, with plain batch validation and missing-ID blocks."""
        ref_ids = self._coerce_ids(ids)
        header = f"# {len(ref_ids)} memory (batch, extent={extent!r})\n\n"
        block_budget = BATCH_VIEW_PAGE_CHARS - len(header)
        selectors = f", q={q!r}" if q is not None else ""
        if ring is not None:
            selectors += f", args={{'ring': {ring!r}}}"
        blocks: list[str] = []
        used = len(header)
        for rid in ref_ids:
            try:
                resp = self.eye(id=rid, extent=extent, q=q, ring=ring)
                block = resp.body
                if len(block) > block_budget:
                    # The scalar read, not a hub's members hint: the cut text
                    # can be the body, which view='members' does not show.
                    whole_read = (
                        f"get(kind='memory', id={rid}, extent={extent!r}{selectors})"
                    )
                    trailer = f"… cut — full read: {whole_read}"
                    lines: list[str] = []
                    size = len(trailer)
                    for line in block.splitlines():
                        size += len(line) + 1
                        if size > block_budget:
                            break
                        lines.append(line)
                    block = "\n".join([*lines, trailer])
            except NotFound:
                block = f"me{rid}  not found"
            except Gone:
                block = f"me{rid}  deleted"
            if blocks and used + len(block) > BATCH_VIEW_PAGE_CHARS:
                break
            blocks.append(block)
            used += len(block) + 2
        body = header + "\n\n".join(blocks)
        rest = ref_ids[len(blocks) :]
        if rest:
            body += (
                f"\n\n{len(rest)} more — next: get(kind='memory', "
                f"id={rest!r}, extent={extent!r}{selectors})"
            )
        return Response(body=body, result_count=len(blocks))

    def _render_members(
        self,
        ref: Ref,
        *,
        offset: int | str | None,
        depth: int | str | None,
    ) -> Response:
        """``view='members'``: the ref's part-of subtree as index bullets.

        Emits whole lines until the context cap (gr538449's "members in
        chunks under the result cap"); ``args={'offset': N}`` resumes at
        the member after the last one shown — the footer's ``next:`` line
        carries the ready-made call. Rows sort by ref_id, not recency:
        band order shifts when a reader touches a member mid-walk, which
        would skip or repeat rows between pages. ``args={'depth': D}``
        bounds the walk (``1`` = direct members); default is the whole
        subtree, matching ``under=``'s scope.
        """
        from precis.cli.memory import bullet_line
        from precis.utils.eye_render import _first_body_lines

        try:
            off = int(str(offset)) if offset is not None else 0
        except (TypeError, ValueError):
            off = -1
        if off < 0:
            raise BadInput(
                f"offset= must be a non-negative integer, got {offset!r}",
                next=f"get(kind='memory', id={ref.id}, view='members')",
            )
        n_depth: int | None = None
        if depth is not None:
            try:
                n_depth = int(str(depth))
            except (TypeError, ValueError):
                n_depth = 0
            if n_depth < 1:
                raise BadInput(
                    f"depth= must be a positive integer, got {depth!r}",
                    next=(
                        "depth=1 lists direct members; omit it for the whole subtree"
                    ),
                )
        handle = handle_registry.try_format(self.kind, ref.id) or str(ref.id)
        found = self.store.fetch_refs_by_ids(
            sorted(self.store.part_of_descendants(ref.id, depth=n_depth))
        )
        ordered = sorted(
            (r for r in found.values() if r.kind == self.kind),
            key=lambda r: r.id,
        )
        non_memory = len(found) - len(ordered)
        total = len(ordered)
        if total == 0:
            return Response(body=f"{self._sense()} {handle} has no part-of members")
        if off >= total:
            return Response(
                body=(
                    f"no members past offset {off} — {total} total; "
                    f"next: get(kind='memory', id={ref.id}, view='members')"
                )
            )
        # Reserve room for the header + the continuation/footer lines so
        # the emitted page itself always fits the cap that binds reads.
        budget = max(200, result_context_cap() - _PAGED_FOOTER_BYTES)
        rest = ordered[off:]
        firsts = _first_body_lines(
            self.store, [r.id for r in rest if not (r.meta or {}).get("hook")]
        )
        sizes = self.store.chunks.body_byte_counts([r.id for r in rest])
        lines: list[str] = []
        used = 0
        for r in rest:
            line = bullet_line(
                r,
                cut_hooks=True,
                filename=True,
                fallback_hook=firsts.get(r.id, ""),
                size=row_size_cell(sizes.get(r.id, 0)),
            )
            cost = len(line.encode("utf-8")) + 1
            if lines and used + cost > budget:
                break
            lines.append(line)
            used += cost
        end = off + len(lines)
        depth_note = f" (depth ≤ {n_depth})" if n_depth else ""
        header = f"# memory {handle} — {total} part-of members{depth_note}"
        body = f"{header}\n" + "\n".join(lines)
        if end < total:
            depth_arg = f", 'depth': {n_depth}" if n_depth else ""
            body += (
                f"\n… {end} of {total} shown — next: get(kind='memory', "
                f"id={ref.id}, view='members', "
                f"args={{'offset': {end}{depth_arg}}})"
            )
        if non_memory:
            body += (
                f"\n… {non_memory} linked non-memory member"
                f"{'s' if non_memory != 1 else ''} not listed "
                f"(view='links' shows them)"
            )
        return Response(
            body=body,
            result_count=len(lines),
            result_ids=[int(r.id) for r in rest[: len(lines)]],
        )

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
            text = self._display_body(ref, body)
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

    @unresolved_mention_notice
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
        dry_run: bool | str = False,
        stages: list[str] | None = None,
        action: str | None = None,
        wait_s: float | None = None,
        scratch_for: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a memory. ``text=`` is the body prose, ``title=`` the short
        header. Emit the body first, then a title once its point is clear.

        ``dry_run=True`` writes nothing and returns the create ack's
        ``Next:`` block (``id='<new>'`` for the memory it would create), the
        nearest same-SPACE memories by embedding distance, and a proposed
        kind by rule (:mod:`precis.handlers._put_preview`).

        A handle mention that resolves to nothing live — a ``[meN]`` bracket
        naming a node that does not exist yet — is named in the ack instead
        of passing silently: no mention edge is created for it, and the
        notice says so at write time (gr570723).

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

        ``args={'scratch_for': 'td<N>'}`` stamps the task-scratch pair —
        ``SPACE:scratch`` + ``scratch-for:td<N>`` — in one call
        (gr605669): the node lives in the throwaway space and is owned
        by the task, so a sibling task's ring ``tags`` filter or
        ``search(tags=[…])`` never sees it.

        ``mode='process'`` schedules bounded current-revision embeddings;
        request/replay and refusal contracts live in :mod:`._memory_process`.
        """
        if mode == "process":
            from precis.handlers._memory_process import process_memory

            if id is None or isinstance(id, (list, bool)):
                raise BadInput(
                    "mode='process' requires exactly one memory id",
                    next="put(kind='memory', id=N, mode='process', args={'stages': ['embeddings']})",
                )
            extras = (
                text,
                title,
                tags,
                untags,
                link,
                unlink,
                rel,
                auto_refresh_days,
                rule,
                warrant,
                meta,
                scratch_for,
            )
            if (
                any(value is not None for value in extras)
                or normalize_dry_run(dry_run) is not None
                or _kw
            ):
                raise BadInput(
                    "mode='process' accepts only id, stages, action and wait_s",
                    next=f"put(kind='memory', id={id!r}, mode='process', args={{'stages': ['embeddings']}})",
                )
            ref = self._resolve_live_ref(self._coerce_id(id))
            return process_memory(
                self.store, ref.id, stages=stages, action=action, wait_s=wait_s
            )
        if any(value is not None for value in (stages, action, wait_s)):
            raise BadInput(
                "stages, action and wait_s require mode='process'",
                next="put(kind='memory', id=N, mode='process', args={'stages': ['embeddings']})",
            )
        if scratch_for is not None:
            tags = _scratch_for_tags(tags, untags, scratch_for)
        dry = normalize_dry_run(dry_run) is not None
        if id is not None and dry:
            raise BadInput(
                "dry_run= previews a new memory; put(id=...) attaches a link "
                "to an existing one",
                next="put(kind='memory', text='...', title='...', dry_run=True)",
            )
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
        is_ruling = any(
            isinstance(t, str) and t.strip() == RULING_TAG for t in tags or []
        )
        create_meta = (
            _validate_meta(meta or {}, ruling=is_ruling)
            if meta is not None or is_ruling
            else {}
        )
        for key, value in (("title", title), ("rule", rule), ("warrant", warrant)):
            if isinstance(value, str) and value.strip():
                create_meta[key] = value.strip()
        if "hook" in create_meta:
            create_meta["hook"] = mask_secrets(create_meta["hook"])
        if dry:
            self._reject_mutating_put(
                id=None, mode=mode, untags=untags, unlink=unlink, rel=rel, link=link
            )
            return self._put_dry_run(
                text=text, tags=tags, link=link, rel=rel, put_meta=create_meta
            )
        resp = super().put(
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
        if is_ruling:
            resp = replace(resp, body=resp.body + _ruling_ack(create_meta))
        return resp

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
        return self._with_processing_hint(
            n,
            Response(
                body=body
                + f"\nlinked via link(); next time use link(kind='memory', id={n}, "
                "target=..., rel=...)"
            ),
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
            for k in ("rule", "warrant", "hook", "type", "expires", *_RULING_META_KEYS)
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
        shape = None
        if set_by == "agent":
            shape = self._shape_audit(
                title=title,
                body=body,
                tags=all_tag_strs,
                meta=meta,
                has_evidence=target is not None
                and relation in {"cites", "derived-from"},
            )
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
            self._apply_shape_tags(ref.id, shape, conn=conn)
            if self.autolink_mentions:
                facts_deferred = self._sync_mention_links(ref.id, body, conn=conn)
            self.store.chunks.sync_header_card(ref.id, conn=conn)
            # Embed the new body + header card within seconds so a split's
            # children are recallable immediately (gr563669).
            nudge_fresh_embed(self.store, conn=conn, reason="put memory")
        # Attribute the new memory's body chunk to the current agent run
        # (Slice B provenance) — a no-op unless PRECIS_CURRENT_AGENTLOG is
        # set (e.g. the dream pass), so a dream's own memories walk back to
        # the tick that wrote them. After the tx above commits, so the
        # attribution query sees the just-inserted chunk row.
        from precis import agentlog

        agentlog.touch_from_env(self.store, chunk_ids=[b.id for b in body_blocks])
        ack = self._with_first_line_nudge(self._render_create_ack(ref.id), title)
        ack = self._facts_note(ack, facts_deferred)
        ack = self._with_attribution_advisory(ack, misses, shape)
        # Spec-root placement audit (gr631564) — runs post-commit so its
        # reads see the just-written part-of edge; only spec roots pay for
        # its queries.
        if spec_root_slug(str(t) for t in parsed_tags) is not None:
            adv = spec_root_advisory(self.store, ref.id)
            if adv is not None:
                ack = Response(body=f"{ack.body}\n\n{adv}", cost=ack.cost)
        return self._with_processing_hint(ref.id, ack), int(ref.id)

    def _put_dry_run(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None,
        put_meta: dict[str, Any],
    ) -> Response:
        """The ``put(dry_run=True)`` preview (gr548271): the create path's
        validation and its ack's ``Next:`` rows, computed from the draft;
        nothing is written."""
        from precis.handlers import _put_preview
        from precis.utils.link_tag_ops import guard_taxon_hierarchy, validate_relation

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text= (the body prose)",
                next="put(kind='memory', text='the thought', title='short title', "
                "dry_run=True)",
            )
        title = put_meta.get("title") or _derive_title(text)
        all_tags = [*self.default_tags_on_create, *(tags or [])]
        if _dream_stamp_defaults(all_tags)[1]:
            all_tags.append("SPACE:research")
        from precis.handlers._tag_redirect import (
            REDIRECTABLE_NAMESPACES,
            _value_needs_redirect,
        )

        def _redirected(t: str) -> bool:
            ns, sep, value = t.partition(":")
            return (
                bool(sep)
                and ns in REDIRECTABLE_NAMESPACES
                and (_value_needs_redirect(value))
            )

        # A prose ask-user:/halt: value becomes an overflow chunk on the real
        # create, not a tag, so it neither refuses nor feeds the hints here.
        values = draft_tag_values(
            [t for t in all_tags if not _redirected(t)], kind=self.kind
        )
        parents: tuple[int, ...] = ()
        if link is not None:
            target = parse_link_target(link, store=self.store)
            relation = validate_relation(rel)
            guard_taxon_hierarchy(
                self.store, None, target, relation, src_kind=self.kind
            )
            if relation == "part-of" and target.ref_id is not None:
                parents = (int(target.ref_id),)
        draft = MemoryDraft(
            title=title,
            body=text,
            hook=put_meta.get("hook"),
            tags=values,
            parents=parents,
        )
        rows = [
            *draft_thread_dup_hint(self.store, draft),
            (
                "get(kind='skill', id='precis-firstline-help')",
                "title conventions (lead with the conclusion)",
            ),
            *draft_link_hints(self, draft),
            *_roundtrip_rows(title, NEW_MEMORY),
        ]
        space = next((v for v in sorted(values) if v.startswith("SPACE:")), None)
        dups = _put_preview.nearest_duplicates(
            self, f"{title}\n{text}", kind=self.kind, space=space
        )
        ack = _put_preview.render_preview(
            self.kind,
            dups=dups,
            space=space,
            next_rows=rows,
            rule=_put_preview.kind_rule(text),
        )
        shape = self._shape_audit(
            title=title,
            body=text,
            tags=all_tags,
            meta=put_meta,
            has_evidence=link is not None and rel in {"cites", "derived-from"},
        )
        return self._with_shape_advisory(ack, shape, dry_run=True)

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
        ack: Response,
        misses: list[UngroundedNumber] | None,
        shape: ShapeAudit | None = None,
    ) -> Response:
        if misses:
            lines = "\n".join(m.advisory() for m in misses)
            ack = Response(body=f"{ack.body}\n\n{lines}", cost=ack.cost)
        return MemoryHandler._with_shape_advisory(ack, shape)

    @staticmethod
    def _shape_audit(**kwargs: Any) -> ShapeAudit | None:
        """Advisory checks fail open; unavailable checks change no tags."""
        try:
            return audit_shape(**kwargs)
        except Exception:
            log.warning("memory shape audit failed open", exc_info=True)
            return None

    def _shape_for_edit(
        self,
        ref: Ref,
        body: str,
        *,
        old_body: str,
        new_title: str | None,
        meta_patch: dict[str, Any],
    ) -> ShapeAudit | None:
        """Audit the resulting title/body and current evidence before writing."""
        try:
            title = new_title or ref.title or ""
            if new_title is None and title == _derive_title(old_body):
                title = _derive_title(body)
            return self._shape_audit(
                title=title,
                body=body,
                tags=[str(tag) for tag in self.store.tags_for(ref.id)],
                meta={**(ref.meta or {}), **meta_patch},
                has_evidence=any(
                    link.relation in {"cites", "derived-from"}
                    for link in self.store.links_for(ref.id, direction="out")
                ),
            )
        except Exception:
            log.warning("memory shape context unavailable", exc_info=True)
            return None

    def _apply_shape_tags(
        self, ref_id: int, shape: ShapeAudit | None, *, conn: Connection
    ) -> None:
        """Maintain system-owned flags; a failed audit never poisons a write.

        The savepoint makes tag maintenance best effort without swallowing a
        SQL error into an aborted body transaction. Each code coexists with
        the other AUDIT codes, including attribution and author-set flags.
        Read ownership before adding: add_tag upserts would otherwise take
        over an author-set flag and let a later clean rewrite erase it.
        """
        if shape is None:
            return
        found = {finding.code for finding in shape.findings}
        try:
            with conn.transaction():
                for code in sorted(shape.checked_codes):
                    tag = Tag.closed("AUDIT", code)
                    row = conn.execute(
                        "SELECT rt.set_by FROM ref_tags rt JOIN tags t "
                        "ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s "
                        "AND t.namespace = 'AUDIT' AND t.value = %s",
                        (ref_id, code),
                    ).fetchone()
                    if row is not None and row[0] != "system":
                        continue
                    if code in found:
                        self.store.add_tag(ref_id, tag, set_by="system", conn=conn)
                    elif row is not None:
                        self.store.remove_tag(ref_id, tag, conn=conn)
        except Exception:
            log.warning("memory shape tags failed open", exc_info=True)

    def _shape_after_write(
        self, ref_id: int, prior: ShapeAudit | None, *, conn: Connection
    ) -> ShapeAudit | None:
        """Check locked, final state; reuse Git only for identical title/body.

        A title or metadata writer may have committed since the preflight.
        Rechecking on the body transaction prevents its clean snapshot from
        clearing flags for that writer's new claim. Git stays outside locks.
        """
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT title, meta FROM refs WHERE ref_id = %s", (ref_id,)
                ).fetchone()
                if row is None:
                    return None
                title = str(row[0] or "")
                chunks = self.store.chunks.list_chunks_for_ref(ref_id, conn=conn)
                body = next(
                    (chunk.text for chunk in chunks if chunk.chunk_kind == _BODY_KIND),
                    title,
                )
                evidence = conn.execute(
                    "SELECT 1 FROM links WHERE src_ref_id = %s "
                    "AND relation IN ('cites', 'derived-from') LIMIT 1",
                    (ref_id,),
                ).fetchone()
                final = self._shape_audit(
                    title=title,
                    body=body,
                    tags=[str(tag) for tag in self.store.tags_for(ref_id, conn=conn)],
                    meta=row[1] or {},
                    has_evidence=evidence is not None,
                    check_git=False,
                )
                if (
                    final is not None
                    and prior is not None
                    and prior.git_context == (title, body)
                ):
                    final = replace(
                        final,
                        findings=final.findings
                        + tuple(
                            finding
                            for finding in prior.findings
                            if finding.code == "old-sha"
                        ),
                        checked_codes=final.checked_codes | {"old-sha"},
                        git_context=prior.git_context,
                    )
                return final
        except Exception:
            log.warning("locked memory shape context unavailable", exc_info=True)
            return None

    @staticmethod
    def _with_shape_advisory(
        ack: Response, shape: ShapeAudit | None, *, dry_run: bool = False
    ) -> Response:
        if shape is None or not shape.findings:
            return ack
        lines = [finding.advisory() for finding in shape.findings]
        if dry_run:
            for finding in shape.findings:
                if finding.titles:
                    lines.append("Proposed atomic titles:")
                    lines.extend(f"- {title}" for title in finding.titles)
        return Response(body=ack.body + "\n\n" + "\n".join(lines), cost=ack.cost)

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

    @link_effect_receipt
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
          find=/text= and the anchor args are refused alongside ``ops=``
          (they belong inside each op dict); ``title=``, ``rule=``,
          ``warrant=``, ``meta=``, ``reason=`` and ``dry_run=`` apply to
          the whole call.

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
            pending_ids: list[int] = []
            token = _PROCESSING_HINT_IDS.set(pending_ids)
            try:
                resp = self._edit_batch(
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
            finally:
                _PROCESSING_HINT_IDS.reset(token)
            pending_ids = [
                ref_id
                for ref_id in dict.fromkeys(pending_ids)
                if self._processing_hint_reason(ref_id) is not None
            ]
            if pending_ids:
                handles = ", ".join(f"me{n}" for n in pending_ids)
                resp = replace(
                    resp,
                    body=resp.body + f"\nCheck processing for {handles}: "
                    f"get(kind='memory', id={pending_ids[0]}, view='processing') "
                    "before testing recall; check each listed memory.",
                )
            return resp
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
        meta_clean = self._edit_meta(id, meta) if meta is not None else {}
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
                        f"edit(kind='memory', id={id}, "
                        "args={'ops': [{'find': 'old', 'text': 'new'}, ...]})"
                    ),
                )
            mixed = [
                name
                for name, val in (
                    ("find", find),
                    ("text", text),
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
                    f"{'/'.join(mixed)}= is a per-op argument — with ops= "
                    "each op dict carries its own find/text/before/after/"
                    "where/match/nth; title=/rule=/warrant=/hook= still "
                    "apply to the whole call",
                    next=(
                        f"edit(kind='memory', id={id}, "
                        "args={'ops': [{'find': 'old', 'text': 'new'}, ...]})"
                    ),
                )
            # ``title=`` / ``rule=`` / ``warrant=`` are ref-level, not
            # per-span — they ride the batch's one transaction the way they
            # ride an anchored edit's (gr506768; gr568375 — the skill
            # promised this and the refusal broke it). ``hook=`` already
            # merged into ``meta_clean`` above.
            ops_meta = dict(meta_clean)
            if rule_clean is not None:
                ops_meta["rule"] = rule_clean
            if warrant_clean is not None:
                ops_meta["warrant"] = warrant_clean
            return self._after_status_write(
                self._coerce_id(id),
                meta_clean,
                dry_mode,
                self._edit_ops(
                    ref_id=self._coerce_id(id),
                    ops=ops,
                    dry_mode=dry_mode,
                    meta_patch=ops_meta,
                    new_title=new_title,
                    reason=reason,
                ),
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
            return self._after_status_write(
                self._coerce_id(id),
                meta_clean,
                dry_mode,
                self._edit_anchored(
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
                ),
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
        previous_body = self._body_text(ref)
        shape = self._shape_for_edit(
            ref,
            text if has_text and text is not None else previous_body,
            old_body=previous_body,
            new_title=new_title,
            meta_patch=meta_patch,
        )
        final_shape: list[ShapeAudit | None] = []
        if not has_text:
            # Meta-only patch (rule=/warrant=/meta=, no body rewrite) — skip the
            # chunk delete+reinsert and mention re-sync entirely; nothing
            # in the prose changed.
            def _patch_meta() -> None:
                with self.store.tx() as conn:
                    row = conn.execute(
                        "SELECT title, meta FROM refs WHERE ref_id = %s "
                        "FOR NO KEY UPDATE",
                        (ref.id,),
                    ).fetchone()
                    patch = dict(meta_patch)
                    if new_title is not None and row is not None:
                        current = replace(ref, title=row[0], meta=row[1] or {})
                        raw = next(
                            (
                                chunk.text
                                for chunk in self.store.chunks.list_chunks_for_ref(
                                    ref.id, conn=conn
                                )
                                if chunk.chunk_kind == _BODY_KIND
                            ),
                            current.title or "",
                        )
                        patch["_memory_body_display"] = self._body_display_policy(
                            raw,
                            self._body_echo_title(raw, current.title, current.meta),
                        )
                    self.store.update_ref(ref.id, meta_patch=patch, conn=conn)
                    if new_title is not None:
                        self.store.chunks.set_ref_title(
                            ref.id, new_title, source="agent", conn=conn
                        )
                    self.store.chunks.sync_header_card(ref.id, conn=conn)
                    # The re-emitted header card carries a new chunk id —
                    # nudge so its vector lands within seconds (gr563669).
                    nudge_fresh_embed(self.store, conn=conn, reason="edit memory meta")
                    checked = self._shape_after_write(ref.id, shape, conn=conn)
                    self._apply_shape_tags(ref.id, checked, conn=conn)
                    final_shape[:] = [checked]

            self._write_retried(ref.id, "meta edit", _patch_meta)
            changed = ", ".join(
                k
                for k in ("rule", "warrant", "hook", "type", *_RULING_META_KEYS)
                if k in meta_patch
            )
            out = f"updated {self._sense()} id={ref.id} meta: {changed}"
            if new_title is not None:
                out += f". title now: {new_title!r}"
            return self._after_status_write(
                ref.id,
                meta_clean,
                dry_mode,
                self._with_shape_advisory(
                    Response(body=out + render_next_section(link_hints(self, ref.id))),
                    final_shape[0],
                ),
            )

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
            shape=shape,
            shape_out=final_shape,
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
        lead = self._thread_lead_hint(
            ref.id, self._display_body(ref, old_body or ""), text
        )
        if lead:
            body += f"\n\nhint: {lead}"
        body += render_next_section(link_hints(self, ref.id))
        return self._after_status_write(
            ref.id,
            meta_clean,
            dry_mode,
            self._with_attribution_advisory(
                self._facts_note(Response(body=body), deferred[0] if deferred else 0),
                misses,
                final_shape[0],
            ),
        )

    def _edit_meta(self, id: str | int | list[str | int], meta: Any) -> dict[str, Any]:
        """``_validate_meta`` for an edit: a ruling key needs the node's tags
        and current meta (a no-op unless the call carries one)."""
        if not isinstance(meta, dict) or not any(k in meta for k in _RULING_META_KEYS):
            return _validate_meta(meta)
        ref = self._resolve_live_ref(self._coerce_id(id))
        tags = self.store.ref_tags_bulk([ref.id]).get(ref.id, [])
        return _validate_meta(
            meta,
            ruling=RULING_TAG in {v for _ns, v in tags},
            existing=dict(ref.meta or {}),
        )

    def _after_status_write(
        self,
        ref_id: int,
        meta_clean: dict[str, Any],
        dry_mode: Any,
        resp: Response,
    ) -> Response:
        """Add processing evidence guidance; after a ``meta.status`` edit, drop the backfill's
        duplicate ``status:<value>`` tag so tag and meta cannot disagree, and
        say so in the ack. Skipped on a dry run."""
        if dry_mode is not None:
            return resp
        resp = self._with_processing_hint(ref_id, resp)
        if "status" not in meta_clean:
            return resp
        dropped = drop_legacy_status_tags(self.store, ref_id)
        if not dropped:
            return resp
        return replace(
            resp,
            body=resp.body
            + f"\ndropped tag(s) {dropped} — ruling status now lives in meta.status",
        )

    def _with_processing_hint(self, ref_id: int, resp: Response) -> Response:
        """One advisory line after a successful write; never undo its receipt.

        Status reads use a separate pool connection. Batch edits only collect
        ids here; the outer edit checks them after the transaction commits so
        the read sees the writes and waits for no connection while holding locks.
        """
        if (pending_ids := _PROCESSING_HINT_IDS.get()) is not None:
            pending_ids.append(ref_id)
            return resp
        reason = self._processing_hint_reason(ref_id)
        if reason is None:
            return resp
        return replace(
            resp,
            body=resp.body + f"\n{reason}: get(kind='memory', id={ref_id}, "
            "view='processing') before testing recall.",
        )

    def _processing_hint_reason(self, ref_id: int) -> str | None:
        """Read committed status; an unavailable advisory never fails a write."""
        from precis.handlers._memory_processing import processing_report

        try:
            if not processing_report(self.store, ref_id).outstanding:
                return None
            return "processing outstanding"
        except Exception:
            log.warning(
                "memory processing receipt unavailable for %s", ref_id, exc_info=True
            )
            return "processing status unavailable"

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

        tags = self.store.ref_tags_bulk([ref_id]).get(ref_id, [])
        if "section:threads" not in {v for _ns, v in tags}:
            return None
        # The write already committed; a concurrent retirement must not turn
        # its optional receipt hint into a failed edit acknowledgement.
        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        if ref is None:
            return None
        displayed = self._display_body(ref, new_body)
        lead = first(displayed)
        if not lead or lead != first(old_body):
            return None
        shown = lead if len(lead) <= 120 else lead[:119].rstrip() + "…"
        anchor = lead if len(lead) <= 120 else lead[:60]
        if displayed.count(anchor) == 1:
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

    @staticmethod
    def _describe_locker(row: dict[str, Any]) -> str:
        who = row["app"] or row["usename"] or "?"
        age = f"{row['age_s']}s" if row["age_s"] is not None else "unknown-age"
        detail = f"pid {row['pid']} {who!r} ({row['btype']}, xact {age}, {row['state']}"
        if row["wait"] and row["wait"] != "-":
            detail += f", waiting on {row['wait']}"
        if row["query"]:
            detail += f", running {row['query']!r}"
        return detail + ")"

    @staticmethod
    def _wait_context(row: dict[str, Any], label: str) -> str:
        if row["locktype"] == "relation":
            return f"{row['mode']} table lock on {row['rel']}"
        if row["locktype"] == "transactionid":
            return f"row lock on {label} (xact {row['xid']})"
        if row["locktype"] == "tuple":
            return f"tuple lock on {row['rel']}"
        return f"{row['locktype']} lock on {row['rel'] or label}"

    def _lock_holder_report(self, ref_id: int) -> str:
        """Name the backend(s) currently blocking this memory's write.

        Best-effort, run on a fresh pair of pool connections AFTER the
        failed attempt's transaction is dead (the aborted tx could not
        query anyway). Granted row locks never reach ``pg_locks`` —
        they live in the tuple header, and a raw ``xmax`` cannot tell
        an xid from a multixact without ``t_infomask`` — so a
        lock-taker connection replays the locks the write needed
        (:data:`_ROW_LOCK_PROBES` and :data:`_TABLE_LOCK_PROBE_SQL`,
        each bounded by :data:`_LOCK_PROBE_TIMEOUT_MS`) while a second
        connection reads the wait queue (:data:`_LOCK_WAIT_SQL`): the
        lock manager itself names the holder — the "who actually holds
        it" gr631568's "locked by another writer" refused to say — so a
        stale ``xmax``, an xid/multixact overlap or a compatible table
        lock can never be reported (td632689). A miss means the
        holders cycle too fast to catch (the processing stages a fresh
        or just-edited memory churns through), not a long write
        nobody can find. Never raises: a broken probe degrades the
        report, never the refusal.
        """
        parts: list[str] = []
        blockers: dict[int, set[str]] = {}
        # ``state`` crosses the lock-taker thread boundary by assignment
        # only — single-writer values stay GIL-atomic.
        state: dict[str, Any] = {}
        done = threading.Event()

        def _take(taker: Connection) -> None:
            try:
                taker.execute(f"SET LOCAL lock_timeout = '{_LOCK_PROBE_TIMEOUT_MS}ms'")
                own = taker.execute("SELECT pg_backend_pid()").fetchone()
                if own is not None:
                    state["pid"] = own[0]
                for label, sql in _ROW_LOCK_PROBES:
                    state["label"] = label
                    taker.execute("SAVEPOINT lock_probe")
                    try:
                        taker.execute(sql, {"ref": ref_id})
                    except (LockNotAvailable, DeadlockDetected):
                        pass
                    # After a timeout the tx needs the savepoint rollback
                    # to go on; after a success it frees the row locks
                    # the probe just took, so the diagnostic cannot
                    # itself stall the writer it is blaming.
                    taker.execute("ROLLBACK TO SAVEPOINT lock_probe")
                state["label"] = "tables"
                try:
                    taker.execute(_TABLE_LOCK_PROBE_SQL)
                except (LockNotAvailable, DeadlockDetected):
                    pass
            except Exception:
                log.warning("memory %s: lock-taker probe failed", ref_id, exc_info=True)
            finally:
                done.set()

        try:
            # Checkout on the BASE pool, not ``pool.connection``: the
            # probe needs two genuinely separate connections — under
            # ``pin_connection`` (``store.atomic()``) ``connection()``
            # would hand both the caller's pinned conn, putting the
            # lock-taker thread and the observer on one connection and
            # the probe locks inside the caller's transaction. The
            # bypass also skips ``SET LOCAL ROLE``: taking row locks
            # wants the login role's write privileges, not a read-only
            # envelope.
            with (
                ConnectionPool.connection(self.store.pool) as taker,
                ConnectionPool.connection(self.store.pool) as probe,
                probe.cursor(row_factory=dict_row) as cur,
            ):
                worker = threading.Thread(
                    target=_take,
                    args=(taker,),
                    daemon=True,
                    name="precis-lock-probe",
                )
                worker.start()
                deadline = time.monotonic() + _LOCK_PROBE_BUDGET_S
                while not done.wait(_LOCK_PROBE_POLL_S):
                    if time.monotonic() > deadline:
                        break
                    pid = state.get("pid")
                    if pid is None:
                        continue
                    for row in cur.execute(_LOCK_WAIT_SQL, {"pid": pid}).fetchall():
                        blocker = row["blocker"]
                        if blocker != pid:
                            blockers.setdefault(blocker, set()).add(
                                self._wait_context(row, state.get("label", ""))
                            )
                worker.join(timeout=_LOCK_PROBE_BUDGET_S)
                if worker.is_alive():
                    # Statements are bounded by lock_timeout; one that
                    # outlived the budget anyway is cancelled so the
                    # pooled conn is never checked in under a live
                    # statement.
                    taker.cancel()
                    worker.join(timeout=2.0)
                if blockers:
                    for row in cur.execute(
                        _BLOCKER_BACKEND_SQL, {"pids": sorted(blockers)}
                    ).fetchall():
                        ctx = "; ".join(sorted(blockers.get(row["pid"], ())))
                        parts.append(
                            f"{self._describe_locker(row)} blocking {ctx or 'a lock'}"
                        )
        except Exception:
            log.warning("memory %s: lock-holder probe failed", ref_id, exc_info=True)
            return "; lock holder unresolved (probe failed)"
        if not parts:
            return (
                "; no live row-lock holder resolved on this memory — the "
                "block was most likely repeated short writes from the "
                "worker stages still processing it"
            )
        return "; lock held by " + "; ".join(parts[:8])

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
        before — only the lock-wait itself is replayed. The budget is
        :data:`_WRITE_LOCK_ATTEMPTS`, not the helper default: a fresh or
        just-edited node's own processing stages churn its rows' locks for
        tens of seconds (gr631568). When the budget runs out the call
        refuses with the resolved lock holder(s) and a ``next=`` instead
        of leaking the bare psycopg error.
        """
        try:
            return retry_locked(
                fn,
                attempts=_WRITE_LOCK_ATTEMPTS,
                label=f"memory {what} ref_id={ref_id}",
            )
        except (LockNotAvailable, DeadlockDetected) as exc:
            raise Upstream(
                f"memory {ref_id} {what} exhausted its lock-retry budget"
                + self._lock_holder_report(ref_id),
                next=(
                    f"get(kind='memory', id={ref_id}, view='processing') "
                    "to see the worker stages still writing this node, "
                    "then retry the edit — a fresh or just-edited memory "
                    "is processed in the background; if it repeats, file "
                    "a gripe naming the id"
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
        exact_body: bool = False,
        misses: list[UngroundedNumber] | None = None,
        shape: ShapeAudit | None = None,
        shape_out: list[ShapeAudit | None] | None = None,
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
        transaction. Shape checks re-read the final stored context under the
        same lock, and return their verdict through ``shape_out`` for the
        ack; Git preflight results apply only to identical title/body text.
        Returns the old body.

        Title derivation (gr538501): when no explicit ``new_title`` was
        given and the stored title still reads as the old body's derived
        title — ``title == _derive_title(old_body)``, i.e. it tracks the
        first line — a body write re-derives it from the new prose the
        same way ``put`` does, so a lead-line edit doesn't leave a stale
        header. A hand-set title (one that never was the derivation) is
        left alone; the re-derived value goes to ``derived_title_out``
        for the caller's ack. Replacing the complete visible body leaves
        its hidden title alone: dropping that prefix for display is not
        an authored first-line edit.
        """

        def _do() -> str | None:
            with self.store.tx() as conn:
                row = conn.execute(
                    "SELECT title, retired_at, meta FROM refs "
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
                current_meta = row[2] or {}
                echo_title = self._body_echo_title(previous, row[0] or "", current_meta)
                had_echo = self._strip_title_echo(previous, echo_title) != previous
                applied_title = new_title
                if (
                    applied_title is None
                    and not (exact_body and had_echo)
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
                patch = dict(meta_patch)
                policy = current_meta.get("_memory_body_display")
                stamped = policy == self._body_display_policy(previous, echo_title)
                echo_survives = had_echo and (
                    self._strip_title_echo(new_text, echo_title) != new_text
                )
                if exact_body or expected_body is None:
                    # A whole displayed-body rewrite is authored prose, even
                    # when its first line matches the header.
                    patch["_memory_body_display"] = self._body_display_policy(
                        new_text, ""
                    )
                elif new_title is not None or (
                    stamped and (not had_echo or echo_survives)
                ):
                    # Retitles preserve the locked row's old display decision.
                    # Ordinary edits only carry a live stamp forward; an empty
                    # echo also stays explicit when authored prose is edited.
                    patch["_memory_body_display"] = self._body_display_policy(
                        new_text, echo_title if echo_survives else ""
                    )
                if patch:
                    self.store.update_ref(ref_id, meta_patch=patch, conn=conn)
                if applied_title is not None:
                    self.store.chunks.set_ref_title(
                        ref_id, applied_title, source="agent", conn=conn
                    )
                if meta_patch or applied_title is not None:
                    self.store.chunks.sync_header_card(ref_id, conn=conn)
                # Re-sync auto-mention links to the rewritten body: drop the
                # auto links the text no longer names, add the new mentions.
                # A still-named edge keeps its row and fingerprint;
                # hand-added links survive.
                deferred = self._sync_mention_links(
                    ref_id, new_text, conn=conn, replace=True
                )
                if deferred_out is not None:
                    # ``retry_locked`` can re-invoke this closure — assign,
                    # never append, so the sink stays single-valued.
                    deferred_out[:] = [deferred]
                if misses is not None:
                    self._apply_attribution_tag(ref_id, misses, conn=conn)
                checked = self._shape_after_write(ref_id, shape, conn=conn)
                self._apply_shape_tags(ref_id, checked, conn=conn)
                if shape_out is not None:
                    shape_out[:] = [checked]
                # The replacement body chunk is semantically invisible
                # until it carries a vector — embed it within seconds so a
                # trimmed/split body stays recallable (gr563669).
                nudge_fresh_embed(self.store, conn=conn, reason="edit memory body")
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
        result, exact_body = self._apply_body_edit(ref, old_body, op)
        new_body = result.new_buffer
        if not new_body.strip():
            raise BadInput(
                f"edit would leave {label} with an empty body",
                next=(
                    f"delete(kind='memory', id={ref.id}) to remove the memory, "
                    "or edit with replacement text"
                ),
            )
        shape = self._shape_for_edit(
            ref, new_body, old_body=old_body, new_title=new_title, meta_patch=meta_patch
        )
        if dry_mode is not None:
            preview_link_effects(self.store, ref.id, new_body)
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
            return self._with_shape_advisory(
                Response(body="\n".join([*header, "", body])), shape, dry_run=True
            )
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        final_shape: list[ShapeAudit | None] = []
        self._write_body(
            ref.id,
            new_body,
            new_title=new_title,
            meta_patch=meta_patch,
            expected_body=old_body,
            exact_body=exact_body,
            misses=misses,
            shape=shape,
            shape_out=final_shape,
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
        lead = self._thread_lead_hint(
            ref.id, self._display_body(ref, old_body), new_body
        )
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
            final_shape[0],
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
        shape = self._shape_for_edit(
            ref, new_body, old_body=old_body, new_title=new_title, meta_patch=meta_patch
        )
        if dry_mode is not None:
            preview_link_effects(self.store, ref.id, new_body)
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
            return self._with_shape_advisory(
                Response(body="\n".join([*header, "", body])), shape, dry_run=True
            )
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        final_shape: list[ShapeAudit | None] = []
        self._write_body(
            ref.id,
            new_body,
            new_title=new_title,
            meta_patch=meta_patch,
            expected_body=old_body,
            misses=misses,
            shape=shape,
            shape_out=final_shape,
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
        lead = self._thread_lead_hint(
            ref.id, self._display_body(ref, old_body), new_body
        )
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
            final_shape[0],
        )

    def _edit_ops(
        self,
        *,
        ref_id: int,
        ops: Any,
        dry_mode: str | None,
        meta_patch: dict[str, Any],
        new_title: str | None = None,
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
        ``new_title`` retitles in the same transaction, the way an
        anchored edit's ``title=`` does (gr568375).
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
        echo_title = self._body_echo_title(old_body, ref.title, ref.meta)
        exact_body = False
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
                    new_body, span, projected = self._op_anchored(
                        raw,
                        new_body,
                        op_kind=op_kind,
                        label=label,
                        ref=ref,
                        echo_title=echo_title,
                    )
                    if projected:
                        exact_body = True
                        echo_title = ""
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
        shape = self._shape_for_edit(
            ref, new_body, old_body=old_body, new_title=new_title, meta_patch=meta_patch
        )
        if dry_mode is not None:
            preview_link_effects(self.store, ref.id, new_body)
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
            if new_title is not None:
                body += f"\n(title would become {new_title!r})"
            return self._with_shape_advisory(
                Response(body="\n".join([*header, "", body])), shape, dry_run=True
            )
        misses = self._attribution_misses(new_body)
        deferred: list[int] = []
        derived_title: list[str] = []
        final_shape: list[ShapeAudit | None] = []
        self._write_retried(
            ref.id,
            "body edit",
            lambda: self._write_body(
                ref.id,
                new_body,
                new_title=new_title,
                meta_patch=meta_patch,
                expected_body=old_body,
                exact_body=exact_body,
                misses=misses,
                shape=shape,
                shape_out=final_shape,
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
        if new_title is not None:
            ack += f" title now: {new_title!r}."
            nudge = self._first_line_nudge(new_title)
            if nudge:
                ack += f"\n\nhint: {nudge}"
        elif derived_title:
            ack += f" title re-derived: {derived_title[0]!r}."
        lead = self._thread_lead_hint(
            ref.id, self._display_body(ref, old_body), new_body
        )
        if lead:
            ack += f"\n\nhint: {lead}"
        ack += render_next_section(link_hints(self, ref.id))
        return self._with_attribution_advisory(
            self._facts_note(Response(body=ack), deferred[0] if deferred else 0),
            misses,
            final_shape[0],
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

    def _op_anchored(
        self,
        raw: dict[str, Any],
        buffer: str,
        *,
        op_kind: str,
        label: str,
        ref: Ref,
        echo_title: str,
    ) -> tuple[str, tuple[tuple[int, int], ...], bool]:
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
        result, exact_body = self._apply_body_edit(
            ref, buffer, op, echo_title=echo_title
        )
        return result.new_buffer, result.edited_spans, exact_body

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
        response = super().tag(id=id, add=add, remove=remove, **_kw)
        # Spec-root placement audit (gr631564): spec:*/section:index tag
        # writes are what make (or unmake) a spec root — re-check the
        # touched nodes so the flag lands or clears with the verdict.
        touched = [*(add or []), *(remove or [])]
        if any(
            t.lower().startswith(("spec:", "section:index"))
            for t in touched
            if isinstance(t, str)
        ):
            try:
                ids = id if isinstance(id, list) else [id]
                advisories = [
                    adv
                    for i in ids
                    if (adv := spec_root_advisory(self.store, self._coerce_id(i)))
                    is not None
                ]
                if advisories:
                    response = replace(
                        response,
                        body=response.body + "\n\n" + "\n".join(advisories),
                    )
            except Exception:
                log.debug("memory tag spec-root advisory skipped", exc_info=True)
        return response

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

    @staticmethod
    def _body_display_policy(body: str, echo_title: str) -> dict[str, str]:
        return {
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "echo_title": echo_title,
        }

    def _body_echo_title(
        self,
        body: str,
        title: str,
        meta: dict[str, Any],
    ) -> str:
        """Use a matching display stamp, otherwise today's title-echo rule.

        The fingerprint keeps an external body replacement from inheriting
        an obsolete decision. Only the supplied body, title and metadata
        participate; reads neither query history nor repair stored data.
        """
        policy = meta.get("_memory_body_display")
        if (
            isinstance(policy, dict)
            and policy.get("sha256") == self._body_display_policy(body, "")["sha256"]
            and isinstance(policy.get("echo_title"), str)
        ):
            return str(policy["echo_title"])

        return title

    def _display_body(self, ref: Ref, raw_body: str) -> str:
        echo_title = self._body_echo_title(raw_body, ref.title or "", ref.meta or {})
        return self._strip_title_echo(raw_body, echo_title)

    def _apply_body_edit(
        self,
        ref: Ref,
        buffer: str,
        op: EditOp,
        *,
        echo_title: str | None = None,
    ) -> tuple[EditResult, bool]:
        """Resolve prose only; a complete visible-body match replaces its echo.

        Explicit legacy title anchors still address the raw chunk. A writer
        matching the complete displayed body instead replaces the whole
        authored body, so its invisible prefix cannot survive the rewrite.
        A delete is not a rewrite: it stays on the raw buffer.
        Generated headers and entity footers never enter either buffer.
        """
        if op.find not in buffer:
            raise BadInput(
                f"anchor not found in memory {ref.id} body; generated headers "
                "and referenced entities footers are not editable",
                next=(
                    f"get(kind='memory', id='me{ref.id}~body'), then retry "
                    "with an anchor from the body prose"
                ),
            )
        if echo_title is None:
            echo_title = self._body_echo_title(buffer, ref.title or "", ref.meta or {})
        visible = self._strip_title_echo(buffer, echo_title)
        exact_body = bool(
            op.op == "edit"
            and op.text
            and visible
            and op.find == visible
            and op.match == "unique"
            and op.nth is None
            and not op.before
            and not op.after
        )
        return apply_edit(visible if exact_body else buffer, op), exact_body

    def _hit_text(self, ref: Ref, block: Any) -> str:
        """Search-hit text: the body prose, even when the best-ranked chunk
        was the title+hook header card (so a card hit still shows a snippet)."""
        if getattr(block, "chunk_kind", None) == "card_combined":
            return self._body_text(ref)
        return str(block.text or "")

    def _strip_title_echo(self, body: str, title: str) -> str:
        """Drop a leading line identified as a legacy title echo.

        ``_derive_title`` copies the first non-empty body line into the
        title, so digest-style bodies (``# Heading`` opener) and
        single-line memories would otherwise print the same text twice —
        once in the ``# memory <id>: <title>`` header, once as the body's
        first line (gr504805). The caller supplies the stable echo title
        from matching metadata or the current header. An empty echo title
        preserves explicitly rewritten prose, even when it repeats the
        header. Stored chunks are untouched; this is display-only.
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
        from precis.utils.entity_courtesy import (
            courtesy_lines,
            entity_handles,
            safe_entity_states,
        )
        from precis.utils.eye_render import _NEIGHBOR_GROUP_CAP

        title = ref.title or ""
        header = f"# {self._sense()} {ref.id}"
        if title:
            header += f": {title}"
        out = [header]
        raw_body = self._body_text(ref)
        body = self._display_body(ref, raw_body)
        if body:
            out += ["", body]
        handles = entity_handles(raw_body)
        courtesy = courtesy_lines(
            handles, safe_entity_states(self.store, handles), cap=_NEIGHBOR_GROUP_CAP
        )
        if courtesy:
            out += ["", courtesy]
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
                    "size": row_size_cell(sizes.get(r.id, 0)),
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
        group_by: str | list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        scope: str | None = None,
        # The dispatcher resolves under=/depth= into include_ref_ids and
        # re-hands the caller's spelling to kinds that declare it — the
        # index listing re-renders it in its next-page hint (gr538490).
        under: str | int | None = None,
        depth: int | str | None = None,
        status: str | None = None,
        sort: str | None = None,
        space: str | None = None,
        min_chars: int | None = None,
        min_reads: int | None = None,
        **kw: Any,
    ) -> Response:
        """Memory search. An unscoped query whose hits cluster in one hub or
        ``section:<type>`` gets a one-line narrowing hint appended
        (:func:`~precis.utils.memory_hubs.narrowing_hints`; ``under=`` itself
        is the dispatcher's generic part-of scope). The parameters are spelled
        out because dispatch routes by this signature: it follows cooperative
        superclass forwarding, not the private ``_search_inner`` helper.

        ``view='rulings'`` lists the nodes tagged ``ruling`` grouped by
        status (:func:`~precis.utils.memory_rulings.render_rulings`);
        ``status=`` (``args={'status': '*'}``) widens it past in-force. The
        default and ``view='index'`` renders leave absorbed, archived and
        retired rulings out of recall.

        A domain-bound connection defaults to the domain's ``SPACE:``
        set when ``tags=`` names no ``SPACE:`` axis (gr572723); a
        caller-supplied ``SPACE:`` tag wins, and the footer names the
        door when the default dropped hits."""
        if group_by is not None and view != "count":
            raise BadInput(
                "group_by= only applies to view='count'",
                next="search(kind='memory', view='count', args={'group_by': 'tag:SPACE'})",
            )
        # gr572723 (C4): a domain-bound connection defaults memory search
        # to the domain's SPACE: set — never by section:/topic:, so
        # rulings, gotchas and runbooks stay reachable for every role in
        # the space. A caller-supplied SPACE: tag (either polarity) or
        # the reads view's space= wins. Fisheye and get by handle are
        # untouched.
        domain = self._domain_space_default(tags, space)
        caller_tags = list(tags or [])
        caller_include = include_ref_ids
        if domain is not None and domain.spaces:
            if len(domain.spaces) == 1:
                tags = [*caller_tags, f"SPACE:{next(iter(domain.spaces))}"]
            else:
                space_ids = self._memory_ids_in_spaces(domain.spaces)
                include_ref_ids = (
                    sorted(space_ids & set(caller_include))
                    if caller_include is not None
                    else sorted(space_ids)
                )
        if view == "reads":
            from precis.handlers._memory_reads import count_reads, render_reads

            unapplied = {
                "q": fold_match_all_q(q),
                "mode": mode,
                "scope": scope,
                "status": status,
                **kw,
            }
            rejected = [
                name
                for name, value in unapplied.items()
                if value is not None
                and value != ""
                and not (name == "q" and isinstance(value, str) and not value.strip())
            ]
            if rejected:
                raise BadInput(
                    f"{', '.join(rejected)}= is not applied by view='reads'",
                    next="search(kind='memory', view='reads', args={'space': 'repo-dev'})",
                )
            return self._domain_space_footer(
                render_reads(
                    self.store,
                    sort=sort,
                    space=space,
                    min_chars=min_chars,
                    min_reads=min_reads,
                    page=page,
                    page_size=page_size,
                    tags=tags,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                ),
                domain=domain,
                q=None,
                exclude_ref_ids=exclude_ref_ids,
                before=(caller_tags, caller_include),
                after=(tags, include_ref_ids),
                count=lambda scope_tags, scope_ids: count_reads(
                    self.store,
                    space=space,
                    min_chars=min_chars,
                    min_reads=min_reads,
                    tags=scope_tags,
                    include_ref_ids=scope_ids,
                    exclude_ref_ids=exclude_ref_ids,
                ),
            )
        if any(value is not None for value in (space, min_chars, min_reads)):
            raise BadInput(
                "space=/min_chars=/min_reads= require view='reads'",
                next="search(kind='memory', view='reads')",
            )
        if sort is not None:
            kw["sort"] = sort
        if view == "smells":
            return self._domain_space_footer(
                render_smell_rollup(
                    self.store,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    tags=tags,
                    under=under,
                    q=q,
                    page=page,
                    mode=mode,
                    scope=scope,
                    status=status,
                ),
                domain=domain,
                q=None,
                exclude_ref_ids=exclude_ref_ids,
                before=(caller_tags, caller_include),
                after=(tags, include_ref_ids),
                count=lambda scope_tags, scope_ids: count_smell_nodes(
                    self.store,
                    include_ref_ids=scope_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    tags=scope_tags,
                ),
            )
        if view == "shape":
            # gr570722: the reviewer's computed smells over a scope —
            # the read-side twin of view='smells' (the proposal ledger).
            from precis.handlers._memory_environment import (
                count_shape_nodes,
                render_shape,
            )

            return self._domain_space_footer(
                render_shape(
                    self.store,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    tags=tags,
                    under=under,
                    q=q,
                    page=page,
                    mode=mode,
                    scope=scope,
                    status=status,
                    sort=sort,
                ),
                domain=domain,
                q=None,
                exclude_ref_ids=exclude_ref_ids,
                before=(caller_tags, caller_include),
                after=(tags, include_ref_ids),
                count=lambda scope_tags, scope_ids: count_shape_nodes(
                    self.store,
                    include_ref_ids=scope_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    tags=scope_tags,
                ),
            )
        if view == "rulings":
            return self._domain_space_footer(
                self._rulings_view(
                    q=q,
                    tags=tags,
                    page=page,
                    mode=mode,
                    scope=scope,
                    status=status,
                    include_ref_ids=include_ref_ids,
                    exclude_ref_ids=exclude_ref_ids,
                ),
                domain=domain,
                q=None,
                exclude_ref_ids=exclude_ref_ids,
                before=(caller_tags, caller_include),
                after=(tags, include_ref_ids),
                count=lambda scope_tags, scope_ids: count_rulings(
                    self.store,
                    status=status,
                    tags=scope_tags,
                    include_ref_ids=scope_ids,
                    exclude_ref_ids=exclude_ref_ids,
                ),
            )
        if status is not None:
            kw["status"] = status
        if view in (None, "index") and (out_of_force := hidden_ids(self.store)):
            exclude_ref_ids = sorted({*(exclude_ref_ids or ()), *out_of_force})
        resp = self._search_inner(
            q=q,
            tags=tags,
            page_size=page_size,
            page=page,
            mode=mode,
            view=view,
            group_by=group_by,
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
            scope=scope,
            under=under,
            depth=depth,
            **kw,
        )
        if q and q.strip():
            try:
                ids = [
                    int(m)
                    for m in re.findall(r"(?:^## memory |\bme)(\d+)\b", resp.body, re.M)
                ]
                hints = narrowing_hints(self.store, ids, q)
            except Exception:
                hints = []
            if hints:
                resp = replace(
                    resp, body=resp.body.rstrip() + "\n\n" + "\n".join(hints)
                )
        return self._domain_space_footer(
            resp,
            domain=domain,
            q=q,
            exclude_ref_ids=exclude_ref_ids,
            before=(caller_tags, caller_include),
            after=(tags, include_ref_ids),
        )

    def _domain_space_default(
        self, tags: list[str] | None, space: str | None
    ) -> Any | None:
        """The connection's :class:`~precis.runtime.scope.Domain` when a
        default ``SPACE:`` filter applies, else ``None`` (gr572723 C4).

        ``None`` when the connection's domain leaves the ``spaces`` axis
        open (``'all'``), when the caller already named ``SPACE:`` in
        ``tags=`` — a caller-supplied ``SPACE:`` tag wins, either
        polarity — or when the reads view's ``space=`` named one.
        """
        from precis.runtime.scope import current_domain

        domain = current_domain()
        if domain.spaces is None or space is not None:
            return None
        if any(_tag_names_axis(str(t), "SPACE") for t in tags or []):
            return None
        return domain

    def _memory_ids_in_spaces(self, spaces: frozenset[str]) -> set[int]:
        """Live ref_ids carrying a ``SPACE:`` value in ``spaces`` — the
        axis is a partition, so membership is the OR a tag-AND filter
        cannot spell for a multi-space domain."""
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT rt.ref_id FROM ref_tags rt"
                " JOIN tags t ON t.tag_id = rt.tag_id"
                " JOIN refs r ON r.ref_id = rt.ref_id"
                " WHERE r.kind = %s AND r.retired_at IS NULL"
                " AND t.namespace = 'SPACE' AND t.value = ANY(%s)"
                " AND (rt.expires_at IS NULL OR rt.expires_at > now())",
                (self.kind, sorted(spaces)),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def _domain_space_footer(
        self,
        resp: Response,
        *,
        domain: Any | None,
        q: str | None,
        exclude_ref_ids: list[int] | None,
        before: tuple[list[str] | None, list[int] | None],
        after: tuple[list[str] | None, list[int] | None],
        count: Callable[[list[str] | None, list[int] | None], int] | None = None,
    ) -> Response:
        """Append the gr572723 footer: name the door when the domain's
        default ``SPACE:`` filter dropped hits. ``before``/``after`` are
        the ``(tags, include_ref_ids)`` scope without and with the
        default. ``count`` sizes the view's corpus under one scope —
        each specialized view passes a counter carrying its own
        predicates (reads' min_chars/min_reads, rulings' implied ruling
        tag + status, smells' proposal-backed nodes) so a hit the view
        never had is never reported hidden. Fail-soft — a counting
        error never breaks the search."""
        if domain is None or not domain.spaces:
            return resp
        before_tags, before_ids = before
        after_tags, after_ids = after
        try:
            if count is not None:
                total = count(before_tags, before_ids)
                kept = count(after_tags, after_ids)
            elif (folded := fold_match_all_q(q)) and folded.strip():
                total = self.store.chunks.count_chunks_lexical(
                    q=folded,
                    kind=self.kind,
                    tags=Tag.normalize_filter(before_tags, kind=self.kind),
                    include_ref_ids=before_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    distinct_refs=True,
                )
                kept = self.store.chunks.count_chunks_lexical(
                    q=folded,
                    kind=self.kind,
                    tags=Tag.normalize_filter(after_tags, kind=self.kind),
                    include_ref_ids=after_ids,
                    exclude_ref_ids=exclude_ref_ids,
                    distinct_refs=True,
                )
            else:
                total = self.store.count_refs(
                    kind=self.kind,
                    tags=Tag.normalize_filter(before_tags, kind=self.kind),
                    include_ref_ids=before_ids,
                    exclude_ref_ids=exclude_ref_ids,
                )
                kept = self.store.count_refs(
                    kind=self.kind,
                    tags=Tag.normalize_filter(after_tags, kind=self.kind),
                    include_ref_ids=after_ids,
                    exclude_ref_ids=exclude_ref_ids,
                )
        except Exception:
            log.debug("domain space footer skipped", exc_info=True)
            return resp
        dropped = total - kept
        if dropped <= 0:
            return resp
        space_list = ", ".join(f"SPACE:{s}" for s in sorted(domain.spaces))
        return replace(
            resp,
            body=(
                resp.body.rstrip()
                + f"\n── domain={domain.name}: {dropped} {self._sense()} "
                f"entr{'y' if dropped == 1 else 'ies'} outside {space_list} "
                "hidden — the door: name a space (a caller SPACE: tag "
                "wins), e.g. tags=['SPACE:research'] ──"
            ),
        )

    def _rulings_view(
        self,
        *,
        q: str | None,
        tags: list[str] | None,
        page: int,
        mode: str | None,
        scope: str | None,
        status: str | None,
        include_ref_ids: list[int] | None,
        exclude_ref_ids: list[int] | None,
    ) -> Response:
        """``search(view='rulings')``: refuse what it cannot apply (rule 6),
        then render. The ``ruling`` tag is implied; extra ``tags=`` narrow."""
        q = fold_match_all_q(q)
        unapplied = [
            name
            for name, val in (("q", q), ("mode", mode), ("scope", scope))
            if val not in (None, "")
        ]
        if unapplied or page != 1:
            raise BadInput(
                f"{'/'.join(unapplied or ['page'])}= is not applied by "
                "view='rulings' — it lists every matching ruling in one page",
                next=(
                    "search(kind='memory', tags=['ruling'], view='rulings') · "
                    "all statuses: args={'status': '*'} · find one by text: "
                    "search(kind='memory', q='<words>', tags=['ruling'])"
                ),
            )
        body, ruling_ids = render_rulings(
            self.store,
            status=status,
            tags=tags,
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
        )
        return Response(body=body, result_count=len(ruling_ids), result_ids=ruling_ids)

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
        under: str | int | None = None,
        depth: int | str | None = None,
        **_kw: Any,
    ) -> Response:
        """Body-chunk search; ``view='index'`` renders hits as index bullets.

        ``scope=`` picks where ``q`` matches: ``'title'`` = only the header
        card (title + hook), ``'body'`` = only the body prose, omitted =
        both. Anything else is refused.

        ``view='index'`` returns one ``- <Title> (me<id>[, <filename>]) —
        <hook>`` line per best-ranked memory (the session-start index shape;
        ``<filename>`` is ``meta.file_mirror.filename`` when mirrored,
        ``<hook>`` is ``meta.hook`` else the body's first line). Ranked by
        :meth:`_rerank_fused`, so the page can carry one admitted gotcha
        past ``page_size``. Any other ``view`` is refused; ``None`` is the
        unchanged default render.

        Without ``q`` the index listing is a bounded enumeration, not a
        ranked set — ``args={'page': N}`` walks it, each page bounded by
        ``page_size`` rows AND the result cap, with a ``next:`` line
        naming the following page (gr538490). Ranked searches still do
        not paginate.
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
                options=["index", "count", "reads", "rulings", "shape", "smells"],
                next=(
                    "search(kind='memory', q='<task>', tags=['SPACE:repo-dev'], "
                    "view='index')"
                ),
            )
        if not (q and q.strip()):
            if under is not None and include_ref_ids is None:
                # under= arrives here resolved (the dispatcher re-hands
                # the spelling for hint echo); a bare under with no
                # include set means a direct caller skipped dispatch —
                # refuse rather than page an unfiltered set while naming
                # under= in the next call (gr538490).
                raise BadInput(
                    "under= reached the handler without a resolved member "
                    "set — it resolves on the dispatch surface",
                    next="search(kind='memory', view='index', "
                    "args={'under': '<handle>'})",
                )
            return self._index_listing(
                tags,
                page_size,
                page,
                exclude_ref_ids,
                include_ref_ids,
                under=under,
                depth=depth,
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
            rerank=True,
        )
        search_trace.page(
            [search_trace.ref_handle(ref, block) for block, ref, _ in hits],
            rendered=True,
        )
        if not hits:
            tag_suffix = f" tagged {normalized}" if normalized else ""
            return Response(
                body=f"no {self._sense()} entries match {q!r}{tag_suffix}",
                result_count=0,
            )
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
                    size=row_size_cell(len(body_text.encode("utf-8"))),
                )
            )
        return Response(
            body="\n".join(lines),
            result_count=len(lines),
            result_ids=[int(ref.id) for _block, ref, _rank in hits],
        )

    def _body_search_hits(
        self,
        *,
        q: str,
        tags: list[str] | None,
        page_size: int,
        mode: str | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        query_vec: list[float] | None = None,
    ) -> list[SearchHit]:
        """Cross-kind stream: the same domain ``SPACE:`` default as
        ``search`` (gr572723) — a domain-bound connection's wildcard
        fan-out stays inside the domain's spaces. Machine path, no
        footer."""
        domain = self._domain_space_default(tags, None)
        if domain is not None and domain.spaces:
            space_ids = self._memory_ids_in_spaces(domain.spaces)
            include_ref_ids = (
                sorted(space_ids & set(include_ref_ids))
                if include_ref_ids is not None
                else sorted(space_ids)
            )
        return super()._body_search_hits(
            q=q,
            tags=tags,
            page_size=page_size,
            mode=mode,
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
            query_vec=query_vec,
        )

    def _rerank_fused(
        self,
        pool: list[tuple[Any, Ref, float]],
        fold_info: dict[int, tuple[float | None, bool]],
        *,
        k: int,
    ) -> tuple[list[tuple[Any, Ref, float]], int]:
        """Recall fusion over the fused pool (gr548265): semantic rank-1
        rescue, band recency prior, gotcha quota — each behind its own
        default-on env flag (:class:`~precis.utils.memory_fusion.FusionFlags`).
        The cut a rescued or admitted hit must clear is the pool's
        query-relative gap cut (``store._search_cut``, gr548264), the same
        bar the fold and the hard cut apply."""
        flags = FusionFlags.from_env()
        if not flags.any or not pool:
            return pool, 0
        ids = [int(ref.id) for _b, ref, _s in pool]
        factors = band_factors(self.store, ids) if flags.recency else {}
        gotchas: set[int] = set()
        if flags.quota:
            gotchas = {
                rid
                for rid, vals in self.store.ref_tags_bulk(ids).items()
                if GOTCHAS_TAG in {v for _ns, v in vals}
            }
        return rerank_fused(
            pool,
            fold_info,
            k=k,
            cut=semantic_fold_distance(self.kind, (d for d, _ in fold_info.values())),
            factors=factors,
            gotchas=gotchas,
            flags=flags,
        )

    def _index_listing(
        self,
        tags: list[str] | None,
        page_size: int,
        page: int,
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        under: str | int | None = None,
        depth: int | str | None = None,
    ) -> Response:
        """``view='index'`` without ``q=``: the tag-filtered memories as index
        bullets, most recently touched first (``GREATEST(last_viewed_at,
        last_recalled_at, updated_at)``).

        A page closes at ``page_size`` rows OR the context cap, whichever
        first — so ``args={'page': N}`` always resumes exactly where the
        previous page stopped, and each emitted page itself fits the cap
        (gr538490: a 50-row page used to be cut by the fitter with the
        rows between the cut and ``page_size`` silently unreachable).
        """

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
            return Response(
                body=f"no {self._sense()} entries{tag_suffix}", result_count=0
            )
        stamps = recency_stamps(self.store, [r.id for r in refs])
        oldest = datetime.min.replace(tzinfo=UTC)
        refs = sorted(refs, key=lambda r: stamps.get(r.id) or oldest, reverse=True)
        try:
            page = max(int(page or 1), 1)
            page_size = max(int(page_size or 1), 1)
        except (TypeError, ValueError):
            raise BadInput(
                f"page=/page_size= must be integers, got page={page!r} "
                f"page_size={page_size!r}",
                next="search(kind='memory', view='index', args={'page': 1})",
            ) from None
        budget = max(200, result_context_cap() - _PAGED_FOOTER_BYTES)
        pages: list[list[tuple[int, str]]] = []
        cur: list[tuple[int, str]] = []
        used = 0
        for ref in refs:
            if len(pages) == page:
                break
            body_text = self._body_text(ref)
            first = next(
                (ln.strip() for ln in body_text.splitlines() if ln.strip()),
                "",
            )
            line = bullet_line(
                ref,
                cut_hooks=False,
                filename=True,
                fallback_hook=first,
                size=row_size_cell(len(body_text.encode("utf-8"))),
            )
            cost = len(line.encode("utf-8")) + 1
            if cur and (len(cur) >= page_size or used + cost > budget):
                pages.append(cur)
                cur, used = [], 0
                if len(pages) == page:
                    break
            cur.append((int(ref.id), line))
            used += cost
        if cur and len(pages) < page:
            pages.append(cur)
        if len(pages) < page:
            return Response(
                body=f"no {self._sense()} entries on page {page}", result_count=0
            )
        page_rows = pages[page - 1]
        lines = [ln for _rid, ln in page_rows]
        body = "\n".join(lines)
        shown = sum(len(c) for c in pages)
        more = len(refs) - shown
        if more > 0:
            args_echo = [f"'page': {page + 1}", f"'page_size': {page_size}"]
            if under is not None:
                args_echo.append(f"'under': {under!r}")
                if depth is not None:
                    args_echo.append(f"'depth': {depth!r}")
            elif include_ref_ids is not None:
                # The filter came from a facet the handler can't re-spell
                # (cited=/raw include_ref_ids=) — echo the resolved ids so
                # the next call pages the same set.
                args_echo.append(
                    f"'include_ref_ids': {sorted({int(i) for i in include_ref_ids})}"
                )
            if exclude_ref_ids:
                args_echo.append(
                    f"'exclude_ref_ids': {sorted({int(i) for i in exclude_ref_ids})}"
                )
            tags_echo = f", tags={[str(t) for t in normalized]!r}" if normalized else ""
            body += (
                f"\n… {more} more — next: search(kind='memory'{tags_echo}, "
                f"view='index', args={{{', '.join(args_echo)}}})"
            )
        return Response(
            body=body,
            result_count=len(lines),
            result_ids=[rid for rid, _ln in page_rows],
        )

    # ── delete: soft-delete + edge cleanup (gr503031) ───────────────

    def delete(
        self,
        *,
        id: str | int | None = None,
        reason: str | None = None,
        dry_run: bool = False,
        superseded_by: str | None = None,
        force: bool = False,
        **_kw: Any,
    ) -> Response:
        """Preview or retire with an explicit disposition for live prose mentions."""
        return self._delete(
            id, reason=reason, dry_run=dry_run, superseded_by=superseded_by, force=force
        )

    def _retire_successor(
        self, raw: str, ref_id: int, *, conn: Connection
    ) -> tuple[str, int | None, str]:
        """Validate a replacement; file- and index-backed successors have no ref."""
        if raw.startswith("md:"):
            address = raw.removeprefix("md:")
            if "~" not in address or not address.rsplit("~", 1)[1]:
                raise BadInput(
                    "an md successor requires a docs path and section",
                    next="superseded_by='md:main/docs/<file>.md~<heading>'",
                )
            handler = self.hub.handler_for("md")
            if handler is None:
                raise BadInput(
                    "md successor validation requires the md handler",
                    next="use a live record handle or enable the md reader",
                )
            handler.get(id=address)
            return raw, None, "md"
        if raw.startswith("python:"):
            address = raw.removeprefix("python:")
            if "::" not in address and "/" not in address:
                raise BadInput(
                    "a python successor requires a symbol or file address",
                    next="superseded_by='python:<alias>::<qualname>' or "
                    "'python:<alias>/<path>.py'",
                )
            handler = self.hub.handler_for("python")
            if handler is None:
                raise BadInput(
                    "python successor validation requires the python handler",
                    next="use a live record handle or configure python roots",
                )
            handler.get(id=address)
            return raw, None, "python"
        if raw.isdecimal():
            raw = f"me{raw}"
        try:
            target = parse_link_target(raw, store=self.store)
        except NotFound as exc:
            raise BadInput(
                f"superseded_by must name a live successor: {raw}",
                next=f"get(id={raw!r})",
            ) from exc
        if target.ref_id == ref_id:
            raise BadInput(
                "a memory cannot supersede itself",
                next="superseded_by='<different live handle>'",
            )
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR SHARE",
            (target.ref_id,),
        ).fetchone()
        if row is None:
            raise BadInput(
                "superseded_by must name a live successor",
                next=f"get(id={raw!r})",
            )
        formatted = handle_registry.try_format(str(row[0]), target.ref_id)
        if formatted is None:
            raise BadInput(
                f"superseded_by kind {row[0]!r} has no record handle",
                next="use a live record handle or an md:path~section successor",
            )
        address = formatted
        if target.pos is not None:
            # The garden's successor resolver follows records and md sections,
            # not chunks. Refuse rather than store an address it would lose.
            raise BadInput(
                "superseded_by cannot name a chunk",
                next=f"use the record handle {address!r} or an md:path~section successor",
            )
        return address, target.ref_id, str(row[0])

    def _delete_successor(
        self,
        ref_id: int,
        meta: dict[str, Any],
        *,
        conn: Any,
        superseded_by: str | None = None,
        reason: str | None = None,
    ) -> tuple[str | None, int | None, str | None]:
        """Resolve the delete route before mutation so garden can snapshot it."""
        raw_sup = superseded_by or meta.get("superseded_by")
        if superseded_by is None:
            inbound: list[str] = []
            for rid, kind in conn.execute(
                "SELECT DISTINCT r.ref_id, r.kind FROM links l JOIN refs r "
                "ON r.ref_id = l.src_ref_id WHERE l.dst_ref_id = %s "
                "AND l.relation = 'supersedes' AND r.retired_at IS NULL "
                "AND r.ref_id <> %s ORDER BY r.ref_id",
                (ref_id, ref_id),
            ):
                address = handle_registry.try_format(str(kind), int(rid))
                if address is not None:
                    inbound.append(address)
                    if len(inbound) == 2:
                        break
            if len(inbound) > 1:
                raise BadInput(
                    "multiple live successors supersede this memory; choose one",
                    next=[
                        f"delete(kind='memory', id={ref_id}, superseded_by={address!r})"
                        for address in inbound
                    ],
                )
            if raw_sup is None and inbound:
                raw_sup = inbound[0]
        if raw_sup is None and reason:
            named = self._reason_named_ref({"reason": reason})
            if (
                named is not None
                and named != ref_id
                and self.store.get_ref(kind="memory", id=named, conn=conn) is not None
            ):
                raw_sup = f"me{named}"
        if raw_sup is None:
            return None, None, None
        return self._retire_successor(str(raw_sup), ref_id, conn=conn)

    def _delete(
        self,
        id: str | int | None,
        *,
        reason: str | None = None,
        dry_run: bool = False,
        superseded_by: str | None = None,
        force: bool = False,
    ) -> Response:
        """Scan, guard, record the redirect and retire atomically; preview writes nothing."""
        from precis.handlers._memory_retire import scan_mentions

        if id is None:
            raise BadInput(
                "delete requires id=",
                next=f"delete(kind={self.kind!r}, id=N)",
            )
        ref_id = self._coerce_id(id)
        self._resolve_ref_for_delete(ref_id)
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT retired_at, meta FROM refs WHERE ref_id = %s "
                "AND kind = 'memory' FOR NO KEY UPDATE",
                (ref_id,),
            ).fetchone()
            if row is None or row[0] is not None:
                raise Gone(
                    f"memory {ref_id} is no longer live",
                    next=f"get(kind='memory', id={ref_id}, view='retired')",
                )
            successor_handle, successor, successor_kind = self._delete_successor(
                ref_id,
                row[1] or {},
                conn=conn,
                superseded_by=superseded_by,
                reason=reason,
            )
            report = scan_mentions(conn, ref_id)
            receipt = report.render(
                f"me{ref_id}",
                datetime.now(UTC).date().isoformat() + " UTC",
                successor_handle or reason or "retired without a successor",
            )
            blocked = report.blocking and successor_handle is None and not force
            if dry_run:
                verdict = (
                    "would refuse: live mentions need superseded_by= or args={'force': True}"
                    if blocked
                    else "would retire"
                )
                replacement = (
                    f"; successor {successor_handle}" if successor_handle else ""
                )
                return Response(
                    body=f"dry run: {verdict} memory id={ref_id}{replacement}\n{receipt}"
                )
            if blocked:
                raise BadInput(
                    f"cannot retire me{ref_id}: {report.blocking} live mention(s)\n{receipt}",
                    next=[
                        f"delete(kind='memory', id={ref_id}, superseded_by='<live handle>')",
                        f"delete(kind='memory', id={ref_id}, args={{'force': True}})",
                    ],
                )
            if successor_handle is not None:
                self.store.stamp_ref_meta(
                    ref_id, {"superseded_by": successor_handle}, conn=conn
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
                moved = self.store.migrate_links(
                    ref_id, successor, conn=conn, preserve_supersedes=True
                )
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
                f"{moved} link(s) migrated to {successor_kind} id={successor} "
                f"(meta.superseded_by)"
            )
        else:
            kept = f"; {n_sup} supersedes edge(s) kept" if n_sup else ""
            detail = (
                f"{n_in} inbound + {n_out} outbound link(s) removed{kept}"
                if (n_in or n_out or removed or n_sup)
                else "no links to remove"
            )
        return Response(
            body=f"deleted {self._sense()} id={ref_id} — {detail}\n{receipt}"
        )

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
            # The survivor's fresh body + header card embed within seconds,
            # like any other memory write (gr563669).
            nudge_fresh_embed(self.store, conn=conn, reason="supersede memory")

        merged = ", ".join(str(m) for m in ids)
        resp = self._with_attribution_advisory(
            Response(
                body=(
                    f"superseded memories [{merged}] → new memory id={survivor.id} "
                    f"(originals soft-deleted, links migrated, tagged "
                    f"{_DREAM_CONSOLIDATED})"
                )
            ),
            misses,
        )
        # Links just migrated onto the survivor — re-check spec-root
        # placement on the merged result (gr631564).
        adv = spec_root_advisory(self.store, survivor.id)
        if adv is not None:
            resp = Response(body=f"{resp.body}\n\n{adv}", cost=resp.cost)
        return resp


def _roundtrip_hint(handler: Any, ref_id: int) -> list[tuple[str, str]]:
    """The create-ack recall check (gr520279): ``view='index'`` recall
    applies the shared gap/corpus cut, so a memory
    whose title/hook lacks the task words silently never comes back. One
    prefilled line telling the writer to verify — in every space, not
    just hub-shaped ones. Never raises, like ``link_hints``."""
    try:
        ref = handler.store.get_ref(kind="memory", id=ref_id)
        me = handle_registry.try_format("memory", ref_id) or f"me{ref_id}"
        return _roundtrip_rows(str(ref.title or "") if ref else "", me)
    except Exception:
        log.debug("memory round-trip hint skipped for %s", ref_id, exc_info=True)
        return []


def _roundtrip_rows(title: str, me: str) -> list[tuple[str, str]]:
    """The round-trip row for a memory titled ``title`` shown as ``me``."""
    title = " ".join(title.split())[:80]
    if not title:
        return []
    return [
        (
            f"search(kind='memory', q={title!r}, view='index')",
            f"round-trip: if {me} is not listed, ranking or the recall "
            "gap/corpus cut may hide it — put the task words in title=/meta.hook",
        )
    ]


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
