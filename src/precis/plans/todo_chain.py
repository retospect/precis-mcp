"""The todo-sourced build chain: the names the dispatcher, the claim
path and the rescue pass share.

A chain minted from a plain todo (not a gripe) hangs off its source the
way a gripe chain does: the plan root is ``related-to`` the source todo
and carries ``meta.source_todo`` = the source's integer ref id. The
``related-to`` edge alone never makes a todo chain — any other todo a
root links to stays a plain relation — so the key is what the chain
inventory (``workers.gripe_dispatch``) and the source holds
(``workers.executors._common.source_todo_holds``) match on.

The brief step's three outcomes live here too: :func:`parse_brief` reads the
session's final message, :func:`post_brief` copies a brief onto the build
node — carrying the source revision the writer read — and
:func:`refuse_source` acts on a refusal (:func:`close_built` on one
that proves the todo already built, closing the source with no ask) and
:func:`split_source` on a split into two to four sequenced part todos
(:func:`sweep_splits` closes the source when every part is done, and a
parent tagged ``factory:split`` when every child todo is). The plan tick
(``plan_dispatch._step_todo_briefs``) is their only caller.

The source todo carries the chain's two completion marks. The land
(``land_slice``) writes a ``landed: <sha>`` comment on it and leaves its
status alone; when that sha is in the deployed build :func:`close_source`
(the tick's ``sha-in-prod`` gate, :func:`sweep_landed_sources` as the
backstop) sets it done. A chain that never landed never closes its source.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.plans.nodes import finish_mint
from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.asks_view import TRIAGE_PREFIX
from precis.todo.guards import check_ask_meta

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: Plan-root ``meta`` key holding the source todo's ref id (an integer).
SOURCE_TODO_KEY = "source_todo"
#: Open tag that makes a todo eligible for the factory; withdrawing it
#: holds the chain built from it.
FACTORY_BUILD_TAG = "factory:build"
#: Open tag on a todo the factory refused to build: it holds the source
#: until a human rewrites or rules on it.
NEEDS_SPEC_TAG = "needs-spec"
#: Open tag the intake session puts on a todo for another lane or a fenced
#: path: it wins over :data:`FACTORY_BUILD_TAG` and holds the source.
SKIP_TAG = "factory:skip"
#: Open tag on a source the brief step closed as already built: the same one
#: the triage session uses, so one search lists every such close and
#: removing it with a reopen undoes one.
CLOSED_BUILT_TAG = "triage:closed-built"
#: Open tag a session or person puts on a parent todo they split by hand:
#: :func:`sweep_splits` closes it when every child todo is done.
SPLIT_PARENT_TAG = "factory:split"

#: The reasons a brief session may refuse a source todo: it does not say
#: what should change (``rewrite``), it is more than one reviewable change
#: (``split``), it needs a decision only a person can make (``decide``),
#: the code already does it (``built``, with commit or ``file:line``
#: evidence), it is not a code change (``not-code``: a run, a measurement,
#: a backfill, a production step).
REFUSAL_REASONS: tuple[str, ...] = ("rewrite", "split", "decide", "built", "not-code")

#: Longest part title, and longest value of one of a part's three lines.
#: A ``split:`` message rides the ``handoff:`` comment ``run_slice`` clips
#: at its handoff cap (the brief's closing section asks for under 5000
#: characters): a four-part split at both caps — ``split: 4`` plus four
#: ``part k:`` / ``Change:`` / ``Where:`` / ``Done when:`` frames (~160
#: characters of labels) — stays under both, so no valid split is clipped.
_PART_TITLE_CAP = 120
_PART_FIELD_CAP = 350

#: What a brief session ends with — the one place the closing section in
#: ``run_slice`` and the verdict parser share. Every outcome rides the
#: FIRST line of the final message; a ``brief:`` also names the source
#: revision the writer read on the SECOND line (``Read: td<id> at
#: <updated_at>``), which the brief the tick posts carries verbatim. Any
#: other ending is a failed attempt.
BRIEF_GRAMMAR = (
    "`brief: td<build id>` as the FIRST line of the final message, "
    "`Read: td<source id> at <updated_at>` as the SECOND (the source "
    "revision read, copied verbatim from the todo), the brief text on "
    "the following lines; or "
    f"`refuse: <{'|'.join(REFUSAL_REASONS)}> — <what is missing, or the "
    "evidence>` as the FIRST line (`built` needs evidence you read yourself, "
    "written in the refusal's text: a done todo or closed gripe that built "
    "the same thing, by handle, plus the file holding the code; or the "
    "todo's `Done when:` observation already holding, with what you ran or "
    "read; or a commit that names the change, by sha — `superseded` and "
    "`no longer wanted` are `decide`; a `built` text naming no handle, sha "
    "or file goes to the owner as an ask); or `split: <n>` as the FIRST line, then "
    "for each part, in build order, a line `part <k>: <title>` followed by "
    "its three lines `Change:`, `Where:` and `Done when:` (a title is cut at "
    f"{_PART_TITLE_CAP} characters, each line at {_PART_FIELD_CAP}) — use it "
    "only when the todo is two to four reviewable changes with no decision "
    "between them"
)

#: The marker ``run_slice`` puts in the middle of a ``handoff:`` comment it
#: had to clip at its cap: a message carrying it lost its middle, so a
#: brief read from it is not the brief the session wrote.
CLIP_MARKER = "…(clipped)…"

#: The chunk-``meta`` key with which ``run_slice`` signs the ``handoff:``
#: comment a job posts: the comment is THAT job's outcome, so the tick
#: binds its read to the job and a later ``handoff:`` comment — a human's
#: ordinary note — decides nothing.
HANDOFF_JOB_KEY = "handoff_job"

#: ``meta`` key on the ask :func:`refuse_source` files: ``{source, reason,
#: root}``, the marker later code finds a refusal ask by.
REFUSAL_KEY = "todo_refusal"
#: ``meta`` key on a split source: ``{root, parts: [ids]}``, written by
#: :func:`split_source` and read by :func:`sweep_splits`.
SPLIT_KEY = "todo_split"
#: ``meta`` key on a part todo: ``{source, index, of, root}``. A todo that
#: carries it is never split again.
SPLIT_PART_KEY = "todo_split_part"
#: The most parts a split may have; more is a refusal with reason ``split``.
MAX_SPLIT_PARTS = 4

#: What a refusal's first line puts between the reason and the why: an em
#: dash, en dash or colon, or a hyphen followed by whitespace (a bare hyphen
#: would read ``rewrite-it`` as the reason ``rewrite``).
_REFUSE_REST_RE = re.compile(r"\s*(?:[—–:]|-(?=\s))\s*(\S.*)", re.DOTALL)
_BRIEF_HEAD_RE = re.compile(r"brief:\s*td(\d+)", re.IGNORECASE)
_REFUSE_HEAD_RE = re.compile(r"refuse:\s*", re.IGNORECASE)
_HANDOFF_PREFIX_RE = re.compile(r"\s*handoff:", re.IGNORECASE)
_SPLIT_HEAD_RE = re.compile(r"split:\s*(\d{1,3})", re.IGNORECASE)
_PART_HEAD_RE = re.compile(r"\s*part\s+(\d{1,3})\s*:\s*(\S.*)", re.IGNORECASE)
_PART_LABEL_RE = re.compile(r"\s*(change|where|done when)\s*:(.*)", re.IGNORECASE)
#: A brief's mandatory second line — ``Read: td<id> at <stamp>`` — the
#: source revision the writer read, reported by the writer itself. The
#: stamp the posted brief carries is what the dispatch gate re-checks
#: against the live source: a source that moved past it re-briefs the
#: chain instead of building from a stale brief. The stamp is the rest
#: of the line — the raw view's form holds a space between date and
#: time — and :func:`norm_stamp` decides whether it parses.
_READ_LINE_RE = re.compile(r"read:\s*td(\d+)\s+at\s+(.+?)\s*$", re.IGNORECASE)
#: The stamp's comparison form: seconds-precision UTC, fixed width, so
#: lexicographic order is chronological order.
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_COMMENT_KIND = "todo_comment"
#: Longest stretch of the session's refusal text quoted into the ask.
_WHY_CAP = 1500


def read_line(source_id: int, stamp: str) -> str:
    """The mandatory second line of a brief: the revision the writer
    read, in :data:`_STAMP_FORMAT`."""
    return f"Read: td{source_id} at {stamp}"


def parse_read_line(line: str) -> tuple[int, str] | None:
    """``(source_id, normalized stamp)`` from a ``Read:`` line, or
    ``None`` when the line is not one or the stamp does not parse."""
    m = _READ_LINE_RE.match(line.strip())
    if m is None:
        return None
    stamp = norm_stamp(m.group(2))
    return (int(m.group(1)), stamp) if stamp is not None else None


def norm_stamp(value: str) -> str | None:
    """A writer-reported ``updated_at`` in the comparison form
    (:data:`_STAMP_FORMAT`), or ``None`` when it does not parse — a
    stamp the writer garbled is no stamp at all."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return str(parsed.astimezone(UTC).strftime(_STAMP_FORMAT))


def source_updated_at(
    store: Store, source_id: int, conn: Connection | None = None
) -> str:
    """The source todo's ``updated_at`` in :data:`_STAMP_FORMAT` — the
    revision a brief's carried read stamp is compared against
    (``"unknown"`` when the row carries none)."""
    ref = (
        store.get_ref(kind="todo", id=source_id)
        if conn is None
        else store.get_ref(kind="todo", id=source_id, conn=conn)
    )
    stamp = getattr(ref, "updated_at", None)
    if stamp is None:
        return "unknown"
    return str(stamp.astimezone(UTC).strftime(_STAMP_FORMAT))


@dataclass(frozen=True, slots=True)
class Brief:
    """A brief outcome: the session named this build node, reported the
    source revision it read — ``read`` is ``(source_id, stamp)`` — and
    wrote ``text`` for it."""

    build_id: int
    text: str
    read: tuple[int, str]


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal outcome: ``reason`` is one of :data:`REFUSAL_REASONS`,
    ``why`` what the session said is missing, or the evidence."""

    reason: str
    why: str


@dataclass(frozen=True, slots=True)
class SplitPart:
    """One part of a split: its title and its three labelled lines."""

    title: str
    change: str
    where: str
    done_when: str


@dataclass(frozen=True, slots=True)
class Split:
    """A split outcome: ``parts`` in build order, as many as the session
    named in its ``split: <n>`` line (so possibly more than
    :data:`MAX_SPLIT_PARTS`: the caller turns that into a refusal)."""

    parts: tuple[SplitPart, ...]


BriefOutcome = Brief | Refusal | Split


def _parse_split(count: int, rest: list[str]) -> Split | None:
    """The parts of a ``split: <count>`` message, or ``None`` when it is
    malformed: fewer than two parts, a count that does not match the parts
    given, parts not numbered 1..n in order, text before the first part or
    between a part's title and its first label, a label missing or given
    twice, or an empty field."""
    if count < 2:
        return None
    blocks: list[tuple[str, dict[str, list[str]]]] = []
    field: list[str] | None = None
    for line in rest:
        head = _PART_HEAD_RE.fullmatch(line)
        if head is not None:
            if int(head.group(1)) != len(blocks) + 1:
                return None
            blocks.append((head.group(2).strip(), {}))
            field = None
            continue
        if not line.strip():
            if field is not None:
                field.append("")
            continue
        if not blocks:
            return None
        label = _PART_LABEL_RE.fullmatch(line)
        if label is not None:
            key = label.group(1).lower()
            if key in blocks[-1][1]:
                return None
            field = [label.group(2)]
            blocks[-1][1][key] = field
        elif field is None:
            return None
        else:
            field.append(line)
    if len(blocks) != count:
        return None
    parts: list[SplitPart] = []
    for title, fields in blocks:
        text = {k: "\n".join(v).strip() for k, v in fields.items()}
        if set(text) != {"change", "where", "done when"} or not all(text.values()):
            return None
        parts.append(SplitPart(title, text["change"], text["where"], text["done when"]))
    return Split(tuple(parts))


def parse_brief(text: str) -> BriefOutcome | None:
    """The outcome of a brief session's final message, or ``None``.

    Decided by the FIRST non-empty line alone (after the optional
    Decided by the FIRST non-empty line alone (after the optional
    ``handoff:`` prefix the posted comment carries): ``brief: td<id>``
    with a ``Read:`` line second — the source revision the writer read,
    see :func:`parse_read_line` — and at least one more non-empty line,
    ``refuse: <reason> <dash> <why>`` with ``<reason>`` in
    :data:`REFUSAL_REASONS`, or ``split: <n>`` with
    ``n`` well-formed parts (:func:`_parse_split`; an ``n`` above
    :data:`MAX_SPLIT_PARTS` still parses, the caller refuses it). Anything
    else, a malformed ``split:`` message included, is ``None``
    — a failed step, never a pass to the build. A message carrying
    :data:`CLIP_MARKER` is ``None``: a cut brief is not a brief. A
    ``brief:``, ``refuse:`` or ``split:`` line further down, or inside a
    quoted block, decides nothing: it is not the first line."""
    if CLIP_MARKER in text:
        return None
    m = _HANDOFF_PREFIX_RE.match(text)
    lines = (text[m.end() :] if m else text).strip().splitlines()
    if not lines:
        return None
    first, rest = lines[0].strip(), "\n".join(lines[1:]).strip()
    brief = _BRIEF_HEAD_RE.fullmatch(first)
    if brief is not None:
        read = parse_read_line(lines[1]) if len(lines) > 1 else None
        if read is None:
            return None
        body = "\n".join(lines[2:]).strip()
        return Brief(int(brief.group(1)), body, read) if body else None
    if first.lower().startswith("split:"):
        head = _SPLIT_HEAD_RE.fullmatch(first)
        return _parse_split(int(head.group(1)), lines[1:]) if head else None
    refuse = _REFUSE_HEAD_RE.match(first)
    if refuse is None:
        return None
    tail = first[refuse.end() :]
    # Longest first: ``not-code`` holds a hyphen the dash set would
    # otherwise read as the separator.
    for reason in sorted(REFUSAL_REASONS, key=len, reverse=True):
        if not tail.startswith(reason):
            continue
        why = _REFUSE_REST_RE.fullmatch(tail[len(reason) :])
        if why is None:
            return None
        return Refusal(reason, "\n".join(p for p in (why.group(1), rest) if p))
    return None


def brief_comment_text(
    source_id: int, brief_node_id: int, text: str, read: tuple[int, str]
) -> str:
    """The build node's ``brief:`` comment: the header the idempotency
    check (and a human) reads, the carried ``Read:`` stamp, then the
    brief itself."""
    return (
        f"brief: written from td{source_id} by brief node td{brief_node_id}\n"
        f"{read_line(*read)}\n\n{text.strip()}"
    )


def post_brief(
    store: Store,
    build_id: int,
    source_id: int,
    brief_node_id: int,
    text: str,
    read: tuple[int, str],
    *,
    conn: Connection | None = None,
) -> bool:
    """Post the brief on the build node as one ``brief:`` comment — the form
    ``run_slice`` reads its builder's brief in — and return whether it did.

    The comment carries ``read`` — the source revision the writer read —
    as its ``Read:`` line: the dispatch gate compares it against the live
    source and re-briefs a chain whose source moved on. Idempotent: a
    build node that already carries this brief node's comment with the
    same read stamp gets nothing; a comment from this brief node with an
    older stamp (the source moved and the chain re-briefed) is replaced,
    so the fresh brief is the only one a builder can read. The comment is
    not worker-stamped, so the dispatch route reads it like any
    coordinator brief."""
    if conn is None:
        with store.tx() as own:
            return post_brief(
                store, build_id, source_id, brief_node_id, text, read, conn=own
            )
    marker = re.compile(rf"brief node td{brief_node_id}\b")
    existing = [
        c
        for c in store.chunks.list_chunks_for_ref(build_id, conn=conn)
        if c.chunk_kind == _COMMENT_KIND
        and str(c.text or "").lstrip().lower().startswith("brief:")
        and marker.search(str(c.text or "").lstrip().splitlines()[0])
    ]
    for c in existing:
        if any(
            parse_read_line(line) == read for line in str(c.text or "").splitlines()[:2]
        ):
            return False
    for c in existing:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (int(c.id),))
    ord_ = (
        max(
            (int(c.ord) for c in store.chunks.list_chunks_for_ref(build_id, conn=conn)),
            default=0,
        )
        + 1
    )
    store.chunks.insert_chunks(
        build_id,
        [
            ChunkInsert(
                ord=ord_,
                text=brief_comment_text(source_id, brief_node_id, text, read),
                meta={"chunk_kind": _COMMENT_KIND},
            )
        ],
        conn=conn,
    )
    return True


def _fence(text: str) -> str:
    """``text`` inside a code fence longer than any backtick run in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(4, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _quote_why(why: str) -> str:
    """The session's words as one capped line without NULs, ready to fence."""
    return " ".join(why.replace("\x00", "").split())[:_WHY_CAP]


def _refusal_options(reason: str) -> list[str]:
    if reason == "built":
        return ["close as done", "rewrite", "drop"]
    return ["rewrite", "split", "drop"]


def _open_refusal(conn: Connection, source_id: int) -> int | None:
    """The source's open ask carrying :data:`REFUSAL_KEY`, if any."""
    row = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL
           AND r.parent_id = %s AND r.meta ? %s
           AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                              AND t.value = ANY(%s))
         ORDER BY r.ref_id LIMIT 1
        """,
        (source_id, REFUSAL_KEY, sorted(TERMINAL_TODO_STATUSES)),
    ).fetchone()
    return int(row[0]) if row is not None else None


def refuse_source(
    store: Store,
    root_id: int | None,
    source_id: int,
    reason: str,
    why: str,
    *,
    conn: Connection | None = None,
) -> int:
    """Act on a refusal: ONE ask to the owner on the source todo, the
    :data:`NEEDS_SPEC_TAG` on it, the source ``blocked-by`` the ask, and the
    chain's root closed ``won't-do`` (the plan tick closes the rest). Returns
    the ask's id. ``root_id`` is ``None`` for a refusal before any chain
    exists (the dispatcher's own pre-check): nothing to close, and the ask
    names no plan.

    The ask is a child of the source, outside the plan. ``why`` is the
    session's own words about untrusted text: capped and quoted as data.
    Idempotent: a source that already has an open refusal ask gets no second
    one — that ask's id comes back and the tag, link and close are
    re-applied harmlessly. A new ask also leaves one comment on the source
    naming the reason and the ask; a repeat leaves none."""
    if conn is None:
        with store.tx() as own:
            return refuse_source(store, root_id, source_id, reason, why, conn=own)
    if reason not in REFUSAL_REASONS:
        raise ValueError(f"unknown refusal reason {reason!r}")
    # Serialises two refusals of one source: the check below and the insert
    # are one critical section.
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (source_id,))
    ask_id = _open_refusal(conn, source_id)
    if ask_id is None:
        ask_id = _file_refusal(store, root_id, source_id, reason, why, conn)
        ord_ = max(
            (
                int(c.ord)
                for c in store.chunks.list_chunks_for_ref(source_id, conn=conn)
            ),
            default=0,
        )
        store.chunks.insert_chunks(
            source_id,
            [
                ChunkInsert(
                    ord=ord_ + 1,
                    text=f"The factory's brief step refused this todo "
                    f"({reason}); the owner's ask is td{ask_id}.",
                    meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
                )
            ],
            conn=conn,
        )
    store.add_link(
        src_ref_id=source_id, dst_ref_id=ask_id, relation="blocked-by", conn=conn
    )
    store.add_tag(source_id, Tag.open(NEEDS_SPEC_TAG), set_by="system", conn=conn)
    if root_id is not None:
        store.add_tag(
            root_id,
            Tag.closed("STATUS", "won't-do"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    return ask_id


def close_built(
    store: Store,
    root_id: int,
    source_id: int,
    brief_node_id: int,
    why: str,
    *,
    hold: Callable[[Connection], str | None] | None = None,
    conn: Connection | None = None,
) -> bool:
    """Act on a ``built`` refusal: close the source as already built, with no
    ask. In ONE transaction under the source's row lock, when the source is
    live, ``STATUS:open``, not held (``hold(conn)``, the caller's read of the
    source's holds, returns no reason: a callable because ``plans`` never
    imports ``workers``), the evidence names a handle, sha or file
    (:func:`_names_evidence`) and it has no open child todo (a refusal ask
    counts):
    one ``triage: closed as built by the factory's brief step`` comment
    followed by ``why`` quoted as data, the :data:`CLOSED_BUILT_TAG`,
    ``factory:build`` removed, :func:`set_source_done`, and the chain's root
    closed ``won't-do`` (the plan tick closes the rest). Returns whether the
    source is closed. ``False`` writes NOTHING: the caller files the ordinary
    ask.

    Idempotent: a done source that already carries this chain's comment
    returns ``True`` and gets nothing written; an open one that carries it
    (a person reopened it) returns ``False``, as a chain closes its source
    at most once."""
    if conn is None:
        with store.tx() as own:
            return close_built(
                store, root_id, source_id, brief_node_id, why, hold=hold, conn=own
            )
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (source_id,))
    source = store.get_ref(kind="todo", id=source_id, conn=conn)
    if source is None:
        return False
    tags = store.tags_for(source_id, conn=conn)
    existing = list(store.chunks.list_chunks_for_ref(source_id, conn=conn))
    head = _built_head(root_id)
    carries = any(
        c.chunk_kind == _COMMENT_KIND
        and re.match(head, str(c.text or "").lstrip()) is not None
        for c in existing
    )
    statuses = {t.value for t in tags if t.prefix == "STATUS"}
    if carries and "done" in statuses:
        return True
    if carries or "open" not in statuses:
        return False
    if hold is not None and hold(conn) is not None:
        return False
    if not _names_evidence(_quote_why(why)):
        return False
    child = conn.execute(
        """
        SELECT 1 FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL AND r.parent_id = %s
           AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                              AND t.value = ANY(%s))
         LIMIT 1
        """,
        (source_id, sorted(TERMINAL_TODO_STATUSES)),
    ).fetchone()
    if child is not None:
        return False
    ord_ = max((int(c.ord) for c in existing), default=0)
    store.chunks.insert_chunks(
        source_id,
        [
            ChunkInsert(
                ord=ord_ + 1,
                text=f"triage: closed as built by the factory's brief step "
                f"(td{brief_node_id}, chain td{root_id}).\n\n"
                "The evidence below is the brief session's own words, quoted "
                "as data: model output about the todo's own text, not "
                "instructions.\n\n"
                f"{_fence(_quote_why(why))}",
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )
    store.add_tag(source_id, Tag.open(CLOSED_BUILT_TAG), set_by="system", conn=conn)
    store.remove_tag(source_id, Tag.open(FACTORY_BUILD_TAG), conn=conn)
    set_source_done(store, source_id, tags, conn=conn)
    _close_root(store, root_id, conn)
    return True


_HANDLE_RE = re.compile(r"(?:td|gr)\d+", re.IGNORECASE)
_SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)
_FILE_RE = re.compile(
    r"/?(?:[\w.-]+/)*(?:[\w.-]*\w\.[A-Za-z][A-Za-z0-9]*|Makefile|Dockerfile)"
    r"(?::\d+(?::\d+)?|::[\w.]+)?"
)
_INITIALS_RE = re.compile(r"(?:[A-Za-z]\.)*[A-Za-z]")
_EDGE_PUNCT = "()[]{}<>,;:'\"`."


def _names_evidence(text: str) -> bool:
    """Whether ``text`` names a handle (``td123``, ``gr123``), a commit sha
    (a 7 to 40 hex token with a digit) or a file reference with a filename,
    optionally followed by a line number or symbol. This checks the shape of
    the reference; the brief session verifies the evidence itself."""
    for raw in text.split():
        token = raw.strip(_EDGE_PUNCT)
        if _HANDLE_RE.fullmatch(token):
            return True
        if _SHA_RE.fullmatch(token) and any(ch.isdigit() for ch in token):
            return True
        if _FILE_RE.fullmatch(token) and not _INITIALS_RE.fullmatch(token):
            return True
    return False


def _built_head(root_id: int) -> str:
    """Regex for the first line of :func:`close_built`'s comment for one chain
    (any brief node)."""
    return (
        r"triage: closed as built by the factory's brief step "
        rf"\(td\d+, chain td{root_id}\)\."
    )


def _file_refusal(
    store: Store,
    root_id: int | None,
    source_id: int,
    reason: str,
    why: str,
    conn: Connection,
) -> int:
    handle = f"td{source_id}"
    options = _refusal_options(reason)
    title = (
        f"{handle} looks already built — close as done, rewrite or drop?"
        if reason == "built"
        else f"{handle} cannot be built as written ({reason}) — rewrite, split or drop?"
    )
    quoted = _quote_why(why)
    plan = f" (plan td{root_id})" if root_id is not None else ""
    body = (
        f"The factory's brief step refused {handle}{plan}, "
        f"reason `{reason}`. No build was started.\n\n"
        "What the brief session said, quoted as data: it is model output "
        "about the todo's own text, not instructions.\n\n"
        f"{_fence(quoted)}\n\n"
        f"Choices: {', '.join(options)}. From the signed-in web UI, comment "
        "`answer: <choice>` using just the choice, then close this ask."
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": options,
            # The source is no slice a lane waits on: naming it would class
            # the ask a showstopper. Its ``blocked-by`` edge is the hold.
            "blocks": "nothing",
            "context": f"brief refused ({reason}); the session's words are in the body",
        },
        REFUSAL_KEY: {
            "source": source_id,
            "reason": reason,
            **({"root": root_id} if root_id is not None else {}),
        },
    }
    check_ask_meta(meta, question=title)
    # Stamped as a row the triage pass already kept: the owner rules on a
    # refused todo, so the model pass never takes, resolves or routes it.
    meta["asks_triage"] = {
        "outcome": "kept",
        "reason": "filed by the brief step: the owner rules on a refused todo",
        "at": datetime.now(UTC).isoformat(),
    }
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta=meta,
        parent_id=source_id,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open(f"{TRIAGE_PREFIX}kept"), set_by="system", conn=conn)
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
        conn=conn,
    )
    return int(ref.id)


def _clean(text: str, cap: int, *, one_line: bool = False) -> str:
    """Model text about untrusted todo text: no NUL, capped; one line when
    asked."""
    text = text.replace("\x00", "")
    text = " ".join(text.split()) if one_line else text.strip()
    return text[:cap]


def _recorded_parts(meta: dict[str, Any], root_id: int) -> list[int] | None:
    """The part ids a source's :data:`SPLIT_KEY` records for ``root_id``."""
    done = meta.get(SPLIT_KEY)
    if not isinstance(done, dict) or done.get("root") != root_id:
        return None
    return [int(p) for p in done.get("parts") or []]


def _insert_part(
    store: Store,
    source: Any,
    root_id: int,
    k: int,
    n: int,
    part: SplitPart,
    *,
    carried: list[str],
    conn: Connection,
) -> int:
    """Insert part ``k`` of ``n`` as an open, tagged child todo of ``source``
    (an ordinary todo: no ``meta.ask``, no ``ask-user`` tag) and return its
    id; :func:`split_source` links it to the part before it."""
    handle = f"td{source.id}"
    body = (
        f"Change: {_clean(part.change, _PART_FIELD_CAP)}\n"
        f"Where: {_clean(part.where, _PART_FIELD_CAP)}\n"
        f"Done when: {_clean(part.done_when, _PART_FIELD_CAP)}\n\n"
        f"Part {k} of {n} of {handle}, split by the factory's brief step."
    )
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=_clean(part.title, _PART_TITLE_CAP, one_line=True),
        meta={
            SPLIT_PART_KEY: {
                "source": int(source.id),
                "index": k,
                "of": n,
                "root": root_id,
            }
        },
        parent_id=int(source.id),
        prio=source.prio,
        conn=conn,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open(FACTORY_BUILD_TAG), set_by="system", conn=conn)
    for value in carried:
        store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
        conn=conn,
    )
    return int(ref.id)


def split_source(
    store: Store,
    root_id: int,
    source_id: int,
    brief_node_id: int,
    split: Split,
    *,
    hold: Callable[[Connection], str | None] | None = None,
    conn: Connection | None = None,
) -> list[int]:
    """Act on a split: in ONE transaction one child todo of the source per
    part (the three labelled lines, ``factory:build``, the source's priority
    and its ``project:`` / ``repo:`` tags), each part after the first
    ``blocked-by`` the one before it; the source loses ``factory:build``,
    records :data:`SPLIT_KEY` and gets one ``split:`` comment naming the
    parts; the chain's root closes ``won't-do`` (the plan tick closes the
    rest). No ask, no build. Returns the part ids in order; an EMPTY list
    means the split was not applied (see below).

    Idempotent: a source that already records a split for ``root_id`` gets
    nothing written and its recorded parts come back. Raises ``BadInput``
    for a count outside 2..:data:`MAX_SPLIT_PARTS` and for a source that is
    itself a part: the caller checks both first, this is the backstop.

    The source may have changed while the brief session ran, so under the
    row lock a split is NOT applied when the source is gone (retired), when
    it records an earlier split by another chain with a part still open, or
    when ``hold(conn)`` (the caller's read of the source's holds: a callable
    because ``plans`` never imports ``workers``) names a reason. A held split
    writes no part, leaves the source's tags and :data:`SPLIT_KEY` alone,
    leaves one ``split not applied:`` comment naming the reason (none when
    the source is gone) and closes the root ``won't-do``."""
    if conn is None:
        with store.tx() as own:
            return split_source(
                store, root_id, source_id, brief_node_id, split, hold=hold, conn=own
            )
    n = len(split.parts)
    if not 2 <= n <= MAX_SPLIT_PARTS:
        raise BadInput(
            f"split of td{source_id}: {n} parts",
            next=f"a split is 2 to {MAX_SPLIT_PARTS} parts; refuse with `split`",
        )
    # Serialises two splits of one source, as in :func:`refuse_source`.
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (source_id,))
    source = store.get_ref(kind="todo", id=source_id, conn=conn)
    if source is None:
        _close_root(store, root_id, conn)
        return []
    meta = dict(source.meta or {})
    if isinstance(meta.get(SPLIT_PART_KEY), dict):
        raise BadInput(
            f"split of td{source_id}: it is already a part of a split",
            next="a part is never split again; refuse with `split`",
        )
    recorded = _recorded_parts(meta, root_id)
    if recorded is not None:
        return recorded
    reason = _earlier_split_hold(meta, conn)
    if reason is None and hold is not None:
        reason = hold(conn)
    if reason is not None:
        _comment_held(store, source_id, brief_node_id, root_id, split, reason, conn)
        _close_root(store, root_id, conn)
        return []
    carried = [
        str(t.value)
        for t in store.tags_for(source_id, conn=conn)
        if t.namespace == "open" and str(t.value).startswith(("project:", "repo:"))
    ]
    part_ids: list[int] = []
    for k, part in enumerate(split.parts, start=1):
        part_id = _insert_part(
            store, source, root_id, k, n, part, carried=carried, conn=conn
        )
        if part_ids:
            store.add_link(
                src_ref_id=part_id,
                dst_ref_id=part_ids[-1],
                relation="blocked-by",
                conn=conn,
            )
        part_ids.append(part_id)
    # The mint contract (td603429): every mint that links its todos ends in
    # ``finish_mint``. The parts are store-direct, so it is a no-op here.
    for part_id in part_ids:
        finish_mint(store, part_id, conn=conn)
    store.remove_tag(source_id, Tag.open(FACTORY_BUILD_TAG), conn=conn)
    store.stamp_ref_meta(
        source_id, {SPLIT_KEY: {"root": root_id, "parts": part_ids}}, conn=conn
    )
    ord_ = max(
        (int(c.ord) for c in store.chunks.list_chunks_for_ref(source_id, conn=conn)),
        default=0,
    )
    store.chunks.insert_chunks(
        source_id,
        [
            ChunkInsert(
                ord=ord_ + 1,
                text=f"split: {_handles(part_ids)} — the factory's brief step "
                f"(td{brief_node_id}, chain td{root_id}) split this todo into "
                f"{n} parts, built in this order. This todo closes when every "
                "part is done.",
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )
    _close_root(store, root_id, conn)
    return part_ids


def _close_root(store: Store, root_id: int, conn: Connection) -> None:
    """Close the chain's root ``won't-do``; the plan tick closes the rest."""
    store.add_tag(
        root_id,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )


def _earlier_split_hold(meta: dict[str, Any], conn: Connection) -> str | None:
    """The reason a split is held when the source records a split by ANOTHER
    chain with at least one part still live and not terminal (a reopened,
    re-tagged source gets no second set of parts beside open old ones)."""
    done = meta.get(SPLIT_KEY)
    if not isinstance(done, dict):
        return None
    old = [int(p) for p in done.get("parts") or []]
    row = conn.execute(
        """
        SELECT 1 FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL AND r.ref_id = ANY(%s)
           AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                              AND t.value = ANY(%s))
         LIMIT 1
        """,
        (old, sorted(TERMINAL_TODO_STATUSES)),
    ).fetchone()
    if row is None:
        return None
    return f"it was already split by chain td{done.get('root')} into {_handles(old)}"


def _comment_held(
    store: Store,
    source_id: int,
    brief_node_id: int,
    root_id: int,
    split: Split,
    reason: str,
    conn: Connection,
) -> None:
    """The one ``split not applied:`` comment on the source."""
    titles = "; ".join(
        _clean(p.title, _PART_TITLE_CAP, one_line=True) for p in split.parts
    )
    ord_ = max(
        (int(c.ord) for c in store.chunks.list_chunks_for_ref(source_id, conn=conn)),
        default=0,
    )
    store.chunks.insert_chunks(
        source_id,
        [
            ChunkInsert(
                ord=ord_ + 1,
                text=f"split not applied: the factory's brief step "
                f"(td{brief_node_id}, chain td{root_id}) proposed "
                f"{len(split.parts)} parts ({titles}), but {reason}.",
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )


def _handles(ids: list[int]) -> str:
    return ", ".join(f"td{i}" for i in ids)


#: ``landed: <sha>`` — the first line of the comment the land writes on a
#: todo (``land_slice._record_land`` and the source todo's own note).
_LANDED_RE = re.compile(r"landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)
#: Sources one :func:`sweep_landed_sources` call closes at most.
SWEEP_LIMIT = 25
#: The pieces of the ``done:`` comment :func:`close_source` writes and the
#: sweep's SQL excludes on: ``done: <sha9> is in prod (chain td<root>)``.
_DONE_HEAD = "done: "
_DONE_MID = " is in prod (chain td"
_DONE_TAIL = ")"


def done_note(sha: str, root_id: int) -> str:
    """The ``done:`` comment a chain's close leaves on its source."""
    return f"{_DONE_HEAD}{sha.strip().lower()[:9]}{_DONE_MID}{root_id}{_DONE_TAIL}"


def source_of_root(
    store: Store, root_id: int, *, conn: Connection | None = None
) -> int | None:
    """The source todo's ref id of the chain rooted at ``root_id`` (its
    ``meta.source_todo``), or ``None`` for any other plan root."""
    # ``conn`` rides only when given: ``land_slice`` reads through the
    # plain store calls its neighbours use.
    root = (
        store.get_ref(kind="todo", id=root_id)
        if conn is None
        else store.get_ref(kind="todo", id=root_id, conn=conn)
    )
    value = (getattr(root, "meta", None) or {}).get(SOURCE_TODO_KEY)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def has_landed_comment(
    store: Store, todo_id: int, sha: str, *, conn: Connection | None = None
) -> bool:
    """Whether ``todo_id`` carries a ``landed: <sha>`` comment naming
    ``sha`` (either side may be the abbreviation). A ``landed: nothing``
    note names no sha and matches none."""
    want = sha.strip().lower()
    if len(want) < 7:
        return False
    chunks = (
        store.chunks.list_chunks_for_ref(todo_id)
        if conn is None
        else store.chunks.list_chunks_for_ref(todo_id, conn=conn)
    )
    for c in chunks:
        if c.chunk_kind != _COMMENT_KIND:
            continue
        m = _LANDED_RE.match(str(c.text or "").lstrip())
        if m is None:
            continue
        named = m.group(1).lower()
        if want.startswith(named) or named.startswith(want):
            return True
    return False


def close_source(
    store: Store, root_id: int, sha: str, *, conn: Connection | None = None
) -> bool:
    """Close the source todo of the chain at ``root_id`` as done because
    ``sha`` is in the deployed build, and return whether it did.

    One ``done: <sha9> is in prod`` comment naming the chain root, every
    ``claimed-by:`` lease dropped and ``STATUS:done`` with its
    ``status:done`` event — what the land notification
    (``land_slice._close_todo``) and the todo handler's tag path write for
    a close. Does nothing and returns ``False`` for a root without
    ``meta.source_todo``, a source that is retired or already terminal,
    a source with no ``landed:`` comment for ``sha`` — a chain that
    never landed never marks its source done — and a source that already
    carries this root's :func:`done_note` (a person reopened it: a chain
    closes its source at most once). A second call finds the source
    terminal and writes nothing."""
    if conn is None:
        with store.tx() as own:
            return close_source(store, root_id, sha, conn=own)
    source_id = source_of_root(store, root_id, conn=conn)
    if source_id is None:
        return False
    # The status check and the close are one critical section.
    locked = conn.execute(
        "SELECT 1 FROM refs WHERE ref_id = %s AND kind = 'todo' "
        "AND retired_at IS NULL FOR UPDATE",
        (source_id,),
    ).fetchone()
    if locked is None:
        return False
    tags = store.tags_for(source_id, conn=conn)
    if any(t.prefix == "STATUS" and t.value in TERMINAL_TODO_STATUSES for t in tags):
        return False
    if not has_landed_comment(store, source_id, sha, conn=conn):
        return False
    existing = list(store.chunks.list_chunks_for_ref(source_id, conn=conn))
    note = done_note(sha, root_id)
    if any(
        c.chunk_kind == _COMMENT_KIND and str(c.text or "") == note for c in existing
    ):
        # This chain already closed the source once; a person reopened it.
        return False
    ord_ = max((int(c.ord) for c in existing), default=0)
    store.chunks.insert_chunks(
        source_id,
        [
            ChunkInsert(
                ord=ord_ + 1,
                text=note,
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )
    set_source_done(store, source_id, tags, conn=conn)
    return True


def set_source_done(
    store: Store, source_id: int, tags: list[Tag], *, conn: Connection
) -> None:
    """The close itself, shared by :func:`close_source` and the
    dispatcher's "already built" answer: every ``claimed-by:`` lease on the
    source dropped, ``STATUS:done`` and its ``status:done`` event. ``tags``
    is the source's current tag list; the caller holds the row lock and
    writes the comment."""
    for t in tags:
        if t.namespace == "open" and str(t.value).startswith("claimed-by:"):
            store.remove_tag(source_id, Tag.open(str(t.value)), conn=conn)
    store.add_tag(
        source_id,
        Tag.closed("STATUS", "done"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.append_event(source_id, source="todo_chain", event="status:done", conn=conn)


def sweep_landed_sources(store: Store, *, limit: int = SWEEP_LIMIT) -> list[int]:
    """Close the sources the gate hook missed; returns their ids.

    A chain root with ``meta.source_todo`` whose ``sha-in-prod`` gate is
    done (the tick turned it done, but its :func:`close_source` call failed
    or the pass died after it), whose source is still non-terminal,
    carries a ``landed:`` comment naming the gate's sha and does not carry
    this chain's ``done:`` comment (a chain closes its source once: a
    source a person reopened is not closed again by the old chain). At most ``limit``
    sources per call, lowest root id first; a close that fails is logged
    and the sweep goes on."""
    from precis.plans.templates import PENDING_SHA

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT root.ref_id, g.meta->'gate'->'args'->>'sha'
              FROM refs root
              JOIN refs g ON g.kind = 'todo' AND g.retired_at IS NULL
               AND g.meta->'gate'->>'kind' = 'sha-in-prod'
               AND g.meta->'gate'->'args'->>'sha' <> %s
              JOIN refs s ON s.kind = 'todo' AND s.retired_at IS NULL
               AND s.ref_id::text = root.meta->>%s
             WHERE root.kind = 'todo' AND root.retired_at IS NULL
               AND root.meta ? %s
               AND EXISTS (SELECT 1 FROM ref_tags gt JOIN tags t USING (tag_id)
                            WHERE gt.ref_id = g.ref_id AND t.namespace = 'OPEN'
                              AND t.value = 'plan:' || root.ref_id)
               AND EXISTS (SELECT 1 FROM ref_tags gt JOIN tags t USING (tag_id)
                            WHERE gt.ref_id = g.ref_id AND t.namespace = 'STATUS'
                              AND t.value = 'done')
               AND NOT EXISTS (SELECT 1 FROM ref_tags st JOIN tags t USING (tag_id)
                                WHERE st.ref_id = s.ref_id AND t.namespace = 'STATUS'
                                  AND t.value = ANY(%s))
               AND EXISTS (SELECT 1 FROM chunks c
                            WHERE c.ref_id = s.ref_id
                              AND c.chunk_kind = 'todo_comment'
                              AND c.retired_at IS NULL
                              AND lower(c.text) LIKE 'landed: '
                                  || lower(left(g.meta->'gate'->'args'->>'sha', 9))
                                  || '%%')
               AND NOT EXISTS (SELECT 1 FROM chunks d
                                WHERE d.ref_id = s.ref_id
                                  AND d.chunk_kind = 'todo_comment'
                                  AND d.retired_at IS NULL
                                  AND d.text = %s
                                      || lower(left(g.meta->'gate'->'args'->>'sha', 9))
                                      || %s || root.ref_id || %s)
             ORDER BY root.ref_id
             LIMIT %s
            """,
            (
                PENDING_SHA,
                SOURCE_TODO_KEY,
                SOURCE_TODO_KEY,
                sorted(TERMINAL_TODO_STATUSES),
                _DONE_HEAD,
                _DONE_MID,
                _DONE_TAIL,
                max(0, limit),
            ),
        ).fetchall()
    closed: list[int] = []
    for root_id, sha in rows:
        try:
            if close_source(store, int(root_id), str(sha)):
                source = source_of_root(store, int(root_id))
                if source is not None:
                    closed.append(source)
        except Exception:
            log.warning(
                "todo_chain: sweep close of chain td%s failed", root_id, exc_info=True
            )
    return closed


#: The pieces of the ``done:`` comment :func:`_close_split_source` writes
#: and the sweep's SQL excludes on: ``done: every part is done (td<id>, …)``.
_SPLIT_DONE_MID = "every part is done ("
_SPLIT_DONE_TAIL = ")"
#: ``STATUS:done`` parts of ``%(parts)s`` (a jsonb array of ids) — the
#: parts that are live and done; a split source closes when it is all of them.
_DONE_PARTS_SQL = """
    SELECT count(*) FROM refs c
     WHERE c.kind = 'todo' AND c.retired_at IS NULL
       AND c.ref_id::text IN (SELECT jsonb_array_elements_text(%(parts)s))
       AND EXISTS (SELECT 1 FROM ref_tags ct JOIN tags t USING (tag_id)
                    WHERE ct.ref_id = c.ref_id AND t.namespace = 'STATUS'
                      AND t.value = 'done')
"""


def _all_parts_done(conn: Connection, parts: list[int]) -> bool:
    row = conn.execute(_DONE_PARTS_SQL, {"parts": Jsonb(parts)}).fetchone()
    return bool(parts) and row is not None and int(row[0]) == len(set(parts))


_CHILDREN_SQL = """
    SELECT c.ref_id, jsonb_typeof(c.meta->'ask') = 'object',
           (SELECT t.value FROM ref_tags ct JOIN tags t USING (tag_id)
             WHERE ct.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1)
      FROM refs c
     WHERE c.kind = 'todo' AND c.retired_at IS NULL AND c.parent_id = %s
     ORDER BY c.ref_id
"""


def _done_children(conn: Connection, source_id: int) -> list[int]:
    """The done non-ask child todos of a ``factory:split`` parent when it may
    close, else ``[]``: at least one such child, every live non-ask child
    ``STATUS:done`` and every live ask child resolved as ``done`` or
    ``won't-do``. A timed-out ask still leaves the parent open."""
    done: list[int] = []
    for child_id, is_ask, status in conn.execute(_CHILDREN_SQL, (source_id,)):
        if is_ask:
            if status not in ("done", "won't-do"):
                return []
        elif status == "done":
            done.append(int(child_id))
        else:
            return []
    return done


def _closable_parts(conn: Connection, source: Any, tags: list[Tag]) -> list[int]:
    """The parts a split source closes on, or ``[]`` when it may not close yet.
    A source recording a split (:data:`SPLIT_KEY`) answers to its recorded
    parts alone; any other carrying :data:`SPLIT_PARENT_TAG` to its live
    child todos (:func:`_done_children`)."""
    meta = source.meta or {}
    if SPLIT_KEY in meta:
        done = meta.get(SPLIT_KEY)
        parts = [int(p) for p in (done or {}).get("parts") or []]
        return parts if _all_parts_done(conn, parts) else []
    if any(t.namespace == "open" and t.value == SPLIT_PARENT_TAG for t in tags):
        return _done_children(conn, int(source.id))
    return []


def _split_done_note(parts: list[int]) -> str:
    """The ``done:`` comment a split close writes for ``parts`` — the exact
    text the once-only check looks for, in :func:`_close_split_source` and
    the sweep's candidate pass."""
    return f"{_DONE_HEAD}{_SPLIT_DONE_MID}{_handles(parts)}{_SPLIT_DONE_TAIL}"


def _close_split_source(store: Store, source_id: int) -> bool:
    """Close one split source whose parts are all done; ``False`` when it is
    not (any more) closable or already carries this close's comment (a
    person reopened it: a split closes its source at most once)."""
    with store.tx() as conn:
        locked = conn.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND kind = 'todo' "
            "AND retired_at IS NULL FOR UPDATE",
            (source_id,),
        ).fetchone()
        source = store.get_ref(kind="todo", id=source_id, conn=conn)
        if locked is None or source is None:
            return False
        tags = store.tags_for(source_id, conn=conn)
        if any(
            t.prefix == "STATUS" and t.value in TERMINAL_TODO_STATUSES for t in tags
        ):
            return False
        parts = _closable_parts(conn, source, tags)
        if not parts:
            return False
        note = _split_done_note(parts)
        existing = list(store.chunks.list_chunks_for_ref(source_id, conn=conn))
        if any(
            c.chunk_kind == _COMMENT_KIND and str(c.text or "") == note
            for c in existing
        ):
            return False
        ord_ = max((int(c.ord) for c in existing), default=0)
        store.chunks.insert_chunks(
            source_id,
            [
                ChunkInsert(
                    ord=ord_ + 1,
                    text=note,
                    meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
                )
            ],
            conn=conn,
        )
        set_source_done(store, source_id, tags, conn=conn)
    return True


def sweep_splits(store: Store, *, limit: int = SWEEP_LIMIT) -> list[int]:
    """Close the split sources whose parts are all done; returns their ids.

    A source carrying :data:`SPLIT_KEY`, live and not terminal, whose
    recorded parts are every one live and ``STATUS:done`` gets one ``done:
    every part is done`` comment and :func:`set_source_done` — once: a
    source that already carries that comment (a person reopened it) is
    passed over before the limit counts, so it never fills the window and
    starves a later completed split. A part that is ``won't-do``, retired
    or still open leaves the source alone. So does a live, non-terminal
    parent carrying :data:`SPLIT_PARENT_TAG` (a split by hand) with at
    least one non-ask child todo, every live non-ask child done, and
    every live ask resolved as ``done`` or ``won't-do``. A source with
    both is judged by its recorded parts.
    At most ``limit`` sources per call, lowest id first; a close that
    fails is logged and the sweep goes on."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT s.ref_id FROM refs s
             WHERE s.kind = 'todo' AND s.retired_at IS NULL
               AND (s.meta ? %(key)s
                    OR EXISTS (SELECT 1 FROM ref_tags pt JOIN tags t USING (tag_id)
                                WHERE pt.ref_id = s.ref_id
                                  AND t.namespace = 'OPEN'
                                  AND t.value = %(parent)s))
               AND NOT EXISTS (SELECT 1 FROM ref_tags st JOIN tags t USING (tag_id)
                                WHERE st.ref_id = s.ref_id
                                  AND t.namespace = 'STATUS'
                                  AND t.value = ANY(%(terminal)s))
             ORDER BY s.ref_id
            """,
            {
                "key": SPLIT_KEY,
                "parent": SPLIT_PARENT_TAG,
                "terminal": sorted(TERMINAL_TODO_STATUSES),
            },
        ).fetchall()
        candidates: list[int] = []
        for (source_id,) in rows:
            if len(candidates) >= max(0, limit):
                break
            ref = store.get_ref(kind="todo", id=int(source_id), conn=conn)
            if ref is None:
                continue
            parts = _closable_parts(
                conn, ref, store.tags_for(int(source_id), conn=conn)
            )
            if not parts:
                continue
            # A source carrying this close's comment was closed once and
            # reopened: it is never a candidate again, and never fills the
            # window (:func:`_close_split_source` would refuse it anyway).
            if conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s AND chunk_kind = %s "
                "AND retired_at IS NULL AND text = %s LIMIT 1",
                (int(source_id), _COMMENT_KIND, _split_done_note(parts)),
            ).fetchone():
                continue
            candidates.append(int(source_id))
    closed: list[int] = []
    for source_id in candidates:
        try:
            if _close_split_source(store, source_id):
                closed.append(source_id)
        except Exception:
            log.warning(
                "todo_chain: sweep close of split td%s failed", source_id, exc_info=True
            )
    return closed


__all__ = [
    "BRIEF_GRAMMAR",
    "CLIP_MARKER",
    "CLOSED_BUILT_TAG",
    "FACTORY_BUILD_TAG",
    "HANDOFF_JOB_KEY",
    "MAX_SPLIT_PARTS",
    "NEEDS_SPEC_TAG",
    "REFUSAL_KEY",
    "REFUSAL_REASONS",
    "SKIP_TAG",
    "SOURCE_TODO_KEY",
    "SPLIT_KEY",
    "SPLIT_PARENT_TAG",
    "SPLIT_PART_KEY",
    "SWEEP_LIMIT",
    "Brief",
    "BriefOutcome",
    "Refusal",
    "Split",
    "SplitPart",
    "brief_comment_text",
    "close_built",
    "close_source",
    "done_note",
    "has_landed_comment",
    "norm_stamp",
    "parse_brief",
    "parse_read_line",
    "post_brief",
    "read_line",
    "refuse_source",
    "set_source_done",
    "source_of_root",
    "source_updated_at",
    "split_source",
    "sweep_landed_sources",
    "sweep_splits",
]
