"""TodoHandler — task / action items with status transitions and tree shape.

Subsystem architecture (facet model, job lanes, planner coroutines,
guardrails): :mod:`precis.handlers` package docstring.

Numeric-id ref kind. Builds on the shared :class:`NumericRefHandler`
shape with four first-class extensions:

1. **`STATUS:` closed-prefix tag** (v1 lifecycle, unchanged):
   ``open|doing|blocked|done|won't-do|paused|auto-timeout``.
   Every put-create starts at ``STATUS:open``.

2. **Hierarchical tree** (Slice 1 of ``docs/backlog/todo-tree-plan.md``):
   each todo carries an optional ``parent_id`` pointing at another
   todo. Branches are outcomes (the first line reads as "what does
   done look like"); leaves are next physical actions. The owner owns the
   top tiers via the ``meta.rotation_root`` / ``meta.worker_mintable``
   facet gradient (§M facet normalization, migration 0102 — replaces the
   old ``level:strategic|tactical`` tags); the guards in
   :mod:`precis.handlers._todo_guards` enforce who can write what.

3. **Tree-aware views** — search views ``roots``, ``projects``,
   ``strategic``, ``doable``, ``waiting``, ``blocked``, ``ask-user``,
   ``attention`` (the :class:`TodoView` closed vocabulary; the
   :data:`_TREE_SEARCH_VIEWS` dispatch table maps each to a renderer in
   :mod:`precis.handlers._todo_views`), plus ``view='tree'`` on ``get``.
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

List views via ``id='/<view>'`` (legacy flat surface):
    /recent /open /doing /blocked /done /queue
Tree views via ``view='<name>'`` on search / get:
    search(kind='todo', view='roots'|'projects'|'strategic'|'doable'
                              |'waiting'|'blocked'|'ask-user'|'attention')
    get(kind='todo', id=N, view='tree')

A ``get(kind='todo', id=N)`` response always includes the walk-up
ancestry chain when the ref isn't a root — depth ≤ 10, cheap, no
caching needed.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput, Unsupported
from precis.handlers import _todo_guards as guards
from precis.handlers import _todo_views as views
from precis.handlers._numeric_ref import NumericRefHandler
from precis.handlers._tag_redirect import redirect_long_tag_values
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store.protocols import PoolStore
    from precis.store.store import Store

#: Chunk kind holding a todo's optional details body (parallel to
#: ``memory_body``, migration 0050). The title stays in ``refs.title``
#: (a good header already); the body is extra prose read on ``get`` and
#: embedded + keyworded for free. Additive — most todos never set one.
_BODY_KIND = "todo_body"


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


def _view_doable(store: Store, args: dict[str, Any] | None, page_size: int) -> Response:
    """``view='doable'`` — the one tree view that reads ``args`` / paging.

    Pulled out of the dispatch table so the table stays a flat
    ``name → renderer`` map; the arg-less views are one-line lambdas.
    """
    under = None
    if args is not None and "under" in args:
        under_raw = args["under"]
        try:
            under = int(under_raw)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"args.under must be an integer, got {under_raw!r}",
                next="search(kind='todo', view='doable', args={'under': N})",
            ) from exc
    return views.render_doable(store, under=under, limit=page_size or 20)


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
}

#: Single-source-of-truth guard: every view in the vocabulary must have
#: a renderer. Drift (a member added to the enum but not wired here, or
#: vice-versa) fails at import, not when a user first hits the view.
assert set(_TREE_SEARCH_VIEWS) == set(TodoView), (
    "TodoView members and _TREE_SEARCH_VIEWS keys diverged: "
    f"{set(TodoView) ^ set(_TREE_SEARCH_VIEWS)}"
)

#: View names that ``get(kind='todo', id=N, view=...)`` accepts on
#: top of the base class's ``links`` / ``log`` views.
_TREE_GET_VIEWS: frozenset[str] = frozenset({"tree"})

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


def _validate_prio(prio: int | None) -> int | None:
    """Range-check ``prio`` (1..10) at the handler boundary.

    Returns ``prio`` on success (None passes through). Raises
    :class:`BadInput` with the catalogue on out-of-range / non-int
    input — the DB CHECK would catch it too, but we want the message
    to mention ``put(prio=N)`` rather than ``check_violation``.
    """
    if prio is None:
        return None
    if not isinstance(prio, int) or isinstance(prio, bool):
        raise BadInput(
            f"prio must be an int 1..10, got {type(prio).__name__} {prio!r}",
            next="prio=1 (chat / preempt), prio=2 (cron), prio=5 (default)",
        )
    if prio < 1 or prio > 10:
        raise BadInput(
            f"prio out of range: {prio} (must be 1..10)",
            next="prio=1 preempts strategic rotation; 3..10 ride the 1/N share",
        )
    return prio


class TodoHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="todo",
        title="Todo",
        description=(
            "Task / action item. Numeric id assigned on create. "
            "Status tracked via STATUS:open|doing|blocked|done|won't-do|paused|"
            "auto-timeout. Optional parent_id wires the todo into the "
            "hierarchical todo tree (see precis-todo-tree-help)."
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
    )

    kind: ClassVar[str] = "todo"
    sense: ClassVar[str] = "todo"
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:open",)

    # Statuses that count as "open work" (i.e. on the agent's queue).
    _OPEN_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"open", "doing", "blocked", "paused", "auto-timeout"}
    )

    # ── list view dispatch (id='/<view>') ─────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "open", "doing", "blocked", "done", "queue")

    def _search_view_names(self) -> frozenset[str]:
        # The tree-aware views live on search(), not get(); passing one to
        # get(view=…) without an id gets a redirect hint (gr48523). Derived
        # from the dispatch table so it stays in sync with TodoView.
        return frozenset(str(v) for v in _TREE_SEARCH_VIEWS)

    def _list_view(self, view: str) -> Response | None:
        # Default behaviour for /recent / "" stays in the base class.
        if view in ("open", "doing", "blocked", "done"):
            return self._render_status_list(view)
        if view == "queue":  # alias
            return self._render_status_list("open")
        return super()._list_view(view)

    def _render_status_list(self, status_filter: str) -> Response:
        """Render todos filtered by STATUS: tag (legacy flat surface).

        ``status_filter='open'`` is the union of open + doing +
        blocked + paused + auto-timeout (everything that's not
        terminally closed). Other filters match the literal status.
        """
        refs = self.store.list_refs(kind=self.kind, limit=200)
        if status_filter == "open":
            wanted = self._OPEN_STATUSES
        else:
            wanted = frozenset({status_filter})

        kept: list[tuple[int, str, str]] = []
        for r in refs:
            tags = self.store.tags_for(r.id)
            status = _status_of(tags)
            if status in wanted:
                kept.append((r.id, status, r.title))

        if not kept:
            body = f"no todos with status in {sorted(wanted)}"
            body += render_next_section(
                [
                    ("get(kind='todo', id='/recent')", "list todos in any state"),
                    ("put(kind='todo', text='new task')", "create a new todo"),
                ]
            )
            return Response(body=body)

        lines = [f"# {len(kept)} todo (status: {status_filter})"]
        for ref_id, status, title in kept:
            preview = (title[:80] + "…") if len(title) > 80 else title
            lines.append(f"  {ref_id:>4}  [{status:<7}]  {preview}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='todo', id=N)", "read full todo + tags"),
                (
                    "tag(kind='todo', id=N, add=['STATUS:done'])",
                    "mark a todo done",
                ),
                ("put(kind='todo', text='new task')", "create a new todo"),
            ]
        )
        return Response(body=body)

    # ── get: single-ref ancestry + tree view ──────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
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
        if view is not None and view in _TREE_GET_VIEWS:
            # Future tree-flavoured get views land here.
            raise Unsupported(
                f"view {view!r} is registered but has no renderer wired",
                options=sorted(_TREE_GET_VIEWS),
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
                return resp
            extra = views.render_ancestry_section(self.store, ref_id)
            if extra:
                resp = Response(body=resp.body + "\n" + extra)
        return resp

    # ── search: tree-aware view router ────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if view is not None:
            try:
                view_enum = TodoView(view)
            except ValueError:
                available = sorted(str(v) for v in TodoView)
                raise Unsupported(
                    f"unknown view {view!r} for kind={self.kind!r} search",
                    options=available,
                    next=f"views available: {available}; see precis-todo-tree-help",
                ) from None
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
        **_kw: Any,
    ) -> Response:
        prio = _validate_prio(prio)
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
        guards.check_facets_on_create(meta)
        guards.check_llm_tier_meta(meta)
        guards.check_llm_select_meta(meta)
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
        # when the caller already chose a tier / executor, or it's a
        # recurring umbrella (``meta.schedule`` set).
        if (
            id is None
            and parent_int is not None
            and not (isinstance(meta, dict) and "llm_tier" in meta)
            and not (isinstance(meta, dict) and meta.get("executor"))
            and not (isinstance(meta, dict) and "schedule" in meta)
        ):
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
        # Delegate to the base put for D6 guardrails (id=/mode=/etc.
        # rejection, tag validation, link target resolution, atomic
        # tx). It calls back into ``_create``, which we override to
        # plumb ``parent_id``, ``meta``, and ``prio`` through to the
        # store layer.
        self._pending_parent_id = parent_int
        self._pending_meta = meta
        self._pending_prio = prio
        self._pending_body = (
            body.strip() if isinstance(body, str) and body.strip() else None
        )
        try:
            return super().put(
                text=text,
                mode=mode,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
                auto_refresh_days=auto_refresh_days,
            )
        finally:
            # Always clear so a follow-up put without parent_id can't
            # accidentally inherit the prior call's value.
            self._pending_parent_id = None
            self._pending_meta = None
            self._pending_prio = None
            self._pending_body = None

    # Per-call slots for plumbing parent_id / meta / prio / body from ``put``
    # into ``_create`` without changing the base class's signature.
    _pending_parent_id: int | None = None
    _pending_meta: dict[str, Any] | None = None
    _pending_prio: int | None = None
    _pending_body: str | None = None

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
    ) -> Response:
        # Mirror of NumericRefHandler._create, with parent_id wired
        # through. We can't just call super() because the base class
        # doesn't accept ``parent_id``; rewriting the body keeps the
        # tx boundary intact.
        from precis.handlers._link_tag_ops import validate_relation
        from precis.handlers._link_target import parse_link_target

        if text is None or not text.strip():
            raise BadInput(
                f"creating a {self._sense()} requires text=",
                next=f"put(kind={self.kind!r}, text='your content')",
            )
        target = parse_link_target(link, store=self.store) if link is not None else None
        relation = validate_relation(rel, store=self.store)

        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text,
                meta=self._pending_meta or {},
                auto_refresh_days=auto_refresh_days,
                parent_id=self._pending_parent_id,
                prio=self._pending_prio,
                conn=conn,
            )
            # Optional details body → a todo_body chunk at pos 0, written
            # *before* the tag-overflow redirect (which appends at the next
            # free ord). Additive: the title stays in refs.title; the
            # body is extra prose, embedded + keyworded by the workers.
            if self._pending_body:
                self.store.chunks.insert_chunks(
                    ref.id,
                    [
                        ChunkInsert(
                            ord=0,
                            text=self._pending_body,
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
            if self.emits_card:
                self.store.chunks.upsert_card_combined(ref.id, text, conn=conn)
        # Soft reminder: a rotation_root todo with no auto-run signal
        # (meta.llm_tier / executor:* tag / meta.executor) will never be
        # picked up by the dispatch worker, so it spawns no children and
        # the planner brief in its body is inert. Non-breaking HintBus
        # tip — the owner may legitimately intend to drive it by hand.
        if guards.strategic_lacks_auto_run(self._pending_meta, all_tag_strs):
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
        return self._render_create_ack(ref.id)

    # ── edit: in-place text rewrite (polish the wording) ──────────

    def edit(
        self,
        *,
        id: str | int | None = None,
        mode: str = "replace",
        text: str | None = None,
        body: str | None = None,
        dry_run: bool | str | None = None,
        **_kw: Any,
    ) -> Response:
        """In-place rewrite of a todo's title and/or details body.

        ``text=`` rewrites the title (``refs.title``); ``body=`` sets or
        rewrites the optional details body (a ``todo_body`` chunk). Pass
        either or both. Same id; parent, links, and tags all stay attached —
        the old text lands in ``ref_events`` as a ``body_replaced`` row
        (``view='log'``). Owner-only on strategic / tactical nodes, the same
        authority veto as delete / reparent. Distinct from delete + re-put,
        which would break every inbound edge and the tree position.

        ``dry_run=True`` previews the replacement without writing — the
        tool-level contract every editable kind must honour (a silent
        write on ``dry_run`` is data loss).
        """
        if id is None:
            raise BadInput(
                "edit(kind='todo') requires id=",
                next="edit(kind='todo', id=N, mode='replace', text='new text')",
            )
        if mode != "replace":
            raise BadInput(
                f"edit(kind='todo') only supports mode='replace', got {mode!r}",
                next="edit(kind='todo', id=N, mode='replace', text='new text')",
            )
        has_text = text is not None and text.strip()
        has_body = body is not None and body.strip()
        if not has_text and not has_body:
            raise BadInput(
                "edit(kind='todo', mode='replace') requires text= and/or body=",
                next="edit(kind='todo', id=N, mode='replace', text='new text')",
            )
        ref_id = self._coerce_id(id)
        guards.check_owner_only_ref(self.store, ref_id)
        ref = self._resolve_live_ref(ref_id)
        if dry_run:
            preview: list[str] = []
            if has_text:
                assert text is not None
                old = ref.title or ""
                preview.append(
                    f"title: {old!r} → {text!r} "
                    f"({len(old.split())} → {len(text.split())} words)"
                )
            if has_body:
                preview.append("details body would be replaced")
            return Response(
                body=(
                    f"dry-run (no write) — would replace {' + '.join(preview)} "
                    f"of todo id={ref.id}."
                )
            )
        old_text: str | None = None
        with self.store.tx() as conn:
            if has_text:
                assert text is not None
                old_text = self.store.replace_ref_text(
                    ref.id, text, source=guards._caller_source(), conn=conn
                )
                if self.emits_card:
                    self.store.chunks.upsert_card_combined(ref.id, text, conn=conn)
            if has_body:
                assert body is not None
                self.store.chunks.replace_body_chunk(
                    ref.id,
                    body,
                    chunk_kind=_BODY_KIND,
                    source=guards._caller_source(),
                    conn=conn,
                )
        parts: list[str] = []
        if has_text:
            assert text is not None
            old_words = len((old_text or "").split())
            new_words = len(text.split())
            parts.append(f"title ({old_words} → {new_words} words)")
        if has_body:
            parts.append("details body")
        return Response(
            body=(
                f"replaced {' + '.join(parts)} of todo id={ref.id}. "
                "view='log' for the full diff."
            )
        )

    # ── tag: gradient guard + STATUS:done event emission ──────────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        prio: int | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
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
        # ``meta=`` is the facet-promotion surface (§M facet
        # normalization): ``rotation_root`` / ``worker_mintable`` /
        # ``schedule`` (owner-only gradient) and ``llm_tier`` (the
        # dispatcher's model picker — e.g. a retry swapping tiers).
        # ``tag()`` already carries non-tag state via ``prio=``, so this
        # follows the same precedent rather than adding a new verb. It
        # is a closed allowlist (``check_meta_keys_promotable``), not a
        # general meta bag — anything else (``deliver``, ``workspace``,
        # …) has its own validation on the ``put()``/``create()`` path
        # and must not skip it by riding through here unvalidated.
        guards.check_meta_keys_promotable(meta)
        guards.check_facets_on_tag(meta)
        guards.check_llm_tier_meta(meta)
        guards.check_llm_select_meta(meta)
        guards.check_halt_remove(remove=remove)
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
        if prio is not None:
            self.store.set_prio(ref_id, prio)
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
        if not add and not remove and (prio is not None or meta):
            # Only a PRIO / meta write happened; the base handler would
            # reject an empty ``tag`` call.
            suffix = f"prio={prio}" if prio is not None else f"meta={meta}"
            return Response(body=f"set {suffix} on {self._sense()} id={ref_id}")
        resp = super().tag(id=id, add=add, remove=remove, **_kw)
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
        return resp

    # ── link: reserved virtual rel='parent' is the move surface ───

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
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
        """
        if rel == _RESERVED_PARENT_REL:
            return self._reparent(id=id, target=target, mode=mode)
        return super().link(id=id, target=target, mode=mode, rel=rel, **_kw)

    def _reparent(self, *, id: str | int, target: str | None, mode: str) -> Response:
        """Apply a ``rel='parent'`` move/detach with full tree guards."""
        from precis.handlers._link_tag_ops import validate_link_mode
        from precis.handlers._link_target import parse_link_target

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

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
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
        suffix = f" (+{n_desc} descendant todos)" if n_desc else ""
        return Response(body=f"deleted {self._sense()} id={ref_id}{suffix}")

    # ── links view: synthesize the virtual parent edge ───────────

    def _render_links_view(self, ref: Ref) -> Response:
        """Prepend the virtual ``parent`` edge to the stored-link view.

        ``parent`` lives in the ``refs.parent_id`` column, not the
        ``links`` table, so the base renderer never sees it. We inject
        it as an outbound ``(parent)`` row so a client that set the
        edge via ``link(rel='parent')`` reads it back here in the same
        ``kind:identifier`` form it can round-trip into a future call.
        Children aren't synthesized — ``view='tree'`` already shows the
        full subtree downward.
        """
        base = super()._render_links_view(ref)
        if ref.parent_id is None:
            return base
        header = f"# {self._sense()} {ref.id} - links"
        rest = base.body
        if rest.startswith(header):
            rest = rest[len(header) :].lstrip("\n")
        # With a parent edge present the ref is never link-less, so the
        # base "(no links)" placeholder (and its add-a-link hint) is
        # dropped in favour of the synthesized section.
        if rest.startswith("(no links)"):
            rest = ""
        # The parent may be a todo (tree) or a folder (placement) —
        # render the actual kind so the handle round-trips.
        parent_ref = self.store.fetch_refs_by_ids({ref.parent_id}).get(ref.parent_id)
        parent_kind = parent_ref.kind if parent_ref is not None else "todo"
        parts = [
            header,
            "",
            "## parent",
            f"→ {parent_kind}:{ref.parent_id}  (parent)",
        ]
        if rest:
            parts.extend(["", rest])
        return Response(body="\n".join(parts))

    # ── single-ref render: include parent header when present ─────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        out = [f"# {self._sense()} {ref.id}", ""]
        if ref.parent_id is not None:
            out.append(f"parent: #{ref.parent_id}")
        if ref.prio is not None:
            out.append(f"prio: {ref.prio}")
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
        return "\n".join(out)

    def _body_text(self, ref: Ref) -> str | None:
        """The todo's optional details body from its ``todo_body`` chunk,
        or ``None`` when it has none (the common case)."""
        for block in self.store.chunks.list_chunks_for_ref(ref.id):
            if block.chunk_kind == _BODY_KIND:
                return block.text
        return None

    # ── create-ack: surface parent_id if set ──────────────────────

    def _render_create_ack(self, ref_id: int) -> Response:
        parent = self._pending_parent_id
        # surface the universal handle (``td<id>``) in the ack.
        handle = handle_registry.try_format(self.kind, ref_id) or f"id={ref_id}"
        body = f"created {self.kind} {handle} (STATUS:open)"
        if parent is not None:
            body += f" under {handle_registry.format_handle('todo', parent)}"
        body += "."
        body += render_next_section(
            [
                (
                    f"tag(kind={self.kind!r}, id={ref_id}, add=['STATUS:doing'])",
                    "start work on this todo",
                ),
                (
                    f"delete(kind={self.kind!r}, id={ref_id})",
                    "delete this todo",
                ),
                (f"get(kind={self.kind!r}, id='/open')", "list open todos"),
            ]
        )
        return Response(body=body, ref_id=ref_id)


def _status_of(tags: list) -> str:
    """Return the STATUS: value from a tag list, or ``'open'`` as default."""
    for t in tags:
        if str(t).startswith("STATUS:"):
            return str(t)[len("STATUS:") :]
    return "open"
