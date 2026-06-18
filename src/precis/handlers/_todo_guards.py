"""Write-time guards for the todo tree (Slice 1 of todo-tree-plan.md).

Three orthogonal checks fire on every ``put`` that wires a child under
a parent (and on level-tag mutations via ``tag``):

* **parent-exists** — ``parent_id`` must point at a live ``todo`` ref.
* **cycle** — the new edge must not create a loop. Cycles in a tree
  with self-referencing FK are not prevented by the DB; the agent
  layer must check.
* **depth** — the ancestor chain may not exceed ``MAX_DEPTH=10``.
  Pathological splitting (Allen's "procrastinating-by-planning"
  failure mode) stops here.
* **level gradient** — ``level:strategic`` and ``level:tactical`` are
  owner-only. Workers (``asa-chatter``/``asa-worker``/``asa-dreamer``
  MCP sources) cannot create, edit, or delete these tiers. The
  authority gradient is the most load-bearing control in the design.

Identity routing
================

The "who is calling?" verdict is read from ``$PRECIS_SOURCE`` at
guard time (Hub doesn't carry config today; we read env directly).

* unset / empty / ``cli`` / ``user`` → **owner** (interactive operator)
* starts with ``web:`` → **owner** (the precis-web UI passes
  ``web:owner`` per the precis-web plan)
* starts with ``asa-`` → **worker** (chatter / worker / dreamer all
  share the same authority verdict — they're all asa)
* anything else → **owner** (forward-compatible — unknown sources
  are not silently demoted to worker)

The MCP critic flagged silent-demotion as a footgun in adjacent
identity work; we err toward owner so a typo in ``PRECIS_SOURCE``
shows up as "the strategic guard didn't fire" rather than as
"strategic writes started failing in production."
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound

if TYPE_CHECKING:
    from precis.store import Store


#: Hard depth cap on the ancestor chain. See knob #3 in
#: ``docs/design/todo-tree-plan.md``: dreamer + worker + chatter all
#: push depth, so 10 catches the procrastinating-by-planning failure
#: mode without strangling a legitimate
#: strategic→tactical→section→subsection→paragraph→sentence chain.
MAX_DEPTH = 10


#: Closed-prefix level values. ``level:`` is an *open* tag for now
#: (no closed-vocab axis registration) — the gradient is enforced
#: here, not in ``Tag.parse_strict``, so the guard logic stays
#: self-contained inside the todo handler. Slice 1 ships gradient
#: enforcement only; if the vocab ever needs centralised validation
#: we can promote to a closed prefix in a follow-up.
LEVEL_STRATEGIC = "level:strategic"
LEVEL_TACTICAL = "level:tactical"
LEVEL_SUBTASK = "level:subtask"
LEVEL_PROPOSED_TACTICAL = "level:proposed-tactical"
#: Slice-4 schedule tier. The recurring root carries the schedule + the
#: spawn rule; spawned children carry ``level:subtask``. Owner-only —
#: so a worker can't mint a ``* * * * *`` cron that burns the budget.
LEVEL_RECURRING = "level:recurring"

#: Tier names that are owner-only. Workers may neither create refs
#: carrying these tags nor add them via ``tag`` later. Slice 4 added
#: ``level:recurring`` so workers can't mint scheduled work.
_OWNER_ONLY_LEVELS: frozenset[str] = frozenset(
    {LEVEL_STRATEGIC, LEVEL_TACTICAL, LEVEL_RECURRING}
)


# ── auto-run tag namespaces (closed-vocab values) ──────────────────


#: Allowed values for the ``LLM:<model>`` open tag. Presence of any
#: ``LLM:*`` tag flips a todo into the dispatch worker's candidate set
#: (planner-coroutine slice); the value picks the model
#: ``claude_inproc`` shells out with. Closed vocab so a typo
#: (``LLM:opos``) is rejected at write time rather than producing a
#: silent dispatch miss or a budget burn against a wrong model.
_LLM_TAG_VALUES: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})


#: Allowed values for the ``executor:<runner>`` open tag — runners that
#: are NOT an LLM (deterministic code paths). v1 has none registered;
#: future entries: ``fetch`` (web-search + ingest), ``ingest``
#: (file → corpus), ``calc`` (sympy). Same closed-vocab discipline as
#: ``LLM:*`` so unknown values reject at write time.
_EXECUTOR_TAG_VALUES: frozenset[str] = frozenset()


def _check_namespaced_tag(
    tags: list[str] | None,
    *,
    prefix: str,
    allowed: frozenset[str],
) -> None:
    """Reject ``prefix:<value>`` tags whose value isn't in ``allowed``.

    Used by both the ``LLM:`` and ``executor:`` guards. Shared so the
    error shape stays consistent and adding a new namespace is one
    call site.
    """
    if not tags:
        return
    if not allowed:
        # Nothing registered yet — let the tag through. The namespace
        # is reserved but its vocab is being grown over time. Without
        # this short-circuit, the FIRST writer of the namespace hits a
        # 100% rejection wall, which is worse than letting the tag
        # land and rejecting at dispatch time if needed.
        return
    for t in tags:
        if not t.startswith(prefix):
            continue
        value = t.removeprefix(prefix)
        if value not in allowed:
            sorted_allowed = ", ".join(sorted(allowed))
            raise BadInput(
                f"{t!r}: unknown {prefix}<value>; allowed values are "
                f"[{sorted_allowed}]",
                next=(
                    f"use one of [{sorted_allowed}] or omit the {prefix}* tag "
                    "if this work isn't dispatchable"
                ),
            )


def check_llm_tag(tags: list[str] | None) -> None:
    """Reject ``LLM:<value>`` where value is not a registered model."""
    _check_namespaced_tag(tags, prefix="LLM:", allowed=_LLM_TAG_VALUES)


def check_executor_tag(tags: list[str] | None) -> None:
    """Reject ``executor:<value>`` where value is not a registered runner."""
    _check_namespaced_tag(tags, prefix="executor:", allowed=_EXECUTOR_TAG_VALUES)


def has_auto_run_signal(
    tags: list[str] | None,
    meta: dict[str, object] | None,
) -> bool:
    """True when a todo carries something the dispatcher can act on.

    The dispatch worker (``workers/dispatch.py``) only considers a todo
    a candidate if it carries one of three auto-run signals: an
    ``LLM:<model>`` tag, an ``executor:<runner>`` tag, or a legacy
    ``meta.executor`` key. Without any of them the todo is inert — it
    never spawns a ``plan_tick`` job and therefore never gets children.
    Mirror the dispatcher's candidate predicate here so the create-time
    reminder agrees exactly with what would (not) be dispatched.
    """
    for t in tags or []:
        if t.startswith("LLM:") or t.startswith("executor:"):
            return True
    return bool(meta) and "executor" in meta  # type: ignore[operator]


def strategic_lacks_auto_run(
    tags: list[str] | None,
    meta: dict[str, object] | None,
) -> bool:
    """True for a ``level:strategic`` todo with no auto-run signal.

    The reminder condition for the soft create-time hint: a strategic
    planner brief is just inert prose unless it carries an auto-run
    signal, so flag the gap. Non-strategic todos and strategics that
    already carry a signal return ``False`` (no nudge).
    """
    if not tags or LEVEL_STRATEGIC not in tags:
        return False
    return not has_auto_run_signal(tags, meta)


def _caller_source() -> str:
    """Return the caller's source identity, lower-cased and stripped.

    Reads ``PRECIS_SOURCE`` from the environment. The deployment
    pattern (precis-web-plan, asa-bot modes) is to set this once per
    process. Defaults to ``cli`` so an interactive ``precis`` session
    or a unit test runs as owner.
    """
    return (os.environ.get("PRECIS_SOURCE") or "cli").strip().lower()


def is_owner(source: str | None = None) -> bool:
    """True when ``source`` has owner authority over the tree.

    Used by ``_check_level_tags``; exposed so future call sites (the
    web UI's own guard) can reuse the same verdict without
    re-implementing the rule.
    """
    s = source if source is not None else _caller_source()
    if not s or s in ("cli", "user"):
        return True
    if s.startswith("web:"):
        return True
    if s.startswith("asa-"):
        return False
    # Forward-compatible default: unknown sources are owners. See
    # module docstring — we'd rather a typo'd $PRECIS_SOURCE leave
    # the guard inert than silently demote a production worker.
    return True


# ── parent / cycle / depth ─────────────────────────────────────────


def check_parent_exists(store: Store, parent_id: int) -> int:
    """Resolve ``parent_id`` to a live ``todo`` ref or raise.

    Returns the parent's id on success (so the caller can chain into
    a depth walk that needs it). Raises :class:`NotFound` when the
    parent is missing or soft-deleted, :class:`BadInput` when it's a
    live ref of the wrong kind. The kind check is what stops a
    caller from accidentally rooting a todo under a paper.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, deleted_at FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None:
        raise NotFound(
            f"parent todo id={parent_id} not found",
            next=("get(kind='todo', id='/recent') to find the parent's actual id"),
        )
    kind, deleted_at = row[0], row[1]
    if deleted_at is not None:
        raise NotFound(
            f"parent todo id={parent_id} was soft-deleted",
            next="pick a live parent or omit parent_id= for a root",
        )
    if kind != "todo":
        raise BadInput(
            f"parent_id={parent_id} is a {kind!r} ref, not a todo",
            next="parent_id must address another todo",
        )
    return parent_id


def check_no_cycle(store: Store, *, child_id: int, parent_id: int) -> None:
    """Reject a parent assignment that would create a loop.

    The "child = parent" case is the trivial loop and is checked
    inline. The longer case (parent's ancestry already contains
    child) is checked via a recursive CTE — Postgres detects the
    cycle for us when ``CYCLE`` is declared, but writing the same
    short walk by hand keeps the SQL portable.

    Only meaningful on a re-parent operation (today: never — Slice 1
    creates leaves and never moves them). Exposed here so the
    re-parent path slated for Slice 2's web UI tree editor doesn't
    have to invent its own walk.
    """
    if child_id == parent_id:
        raise BadInput(
            f"todo id={child_id} cannot be its own parent",
            next="pick a different parent or omit parent_id=",
        )
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT ref_id, parent_id FROM refs WHERE ref_id = %s
                UNION ALL
                SELECT r.ref_id, r.parent_id
                  FROM refs r
                  JOIN ancestors a ON r.ref_id = a.parent_id
            )
            SELECT 1 FROM ancestors WHERE ref_id = %s LIMIT 1
            """,
            (parent_id, child_id),
        ).fetchone()
    if row is not None:
        raise BadInput(
            f"cycle: todo id={parent_id} is already a descendant of todo id={child_id}",
            next="pick a parent that is not under this todo",
        )


def check_depth_under(store: Store, parent_id: int) -> int:
    """Return the depth of ``parent_id`` (root = 0); raise at MAX_DEPTH-1.

    A new child at depth N+1 is rejected when the parent is already
    at depth ``MAX_DEPTH - 1`` — the resulting tree would be
    ``MAX_DEPTH+1`` deep. Hand the caller the exact recovery hint
    spelled out in the plan: attach a ``waiting-for:`` or a
    ``blocks`` link instead of splitting further.
    """
    depth = _depth_of(store, parent_id)
    if depth >= MAX_DEPTH - 1:
        raise BadInput(
            f"depth limit hit (todo id={parent_id} is at depth "
            f"{depth}, max is {MAX_DEPTH})",
            next=(
                "either do the work, or attach a waiting-for:<x> tag "
                "or rel='blocks' link to record the dependency without "
                "splitting further"
            ),
        )
    return depth


def check_reparent_depth(store: Store, *, child_id: int, new_parent_id: int) -> None:
    """Reject a move that would push the moved subtree past MAX_DEPTH.

    ``check_depth_under`` only measures the *parent* — correct when a
    leaf is being created. A re-parent moves a whole subtree, so the
    deepest resulting node is::

        depth(new_parent) + 1 + height(subtree under child)

    where ``height`` is 0 for a leaf. Rejected on the same boundary
    as the create-time check (``>= MAX_DEPTH``) so a leaf move and a
    leaf create behave identically.
    """
    new_parent_depth = _depth_of(store, new_parent_id)
    height = _subtree_height(store, child_id)
    deepest = new_parent_depth + 1 + height
    if deepest >= MAX_DEPTH:
        raise BadInput(
            f"move rejected: todo id={child_id} has a subtree {height} deep; "
            f"under id={new_parent_id} (depth {new_parent_depth}) the deepest "
            f"node would reach depth {deepest} (max is {MAX_DEPTH})",
            next=(
                "pick a shallower parent, or flatten the subtree first "
                "(record dependencies via rel='blocks' instead of nesting)"
            ),
        )


def _subtree_height(store: Store, ref_id: int) -> int:
    """Return the height of the subtree rooted at ``ref_id`` (leaf → 0).

    Descends ``parent_id`` the other way (children of children), so
    the cost is bounded by the subtree size. Soft-deleted rows are
    excluded — a tombstoned branch doesn't constrain a move.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE sub(ref_id, lvl) AS (
                SELECT ref_id, 0
                  FROM refs WHERE ref_id = %s AND deleted_at IS NULL
                UNION ALL
                SELECT r.ref_id, s.lvl + 1
                  FROM refs r
                  JOIN sub s ON r.parent_id = s.ref_id
                 WHERE r.deleted_at IS NULL
            )
            SELECT max(lvl) FROM sub
            """,
            (ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _depth_of(store: Store, ref_id: int) -> int:
    """Return ``ref_id``'s depth from the strategic root (root → 0).

    Implemented as a recursive CTE walking up ``parent_id``. Cheap
    even at the depth cap (10 rows, one index lookup per).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, parent_id, lvl) AS (
                SELECT ref_id, parent_id, 0
                  FROM refs WHERE ref_id = %s
                UNION ALL
                SELECT r.ref_id, r.parent_id, w.lvl + 1
                  FROM refs r
                  JOIN walk w ON r.ref_id = w.parent_id
            )
            SELECT max(lvl) FROM walk
            """,
            (ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


# ── level / authority ──────────────────────────────────────────────


def check_level_tags_on_create(tags: list[str] | None) -> None:
    """Reject ``level:strategic|tactical`` from worker sources at create.

    The plan calls this the single most load-bearing control: workers
    physically cannot mint strategic or tactical refs. ``proposed-
    tactical`` stays open to everyone so workers / dreamers can
    suggest promotions for owner triage.
    """
    if not tags:
        return
    if is_owner():
        return
    for t in tags:
        if t in _OWNER_ONLY_LEVELS:
            raise BadInput(
                f"{t!r} is owner-only; the current source has worker authority",
                next=(
                    "propose via tag='level:proposed-tactical' instead, "
                    "or run from a non-worker source (web:owner / cli)"
                ),
            )


def check_level_tags_on_tag(
    *,
    add: list[str] | None,
    remove: list[str] | None,
) -> None:
    """Reject level-gradient mutations from worker sources at ``tag``.

    Both ``add`` and ``remove`` are gated — workers can neither
    promote a subtask to strategic nor demote a strategic by yanking
    the tag. The ``proposed-tactical`` tag stays freely mutable for
    anyone.
    """
    if is_owner():
        return
    touched = (add or []) + (remove or [])
    for t in touched:
        if t in _OWNER_ONLY_LEVELS:
            raise BadInput(
                f"{t!r} is owner-only; the current source has worker authority",
                next=(
                    "propose via tag='level:proposed-tactical' instead, "
                    "or run from a non-worker source (web:owner / cli)"
                ),
            )


def check_status_done_artifact(
    store: Store,
    ref_id: int,
    add: list[str] | None,
) -> None:
    """Reject ``STATUS:done`` from worker sources when no artifact landed.

    The planner-coroutine cascade can "cheat" by tagging itself
    ``STATUS:done`` without producing any durable output — no file
    written, no citation minted, no successful child job. The parent
    re-tick then assumes the leaf finished its work and moves on, but
    the actual deliverable doesn't exist. This guardrail prevents that
    by demanding evidence of work before letting the worker close a
    leaf.

    Evidence is any one of:

    * **A file written under the workspace** during this tick —
      detected via ``ref_events`` of source ``write_file`` linked to
      this ref.
    * **A citation minted that points at this todo** — any
      ``kind='citation'`` ref linked from this todo or sharing its
      project tag.
    * **A successful child job** under this todo — at least one
      ``kind='job'`` ref with ``STATUS='succeeded'``.
    * **All live child todos are done** — the parent's role is
      stitching, not writing; if its children resolved, it can close.

    Owner callers pass straight through — the owner can declare
    anything done manually. Workers are bound by the evidence rule.

    Wired into ``TodoHandler.tag`` so it fires on every tag-add by
    workers. Raises :class:`BadInput` when evidence is absent so the
    LLM sees a structured "no, you didn't do the work yet" rather
    than the tag silently sticking.
    """
    if is_owner():
        return
    if not add or "STATUS:done" not in add:
        return
    with store.pool.connection() as conn:
        # 1. Successful child job under this todo?
        cur = conn.execute(
            """
            SELECT 1 FROM refs c
              JOIN ref_tags rt ON rt.ref_id = c.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE c.parent_id = %s
               AND c.kind = 'job'
               AND c.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'succeeded'
             LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
        if cur:
            return
        # 2. All live child todos are STATUS:done / won't-do (stitching role)?
        cur = conn.execute(
            """
            SELECT count(*) FILTER (WHERE c.kind = 'todo' AND c.deleted_at IS NULL
                                     AND COALESCE(
                                       (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                                         WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                                       'open'
                                     ) NOT IN ('done', 'won''t-do')) AS open_kids,
                   count(*) FILTER (WHERE c.kind = 'todo' AND c.deleted_at IS NULL) AS total_kids
              FROM refs c WHERE c.parent_id = %s
            """,
            (ref_id,),
        ).fetchone()
        open_kids = int(cur[0] or 0)
        total_kids = int(cur[1] or 0)
        if total_kids > 0 and open_kids == 0:
            return
        # 3. Citation minted under the same project tag?
        cur = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM refs cit
                  JOIN ref_tags rt_cit ON rt_cit.ref_id = cit.ref_id
                  JOIN tags t_cit ON t_cit.tag_id = rt_cit.tag_id
                 WHERE cit.kind = 'citation'
                   AND cit.deleted_at IS NULL
                   AND t_cit.namespace = 'OPEN'
                   AND t_cit.value LIKE 'project:%%'
                   AND t_cit.value IN (
                       SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                         WHERE rt.ref_id = %s AND t.namespace = 'OPEN'
                           AND t.value LIKE 'project:%%'
                   )
                   AND cit.created_at > now() - interval '24 hours'
            )
            """,
            (ref_id,),
        ).fetchone()
        if cur and cur[0]:
            return
        # 4. File written under the workspace? Detected via ref_events
        #    'put_file' source on a ref tagged the same project. (Best-effort;
        #    the put handlers append these events when wired to.)
        cur = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM ref_events ev
                  JOIN refs r ON r.ref_id = ev.ref_id
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE r.kind IN ('tex','markdown','plaintext','pic')
                   AND r.deleted_at IS NULL
                   AND t.namespace = 'OPEN' AND t.value LIKE 'project:%%'
                   AND t.value IN (
                       SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                         WHERE rt2.ref_id = %s AND t2.namespace = 'OPEN'
                           AND t2.value LIKE 'project:%%'
                   )
                   AND ev.ts > now() - interval '24 hours'
            )
            """,
            (ref_id,),
        ).fetchone()
        if cur and cur[0]:
            return
    raise BadInput(
        f"STATUS:done rejected on todo id={ref_id}: no artifact found "
        "(no file written, no citation minted, no successful child job, "
        "no resolved child todos in the last 24h)",
        next=(
            "do the work first: put(kind='tex', name='<slug>', text='...') "
            "OR put(kind='citation', text='<claim>', source_handle='...', ...) "
            "OR mint subtasks via put(kind='todo', tags=['LLM:<model>'], ...) "
            "and let them resolve. Yield via ask-user:<question> if blocked. "
            "Halt via halt:<reason> if structurally stuck. STATUS:done means "
            "your deliverable EXISTS — not that you thought about it."
        ),
    )


def check_halt_remove(remove: list[str] | None) -> None:
    """Reject ``remove=['halt']`` / ``halt:<reason>`` from worker sources.

    Asymmetric to the level-gradient guard: workers MAY add ``halt`` /
    ``halt:<reason>`` (an escalation — "I think this needs human eyes,"
    or a self-imposed brake like ``halt:cost-cap``) but only the owner
    may remove it (the resume decision). Adds are unrestricted so a
    worker that hits something it can't handle can stop the bleeding
    without waiting for human attention.

    The doable view and dispatch worker both honour ``halt`` /
    ``halt:*`` via the shared ``_DOABLE_EXCLUSION_TAGS`` registry in
    ``_todo_views``; this guard just protects the resume edge.
    """
    if is_owner():
        return
    if not remove:
        return
    for t in remove:
        if t == "halt" or t.startswith("halt:"):
            raise BadInput(
                f"removing {t!r} is owner-only; workers may add halt but not clear it",
                next=(
                    "the halt marker is the owner's resume edge — run from "
                    "a non-worker source (web:owner / cli) to lift it"
                ),
            )


# ── ref-level authority check (delete / re-parent) ─────────────────


def check_owner_only_ref(store: Store, ref_id: int) -> None:
    """Reject a destructive op on a ref carrying an owner-only level.

    Called from ``delete`` and from any future ``re-parent`` path —
    workers must not soft-delete strategic / tactical refs. Owner
    callers pass straight through. The reverse check (workers can
    delete their own subtasks) is the default; this function is the
    veto, not the gate.
    """
    if is_owner():
        return
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT t.namespace || ':' || t.value
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value IN ('level:strategic', 'level:tactical')
            """,
            (ref_id,),
        ).fetchall()
    # Tags live in OPEN namespace with the literal ``level:<tier>``
    # value; the rendered form is just the value.
    hits = [r[0].removeprefix("OPEN:") for r in rows]
    if hits:
        raise BadInput(
            f"todo id={ref_id} carries {hits[0]!r} and is owner-only",
            next="run this from a non-worker source (web:owner / cli)",
        )


# ── builtin (Watches umbrella, etc.) ───────────────────────────────


def check_not_builtin(store: Store, ref_id: int) -> None:
    """Reject destructive ops on refs flagged ``meta.builtin`` non-null.

    Slice 4 footgun protection: the seeded Watches umbrella root
    carries ``meta.builtin='watches-root'`` and would orphan every
    recurring beneath it if deleted. Any future seeded "folder" ref
    (a structural anchor the system depends on) carries the same
    marker and gets the same protection — the check is on the
    presence of the key, not on a specific value, so adding new
    builtins doesn't need a new guard.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'builtin' FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None:
        return
    builtin = row[0]
    if builtin:
        raise BadInput(
            f"todo id={ref_id} is the {builtin!r} builtin and cannot be deleted",
            next=(
                "this ref is a structural anchor (e.g. the Watches umbrella). "
                "If you really need to retire it, clear meta.builtin first "
                "via an explicit DB write."
            ),
        )


# ── schedule (Slice 4) ─────────────────────────────────────────────


def check_schedule_in_meta(meta: dict[str, object] | None):
    """Validate ``meta.schedule`` if present; return the canonical block.

    Returns ``None`` when no schedule is set. Returns the parsed
    :class:`~precis.workers.schedule.parse.Schedule` so the handler can
    rewrite ``meta.schedule`` to its canonical form (``every:``
    shorthand translated to cron) before persistence. Raises
    :class:`BadInput` on any malformed input.

    Kept here next to the level-recurring guard so the two pieces of
    Slice 4 write-time policy live together.
    """
    if not meta:
        return None
    spec = meta.get("schedule")
    if spec is None:
        return None
    # Local import — workers and handlers are imported in either order
    # depending on the entry point, and the parser module is the
    # leaf, so this stays cycle-safe.
    from precis.workers.schedule.parse import validate_schedule

    return validate_schedule(spec)


__all__ = [
    "LEVEL_PROPOSED_TACTICAL",
    "LEVEL_RECURRING",
    "LEVEL_STRATEGIC",
    "LEVEL_SUBTASK",
    "LEVEL_TACTICAL",
    "MAX_DEPTH",
    "check_depth_under",
    "check_executor_tag",
    "check_halt_remove",
    "check_level_tags_on_create",
    "check_level_tags_on_tag",
    "check_llm_tag",
    "check_no_cycle",
    "check_not_builtin",
    "check_owner_only_ref",
    "check_parent_exists",
    "check_reparent_depth",
    "check_schedule_in_meta",
    "check_status_done_artifact",
    "has_auto_run_signal",
    "is_owner",
    "strategic_lacks_auto_run",
]
