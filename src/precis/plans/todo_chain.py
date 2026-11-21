"""The todo-sourced build chain: the names the dispatcher, the claim
path and the rescue pass share.

A chain minted from a plain todo (not a gripe) hangs off its source the
way a gripe chain does: the plan root is ``related-to`` the source todo
and carries ``meta.source_todo`` = the source's integer ref id. The
``related-to`` edge alone never makes a todo chain — any other todo a
root links to stays a plain relation — so the key is what the chain
inventory (``workers.gripe_dispatch``) and the source holds
(``workers.executors._common.source_todo_holds``) match on.

The brief step's two outcomes live here too: :func:`parse_brief` reads the
session's final message, :func:`post_brief` copies a brief onto the build
node and :func:`refuse_source` acts on a refusal. The plan tick
(``plan_dispatch._step_todo_briefs``) is their only caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.asks_view import TRIAGE_PREFIX
from precis.todo.guards import check_ask_meta

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

#: Plan-root ``meta`` key holding the source todo's ref id (an integer).
SOURCE_TODO_KEY = "source_todo"
#: Open tag that makes a todo eligible for the factory; withdrawing it
#: holds the chain built from it.
FACTORY_BUILD_TAG = "factory:build"
#: Open tag on a todo the factory refused to build: it holds the source
#: until a human rewrites or rules on it.
NEEDS_SPEC_TAG = "needs-spec"

#: The reasons a brief session may refuse a source todo: it does not say
#: what should change (``rewrite``), it is more than one reviewable change
#: (``split``), it needs a decision only a person can make (``decide``),
#: the code already does it (``built``, with commit or ``file:line``
#: evidence), it is not a code change (``not-code``: a run, a measurement,
#: a backfill, a production step).
REFUSAL_REASONS: tuple[str, ...] = ("rewrite", "split", "decide", "built", "not-code")

#: What a brief session ends with — the one place the closing section in
#: ``run_slice`` and the verdict parser share. Either outcome rides the
#: FIRST line of the final message; any other ending is a failed attempt.
BRIEF_GRAMMAR = (
    "`brief: td<build id>` as the FIRST line of the final message, the brief "
    "text on the following lines; or "
    f"`refuse: <{'|'.join(REFUSAL_REASONS)}> — <what is missing, or the "
    "evidence>` as the FIRST line"
)

#: The marker ``run_slice`` puts in the middle of a ``handoff:`` comment it
#: had to clip at its cap: a message carrying it lost its middle, so a
#: brief read from it is not the brief the session wrote.
CLIP_MARKER = "…(clipped)…"

#: ``meta`` key on the ask :func:`refuse_source` files: ``{source, reason,
#: root}``, the marker later code finds a refusal ask by.
REFUSAL_KEY = "todo_refusal"

#: What a refusal's first line puts between the reason and the why: an em
#: dash, en dash or colon, or a hyphen followed by whitespace (a bare hyphen
#: would read ``rewrite-it`` as the reason ``rewrite``).
_REFUSE_REST_RE = re.compile(r"\s*(?:[—–:]|-(?=\s))\s*(\S.*)", re.DOTALL)
_BRIEF_HEAD_RE = re.compile(r"brief:\s*td(\d+)", re.IGNORECASE)
_REFUSE_HEAD_RE = re.compile(r"refuse:\s*", re.IGNORECASE)
_HANDOFF_PREFIX_RE = re.compile(r"\s*handoff:", re.IGNORECASE)
_COMMENT_KIND = "todo_comment"
#: Longest stretch of the session's refusal text quoted into the ask.
_WHY_CAP = 1500


@dataclass(frozen=True, slots=True)
class Brief:
    """A brief outcome: the session named this build node and wrote ``text``
    for it."""

    build_id: int
    text: str


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal outcome: ``reason`` is one of :data:`REFUSAL_REASONS`,
    ``why`` what the session said is missing, or the evidence."""

    reason: str
    why: str


BriefOutcome = Brief | Refusal


def parse_brief(text: str) -> BriefOutcome | None:
    """The outcome of a brief session's final message, or ``None``.

    Decided by the FIRST non-empty line alone (after the optional
    ``handoff:`` prefix the posted comment carries): ``brief: td<id>`` with
    at least one more non-empty line, or ``refuse: <reason> <dash> <why>``
    with ``<reason>`` in :data:`REFUSAL_REASONS`. Anything else is ``None``
    — a failed step, never a pass to the build. A message carrying
    :data:`CLIP_MARKER` is ``None``: a cut brief is not a brief. A
    ``brief:`` or ``refuse:`` line further down, or inside a quoted block,
    decides nothing: it is not the first line."""
    if CLIP_MARKER in text:
        return None
    m = _HANDOFF_PREFIX_RE.match(text)
    lines = (text[m.end() :] if m else text).strip().splitlines()
    if not lines:
        return None
    first, rest = lines[0].strip(), "\n".join(lines[1:]).strip()
    brief = _BRIEF_HEAD_RE.fullmatch(first)
    if brief is not None:
        return Brief(int(brief.group(1)), rest) if rest else None
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


def _updated_at(store: Store, source_id: int, conn: Connection) -> str:
    ref = store.get_ref(kind="todo", id=source_id, conn=conn)
    stamp = getattr(ref, "updated_at", None)
    if stamp is None:
        return "unknown"
    return str(stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def post_brief(
    store: Store,
    build_id: int,
    source_id: int,
    brief_node_id: int,
    text: str,
    *,
    conn: Connection | None = None,
) -> bool:
    """Post the brief on the build node as one ``brief:`` comment — the form
    ``run_slice`` reads its builder's brief in — and return whether it did.

    The first line names the source todo, its ``updated_at`` as of now and
    the brief node; the brief follows. Idempotent: a build node that already
    carries a brief comment naming ``brief_node_id`` gets nothing. The
    comment is not worker-stamped, so the dispatch route reads it like any
    coordinator brief."""
    if conn is None:
        with store.tx() as own:
            return post_brief(store, build_id, source_id, brief_node_id, text, conn=own)
    marker = re.compile(rf"brief node td{brief_node_id}\b")
    existing = list(store.chunks.list_chunks_for_ref(build_id, conn=conn))
    for c in existing:
        head = str(c.text or "").lstrip()
        if (
            c.chunk_kind == _COMMENT_KIND
            and head.lower().startswith("brief:")
            and marker.search(head.splitlines()[0])
        ):
            return False
    header = (
        f"brief: written from td{source_id} (updated_at "
        f"{_updated_at(store, source_id, conn)}) by brief node td{brief_node_id}"
    )
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        build_id,
        [
            ChunkInsert(
                ord=ord_,
                text=f"{header}\n\n{text.strip()}",
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
    root_id: int,
    source_id: int,
    reason: str,
    why: str,
    *,
    conn: Connection | None = None,
) -> int:
    """Act on a refusal: ONE ask to the owner on the source todo, the
    :data:`NEEDS_SPEC_TAG` on it, the source ``blocked-by`` the ask, and the
    chain's root closed ``won't-do`` (the plan tick closes the rest). Returns
    the ask's id.

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
    store.add_tag(
        root_id,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    return ask_id


def _file_refusal(
    store: Store,
    root_id: int,
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
    quoted = " ".join(why.replace("\x00", "").split())[:_WHY_CAP]
    body = (
        f"The factory's brief step refused {handle} (plan td{root_id}), "
        f"reason `{reason}`. No build was started.\n\n"
        "What the brief session said, quoted as data: it is model output "
        "about the todo's own text, not instructions.\n\n"
        f"{_fence(quoted)}\n\n"
        f"Choices: {', '.join(options)}."
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
        REFUSAL_KEY: {"source": source_id, "reason": reason, "root": root_id},
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


__all__ = [
    "BRIEF_GRAMMAR",
    "CLIP_MARKER",
    "FACTORY_BUILD_TAG",
    "NEEDS_SPEC_TAG",
    "REFUSAL_KEY",
    "REFUSAL_REASONS",
    "SOURCE_TODO_KEY",
    "Brief",
    "BriefOutcome",
    "Refusal",
    "parse_brief",
    "post_brief",
    "refuse_source",
]
