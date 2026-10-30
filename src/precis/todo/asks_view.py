"""The asks list — everything that waits on Reto, in three classes.

``search(kind='todo', view='asks')`` and the web ``/asks`` + Needs-you
landing read one model (:func:`asks_model`): rows grouped by class, then
by quest, then by thread. Dark-factory step 1b (td540847,
``docs/backlog/dark-factory.md``); it replaces the review session's
gather step in ``.claude/fleet/review-protocol.md``.

Classes (the highest one a row qualifies for wins):

* **showstopper** — a lane is stopped: ``waiting-for:reto`` on a claimed
  slice (``claimed-by:`` lease), or an explicit ``showstopper`` tag. A
  tripped breaker and a red open release join here when dark-factory
  step 4 gives them a source; today neither exists to read.
* **decision** — a next slice waits: ``ask-user`` / ``ask-user:`` todos,
  an unclaimed ``waiting-for:reto``, gripes tagged ``needs:ruling``.
* **discussion** — a shape question: tag ``discussion`` (todo or gripe).

Review-queue files (``<review dir>/*.md`` with the protocol's frontmatter)
are a fourth source, read straight from disk until the protocol retires;
the dir is ``PRECIS_REVIEW_QUEUE_DIR`` or the checkout's
``.claude/fleet/open``, and a server without one simply has none. A file
that answers via ``precis:<td>`` is dropped when that todo already has a
row. File class: ``kind: design-verdict`` is a discussion; a ``blocks:``
other than ``nothing`` is a showstopper; the rest are decisions.

Quest grouping: :func:`quest_of` walks ``refs.parent_id`` up from the
row to its root and takes the first ``serves`` link to a quest on the
way (the row itself first). Argument happens as comments on the row —
every row starts with its handle so it is a ``get``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis.response import Response
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
#: Open tags that put a todo or gripe on the list (``ask-user:<q>`` is
#: matched by prefix in the query).
_TAGS = (
    "ask-user",
    "waiting-for:reto",
    "needs:ruling",
    "discussion",
    "showstopper",
)
#: Ancestor hops when walking ``parent_id``.
_MAX_HOPS = 20
_QUESTION_CAP = 160
_TITLE_CAP = 72


@dataclass(frozen=True)
class AskRow:
    """One thing that waits on the human."""

    klass: str
    #: ``todo`` | ``gripe`` | ``review`` (a file item with no ref).
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

    def counts(self) -> dict[str, int]:
        return {c: sum(1 for r in self.rows if r.klass == c) for c in CLASSES}


def _quest_key(
    qid: int | None, by_quest: dict[int | None, list[AskRow]]
) -> tuple[int, int, int]:
    if qid is None:
        return (1, 0, 0)
    prio = by_quest[qid][0].quest_prio
    return (0, prio if prio is not None else 5, qid)


def _row_key(r: AskRow) -> tuple[str, int]:
    return (r.kind, r.ref_id if r.ref_id is not None else 0)


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


def _classify(kind: str, tags: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """``(class, reasons)`` for a todo/gripe's open tags; ``None`` when no
    tag puts it on the list."""
    classes: list[str] = []
    reasons: list[str] = []
    if kind == "todo" and any(
        t == "ask-user" or t.startswith("ask-user:") for t in tags
    ):
        classes.append(DECISION)
        reasons.append("ask-user")
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
    if "showstopper" in tags:
        classes.append(SHOWSTOPPER)
        reasons.append("showstopper")
    if "discussion" in tags:
        classes.append(DISCUSSION)
        reasons.append("discussion")
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
            SELECT r.ref_id, r.kind, r.title
              FROM refs r
             WHERE r.kind IN ('todo', 'gripe') AND r.retired_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                      AND (t.value = ANY(%s) OR t.value LIKE 'ask-user:%%'))
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
    for ref_id, kind, title in cands:
        rid = int(ref_id)
        tags = tags_by_ref.get(rid, [])
        verdict = _classify(str(kind), tags)
        if verdict is None:
            continue
        klass, reasons = verdict
        chain = _chain(store, rid)
        quest = _quest_in_chain(store, chain)
        thread = _thread_of(tags, chain, store)
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
            )
        )
    return rows


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


# ── review-queue files ────────────────────────────────────────────

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_QUESTION_RE = re.compile(r"\*\*Question\.\*\*\s*(.+?)(?:\n\s*\n|\Z)", re.DOTALL)
_ANSWER_TD_RE = re.compile(r"precis:\s*td?(\d+)")


def review_queue_dir() -> Path | None:
    """Where the protocol's ``open/*.md`` items live, if anywhere."""
    env = os.environ.get("PRECIS_REVIEW_QUEUE_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        cand = parent / ".claude" / "fleet" / "open"
        if cand.is_dir():
            return cand
    return None


def _review_rows(review_dir: Path | None) -> list[tuple[AskRow, int | None]]:
    """``(row, answered-via todo id)`` per protocol file in ``review_dir``."""
    if review_dir is None or not review_dir.is_dir():
        return []
    out: list[tuple[AskRow, int | None]] = []
    for path in sorted(review_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _FRONT_RE.match(text)
        if m is None:
            continue
        meta: dict[str, str] = {}
        for line in m.group(1).splitlines():
            key, sep, val = line.partition(":")
            if sep:
                meta[key.strip()] = val.strip()
        qm = _QUESTION_RE.search(m.group(2))
        question = _first_line(qm.group(1) if qm else "", _QUESTION_CAP)
        item_kind = meta.get("kind", "")
        blocks = meta.get("blocks", "")
        blocked = f"blocks: {blocks}" if blocks.lower() not in ("", "nothing") else ""
        if item_kind == "design-verdict":
            klass = DISCUSSION
        elif blocked:
            klass = SHOWSTOPPER
        else:
            klass = DECISION
        source = f"source={meta['source']}" if meta.get("source") else ""
        reasons = tuple(v for v in (item_kind, source) if v)
        td = _ANSWER_TD_RE.search(meta.get("answer-via", ""))
        out.append(
            (
                AskRow(
                    klass=klass,
                    kind="review",
                    ref_id=None,
                    handle=f"review:{path.stem}",
                    title=_first_line(question or blocks or path.stem),
                    reasons=("review-queue", *reasons),
                    question=blocked,
                    thread=meta.get("area", ""),
                ),
                int(td.group(1)) if td else None,
            )
        )
    return out


# ── the model and the view ────────────────────────────────────────


def asks_model(store: Store, *, review_dir: Path | None = None) -> AsksModel:
    """Every row that waits on the human, classed and quest-resolved.

    ``review_dir`` defaults to :func:`review_queue_dir`; pass a path to
    read a different protocol directory (tests).
    """
    rows = _ref_rows(store)
    seen = {r.ref_id for r in rows if r.ref_id is not None}
    if review_dir is None:
        review_dir = review_queue_dir()
    for row, td in _review_rows(review_dir):
        if td is not None and td in seen:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (_RANK[r.klass], r.kind, r.ref_id or 0, r.handle))
    return AsksModel(rows=tuple(rows))


def _line(r: AskRow, indent: str) -> str:
    out = f"{' '.join((r.handle, *r.reasons))} — {r.title}"
    if r.question and r.question != r.title:
        out += f"\n{indent}  ? {r.question}"
    return out


def render_asks(store: Store, *, review_dir: Path | None = None) -> Response:
    """``search(kind='todo', view='asks')`` — the list, grouped for reading."""
    model = asks_model(store, review_dir=review_dir)
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
