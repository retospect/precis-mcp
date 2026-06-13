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

* unset / empty / ``cli`` / ``user`` → **owner** (interactive Reto)
* starts with ``web:`` → **owner** (the precis-web UI passes
  ``web:reto`` per the precis-web plan)
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

#: Tier names that are owner-only. Workers may neither create refs
#: carrying these tags nor add them via ``tag`` later.
_OWNER_ONLY_LEVELS: frozenset[str] = frozenset({LEVEL_STRATEGIC, LEVEL_TACTICAL})


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
                    "or run from a non-worker source (web:reto / cli)"
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
                    "or run from a non-worker source (web:reto / cli)"
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
            next="run this from a non-worker source (web:reto / cli)",
        )


__all__ = [
    "LEVEL_PROPOSED_TACTICAL",
    "LEVEL_STRATEGIC",
    "LEVEL_SUBTASK",
    "LEVEL_TACTICAL",
    "MAX_DEPTH",
    "check_depth_under",
    "check_level_tags_on_create",
    "check_level_tags_on_tag",
    "check_no_cycle",
    "check_owner_only_ref",
    "check_parent_exists",
    "is_owner",
]
