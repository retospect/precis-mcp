"""The todo dispatch pass: the factory's second source of work.

A plain todo tagged ``factory:build`` is offered to the factory. This pass
runs right after the gripe dispatch pass, inside its scheduler fire (so only
on a host where ``gripe_dispatch`` is lit), behind the DB setting
``factory.todo_dispatch.enabled`` (default off: a pass with the switch off
reads nothing and writes nothing). Switched on, one fire does, in order:

* **Sweeps.** :func:`sweep_refusals` applies the owner's answer to a refusal
  ask once it names exactly one of ``rewrite``, ``split``, ``drop`` or
  ``close as done`` (any other answer changes nothing and is said once).
  :func:`sweep_ended` releases the source of a chain that ended without a
  land (its root closed in any terminal status, no refusal). The completion
  backstop (:func:`~precis.plans.todo_chain.sweep_landed_sources`) closes
  the sources the gate hook missed. :func:`~precis.plans.todo_chain.sweep_splits`
  closes a source the brief step split once every one of its parts is done
  (a refused or dropped part leaves it open), and a parent tagged
  ``factory:split`` once every one of its child todos is done. Each sweep
  fails on its own.
* **Survey.** :func:`survey` lists the tagged todos that are eligible — open,
  not a plan node or an ask, no open child, no open blocker (a split part
  counts every predecessor that is not live and ``done``), no hold tag
  (the gripe dispatcher's, ``needs-spec``, ``factory:skip``), no live chain
  and none whose land is still being closed out, landable in the repository the gripe dispatcher lands in and unchanged for
  its settle time — by priority, then age; every other tagged todo comes back
  with one reason word.
* **Budget.** The pass may hold ``factory.todo_dispatch.max_open`` unlanded
  todo chains (brief stage and stalled ones count). When the gripe pass had
  free build slots and found no eligible gripe, those slots are offered
  (:class:`GripeOffer`) and the todo pass may grow into them; while an
  eligible gripe exists it starts nothing beyond its base. At most
  :data:`_MINT_BATCH` chains start per pass; running chains are never
  cancelled.
* **Mint.** Each pick is checked first for a path no lane may write or a
  skill that governs the factory (refused by code: one ask, no session
  spent), then minted by :func:`precis.plans.templates.mint_todo_build`.

The pass bills no model call; each mint is the template's own transaction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from psycopg import Connection

from precis import settings as psettings
from precis.errors import BadInput
from precis.plans import governs as _governs
from precis.plans import lanes as _lanes
from precis.plans import route as _route
from precis.plans.templates import PENDING_SHA, mint_todo_build
from precis.plans.todo_chain import (
    _DONE_HEAD,
    _DONE_MID,
    _DONE_TAIL,
    FACTORY_BUILD_TAG,
    NEEDS_SPEC_TAG,
    REFUSAL_KEY,
    SKIP_TAG,
    SOURCE_TODO_KEY,
    SPLIT_PART_KEY,
    refuse_source,
    set_source_done,
    sweep_landed_sources,
    sweep_splits,
)
from precis.principal import worker_comment_meta
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.blocked import open_blocked_by, undone_blocked_by
from precis.workers.gripe_dispatch import (
    _LANDABLE_PROJECTS,
    _MIN_AGE_MINUTES,
    HOLD_TAGS,
    chains,
)
from precis.workers.gripe_dispatch import (
    SERVICE_NAME as GRIPE_SERVICE,
)
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

NAME = "todo_dispatch"
ENABLED_KEY = "factory.todo_dispatch.enabled"
MAX_OPEN_KEY = "factory.todo_dispatch.max_open"
DEFAULT_MAX_OPEN = 3
#: New chains one pass starts at most, whatever the budget says: a large
#: free offer ramps over several passes (the brief sessions share one
#: three-slot lane with gripe reviews).
_MINT_BATCH = 3
#: Entries of the pass report's ``skipped`` list; the rest is counted.
_REPORT_CAP = 50
#: The one comment a todo bound for another repository gets.
OTHER_REPO_PREFIX = "dispatch: skipped other repository:"
#: What :func:`precis.plans.route.coordinator_route` opens with for graph-only
#: text — the rest of its reasons name a deny path.
_GRAPH_ONLY_PREFIX = "prod-graph only"
#: ``meta`` keys :func:`sweep_refusals` and :func:`sweep_ended` stamp so each
#: ask and each root is handled once.
_APPLIED_KEY = "refusal_applied"
_RELEASED_KEY = "source_released"
_COMMENT_KIND = "todo_comment"
_ANSWERS = {
    "close as done": "close",
    "rewrite": "rewrite",
    "split": "split",
    "drop": "drop",
}
_UNREAD = "unread"
#: A closed refusal ask waits this long before its answer is read, so a
#: comment posted just after the close is not missed.
_ANSWER_GRACE_MINUTES = 2


def todo_dispatch_enabled(store: Store) -> bool:
    """The ``factory.todo_dispatch.enabled`` switch (default off)."""
    return bool(psettings.get_bool(ENABLED_KEY, store=store))


def max_open(store: Store) -> int:
    """``factory.todo_dispatch.max_open``: the base number of unlanded todo
    chains; a negative value reads as 0, an unreadable one as the default."""
    raw = psettings.get_int(MAX_OPEN_KEY, store=store)
    return DEFAULT_MAX_OPEN if raw is None else max(raw, 0)


@dataclass(frozen=True, slots=True)
class GripeOffer:
    """What the gripe pass left for the todo pass this fire."""

    #: The gripe pass really ran (it returned its figures).
    lit: bool
    #: It had build slots, looked, and found no eligible gripe.
    idle: bool
    #: Build slots it would have filled: offered to the todo pass.
    free: int


def offer_from(result: BatchResult) -> GripeOffer:
    """The offer a gripe pass result makes. A dark or read-only pass
    returned no figures and is not lit; with ``slots == 0`` the pass did
    not look for gripes, so it is not idle either."""
    extra = result.extra or {}
    if result.handler != GRIPE_SERVICE or "slots" not in extra:
        return GripeOffer(lit=False, idle=False, free=0)
    slots = int(extra["slots"])
    idle = (
        not extra.get("parked_full") and slots > 0 and int(extra.get("picked", 0)) == 0
    )
    return GripeOffer(lit=True, idle=idle, free=slots if idle else 0)


def budget(base: int, offer: GripeOffer, open_todo: int) -> int:
    """New chains the pass may start: the base plus the offered slots, less
    the unlanded todo chains already open."""
    return max(base + offer.free - open_todo, 0)


@dataclass(frozen=True, slots=True)
class TodoPick:
    todo_id: int
    title: str
    prio: int | None


def _closing_sql(root: str, src: str) -> str:
    """SQL predicate over the chain root alias ``root`` and its source alias
    ``src``: the chain has landed and its source is waiting to be closed —
    the root's ``sha-in-prod`` gate is done with a real sha, the source
    carries a ``landed:`` comment naming it and not yet this root's
    ``done:`` comment (:func:`~precis.plans.todo_chain.close_source` can
    still succeed). Parameters: :data:`_CLOSING_PARAMS`."""
    return f"""EXISTS (SELECT 1 FROM refs g
        WHERE g.kind = 'todo' AND g.retired_at IS NULL
          AND g.meta->'gate'->>'kind' = 'sha-in-prod'
          AND g.meta->'gate'->'args'->>'sha' <> %(pending)s
          AND EXISTS (SELECT 1 FROM ref_tags gp JOIN tags t USING (tag_id)
                       WHERE gp.ref_id = g.ref_id AND t.namespace = 'OPEN'
                         AND t.value = 'plan:' || {root}.ref_id)
          AND EXISTS (SELECT 1 FROM ref_tags gs JOIN tags t USING (tag_id)
                       WHERE gs.ref_id = g.ref_id AND t.namespace = 'STATUS'
                         AND t.value = 'done')
          AND EXISTS (SELECT 1 FROM chunks c
                       WHERE c.ref_id = {src}.ref_id
                         AND c.chunk_kind = 'todo_comment' AND c.retired_at IS NULL
                         AND lower(c.text) LIKE 'landed: '
                             || lower(left(g.meta->'gate'->'args'->>'sha', 9)) || '%%')
          AND NOT EXISTS (SELECT 1 FROM chunks d
                           WHERE d.ref_id = {src}.ref_id
                             AND d.chunk_kind = 'todo_comment' AND d.retired_at IS NULL
                             AND d.text = %(done_head)s
                                 || lower(left(g.meta->'gate'->'args'->>'sha', 9))
                                 || %(done_mid)s || {root}.ref_id || %(done_tail)s))"""


_CLOSING_PARAMS = {
    "pending": PENDING_SHA,
    "done_head": _DONE_HEAD,
    "done_mid": _DONE_MID,
    "done_tail": _DONE_TAIL,
}

_SURVEY_SQL = f"""
    SELECT r.ref_id, r.title, r.prio, r.meta,
           (SELECT t.value FROM ref_tags st JOIN tags t USING (tag_id)
             WHERE st.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
           COALESCE((SELECT array_agg(t.value) FROM ref_tags ot JOIN tags t USING (tag_id)
                      WHERE ot.ref_id = r.ref_id AND t.namespace IN ('OPEN', 'FLAG')),
                    '{{}}'),
           EXISTS (SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id AND c.kind = 'todo'
                      AND c.retired_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM ref_tags ct JOIN tags t USING (tag_id)
                                       WHERE ct.ref_id = c.ref_id
                                         AND t.namespace = 'STATUS'
                                         AND t.value = ANY(%(terminal)s))),
           EXISTS (SELECT 1 FROM refs root
                    WHERE root.kind = 'todo' AND root.retired_at IS NULL
                      AND root.meta->>%(key)s = r.ref_id::text
                      AND EXISTS (SELECT 1 FROM ref_tags pt JOIN tags t USING (tag_id)
                                   WHERE pt.ref_id = root.ref_id
                                     AND t.namespace = 'OPEN'
                                     AND t.value = 'plan:' || root.ref_id)
                      AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                                       WHERE rt.ref_id = root.ref_id
                                         AND t.namespace = 'STATUS'
                                         AND t.value = ANY(%(terminal)s))),
           EXISTS (SELECT 1 FROM refs root
                    WHERE root.kind = 'todo' AND root.retired_at IS NULL
                      AND root.meta->>%(key)s = r.ref_id::text
                      AND {_closing_sql("root", "r")}),
           COALESCE(greatest(
               r.updated_at,
               (SELECT max(tg.created_at) FROM ref_tags tg WHERE tg.ref_id = r.ref_id),
               (SELECT max(c.created_at) FROM chunks c
                 WHERE c.ref_id = r.ref_id AND c.retired_at IS NULL)
           ), r.created_at) < now() - %(minutes)s * interval '1 minute'
      FROM refs r
     WHERE r.kind = 'todo' AND r.retired_at IS NULL
       AND EXISTS (SELECT 1 FROM ref_tags ft JOIN tags t USING (tag_id)
                    WHERE ft.ref_id = r.ref_id AND t.namespace IN ('OPEN', 'FLAG')
                      AND t.value = %(tag)s)
     ORDER BY COALESCE(r.prio, 5), r.created_at, r.ref_id
"""


def _other_repo(tags: list[str]) -> str | None:
    """Why the todo is not landable where the gripe dispatcher lands, or
    ``None``: a ``repo:`` tag other than the default repository, or a
    ``project:`` tag outside the gripe dispatcher's allowlist."""
    for t in tags:
        if t.startswith("repo:") and t[len("repo:") :].strip() != _lanes.DEFAULT_REPO:
            return t
        if t.startswith("project:") and t[len("project:") :] not in _LANDABLE_PROJECTS:
            return t
    return None


def survey(store: Store) -> tuple[list[TodoPick], list[dict[str, Any]]]:
    """``(eligible, skipped)`` over the live todos tagged
    :data:`FACTORY_BUILD_TAG`.

    ``eligible`` is in dispatch order (priority, then age, then id).
    ``skipped`` has one ``{todo, reason}`` per other tagged todo, the reason
    the first failing condition's word: ``not-open``, ``plan-node``,
    ``ask``, ``hold-tag``, ``needs-spec``, ``skip-tag``, ``open-child``,
    ``blocked`` (for a split part: any predecessor not live and ``done``,
    dropped or retired included), ``chained``, ``closing`` (its chain
    landed and the source is not yet closed), ``other-repo``,
    ``settling``. A todo that does not carry the tag is never read."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            _SURVEY_SQL,
            {
                "terminal": sorted(TERMINAL_TODO_STATUSES),
                "key": SOURCE_TODO_KEY,
                "minutes": _MIN_AGE_MINUTES,
                "tag": FACTORY_BUILD_TAG,
                **_CLOSING_PARAMS,
            },
        ).fetchall()
    blocked = open_blocked_by(store, [int(r[0]) for r in rows])
    # A split part sequences on its predecessor: only a live, done one
    # releases it — a dropped (``won't-do``, ``auto-timeout``) or retired
    # part holds the next, where an ordinary blocker clears on terminal.
    undone = undone_blocked_by(
        store,
        [int(r[0]) for r in rows if isinstance((r[3] or {}).get(SPLIT_PART_KEY), dict)],
    )
    holds = set(HOLD_TAGS)
    eligible: list[TodoPick] = []
    skipped: list[dict[str, Any]] = []
    for (
        ref_id,
        title,
        prio,
        meta,
        status,
        tags,
        child,
        chained,
        closing,
        settled,
    ) in rows:
        todo = int(ref_id)
        tagged = [str(t) for t in tags]
        reason: str | None = None
        if status != "open":
            reason = "not-open"
        elif any(t.startswith("plan:") for t in tagged):
            reason = "plan-node"
        elif isinstance((meta or {}).get("ask"), dict):
            reason = "ask"
        elif holds & set(tagged):
            reason = "hold-tag"
        elif NEEDS_SPEC_TAG in tagged:
            reason = "needs-spec"
        elif SKIP_TAG in tagged:
            reason = "skip-tag"
        elif child:
            reason = "open-child"
        elif todo in blocked or todo in undone:
            reason = "blocked"
        elif chained:
            reason = "chained"
        elif closing:
            reason = "closing"
        elif _other_repo(tagged) is not None:
            reason = "other-repo"
        elif not settled:
            reason = "settling"
        if reason is None:
            eligible.append(TodoPick(todo, str(title or ""), prio))
        else:
            skipped.append({"todo": todo, "reason": reason})
    return eligible, skipped


def _comment(
    store: Store,
    todo_id: int,
    text: str,
    *,
    once_prefix: str | None = None,
    conn: Any = None,
) -> bool:
    """One worker-stamped ``todo_comment``; with ``once_prefix`` nothing is
    written when a comment with that prefix is already there. Returns
    whether it wrote."""
    if conn is None:
        with store.tx() as own:
            return _comment(store, todo_id, text, once_prefix=once_prefix, conn=own)
    # The check and the append are one critical section: overlapping
    # passes converge on one comment.
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (todo_id,))
    if (
        once_prefix is not None
        and conn.execute(
            "SELECT 1 FROM chunks WHERE ref_id = %s AND chunk_kind = %s "
            "AND retired_at IS NULL AND text LIKE %s LIMIT 1",
            (todo_id, _COMMENT_KIND, once_prefix.replace("%", r"\%") + "%"),
        ).fetchone()
    ):
        return False
    ord_ = conn.execute(
        "SELECT coalesce(max(ord), 0) + 1 FROM chunks WHERE ref_id = %s", (todo_id,)
    ).fetchone()
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=int(ord_[0]) if ord_ else 1,
                text=text,
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )
    return True


def _note_other_repo(store: Store, todo_id: int) -> None:
    allowed = ", ".join(f"project:{p}" for p in sorted(_LANDABLE_PROJECTS))
    _comment(
        store,
        todo_id,
        f"{OTHER_REPO_PREFIX} the factory builds only in "
        f"{_lanes.DEFAULT_REPO} ({allowed}); this todo names another target. "
        "No chain minted.",
        once_prefix=OTHER_REPO_PREFIX,
    )


# -- the fenced-path check ---------------------------------------------------


def _todo_text(store: Store, todo_id: int, title: str) -> str:
    """The text the pre-mint check judges: the title and the body."""
    body = [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == TODO_BODY_KIND and c.text
    ]
    return "\n\n".join([title, *body])


def _governed_slug(text: str) -> tuple[str, str] | None:
    """``(slug, path)`` of a skill that governs the factory and that
    ``text`` names by its slug (the file's stem) as a whole token. The route
    reads path-shaped tokens only, so a bare slug never reaches it. Only a
    hyphenated stem is matched: a one-word stem (``bug``, ``land``) is an
    ordinary word, and such a skill is caught by its path alone. A neighbour
    that is a word character, ``/`` or ``-`` (or a ``.`` that starts an
    extension) makes it part of a longer token or a path, not a mention."""
    for path in _governs.governed_skill_paths():
        slug = PurePosixPath(path).stem
        if "-" not in slug:
            continue
        pattern = rf"(?<![\w./-]){re.escape(slug)}(?![\w/-])(?!\.\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return slug, path
    return None


def fenced_refusal(store: Store, pick: TodoPick) -> tuple[str, str] | None:
    """``(refusal reason, why)`` when the todo must not be minted: ``decide``
    when it names a path no lane may write or a governed skill (by path or
    by slug), ``not-code`` when it is prod-graph only work; ``None`` when a
    builder lane may take it. A failure of the check itself fails open, as
    the gripe pass does."""
    try:
        text = _todo_text(store, pick.todo_id, pick.title)
        deny = _lanes.deny_paths_for(store) + _governs.governed_skill_paths()
        routed = _route.coordinator_route(text, deny)
        if routed is not None and not routed.startswith(_GRAPH_ONLY_PREFIX):
            return "decide", f"the todo {routed}; no lane may write it"
        named = _governed_slug(text)
        if named is not None:
            slug, path = named
            return (
                "decide",
                f"the todo names the skill `{slug}` (`{path}`), which governs "
                "the factory; no lane may write it",
            )
        if routed is not None:
            return "not-code", f"the todo is {routed}"
    except Exception:
        log.warning(
            "todo_dispatch: fence check on td%d failed; minting anyway",
            pick.todo_id,
            exc_info=True,
        )
    return None


# -- sweeps ------------------------------------------------------------------


def _answer_text(store: Store, ask_id: int, *, conn: Connection) -> str:
    """Read the newest explicit answer stamped by the authenticated web UI."""
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(ask_id, conn=conn)
        if c.chunk_kind == _COMMENT_KIND
        and str(c.meta.get("principal") or "").startswith("user:")
        and str(c.text or "").lstrip().lower().startswith("answer:")
    ]
    if not comments:
        return ""
    return str(max(comments, key=lambda c: (c.ord, c.id)).text).lstrip()[
        len("answer:") :
    ]


def _outcome(answer: str) -> str:
    """Accept one literal choice, with case and whitespace normalized."""
    return _ANSWERS.get(" ".join(answer.lower().split()), _UNREAD)


_APPLIED_TEXT = {
    "rewrite": "removed `needs-spec`; the todo re-enters intake with its new text "
    "once it has settled",
    "split": "removed `needs-spec` and `factory:build`; the parts are separate "
    "todos for the owner to tag",
    "drop": f"removed `needs-spec` and `{FACTORY_BUILD_TAG}`",
    "close": f"removed `needs-spec` and `{FACTORY_BUILD_TAG}` and closed the "
    "todo as done",
}
_UNREAD_TEXT = (
    "dispatch: could not read an owner answer on td{ask}. Expected one literal "
    "`answer:` choice from the signed-in web UI. Nothing changed: remove "
    "`needs-spec` to offer the "
    f"todo again, or remove `{FACTORY_BUILD_TAG}` to drop it."
)


def _answered_refusals(
    conn: Connection, *, source: int | None = None
) -> list[tuple[int, int]]:
    """``(source, ask)`` for each open todo tagged ``needs-spec`` whose
    newest refusal ask has been terminal for :data:`_ANSWER_GRACE_MINUTES`
    and is not yet applied."""
    rows = conn.execute(
        """
        SELECT s.ref_id, a.ref_id
          FROM refs s
          JOIN LATERAL (
              SELECT x.ref_id, x.meta, x.created_at FROM refs x
               WHERE x.kind = 'todo' AND x.retired_at IS NULL
                 AND x.parent_id = s.ref_id AND x.meta ? %(rk)s
               ORDER BY x.ref_id DESC LIMIT 1) a ON TRUE
         WHERE s.kind = 'todo' AND s.retired_at IS NULL
           AND (%(source)s::bigint IS NULL OR s.ref_id = %(source)s)
           AND NOT (a.meta ? %(applied)s)
           AND a.meta->%(rk)s::text->>'source' = s.ref_id::text
           AND EXISTS (SELECT 1 FROM ref_tags nt JOIN tags t USING (tag_id)
                        WHERE nt.ref_id = s.ref_id
                          AND t.namespace IN ('OPEN', 'FLAG')
                          AND t.value = %(needs)s AND nt.created_at <= a.created_at)
           AND EXISTS (SELECT 1 FROM ref_tags st JOIN tags t USING (tag_id)
                        WHERE st.ref_id = s.ref_id AND t.namespace = 'STATUS'
                          AND t.value = 'open' AND st.created_at <= a.created_at)
           AND EXISTS (SELECT 1 FROM ref_tags ft JOIN tags t USING (tag_id)
                        WHERE ft.ref_id = s.ref_id
                          AND t.namespace IN ('OPEN', 'FLAG')
                          AND t.value = %(build)s AND ft.created_at <= a.created_at)
           AND EXISTS (SELECT 1 FROM ref_tags ast JOIN tags t USING (tag_id)
                        WHERE ast.ref_id = a.ref_id AND t.namespace = 'STATUS'
                          AND t.value = ANY(%(terminal)s)
                          AND ast.created_at
                              < now() - %(grace)s * interval '1 minute')
         ORDER BY s.ref_id
        """,
        {
            "rk": REFUSAL_KEY,
            "source": source,
            "applied": _APPLIED_KEY,
            "needs": NEEDS_SPEC_TAG,
            "build": FACTORY_BUILD_TAG,
            "terminal": sorted(TERMINAL_TODO_STATUSES),
            "grace": _ANSWER_GRACE_MINUTES,
        },
    ).fetchall()
    return [(int(s), int(a)) for s, a in rows]


def _apply_refusal(store: Store, source: int, ask: int) -> str | None:
    with store.tx() as conn:
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (source,))
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ask,))
        if (source, ask) not in _answered_refusals(conn, source=source):
            return None
        outcome = _outcome(_answer_text(store, ask, conn=conn))
        tags = store.tags_for(source, conn=conn)
        if outcome == _UNREAD:
            _comment(store, source, _UNREAD_TEXT.format(ask=ask), conn=conn)
        else:
            remove = {NEEDS_SPEC_TAG}
            if outcome != "rewrite":
                remove.add(FACTORY_BUILD_TAG)
            for tag in tags:
                if tag.prefix is None and tag.value in remove:
                    store.remove_tag(source, tag, conn=conn)
            _comment(
                store,
                source,
                f"dispatch: applied the answer to td{ask} ({outcome}): "
                f"{_APPLIED_TEXT[outcome]}.",
                conn=conn,
            )
            if outcome == "close":
                set_source_done(store, source, tags, conn=conn)
        store.stamp_ref_meta(ask, {_APPLIED_KEY: {"outcome": outcome}}, conn=conn)
    return outcome


def sweep_refusals(store: Store) -> list[dict[str, Any]]:
    """Apply the owner's answer to every closed, unapplied refusal ask
    (:func:`~precis.plans.todo_chain.refuse_source`) that has been closed for
    :data:`_ANSWER_GRACE_MINUTES`. An explicit authenticated owner answer
    with one literal choice is applied: ``rewrite`` removes ``needs-spec`` (the tag stays, so the
    todo re-enters intake), ``split`` also removes ``factory:build`` from
    the original, ``drop`` removes both, ``close as done`` removes both and
    closes the source as done. An answer that names none or several (or no
    answer) changes nothing: outcome ``unread``, one comment saying so. One
    comment on the source says what was applied and from which ask; the
    ask's meta is stamped so it is handled once. Returns ``{todo, ask,
    outcome}`` per ask handled."""
    applied: list[dict[str, Any]] = []
    with store.pool.connection() as conn:
        candidates = _answered_refusals(conn)
    for source, ask in candidates:
        try:
            outcome = _apply_refusal(store, source, ask)
        except Exception:
            log.warning(
                "todo_dispatch: applying the answer to td%d on td%d failed",
                ask,
                source,
                exc_info=True,
            )
            continue
        if outcome is not None:
            applied.append({"todo": source, "ask": ask, "outcome": outcome})
    return applied


def _ended_roots(
    conn: Connection, *, root: int | None = None
) -> list[tuple[int, int, str]]:
    """``(root, source, root status)`` of each todo chain root in a terminal
    status that has not released its source, where the source is still
    open, tagged ``factory:build`` (a tag set before the root ended: a tag a
    person added later is theirs), not ``needs-spec`` and without a live
    chain. Left alone: a root a refusal ask names (the brief step's own
    refusal), a root whose land is being closed out
    (:func:`_closing_sql`) and a source that carries this root's ``done:``
    comment (a person reopened it)."""
    rows = conn.execute(
        f"""
        SELECT root.ref_id, s.ref_id, rs.value
          FROM refs root
          JOIN refs s ON s.kind = 'todo' AND s.retired_at IS NULL
           AND s.ref_id::text = root.meta->>%(key)s
          JOIN LATERAL (
              SELECT t.value, rt.created_at
                FROM ref_tags rt JOIN tags t USING (tag_id)
               WHERE rt.ref_id = root.ref_id AND t.namespace = 'STATUS'
                 AND t.value = ANY(%(terminal)s)
               ORDER BY rt.created_at DESC LIMIT 1) rs ON TRUE
         WHERE root.kind = 'todo' AND root.retired_at IS NULL
           AND (%(root)s::bigint IS NULL OR root.ref_id = %(root)s)
           AND root.meta ? %(key)s AND NOT (root.meta ? %(released)s)
           AND EXISTS (SELECT 1 FROM ref_tags st JOIN tags t USING (tag_id)
                        WHERE st.ref_id = s.ref_id AND t.namespace = 'STATUS'
                          AND t.value = 'open' AND st.created_at < rs.created_at)
           AND EXISTS (SELECT 1 FROM ref_tags ft JOIN tags t USING (tag_id)
                        WHERE ft.ref_id = s.ref_id
                          AND t.namespace IN ('OPEN', 'FLAG')
                          AND t.value = %(build)s
                          AND ft.created_at < rs.created_at)
           AND NOT EXISTS (SELECT 1 FROM ref_tags nt JOIN tags t USING (tag_id)
                            WHERE nt.ref_id = s.ref_id
                              AND t.namespace IN ('OPEN', 'FLAG')
                              AND t.value = %(needs)s)
           AND NOT EXISTS (SELECT 1 FROM refs a
                            WHERE a.kind = 'todo' AND a.meta ? %(rk)s
                              AND a.meta->%(rk)s::text->>'root' = root.ref_id::text)
           AND NOT EXISTS (SELECT 1 FROM refs live
                            WHERE live.kind = 'todo' AND live.retired_at IS NULL
                              AND live.meta->>%(key)s = s.ref_id::text
                              AND NOT EXISTS (SELECT 1 FROM ref_tags lt
                                                JOIN tags t USING (tag_id)
                                               WHERE lt.ref_id = live.ref_id
                                                 AND t.namespace = 'STATUS'
                                                 AND t.value = ANY(%(terminal)s)))
           AND NOT EXISTS (SELECT 1 FROM chunks d
                            WHERE d.ref_id = s.ref_id
                              AND d.chunk_kind = 'todo_comment'
                              AND d.retired_at IS NULL
                              AND d.text LIKE %(done_head)s || '%%'
                                  || %(done_mid)s || root.ref_id || %(done_tail)s)
           AND NOT {_closing_sql("root", "s")}
         ORDER BY root.ref_id
        """,
        {
            "key": SOURCE_TODO_KEY,
            "released": _RELEASED_KEY,
            "root": root,
            "build": FACTORY_BUILD_TAG,
            "needs": NEEDS_SPEC_TAG,
            "rk": REFUSAL_KEY,
            "terminal": sorted(TERMINAL_TODO_STATUSES),
            **_CLOSING_PARAMS,
        },
    ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]


def sweep_ended(store: Store) -> list[int]:
    """Release the source of a chain that ended without a land: a todo chain
    whose root is in a terminal status (``won't-do`` for a dropped chain,
    ``done`` for one that closed without a land: a ``no-op`` verdict, a hand
    close) while its source is still open, tagged ``factory:build`` and not
    ``needs-spec`` loses the tag (one comment), so it is not picked again.
    The root's meta is stamped so one root releases a source once (the owner
    may tag it again later). See :func:`_ended_roots` for what is left
    alone. Returns the ids of the sources released."""
    released: list[int] = []
    with store.pool.connection() as conn:
        candidates = _ended_roots(conn)
    for root, source, _status in candidates:
        try:
            with store.tx() as conn:
                conn.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (source,)
                )
                conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (root,))
                current = _ended_roots(conn, root=root)
                if not current or current[0][1] != source:
                    continue
                status = current[0][2]
                reason = (
                    "was dropped"
                    if status == "won't-do"
                    else "ended without a land (see its build node)"
                )
                for tag in store.tags_for(source, conn=conn):
                    if tag.prefix is None and tag.value == FACTORY_BUILD_TAG:
                        store.remove_tag(source, tag, conn=conn)
                _comment(
                    store,
                    source,
                    f"dispatch: the chain td{root} {reason}; removed "
                    f"`{FACTORY_BUILD_TAG}`. Tag the todo again to offer it "
                    "to the factory.",
                    conn=conn,
                )
                store.stamp_ref_meta(root, {_RELEASED_KEY: source}, conn=conn)
        except Exception:
            log.warning(
                "todo_dispatch: releasing td%d from the ended chain td%d failed",
                source,
                root,
                exc_info=True,
            )
            continue
        released.append(source)
    return released


# -- the pass ----------------------------------------------------------------


def run_todo_dispatch_pass(
    store: Store, *, offer: GripeOffer, host: str | None = None
) -> BatchResult:
    """One fire after the gripe pass: the four sweeps (refusal answers,
    ended chains, the completion backstop, split sources), survey, budget, then one
    todo-build chain per pick (at most :data:`_MINT_BATCH`). ``claimed`` is
    the number of picks tried, ``ok`` the chains minted. With the switch off
    nothing is read beyond the setting and nothing is written."""
    del host
    if not todo_dispatch_enabled(store):
        return BatchResult(handler=NAME, claimed=0, ok=0, failed=0)
    if not offer.lit:
        log.info("todo_dispatch: the gripe pass is not lit — nothing to do")
        return BatchResult(
            handler=NAME,
            claimed=0,
            ok=0,
            failed=0,
            extra={"enabled": True, "gripe_pass": "not lit"},
        )
    from precis.workers.job_types.plan_dispatch import enqueue_roots, read_only_reason

    reason = read_only_reason(store)
    if reason is not None:
        log.info("todo_dispatch: read-only (%s) — minting nothing", reason)
        return BatchResult(
            handler=NAME,
            claimed=0,
            ok=0,
            failed=0,
            extra={"enabled": True, "read_only": reason},
        )
    # Each sweep fails on its own: none may stop the others or the mint.
    applied: list[dict[str, Any]] = []
    released: list[int] = []
    closed: list[int] = []
    split_closed: list[int] = []
    try:
        applied = sweep_refusals(store)
    except Exception:
        log.warning("todo_dispatch: refusal sweep failed", exc_info=True)
    try:
        released = sweep_ended(store)
    except Exception:
        log.warning("todo_dispatch: ended-chain sweep failed", exc_info=True)
    try:
        closed = sweep_landed_sources(store)
    except Exception:
        log.warning("todo_dispatch: landed-source sweep failed", exc_info=True)
    try:
        split_closed = sweep_splits(store)
    except Exception:
        log.warning("todo_dispatch: split-source sweep failed", exc_info=True)
    eligible, skipped = survey(store)
    for s in skipped:
        if s["reason"] == "other-repo":
            try:
                _note_other_repo(store, s["todo"])
            except Exception:
                log.warning(
                    "todo_dispatch: other-repo note on td%d failed",
                    s["todo"],
                    exc_info=True,
                )
    base = max_open(store)
    open_todo = sum(1 for c in chains(store) if c.todo_ids)
    allowed = budget(base, offer, open_todo)
    picks = eligible[: min(allowed, _MINT_BATCH)]
    minted: list[dict[str, int]] = []
    refused: list[dict[str, Any]] = []
    failed = 0
    for pick in picks:
        fenced = fenced_refusal(store, pick)
        if fenced is not None:
            why_reason, why = fenced
            try:
                ask = refuse_source(store, None, pick.todo_id, why_reason, why)
            except Exception:
                failed += 1
                log.warning(
                    "todo_dispatch: refusing td%d failed", pick.todo_id, exc_info=True
                )
                continue
            refused.append(
                {
                    "todo": pick.todo_id,
                    "reason": "fenced-path" if why_reason == "decide" else why_reason,
                    "ask": ask,
                }
            )
            continue
        try:
            mint = mint_todo_build(store, todo_id=pick.todo_id)
        except BadInput as exc:
            skipped.append({"todo": pick.todo_id, "reason": str(exc)})
            continue
        except Exception:
            failed += 1
            log.warning(
                "todo_dispatch: mint for td%d failed", pick.todo_id, exc_info=True
            )
            continue
        minted.append({"todo": pick.todo_id, "root": mint.root_id})
    if minted:
        try:
            enqueue_roots(store, [m["root"] for m in minted])
        except Exception:
            log.warning("todo_dispatch: enqueue of new roots failed", exc_info=True)
    extra: dict[str, Any] = {
        "enabled": True,
        "base": base,
        "free": offer.free,
        "open_todo": open_todo,
        "budget": allowed,
        "minted": minted,
        "skipped": skipped[:_REPORT_CAP],
        "skipped_total": len(skipped),
        "refused": refused,
        "refusals_applied": applied,
        "released": released,
        "closed": closed,
        "split_closed": split_closed,
    }
    log.info(
        "todo_dispatch: base=%d free=%d open=%d budget=%d minted=%d refused=%d "
        "failed=%d",
        base,
        offer.free,
        open_todo,
        allowed,
        len(minted),
        len(refused),
        failed,
    )
    return BatchResult(
        handler=NAME, claimed=len(picks), ok=len(minted), failed=failed, extra=extra
    )


__all__ = [
    "NAME",
    "OTHER_REPO_PREFIX",
    "GripeOffer",
    "TodoPick",
    "budget",
    "fenced_refusal",
    "max_open",
    "offer_from",
    "run_todo_dispatch_pass",
    "survey",
    "sweep_ended",
    "sweep_refusals",
    "todo_dispatch_enabled",
]
