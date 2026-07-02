"""Store ops for the ``cad`` kind (ADR 0041 + Amendment 1).

Storage splits by what is actually a search target:

- the **design** is a slug-addressed ``refs`` row (``kind='cad'``);
  design-level metadata (units, tolerances) lives on ``refs.meta``;
- the design keeps **one** ``card_combined`` chunk — an auto-built
  summary (title + component + node names + shapes + bbox) — so
  ``search(kind='cad', q=…)`` works on intent and joins the cross-kind
  embedding search. One vector per design;
- the **nodes** live in the dedicated ``cad_nodes`` table — structured
  geometry, never embedded. Re-authoring retires the old node rows and
  the old card, then writes the new set (ADR 0033 soft-delete model).

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.insert_ref`` / ``self.get_ref``.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from precis.cad.scene import NodeSpec, SceneSpec


class CadMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    _replace_card_combined: Any  # BlocksMixin — the shared card_combined write

    def cad_save(
        self,
        *,
        slug: str,
        title: str,
        spec: SceneSpec,
        card_text: str,
    ) -> tuple[Any, bool, int]:
        """Create-or-replace a design. Returns ``(ref, created, n_nodes)``."""
        existing = self.get_ref(kind="cad", id=slug)
        created = existing is None
        with self.tx() as conn:
            if created:
                ref = self.insert_ref(
                    kind="cad",
                    slug=slug,
                    title=title,
                    meta=dict(spec.meta),
                    conn=conn,
                )
            else:
                ref = existing
                conn.execute(
                    "UPDATE cad_nodes SET retired_at = now() "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref.id,),
                )
                conn.execute(
                    "UPDATE refs SET title = %s, meta = %s WHERE ref_id = %s",
                    (title, Jsonb(dict(spec.meta)), ref.id),
                )
            n = 0
            for ordi, node in enumerate(spec.nodes):
                conn.execute(
                    """
                    INSERT INTO cad_nodes
                        (ref_id, ord, name, component, op, config,
                         loc, rot, pattern)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ref.id,
                        ordi,
                        node.name,
                        node.component,
                        node.op,
                        node.config,
                        list(node.loc),
                        list(node.rot),
                        Jsonb(node.pattern) if node.pattern is not None else None,
                    ),
                )
                n += 1
            self._replace_card_combined(conn, ref_id=ref.id, card_text=card_text)
        return ref, created, n

    # -- read ------------------------------------------------------------
    def cad_load(self, ref_id: int) -> tuple[SceneSpec, dict[str, int]]:
        """Reconstruct a design's :class:`SceneSpec` from ``cad_nodes``.

        Returns the spec plus a ``{node_name: node_id}`` map so the handler
        can render the ``ca<node_id>`` node handles.
        """
        ref = self.get_ref(kind="cad", id=ref_id)
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT node_id, name, component, op, config, loc, rot, pattern "
                "FROM cad_nodes WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY ord ASC",
                (ref_id,),
            ).fetchall()
        spec = SceneSpec()
        if ref is not None and ref.meta:
            spec.meta = dict(ref.meta)
        handles: dict[str, int] = {}
        components: list[str] = []
        for node_id, name, component, op, config, loc, rot, pattern in rows:
            spec.nodes.append(
                NodeSpec(
                    name=str(name),
                    op=str(op),
                    config=str(config),
                    component=str(component),
                    loc=tuple(float(x) for x in (loc or [0, 0, 0])),  # type: ignore[arg-type]
                    rot=tuple(float(x) for x in (rot or [0, 0, 0])),  # type: ignore[arg-type]
                    pattern=dict(pattern) if pattern else None,
                )
            )
            handles[str(name)] = int(node_id)
            if component not in components:
                components.append(str(component))
        spec.components = components or ["part"]
        return spec, handles

    def cad_node(self, node_id: int) -> tuple[int, str, dict[str, Any]] | None:
        """A single live cad node by node_id → (ref_id, name, meta-dict)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id, name, component, op, config, loc, rot, pattern "
                "FROM cad_nodes WHERE node_id = %s AND retired_at IS NULL",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        ref_id, name, component, op, config, loc, rot, pattern = row
        meta = {
            "component": component,
            "op": op,
            "config": config,
            "loc": list(loc or []),
            "rot": list(rot or []),
        }
        if pattern:
            meta["pattern"] = dict(pattern)
        return int(ref_id), str(name), meta

    # -- delete ----------------------------------------------------------
    def cad_delete(self, ref_id: int) -> int:
        """Soft-delete a design: mark the ref deleted, retire its nodes,
        drop its search card — atomically. Returns nodes retired."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE ref_id = %s AND kind = 'cad' AND deleted_at IS NULL",
                (ref_id,),
            )
            n = conn.execute(
                "UPDATE cad_nodes SET retired_at = now() "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).rowcount
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
                (ref_id,),
            )
        return int(n)
