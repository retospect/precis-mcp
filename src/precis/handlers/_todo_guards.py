"""Write-time guards for the todo tree (Slice 1 of todo-tree-plan.md).

Three orthogonal checks fire on every ``put`` that wires a child under
a parent (and on facet mutations via ``tag(meta=...)``):

* **parent-exists** — ``parent_id`` must point at a live ``todo`` ref.
* **cycle** — the new edge must not create a loop. Cycles in a tree
  with self-referencing FK are not prevented by the DB; the agent
  layer must check.
* **depth** — the ancestor chain may not exceed ``MAX_DEPTH=10``.
  Pathological splitting (Allen's "procrastinating-by-planning"
  failure mode) stops here.
* **level gradient** — ``meta.rotation_root`` and ``meta.worker_mintable
  is False`` (the §M facet-normalized replacement for the old
  ``level:strategic``/``level:tactical`` tags) are owner-only. Workers
  (``asa-chatter``/``asa-worker``/``asa-dreamer`` MCP sources) cannot
  create, edit, or delete these tiers. The authority gradient is the
  most load-bearing control in the design.

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
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.utils.llm.router import PLANNER_MODEL_ALIASES as _PLANNER_ALIASES

if TYPE_CHECKING:
    from precis.store import Store


#: Hard depth cap on the ancestor chain. See knob #3 in
#: ``docs/backlog/todo-tree-plan.md``: dreamer + worker + chatter all
#: push depth, so 10 catches the procrastinating-by-planning failure
#: mode without strangling a legitimate
#: strategic→tactical→section→subsection→paragraph→sentence chain.
MAX_DEPTH = 10


#: §M facet normalization (migration 0102): the old ``level:`` 3-enum
#: (``strategic`` / ``tactical`` / ``subtask``) is now two explicit
#: boolean ``meta`` fields on the ref instead of a tag. ``rotation_root``
#: marks a strategic root (the picks-7d rotation unit, the nursery's
#: orphan-ancestor anchor); ``worker_mintable`` says whether a
#: non-owner source may create/hold this tier — defaults to ``True``
#: when absent (matches the old "omit level:* entirely" = subtask
#: behaviour). The 2×2 space maps the 3 real tiers plus one unused
#: combination:
#:
#:   rotation_root=True,  worker_mintable=False  → strategic (owner-only root)
#:   rotation_root=False, worker_mintable=False  → tactical  (owner-only, non-root)
#:   rotation_root=False, worker_mintable=True   → subtask   (default; worker-mintable)
#:   rotation_root=True,  worker_mintable=True   → (unused)
#:
#: ``level:recurring`` is dropped outright — it was redundant with
#: ``meta.schedule`` (Slice 4); readers now key on schedule presence
#: (``meta ? 'schedule'``) instead of a tag. ``level:proposed-tactical``
#: had no reader behaviour tied to it (a free worker-settable suggestion
#: label, never gated or queried) — it survives as the plain open tag
#: ``proposed-tactical`` (prefix dropped so it falls outside the
#: retired ``level:`` axis).
META_ROTATION_ROOT = "rotation_root"
META_WORKER_MINTABLE = "worker_mintable"
#: The un-gated worker-suggestion tag (replaces ``level:proposed-tactical``
#: — never owner-only, so it needed no facet, just a prefix drop).
PROPOSED_TACTICAL = "proposed-tactical"


# ── auto-run policy fields (closed-vocab values, ``meta``-side) ────


#: Allowed values for ``meta.llm_tier``. §M facet normalization demotes
#: the old ``LLM:<model>`` open tag to this single ``meta`` field — its
#: mere presence flips a todo into the dispatch worker's candidate set
#: (planner-coroutine slice); the value picks the *capability tier* the
#: tick runs on (``local`` = the cluster's served OSS model, the cloud
#: triad = claude). Closed vocab so a typo (``opos``) is rejected at
#: write time rather than producing a silent dispatch miss or a budget
#: burn against a wrong model. Single-sourced from the router's planner
#: alias map so this guard and the web model-picker never drift.
_LLM_TIER_VALUES: frozenset[str] = frozenset(_PLANNER_ALIASES)


#: Allowed values for the ``executor:<runner>`` open tag — runners that
#: are NOT an LLM (deterministic code paths). v1 has none registered;
#: future entries: ``fetch`` (web-search + ingest), ``ingest``
#: (file → corpus), ``calc`` (sympy). Same closed-vocab discipline as
#: ``meta.llm_tier`` so unknown values reject at write time.
_EXECUTOR_TAG_VALUES: frozenset[str] = frozenset()


def _check_namespaced_tag(
    tags: list[str] | None,
    *,
    prefix: str,
    allowed: frozenset[str],
) -> None:
    """Reject ``prefix:<value>`` tags whose value isn't in ``allowed``.

    Used by the ``executor:`` guard (``meta.llm_tier`` moved to
    :func:`check_llm_tier_meta` below, off the tag surface entirely).
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


def check_llm_tier_meta(meta: dict[str, Any] | None) -> None:
    """Reject ``meta.llm_tier`` when it isn't a registered model tier."""
    if not meta or "llm_tier" not in meta:
        return
    value = meta.get("llm_tier")
    if value not in _LLM_TIER_VALUES:
        sorted_allowed = ", ".join(sorted(_LLM_TIER_VALUES))
        raise BadInput(
            f"meta.llm_tier={value!r}: unknown tier; allowed values are "
            f"[{sorted_allowed}]",
            next=(
                f"use one of [{sorted_allowed}] or omit meta.llm_tier "
                "if this work isn't dispatchable"
            ),
        )


#: Allowed keys under ``meta.llm_select`` — an OPTIONAL sibling of
#: ``meta.llm_tier`` (the alias string) carrying a finer-grained
#: (placement, thinking, effort, temperature) selection that threads onto
#: the dispatcher's :class:`~precis.utils.llm.router.LlmRequest`. Every key
#: is optional; absence of the whole dict means "auto / chain-default",
#: same as today.
_LLM_SELECT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"placement", "thinking", "effort", "temperature"}
)
#: ``meta.llm_select.placement`` closed vocab — mirrors
#: :class:`~precis.utils.llm.router.LlmRequest.placement`'s strict
#: local/cloud rung filter.
_LLM_SELECT_PLACEMENT_VALUES: frozenset[str] = frozenset({"local", "cloud"})
#: ``meta.llm_select.effort`` closed vocab — mirrors
#: :func:`~precis.utils.llm.router.reasoning_to_knobs`'s reasoning levels.
_LLM_SELECT_EFFORT_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})


def check_llm_select_meta(meta: dict[str, Any] | None) -> None:
    """Reject ``meta.llm_select`` when it isn't a valid structured selection.

    Unlike ``meta.llm_tier``'s single closed-vocab string, ``llm_select`` is
    a dict of independently-optional knobs — validate the dict shape, reject
    any key outside the allowlist, and validate each present key against its
    own closed vocab / range. A typo'd knob is rejected outright here rather
    than silently dropped, so a caller never believes a write landed that
    didn't (mirrors :func:`check_meta_keys_promotable`'s reject-the-whole-
    call stance).
    """
    if not meta or "llm_select" not in meta:
        return
    value = meta.get("llm_select")
    if not isinstance(value, dict):
        raise BadInput(
            f"meta.llm_select must be a dict, got {type(value).__name__}",
            next="meta={'llm_select': {'placement': 'local', 'effort': 'high'}}",
        )
    extra = set(value) - _LLM_SELECT_ALLOWED_KEYS
    if extra:
        sorted_allowed = ", ".join(sorted(_LLM_SELECT_ALLOWED_KEYS))
        raise BadInput(
            f"meta.llm_select key(s) {sorted(extra)} are unknown; "
            f"allowed keys are [{sorted_allowed}]",
            next=f"use only [{sorted_allowed}] under meta.llm_select",
        )
    if "placement" in value and value["placement"] not in _LLM_SELECT_PLACEMENT_VALUES:
        sorted_allowed = ", ".join(sorted(_LLM_SELECT_PLACEMENT_VALUES))
        raise BadInput(
            f"meta.llm_select.placement={value['placement']!r}: unknown "
            f"placement; allowed values are [{sorted_allowed}]",
            next=(
                f"use one of [{sorted_allowed}] or omit placement for "
                "auto/chain-default"
            ),
        )
    if "thinking" in value and not isinstance(value["thinking"], bool):
        raise BadInput(
            f"meta.llm_select.thinking must be a bool, got {value['thinking']!r}",
            next="meta.llm_select.thinking=True or False",
        )
    if "effort" in value and value["effort"] not in _LLM_SELECT_EFFORT_VALUES:
        sorted_allowed = ", ".join(sorted(_LLM_SELECT_EFFORT_VALUES))
        raise BadInput(
            f"meta.llm_select.effort={value['effort']!r}: unknown effort; "
            f"allowed values are [{sorted_allowed}]",
            next=f"use one of [{sorted_allowed}]",
        )
    if "temperature" in value:
        temperature = value["temperature"]
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise BadInput(
                f"meta.llm_select.temperature must be a number, got {temperature!r}",
                next="meta.llm_select.temperature=<0..2>",
            )
        if not (0 <= temperature <= 2):
            raise BadInput(
                f"meta.llm_select.temperature={temperature!r} out of "
                "range; must be 0 <= t <= 2",
                next="pick a temperature between 0 and 2",
            )


def check_executor_tag(tags: list[str] | None) -> None:
    """Reject ``executor:<value>`` where value is not a registered runner."""
    _check_namespaced_tag(tags, prefix="executor:", allowed=_EXECUTOR_TAG_VALUES)


def has_auto_run_signal(
    tags: list[str] | None,
    meta: dict[str, object] | None,
) -> bool:
    """True when a todo carries something the dispatcher can act on.

    The dispatch worker (``workers/dispatch.py``) only considers a todo
    a candidate if it carries one of three auto-run signals:
    ``meta.llm_tier``, an ``executor:<runner>`` tag, or a legacy
    ``meta.executor`` key. Without any of them the todo is inert — it
    never spawns a ``plan_tick`` job and therefore never gets children.
    Mirror the dispatcher's candidate predicate here so the create-time
    reminder agrees exactly with what would (not) be dispatched.
    """
    for t in tags or []:
        if t.startswith("executor:"):
            return True
    if not meta:
        return False
    return "llm_tier" in meta or "executor" in meta


def strategic_lacks_auto_run(
    meta: dict[str, object] | None,
    tags: list[str] | None = None,
) -> bool:
    """True for a ``meta.rotation_root`` todo with no auto-run signal.

    The reminder condition for the soft create-time hint: a strategic
    planner brief is just inert prose unless it carries an auto-run
    signal, so flag the gap. Non-strategic todos and strategics that
    already carry a signal return ``False`` (no nudge).
    """
    if not meta or meta.get(META_ROTATION_ROOT) is not True:
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


def todo_root_sql(alias: str) -> str:
    """SQL predicate: the ``alias`` row is a todo-tree *root*.

    A root's parent is not a todo: either ``parent_id IS NULL`` (the
    classic shape) or the parent is a ``kind='folder'`` container —
    placement is *where*, never part of the scheduling tree, so a
    strategic sitting in a folder stays a root for rotation / doable /
    picks / review purposes. One shared fragment so the predicate
    cannot drift across the many root-detection queries.

    ``alias`` is a trusted table alias supplied by the caller — never
    user input.
    """
    return (
        f"({alias}.parent_id IS NULL OR EXISTS ("
        f"SELECT 1 FROM refs _pf WHERE _pf.ref_id = {alias}.parent_id "
        f"AND _pf.kind = 'folder'))"
    )


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
            "SELECT kind, retired_at FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None:
        raise NotFound(
            f"parent todo id={parent_id} not found",
            next=("get(kind='todo', id='/recent') to find the parent's actual id"),
        )
    kind, retired_at = row[0], row[1]
    if retired_at is not None:
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


def check_job_parent_exists(
    store: Store, parent_id: int, *, allowed_kinds: frozenset[str]
) -> tuple[int, str]:
    """Resolve a job's ``parent_id`` to a live ref of an allowed kind.

    A job's parent is polymorphic: a ``todo`` (the *intent*
    lane — rotation + the ``child-failed`` bubble) or an artifact such
    as ``structure`` / ``cad`` / ``draft`` (the *compute* lane — an
    idempotent, cache-fillable build step whose owner is the artifact,
    not a task). Both are enforced here; behaviour downstream branches
    on the returned ``kind`` (the bubble targets the requesting todo for
    a compute-lane job, the artifact has no rotation to enter).

    Returns ``(parent_id, kind)`` on success. Raises :class:`NotFound`
    for a missing / soft-deleted parent, :class:`BadInput` for a live
    ref whose kind is outside ``allowed_kinds``.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, retired_at FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None:
        raise NotFound(
            f"job parent id={parent_id} not found",
            next="get(kind='todo', id='/recent') to find the parent's actual id",
        )
    kind, retired_at = row[0], row[1]
    if retired_at is not None:
        raise NotFound(
            f"job parent id={parent_id} was soft-deleted",
            next="pick a live parent todo (or the subject artifact for a derived job)",
        )
    if kind not in allowed_kinds:
        raise BadInput(
            f"parent_id={parent_id} is a {kind!r} ref; a job parents on a "
            f"todo (intent) or a build subject ({', '.join(sorted(k for k in allowed_kinds if k != 'todo'))})",
            next="parent_id must address a todo or the artifact the job builds",
        )
    return parent_id, kind


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
                  FROM refs WHERE ref_id = %s AND retired_at IS NULL
                UNION ALL
                SELECT r.ref_id, s.lvl + 1
                  FROM refs r
                  JOIN sub s ON r.parent_id = s.ref_id
                 WHERE r.retired_at IS NULL
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
    even at the depth cap (10 rows, one index lookup per). The walk
    counts **todo ancestors only**: a folder above the
    strategic root is placement, not tree depth, so folder levels
    never consume the MAX_DEPTH budget.
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
                 WHERE r.kind = 'todo'
            )
            SELECT max(lvl) FROM walk
            """,
            (ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


# ── level / authority ──────────────────────────────────────────────


def _facet_violation(meta: dict[str, Any]) -> str | None:
    """Return the owner-only facet key set by ``meta``, or ``None``.

    A worker source may not set ``rotation_root=True`` (mint a strategic
    root), ``worker_mintable=False`` (mint a tactical — the owner-only,
    non-root tier), or ``schedule`` (mint scheduled/recurring work —
    the old ``level:recurring`` gate, now keyed on ``meta.schedule``
    presence rather than a tag). Absence of a key, or a value that
    matches the worker-mintable default, is never a violation.
    """
    if meta.get(META_ROTATION_ROOT) is True:
        return META_ROTATION_ROOT
    if meta.get(META_WORKER_MINTABLE) is False:
        return META_WORKER_MINTABLE
    if "schedule" in meta:
        return "schedule"
    return None


def check_facets_on_create(meta: dict[str, Any] | None) -> None:
    """Reject an owner-only facet from worker sources at create.

    The plan calls this the single most load-bearing control: workers
    physically cannot mint strategic or tactical refs, or scheduled
    (recurring) work. ``proposed-tactical`` (the plain open tag) stays
    open to everyone so workers / dreamers can suggest promotions for
    owner triage.
    """
    if not meta:
        return
    if is_owner():
        return
    violation = _facet_violation(meta)
    if violation is not None:
        raise BadInput(
            f"meta.{violation!r} is owner-only; the current source has "
            "worker authority",
            next=(
                f"propose via tag='{PROPOSED_TACTICAL}' instead, "
                "or run from a non-worker source (web:owner / cli)"
            ),
        )


#: The facet keys the level/schedule authority gradient governs.
#: ``tag(meta=...)`` touching any of these from a worker source is
#: rejected outright, in *either* direction — mirrors the old
#: tag-gradient guard, which rejected both ``add`` AND ``remove`` of an
#: owner-only level tag from a worker (a worker could neither promote a
#: subtask to strategic nor demote a strategic by yanking the tag).
_TAG_TIME_OWNER_ONLY_KEYS: frozenset[str] = frozenset(
    {META_ROTATION_ROOT, META_WORKER_MINTABLE, "schedule"}
)


def check_facets_on_tag(meta: dict[str, Any] | None) -> None:
    """Reject any facet-gradient mutation from worker sources at ``tag``.

    ``tag(meta=...)`` is the promotion surface (§M facet normalization
    — meta fields replaced the old level tag; ``tag()`` already carries
    non-tag state via ``prio=``, so this follows the same precedent).
    Unlike :func:`check_facets_on_create` (which only blocks the
    *owner-tier* value at mint time), this blocks a worker from
    touching ``rotation_root`` / ``worker_mintable`` / ``schedule`` at
    all post-creation — promoting AND demoting are both owner-only. The
    ``proposed-tactical`` open tag stays freely mutable for anyone via
    ``add=``/``remove=``.
    """
    if not meta or is_owner():
        return
    touched = set(meta) & _TAG_TIME_OWNER_ONLY_KEYS
    if touched:
        key = sorted(touched)[0]
        raise BadInput(
            f"meta.{key!r} is owner-only; the current source has worker authority",
            next=(
                f"propose via tag='{PROPOSED_TACTICAL}' instead, "
                "or run from a non-worker source (web:owner / cli)"
            ),
        )


#: The closed set of keys ``tag(meta=...)`` is allowed to promote.
#: ``tag()`` is a post-creation *mutation* surface, not a general meta
#: bag — anything not on this list (notably ``deliver``, the cron-folded-into-recurring
#: push-notification target, and ``workspace``, the sandbox root) must
#: go through ``put()``/``create()``, which run their own validation.
#: Reject the whole call rather than silently dropping an unpromotable
#: key, so a caller never believes a write landed that didn't.
TAG_META_ALLOWED_KEYS: frozenset[str] = frozenset(
    {META_ROTATION_ROOT, META_WORKER_MINTABLE, "schedule", "llm_tier", "llm_select"}
)


def check_meta_keys_promotable(meta: dict[str, Any] | None) -> None:
    """Reject any ``tag(meta=...)`` key outside :data:`TAG_META_ALLOWED_KEYS`."""
    if not meta:
        return
    extra = set(meta) - TAG_META_ALLOWED_KEYS
    if extra:
        sorted_allowed = ", ".join(sorted(TAG_META_ALLOWED_KEYS))
        raise BadInput(
            f"meta key(s) {sorted(extra)} cannot be set via tag(); "
            f"the promotable allowlist is [{sorted_allowed}]",
            next=(
                "use put() to set other meta fields (e.g. deliver, "
                "workspace) at create time or via a full meta rewrite"
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
               AND c.retired_at IS NULL
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
            SELECT count(*) FILTER (WHERE c.kind = 'todo' AND c.retired_at IS NULL
                                     AND COALESCE(
                                       (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                                         WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                                       'open'
                                     ) NOT IN ('done', 'won''t-do')) AS open_kids,
                   count(*) FILTER (WHERE c.kind = 'todo' AND c.retired_at IS NULL) AS total_kids
              FROM refs c WHERE c.parent_id = %s
            """,
            (ref_id,),
        ).fetchone()
        assert cur is not None
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
                   AND cit.retired_at IS NULL
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
                   AND r.retired_at IS NULL
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
            "OR mint subtasks via put(kind='todo', meta={'llm_tier': '<model>'}, ...) "
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
        row = conn.execute(
            """
            SELECT COALESCE((meta->>%s)::boolean, false) AS rotation_root,
                   COALESCE((meta->>%s)::boolean, true) AS worker_mintable
              FROM refs WHERE ref_id = %s
            """,
            (META_ROTATION_ROOT, META_WORKER_MINTABLE, ref_id),
        ).fetchone()
    if row is None:
        return
    rotation_root, worker_mintable = bool(row[0]), bool(row[1])
    if rotation_root or not worker_mintable:
        tier = "strategic" if rotation_root else "tactical"
        raise BadInput(
            f"todo id={ref_id} is {tier!r} (owner-only)",
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


# ── deliver (cron-folded-into-recurring — folded from kind='cron') ───────────────────


def check_deliver_in_meta(meta: dict[str, object] | None) -> dict[str, str] | None:
    """Validate ``meta.deliver`` if present; return the canonical block.

    ``meta.deliver = {'target': 'conv:discord/<g>/<c>/<t>'}`` marks a
    recurring todo (``meta.schedule`` set — or one of its ticks) for
    **push** delivery —
    a synthetic prompt fired at asa_bot via ``pg_notify('precis.cron', ...)``
    — instead of (or as well as, for a folder-level automation) minting a
    subtask into the doable queue. This is the delivery-address field the
    retired ``kind='cron'`` push mechanism was folded onto.

    Returns ``None`` when no deliver target is set. Raises
    :class:`BadInput` on a malformed shape.
    """
    if not meta:
        return None
    spec = meta.get("deliver")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise BadInput(
            f"meta.deliver must be a dict, got {type(spec).__name__}",
            next="meta={'deliver': {'target': 'conv:discord/<g>/<c>/<t>'}}",
        )
    extra = set(spec) - {"target"}
    if extra:
        raise BadInput(
            f"unknown meta.deliver keys: {sorted(extra)}",
            options=["target"],
        )
    target = spec.get("target")
    if not isinstance(target, str) or not target.strip():
        raise BadInput(
            "meta.deliver.target is required (where to push the synthetic prompt)",
            next="meta={'deliver': {'target': 'conv:discord/<g>/<c>/<t>'}}",
        )
    return {"target": target.strip()}


__all__ = [
    "MAX_DEPTH",
    "META_ROTATION_ROOT",
    "META_WORKER_MINTABLE",
    "PROPOSED_TACTICAL",
    "TAG_META_ALLOWED_KEYS",
    "check_deliver_in_meta",
    "check_depth_under",
    "check_executor_tag",
    "check_facets_on_create",
    "check_facets_on_tag",
    "check_halt_remove",
    "check_llm_select_meta",
    "check_llm_tier_meta",
    "check_meta_keys_promotable",
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
    "todo_root_sql",
]
