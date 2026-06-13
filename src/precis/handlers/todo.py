"""TodoHandler — task / action items with status transitions and tree shape.

Numeric-id ref kind. Builds on the shared :class:`NumericRefHandler`
shape with three first-class extensions:

1. **`STATUS:` closed-prefix tag** (v1 lifecycle, unchanged):
   ``open|doing|blocked|done|won't-do|paused|auto-timeout``.
   Every put-create starts at ``STATUS:open``.

2. **Hierarchical tree** (Slice 1 of ``docs/design/todo-tree-plan.md``):
   each todo carries an optional ``parent_id`` pointing at another
   todo. Branches are outcomes (the first line reads as "what does
   done look like"); leaves are next physical actions. Reto owns the
   top tiers via the ``level:strategic|tactical`` tag gradient; the
   guards in :mod:`precis.handlers._todo_guards` enforce who can
   write what.

3. **Tree-aware views** — ``roots``, ``strategic``, ``tree``,
   ``doable``, ``waiting``, ``blocked``, ``asking-reto``. Renderers
   live in :mod:`precis.handlers._todo_views`; this module routes.

List views via ``id='/<view>'`` (legacy flat surface):
    /recent /open /doing /blocked /done /queue
Tree views via ``view='<name>'`` on search / get:
    search(kind='todo', view='roots'|'strategic'|'doable'|'waiting'
                              |'blocked'|'asking-reto')
    get(kind='todo', id=N, view='tree')

A ``get(kind='todo', id=N)`` response always includes the walk-up
ancestry chain when the ref isn't a root — depth ≤ 10, cheap, no
caching needed.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput, Unsupported
from precis.handlers import _todo_guards as guards
from precis.handlers import _todo_views as views
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.utils.next_block import render_next_section

#: View names that ``search(kind='todo', view=...)`` accepts. Each
#: name routes to a renderer in :mod:`._todo_views`.
_TREE_SEARCH_VIEWS: frozenset[str] = frozenset(
    {"roots", "strategic", "doable", "waiting", "blocked", "asking-reto"}
)

#: View names that ``get(kind='todo', id=N, view=...)`` accepts on
#: top of the base class's ``links`` / ``log`` views.
_TREE_GET_VIEWS: frozenset[str] = frozenset({"tree"})


class TodoHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="todo",
        title="Todo",
        description=(
            "Task / action item. Numeric id assigned on create. "
            "Status tracked via STATUS:open|doing|blocked|done|won't-do|paused|"
            "auto-timeout. Optional parent_id wires the todo into the "
            "hierarchical task tree (see precis-tasks-help)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
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

    def get(  # type: ignore[override]
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

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if view is not None and view in _TREE_SEARCH_VIEWS:
            if view == "roots":
                return views.render_roots(self.store)
            if view == "strategic":
                return views.render_strategic(self.store)
            if view == "doable":
                under = None
                if args is not None and "under" in args:
                    under_raw = args["under"]
                    try:
                        under = int(under_raw)
                    except (TypeError, ValueError) as exc:
                        raise BadInput(
                            f"args.under must be an integer, got {under_raw!r}",
                            next=(
                                "search(kind='todo', view='doable', args={'under': N})"
                            ),
                        ) from exc
                return views.render_doable(
                    self.store, under=under, limit=page_size or 20
                )
            if view == "waiting":
                return views.render_waiting(self.store)
            if view == "blocked":
                return views.render_blocked(self.store)
            if view == "asking-reto":
                return views.render_asking_reto(self.store)
        if view is not None and view not in _TREE_SEARCH_VIEWS:
            raise Unsupported(
                f"unknown view {view!r} for kind={self.kind!r} search",
                options=sorted(_TREE_SEARCH_VIEWS),
                next=(
                    f"views available: {sorted(_TREE_SEARCH_VIEWS)}; "
                    "see precis-tasks-help"
                ),
            )
        return super().search(q=q, tags=tags, page_size=page_size, **_kw)

    # ── put: parent_id + level guard at create ────────────────────

    def put(  # type: ignore[override]
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
        **_kw: Any,
    ) -> Response:
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
        guards.check_level_tags_on_create(tags)
        # Delegate to the base put for D6 guardrails (id=/mode=/etc.
        # rejection, tag validation, link target resolution, atomic
        # tx). It calls back into ``_create``, which we override to
        # plumb ``parent_id`` through to the store layer.
        self._pending_parent_id = parent_int
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

    # Per-call slot for plumbing parent_id from ``put`` into ``_create``
    # without changing the base class's signature. Initialised on
    # first use.
    _pending_parent_id: int | None = None

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
        relation = validate_relation(rel)

        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text,
                meta={},
                auto_refresh_days=auto_refresh_days,
                parent_id=self._pending_parent_id,
                conn=conn,
            )
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
                self.store.upsert_card_combined(ref.id, text, conn=conn)
        return self._render_create_ack(ref.id)

    # ── tag: gradient guard + STATUS:done event emission ──────────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        guards.check_level_tags_on_tag(add=add, remove=remove)
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

    # ── delete: owner-only guard on strategic / tactical ──────────

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
        ref_id = self._coerce_id(id)
        guards.check_owner_only_ref(self.store, ref_id)
        return super().delete(id=id, **_kw)

    # ── single-ref render: include parent header when present ─────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        out = [f"# {self._sense()} {ref.id}", ""]
        if ref.parent_id is not None:
            out.append(f"parent: #{ref.parent_id}")
            out.append("")
        out.append(ref.title)
        if tags:
            out.append("")
            out.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(out)

    # ── create-ack: surface parent_id if set ──────────────────────

    def _render_create_ack(self, ref_id: int) -> Response:
        parent = self._pending_parent_id
        body = f"created {self.kind} id={ref_id} (STATUS:open)"
        if parent is not None:
            body += f" under #{parent}"
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
        return Response(body=body)


def _status_of(tags: list) -> str:  # type: ignore[type-arg]
    """Return the STATUS: value from a tag list, or ``'open'`` as default."""
    for t in tags:
        if str(t).startswith("STATUS:"):
            return str(t)[len("STATUS:") :]
    return "open"
