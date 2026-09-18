"""Evaluate a reaction_network on a material.

Pure-function implementation of the v1 ``reaction_evaluate``
job_type's logic. The job_type's ``run()`` shim (in
``precis_dft.job_types.reaction_evaluate``) calls this function
once it has resolved the material ref and the network record.

The evaluator:

1. Walks the network's species library; for each adsorbed species
   (``*OH``, ``*O``, ``*OOH``, ``*H``, …) finds the corresponding
   ``dft_calculation`` ref on the material's canonical surface.
2. Reads each calc's CHE-referenced free energy from
   ``meta.derived.G[conditions_hash]`` (or falls back to
   ``meta.derived.eads[<scheme>:<species>].value`` + ZPE+TS if
   ``G`` isn't materialised).
3. Calls :func:`precis_dft.reactions.evaluate` to compute the step
   ΔGs + overpotential + RDS.
4. Returns the full record (the material handler writes it as a
   ``reaction_eval`` chunk).

If any species is missing a calc the record carries
``missing_intermediates``; the caller decides whether to spawn
child jobs (the real ``dft_campaign`` coordinator does this; the
v1 tests just inspect the list).
"""

from __future__ import annotations

from typing import Any

from precis_dft import reactions


def compute_overpotential(
    *,
    network: dict[str, Any],
    material_meta: dict[str, Any],
    calculations: dict[str, dict[str, Any]],
    conditions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the reaction_evaluate record.

    ``calculations`` is keyed by species id (``'*OH'`` etc.) and
    each value is the calc record's ``meta`` dict (same shape as
    ``DftCalculationHandler`` stores). The function reads each
    calc's CHE-referenced free energy and feeds them to
    :func:`reactions.evaluate`.

    The clean-slate ``'*'`` species can be omitted from
    ``calculations`` — the evaluator treats its free energy as 0
    by convention.

    Returns the record shape :func:`reactions.evaluate` produces,
    augmented with ``material`` (the source material id, when
    available in ``material_meta``).
    """
    conditions = dict(conditions or {})
    conditions.setdefault("T", 298.0)
    conditions.setdefault("pH", 0.0)
    conditions.setdefault("U_RHE", 0.0)

    canonical = material_meta.get("canonical", {}) or {}
    canonical_eads_scheme = canonical.get("eads_ref_scheme", "norskov_water_h2")

    free_energies: dict[str, float] = {}
    for species, calc_meta in calculations.items():
        g = _resolve_g_for_species(
            species,
            calc_meta,
            scheme=canonical_eads_scheme,
            conditions_hash=reactions.conditions_hash(conditions),
        )
        if g is not None:
            free_energies[species] = g

    record = reactions.evaluate(
        network,
        free_energies=free_energies,
        conditions=conditions,
    )
    if material_meta.get("id"):
        record["material"] = material_meta["id"]
    return record


def _resolve_g_for_species(
    species: str,
    calc_meta: dict[str, Any],
    *,
    scheme: str,
    conditions_hash: str,
) -> float | None:
    """Find the CHE-referenced free energy for one species.

    Lookup order:
    1. ``meta.derived.G[conditions_hash]`` (preferred; populated by
       ``derive_freeenergy``).
    2. ``meta.derived.eads[(scheme, conditions_hash)]`` carrying a
       precomputed CHE-referenced value (when ``derive_eads`` ran
       with the CHE scheme).
    3. ``meta.derived.G_che`` plain scalar (fallback for tests
       that pre-bake the CHE value).
    Returns ``None`` when the calc doesn't carry the species's G in
    any of these forms; the caller treats it as a missing
    intermediate.
    """
    derived = calc_meta.get("derived", {}) or {}

    # 1. derived.G keyed by conditions_hash.
    g_dict = derived.get("G", {}) or {}
    if isinstance(g_dict, dict):
        for key, entry in g_dict.items():
            if conditions_hash in str(key):
                if isinstance(entry, dict) and "value" in entry:
                    return float(entry["value"])
                if isinstance(entry, (int, float)):
                    return float(entry)

    # 2. derived.eads keyed by (scheme, conditions_hash).
    eads_dict = derived.get("eads", {}) or {}
    if isinstance(eads_dict, dict):
        for key, entry in eads_dict.items():
            key_s = str(key)
            if scheme in key_s and conditions_hash in key_s:
                if isinstance(entry, dict) and "value" in entry:
                    return float(entry["value"])
                if isinstance(entry, (int, float)):
                    return float(entry)

    # 3. Plain scalar.
    if "G_che" in derived:
        return float(derived["G_che"])

    return None


__all__ = ["compute_overpotential"]
