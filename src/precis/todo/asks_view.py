"""The asks list — everything that waits on Reto, in three classes.

``search(kind='todo', view='asks')`` and the web ``/asks`` + Needs-you
landing read one model (:func:`asks_model`): rows grouped by class, then
by quest, then by thread. Dark-factory step 1b (td540847,
`me692430`). A question for the owner is an
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

Triage (gr652333): the ``asks_triage`` pass stamps every row it handles
with an ``asks-triage:<outcome>`` open tag + ``meta.asks_triage``. A row
stamped ``resolved`` or ``routed`` leaves this list — it is closed,
parked on the coordinator queue (``waiting-for:coordinator`` —
``view='coordinator'``; also where an ``uncertain-halt-outcome`` row
whose prior attempt fails the re-arm audit lands), or folded —
parked ``waiting-for:chore-batch`` so a member carrying a dispatch
signal can't run its own copy — into a ``chore:<kind>`` worker
batch; ``kept`` and ``rearmed`` rows stay and
carry their stamp. Both rendered headers report the rows still WITHOUT
a stamp as ``N untriaged`` — the spec's done-check: after a sweep every
row is stamped, and the ones that aren't are counted out loud.

The Reto queue (gr653289). ``view='reto-queue'`` is every non-discussion
row — showstoppers, asks and bare ``waiting-for:reto`` — plus three
sources the asks tag set never covered: ``STATUS:needs_spec`` gripes
carrying ``reto:ask``/``fix:reto`` (a ruling only Reto can give), open
alerts at ``severity:warn`` or higher outside
:data:`_NOISY_ALERT_SOURCES` (a human condition, not hygiene noise), and
one synthetic ``kind='release'`` row per release pass whose landed main
shas the recorded prod head doesn't cover (the deploy is a shell job).
Every row carries ``needs`` — ``terminal`` (shell / vault / host),
``hands`` (physical or browser work) or ``chat`` (a ruling said in
words) — derived from open tags (:func:`_needs_of`); the render groups
TERMINAL / CHAT / HANDS in that order, and ``view='asks'`` marks a
terminal row ``[terminal]``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.plans.gates import MIN_SHA_LEN, read_prod_head
from precis.response import Response
from precis.store._secret_scope import link_fence, secret_fence, secret_fence_for
from precis.todo import blocker_terminal_sql
from precis.todo.blocked import open_blocked_by
from precis.utils import handle_registry
from precis.utils.memory_threads import PENDING_THREAD_ASKS
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
#: The ``asks_triage`` stamp (gr652333): ``asks-triage:<outcome>`` —
#: ``kept`` stays, ``rearmed`` is a released halt park, ``resolved`` /
#: ``routed`` are hidden by the model.
TRIAGE_PREFIX = "asks-triage:"
#: Stamp outcomes :func:`asks_model` hides — a resolved or routed row
#: left the owner's list; kept/rearmed stay visible with their stamp.
TRIAGE_HIDDEN: frozenset[str] = frozenset({"resolved", "routed"})
#: A routed row's coordinator tag — ``view='coordinator'`` lists it.
COORDINATOR_TAG = "waiting-for:coordinator"
#: ``needs`` values — where a row resolves (gr653289): ``terminal``
#: wants a shell, vault or host login; ``hands`` physical or browser
#: work; ``chat`` a ruling, choice or spec said in words.
NEED_TERMINAL = "terminal"
NEED_CHAT = "chat"
NEED_HANDS = "hands"
#: Group order for :func:`render_reto_queue` — terminal first (the
#: shell jobs), then the chat answers, then hands.
NEED_ORDER: tuple[str, ...] = (NEED_TERMINAL, NEED_CHAT, NEED_HANDS)
#: Open tags putting a row in ``needs='hands'``: ``needs-hands`` is the
#: generic mark, ``want-paper`` the hand-fetch convention
#: (precis-user-queue-help).
_HANDS_TAGS: frozenset[str] = frozenset({"needs-hands", "want-paper"})
#: Alert severities that wait on a human — ``warn`` or higher
#: (``precis.alerts.SEVERITIES`` is the axis); info stays hygiene noise.
_QUEUE_SEVERITIES: frozenset[str] = frozenset({"warn", "critical"})
#: Warn-or-higher alert sources still too noisy for the queue
#: (gr653289 — the exclusion list gets a review after a week; the two
#: nursery names are info-level today and already excluded by severity,
#: pinned so a severity bump can't page the owner).
_NOISY_ALERT_SOURCES: frozenset[str] = frozenset(
    {"nursery:stale-halt", "nursery:long-wait"}
)
#: Open gripe tags that put a ``STATUS:needs_spec`` gripe on the queue —
#: only a ``reto:ask``/``fix:reto`` needs-spec waits on Reto (the rest
#: are the spec pass's job).
_RETO_GRIPES: frozenset[str] = frozenset({"reto:ask", "fix:reto"})
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
    thread_decision: bool = False
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
    #: Open ``blocked-by`` targets of THIS row — the other direction of
    #: the gr656970 sequencing edge: an ask itself held by live work
    #: names its blocker like the work board's ``after`` column.
    after: tuple[str, ...] = ()
    #: The ``asks-triage:<outcome>`` stamp's outcome, ``""`` when the row
    #: was never triaged (gr652333 — ``asks_model`` drops ``resolved`` /
    #: ``routed``; the header counts what's left unstamped).
    triage: str = ""
    #: Where the row resolves — ``terminal`` | ``chat`` | ``hands``
    #: (:func:`_needs_of`; the queue groups on it, asks marks
    #: ``[terminal]``).
    needs: str = NEED_CHAT
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
            f"""
            WITH RECURSIVE chain AS (
                SELECT ref_id, parent_id, 0 AS depth FROM refs
                 WHERE ref_id = %s AND retired_at IS NULL
                   AND {secret_fence("refs", path="asks_view")}
                UNION ALL
                SELECT r.ref_id, r.parent_id, c.depth + 1 FROM refs r
                  JOIN chain c ON r.ref_id = c.parent_id
                 WHERE r.retired_at IS NULL AND c.depth < %s
                   AND {secret_fence("r", path="asks_view")}
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
            f"""
            SELECT l.src_ref_id, q.ref_id, q.title, q.prio
              FROM links l JOIN refs q ON q.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = ANY(%s) AND l.relation = 'serves'
               AND q.kind = 'quest' AND q.retired_at IS NULL
               AND {link_fence(path="asks_view")}
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
            f"""
            SELECT rt.ref_id, t.value
              FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
               AND {secret_fence_for("rt.ref_id", path="asks_view")}
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


def _needs_of(tags: list[str]) -> str:
    """The row's ``needs`` bucket (gr653289): ``needs-terminal`` →
    terminal, ``needs-hands``/``want-paper`` → hands, else chat.
    Terminal wins when both stick — a shell job is never only hands."""
    if "needs-terminal" in tags:
        return NEED_TERMINAL
    if _HANDS_TAGS.intersection(tags):
        return NEED_HANDS
    return NEED_CHAT


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
               AND {link_fence(path="asks_view")}
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
    kind: str,
    tags: list[str],
    ask: dict[str, Any] | None = None,
    status: str = "open",
) -> tuple[str, tuple[str, ...]] | None:
    """``(class, reasons)`` for a todo/gripe/alert's open tags and
    ``meta.ask``; ``None`` when no tag puts it on the list. ``status``
    is the row's effective STATUS — a gripe earns the
    ``reto:ask``/``fix:reto`` reason only at ``needs_spec``."""
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
    if kind == "gripe" and status == "needs_spec":
        # gr653289: a reto:ask/fix:reto needs-spec is a ruling for Reto,
        # not a job the spec pass writes. On any other status the tag is
        # parked context — the row lists only if another tag admits it.
        reto = [t for t in tags if t in _RETO_GRIPES]
        if reto:
            classes.append(DECISION)
            reasons.extend(reto)
    if kind == "alert" and "alert-state:open" in tags:
        # gr653289: a warn-or-higher open alert is a human condition —
        # info is hygiene noise, and _NOISY_ALERT_SOURCES stays out even
        # at warn.
        sev = next(
            (t[len("severity:") :] for t in tags if t.startswith("severity:")),
            "warn",
        )
        src = next(
            (t[len("alert-source:") :] for t in tags if t.startswith("alert-source:")),
            "",
        )
        if sev in _QUEUE_SEVERITIES and src not in _NOISY_ALERT_SOURCES:
            classes.append(DECISION)
            reasons.append(f"severity:{sev}")
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
            f"""
            WITH pending AS ({PENDING_THREAD_ASKS})
            SELECT r.ref_id, r.kind, r.title, r.meta->'ask', r.created_at,
                   r.updated_at,
                   COALESCE(
                         (SELECT t3.value FROM ref_tags rt3
                            JOIN tags t3 ON t3.tag_id = rt3.tag_id
                           WHERE rt3.ref_id = r.ref_id
                             AND t3.namespace = 'STATUS'
                           LIMIT 1),
                         'open') AS status, pending.ask_id IS NOT NULL
              FROM refs r LEFT JOIN pending ON pending.ask_id = r.ref_id
             WHERE r.kind IN ('todo', 'gripe', 'alert') AND r.retired_at IS NULL
               AND {secret_fence("r", path="asks_view")}
               -- only alerts ever stamp resolved_at: a resolved alert
               -- leaves the list even without a terminal STATUS tag
               AND r.resolved_at IS NULL
               AND (pending.ask_id IS NOT NULL OR (EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                          AND (t.value = ANY(%s) OR t.value LIKE 'ask-user:%%'
                               OR t.value LIKE 'for-reto:%%'
                               OR t.value LIKE 'rescue:%%'))
                    -- gr653289: a needs_spec gripe carrying reto:ask /
                    -- fix:reto waits on Reto's ruling, spec pass or not
                    OR (r.kind = 'gripe'
                        AND EXISTS (
                            SELECT 1 FROM ref_tags rt JOIN tags t
                              ON t.tag_id = rt.tag_id
                             WHERE rt.ref_id = r.ref_id
                               AND t.namespace = 'OPEN'
                               AND t.value IN ('reto:ask', 'fix:reto'))
                        AND EXISTS (
                            SELECT 1 FROM ref_tags rt JOIN tags t
                              ON t.tag_id = rt.tag_id
                             WHERE rt.ref_id = r.ref_id
                               AND t.namespace = 'STATUS'
                               AND t.value = 'needs_spec'))
                    -- every open alert is a candidate; _classify keeps
                    -- warn-or-higher outside _NOISY_ALERT_SOURCES
                    OR (r.kind = 'alert'
                        AND EXISTS (
                            SELECT 1 FROM ref_tags rt JOIN tags t
                              ON t.tag_id = rt.tag_id
                             WHERE rt.ref_id = r.ref_id
                               AND t.namespace = 'OPEN'
                               AND t.value = 'alert-state:open')))
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s))
             ORDER BY r.ref_id
            """,
            (list(_TAGS), list(_TERMINAL)),
        ).fetchall()
    tags_by_ref = _open_tags(store, [int(c[0]) for c in cands])
    rows: list[AskRow] = []
    for ref_id, kind, title, ask_meta, created, updated, status, pending in cands:
        rid = int(ref_id)
        tags = tags_by_ref.get(rid, [])
        ask = ask_meta if isinstance(ask_meta, dict) else None
        verdict = (
            (DECISION, ("ask-user",))
            if pending
            else _classify(str(kind), tags, ask, status=str(status))
        )
        if verdict is None:
            continue
        klass, reasons = verdict
        chain = _chain(store, rid)
        quest = _quest_in_chain(store, chain)
        thread = _thread_of(tags, chain, store)
        blocks = _ask_blocks(ask)
        if blocks is not None:
            blocked_chain = _chain(store, blocks)
            if not blocked_chain:
                blocks = None
            elif quest is None or not thread:
                # A parentless question sits where the visible slice it blocks sits.
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
                title=_first_line(title, cap=140 if pending else _TITLE_CAP),
                reasons=reasons,
                question="" if pending else _ask_question(store, rid, tags),
                thread_decision=pending,
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
                triage=next(
                    (
                        t[len(TRIAGE_PREFIX) :]
                        for t in tags
                        if t.startswith(TRIAGE_PREFIX)
                    ),
                    "",
                ),
                needs=_needs_of(tags),
                created_at=created,
                updated_at=updated,
            )
        )
    ids = [r.ref_id for r in rows if r.ref_id]
    blocked = _blocked_counts(store, ids)
    holds = open_blocked_by(store, ids)
    return [
        replace(
            r,
            blocked=blocked.get(r.ref_id or 0, 0),
            after=tuple(_h(k, i) for k, i in holds.get(r.ref_id or 0, ())),
        )
        for r in rows
    ]


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
    sorted most-blocked first within a class (gr595189). A row stamped
    ``asks-triage:resolved``/``:routed`` already left the owner's plate
    (closed, parked on the coordinator queue, or folded into a chore
    batch) — the model drops it (gr652333)."""
    rows = [
        r
        for r in _ref_rows(store)
        if r.thread_decision or r.triage not in TRIAGE_HIDDEN
    ]
    rows.sort(key=lambda r: (_RANK[r.klass], *_blocked_key(r), r.kind, r.ref_id or 0))
    return AsksModel(rows=tuple(rows))


def untriaged_count(rows: Iterable[AskRow]) -> int:
    """Todo rows without an ``asks-triage:`` stamp — the header's
    done-check (gr652333): after a sweep every row is stamped; these
    aren't. The count covers exactly what the sweep can stamp —
    ``asks_triage`` handles ``kind='todo'`` rows only, so gripe, alert
    and synthetic ``kind='release'`` rows never count (gr653289)."""
    return sum(
        1 for r in rows if r.kind == "todo" and not r.triage and not r.thread_decision
    )


def _age(age_days: float) -> str:
    """``3d`` / ``9h`` / ``20m`` — the compact age a waiting ask shows."""
    if age_days >= 1:
        return f"{int(age_days)}d"
    hours = age_days * 24
    if hours >= 1:
        return f"{int(hours)}h"
    return f"{max(1, int(hours * 60))}m"


def _line(r: AskRow, indent: str) -> str:
    # gr653289: a terminal row wears its mark in view='asks' — the same
    # convention precis-user-queue-help's needs-terminal tag fixes.
    mark = "[terminal]" if r.needs == NEED_TERMINAL else ""
    head = " ".join(b for b in (r.handle, mark, *r.reasons) if b)
    out = f"{head} — {r.title} · {_age(r.age_days)}"
    if r.thread_decision:
        return f"{out} · {r.context} · extend / retire / transfer"
    if r.blocked:
        out += f" · blocks {r.blocked}"
    if r.after:
        out += f" · after {','.join(r.after)}"
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
    if untriaged := untriaged_count(model.rows):
        head += f" · {untriaged} untriaged"
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


def _q_haystack(r: AskRow) -> str:
    """The text ``q`` narrows a queue row on — every field the render
    shows plus the classification vocabulary (handle, title, ask
    question/context/options, ``for-reto:`` answer, class, reasons,
    thread/quest), not ``refs.title`` alone, so ``q='ruling'`` keeps the
    ``for-reto:ruling`` rows (gr673370)."""
    return " ".join(
        str(p)
        for p in (
            r.handle,
            r.answer or r.ask_kind or "decision",
            r.title,
            r.question,
            r.context,
            r.klass,
            r.kind,
            r.ask_kind,
            r.answer,
            r.thread,
            r.quest_title,
            r.needs,
            *r.reasons,
            *r.options,
            *r.after,
        )
        if p
    ).lower()


def _on_prod(rec: dict[str, Any] | None, sha: str) -> bool:
    """``sha`` (any hex prefix) is the recorded prod head or one of the
    shas it landed — the coverage test ``gripe_validate.sha_live``
    applies to the same ``release.prod_head`` record."""
    if rec is None or not sha:
        return False

    def match(a: str, b: str) -> bool:
        n = min(len(a), len(b))
        return n >= MIN_SHA_LEN and a[:n] == b[:n]

    sha = sha.lower()
    if match(sha, str(rec.get("head") or "").lower()):
        return True
    return any(
        match(sha, str(s).lower())
        for s in rec.get("landed") or []
        if isinstance(s, str)
    )


def _pending_release_rows(store: Store) -> list[AskRow]:
    """One synthetic row per release pass whose pinned main head the
    recorded prod head doesn't cover (gr653289): a pass root's
    ``meta.release_target = {sha, upto}`` names the newest reviewed main
    head its release ships and the sha12s ahead of prod it carries
    (``release_poll``/``mint_release_pass``). Prod covering ``sha`` —
    the head itself or a ``landed`` entry — means the release rolled
    out; a ``meta.release.confirmed`` stamp or a terminal STATUS settles
    it the same way. ``needs='terminal'``: the deploy is a shell job,
    not a chat answer."""
    with store.pool.connection() as conn:
        cands = conn.execute(
            f"""
            SELECT r.ref_id, r.meta -> 'release_target', r.meta -> 'release',
                   r.created_at
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND {secret_fence("r", path="asks_view")}
               AND r.meta ? 'release_target'
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             ORDER BY r.ref_id
            """,
            (list(_TERMINAL),),
        ).fetchall()
    rec = read_prod_head(store)
    head9 = str(rec.get("head") or "").lower()[:9] if rec else ""
    rows: list[AskRow] = []
    for ref_id, target, rel, created in cands:
        tgt = target if isinstance(target, dict) else {}
        sha = str(tgt.get("sha") or "").strip().lower()
        if not sha or _on_prod(rec, sha):
            continue
        rel = rel if isinstance(rel, dict) else {}
        if isinstance(rel.get("confirmed"), dict):
            continue
        upto = [s for s in tgt.get("upto") or [] if isinstance(s, str)]
        n = len(upto) or 1
        label = str(rel.get("rN") or "").strip() or sha[:12]
        rows.append(
            AskRow(
                klass=DECISION,
                kind="release",
                ref_id=int(ref_id),
                handle=_h("todo", int(ref_id)),
                title=_first_line(
                    f"release {label} pending — {n} "
                    f"sha{'s' if n != 1 else ''} on main await deploy"
                ),
                reasons=("release-pending",),
                ask_kind="deploy",
                context=f"prod head {head9 or 'unrecorded'} — deploy to prod",
                needs=NEED_TERMINAL,
                created_at=created,
            )
        )
    return rows


def reto_queue(store: Store, *, q: str | None = None) -> list[AskRow]:
    """Everything that waits on Reto — every classed row that isn't a
    pure ``discussion``: showstoppers, asks (``ask-user``,
    ``needs:ruling``, ``for-reto:``), a bare ``waiting-for:reto`` row, a
    needs-spec gripe carrying ``reto:ask``/``fix:reto`` and a
    warn-or-higher open alert — plus one synthetic row per release pass
    whose landed main shas the prod head doesn't cover (gr653289).
    TERMINAL first, then CHAT, then HANDS (:data:`NEED_ORDER`); most
    blocked first, then oldest first inside a group. The walk order is
    group-major so a page boundary can never pull a later group ahead of
    an earlier one.

    ``q=`` narrows the set before ordering: every whitespace-split term
    must appear in :func:`_q_haystack` — the same every-term-must-match
    reading ``websearch_to_tsquery`` gives the ref-level ``q=``, as a
    case-insensitive substring check over the fields this view renders."""
    rows = [
        r for r in asks_model(store).rows if r.klass != DISCUSSION
    ] + _pending_release_rows(store)
    if q and q.strip():
        terms = q.lower().split()
        rows = [r for r in rows if all(t in _q_haystack(r) for t in terms)]
    need_rank = {n: i for i, n in enumerate(NEED_ORDER)}
    rows.sort(
        key=lambda r: (
            need_rank.get(r.needs, len(NEED_ORDER)),
            -r.blocked,
            r.created_at.timestamp() if r.created_at else float("inf"),
            r.ref_id or 0,
        )
    )
    return rows


def render_reto_queue(
    store: Store,
    *,
    q: str | None = None,
    page_size: Any = None,
    page: Any = 1,
) -> Response:
    """``search(kind='todo', view='reto-queue')`` — the prioritized queue,
    read top to bottom: context, options (recommended first), impact.

    ``q=`` narrows the queue (:func:`reto_queue`), ``page_size=`` caps the
    window and ``page=`` (via ``args={'page': N}``) walks it — all three
    were silently dropped at the view dispatch (gr673370), which is what
    made a flooded head of the queue unreachable past the render cap."""
    try:
        size = int(page_size) if page_size is not None else 0
        page_n = int(page)
    except (TypeError, ValueError):
        raise BadInput(
            "page/page_size must be positive integers, got "
            f"page={page!r}, page_size={page_size!r}",
            next="search(kind='todo', view='reto-queue', page_size=40)",
        ) from None
    if page_n < 1 or size < 0:
        raise BadInput(
            "page/page_size must be positive integers, got "
            f"page={page!r}, page_size={page_size!r}",
            next="search(kind='todo', view='reto-queue', page_size=40)",
        )
    if page_n > 1 and not size:
        raise BadInput(
            "page= needs page_size= to bound the window",
            next="search(kind='todo', view='reto-queue', "
            "page_size=40, args={'page': 2})",
        )
    rows = reto_queue(store, q=q)
    total = len(rows)
    offset = (page_n - 1) * size
    window = rows[offset : offset + size] if size else rows
    if not rows:
        body = (
            f"# reto-queue — nothing matching q={q!r} waits on you"
            if q and q.strip()
            else "# reto-queue — nothing waits on you"
        )
        body += render_next_section(
            [("search(kind='todo', view='reto-queue')", "the unfiltered queue")]
            if q and q.strip()
            else []
        )
        return Response(body=body, result_count=0)
    if not window:
        body = f"# reto-queue — page {page_n} is past the end ({total} waiting)"
        body += render_next_section(
            [
                (
                    "search(kind='todo', view='reto-queue', "
                    f"page_size={size}, args={{'page': 1}})",
                    "back to the head of the queue",
                )
            ]
        )
        return Response(body=body, result_count=0)
    shown = len(window)
    if page_n > 1:
        frag = f"rows {offset + 1}-{offset + shown} of {total}"
    elif shown < total:
        frag = f"{shown} of {total}"
    else:
        frag = str(total)
    untriaged = untriaged_count(rows)
    out = [
        f"# reto-queue — {frag} waiting, most blocked first"
        + (f" (q={q!r})" if q and q.strip() else "")
        + (f" · {untriaged} untriaged" if untriaged else "")
    ]
    now = datetime.now(UTC)
    # gr653289: the queue is already group-ordered (:func:`reto_queue`),
    # so the window's TERMINAL / CHAT / HANDS sections stay contiguous —
    # a page-2 CHAT row can't precede a page-1 TERMINAL one. A header
    # counts the whole group, not just what this page shows; numbering
    # stays continuous across groups and pages.
    totals = Counter(r.needs for r in rows)
    i = offset
    for need in NEED_ORDER:
        grows = [r for r in window if r.needs == need]
        if not grows:
            continue
        out += ["", f"{need.upper()} ({totals[need]})"]
        for r in grows:
            i += 1
            want = r.answer or r.ask_kind or "decision"
            out.append(f"{i}. {r.handle} [{want}] — {r.title}")
            if r.thread_decision:
                out[-1] += f" · {r.context} · extend / retire / transfer"
                continue
            if r.context:
                out.append(f"   context: {r.context}")
            if r.question and r.question != r.title:
                out.append(f"   ask: {r.question}")
            for n, opt in enumerate(r.options, 1):
                out.append(f"   option {n}{' (recommended)' if n == 1 else ''}: {opt}")
            age = f"{(now - r.created_at).days}d old" if r.created_at else "age unknown"
            hold = f" · after {','.join(r.after)}" if r.after else ""
            out.append(f"   impact: blocks {r.blocked} · {age} · {r.klass}{hold}")
    remaining = total - offset - shown
    if remaining > 0:
        q_frag = f", q={q!r}" if q and q.strip() else ""
        out += [
            "",
            f"… {remaining} more — search(kind='todo', view='reto-queue'"
            f"{q_frag}, page_size={size}, args={{'page': {page_n + 1}}})",
        ]
    body = "\n".join(out)
    body += render_next_section(
        [
            ("get(kind='todo', id=N)", "a row with its comment timeline"),
            ("search(kind='todo', view='asks')", "the same rows grouped by quest"),
        ]
    )
    return Response(
        body=body,
        result_count=shown,
        result_ids=[r.ref_id for r in window if r.ref_id is not None],
    )


# ── the coordinator queue ──────────────────────────────────────────


def coordinator_model(store: Store) -> list[AskRow]:
    """``waiting-for:coordinator`` rows — the queue ``asks_triage``
    routes coordinator-bound work onto (gr652333). These are NOT asks:
    the tag is outside the owner-facing ``_TAGS`` set, so a routed row
    leaves :func:`asks_model` and waits here for the coordinator lane.
    Oldest first — the longest wait surfaces first."""
    with store.pool.connection() as conn:
        cands = conn.execute(
            f"""
            SELECT r.ref_id, r.title, r.created_at, r.updated_at
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND {secret_fence("r", path="asks_view")}
               AND r.resolved_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                      AND t.value = %s)
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             ORDER BY r.created_at, r.ref_id
            """,
            (COORDINATOR_TAG, list(_TERMINAL)),
        ).fetchall()
    return [
        AskRow(
            klass="coordinator",
            kind="todo",
            ref_id=int(ref_id),
            handle=_h("todo", int(ref_id)),
            title=_first_line(title),
            reasons=(COORDINATOR_TAG,),
            created_at=created,
            updated_at=updated,
        )
        for ref_id, title, created, updated in cands
    ]


def render_coordinator(store: Store) -> Response:
    """``search(kind='todo', view='coordinator')`` — the coordinator's own
    queue: asks rows ``asks_triage`` routed away from the owner."""
    rows = coordinator_model(store)
    if not rows:
        body = "# coordinator — nothing waits on the coordinator"
        body += render_next_section(
            [("search(kind='todo', view='asks')", "the owner-facing list")]
        )
        return Response(body=body, result_count=0)
    out = [f"# coordinator — {len(rows)} waiting, oldest first"]
    for r in rows:
        out.append(f"{r.handle} — {r.title} · {_age(r.age_days)}")
    body = "\n".join(out)
    body += render_next_section(
        [
            ("get(kind='todo', id=N)", "a row with its comment timeline"),
            ("search(kind='todo', view='asks')", "the owner-facing list"),
        ]
    )
    return Response(
        body=body,
        result_count=len(rows),
        result_ids=[r.ref_id for r in rows if r.ref_id is not None],
    )
