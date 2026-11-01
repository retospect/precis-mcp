"""Native structure ingest (slice 1b) — each relaxed intermediate becomes a
first-class precis `structure` ref, linked back to its pathway.

Constructs precis's own `Scene`/`Cell`/`Atom` from an ASE `Atoms` (so no
precis-core change is needed — the bridge just uses the public structure
classes + `store.structure_save`). Slabs are metallic and periodic, so we
ingest **bond-free** (atoms + cell + the fixed-layer mask); coordination is
legible from the geometry without a declared bond graph.

Links use the symmetric `related-to` relation — it validates on every precis
version (a dedicated `pathway-node` relation is a later refinement, once the
live-relations validation is everywhere). The precise `{state → ref_id}` map is
also stored in the pathway meta, so lookup never depends on link semantics.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FIX_ALL = 7  # FIX_X | FIX_Y | FIX_Z — a frozen slab atom


def _slug(s: Any) -> str:
    return _SLUG_RE.sub("-", str(s).strip().lower()).strip("-") or "x"


def _slug_groups(states: Iterable[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for state in states:
        groups.setdefault(_slug(state), []).append(state)
    return groups


def colliding_states(states: Iterable[str]) -> dict[str, list[str]]:
    """States that shared one structure slug under the legacy ``_slug``
    (gr549179) — groups of ≥2 keyed by that slug; ``{}`` when nothing
    collided. The re-ingest report names the pairs that lost geometry."""
    return {b: n for b, n in _slug_groups(states).items() if len(n) > 1}


def state_slugs(states: Iterable[str]) -> dict[str, str]:
    """Per-state slug tails, injective over ONE pathway's raw state names
    (gr549179).

    ``_slug`` folds case and collapses every non-alnum run to ``-``, so a
    parked sink ``*~nh2oh`` and the adsorbed ``NH2OH`` shared slug
    ``nh2oh`` and one's ``structure_save`` replaced the other's geometry
    on the same ref (prod pw527720 mapped both to the clean slab). The
    fix is set-aware, so only pathway state lists that actually collide
    move:

    * a state whose legacy-slug group has one member keeps the legacy
      slug — a lone ``*~co`` stays ``co``, ``CO*`` stays ``co``,
      ``H2O(g)`` stays ``h2o-g`` — so every non-colliding pathway keeps
      resolving and re-ingesting in place;
    * in a group of ≥2, the member carrying no characters ``_slug``
      drops keeps the slug (``NH2OH`` over ``*~nh2oh`` — the adsorbed
      form is repaired in place on the collided ref); when zero or
      several members qualify, the first in sorted order keeps it;
    * every other member takes ``-<sha8(raw)>`` — a case-only twin like
      ``h2`` next to ``H2`` moves too.
    """
    out: dict[str, str] = {}
    for base, names in _slug_groups(states).items():
        if len(names) == 1:
            out[names[0]] = base
            continue
        clean = [n for n in names if str(n).strip().lower() == base]
        keeper = clean[0] if len(clean) == 1 else sorted(names)[0]
        for name in names:
            if name == keeper:
                out[name] = base
            else:
                digest = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:8]
                out[name] = f"{base}-{digest}"
    return out


def scene_from_ase(atoms: Any) -> Any:
    """ASE Atoms → precis Scene. Order-preserving (atoms keep their ASE order),
    fixed-layer mask carried from any FixAtoms constraint, bond-free."""
    import numpy as np

    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    lattice = np.asarray(atoms.cell, dtype=float)
    pbc_bits = [bool(x) for x in atoms.pbc]
    pbc: tuple[bool, bool, bool] = (pbc_bits[0], pbc_bits[1], pbc_bits[2])
    scaled = atoms.get_scaled_positions(wrap=False)

    fixed_idx: set[int] = set()
    for con in getattr(atoms, "constraints", None) or []:
        if type(con).__name__ == "FixAtoms":
            try:
                fixed_idx.update(int(i) for i in con.get_indices())
            except Exception:  # pragma: no cover - ASE version variance
                fixed_idx.update(int(i) for i in getattr(con, "index", []))

    scene_atoms: dict[str, Any] = {}
    label_hi: dict[str, int] = {}
    for i, sym in enumerate(atoms.get_chemical_symbols()):
        label_hi[sym] = label_hi.get(sym, 0) + 1
        label = f"a{sym}{label_hi[sym]}"
        scene_atoms[label] = Atom(
            label=label,
            element=sym,
            frac=np.asarray(scaled[i], dtype=float),
            fixed=_FIX_ALL if i in fixed_idx else 0,
        )
    return Scene(
        cell=Cell(lattice=lattice, pbc=pbc), atoms=scene_atoms, label_hi=label_hi
    )


def _read_extxyz(text: str) -> Any:
    from ase.io import read as ase_read

    return ase_read(io.StringIO(text), format="extxyz")


def _structures_key(structures_extxyz: dict[str, str]) -> str:
    """Content address of the relaxed structure set — the slab-discriminating
    half of the ingest slug. ``runner.content_key`` is slab-blind by design
    (an injected ``_prebuilt_slab`` never enters ``Config.to_dict``), so every
    candidate sharing a config+engine version collides on it and same-key
    pathways used to overwrite one shared set of structure refs (gr482121).
    The geometry itself is the only slab identity the artifact carries:
    identical structures address identically (idempotent re-runs still
    replace), any difference — dopant, placement, coverage — mints fresh
    refs instead of a silent overwrite."""
    h = hashlib.sha256()
    for state, xyz in sorted((structures_extxyz or {}).items()):
        h.update(state.encode("utf-8"))
        h.update(b"\x00")
        h.update(xyz.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:10]


def intermediate_slugs(key: str, structures_extxyz: dict[str, str]) -> dict[str, str]:
    """``{state → structure-ref slug}`` for one structure set — the same map
    ``ingest_intermediates`` writes under, exposed so the re-ingest dry-run
    names each ref before it exists."""
    kp = (key or "x")[:10]
    sk = _structures_key(structures_extxyz)
    return {
        state: f"pw-{kp}-{sk}-{tail}"
        for state, tail in state_slugs(structures_extxyz or {}).items()
    }


def ingest_intermediates(
    store: Store,
    pathway_ref_id: int,
    pathway_slug: str,
    key: str,
    structures_extxyz: dict[str, str],
) -> dict[str, int]:
    """Save each relaxed intermediate as a `structure` ref (slug keyed on the
    config's content key AND the structure-set fingerprint, so re-runs of the
    same geometry are idempotent and any different slab makes fresh refs;
    the per-state tail is injective over raw state names — gr549179),
    link it `related-to` the pathway, and return {state → ref_id}.
    Best-effort per intermediate — a bad geometry is skipped, not fatal."""
    out: dict[str, int] = {}
    slugs = intermediate_slugs(key, structures_extxyz)
    for state, xyz in (structures_extxyz or {}).items():
        try:
            scene = scene_from_ase(_read_extxyz(xyz))
        except Exception:
            continue
        ref, _created = store.structure_save(
            slug=slugs[state],
            title=f"{pathway_slug}: {state}",
            scene=scene,
            version=1,
            card_text=f"{state} — {len(scene.atoms)} atoms, autocatpath intermediate "
            f"of pathway {pathway_slug}",
            description=f"autocatpath intermediate '{state}' of pathway {pathway_slug}",
        )
        out[state] = ref.id
        try:
            store.add_link(
                src_ref_id=pathway_ref_id, dst_ref_id=ref.id, relation="related-to"
            )
        except Exception:
            pass  # duplicate link on re-run — fine
    return out


def seed_geometry(store: Store, pathway_ref_id: int) -> tuple[dict[str, str], str]:
    """A pathway's per-state relaxed extxyz re-collected from its succeeded
    ``autocatpath_seed`` jobs' ``meta.structures`` — the record that
    survived the gr549179 slug collision — as ``(structures_extxyz,
    content_key)``.

    The merge is the one ``runner.aggregate_seed_partials`` runs on
    ``partial["structures"]``: lowest-energy geometry per state across
    the seeds that collected any (only ``model_index == 0`` units write
    structures, so the energies sit on one potential's scale). Empty
    structures for a monolith-era pathway or seeds predating the field;
    the caller reports that as "nothing to re-ingest from"."""
    from .modes_job import seed_jobs

    best: dict[str, tuple[float, str]] = {}
    content_key = ""
    for _job_id, meta in seed_jobs(store, pathway_ref_id):
        if not content_key:
            content_key = str(meta.get("content_key") or "")
        for name, geo in (meta.get("structures") or {}).items():
            if not isinstance(geo, dict):
                continue
            energy, xyz = geo.get("energy"), geo.get("extxyz")
            if energy is None or not xyz:
                continue
            if name not in best or float(energy) < best[name][0]:
                best[name] = (float(energy), str(xyz))
    return {name: xyz for name, (_e, xyz) in best.items()}, content_key


@dataclasses.dataclass(frozen=True)
class ReingestResult:
    """What a ``reingest_pathway_structures`` pass did."""

    structure_refs: dict[str, int]  # the stamped map: new refs over prior
    written: int  # states ingested this pass
    input_states: int  # states collected from seed jobs
    skipped: tuple[str, ...]  # input states whose geometry failed to parse


def reingest_pathway_structures(store: Store, ref: Any) -> ReingestResult:
    """Operator repair for gr549179: re-write one pathway's intermediate
    ``structure`` refs from the geometries still on its seed jobs, then
    re-stamp ``meta.structure_refs``.

    The stamped map is the new ``{state → ref_id}`` over the prior one:
    a state whose seed geometry fails to parse keeps its existing entry
    rather than dropping out of the map. Adsorbed names keep their
    legacy slug, so the ref a ``*~`` collision overwrote is repaired in
    place; the sink forms mint fresh refs under their now-distinct
    slugs. ``structure_save`` is create-or-replace, so the pass is
    idempotent. ``input_states == 0`` (a monolith-era pathway keeps no
    per-state geometry — re-run it instead) writes nothing and reports
    zero counts."""
    # Re-read the ref: the caller's object may carry a stale meta
    # snapshot (structure_refs was stamped after it was fetched).
    fresh = store.get_ref(kind="pathway", id=int(ref.id))
    meta = dict((fresh.meta if fresh is not None else getattr(ref, "meta", None)) or {})
    structures, content_key = seed_geometry(store, int(ref.id))
    prior = {str(s): int(r) for s, r in (meta.get("structure_refs") or {}).items()}
    if not structures:
        return ReingestResult(prior, 0, 0, ())
    content_key = str(meta.get("content_key") or "") or content_key
    out = ingest_intermediates(
        store,
        int(ref.id),
        str(ref.slug or ref.id),
        content_key,
        structures,
    )
    merged = {**prior, **out}
    store.stamp_ref_meta(
        int(ref.id),
        {"structure_refs": merged, "n_structures": len(structures)},
    )
    skipped = tuple(s for s in structures if s not in out)
    return ReingestResult(merged, len(out), len(structures), skipped)
