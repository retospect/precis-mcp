"""Cluster-map grid — the hierarchical-SOM browse surface.

Renders the precomputed cluster maps (see
:mod:`precis.workers.clusterize`) as a grid of word-cloud tiles:

* ``GET /clusters`` — top-level grid for a scope ('paper' | 'memory').
* ``GET /clusters?path=4.7`` — drill into a tile. Internal tiles show
  their child grid; leaf tiles show the papers they hold.
* ``GET /clusters/word`` — htmx fragment: the papers under a tile whose
  chunks carry a hovered keyword (the "click a word → relevant things"
  affordance).

The heavy lifting (SOM training, c-TF-IDF) happens offline in the
worker; these routes are thin reads over ``cluster_cells`` /
``cluster_assignments`` plus a little presentation shaping.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates

router = APIRouter(tags=["clusters"])

_SCOPES = ("paper", "memory")
_TILE_WORDS = 14  # words shown per tile


def _current_run(store: Any, scope: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT run_id FROM cluster_runs "
            "WHERE scope=%s AND status='ok' "
            "ORDER BY finished_at DESC LIMIT 1",
            (scope,),
        ).fetchone()
    return int(row[0]) if row else None


def _shape_tile(row: dict[str, Any]) -> dict[str, Any]:
    """Attach per-word font sizes (normalised to the tile max)."""
    words = row.get("words") or []
    top = words[:_TILE_WORDS]
    max_s = max((float(w["s"]) for w in top), default=1.0) or 1.0
    row["cloud"] = [
        {"w": w["w"], "size": round(0.72 + 1.55 * (float(w["s"]) / max_s), 3)}
        for w in top
    ]
    return row


def _members_clause(path: str) -> tuple[str, dict[str, str]]:
    """SQL predicate + params matching every assignment under ``path``
    (the leaf itself, or any descendant leaf of an internal cell)."""
    return (
        "(a.leaf_path = %(path)s OR a.leaf_path LIKE %(pfx)s)",
        {"path": path, "pfx": f"{path}.%"},
    )


@router.get("/clusters", response_class=HTMLResponse)
async def clusters(
    request: Request,
    scope: str = "paper",
    path: str | None = None,
) -> HTMLResponse:
    """Top-level grid, a drilled-in child grid, or a leaf's papers."""
    if scope not in _SCOPES:
        scope = "paper"
    store = get_store(request)
    run_id = _current_run(store, scope)

    ctx: dict[str, Any] = {
        "active_tab": "clusters",
        "scope": scope,
        "scopes": _SCOPES,
        "path": path,
        "run_id": run_id,
        "tiles": [],
        "members": None,
        "breadcrumb": _breadcrumb(store, run_id, path) if run_id else [],
    }
    if run_id is None:
        return templates.TemplateResponse(request, "clusters/grid.html.j2", ctx)

    with store.pool.connection() as conn:
        if path is not None:
            cell = conn.execute(
                "SELECT is_leaf FROM cluster_cells WHERE run_id=%s AND path=%s",
                (run_id, path),
            ).fetchone()
            if cell is not None and cell[0]:
                ctx["members"] = _members(store, run_id, path)
                return templates.TemplateResponse(request, "clusters/grid.html.j2", ctx)

        parent_pred = "parent_path = %s" if path is not None else "parent_path IS NULL"
        args: tuple[Any, ...] = (run_id, path) if path is not None else (run_id,)
        rows = conn.execute(
            "SELECT path, grid_row, grid_col, is_leaf, n_chunks, n_refs, words "
            "FROM cluster_cells "
            f"WHERE run_id=%s AND {parent_pred} "
            "ORDER BY grid_row, grid_col",
            args,
        ).fetchall()

    tiles = [
        _shape_tile(
            {
                "path": r[0],
                "grid_row": r[1],
                "grid_col": r[2],
                "is_leaf": r[3],
                "n_chunks": r[4],
                "n_refs": r[5],
                "words": r[6],
            }
        )
        for r in rows
    ]
    ctx["tiles"] = tiles
    ctx["n_cols"] = (max((t["grid_col"] for t in tiles), default=0) + 1) if tiles else 1
    return templates.TemplateResponse(request, "clusters/grid.html.j2", ctx)


@router.get("/clusters/word", response_class=HTMLResponse)
async def cluster_word(request: Request, scope: str, path: str, w: str) -> HTMLResponse:
    """htmx fragment: papers under ``path`` whose chunks carry ``w``."""
    if scope not in _SCOPES:
        scope = "paper"
    store = get_store(request)
    run_id = _current_run(store, scope)
    papers: list[dict[str, Any]] = []
    if run_id is not None:
        pred, params = _members_clause(path)
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, r.title, count(*) AS n "
                "FROM cluster_assignments a "
                "JOIN chunks c ON c.chunk_id = a.chunk_id "
                "JOIN refs r ON r.ref_id = a.ref_id "
                f"WHERE a.run_id = %(run)s AND {pred} "
                "AND c.keywords @> ARRAY[%(w)s] "
                "GROUP BY r.ref_id, r.title ORDER BY n DESC LIMIT 20",
                {"run": run_id, "w": w.lower(), **params},
            ).fetchall()
        papers = [
            {
                "ref_id": r[0],
                "title": (r[1] or "(untitled)").split("\n", 1)[0],
                "n": r[2],
            }
            for r in rows
        ]
    return templates.TemplateResponse(
        request,
        "clusters/word.html.j2",
        {"scope": scope, "word": w, "papers": papers},
    )


def _members(store: Any, run_id: int, path: str) -> list[dict[str, Any]]:
    pred, params = _members_clause(path)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, count(*) AS n "
            "FROM cluster_assignments a JOIN refs r ON r.ref_id = a.ref_id "
            f"WHERE a.run_id = %(run)s AND {pred} "
            "GROUP BY r.ref_id, r.title ORDER BY n DESC LIMIT 100",
            {"run": run_id, **params},
        ).fetchall()
    return [
        {"ref_id": r[0], "title": (r[1] or "(untitled)").split("\n", 1)[0], "n": r[2]}
        for r in rows
    ]


def _breadcrumb(store: Any, run_id: int, path: str | None) -> list[dict[str, str]]:
    """Ancestor crumbs labelled by each cell's top word (or its index)."""
    if not path:
        return []
    parts = path.split(".")
    paths = [".".join(parts[: i + 1]) for i in range(len(parts))]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT path, words FROM cluster_cells WHERE run_id=%s AND path = ANY(%s)",
            (run_id, paths),
        ).fetchall()
    words_by_path = {r[0]: (r[1] or []) for r in rows}
    crumbs = []
    for p in paths:
        words = words_by_path.get(p) or []
        label = words[0]["w"] if words else p.rsplit(".", 1)[-1]
        crumbs.append({"path": p, "label": label})
    return crumbs
