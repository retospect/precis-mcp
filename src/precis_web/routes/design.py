"""Design tab — a read-only view across the two kinds the design
workbench sits on (the design-workbench build, slice 1 (2026-09-18)): one
tree per live ``se`` design (block graph → the ``structure`` bound to
each atomic-mode leaf, via ``se_blocks.bound_kind = 'structure'`` +
``bound_design`` — migration ``0001_se_kind.sql``/``0007_se_atomic.sql``,
not links) plus a flat "Loose structures" section for every live
``structure`` no block binds. Pure read: nothing here mutates a design;
every node click lands on the existing ``/se/{slug}``/``/structure/{slug}``
page.

* ``GET /design`` — the tree list (this module's one route).

Query budget: **≤ 3 SELECTs per render, regardless of how many designs
or structures exist** — one SQL joining every live ``se`` design against
its live blocks (:func:`_se_rows`), one for every live ``structure``
(:func:`_structure_rows`), one batched ``links`` count for lineage across
every structure the second query returned (:func:`_lineage_counts`, the
same ``derived-from`` edge ``routes/structure.py::_lineage`` reads one ref
at a time — batched here into a single ``GROUP BY``). No per-node round
trips; a design with a hundred blocks or a hundred bound structures still
costs the same three queries as one with none.

Level badges (L0–L3) are derived from columns the first query already
fetched, never geometry: L0 is unconditional (the block exists in the
design's own graph); L1 is "an envelope is declared"; L2 is "the block
itself carries a declared invariant" (``dof``, non-empty ``objectives``,
or a ``process_overrides`` entry) — a connect's ``joint`` is a tree-level
fact between two blocks, not a per-block column, so it is deliberately
out of scope for this pass rather than pulled in via a second query; L3
is "``bound_kind`` is set" (a cad/structure/component/part realization).
No L4 fit — that is an ``envelope_fit`` evaluation per block, explicitly
out of scope (spec's "no geometry evaluation").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["design"])

#: Browse-surface cap on the number of se DESIGNS rendered (mirrors
#: structure.py/blocktree_view.py's ``_LIST_LIMIT`` convention) — a
#: design's own block count is never capped, only how many trees this
#: page draws.
_LIST_LIMIT = 100


def _se_rows(store: Store) -> list[tuple[Any, ...]]:
    """Every live ``se`` design LEFT JOINed against its live blocks —
    one SQL statement, one round trip, whatever the design/block count."""
    sql = """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               r.updated_at,
               b.id, b.parent_block_id, b.name, b.envelope, b.dof,
               b.objectives, b.process_overrides, b.bound_kind,
               b.bound_design, b.array_spec
          FROM refs r
          LEFT JOIN se_blocks b
            ON b.ref_id = r.ref_id AND b.retired_at IS NULL
         WHERE r.kind = 'se' AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC, b.id ASC
    """
    with store.pool.connection() as conn:
        return conn.execute(sql).fetchall()


def _structure_rows(store: Store) -> list[tuple[Any, ...]]:
    """Every live ``structure`` design, bound or loose — one SQL
    statement (mirrors ``routes/structure.py::_list_rows``' shape)."""
    sql = """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               COALESCE((r.meta->>'version')::int, 0)          AS version,
               (SELECT sr.fidelity FROM struct_runs sr
                 WHERE sr.ref_id = r.ref_id AND sr.status = 'succeeded'
                   AND sr.energy IS NOT NULL
                 ORDER BY sr.id DESC LIMIT 1)                  AS last_fidelity,
               (SELECT sr.energy FROM struct_runs sr
                 WHERE sr.ref_id = r.ref_id AND sr.status = 'succeeded'
                   AND sr.energy IS NOT NULL
                 ORDER BY sr.id DESC LIMIT 1)                  AS last_energy,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'structure' AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC
    """
    with store.pool.connection() as conn:
        return conn.execute(sql).fetchall()


def _lineage_counts(store: Store, ref_ids: list[int]) -> dict[int, dict[str, int]]:
    """``derived-from`` parent/child counts for every ref in ``ref_ids``,
    batched into one query (the ``routes/structure.py::_lineage`` edge,
    read many-at-once instead of one ref per round trip). Empty input
    skips the query entirely — it never fires on an empty design tab."""
    if not ref_ids:
        return {}
    sql = """
        SELECT COALESCE(a.ref_id, b.ref_id) AS ref_id,
               COALESCE(a.cnt, 0) AS parents,
               COALESCE(b.cnt, 0) AS children
          FROM (SELECT src_ref_id AS ref_id, count(*) AS cnt FROM links
                 WHERE relation = 'derived-from' AND src_ref_id = ANY(%s)
                 GROUP BY src_ref_id) a
          FULL OUTER JOIN
               (SELECT dst_ref_id AS ref_id, count(*) AS cnt FROM links
                 WHERE relation = 'derived-from' AND dst_ref_id = ANY(%s)
                 GROUP BY dst_ref_id) b
            ON a.ref_id = b.ref_id
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ref_ids, ref_ids)).fetchall()
    return {int(r[0]): {"parents": int(r[1]), "children": int(r[2])} for r in rows}


def _levels(
    envelope: str | None,
    dof: dict[str, Any] | None,
    objectives: dict[str, Any] | None,
    process_overrides: dict[str, Any] | None,
    bound_kind: str | None,
) -> dict[str, bool]:
    """L0–L3 presence for one block, straight off the columns
    :func:`_se_rows` already fetched — see the module docstring for what
    each badge means and why L2 stays block-scoped this pass."""
    return {
        "l0": True,
        "l1": envelope is not None,
        "l2": bool(dof) or bool(objectives) or bool(process_overrides),
        "l3": bound_kind is not None,
    }


def _structures_by_slug(rows: list[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    """:func:`_structure_rows` → ``{slug: row-as-dict}``, insertion order
    preserved (the query's own ``ORDER BY ref_id DESC``) — a slug-less
    live ref (no ``cite_key`` identifier yet) is skipped: nothing in this
    design/structure surface is addressable without one."""
    out: dict[str, dict[str, Any]] = {}
    for ref_id, slug, title, version, fidelity, energy, updated in rows:
        if not slug:
            continue
        out[slug] = {
            "ref_id": int(ref_id),
            "slug": slug,
            "title": title or slug,
            "version": version,
            "last_relax": (
                {"fidelity": fidelity, "energy": float(energy)}
                if energy is not None
                else None
            ),
            "updated": _ago(updated),
            "lineage": {"parents": 0, "children": 0},
        }
    return out


def _build_designs(
    se_rows: list[tuple[Any, ...]], structures: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """:func:`_se_rows` → one nested tree per design, array nodes
    collapsed to ``name ×N`` (``array_spec`` is a multiplicity spec on a
    single block row, never N sibling rows — module docstring), each
    atomic-mode leaf carrying the bound structure's live facts (or
    ``live: False`` for a binding whose target isn't a live structure
    today). Returns the design list plus every slug bound by some block,
    so the caller can compute "loose" as the complement."""
    order: list[int] = []
    blocks_by_id: dict[int, dict[int, dict[str, Any]]] = {}
    children_by_parent: dict[int, dict[int | None, list[dict[str, Any]]]] = {}
    meta: dict[int, dict[str, Any]] = {}
    bound_slugs: set[str] = set()

    for row in se_rows:
        (
            ref_id,
            slug,
            title,
            updated,
            b_id,
            parent_id,
            name,
            envelope,
            dof,
            objectives,
            process_overrides,
            bound_kind,
            bound_design,
            array_spec,
        ) = row
        ref_id = int(ref_id)
        if ref_id not in meta:
            if len(order) >= _LIST_LIMIT:
                continue  # browse-surface cap on designs, not blocks
            order.append(ref_id)
            meta[ref_id] = {
                "ref_id": ref_id,
                "slug": slug,
                "title": title or slug,
                "href": f"/se/{slug}" if slug else None,
                "updated": _ago(updated),
            }
            blocks_by_id[ref_id] = {}
            children_by_parent[ref_id] = {}
        if ref_id not in meta:
            continue  # past the browse-surface cap — every row of a
            # capped design's own first row already hit the branch above
        if b_id is None:
            continue  # a design with zero live blocks

        display_name = str(name)
        if array_spec:
            count = array_spec.get("count")
            if count:
                display_name = f"{name} ×{count}"

        bound: dict[str, Any] | None = None
        if bound_kind == "structure" and bound_design:
            bound_slugs.add(bound_design)
            struct = structures.get(bound_design)
            bound = {
                "slug": bound_design,
                "href": f"/structure/{bound_design}" if struct else None,
                "live": struct is not None,
                "version": struct["version"] if struct else None,
                "last_relax": struct["last_relax"] if struct else None,
                "lineage": struct["lineage"] if struct else None,
            }

        node = {
            "id": int(b_id),
            "name": display_name,
            "href": meta[ref_id]["href"],
            "levels": _levels(envelope, dof, objectives, process_overrides, bound_kind),
            "bound": bound,
            "children": [],
        }
        blocks_by_id[ref_id][int(b_id)] = node
        parent_key = int(parent_id) if parent_id is not None else None
        children_by_parent[ref_id].setdefault(parent_key, []).append(node)

    designs: list[dict[str, Any]] = []
    for ref_id in order:
        for node_id, node in blocks_by_id[ref_id].items():
            node["children"] = children_by_parent[ref_id].get(node_id, [])
        designs.append(
            {
                **meta[ref_id],
                "roots": children_by_parent[ref_id].get(None, []),
            }
        )
    return designs, bound_slugs


@router.get("/design", response_class=HTMLResponse)
async def design_list(request: Request) -> HTMLResponse:
    """The tree list: one tree per live ``se`` design + loose structures."""
    store = get_store(request)
    se_rows = _se_rows(store)
    structure_rows = _structure_rows(store)
    structures = _structures_by_slug(structure_rows)
    lineage = _lineage_counts(store, [s["ref_id"] for s in structures.values()])
    for s in structures.values():
        s["lineage"] = lineage.get(s["ref_id"], {"parents": 0, "children": 0})

    designs, bound_slugs = _build_designs(se_rows, structures)
    loose_structures = [s for slug, s in structures.items() if slug not in bound_slugs]
    return templates.TemplateResponse(
        request,
        "design/list.html.j2",
        {
            "active_tab": "design",
            "designs": designs,
            "loose_structures": loose_structures,
            "total_designs": len(designs),
            "total_loose": len(loose_structures),
        },
    )
