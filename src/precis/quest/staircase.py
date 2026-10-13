"""Surface-Pourbaix slice 3 — structure-side helpers for the linked
reaction-staircase panel.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(§7.2 map → staircase; the campaign carry-over asks for the resting
termination at U and the coads-mismatch verdict). Lives in core for the
same reason :mod:`precis.quest.pourbaix_map` does: the structure
``view='staircase'`` surface is served by a core handler, and core may not
import the plugin that owns the affine adapter
(``precis_pathway.staircase_routes`` — ``tests/test_plugin_import_boundary.py``).
Everything here reads plain dicts and link rows — no engine call.

* a structure's pathways are the ``related-to`` links the harvest wires
  (``compute._link_pathway``: structure → pathway); "best" is a documented
  ranking, not a quest verdict — renderable (carries ``che_affine``) first,
  then the fidelity tier, then newest;
* :func:`resting_region` answers "which termination does this map rest on
  at U" — the §7.2 starting-surface link between the map and the
  staircase;
* :func:`mismatch` is the campaign's coads verdict: the pathway was
  computed on the candidate's spec (``meta.params.coads``), so when the
  map's resting termination at U differs, the staircase sits on the wrong
  starting surface and the view says so rather than silently compositing.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref

#: Pathway ``meta.tier`` → fidelity rank for :func:`best_pathway` (mirrors
#: ``compute._TIER_FIDELITY``; unknown/absent tiers rank below screening —
#: a pre-ladder pathway is evidence, just not the best one).
_TIER_RANK = {"screening": 1, "neb": 2, "verify": 3}

#: Cap on the linked-structure rows a staircase view walks (content-keyed
#: pathways can be shared across many candidates — one resting verdict per
#: structure is the signal, not a census).
MAX_LINKED_STRUCTURES = 4


def linked_pathways(store: Store, structure_ref_id: int) -> list[Ref]:
    """Live ``pathway`` refs linked ``related-to`` from this structure
    (``compute._link_pathway`` wires structure → pathway), newest first."""
    links = store.links_for(
        int(structure_ref_id), direction="out", relation="related-to"
    )
    ids = {int(ln.dst_ref_id) for ln in links}
    out = [
        r
        for r in store.fetch_refs_by_ids(ids).values()
        if r is not None and r.kind == "pathway"
    ]
    out.sort(key=lambda r: -int(r.id))
    return out


def structures_for_pathway(store: Store, pathway_ref_id: int) -> list[Ref]:
    """Live ``structure`` refs linked ``related-to`` to this pathway,
    capped at :data:`MAX_LINKED_STRUCTURES` (newest first)."""
    links = store.links_for(int(pathway_ref_id), direction="in", relation="related-to")
    ids = {int(ln.src_ref_id) for ln in links}
    out = [
        r
        for r in store.fetch_refs_by_ids(ids).values()
        if r is not None and r.kind == "structure"
    ]
    out.sort(key=lambda r: -int(r.id))
    return out[:MAX_LINKED_STRUCTURES]


def best_pathway(store: Store, structure_ref_id: int) -> Ref | None:
    """The pathway the staircase panel renders for this structure.

    Ranked, documented, not a quest verdict: ready pathways only; a
    ``che_affine``-carrying (engine ≥ 0.25.0) result beats one without
    (there is no staircase to draw without it); then the fidelity tier
    (``verify`` > ``neb`` > ``screening``); then newest. ``None`` when no
    ready pathway is linked."""

    def key(ref: Ref) -> tuple[int, int, int]:
        meta = ref.meta or {}
        res = meta.get("results")
        affine = (
            1
            if isinstance(res, dict) and isinstance(res.get("che_affine"), dict)
            else 0
        )
        return (
            affine,
            _TIER_RANK.get(str(meta.get("tier") or ""), 0),
            int(ref.id),
        )

    ready = [
        r
        for r in linked_pathways(store, structure_ref_id)
        if (r.meta or {}).get("status") == "ready"
    ]
    return max(ready, key=key, default=None)


def quest_operating_u(store: Store, structure_ref_id: int) -> float | None:
    """The first serving quest's ``meta.operating_conditions.U_RHE`` —
    the human-set operating point the staircase defaults to. ``None``
    when no serving quest declares one."""
    links = store.links_for(int(structure_ref_id), direction="out", relation="serves")
    quests = store.fetch_refs_by_ids({int(ln.dst_ref_id) for ln in links})
    for q in quests.values():
        if q is None or q.kind != "quest":
            continue
        u = (q.meta or {}).get("operating_conditions", {})
        u = u.get("U_RHE") if isinstance(u, dict) else None
        if isinstance(u, int | float) and not isinstance(u, bool) and math.isfinite(u):
            return float(u)
    return None


def pathway_operating_u(
    config: dict[str, Any] | None, results: dict[str, Any] | None
) -> float:
    """A pathway's recorded operating potential: ``conditions.potential_U``
    first (the human-set point), then ``electrochemistry.U_vs_RHE`` —
    ``"optimal"`` resolves to the engine's own ``U_opt``. ``0.0`` when
    nothing is recorded."""
    cfg = config or {}
    for raw in (
        (cfg.get("conditions") or {}).get("potential_U"),
        (cfg.get("electrochemistry") or {}).get("U_vs_RHE"),
    ):
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int | float) and math.isfinite(raw):
            return float(raw)
        if raw == "optimal":
            u = (results or {}).get("U_opt")
            if isinstance(u, int | float) and not isinstance(u, bool):
                return float(u)
    return 0.0


def resting_region(m: dict[str, Any], u: float) -> dict[str, Any] | None:
    """The validity region of one map covering ``u`` (V vs RHE), or
    ``None`` outside every region."""
    for r in m.get("regions") or []:
        rng = r.get("U_RHE_range") or [None, None]
        if rng[0] is None or rng[1] is None:
            continue
        if rng[0] - 1e-9 <= float(u) <= rng[1] + 1e-9:
            return r
    return None


def resting_label(m: dict[str, Any], u: float) -> str | None:
    """``'H* at 1.00 ML'`` — the map's resting termination at U, or
    ``None`` when no region covers it."""
    r = resting_region(m, u)
    return None if r is None else str(r.get("label") or "?")


def coads_of(meta: dict[str, Any]) -> dict[str, int]:
    """The candidate spec's coadsorbates ``{species: count}`` —
    ``meta.params.coads`` (``compute.params_from_spec``). ``{}`` = the
    pathway assumed a clean surface."""
    params = (meta or {}).get("params")
    coads = params.get("coads") if isinstance(params, dict) else None
    if not isinstance(coads, dict):
        return {}
    return {str(k): int(v) for k, v in coads.items() if isinstance(v, int | float)}


def assumed_label(coads: dict[str, int]) -> str:
    """``'clean'`` or ``'H×9 + O×1'`` — what the pathway's slab spec carried."""
    if not coads:
        return "clean"
    return " + ".join(f"{sp}×{n}" for sp, n in sorted(coads.items()))


def region_coads(region: dict[str, Any] | None) -> dict[str, int]:
    """The resting termination as a ``{species: count}`` dict so it can be
    compared against ``meta.params.coads`` — ``{}`` for the clean region."""
    term = (region or {}).get("termination")
    if not isinstance(term, dict):
        return {}
    ads = term.get("adsorbate")
    n = term.get("n")
    if not isinstance(ads, str) or not ads or not isinstance(n, int | float):
        return {}
    return {ads: int(n)}


def mismatch(
    region: dict[str, Any] | None, coads: dict[str, int], *, u: float, label: str | None
) -> str | None:
    """The coads verdict (campaign carry-over): ``None`` when the map's
    resting termination at ``u`` equals what the pathway's slab spec
    assumed; otherwise the one-line "the staircase starts from the wrong
    surface" warning."""
    if region is None:
        return None
    actual = region_coads(region)
    if actual == coads:
        return None
    return (
        f"coads mismatch: the pathway assumed {assumed_label(coads)}, but the "
        f"resting termination at {u:+.2f} V is {label or '?'} — the staircase "
        "below sits on a different starting surface than the map says exists"
    )


__all__ = [
    "MAX_LINKED_STRUCTURES",
    "assumed_label",
    "best_pathway",
    "coads_of",
    "linked_pathways",
    "mismatch",
    "pathway_operating_u",
    "quest_operating_u",
    "region_coads",
    "resting_label",
    "resting_region",
    "structures_for_pathway",
]
