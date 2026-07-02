"""FolderHandler — the organizational container kind (ADR 0045).

A folder is a plain numeric ref: ``title`` is the name, children are
the live refs whose ``parent_id`` points at it — the same column the
todo tree uses (migration 0013 put ``parent_id`` on every ref; only
todo used it until ADR 0045). No new tables: subtree reads are a
recursive CTE over the indexed column, a move is one column write.

Containment is single-parent and shallow by policy (1-2 levels,
artifact kinds only — ``KindSpec.role == 'artifact'``). Corpus kinds
(paper, cfp) keep their own discovery layer; stream kinds (memory,
alert, job, …) reach folders only by promotion into an authored note.

The placement surface is the reserved virtual ``parent`` relation
(ADR 0027, generalized): each placeable handler intercepts
``rel='parent'`` and routes to :mod:`precis.handlers._placement`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.handlers._placement import (
    RESERVED_PARENT_REL,
    place_ref,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

# Folder trees are shallow by policy; the walk cap is a backstop, not
# a promise. Matches the todo tree's descent cap.
_TREE_DEPTH_CAP = 10


class FolderHandler(NumericRefHandler):
    """Numeric-ref handler for ``kind='folder'``."""

    kind: ClassVar[str] = "folder"
    sense: ClassVar[str] = "folder"

    spec: ClassVar[KindSpec] = KindSpec(
        kind="folder",
        title="Folder",
        description=(
            "Organizational container (ADR 0045) for authored artifacts "
            "(draft, structure, cad, todo roots, other folders). "
            "put(text='<name>') creates one (nest it with link(id=N, "
            "target='folder:M', rel='parent')); get lists the folder tree; "
            "get(id=N) shows path + "
            "contents; view='tree' renders the whole subtree. Place an "
            "artifact with link(kind='<its kind>', id=..., target='folder:N', "
            "rel='parent'); mode='remove' unfiles it. delete refuses while "
            "the folder has live contents. Folders organize what you MAKE - "
            "papers, memories, and alerts stay out. Keep them shallow. "
            "See precis-folder-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        role="artifact",
        views=("tree", "links", "log", "raw"),
    )

    # ── get: layer view='tree' over the base views ─────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **kw: Any,
    ) -> Response:
        if view == "tree":
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_tree(ref)
        return super().get(id=id, view=view, **kw)

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(self.spec.views)

    # ── link: rel='parent' nests this folder in another ───────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        if rel == RESERVED_PARENT_REL:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return place_ref(
                self.store, kind=self.kind, ref=ref, target=target, mode=mode
            )
        return super().link(id=id, target=target, mode=mode, rel=rel, **_kw)

    # ── edit: rename ───────────────────────────────────────────────

    def edit(  # type: ignore[override]
        self, *, id: str | int, text: str | None = None, **_kw: Any
    ) -> Response:
        """Rename the folder — the title *is* the name; contents are
        untouched (children key on ``parent_id``, not the name)."""
        ref_id = self._coerce_id(id)
        self._resolve_live_ref(ref_id)
        if text is None or not text.strip():
            raise BadInput(
                "rename requires text=<new name>",
                next=f"edit(kind='folder', id={ref_id}, text='New name')",
            )
        self.store.update_ref(ref_id, title=text.strip())
        return Response(body=f"renamed folder id={ref_id} to {text.strip()!r}")

    # ── delete: refuse while the folder has live contents ─────────

    def delete(self, *, id: str | int, **_kw: Any) -> Response:  # type: ignore[override]
        ref_id = self._coerce_id(id)
        self._resolve_live_ref(ref_id)
        n_children = self._live_child_count(ref_id)
        if n_children:
            raise BadInput(
                f"folder id={ref_id} has {n_children} live "
                f"item{'s' if n_children != 1 else ''} - move them out first",
                next=(
                    f"get(kind='folder', id={ref_id}) lists contents; "
                    "link(kind='<its kind>', id=..., rel='parent', "
                    "mode='remove') unfiles each"
                ),
            )
        return super().delete(id=id, **_kw)

    # ── list view: the folder tree is the landing page ────────────

    def _list_view(self, view: str) -> Response | None:
        if view in ("", "recent", "tree"):
            return self._render_folder_index()
        return None

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "tree")

    # ── SQL (store.pool, same pattern as _todo_guards / _todo_views) ──

    def _live_child_count(self, ref_id: int) -> int:
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM refs WHERE parent_id = %s AND deleted_at IS NULL",
                (ref_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _children(self, ref_id: int) -> list[tuple[int, str, str, str | None]]:
        """Live children as ``(ref_id, kind, title, slug)``, folders first."""
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.ref_id, r.kind, r.title,
                       (SELECT ri.id_value FROM ref_identifiers ri
                         WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
                         LIMIT 1) AS slug
                  FROM refs r
                 WHERE r.parent_id = %s AND r.deleted_at IS NULL
                 ORDER BY (r.kind != 'folder'), r.kind, lower(r.title)
                """,
                (ref_id,),
            ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2] or ""), r[3]) for r in rows]

    def _breadcrumb(self, ref: Ref) -> list[str]:
        """Folder names root→here, walking up while the parent is a folder."""
        names: list[str] = [ref.title]
        seen = {ref.id}
        parent_id = ref.parent_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT kind, title, parent_id FROM refs "
                    "WHERE ref_id = %s AND deleted_at IS NULL",
                    (parent_id,),
                ).fetchone()
            if row is None or row[0] != "folder":
                break
            names.append(str(row[1] or ""))
            parent_id = row[2]
        names.reverse()
        return names

    # ── rendering ──────────────────────────────────────────────────

    @staticmethod
    def _child_handle(kind: str, ref_id: int, slug: str | None) -> str:
        return handle_registry.try_format(kind, ref_id) or (
            f"{kind}:{slug if slug is not None else ref_id}"
        )

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        handle = handle_registry.try_format(self.kind, ref.id) or str(ref.id)
        out = [f"# folder {handle} - {ref.title}"]
        crumb = self._breadcrumb(ref)
        if len(crumb) > 1:
            out.append(f"path: /{'/'.join(crumb)}")
        if tags:
            out.append("tags: " + " ".join(str(t) for t in tags))
        children = self._children(ref.id)
        out.append("")
        if not children:
            out.append("(empty)")
            body = "\n".join(out)
            body += render_next_section(
                [
                    (
                        "link(kind='<its kind>', id=..., "
                        f"target='folder:{ref.id}', rel='parent')",
                        "place an artifact here",
                    ),
                    (
                        "put(kind='folder', text='<name>') then "
                        f"link(kind='folder', id=<new>, target='folder:{ref.id}', "
                        "rel='parent')",
                        "create a subfolder",
                    ),
                ]
            )
            return body
        from precis.format import render_agent_table

        rows = []
        for child_id, kind, title, slug in children:
            rows.append(
                {
                    "kind": kind,
                    "id": self._child_handle(kind, child_id, slug),
                    "title": (title[:80] + "…") if len(title) > 80 else title,
                }
            )
        out.append(f"contents ({len(children)}):")
        out.append(render_agent_table(rows, schema=["kind", "id", "title"]))
        return "\n".join(out)

    def _render_tree(self, ref: Ref) -> Response:
        """``view='tree'`` — the whole live subtree, folders as branches."""
        lines: list[str] = [f"# folder {ref.id} - {ref.title} (tree)", ""]
        n = self._render_tree_level(ref.id, ref.title, depth=0, lines=lines)
        lines.append("")
        lines.append(f"({n} item{'s' if n != 1 else ''})")
        return Response(body="\n".join(lines))

    def _render_tree_level(
        self, ref_id: int, title: str, *, depth: int, lines: list[str]
    ) -> int:
        indent = "  " * depth
        lines.append(f"{indent}📁 {title}  (folder:{ref_id})")
        if depth >= _TREE_DEPTH_CAP:
            lines.append(f"{indent}  … (depth cap)")
            return 0
        n = 0
        for child_id, kind, child_title, slug in self._children(ref_id):
            if kind == "folder":
                n += 1 + self._render_tree_level(
                    child_id, child_title, depth=depth + 1, lines=lines
                )
            else:
                n += 1
                handle = self._child_handle(kind, child_id, slug)
                shown = (
                    (child_title[:70] + "…") if len(child_title) > 70 else child_title
                )
                lines.append(f"{'  ' * (depth + 1)}{kind}  {handle}  {shown}")
        return n

    def _render_folder_index(self) -> Response:
        """Bare ``get(kind='folder')`` — every live folder, nested."""
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT f.ref_id, f.title, f.parent_id,
                       (SELECT count(*) FROM refs c
                         WHERE c.parent_id = f.ref_id
                           AND c.deleted_at IS NULL) AS n_children,
                       (SELECT p.kind FROM refs p
                         WHERE p.ref_id = f.parent_id) AS parent_kind
                  FROM refs f
                 WHERE f.kind = 'folder' AND f.deleted_at IS NULL
                 ORDER BY lower(f.title)
                """
            ).fetchall()
        if not rows:
            body = "no folders yet"
            body += render_next_section(
                [
                    (
                        "put(kind='folder', text='<name>')",
                        "create your first folder",
                    ),
                ]
            )
            return Response(body=body)
        by_parent: dict[int | None, list[tuple[int, str, int]]] = {}
        folder_ids = {int(r[0]) for r in rows}
        for r in rows:
            fid, title, parent_id, n_children, parent_kind = (
                int(r[0]),
                str(r[1] or ""),
                r[2],
                int(r[3]),
                r[4],
            )
            # A folder whose parent is not a live folder lists at top level.
            key = (
                int(parent_id)
                if parent_id is not None
                and parent_kind == "folder"
                and int(parent_id) in folder_ids
                else None
            )
            by_parent.setdefault(key, []).append((fid, title, n_children))

        lines = [f"# folders ({len(rows)})", ""]

        def emit(parent: int | None, depth: int) -> None:
            for fid, title, n_children in by_parent.get(parent, []):
                lines.append(
                    f"{'  ' * depth}📁 {title}  (folder:{fid}, "
                    f"{n_children} item{'s' if n_children != 1 else ''})"
                )
                emit(fid, depth + 1)

        emit(None, 0)
        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='folder', id=N)", "open a folder (path + contents)"),
                ("put(kind='folder', text='<name>')", "create a folder"),
                (
                    "link(kind='<its kind>', id=..., target='folder:N', rel='parent')",
                    "place an artifact",
                ),
            ]
        )
        return Response(body=body)
