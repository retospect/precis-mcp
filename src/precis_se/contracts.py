"""Interface contracts and insertion cones — se's adoption of the design
core's contract vocabulary (docs/backlog/design-state-core.md item 7a;
multiscale-design-system-spec.md §1.2 ``Contract`` / ``Block.
insertion_direction``, §2 "single-direction assembly").

**A contract is six load components, and absent means UNKNOWN.** A
connect's ``contract`` carries ``loads`` over :data:`COMPONENTS` — forces
``Fx``/``Fy``/``Fz`` in newtons, moments ``Mx``/``My``/``Mz`` in
newton-metres, in the design frame — each one present only when somebody
stated or derived it. A missing component is never read as zero: zero is a
claim ("this joint carries no moment about z"), unknown is the absence of
one, and a check that silently treats the second as the first passes a
joint nobody sized. :func:`missing` names what is unknown so the
validate finding (``contract_component_unknown``) can say exactly which
component to resubmit — the spec's "a contract missing Mz is rejected
naming Mz".

Each component carries its own provenance entry
(:mod:`precis.design.provenance`). The spec's separate ``load_provenance``
enum (``user_stated | llm_assumed | derived_by_statics``) is accepted as a
shorthand and mapped through
:func:`precis.design.provenance.from_load_provenance` — one vocabulary,
not two.

**An insertion cone** on a block is the set of directions it may be
assembled along: a unit ``direction`` in the BLOCK's frame plus a
``half_angle`` (0 < h ≤ 90°). :func:`common_direction` intersects every
declared cone in the design frame; an empty intersection is the early
"single-direction assembly" rejection — a set of individually satisfiable
perpendicular clips that no one press stroke can close.

Pure: no store access, no import of the op table (the ops in
:mod:`precis_se.ops` call in here).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from precis.design import provenance as design_provenance

#: The six components of a contract's load set, in canonical order.
COMPONENTS: tuple[str, ...] = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")

#: Widest cone accepted: a hemisphere. Past it a cone stops being convex on
#: the sphere and "assemble from anywhere but straight up" is not a
#: constraint this check can intersect honestly.
MAX_HALF_ANGLE_DEG = 90.0

#: Angular slack for the intersection test, radians (about 0.006°).
_ANGLE_TOL_RAD = 1e-4


class ContractError(ValueError):
    """A malformed contract or insertion cone. Raised at write time."""


def merge_loads(
    current: Mapping[str, Any] | None,
    loads: Mapping[str, Any],
    *,
    load_provenance: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Overlay ``loads`` (component → number, or ``None`` to make that
    component unknown again) onto the stored contract ``current`` and
    return the new contract, or ``None`` when nothing is left.

    Provenance per component: an explicit ``provenance[comp]`` wins, else
    the ``load_provenance`` shorthand, else none. A component restated
    without any claim DROPS its old entry — the number changed, so the old
    claim no longer describes it. ``provenance`` may also name a component
    this call doesn't restate, to annotate a number already stored."""
    if not isinstance(loads, Mapping):
        raise ContractError(
            f"'loads' must be an object over {', '.join(COMPONENTS)}, got {loads!r}"
        )
    unknown = sorted(set(loads) - set(COMPONENTS))
    if unknown:
        raise ContractError(
            f"unknown contract component(s) {', '.join(unknown)} — the six are "
            f"{', '.join(COMPONENTS)} (forces N, moments N·m)"
        )
    shared: dict[str, Any] | None = None
    if load_provenance is not None:
        try:
            shared = design_provenance.from_load_provenance(str(load_provenance))
        except design_provenance.ProvenanceError as exc:
            raise ContractError(str(exc)) from exc
    explicit = _vet_component_provenance(provenance)
    stored = dict(current or {})
    values: dict[str, float] = dict(stored.get("loads") or {})
    claims: dict[str, dict[str, Any]] = dict(stored.get("provenance") or {})
    for comp, raw in loads.items():
        if raw is None:
            values.pop(comp, None)
            claims.pop(comp, None)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ContractError(
                f"contract component {comp} must be a number "
                f"({'N·m' if comp.startswith('M') else 'N'}), got {raw!r}"
            )
        if not math.isfinite(float(raw)):
            raise ContractError(
                f"contract component {comp} must be finite, got {raw!r}"
            )
        values[comp] = float(raw)
        claim = explicit.get(comp, shared)
        if claim is None:
            claims.pop(comp, None)
        else:
            claims[comp] = claim
    for comp, claim in explicit.items():
        if comp in loads:
            continue
        if comp not in values:
            raise ContractError(
                f"provenance names {comp}, but {comp} is unknown on this "
                "contract — state its value in 'loads' first"
            )
        claims[comp] = claim
    if not values and not claims:
        return None
    return {
        "loads": {c: values[c] for c in COMPONENTS if c in values},
        "provenance": {c: claims[c] for c in COMPONENTS if c in claims},
    }


def _vet_component_provenance(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ContractError(
            f"contract 'provenance' must be an object keyed by component, got {raw!r}"
        )
    unknown = sorted(set(raw) - set(COMPONENTS))
    if unknown:
        raise ContractError(
            f"provenance names unknown component(s) {', '.join(unknown)} — the "
            f"six are {', '.join(COMPONENTS)}"
        )
    try:
        return {
            comp: design_provenance.validate_entry(entry, param=comp)
            for comp, entry in raw.items()
        }
    except design_provenance.ProvenanceError as exc:
        raise ContractError(str(exc)) from exc


def missing(contract: Mapping[str, Any] | None) -> list[str]:
    """The components a contract leaves UNKNOWN, canonical order. A
    connect with no contract at all has not declared one yet, which is a
    different state — the caller decides whether that is a finding."""
    have = set(((contract or {}).get("loads") or {}).keys())
    return [c for c in COMPONENTS if c not in have]


def vet_cone(direction: Any, half_angle_deg: float) -> dict[str, Any]:
    """An insertion cone in its stored shape: ``{'direction': unit [x, y,
    z] (block frame), 'half_angle_rad': h}``."""
    try:
        vec = np.asarray([float(v) for v in direction], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            f"insertion 'direction' must be a 3-vector, got {direction!r}"
        ) from exc
    if vec.shape != (3,) or not np.all(np.isfinite(vec)):
        raise ContractError(
            f"insertion 'direction' must be a 3-vector, got {direction!r}"
        )
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        raise ContractError("insertion 'direction' must be non-zero")
    if not 0.0 < half_angle_deg <= MAX_HALF_ANGLE_DEG:
        raise ContractError(
            f"insertion 'half_angle' must be in (0°, {MAX_HALF_ANGLE_DEG:g}°], got "
            f"{half_angle_deg:g}° — 0 is a single direction (give a small "
            "angle for the tolerance you have); past a hemisphere the cone "
            "constrains nothing this check can intersect"
        )
    return {
        "direction": [float(v) / norm for v in vec],
        "half_angle_rad": math.radians(half_angle_deg),
    }


def common_direction(
    cones: Sequence[tuple[Sequence[float], float]],
) -> list[float] | None:
    """A unit direction inside every cone ``(axis, half_angle_rad)`` (axes
    in ONE shared frame), or ``None`` when their intersection is empty.

    Each cone is a spherical cap no wider than a hemisphere, so the
    intersection is convex on the sphere; when it is non-empty it contains
    either one of the axes or a point where two cap boundaries cross. The
    candidates are exactly those, and each is tested against every cone —
    exact up to :data:`_ANGLE_TOL_RAD`, no sampling."""
    if not cones:
        return None
    axes = [_unit(np.asarray(a, dtype=float)) for a, _ in cones]
    cos_h = [math.cos(h) for _, h in cones]
    candidates: list[np.ndarray] = list(axes)
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            candidates.extend(_boundary_crossings(axes[i], cos_h[i], axes[j], cos_h[j]))
    for x in candidates:
        if all(
            float(np.dot(x, a)) >= math.cos(h + _ANGLE_TOL_RAD)
            for a, (_, h) in zip(axes, cones, strict=True)
        ):
            return [float(v) for v in x]
    return None


def _unit(v: np.ndarray) -> np.ndarray:
    return v / float(np.linalg.norm(v))


def _boundary_crossings(
    a: np.ndarray, ca: float, b: np.ndarray, cb: float
) -> list[np.ndarray]:
    """Unit vectors x with x·a = ca and x·b = cb — where two cap
    boundaries cross (0, 1 or 2 points; none for parallel axes)."""
    n = np.cross(a, b)
    nn = float(np.dot(n, n))
    if nn < 1e-18:
        return []
    ab = float(np.dot(a, b))
    det = 1.0 - ab * ab
    alpha = (ca - cb * ab) / det
    beta = (cb - ca * ab) / det
    base = alpha * a + beta * b
    rest = 1.0 - float(np.dot(base, base))
    if rest < -1e-12:
        return []
    gamma = math.sqrt(max(rest, 0.0) / nn)
    return [_unit(base + gamma * n), _unit(base - gamma * n)]
