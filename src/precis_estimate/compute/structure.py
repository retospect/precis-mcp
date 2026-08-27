"""Tier-2 (structure-coupled) panel — the `estimate` kind's slice-2 surface.

Given a hydrated :class:`~precis.structure.scene.Scene`, renders the same
five-row workup every time: GEOMETRY LINT, COORDINATION/STRAIN, SYMMETRY,
DEDUP, BEP. Every check **reuses** the existing structure-kernel machinery
rather than reimplementing it — this module is glue + rendering, not new
chemistry:

- lint: :func:`precis.structure.preflight.preflight` (the tier-0 MLIP gate —
  floating/detached atoms, clashes, vacuum/porosity).
- coordination/strain: :mod:`precis.structure.probe` (coordination number)
  + :mod:`precis.structure.invariants`'s existing layer/adsorbate-site split
  (``_layers``/``_split_slab_adsorbate``/``_SITE_BY_COORD``/
  ``_SURFACE_CUTOFF``) + ``ase.data.covalent_radii`` for the strain-%
  denominator (same table :mod:`precis.structure.preflight`'s settle field
  already leans on).
- symmetry: ``spglib`` (core dep) — space group + Wyckoff letters, symprec
  1e-3, degrading to a note (never a crash) on any failure.
- dedup: ``pymatgen`` (the ``[estimate]`` extra, lazily imported —
  mendeleev's pattern) ``StructureMatcher`` against a quest's served
  structures (:func:`precis.quest.gaps._live_servers`), composition-
  prefiltered before the expensive fit.
- BEP: a plain ``numpy.linalg.lstsq`` line fit of the campaign's own
  *trusted* barriers (``meta['barrier']`` where ``meta['barrier_trusted']
  is not False`` — the structured trust-records consumer's own predicate,
  see ``quest/frontier.py::_candidate_from_structure``) against the vendored
  Hammer-Nørskov εd of each candidate's dominant surface metal
  (composition-weighted mean when alloyed across >1 vendored metal).

``structure_workup`` is the single entry point the handler calls for both
the plain panel and each side of a ``view='compare'``; :func:`render_compare`
wraps two workups + a scalar-summary delta table.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
from ase.data import atomic_numbers, covalent_radii

from precis.format import toon
from precis.structure import invariants, preflight, probe
from precis.structure.scene import Scene
from precis_estimate.data.hammer_norskov import D_BAND_CENTERS_EV

_DASH = "—"

#: symprec for spglib's symmetry search — loose enough to absorb ordinary
#: relax jitter, tight enough not to over-collapse a genuinely lower-symmetry
#: doped/adsorbate structure into its parent's space group.
_SYMPREC = 1e-3

#: Still not built (slice 3) — named so the drill-down footer keeps naming
#: what's coming rather than pretending it exists (mirrors composition.py).
_PLANNED_VIEWS = ("shape", "orbitals", "spin", "kinetics", "card")

#: Minimum trusted (descriptor, barrier) points before a BEP line is fit —
#: below this a line is not a fit, it's a ruler through noise.
_BEP_MIN_POINTS = 3

#: Half-a-campaign-sigma tolerance band around the campaign's median trusted
#: barrier for the pre-registered lower/on-trend/higher branch call — a
#: deterministic, documented threshold (no numeric constant is given
#: upstream); see :func:`_bep_branch`.
_BEP_BRANCH_TOL_SIGMA = 0.25


def _fmt(x: float | None, spec: str = "{:.2f}") -> str:
    return _DASH if x is None else spec.format(x)


# ── GEOMETRY LINT (reuses preflight.preflight) ──────────────────────────


def _lint_rows(scene: Scene) -> list[dict[str, Any]]:
    verdict = preflight.preflight(scene)
    return [
        {
            "code": r.code,
            "atom": "—" if r.atom is None else str(r.atom),
            "element": r.element or "—",
            "message": r.message,
        }
        for r in verdict.reasons
    ]


# ── COORDINATION / STRAIN (reuses invariants' layer/site split) ─────────


def _dominant_element(scene: Scene) -> str | None:
    """The most common element — the assumed slab metal (mirrors
    ``preflight._dominant_element`` / ``invariants``' own dense-layer
    heuristic; there is no slab-vs-adsorbate provenance on the IR yet)."""
    if not scene.atoms:
        return None
    return Counter(a.element for a in scene.atoms.values()).most_common(1)[0][0]


def _covalent_radius(element: str) -> float | None:
    z = atomic_numbers.get(element)
    if z is None or not (0 <= z < len(covalent_radii)):
        return None
    r = float(covalent_radii[z])
    return r if r > 0 else None


def _coordination_rows(scene: Scene) -> list[dict[str, Any]]:
    """One row per non-dominant-element atom (dopant or adsorbate) — nearest-
    neighbour distance vs covalent-radius-sum strain %, plus a site
    classification (top/bridge/hollow/detached) for atoms in the sparse top
    layer (true adsorbates); a dopant substituted into a dense slab layer
    gets a strain row with no site type ('—' — it's not sitting *on* a
    site, it *is* a lattice site)."""
    dominant = _dominant_element(scene)
    if dominant is None:
        return []
    layers = invariants._layers(scene)
    surface, adsorbate = invariants._split_slab_adsorbate(scene, layers)
    surf_set = set(surface)
    ads_set = set(adsorbate)

    rows: list[dict[str, Any]] = []
    for label, atom in scene.atoms.items():
        if atom.element == dominant:
            continue
        neighbors = [(other, dist) for other, _img, dist in scene.neighbors(label, 6.0)]
        if not neighbors:
            continue
        nearest_label, d_min = neighbors[0]
        nearest_el = scene.atoms[nearest_label].element
        r_a, r_b = _covalent_radius(atom.element), _covalent_radius(nearest_el)
        strain_pct: float | None = None
        r_sum: float | None = None
        if r_a is not None and r_b is not None:
            r_sum = r_a + r_b
            strain_pct = (d_min - r_sum) / r_sum * 100.0

        if label in ads_set:
            role = "adsorbate"
            n_surf = sum(
                1
                for other, _img, dist in scene.neighbors(
                    label, invariants._SURFACE_CUTOFF
                )
                if other in surf_set
            )
            site = invariants._SITE_BY_COORD.get(n_surf, "hollow")
        else:
            role = "dopant"
            site = "—"

        rows.append(
            {
                "atom": label,
                "element": atom.element,
                "role": role,
                "site": site,
                "coord_n": str(probe.coordination(scene, label)),
                "nearest": f"{nearest_label}({nearest_el})",
                "d_min_A": _fmt(d_min),
                "r_sum_A": _fmt(r_sum),
                "strain_pct": _DASH if strain_pct is None else f"{strain_pct:+.1f}",
            }
        )
    return rows


def _mean_coordination(scene: Scene) -> float | None:
    if not scene.atoms:
        return None
    return float(np.mean([probe.coordination(scene, la) for la in scene.atoms]))


# ── SYMMETRY (spglib — core dep) ─────────────────────────────────────────


def _spg_get(dataset: Any, key: str) -> Any:
    """spglib >= 2.0 returns an attribute-style ``SpglibDataset``; tolerate
    a dict-shaped return too (older spglib / a future API change) so this
    degrades rather than crashing on a version drift."""
    if hasattr(dataset, key):
        return getattr(dataset, key)
    if isinstance(dataset, dict):
        return dataset.get(key)
    return None


def _symmetry_row(scene: Scene) -> dict[str, str]:
    if not scene.atoms:
        return {
            "space_group": _DASH,
            "number": _DASH,
            "wyckoff": _DASH,
            "note": "no atoms",
        }
    try:
        import spglib

        numbers = []
        for atom in scene.atoms.values():
            z = atomic_numbers.get(atom.element)
            if z is None:
                raise ValueError(f"unknown element for spglib: {atom.element!r}")
            numbers.append(z)
        positions = [list(map(float, a.frac)) for a in scene.atoms.values()]
        lattice = [list(map(float, row)) for row in scene.cell.lattice]
        dataset = spglib.get_symmetry_dataset(
            (lattice, positions, numbers), symprec=_SYMPREC
        )
    except ImportError:
        return {
            "space_group": _DASH,
            "number": _DASH,
            "wyckoff": _DASH,
            "note": "spglib not installed",
        }
    except Exception as exc:
        return {
            "space_group": _DASH,
            "number": _DASH,
            "wyckoff": _DASH,
            "note": f"spglib failed: {exc}",
        }
    if dataset is None:
        return {
            "space_group": _DASH,
            "number": _DASH,
            "wyckoff": _DASH,
            "note": "no symmetry dataset (degenerate cell)",
        }
    intl = _spg_get(dataset, "international") or _DASH
    number = _spg_get(dataset, "number")
    wyckoffs_raw = _spg_get(dataset, "wyckoffs")
    wyckoffs = [] if wyckoffs_raw is None else list(wyckoffs_raw)
    equiv_raw = _spg_get(dataset, "equivalent_atoms")
    equiv = [] if equiv_raw is None else list(equiv_raw)
    wy_counts = Counter(wyckoffs)
    wy_str = (
        ", ".join(f"{letter}×{n}" for letter, n in sorted(wy_counts.items())) or _DASH
    )
    n_orbits = len(set(equiv)) if equiv else 0
    return {
        "space_group": str(intl),
        "number": _DASH if number is None else str(number),
        "wyckoff": wy_str,
        "note": f"{n_orbits} site orbit(s) over {len(scene.atoms)} atom(s)"
        if n_orbits
        else "—",
    }


# ── DEDUP (pymatgen StructureMatcher vs a quest's tried set) ─────────────


def _to_pymatgen(scene: Scene) -> Any:
    from pymatgen.core import Lattice, Structure

    lattice = Lattice(np.asarray(scene.cell.lattice, dtype=float))
    species = [a.element for a in scene.atoms.values()]
    coords = [list(map(float, a.frac)) for a in scene.atoms.values()]
    return Structure(lattice, species, coords, coords_are_cartesian=False)


def _dedup(
    scene: Scene, *, store: Any, quest_ref_id: int | None
) -> tuple[list[dict[str, str]], str]:
    if quest_ref_id is None:
        return [], "no quest context given (args={'quest': N})"
    try:
        from pymatgen.analysis.structure_matcher import StructureMatcher
    except ImportError:
        return [], "pymatgen not installed — dedup unavailable"

    from precis.quest.gaps import _live_servers

    servers = [r for r in _live_servers(store, quest_ref_id) if r.kind == "structure"]
    if not servers:
        return [], f"quest serves no structures yet (quest {quest_ref_id})"

    target = _to_pymatgen(scene)
    target_formula = target.composition.reduced_formula
    matcher = StructureMatcher()
    matches: list[dict[str, str]] = []
    for ref in servers:
        try:
            other_scene, _handles = store.structure_load(ref.id)
        except Exception:
            continue
        if not other_scene.atoms:
            continue
        other = _to_pymatgen(other_scene)
        if other.composition.reduced_formula != target_formula:
            continue  # cheap prefilter before the expensive fit
        try:
            is_match = matcher.fit(target, other)
        except Exception:
            continue
        if is_match:
            from precis.utils import handle_registry

            handle = (
                handle_registry.try_format("structure", ref.id) or f"structure:{ref.id}"
            )
            matches.append({"handle": handle, "slug": ref.slug or "—"})

    if matches:
        note = (
            f"{len(matches)} structural match(es) among {len(servers)} served "
            "structure(s) — SKIP DISPATCH (duplicate of already-tried candidate)"
        )
    else:
        note = f"no structural match among {len(servers)} served structure(s)"
    return matches, note


# ── BEP (own-campaign trusted-barrier scaling) ────────────────────────────


def _weighted_eps_d(scene: Scene) -> float | None:
    """Composition-weighted mean vendored d-band center over every vendored
    metal present (Ni/Cu/Pd/Ag/Pt/Au) — ``None`` when none of the scene's
    elements are vendored at all."""
    comp = scene.composition()
    weighted = [
        (D_BAND_CENTERS_EV[el], n) for el, n in comp.items() if el in D_BAND_CENTERS_EV
    ]
    if not weighted:
        return None
    total = sum(n for _, n in weighted)
    return sum(v * n for v, n in weighted) / total


def _trusted_points(store: Any, quest_ref_id: int) -> list[tuple[float, float]]:
    """(descriptor εd, trusted barrier eV) pairs over a quest's served
    structures — trusted per the frontier's own predicate
    (``barrier_trusted is not False``; absent = trusted, only an explicit
    ``False`` excludes)."""
    from precis.quest.gaps import _live_servers

    points: list[tuple[float, float]] = []
    for ref in _live_servers(store, quest_ref_id):
        if ref.kind != "structure":
            continue
        meta = ref.meta or {}
        if meta.get("barrier_trusted") is False:
            continue
        barrier = meta.get("barrier")
        if barrier is None:
            continue
        try:
            barrier_f = float(barrier)
        except (TypeError, ValueError):
            continue
        try:
            other_scene, _handles = store.structure_load(ref.id)
        except Exception:
            continue
        eps_d = _weighted_eps_d(other_scene)
        if eps_d is None:
            continue
        points.append((eps_d, barrier_f))
    return points


def _bep_branch(predicted: float, ys: np.ndarray) -> str:
    """Pre-registered lower/on-trend/higher call vs the campaign's own
    trusted-barrier spread: within ``_BEP_BRANCH_TOL_SIGMA`` campaign-sigma
    of the median is 'on-trend'; clearly below/above is 'lower'/'higher'.
    A deterministic, documented threshold — no numeric constant is given
    upstream, so this is the implementer's own defensible rule."""
    med = float(np.median(ys))
    sigma = float(np.std(ys))
    tol = sigma * _BEP_BRANCH_TOL_SIGMA
    if predicted < med - tol:
        return "lower"
    if predicted > med + tol:
        return "higher"
    return "on-trend"


def _bep(scene: Scene, *, store: Any, quest_ref_id: int | None) -> dict[str, Any]:
    if quest_ref_id is None:
        return {"note": "no quest context given (args={'quest': N})"}
    points = _trusted_points(store, quest_ref_id)
    n = len(points)
    if n < _BEP_MIN_POINTS:
        return {"n_trusted": n, "note": f"insufficient trusted barriers (n={n})"}

    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    a_mat = np.vstack([xs, np.ones_like(xs)]).T
    (slope, intercept), *_rest = np.linalg.lstsq(a_mat, ys, rcond=None)
    y_pred = slope * xs + intercept
    ss_res = float(np.sum((ys - y_pred) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0

    this_eps_d = _weighted_eps_d(scene)
    out: dict[str, Any] = {
        "n_trusted": n,
        "slope": float(slope),
        "intercept": float(intercept),
        "r2": r2,
    }
    if this_eps_d is None:
        dominant = _dominant_element(scene) or "?"
        out["note"] = f"no descriptor for {dominant}"
        return out
    predicted = float(slope * this_eps_d + intercept)
    out["eps_d"] = this_eps_d
    out["predicted_barrier_eV"] = predicted
    out["branch"] = _bep_branch(predicted, ys)
    out["note"] = None
    return out


# ── panel assembly ────────────────────────────────────────────────────────


def _lint_section(scene: Scene) -> list[str]:
    rows = _lint_rows(scene)
    lines = ["## Geometry lint"]
    if not rows:
        lines.append("no lint issues (no floating/detached atoms or clashes)")
    else:
        lines.append(toon.dump(rows, schema=["code", "atom", "element", "message"]))
    return lines


def _coordination_section(scene: Scene) -> list[str]:
    rows = _coordination_rows(scene)
    lines = ["", "## Coordination / strain"]
    if not rows:
        lines.append("no dopant/adsorbate atoms to report (or single-element scene)")
    else:
        lines.append(
            toon.dump(
                rows,
                schema=[
                    "atom",
                    "element",
                    "role",
                    "site",
                    "coord_n",
                    "nearest",
                    "d_min_A",
                    "r_sum_A",
                    "strain_pct",
                ],
            )
        )
    return lines


def _symmetry_section(scene: Scene) -> list[str]:
    row = _symmetry_row(scene)
    return [
        "",
        "## Symmetry",
        toon.dump([row], schema=["space_group", "number", "wyckoff", "note"]),
    ]


def _dedup_section(scene: Scene, *, store: Any, quest_ref_id: int | None) -> list[str]:
    matches, note = _dedup(scene, store=store, quest_ref_id=quest_ref_id)
    lines = ["", "## Dedup", note]
    if matches:
        lines.append(toon.dump(matches, schema=["handle", "slug"]))
    return lines


def _bep_section(scene: Scene, *, store: Any, quest_ref_id: int | None) -> list[str]:
    bep = _bep(scene, store=store, quest_ref_id=quest_ref_id)
    lines = ["", "## BEP (Brønsted–Evans–Polanyi, own-campaign fit)"]
    if bep.get("note"):
        lines.append(bep["note"])
    if "slope" in bep:
        row = {
            "n_trusted": str(bep["n_trusted"]),
            "slope": _fmt(bep["slope"], "{:.4f}"),
            "intercept": _fmt(bep["intercept"], "{:.4f}"),
            "r2": _fmt(bep.get("r2"), "{:.3f}"),
            "eps_d_this": _fmt(bep.get("eps_d")),
            "predicted_barrier_eV": _fmt(bep.get("predicted_barrier_eV")),
            "branch": bep.get("branch") or _DASH,
        }
        lines.append(
            toon.dump(
                [row],
                schema=[
                    "n_trusted",
                    "slope",
                    "intercept",
                    "r2",
                    "eps_d_this",
                    "predicted_barrier_eV",
                    "branch",
                ],
            )
        )
    return lines


_FOOTER = (
    "ms structure-workup tier (preflight geometry lint + invariants "
    "coordination/strain + spglib symmetry + pymatgen StructureMatcher dedup "
    "+ own-campaign BEP scaling) — hypothesis-generating only, inadmissible "
    "for rulings; measure before citing as fact."
)


def structure_workup(
    scene: Scene, *, store: Any, quest_ref_id: int | None, title: str
) -> str:
    """The full structure-tier workup body for a hydrated ``scene``."""
    formula = "".join(f"{el}{n}" for el, n in sorted(scene.composition().items()))
    lines: list[str] = [f"# estimate: {title} — {formula} (structure tier)"]
    lines += _lint_section(scene)
    lines += _coordination_section(scene)
    lines += _symmetry_section(scene)
    lines += _dedup_section(scene, store=store, quest_ref_id=quest_ref_id)
    lines += _bep_section(scene, store=store, quest_ref_id=quest_ref_id)
    lines += [
        "",
        "---",
        _FOOTER,
        "Drill-down (slice 3, not built yet): " + ", ".join(_PLANNED_VIEWS) + ".",
    ]
    return "\n".join(lines)


# ── compare (side-by-side workups + a scalar delta table) ────────────────


def _scalar_summary(
    scene: Scene, *, store: Any, quest_ref_id: int | None
) -> dict[str, float | None]:
    """The small numeric-only cross-section :func:`render_compare` diffs —
    everything else in the panel (lint messages, dedup handles, symmetry
    labels) is prose/handles, not a number to delta."""
    lint_rows = _lint_rows(scene)
    coord_rows = _coordination_rows(scene)
    strains = [float(r["strain_pct"]) for r in coord_rows if r["strain_pct"] != _DASH]
    bep = _bep(scene, store=store, quest_ref_id=quest_ref_id)
    return {
        "n_atoms": float(len(scene.atoms)),
        "n_lint_issues": float(len(lint_rows)),
        "mean_coordination": _mean_coordination(scene),
        "max_strain_pct": max((abs(s) for s in strains), default=None),
        "predicted_barrier_eV": bep.get("predicted_barrier_eV"),
    }


def _delta_rows(
    a: dict[str, float | None], b: dict[str, float | None]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key in a:
        va, vb = a.get(key), b.get(key)
        delta = None if va is None or vb is None else vb - va
        rows.append(
            {
                "metric": key,
                "a": _fmt(va),
                "b": _fmt(vb),
                "delta_b_minus_a": _fmt(delta, "{:+.2f}"),
            }
        )
    return rows


def render_compare(
    name_a: str,
    scene_a: Scene,
    name_b: str,
    scene_b: Scene,
    *,
    store: Any,
    quest_ref_id: int | None,
) -> str:
    """Two full workups back to back, plus a scalar-delta table — the
    "what did the dopant/op DO" argument form (design doc §Shape)."""
    body_a = structure_workup(
        scene_a, store=store, quest_ref_id=quest_ref_id, title=name_a
    )
    body_b = structure_workup(
        scene_b, store=store, quest_ref_id=quest_ref_id, title=name_b
    )
    summary_a = _scalar_summary(scene_a, store=store, quest_ref_id=quest_ref_id)
    summary_b = _scalar_summary(scene_b, store=store, quest_ref_id=quest_ref_id)
    delta_rows = _delta_rows(summary_a, summary_b)
    lines = [
        f"# estimate: compare {name_a} vs {name_b}",
        "",
        f"## A — {name_a}",
        body_a,
        "",
        f"## B — {name_b}",
        body_b,
        "",
        "## Delta (B − A, numeric rows only)",
        toon.dump(delta_rows, schema=["metric", "a", "b", "delta_b_minus_a"]),
    ]
    return "\n".join(lines)


__all__ = ["render_compare", "structure_workup"]
