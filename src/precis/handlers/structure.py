"""StructureHandler — the atomistic cell + bond-graph kind.

A ``structure`` design is a slug-addressed ref: a periodic cell (on
``refs.meta``) filled with atoms + a bond graph (the ``struct_*`` tables). The
LLM authors it as typed *ops* and reads it via *probes*, never pixels. Maps onto
the seven-verb surface:

- ``put``    — create/replace from a JSON spec ``{cell, ops}`` (``id=`` slug).
- ``edit``   — apply more ops to an existing design (``ops=`` or ``text=`` JSON).
- ``get``    — list designs, a design's TOC (``id=slug``), a probe
  (``view='atom'|'neighborhood'|'bonds'|'find'|'validate'`` — the hard-reject
  gate findings plus the never-gating VSEPR/hybridization advisory tier), a
  navigation
  probe (``view='line'|'plane'|'bonds_through_plane'|'bonds_in_sphere'|'path'|
  'rings'|'fragments'|'diff'|'pov'``), or an export — all with ``args=``.
  ``view='pair'`` (``args={'other': '<fs-slug>'}``) is the read-only NEB
  endpoint gate table (gr512126, slice 2) — see
  :mod:`precis.structure.pair_check`.
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

``completion`` and ``completion_digest`` read one captured geometry/run/request
state and delegate to the pure shared derivation. They bypass Scene hydration
and accept no policy/proof input, so polling cannot relax or import a design.

See ``precis-structure-help``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from precis.design.history import record_revision
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, Internal, NotFound, Unsupported, Upstream
from precis.format import render_agent_table
from precis.handlers._placement import RESERVED_PARENT_REL, place_ref
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.structure import (
    Atom,
    Bond,
    OpError,
    RelaxUnsupported,
    Scene,
    apply_ops,
    evaluate_measure,
    export,
    geometry_stats,
    probe,
    validate,
    vsepr,
)
from precis.structure import cache as relax_cache
from precis.structure import relax as run_relax
from precis.structure.canonical import normalize_scene
from precis.structure.card import card_text
from precis.structure.cell import Cell
from precis.structure.importers import (
    bundle_index,
    catalysis_hub,
    get_adapter,
    materials_project,
    mof,
)
from precis.structure.importers.cif import (
    CifRefused,
    CifUnsupported,
    cif_to_scene,
    fetch_cif_url,
)
from precis.structure.importers.extxyz import extxyz_to_scene
from precis.structure.preflight import PreflightReason
from precis.structure.preflight import _preflight_enabled as _mlip_preflight_enabled
from precis.structure.preflight import preflight as _mlip_preflight
from precis.utils.link_tag_ops import (
    apply_link_ops,
    format_link_tag_ack,
    require_link_target,
    validate_link_mode,
)

if TYPE_CHECKING:
    from precis.store.protocols import LinksStore

# NB: ``precis.structure`` re-exports the ``relax`` *function* under that
# name (see ``run_relax`` above), shadowing the submodule — reach
# ``EMT_ELEMENTS`` via the submodule path directly.
from precis.structure.relax import (
    EMT_ELEMENTS,
    RelaxResult,
    estimate_forces_emt,
    route_ml_model,
)
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)


def _atom_arg(args: dict[str, Any], *, alias: str | None = None) -> str:
    """The required atom label for ``view='atom'`` / ``'neighborhood'``.

    ``atom`` is the one key; ``alias`` (``center`` on neighborhood) is also
    accepted. Absent/empty raises BadInput naming the key(s); the caller
    raises NotFound for a present-but-unknown label.
    """
    keys = ("atom", alias) if alias else ("atom",)
    for k in keys:
        v = args.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().split("#")[-1]
    named = " or ".join(f"{k}=" for k in keys)
    raise BadInput(
        f"missing required arg {named} (an atom label, e.g. args={{'atom': 'aPd2'}})",
        next="get(kind='structure', id='<slug>', view='atoms') for the live labels",
    )


_PROBE_VIEWS = ("atom", "neighborhood", "bonds", "find", "validate", "stats")
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
_EXPORT_VIEWS = ("poscar", "extxyz", "cif", "pdb")
_VIEWS = (
    *_PROBE_VIEWS,
    *_NAV_VIEWS,
    "atoms",
    "runs",
    "pair",
    "neb",
    "generator",
    "completion",
    "completion_digest",
    "markers",
    "ray",
    "rays",
    "links",
    "literature",
    "pourbaix",
    "staircase",
    *_EXPORT_VIEWS,
)

#: At or below this many atoms the default TOC inlines the per-atom table
#: after the summary head; above it the table is behind ``view='atoms'``.
_TOC_INLINE_ATOMS = 40

#: ``meta['generated']`` keys ``view='generator'`` elides from the
#: record dump — per-atom payloads (site lists, termination ordinals,
#: seam atoms), the build ``report`` and the surface target/meridian
#: geometry, each kilobytes on a real record (gr514060). The elided names
#: still print in a trailer so the record doesn't read as smaller than
#: it is; ``precis_se``'s ``view='block'`` renders them for a bound
#: block.
_GENERATED_VIEW_ELIDED = frozenset(
    {
        "report",
        "sites",
        "junctions",
        "seam_atoms",
        "terminated",
        "surface_meridian",
        "surface_target",
    }
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
    #: Explicit force-convergence target (eV/Å) — the job's ``fmax`` param;
    #: None ⇒ each rung's own default.
    fmax: float | None = None
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


def _format_preflight_caveats(caveats: list[str]) -> str | None:
    """Echo block for the never-gating domain caveats (gripe 285774) — a
    metal-organic straddle or a declared net charge — so a passing edit
    still says "chemistry may be off-distribution" instead of a bare
    "converged". ``None`` when there's nothing to say (the common case)."""
    if not caveats:
        return None
    lines = ["structure preflight caveat(s) — advisory only, not blocking:"]
    lines += caveats
    return "\n".join(lines)


def _combine_echo(*parts: str | None) -> str | None:
    """Join whichever of ``parts`` are non-empty with a blank line, matching
    :meth:`StructureHandler._with_echo`'s own separator — the shared
    append-to-body mechanism for the import-fragment echo and the preflight
    caveat block, which a single response may carry both of."""
    joined = [p for p in parts if p]
    return "\n\n".join(joined) if joined else None


def _run_preflight_gate(scene: Scene) -> list[str]:
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
    :class:`BadInput` — the caller does not catch this.

    Returns the passing verdict's never-gating **caveat** messages (gripe
    285774: a metal-organic straddle, a declared net charge) — empty when
    preflight is off, degraded, or found nothing to flag. The caller
    surfaces these as an echo rather than dropping them silently."""
    if not _mlip_preflight_enabled():
        return []
    try:
        verdict = _mlip_preflight(scene)
    except Exception as exc:  # ImportError (no ASE/[dft]) or any other infra hiccup
        log.debug("structure preflight degraded (fail-open): %s", exc)
        return []
    if not verdict.ok:
        raise BadInput(
            _format_preflight_rejection(verdict.reasons),
            next="fix the flagged atom(s) (swap element / reposition / "
            "pack the slab) and re-apply the edit",
        )
    return [c.message for c in verdict.caveats]


def _relax_cache_address(
    scene: Scene,
    *,
    fidelity: str,
    model: str,
    steps: int,
    cell_mode: str | None,
    dispersion: bool = False,
    fmax: float | None = None,
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
    # Same convention as ``cell``: an explicit fmax changes the convergence
    # contract, so it folds into the key only when asked for — a tight-fmax
    # run never collides with a default one, and every pre-existing row stays
    # a hit.
    if fmax is not None:
        params["fmax"] = fmax
    # Dispersion is part of the calculator's identity, not a display flag: the
    # same model with D3 on and off returns different energies for the same
    # geometry. Keyed in only when ON, so every pre-existing (dispersion-free)
    # run-cube row stays a hit rather than being orphaned by a key change.
    if dispersion:
        params["dispersion"] = True
    cache_key = relax_cache.run_cache_key(
        scene, fidelity=fidelity, model=model, params=params
    )
    structure_sha = relax_cache.structure_sha(scene)
    order = relax_cache.canonical_order(scene)
    return cache_key, structure_sha, order


def _run_params(res: RelaxResult) -> dict[str, Any] | None:
    """The ``params`` jsonb for a ``struct_runs`` row — audit bookkeeping
    (``cached``) plus the bits of calculator identity that live outside the
    typed columns (``dispersion``). ``None`` when there's nothing to say, so
    the common row keeps its historically-empty params."""
    params: dict[str, Any] = {}
    if res.from_cache:
        params["cached"] = True
    if res.dispersion:
        params["dispersion"] = True
    if res.fmax is not None:
        params["fmax"] = res.fmax
    return params or None


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


def _is_conv_ml_on_version(run: Mapping[str, Any], version: int) -> bool:
    """A ``struct_runs`` row is the endpoint relax a NEB band starts from
    only when it's a converged ``ml`` run on the design's *current*
    version — the ``relaxed`` pair gate's unit, singled out so the mint
    can cite the run row (``is_run_id``/``fs_run_id``)."""
    return (
        run.get("fidelity") == "ml"
        and bool(run.get("converged"))
        and run.get("status") == "succeeded"
        and int(run.get("on_version") or 0) == version
    )


def _neb_conv_run(
    runs: Iterable[Mapping[str, Any]], version: int, model: str | None
) -> Mapping[str, Any] | None:
    """The newest converged ``ml`` run on ``version`` at ``model``
    (``structure_runs`` is newest-first)."""
    for r in runs:
        if _is_conv_ml_on_version(r, version) and str(r.get("model")) == model:
            return r
    return None


#: Env pin + default for the rolling-24h ``struct_neb`` mint cap per quest
#: (spec §Compute — the count covers jobs whose parent todo ``serves`` the
#: quest).
_NEB_QUEST_DAILY_ENV = "PRECIS_STRUCT_NEB_QUEST_DAILY"
_NEB_QUEST_DAILY_DEFAULT = 4

#: Env pin + default for the UTC-day ``struct_neb`` mint cap a quest can't
#: be charged for — calls without ``requested_by=`` (or whose todo serves
#: no quest). The cache key dedupes identical edges but nothing stops a
#: knob-varying loop without this bound (gr512126 review #5).
_NEB_GLOBAL_DAILY_ENV = "PRECIS_STRUCT_NEB_GLOBAL_DAILY"
_NEB_GLOBAL_DAILY_DEFAULT = 20


def _neb_quest_daily() -> int:
    raw = os.environ.get(_NEB_QUEST_DAILY_ENV)
    try:
        return max(1, int(raw)) if raw else _NEB_QUEST_DAILY_DEFAULT
    except ValueError:
        return _NEB_QUEST_DAILY_DEFAULT


def _neb_global_daily() -> int:
    raw = os.environ.get(_NEB_GLOBAL_DAILY_ENV)
    try:
        return max(1, int(raw)) if raw else _NEB_GLOBAL_DAILY_DEFAULT
    except ValueError:
        return _NEB_GLOBAL_DAILY_DEFAULT


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


#: A minimal copy-pasteable ``put`` payload, shared by every raise that
#: refuses a malformed or cell-less spec so the fix is one call.
_PUT_EXAMPLE = (
    "put(kind='structure', id='<slug>', text='"
    '{"cell":{"a":8.4,"b":8.4,"c":24,"pbc":[true,true,false]},"ops":[]}'
    "')"
)


def _looks_like_cif(text: str | None) -> bool:
    """A ``text=`` body that is a CIF rather than the JSON payload: not a
    JSON object, with a ``data_`` block line. :func:`cif_to_scene` makes the
    real check and refuses a body that only looks the part."""
    if not text or text.lstrip().startswith(("{", "[")):
        return False
    return any(line.lstrip().startswith("data_") for line in text.splitlines())


def _payload(text: str | None, args: dict[str, Any] | None) -> dict[str, Any]:
    if args:
        return dict(args)
    if text and text.strip():
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BadInput(
                f"structure payload must be JSON: {exc}", next=_PUT_EXAMPLE
            ) from exc
        if not isinstance(obj, dict):
            raise BadInput(
                "structure payload must be a JSON object {cell, ops}",
                next=_PUT_EXAMPLE,
            )
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
_CALC_PARAM_DIGEST_KEYS = (
    "steps",
    "fmax",
    "cutoff_eV",
    "kmesh",
    "spin",
    "cell",
    "dispersion",
)


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


def paper_provenance_rows(store: LinksStore, ref_id: int) -> list[dict[str, str]]:
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


def _kv_brief(d: Mapping[str, Any]) -> str:
    """``k=v`` pairs of a record section's scalar entries, in order."""
    return " ".join(
        f"{k}={v}" for k, v in d.items() if isinstance(v, str | int | float | bool)
    )


def _generator_params_brief(rec: Mapping[str, Any]) -> str:
    """The authored params of a ``meta['generated']`` record as one line.

    ``hexfold_scene`` keeps its normalized params under ``scene`` (a
    ``features`` list of dicts plus scene-level scalars), a workspace
    import under ``params``, and a spec generator's only params *are*
    the ``spec`` source — collapsed onto one line."""
    scene = rec.get("scene")
    if isinstance(scene, Mapping):
        parts = [
            _kv_brief(f) for f in scene.get("features") or () if isinstance(f, Mapping)
        ]
        rest = _kv_brief({k: v for k, v in scene.items() if k != "features"})
        if rest:
            parts.append(rest)
        joined = "; ".join(p for p in parts if p)
        if joined:
            return joined
    params = rec.get("params")
    if isinstance(params, Mapping) and params:
        return _kv_brief(params)
    spec = rec.get("spec")
    if isinstance(spec, str) and spec.strip():
        return " ".join(spec.split())
    return ""


def _generator_brief(rec: Any) -> str:
    """The structure search row's ``gen`` cell: the generator name plus
    the authored-params digest (``hexfold_scene rows=6 periods=8
    seams=++++++ strip_rows=4``), ``'—'`` for a design with no
    ``meta['generated']`` record (gr514060)."""
    if not isinstance(rec, Mapping) or not rec.get("generator"):
        return "—"
    brief = _generator_params_brief(rec)
    name = str(rec["generator"])
    if not brief:
        return name
    if len(brief) > 200:
        brief = brief[:197].rstrip() + "…"
    return f"{name} {brief}"


class StructureHandler(Handler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with the verb bodies.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "put": ArgsSpec(
            {
                "cell",
                "ops",
                "description",
                "cif",
                "cif_url",
                "extxyz",
                "source",
                "config_id",
                # args={'derive_from': '<parent>', 'ops': [...]} — the
                # label-matched endpoint door (gr512126 slice 1). Left
                # undeclared at ship, it bounced off the dispatch args-key
                # gate — the pair gates' nexts point at it.
                "derive_from",
                # mode='rerun' only: ladder-rung override (default: the
                # candidate's highest completed rung), and the dispatch-time
                # seed list (default: the reaction config's pinned seeds).
                "tier",
                "seeds",
                # mode='neb' (gr512126 slice 3): the FS endpoint, the
                # engine knob set (structure.neb.NEB_DEFAULTS) and the
                # request context.
                "final",
                "fs",
                "model",
                "requested_by",
                "parent_id",
                "images",
                "path_seeds",
                "fmax",
                "max_steps",
                "retries",
                "tether",
                "optimizer",
            }
        ),
        "edit": ArgsSpec({"ops"}),
        "get": ArgsSpec(
            {
                "source",
                "q",
                "config_id",
                "surface_composition",
                "facet",
                "first",
                "atom",
                "center",
                "run",
                "radius",
                "element",
                "undercoordinated",
                "origin",
                "direction",
                "dir",
                "point",
                "normal",
                "n",
                "thickness",
                "a",
                "b",
                "from",
                "to",
                "max_size",
                "other",
                "vs",
                "support",
                "reach",
                # view='staircase': the picked point (``point`` doubles as
                # 'U,pH' there, gr489471)
                "U",
                "pH",
            }
        ),
    }

    #: atomic coordinates / CIF / edit ops: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="structure",
        title="Structure",
        description=(
            "Atomistic cell + bond-graph design. put creates/replaces "
            "from JSON {cell:{a,b,c,pbc}|{lattice,pbc}, ops:[...]} (cell optional "
            "when ops start with a `slab` bulk template), or from a CIF "
            "(args={'cif': text} or {'cif_url': url}); edit applies "
            "more ops (slab/set_cell/add_atom/set_element/vacancy/displace/add_bond/"
            "remove_bond/constrain, plus eye/measure/unmark/remove_measure "
            "markers, plus the fragment-building ops "
            "ring/attach/import_fragment/from_smiles); "
            "get lists designs, shows a TOC (id=slug), or probes "
            "(view='atom|neighborhood|bonds|find|validate|markers', args={...}); "
            "view='literature' assembles a deterministic paper-search query "
            "from the design's own composition/description (no LLM) and runs "
            "it against the paper corpus (gr161578); "
            "get(args={'source':'catalysis-hub', 'surface_composition':.., "
            "'facet':.., 'config_id':..}) (or source 'qmof'|'core-mof' with "
            "config_id) on-demand hydrates a config from an "
            "external DB into a cited, read-only design "
            "(a repeat call by config_id is a cache hit, no refetch); "
            "get(id='mp-<n>', args={'source':'materials-project'}) does the "
            "same for a Materials Project bulk crystal (needs the "
            "PRECIS_MP_API_KEY secret); "
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
        placement="artifact",
        views=_VIEWS,
        # put(id=<slug-or-handle>, mode='rerun') is the deliberate-replay
        # door onto the quest compute lane (gr499904): re-dispatches the
        # candidate's autocatpath sim unchanged under a fresh nonce, linked
        # to the earlier result. See precis-quest-help / request_rerun.
        # mode='neb' queues one CI-NEB edge between two gated endpoints
        # (gr512126 slice 3) — _put_neb.
        modes=("rerun", "neb"),
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
        normalize: bool = False,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        """``normalize=True`` rewrites the materialized scene into its
        periodic-symmetry canonical frame (:func:`precis.structure.canonical.
        normalize_scene`) right after ops are applied, before preflight/
        validation/save — quest candidates opt in so every stored candidate
        for a quest lives in one canonical frame and translation/rotation/
        mirror twins hash identically. Default off; existing callers
        (dispatch registry included) are unaffected.

        CIF import: ``args={'cif': <text>}`` or ``args={'cif_url': <url>}``
        (or a bare CIF body in ``text=``) builds the design from a CIF
        instead of ops — see :meth:`_put_cif`. extXYZ import:
        ``args={'extxyz': <text>}`` — the workspace atomic geometry format
        (per-atom ``label``/``maker`` columns) — :meth:`_put_extxyz`.

        ``mode='rerun'`` does not touch the design at all — it asks the quest
        compute lane for a deliberate replay of this candidate's autocatpath
        evaluation (:func:`precis.quest.compute.request_rerun`).

        ``args={'derive_from': '<parent>', 'ops': [...]}`` branches a NEW
        design (``id=``) off ``<parent>`` with the ops applied and the
        parent's atom labels kept — the label-matched-endpoint door
        (gr512126; :meth:`_put_derive` → :meth:`derive`)."""
        if mode == "rerun":
            return self._rerun(id=id, args=args)
        if mode == "neb":
            return self._put_neb(id=id, args=args)
        a = args or {}
        if a.get("derive_from") is not None:
            return self._put_derive(id=id, title=title, args=a)
        cif_text = a.get("cif")
        cif_url = a.get("cif_url")
        if (
            cif_text is None
            and cif_url is None
            and not set(a) - {"description"}
            and _looks_like_cif(text)
        ):
            cif_text = text
        if cif_text is not None or cif_url is not None:
            return self._put_cif(
                id=id,
                title=title,
                cif_text=cif_text,
                cif_url=cif_url,
                args=a,
                normalize=normalize,
            )
        if a.get("extxyz") is not None:
            return self._put_extxyz(
                id=id,
                title=title,
                extxyz_text=a["extxyz"],
                args=a,
                normalize=normalize,
            )
        if a.get("source") is not None or a.get("config_id") is not None:
            raise BadInput(
                "args source=/config_id= name a CIF import's source; they need "
                "args cif= or cif_url=",
                next="put(kind='structure', args={'cif': '<CIF text>', "
                "'source': 'core-mof', 'config_id': '<refcode>'})",
            )
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
                next=f"put(kind='structure', id='<new-slug>', "
                f"args={{'derive_from': {slug!r}, 'ops': [...]}})",
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
                "cell-establishing op like 'slab')",
                next=_PUT_EXAMPLE,
            )
        relax_ops, import_echo = self._apply_ops_with_imports(scene, ops)
        res = self._run_ops(scene, relax_ops)
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
        if normalize:
            normalize_scene(scene)
        relax_summary = self._relax_summary(relax_result)
        version = (int(existing.meta.get("version", 0)) + 1) if existing else 1
        desc = str(payload.get("description") or "").strip()
        ttl = (title or slug).strip() or slug
        # Tier-0 preflight (gated, default off): reject before anything
        # persists — the prior version (none yet, for a fresh put) stands.
        # A passing verdict may still carry never-gating domain caveats
        # (gripe 285774) — echoed below, never dropped silently.
        preflight_caveats = _format_preflight_caveats(_run_preflight_gate(scene))
        with self.store.tx() as conn:
            ref, created = self.store.structure_save(
                slug=slug,
                title=ttl,
                scene=scene,
                version=version,
                card_text=self._card_text(ttl, scene, desc),
                description=desc,
                relax_summary=relax_summary,
                conn=conn,
            )
            # The revision record (design-workbench slice 2) commits with
            # the version it describes. A put's ops built the whole scene
            # from nothing, so they ARE the delta from an empty design.
            record_revision(self.store, ref.id, rev=version, ops=list(ops), conn=conn)
        self._record_run(ref.id, relax_result, version)
        if dispatch is not None:
            return self._with_echo(
                self._dispatch_relax(ref, version, dispatch),
                _combine_echo(import_echo, preflight_caveats),
            )
        _scene, handles = self.store.structure_load(ref.id)
        verb = "created" if created else "updated"
        return self._with_echo(
            self._toc_response(
                _scene, ref, handles, head_verb=verb, relax_summary=relax_summary
            ),
            _combine_echo(import_echo, preflight_caveats),
        )

    def _rerun(self, *, id: str | int | None, args: dict[str, Any] | None) -> Response:
        """``put(id=<slug-or-handle>, mode='rerun')``: ask the quest compute
        lane to re-run this candidate's autocatpath evaluation unchanged
        (:func:`precis.quest.compute.request_rerun` — mints a fresh,
        nonce-salted job tree linked to the earlier result; refuses while a
        run is in flight). ``args={'tier': 'screening'|'neb'|'verify'}``
        overrides the rung; default is the candidate's highest completed
        rung. ``args={'seeds': [<ints>]}`` picks the seed set this
        evaluation runs (gr508948 — multi-seed at dispatch time, e.g. fresh
        endpoint-agreement evidence); default is the reaction config's
        pinned seeds. The design itself is untouched — no cell/ops accepted
        here."""
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='structure', mode='rerun') requires id= (the "
                "candidate slug or st<N> handle)",
                next="put(kind='structure', id='st<N>', mode='rerun')",
            )
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        tier = (args or {}).get("tier")
        from precis.quest.compute import request_rerun

        note = request_rerun(
            self.store,
            int(ref.id),
            hub=self.hub,
            tier=str(tier) if tier else None,
            seeds=(args or {}).get("seeds"),
        )
        return Response(body=note)

    def _put_cif(
        self,
        *,
        id: str | int | None,
        title: str | None,
        cif_text: Any,
        cif_url: Any,
        args: dict[str, Any],
        normalize: bool = False,
    ) -> Response:
        """Create a design from a CIF (text, or a URL fetched through
        ``safe_get``). No ``source=``: an ordinary editable design under
        ``id=``, with no external run. ``source='qmof'|'core-mof'`` plus
        ``config_id=``: the record goes through that source's adapter and
        ``store.structure_import``, so it lands read-only with an external
        run under the import slug; ``id=`` is refused there because the
        import picks the slug. A named-source put never overwrites an
        already-imported ``(source, config_id)`` (that would rewrite a real
        reference record with caller geometry), and its method fingerprint
        records that the caller supplied the CIF.

        The Tier-0 preflight gate is not run: its vacuum-headroom and porous-
        slab checks assume a slab, and a CIF is usually a 3D framework."""
        if cif_text is not None and cif_url is not None:
            raise BadInput(
                "put(kind='structure') takes args cif= or cif_url=, not both",
                next="drop one of args cif= / cif_url=",
            )
        if cif_text is not None and not isinstance(cif_text, str):
            raise BadInput(
                "args cif= must be the CIF text as a string",
                next="put(kind='structure', id='<slug>', args={'cif': '<CIF text>'})",
            )
        source = args.get("source")
        source = str(source).strip().lower() if source is not None else None
        config_id = str(args.get("config_id") or "").strip() or None
        if source is not None:
            if source not in mof.CIF_ADAPTERS:
                raise BadInput(
                    f"no CIF adapter for source {source!r}",
                    next="omit source= for an ordinary design, or use one of "
                    f"{', '.join(mof.CIF_ADAPTERS)}",
                    options=list(mof.CIF_ADAPTERS),
                )
            if config_id is None:
                raise BadInput(
                    f"a {source} import needs config_id= (the record id)",
                    next=f"put(kind='structure', args={{'cif': '<CIF text>', "
                    f"'source': {source!r}, 'config_id': '<id>'}})",
                )
            if id is not None and str(id).strip():
                raise BadInput(
                    f"a {source} import picks its own slug from config_id=; "
                    "id= is not used",
                    next="drop id= and keep source= / config_id=",
                )
            if self.store.find_ref_by_identifier(source, config_id, kind="structure"):
                raise BadInput(
                    f"{source} record {config_id!r} is already imported; a put "
                    "does not overwrite a reference record",
                    next=f"get(kind='structure', args={{'source': {source!r}, "
                    f"'config_id': {config_id!r}}}), or put the CIF under your "
                    "own id= without source=",
                )
        elif id is None or not str(id).strip():
            raise BadInput(
                "put(kind='structure') with a CIF requires id= (the design slug)",
                next="put(kind='structure', id='<slug>', args={'cif': '<CIF text>'})",
            )
        if cif_url is not None:
            url = str(cif_url).strip()
            if not url.lower().startswith(("http://", "https://")):
                raise BadInput(
                    f"args cif_url= must be an http(s) URL, got {url!r}",
                    next="put(kind='structure', id='<slug>', "
                    "args={'cif_url': 'https://…/x.cif'})",
                )
            import httpx

            try:
                cif_text = fetch_cif_url(url)
            except CifRefused as exc:
                raise BadInput(str(exc), next=exc.next) from exc
            except httpx.HTTPError as exc:
                raise Upstream(
                    f"fetching {url} failed: {exc}",
                    next="retry in a minute, or pass the CIF text as args={'cif': ...}",
                ) from exc
        else:
            url = None
        try:
            if source is not None:
                adapter_fn = get_adapter(source)
                scene, run, ext_id = adapter_fn(
                    {
                        "id": config_id,
                        "cif_text": cif_text,
                        "method": {"geometry_supplied_by": "put"},
                    }
                )
            else:
                scene = cif_to_scene(cif_text)
        except CifRefused as exc:
            raise BadInput(str(exc), next=exc.next) from exc
        except CifUnsupported as exc:
            raise Unsupported(
                str(exc), next="install it:  pip install 'precis-mcp[dft]'"
            ) from exc
        if source is not None:
            ref_id = self.store.structure_import(scene, run, ext_id)
            return self._render_hydrated(
                [(ref_id, ext_id.dataset, ext_id.config_id, run.method)],
                cached=False,
                note=None if run.energy is not None else "no energy (geometry only)",
            )
        slug = str(id).strip()
        existing = self.store.get_ref(kind="structure", id=slug)
        if existing is not None and self._external_provenance(existing.id) is not None:
            raise BadInput(
                "this is an imported reference config (provenance:external) "
                "— derive a variant instead of overwriting it in place",
                next=f"put(kind='structure', id='<new-slug>', "
                f"args={{'derive_from': {slug!r}, 'ops': [...]}})",
            )
        if normalize:
            normalize_scene(scene)
        version = (int(existing.meta.get("version", 0)) + 1) if existing else 1
        desc = str(args.get("description") or "").strip()
        ttl = (title or slug).strip() or slug
        op: dict[str, Any] = {"op": "import_cif", "natoms": len(scene.atoms)}
        if url is not None:
            op["url"] = url
        with self.store.tx() as conn:
            ref, created = self.store.structure_save(
                slug=slug,
                title=ttl,
                scene=scene,
                version=version,
                card_text=self._card_text(ttl, scene, desc),
                description=desc,
                conn=conn,
            )
            record_revision(self.store, ref.id, rev=version, ops=[op], conn=conn)
        _scene, handles = self.store.structure_load(ref.id)
        return self._toc_response(
            _scene, ref, handles, head_verb="created" if created else "updated"
        )

    def _put_extxyz(
        self,
        *,
        id: str | int | None,
        title: str | None,
        extxyz_text: Any,
        args: dict[str, Any],
        normalize: bool = False,
    ) -> Response:
        """Create a design from extXYZ text — the workspace atomic
        geometry format (structure-workspaces slice 0,
        :func:`precis.structure.importers.extxyz.extxyz_to_scene`): the
        ``label``/``maker`` per-atom columns land on the structure rows.
        No ``source=``/URL path — this is the plain editable-design door
        beside ``_put_cif``'s no-source half."""
        if not isinstance(extxyz_text, str):
            raise BadInput(
                "args extxyz= must be the extXYZ text as a string",
                next="put(kind='structure', id='<slug>', "
                "args={'extxyz': '<extxyz text>'})",
            )
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='structure') with extXYZ requires id= (the design slug)",
                next="put(kind='structure', id='<slug>', "
                "args={'extxyz': '<extxyz text>'})",
            )
        scene = extxyz_to_scene(extxyz_text)
        slug = str(id).strip()
        existing = self.store.get_ref(kind="structure", id=slug)
        if existing is not None and self._external_provenance(existing.id) is not None:
            raise BadInput(
                "this is an imported reference config (provenance:external) "
                "— derive a variant instead of overwriting it in place",
                next=f"put(kind='structure', id='<new-slug>', "
                f"args={{'derive_from': {slug!r}, 'ops': [...]}})",
            )
        if normalize:
            normalize_scene(scene)
        version = (int(existing.meta.get("version", 0)) + 1) if existing else 1
        desc = str(args.get("description") or "").strip()
        ttl = (title or slug).strip() or slug
        with self.store.tx() as conn:
            ref, created = self.store.structure_save(
                slug=slug,
                title=ttl,
                scene=scene,
                version=version,
                card_text=self._card_text(ttl, scene, desc),
                description=desc,
                conn=conn,
            )
            record_revision(
                self.store,
                ref.id,
                rev=version,
                ops=[{"op": "import_extxyz", "natoms": len(scene.atoms)}],
                conn=conn,
            )
        _scene, handles = self.store.structure_load(ref.id)
        return self._toc_response(
            _scene, ref, handles, head_verb="created" if created else "updated"
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
        turn: str | None = None,
        **_kw: Any,
    ) -> Response:
        """``turn`` is the workbench chat turn this edit came from
        (``<conv-slug>~<block ordinal>``, :mod:`precis_web.design_turn`),
        stamped onto the revision row; ``None`` for every other caller."""
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
            raise BadInput(
                "edit(kind='structure') requires id= (the design slug)",
                next="get(kind='structure') to list designs",
            )
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        if self._external_provenance(ref.id) is not None:
            raise BadInput(
                "this is an imported reference config (provenance:external) "
                "— derive a variant instead of editing it in place",
                next=f"put(kind='structure', id='<new-slug>', "
                f"args={{'derive_from': {ref.slug!r}, 'ops': [...]}})",
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
        relax_ops, import_echo = self._apply_ops_with_imports(scene, op_list)
        res = self._run_ops(scene, relax_ops)
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
        # A passing verdict may still carry never-gating domain caveats
        # (gripe 285774) — echoed below, never dropped silently.
        preflight_caveats = _format_preflight_caveats(_run_preflight_gate(scene))
        with self.store.tx() as conn:
            self.store.structure_save(
                slug=str(ref.slug),
                title=ttl,
                scene=scene,
                version=version,
                card_text=self._card_text(ttl, scene, desc),
                description=desc,
                relax_summary=relax_summary,
                conn=conn,
            )
            # Same transaction as the save: the version and its record are
            # one fact (design-workbench slice 2).
            record_revision(
                self.store,
                ref.id,
                rev=version,
                ops=list(op_list),
                turn=turn,
                conn=conn,
            )
        self._record_run(ref.id, relax_result, version)
        if dispatch is not None:
            return self._with_echo(
                self._dispatch_relax(ref, version, dispatch),
                _combine_echo(import_echo, preflight_caveats),
            )
        _scene, handles = self.store.structure_load(ref.id)
        return self._with_echo(
            self._toc_response(
                _scene, ref, handles, head_verb="edited", relax_summary=relax_summary
            ),
            _combine_echo(import_echo, preflight_caveats),
        )

    def _put_derive(
        self,
        *,
        id: str | int | None,
        title: str | None,
        args: dict[str, Any],
    ) -> Response:
        """``put(kind='structure', id='<new-slug>', args={'derive_from':
        '<parent>', 'ops': [...]})`` — the MCP door onto :meth:`derive`
        (gr512126): branch a new design off a parent, keeping the parent's
        atom labels, so the new design can be a label-matched NEB endpoint
        partner. ``id=`` is the NEW slug; ``derive_from`` is the parent."""
        slug = str(id).strip() if id is not None else ""
        call = (
            "put(kind='structure', id='<new-slug>', "
            "args={'derive_from': '<parent-slug>', 'ops': [...]})"
        )
        if not slug:
            raise BadInput(
                "put(kind='structure', args={'derive_from'}) requires id= "
                "(the NEW design's slug — the parent goes in derive_from=)",
                next=call,
            )
        parent_slug = str(args.get("derive_from") or "").strip()
        if not parent_slug:
            raise BadInput(
                "args 'derive_from' needs the parent design's slug",
                next=call,
            )
        if self.store.get_ref(kind="structure", id=slug) is not None:
            raise BadInput(
                f"design {slug!r} already exists",
                next=f"put(kind='structure', id={slug + '-2'!r}, "
                f"args={{'derive_from': {parent_slug!r}, 'ops': [...]}})",
            )
        ops = args.get("ops") or []
        if not isinstance(ops, list):
            raise BadInput(
                "args 'ops' must be a list of op objects",
                next="args={'derive_from': '<parent>', 'ops': [{'op': ...}]}",
            )
        return self.derive(id=parent_slug, to=slug, ops=ops, title=title)

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
        ``derived-from`` the parent (bundle — the instruction-box Apply; over
        MCP this is ``put(id=<new-slug>, args={'derive_from': <parent>})``).

        The parent is untouched, so a before/after ``view='diff'`` works. Applies
        graph/marker ops only — a relax is a separate compute step, never part of
        a proposal. The parent's markers carry over (they live on the scene).
        The ack names whether the atom label set survived the ops — labels
        preserved means the child is pairable with the parent as an endpoint
        pair; a changed atom set is not."""
        parent = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        to_slug = str(to).strip()
        if not to_slug:
            raise BadInput(
                "derive requires to= (the new design slug)",
                next="put(kind='structure', id='<new-slug>', "
                "args={'derive_from': '<parent-slug>', 'ops': [...]})",
            )
        if self.store.get_ref(kind="structure", id=to_slug) is not None:
            raise BadInput(
                f"design {to_slug!r} already exists",
                next="pick a fresh slug for the derived design",
            )
        scene, _ = self.store.structure_load(parent.id)
        parent_labels = set(scene.atoms)
        op_list = ops or []
        if any(o.get("op") == "relax" for o in op_list):
            raise BadInput(
                "derive applies graph/marker ops only (no relax)",
                next="drop the relax op, then relax the derived design with "
                "edit(kind='structure', id='<new-slug>', "
                "ops=[{'op':'relax','fidelity':'ml','steps':300}])",
            )
        # Same interception as put/edit so import_fragment works in a derive
        # too (relax was rejected above, so the returned relax tail is empty).
        _relax_tail, import_echo = self._apply_ops_with_imports(scene, op_list)
        ttl = (title or to_slug).strip() or to_slug
        with self.store.tx() as conn:
            ref, _created = self.store.structure_save(
                slug=to_slug,
                title=ttl,
                scene=scene,
                version=1,
                card_text=self._card_text(ttl, scene, ""),
                conn=conn,
            )
            # Revision 1 of the new design is the delta from its parent —
            # the ops the Apply button (or a derive call) handed over.
            record_revision(self.store, ref.id, rev=1, ops=list(op_list), conn=conn)
        # lineage: the derived design points back to its parent
        self.store.add_link(
            src_ref_id=ref.id, dst_ref_id=parent.id, relation="derived-from"
        )
        _scene, handles = self.store.structure_load(ref.id)
        return self._with_echo(
            self._toc_response(_scene, ref, handles, head_verb="derived"),
            _combine_echo(
                import_echo,
                self._pairability_echo(scene, parent, op_list, parent_labels),
            ),
        )

    @staticmethod
    def _pairability_echo(
        scene: Scene,
        parent: Any,
        op_list: list[dict[str, Any]],
        parent_labels: set[str],
    ) -> str:
        """The derive ack's endpoint-pair verdict (gr512126): whether the
        child's atom LABEL SET still matches the parent's — an NEB band
        interpolates between endpoints by label, so a label-matched pair is
        a candidate and a changed atom set is not."""
        parent_handle = handle_registry.try_format(
            "structure", parent.id, chunk=False
        ) or str(parent.slug)
        added = sorted(set(scene.atoms) - parent_labels)
        removed = sorted(parent_labels - set(scene.atoms))
        if not added and not removed:
            return (
                f"labels preserved · {len(op_list)} ops · "
                f"{len(scene.atoms)} atoms — NEB-pairable with {parent_handle}"
            )
        return (
            f"atom set changed (+{len(added)}/-{len(removed)}): "
            "not NEB-pairable with "
            f"{parent_handle}"
        )

    # ── mode='neb' ───────────────────────────────────────────────────
    def _put_neb(
        self, *, id: str | int | None, args: dict[str, Any] | None
    ) -> Response:
        """``put(id=<is-slug>, mode='neb', args={'final': '<fs-slug>',
        'images': 7, 'path_seeds': 2, 'model': 'mace_mp', 'fmax': 0.05})`` —
        queue one CI-NEB edge between two agent-authored endpoints
        (gr512126, slice 3; ``docs/backlog/neb-structure-builder.md``).

        The pair gates — the same :meth:`_render_pair` table — run first
        and zero-compute: a failed gate refuses with that row's ``next=``
        and nothing is minted; a warn rides the ack. On pass the request
        is content-addressed (``neb.neb_cache_key``): an identical earlier
        success is a cache hit, an in-flight one collapses onto it, and
        only a genuinely new edge mints a ``struct_neb_runs`` row plus the
        ``struct_neb`` job. The rung is ``ml`` only (spec §Compute).
        ``requested_by=<todo>`` arms the wait exactly like a relax and
        counts against the serving quest's daily cap
        (:data:`_NEB_QUEST_DAILY_ENV`, default 4 per rolling 24 h)."""
        from precis.structure import pair_check
        from precis.structure.neb import (
            NEB_DEFAULTS,
            neb_cache_key,
            normalize_neb_params,
        )

        call = (
            "put(kind='structure', id='<is-slug>', mode='neb', "
            "args={'final': '<fs-slug>'})"
        )
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='structure', mode='neb') requires id= (the "
                "initial-state design slug or st<N> handle)",
                next=call,
            )
        a = args or {}
        fs_arg = str(a.get("final") or a.get("fs") or "").strip()
        if not fs_arg:
            raise BadInput(
                "mode='neb' needs args final= (the final-state design slug)",
                next=call,
            )
        request_keys = {"final", "fs", "model", "requested_by", "parent_id"}
        unknown = sorted(set(a) - set(NEB_DEFAULTS) - request_keys)
        if unknown:
            raise BadInput(
                f"mode='neb' args: unknown key(s) {unknown}",
                next=f"the engine knobs are {sorted(NEB_DEFAULTS)} plus "
                f"{sorted(request_keys)}",
            )
        try:
            engine_params = normalize_neb_params(
                {k: a[k] for k in NEB_DEFAULTS if k in a}
            )
        except ValueError as exc:
            raise BadInput(
                str(exc),
                next="the engine knobs are " + ", ".join(sorted(NEB_DEFAULTS)),
            ) from exc

        is_ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        fs_ref = resolve_live_slug_ref(self.store, kind="structure", id=fs_arg)
        if fs_ref.id == is_ref.id:
            raise BadInput(
                "mode='neb': the IS and FS are the same design — an edge "
                "needs two distinct endpoints",
                next="derive a distinct FS — "
                f"put(kind='structure', id='<new-slug>', "
                f"args={{'derive_from': {is_ref.slug!r}, 'ops': [...]}})",
            )
        is_scene, _ = self.store.structure_load(is_ref.id)
        fs_scene, _ = self.store.structure_load(fs_ref.id)
        is_version = int((is_ref.meta or {}).get("version", 0))
        fs_version = int((fs_ref.meta or {}).get("version", 0))
        is_runs = self.store.structure_runs(is_ref.id)
        fs_runs = self.store.structure_runs(fs_ref.id)
        is_slug = str(is_ref.slug or is_ref.id)
        fs_slug = str(fs_ref.slug or fs_arg)
        verdict = pair_check.evaluate_pair(
            is_scene,
            fs_scene,
            is_version=is_version,
            fs_version=fs_version,
            is_runs=is_runs,
            fs_runs=fs_runs,
            is_slug=is_slug,
            fs_slug=fs_slug,
        )
        if not verdict.ok:
            fails = verdict.failed
            detail = " · ".join(f"{g.gate} ({g.detail})" for g in fails)
            raise BadInput(
                f"neb pair {is_slug} ↔ {fs_slug} blocked — {detail}",
                next=fails[0].next
                or (
                    f"get(kind='structure', id={is_slug!r}, view='pair', "
                    f"args={{'other': {fs_slug!r}}}) for the gate table"
                ),
            )
        warns = [g for g in verdict.gates if g.verdict == pair_check.WARN]

        # The band's calculator must be a model BOTH endpoints converged
        # at on their current versions — the 'relaxed' gate's shared set —
        # else it would price a differently-relaxed geometry than the one
        # the gate checked.
        caller_model = str(a.get("model") or "").strip() or None
        is_models = pair_check._ml_models(is_runs, is_version)
        fs_models = pair_check._ml_models(fs_runs, fs_version)
        shared = is_models & fs_models
        if caller_model is not None:
            missing = [
                slug
                for slug, models in ((is_slug, is_models), (fs_slug, fs_models))
                if caller_model not in models
            ]
            if missing:
                raise BadInput(
                    f"model {caller_model!r} has no converged ml run on "
                    f"{' and '.join(missing)}'s current version — the band "
                    "would price a differently-relaxed geometry",
                    next=" then ".join(
                        pair_check._relax_edit(s, model=caller_model) for s in missing
                    ),
                )
            model: str | None = caller_model
        else:
            # The IS's freshest shared-model relax names the calculator.
            model = next(
                (
                    str(r["model"])
                    for r in is_runs
                    if str(r.get("model")) in shared
                    and _is_conv_ml_on_version(r, is_version)
                ),
                sorted(shared)[0] if shared else None,
            )
        is_run = _neb_conv_run(is_runs, is_version, model)
        fs_run = _neb_conv_run(fs_runs, fs_version, model)
        cache_key = neb_cache_key(
            is_scene, fs_scene, fidelity="ml", model=model, params=engine_params
        )

        # Content-addressed dedupe before any cap or mint: an identical
        # earlier success is the stored run; an in-flight twin collapses.
        # Both probes repeat inside the mint lock in _dispatch_neb — this
        # is only the fast path that avoids the lock for the common case.
        hit = self.store.struct_neb_cached(cache_key)
        if hit is not None:
            return self._neb_cached_response(hit, is_slug, fs_slug)
        inflight = self.store.struct_neb_inflight(cache_key)
        if inflight is not None:
            return self._neb_inflight_response(inflight, is_slug, fs_slug)

        requester_id = _as_int_or_none(a.get("requested_by", a.get("parent_id")))
        quest_ref_id: int | None = None
        if requester_id is not None:
            # A named requester must be a live todo (fail fast, before the
            # mint) — same contract as _dispatch_relax.
            from precis.todo import guards as todo_guards

            todo_guards.check_parent_exists(self.store, requester_id)
            for link in self.store.links_for(
                requester_id, direction="out", relation="serves"
            ):
                quest_ref_id = int(link.dst_ref_id)
                break
        return self._dispatch_neb(
            is_ref,
            fs_ref,
            is_version=is_version,
            fs_version=fs_version,
            is_run=is_run,
            fs_run=fs_run,
            model=model,
            engine_params=engine_params,
            cache_key=cache_key,
            requester_id=requester_id,
            quest_ref_id=quest_ref_id,
            warns=warns,
            is_slug=is_slug,
            fs_slug=fs_slug,
        )

    def _dispatch_neb(
        self,
        is_ref: Any,
        fs_ref: Any,
        *,
        is_version: int,
        fs_version: int,
        is_run: Any,
        fs_run: Any,
        model: str | None,
        engine_params: dict[str, Any],
        cache_key: str,
        requester_id: int | None,
        quest_ref_id: int | None,
        warns: list[Any],
        is_slug: str,
        fs_slug: str,
    ) -> Response:
        """Mint the ``struct_neb_runs`` row + ``struct_neb`` job for one
        gated edge — the in-process GPU-node rung (spec §Compute).

        The whole mint runs in one transaction under a per-edge advisory
        lock (``struct_neb_mint:<cache_key>`` — deliberately NOT the job's
        own ``struct_neb:<cache_key>`` idem string, which job.put takes on
        a second connection and would self-deadlock): lock → cached/
        inflight re-check → the daily-cap count under its own lock → run
        row → job.put → ``job_ref_id`` backfill. A concurrent identical
        request blocks on the lock, then sees the committed row and
        collapses — no orphan ``queued`` row can wedge the edge. When
        job.put still reports ``reused`` (a live job whose run row this
        request can't see), the fresh row fails with a dedupe note and
        the ack names the run the existing job carries."""
        from precis.handlers.job import _idem_lock_key
        from precis.structure.neb import neb_engine_token
        from precis.workers.job_types import struct_neb
        from precis.workers.job_types.struct_relax import target_node_for

        # In-process ml rung → the compute group, deterministic on the
        # cache key (a re-dispatch lands back on the same host's warm
        # model cache). No node-literal fallback — same rule as relax.
        target = target_node_for("ml", key=cache_key)
        if not target:
            raise Internal(
                "no compute node configured: neither PRECIS_MLIP_NODES, "
                "PRECIS_AUTOCATPATH_ROUTE_NODE nor PRECIS_DFT_NODE is set "
                "on this host, so a struct_neb job would have nowhere to "
                "run — deploy renders them from topology "
                "(precis_capabilities.autocatpath / .dft)",
                next="the node lists are deploy env this host lacks",
            )
        timeout_s = struct_neb._timeout_s()
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        run_params = dict(engine_params)
        if quest_ref_id is not None:
            # The daily cap's attribution — request context, not an
            # engine knob, so it stamps the stored row only and never
            # reaches the cache key or the job's engine params.
            run_params["quest_ref_id"] = str(quest_ref_id)
        engine = neb_engine_token()
        job_resp: Response | None = None
        existing_run_id: int | None = None
        with self.store.tx() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_idem_lock_key(f"struct_neb_mint:{cache_key}"),),
            )
            # The authoritative re-check under the lock — the fast-path
            # probes in _put_neb raced a concurrent mint's commit.
            hit = self.store.struct_neb_cached(cache_key, conn=conn)
            if hit is not None:
                return self._neb_cached_response(hit, is_slug, fs_slug)
            inflight = self.store.struct_neb_inflight(cache_key, conn=conn)
            if inflight is not None:
                return self._neb_inflight_response(inflight, is_slug, fs_slug)
            # Dead rows: a queued/running row whose job went terminal
            # (worker kill, OOM, lease expiry — the row's own status is
            # job-written, so it lies) is failed here so view='neb' never
            # shows a zombie.
            self.store.struct_neb_reap_dead(cache_key, conn=conn)
            if quest_ref_id is not None:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_idem_lock_key(f"struct_neb_quest:{quest_ref_id}"),),
                )
                cap = _neb_quest_daily()
                minted = self.store.struct_neb_quest_mints_24h(quest_ref_id, conn=conn)
                if minted >= cap:
                    raise BadInput(
                        f"quest daily NEB cap reached — {minted} struct_neb "
                        f"mints for this quest in the last 24 h "
                        f"({_NEB_QUEST_DAILY_ENV}={cap})",
                        next="the rolling window reopens "
                        + self._neb_quest_reopens(quest_ref_id)
                        + " — or run without requested_by= (that lane "
                        f"counts against {_NEB_GLOBAL_DAILY_ENV})",
                    )
            else:
                # No quest to charge: the global UTC-day cap meters it —
                # the mint lock is per-edge, so non-quest mints serialize
                # on this fixed key for the count + insert.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_idem_lock_key("struct_neb_quest:global"),),
                )
                cap = _neb_global_daily()
                minted = self.store.struct_neb_nonquest_mints_today(conn=conn)
                if minted >= cap:
                    raise BadInput(
                        f"daily struct_neb cap reached — {minted} mints "
                        f"today without a quest stamp "
                        f"({_NEB_GLOBAL_DAILY_ENV}={cap})",
                        next="stamp requested_by=<a todo serving a quest> — "
                        f"that lane has its own {_NEB_QUEST_DAILY_ENV} cap — "
                        "or wait for the UTC-day rollover",
                    )
            run_id = self.store.struct_neb_record_run(
                is_ref_id=is_ref.id,
                fs_ref_id=fs_ref.id,
                fidelity="ml",
                model=model,
                params=run_params,
                cache_key=cache_key,
                job_ref_id=None,
                is_run_id=is_run["id"] if is_run else None,
                fs_run_id=fs_run["id"] if fs_run else None,
                is_version=is_version,
                fs_version=fs_version,
                engine=engine,
                conn=conn,
            )
            job_resp = hub.sibling("job").put(
                job_type="struct_neb",
                executor="ssh_node",
                # The IS design owns the job (compute lane, like the relax).
                parent_id=is_ref.id,
                params={
                    "neb_run_id": run_id,
                    "is_ref_id": is_ref.id,
                    "fs_ref_id": fs_ref.id,
                    "is_version": is_version,
                    "fs_version": fs_version,
                    "is_run_id": is_run["id"] if is_run else None,
                    "fs_run_id": fs_run["id"] if fs_run else None,
                    "is_slug": is_slug,
                    "fidelity": "ml",
                    "model": model,
                    "params": engine_params,
                    "cache_key": cache_key,
                    "target_node": target,
                    "timeout_s": timeout_s,
                    # ssh_node sizes the lease max(7200, wall + 1 h margin)
                    # off params.resources.wall_seconds (spec §Compute).
                    "resources": {"wall_seconds": int(timeout_s)},
                },
                idem_key=f"struct_neb:{cache_key}",
            )
            if job_resp.reused:
                # A live job already owns this edge while no run row reads
                # in flight (its own row went terminal — e.g. failed while
                # the job was still finishing). Fail the fresh row with a
                # dedupe note and point the ack at the job's run.
                jref = (
                    self.store.get_ref(kind="job", id=int(job_resp.ref_id))
                    if job_resp.ref_id is not None
                    else None
                )
                jparams = ((jref.meta if jref is not None else None) or {}).get(
                    "params"
                ) or {}
                existing_run_id = (
                    int(jparams["neb_run_id"])
                    if jparams.get("neb_run_id") is not None
                    else None
                )
                self.store.struct_neb_update_run(
                    run_id,
                    status="failed",
                    trust=[
                        {
                            "id": "precis-dedupe",
                            "check": "dedupe",
                            "verdict": "not-run",
                            "severity": "info",
                            "seed": 0,
                            "step": None,
                            "evidence": {
                                "reason": "collapsed onto in-flight job "
                                f"jo{job_resp.ref_id}"
                                + (
                                    f" (run {existing_run_id})"
                                    if existing_run_id is not None
                                    else ""
                                ),
                            },
                        }
                    ],
                    conn=conn,
                )
            elif job_resp.ref_id is not None:
                self.store.struct_neb_update_run(
                    run_id, job_ref_id=job_resp.ref_id, conn=conn
                )
        assert job_resp is not None  # put() raised → tx rolled back, exited
        note = ""
        if requester_id is not None:
            if job_resp.ref_id is None:  # defensive — put always reports the id
                log.warning(
                    "structure._dispatch_neb: job put() returned no ref_id "
                    "for requester #%s; skipping requester wiring",
                    requester_id,
                )
            else:
                self._wire_requester(requester_id, job_resp.ref_id)
            note = f" (todo #{requester_id} will block on it)"
        warn_lines = "".join(
            f"# warn {g.gate}: {g.detail}" + (f" — {g.next}" if g.next else "") + "\n"
            for g in warns
        )
        if job_resp.reused:
            existing = (
                self.store.struct_neb_get(existing_run_id)
                if existing_run_id is not None
                else None
            )
            if existing is not None and existing["status"] == "succeeded":
                return self._neb_cached_response(existing, is_slug, fs_slug)
            run_txt = (
                f"run {existing_run_id}" if existing_run_id is not None else "its run"
            )
            status_txt = f" ({existing['status']})" if existing is not None else ""
            return Response(
                body=(
                    f"# struct_neb {run_txt} already owned by jo"
                    f"{job_resp.ref_id}{status_txt} — st{is_ref.id} → "
                    f"st{fs_ref.id} ({is_slug} → {fs_slug}){note}\n\n"
                    "An identical request is owned by that job — this call "
                    "collapsed onto it instead of minting a duplicate.\n\n"
                    f"Poll: get(kind='structure', id={is_slug!r}, view='neb')."
                )
            )
        return Response(
            body=(
                f"queued struct_neb jo{job_resp.ref_id} run {run_id}{note}\n"
                f"st{is_ref.id} → st{fs_ref.id} ({is_slug} → {fs_slug}) · "
                f"ml/{model} · {engine_params['images']} images × "
                f"{engine_params['path_seeds']} seeds · wall cap "
                f"{timeout_s / 3600.0:g} h\n"
                f"{warn_lines}"
                f"\nPoll: get(kind='structure', id={is_slug!r}, view='neb')."
            )
        )

    @staticmethod
    def _neb_inflight_response(
        inflight: Mapping[str, Any], is_slug: str, fs_slug: str
    ) -> Response:
        """The collapse ack for an identical in-flight request — shared
        by the fast path in ``_put_neb`` and the locked re-check in
        ``_dispatch_neb``."""
        return Response(
            body=(
                f"# struct_neb run {inflight['id']} already "
                f"{inflight['status']} — st{inflight['is_ref_id']} → "
                f"st{inflight['fs_ref_id']} ({is_slug} → {fs_slug})\n\n"
                "An identical request is in flight — it collapses onto "
                "that run instead of minting a duplicate.\n\n"
                f"Poll: get(kind='structure', id={is_slug!r}, view='neb')."
            )
        )

    def _neb_quest_reopens(self, quest_ref_id: int) -> str:
        """When the rolling-24h quest cap window reopens — the oldest mint
        still inside it plus 24 h, rendered UTC."""
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT min(created_at) FROM struct_neb_runs "
                "WHERE params->>'quest_ref_id' = %s "
                "AND created_at > now() - interval '24 hours'",
                (str(quest_ref_id),),
            ).fetchone()
        if row is None or row[0] is None:
            return "now"
        from datetime import UTC, timedelta

        reopen = row[0] + timedelta(hours=24)
        if reopen.tzinfo is None:
            reopen = reopen.replace(tzinfo=UTC)
        return reopen.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _neb_scalars(run: Mapping[str, Any]) -> str:
        """The priced line shared by the cache-hit ack and ``view='neb'``:
        E_a fwd/rev, ΔE, trusted — ``not priced`` when the band never
        produced a TS (the NULL-barrier contract)."""
        ea, rev, de = (
            run.get("barrier_fwd_ev"),
            run.get("barrier_rev_ev"),
            run.get("delta_e_ev"),
        )
        parts = [f"E_a fwd {ea:.3f} eV" if ea is not None else "not priced"]
        if rev is not None:
            parts.append(f"rev {rev:.3f} eV")
        if de is not None:
            parts.append(f"ΔE {de:.3f} eV")
        parts.append("trusted" if run.get("trusted") else "untrusted")
        return " · ".join(parts)

    def _neb_cached_response(
        self, hit: Mapping[str, Any], is_slug: str, fs_slug: str
    ) -> Response:
        """The zero-compute ack for an identical re-request: report the
        stored run, mint nothing."""
        return Response(
            body=(
                f"# cached struct_neb run {hit['id']} — st{hit['is_ref_id']} → "
                f"st{hit['fs_ref_id']} ({is_slug} → {fs_slug})\n\n"
                f"{self._neb_scalars(hit)}\n\n"
                "An identical request (same endpoint geometry, params and "
                "code version) already priced this edge — no job minted.\n\n"
                f"Band + trust: get(kind='structure', id={is_slug!r}, "
                f"view='neb', args={{'run': {hit['id']}}})."
            )
        )

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
        if view in ("completion", "completion_digest") and src is not None:
            raise BadInput(
                "completion reads cannot hydrate external sources",
                next="get(kind='structure', id='<slug>', view='completion') "
                "without source=",
            )
        if src is not None:
            query = q if q is not None else a.get("q")
            if id is not None and str(id).strip() and not a.get("config_id"):
                a = {**a, "config_id": str(id).strip()}
            return self._get_external(source=src, q=query, args=a)
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            if view in ("completion", "completion_digest"):
                raise BadInput(
                    "completion requires a specific structure id",
                    next="get(kind='structure') to list designs, then "
                    "get(kind='structure', id='<slug>', view='completion')",
                )
            return self._render_list()
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        if view in ("completion", "completion_digest"):
            from precis.structure.completion_read import (
                STRUCTURE_ARGS,
                read_structure_completion,
            )

            try:
                body = read_structure_completion(
                    self.store, ref.id, a, digest=view == "completion_digest"
                )
            except (ValueError, TypeError) as exc:
                raise BadInput(str(exc), options=sorted(STRUCTURE_ARGS)) from exc
            if body is None:
                raise NotFound(
                    f"structure {id!r} no longer exists",
                    next="get(kind='structure') to list live designs",
                )
            return Response(body=body, ref_id=ref.id)
        if view == "pourbaix":
            # No scene needed — the map reads job meta, not geometry, so it
            # also answers for a structure whose atoms never landed.
            from precis.quest import pourbaix_map

            return Response(
                body=pourbaix_map.render_structure_maps(
                    ref, pourbaix_map.maps_for_structure(self.store, ref)
                )
            )
        if view == "staircase":
            return self._render_staircase(ref, a)
        if view == "generator":
            # meta only — no Scene hydration, so it also answers for a
            # generated design whose atoms never landed (gr514060).
            return self._render_generator(ref)
        scene, handles = self.store.structure_load(ref.id)
        if view is None:
            return self._toc_response(scene, ref, handles)
        if view == "atoms":
            return self._toc_response(scene, ref, handles, inline_atoms=True)
        if view == "runs":
            return self._render_runs(ref)
        if view == "markers":
            return self._render_markers(scene)
        if view in ("ray", "rays"):
            return self._render_rays(view, ref, scene, args or {})
        if view == "literature":
            return self._render_literature(scene, ref)
        if view == "pair":
            return self._render_pair(ref, scene, a)
        if view == "neb":
            return self._render_neb(ref, a)
        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="structure")
        if view in _EXPORT_VIEWS:
            return self._render_export(view, scene, str(ref.slug or id), meta=ref.meta)
        if view in _NAV_VIEWS:
            return self._render_nav(view, scene, args or {})
        if view not in _PROBE_VIEWS:
            raise BadInput(
                f"unknown structure view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the TOC",
            )
        return self._render_probe(view, scene, args or {}, ref=ref)

    def eye(
        self,
        *,
        id: str | int | None = None,
        extent: Any = "fisheye",
        q: str | None = None,
        ring: Any = None,
    ) -> Response:
        """The fisheye ladder — a bare ``st<id>``/slug is the note eye
        (default), ``<ref>~<focus>`` the spatial eye
        (:mod:`precis.handlers._structure_eye`; structure-workspaces 2e).
        ``st<id>#<label>`` is the ``~`` form's alias
        (precis-structure-help's atom addressing)."""
        from precis.handlers._eye import EYE_NEXT
        from precis.handlers._structure_eye import recall_refusal, render
        from precis.utils.eye_render import parse_extent

        try:
            ext, recall = parse_extent(extent)
        except ValueError as exc:
            raise BadInput(str(exc), next=EYE_NEXT) from exc
        s = str(id or "").strip()
        if s.startswith("structure:"):
            s = s[len("structure:") :]
        focus = ""
        base = s
        if "~" in s:
            base, _sep, focus = s.partition("~")
        elif "#" in s and handle_registry.parse(s.partition("#")[0]):
            # st<id>#<focus> — the precis-structure-help atom alias.
            base, _sep, focus = s.partition("#")
        ref = resolve_live_slug_ref(self.store, kind="structure", id=base)
        if not focus:
            # Bare ref (or an empty trailing separator like `st7#`).
            return super().eye(id=base, extent=extent, q=q, ring=ring)
        if focus.lstrip("-").isdigit():
            # A digit ~ suffix is the chunk selector (st<id>~<ord>),
            # not an atom — the base eye owns it and needs the suffix.
            return super().eye(id=s, extent=extent, q=q, ring=ring)
        if recall:
            raise recall_refusal()
        scene, _handles = self.store.structure_load(ref.id)
        if focus.startswith("ray:"):
            return Response(body=self._ray_eye(ref, scene, focus[4:].strip(), ext))
        return Response(body=render(self.store, ref, scene, focus, ext, q=q, ring=ring))

    def _ray_eye(self, ref: Any, scene: Scene, name: str, ext: Any) -> str:
        """``st<id>~ray:<name>``: ``kwd`` the header, ``summary`` + per-field
        counts, ``verbatim`` and up the hits (a bare structure has no
        blocks or pockets to cross; its π systems are on the hits)."""
        from precis.workers.working_set import Extent

        lines = self._render_rays("ray", ref, scene, {"name": name}).body.splitlines()
        if ext == Extent.TOC:
            return lines[0]
        counts: dict[str, int] = {}
        for ln in lines[1:]:
            parts = ln.split()
            if len(parts) > 2 and parts[1] == "Å":
                f = parts[2].split(".")[0]
                counts[f] = counts.get(f, 0) + 1
        summary = [
            lines[0],
            "hits: " + " · ".join(f"{k} {v}" for k, v in counts.items()),
        ]
        if ext == Extent.SUMMARY:
            return "\n".join(summary)
        return "\n".join([*summary, *lines[1:]])

    def _render_staircase(self, ref: Any, args: dict[str, Any]) -> Response:
        """``view='staircase'`` — the reaction staircase of this
        structure's best linked pathway (SPSO slice 3), re-tilted at
        ``args['U']`` (V vs RHE; default the serving quest's operating
        point, else the pathway's recorded one, else 0).

        The render itself is the pathway handler's — the affine adapter
        lives in the plugin and core may not import it
        (``tests/test_plugin_import_boundary.py``), so this resolves the
        best pathway in core (``precis.quest.staircase``) and delegates
        through :meth:`Hub.sibling`. A structure without a ready linked
        pathway gets the honest no-pathway answer; a missing plugin
        degrades to the same."""
        u_raw = args.get("U")
        u: float | None = None
        if u_raw is not None:
            if (
                isinstance(u_raw, bool)
                or not isinstance(u_raw, int | float)
                or not math.isfinite(u_raw)
            ):
                raise BadInput(
                    f"args={{'U': {u_raw!r}}}: U must be a finite number (V vs RHE)",
                    next="get(kind='structure', id='<slug>', view='staircase', "
                    "args={'U': 0.9})",
                )
            u = float(u_raw)
        from precis.quest import staircase as sq

        pw = sq.best_pathway(self.store, int(ref.id))
        handle = (
            handle_registry.try_format("structure", int(ref.id))
            or f"structure:{ref.id}"
        )
        head = (ref.title or "").splitlines()[0] if ref.title else handle
        if pw is None:
            return Response(
                body=(
                    f"# reaction staircase — {handle} {head}\n\n"
                    "no staircase — no ready pathway is linked to this "
                    "structure (the harvest wires candidates to their "
                    "evaluating pathway with a `related-to` link)."
                )
            )
        # the picked point (gr489471) rides through to the pathway render,
        # which validates it; point= carries its own U
        fwd: dict[str, Any] = {k: args[k] for k in ("pH", "point") if k in args}
        if u is None and "point" not in fwd:
            u = sq.quest_operating_u(self.store, int(ref.id))
        pw_handle = (
            handle_registry.try_format("pathway", int(pw.id)) or f"pathway:{pw.id}"
        )
        try:
            pw_handler = self.hub.sibling("pathway")
        except (KeyError, ImportError, InitError) as exc:
            # plugin absent (no entry point / its import fails) or its
            # init guard tripped
            log.debug("structure %s: pathway sibling unavailable (%s)", ref.id, exc)
            body = (
                f"(staircase render unavailable — the pathway kind is not "
                f"installed here; best linked pathway is {pw_handle})"
            )
        else:
            # the pathway kind is slug-keyed — a bare ref_id would hit its
            # cite_key lookup, never the ref table. Omit U entirely when no
            # quest/point is recorded — the pathway side then falls back to
            # its own operating point (args={'U': None} is a caller bug it
            # rejects). A failure inside the render is the render's, not a
            # missing plugin: it propagates.
            body = pw_handler.get(
                id=str(pw.slug or pw.id),
                view="staircase",
                args={**({"U": u} if u is not None else {}), **fwd},
            ).body
        return Response(
            body=(
                f"# reaction staircase — {handle} {head}\n"
                f"best linked pathway: {pw_handle} "
                f"({(pw.title or '').splitlines()[0] if pw.title else '?'})\n\n"
                f"{body}"
            )
        )

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
            raise BadInput(
                str(exc),
                next="get(kind='structure', args={'source': 'catalysis-hub'|"
                "'qmof'|'core-mof', 'config_id': '<id>'}) or get(kind='structure', "
                "id='mp-<n>', args={'source': 'materials-project'})",
            ) from exc

        config_id = args.get("config_id")
        if config_id:
            existing_ref_id = self.store.find_ref_by_identifier(
                source, str(config_id), kind="structure"
            )
            if existing_ref_id is not None:
                return self._render_hydrated(
                    [(existing_ref_id, source, str(config_id), None)], cached=True
                )

        if source in mof.CIF_ADAPTERS:
            return self._get_bundle_record(source, config_id, adapter_fn)
        if source == "materials-project":
            return self._get_materials_project(adapter_fn, config_id)
        if source != "catalysis-hub":
            raise BadInput(
                f"on-demand hydrate has no fetch layer wired for source={source!r} yet",
                next="known sources: catalysis-hub, qmof, core-mof, materials-project",
            )
        filters = _resolve_catalysis_hub_filters(q, args)
        try:
            raw_records = catalysis_hub.fetch_config(**filters)
        except catalysis_hub.CatalysisHubUnsupported as exc:
            raise Unsupported(
                str(exc), next="pip install --force-reinstall 'precis-mcp'"
            ) from exc
        except catalysis_hub.CatalysisHubAuthRequired as exc:
            raise Unsupported(
                str(exc),
                next=(
                    f"the {catalysis_hub.CATHUB_KEY_SECRET} secret is rejected "
                    "— replace it on the /secrets page"
                    if exc.keyed
                    else f"set the {catalysis_hub.CATHUB_KEY_SECRET} secret "
                    "(SUNCAT-issued key) on the /secrets page, then retry"
                ),
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

    def _get_bundle_record(
        self, source: str, config_id: Any, adapter_fn: Any
    ) -> Response:
        """Import one QMOF / CoRE MOF record by id (the by-id cache hit has
        already been checked). The NAS bundle index comes first: it holds
        the CIF and, for QMOF, the PBE energy. With no index, QMOF falls
        back to MPContribs when the Materials Project key is in the vault
        (geometry only: MPContribs has no QMOF total energy); otherwise the
        refusal names the index-build command."""
        from precis.config import struct_bundle_index_dir

        if not config_id:
            raise BadInput(
                f"a {source} lookup needs config_id= (the record id); "
                "there is no filter search over a bundle",
                next=f"get(kind='structure', args={{'source': {source!r}, "
                "'config_id': '<id>'})",
            )
        record_id = str(config_id).strip()
        index_dir = struct_bundle_index_dir()
        note: str | None = None
        try:
            raw = bundle_index.load_record(source, record_id, index_dir)
        except bundle_index.BundleIndexError as exc:
            if not bundle_index.index_path(source, index_dir).is_file():
                raw = (
                    self._qmof_from_mpcontribs(record_id) if source == "qmof" else None
                )
                if raw is None:
                    raise Unsupported(str(exc), next=exc.next) from exc
                note = (
                    "no energy: fetched from MPContribs, which has no QMOF total "
                    "energy; index the figshare bundle for PBE energies"
                )
            else:
                raise Unsupported(str(exc), next=exc.next) from exc
        except CifRefused as exc:
            raise BadInput(str(exc), next=exc.next) from exc
        if raw is None:
            raise NotFound(
                f"no {source} record {record_id!r} in the bundle index",
                next="check the id (QMOF ids look like 'qmof-1a2b3c4'; CoRE MOF "
                "ids are the CIF file stem, e.g. 'ABAVIJ_clean')",
            )
        try:
            scene, run, ext_id = adapter_fn(raw)
        except CifRefused as exc:
            raise BadInput(str(exc), next=exc.next) from exc
        except CifUnsupported as exc:
            raise Unsupported(
                str(exc), next="install it:  pip install 'precis-mcp[dft]'"
            ) from exc
        if note is None and run.energy is None:
            note = "no energy (geometry only)"
        ref_id = self.store.structure_import(scene, run, ext_id)
        return self._render_hydrated(
            [(ref_id, ext_id.dataset, ext_id.config_id, run.method)],
            cached=False,
            note=note,
        )

    def _qmof_from_mpcontribs(self, record_id: str) -> dict[str, Any] | None:
        """One QMOF raw record from MPContribs, or ``None`` when the
        Materials Project key is not in the vault (never required)."""
        from precis import secrets
        from precis.workers.job_types.pourbaix_bulk import MP_API_KEY_SECRET

        api_key = (secrets.get_secret(MP_API_KEY_SECRET) or "").strip()
        if not api_key:
            return None
        import httpx

        from precis.utils.http import VendorError

        try:
            raw = mof.fetch_qmof_mpcontribs(record_id, api_key)
        except (httpx.HTTPError, VendorError, ValueError) as exc:
            raise Upstream(
                f"MPContribs qmof lookup for {record_id!r} failed: {exc}",
                next="retry in a minute, or index the QMOF bundle with "
                "`precis structure index-bundle qmof <bundle path>`",
            ) from exc
        if raw is None:
            raise NotFound(
                f"MPContribs has no qmof record {record_id!r}",
                next="check the id (QMOF ids look like 'qmof-1a2b3c4')",
            )
        return raw

    def _get_materials_project(self, adapter_fn: Any, config_id: object) -> Response:
        """Hydrate one Materials Project material by ``mp-<n>`` (cache hit
        already short-circuited by the caller)."""
        material_id = str(config_id or "").strip()
        if not materials_project.valid_material_id(material_id):
            raise BadInput(
                f"materials-project needs an mp-<digits> id, got {material_id!r}",
                next="get(kind='structure', id='mp-149', "
                "args={'source': 'materials-project'})",
            )
        try:
            raw = materials_project.fetch_record(material_id)
        except materials_project.MaterialsProjectKeyMissing as exc:
            raise Unsupported(
                str(exc),
                next=f"set the {materials_project.MP_KEY_SECRET} secret "
                "(free key from the Materials Project API page) on the "
                "/secrets page, then retry",
            ) from exc
        except materials_project.MaterialsProjectAuthRejected as exc:
            raise Unsupported(
                str(exc),
                next=f"replace the {materials_project.MP_KEY_SECRET} secret "
                "on the /secrets page",
            ) from exc
        if raw is None:
            return Response(
                body=f"materials-project has no material {material_id!r}\n\n"
                "Next: check the id on the Materials Project site."
            )
        try:
            scene, run, ext_id = adapter_fn(raw)
        except ValueError as exc:
            raise BadInput(
                str(exc),
                next="pick another material (ordered structure with a "
                "formation energy)",
            ) from exc
        ref_id = self.store.structure_import(scene, run, ext_id)
        return self._render_hydrated(
            [(ref_id, ext_id.dataset, ext_id.config_id, run.method)], cached=False
        )

    def _render_hydrated(
        self,
        rows: list[tuple[int, str, str, dict[str, Any] | None]],
        *,
        cached: bool,
        note: str | None = None,
    ) -> Response:
        """Render on-demand-hydrate result(s) — a full TOC + citation footer
        for a single config (the common case: a by-id hit, or a filtered
        fetch that resolved to exactly one record), else a summary table.
        ``note`` (one record only) rides on the footer, e.g. that the
        record carries no energy."""
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
            if note:
                footer += f" · {note}"
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

    def _render_export(
        self, view: str, scene: Scene, slug: str, *, meta: dict[str, Any] | None = None
    ) -> Response:
        """Emit the geometry as a file format. POSCAR/extXYZ/PDB are pure; CIF
        needs ASE (the optional ``[dft]`` extra) — a missing one is Unsupported
        with an install hint, not a crash. PDB names residues from the ref's
        ``meta['chain_atoms']`` when a ``realize_chain`` mint wrote one."""
        if view == "poscar":
            return Response(body=export.to_poscar(scene))
        if view == "extxyz":
            return Response(body=export.to_extxyz(scene))
        if view == "pdb":
            return Response(
                body=export.to_pdb(scene, chain_atoms=(meta or {}).get("chain_atoms"))
            )
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
        """The design's compute history — the fidelity levels over time (§9),
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

    def _render_pair(self, ref: Any, scene: Scene, args: dict[str, Any]) -> Response:
        """``view='pair'`` — the IS/FS NEB-readiness gate table (gr512126,
        slice 2). Read-only and zero compute: every gate of the spec's
        Validation table renders one row — measured input, threshold,
        pass/fail/warn/n/a, and a working ``next=`` on each failed row —
        so an agent learns why a pair is not NEB-ready before any NEB
        exists. The partner goes in ``args['other']`` (``fs``/``vs`` also
        accepted), matching ``view='diff'``."""
        from precis.structure import pair_check

        other = str(args.get("other") or args.get("fs") or args.get("vs") or "").strip()
        if not other:
            raise BadInput(
                "pair needs other= (the final-state design slug)",
                next="get(kind='structure', id='<is-slug>', view='pair', "
                "args={'other': '<fs-slug>'})",
            )
        oref = resolve_live_slug_ref(self.store, kind="structure", id=other)
        oscene, _ = self.store.structure_load(oref.id)
        is_slug = str(ref.slug or ref.id)
        fs_slug = str(oref.slug or other)
        verdict = pair_check.evaluate_pair(
            scene,
            oscene,
            is_version=int((ref.meta or {}).get("version", 0)),
            fs_version=int((oref.meta or {}).get("version", 0)),
            is_runs=self.store.structure_runs(ref.id),
            fs_runs=self.store.structure_runs(oref.id),
            is_slug=is_slug,
            fs_slug=fs_slug,
        )
        n_fail = len(verdict.failed)
        n_warn = sum(1 for g in verdict.gates if g.verdict == pair_check.WARN)
        state = (
            "NEB-ready"
            if verdict.ok and not n_warn
            else "NEB-ready (with warnings)"
            if verdict.ok
            else f"blocked — {n_fail} gate(s) failing"
        )
        rows = [
            {
                "gate": g.gate,
                "input": g.detail,
                "threshold": g.threshold,
                "verdict": g.verdict,
                "next": g.next or "—",
            }
            for g in verdict.gates
        ]
        return Response(
            body=f"# pair {is_slug} ↔ {fs_slug} · {state}\n"
            + render_agent_table(
                rows, schema=["gate", "input", "threshold", "verdict", "next"]
            )
        )

    def _render_neb(self, ref: Any, args: dict[str, Any]) -> Response:
        """``view='neb'`` — the NEB edge runs touching this design as IS,
        FS or minted TS (``struct_neb_runs``; gr512126, slice 3).
        ``args={'run': N}`` opens one run: the priced scalars, the trust
        table and the band — image · E−E_IS · max |F| · the distances of
        the bonds that change IS→FS."""
        run_arg = args.get("run")
        if run_arg is not None:
            return self._render_neb_run(ref, run_arg)
        runs = self.store.struct_neb_runs_for(ref.id)
        if not runs:
            return Response(
                body=(
                    f"# {ref.slug}: no NEB runs\n\n"
                    "Next: gate the pair first — "
                    f"get(kind='structure', id={ref.slug!r}, view='pair', "
                    "args={'other': '<fs-slug>'}), then "
                    "put(kind='structure', id='<is-slug>', mode='neb', "
                    "args={'final': '<fs-slug>'})."
                )
            )
        ids = {
            int(v)
            for r in runs
            for v in (r["is_ref_id"], r["fs_ref_id"], r["ts_ref_id"])
            if v is not None
        }
        names = {
            rid: (str(rl.slug) if rl.slug else f"st{rid}")
            for rid, rl in self.store.fetch_refs_by_ids(ids).items()
        }
        from precis.structure.neb import neb_engine_token

        current_engine = neb_engine_token()
        rows = []
        for r in runs:
            role = (
                "IS"
                if r["is_ref_id"] == ref.id
                else "FS"
                if r["fs_ref_id"] == ref.id
                else "TS"
            )
            eng = r.get("engine")
            rows.append(
                {
                    "run": r["id"],
                    "edge": f"{names.get(int(r['is_ref_id']), '?')}→"
                    f"{names.get(int(r['fs_ref_id']), '?')}",
                    "role": role,
                    "status": r["status"],
                    "E_a fwd": "—"
                    if r["barrier_fwd_ev"] is None
                    else f"{r['barrier_fwd_ev']:.3f}",
                    "ΔE": "—" if r["delta_e_ev"] is None else f"{r['delta_e_ev']:.3f}",
                    "trusted": "yes" if r["trusted"] else "no",
                    "model": r["model"] or "—",
                    "engine": (
                        "—"
                        if not eng
                        else eng + (" (stale)" if eng != current_engine else "")
                    ),
                    "job": f"jo{r['job_ref_id']}" if r["job_ref_id"] else "—",
                }
            )
        return Response(
            body=f"# {ref.slug}: {len(runs)} NEB run(s)\n"
            + render_agent_table(
                rows,
                schema=[
                    "run",
                    "edge",
                    "role",
                    "status",
                    "E_a fwd",
                    "ΔE",
                    "trusted",
                    "model",
                    "engine",
                    "job",
                ],
            )
            + "\n\nBand + trust: get(kind='structure', id="
            + repr(ref.slug)
            + ", view='neb', args={'run': N})."
        )

    def _render_neb_run(self, ref: Any, run_arg: Any) -> Response:
        """``view='neb', args={'run': N}`` — one run's scalars, trust table
        and band. The run must touch this design (as IS, FS or TS)."""
        try:
            run_id = int(run_arg)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"args 'run' must be a run id, got {run_arg!r}",
                next=f"get(kind='structure', id={ref.slug!r}, view='neb') "
                "for the run list",
            ) from exc
        run = self.store.struct_neb_get(run_id)
        if run is None or ref.id not in {
            run["is_ref_id"],
            run["fs_ref_id"],
            run["ts_ref_id"],
        }:
            raise BadInput(
                f"neb run {run_id} doesn't touch {ref.slug}",
                next=f"get(kind='structure', id={ref.slug!r}, view='neb') "
                "for the run list",
            )
        from precis.structure.neb import neb_engine_token

        eng = run.get("engine")
        stale = bool(eng) and eng != neb_engine_token()
        lines = [
            f"# neb run {run_id} — st{run['is_ref_id']} → "
            f"st{run['fs_ref_id']} · {run['status']}",
            self._neb_scalars(run),
            f"{run['fidelity']}/{run['model']} · converged "
            f"{'yes' if run['converged'] else 'no'}"
            + (f" · wall {run['wall_s']:.0f} s" if run.get("wall_s") else "")
            + (f" · job jo{run['job_ref_id']}" if run.get("job_ref_id") else ""),
        ]
        if eng:
            lines.append(
                f"engine {eng}" + (" — stale, rerun on current" if stale else "")
            )
        if run.get("ts_ref_id"):
            lines.append(
                f"TS → st{run['ts_ref_id']} (band image {run['ts_index']}, "
                f"fmax {run['ts_fmax']})"
            )
        if run["status"] == "failed":
            reason, nxt = self._neb_failure(run)
            lines += [f"failed: {reason}", f"next: {nxt}"]
        lines.append("")
        lines.append(self._neb_trust_table(recs=list(run.get("trust") or []), run=run))
        images = self.store.struct_neb_images(run_id)
        if images:
            lines.append("")
            lines.append(self._neb_band_table(run, images))
        return Response(body="\n".join(lines))

    @staticmethod
    def _neb_failure(run: Mapping[str, Any]) -> tuple[str, str]:
        """Why a failed run failed, and the working ``next=`` (spec: a
        failed run shows its reason and a next — denser band, more seeds,
        or fix the endpoint)."""
        recs = [r for r in (run.get("trust") or []) if r.get("verdict") == "fail"]
        if recs:
            ev = recs[0].get("evidence") or {}
            reason = str(ev.get("reason") or f"{recs[0].get('check')} failed")
        else:
            reason = f"failed before pricing — see job jo{run.get('job_ref_id')}"
        return reason, (
            "retry with a denser band (more 'images') or more seeds "
            "('path_seeds'), or fix the endpoint geometry — "
            "put(kind='structure', id='<is-slug>', mode='neb', "
            "args={'final': '<fs-slug>', ...})"
        )

    def _neb_trust_table(
        self, *, recs: list[dict[str, Any]], run: Mapping[str, Any]
    ) -> str:
        """The run-trust table: one row per stored record, ``not-run``
        rows for required checks the engine never graded (a check that
        emitted no records is not a passing one — trust_gate's rule),
        plus the slice-5 ``frequency`` slot — ``not checked`` until the
        saddle frequency pass exists. A record stamped to a different
        engine than the run's own renders ``stale, rerun on current``
        (the same-version-seeds ruling, gr517804)."""
        from precis.utils.trust_gate import is_stale, record_engine
        from precis.workers.job_types.struct_neb import _NEB_REQUIRED_CHECKS

        engines = [e for e in (record_engine(r) for r in recs) if e]
        current = max(set(engines), key=engines.count) if engines else None
        rows = []
        for r in recs:
            ev = r.get("evidence") or {}
            note = (
                "stale, rerun on current"
                if is_stale(r, current)
                else str(ev.get("reason") or "")
            )
            rows.append(
                {
                    "check": r.get("check"),
                    "verdict": r.get("verdict"),
                    "severity": r.get("severity"),
                    "engine": record_engine(r) or "—",
                    "note": note,
                }
            )
        seen = {r.get("check") for r in recs}
        for check in _NEB_REQUIRED_CHECKS:
            if check not in seen:
                rows.append(
                    {
                        "check": check,
                        "verdict": "not-run",
                        "severity": "—",
                        "engine": "—",
                        "note": "check emitted no records",
                    }
                )
        n_imag = run.get("n_imag")
        rows.append(
            {
                "check": "frequency",
                "verdict": "not checked" if n_imag is None else "checked",
                "severity": "—",
                "engine": "—",
                "note": ""
                if n_imag is None
                else f"n_imag={n_imag}"
                + (
                    f", {run['imag_cm1']} cm⁻¹"
                    if run.get("imag_cm1") is not None
                    else ""
                ),
            }
        )
        verdict = "trusted" if run.get("trusted") else "untrusted"
        return f"trust: {verdict}\n" + render_agent_table(
            rows, schema=["check", "verdict", "severity", "engine", "note"]
        )

    def _neb_band_table(
        self, run: Mapping[str, Any], images: list[dict[str, Any]]
    ) -> str:
        """The band: per image E−E_IS (eV), max |F| (eV/Å) and the
        distances (Å) of the bonds that change IS→FS — the changed-bond
        set is the stored graphs' symmetric difference, read in the band's
        slab-first atom order (``neb.slab_first_order`` on the IS — the
        same order the job built the images in). Endpoints load at the
        versions the run was minted against (``is_version``/``fs_version``
        on the row) — a later edit must not relabel the band's atoms."""
        from precis.structure.neb import slab_first_order

        is_v = run.get("is_version")
        fs_v = run.get("fs_version")
        is_scene, _ = self.store.structure_load(
            run["is_ref_id"], version=int(is_v) if is_v is not None else None
        )
        fs_scene, _ = self.store.structure_load(
            run["fs_ref_id"], version=int(fs_v) if fs_v is not None else None
        )
        order, _n_slab = slab_first_order(is_scene)
        label_idx = {la: i for i, la in enumerate(order)}
        is_bonds = {frozenset((b.i, b.j)) for b in is_scene.bonds}
        fs_bonds = {frozenset((b.i, b.j)) for b in fs_scene.bonds}
        changed = sorted(
            (tuple(sorted(p)) for p in (is_bonds ^ fs_bonds)),
        )
        e0 = images[0].get("energy_ev")
        rows = []
        for img in images:
            e = img.get("energy_ev")
            row: dict[str, Any] = {
                "idx": img["idx"],
                "E−E_IS": "—" if e is None or e0 is None else f"{e - e0:+.3f}",
                "max|F|": "—"
                if img.get("max_force") is None
                else f"{img['max_force']:.3f}",
            }
            fracs = (img.get("positions") or {}).get("frac") or []
            full = len(fracs) == len(order)
            for la, lb in changed:
                col = f"{la}–{lb}"
                if full and la in label_idx and lb in label_idx:
                    d, _img_off = is_scene.cell.mic(
                        np.asarray(fracs[label_idx[la]], dtype=float),
                        np.asarray(fracs[label_idx[lb]], dtype=float),
                    )
                    row[col] = f"{d:.2f}"
                else:
                    row[col] = "—"
            rows.append(row)
        schema = ["idx", "E−E_IS", "max|F|"] + [f"{la}–{lb}" for la, lb in changed]
        mark = (
            f" · TS at image {run['ts_index']}"
            if run.get("ts_index") is not None
            else ""
        )
        return f"band (E−E_IS eV, max|F| eV/Å, bond Å){mark}:\n" + render_agent_table(
            rows, schema=schema
        )

    def _render_generator(self, ref: Any) -> Response:
        """``view='generator'`` — the stored generator record
        (``refs.meta['generated']``, written by the ``generate`` op's
        :func:`precis_se.atomic.generate.generated_record` or the
        workspace importer): which generator built the design plus its
        authored params/``spec``, so an agent can compare a stored build
        against a local one (gr514060 — the local ``out/`` diff itself is
        deferred). Reads meta only; a design authored by hand, or minted
        before the record existed (2026-09-27), refuses with a next=."""
        rec = (getattr(ref, "meta", None) or {}).get("generated")
        slug = str(ref.slug or ref.id)
        if not isinstance(rec, Mapping) or not rec.get("generator"):
            raise NotFound(
                f"structure {slug!r} carries no generator record (authored "
                "directly, or minted before the record existed)",
                next=f"get(kind='structure', id={slug!r}) for the TOC",
            )
        lines = [f"# {slug}: generator {rec['generator']}"]
        fact_keys = (
            "hexfold",
            "seed_kind",
            "n_atoms",
            "n_bonds",
            "chiral_index",
            "radius_A",
        )
        facts = [f"{k}={rec[k]}" for k in fact_keys if k in rec]
        rings = rec.get("rings")
        if isinstance(rings, Mapping) and rings:
            census = ", ".join(
                f"{k}:{v}" for k, v in sorted(rings.items(), key=lambda kv: int(kv[0]))
            )
            facts.append(f"rings={{{census}}}")
        if facts:
            lines.append("  ".join(facts))
        elided = sorted(k for k in rec if k in _GENERATED_VIEW_ELIDED)
        shown = {*fact_keys, "generator", "spec", "rings"}
        for k in sorted(set(rec) - shown - _GENERATED_VIEW_ELIDED):
            lines.append(f"{k}: {json.dumps(rec[k], sort_keys=True, default=str)}")
        spec = rec.get("spec")
        if isinstance(spec, str) and spec.strip():
            lines.extend(
                ["", "spec (regeneration input):", "```", spec.rstrip("\n"), "```"]
            )
        if elided:
            lines.append(f"(elided verbose record keys: {', '.join(elided)})")
        return Response(body="\n".join(lines))

    def _render_rays(
        self, view: str, ref: Any, scene: Scene, args: dict[str, Any]
    ) -> Response:
        """``view='ray'`` (``args={'name', 'from'?}``) / ``view='rays'`` —
        the design's observation rays (structure-workspaces 2r), events
        recomputed from the live geometry on every read."""
        from precis.handlers._structure_eye import _pi_data
        from precis.structure.rayview import render_structure_ray, structure_ray

        handle = handle_registry.try_format("structure", int(ref.id)) or str(ref.slug)
        rays = [m for m in scene.measures if m.kind == "ray"]
        if view == "rays":
            if not rays:
                return Response(
                    body="# no rays yet\n\nNext: edit(kind='structure', id=…, "
                    "ops=[{'op':'ray','name':'axis','origin':[0,0,-5],"
                    "'direction':[0,0,1],'length':20}])"
                )
            pi = _pi_data(self.store, int(ref.id), scene)
            lines = [f"# {len(rays)} ray(s)"]
            for m in rays:
                tr = structure_ray(scene, m, pi)
                lines.append(
                    f"  {m.name} · {float((m.spec or {}).get('length') or 0):g} Å · "
                    f"{len(tr.hits)} hits · {handle}~ray:{m.name}"
                )
            return Response(body="\n".join(lines))
        name = str(args.get("name") or "").strip()
        hit = next((x for x in rays if x.name == name), None)
        if hit is None:
            raise NotFound(
                f"no ray {name!r} on {handle} (rays: "
                f"{', '.join(str(x.name) for x in rays) or 'none'})",
                next=f"get(kind='structure', id={handle!r}, view='rays')",
            )
        start = args.get("from")
        lines = render_structure_ray(
            scene,
            hit,
            _pi_data(self.store, int(ref.id), scene),
            handle=handle,
            start=float(start) if start is not None else None,
        )
        return Response(body="\n".join(lines))

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
            elif m.kind == "ray":
                shown = f"{value.get('hits')} hits · view='ray'"
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
            hub=getattr(self, "hub", None),
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
            raise BadInput(
                "delete(kind='structure') requires id= (the design slug)",
                next="get(kind='structure') to list designs",
            )
        ref = resolve_live_slug_ref(self.store, kind="structure", id=str(id).strip())
        n = self.store.structure_delete(ref.id)
        return Response(body=f"retired structure {ref.slug} ({n} atom(s))")

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _with_echo(resp: Response, echo: str | None) -> Response:
        """Append an ``import_fragment`` label-mapping echo (if any) to an
        already-built response body — used by both the ordinary TOC-response
        path and the relax-dispatch path in ``put``/``edit``."""
        if not echo:
            return resp
        # dataclasses.replace, not a hand-rebuilt Response — a re-listed field
        # set silently drops any field added to Response later.
        return replace(resp, body=resp.body + "\n\n" + echo)

    def _default_import_offset(self, scene: Scene) -> np.ndarray:
        """Default ``import_fragment`` offset (Cartesian, Å): past the
        scene's current max x-coordinate + 5 Å, so an imported fragment
        never lands overlapping the existing atoms. The origin, for an
        empty scene."""
        if not scene.atoms:
            return np.zeros(3)
        max_x = max(
            float(scene.cell.frac_to_cart(a.frac)[0]) for a in scene.atoms.values()
        )
        return np.array([max_x + 5.0, 0.0, 0.0])

    def _import_fragment(self, scene: Scene, op: dict[str, Any]) -> str:
        """Handler-level expansion of ``import_fragment`` — ``ops.py`` has no
        store access, so this hydration + mint lives here, intercepted
        before ``apply_ops`` ever sees the op (:func:`_apply_ops_with_imports`).

        ``{"op": "import_fragment", "design": "<slug>", "offset": [x,y,z]
        (Cartesian, optional — default :meth:`_default_import_offset`)}``.
        Loads the source design via the same load path this handler uses
        for the target, mints one fresh atom per source atom (element,
        cart = source cart + offset, carrying hybridization/magmom/
        oxidation/charge) with freshly minted TARGET-scene labels, then one bond
        per source DECLARED bond (mapped labels, order/kind carried,
        periodic images dropped — the import is geometric only). Measures
        do not import. Importing a design into itself is fine (duplicates
        the fragment with fresh labels) — but note the source is hydrated
        from the store, i.e. the design's last-SAVED state: a self-import
        sharing one edit() call with earlier ops sees the pre-edit
        fragment, not those ops' in-memory mutations. Returns a compact
        echo line naming the label mapping, for the caller's response."""
        design = op.get("design")
        if not design:
            raise BadInput(
                "import_fragment needs 'design' (the source slug)",
                next="ops=[{'op': 'import_fragment', 'design': '<source-slug>'}]",
            )
        src_ref = self.store.get_ref(kind="structure", id=str(design).strip())
        if src_ref is None:
            roster = ", ".join(
                r.slug
                for r in self.store.list_refs(
                    kind="structure", order_by="id_desc", limit=8
                )
                if r.slug
            )
            raise NotFound(
                f"no structure design {design!r}",
                next=(
                    f"known designs: {roster}"
                    if roster
                    else "put(kind='structure', id=..., ...) to create one first"
                ),
            )
        src_scene, _ = self.store.structure_load(src_ref.id)
        offset = _vec(op, "offset")
        if offset is None:
            offset = self._default_import_offset(scene)
        label_map: dict[str, str] = {}
        for label, atom in src_scene.atoms.items():
            new_cart = src_scene.cell.frac_to_cart(atom.frac) + offset
            new_label = scene.next_label(atom.element)
            frac = scene.cell.wrap(scene.cell.cart_to_frac(new_cart))
            scene.atoms[new_label] = Atom(
                label=new_label,
                element=atom.element,
                frac=frac,
                magmom=atom.magmom,
                oxidation=atom.oxidation,
                charge=atom.charge,
                hybridization=atom.hybridization,
            )
            label_map[label] = new_label
        for bond in src_scene.bonds:
            if bond.provenance != "declared":
                continue
            if bond.i not in label_map or bond.j not in label_map:
                continue
            scene.bonds.append(
                Bond(
                    i=label_map[bond.i],
                    j=label_map[bond.j],
                    order=bond.order,
                    kind=bond.kind,
                    provenance="declared",
                )
            )
        mapping = ", ".join(f"{old}→{new}" for old, new in label_map.items())
        return f"imported {len(label_map)} atom(s) from {src_ref.slug}: {mapping}"

    def _apply_ops_with_imports(
        self, scene: Scene, ops: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk ``ops`` in order, applying every non-``relax`` op to
        ``scene`` immediately — ``import_fragment`` (store-aware; ``ops.py``
        never sees it) is hydrated + expanded here into real ``add_atom``/
        ``add_bond`` mutations (:meth:`_import_fragment`), and every other
        graph op runs through the ordinary :func:`apply_ops`, one op at a
        time, so an ``import_fragment`` sharing a call with an earlier
        label-minting op sees exactly what that op already placed (the same
        ordering ``apply_ops`` would give a plain op list). Returns the
        (untouched) list of any ``relax`` op(s) for the caller's
        ``_run_ops`` to process as usual, plus an echo string naming every
        import's label mapping (``None`` when there were none)."""
        relax_ops: list[dict[str, Any]] = []
        echoes: list[str] = []
        for op in ops:
            if not isinstance(op, dict) or "op" not in op:
                raise BadInput(
                    f"op missing 'op' key: {op!r}",
                    next="ops=[{'op': 'add_atom', 'element': 'O', "
                    "'frac': [0.5, 0.5, 0.6]}]",
                )
            name = op["op"]
            if name == "relax":
                relax_ops.append(op)
                continue
            if name != "import_fragment":
                try:
                    apply_ops(scene, [op])
                except OpError as exc:
                    raise BadInput(
                        f"op error: {exc}",
                        next="get(kind='skill', id='precis-structure-help') "
                        "for the op vocabulary",
                        options=exc.options,
                    ) from exc
                continue
            echoes.append(self._import_fragment(scene, op))
        return relax_ops, ("\n".join(echoes) if echoes else None)

    def _run_ops(
        self, scene: Scene, ops: list[dict[str, Any]]
    ) -> RelaxResult | _NeedsDispatch | None:
        """Run the relax tail of an op list. Callers apply graph ops via
        :meth:`_apply_ops_with_imports` first and pass only the relax op(s)
        here (the graph-op filter below is defensive, normally a no-op).
        Returns the :class:`RelaxResult` (or None), or a
        :class:`_NeedsDispatch` when an energy rung missed the cache and
        must not compute here — no local backend, or a dispatch-only rung
        (``xtb``, gr496562) — (the caller mints a ``struct_relax`` job). A
        graph edit invalidates any prior relax; the caller persists the run
        (§9 system-of-record)."""
        graph_ops = [o for o in ops if o.get("op") != "relax"]
        relax_ops = [o for o in ops if o.get("op") == "relax"]
        try:
            apply_ops(scene, graph_ops)
        except OpError as exc:
            raise BadInput(
                f"op error: {exc}",
                next="get(kind='skill', id='precis-structure-help') "
                "for the op vocabulary",
                options=exc.options,
            ) from exc
        if not relax_ops:
            return None
        ro = relax_ops[-1]
        fidelity = str(ro.get("fidelity", "clean"))
        steps = int(ro.get("steps", 200))
        # No explicit model=: route by composition on the 'ml' rung (gripe
        # 285774) — an all-organic, non-periodic, neutral scene defaults to
        # the organic MLIP (mace_off) rather than the materials one
        # (mace_mp), which is off-distribution for exactly that chemistry.
        # The 'xtb' rung has exactly one backend (GFN2-xTB via tblite), so
        # its model is pinned rather than routed — naming anything else is
        # refused here rather than silently computed under a wrong label.
        # Every other rung keeps the historical literal default — model
        # isn't functional there yet, so there's nothing to route.
        explicit_model = ro.get("model")
        if explicit_model is not None:
            model = str(explicit_model)
            if fidelity == "xtb" and model.lower().replace("_", "-") != "gfn2-xtb":
                raise BadInput(
                    f"relax model {model!r} does not apply to rung 'xtb' — "
                    "the rung is GFN2-xTB only (model='gfn2-xtb' or omit)",
                    next="ops=[{'op': 'relax', 'fidelity': 'xtb'}] "
                    "(model pins to gfn2-xtb)",
                )
        elif fidelity == "ml":
            model = route_ml_model(scene)
        elif fidelity == "xtb":
            model = "gfn2-xtb"
        else:
            model = "mace_mp"
        # Optional variable-cell relax: 'inplane' (slab box, vacuum pinned) or
        # 'full' (bulk). Validated up front so a bad mode is a retryable BadInput
        # rather than being swallowed into the dispatch/Unsupported path below.
        cell_mode = ro.get("cell") or None
        if cell_mode == "fixed":
            cell_mode = None
        if cell_mode is not None and cell_mode not in ("inplane", "full"):
            raise BadInput(
                f"relax cell mode {cell_mode!r} not understood "
                "(use 'inplane', 'full', or omit for an atoms-only relax)",
                options=["inplane", "full"],
            )
        if cell_mode is not None and fidelity in ("clean", "0", "jiggle"):
            raise BadInput(
                "variable-cell relax (cell=…) needs fidelity='ml'; the 'clean' "
                "geometry repair has no stress to relax the cell against",
                next="ops=[{'op': 'relax', 'fidelity': 'ml', 'cell': 'inplane'}]",
            )
        if cell_mode is not None and fidelity == "xtb":
            # Refused here, not left to relax()'s RelaxUnsupported: that
            # exception is the cloud-dispatch trigger below, and a struct_relax
            # job carrying cell= for an atoms-only rung would only fail as
            # infra on the node.
            raise BadInput(
                "variable-cell relax (cell=…) needs fidelity='ml'; rung 'xtb' "
                "relaxes atoms only",
                next="ops=[{'op': 'relax', 'fidelity': 'ml', 'cell': 'inplane'}]",
            )
        # Optional explicit force target (eV/Å) — the way to ask for a tighter
        # relax than the rung's max(tol, 0.05) floor (the β-PdH re-runs). It
        # applies only to an energy rung: a geometry repair has no forces to
        # converge, so fmax there is refused up front like cell=.
        fmax_raw = ro.get("fmax")
        fmax: float | None = None
        if fmax_raw is not None:
            try:
                fmax = float(fmax_raw)
            except (TypeError, ValueError):
                fmax = -1.0
            if not (fmax > 0.0):  # also catches NaN
                raise BadInput(
                    f"relax fmax must be a positive force target (eV/Å), "
                    f"got {fmax_raw!r}",
                    next="ops=[{'op': 'relax', 'fidelity': 'ml', 'fmax': 0.01}]",
                )
            if fidelity in ("clean", "0", "geo", "jiggle"):
                raise BadInput(
                    f"relax fmax is a force target — rung {fidelity!r} is a "
                    "geometry repair with no forces to converge",
                    next="ops=[{'op': 'relax', 'fidelity': 'ml', 'fmax': 0.01}]",
                )
        # Optional DFT-D3 on top of the MLIP. Off by default (the historical
        # behaviour), but load-bearing for anything held together by van der
        # Waals contact — a host-guest complex or an interlocked ring reads as
        # barely bound without it. Validated here so a wrong rung is a
        # retryable BadInput rather than a RelaxUnsupported swallowed into the
        # cloud-dispatch path below.
        dispersion = bool(ro.get("dispersion", False))
        if dispersion and fidelity != "ml":
            raise BadInput(
                f"dispersion=True needs fidelity='ml'; rung {fidelity!r} has no "
                "potential to correct",
                next="ops=[{'op': 'relax', 'fidelity': 'ml', 'dispersion': true}]",
            )

        # Cache-first for the expensive energy rungs. The rung-0
        # ``clean`` repair is instant + pure + energy-free, so it is never
        # cached — it just runs. ``jiggle`` is uncached too: it is a
        # stochastic test rung whose seed/sigma aren't part of the key,
        # so a cached hit would silently freeze one random draw and let
        # two different jiggles collide. The key is over the *input*
        # geometry (this scene, after graph ops, before relax mutates
        # it), so capture the content address + canonical order now.
        cached_rung = fidelity not in ("clean", "0", "jiggle")
        cache_key = structure_sha = None
        order: list[str] = []
        if cached_rung:
            # Only fold ``cell`` into the key when a variable-cell relax is
            # asked for — an atoms-only relax keeps its historical key so the
            # existing run-cube stays a hit (a bare {"steps"} vs {"steps","cell"}).
            cache_key, structure_sha, order = _relax_cache_address(
                scene,
                fidelity=fidelity,
                model=model,
                steps=steps,
                cell_mode=cell_mode,
                dispersion=dispersion,
                fmax=fmax,
            )
            hit_result = self._cache_hit_result(
                scene,
                cache_key=cache_key,
                structure_sha=structure_sha,
                fidelity=fidelity,
                dispersion=dispersion,
            )
            if hit_result is not None:
                return hit_result

        # gr496562: rung 'xtb' is dispatch-only from the request path — a
        # GFN2-xTB relax on a real block is minutes of compute, and tblite
        # being importable in the MCP container must not turn the call into
        # a blocking inline relax (three hexfold blocks hung 300 s+ that
        # way, minting no job). Stage it exactly like a rung with no local
        # backend; the in-process compute still happens inside the
        # struct_relax job on the compute group (_INPROC_FIDELITIES).
        if fidelity == "xtb":
            return self._stage_relax_dispatch(
                scene,
                ro,
                fidelity=fidelity,
                model=model,
                steps=steps,
                cell_mode=cell_mode,
                dispersion=dispersion,
                cache_key=cache_key,
                fmax=fmax,
            )

        try:
            res = run_relax(
                scene,
                fidelity=fidelity,
                steps=steps,
                model=model,
                cell=cell_mode,
                dispersion=dispersion,
                fmax=fmax,
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
            # No local backend for this energy rung — stage a
            # ``struct_relax`` job (the dispatch-only rung above takes the
            # same path with no ``cause``).
            return self._stage_relax_dispatch(
                scene,
                ro,
                fidelity=fidelity,
                model=model,
                steps=steps,
                cell_mode=cell_mode,
                dispersion=dispersion,
                cache_key=cache_key,
                fmax=fmax,
                cause=exc,
            )

        # Cache miss: stamp the content address + relaxed geometry so the next
        # identical request — on this design or any other sharing the input —
        # is a zero-compute hit.
        if cached_rung and res.converged:
            res.cache_key = cache_key
            res.structure_sha = structure_sha
            res.final_geometry = relax_cache.serialize_geometry(scene, order)
        return res

    def _stage_relax_dispatch(
        self,
        scene: Scene,
        ro: dict[str, Any],
        *,
        fidelity: str,
        model: str,
        steps: int,
        cell_mode: str | None,
        dispersion: bool,
        cache_key: str | None,
        fmax: float | None = None,
        cause: RelaxUnsupported | None = None,
    ) -> RelaxResult | _NeedsDispatch:
        """Stage a ``struct_relax`` dispatch for a rung that must not compute
        in the request path — either an energy rung whose local backend is
        missing (``cause`` carries the :class:`RelaxUnsupported` the inline
        attempt raised) or the dispatch-only ``xtb`` rung (gr496562;
        ``cause=None`` — tblite importable or not, a GFN2-xTB relax never
        runs inside an MCP call).

        Refuses rather than stages when the dispatch contract cannot carry
        the request honestly (``dispersion``: the ``struct_relax`` params
        schema carries no dispersion field, so a dispatched job would compute
        D3-*free* and sink it into the run-cube under a dispersion=True cache
        key — the same call the dispatcher's own fidelity gate made,
        gr346449), or when the geometry fails the hard-reject validator
        (gripe 51393). Otherwise it runs the cheap local pre-relax
        (``preflight=`` picks ``clean``/``emt``), re-checks the run-cube over
        the cleaned geometry — a preflight-clean onto an already-computed
        input is still a zero-compute hit — and returns the
        :class:`_NeedsDispatch` the caller mints."""
        if dispersion:
            raise Unsupported(
                f"{cause} — dispersion=True runs locally only; the "
                "struct_relax dispatch contract carries no dispersion "
                "flag, so this relax cannot be dispatched",
                next="install the [dft-ml] extra locally, or re-run with "
                "dispersion omitted to use the cluster",
            ) from cause
        # xtb dispatches like ml: its jobs pin to the compute group
        # (struct_relax.mlip_nodes), whose worker venv carries the
        # linux-aarch64 tblite wheel from the wheelhouse
        # (scripts/build-tblite-wheel, gr488907). A node without it fails
        # the job as infra, naming the extra.
        if cache_key is None:  # defensive — an uncached rung never reaches here
            raise Unsupported(
                str(cause),
                next="relax with fidelity='clean' (geometry repair, always available)",
            ) from cause

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
            ) from cause

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
        #
        # ``dispersion`` is threaded explicitly even though the guard above
        # means it is always False here: stating it keeps this address
        # honest if that early raise is ever relaxed, rather than silently
        # minting a dispersion-free key for a dispersion-on relax.
        cache_key, structure_sha, order = _relax_cache_address(
            scene,
            fidelity=fidelity,
            model=model,
            steps=steps,
            cell_mode=cell_mode,
            dispersion=dispersion,
            fmax=fmax,
        )
        hit_result = self._cache_hit_result(
            scene,
            cache_key=cache_key,
            structure_sha=structure_sha,
            fidelity=fidelity,
            dispersion=dispersion,
        )
        if hit_result is not None:
            return hit_result
        return _NeedsDispatch(
            fidelity=fidelity,
            model=model,
            steps=steps,
            cell=cell_mode,
            fmax=fmax,
            cache_key=cache_key,
            structure_sha=structure_sha or "",
            order=order,
            poscar=export.to_poscar(scene),
            poscar_labels=_poscar_row_labels(scene),
            # Optional requesting todo (compute lane). Accept
            # the clear ``requested_by`` key; tolerate the legacy
            # ``parent_id`` spelling from before the lane split.
            requester_id=_as_int_or_none(ro.get("requested_by", ro.get("parent_id"))),
            preflight_result=preflight_result,
            preflight_note=preflight_note,
        )

    def _cache_hit_result(
        self,
        scene: Scene,
        *,
        cache_key: str,
        structure_sha: str,
        fidelity: str,
        dispersion: bool = False,
    ) -> RelaxResult | None:
        """The run-cube lookup, shared by both cache-check sites
        in ``_run_ops``: the early check over the as-authored geometry, and
        the post-preflight re-check over the cleaned geometry (gripe 51393)
        — a design that preflight-cleans onto an already-cached input must
        be a zero-compute hit too, not a fresh cloud dispatch.

        On a hit, applies the cached geometry onto ``scene`` and returns the
        stored envelope as a :class:`RelaxResult`; ``None`` on a miss.

        ``dispersion`` is echoed onto the returned envelope rather than read
        back off the row: it is already folded into ``cache_key``, so a hit
        *is* a run with this setting — echoing it keeps the fresh audit row
        this hit writes labelled with the same calculator identity."""
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
            dispersion=dispersion,
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
        if res.note:
            out["note"] = res.note
        if res.dispersion:
            out["dispersion"] = True
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
            #
            # ``dispersion`` rides in params (not a column) because it is part
            # of the calculator's identity: format_calc_identity surfaces it in
            # the ``calc:`` header so a D3-corrected energy is never read as a
            # bare-MLIP one.
            params=_run_params(res),
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
        from precis.todo import guards as todo_guards

        # A named requester must be a live todo (fail fast, before the mint).
        if nd.requester_id is not None:
            todo_guards.check_parent_exists(self.store, nd.requester_id)

        # Resolve the node pin before minting. A container rung goes to the one
        # DFT node (image + GPU + the NFS scratch the stager writes into); an
        # in-process MLIP rung needs none of those and spreads over the compute
        # group instead (gr346449) — see struct_relax.target_node_for. Deploy
        # renders both lists from topology (precis_capabilities.dft /
        # .autocatpath) into every minting daemon's env. Deliberately no
        # node-literal fallback — a hardcoded default outlives the node it names
        # (the 2026-08-29 spark retirement left jobs minting against a ghost for
        # days), so unset refuses loudly.
        from precis.workers.job_types.struct_relax import target_node_for

        target = target_node_for(nd.fidelity, key=nd.cache_key)
        if not target:
            raise Internal(
                "no compute node configured: neither PRECIS_DFT_NODE nor "
                "PRECIS_MLIP_NODES is set on this host, so a struct_relax job "
                "would have nowhere to run — deploy renders them from topology "
                "(precis_capabilities.dft / .autocatpath)",
                next="re-run with a locally-computable rung "
                "(ops=[{'op':'relax','fidelity':'clean'}] always works) — "
                "the node lists are deploy env this host lacks",
            )

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
            # Explicit force target, same convention: present only when asked
            # for; absent ⇒ each backend's own fmax default.
            **({"fmax": nd.fmax} if nd.fmax is not None else {}),
            "cache_key": nd.cache_key,
            "structure_sha": nd.structure_sha,
            "order": nd.order,
            "poscar_labels": nd.poscar_labels,
            "poscar": nd.poscar,
            # Pin the job so exactly one host's worker claims it (§23 #3) —
            # for a container rung that means the stager and the container
            # share one host's NFS view.
            "target_node": target,
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
        ``derived_job_succeeded`` evaluator + the failure bubble follow), then
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

    @staticmethod
    def _geometry_tier(ref: Any, relax_summary: dict[str, Any] | None = None) -> str:
        """Tier of the stored coordinates, from the recorded last relax; never invented."""
        lr = relax_summary or (ref.meta or {}).get("last_relax")
        if lr and lr.get("rung") is not None:
            return f"relax[{lr['rung']}]"
        return "unknown"

    def _toc_response(
        self,
        scene: Scene,
        ref: Any,
        handles: dict[str, int],
        *,
        head_verb: str | None = None,
        relax_summary: dict[str, Any] | None = None,
        inline_atoms: bool = False,
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
                f"(last-step max move {lr.get('max_disp')} Å)"
            )
            if lr.get("note"):
                head += f"\n# relax note: {lr['note']}"
        if scene.atoms:
            cart = np.array(
                [
                    scene.cell.frac_to_cart(np.asarray(at.frac))
                    for at in scene.atoms.values()
                ]
            )
            lo, hi = cart.min(axis=0), cart.max(axis=0)
            head += "\n# bbox Å: " + " · ".join(
                f"{ax} {a:.2f}..{b:.2f}" for ax, a, b in zip("xyz", lo, hi, strict=True)
            )
        gstats = geometry_stats.compute(scene)
        gline = geometry_stats.head_line(gstats)
        if gline:
            head += f"\n# geometry: {gline} · tier: {self._geometry_tier(ref, relax_summary)}"
        findings = validate(scene) + vsepr.advisories(scene)
        n_err = sum(1 for f in findings if f.severity == "error")
        n_warn = sum(1 for f in findings if f.severity == "warn")
        head += f"\n# validate: {n_err} error(s), {n_warn} warning(s)"
        show_atoms = inline_atoms or len(scene.atoms) <= _TOC_INLINE_ATOMS
        if not show_atoms:
            head += (
                f"\n# {t['natoms']} atoms not listed — "
                "view='atoms' for the per-atom table; view='validate' for findings"
            )
        body = head
        if show_atoms:
            # gripe 161576: a compact |F| (eV/Å) column — a cheap DB read from
            # a recorded run at the CURRENT design version only (FIX 2); never
            # the live EMT estimate here (FIX 3 — that's view='atom'-only).
            # '—' when no such run exists (never fabricated).
            force_mags = self._force_magnitudes(scene, ref)
            rows = []
            coord_all = probe.coordination_all(scene)
            for label, atom in scene.atoms.items():
                rows.append(
                    {
                        "atom": f"{handle}#{label}",
                        "element": atom.element,
                        "frac": ",".join(f"{x:.3f}" for x in atom.frac),
                        "coord": coord_all[label],
                        "fixed": "yes" if atom.fixed else "no",
                        "|F|": force_mags.get(label, "—"),
                    }
                )
            body += "\n" + render_agent_table(
                rows, schema=["atom", "element", "frac", "coord", "fixed", "|F|"]
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
                f"run={run_arg!r} is not a run id (try 'r12' or 12)",
                next="get(kind='structure', id='<slug>', view='runs') for the run ids",
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
                    f"no run r{run_id} on this structure with forces recorded",
                    next="get(kind='structure', id='<slug>', view='runs') "
                    "for the recorded runs",
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
        if view == "stats":
            return Response(
                body=geometry_stats.render(
                    geometry_stats.compute(scene), tier=self._geometry_tier(ref)
                )
            )
        if view == "atom":
            label = _atom_arg(args)
            if label not in scene.atoms:
                raise NotFound(
                    f"no atom {label!r} in this structure",
                    next="get(kind='structure', id='<slug>', view='atoms') "
                    "for the live labels",
                )
            atom = scene.atoms[label]
            nbrs = probe.neighborhood(scene, label, radius=3.5)
            charge_note = "" if atom.charge == 0 else f" · charge={atom.charge:+d}"
            head = (
                f"# {label} — {atom.element} frac({','.join(f'{x:.3f}' for x in atom.frac)}) "
                f"· coord {probe.coordination(scene, label)} · "
                f"fixed={'yes' if atom.fixed else 'no'}{charge_note}"
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
            center = _atom_arg(args, alias="center")
            if center not in scene.atoms:
                raise NotFound(
                    f"no atom {center!r} in this structure",
                    next="get(kind='structure', id='<slug>', view='atoms') "
                    "for the live labels",
                )
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
        # validate — the hard-reject gate (error) plus the advisory VSEPR/
        # hybridization tier (warn); the latter never blocks anything, it's
        # display-only here (see vsepr.py's module docstring).
        findings = validate(scene) + vsepr.advisories(scene)
        if not findings:
            return Response(body="✓ no validator findings")
        n_error = sum(1 for f in findings if f.severity == "error")
        n_warn = sum(1 for f in findings if f.severity == "warn")
        tally = Counter((f.severity, f.rule) for f in findings)
        tally_rows = [
            {"severity": sev, "rule": rule, "count": n}
            for (sev, rule), n in sorted(
                tally.items(), key=lambda kv: (kv[0][0] != "error", -kv[1], kv[0][1])
            )
        ]
        rows = [
            {
                "severity": f.severity,
                "rule": f.rule,
                "atoms": ",".join(f.atoms),
                "measured": f"{f.measured}",
                "expected": f"{f.expected}",
                "fix": f.suggested_fix,
            }
            for f in findings
        ]
        return Response(
            body=f"# {n_error} error(s), {n_warn} warning(s)\n"
            + render_agent_table(tally_rows, schema=["severity", "rule", "count"])
            + "\n"
            + render_agent_table(
                rows,
                schema=["severity", "rule", "atoms", "measured", "expected", "fix"],
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
                raise BadInput(
                    "line needs direction= (a 3-vector, Cartesian Å)",
                    next="get(kind='structure', id='<slug>', view='line', "
                    "args={'direction': [0, 0, 1]})",
                )
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
                raise BadInput(
                    "plane needs normal= (a 3-vector, Cartesian)",
                    next="get(kind='structure', id='<slug>', view='plane', "
                    "args={'normal': [0, 0, 1]})",
                )
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
                    raise BadInput(
                        "bonds_through_plane needs normal=",
                        next="get(kind='structure', id='<slug>', "
                        "view='bonds_through_plane', args={'normal': [0, 0, 1]})",
                    )
                crossing = probe.bonds_through_plane(scene, point, normal)
                head = f"# {len(crossing)} bonds cross the plane"
                acol = "∠normal"
            else:
                center = _vec(args, "center", "point")
                if center is None:
                    raise BadInput(
                        "bonds_in_sphere needs center=",
                        next="get(kind='structure', id='<slug>', "
                        "view='bonds_in_sphere', "
                        "args={'center': [0, 0, 0], 'radius': 3.0})",
                    )
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
                raise NotFound(
                    "path needs a= and b= as live atom labels",
                    next="get(kind='structure', id='<slug>', view='atoms') "
                    "for the live labels",
                )
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
                    "diff needs other= (another structure slug to compare against)",
                    next="get(kind='structure') to list designs, then "
                    "get(kind='structure', id='<slug>', view='diff', "
                    "args={'other': '<slug>'})",
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
            raise BadInput(
                "pov needs support= (an atom label or a list of labels)",
                next="get(kind='structure', id='<slug>', view='pov', "
                "args={'support': '<label>'})",
            )
        support = (
            [str(support_raw).split("#")[-1]]
            if isinstance(support_raw, str)
            else [str(s).split("#")[-1] for s in support_raw]
        )
        missing = [s for s in support if s not in scene.atoms]
        if missing:
            raise NotFound(
                f"no such atom(s) in support: {', '.join(missing)}",
                next="get(kind='structure', id='<slug>', view='atoms') "
                "for the live labels",
            )
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
        """Delegates to the module-level :func:`card_text` (see its
        docstring) — kept as a method so every existing call site here reads
        unchanged."""
        return card_text(title, scene, description)

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
            rows.append(
                {
                    "handle": handle,
                    "design": ref.slug,
                    "title": ref.title,
                    # gr514060: the stored generator record's name + key
                    # params, so a hit like 'hex (hexfold_scene generator)'
                    # shows what it was built from — '—' when absent.
                    "gen": _generator_brief((ref.meta or {}).get("generated")),
                }
            )
        return Response(
            body=f"# {len(triples)} structure(s) for {q!r}\n"
            + render_agent_table(rows, schema=["handle", "design", "title", "gen"])
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
        return self.store.chunks.search_chunks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="structure",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            card_kinds=("card_combined", "card_description"),
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
        self.store.chunks.bump_salience([b.id for b, _r, _s in triples])
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
