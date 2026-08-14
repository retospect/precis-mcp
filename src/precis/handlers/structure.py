"""StructureHandler — the atomistic cell + bond-graph kind.

A ``structure`` design is a slug-addressed ref: a periodic cell (on
``refs.meta``) filled with atoms + a bond graph (the ``struct_*`` tables). The
LLM authors it as typed *ops* and reads it via *probes*, never pixels. Maps onto
the seven-verb surface:

- ``put``    — create/replace from a JSON spec ``{cell, ops}`` (``id=`` slug).
- ``edit``   — apply more ops to an existing design (``ops=`` or ``text=`` JSON).
- ``get``    — list designs, a design's TOC (``id=slug``), a probe
  (``view='atom'|'neighborhood'|'bonds'|'find'|'validate'``), a navigation
  probe (``view='line'|'plane'|'bonds_through_plane'|'bonds_in_sphere'|'path'|
  'rings'|'fragments'|'diff'|'pov'``), or an export — all with ``args=``.
- ``delete`` — soft-retire a whole design.

The relaxer/DFT and file export (CIF/POSCAR/XYZ) are rented backends
(:mod:`precis.structure.relax`). A relax that would dispatch to the GPU
first runs ``validate()`` as a **hard-reject** (overlap / over-valence /
impossibly-long declared bond mints no job), then a local ``clean`` (or
opt-in ``preflight='emt'``) pre-relax and a ``cache_key`` re-check — cloud
is last-resort; a plain local ``clean``/``emt`` edit is never gated.
``put``/``edit`` also run the tier-0 MLIP preflight
(:mod:`precis.structure.preflight`, dark by default) and reject + undo a
failing edit before the version commits.

``view='literature'`` assembles a deterministic paper query from the design
(description + host metals / adsorbate / facet from ``scene.composition()``)
and runs the shared paper search; paper-provenance links + a rationale note
(``link(..., note=…)`` → ``links.meta``) show *why* a design was made.

See ``precis-structure-help``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import render_agent_table
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    format_link_tag_ack,
    require_link_target,
    validate_link_mode,
)
from precis.handlers._placement import RESERVED_PARENT_REL, place_ref
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.structure import (
    OpError,
    RelaxUnsupported,
    Scene,
    apply_ops,
    evaluate_measure,
    export,
    probe,
    validate,
)
from precis.structure import cache as relax_cache
from precis.structure import relax as run_relax
from precis.structure.cell import Cell
from precis.structure.importers import catalysis_hub, get_adapter
from precis.structure.preflight import PreflightReason
from precis.structure.preflight import _preflight_enabled as _mlip_preflight_enabled
from precis.structure.preflight import preflight as _mlip_preflight

# NB: ``precis.structure`` re-exports the ``relax`` *function* under that
# name (see ``run_relax`` above), shadowing the submodule — reach
# ``EMT_ELEMENTS`` via the submodule path directly.
from precis.structure.relax import EMT_ELEMENTS, RelaxResult, estimate_forces_emt
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)

_PROBE_VIEWS = ("atom", "neighborhood", "bonds", "find", "validate")
_NAV_VIEWS = (
    "line",
    "plane",
    "bonds_through_plane",
    "bonds_in_sphere",
    "path",
    "rings",
    "fragments",
    "diff",
    "pov",
)
_EXPORT_VIEWS = ("poscar", "extxyz", "cif")
_VIEWS = (
    *_PROBE_VIEWS,
    *_NAV_VIEWS,
    "runs",
    "markers",
    "links",
    "literature",
    *_EXPORT_VIEWS,
)

#: Host-metal candidates for the deterministic ``view='literature'`` query
#: heuristic (gr161578) — mirrors the metal/support elements the catalysis-hub
#: adapter's ``_SYMBOLS`` table carries (structure/importers/catalysis_hub.py),
#: minus the light adsorbate/support elements that table also lists (H/C/N/O/
#: Na/Al/Si/K). Kept as a local literal rather than importing the importer's
#: private symbol map, so this module stays decoupled from that one.
_HOST_METAL_ELEMENTS = frozenset(
    {
        "Ti", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Zr", "Mo",
        "Ru", "Rh", "Pd", "Ag", "W", "Re", "Ir", "Pt", "Au",
    }
)  # fmt: skip


@dataclass
class _NeedsDispatch:
    """An energy-rung relax that missed the §23.16 cache and has no local
    backend — it must run on the GPU node as a ``struct_relax`` job (§23.12).
    Carries everything the dispatch needs: the content address + the staged
    input geometry + the canonical / POSCAR-row orderings for the write-back.

    ``requester_id`` is the optional todo that *asked for* this relax and
    wants to block on it (compute lane). The job parents on the
    structure regardless; when a requester is named, the dispatch also
    writes a ``requested`` link + a ``derived_job_succeeded`` auto_check so
    the todo closes on completion / bubbles on failure.

    ``preflight_result`` is the cheap local relax (``clean``, or ``emt`` when
    opted in) run on the scene before it was staged — a last-resort-cloud
    guardrail (gripe 51393): mild clashes get repaired for free before the
    GPU node ever sees the geometry. ``poscar``/``poscar_labels``/
    ``cache_key``/``structure_sha``/``order`` are all taken from the
    *post-preflight* scene, so the recorded address matches what's actually
    dispatched. ``preflight_note`` carries a legible fallback explanation
    (e.g. ``emt`` requested but unavailable) — ``None`` when nothing needed
    explaining."""

    fidelity: str
    model: str
    steps: int
    cache_key: str
    structure_sha: str
    order: list[str]
    poscar: str
    poscar_labels: list[str]
    requester_id: int | None
    #: Variable-cell relax mode ('inplane'/'full') or None for atoms-only.
    cell: str | None = None
    preflight_result: RelaxResult | None = None
    preflight_note: str | None = None


def _poscar_row_labels(scene: Scene) -> list[str]:
    """Atom labels in the row order ``export.to_poscar`` emits (element-grouped),
    so a relaxed POSCAR's rows map back to labels → canonical rank."""
    order, groups = export._grouped(scene)
    return [a.label for el in order for a in groups[el]]


def _format_gate_rejection(findings: list[Any]) -> str:
    """One BadInput message for every validator finding blocking a cloud
    relax dispatch (gripe 51393) — names each offending atom pair and why,
    never just "invalid geometry"."""
    lines = [
        f"structure fails the pre-dispatch validator "
        f"({len(findings)} finding(s)) — refusing to mint a cloud relax job:"
    ]
    lines += [f"  [{f.rule}] {f.suggested_fix}" for f in findings]
    return "\n".join(lines)


def _format_preflight_rejection(reasons: list[PreflightReason]) -> str:
    """One BadInput message for every Tier-0 preflight reason (element/clash/
    detached/vacuum/porosity) blocking an edit — names each offending atom
    and why, mirroring :func:`_format_gate_rejection`'s shape."""
    lines = [f"structure preflight rejected this edit ({len(reasons)} issue(s)):"]
    lines += [r.message for r in reasons]
    return "\n".join(lines)


def _run_preflight_gate(scene: Scene) -> None:
    """Tier-0 MLIP preflight — hard reject an edit whose *resulting* scene
    fails (element out of the MLIP's coverage, a clash, a floating
    adsorbate, no vacuum headroom, a porous slab). Only runs when
    ``PRECIS_STRUCTURE_PREFLIGHT`` is on (:func:`_mlip_preflight_enabled`,
    default OFF); called after ops are applied to the in-memory scene but
    *before* ``structure_save`` commits, so a rejection leaves the prior
    version standing (transactional reject + undo — nothing to roll back).

    Fail-**open** on infra (ASE/[dft] missing, or any other unexpected
    preflight-internal error): a preflight that can't run must not block an
    edit. Fail-**closed** only on a real ``not ok`` verdict, which raises
    :class:`BadInput` — the caller does not catch this."""
    if not _mlip_preflight_enabled():
        return
    try:
        verdict = _mlip_preflight(scene)
    except Exception as exc:  # ImportError (no ASE/[dft]) or any other infra hiccup
        log.debug("structure preflight degraded (fail-open): %s", exc)
        return
    if not verdict.ok:
        raise BadInput(
            _format_preflight_rejection(verdict.reasons),
            next="fix the flagged atom(s) (swap element / reposition / "
            "pack the slab) and re-apply the edit",
        )


def _relax_cache_address(
    scene: Scene, *, fidelity: str, model: str, steps: int, cell_mode: str | None
) -> tuple[str, str, list[str]]:
    """The ``(cache_key, structure_sha, canonical_order)`` triple addressing a
    relax of ``scene`` at this fidelity/model/params.

    Called twice on the dispatch-with-no-local-backend path: once over the
    as-authored scene (the early cache-hit lookup, §23.16) and again over the
    pre-relax-cleaned scene actually staged for the cloud job (gripe 51393) —
    so the address that lands on the ``struct_relax`` job / run-cube row
    always matches the geometry that was really computed."""
    params: dict[str, Any] = {"steps": steps}
    if cell_mode is not None:
        params["cell"] = cell_mode
    cache_key = relax_cache.run_cache_key(
        scene, fidelity=fidelity, model=model, params=params
    )
    structure_sha = relax_cache.structure_sha(scene)
    order = relax_cache.canonical_order(scene)
    return cache_key, structure_sha, order


def _preflight_relax(
    scene: Scene, mode: str, *, steps: int
) -> tuple[RelaxResult, str | None]:
    """The cheap local pre-relax that always runs before a heavy relax is
    staged for cloud dispatch (gripe 51393) — turns cloud compute into a
    last resort by repairing mild clashes for free first. ``clean`` (rung 0,
    pure geometry repair) is the default; ``mode='emt'`` opts into ASE's EMT
    rung instead when the element set is covered and ASE is installed.

    Never raises: an ``emt`` request outside its closed element set, or with
    no local ASE, falls back to ``clean`` with a legible ``preflight_note``
    rather than blocking the dispatch."""
    if mode != "emt":
        return run_relax(scene, fidelity="clean", steps=steps), None
    bad = {a.element for a in scene.atoms.values()} - EMT_ELEMENTS
    if bad:
        note = (
            f"preflight='emt' skipped (elements {sorted(bad)} outside EMT's "
            "coverage) — fell back to 'clean'"
        )
        return run_relax(scene, fidelity="clean", steps=steps), note
    if not export.ase_available():
        note = "preflight='emt' skipped (ASE not installed) — fell back to 'clean'"
        return run_relax(scene, fidelity="clean", steps=steps), note
    try:
        return run_relax(scene, fidelity="emt", steps=steps), None
    except RelaxUnsupported as exc:
        note = f"preflight='emt' failed ({exc}) — fell back to 'clean'"
        return run_relax(scene, fidelity="clean", steps=steps), note


def _as_int_or_none(v: Any) -> int | None:
    """Coerce a relax-op requester id to int, tolerating a string id."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _vec(args: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Pull a 3-vector arg (list or comma string) as a numpy array.

    Accepts several alias keys (first present wins) so callers needn't chain
    ``a or b`` — numpy arrays are not truthy.
    """
    raw = default
    for key in keys:
        if key in args and args[key] is not None:
            raw = args[key]
            break
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [float(x) for x in raw.replace(",", " ").split()]
    return np.asarray(raw, dtype=float)


def _build_cell(spec: dict[str, Any]) -> Cell:
    pbc = tuple(spec.get("pbc", (True, True, True)))
    if "lattice" in spec:
        return Cell(np.array(spec["lattice"], dtype=float), pbc)
    try:
        return Cell.from_lengths_angles(
            float(spec["a"]),
            float(spec["b"]),
            float(spec["c"]),
            float(spec.get("alpha", 90.0)),
            float(spec.get("beta", 90.0)),
            float(spec.get("gamma", 90.0)),
            pbc,
        )
    except KeyError as exc:
        raise BadInput(
            f"cell needs 'lattice' or a/b/c (missing {exc})",
            next="cell={'a':8.4,'b':8.4,'c':24,'pbc':[true,true,false]}",
        ) from None


#: Ops that establish the cell themselves, so a top-level ``cell`` is optional.
_CELL_ESTABLISHING_OPS = frozenset({"slab", "set_cell"})


def _ops_establish_cell(ops: Any) -> bool:
    """True if ``ops`` contains an op that seeds the cell (``slab``/``set_cell``)."""
    return isinstance(ops, list) and any(
        isinstance(o, dict) and o.get("op") in _CELL_ESTABLISHING_OPS for o in ops
    )


def _payload(text: str | None, args: dict[str, Any] | None) -> dict[str, Any]:
    if args:
        return dict(args)
    if text and text.strip():
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BadInput(f"structure payload must be JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise BadInput("structure payload must be a JSON object {cell, ops}")
        return obj
    return {}


# ── provenance / method-fingerprint guards (migration 0084) ──
#
# An imported design carries a ``struct_runs`` row with ``provenance =
# 'external'`` and a ``method`` fingerprint (functional/cutoff/spin/…) —
# it is a faithful mirror of a source dataset entry (OC20/OC22, Materials
# Project, …), not something we computed. Two consequences live here:
#
#   * the design stays read-only (``edit`` and ``put`` refuse — see the
#     ``_external_provenance`` guard); all work branches via ``derive``.
#   * an energy delta across two runs is only meaningful when both runs
#     share a method — mixing functionals/cutoffs, or an external energy
#     against a differently-modeled computed relax, is a category error,
#     not a real ΔE. ``guard_energy_comparable`` is the reusable check; no
#     ΔE surface exists in this handler yet, so it isn't wired to a verb —
#     a future one (pathway barriers, cross-run energy tables) should call
#     it before subtracting two energies.


def _format_method_fingerprint(method: Mapping[str, Any] | None) -> str:
    """Compact display of an external run's method fingerprint, e.g.
    ``PBE/500eV``. ``—`` when there's no fingerprint to show (a computed row,
    or an external row that somehow recorded none)."""
    if not method:
        return "—"
    parts: list[str] = []
    if "functional" in method:
        parts.append(str(method["functional"]))
    if "cutoff_eV" in method:
        parts.append(f"{method['cutoff_eV']}eV")
    if not parts:
        parts = [f"{k}={v}" for k, v in sorted(method.items())]
    return "/".join(parts)


def _describe_run_forces(forces: Mapping[str, Any] | None) -> str:
    """Compact ``view='runs'`` label for a run's ``forces`` payload (gripe
    161576) — ``approx (emt)`` for the cheap clean-rung estimate, ``yes
    (<source>)`` for a real emt/ml relax force, ``—`` when none was recorded."""
    if not forces or not forces.get("vectors"):
        return "—"
    source = forces.get("source") or "?"
    return f"approx ({source})" if forces.get("approx") else f"yes ({source})"


def _describe_run_method(run: Mapping[str, Any]) -> str:
    """Human-readable method label for one side of a ΔE comparison error —
    an external run's fingerprint (``PBE/500eV``), or a computed run's
    model (``model=mace``)."""
    if run.get("provenance") == "external":
        return _format_method_fingerprint(run.get("method"))
    return f"model={run.get('model') or '?'}"


def _method_key(run: Mapping[str, Any]) -> tuple[str, Any]:
    """The comparability key for a run's energy: two runs are
    only safely subtracted for a ΔE when this matches — the same computed
    model, or an identical external method fingerprint. Provenance alone
    isn't enough (two 'external' rows from different functionals still
    collide)."""
    if run.get("provenance") == "external":
        method = run.get("method") or {}
        return ("external", tuple(sorted(method.items())))
    return ("computed", run.get("model"))


# ── calculator identity (gripe 161576 remainder) ──
#
# Energetics surface everywhere (view='atom', pathway barriers) but never
# said WHAT produced the numbers. ``format_calc_identity`` is the one place
# that turns a struct_runs row into the compact "calc: …" label both
# view='atom' below and the pathway web explorer
# (precis_web.routes.refs._pathway_state_calc_identities) show — a shared
# pure function so the two surfaces can't drift on how they describe a run.

#: Keys worth surfacing from a computed run's ``params`` jsonb in the
#: ``calc:`` header — a hand-picked, load-bearing subset; most of what
#: lands in ``params`` today (e.g. ``cached``) is audit bookkeeping, not
#: part of the calculator's identity, and the column is otherwise a
#: free-form jsonb blob not worth dumping wholesale.
_CALC_PARAM_DIGEST_KEYS = ("steps", "fmax", "cutoff_eV", "kmesh", "spin", "cell")


def _format_params_digest(params: Mapping[str, Any] | None) -> str:
    """Comma-joined ``key=value`` for the few ``_CALC_PARAM_DIGEST_KEYS``
    present in ``params`` — ``''`` when none of them were recorded (most
    runs today carry an empty or near-empty ``params``, e.g. just
    ``{"cached": True}``, which isn't load-bearing enough to show here)."""
    if not params:
        return ""
    parts = [
        f"{k}={params[k]}" for k in _CALC_PARAM_DIGEST_KEYS if params.get(k) is not None
    ]
    return ", ".join(parts)


def format_calc_identity(run: Mapping[str, Any] | None) -> str | None:
    """Compact ``calc: …`` header naming what produced a run's numbers.

    A computed run (our relax/NEB pipeline, migration 0043) shows its
    ``model`` plus a short digest of the few load-bearing ``params``. An
    external row (an imported OC20/Materials Project entry, migration 0084)
    shows its method fingerprint (functional/cutoff/kmesh — whichever were
    recorded) plus an explicit ``external`` marker and its ``dataset_doi``
    when present, so an imported PBE energy is never mistaken for one we
    computed. ``None`` when ``run`` is ``None`` — no run row, no line, never
    invented.

    ``run`` is the plain ``{"provenance", "model", "params", "method"}``
    shape each call site assembles from its own ``struct_runs`` read."""
    if run is None:
        return None
    if run.get("provenance") == "external":
        method = run.get("method") or {}
        parts = []
        if method.get("functional"):
            parts.append(str(method["functional"]))
        if method.get("cutoff_eV") is not None:
            parts.append(f"{method['cutoff_eV']}eV")
        if method.get("kmesh"):
            parts.append(f"kmesh {method['kmesh']}")
        fingerprint = "/".join(parts) if parts else "?"
        doi = method.get("dataset_doi")
        tail = f"external — {doi}" if doi else "external"
        return f"calc: {fingerprint} — {tail}"
    model = run.get("model") or "?"
    digest = _format_params_digest(run.get("params"))
    return f"calc: {model}" + (f" ({digest})" if digest else "")


def guard_energy_comparable(run_a: Mapping[str, Any], run_b: Mapping[str, Any]) -> None:
    """Refuse a ΔE across two runs produced by different methods — mixing functionals/cutoffs, or an external dataset energy against
    a differently-modeled computed relax, is a category error, not a real
    energy difference.

    Geometry/graph comparisons (RMSD, bond formed/broken — ``view='diff'``)
    are method-agnostic and unaffected; this guard is only for scalar
    energy deltas. Raises :class:`BadInput` on a mismatch, returns silently
    on a match."""
    if _method_key(run_a) != _method_key(run_b):
        raise BadInput(
            "energies not comparable across methods: "
            f"{_describe_run_method(run_a)} vs {_describe_run_method(run_b)} "
            "— this ΔE is a category error",
            next="compare runs sharing a method/model, or use view='diff' "
            "for a method-agnostic geometry comparison",
        )


# ── on-demand hydrate from an external catalyst DB ──────────
#
# ``get(kind='structure', source='catalysis-hub', ...)`` is the "quest
# worker pokes around and pulls real substrates" surface: resolve a config
# from the source, first-touch hydrate it via the adapter into an ordinary
# (searchable, cited) ``structure`` ref through ``store.structure_import``
# (idempotent — T3), and a repeat lookup by the same ``config_id=`` is a
# cache hit with no refetch. Only ``catalysis-hub`` (T5) has a fetch layer
# wired today; a source with a registered *adapter* but no fetch layer
# raises a clear BadInput rather than silently doing nothing.

#: fetch_config's own filter kwargs, straight off the handler's args=.
_CATALYSIS_HUB_FILTER_KEYS = ("surface_composition", "facet", "first")

#: Best-effort ``q=`` parse for the common "<adsorbate> on <El><facet>"
#: phrasing (e.g. "NO on Pd(111)") — only used when no explicit filter arg
#: is given. Anything fancier is left to a future slice; this is a
#: convenience, not a query language.
_CATALYSIS_HUB_Q_RE = re.compile(r"\bon\s+([A-Za-z]{1,2})\s*\(?\s*(\d{1,3})\s*\)?")


def _resolve_catalysis_hub_filters(
    q: str | None, args: Mapping[str, Any]
) -> dict[str, Any]:
    """Map the handler's ``q=``/explicit filter args onto ``fetch_config``'s
    kwargs (``surface_composition=``/``facet=``/``first=``)."""
    filters: dict[str, Any] = {
        k: args[k] for k in _CATALYSIS_HUB_FILTER_KEYS if args.get(k) is not None
    }
    if not filters and q:
        m = _CATALYSIS_HUB_Q_RE.search(q)
        if m:
            filters["surface_composition"] = m.group(1).capitalize()
            filters["facet"] = m.group(2)
    return filters


# ── paper-provenance links (gr161577, structure → paper) ──────────────────
#
# Ordinary generic links (``link(kind='structure', target='paper:<slug>',
# rel='cites')`` — or any other registered relation) already carry a
# design's "why" — this is just the shared read side: pull the design's
# outbound links whose target is a paper, with the paper's title/DOI and
# the link's own rationale note (``links.meta['note']``, see
# ``StructureHandler.link``'s ``note=``).
#
# NB: a ``motivated-by`` relation reading better for this use-case doesn't
# exist yet — ``links.relation`` FKs against a seeded ``relations`` table
# (doesn't touch it), so minting it needs a migration. Deferred;
# ``cites``/``related-to``/``derived-from`` already cover the "why" today.
# Free-standing (not a method) so both the TOC (below) and the web detail
# route (``precis_web/routes/structure.py``) can call it without importing
# the handler class.


def paper_provenance_rows(store: Any, ref_id: int) -> list[dict[str, str]]:
    """Paper-provenance links off a structure design: ``[{rel, paper, slug,
    doi, note}, ...]``, newest-link-first is not guaranteed — callers that
    care about order should sort. Empty list when there are none."""
    out_links = store.links_for(ref_id, direction="out")
    if not out_links:
        return []
    endpoints = store.fetch_refs_by_ids({lk.dst_ref_id for lk in out_links})
    rows: list[dict[str, str]] = []
    for lk in out_links:
        target = endpoints.get(lk.dst_ref_id)
        if target is None or target.kind != "paper":
            continue
        rows.append(
            {
                "rel": lk.relation,
                "paper": target.title or target.slug or str(target.id),
                "slug": target.slug or str(target.id),
                "doi": (target.meta or {}).get("doi") or "",
                "note": (lk.meta or {}).get("note") or "",
            }
        )
    return rows


class StructureHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="structure",
        title="Structure",
        description=(
            "Atomistic cell + bond-graph design. put creates/replaces "
            "from JSON {cell:{a,b,c,pbc}|{lattice,pbc}, ops:[...]} (cell optional "
            "when ops start with a `slab` bulk template); edit applies "
            "more ops (slab/set_cell/add_atom/set_element/vacancy/displace/add_bond/"
            "remove_bond/constrain, plus eye/measure/unmark/remove_measure "
            "markers); get lists designs, shows a TOC (id=slug), or probes "
            "(view='atom|neighborhood|bonds|find|validate|markers', args={...}); "
            "view='literature' assembles a deterministic paper-search query "
            "from the design's own composition/description (no LLM) and runs "
            "it against the paper corpus (gr161578); "
            "get(args={'source':'catalysis-hub', 'surface_composition':.., "
            "'facet':.., 'config_id':..}) on-demand hydrates a config from an "
            "external catalyst DB into a cited, read-only design "
            "(a repeat call by config_id is a cache hit, no refetch); "
            "link relates designs (rel='derived-from') or papers that "
            "motivated the design (rel='cites', note='rationale…', "
            "gr161577; surfaced in the TOC's Provenance: section); "
            "delete soft-retires. "
            "Atoms are a<El><n>, addressed st<id>#a<El><n>. "
            "Postgres-canonical, in-memory probes, no pixels. "
            "See precis-structure-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_link=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=False,
        role="artifact",
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("structure: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='structure') requires id= (the design slug)",
                next="put(kind='structure', id='pd111', text='{\"cell\":{\"a\":8.4,"
                '"b":8.4,"c":24,"pbc":[true,true,false]},"ops":[]}\')',
            )
        slug = str(id).strip()
        existing = self.store.get_ref(kind="structure", id=slug)
        if existing is not None and self._external_provenance(existing.id) is not None:
            # An imported reference config is a read-only mirror:
            # `put` targeting its slug would overwrite the geometry in place via
            # structure_save's existing-branch while the external `struct_runs`
            # row keeps describing the old atoms — the same bypass `edit` guards.
            raise BadInput(
                "this is an imported reference config (provenance:external) "
                "— derive a variant instead of overwriting it in place",
                next=f"derive(kind='structure', id={slug!r}, "
                "to='<new-slug>', ops=[...])",
            )
        payload = _payload(text, args)
        ops = payload.get("ops") or []
        if "cell" in payload:
            scene = Scene(cell=_build_cell(payload["cell"]))
        elif _ops_establish_cell(ops):
            # A bulk template (`slab`) or a `set_cell` op provides the cell — the
            # placeholder is overwritten before any atom is placed.
            scene = Scene(cell=Cell(np.eye(3), (True, True, True)))
        else:
            raise BadInput(
                "put(kind='structure') payload needs a 'cell' (or a "
                "cell-establishing op like 'slab')"
            )
        res = self._run_ops(scene, payload.get("ops", []))
        if isinstance(res, _NeedsDispatch):
            # The preflight clean/emt pre-relax already ran (mutating
            # ``scene``) as part of staging the dispatch — carry its result
            # through the normal relax-summary/run-recording path below so
            # the pre-relaxed geometry's write-back is indistinguishable
            # from an ordinary local ``clean`` relax (gripe 51393).
            relax_result: RelaxResult | None = res.preflight_result
            dispatch: _NeedsDispatch | None = res
        else:
            relax_result, dispatch = res, None
        relax_summary = self._relax_summary(relax_result)
        version = (int(existing.meta.get("version", 0)) + 1) if existing else 1
        desc = str(payload.get("description") or "").strip()
        ttl = (title or slug).strip() or slug
        # Tier-0 preflight (gated, default off): reject before anything
        # persists — the prior version (none yet, for a fresh put) stands.
        _run_preflight_gate(scene)
        ref, created = self.store.structure_save(
            slug=slug,
            title=ttl,
            scene=scene,
            version=version,
            card_text=self._card_text(ttl, scene, desc),
            description=desc,
            relax_summary=relax_summary,
        )
        self._record_run(ref.id, relax_result, version)
        if dispatch is not None:
            return self._dispatch_relax(ref, version, dispatch)
        _scene, handles = self.store.structure_load(ref.id)
        verb = "created" if created else "updated"
        return self._toc_response(
            _scene, ref, handles, head_verb=verb, relax_summary=relax_summary
        )

    # ── edit ─────────────────────────────────────────────────────────
    def edit(
        self,
        *,
        id: str | int | None = None,
        ops: list[dict[str, Any]] | None = None,
        text: str | None = None,
        args: dict[str, Any] | None = None,
        dry_run: bool | str | None = None,
        **_kw: Any,
    ) -> Response:
        if dry_run:
            # Structure ops mutate the cell/bond IR (and may dispatch a
            # GPU relax). No faithful preview yet — reject rather than
            # silently apply on dry_run (that was a data-loss footgun).
            raise BadInput(
                "edit(kind='structure') does not support dry_run yet — ops mutate "
                "the cell/bond graph (and may dispatch compute); omit dry_run to apply",
                next="edit(kind='structure', id='pd111', ops=[{'op':'add_atom', ...}])",
            )
        if id is None or not str(id).strip():
            raise BadInput("edit(kind='structure') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        if self._external_provenance(ref.id) is not None:
            raise BadInput(
                "this is an imported reference config (provenance:external) "
                "— derive a variant instead of editing it in place",
                next=f"derive(kind='structure', id={ref.slug!r}, "
                "to='<new-slug>', ops=[...])",
            )
        op_list = ops
        if op_list is None:
            payload = _payload(text, args)
            op_list = payload.get("ops", payload if isinstance(payload, list) else [])
        if not op_list:
            raise BadInput(
                "edit(kind='structure') requires ops=",
                next="edit(kind='structure', id='pd111', "
                "ops=[{'op':'add_atom','element':'O','frac':[0.33,0.33,0.55]}])",
            )
        scene, _ = self.store.structure_load(ref.id)
        res = self._run_ops(scene, op_list)
        if isinstance(res, _NeedsDispatch):
            # See put()'s matching branch: the preflight relax already ran
            # as part of staging the dispatch, so its result rides the
            # normal relax-summary/run-recording path (gripe 51393).
            relax_result: RelaxResult | None = res.preflight_result
            dispatch: _NeedsDispatch | None = res
        else:
            relax_result, dispatch = res, None
        relax_summary = self._relax_summary(relax_result)
        version = self.store.structure_version(ref.id) + 1
        desc = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        # Tier-0 preflight (gated, default off): reject before this new
        # version commits — the prior version stands (transactional undo).
        _run_preflight_gate(scene)
        self.store.structure_save(
            slug=str(ref.slug),
            title=ttl,
            scene=scene,
            version=version,
            card_text=self._card_text(ttl, scene, desc),
            description=desc,
            relax_summary=relax_summary,
        )
        self._record_run(ref.id, relax_result, version)
        if dispatch is not None:
            return self._dispatch_relax(ref, version, dispatch)
        _scene, handles = self.store.structure_load(ref.id)
        return self._toc_response(
            _scene, ref, handles, head_verb="edited", relax_summary=relax_summary
        )

    # ── derive ───────────────────────────────────────────────────────
    def derive(
        self,
        *,
        id: str | int,
        to: str,
        ops: list[dict[str, Any]] | None = None,
        title: str | None = None,
    ) -> Response:
        """Branch a **new** design ``to`` from ``id`` with ``ops`` applied, linked
        ``derived-from`` the parent (bundle — the instruction-box Apply).

        The parent is untouched, so a before/after ``view='diff'`` works. Applies
        graph/marker ops only — a relax is a separate compute step, never part of
        a proposal. The parent's markers carry over (they live on the scene)."""
        parent = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        to_slug = str(to).strip()
        if not to_slug:
            raise BadInput("derive requires to= (the new design slug)")
        if self.store.get_ref(kind="structure", id=to_slug) is not None:
            raise BadInput(
                f"design {to_slug!r} already exists",
                next="pick a fresh slug for the derived design",
            )
        scene, _ = self.store.structure_load(parent.id)
        op_list = ops or []
        if any(o.get("op") == "relax" for o in op_list):
            raise BadInput("derive applies graph/marker ops only (no relax)")
        try:
            apply_ops(scene, op_list)
        except OpError as exc:
            raise BadInput(f"op error: {exc}") from exc
        ttl = (title or to_slug).strip() or to_slug
        ref, _created = self.store.structure_save(
            slug=to_slug,
            title=ttl,
            scene=scene,
            version=1,
            card_text=self._card_text(ttl, scene, ""),
        )
        # lineage: the derived design points back to its parent
        self.store.add_link(
            src_ref_id=ref.id, dst_ref_id=parent.id, relation="derived-from"
        )
        _scene, handles = self.store.structure_load(ref.id)
        return self._toc_response(_scene, ref, handles, head_verb="derived")

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        source: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # ``source=`` — on-demand hydrate from an external catalyst DB (T6).
        # The MCP ``get`` tool has no top-level ``source=``/query-filter
        # params yet, so a caller reaches this via
        # ``get(kind='structure', args={'source': 'catalysis-hub', ...})``
        # — the dispatcher forwards the whole ``args=`` dict verbatim
        # because this handler already opts into an ``args`` kwarg (see
        # ``DispatchMixin._invoke_handler``). A direct Python caller (tests,
        # a future in-proc dispatch) may also pass ``source=``/``q=``
        # top-level; both routes are honoured.
        a = args or {}
        src = (source or a.get("source") or "").strip() or None
        if src is not None:
            query = q if q is not None else a.get("q")
            return self._get_external(source=src, q=query, args=a)
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        scene, handles = self.store.structure_load(ref.id)
        if view is None:
            return self._toc_response(scene, ref, handles)
        if view == "runs":
            return self._render_runs(ref)
        if view == "markers":
            return self._render_markers(scene)
        if view == "literature":
            return self._render_literature(scene, ref)
        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="structure")
        if view in _EXPORT_VIEWS:
            return self._render_export(view, scene, str(ref.slug or id))
        if view in _NAV_VIEWS:
            return self._render_nav(view, scene, args or {})
        if view not in _PROBE_VIEWS:
            raise BadInput(
                f"unknown structure view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the TOC",
            )
        return self._render_probe(view, scene, args or {}, ref=ref)

    def _get_external(
        self, *, source: str, q: str | None, args: dict[str, Any]
    ) -> Response:
        """On-demand hydrate one (or a filtered set of) external config(s)
        from ``source``. First touch: fetch → adapter →
        ``store.structure_import`` (idempotent) → render with a citation
        footer. A caller who names an exact ``config_id=`` gets a
        network-free cache hit when that config is already imported — the
        by-id short-circuit checks ``ref_identifiers`` before ever calling
        the fetch layer. A broad filter-only query (``surface_composition=``/
        ``facet=``/``q=`` with no ``config_id=``) always refetches — there's
        no way to know in advance whether the source has new configs
        matching those filters."""
        try:
            adapter_fn = get_adapter(source)
        except ValueError as exc:
            raise BadInput(str(exc)) from exc

        config_id = args.get("config_id")
        if config_id:
            existing_ref_id = self.store.find_ref_by_identifier(
                source, str(config_id), kind="structure"
            )
            if existing_ref_id is not None:
                return self._render_hydrated(
                    [(existing_ref_id, source, str(config_id), None)], cached=True
                )

        if source != "catalysis-hub":
            raise BadInput(
                f"on-demand hydrate has no fetch layer wired for source={source!r} yet",
                next="known sources: catalysis-hub",
            )
        filters = _resolve_catalysis_hub_filters(q, args)
        try:
            raw_records = catalysis_hub.fetch_config(**filters)
        except catalysis_hub.CatalysisHubUnsupported as exc:
            raise Unsupported(
                str(exc), next="pip install 'precis-mcp[import]'"
            ) from exc

        if not raw_records:
            return Response(
                body=f"no {source} configs match {filters!r}\n\n"
                "Next: widen surface_composition=/facet=, or omit filters "
                "for a broader sweep."
            )

        imported: list[tuple[int, str, str, dict[str, Any] | None]] = []
        for raw in raw_records:
            scene, run, ext_id = adapter_fn(raw)
            ref_id = self.store.structure_import(scene, run, ext_id)
            imported.append((ref_id, ext_id.dataset, ext_id.config_id, run.method))

        if config_id:
            matched = [row for row in imported if row[2] == str(config_id)]
            if not matched:
                # The fetch returned configs but none is the requested id —
                # never silently substitute an unrelated record as if it were
                # the one asked for (it could feed the wrong substrate to a
                # quest). Report the miss instead.
                return Response(
                    body=f"no {source} config with config_id={config_id!r} in "
                    f"the fetched set (filters {filters!r} returned "
                    f"{len(imported)} other config(s))\n\n"
                    "Next: check the config_id, or omit it to browse the "
                    "filtered set."
                )
            imported = matched

        return self._render_hydrated(imported, cached=False)

    def _render_hydrated(
        self,
        rows: list[tuple[int, str, str, dict[str, Any] | None]],
        *,
        cached: bool,
    ) -> Response:
        """Render on-demand-hydrate result(s) — a full TOC + citation footer
        for a single config (the common case: a by-id hit, or a filtered
        fetch that resolved to exactly one record), else a summary table."""
        if len(rows) == 1:
            ref_id, dataset, config_id, method = rows[0]
            ref = self.store.get_ref(kind="structure", id=ref_id)
            assert ref is not None
            scene, handles = self.store.structure_load(ref.id)
            head_verb = "cached" if cached else "hydrated"
            body = self._toc_response(scene, ref, handles, head_verb=head_verb).body
            if method is None:
                method = (self._external_provenance(ref.id) or {}).get("method") or {}
            doi = method.get("dataset_doi")
            footer = f"\n\n# source: {dataset} · config {config_id}"
            if doi:
                footer += f" · doi:{doi}"
            return Response(body=body + footer)

        table_rows = []
        for ref_id, dataset, config_id, method in rows:
            ref = self.store.get_ref(kind="structure", id=ref_id)
            handle = handle_registry.try_format("structure", ref_id, chunk=False) or "—"
            doi = (method or {}).get("dataset_doi") or "—"
            table_rows.append(
                {
                    "handle": handle,
                    "design": ref.slug if ref else "—",
                    "dataset": dataset,
                    "config_id": config_id,
                    "doi": doi,
                }
            )
        return Response(
            body=f"# {len(table_rows)} hydrated structure(s)\n"
            + render_agent_table(
                table_rows, schema=["handle", "design", "dataset", "config_id", "doi"]
            )
        )

    def _render_export(self, view: str, scene: Scene, slug: str) -> Response:
        """Emit the geometry as a file format. POSCAR/extXYZ are pure; CIF
        needs ASE (the optional ``[dft]`` extra) — a missing one is Unsupported
        with an install hint, not a crash."""
        if view == "poscar":
            return Response(body=export.to_poscar(scene))
        if view == "extxyz":
            return Response(body=export.to_extxyz(scene))
        # cif
        if not export.ase_available():
            raise Unsupported(
                "CIF export needs ASE",
                next="install it:  pip install 'precis-mcp[dft]'  (POSCAR/extXYZ work without it)",
            )
        return Response(body=export.to_cif(scene))

    def _external_provenance(self, ref_id: int) -> dict[str, Any] | None:
        """The design's newest external-provenance run, if any (migration 0084). Non-``None`` means this design mirrors an imported
        dataset entry (OC20/OC22, Materials Project, …) — it must stay a
        faithful, read-only reference; all work branches via ``derive``.

        Queried directly against ``struct_runs`` rather than through
        ``store.structure_runs`` so this guard is correct regardless of
        whether that helper's own column list has caught up with 0084 yet."""
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT method FROM struct_runs "
                "WHERE ref_id = %s AND provenance = 'external' "
                "ORDER BY id DESC LIMIT 1",
                (ref_id,),
            ).fetchone()
        return None if row is None else {"method": row[0] or {}}

    def _runs_provenance(
        self, run_ids: list[int]
    ) -> dict[int, tuple[str, dict[str, Any] | None]]:
        """``{run_id: (provenance, method)}`` for a batch of run ids
        (migration 0084) — a direct query for the same reason as
        ``_external_provenance`` above."""
        if not run_ids:
            return {}
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, provenance, method FROM struct_runs WHERE id = ANY(%s)",
                (run_ids,),
            ).fetchall()
        return {int(rid): (prov, method) for rid, prov, method in rows}

    def _atom_view_calc_row(
        self, ref_id: int, *, run_id: int | None, on_version: int
    ) -> dict[str, Any] | None:
        """The struct_runs row backing view='atom''s ``calc:`` header (gripe
        161576 remainder) — same run selection as ``_resolve_forces`` (a
        pinned ``run_id``, else the latest run at the design's CURRENT
        version, FIX 2), but NOT restricted to force-bearing rows: an
        external import's energy-only row, or a computed run whose per-atom
        forces never landed, still deserves a calculator-identity line. One
        direct ``struct_runs`` query, the same rationale as
        ``_external_provenance`` above — provenance/method (0084) aren't in
        ``structure_run_forces``'s own column list."""
        with self.store.pool.connection() as conn:
            if run_id is not None:
                row = conn.execute(
                    "SELECT provenance, model, params, method FROM struct_runs "
                    "WHERE id = %s AND ref_id = %s",
                    (run_id, ref_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT provenance, model, params, method FROM struct_runs "
                    "WHERE ref_id = %s AND on_version = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (ref_id, on_version),
                ).fetchone()
        if row is None:
            return None
        provenance, model, params, method = row
        return {
            "provenance": provenance,
            "model": model,
            "params": params,
            "method": method,
        }

    def _render_runs(self, ref: Any) -> Response:
        """The design's compute history — the fidelity ladder over time (§9),
        each row labeled with its provenance (``computed`` vs ``external``) and, for an imported row, its method fingerprint."""
        runs = self.store.structure_runs(ref.id)
        if not runs:
            return Response(
                body=f"# {ref.slug}: no compute runs yet\n\n"
                "Next: edit(kind='structure', id='"
                + str(ref.slug)
                + "', ops=[{'op':'relax','fidelity':'clean'}])"
            )
        prov = self._runs_provenance([r["id"] for r in runs])
        rows = []
        for r in runs:
            provenance, method = prov.get(r["id"], ("computed", None))
            rows.append(
                {
                    "run": f"r{r['id']}",
                    "fidelity": r["fidelity"],
                    "status": r["status"],
                    "conv": "yes" if r["converged"] else "no",
                    "steps": str(r["n_steps"]),
                    "energy": "—" if r["energy"] is None else f"{r['energy']:.4f}",
                    "max_force": "—"
                    if r["max_force"] is None
                    else f"{r['max_force']:.4f}",
                    "v": str(r["on_version"]),
                    "provenance": provenance,
                    "method": _format_method_fingerprint(method)
                    if provenance == "external"
                    else "—",
                    # gripe 161576: which runs carry a per-atom force estimate,
                    # and whether it's a real emt/ml force or the cheap EMT
                    # approximation (rung 0 has no calculator of its own).
                    "forces": _describe_run_forces(r.get("forces")),
                }
            )
        return Response(
            body=f"# {ref.slug}: {len(runs)} compute run(s)\n"
            + render_agent_table(
                rows,
                schema=[
                    "run",
                    "fidelity",
                    "status",
                    "conv",
                    "steps",
                    "energy",
                    "max_force",
                    "v",
                    "provenance",
                    "method",
                    "forces",
                ],
            )
        )

    def _render_markers(self, scene: Scene) -> Response:
        """The design's eyes + measures (§6.8/§7), each re-evaluated against
        the current geometry so value + verdict are live, never stale."""
        if not scene.measures:
            return Response(
                body="# no eyes or measures yet\n\nNext: edit(kind='structure', "
                "id=…, ops=[{'op':'eye','name':'active_site',"
                "'atoms':['aPd12'],'reach':3.0,'for':'the reactive site'}])"
            )
        rows: list[dict[str, str]] = []
        for m in scene.measures:
            value, verdict = evaluate_measure(scene, m)
            if m.kind == "eye":
                shown = (
                    value["error"]
                    if "error" in value
                    else f"touches {len(value.get('touch', []))}"
                )
            elif "error" in value:
                shown = value["error"]
            else:
                unit = value.get("unit") or ""
                shown = f"{value.get('value')}{(' ' + unit) if unit else ''}"
            rows.append(
                {
                    "marker": m.name or m.kind,
                    "kind": m.kind,
                    "atoms": " ".join(m.operands),
                    "for": (m.for_ or "")[:40],
                    "value": str(shown),
                    "verdict": verdict or "—",
                }
            )
        return Response(
            body=f"# {len(rows)} eye(s) + measure(s)\n"
            + render_agent_table(
                rows,
                schema=["marker", "kind", "atoms", "for", "value", "verdict"],
            )
        )

    # ── literature (gr161578: structure → paper search) ────────────────

    def _literature_query(self, scene: Scene, ref: Any) -> str:
        """Deterministic paper-search query built from the design's own data
        (gr161578) — **no LLM step**. Assembled, in order:

        1. the author's own ``description`` (meta), when set;
        2. a host-metal/adsorbate/facet phrase over ``scene.composition()``
           — ``surface_composition``/``facet`` from an external-provenance
           method fingerprint when this design was hydrated from a catalyst
           DB (mirroring the catalysis-hub adapter's fields)
           and that string names an actual composition element, else a
           heuristic over :data:`_HOST_METAL_ELEMENTS` — **every** recognised
           host metal, not just the top-count one (a bimetallic/alloy host,
           e.g. Pd/Ag, must not silently lose its minority metal), ordered
           count-desc then alpha for determinism;
        3. the bare formula (``probe.toc(scene)['formula']``) — **only** as
           a last-resort fallback when the design has neither a description
           nor any recognisable elements, so the query is never empty. It's
           otherwise omitted from the searched string on purpose: the glued
           token (e.g. ``O1Pd4``) is just a supercell atom count, not a real
           stoichiometry a paper would ever quote verbatim, and the shared
           paper-search engine's lexical leg is a strict AND
           (``websearch_to_tsquery``) — one unmatchable glued token would
           zero out an otherwise-good hit.

        Same scene + ref meta ⇒ same string (composition/method dicts are
        walked in a fixed sort order)."""
        comp = scene.composition()
        method = (self._external_provenance(ref.id) or {}).get("method") or {}
        ext_host = method.get("surface_composition") or None
        facet = method.get("facet") or None
        # An alloy-formula string (e.g. "Ag3Pd") names no single composition
        # element symbol — using it verbatim as "the host" produces a
        # redundant query ("Ag Pd on Ag3Pd") instead of a useful one; fall
        # back to the composition-derived host-metal list below.
        if ext_host is not None and ext_host not in comp:
            ext_host = None

        if ext_host:
            host_str: str | None = ext_host
            adsorbates = sorted(
                el for el in comp if el != ext_host and el not in _HOST_METAL_ELEMENTS
            )
        else:
            # A bimetallic/alloy host keeps ALL recognised metals, not just
            # the top-count one, so a Pd/Ag surface doesn't silently drop
            # the Ag half of the query.
            host_metals = sorted(
                (el for el in comp if el in _HOST_METAL_ELEMENTS),
                key=lambda el: (-comp[el], el),
            )
            host_str = " ".join(host_metals) if host_metals else None
            adsorbates = sorted(el for el in comp if el not in _HOST_METAL_ELEMENTS)

        parts: list[str] = []
        desc = str((ref.meta or {}).get("description") or "").strip()
        if desc:
            parts.append(desc)
        if host_str:
            phrase = (
                f"{' '.join(adsorbates)} on {host_str}"
                if adsorbates
                else f"{host_str} surface"
            )
            if facet:
                phrase += f"({facet})"
            parts.append(phrase)
        elif adsorbates:
            parts.append(" ".join(adsorbates))
        if not parts:
            parts.append(str(probe.toc(scene)["formula"]))
        return " — ".join(p for p in parts if p)

    def _render_literature(self, scene: Scene, ref: Any) -> Response:
        """``view='literature'`` (gr161578): run the deterministic query above
        against the paper corpus via ``PaperHandler.search_hits`` — the same
        fused block-search engine ``kind='paper'`` search uses, called
        in-process rather than hand-rolling SQL. Returns both the generated
        query (so the caller can see/refine it) and the ranked hits. Pure
        read: no writes, no LLM, no network beyond the existing paper-search
        path."""
        from precis.handlers.paper import PaperHandler

        query = self._literature_query(scene, ref)
        hub = (
            self.hub
            if self.hub is not None
            else Hub(store=self.store, embedder=self.embedder)
        )
        hits = PaperHandler(hub=hub).search_hits(q=query, page_size=10)
        head = f"# literature query for {ref.slug}:\n> {query}"
        if not hits:
            return Response(
                body=f"{head}\n\nno matching papers\n\n"
                "Next: enrich the design's description= for a sharper query, "
                f"or search(kind='paper', q={query!r}) by hand for a wider net."
            )
        rows = [
            {
                "handle": hit.uhandle
                or (f"paper:{hit.slug}" if hit.slug else str(hit.ref_id)),
                "title": hit.title,
                "score": f"{hit.score:.3f}",
            }
            for hit in hits
        ]
        return Response(
            body=f"{head}\n\n"
            + render_agent_table(rows, schema=["handle", "title", "score"])
        )

    # ── link ─────────────────────────────────────────────────────────
    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        note: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove a link from this design to another ref — e.g. a derived
        design → its parent (``rel='derived-from'``, target ``structure:<slug>``),
        or a paper that motivated it (``rel='cites'``, target ``paper:<slug>``;
        any registered relation works — the TOC's provenance section picks up
        every outbound link whose target is a paper, regardless of ``rel``).

        The reserved virtual ``rel='parent'`` is folder placement
         — a ``refs.parent_id`` write, never a stored link.
        Derivation (``derived-from``) and placement are orthogonal axes.

        ``note=`` (add-only, gr161577) attaches a short "designed because…"
        rationale to the link itself (``links.meta['note']`` — no migration,
        that column already exists) — surfaced in the TOC's provenance
        section and the web detail page. Re-linking the same (target, rel)
        with a fresh ``note=`` updates it in place.
        """
        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(
                self.store, kind="structure", id=str(id).strip()
            )
            return place_ref(
                self.store, kind="structure", ref=ref, target=target, mode=mode
            )
        target = require_link_target("structure", target)
        validate_link_mode(mode)
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        link_meta = {"note": note.strip()} if mode == "add" and note else None
        n_added, n_removed = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
            meta=link_meta,
            # opt-in: re-linking with a fresh note= updates it in place
            # (Store.add_link's merge_meta, scoped to this call site only).
            merge_meta=link_meta is not None,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=str(ref.slug),
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='structure') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        n = self.store.structure_delete(ref.id)
        return Response(body=f"retired structure {ref.slug} ({n} atom(s))")

    # ── helpers ──────────────────────────────────────────────────────
    def _run_ops(
        self, scene: Scene, ops: list[dict[str, Any]]
    ) -> RelaxResult | _NeedsDispatch | None:
        """Apply graph ops, then an optional terminal ``relax`` op. Returns the
        :class:`RelaxResult` (or None), or a :class:`_NeedsDispatch` when an
        energy rung missed the cache and has no local backend (the caller mints
        a ``struct_relax`` job). A graph edit invalidates any prior relax; the
        caller persists the run (§9 system-of-record)."""
        graph_ops = [o for o in ops if o.get("op") != "relax"]
        relax_ops = [o for o in ops if o.get("op") == "relax"]
        try:
            apply_ops(scene, graph_ops)
        except OpError as exc:
            raise BadInput(f"op error: {exc}") from exc
        if not relax_ops:
            return None
        ro = relax_ops[-1]
        fidelity = str(ro.get("fidelity", "clean"))
        steps = int(ro.get("steps", 200))
        model = str(ro.get("model", "mace_mp"))
        # Optional variable-cell relax: 'inplane' (slab box, vacuum pinned) or
        # 'full' (bulk). Validated up front so a bad mode is a retryable BadInput
        # rather than being swallowed into the dispatch/Unsupported path below.
        cell_mode = ro.get("cell") or None
        if cell_mode == "fixed":
            cell_mode = None
        if cell_mode is not None and cell_mode not in ("inplane", "full"):
            raise BadInput(
                f"relax cell mode {cell_mode!r} not understood "
                "(use 'inplane', 'full', or omit for an atoms-only relax)"
            )
        if cell_mode is not None and fidelity in ("clean", "0"):
            raise BadInput(
                "variable-cell relax (cell=…) needs fidelity='ml'; the 'clean' "
                "geometry repair has no stress to relax the cell against"
            )

        # Cache-first for the expensive energy rungs. The rung-0
        # ``clean`` repair is instant + pure + energy-free, so it is never
        # cached — it just runs. The key is over the *input* geometry (this
        # scene, after graph ops, before relax mutates it), so capture the
        # content address + canonical order now.
        cached_rung = fidelity not in ("clean", "0")
        cache_key = structure_sha = None
        order: list[str] = []
        if cached_rung:
            # Only fold ``cell`` into the key when a variable-cell relax is
            # asked for — an atoms-only relax keeps its historical key so the
            # existing run-cube stays a hit (a bare {"steps"} vs {"steps","cell"}).
            cache_key, structure_sha, order = _relax_cache_address(
                scene, fidelity=fidelity, model=model, steps=steps, cell_mode=cell_mode
            )
            hit_result = self._cache_hit_result(
                scene,
                cache_key=cache_key,
                structure_sha=structure_sha,
                fidelity=fidelity,
            )
            if hit_result is not None:
                return hit_result

        try:
            res = run_relax(
                scene, fidelity=fidelity, steps=steps, model=model, cell=cell_mode
            )
            # Per-atom forces (gripe 161576), label-paired for storage (FIX 1
            # — never canonical-rank-indexed, see serialize_forces) —
            # regardless of ``cached_rung``/convergence, so a 'clean' rung's
            # cheap EMT estimate, and a non-converged emt/ml relax's real
            # forces, both persist (a strain signal is still informative when
            # a run didn't converge).
            if res.forces is not None:
                res.forces_labels, res.forces_vectors = relax_cache.serialize_forces(
                    res.forces
                )
        except RelaxUnsupported as exc:
            # No local backend for this energy rung. If the caller named a
            # parent todo we dispatch it to the GPU node (§23.12); otherwise
            # the caller turns this into an Unsupported with the exact call.
            if cache_key is None:  # defensive — clean never reaches here
                raise Unsupported(
                    str(exc),
                    next="relax with fidelity='clean' (geometry repair, "
                    "always available)",
                ) from exc

            # Hard-reject gate (gripe 51393): cloud dispatch is a last
            # resort, never a place to burn GPU time on an impossible
            # geometry. Only the heavy-dispatch branch is gated — a direct
            # local ``clean``/``emt`` relax above never reaches here, so it
            # stays free to repair the very geometry this would reject.
            findings = validate(scene)
            if findings:
                raise BadInput(
                    _format_gate_rejection(findings),
                    next="fix the flagged atom(s)/bond(s), or run "
                    "edit(ops=[{'op':'relax','fidelity':'clean'}]) locally "
                    "first to repair mild clashes before requesting "
                    f"fidelity={fidelity!r}",
                ) from exc

            # Always pre-relax locally before staging anything for the cloud
            # (gripe 51393) — ``clean`` by default, or ``emt`` when the op
            # opts in and the element set/backend allow it. Mutates ``scene``
            # in place, so the caller's subsequent ``structure_save`` persists
            # the cleaned geometry exactly like a normal ``clean`` relax would.
            preflight_mode = str(ro.get("preflight", "clean"))
            preflight_result, preflight_note = _preflight_relax(
                scene, preflight_mode, steps=steps
            )
            # gripe 161576: label-pair the preflight's own forces (a real
            # local clean/emt pass) for storage — same as an ordinary
            # standalone relax above (FIX 1: never canonical-rank-indexed).
            if preflight_result.forces is not None:
                preflight_result.forces_labels, preflight_result.forces_vectors = (
                    relax_cache.serialize_forces(preflight_result.forces)
                )
            # The geometry just changed — recompute the address so the
            # dispatched job / run-cube row is addressed by what's actually
            # staged, not the pre-preflight input. A design that happens to
            # preflight-clean onto an already-cached geometry (e.g. another
            # design's completed relax converged to the same input) must
            # still be a zero-compute hit here too — the earlier lookup ran
            # against the as-authored (pre-preflight) geometry and can't
            # have caught this.
            cache_key, structure_sha, order = _relax_cache_address(
                scene, fidelity=fidelity, model=model, steps=steps, cell_mode=cell_mode
            )
            hit_result = self._cache_hit_result(
                scene,
                cache_key=cache_key,
                structure_sha=structure_sha,
                fidelity=fidelity,
            )
            if hit_result is not None:
                return hit_result
            return _NeedsDispatch(
                fidelity=fidelity,
                model=model,
                steps=steps,
                cell=cell_mode,
                cache_key=cache_key,
                structure_sha=structure_sha or "",
                order=order,
                poscar=export.to_poscar(scene),
                poscar_labels=_poscar_row_labels(scene),
                # Optional requesting todo (compute lane). Accept
                # the clear ``requested_by`` key; tolerate the legacy
                # ``parent_id`` spelling from before the lane split.
                requester_id=_as_int_or_none(
                    ro.get("requested_by", ro.get("parent_id"))
                ),
                preflight_result=preflight_result,
                preflight_note=preflight_note,
            )

        # Cache miss: stamp the content address + relaxed geometry so the next
        # identical request — on this design or any other sharing the input —
        # is a zero-compute hit.
        if cached_rung and res.converged:
            res.cache_key = cache_key
            res.structure_sha = structure_sha
            res.final_geometry = relax_cache.serialize_geometry(scene, order)
        return res

    def _cache_hit_result(
        self, scene: Scene, *, cache_key: str, structure_sha: str, fidelity: str
    ) -> RelaxResult | None:
        """The run-cube lookup, shared by both cache-check sites
        in ``_run_ops``: the early check over the as-authored geometry, and
        the post-preflight re-check over the cleaned geometry (gripe 51393)
        — a design that preflight-cleans onto an already-cached input must
        be a zero-compute hit too, not a fresh cloud dispatch.

        On a hit, applies the cached geometry onto ``scene`` and returns the
        stored envelope as a :class:`RelaxResult`; ``None`` on a miss."""
        hit = self.store.structure_find_cached_run(cache_key)
        if hit is None:
            return None
        geom = hit.get("final_geometry")
        if geom:
            relax_cache.apply_geometry(scene, geom)
        # gripe 161576: a cache hit still records a fresh struct_runs row
        # below (append-only audit truth) — carry the cached run's forces
        # payload through so that row isn't silently force-less.
        forces_blob = hit.get("forces") or {}
        return RelaxResult(
            rung=fidelity,
            converged=bool(hit["converged"]),
            n_steps=int(hit["n_steps"]),
            max_disp=float(hit["max_disp"] or 0.0),
            curve=list(hit.get("curve") or []),
            energy=hit["energy"],
            max_force=hit["max_force"],
            model=hit["model"],
            from_cache=True,
            cache_key=cache_key,
            structure_sha=structure_sha,
            final_geometry=geom,
            forces_labels=forces_blob.get("labels"),
            forces_vectors=forces_blob.get("vectors"),
            forces_approx=bool(forces_blob.get("approx", False)),
            forces_source=forces_blob.get("source"),
        )

    @staticmethod
    def _relax_summary(res: RelaxResult | None) -> dict[str, Any] | None:
        """The compact relax envelope stamped on ``meta.last_relax`` + the TOC."""
        if res is None:
            return None
        out: dict[str, Any] = {
            "rung": res.rung,
            "converged": res.converged,
            "n_steps": res.n_steps,
            "max_disp": res.max_disp,
        }
        if res.energy is not None:
            out["energy"] = res.energy
        if res.max_force is not None:
            out["max_force"] = res.max_force
        if res.model is not None:
            out["model"] = res.model
        return out

    def _record_run(self, ref_id: int, res: RelaxResult | None, version: int) -> None:
        """Persist a relax as a ``struct_runs`` row + its convergence curve.

        A cache *hit* still records a row for this design/version (per-design
        audit truth — ``view='runs'`` shows it was relaxed), carrying the same
        cache_key, so the cube stays append-only and internally consistent."""
        if res is None:
            return
        forces_payload = None
        if res.forces_vectors is not None:
            # gripe 161576: {"vectors", "labels", "approx", "source"} —
            # labels[i] <-> vectors[i] (FIX 1: the read-side join is by
            # label, never a re-derived canonical rank). approx=true is the
            # cheap clean-rung EMT single-point estimate, never confused with
            # a real emt/ml relax force (approx=false).
            forces_payload = {
                "vectors": res.forces_vectors,
                "labels": res.forces_labels,
                "approx": res.forces_approx,
                "source": res.forces_source,
            }
        self.store.structure_record_run(
            ref_id,
            fidelity=res.rung,
            on_version=version,
            converged=res.converged,
            n_steps=res.n_steps,
            max_disp=res.max_disp,
            energy=res.energy,
            max_force=res.max_force,
            model=res.model,
            curve=res.curve,
            cache_key=res.cache_key,
            structure_sha=res.structure_sha,
            final_geometry=res.final_geometry,
            forces=forces_payload,
            # charges: always None today — no backend produces partial
            # charges yet (never fabricated); the column exists for a future
            # charge-bearing rung (DFT+Bader).
            params={"cached": True} if res.from_cache else None,
        )

    def _dispatch_relax(self, ref: Any, version: int, nd: _NeedsDispatch) -> Response:
        """Mint a ``struct_relax`` job for an energy rung with no local backend. The relaxed geometry lands in the §23.16
        run-cube on completion, so the next identical relax — on this design or
        any other sharing the input — is a zero-compute cache hit.

        The job is a *derived* compute step: it parents on the **structure**,
        not a todo — the artifact is its owner (cache-fillable, idempotent, no
        human-steering loop). When a caller names ``requested_by=<todo_id>`` it
        also wants to block on the result; we then write a ``requested`` link
        and inject a ``derived_job_succeeded`` auto_check so that todo closes on
        success and gets a ``child-failed`` bubble on failure."""
        slug = str(ref.slug)
        from precis.handlers import _todo_guards as todo_guards

        # A named requester must be a live todo (fail fast, before the mint).
        if nd.requester_id is not None:
            todo_guards.check_parent_exists(self.store, nd.requester_id)

        # self.hub is set at registration; a hand-constructed handler (tests)
        # leaves it None, so fall back to a minimal hub over the same store —
        # JobHandler only needs hub.store.
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        params = {
            "structure_ref_id": ref.id,
            "on_version": version,
            "fidelity": nd.fidelity,
            "model": nd.model,
            "steps": nd.steps,
            # Variable-cell relax mode (present only when asked for) — the GPU
            # container honours it; absent ⇒ atoms-only, the historical default.
            **({"cell": nd.cell} if nd.cell is not None else {}),
            "cache_key": nd.cache_key,
            "structure_sha": nd.structure_sha,
            "order": nd.order,
            "poscar_labels": nd.poscar_labels,
            "poscar": nd.poscar,
            # Pin to the GPU node so its worker claims the job (§23 #3) — the
            # stager + container then share one host's NFS view.
            "target_node": os.environ.get("PRECIS_DFT_NODE", "spark"),
        }
        job_resp = hub.sibling("job").put(
            job_type="struct_relax",
            executor="ssh_node",
            # The build subject owns the job (compute lane).
            parent_id=ref.id,
            params=params,
            # Collapse re-submits of the *same* relax onto one in-flight job.
            idem_key=f"struct_relax:{nd.cache_key}",
        )
        note = ""
        if nd.requester_id is not None:
            if job_resp.ref_id is None:  # defensive — put always reports the id
                log.warning(
                    "structure._dispatch_relax: job put() returned no ref_id "
                    "for requester #%s; skipping requester wiring",
                    nd.requester_id,
                )
            else:
                self._wire_requester(nd.requester_id, job_resp.ref_id)
            note = f" (todo #{nd.requester_id} will block on it)"
        pre = nd.preflight_result
        preflight_line = (
            f"# pre-relaxed locally (rung {pre.rung!r}, "
            f"{'converged' if pre.converged else 'not converged'}) before staging\n"
            if pre is not None
            else ""
        )
        if nd.preflight_note:
            preflight_line += f"# {nd.preflight_note}\n"
        return Response(
            body=(
                f"# relax[{nd.fidelity}] dispatched to the GPU node{note}\n"
                f"{preflight_line}\n"
                f"{job_resp.body}\n\n"
                f"The run lands in the cache on completion. "
                f"Poll: get(kind='structure', id='{slug}', view='runs')."
            )
        )

    def _wire_requester(self, requester_id: int, job_id: int) -> None:
        """Link the requesting todo to the just-minted job and arm its wait.

        Writes ``requester --requested--> job`` (the edge the
        ``derived_job_succeeded`` evaluator + the failure-bubble follow), then
        injects that evaluator as the todo's ``auto_check`` when it has none —
        mirroring how ``dispatch`` arms ``child_job_succeeded`` for the intent
        lane. A todo that already carries a deliberate auto_check is left
        alone. Idempotent on both writes."""
        with self.store.tx() as conn:
            self.store.add_link(
                src_ref_id=requester_id,
                dst_ref_id=job_id,
                relation="requested",
                set_by="system",
                conn=conn,
            )
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta || jsonb_build_object(
                                'auto_check',
                                jsonb_build_object('type', 'derived_job_succeeded')
                              )
                 WHERE ref_id = %s
                   AND NOT (meta ? 'auto_check')
                """,
                (requester_id,),
            )

    def _render_list(self) -> Response:
        designs = self.store.list_refs(kind="structure", order_by="id_desc", limit=50)
        if not designs:
            return Response(
                body="no structures yet\n\nNext: put(kind='structure', id='pd111', "
                'text=\'{"cell":{"a":8.4,"b":8.4,"c":24,"pbc":[true,true,false]},'
                '"ops":[]}\')'
            )
        rows = [{"design": r.slug, "title": r.title} for r in designs]
        return Response(
            body=f"# {len(designs)} structure(s)\n"
            + render_agent_table(rows, schema=["design", "title"])
        )

    def _toc_response(
        self,
        scene: Scene,
        ref: Any,
        handles: dict[str, int],
        *,
        head_verb: str | None = None,
        relax_summary: dict[str, Any] | None = None,
    ) -> Response:
        t = probe.toc(scene)
        pbc = "".join("T" if p else "F" for p in scene.cell.pbc)
        handle = handle_registry.try_format("structure", ref.id, chunk=False) or "—"
        verb = f" — {head_verb}" if head_verb else ""
        head = (
            f"# {ref.slug}{verb} · {t['formula']} · {t['natoms']} atoms · "
            f"pbc[{pbc}] · {t['nbonds']} bonds · {handle}"
        )
        lr = relax_summary or (ref.meta or {}).get("last_relax")
        if lr:
            state = "converged" if lr.get("converged") else "not converged"
            head += (
                f"\n# relax[{lr.get('rung')}]: {state} in {lr.get('n_steps')} steps "
                f"(max move {lr.get('max_disp')} Å)"
            )
        # gripe 161576: a compact |F| (eV/Å) column — a cheap DB read from a
        # recorded run at the CURRENT design version only (FIX 2); never the
        # live EMT estimate here (FIX 3 — that's view='atom'-only). '—' when
        # no such run exists (never fabricated).
        force_mags = self._force_magnitudes(scene, ref)
        rows = []
        for label, atom in scene.atoms.items():
            rows.append(
                {
                    "atom": f"{handle}#{label}",
                    "element": atom.element,
                    "frac": ",".join(f"{x:.3f}" for x in atom.frac),
                    "coord": probe.coordination(scene, label),
                    "fixed": "yes" if atom.fixed else "no",
                    "|F|": force_mags.get(label, "—"),
                }
            )
        body = (
            head
            + "\n"
            + render_agent_table(
                rows, schema=["atom", "element", "frac", "coord", "fixed", "|F|"]
            )
        )
        prov_rows = paper_provenance_rows(self.store, ref.id)
        if prov_rows:
            body += "\n\nProvenance:\n" + render_agent_table(
                prov_rows, schema=["rel", "paper", "doi", "note"]
            )
        return Response(body=body)

    # ── per-atom forces (gripe 161576) — a qualitative "which atoms are
    # doing the work" signal for the modeling LLM, not physics-grade truth (a
    # real MLIP/DFT relax runs later). Local-only: no cloud/container contract
    # change — the struct_relax cloud writeback leaves these columns NULL.

    def _resolve_forces(
        self,
        scene: Scene,
        ref: Any,
        *,
        run_id: int | None = None,
        allow_estimate: bool = False,
    ) -> dict[str, Any] | None:
        """Per-atom forces for this design, keyed by **label** — from a
        stored run (``run_id`` pins one regardless of design version; omitted,
        the *latest* run recorded at the design's *current* version, FIX 2:
        a superseded version's forces never silently surface as if current),
        or — only when ``allow_estimate`` (view='atom' explicitly, never the
        default TOC/list read, FIX 3) — a cheap on-demand ASE-EMT single-point
        estimate when no current-version run has one. ``None`` when nothing
        is available: a pinned ``run_id`` that doesn't exist (or recorded
        none), or no stored run and either estimation is disallowed here or
        the element set falls outside EMT's coverage — never fabricated.

        The join is by **label** (FIX 1), never a re-derived canonical rank:
        a relax can move an atom across a periodic image boundary, changing
        canonical rank (which sorts on fractional position) between write
        time and this read — re-deriving rank here would then silently
        attribute a force to the wrong atom. The stored ``forces`` payload
        already pairs labels with vectors at write time
        (:func:`precis.structure.cache.serialize_forces`), so this method
        never calls ``canonical_order``.

        Returns ``{"by_label": {label: [fx,fy,fz]}, "approx": bool, "source":
        str|None, "run": str|None}`` (``run`` is ``None`` for an on-demand
        estimate, since it was never persisted). An atom in ``scene`` missing
        from ``by_label`` (added since that run) is simply absent — callers
        check membership, never a bare index."""
        on_version = int((ref.meta or {}).get("version", 0))
        hit = self.store.structure_run_forces(
            ref.id, run_id=run_id, on_version=on_version
        )
        if run_id is not None and hit is None:
            return None  # no such run on this design
        labels = hit.get("labels") if hit else None
        vectors = hit.get("vectors") if hit else None
        approx = bool(hit.get("approx")) if hit else False
        source = hit.get("source") if hit else None
        run_label = f"r{hit['run_id']}" if hit else None
        if labels is None or vectors is None:
            if run_id is not None:
                return None  # the pinned run exists but recorded no forces
            if not allow_estimate:
                return None  # TOC/list path (FIX 3): never run live physics
            est = estimate_forces_emt(scene)
            if est is None:
                return None  # no current-version run, and outside EMT's coverage
            labels, vectors = relax_cache.serialize_forces(est)
            approx, source, run_label = True, "emt", None
        by_label = dict(zip(labels, vectors, strict=True))
        return {
            "by_label": by_label,
            "approx": approx,
            "source": source,
            "run": run_label,
        }

    @staticmethod
    def _parse_run_arg(run_arg: Any) -> int | None:
        """``run=`` accepts a bare id or the ``r<id>`` display form."""
        if run_arg is None:
            return None
        try:
            return int(str(run_arg).lstrip("rR"))
        except ValueError:
            raise BadInput(
                f"run={run_arg!r} is not a run id (try 'r12' or 12)"
            ) from None

    def _atom_force_line(
        self, scene: Scene, ref: Any, label: str, args: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """One ``view='atom'`` line: ``|F|`` + vector for ``label``, joined
        from :meth:`_resolve_forces` (the live EMT estimate IS allowed here —
        an explicit ``view='atom'`` read, FIX 3) — or a ``(None, note)``
        explaining why no estimate is available."""
        run_id = self._parse_run_arg(args.get("run"))
        resolved = self._resolve_forces(scene, ref, run_id=run_id, allow_estimate=True)
        if resolved is None:
            if run_id is not None:
                raise NotFound(
                    f"no run r{run_id} on this structure with forces recorded"
                )
            return None, (
                "|F|: unavailable (no force-bearing run at the current design "
                "version, and this element set is outside EMT's "
                "cheap-estimate coverage)"
            )
        if label not in resolved["by_label"]:
            return None, "|F|: unavailable (this atom was added since that run)"
        fx, fy, fz = resolved["by_label"][label]
        mag = (fx * fx + fy * fy + fz * fz) ** 0.5
        tag = "approx" if resolved["approx"] else "computed"
        src = f", {resolved['source']}" if resolved["source"] else ""
        run_note = (
            f" [{resolved['run']}]" if resolved["run"] else " [on-demand estimate]"
        )
        return (
            f"|F| = {mag:.4f} eV/Å (vector {fx:.3f},{fy:.3f},{fz:.3f}) "
            f"— {tag}{src}{run_note}",
            None,
        )

    def _force_magnitudes(self, scene: Scene, ref: Any) -> dict[str, str]:
        """Compact ``{label: '|F|'}`` for the TOC atom table's ``|F|`` column
        (gripe 161576) — a cheap DB read only, from a recorded run at the
        current design version; ``allow_estimate`` stays False (FIX 3: the
        live EMT estimate never runs on the default read). ``{}`` when
        nothing is available (renders '—' per atom)."""
        resolved = self._resolve_forces(scene, ref)
        if resolved is None:
            return {}
        out: dict[str, str] = {}
        for label, (fx, fy, fz) in resolved["by_label"].items():
            out[label] = f"{(fx * fx + fy * fy + fz * fz) ** 0.5:.3f}"
        return out

    def _render_probe(
        self, view: str, scene: Scene, args: dict[str, Any], *, ref: Any
    ) -> Response:
        if view == "atom":
            label = str(args.get("atom") or "").split("#")[-1]
            if label not in scene.atoms:
                raise NotFound(f"no atom {label!r} in this structure")
            atom = scene.atoms[label]
            nbrs = probe.neighborhood(scene, label, radius=3.5)
            head = (
                f"# {label} — {atom.element} frac({','.join(f'{x:.3f}' for x in atom.frac)}) "
                f"· coord {probe.coordination(scene, label)} · "
                f"fixed={'yes' if atom.fixed else 'no'}"
            )
            # gripe 161576 remainder: a "calc: …" line naming what produced
            # this design's numbers — the same struct_runs row selection as
            # the force readout below (pinned run=, else latest at the
            # CURRENT design version), but not restricted to force-bearing
            # rows (an external import's energy-only row still gets one).
            run_id = self._parse_run_arg(args.get("run"))
            calc_row = self._atom_view_calc_row(
                ref.id,
                run_id=run_id,
                on_version=int((ref.meta or {}).get("version", 0)),
            )
            calc_line = format_calc_identity(calc_row)
            if calc_line:
                head += "\n" + calc_line
            # gripe 161576: per-atom force readout — |F| + vector, joined from
            # a run's stored ``forces`` BY LABEL (``run=`` pins one), or a
            # cheap on-demand EMT estimate when no current-version run
            # carries one (the live estimate is only ever computed here, an
            # explicit view='atom' — never on the default TOC, FIX 3).
            force_line, force_note = self._atom_force_line(scene, ref, label, args)
            if force_line:
                head += "\n" + force_line
            head += "\nCharges: — (no backend produces partial charges yet)"
            if force_note:
                head += "\n" + force_note
            rows = [
                {
                    "neighbor": n.label,
                    "element": n.element,
                    "dist": f"{n.distance:.3f}",
                    "image": ",".join(str(x) for x in n.image),
                }
                for n in nbrs
            ]
            return Response(
                body=head
                + "\n"
                + render_agent_table(
                    rows, schema=["neighbor", "element", "dist", "image"]
                )
            )
        if view == "neighborhood":
            center = str(args.get("center") or "").split("#")[-1]
            if center not in scene.atoms:
                raise NotFound(f"no atom {center!r} in this structure")
            radius = float(args.get("radius", 3.0))
            rows = [
                {
                    "neighbor": n.label,
                    "element": n.element,
                    "dist": f"{n.distance:.3f}",
                    "image": ",".join(str(x) for x in n.image),
                }
                for n in probe.neighborhood(scene, center, radius)
            ]
            return Response(
                body=f"# neighbourhood of {center} within {radius} Å\n"
                + render_agent_table(
                    rows, schema=["neighbor", "element", "dist", "image"]
                )
            )
        if view == "bonds":
            rows = [
                {
                    "i": b.i,
                    "j": b.j,
                    "order": f"{b.order:g}",
                    "kind": b.kind,
                    "provenance": b.provenance,
                    "image": ",".join(str(x) for x in b.image),
                }
                for b in scene.bonds
            ]
            return Response(
                body=f"# {len(rows)} bonds\n"
                + render_agent_table(
                    rows, schema=["i", "j", "order", "kind", "provenance", "image"]
                )
            )
        if view == "find":
            labels = probe.find(
                scene,
                element=args.get("element"),
                undercoordinated=bool(args.get("undercoordinated", False)),
            )
            return Response(body="# found: " + (", ".join(labels) or "(none)"))
        # validate
        findings = validate(scene)
        if not findings:
            return Response(body="✓ no validator findings")
        rows = [
            {
                "rule": f.rule,
                "atoms": ",".join(f.atoms),
                "measured": f"{f.measured}",
                "expected": f"{f.expected}",
                "fix": f.suggested_fix,
            }
            for f in findings
        ]
        return Response(
            body=f"# {len(findings)} validator finding(s)\n"
            + render_agent_table(
                rows, schema=["rule", "atoms", "measured", "expected", "fix"]
            )
        )

    def _render_nav(self, view: str, scene: Scene, args: dict[str, Any]) -> Response:
        """The §6.2/§6.3/§6.5/§6.6 navigation probes — spatial rays/planes/
        spheres, graph topology (path/rings/fragments), diff, and the uniform
        embodiment readout. All pure reads over the hydrated Scene."""
        if view == "line":
            origin = _vec(args, "origin", default=[0.0, 0.0, 0.0])
            direction = _vec(args, "direction", "dir")
            if direction is None:
                raise BadInput("line needs direction= (a 3-vector, Cartesian Å)")
            radius = float(args.get("radius", 1.5))
            hits = probe.line(scene, origin, direction, radius)
            rows = [
                {
                    "atom": h.label,
                    "element": h.element,
                    "along": f"{h.along:.3f}",
                    "offset": f"{h.offset:.3f}",
                }
                for h in hits
            ]
            return Response(
                body=f"# {len(rows)} atoms within {radius} Å of the ray\n"
                + render_agent_table(
                    rows, schema=["atom", "element", "along", "offset"]
                )
            )
        if view == "plane":
            point = _vec(args, "point", default=[0.0, 0.0, 0.0])
            normal = _vec(args, "normal", "n")
            if normal is None:
                raise BadInput("plane needs normal= (a 3-vector, Cartesian)")
            thickness = float(args.get("thickness", 1.0))
            phits = probe.plane(scene, point, normal, thickness)
            rows = [
                {
                    "atom": h.label,
                    "element": h.element,
                    "off": f"{h.signed:+.3f}",
                    "u": f"{h.u:.3f}",
                    "v": f"{h.v:.3f}",
                }
                for h in phits
            ]
            return Response(
                body=f"# layer slice: {len(rows)} atoms within {thickness} Å of the plane\n"
                + render_agent_table(rows, schema=["atom", "element", "off", "u", "v"])
            )
        if view in ("bonds_through_plane", "bonds_in_sphere"):
            if view == "bonds_through_plane":
                point = _vec(args, "point", default=[0.0, 0.0, 0.0])
                normal = _vec(args, "normal", "n")
                if normal is None:
                    raise BadInput("bonds_through_plane needs normal=")
                crossing = probe.bonds_through_plane(scene, point, normal)
                head = f"# {len(crossing)} bonds cross the plane"
                acol = "∠normal"
            else:
                center = _vec(args, "center", "point")
                if center is None:
                    raise BadInput("bonds_in_sphere needs center=")
                radius = float(args.get("radius", 3.0))
                crossing = probe.bonds_in_sphere(scene, center, radius)
                head = f"# {len(crossing)} bonds in/crossing the {radius} Å sphere"
                acol = "∠"
            rows = [
                {
                    "i": c.i,
                    "j": c.j,
                    "order": f"{c.order:g}",
                    "length": f"{c.length:.3f}",
                    acol: f"{c.angle_to_normal:.1f}",
                }
                for c in crossing
            ]
            return Response(
                body=head
                + "\n"
                + render_agent_table(rows, schema=["i", "j", "order", "length", acol])
            )
        if view == "path":
            a = str(args.get("a") or args.get("from") or "").split("#")[-1]
            b = str(args.get("b") or args.get("to") or "").split("#")[-1]
            if a not in scene.atoms or b not in scene.atoms:
                raise NotFound("path needs a= and b= as live atom labels")
            chain = probe.path(scene, a, b)
            if chain is None:
                return Response(body=f"# no bond path {a} → {b} (disconnected)")
            return Response(
                body=f"# path {a} → {b} ({len(chain) - 1} bonds)\n" + " → ".join(chain)
            )
        if view == "rings":
            max_size = int(args.get("max_size", 8))
            found = probe.rings(scene, max_size)
            if not found:
                return Response(body=f"# no rings ≤ {max_size} atoms")
            lines = [f"- {len(r)}-ring: {', '.join(r)}" for r in found]
            return Response(body=f"# {len(found)} ring(s)\n" + "\n".join(lines))
        if view == "fragments":
            comps = probe.fragments(scene)
            rows = [
                {
                    "fragment": f"f{i + 1}",
                    "size": str(len(c)),
                    "formula": self._frag_formula(scene, c),
                    "atoms": ", ".join(c if len(c) <= 8 else [*c[:8], "…"]),
                }
                for i, c in enumerate(comps)
            ]
            return Response(
                body=f"# {len(comps)} fragment(s)\n"
                + render_agent_table(
                    rows, schema=["fragment", "size", "formula", "atoms"]
                )
            )
        if view == "diff":
            other = str(args.get("other") or args.get("vs") or "").strip()
            if not other:
                raise BadInput(
                    "diff needs other= (another structure slug to compare against)"
                )
            oref = resolve_live_slug_ref(self.store, kind="structure", id=other)
            oscene, _ = self.store.structure_load(oref.id)
            d = probe.diff(oscene, scene)
            head = f"# diff {other} → this · RMSD {d.rmsd:.3f} Å · max move {d.max_disp:.3f} Å"
            parts = [head]
            if d.atoms_added:
                parts.append("added: " + ", ".join(d.atoms_added))
            if d.atoms_removed:
                parts.append("removed: " + ", ".join(d.atoms_removed))
            if d.bonds_formed:
                parts.append(
                    "bonds formed: " + ", ".join(f"{i}-{j}" for i, j in d.bonds_formed)
                )
            if d.bonds_broken:
                parts.append(
                    "bonds broken: " + ", ".join(f"{i}-{j}" for i, j in d.bonds_broken)
                )
            top = [m for m in d.moved if m[1] > 1e-6][:10]
            if top:
                rows = [{"atom": la, "moved": f"{dd:.3f}"} for la, dd in top]
                parts.append(render_agent_table(rows, schema=["atom", "moved"]))
            return Response(body="\n".join(parts))
        # pov — the §6.6 embodiment readout
        support_raw = args.get("support") or args.get("atom")
        if support_raw is None:
            raise BadInput("pov needs support= (an atom label or a list of labels)")
        support = (
            [str(support_raw).split("#")[-1]]
            if isinstance(support_raw, str)
            else [str(s).split("#")[-1] for s in support_raw]
        )
        missing = [s for s in support if s not in scene.atoms]
        if missing:
            raise NotFound(f"no such atom(s) in support: {', '.join(missing)}")
        reach = float(args.get("reach", 3.0))
        p = probe.pov(scene, support, reach)
        head = (
            f"# pov · i_am={p.i_am} · i_include=[{', '.join(p.i_include)}] · "
            f"reach {reach} Å"
        )
        rows = [
            {"touch": la, "element": scene.atoms[la].element, "dist": f"{dist:.3f}"}
            for la, dist in p.i_touch
        ]
        return Response(
            body=head
            + "\n"
            + render_agent_table(rows, schema=["touch", "element", "dist"])
        )

    def _frag_formula(self, scene: Scene, labels: list[str]) -> str:
        comp: dict[str, int] = {}
        for la in labels:
            el = scene.atoms[la].element
            comp[el] = comp.get(el, 0) + 1
        return "".join(f"{el}{comp[el]}" for el in sorted(comp))

    def _card_text(self, title: str, scene: Scene, description: str = "") -> str:
        """The one embeddable summary per design — title + composition + the
        LLM's own description, so search(kind='structure') lands on intent."""
        t = probe.toc(scene)
        pbc = "".join("T" if p else "F" for p in scene.cell.pbc)
        elements = ", ".join(sorted(scene.composition()))
        intent = f" {description}" if description else ""
        return (
            f"{title} (atomistic structure).{intent} Composition: {t['formula']} "
            f"({elements}); {t['natoms']} atoms, {t['nbonds']} bonds; pbc[{pbc}]."
        )

    # ── search ───────────────────────────────────────────────────────
    def search(
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        """Find structures by intent over each design's one summary card
        (title + composition + description); ``mode=`` is lexical/semantic/hybrid."""
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='structure') requires q=",
                next="search(kind='structure', q='OH on Pd(111)')",
            )
        triples = self._card_search(
            str(q), query_vec=None, mode=mode, page_size=page_size
        )
        if not triples:
            return Response(
                body=f"no structures match {q!r}\n\n"
                "Next: widen with mode='semantic', or add a 'description' to a "
                "design so it's findable by purpose."
            )
        rows = []
        for _block, ref, _score in triples:
            handle = handle_registry.try_format("structure", ref.id, chunk=False) or "—"
            rows.append({"handle": handle, "design": ref.slug, "title": ref.title})
        return Response(
            body=f"# {len(triples)} structure(s) for {q!r}\n"
            + render_agent_table(rows, schema=["handle", "design", "title"])
        )

    def _card_search(
        self,
        q: str,
        *,
        query_vec: list[float] | None,
        mode: str | None,
        page_size: int,
    ) -> list[Any]:
        """Fused search over the per-design ``card_combined`` chunks."""
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        return self.store.blocks.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="structure",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            card_kinds=("card_combined",),
        )

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Design-level hits for the cross-kind merge (kind='*')."""
        triples = self._card_search(
            q, query_vec=query_vec, mode=mode, page_size=page_size
        )
        self.store.blocks.bump_salience([b.id for b, _r, _s in triples])
        out: list[SearchHit] = []
        for block, ref, score in triples:
            text = (getattr(block, "text", "") or "").strip()
            preview = text if len(text) <= 200 else text[:199].rstrip() + "…"
            out.append(
                SearchHit(
                    score=float(score),
                    kind="structure",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"structure:{ref.slug or ref.id}",
                    uhandle=handle_registry.try_format(
                        "structure", ref.id, chunk=False
                    ),
                )
            )
        return out
