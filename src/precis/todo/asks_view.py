"""The asks list — everything that waits on Reto, in three classes.

``search(kind='todo', view='asks')`` and the web ``/asks`` + Needs-you
landing read one model (:func:`asks_model`): rows grouped by class, then
by quest, then by thread. Dark-factory step 1b (td540847,
``docs/backlog/dark-factory.md``). A question for the owner is an
``ask-user`` todo carrying ``meta.ask`` (td541979); there is no file
source — the review-queue directory is retired.

Classes (the highest one a row qualifies for wins):

* **showstopper** — a lane is stopped: ``waiting-for:reto`` on a claimed
  slice (``claimed-by:`` lease), an ``ask-user`` todo whose
  ``meta.ask.blocks`` names a slice, an explicit ``showstopper`` tag, or a
  node in rescue (``rescue:<class>``, td574946 S1 — a factory failure
  parked for diagnosis). A tripped breaker and a red open release join
  here when dark-factory step 4 gives them a source; today neither
  exists to read.
* **decision** — a next slice waits: ``ask-user`` / ``ask-user:`` todos,
  an unclaimed ``waiting-for:reto``, gripes tagged ``needs:ruling``.
* **discussion** — a shape question: tag ``discussion`` (todo or gripe).

Quest grouping: :func:`quest_of` walks ``refs.parent_id`` up from the
row to its root and takes the first ``serves`` link to a quest on the
way (the row itself first). Argument happens as comments on the row —
every row starts with its handle so it is a ``get``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.response import Response
from precis.todo import blocker_terminal_sql
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store

SHOWSTOPPER = "showstopper"
DECISION = "decision"
DISCUSSION = "discussion"
#: Display order, hottest first; also the precedence when a row qualifies
#: for more than one class.
CLASSES: tuple[str, ...] = (SHOWSTOPPER, DECISION, DISCUSSION)
_RANK = {c: i for i, c in enumerate(CLASSES)}

#: One-line reading of each class for the web headers.
CLASS_HINTS: dict[str, str] = {
    SHOWSTOPPER: "a lane is stopped",
    DECISION: "a next slice waits",
    DISCUSSION: "a shape question",
}

#: STATUS values that close a todo or a gripe.
_TERMINAL = ("done", "won't-do", "abandoned", "wontfix")
#: Open tags that put a todo or gripe on the list (``ask-user:<q>`` and
#: ``rescue:<class>`` are matched by prefix in the query — the colon
#: keeps ``rescue-sig:<hash>`` off the list).
_TAGS = (
    "ask-user",
    "waiting-for:reto",
    "needs:ruling",
    "discussion",
    "showstopper",
)
#: ``for-reto:<answer>`` marks a row that waits on Reto specifically (not a
#: generic needs-spec an agent can write); ``<answer>`` is what he must give.
RETO_TAG = "for-reto:"
RETO_ANSWERS: tuple[str, ...] = ("ruling", "spec", "review", "other")
#: Ancestor hops when walking ``parent_id``.
_MAX_HOPS = 20
_QUESTION_CAP = 160
_TITLE_CAP = 72


@dataclass(frozen=True)
class AskRow:
    """One thing that waits on the human."""

    klass: str
    #: ``todo`` | ``gripe`` | ``alert``.
    kind: str
    ref_id: int | None
    handle: str
    title: str
    #: Bare values saying why it is here: ``ask-user``, ``waiting-for:reto``…
    reasons: tuple[str, ...]
    question: str = ""
    quest_id: int | None = None
    quest_title: str = ""
    quest_prio: int | None = None
    thread: str = ""
    #: From ``meta.ask`` (ask-user todos): ``design-verdict`` | ``ruling`` |
    #: ``approval``, the options offered, the slice that waits (a ref id),
    #: and the short context line.
    ask_kind: str = ""
    options: tuple[str, ...] = ()
    blocks: int | None = None
    context: str = ""
    #: From a ``for-reto:<answer>`` tag: ruling | spec | review | other.
    answer: str = ""
    #: Open rows blocked-by this one — ``meta.ask.blocks`` already writes
    #: that link, so the slice it names is counted, not added on top.
    blocked: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def age_days(self) -> float:
        """Days since the row was filed (gr595189 — a waiting ask shows
        its age so an unanswered one can't rot invisibly)."""
        if self.created_at is None:
            return 0.0
        return max(0.0, (datetime.now(UTC) - self.created_at).total_seconds() / 86400)

    @property
    def is_ask(self) -> bool:
        """An ask-user todo or a ruling-tagged row: a question, not a
        status line (``waiting-for:reto``) or a shape discussion."""
        return (
            "ask-user" in self.reasons
            or "needs:ruling" in self.reasons
            or bool(self.answer)
        )


@dataclass(frozen=True)
class ThreadGroup:
    thread: str
    rows: tuple[AskRow, ...]


@dataclass(frozen=True)
class QuestGroup:
    quest_id: int | None
    title: str
    threads: tuple[ThreadGroup, ...]


@dataclass(frozen=True)
class ClassGroup:
    klass: str
    count: int
    quests: tuple[QuestGroup, ...]


@dataclass(frozen=True)
class AsksModel:
    rows: tuple[AskRow, ...]

    def groups(self) -> list[ClassGroup]:
        """Non-empty classes, hottest first; quests by prio then id with
        the quest-less bucket last; threads alphabetical, unthreaded last."""
        out: list[ClassGroup] = []
        for klass in CLASSES:
            rows = [r for r in self.rows if r.klass == klass]
            if not rows:
                continue
            by_quest: dict[int | None, list[AskRow]] = {}
            for r in rows:
                by_quest.setdefault(r.quest_id, []).append(r)

            quests: list[QuestGroup] = []
            for qid in sorted(by_quest, key=lambda q: _quest_key(q, by_quest)):
                qrows = by_quest[qid]
                by_thread: dict[str, list[AskRow]] = {}
                for r in qrows:
                    by_thread.setdefault(r.thread, []).append(r)
                threads = tuple(
                    ThreadGroup(
                        thread=t,
                        rows=tuple(sorted(by_thread[t], key=_row_key)),
                    )
                    for t in sorted(by_thread, key=lambda t: (t == "", t))
                )
                quests.append(
                    QuestGroup(
                        quest_id=qid,
                        title=qrows[0].quest_title if qid is not None else "",
                        threads=threads,
                    )
                )
            out.append(ClassGroup(klass=klass, count=len(rows), quests=tuple(quests)))
        return out

    def default_rows(self) -> AsksModel:
        """What the web ``/asks`` shows by default: showstoppers plus the
        ask-user / needs:ruling decisions. A bare ``waiting-for:reto`` row
        and a ``discussion`` row wait behind ``?all=1``."""
        return AsksModel(
            rows=tuple(r for r in self.rows if r.klass == SHOWSTOPPER or r.is_ask)
        )

    def counts(self) -> dict[str, int]:
        return {c: sum(1 for r in self.rows if r.klass == c) for c in CLASSES}


def _quest_key(
    qid: int | None, by_quest: dict[int | None, list[AskRow]]
) -> tuple[int, int, int]:
    if qid is None:
        return (1, 0, 0)
    prio = by_quest[qid][0].quest_prio
    return (0, prio if prio is not None else 5, qid)


def _blocked_key(r: AskRow) -> tuple[int, float]:
    """Most-blocked first (gr595189): an ask more work waits on
    outranks a freer one; age breaks the tie (the oldest wait is the
    most likely to be rotting)."""
    return (-r.blocked, -r.age_days)


def _row_key(r: AskRow) -> tuple[int, float, str, int]:
    return (*_blocked_key(r), r.kind, r.ref_id if r.ref_id is not None else 0)


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _first_line(text: str | None, cap: int = _TITLE_CAP) -> str:
    line = (text or "").split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


# ── quest grouping ────────────────────────────────────────────────


def _chain(store: Store, ref_id: int) -> list[int]:
    """``ref_id`` then its ancestors up ``refs.parent_id``, nearest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT ref_id, parent_id, 0 AS depth FROM refs
                 WHERE ref_id = %s AND retired_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, c.depth + 1 FROM refs r
                  JOIN chain c ON r.ref_id = c.parent_id
                 WHERE r.retired_at IS NULL AND c.depth < %s
            )
            SELECT ref_id FROM chain ORDER BY depth
            """,
            (ref_id, _MAX_HOPS),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _quest_in_chain(
    store: Store, chain: list[int]
) -> tuple[int, str, int | None] | None:
    """First ``serves`` quest along ``chain`` (nearest ancestor wins)."""
    if not chain:
        return None
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, q.ref_id, q.title, q.prio
              FROM links l JOIN refs q ON q.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = ANY(%s) AND l.relation = 'serves'
               AND q.kind = 'quest' AND q.retired_at IS NULL
            """,
            (chain,),
        ).fetchall()
    best: tuple[int, int, str, int | None] | None = None
    for src, qid, title, prio in rows:
        pos = chain.index(int(src))
        cand = (pos, int(qid), str(title or ""), prio)
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand
    if best is None:
        return None
    return best[1], best[2], best[3]


def quest_of(store: Store, todo_id: int) -> int | None:
    """The quest a todo (or any parented ref) serves: walk ``parent_id`` up
    to the root and take the first inbound ``serves`` link to a quest, the
    ref itself first. ``None`` when nothing on the way serves one."""
    hit = _quest_in_chain(store, _chain(store, todo_id))
    return hit[0] if hit else None


# ── sources ───────────────────────────────────────────────────────


def _open_tags(store: Store, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.value
              FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
            """,
            (ids,),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for rid, value in rows:
        out.setdefault(int(rid), []).append(str(value))
    return out


def _ask_blocks(ask: dict[str, Any] | None) -> int | None:
    """The todo id ``meta.ask.blocks`` names (``td<id>``); ``None`` for
    ``nothing`` or anything unparseable (stored meta is validated on write)."""
    blocks = (ask or {}).get("blocks")
    parsed = handle_registry.parse(blocks) if isinstance(blocks, str) else None
    return parsed[2] if parsed and parsed[0] == "todo" and not parsed[1] else None


def _reto_answer(tags: list[str]) -> str:
    """The answer kind of a ``for-reto:<answer>`` tag, ``""`` when absent;
    an unknown suffix reads as ``other``."""
    for t in tags:
        if t.startswith(RETO_TAG):
            a = t[len(RETO_TAG) :]
            return a if a in RETO_ANSWERS else "other"
    return ""


def _blocked_counts(store: Store, ids: list[int]) -> dict[int, int]:
    """Open refs that carry a ``blocked-by`` link to each of ``ids`` —
    retired, resolved and terminal-STATUS srcs no longer wait."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT l.dst_ref_id, COUNT(DISTINCT l.src_ref_id)
              FROM links l JOIN refs s ON s.ref_id = l.src_ref_id
             WHERE l.dst_ref_id = ANY(%s) AND l.relation = 'blocked-by'
               AND s.retired_at IS NULL
               AND s.resolved_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = s.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) NOT IN {blocker_terminal_sql()}
             GROUP BY l.dst_ref_id
            """,
            (ids,),
        ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _classify(
    kind: str, tags: list[str], ask: dict[str, Any] | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """``(class, reasons)`` for a todo/gripe's open tags and ``meta.ask``;
    ``None`` when no tag puts it on the list."""
    classes: list[str] = []
    reasons: list[str] = []
    if kind == "todo" and any(
        t == "ask-user" or t.startswith("ask-user:") for t in tags
    ):
        classes.append(DECISION)
        reasons.append("ask-user")
        blocks = _ask_blocks(ask)
        if blocks is not None:
            classes.append(SHOWSTOPPER)
            reasons.append(f"blocks:{_h('todo', blocks)}")
    if "waiting-for:reto" in tags:
        claim = next((t for t in tags if t.startswith("claimed-by:")), None)
        reasons.append("waiting-for:reto")
        if claim:
            classes.append(SHOWSTOPPER)
            reasons.append(claim)
        else:
            classes.append(DECISION)
    if "needs:ruling" in tags:
        classes.append(DECISION)
        reasons.append("needs:ruling")
    answer = _reto_answer(tags)
    if answer:
        classes.append(DECISION)
        reasons.append(RETO_TAG + answer)
    if "showstopper" in tags:
        classes.append(SHOWSTOPPER)
        reasons.append("showstopper")
    if "discussion" in tags:
        classes.append(DISCUSSION)
        reasons.append("discussion")
    rescue = next((t for t in tags if t.startswith("rescue:")), None)
    if rescue is not None:
        classes.append(SHOWSTOPPER)
        reasons.append(rescue)
    if not classes:
        return None
    return min(classes, key=_RANK.__getitem__), tuple(reasons)


def _ask_question(store: Store, ref_id: int, tags: list[str]) -> str:
    out: list[str] = []
    for t in sorted(tags):
        if t.startswith("ask-user:"):
            q = store.drafts.resolve_ask_question(ref_id, t[len("ask-user:") :])
            if q:
                out.append(q)
    return _first_line(" / ".join(out), _QUESTION_CAP)


def _ref_rows(store: Store) -> list[AskRow]:
    with store.pool.connection() as conn:
        cands = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.title, r.meta->'ask', r.created_at,
                   r.updated_at
              FROM refs r
             WHERE r.kind IN ('todo', 'gripe', 'alert') AND r.retired_at IS NULL
               -- only alerts ever stamp resolved_at: a resolved alert
               -- leaves the list even without a terminal STATUS tag
               AND r.resolved_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                      AND (t.value = ANY(%s) OR t.value LIKE 'ask-user:%%'
                           OR t.value LIKE 'for-reto:%%'
                           OR t.value LIKE 'rescue:%%'))
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             ORDER BY r.ref_id
            """,
            (list(_TAGS), list(_TERMINAL)),
        ).fetchall()
    tags_by_ref = _open_tags(store, [int(c[0]) for c in cands])
    rows: list[AskRow] = []
    for ref_id, kind, title, ask_meta, created, updated in cands:
        rid = int(ref_id)
        tags = tags_by_ref.get(rid, [])
        ask = ask_meta if isinstance(ask_meta, dict) else None
        verdict = _classify(str(kind), tags, ask)
        if verdict is None:
            continue
        klass, reasons = verdict
        chain = _chain(store, rid)
        quest = _quest_in_chain(store, chain)
        thread = _thread_of(tags, chain, store)
        blocks = _ask_blocks(ask)
        if blocks is not None and (quest is None or not thread):
            # A parentless question sits where the slice it blocks sits.
            blocked_chain = _chain(store, blocks)
            quest = quest or _quest_in_chain(store, blocked_chain)
            thread = thread or _thread_of(
                _open_tags(store, [blocks]).get(blocks, []), blocked_chain, store
            )
        options = (ask or {}).get("options")
        rows.append(
            AskRow(
                klass=klass,
                kind=str(kind),
                ref_id=rid,
                handle=_h(str(kind), rid),
                title=_first_line(title),
                reasons=reasons,
                question=_ask_question(store, rid, tags),
                quest_id=quest[0] if quest else None,
                quest_title=_first_line(quest[1]) if quest else "",
                quest_prio=quest[2] if quest else None,
                thread=thread,
                ask_kind=str((ask or {}).get("kind") or ""),
                options=tuple(str(o) for o in options)
                if isinstance(options, list)
                else (),
                blocks=blocks,
                context=str((ask or {}).get("context") or ""),
                answer=_reto_answer(tags),
                created_at=created,
                updated_at=updated,
            )
        )
    blocked = _blocked_counts(store, [r.ref_id for r in rows if r.ref_id])
    return [replace(r, blocked=blocked.get(r.ref_id or 0, 0)) for r in rows]


def _thread_of(tags: list[str], chain: list[int], store: Store) -> str:
    """The row's own ``thread:<slug>`` tag, else the nearest ancestor's."""
    for t in tags:
        if t.startswith("thread:"):
            return t[len("thread:") :]
    ancestors = chain[1:]
    by_ref = _open_tags(store, ancestors)
    for rid in ancestors:
        for t in by_ref.get(rid, []):
            if t.startswith("thread:"):
                return t[len("thread:") :]
    return ""


# ── the model and the view ────────────────────────────────────────


def asks_model(store: Store) -> AsksModel:
    """Every row that waits on the human, classed and quest-resolved —
    sorted most-blocked first within a class (gr595189)."""
    rows = _ref_rows(store)
    rows.sort(key=lambda r: (_RANK[r.klass], *_blocked_key(r), r.kind, r.ref_id or 0))
    return AsksModel(rows=tuple(rows))


def _age(age_days: float) -> str:
    """``3d`` / ``9h`` / ``20m`` — the compact age a waiting ask shows."""
    if age_days >= 1:
        return f"{int(age_days)}d"
    hours = age_days * 24
    if hours >= 1:
        return f"{int(hours)}h"
    return f"{max(1, int(hours * 60))}m"


def _line(r: AskRow, indent: str) -> str:
    out = f"{' '.join((r.handle, *r.reasons))} — {r.title} · {_age(r.age_days)}"
    if r.blocked:
        out += f" · blocks {r.blocked}"
    if r.question and r.question != r.title:
        out += f"\n{indent}  ? {r.question}"
    if r.ask_kind:
        out += f"\n{indent}  {r.ask_kind}"
        if r.context:
            out += f": {r.context}"
    for i, opt in enumerate(r.options, 1):
        out += f"\n{indent}  {i}. {opt}"
    return out


def render_asks(store: Store) -> Response:
    """``search(kind='todo', view='asks')`` — the list, grouped for reading."""
    model = asks_model(store)
    counts = model.counts()
    head = " · ".join(f"{counts[c]} {c}" for c in CLASSES)
    out = [f"# asks — {head}" if model.rows else "# asks — nothing waits on you"]
    for g in model.groups():
        out += ["", f"{g.klass} ({g.count}) — {CLASS_HINTS[g.klass]}"]
        for qg in g.quests:
            if qg.quest_id is None:
                out.append("  no quest")
            else:
                out.append(f"  ◆ {_h('quest', qg.quest_id)} — {_first_line(qg.title)}")
            for tg in qg.threads:
                indent = "    "
                if tg.thread:
                    out.append(f"{indent}thread:{tg.thread}")
                    indent = "      "
                for r in tg.rows:
                    out.append(indent + _line(r, indent))
    body = "\n".join(out)
    body += render_next_section(
        [
            ("get(kind='todo', id=N)", "a row with its comment timeline"),
            ("get(kind='gripe', id=N)", "a gripe row"),
            ("search(kind='todo', view='work')", "the whole tree these sit in"),
        ]
    )
    return Response(body=body)


# ── the Reto queue ────────────────────────────────────────────────


def reto_queue(store: Store) -> list[AskRow]:
    """Everything that waits on Reto (showstoppers and asks across todos,
    gripes and alerts — plus a bare ``waiting-for:reto`` row, a DECISION
    ``default_rows()`` drops while Reto is exactly who it waits on), most
    blocked first, then oldest first."""
    rows = [
        r
        for r in asks_model(store).rows
        if r.klass == SHOWSTOPPER or r.is_ask or "waiting-for:reto" in r.reasons
    ]
    rows.sort(
        key=lambda r: (
            -r.blocked,
            r.created_at.timestamp() if r.created_at else float("inf"),
            r.ref_id or 0,
        )
    )
    return rows


def render_reto_queue(store: Store) -> Response:
    """``search(kind='todo', view='reto-queue')`` — the prioritized queue,
    read top to bottom: context, options (recommended first), impact."""
    rows = reto_queue(store)
    out = [
        f"# reto-queue — {len(rows)} waiting, most blocked first"
        if rows
        else "# reto-queue — nothing waits on you"
    ]
    now = datetime.now(UTC)
    for i, r in enumerate(rows, 1):
        needs = r.answer or r.ask_kind or "decision"
        out.append("")
        out.append(f"{i}. {r.handle} [{needs}] — {r.title}")
        if r.context:
            out.append(f"   context: {r.context}")
        if r.question and r.question != r.title:
            out.append(f"   ask: {r.question}")
        for n, opt in enumerate(r.options, 1):
            out.append(f"   option {n}{' (recommended)' if n == 1 else ''}: {opt}")
        age = f"{(now - r.created_at).days}d old" if r.created_at else "age unknown"
        out.append(f"   impact: blocks {r.blocked} · {age} · {r.klass}")
    body = "\n".join(out)
    body += render_next_section(
        [
            ("get(kind='todo', id=N)", "a row with its comment timeline"),
            ("search(kind='todo', view='asks')", "the same rows grouped by quest"),
        ]
    )
    return Response(body=body)
