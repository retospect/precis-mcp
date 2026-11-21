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

from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.asks_view import TRIAGE_PREFIX
from precis.todo.guards import check_ask_meta

if TYPE_CHECKING:
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
    quoted = " ".join(why.replace("\x00", "").split())[:_WHY_CAP]
    plan = f" (plan td{root_id})" if root_id is not None else ""
    body = (
        f"The factory's brief step refused {handle}{plan}, "
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


__all__ = [
    "BRIEF_GRAMMAR",
    "CLIP_MARKER",
    "FACTORY_BUILD_TAG",
    "NEEDS_SPEC_TAG",
    "REFUSAL_KEY",
    "REFUSAL_REASONS",
    "SKIP_TAG",
    "SOURCE_TODO_KEY",
    "SWEEP_LIMIT",
    "Brief",
    "BriefOutcome",
    "Refusal",
    "close_source",
    "done_note",
    "has_landed_comment",
    "parse_brief",
    "post_brief",
    "refuse_source",
    "set_source_done",
    "source_of_root",
    "sweep_landed_sources",
]
