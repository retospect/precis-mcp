"""TodoHandler — task / action items with status transitions.

Numeric-id ref kind. The first-class semantic on top of the shared
:class:`NumericRefHandler` shape is a `STATUS:` closed-prefix tag with
the v1-canonical vocabulary:

    open      — newly created, no work started
    doing     — currently in progress
    blocked   — waiting on something external
    done      — completed (excluded from default list view)
    won't-do  — abandoned (excluded from default list view)

Every put-create gets ``STATUS:open`` automatically. The agent can
override by passing ``tags=['STATUS:doing']`` etc.

List views (id starts with `/`):
    /recent  — most recent 20 (any status, default)
    /open    — open + doing + blocked (the agent's actual queue)
    /done    — completed items
"""

from __future__ import annotations

from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils.next_block import render_next_section


class TodoHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="todo",
        title="Todo",
        description=(
            "Task / action item. Numeric id assigned on create. "
            "Status tracked via STATUS:open|doing|blocked|done|won't-do."
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
    _OPEN_STATUSES: ClassVar[frozenset[str]] = frozenset({"open", "doing", "blocked"})

    def _supported_list_views(self) -> tuple[str, ...]:
        # Surfaced via the unknown-list-view error so a 7B caller
        # who tries ``id='/all'`` gets the actual recovery options
        # rather than a dangling ``see precis-todo-help`` hint.
        return ("recent", "open", "doing", "blocked", "done", "queue")

    def _list_view(self, view: str) -> Response | None:
        # Default behaviour for /recent / "" stays in the base class.
        if view in ("open", "doing", "blocked", "done"):
            return self._render_status_list(view)
        if view == "queue":  # alias
            return self._render_status_list("open")
        return super()._list_view(view)

    def _render_status_list(self, status_filter: str) -> Response:
        """Render todos filtered by STATUS: tag.

        ``status_filter='open'`` is the union of open + doing + blocked
        (everything that's not done / won't-do). Other filters match
        the literal status.
        """
        # Pull recent refs and filter in Python — todo volume is small
        # so a server-side tag join isn't worth the complexity yet. If
        # this gets slow we can move to a Store.search_refs_by_tag()
        # primitive.
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

    def _render_create_ack(self, ref_id: int) -> Response:
        # Unified shape (broad-pass finding #9): "created {kind} id=N
        # (STATUS:open).\nNext: ..." — uppercase axis form, kwarg
        # spelling, TOON Next: trailer.
        body = f"created {self.kind} id={ref_id} (STATUS:open)."
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
