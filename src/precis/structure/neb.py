"""NEB edge helpers — params normalization, the content address and the
slab-first atom order (gr512126, slice 3).

Three pure pieces the ``struct_neb`` job and the ``mode='neb'`` mint both
need (``docs/backlog/neb-structure-builder.md`` §Compute placement):

- :func:`normalize_neb_params` — the engine knob set with spec defaults.
  Strict: an unknown key raises rather than silently dropping a typo'd
  knob. Request-context stamps (``quest_ref_id`` for the daily cap) are
  merged into the stored row's ``params`` by the caller *after* this —
  they are not engine params and never reach :func:`neb_cache_key`.
- :func:`neb_cache_key` — the content address ``sha256(IS sha · FS sha ·
  fidelity · model · engine params · code version)`` the
  ``struct_neb_runs.cache_key`` column stores. Direction is part of the
  address: ``IS → FS`` and ``FS → IS`` are different requests (the fwd/rev
  barrier pair swaps), so a reversed call is a miss, not a wrong hit.
- :func:`slab_labels` / :func:`slab_first_order` / :func:`neb_atoms` —
  autocatpath assumes slab atoms come first (``index < n_slab``). The job
  computes the order once from the IS, builds both ASE ``Atoms`` through
  it (the pair gates have already fixed that IS/FS share a label set), and
  maps image results back through the order's labels. The slab is
  ``ops._largest_metal_component`` — the largest connected metal component
  over combined adjacency, so a dopant swapped into the surface counts as
  slab (the dominant-element heuristic in ``preflight`` gets PdCu wrong).

Unit enclave (package docstring): Å-native.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .cache import structure_sha
from .ops import _combined_adjacency, _largest_metal_component
from .scene import Scene

#: Bump when the NEB call changes such that a cached band would differ
#: from a fresh run (a changed default, a changed write-back contract).
#: Rolling this forward retires every prior cache entry without touching
#: a row — same contract as :data:`cache.RELAX_CODE_VERSION`. The engine
#: build itself rides the separate ``engine`` component
#: (:func:`neb_engine_token`) — a precis autocatpath-pin bump re-keys
#: without a code change.
NEB_CODE_VERSION = "1"


def neb_engine_token() -> str:
    """The precis-side autocatpath engine token the cache key folds —
    ``PRECIS_AUTOCATPATH_VERSION`` env or the dist pin floor, resolved
    fresh on every call (the env is the test seam and the ops escape
    hatch). Strict-engine ruling (Reto 2026-10-25, gr517804): only
    current-engine results count, so an engine bump re-keys every edge
    and an older run is never a cache hit."""
    from precis.structure.engine_token import _autocatpath_engine_token

    return _autocatpath_engine_token()


#: The engine knob set + spec defaults (§Compute placement): 7 interior
#: images, k-start ``path_seeds=2``, ``fmax`` 0.05 eV/Å, ≤300 steps per
#: image, one retry, the autocatpath tether on, bfgs optimizer.
NEB_DEFAULTS: dict[str, Any] = {
    "images": 7,
    "path_seeds": 2,
    "fmax": 0.05,
    "max_steps": 300,
    "retries": 1,
    "tether": True,
    "optimizer": "bfgs",
}

_INT_KEYS = ("images", "path_seeds", "max_steps", "retries")
_FLOAT_KEYS = ("fmax",)


def _coerce_bool(key: str, val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str) and val.strip().lower() in ("true", "1", "false", "0"):
        return val.strip().lower() in ("true", "1")
    raise ValueError(f"neb params: {key!r} must be a boolean, got {val!r}")


def normalize_neb_params(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fill the engine knobs with spec defaults; coerce JSON-typed values.

    Unknown keys raise ``ValueError`` naming the key and the knob set —
    the handler maps that to ``BadInput`` with a ``next=``. Bounds are the
    ones the engine assumes: ``images``/``path_seeds``/``max_steps`` ≥ 1,
    ``retries`` ≥ 0, ``fmax`` > 0.
    """
    args = dict(args or {})
    unknown = sorted(set(args) - set(NEB_DEFAULTS))
    if unknown:
        raise ValueError(
            f"neb params: unknown key(s) {unknown} — engine knobs are "
            f"{sorted(NEB_DEFAULTS)}"
        )
    out: dict[str, Any] = {}
    for key, default in NEB_DEFAULTS.items():
        raw = args.get(key, default)
        try:
            if key in _INT_KEYS:
                val: Any = int(raw)
            elif key in _FLOAT_KEYS:
                val = float(raw)
            elif key == "tether":
                val = _coerce_bool(key, raw)
            else:
                val = str(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"neb params: {key!r} must be a {type(default).__name__}, got {raw!r}"
            ) from exc
        out[key] = val
    if out["images"] < 1:
        raise ValueError("neb params: 'images' must be ≥ 1 (interior images)")
    if out["path_seeds"] < 1:
        raise ValueError("neb params: 'path_seeds' must be ≥ 1")
    if out["max_steps"] < 1:
        raise ValueError("neb params: 'max_steps' must be ≥ 1")
    if out["retries"] < 0:
        raise ValueError("neb params: 'retries' must be ≥ 0")
    if out["fmax"] < 0.01:
        raise ValueError("neb params: 'fmax' must be ≥ 0.01 eV/Å")
    # GPU-time ceilings (gr512126 review #5): an unbounded knob mints an
    # unbounded GPU job — a 200-image × 100000-step call passed every gate
    # before these existed.
    if out["images"] > 16:
        raise ValueError(
            f"neb params: 'images' must be ≤ 16 (ceiling), got {out['images']}"
        )
    if out["path_seeds"] > 5:
        raise ValueError(
            f"neb params: 'path_seeds' must be ≤ 5 (ceiling), got {out['path_seeds']}"
        )
    if out["max_steps"] > 2000:
        raise ValueError(
            f"neb params: 'max_steps' must be ≤ 2000 (ceiling), got {out['max_steps']}"
        )
    if out["retries"] > 3:
        raise ValueError(
            f"neb params: 'retries' must be ≤ 3 (ceiling), got {out['retries']}"
        )
    return out


def neb_cache_key(
    is_scene: Scene,
    fs_scene: Scene,
    *,
    fidelity: str,
    model: str | None,
    params: Mapping[str, Any] | None = None,
    code_version: str = NEB_CODE_VERSION,
    engine: str | None = None,
) -> str:
    """The content address for one NEB edge run.

    ``params`` should be :func:`normalize_neb_params`'s output — the key
    filters to the engine knob set regardless, so request-context stamps
    merged into the stored row's ``params`` (e.g. ``quest_ref_id``) can
    never fork the address of an otherwise-identical request.

    ``engine`` is the precis-side autocatpath token
    (:func:`neb_engine_token` — resolved per call, so a pin/env bump
    re-keys every edge). The strict-engine ruling (gr517804) makes a run
    from an older engine never a hit; the stored row's ``engine`` column
    renders it ``stale, rerun on current``."""
    payload = {
        "is_sha": structure_sha(is_scene),
        "fs_sha": structure_sha(fs_scene),
        "fidelity": fidelity,
        "model": model,
        "params": {k: v for k, v in (params or {}).items() if k in NEB_DEFAULTS},
        "code_version": code_version,
        "engine": engine if engine is not None else neb_engine_token(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def slab_labels(scene: Scene) -> set[str]:
    """The slab label set — largest connected metal component over
    combined adjacency (the spec rule; dopant-safe). Empty for a
    molecule/cluster with no metal component."""
    return _largest_metal_component(scene, _combined_adjacency(scene))


def slab_first_order(scene: Scene) -> tuple[list[str], int]:
    """(label order, n_slab): slab labels first, then adsorbates, each
    block sorted for determinism. IS/FS share a label set (the pair
    gates), so the order computed on the IS applies to the FS verbatim."""
    slab = slab_labels(scene)
    order = sorted(slab) + sorted(set(scene.atoms) - slab)
    return order, len(slab)


def neb_atoms(scene: Scene, order: list[str]) -> Any:
    """Scene → ASE ``Atoms`` in ``order`` (from :func:`slab_first_order`),
    with ``FixAtoms`` over the fixed labels — the same whole-atom pin the
    relax rungs honour. ``order`` must cover the scene's atom set exactly
    (the pair gates' ``same atoms`` guarantee).

    Returns the bare ``Atoms``; the caller keeps ``order`` to map
    image/atom results back to labels.
    """
    from ase import Atoms
    from ase.constraints import FixAtoms

    if set(order) != set(scene.atoms):
        missing = sorted(set(scene.atoms) - set(order))
        extra = sorted(set(order) - set(scene.atoms))
        raise ValueError(
            f"neb_atoms: order does not cover the scene's atoms "
            f"(missing {missing}, extra {extra})"
        )
    atoms = Atoms(
        symbols=[scene.atoms[la].element for la in order],
        scaled_positions=[list(map(float, scene.atoms[la].frac)) for la in order],
        cell=scene.cell.lattice,
        pbc=scene.cell.pbc,
    )
    pinned = [i for i, la in enumerate(order) if scene.atoms[la].fixed]
    if pinned:
        atoms.set_constraint(FixAtoms(indices=pinned))
    return atoms
