"""Linearise the quest graph with its current frontier evidence."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from functools import partial
from itertools import chain
from typing import TYPE_CHECKING

from precis.quest.directions import (
    _render_ledger,
    quest_evidence,
    read_direction_forest,
)
from precis.quest.figures import (
    _profile_root_target,
    pathway_profile_figure,
    quest_pareto_figure,
)
from precis.quest.frontier import _SCATTER_MIN_POINTS, pareto_axes, quest_frontier
from precis.render.figure import _render_view_png
from precis.response import Response, ResponseImage
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.quest.frontier import Candidate, FrontierResult
    from precis.store import Ref, Store

log = logging.getLogger(__name__)
_MAX_CASES = 4


def _figure_cases(
    fr: FrontierResult, x_measure: str, y_measure: str
) -> list[Candidate]:
    """Frontier members ordered by relevance — normalised distance to the
    utopia corner (each axis's best value per its declared sense) over the
    frontier cohort's own spans, mirroring
    :func:`precis.quest.figures._rank_by_frontier_distance`'s
    empty-frontier utopia fallback. Members missing an axis measure (an
    ``optional:`` objective the candidate never measured) sort last by ref
    id — still frontier members, still embedded, just ranked below the
    plottable optima."""
    senses = dict(fr.objectives)
    plottable: list[Candidate] = []
    rest: list[Candidate] = []
    for c in fr.frontier:
        if (
            c.measures.get(x_measure) is not None
            and c.measures.get(y_measure) is not None
        ):
            plottable.append(c)
        else:
            rest.append(c)
    tail = sorted(rest, key=lambda c: c.ref_id)
    if not plottable:
        return tail
    xs = [float(c.measures[x_measure]) for c in plottable]
    ys = [float(c.measures[y_measure]) for c in plottable]
    best_x = max(xs) if senses.get(x_measure) == "max" else min(xs)
    best_y = max(ys) if senses.get(y_measure) == "max" else min(ys)
    span_x = max(max(xs) - min(xs), 1e-12)
    span_y = max(max(ys) - min(ys), 1e-12)

    def _utopia_distance(c: Candidate) -> float:
        return math.hypot(
            (float(c.measures[x_measure]) - best_x) / span_x,
            (float(c.measures[y_measure]) - best_y) / span_y,
        )

    plottable.sort(key=lambda c: (_utopia_distance(c), c.ref_id))
    return [*plottable, *tail]


def _slab_source(store: Store, candidate: Ref, pathway: Ref | None) -> tuple[int, str]:
    """Prefer the pathway's relaxed starting adsorbate when it was ingested."""
    if pathway is not None:
        meta = pathway.meta or {}
        root, _ = _profile_root_target(
            meta.get("graph") or {}, meta.get("results") or {}
        )
        state_id = (meta.get("structure_refs") or {}).get(root)
        if type(state_id) is int:
            state = store.get_ref(kind="structure", id=state_id)
            if state is not None and state.retired_at is None:
                return state_id, f" — {root} [st{state_id}]"
    return candidate.id, ""


def render_synthesis(store: Store, quest: Ref) -> Response:
    """Read directions and evidence from the graph and attach live renders.

    Allocate figure numbers only after rendering so captions always index
    the corresponding MCP image blocks. A failed renderer affects only its
    own figure and is retried on the next read.
    """
    body = [f"# Synthesis — qu{quest.id}", quest.title]
    directions = read_direction_forest(store, quest.id)
    if directions:
        body.append(_render_ledger(directions))
    sources = []
    for ref in quest_evidence(store, quest.id):
        handle = handle_registry.try_format(ref.kind, ref.id) or f"{ref.kind}:{ref.id}"
        title = ref.title.splitlines()[0] if ref.title else ""
        sources.append(f"- [{handle}] {title}")
    if sources:
        body.append("## Evidence\n\n" + "\n".join(sources))

    images: list[ResponseImage] = []
    captions: list[str] = []

    def add_figure(caption: str, render: Callable[[], bytes | None]) -> None:
        try:
            png = render()
        except Exception:
            log.exception("Quest %s failed to render %s", quest.id, caption)
            png = None
        if png is None:
            captions.append(f"- {caption} (render unavailable)")
            return
        images.append(ResponseImage(data=png, mime="image/png"))
        captions.append(f"- Figure {len(images)}. {caption}")

    fr = quest_frontier(store, quest.id)
    x, y, x_label, y_label = pareto_axes(quest.meta, fr.objectives, None, None)
    measures = chain(
        (point.measures for point in fr.frontier),
        (point.measures for point in fr.dominated),
        (point.measures for point in fr.provisional),
    )
    plottable = sum(
        values.get(x) is not None and values.get(y) is not None for values in measures
    )
    if plottable >= _SCATTER_MIN_POINTS:
        add_figure(
            f"Pareto frontier — {x_label} vs {y_label}",
            lambda: quest_pareto_figure(store, quest, label="handles", frontier=fr)[0],
        )

    cases = _figure_cases(fr, x, y)[:_MAX_CASES]
    candidate_refs = (
        store.fetch_refs_by_ids({case.ref_id for case in cases}) if cases else {}
    )
    seen_pathways: set[int] = set()

    def render_slab(ref_id: int) -> bytes | None:
        scene, _labels = store.structure_load(ref_id)
        return _render_view_png(scene, {"refine": 2})

    def render_profile(pathway: Ref) -> bytes:
        return pathway_profile_figure(store, pathway)[0]

    for case in cases:
        candidate = candidate_refs.get(case.ref_id)
        if candidate is None or candidate.retired_at is not None:
            continue
        source = f"{case.name} [{case.handle}]"
        pid = (candidate.meta or {}).get("barrier_pathway_ref_id")
        pathway = store.get_ref(kind="pathway", id=pid) if type(pid) is int else None
        if pathway is not None and pathway.retired_at is not None:
            pathway = None
        slab_id, slab_state = _slab_source(store, candidate, pathway)
        add_figure(
            f"Slab for frontier case {source}{slab_state}",
            partial(render_slab, slab_id),
        )
        if pathway is not None and pathway.id not in seen_pathways:
            seen_pathways.add(pathway.id)
            add_figure(
                f"Energy profile for {source} — {pathway.title} [pw{pathway.id}]",
                partial(render_profile, pathway),
            )
    if captions:
        body.append("## Frontier figures\n\n" + "\n".join(captions))
    return Response(body="\n\n".join(body), images=tuple(images))
