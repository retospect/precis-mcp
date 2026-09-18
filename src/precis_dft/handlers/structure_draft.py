"""``kind='structure_draft'`` — mutable workbench structure.

Lifecycle:

- ``put(kind='structure_draft', **{'from': 'structure:<sha>'})`` —
  fork a draft from a frozen parent.
- ``edit(id='draft:<uuid>', ops=[...])`` — apply typed ops, refresh
  the POSCAR, bump view_version, mark ``views_pending=True``.
- ``edit(id='draft:<uuid>', mode='commit')`` — promote the draft
  to a frozen ``structure:<sha>`` and write a ``derived_from``
  link back to the parent. The link payload carries the flat op
  list so the derivation tree can be walked and ops replayed on
  a different parent.
- ``get(id='draft:<uuid>', view=...)`` — read the current draft +
  views.

Combinatorial ops (``substitute`` with ``enumerate='all'``)
produce N sibling drafts via one edit. Each sibling gets its own
``draft:<uuid>`` and shares ``parent_id`` with the original draft.
The handler creates each sibling as a separate ref.
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_dft import workbench

_META_POSCAR = "poscar"
_META_PARENT = "parent_id"
_META_EDIT_LOG = "edit_log"
_META_VIEW_VERSION = "view_version"
_META_VIEWS_PENDING = "views_pending"


class StructureDraftHandler(Handler):
    spec = KindSpec(
        kind="structure_draft",
        title="Structure workbench draft",
        description=(
            "Mutable workbench structure. Supports typed edit ops "
            "(substitute / add_adsorbate / intercalate / vacancy / strain / "
            "supercell / constrain / displace / set_magmom). Commits into "
            "a frozen structure:<sha> with a derived_from link."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
        views=(
            "toc",
            "header",
            "sites",
            "graph",
            "special_sites",
            "symmetry",
            "ascii_top",
            "ascii_side",
            "neighborhood",
            "compare",
            "path",
            "interstitials",
            "edit_log",
        ),
        modes=("commit",),  # edit(mode='commit') freezes
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    # ── put: fork from frozen parent ─────────────────────────────

    def put(self, **kw: Any) -> Response:
        from_id = kw.get("from") or kw.get("from_")
        if not from_id:
            return Response(
                body=(
                    "structure_draft put requires from='structure:<sha>' "
                    "(the frozen parent to fork)"
                )
            )
        store = self._store()
        parent_ref = store.fetch_ref_by_slug("structure", from_id)
        if parent_ref is None:
            return Response(body=f"frozen parent not found: {from_id}")

        from precis_dft.workbench import FrozenStructure

        parent = FrozenStructure(
            id=from_id,
            poscar=parent_ref.meta.get(_META_POSCAR, ""),
            sha=parent_ref.meta.get("sha", from_id.removeprefix("structure:")),
            n_atoms=parent_ref.meta.get("n_atoms", 0),
            formula=parent_ref.meta.get("formula", "?"),
            composition=parent_ref.meta.get("composition", {}),
            dimensionality=parent_ref.meta.get("dimensionality", "bulk"),
        )

        draft = workbench.fork_draft(parent)
        ref = store.insert_ref(
            kind="structure_draft",
            slug=draft.id,
            title=f"{parent.formula} (draft)",
            meta={
                _META_POSCAR: draft.poscar,
                _META_PARENT: draft.parent_id,
                _META_EDIT_LOG: draft.edit_log,
                _META_VIEW_VERSION: draft.view_version,
                _META_VIEWS_PENDING: draft.views_pending,
            },
        )
        return Response(
            body=(
                f"forked draft id={draft.id} from parent={parent.id} (ref_id={ref.ref_id})"
            )
        )

    # ── edit: ops or commit ──────────────────────────────────────

    def edit(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            return Response(body="structure_draft edit requires id='draft:<uuid>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("structure_draft", id_)
        if ref is None:
            return Response(body=f"draft not found: {id_}")

        mode = kw.get("mode")
        if mode == "commit":
            return self._do_commit(store, ref, msg=kw.get("msg"))

        ops = kw.get("ops")
        if not ops:
            return Response(
                body=(
                    "structure_draft edit requires either ops=[...] "
                    "(to apply edits) or mode='commit' (to freeze)"
                )
            )
        return self._do_apply(store, ref, ops, msg=kw.get("msg"))

    # ── get ──────────────────────────────────────────────────────

    def get(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            return Response(body="structure_draft get requires id='draft:<uuid>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("structure_draft", id_)
        if ref is None:
            return Response(body=f"draft not found: {id_}")
        poscar = ref.meta.get(_META_POSCAR, "")

        view = kw.get("view")
        if view in (None, "summary"):
            return Response(body=_format_draft_summary(ref.meta, id_))
        if view == "edit_log":
            return Response(body=str(ref.meta.get(_META_EDIT_LOG, [])))
        if view == "toc":
            return Response(body=str(workbench.view_toc(poscar)))
        if view == "header":
            views = workbench.views_for(poscar)
            return Response(body=str(views["header"]))
        if view in (
            "sites",
            "graph",
            "special_sites",
            "symmetry",
            "ascii_top",
            "ascii_side",
        ):
            views = workbench.views_for(poscar)
            return Response(body=str(views[view]))
        if view == "poscar":
            return Response(body=poscar)
        return Response(body=f"unknown view {view!r} for kind='structure_draft'")

    # ── delete ───────────────────────────────────────────────────

    def delete(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            return Response(body="structure_draft delete requires id='draft:<uuid>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("structure_draft", id_)
        if ref is None:
            return Response(body=f"draft not found: {id_}")
        store.soft_delete(ref.ref_id)
        return Response(body=f"deleted draft {id_}")

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError("StructureDraftHandler.search — wiring not landed")

    def tag(self, **kw: Any) -> Response:
        raise NotImplementedError("StructureDraftHandler.tag — wiring not landed")

    def link(self, **kw: Any) -> Response:
        raise NotImplementedError("StructureDraftHandler.link — wiring not landed")

    # ── internals ────────────────────────────────────────────────

    def _do_apply(
        self,
        store: Any,
        ref: Any,
        ops: list[dict[str, Any]],
        *,
        msg: str | None,
    ) -> Response:
        from precis_dft.workbench import DraftStructure

        draft = DraftStructure(
            id=ref.slug,
            poscar=ref.meta[_META_POSCAR],
            parent_id=ref.meta[_META_PARENT],
            edit_log=list(ref.meta.get(_META_EDIT_LOG, [])),
            view_version=int(ref.meta.get(_META_VIEW_VERSION, 0)),
            views_pending=bool(ref.meta.get(_META_VIEWS_PENDING, False)),
        )
        try:
            result = workbench.apply_edit(draft, ops, msg=msg)
        except Exception as exc:
            return Response(body=f"edit failed: {exc!r}")

        if isinstance(result, list):
            # Combinatorial expansion — write each sibling as a fresh
            # draft ref. The original draft is left unchanged so the
            # caller can still inspect it.
            sibling_ids: list[str] = []
            for sibling in result:
                store.insert_ref(
                    kind="structure_draft",
                    slug=sibling.id,
                    title=f"{draft.id} sibling",
                    meta={
                        _META_POSCAR: sibling.poscar,
                        _META_PARENT: sibling.parent_id,
                        _META_EDIT_LOG: sibling.edit_log,
                        _META_VIEW_VERSION: sibling.view_version,
                        _META_VIEWS_PENDING: sibling.views_pending,
                    },
                )
                sibling_ids.append(sibling.id)
            return Response(
                body=(
                    f"combinatorial edit produced {len(sibling_ids)} sibling drafts "
                    f"from {draft.id}:\n  " + "\n  ".join(sibling_ids)
                )
            )

        # Single mutation — update in place.
        store.set_meta(
            ref.ref_id,
            **{
                _META_POSCAR: result.poscar,
                _META_EDIT_LOG: result.edit_log,
                _META_VIEW_VERSION: result.view_version,
                _META_VIEWS_PENDING: result.views_pending,
            },
        )
        return Response(
            body=(
                f"applied {len(ops)} op(s) to {draft.id}; "
                f"view_version={result.view_version} (views_pending=True)"
            )
        )

    def _do_commit(self, store: Any, ref: Any, *, msg: str | None) -> Response:
        from precis_dft.workbench import DraftStructure

        draft = DraftStructure(
            id=ref.slug,
            poscar=ref.meta[_META_POSCAR],
            parent_id=ref.meta[_META_PARENT],
            edit_log=list(ref.meta.get(_META_EDIT_LOG, [])),
            view_version=int(ref.meta.get(_META_VIEW_VERSION, 0)),
            views_pending=False,
        )
        commit = workbench.commit_draft(draft, msg=msg)

        # Insert (or fetch) the frozen ref.
        frozen_ref = store.fetch_ref_by_slug("structure", commit.frozen.id)
        if frozen_ref is None:
            frozen_ref = store.insert_ref(
                kind="structure",
                slug=commit.frozen.id,
                title=commit.frozen.formula,
                meta={
                    _META_POSCAR: commit.frozen.poscar,
                    "sha": commit.frozen.sha,
                    "n_atoms": commit.frozen.n_atoms,
                    "formula": commit.frozen.formula,
                    "composition": commit.frozen.composition,
                    "dimensionality": commit.frozen.dimensionality,
                },
            )

        # Write the derived_from link with the op payload.
        parent_ref = store.fetch_ref_by_slug("structure", draft.parent_id)
        if parent_ref is not None:
            store.add_link(
                src_ref_id=frozen_ref.ref_id,
                dst_ref_id=parent_ref.ref_id,
                relation="derived_from",
                meta=commit.ops_payload,
            )

        # Soft-delete the draft now that it's been promoted.
        store.soft_delete(ref.ref_id)

        return Response(
            body=(
                f"committed draft {draft.id} → frozen {commit.frozen.id} "
                f"(parent {draft.parent_id}, "
                f"{len(commit.ops_payload['ops'])} op(s) recorded on the link)"
            )
        )

    def _store(self) -> Any:
        if self.hub is not None and hasattr(self.hub, "store"):
            return self.hub.store
        return self.store  # type: ignore[attr-defined]


def _format_draft_summary(meta: dict[str, Any], id_: str) -> str:
    """One-screen text summary of a draft."""
    n_edits = len(meta.get(_META_EDIT_LOG, []))
    return (
        f"# {id_}\n"
        f"parent:        {meta.get(_META_PARENT, '?')}\n"
        f"view_version:  {meta.get(_META_VIEW_VERSION, 0)}\n"
        f"views_pending: {meta.get(_META_VIEWS_PENDING, False)}\n"
        f"edits applied: {n_edits}\n"
    )
