"""Render orchestration: a computed `figure` chunk → a PNG in `chunk_blobs`.

The glue between the document model and the sandboxed engine:
pull a figure's render recipe (`meta.render.src`) and the `meta.table` data of
every chunk it `plots`, run the code in the phase-1 sandbox
(:func:`precis.render.sandbox.render_python`), and on success write the image
into the figure's `chunk_blobs` row (regenerable) and stamp its invalidation
key. Pure orchestration — the isolation lives in the engine, the storage in the
store; this never executes chunk code in-process.

A second recipe kind, ``meta.render.kind == 'view'`` (se-view-figures §2), has
no code or plotted data: it names a ``structure`` design plus camera/style
params and is rasterised server-side through :mod:`precis.viz3d`; its
``cached_key`` hashes the structure's geometry sha, the canonical view params
and :data:`VIZ3D_CODE_VERSION`, so a geometry edit marks it stale.

The render code receives ``data = {'tables': [<meta.table>, ...]}`` in plotted
order, plus ``out`` (the PNG path) — see the engine's contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from precis.render.sandbox import DEFAULT_TIMEOUT_S, render_python

log = logging.getLogger(__name__)

#: mime of the rendered artifact (the engine always emits PNG).
RENDER_MIME = "image/png"


@dataclass(frozen=True)
class RenderOutcome:
    """Result of rendering one figure chunk.

    ``ok`` means a PNG was produced and written to the figure's blob.
    ``error`` is a short tag on failure — ``"not-a-graph"`` (no render
    recipe), ``"no-data"`` (no plotted tables), or the engine's own tag
    (``"timeout"`` / ``"exit:n"`` / ``"no-output"``); ``detail`` carries the
    child stderr for the failure bubble.
    """

    ok: bool
    error: str | None = None
    detail: str = ""
    cached_key: str | None = None


def invalidation_key(input_shas: list[str]) -> str:
    """`hash(render_src_sha, sorted(plotted_data_shas))` — the
    content-addressed key. Any plotted table's `content_sha` change (or a
    recipe edit) flips it, marking the figure stale."""
    joined = "\n".join(sorted(input_shas))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


#: Roll to retire every cached view render (append-only key scheme).
VIZ3D_CODE_VERSION = "1"

#: Hard cost bounds for a structure view. The render runs in-process (viz3d is
#: pure Python/numpy, no sandbox), so cost is bounded by atom count instead of
#: a wall-clock budget: refuse above ``VIEW_MAX_ATOMS`` (at put AND render
#: time), and render quality is downgraded (r2 -> r1 -> r0) above the two
#: thresholds below, since gradient shading is the expensive part.
VIEW_MAX_ATOMS = 20000
_VIEW_R2_MAX_ATOMS = 2000
_VIEW_R1_MAX_ATOMS = 6000

_VIEW_CAMERA_KEYS = ("azimuth_deg", "elevation_deg", "twist_deg", "projection", "zoom")
_VIEW_STYLE_KEYS = ("stick_radius", "ball_scale", "color")
_VIEW_ERRORS = {"no-source", "view-error", "rasterise", "too-large"}
_VIEW_PROJECTIONS = ("ortho", "persp")
_VIEW_COLORS = ("cpk", "mono")


def _finite(
    name: str, v: Any, lo: float | None = None, hi: float | None = None
) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {v!r}")
    if lo is not None and hi is not None and not lo <= v <= hi:
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {v!r}")
    return float(v)


def view_recipe(
    slug: str,
    camera: Any,
    style: Any,
    refine: Any,
    *,
    se_block: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Validated canonical ``meta.render`` recipe for a structure stick view.
    Raises ``ValueError`` on unknown keys, wrong types, non-finite or
    out-of-range values, or a ``refine`` that is not the int 0/1/2. Angles are
    normalised to [0, 360). ``se_block=(design, block)`` records that the
    structure was picked through an se design's bound block — provenance
    only; rendering always reads ``source.slug``."""
    camera = {} if camera is None else camera
    style = {} if style is None else style
    if not isinstance(camera, dict) or not isinstance(style, dict):
        raise ValueError("camera and style must be objects")
    for got, ok, what in (
        (camera, _VIEW_CAMERA_KEYS, "camera"),
        (style, _VIEW_STYLE_KEYS, "style"),
    ):
        bad = sorted(set(got) - set(ok))
        if bad:
            raise ValueError(f"unknown {what} key(s) {bad}; allowed: {list(ok)}")
    cam: dict[str, Any] = {}
    for k in ("azimuth_deg", "elevation_deg", "twist_deg"):
        if k in camera:
            cam[k] = _finite(k, camera[k]) % 360.0
    if "zoom" in camera:
        cam["zoom"] = _finite("zoom", camera["zoom"], 0.1, 20.0)
    if "projection" in camera:
        if camera["projection"] not in _VIEW_PROJECTIONS:
            raise ValueError(f"projection must be one of {list(_VIEW_PROJECTIONS)}")
        cam["projection"] = camera["projection"]
    sty: dict[str, Any] = {}
    if "stick_radius" in style:
        sty["stick_radius"] = _finite("stick_radius", style["stick_radius"], 0.01, 2.0)
    if "ball_scale" in style:
        sty["ball_scale"] = _finite("ball_scale", style["ball_scale"], 0.05, 3.0)
    if "color" in style:
        if style["color"] not in _VIEW_COLORS:
            raise ValueError(f"color must be one of {list(_VIEW_COLORS)}")
        sty["color"] = style["color"]
    if (
        isinstance(refine, bool)
        or not isinstance(refine, int)
        or refine not in (0, 1, 2)
    ):
        raise ValueError(f"refine must be the integer 0, 1 or 2, got {refine!r}")
    source: dict[str, Any] = {"kind": "structure", "slug": slug}
    if se_block is not None:
        source["se"], source["block"] = se_block
    return {
        "kind": "view",
        "source": source,
        "view": "stick",
        "camera": cam,
        "style": sty,
        "refine": refine,
    }


def view_load(store: Any, recipe: dict[str, Any]) -> tuple[int, int, Any] | None:
    """``(structure ref id, version, Scene)`` for the live structure a view
    recipe points at, or ``None`` when the slug no longer resolves. The ref id
    matters because a re-generate deletes the structure and re-mints the slug
    at version 1 — the version alone would read as unchanged."""
    slug = str((recipe.get("source") or {}).get("slug") or "")
    ref = store.get_ref(kind="structure", id=slug) if slug else None
    if ref is None:
        return None
    scene, _handles = store.structure_load(ref.id)
    return int(ref.id), int((ref.meta or {}).get("version", 0)), scene


def _bond_hash(scene: Any) -> str:
    """Order-independent hash of the bond graph (``structure_sha`` ignores
    bonds, but they change the picture)."""
    rows = sorted(
        (*sorted((b.i, b.j)), float(b.order), tuple(b.image)) for b in scene.bonds
    )
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def effective_refine(recipe: dict[str, Any], n_atoms: int) -> int:
    """The recipe's refine, downgraded for large scenes (see ``VIEW_MAX_ATOMS``)."""
    r = int(recipe.get("refine", 2))
    if n_atoms > _VIEW_R1_MAX_ATOMS:
        return 0
    if n_atoms > _VIEW_R2_MAX_ATOMS:
        return min(r, 1)
    return r


def view_key(scene: Any, recipe: dict[str, Any]) -> str:
    """`sha256(structure_sha | bond hash | canonical view params | code version)`."""
    from precis.structure.cache import structure_sha

    params = {
        k: v
        for k, v in recipe.items()
        if k not in ("cached_key", "source_version", "source_ref", "failed")
    }
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"))
    joined = "\n".join(
        [structure_sha(scene), _bond_hash(scene), canon, VIZ3D_CODE_VERSION]
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _render_view_png(scene: Any, recipe: dict[str, Any]) -> bytes | None:
    """Rasterise a structure scene per ``recipe`` (viz3d stick figure → resvg)."""
    import numpy as np

    from precis.utils.figure_source import _svg_to_png
    from precis.viz3d.camera import Camera
    from precis.viz3d.render import render_svg
    from precis.viz3d.stickfig import Style, stick_scene

    labels = list(scene.atoms)
    index = {lab: n for n, lab in enumerate(labels)}
    coords = np.array(
        [scene.cell.frac_to_cart(scene.atoms[lab].frac) for lab in labels],
        dtype=np.float64,
    ).reshape(len(labels), 3)
    # Periodic-image bonds are not drawn (molecular scenes only, v1).
    bonds = [
        (index[b.i], index[b.j])
        for b in scene.bonds
        if b.i in index and b.j in index and tuple(b.image) == (0, 0, 0)
    ]
    elements = [scene.atoms[lab].element for lab in labels]
    svg = render_svg(
        stick_scene(
            elements, coords, bonds, style=Style(**(recipe.get("style") or {}))
        ),
        Camera(**(recipe.get("camera") or {})),
        refine=effective_refine(recipe, len(labels)),
    )
    return _svg_to_png(svg)


def _failure_message(error: str | None, detail: str) -> str:
    tail = detail.strip().splitlines()[-1:] or [""]
    what = "view" if error in _VIEW_ERRORS else "graph"
    return f"{what} render failed ({error})" + (f": {tail[0]}" if tail[0] else "")


def _render_view_chunk(
    store: Any, figure_chunk_id: int, recipe: dict[str, Any]
) -> RenderOutcome:
    loaded = view_load(store, recipe)
    if loaded is None:
        return RenderOutcome(ok=False, error="no-source")
    source_ref, version, scene = loaded

    def fail(error: str, detail: str = "") -> RenderOutcome:
        # Marker keyed to this structure version, so the blob route does not
        # re-render on every GET until the structure or recipe changes (a
        # recipe rewrite replaces meta.render wholesale, dropping the marker).
        store.drafts.stamp_render_fields(
            figure_chunk_id,
            {
                "failed": {
                    "version": version,
                    "ref": source_ref,
                    "message": _failure_message(error, detail),
                }
            },
        )
        return RenderOutcome(ok=False, error=error, detail=detail)

    if not scene.atoms:
        return fail("no-data")
    if len(scene.atoms) > VIEW_MAX_ATOMS:
        return fail("too-large", f"{len(scene.atoms)} atoms > {VIEW_MAX_ATOMS}")
    try:
        png = _render_view_png(scene, recipe)
    except Exception as exc:  # bad camera/style values in the recipe
        return fail("view-error", str(exc))
    if png is None:
        return fail("rasterise")
    store.drafts.upsert_chunk_blob(figure_chunk_id, png, RENDER_MIME)
    key = view_key(scene, recipe)
    store.drafts.stamp_render_fields(
        figure_chunk_id,
        {"cached_key": key, "source_version": version, "source_ref": source_ref},
        drop=("failed",),
    )
    return RenderOutcome(ok=True, cached_key=key)


def render_figure_chunk(
    store: Any,
    figure_chunk_id: int,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RenderOutcome:
    """Render one computed `figure` chunk and write its PNG to `chunk_blobs`.

    No-op-safe: returns ``ok=False`` (never raises) for a plain image figure
    or a render failure, so a caller (the render pass / export barrier) can
    bubble the reason without crashing the worker."""
    recipe = store.drafts.figure_render_recipe(figure_chunk_id)
    if recipe is not None and recipe.get("kind") == "view":
        return _render_view_chunk(store, figure_chunk_id, recipe)
    bundle = store.drafts.figure_render_bundle(figure_chunk_id)
    if bundle is None:
        return RenderOutcome(ok=False, error="not-a-graph")
    tables = bundle["tables"]
    if not tables:
        return RenderOutcome(ok=False, error="no-data")

    result = render_python(
        str(bundle["render"]["src"]),
        inputs={"tables": tables},
        timeout_s=timeout_s,
    )
    if not result.ok or result.png is None:
        return RenderOutcome(ok=False, error=result.error, detail=result.stderr)

    store.drafts.upsert_chunk_blob(figure_chunk_id, result.png, RENDER_MIME)
    key = invalidation_key(bundle["input_shas"])
    store.drafts.stamp_render_key(figure_chunk_id, key)
    return RenderOutcome(ok=True, cached_key=key)


def needs_render(store: Any, chunk: Any) -> bool:
    """True when a draft graph figure must be (re)rendered before use: it is
    still ``meta.figure.render_pending``, or its stamped ``meta.render.cached_key``
    no longer matches the key of the recipe + plotted tables' current
    ``content_sha`` (the plotted data changed since the last render). A graph
    whose plotted tables are all gone keeps its last image."""
    meta = getattr(chunk, "meta", None) or {}
    if (meta.get("figure") or {}).get("render_pending"):
        return True
    if not isinstance(meta.get("render"), dict):
        return False
    if meta["render"].get("kind") == "view":
        return _view_fresh(store, meta) is _STALE
    bundle = store.drafts.figure_render_bundle(chunk.chunk_id)
    if bundle is None or not bundle["tables"]:
        return False
    return meta["render"].get("cached_key") != invalidation_key(bundle["input_shas"])


_STALE: Any = object()


def _view_fresh(store: Any, meta: dict[str, Any]) -> Any:
    """Cheap freshness check for a view figure: compares the structure's
    stored version (one ``get_ref``) to the version stamped at the last render
    or failure, loading the full structure only when that differs. Returns
    ``None`` (fresh), a failure message (known-failed at this version, not
    retried), or ``_STALE`` (render needed)."""
    render = meta["render"]
    pending = bool((meta.get("figure") or {}).get("render_pending"))
    slug = str((render.get("source") or {}).get("slug") or "")
    ref = store.get_ref(kind="structure", id=slug) if slug else None
    if ref is None:
        return _STALE if pending else None  # a vanished source keeps its image
    version = int((ref.meta or {}).get("version", 0))
    # Stamps from before ``source_ref`` existed carry no ref: treat as same.
    failed = render.get("failed")
    if (
        isinstance(failed, dict)
        and failed.get("version") == version
        and failed.get("ref", ref.id) == ref.id
    ):
        return str(failed.get("message") or "view render failed")
    if pending or not render.get("cached_key"):
        return _STALE
    if (
        render.get("source_version") == version
        and render.get("source_ref", ref.id) == ref.id
    ):
        return None
    loaded = view_load(store, render)
    if loaded is not None and render["cached_key"] == view_key(loaded[2], render):
        return None
    return _STALE


def ensure_rendered(store: Any, chunk: Any) -> str | None:
    """Render a draft graph figure that is pending or stale (see
    :func:`needs_render`).

    ``DraftHandler._add_graph_figure`` writes a placeholder PNG flagged
    ``meta.figure.render_pending``; the real image is rendered here, lazily, at
    the moment it is consumed (export, the reader's blob route). A figure whose
    plotted data was edited after its last render is re-rendered the same way.
    The result is persisted — ``chunk_blobs`` is the regenerable render cache,
    so the next consumer finds the current image and the flag cleared.

    Returns ``None`` when nothing was needed or the render succeeded, else a
    human-readable failure the caller must surface (the flag/stale key stays,
    so the next consume retries)."""
    meta = getattr(chunk, "meta", None) or {}
    if (meta.get("render") or {}).get("kind") == "view":
        early = _view_fresh(store, meta)
        if early is not _STALE:
            return early
    elif not needs_render(store, chunk):
        return None
    outcome = render_figure_chunk(store, chunk.chunk_id)
    if not outcome.ok:
        return _failure_message(outcome.error, outcome.detail)
    store.drafts.clear_render_pending(chunk.chunk_id)
    return None
