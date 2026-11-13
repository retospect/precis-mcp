"""TodoHandler — task / action items with status transitions and tree shape.

Subsystem architecture (facet model, job lanes, planner coroutines,
guardrails): :mod:`precis.handlers` package docstring.

Numeric-id ref kind. Builds on the shared :class:`NumericRefHandler`
shape with four first-class extensions:

1. **`STATUS:` closed-prefix tag** (v1 lifecycle, unchanged):
   ``open|doing|blocked|done|won't-do|paused|auto-timeout``.
   Every put-create starts at ``STATUS:open``.

2. **Hierarchical tree** (Slice 1 of the closed todo-tree-plan spec):
   each todo carries an optional ``parent_id`` pointing at another
   todo. Branches are outcomes (the first line reads as "what does
   done look like"); leaves are next physical actions. The owner owns the
   top tiers via the ``meta.rotation_root`` / ``meta.worker_mintable``
   facet gradient (§M facet normalization, migration 0102 — replaces the
   old ``level:strategic|tactical`` tags); the guards in
   :mod:`precis.todo.guards` enforce who can write what.

3. **Views** — search views ``roots``, ``projects``, ``strategic``,
   ``doable``, ``waiting``, ``blocked``, ``ask-user``, ``attention``,
   plus the flat status lists ``active`` (the open+doing+blocked+
   paused+auto-timeout union), ``doing``, ``done`` (the
   :class:`TodoView` closed vocabulary; the :data:`_TREE_SEARCH_VIEWS`
   dispatch table maps each to a renderer in
   :mod:`precis.todo.views`), plus ``view='tree'`` / ``'comments'`` /
   ``'body'`` / ``'scratch'`` on ``get``.
   ``projects`` lists strategic roots that own a ``meta.workspace``;
   ``attention`` unions ``ask-user:`` leaves, ``child-failed`` parents,
   and halted todos.

4. **PRIO column + recurring schedule** (Slice 4): ``prio`` is a
   small int (1..10) on ``refs`` driving the doable ORDER BY;
   a recurring root is any todo with ``meta.schedule`` set — owner-only
   to create, spawned children are ordinary worker-mintable subtasks.
   ``meta.schedule`` gets canonicalised (every-shorthand → cron) and
   validated at write time. The seeded Watches umbrella is the default
   parent for recurring roots without an explicit ``parent_id``.

Views via ``view='<name>'`` on search / get (the canonical surface):
    search(kind='todo', view='roots'|'projects'|'strategic'|'doable'
                              |'waiting'|'blocked'|'ask-user'|'attention'
                              |'active'|'doing'|'done'|'work'|'asks'|'reto-queue')
    get(kind='todo', id=N, view='tree'|'comments'|'body'|'scratch')

``view='comments'`` is selectable: ``args={'comment': K}`` reads one
comment, ``args={'comments': K}`` the newest K, ``args={'comments':
'A-B'`` an ord window, ``args={'last': K}`` the newest K; on a bare get
the same selectors read as ``view='comments'`` (gr567276).

``view='work'`` (dark-factory step 1) is the cross-kind hierarchy: quest →
the roots that ``serves`` it → subtrees, with lease and fleet agents per
row. The eye (``extent='fisheye+1hop'``) on a todo appends the tree ring
— parent, peers, children off ``parent_id`` — since the link
neighbourhood never sees that column (gr541348).

``view='asks'`` (dark-factory step 1b) is everything that waits on the
human in three classes (showstopper / decision / discussion), grouped by
quest then thread — ``ask-user`` todos, ``waiting-for:reto``, gripes
tagged ``needs:ruling`` or ``discussion`` (:mod:`precis.todo.asks_view`);
the web ``/asks`` shows the same. A row tagged ``for-reto:<ruling|spec|
review|other>`` waits on Reto specifically; ``view='reto-queue'`` lists
those and the asks flat, most blocked first, then oldest (gr648249). A question for the owner is a todo
tagged ``ask-user`` with ``meta.ask`` (``kind``, ``options``, ``blocks``,
``context``; validated on put and edit, ``blocks='td<id>'`` writes a
blocked-by link from that slice and makes the row a showstopper); answer
it with a comment ``answer: …`` and ``tag`` ``STATUS:done`` + remove
``ask-user``.

``view='scratch'`` (gr605672) is the task-scratch index: one line per
live ``SPACE:scratch`` memory the todo owns through ``scratch-for:td<N>``
— the single read the closing agent makes before posting ``harvest:``
on the todo (precis-gripe-loop-help's close step). The renderer lives
with the other get views in :mod:`precis.todo.views` too
(:func:`precis.todo.views.render_scratch`).
``id='/<view>'`` paths (/open /queue /doing /blocked /done) are
deprecated aliases — same render, plus a pointer at the ``view=``
spelling ('/open' and '/queue' land on ``view='active'``).

A ``get(kind='todo', id=N)`` response always includes the walk-up
ancestry chain when the ref isn't a root — depth ≤ 10, cheap, no
caching needed.
"""

from __future__ import annotations

import dataclasses
import difflib
import time
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, cast

from precis.errors import BadInput, Gone, NotFound, Unsupported
from precis.handlers import _comment_timeline as tl
from precis.handlers._eye import is_eye_view
from precis.handlers._interpret import interpreted
from precis.handlers._mode_help import require_mode
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.handlers._prio_tag import (
    prio_display,
    prio_tag_int,
    split_prio,
    validate_prio,
)
from precis.handlers._request_state import RequestState
from precis.handlers._tag_redirect import redirect_long_tag_values
from precis.handlers.plaintext import _require_find_and_text
from precis.plans import logbook as plan_log
from precis.plans import nodes as plan_nodes
from precis.plans import templates as plan_templates
from precis.protocol import ArgsSpec, KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.store.types import TODO_BODY_KIND, ChunkInsert
from precis.todo import (
    TERMINAL_TODO_STATUSES,
    asks_view,
    guards,
    views,
    work_view,
)
from precis.utils import handle_registry
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.match_all import fold_match_all_q
from precis.utils.next_block import render_next_section
from precis.utils.secret_scan import mask_secrets

if TYPE_CHECKING:
    from precis.store.protocols import PoolStore
    from precis.store.store import Store

#: Chunk kind holding a todo's optional details body (parallel to
#: ``memory_body``, migration 0050). The title stays in ``refs.title``
#: (a good header already); the body is extra prose read on ``get`` and
#: embedded + keyworded for free. Additive — most todos never set one.
_BODY_KIND = TODO_BODY_KIND

#: ``edit(mode='replace', text=...)`` preserves the old title as the
#: details body when it ran past ~120 chars — the length at which a
#: title stopped being a header and started carrying the details a
#: ``todo_body`` would hold (gr515178, td508297).
_PRESERVE_TITLE_CHARS = 120


def _title_holds_details(title: str) -> bool:
    """True when a title is doing body work: over
    :data:`_PRESERVE_TITLE_CHARS`, or more than one sentence (a
    ``. ``/``! ``/``? `` boundary). A wholesale retitle of one would
    lose the details, so the replace path moves it into the body
    first (gr515178)."""
    if len(title) > _PRESERVE_TITLE_CHARS:
        return True
    return any(
        ch in ".!?" and title[i + 1].isspace() for i, ch in enumerate(title[:-1])
    )


def _check_plan_meta(meta: dict[str, Any] | None, *, only: str | None = None) -> None:
    """Refuse a malformed plan node key (``meta.op`` / ``gate`` / ``loop`` /
    ``generate`` / ``auto_close``, td542092) with a ``next=`` naming the
    key and the accepted shape. ``only`` keeps one problem family (the
    ``'gate/loop/generate'`` at-most-one rule) for the edit path's merged
    re-check, which must not re-judge keys it is not changing."""
    problems = [
        (key, msg)
        for key, msg in plan_nodes.plan_problems(meta)
        if only is None or key == only
    ]
    if not problems:
        return
    keys = list(dict.fromkeys(k for k, _ in problems))
    shapes = [
        f"meta.{k}: {plan_nodes.PLAN_SHAPES[k]}"
        for key in keys
        for k in key.split("/")
        if k in plan_nodes.PLAN_SHAPES
    ]
    raise BadInput(
        "; ".join(msg for _, msg in problems),
        next="accepted shapes — " + " | ".join(dict.fromkeys(shapes)),
    )


@dataclasses.dataclass(frozen=True)
class _PutState:
    """What one ``put`` hands ``_create`` / ``_render_create_ack``.

    The base ``put`` calls those hooks with a fixed argument set, so the
    todo-only fields ride a request-scoped :class:`RequestState` — never
    the shared handler instance (concurrent calls would swap them).
    """

    parent_id: int | None = None
    meta: dict[str, Any] | None = None
    prio: int | None = None
    body: str | None = None
    #: True when this call's parented-write default stamped
    #: ``meta.llm_tier='opus'`` (auto-dispatch armed) — the create ack
    #: announces it so the arming is never silent.
    stamped_default_tier: bool = False
    #: True when the caller passed ``manual=True`` on a parented create and
    #: that flag — not a caller-chosen tier/executor/schedule — is what
    #: kept the leaf inert (gr660003): gated on the would-stamp predicate
    #: plus ``guards.has_auto_run_signal`` so the "will not auto-run" ack
    #: is never a false receipt on a leaf the caller armed themselves
    #: (td660679). The ack says so, since an unstamped parented leaf is
    #: otherwise easy to mistake for a forgotten auto-run signal.
    manual_no_stamp: bool = False
    #: ``waiting-for:mint`` stamped because the put landed inside a live
    #: plan subtree — unconditional, no session-signal inference
    #: (td603429); the ack says so, since the hold keeps the child out of
    #: dispatch until released.
    mint_hold: bool = False


_PUT_STATE: RequestState[_PutState] = RequestState("precis_todo_put", _PutState())
#: Chunk kind of an appended comment (migration 0195; the gripe_comment shape).
_COMMENT_KIND = "todo_comment"

#: ``get(kind='todo', id=N, args={'wait': <seconds>})``: poll cadence and
#: the per-call cap of the one-shot wait a submitting session blocks on
#: until the node reaches a terminal STATUS (gr570506 — the factory is
#: pull-only; the wait IS the submitter's notification channel).
_WAIT_POLL_S = 5.0
_WAIT_CAP_S = 3600.0


def _mint_held(tags: list[str] | None) -> bool:
    """True when ``tags`` already holds a fresh mint out of dispatch —
    a doable-exclusion tag (``waiting-for:`` / ``halt`` / ``ask-user`` /
    a ``claimed-by:`` lease) or a non-``open`` ``STATUS:`` — so the
    held-at-creation stamp doesn't double up."""
    for t in tags or []:
        if t.startswith(("waiting-for:", "halt:", "ask-user:", "claimed-by:")) or t in (
            "halt",
            "ask-user",
        ):
            return True
        if t.startswith("STATUS:") and t != "STATUS:open":
            return True
    return False


class TodoView(StrEnum):
    """Closed vocabulary for ``search(kind='todo', view=...)``.

    Named constants rather than bare string literals: the members are
    the canonical view names, importable from tests / CLI / skill-doc
    generation instead of being retyped as magic strings. ``StrEnum``
    members ARE strings, so a member compares equal to the wire value
    that arrives over MCP and formats as the plain name in error
    messages — no ``.value`` noise at the boundary.
    """

    ROOTS = "roots"
    PROJECTS = "projects"
    STRATEGIC = "strategic"
    DOABLE = "doable"
    WAITING = "waiting"
    BLOCKED = "blocked"
    ASK_USER = "ask-user"
    ATTENTION = "attention"
    # Flat status lists (todo-surface-naming, approved 2026-09-06):
    # converged here from the id='/<view>' path overload, which stays
    # as a deprecated alias. ``active`` is the open+doing+blocked+
    # paused+auto-timeout union the '/open' path used to render —
    # renamed because "open" already means both a STATUS value and the
    # OPEN tag namespace.
    ACTIVE = "active"
    DOING = "doing"
    DONE = "done"
    # gr489581: a flat listing — every live todo with id / status /
    # parent columns, no tree roll-up. 'index' is the name the other
    # kinds use; both spellings land here.
    FLAT = "flat"
    INDEX = "index"
    # Dark-factory step 1 (td540840): quest → serving roots → subtrees,
    # with lease and fleet agents per row; the one read every later
    # factory step makes (:mod:`precis.todo.work_view`).
    WORK = "work"
    # Dark-factory step 1b (td540847): the asks list in three classes,
    # grouped by quest (:mod:`precis.todo.asks_view`).
    ASKS = "asks"
    # gr648249: everything waiting on Reto, most blocked first.
    RETO_QUEUE = "reto-queue"
    # td574946 S1: the rescue queue — every live node carrying an open
    # ``rescue:<class>`` tag, grouped by class.
    RESCUE = "rescue"


def _doable_under_ids(store: Store, raw: Any) -> list[int]:
    """Resolve ``view='doable'``'s ``under=`` to the todo roots whose
    subtrees the leaf walk unions.

    An integer (or ``td<N>``/``todo:N``) scopes one subtree as before.
    A memory handle (``me<N>``/``memory:N``) scopes the union of the
    ``plan-of`` todos hanging off it and its ``part-of`` spec roots —
    the "next doable slice in programme X" question (gr568264): with
    specs in memory, one ``get(kind='memory')`` plus this search
    answers it without the caller knowing a todo-root handle.
    """
    try:
        return [int(raw)]
    except (TypeError, ValueError):
        pass
    from precis.utils.link_target import parse_link_target

    try:
        target = parse_link_target(str(raw), store=store)
    except (BadInput, NotFound) as exc:
        raise BadInput(
            f"under must be a todo id or a memory handle, got {raw!r}",
            next="search(kind='todo', view='doable', "
            "args={'under': 'me<spec-or-programme root>'})",
        ) from exc
    if target.kind == "todo":
        return [target.ref_id]
    if target.kind != "memory":
        raise BadInput(
            f"under={raw!r} is a {target.kind} ref — a todo id or a "
            "memory handle is expected",
            next="search(kind='todo', view='doable', "
            "args={'under': 'me<spec-or-programme root>'})",
        )
    members = {target.ref_id, *store.part_of_descendants(target.ref_id)}
    roots: set[int] = set()
    for member in members:
        for edge in store.links_for(member, direction="in", relation="plan-of"):
            roots.add(edge.dst_ref_id if edge.src_ref_id == member else edge.src_ref_id)
    live = (
        store.list_refs(kind="todo", include_ref_ids=sorted(roots), limit=len(roots))
        if roots
        else []
    )
    if not live:
        raise BadInput(
            f"memory {str(raw)!r} has no plan-of todos — nothing to scope to",
            next=(
                f"link(kind='todo', id=<the plan todo>, target={str(raw)!r}, "
                "rel='plan-of') — then retry"
            ),
        )
    return [r.id for r in live]


def _view_doable(
    store: Store,
    args: dict[str, Any] | None,
    page_size: int,
    *,
    tags: list[str] | None = None,
) -> Response:
    """``view='doable'`` — the one tree view that reads ``args`` / paging.

    Pulled out of the dispatch table so the table stays a flat
    ``name → renderer`` map; the arg-less views are one-line lambdas.
    ``tags=`` filters the leaf set through the shared tag filter
    (gr568264): positive entries AND, ``'!<tag>'`` entries exclude —
    same grammar as every other ``search`` tag filter.
    """
    under = None
    if args is not None and "under" in args:
        under = _doable_under_ids(store, args["under"])
    normalized_tags = Tag.normalize_filter(
        tags,
        kind="todo",
        axis_in_use=lambda prefix: prefix in store.tag_namespaces(),
    )
    return views.render_doable(
        store, under=under, limit=page_size or 20, tags=normalized_tags
    )


#: Dispatch table: view → renderer ``(store, args, page_size) -> Response``.
#: Arg-less renderers ignore the trailing two params so the dispatch
#: stays branch-free. The keys are :class:`TodoView` members; ``search``
#: resolves the wire string to the enum and looks it up here. The
#: totality assertion below makes a half-wired view an import-time error
#: rather than a runtime "unknown view".
_TREE_SEARCH_VIEWS: dict[
    TodoView, Callable[[Any, dict[str, Any] | None, int], Response]
] = {
    TodoView.ROOTS: lambda store, args, ps: views.render_roots(store),
    TodoView.PROJECTS: lambda store, args, ps: views.render_projects(store),
    TodoView.STRATEGIC: lambda store, args, ps: views.render_strategic(store),
    TodoView.DOABLE: _view_doable,
    TodoView.WAITING: lambda store, args, ps: views.render_waiting(store),
    TodoView.BLOCKED: lambda store, args, ps: views.render_blocked(store),
    TodoView.ASK_USER: lambda store, args, ps: views.render_ask_user(store),
    TodoView.ATTENTION: lambda store, args, ps: views.render_attention(store),
    TodoView.ACTIVE: lambda store, args, ps: views.render_status_flat(
        store, statuses=views.ACTIVE_STATUSES, label="active", limit=ps or 200
    ),
    TodoView.DOING: lambda store, args, ps: views.render_status_flat(
        store, statuses=frozenset({"doing"}), label="doing", limit=ps or 200
    ),
    TodoView.DONE: lambda store, args, ps: views.render_status_flat(
        store, statuses=frozenset({"done"}), label="done", limit=ps or 200
    ),
    TodoView.FLAT: lambda store, args, ps: views.render_flat(store, limit=ps or 200),
    TodoView.INDEX: lambda store, args, ps: views.render_flat(store, limit=ps or 200),
    TodoView.WORK: lambda store, args, ps: work_view.render_work(store),
    TodoView.ASKS: lambda store, args, ps: asks_view.render_asks(store),
    TodoView.RETO_QUEUE: lambda store, args, ps: asks_view.render_reto_queue(store),
    TodoView.RESCUE: lambda store, args, ps: views.render_rescue(store),
}

#: Single-source-of-truth guard: every view in the vocabulary must have
#: a renderer. Drift (a member added to the enum but not wired here, or
#: vice-versa) fails at import, not when a user first hits the view.
assert set(_TREE_SEARCH_VIEWS) == set(TodoView), (
    "TodoView members and _TREE_SEARCH_VIEWS keys diverged: "
    f"{set(TodoView) ^ set(_TREE_SEARCH_VIEWS)}"
)

#: ``id='/<path>'`` list spellings that ``get`` still accepts but that
#: are NOT :class:`TodoView` members — so ``search(view='open')`` /
#: ``search(view='queue')`` hit the unknown-view refusal. When one of
#: these arrives, the error names both the working path spelling and
#: the canonical search view instead of only listing the vocabulary
#: (gr513891 — a builder guessed ``search(view='queue')`` and got no
#: pointer at ``id='/queue'`` or ``view='active'``). ``doing`` / ``done``
#: / ``blocked`` are excluded: they ARE TodoView members, so the search
#: view works directly and no hint is needed.
_SEARCH_PATH_SPELLING_HINTS: dict[str, str] = {
    "open": (
        "search(kind='todo', view='active') — or the deprecated path "
        "spelling get(kind='todo', id='/open')"
    ),
    "queue": (
        "search(kind='todo', view='active') — or the deprecated path "
        "spelling get(kind='todo', id='/queue')"
    ),
    "recent": "get(kind='todo', id='/recent')",
}

#: View names that ``get(kind='todo', id=N, view=...)`` accepts on
#: top of the base class's ``links`` / ``log`` views.
_TREE_GET_VIEWS: frozenset[str] = frozenset({"tree"})

#: ``get`` view spellings with one reading on a todo (gr511440 — the
#: prod ledger showed ``get(kind='todo', view='full')`` refusals; the same
#: family already folds on job/memory/gripe). ``'full'`` is the default
#: card — a todo's plain get already is the complete read (title +
#: body + comments + links; ``view='body'`` is the bounded body-only
#: read, gr554136). ``'brief'`` and ``'tags'`` (gr547804) read as the
#: same card — a todo card is the brief and carries its ``tags:``
#: line. ``None`` maps to the default view; each alias runs the
#: canonical call and names the reading via
#: :func:`interpreted` (gr494831's shape).
_TODO_GET_VIEW_ALIASES: dict[str, str | None] = {
    "full": None,
    "brief": None,
    "tags": None,
}

#: Reserved *virtual* link relation. ``link(rel='parent')`` is a
#: façade over the ``refs.parent_id`` column — it re-points the tree
#: edge (running the cycle/depth/owner guards) instead of inserting a
#: ``links`` row, and is synthesized on read in the links view. It is
#: deliberately NOT in the closed ``Relation`` vocabulary so it never
#: leaks into ``link`` for kinds where "parent" is meaningless.
_RESERVED_PARENT_REL = "parent"


def _inherit_workspace_from_parent(
    store: PoolStore, parent_id: int
) -> dict[str, Any] | None:
    """Pull ``meta.workspace`` from the parent ref, or None if unset.

    Returns a plain dict so the caller can splice it into the child's
    meta. The shape isn't validated here — :class:`Workspace.from_meta`
    handles malformed entries downstream (logs + treats as missing).

    Cascade pattern: strategic root owns the workspace block; every
    descendant inherits it on put unless the put explicitly overrides
    ``meta.workspace``. No-op when the parent has no workspace set
    (most legacy todos).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'workspace' FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    ws = row[0]
    if not isinstance(ws, dict):
        return None
    return ws


#: Plan node kinds a parented put must not stamp ``meta.llm_tier='opus'`` on:
#: the plan dispatcher tick drives them (a gate is only evaluated), so a
#: ``plan_tick`` would be wrong. A task node (``meta.op``) is skipped at the
#: call site; a plain todo keeps the parented default.
_PLAN_KINDS_NOT_DISPATCHED: frozenset[str | None] = frozenset(
    {"gate", "loop", "generator", "phase"}
)

#: Shared range check (moved to ``_prio_tag`` so gripe / quest validate
#: their ``prio=`` kwarg identically).
_validate_prio = validate_prio


class TodoHandler(NumericRefHandler):
    owns_under = True
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with the verb bodies.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "search": ArgsSpec({"under"}),
        "delete": ArgsSpec(tl.DELETE_ARGS_KEYS),
    }
    #: ``put(id=N, text=...)`` appends a comment (the unknown-args= refusal
    #: points at it, gr478557).
    supports_comment: ClassVar[bool] = True

    #: Agent-authored assertions (graph-node-trust-state slice 1).
    trust_origin: ClassVar[str | None] = "generated"
    spec: ClassVar[KindSpec] = KindSpec(
        kind="todo",
        title="Todo",
        description=(
            "Task / action item. Numeric id assigned on create. "
            "Status tracked via STATUS:open|doing|blocked|done|won't-do|paused|"
            "auto-timeout. Optional parent_id wires the todo into the "
            "hierarchical todo tree (see precis-todo-tree-help); a parented "
            "create stamps meta.llm_tier='opus' by default so the child "
            "auto-runs — pass manual=True to skip the stamp on a hand-written "
            "item (a backlog/review note) that must stay inert."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        # In-place text rewrite via edit(mode='replace', text='...'):
        # same id, parent, links, and tags survive; old body audited in
        # ref_events (view='log'). Owner-only on strategic / tactical.
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
        placement="artifact",
        # 'replace' rewrites the title and/or details body; 'find-replace' /
        # 'insert' are anchored edits of the details body (gr478556). The
        # top-level tools/core.py::edit signature defaults mode= to
        # 'find-replace' (the file-kind default), so a caller that omits
        # mode= still arrives here with it; require_mode() reads this
        # declaration (gr292913). 'gate' (gr597921) fills a pending
        # sha-in-prod gate's args.sha — the lander's post-land write for a
        # slice landed by hand.
        edit_modes=("replace", "find-replace", "insert", "delete", "gate"),
    )

    kind: ClassVar[str] = "todo"
    sense: ClassVar[str] = "todo"
    #: get/tag/link take ``id=[...]`` (summary get, one-transaction tag/fan-in link).
    batch_ids: ClassVar[bool] = True
    #: ``edit(id=[...])`` runs the handler's own one-transaction batch.
    edit_id_list: ClassVar[str] = "native"
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:open",)

    #: An overflowing queryless list names its ``project:`` groups
    #: (gr639965) — the cross-cutting workspace tag every ref minted
    #: under a project carries (auto-injected in ``_create`` below).
    facet_axis: ClassVar[str | None] = "project"

    # Mentions in the title/body (incl. bare C-numbers → part refs,
    # gr520243) materialise as ``related-to`` links on write — same
    # autolinker memory uses. ``_create``/``edit``/``_edit_anchored``
    # below call ``_sync_mention_links`` over title + details body.
    autolink_mentions: ClassVar[bool] = True

    # ── list view dispatch (id='/<view>') ─────────────────────────
    # ("open work" union: views.ACTIVE_STATUSES — the view='active' set.)

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "open", "doing", "blocked", "done", "queue")

    def _search_view_names(self) -> frozenset[str]:
        # The tree-aware views live on search(), not get(); passing one to
        # get(view=…) without an id gets a redirect hint (gr48523). Derived
        # from the dispatch table so it stays in sync with TodoView.
        return frozenset(str(v) for v in _TREE_SEARCH_VIEWS)

    def _list_view(self, view: str) -> Response | None:
        # Default behaviour for /recent / "" stays in the base class.
        # The flat status paths are deprecated aliases of the search
        # views (todo-surface-naming, approved 2026-09-06): same render,
        # plus a one-line pointer at the canonical spelling. '/blocked'
        # now aliases the richer tree-aware blocked view (blocked-by
        # links + STATUS:blocked union) rather than the bare flat list.
        if view in ("open", "queue"):
            return self._deprecated_path_alias(view, TodoView.ACTIVE)
        if view == "doing":
            return self._deprecated_path_alias(view, TodoView.DOING)
        if view == "done":
            return self._deprecated_path_alias(view, TodoView.DONE)
        if view == "blocked":
            return self._deprecated_path_alias(view, TodoView.BLOCKED)
        return super()._list_view(view)

    def _deprecated_path_alias(self, path: str, target: TodoView) -> Response:
        resp = _TREE_SEARCH_VIEWS[target](self.store, None, 200)
        hint = (
            f"\n\n⚠ id='/{path}' is a deprecated alias — use "
            f"search(kind='todo', view='{target}')"
        )
        return dataclasses.replace(resp, body=resp.body + hint)

    # ── get: single-ref ancestry + tree view ──────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        # ``args={'comment': K}`` / ``{'comments': K|'A-B'}`` /
        # ``{'last': K}`` — the timeline selectors inside
        # ``view='comments'``; on a bare get they read as
        # ``view='comments'`` (gr567276 — same contract as gripe).
        comment: int | str | None = None,
        comments: int | str | None = None,
        last: int | None = None,
        # ``args={'wait': <seconds>}`` — the one-shot wait: block until
        # the todo's STATUS turns terminal, then render the card (the
        # factory is pull-only, so a submitting session's notification
        # is this wait, gr570506).
        wait: int | float | str | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        if isinstance(id, list):
            if comment is not None or comments is not None or last is not None:
                raise BadInput(
                    "args={'comment'|'comments'|'last'} select inside one "
                    "todo's timeline — pass a single id=",
                    next="get(kind='todo', id=<N>, view='comments', "
                    "args={'comments': 5})",
                )
            if wait is not None:
                raise BadInput(
                    "args={'wait': <seconds>} waits on one todo — pass a "
                    "single numeric id=",
                    next="get(kind='todo', id=<N>, args={'wait': 900})",
                )
            return super().get(id=id, view=view, q=q, **_kw)
        readings: list[str] = []
        single = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if wait is not None:
            if not single:
                raise BadInput(
                    "args={'wait': <seconds>} waits on one todo — pass a "
                    "single numeric id=",
                    next="get(kind='todo', id=<N>, args={'wait': 900})",
                )
            readings += self._wait_terminal(id, wait)
        if view in _TODO_GET_VIEW_ALIASES and single and q is None:
            canon = _TODO_GET_VIEW_ALIASES[view]
            shown = f"view={canon!r}" if canon is not None else "the default view"
            readings.append(f"view={view!r} read as {shown}")
            view = canon
        if comment is not None or comments is not None or last is not None:
            # The selectors only mean something inside the timeline
            # read — anywhere else they would be a silent drop
            # (mcp-authoring rule 6). On a *bare* get they have exactly
            # one reading — view='comments' — so run it and name the
            # reading (the interpreted convention gr494831; prod kept
            # re-issuing the refused call, gr567276).
            if view is None:
                view = "comments"
                readings.append(
                    "args={'comment': K} / {'comments': K} / {'last': K} "
                    "on a bare get read as view='comments'"
                )
            elif view != "comments":
                raise BadInput(
                    "args={'comment': K} / {'comments': K} / {'last': K} "
                    "select inside view='comments'",
                    next=(
                        f"get(kind='todo', id={id}, view='comments', "
                        "args={'comments': 5})"
                    ),
                )
        # Tree view is special: needs a numeric id, dispatches before
        # the base class can complain about unknown views.
        if view == "tree":
            if id is None:
                raise BadInput(
                    "view='tree' requires id=",
                    next="get(kind='todo', id=N, view='tree')",
                )
            ref_id = self._coerce_id(id)
            return views.render_tree(self.store, ref_id)
        if view == "comments":
            ref = self._resolve_live_ref(self._coerce_id(id))
            return interpreted(
                Response(
                    body=self._render_comments_view(
                        ref, comment=comment, comments=comments, last=last
                    )
                ),
                readings,
            )
        if view == "body":
            # gr554136: the bounded body-only read — title + the
            # untruncated todo_body, no comments/links/ancestry — the
            # call a cut or fitted marker names when the whole card
            # can't fit (the bare get that produced the cut was no
            # route to the rest).
            ref = self._resolve_live_ref(self._coerce_id(id))
            return Response(
                body=self._render_body_view(ref, self.store.tags_for(ref.id)),
                result_count=1,
                result_budget_hint=(
                    f"get(kind='todo', id={ref.id}, q='<terms>') reads "
                    "matching passages when the body alone is over budget"
                ),
            )
        if view == "scratch":
            # gr605672: the task-scratch index — one line per
            # ``scratch-for:td<N>`` memory. The read the closing agent
            # makes before posting ``harvest:`` on the todo.
            if id is None:
                raise BadInput(
                    "view='scratch' requires id=",
                    next="get(kind='todo', id=N, view='scratch')",
                )
            ref = self._resolve_live_ref(self._coerce_id(id))
            return views.render_scratch(self.store, ref.id)
        # ``view='meta'`` (gr573038) is a base view now — the ref's
        # meta dict as JSON, shared by every numeric-ref kind
        # (gr613470); it rides ``_BASE_VIEWS`` into the base dispatch.
        if view is not None and view in _TREE_GET_VIEWS:
            # Future tree-flavoured get views land here.
            raise Unsupported(
                f"view {view!r} is registered but has no renderer wired",
                options=sorted(_TREE_GET_VIEWS),
            )
        if (
            view is not None
            and view not in _BASE_VIEWS
            and view not in self._search_view_names()
            and view not in _TODO_GET_VIEW_ALIASES
            and not is_eye_view(view)
        ):
            # gr547804: the base class's generic raise lists only
            # ``_BASE_VIEWS`` — a caller guessing a view name never sees
            # todo's own get views ('comments', 'tree') or where the
            # list views live. Name the whole vocabulary here. Search
            # views stay excluded so the base's search-redirect (or its
            # own refusal) keeps handling them; alias spellings ('full',
            # 'brief', 'tags') without an id fall through to the
            # "requires id=" refusal; eye extents ('fisheye', 'kwd', …)
            # route to ``eye`` in the base class.
            views_list = ["body", "comments", "scratch", "tree", "full", *_BASE_VIEWS]
            near = difflib.get_close_matches(view, views_list, n=1)
            lead = f"nearest is view={near[0]!r}. " if near else ""
            raise Unsupported(
                f"unknown view {view!r} for kind='todo'",
                options=views_list,
                next=(
                    lead + "get(kind='todo', id=<id>) — the card (text, "
                    "tags, status); view='body' the untruncated details "
                    "body; view='comments' the timeline; view='meta' the "
                    "raw meta dict; view='tree' the subtree; list views "
                    "are search-side: "
                    "search(kind='todo', view='projects'|'doable'|'active')"
                ),
            )
        # Single-ref reads route through the base class so links /
        # log views and the soft-delete vs not-found split keep
        # working. The base class returns Response(body=...); we
        # post-pend the ancestry section when this is a single-ref
        # read (id was numeric, view was None).
        resp = super().get(id=id, view=view, q=q, **_kw)
        if (
            view is None
            and isinstance(id, (int, str))
            and not (isinstance(id, str) and id.startswith("/"))
        ):
            try:
                ref_id = self._coerce_id(id)
            except BadInput:
                return interpreted(resp, readings)
            extra = views.render_ancestry_section(self.store, ref_id)
            resp = dataclasses.replace(
                resp,
                body=resp.body + ("\n" + extra if extra else ""),
                # gr554136: when the fitter cuts the card (body +
                # comments + links over the byte budget), its ``next:``
                # must name the bounded body read — not a search that
                # can't return it.
                result_budget_hint=resp.result_budget_hint
                or (
                    f"get(kind='todo', id={ref_id}, view='body') reads "
                    "the details body uncapped"
                ),
            )
        return interpreted(resp, readings)

    def _wait_terminal(self, raw_id: Any, wait: Any) -> list[str]:
        """``args={'wait': <seconds>}`` (gr570506): poll the todo's STATUS
        tag until it turns terminal or the budget runs out — a plan is
        pull-only, so a session that submitted a factory node learns its
        outcome by waiting here, then reading the card. Returns the
        ``wait:`` reading lines; a gone ref lets the normal render raise
        its NotFound."""
        try:
            seconds = float(wait)
        except (TypeError, ValueError):
            raise BadInput(
                f"args['wait']={wait!r} is not a number of seconds",
                next="get(kind='todo', id=<N>, args={'wait': 900})",
            ) from None
        if not 0 < seconds <= _WAIT_CAP_S:
            raise BadInput(
                f"args['wait']={wait!r}: pass 0 < seconds <= {int(_WAIT_CAP_S)}",
                next="re-issue the get to keep waiting past the cap",
            )
        try:
            ref_id = self._coerce_id(raw_id)
        except BadInput:
            raise BadInput(
                f"args['wait'] needs a single numeric todo id, got {raw_id!r}",
                next="get(kind='todo', id=<N>, args={'wait': 900})",
            ) from None
        deadline = time.monotonic() + seconds
        while True:
            ref = self.store.get_ref(kind="todo", id=ref_id)
            if ref is None:
                return []  # the render below raises the usual NotFound
            status = next(
                (
                    str(t.value)
                    for t in self.store.tags_for(ref_id)
                    if t.prefix == "STATUS"
                ),
                "open",
            )
            waited = seconds - (deadline - time.monotonic())
            if status in TERMINAL_TODO_STATUSES:
                return [f"wait: td{ref_id} reached STATUS:{status} after {waited:.0f}s"]
            if time.monotonic() >= deadline:
                return [
                    f"wait: td{ref_id} still STATUS:{status} after "
                    f"{waited:.0f}s — re-issue the same get to keep waiting"
                ]
            time.sleep(min(_WAIT_POLL_S, max(0.0, deadline - time.monotonic())))

    # ── search: tree-aware view router ────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        under: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        q = fold_match_all_q(q)
        # ``view='count'`` is the shared numeric-ref aggregate (total +
        # group_by facets) — it takes no tree args, so under= (todo's
        # own subtree scope) is refused rather than silently dropped.
        if view == "count":
            rest = dict(args or {})
            if under is not None or rest.pop("under", None) is not None:
                raise BadInput(
                    "under= is not supported with view='count' on todo search",
                    next="search(kind='todo', view='count', tags=['<tag>'])",
                )
            # The ref-id channels stay admitted: a paged count's next:
            # call re-spells dispatch-resolved filters (uncited=/cited=/
            # folder=) as args={'include_ref_ids'/'exclude_ref_ids': […]}
            # — refusing them would dead-end its own continuation.
            count_args = {
                "group_by",
                "since",
                "until",
                "page",
                "include_ref_ids",
                "exclude_ref_ids",
            }
            unknown = sorted(set(rest) - count_args)
            if unknown:
                raise BadInput(
                    f"args= keys {unknown!r} not supported with view='count' "
                    f"on kind={self.kind!r}",
                    options=sorted(count_args),
                )
            merged = dict(_kw)
            group_by = merged.pop("group_by", rest.get("group_by"))
            # ``page`` windows the facet groups (gr592971) — the paged
            # count's own next: call is issued with it inside args=;
            # the ref-id channels are the continuation's replayed
            # dispatch-scope filters.
            for key in ("since", "until", "page", "include_ref_ids", "exclude_ref_ids"):
                if rest.get(key) is not None:
                    merged.setdefault(key, rest[key])
            return super().search(
                q=q,
                tags=tags,
                page_size=page_size,
                view="count",
                group_by=group_by,
                **merged,
            )
        if under is not None:
            if view != "doable":
                raise BadInput(
                    "under= requires view='doable' on todo search",
                    next="search(kind='todo', view='doable', under='<todo id>')",
                )
            if (
                args is not None
                and "under" in args
                and str(args["under"]) != str(under)
            ):
                raise BadInput(
                    "under= and args.under disagree",
                    next="pass under= once, either as the kwarg or inside args=, "
                    "with the same todo id",
                )
            args = {**(args or {}), "under": under}
        if view is not None:
            try:
                view_enum = TodoView(view)
            except ValueError:
                available = sorted(str(v) for v in TodoView)
                if view in _SEARCH_PATH_SPELLING_HINTS:
                    raise Unsupported(
                        f"unknown view {view!r} for kind={self.kind!r} search "
                        f"— it is a get() path spelling, not a search view",
                        options=available,
                        next=_SEARCH_PATH_SPELLING_HINTS[view],
                    ) from None
                raise Unsupported(
                    f"unknown view {view!r} for kind={self.kind!r} search",
                    options=available,
                    next=f"views available: {available}; see precis-todo-tree-help",
                ) from None
            if view_enum is TodoView.DOABLE:
                # gr568264: doable is the one tree view that honours
                # tags= — the table's 3-arg signature has no channel
                # for it, so it dispatches here with the raw tags.
                return _view_doable(self.store, args, page_size, tags=tags)
            return _TREE_SEARCH_VIEWS[view_enum](self.store, args, page_size)
        return super().search(q=q, tags=tags, page_size=page_size, **_kw)

    # ── put: parent_id + level guard at create ────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
        parent_id: int | str | None = None,
        prio: int | None = None,
        meta: dict[str, Any] | None = None,
        body: str | None = None,
        # manual=True (create only, see precis-todo-tree-help, gr660003):
        # a parented create defaults to stamping meta.llm_tier='opus' so a
        # generated child actually runs (dispatch only mints a plan_tick
        # under an llm_tier-set todo). For a hand-written backlog/review
        # item the operator never meant to auto-run, that default silently
        # arms billed opus compute — manual=True suppresses the stamp. An
        # explicit meta.llm_tier / meta.executor still wins over the flag.
        manual: bool | None = None,
        # Executor-dispatch config (see precis-job-help / workers/dispatch.py):
        # declared at the top-level put() verb schema (tools/core.py) for the
        # 'job' kind's submit path, but a strict-schema MCP client sends every
        # declared kwarg regardless of kind= — so a todo create call that
        # passes these arrives here too. Before gr333433 they fell into
        # ``**_kw`` and were silently dropped: a todo minted with
        # ``executor=``/``job_type=``/``params=`` looked identical to a plain
        # planner todo, with no error and no signal anything was lost. The
        # dispatch worker (``workers/dispatch.py``) reads its dispatch config
        # from ``meta.executor``/``meta.job_type``/``meta.params`` on the
        # todo row, so land these kwargs there — the exact shape a
        # correctly-minted executor-dispatched todo already carries. An
        # explicit kwarg wins over the same key already present in
        # ``meta=`` (same "declared kwarg beats the bag" precedent as
        # ``prio=`` above).
        executor: str | None = None,
        job_type: str | None = None,
        params: dict[str, Any] | None = None,
        # ``mode='plan'`` keys (args={'template': …, …}, td542095): the
        # template name and its arguments; read only under that mode.
        template: str | None = None,
        gripe_id: int | str | None = None,
        section: str | None = None,
        board: str | None = None,
        lane: str | None = None,
        iterations: int | None = None,
        after: str | None = None,
        context: str | None = None,
        **_kw: Any,
    ) -> Response:
        text, body = mask_secrets(text), mask_secrets(body)
        # ``put`` creates, or (id=N + text=) appends a comment. ``id``
        # isn't forwarded to ``super().put()`` below (it's consumed here for
        # the parent/tier bookkeeping), so the base's own id-rejection never
        # sees it and would silently mint a duplicate row for any other
        # mutate-an-existing-todo shape (gr450132): refuse those up front.
        if id is not None:
            # Text on an existing todo appends a comment (the gripe
            # idiom); tags=/untags= ride along through the shared
            # append-then-link helper — comment-then-close is one put,
            # not put + tag (gr570088; the gripe shape, gr484232). Any
            # other put kwarg keeps the old refusal.
            others = (
                mode, link, unlink, rel, auto_refresh_days,
                parent_id, prio, meta, body, executor, job_type, params,
                manual,
            )  # fmt: skip
            if text is not None and text.strip() and all(o is None for o in others):
                ref = self._resolve_live_ref(self._coerce_id(id))
                return self._append_then_link(
                    lambda: tl.append_comment(
                        self.store,
                        ref,
                        kind=self.kind,
                        comment_kind=_COMMENT_KIND,
                        text=text,
                    ),
                    id=ref.id,
                    link=None,
                    unlink=None,
                    rel=None,
                    tags=tags,
                    untags=untags,
                    what=f"{self._sense()} comment",
                )
            raise BadInput(
                f"put on existing {self._sense()} id={id!r} is not supported"
                " (text= appends a comment; tags=/untags= ride along — "
                "other fields are not accepted)",
                next=(
                    f"put(kind={self.kind!r}, id={id}, text='your comment', "
                    "tags=[...], untags=[...]) to comment and retag in one "
                    "call, "
                    f"edit(kind={self.kind!r}, id={id}, mode='replace', "
                    "text='...'/body='...') to rewrite it, or "
                    f"tag(kind={self.kind!r}, id={id}, add=[...]/remove=[...]) "
                    "to change status/meta"
                ),
            )
        prio = _validate_prio(prio)
        # A create-time ``PRIO:`` alias syncs to the canonical prio column
        # (the doable ORDER BY), stripped from the tag set — same
        # translation gripe / quest already do (``_prio_tag``). An explicit
        # ``prio=`` kwarg wins over the tag form.
        tags, prio_from_tag = split_prio(tags)
        if prio is None:
            prio = prio_from_tag
        if executor is not None or job_type is not None or params is not None:
            meta = dict(meta or {})
            if executor is not None:
                meta["executor"] = executor
            if job_type is not None:
                meta["job_type"] = job_type
            if params is not None:
                meta["params"] = params
        # ``meta.schedule`` may carry the ``every:`` shorthand; validate
        # and rewrite to canonical cron so the runtime only ever sees
        # one shape. The recurring spawner trusts the stored form.
        if meta is not None and "schedule" in meta:
            parsed = guards.check_schedule_in_meta(meta)
            if parsed is not None:
                if parsed.at is not None:
                    schedule_out: dict[str, Any] = {
                        "at": parsed.at,
                        "catch_up": parsed.catch_up,
                    }
                else:
                    schedule_out = {
                        "cron": parsed.cron,
                        "backfill_missed": parsed.backfill_missed,
                    }
                meta = {**meta, "schedule": schedule_out}
        # ``meta.deliver`` marks a recurring (or its ticks) for
        # push delivery — a synthetic prompt fired at asa_bot instead of
        # (or alongside) minting a doable-queue subtask.
        if meta is not None and "deliver" in meta:
            parsed_deliver = guards.check_deliver_in_meta(meta)
            if parsed_deliver is not None:
                meta = {**meta, "deliver": parsed_deliver}
        # Auto-inject parent_id from the runtime context
        # (PRECIS_CURRENT_TODO env). The runner sets this to the parent
        # todo's ref_id on the claude -p subprocess; the LLM doesn't
        # have to remember its own id every put call. The "I am at the
        # root" case still works — caller passes parent_id explicitly
        # (or no env is set when the operator is in an interactive
        # session and is minting a fresh root).
        if parent_id is None:
            from precis.utils.workspace import current_todo_from_env

            parent_id = current_todo_from_env()
        if parent_id is not None:
            try:
                parent_int = parent_id if isinstance(parent_id, int) else int(parent_id)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"parent_id must be an integer, got {parent_id!r}",
                    next="parent_id=<int> (the parent todo's id)",
                ) from exc
            guards.check_parent_exists(self.store, parent_int)
            guards.check_depth_under(self.store, parent_int)
        else:
            parent_int = None
        if mode == "plan":
            return self._put_plan(
                template=template,
                template_args={
                    "gripe_id": gripe_id,
                    "section": section,
                    "board": board,
                    "lane": lane,
                    "iterations": iterations,
                    "after": after,
                    "context": context,
                },
                parent_id=parent_int,
                refused={
                    "text": text,
                    "tags": tags,
                    "link": link,
                    "meta": meta,
                    "body": body,
                    "prio": prio,
                },
            )
        # ``meta.tier`` (strategic|tactical|subtask) is the agent-facing
        # spelling of the facet booleans — translated here so the
        # owner-only gate below sees the derived booleans and enforces
        # identically. The booleans are what persists.
        meta = guards.normalize_tier_meta(meta)
        guards.check_facets_on_create(meta)
        # ``meta={'llm_tier': None}`` is the documented auto-dispatch
        # opt-out (precis-todo-tree-help): drop the key (a stored null
        # would still satisfy dispatch's ``meta ? 'llm_tier'`` check) and
        # suppress the parented-child ``opus`` default below (gr474987).
        llm_tier_opt_out = (
            isinstance(meta, dict) and "llm_tier" in meta and meta["llm_tier"] is None
        )
        if llm_tier_opt_out and meta is not None:
            meta = {k: v for k, v in meta.items() if k != "llm_tier"}
        guards.check_llm_tier_meta(meta)
        guards.check_llm_select_meta(meta)
        guards.check_budget_usd_meta(meta)
        guards.check_rung_meta(meta)
        guards.check_fix_meta(meta)
        _check_plan_meta(meta)
        ask_blocks = guards.check_ask_meta(meta, question=text)
        if meta is not None and "ask" in meta and not guards.has_ask_user_tag(tags):
            raise BadInput(
                "meta.ask makes this todo a question for the owner, which "
                "needs the 'ask-user' tag (the asks view lists by it)",
                next="add tags=['ask-user', 'thread:<slug>'] to the put",
            )
        if ask_blocks is not None:
            guards.check_ask_blocks_target(self.store, ask_blocks, self_id=None)
        guards.check_executor_tag(tags)
        # Workspace inheritance: if the parent carries meta.workspace
        # and this child doesn't specify its own, copy the parent's
        # workspace dict down. The cascade flows the project context
        # from strategic root to every leaf without explicit work.
        # See utils/workspace.Workspace for the shape.
        if parent_int is not None and (meta is None or "workspace" not in meta):
            inherited = _inherit_workspace_from_parent(self.store, parent_int)
            if inherited is not None:
                meta = {**(meta or {}), "workspace": inherited}
        # Auto-inject ``project:<slug>`` cross-cutting tag from the
        # runtime workspace context. Every ref minted under a workspace
        # carries this tag so search(tags=['project:nanotrans_auto'])
        # surfaces the full project surface (todos + citations +
        # findings + file refs) regardless of kind. The LLM doesn't
        # think about it; the env propagates it.
        from precis.utils.workspace import (
            current_project_tag_from_env,
            project_tag_for_path,
        )

        project_tag = current_project_tag_from_env()
        # Owner-path fallback: the env var is only set inside a planner
        # tick. When the operator mints a todo whose meta carries a
        # workspace (set explicitly, or inherited from a project root
        # above), derive the same ``project:<slug>`` tag from that
        # workspace so manually-filed refs join the project surface too.
        # Forward-only — this stamps the ref being created, not its
        # existing subtree.
        if not project_tag and isinstance(meta, dict):
            ws = meta.get("workspace")
            if isinstance(ws, dict):
                project_tag = project_tag_for_path(ws.get("path"))
        if project_tag and (not tags or project_tag not in tags):
            tags = [*(tags or []), project_tag]
        # A *project root* — a workspace-owning todo with no parent — is by
        # definition a strategic root ("a project is a strategic-root todo
        # that owns ``meta.workspace``"). Auto-stamp ``meta.rotation_root``
        # so that invariant holds however the root is minted. ``/drafts/new``
        # already passes it, but a CLI / test / script write that sets
        # ``meta.workspace`` on a fresh root would otherwise leave it
        # non-strategic — and the nursery then flags the entire subtree as
        # orphaned (no ``rotation_root`` ancestor), flooding alerts (the
        # ``draft:test01`` scratch-project flood). Scoped tightly:
        #   * roots only (``parent_int is None``) — inheriting children stay
        #     worker-mintable subtasks and must not all become strategic;
        #   * owner sources only (``is_owner()``) — workers physically can't
        #     mint strategic refs, so never stamp one onto a worker write;
        #   * skip when the caller already set either facet field.
        if (
            parent_int is None
            and guards.is_owner()
            and isinstance(meta, dict)
            and isinstance(meta.get("workspace"), dict)
            and "rotation_root" not in meta
            and "worker_mintable" not in meta
        ):
            meta = {**meta, "rotation_root": True}
        # Default a *generated* (parented) todo to ``meta.llm_tier='opus'``
        # so it actually runs: a todo with no auto-run signal is inert —
        # ``dispatch`` only mints a ``plan_tick`` under an ``llm_tier``-set
        # todo, so a planner-minted / change-request child would sit
        # ``open`` forever (#40159 sat ~40h). Scoped to children
        # (``parent_int`` set — minted under a project, a planner tick, or a
        # change request) so a deliberately-created **root** still gets the
        # "no auto-run" reminder instead of silently auto-running. Skip
        # when the caller already chose a tier / executor, it's a
        # recurring umbrella (``meta.schedule`` set), or it's a parked
        # ``meta.auto_check`` leaf (gr311333) — those are meant to sit
        # inert until their evaluator flips them, not become a
        # ``plan_tick`` dispatch candidate; stamping ``llm_tier`` there
        # both defeats the parking and silently bills opus for a leaf
        # that never should have run.
        stampable_default_tier = (
            id is None
            and parent_int is not None
            and not llm_tier_opt_out
            and not (isinstance(meta, dict) and "llm_tier" in meta)
            and not (isinstance(meta, dict) and meta.get("executor"))
            and not (isinstance(meta, dict) and "schedule" in meta)
            and not (isinstance(meta, dict) and "auto_check" in meta)
            # a question for the owner is never auto-run (td541979)
            and not (isinstance(meta, dict) and "ask" in meta)
            # plan nodes that are evaluated or ticked, never dispatched to
            # a plan_tick (td542095)
            and plan_nodes.node_kind(meta) not in _PLAN_KINDS_NOT_DISPATCHED
            # a task node (meta.op) is driven by plan_dispatch alone, so a
            # hand-added one gets no second dispatcher (gr542301)
            and not (isinstance(meta, dict) and "op" in meta)
        )
        # ``manual=True`` (gr660003) opts a hand-written backlog/review
        # item out of the stamp — the same opt-out as
        # ``meta={'llm_tier': None}`` as a first-class kwarg. Gate the
        # actual stamp on it, and gate the "suppressed" receipt on the
        # un-``manual`` predicate plus ``has_auto_run_signal``: the ack
        # may only claim "will not auto-run" when the flag is what kept
        # the leaf inert — a caller who armed it themselves (explicit
        # tier, ``executor=``, an ``executor:<runner>`` tag) must not get
        # that receipt on a leaf that will run (td660679 review).
        stamped_default_tier = stampable_default_tier and not manual
        # The flag rides ``_PutState`` so the create ack surfaces the stamp
        # — the default silently arms billed compute (dispatch mints a
        # plan_tick for any llm_tier-set leaf), and a write receipt must
        # state consequential state the server added, not just what the
        # caller sent.
        if stamped_default_tier:
            meta = {**(meta or {}), "llm_tier": "opus"}
        # Default parent_id for a recurring root (``meta.schedule`` set) to
        # the seeded Watches umbrella — every recurring lives under it by
        # default, so the operator gets a tidy two-panel ``view='roots'``
        # without per-write boilerplate. Owner can override by passing
        # ``parent_id=<some-strategic>`` to nest under a goal.
        if parent_int is None and isinstance(meta, dict) and "schedule" in meta:
            from precis.workers.schedule.seed import ensure_watches_root

            parent_int = ensure_watches_root(self.store)
        # Validate ``meta.auto_check`` shape so a typo in the
        # evaluator name surfaces at write-time instead of at the
        # next poll. The check is lightweight — type-specific arg
        # validation happens in the evaluator itself.
        if meta is not None and "auto_check" in meta:
            from precis.workers.auto_check_evaluators import (
                validate_auto_check_spec,
            )

            validate_auto_check_spec(meta["auto_check"])
        # A put landing inside a live plan subtree mints the child held
        # (``waiting-for:mint``), unconditionally (td603429): the row
        # commits open and linkless and the ``blocked-by`` links land in
        # later calls, so the child must not dispatch in the gap — and
        # every ancestry/job/lease/principal inference the
        # held-at-creation chain tried left a hole a review found
        # (gr590954; td599743, td601023, td601626, td602770, td603427).
        # The minter removes the tag once its links are settled — the
        # mint-held runbook contract (me570515) enforced; the factory's
        # own mint sequences end in ``plans.nodes.finish_mint``; a clean
        # ``run_slice`` exit finishes its job window's settled mints
        # (gr603945); plan_dispatch hygiene releases a settled mint's
        # hold past ``MINT_STALE`` and asks on an orphan (gr574881). A
        # caller-supplied hold is already held.
        mint_hold = False
        if parent_int is not None and not _mint_held(tags):
            from precis.workers.job_types import plan_dispatch as _pd

            if _pd.in_live_plan(self.store, parent_int):
                tags = [*(tags or []), plan_nodes.MINT_HOLD_TAG]
                mint_hold = True
        # Delegate to the base put for the remaining D6 guardrails
        # (mode=/untags=/unlink= rejection, tag validation, link target
        # resolution, atomic tx) — id= was already rejected above (it's
        # consumed here, not forwarded, so the base's own id-rejection
        # would never fire on it). It calls back into ``_create``, which
        # we override to plumb ``parent_id``, ``meta``, and ``prio``
        # through to the store layer via the request-scoped ``_PUT_STATE``.
        state = _PutState(
            parent_id=parent_int,
            meta=dict(meta) if meta is not None else None,
            prio=prio,
            body=body.strip() if isinstance(body, str) and body.strip() else None,
            stamped_default_tier=stamped_default_tier,
            manual_no_stamp=bool(manual)
            and stampable_default_tier
            and not guards.has_auto_run_signal(tags, meta),
            mint_hold=mint_hold,
        )
        with _PUT_STATE.bind(state):
            resp = super().put(
                text=text,
                mode=mode,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
                auto_refresh_days=auto_refresh_days,
            )
        # gr548800: an ask raised inside a plan is a quest-logbook event.
        if (
            resp.ref_id is not None
            and isinstance(meta, dict)
            and isinstance(meta.get("ask"), dict)
        ):
            plan_log.log_plan_event(
                self.store,
                resp.ref_id,
                f"ask td{resp.ref_id} raised — {plan_log.first_line(text)}",
            )
        return resp

    def _put_plan(
        self,
        *,
        template: str | None,
        template_args: dict[str, Any],
        parent_id: int | None,
        refused: dict[str, Any],
    ) -> Response:
        """``put(mode='plan', args={'template': name, …})``: mint a plan
        subtree (:mod:`precis.plans.templates`) and ack with its tree."""
        given = sorted(k for k, v in refused.items() if v not in (None, "", [], {}))
        if given:
            raise BadInput(
                f"mode='plan' mints from the template; {given} are not accepted",
                next="put(kind='todo', mode='plan', args={'template': '<name>', ...})",
            )
        names = sorted(plan_templates.TEMPLATES)
        tpl = plan_templates.TEMPLATES.get(template) if template else None
        if tpl is None:
            raise BadInput(
                f"unknown plan template {template!r}; templates: {names}"
                if template
                else f"mode='plan' requires args={{'template': <name>}}; templates: {names}",
                options=names,
                next="put(kind='todo', mode='plan', args={'template': "
                + repr(names[0])
                + ", ...})",
            )
        kwargs = {k: template_args[k] for k in tpl.required if k in template_args}
        kwargs.update(
            {
                k: template_args[k]
                for k in tpl.optional
                if template_args.get(k) not in (None, "")
            }
        )
        missing = [k for k in tpl.required if template_args.get(k) in (None, "")]
        if missing:
            raise BadInput(
                f"plan template {tpl.name!r} needs args {missing}",
                next=(
                    "put(kind='todo', mode='plan', args={'template': "
                    f"{tpl.name!r}, "
                    + ", ".join(f"{k!r}: ..." for k in tpl.required)
                    + "})"
                ),
            )
        mint = tpl.mint(self.store, parent_id, **kwargs)
        tree = views.render_tree(self.store, mint.root_id)
        return Response(
            body=(
                f"minted plan {tpl.name!r}: root id={mint.root_id}, "
                f"{len(mint.ids)} nodes, all tagged plan:{mint.root_id}, "
                "no auto-dispatch (meta.llm_tier unset)\n" + tree.body
            )
        )

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
        put_meta: dict[str, Any] | None = None,
    ) -> Response:
        # Mirror of NumericRefHandler._create, with parent_id wired
        # through. We can't just call super() because the base class
        # doesn't accept ``parent_id``; rewriting the body keeps the
        # tx boundary intact.
        from precis.utils.link_tag_ops import (
            guard_taxon_hierarchy,
            validate_relation,
        )
        from precis.utils.link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel, store=self.store)
        if target is not None:
            # Taxon hierarchy rules (docs/backlog/term-taxonomy.md): this
            # create path writes its own link, so it calls the shared guard.
            guard_taxon_hierarchy(
                self.store, None, target, relation, src_kind=self.kind
            )

        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        state = _PUT_STATE.get()
        facts_deferred = 0

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text,
                meta=dict(state.meta or {}),
                auto_refresh_days=auto_refresh_days,
                parent_id=state.parent_id,
                prio=state.prio,
                conn=conn,
            )
            # Optional details body → a todo_body chunk at pos 0, written
            # *before* the tag-overflow redirect (which appends at the next
            # free ord). Additive: the title stays in refs.title; the
            # body is extra prose, embedded + keyworded by the workers.
            if state.body:
                self.store.chunks.insert_chunks(
                    ref.id,
                    [
                        ChunkInsert(
                            ord=0,
                            text=state.body,
                            meta={"chunk_kind": _BODY_KIND},
                        )
                    ],
                    conn=conn,
                )
            # Redirect long/whitespace ask-user:/halt: yields into a
            # tag_overflow chunk on the just-minted ref *before*
            # parse_strict's whitespace guard fires — so a legitimate
            # create-time yield becomes a space-free see-chunk-N handle
            # rather than being rejected (gripe #39254). Runs on the
            # create connection so the chunk + tags land atomically; a
            # later rejected tag rolls the ref insert back with the tx.
            redirected, _redirected_chunks = redirect_long_tag_values(
                self.store, ref_id=ref.id, tags=all_tag_strs, conn=conn
            )
            parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in redirected]
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation,
                    conn=conn,
                )
            # meta.ask.blocks: the named slice waits on this question.
            ask_blocks = guards.check_ask_meta(state.meta)
            if ask_blocks is not None:
                self.store.add_link(
                    src_ref_id=ask_blocks,
                    dst_ref_id=ref.id,
                    relation="blocked-by",
                    conn=conn,
                )
            if self.emits_card:
                self.store.chunks.upsert_card_combined(ref.id, text, conn=conn)
            facts_deferred = self._sync_mention_links(
                ref.id,
                text + ("\n" + state.body if state.body else ""),
                conn=conn,
            )
        if target is not None and target.kind == "part":
            self._enqueue_part_datasheet(target.ref_id)
        # Soft reminder: a rotation_root todo with no auto-run signal
        # (meta.llm_tier / executor:* tag / meta.executor) will never be
        # picked up by the dispatch worker, so it spawns no children and
        # the planner brief in its body is inert. Non-breaking HintBus
        # tip — the owner may legitimately intend to drive it by hand.
        if guards.strategic_lacks_auto_run(state.meta, all_tag_strs):
            hub = getattr(self, "hub", None)
            if hub is not None:
                from precis.hints import Hint

                hub.emit_hint(
                    Hint(
                        text=(
                            f"strategic #{ref.id} has no auto-run signal "
                            "(meta.llm_tier / executor:*). The dispatcher "
                            "won't pick it up and it'll spawn no children. "
                            "Set meta.llm_tier='opus' (or sonnet/haiku) to "
                            "run it autonomously, or ignore if you'll drive "
                            "it by hand."
                        ),
                        topic="todo.strategic.no_auto_run",
                    )
                )
        return self._facts_note(self._render_create_ack(ref.id), facts_deferred)

    # ── edit: in-place text rewrite (polish the wording) ──────────

    def edit(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        mode: str = "replace",
        text: str | None = None,
        body: str | None = None,
        meta: dict[str, Any] | None = None,
        dry_run: bool | str | None = None,
        find: str | None = None,
        before: str = "",
        after: str = "",
        where: str | None = None,
        match: str = "unique",
        nth: int | None = None,
        sha: str | None = None,
        landed_by: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        """In-place rewrite of a todo's title and/or details body.

        ``text=`` rewrites the title (``refs.title``); ``body=`` sets or
        rewrites the optional details body (a ``todo_body`` chunk). Pass
        either or both — ``text=`` alone keeps the body, and an explicit
        ``body=''`` (whitespace stays a no-op) clears it. When a retitle
        would drop a long detail-carrying title and no ``todo_body``
        exists, the old title is moved into the body first — the ack
        says "old title preserved as body" (gr515178). Same id; parent,
        links, and tags all stay attached —
        the old text lands in ``ref_events`` as a ``body_replaced`` row
        (``view='log'``). Owner-only on strategic / tactical nodes, the same
        authority veto as delete / reparent. Distinct from delete + re-put,
        which would break every inbound edge and the tree position.

        ``meta={'llm_tier': None}`` unsets ``meta.llm_tier`` on the leaf,
        parking it — dispatch (``workers/dispatch.py``) only mints a
        ``plan_tick`` for an ``llm_tier``-set leaf, and a stored JSON
        ``null`` still satisfies its ``meta ? 'llm_tier'`` existence
        check, so this deletes the key outright rather than nulling it
        (gr439934).

        ``meta={'params': {...}}`` replaces a todo's job params (gr504853
        — the recurring-todo edit door: the schedule worker copies
        ``meta.params`` onto every spawned tick, so this retunes a
        recurring in place instead of delete + re-seed); ``{'params':
        None}`` deletes the key. A recurring's params follow the
        schedule's owner-only gradient (:func:`check_recurring_params_edit`
        ). The write lands as an ``agent/meta_patched`` ref_events row
        (view='log'). ``meta={'op'|'gate'|'loop'|'generate'|'auto_close':
        ...}`` sets a plan node key (shape-checked by
        :mod:`precis.plans.nodes`; ``None`` clears it; the merged node may
        carry at most one of gate / loop / generate). Every other meta
        mutation still goes through ``tag(meta=...)``'s allowlisted
        promotion.

        ``mode='find-replace'`` / ``'insert'`` (gr478556) edit the details
        body in place: ``find=`` locates literal text (unique unless
        ``match=``), ``text=`` replaces it (or goes ``where='before'|'after'``
        it for insert); same vocabulary as memory via
        :mod:`precis.utils.edit_resolve`. Comment chunks are never touched.
        The title stays replace-only.

        ``dry_run=True`` previews the replacement without writing — the
        tool-level contract every editable kind must honour (a silent
        write on ``dry_run`` is data loss).

        ``id=[...]`` (``batch_ids``) applies the same edit to every id in
        one transaction — :meth:`_edit_batch` (gr514482).

        ``mode='gate'`` (gr597921) is the MCP door for the post-land write
        ``land_slice`` does after a factory land
        (:func:`precis.workers.job_types.land_slice._write_gate`):
        ``args={'sha': '<7-40 hex>'}`` fills a ``sha-in-prod`` gate's
        ``args.sha`` — but only while it still holds the placeholder
        :data:`precis.plans.templates.PENDING_SHA`, so a hand-landed slice's
        gate can be filled without SQL and an already-filled gate is never
        rewritten. ``args={'landed_by': 'td<N>'}`` names the build todo for
        the same audit comment the lander writes. Text/body/meta/anchor
        kwargs are refused under it; ``dry_run`` previews.
        """
        if isinstance(id, list):
            return self._edit_batch(
                id,
                mode=mode,
                text=text,
                body=body,
                meta=meta,
                dry_run=dry_run,
                find=find,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                sha=sha,
                landed_by=landed_by,
                **_kw,
            )
        text, body = mask_secrets(text), mask_secrets(body)
        if id is None:
            raise BadInput(
                "edit(kind='todo') requires id=",
                next="edit(kind='todo', id=N, mode='replace', text='new text')",
            )
        if mode == "gate":
            strays = sorted(
                k
                for k, v in (
                    ("text", text),
                    ("body", body),
                    ("meta", meta),
                    ("find", find),
                    ("before", before or None),
                    ("after", after or None),
                    ("where", where),
                    ("match", match if match != "unique" else None),
                    ("nth", nth),
                )
                if v is not None
            )
            if strays:
                raise BadInput(
                    f"mode='gate' takes args={{'sha': ...}} only, not {strays}",
                    next=(
                        "edit(kind='todo', id=<gate>, mode='gate', "
                        "args={'sha': '<hex sha>'})"
                    ),
                )
            return self._edit_gate_sha(
                ref_id=self._coerce_id(id),
                sha=sha,
                landed_by=landed_by,
                dry_run=dry_run,
            )
        if sha is not None or landed_by is not None:
            raise BadInput(
                "sha=/landed_by= only apply under mode='gate'",
                next=(
                    "edit(kind='todo', id=<gate>, mode='gate', "
                    "args={'sha': '<hex sha>'})"
                ),
            )
        delete_span = mode == "delete"
        if delete_span:
            # gr503460: clients mangle an empty ``text=''`` before the
            # call reaches the server — explicit span-delete mode,
            # an alias of find-replace with text=''.
            if text is not None and text != "":
                raise BadInput(
                    "mode='delete' takes find= only",
                    next=(
                        "edit(kind='todo', id=N, mode='delete', "
                        "find='exact span to remove') — or mode='find-replace' "
                        "with text='' where the client passes it through"
                    ),
                )
            if not find:
                raise BadInput(
                    "mode='delete' requires find= (the exact span to remove)",
                    next="edit(kind='todo', id=N, mode='delete', find='exact text')",
                )
            mode, text = "find-replace", ""
        if mode == "insert" or (mode == "find-replace" and find is not None):
            if meta is not None or (body is not None and body.strip()):
                raise BadInput(
                    f"meta=/body= are replace-only; mode={mode!r} edits the "
                    "details body in place",
                    next=f"edit(kind='todo', id={id}, mode='replace', body='...')",
                )
            return self._edit_anchored(
                ref_id=self._coerce_id(id),
                mode=mode,
                find=find,
                text=text,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                dry_run=dry_run,
                delete_span=delete_span,
            )
        # A meta-only patch rewrites no text, so mode= has nothing to select
        # and is not required. It cannot simply be validated either: the
        # tool-layer ``tools/core.py::edit`` defaults mode= to the file-kind
        # ``'find-replace'``, so an omitted mode= arrives here as a value this
        # kind rejects — which would make the parking shape this path exists
        # for (``edit(kind='todo', id=N, meta={'llm_tier': None})``)
        # unreachable over the MCP door (same trap as quest, 4db6836b).
        # ``body=''`` (exactly the empty string — whitespace-only stays a
        # no-op, refusing like an omitted field) is an explicit clear
        # (gr515178): a title-only replace keeps the body, while
        # ``body=''`` drops the ``todo_body`` chunk outright.
        clear_body = body == ""
        meta_only = (
            meta is not None
            and (text is None or not text.strip())
            and (body is None or not body.strip())
            and not clear_body
        )
        if not meta_only:
            require_mode(spec=self.spec, verb="edit", mode=mode)
            if mode != "replace":
                # The MCP default mode is 'find-replace'; with no find= this
                # call is a whole-text write that never chose 'replace'.
                raise BadInput(
                    f"mode={mode!r} requires find= (the exact text to locate)",
                    next=(
                        f"edit(kind='todo', id={id}, mode='find-replace', "
                        "find='exact text', text='replacement') — or, to "
                        f"rewrite the title/body, edit(kind='todo', id={id}, "
                        "mode='replace', text='...'/body='...')"
                    ),
                )
        unset_llm_tier = False
        params_value: dict[str, Any] | None = None
        clear_params = False
        ask_value: dict[str, Any] | None = None
        ask_blocks: int | None = None
        plan_set: dict[str, Any] = {}
        plan_clear: list[str] = []
        if meta is not None:
            extra_keys = set(meta) - {
                "llm_tier",
                "params",
                "ask",
                *plan_nodes.PLAN_KEYS,
            }
            if extra_keys:
                raise BadInput(
                    f"edit(kind='todo', meta=...) only accepts keys 'llm_tier' "
                    "(None parks the leaf's auto-run tier), 'params' (a "
                    "recurring / executor todo's job config), 'ask' (a "
                    "question for the owner) and the plan node keys "
                    f"{list(plan_nodes.PLAN_KEYS)} (None clears one), "
                    f"not {sorted(extra_keys)}",
                    next=(
                        f"other meta mutations: tag(kind='todo', id={id}, meta={{...}})"
                    ),
                )
            if "llm_tier" in meta:
                if meta["llm_tier"] is not None:
                    raise BadInput(
                        "edit(kind='todo', meta=...) only accepts 'llm_tier' as "
                        "None (park a leaf's auto-run tier), not a value",
                        next=(
                            f"to set a tier instead: tag(kind='todo', id={id}, "
                            "meta={'llm_tier': 'opus'})"
                        ),
                    )
                unset_llm_tier = True
            if "params" in meta:
                if meta["params"] is None:
                    clear_params = True
                elif isinstance(meta["params"], dict):
                    params_value = meta["params"]
                else:
                    raise BadInput(
                        "meta.params must be a dict, got "
                        f"{type(meta['params']).__name__}",
                        next=(
                            f"edit(kind='todo', id={id}, meta={{'params': {{...}}}})"
                        ),
                    )
            if "ask" in meta:
                # The delegation lint sees the question only in replace
                # mode — under find-replace ``text`` is a body snippet.
                ask_blocks = guards.check_ask_meta(
                    meta, question=text if mode == "replace" else None
                )
                ask_value = dict(meta["ask"])
            plan_set = {
                k: meta[k]
                for k in plan_nodes.PLAN_KEYS
                if k in meta and meta[k] is not None
            }
            plan_clear = [
                k for k in plan_nodes.PLAN_KEYS if k in meta and meta[k] is None
            ]
            _check_plan_meta(plan_set)
        has_text = text is not None and text.strip()
        has_body = body is not None and body.strip()
        if (
            not has_text
            and not has_body
            and not clear_body
            and not unset_llm_tier
            and params_value is None
            and not clear_params
            and ask_value is None
            and not plan_set
            and not plan_clear
        ):
            raise BadInput(
                "edit(kind='todo', mode='replace') requires text= and/or "
                "body= (or meta={'llm_tier': None} to park the leaf, or "
                "meta={'params': {...}} to retune a recurring / executor todo, "
                "or a plan node key in meta=)",
                next="edit(kind='todo', id=N, mode='replace', text='new text')",
            )
        ref_id = self._coerce_id(id)
        guards.check_owner_only_ref(self.store, ref_id)
        ref = self._resolve_live_ref(ref_id)
        body_chunk = next(
            (
                c
                for c in self.store.chunks.list_chunks_for_ref(ref.id)
                if c.chunk_kind == _BODY_KIND
            ),
            None,
        )
        body_present = body_chunk is not None
        if params_value is not None or clear_params:
            guards.check_recurring_params_edit(True, ref.meta)
        if ask_blocks is not None:
            guards.check_ask_blocks_target(self.store, ask_blocks, self_id=ref.id)
        if plan_set:
            # At most one of gate / loop / generate -- judged on the node
            # as it will be, so adding a loop to a gate node is refused.
            merged = {
                k: v
                for k, v in {**(ref.meta or {}), **plan_set}.items()
                if k not in plan_clear
            }
            _check_plan_meta(merged, only="gate/loop/generate")
        if dry_run:
            preview: list[str] = []
            if has_text:
                assert text is not None
                old = ref.title or ""
                preview.append(
                    f"title: {old!r} → {text!r} "
                    f"({len(old.split())} → {len(text.split())} words)"
                )
                if (
                    not has_body
                    and not clear_body
                    and not body_present
                    and _title_holds_details(old)
                ):
                    preview.append("old title would be preserved as the details body")
            if has_body:
                preview.append("details body would be replaced")
            elif clear_body:
                preview.append(
                    "details body would be cleared"
                    if body_present
                    else "no details body to clear"
                )
            if unset_llm_tier:
                preview.append("meta.llm_tier would be unset (parked)")
            if params_value is not None:
                preview.append("meta.params would be replaced")
            if clear_params:
                preview.append("meta.params would be cleared")
            if ask_value is not None:
                preview.append("meta.ask would be replaced")
            preview.extend(f"meta.{k} would be replaced" for k in plan_set)
            preview.extend(f"meta.{k} would be cleared" for k in plan_clear)
            return Response(
                body=(
                    f"dry-run (no write) — would replace {' + '.join(preview)} "
                    f"of todo id={ref.id}."
                )
            )
        # The post-edit title+body, for the mention resync below. The
        # body arm mirrors the write logic: explicit body= wins, an
        # explicit '' clear leaves nothing, a detail-carrying old title
        # survives as the new body, else the prior body stands.
        post_title = (text or "") if has_text else (ref.title or "")
        if has_body:
            post_body = body or ""
        elif clear_body:
            post_body = ""
        elif not body_present and _title_holds_details(ref.title or ""):
            post_body = ref.title or ""
        else:
            post_body = body_chunk.text if body_chunk is not None else ""
        prose_changed = bool(has_text or has_body or clear_body)
        old_text: str | None = None
        preserved_title = False
        cleared_body: str | None = None
        facts_deferred = 0
        with self.store.tx() as conn:
            if has_text:
                assert text is not None
                old_text = self.store.replace_ref_text(
                    ref.id, text, source=guards._caller_source(), conn=conn
                )
                if self.emits_card:
                    self.store.chunks.upsert_card_combined(ref.id, text, conn=conn)
                # gr515178 (td508297): a long title was doing body work —
                # a retitle that drops it loses the details. When the
                # caller didn't touch body= (an explicit ``body=`` write
                # or ``''`` clear means they manage the details
                # themselves) and no ``todo_body`` exists yet, move the
                # old title into the body before it's gone.
                if (
                    not has_body
                    and not clear_body
                    and not body_present
                    and _title_holds_details(old_text or "")
                ):
                    self.store.chunks.replace_body_chunk(
                        ref.id,
                        old_text or "",
                        chunk_kind=_BODY_KIND,
                        source=guards._caller_source(),
                        op="preserve-title",
                        conn=conn,
                    )
                    preserved_title = True
            if has_body:
                assert body is not None
                self.store.chunks.replace_body_chunk(
                    ref.id,
                    body,
                    chunk_kind=_BODY_KIND,
                    source=guards._caller_source(),
                    conn=conn,
                )
            elif clear_body:
                cleared_body = self.store.chunks.delete_body_chunk(
                    ref.id,
                    chunk_kind=_BODY_KIND,
                    source=guards._caller_source(),
                    op="clear",
                    conn=conn,
                )
            if unset_llm_tier:
                # A real JSONB key deletion, not a ``stamp_ref_meta``
                # merge — ``meta || {'llm_tier': null}`` would leave the
                # key present (storing JSON null), and dispatch's
                # ``meta ? 'llm_tier'`` existence check doesn't inspect
                # the value, only key presence, so a stored null would
                # still mint a plan_tick.
                conn.execute(
                    "UPDATE refs SET meta = meta - 'llm_tier', "
                    "updated_at = now() WHERE ref_id = %s",
                    (ref_id,),
                )
            if params_value is not None or clear_params:
                old_params = (ref.meta or {}).get("params")
                if params_value is not None:
                    # Whole-subtree replace (jsonb ||): the spawner copies
                    # meta.params verbatim onto each tick, so a deep-merge
                    # would leave deleted keys haunting every future tick.
                    self.store.stamp_ref_meta(
                        ref_id, {"params": params_value}, conn=conn
                    )
                else:
                    conn.execute(
                        "UPDATE refs SET meta = meta - 'params', "
                        "updated_at = now() WHERE ref_id = %s",
                        (ref_id,),
                    )
                # Same audit shape quest's meta patch carries
                # (agent/meta_patched, view='log').
                self.store.append_event(
                    ref_id,
                    source="agent",
                    event="meta_patched",
                    payload={
                        "old": {"params": old_params},
                        "new": {"params": params_value},
                    },
                    conn=conn,
                )
            if ask_value is not None:
                # Whole-key replace (jsonb ||): the new ask is the ask.
                self.store.stamp_ref_meta(ref_id, {"ask": ask_value}, conn=conn)
                if ask_blocks is not None:
                    self.store.add_link(
                        src_ref_id=ask_blocks,
                        dst_ref_id=ref_id,
                        relation="blocked-by",
                        conn=conn,
                    )
                self.store.append_event(
                    ref_id,
                    source="agent",
                    event="meta_patched",
                    payload={
                        "old": {"ask": (ref.meta or {}).get("ask")},
                        "new": {"ask": ask_value},
                    },
                    conn=conn,
                )
            if plan_set or plan_clear:
                old_plan = {
                    k: (ref.meta or {}).get(k) for k in (*plan_set, *plan_clear)
                }
                if plan_set:
                    # Whole-key replace (jsonb ||), like params and ask.
                    self.store.stamp_ref_meta(ref_id, plan_set, conn=conn)
                for k in plan_clear:
                    conn.execute(
                        "UPDATE refs SET meta = meta - %s::text, "
                        "updated_at = now() WHERE ref_id = %s",
                        (k, ref_id),
                    )
                self.store.append_event(
                    ref_id,
                    source="agent",
                    event="meta_patched",
                    payload={
                        "old": old_plan,
                        "new": {**plan_set, **dict.fromkeys(plan_clear)},
                    },
                    conn=conn,
                )
            if prose_changed:
                facts_deferred = self._sync_mention_links(
                    ref.id,
                    post_title + "\n" + post_body,
                    conn=conn,
                    replace=True,
                )
        parts: list[str] = []
        if has_text:
            assert text is not None
            old_words = len((old_text or "").split())
            new_words = len(text.split())
            parts.append(f"title ({old_words} → {new_words} words)")
        if preserved_title:
            parts.append("old title preserved as body")
        if has_body:
            parts.append("details body")
        elif clear_body:
            parts.append(
                "details body (cleared)"
                if cleared_body is not None
                else "no details body to clear"
            )
        if unset_llm_tier:
            parts.append("meta.llm_tier (unset, parked)")
        if params_value is not None:
            parts.append("meta.params (replaced)")
        if clear_params:
            parts.append("meta.params (cleared)")
        parts.extend(f"meta.{k} (replaced)" for k in plan_set)
        parts.extend(f"meta.{k} (cleared)" for k in plan_clear)
        if ask_value is not None:
            parts.append("meta.ask (replaced)")
            if ask_blocks is not None:
                parts.append(
                    f"blocked-by link from {handle_registry.format_handle('todo', ask_blocks)}"
                )
        return self._facts_note(
            Response(
                body=(
                    f"replaced {' + '.join(parts)} of todo id={ref.id}. "
                    "view='log' for the full diff."
                )
            ),
            facts_deferred,
        )

    # ── edit: mode='gate' — fill a pending sha-in-prod gate (gr597921) ──

    @staticmethod
    def _gate_landed_by(value: str | int) -> str:
        """Normalise ``landed_by`` to the ``td<N>`` handle the lander stamps."""
        digits = str(value).strip().removeprefix("todo:").removeprefix("td")
        if not digits.isdigit():
            raise BadInput(
                f"args['landed_by'] must be the landed todo (an id or 'td<N>'), "
                f"got {value!r}",
                next="args={'sha': '<sha>', 'landed_by': 'td<N>'} — or omit it",
            )
        return f"td{int(digits)}"

    @staticmethod
    def _gate_fill_verdict(
        ref_id: int, gate: Any, sha_norm: str
    ) -> dict[str, Any] | Response:
        """The gate's ``args`` when it may be filled with ``sha_norm`` —
        else the refusal: the idempotent ack ``Response`` when it already
        holds ``sha_norm``, ``BadInput`` for a non-``sha-in-prod`` gate or
        one filled with a different sha. Runs on the resolved ref and again
        on the ``FOR UPDATE`` row inside the fill transaction, so a
        concurrent fill is rechecked under the lock, never overwritten."""
        if not isinstance(gate, dict) or gate.get("kind") != "sha-in-prod":
            kind = gate.get("kind") if isinstance(gate, dict) else gate
            raise BadInput(
                f"todo id={ref_id} is not a sha-in-prod gate (meta.gate.kind={kind!r})",
                next="mode='gate' only fills a pending sha-in-prod gate's "
                "args.sha — other gate kinds have no sha to fill",
            )
        gate_args = dict(gate.get("args") or {})
        current = str(gate_args.get("sha") or "")
        if current == sha_norm:
            return Response(body=f"gate td{ref_id}: sha-in-prod sha already {sha_norm}")
        if current != plan_templates.PENDING_SHA:
            raise BadInput(
                f"gate td{ref_id} already holds sha '{current}' — mode='gate' "
                f"only fills the {plan_templates.PENDING_SHA} placeholder",
                next="a filled gate is rewritten wholesale via "
                "edit(kind='todo', id=N, meta={'gate': {...}})",
            )
        return gate_args

    def _edit_gate_sha(
        self,
        *,
        ref_id: int,
        sha: str | None,
        landed_by: str | int | None,
        dry_run: bool | str | None,
    ) -> Response:
        """``edit(mode='gate')``: write the real squash sha into a pending
        ``sha-in-prod`` gate — the write ``land_slice._write_gate`` makes
        after a factory land, as the door for a slice landed by hand
        (``scripts/ship --quick`` never reaches the lander, gr597921).
        Restricted to gates still at :data:`PENDING_SHA`: the write is a
        fill, never a rewrite. The placeholder check re-runs on the
        ``FOR UPDATE`` row inside ``store.atomic()`` and the
        ``sha: <sha>`` audit comment commits in the same transaction, so a
        concurrent fill is refused rather than overwritten and a failed
        comment leaves nothing half-written for a retry to skip."""
        sha_norm = sha.strip().lower() if isinstance(sha, str) else ""
        if not plan_nodes._SHA_RE.fullmatch(sha_norm):
            raise BadInput(
                "mode='gate' requires args={'sha': '<7-40 lowercase hex sha>'}",
                next=(
                    "edit(kind='todo', id=<gate>, mode='gate', args={'sha': '<sha>'})"
                ),
            )
        if sha_norm == plan_templates.PENDING_SHA:
            raise BadInput(
                f"args.sha {plan_templates.PENDING_SHA!r} is the placeholder "
                "itself — pass the squash sha the land wrote to main",
                next="read it off the build todo's landed: comment",
            )
        who = (
            f" — landed by {self._gate_landed_by(landed_by)}"
            if landed_by is not None
            else ""
        )
        guards.check_owner_only_ref(self.store, ref_id)
        ref = self._resolve_live_ref(ref_id)
        verdict = self._gate_fill_verdict(
            ref_id, (ref.meta or {}).get("gate"), sha_norm
        )
        if isinstance(verdict, Response):
            return verdict
        if dry_run:
            return Response(
                body=(
                    f"dry-run (no write) — would set gate td{ref.id} "
                    f"sha-in-prod args.sha = {sha_norm}."
                )
            )
        with self.store.atomic() as conn:
            # Lock the row and re-run the verdict on the locked meta: a
            # concurrent fill committed between the resolve above and this
            # lock is seen here, never overwritten.
            row = conn.execute(
                "SELECT meta->'gate' FROM refs "
                "WHERE ref_id = %s AND retired_at IS NULL FOR UPDATE",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise Gone(
                    f"todo id={ref_id} was deleted concurrently",
                    next=f"get(kind='todo', id={ref_id}) — a soft-delete "
                    "is post-mortem only, recoverable at the SQL layer",
                )
            live_gate = row[0]
            verdict = self._gate_fill_verdict(ref_id, live_gate, sha_norm)
            if isinstance(verdict, Response):
                return verdict
            new_gate = {**live_gate, "args": {**verdict, "sha": sha_norm}}
            self.store.stamp_ref_meta(ref_id, {"gate": new_gate}, conn=conn)
            self.store.append_event(
                ref_id,
                source="agent",
                event="meta_patched",
                payload={"old": {"gate": live_gate}, "new": {"gate": new_gate}},
                conn=conn,
            )
            # The audit comment joins the same transaction (its inner
            # checkouts savepoint-join under ``atomic``): a comment failure
            # rolls the fill back, so a retry still finds the placeholder.
            tl.append_comment(
                self.store,
                ref,
                kind=self.kind,
                comment_kind=_COMMENT_KIND,
                text=f"sha: {sha_norm}{who}",
            )
        return Response(body=f"gate td{ref.id}: sha-in-prod sha set to {sha_norm}")

    def _edit_anchored(
        self,
        *,
        ref_id: int,
        mode: str,
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_run: bool | str | None,
        delete_span: bool = False,
    ) -> Response:
        """``mode='find-replace'`` / ``'insert'`` on the ``todo_body`` chunk.

        Resolves the edit over the current body with the shared pure
        resolver, then writes via ``replace_body_chunk`` (DELETE + INSERT,
        ``body_replaced`` audit row) — only the ``todo_body`` chunk is
        replaced, comment chunks are untouched. Owner-only guard and
        ``dry_run`` apply as for ``mode='replace'``.
        """
        text = mask_secrets(text)
        dry_mode = normalize_dry_run(dry_run)
        op_kind = (
            "delete-span"
            if delete_span
            else "edit"
            if mode == "find-replace"
            else "insert"
        )
        _require_find_and_text(
            op_kind=op_kind,
            kind=self.kind,
            slug=str(ref_id),
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
        )
        assert find is not None and text is not None
        guards.check_owner_only_ref(self.store, ref_id)
        ref = self._resolve_live_ref(ref_id)
        old_body = self._body_text(ref)
        if old_body is None:
            raise BadInput(
                f"todo id={ref.id} has no details body to edit",
                next=(
                    f"edit(kind='todo', id={ref.id}, mode='replace', "
                    "body='...') sets one"
                ),
            )
        label = f"{self.kind} {ref.id}"
        op = EditOp(
            op="insert" if op_kind == "insert" else "edit",
            find=find,
            text=text,
            before=before,
            after=after,
            where=cast("Any", where),
            match=cast("Any", match),
            nth=nth,
            region_label=label,
        )
        result = apply_edit(old_body, op)
        new_body = result.new_buffer
        if not new_body.strip():
            raise BadInput(
                f"edit would leave {label} with an empty details body",
                next="edit with replacement text, or keep the body as is",
            )
        if dry_mode is not None:
            header = render_dry_run_header(
                region_label=label,
                edited_spans=result.edited_spans,
                match_policy=op.match,
            )
            if dry_mode == "full":
                preview = render_dry_run_full(
                    new_body, edited_spans=result.edited_spans, region_label=label
                )
            else:
                diff = format_unified_diff(old_body, new_body, file_label=label)
                preview = diff.rstrip("\n") or "(no diff - pre and post are identical)"
            return Response(body="\n".join([*header, "", preview]))
        with self.store.tx() as conn:
            self.store.chunks.replace_body_chunk(
                ref.id,
                new_body,
                chunk_kind=_BODY_KIND,
                source=guards._caller_source(),
                op=op_kind if op_kind == "delete-span" else None,
                conn=conn,
            )
            facts_deferred = self._sync_mention_links(
                ref.id,
                (ref.title or "") + "\n" + new_body,
                conn=conn,
                replace=True,
            )
        verb = (
            "edited"
            if op_kind == "edit"
            else "deleted from"
            if op_kind == "delete-span"
            else "inserted into"
        )
        n_spans = len(result.edited_spans)
        return self._facts_note(
            Response(
                body=(
                    f"{verb} details body of todo id={ref.id} "
                    f"({len(old_body.split())} → {len(new_body.split())} words, "
                    f"{n_spans} span{'s' if n_spans != 1 else ''}). "
                    "view='log' for the full diff."
                )
            ),
            facts_deferred,
        )

    # ── comments: render + hide ───────────────────────────────────

    def _render_comments(self, ref: Ref) -> str:
        comments = [
            b
            for b in self.store.chunks.list_chunks_for_ref(ref.id)
            if b.chunk_kind == _COMMENT_KIND
        ]
        return tl.render_comments(ref, comments, kind=self.kind)

    def _render_comments_view(
        self,
        ref: Ref,
        *,
        comment: int | str | None = None,
        comments: int | str | None = None,
        last: int | None = None,
    ) -> str:
        """``view='comments'`` — the selectable, whole-comment-paged read.

        No selectors: every comment oldest first under the shared page
        budget, with the ``args={...}`` spellings in the footer. With
        selectors (``comment`` / ``comments`` / ``last``): the matching
        window only (gr567276).
        """
        chunks = [
            b
            for b in self.store.chunks.list_chunks_for_ref(ref.id)
            if b.chunk_kind == _COMMENT_KIND
        ]
        only, last_n, lo, hi = tl.parse_comment_selectors(
            kind=self.kind,
            ref_id=ref.id,
            ords=sorted(c.ord for c in chunks),
            comment=comment,
            comments=comments,
            last=last,
        )
        return tl.render_comment_view(
            ref, chunks, kind=self.kind, last=last_n, only=only, lo=lo, hi=hi
        )

    # ── tag: gradient guard + STATUS:done event emission ──────────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        prio: int | None = None,
        meta: dict[str, Any] | None = None,
        reason: str | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, list):
            return self._tag_batch(
                id, add=add, remove=remove, prio=prio, meta=meta, reason=reason, **_kw
            )
        prio = _validate_prio(prio)
        # Auto-redirect long ask-user:/halt: values into a chunk so the
        # tag stays a short structured label and the LLM's natural
        # explanation prose lands somewhere queryable. Triggers before
        # the halt/llm guards because those guards inspect the final
        # tag forms.
        if add:
            add, _redirected_chunks = redirect_long_tag_values(
                self.store, ref_id=self._coerce_id(id), tags=add
            )
        # ``PRIO:`` → the canonical prio column (the doable ORDER BY),
        # stripped from the tag set like gripe / quest. A bare ``PRIO:*``
        # in remove clears the column back to the sort-time default. An
        # explicit ``prio=`` kwarg wins over the tag form.
        add, prio_from_tag = split_prio(add)
        clear_prio = False
        if remove:
            kept = [t for t in remove if prio_tag_int(t) is None]
            clear_prio = len(kept) != len(remove)
            remove = kept or None
        if prio is None and prio_from_tag is not None:
            prio = prio_from_tag
        # ``meta=`` is the facet-promotion surface (§M facet
        # normalization): ``rotation_root`` / ``worker_mintable`` /
        # ``schedule`` (owner-only gradient), ``llm_tier`` (the
        # dispatcher's model picker — e.g. a retry swapping tiers),
        # ``budget_usd`` (a per-todo override of the planner
        # guardrails' cost cap, gr332026), and ``fix`` (a code todo's
        # prod-check line, td570413 — ``check_fix_meta`` runs the
        # ``gripe_autoclose`` grammar at write time).
        # ``tag()`` already carries non-tag state via ``prio=``, so this
        # follows the same precedent rather than adding a new verb. It
        # is a closed allowlist (``check_meta_keys_promotable``), not a
        # general meta bag — anything else (``deliver``, ``workspace``,
        # …) has its own validation on the ``put()``/``create()`` path
        # and must not skip it by riding through here unvalidated.
        # ``meta.tier`` → facet booleans first (same translation as
        # ``put()``), so the promotable allowlist and the owner-only
        # facet gate below both see the stored keys.
        meta = guards.normalize_tier_meta(meta)
        guards.check_meta_keys_promotable(meta)
        guards.check_facets_on_tag(meta)
        # ``meta={'llm_tier': None}`` parks the leaf — the same opt-out
        # convention ``put()``'s create and ``edit()``'s meta door
        # already honour (gr639083). It must DELETE the key below, not
        # merge a JSON ``null`` through ``stamp_ref_meta``: dispatch's
        # ``meta ? 'llm_tier'`` existence check reads presence only, so
        # a stored null would still mint plan_ticks.
        clear_llm_tier = (
            isinstance(meta, dict) and "llm_tier" in meta and meta["llm_tier"] is None
        )
        if clear_llm_tier and meta is not None:
            meta = {k: v for k, v in meta.items() if k != "llm_tier"}
        guards.check_llm_tier_meta(meta)
        guards.check_llm_select_meta(meta)
        guards.check_budget_usd_meta(meta)
        guards.check_rung_meta(meta)
        guards.check_fix_meta(meta)
        guards.check_halt_remove(remove=remove)
        # Claim CAS: a worker claiming a leaf whose live lease another
        # handle holds gets a BadInput naming the holder, instead of the
        # pre-lease double-claim (two claimed-by tags coexisting). The
        # lease upkeep itself (expiry stamp, single-holder invariant,
        # release on terminal STATUS) runs in ``_after_tag_mutation``,
        # atomic with the tag write.
        guards.check_claim_takeover(self.store, self._coerce_id(id), add)
        # No STATUS:done from a worker without artifact evidence.
        # Prevents the cheating mode where the LLM marks itself done
        # without producing a file / citation / successful child job.
        guards.check_status_done_artifact(self.store, self._coerce_id(id), add)
        # Layer-3 compile guard — runs latexmk at workspace-root
        # STATUS:done so the LLM can't declare victory on a broken
        # paper. No-op on intermediate leaves (live siblings) or
        # non-tex workspaces. Quietly skips when latexmk isn't on
        # PATH (degrade gracefully on dev hosts).
        from precis.utils.compile_guard import check_workspace_compiles

        check_workspace_compiles(self.store, self._coerce_id(id), add)
        guards.check_executor_tag(add)
        ref_id = self._coerce_id(id)
        # Atomicity (A6): prio/meta below write on their own connections,
        # so every check the base ``tag`` would raise on must fire first —
        # a rejected ``add=`` must not leave a changed prio behind.
        self._resolve_live_ref(ref_id)
        for s in add or []:
            Tag.parse_strict(s, kind=self.kind)
        for s in remove or []:
            Tag.parse_strict(s, kind=self.kind)
        if meta and "schedule" in meta:
            guards.check_schedule_in_meta(dict(meta))
        if prio is not None:
            self.store.set_prio(ref_id, prio)
        elif clear_prio:
            self.store.set_prio(ref_id, None)
        if meta:
            meta_out = dict(meta)
            if "schedule" in meta_out:
                parsed = guards.check_schedule_in_meta(meta_out)
                if parsed is not None:
                    if parsed.at is not None:
                        meta_out["schedule"] = {
                            "at": parsed.at,
                            "catch_up": parsed.catch_up,
                        }
                    else:
                        meta_out["schedule"] = {
                            "cron": parsed.cron,
                            "backfill_missed": parsed.backfill_missed,
                        }
            self.store.stamp_ref_meta(ref_id, meta_out)
        if clear_llm_tier:
            # A real JSONB key deletion — ``stamp_ref_meta``'s
            # ``meta ||`` merge cannot express it (the edit-side park
            # runs the same statement, gr439934).
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE refs SET meta = meta - 'llm_tier', "
                    "updated_at = now() WHERE ref_id = %s",
                    (ref_id,),
                )
        if (
            not add
            and not remove
            and (prio is not None or clear_prio or meta or clear_llm_tier)
        ):
            # Only a PRIO / meta write happened; the base handler would
            # reject an empty ``tag`` call. A bare clear renders as
            # "cleared", not the misreadable "prio=None".
            if prio is not None:
                suffix = f"prio={prio_display(prio)}"
            elif clear_prio:
                suffix = "prio=cleared (back to the default)"
            elif meta:
                suffix = f"meta={meta}"
            else:
                suffix = ""
            if clear_llm_tier:
                suffix += ("; " if suffix else "") + ("meta.llm_tier=cleared (parked)")
            # The prio/meta writes above committed on their own
            # connections; the gr504803 audit row lands right after them.
            self._record_tag_reason(ref_id, reason)
            return Response(body=f"set {suffix} on {self._sense()} id={ref_id}")
        prev_status = next(
            (str(t.value) for t in self.store.tags_for(ref_id) if t.prefix == "STATUS"),
            "open",
        )
        resp = super().tag(id=id, add=add, remove=remove, reason=reason, **_kw)
        # Picks-7d accounting (plan's Accounting section): when a
        # leaf flips to STATUS:done, append a ``status:done`` event
        # with the caller's source. Tag mutations on numeric refs are
        # cheap to detect after-the-fact.
        if add and any(a == "STATUS:done" for a in add):
            ref_id = self._coerce_id(id)
            self.store.append_event(
                ref_id,
                source=guards._caller_source(),
                event="status:done",
            )
        # gr548800: pausing a plan root is a quest-logbook event (the
        # ``plan:<self>`` open tag marks the root the minter tagged).
        if (
            add
            and "STATUS:paused" in add
            and prev_status != "paused"
            and f"plan:{ref_id}"
            in {
                str(t.value)
                for t in self.store.tags_for(ref_id)
                if t.namespace == "open"
            }
        ):
            ref = self._resolve_live_ref(ref_id)
            plan_log.log_plan_event(
                self.store,
                ref_id,
                f"paused — {plan_log.first_line(ref.title)}",
                root=ref_id,
            )
        return resp

    def _after_tag_mutation(
        self,
        ref_id: int,
        added: list[Tag],
        removed: list[Tag],
        *,
        conn: Any,
    ) -> None:
        """Claim-lease upkeep, atomic with the tag write.

        Two invariants (see ``guards.check_claim_takeover`` for the CAS
        half that runs *before* the write):

        * A freshly-(re)added ``claimed-by:<x>`` becomes the *only*
          claim row (other holders' rows — expired, or overridden by an
          owner source — are dropped) and gets its lease stamped:
          ``expires_at = now() + CLAIM_TTL_HOURS``. ``add_tag``'s
          ON CONFLICT path resets ``expires_at`` to NULL on a re-add,
          so the stamp here is what makes re-claiming *extend* the
          lease rather than silently un-lease it.
        * A terminal STATUS (done / won't-do) releases every claim —
          finished leaves don't accumulate stale leases.
        """
        guards.stamp_claim_lease(conn, ref_id, added)
        if any(str(t) in ("STATUS:done", "STATUS:won't-do") for t in added):
            guards.release_claims(conn, ref_id)

    # ── link: reserved virtual rel='parent' is the move surface ───

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int | list[str | int],
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        meta: dict[str, Any] | None = None,
        reason: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Intercept the reserved ``rel='parent'`` relation as a move.

        ``parent`` is a *virtual* relation: it never lands in the
        ``links`` table. Instead it re-points ``refs.parent_id`` via
        :meth:`_reparent`, running the same cycle / depth / owner
        guards as a create-time parent assignment. Every other
        relation falls through to the stored-link machinery in
        :class:`NumericRefHandler`.

        * ``mode='add'``    — move ``id`` under ``target`` (a ``todo:N``).
        * ``mode='remove'`` — detach ``id`` to a root (``parent_id=NULL``).

        Reads round-trip: ``get(kind='todo', id=N, view='links')``
        synthesizes the ``parent`` edge from the column, so a client
        that sets the edge via ``link`` sees it back via ``link``.

        Why a reserved relation, not a new verb or a real ``links``
        row: ``link`` already means "connect A to B" (no new surface
        to learn), and ``refs.parent_id`` stays the single source of
        truth — tree views, the ON DELETE cascade, and every guard
        key off the column; a mirrored links row would duplicate
        state and need a sync trigger.

        ``reason=`` is the kwarg spelling of ``meta={'reason': …}``
        (:func:`merge_link_reason`, gr626896; gr657045 — the verb-level
        ``link`` signature advertises it on every kind). Edge meta can
        ride only a *stored* edge: the virtual ``rel='parent'`` move has
        no ``links`` row to hold it, and a fan-in ``id=[...]`` fans one
        edge shape across many ids — both refuse rather than drop it.
        """
        from precis.utils.link_tag_ops import merge_link_reason

        if rel == _RESERVED_PARENT_REL:
            if meta is not None or reason is not None:
                raise BadInput(
                    "meta=/reason= can't ride rel='parent' — the parent "
                    "edge is virtual (refs.parent_id, no links row to hold it)",
                    next="drop meta=/reason=, or record the why on a stored "
                    "edge (e.g. rel='blocked-by')",
                )
            if isinstance(id, list):
                # Fan-in is for stored edges; a tree move carries the
                # reparent guards (cycle/depth/owner) per id — one call
                # each keeps the error pointed at the offending move.
                raise BadInput(
                    "rel='parent' moves one todo at a time — id= is not a list",
                    next=f"link(kind={self.kind!r}, id=<one id>, target='td<N>', "
                    "rel='parent')",
                )
            return self._reparent(id=id, target=target, mode=mode)
        meta = merge_link_reason(meta, reason, mode=mode)
        if isinstance(id, list):
            if meta is not None:
                raise BadInput(
                    "link meta requires a single todo id",
                    next="link each todo separately with its own meta=/reason=",
                )
            return self._link_batch(id, target=target, mode=mode, rel=rel)
        return self._link(id=id, target=target, mode=mode, rel=rel, meta=meta)

    def _reparent(self, *, id: str | int, target: str | None, mode: str) -> Response:
        """Apply a ``rel='parent'`` move/detach with full tree guards."""
        from precis.utils.link_tag_ops import validate_link_mode
        from precis.utils.link_target import parse_link_target

        validate_link_mode(mode)
        child_id = self._coerce_id(id)
        # Owner-only refs (strategic / tactical) can't be moved by a
        # worker source — same veto as delete. Fires before any target
        # resolution so a worker gets the authority error first.
        guards.check_owner_only_ref(self.store, child_id)

        if mode == "remove":
            # Detach to a root. ``target`` is optional; when given it
            # must name the *current* parent so a stale "remove parent
            # X" can't silently detach from a different parent Y.
            if target is not None:
                claimed = parse_link_target(target, store=self.store)
                current = self._resolve_live_ref(child_id)
                if current.parent_id != claimed.ref_id:
                    raise BadInput(
                        f"todo id={child_id} is not parented under "
                        f"{target!r} (current parent: "
                        f"{'#' + str(current.parent_id) if current.parent_id else 'none'})",
                        next=(
                            "omit target= to detach, or pass the actual current parent"
                        ),
                    )
            self.store.set_parent(child_id, None)
            return Response(body=f"detached {self._sense()} id={child_id} to a root")

        # mode == "add" — move under a new parent.
        if target is None:
            raise BadInput(
                f"link(kind={self.kind!r}, id=..., rel='parent') requires target=",
                next=f"link(kind={self.kind!r}, id={child_id}, target='todo:N', rel='parent')",
            )
        new_parent = parse_link_target(target, store=self.store)
        new_parent_id = new_parent.ref_id
        # a *folder* target is placement, not tree surgery —
        # legal only for strategic roots (folder = where; the
        # scheduling tree stays todo-rooted below it). The kind-aware
        # root predicate (``todo_root_sql``) keeps a folder-parented
        # strategic in every rotation / doable / review query, and the
        # depth walk stops at the folder, so nothing else changes.
        parent_ref = self.store.fetch_refs_by_ids({new_parent_id}).get(new_parent_id)
        if parent_ref is not None and parent_ref.kind == "folder":
            child_ref = self.store.fetch_refs_by_ids({child_id}).get(child_id)
            is_rotation_root = bool(
                child_ref is not None and (child_ref.meta or {}).get("rotation_root")
            )
            if not is_rotation_root:
                raise BadInput(
                    f"only strategic roots can be placed in folders "
                    f"(todo id={child_id} lacks meta.rotation_root)",
                    next=(
                        "move it under a todo instead, or set "
                        "meta.rotation_root=true on the root first"
                    ),
                )
            self.store.set_parent(child_id, new_parent_id)
            child_h = handle_registry.format_handle(self.kind, child_id)
            return Response(body=f"placed {child_h} in folder:{new_parent_id}")
        guards.check_parent_exists(self.store, new_parent_id)
        guards.check_no_cycle(self.store, child_id=child_id, parent_id=new_parent_id)
        guards.check_reparent_depth(
            self.store, child_id=child_id, new_parent_id=new_parent_id
        )
        self.store.set_parent(child_id, new_parent_id)
        child_h = handle_registry.format_handle(self.kind, child_id)
        parent_h = handle_registry.format_handle("todo", new_parent_id)
        return Response(body=f"moved {self._sense()} {child_h} under {parent_h}")

    # ── delete: owner-only guard on strategic / tactical ──────────

    def delete(
        self,
        *,
        id: str | int | None = None,
        args: dict[str, Any] | None = None,
        reason: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput(
                f"delete(kind={self.kind!r}) requires id=",
                next=f"search(kind='{self.kind}', q='<keywords>') for the id, "
                f"then delete(kind={self.kind!r}, id=<N>)",
            )
        if args:
            if "comment" not in args:
                raise BadInput(
                    f"unknown delete args {sorted(args)!r}",
                    next=f"delete(kind='todo', id={id}, args={{'comment': <K>}})",
                )
            ref = self._resolve_live_ref(self._coerce_id(id))
            return tl.delete_comment(
                self.store, ref, kind=self.kind, comment_kind=_COMMENT_KIND, args=args
            )
        ref_id = self._coerce_id(id)
        guards.check_owner_only_ref(self.store, ref_id)
        # Slice 4 footgun: refuse to delete refs that carry a
        # ``meta.builtin`` marker (the Watches umbrella, future
        # seeded folders). The check is on key presence so adding
        # new builtins doesn't need a new guard.
        guards.check_not_builtin(self.store, ref_id)
        # Cascading soft delete: a live child under a deleted parent
        # is invisible to the nursery orphan walk (it needs a live
        # parent chain) yet rots forever as a stuck-doable leaf — the
        # subtree goes with its root. Jobs/findings parented here keep
        # their own lifecycles and stay.
        n_desc = self.store.retire_todo_subtree(ref_id)
        if reason and reason.strip():
            # The ``deleted`` audit row the Gone tombstone read looks
            # for (gr503546); retire_todo_subtree runs its own tx, so
            # this lands right after it.
            self.store.append_event(
                ref_id,
                source="agent",
                event="deleted",
                payload={"reason": reason.strip()},
            )
        suffix = f" (+{n_desc} descendant todos)" if n_desc else ""
        return Response(body=f"deleted {self._sense()} id={ref_id}{suffix}")

    # ── links view: synthesize the virtual parent edge ───────────

    def _render_links_view(
        self,
        ref: Ref,
        *,
        of_kind: str | None = None,
        rel: str | None = None,
        direction: str | None = None,
    ) -> Response:
        """Prepend the virtual ``parent`` edge to the stored-link view.

        ``parent`` lives in the ``refs.parent_id`` column, not the
        ``links`` table, so the base renderer never sees it. We inject
        it as an outbound ``(parent)`` row so a client that set the
        edge via ``link(rel='parent')`` reads it back here in the same
        ``kind:identifier`` form it can round-trip into a future call.
        Children aren't synthesized — ``view='tree'`` already shows the
        full subtree downward. A ``rel=`` filter (gr514335) hides the
        synthesized edge: it is no stored-links row, and ``rel='parent'``
        is a reserved pseudo-rel ``validate_relation`` refuses anyway.
        ``direction='in'`` (gr631571) hides it too — the parent edge is
        outbound.
        """
        base = super()._render_links_view(
            ref, of_kind=of_kind, rel=rel, direction=direction
        )
        if ref.parent_id is None or rel is not None or direction == "in":
            return base
        header, _, rest = base.body.partition("\n")
        rest = rest.lstrip("\n")
        # With a parent edge present the ref is never link-less, so the
        # base "(no links)" placeholder (and its add-a-link hint) is
        # dropped in favour of the synthesized section.
        if rest.startswith(("(no links)", "(no links to kind")):
            rest = ""
        # The parent may be a todo (tree) or a folder (placement) —
        # render the actual kind so the handle round-trips.
        parent_ref = self.store.fetch_refs_by_ids({ref.parent_id}).get(ref.parent_id)
        parent_kind = parent_ref.kind if parent_ref is not None else "todo"
        if of_kind is not None and parent_kind != of_kind:
            return base
        # gr555998 round 3: a parent this surface may not read collapses
        # to the same opaque placeholder the stored-link rows print.
        from precis.utils.surface_scope import masked_ref_placeholder

        parent_label = masked_ref_placeholder(parent_ref) or (
            f"{parent_kind}:{ref.parent_id}"
        )
        parts = [
            header,
            "",
            "## parent",
            f"→ {parent_label}  (parent)",
        ]
        if rest:
            parts.extend(["", rest])
        return Response(body="\n".join(parts))

    # ── single-ref render: include parent header when present ─────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        out = self._card_lines(ref, tags)
        comments = self._render_comments(ref)
        if comments:
            out.append("")
            out.append(comments)
        return "\n".join(out)

    def _render_body_view(self, ref: Ref, tags: list[Tag]) -> str:
        """``view='body'`` — the card minus its comment timeline:
        header, title, the *untruncated* ``todo_body``, tags. The
        bounded read the cut/fitted markers name when comments and
        links push the whole card over budget (gr554136)."""
        return "\n".join(self._card_lines(ref, tags))

    def _card_lines(self, ref: Ref, tags: list[Tag]) -> list[str]:
        """Header + title + details body + tags — everything a todo owns
        itself, before the comment/links/ancestry appendages."""
        out = [f"# {self._sense()} {ref.id}", ""]
        if ref.parent_id is not None:
            out.append(f"parent: #{ref.parent_id}")
        if ref.prio is not None:
            out.append(f"prio: {prio_display(ref.prio)}")
        if ref.parent_id is not None or ref.prio is not None:
            out.append("")
        out.append(ref.title)
        # Optional details body (todo_body chunk), when the todo has one.
        body = self._body_text(ref)
        if body:
            out.append("")
            out.append(body)
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return out

    def _body_text(self, ref: Ref) -> str | None:
        """The todo's optional details body from its ``todo_body`` chunk,
        or ``None`` when it has none (the common case)."""
        for block in self.store.chunks.list_chunks_for_ref(ref.id):
            if block.chunk_kind == _BODY_KIND:
                return block.text
        return None

    # ── create-ack: surface parent_id if set ──────────────────────

    def _render_create_ack(self, ref_id: int) -> Response:
        state = _PUT_STATE.get()
        parent = state.parent_id
        # surface the universal handle (``td<id>``) in the ack.
        handle = handle_registry.try_format(self.kind, ref_id) or f"id={ref_id}"
        tags = self.store.tags_for(ref_id)
        status = next(
            (t.value for t in tags if t.namespace == "closed" and t.prefix == "STATUS"),
            "open",
        )
        body = f"created {self.kind} {handle} (STATUS:{status})"
        if parent is not None:
            body += f" under {handle_registry.format_handle('todo', parent)}"
        if state.prio is not None:
            body += f", prio={prio_display(state.prio)}"
        body += "."
        executor = (state.meta or {}).get("executor")
        if executor:
            # gr453861: the minter is one pass in a sequential SYS cycle, so
            # the job child is not instant — say so in the write receipt.
            body += (
                f" executor={executor}: the minter picks this up on its next"
                " pass (minutes, up to ~20 min on a busy cluster); a child"
                " job appears under this todo with a `job-minted` event."
            )
        if state.stamped_default_tier:
            # The parented-write default armed billed compute; a write
            # receipt must say so (silent side effects erode agent trust).
            body += (
                " meta.llm_tier='opus' stamped (parented default) — the"
                " dispatcher will auto-run this leaf as a plan_tick; run"
                f" edit(kind={self.kind!r}, id={ref_id},"
                " meta={'llm_tier': None}) to park it instead, or pass"
                " manual=True on a create to skip the stamp."
            )
        elif state.manual_no_stamp:
            body += (
                " manual=True: no meta.llm_tier stamped (parented default"
                " suppressed) — the dispatcher will not auto-run this leaf."
            )
        if state.mint_hold:
            body += (
                f" minted held ({plan_nodes.MINT_HOLD_TAG}) — server added "
                "it unconditionally inside a live plan; remove the tag "
                "once its blocked-by links are settled."
            )
        ask_blocks = guards.check_ask_meta(state.meta)
        if ask_blocks is not None:
            body += (
                f" blocked-by link written: {handle_registry.format_handle('todo', ask_blocks)}"
                f" now waits on {handle}; the asks view lists this as a showstopper."
            )
        body += render_next_section(
            [
                *(
                    [
                        (
                            f"tag(kind={self.kind!r}, id={ref_id}, add=['STATUS:doing'])",
                            "start work on this todo",
                        )
                    ]
                    if status == "open"
                    and not any(
                        str(t) == "halt" or str(t).startswith("halt:") for t in tags
                    )
                    else []
                ),
                (
                    f"delete(kind={self.kind!r}, id={ref_id})",
                    "delete this todo",
                ),
                (
                    f"search(kind={self.kind!r}, view='active')",
                    "list open work (open/doing/blocked/paused)",
                ),
            ]
        )
        return Response(body=body, ref_id=ref_id)
