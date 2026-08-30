"""Store ops for the ``structure`` kind.

Storage splits by what is a search target (the `cad`/`pcb` pattern):

- the **design** is a slug-addressed ``refs`` row (``kind='structure'``); the
  cell (lattice 3×3, pbc, version, per-element label high-water) lives on
  ``refs.meta``;
- it keeps **one** ``card_combined`` chunk (composition + intent) so
  ``search(kind='structure', q=…)`` works on intent — one vector per design;
- the **graph** lives in the dedicated ``struct_atoms`` / ``struct_bonds``
  tables — never embedded.

v1 save is retire-all-then-insert (the `cad` model): the graph is small, so a
rewrite is cheap; version-stamped *incremental* soft-delete is a later
refinement. Bonds reference atoms by ``id`` (FK integrity), so atoms insert
first and bonds map through a ``{label: id}`` lookup.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.insert_ref`` / ``self.get_ref``.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict, cast

import numpy as np
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.structure.cell import Cell, as_image3
from precis.structure.importers import ExternalId, ExternalRun
from precis.structure.measures import evaluate as _evaluate_measure
from precis.structure.scene import Atom, Bond, Measure, Scene


class StructRunRow(TypedDict):
    """One ``struct_runs`` row — the fidelity-ladder compute history
    (``structure_runs``)."""

    id: int
    fidelity: str
    status: str
    model: str | None
    on_version: int
    converged: bool
    n_steps: int
    energy: float | None
    max_force: float | None
    max_disp: float | None
    created_at: Any
    forces: dict[str, Any] | None
    charges: dict[str, Any] | None


class StructCachedRunRow(TypedDict):
    """A run-cube cache hit (``structure_find_cached_run``): the scalar
    envelope of the newest ``succeeded`` computed run for a cache key, plus
    its relaxed geometry and per-step convergence curve."""

    id: int
    fidelity: str
    model: str | None
    converged: bool
    n_steps: int
    energy: float | None
    max_force: float | None
    max_disp: float | None
    final_geometry: dict[str, Any] | None
    structure_sha: str | None
    forces: dict[str, Any] | None
    curve: list[float]


class StructForcesRow(TypedDict):
    id: int
    fidelity: str
    forces: dict[str, Any] | None


class StructForcesPayload(TypedDict):
    """``structure_run_forces``'s return shape — a per-atom force estimate
    for one run, unpacked from that run's raw ``forces`` jsonb blob."""

    run_id: int
    fidelity: str
    vectors: list[list[float]] | None
    labels: list[str] | None
    approx: bool
    source: str | None


_LABEL_RE = re.compile(r"^a([A-Z][a-z]?)(\d+)$")
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")

#: ``refs.meta`` keys ``structure_save`` itself computes fresh every call —
#: anything else (e.g. ``barrier``/``span``/``quest_harvested_upto``/
#: ``quest_autocatpath_harvested_upto``/``params`` stamped externally via
#: ``stamp_ref_meta``) must survive an edit, not be wholesale-replaced.
_STRUCTURE_OWNED_META_KEYS = frozenset(
    {"lattice", "pbc", "version", "label_hi", "description", "last_relax"}
)


def _import_slug(dataset: str, config_id: str) -> str:
    """A deterministic, human-legible design slug for a first-time import.

    Cosmetic only — the *identity* a ``structure_import`` re-import collapses
    on is the ``ref_identifiers`` row (``id_kind=dataset``, ``id_value=
    config_id``), not this slug. Non-alnum runs collapse to a single ``-``.
    """
    raw = f"{dataset}-{config_id}".strip().lower()
    return _SLUG_UNSAFE_RE.sub("-", raw).strip("-") or "ext"


def _label_hi(scene: Scene) -> dict[str, int]:
    """Per-element high-water mark over live atoms, merged with the seed."""
    hi = dict(scene.label_hi)
    for label in scene.atoms:
        m = _LABEL_RE.match(label)
        if m:
            el, n = m.group(1), int(m.group(2))
            hi[el] = max(hi.get(el, 0), n)
    return hi


class StructureMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    chunks: Any  # ChunkStore sub-store — the shared card_combined write
    find_ref_by_identifier: Any  # IdentifiersMixin — external-id collapse
    insert_ref_identifiers: Any  # IdentifiersMixin — external-id collapse

    def structure_save(
        self,
        *,
        slug: str,
        title: str,
        scene: Scene,
        version: int,
        card_text: str,
        description: str = "",
        relax_summary: dict[str, Any] | None = None,
    ) -> tuple[Any, bool]:
        """Create-or-replace a design from a Scene. Returns ``(ref, created)``."""
        existing = self.get_ref(kind="structure", id=slug)
        created = existing is None
        meta: dict[str, Any] = {
            "lattice": [list(map(float, row)) for row in scene.cell.lattice],
            "pbc": list(scene.cell.pbc),
            "version": version,
            "label_hi": _label_hi(scene),
        }
        if description:
            meta["description"] = description
        if relax_summary is not None:
            meta["last_relax"] = relax_summary
        with self.tx() as conn:
            if created:
                ref = self.insert_ref(
                    kind="structure", slug=slug, title=title, meta=meta, conn=conn
                )
            else:
                ref = existing
                preserved = {
                    k: v
                    for k, v in (existing.meta or {}).items()
                    if k not in _STRUCTURE_OWNED_META_KEYS
                }
                meta = {**preserved, **meta}
                conn.execute(
                    "UPDATE struct_atoms SET retired_version = %s "
                    "WHERE ref_id = %s AND retired_version IS NULL",
                    (version, ref.id),
                )
                conn.execute(
                    "UPDATE struct_bonds SET retired_version = %s "
                    "WHERE ref_id = %s AND retired_version IS NULL",
                    (version, ref.id),
                )
                # Markers persist across edits (no added_version — they are
                # re-evaluated, §6.8), so retire-and-reinsert the whole set to
                # refresh value_derived / verdict against the new geometry.
                conn.execute(
                    "UPDATE struct_measures SET retired_version = %s "
                    "WHERE ref_id = %s AND retired_version IS NULL",
                    (version, ref.id),
                )
                conn.execute(
                    "UPDATE refs SET title = %s, meta = %s WHERE ref_id = %s",
                    (title, Jsonb(meta), ref.id),
                )
            idmap: dict[str, int] = {}
            for atom in scene.atoms.values():
                row = conn.execute(
                    "INSERT INTO struct_atoms "
                    "(ref_id, label, element, fa, fb, fc, fixed, magmom, "
                    " oxidation, hybridization, added_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        ref.id,
                        atom.label,
                        atom.element,
                        float(atom.frac[0]),
                        float(atom.frac[1]),
                        float(atom.frac[2]),
                        atom.fixed,
                        atom.magmom,
                        atom.oxidation,
                        atom.hybridization,
                        version,
                    ),
                ).fetchone()
                idmap[atom.label] = int(row[0])
            for bond in scene.bonds:
                conn.execute(
                    "INSERT INTO struct_bonds "
                    "(ref_id, kind, bond_order, provenance, i, j, image, added_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        ref.id,
                        bond.kind,
                        bond.order,
                        bond.provenance,
                        idmap.get(bond.i),
                        idmap.get(bond.j),
                        list(bond.image),
                        version,
                    ),
                )
            self._write_measures(conn, ref_id=ref.id, scene=scene)
            self.chunks._replace_card_combined(conn, ref_id=ref.id, card_text=card_text)
        return ref, created

    def _write_measures(self, conn: Connection, *, ref_id: int, scene: Scene) -> None:
        """Insert the live eye/measure set, snapshotting each marker's derived
        value + verdict against the final geometry. Anchors are atom **labels**
        in the jsonb (stable identity), never row ids that an edit would orphan.
        """
        for m in scene.measures:
            value, verdict = _evaluate_measure(scene, m)
            conn.execute(
                "INSERT INTO struct_measures "
                "(ref_id, kind, direction, goal, strength, operands, embodiment, "
                ' "for", value_derived, verdict) '
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    m.kind,
                    m.direction,
                    Jsonb(m.goal) if m.goal is not None else None,
                    m.strength,
                    Jsonb({"atoms": m.operands}),
                    Jsonb({"name": m.name, "reach": m.reach}),
                    m.for_,
                    Jsonb(value),
                    verdict,
                ),
            )

    # -- external import -----------------------------------
    def structure_import(
        self,
        scene: Scene,
        run: ExternalRun,
        external_id: ExternalId,
    ) -> int:
        """The single write path all three external DFT library import ingest modes funnel
        through (on-demand hydrate, batch mirror, derivative anchor) —
        idempotent on ``external_id``, exactly the ``ref_identifiers``
        "an external ID collapses to one ref" discipline (AGENTS.md).

        Collapse key: a ``ref_identifiers`` row with ``id_kind=
        external_id.dataset``, ``id_value=external_id.config_id``. **First**
        import mints a fresh ``structure`` design (:meth:`structure_save`)
        under a deterministic-but-cosmetic slug, registers that identifier
        row, then inserts one ``struct_runs`` row (``provenance='external'``,
        §5). A **re-import** of the same ``(dataset, config_id)`` reuses that
        ref — the Scene is rewritten, not duplicated — and *updates* its one
        external run row in place rather than inserting a second one.

        External and computed rows for one design coexist as distinct rows:
        migration 0084 narrows ``struct_runs_cache_idx`` to
        ``provenance='computed'``, so this row can never answer — or be
        answered by — the compute-cache-fill path in ``structure_find_cached_run``.

        Returns the structure ref's ``ref_id``.
        """
        dataset = external_id.dataset.strip().lower()
        config_id = external_id.config_id.strip()
        if not dataset or not config_id:
            raise ValueError(
                "structure_import needs a non-empty ExternalId(dataset, config_id)"
            )
        existing_ref_id = self.find_ref_by_identifier(
            dataset, config_id, kind="structure"
        )
        if existing_ref_id is not None:
            existing = self.get_ref(kind="structure", id=existing_ref_id)
            assert existing is not None
            slug = str(existing.slug)
            title = existing.title or slug
            version = int((existing.meta or {}).get("version", 0)) + 1
        else:
            slug = _import_slug(dataset, config_id)
            title = f"{dataset}:{config_id}"
            version = 1
        card_text = (
            f"{title} (imported structure). Composition: "
            f"{''.join(f'{el}{n}' for el, n in sorted(scene.composition().items()))}; "
            f"source: {dataset} {config_id}."
        )
        ref, created = self.structure_save(
            slug=slug,
            title=title,
            scene=scene,
            version=version,
            card_text=card_text,
            description=f"imported from {dataset} (config {config_id})",
        )
        with self.tx() as conn:
            if created:
                self.insert_ref_identifiers(
                    ref.id, [(dataset, config_id, "import")], conn=conn
                )
            self._import_run_upsert(
                conn,
                ref_id=ref.id,
                run=run,
                on_version=version,
                dataset=dataset,
                config_id=config_id,
            )
        return int(ref.id)

    def _import_run_upsert(
        self,
        conn: Connection,
        *,
        ref_id: int,
        run: ExternalRun,
        on_version: int,
        dataset: str,
        config_id: str,
    ) -> int:
        """Insert-or-update the *one* external ``struct_runs`` row for
        ``ref_id`` — never a second one for a re-import of the same design.
        ``model``/``cache_key`` are labelled distinctly from a computed run
        (``external:<dataset>``) purely for legibility; the compute cache-fill
        path is already structurally excluded from ever matching an external
        row via the ``struct_runs_cache_idx`` provenance predicate (0084)."""
        model = f"external:{dataset}"
        cache_key = f"external:{dataset}:{config_id}"
        geometry = Jsonb(run.final_geometry) if run.final_geometry is not None else None
        method = Jsonb(run.method)
        row = conn.execute(
            "SELECT id FROM struct_runs "
            "WHERE ref_id = %s AND provenance = 'external' "
            "ORDER BY id DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
        if row is not None:
            run_id = int(row[0])
            conn.execute(
                "UPDATE struct_runs SET "
                "on_version = %s, energy = %s, max_force = %s, "
                "final_geometry = %s, method = %s, model = %s, cache_key = %s, "
                "status = 'succeeded', converged = TRUE "
                "WHERE id = %s",
                (
                    on_version,
                    run.energy,
                    run.max_force,
                    geometry,
                    method,
                    model,
                    cache_key,
                    run_id,
                ),
            )
            return run_id
        inserted = conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, status, model, on_version, converged, n_steps, "
            " energy, max_force, max_disp, params, cache_key, final_geometry, "
            " provenance, method) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                ref_id,
                "external",
                "succeeded",
                model,
                on_version,
                True,
                0,
                run.energy,
                run.max_force,
                None,
                Jsonb({}),
                cache_key,
                geometry,
                run.provenance,
                method,
            ),
        ).fetchone()
        assert inserted is not None
        return int(inserted[0])

    # -- read ------------------------------------------------------------
    def structure_load(self, ref_id: int) -> tuple[Scene, dict[str, int]]:
        """Reconstruct the in-memory Scene + a ``{label: atom_id}`` map."""
        ref = self.get_ref(kind="structure", id=ref_id)
        meta = dict(ref.meta) if (ref is not None and ref.meta) else {}
        lattice = np.array(meta.get("lattice", np.eye(3) * 10.0), dtype=float)
        pbc = tuple(meta.get("pbc", (True, True, True)))
        scene = Scene(cell=Cell(lattice, pbc), label_hi=dict(meta.get("label_hi", {})))
        with self.pool.connection() as conn:
            arows = conn.execute(
                "SELECT id, label, element, fa, fb, fc, fixed, magmom, oxidation, "
                "hybridization FROM struct_atoms "
                "WHERE ref_id = %s AND retired_version IS NULL ORDER BY id ASC",
                (ref_id,),
            ).fetchall()
            brows = conn.execute(
                "SELECT kind, bond_order, provenance, i, j, image FROM struct_bonds "
                "WHERE ref_id = %s AND retired_version IS NULL ORDER BY id ASC",
                (ref_id,),
            ).fetchall()
            mrows = conn.execute(
                'SELECT kind, direction, goal, strength, operands, embodiment, "for" '
                "FROM struct_measures "
                "WHERE ref_id = %s AND retired_version IS NULL ORDER BY id ASC",
                (ref_id,),
            ).fetchall()
        handles: dict[str, int] = {}
        id_to_label: dict[int, str] = {}
        for aid, label, element, fa, fb, fc, fixed, magmom, oxi, hyb in arows:
            scene.atoms[str(label)] = Atom(
                label=str(label),
                element=str(element),
                frac=np.array([float(fa), float(fb), float(fc)]),
                fixed=int(fixed),
                magmom=magmom,
                oxidation=oxi,
                hybridization=hyb,
            )
            handles[str(label)] = int(aid)
            id_to_label[int(aid)] = str(label)
        for kind, order, prov, i, j, image in brows:
            li, lj = id_to_label.get(i), id_to_label.get(j)
            if li is None or lj is None:
                continue
            scene.bonds.append(
                Bond(
                    i=li,
                    j=lj,
                    order=float(order),
                    kind=str(kind),
                    provenance=str(prov),
                    image=as_image3(image),
                )
            )
        for mkind, direction, goal, strength, operands, embodiment, for_ in mrows:
            emb = embodiment or {}
            reach = emb.get("reach")
            scene.measures.append(
                Measure(
                    kind=str(mkind),
                    operands=list((operands or {}).get("atoms", [])),
                    name=emb.get("name"),
                    reach=float(reach) if reach is not None else None,
                    direction=direction,
                    goal=goal,
                    strength=str(strength or "gauge"),
                    for_=for_,
                )
            )
        return scene, handles

    def structure_version(self, ref_id: int) -> int:
        """Current design version (0 if absent)."""
        ref = self.get_ref(kind="structure", id=ref_id)
        if ref is None or not ref.meta:
            return 0
        return int(ref.meta.get("version", 0))

    # -- compute runs ----------------------------------
    def structure_record_run(
        self,
        ref_id: int,
        *,
        fidelity: str,
        on_version: int,
        converged: bool,
        n_steps: int,
        max_disp: float,
        energy: float | None = None,
        max_force: float | None = None,
        model: str | None = None,
        curve: list[float] | None = None,
        status: str = "succeeded",
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        structure_sha: str | None = None,
        final_geometry: dict[str, Any] | None = None,
        forces: dict[str, Any] | None = None,
        charges: dict[str, Any] | None = None,
    ) -> int:
        """Record one compute pass + its per-step convergence curve. The curve
        is stored as ``struct_frames`` (energy/force per step); geometry frames
        are MD/NEB-only (§6.9). ``cache_key`` / ``structure_sha`` /
        ``final_geometry`` populate the §23.16 run-cube cache (NULL for the
        uncached ``clean`` rung). ``forces`` (gripe 161576) is the caller-built
        ``{"vectors": [[fx,fy,fz], ...], "approx": bool, "source": str}``
        payload, canonical-rank-indexed like ``final_geometry`` — NULL when no
        force estimate was available. ``charges`` is always NULL today (no
        backend produces partial charges yet); the param exists so a future
        charge-bearing rung has a column to write without another migration.
        Returns the new ``struct_runs.id``."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO struct_runs "
                "(ref_id, fidelity, status, model, on_version, converged, "
                " n_steps, energy, max_force, max_disp, params, "
                " cache_key, structure_sha, final_geometry, forces, charges) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    ref_id,
                    fidelity,
                    status,
                    model,
                    on_version,
                    converged,
                    n_steps,
                    energy,
                    max_force,
                    max_disp,
                    Jsonb(params or {}),
                    cache_key,
                    structure_sha,
                    Jsonb(final_geometry) if final_geometry is not None else None,
                    Jsonb(forces) if forces is not None else None,
                    Jsonb(charges) if charges is not None else None,
                ),
            ).fetchone()
            run_id = int(row[0])
            # the per-step curve is max_force (ml) or the max atomic move (clean);
            # either way a force-proxy, stored in max_force. Per-step energy is not
            # tracked for a plain relax (§6.9 — curve + final state, not every frame).
            for step, fval in enumerate(curve or [], start=1):
                conn.execute(
                    "INSERT INTO struct_frames (run_id, step, energy, max_force) "
                    "VALUES (%s,%s,%s,%s)",
                    (run_id, step, None, float(fval)),
                )
        return run_id

    def structure_runs(self, ref_id: int, *, limit: int = 20) -> list[StructRunRow]:
        """A design's compute history, most-recent first (the fidelity ladder).
        ``forces``/``charges`` (gripe 161576) ride along raw (jsonb-decoded
        dict/``None``) so a renderer can flag which runs carry a per-atom force
        estimate without a second query."""
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, fidelity, status, model, on_version, converged, "
                    "n_steps, energy, max_force, max_disp, created_at, forces, "
                    "charges FROM struct_runs "
                    "WHERE ref_id = %s ORDER BY id DESC LIMIT %s",
                    (ref_id, limit),
                )
                rows = cur.fetchall()
        return [cast(StructRunRow, r) for r in rows]

    def structure_find_cached_run(self, cache_key: str) -> StructCachedRunRow | None:
        """Look a relax request up in the run-cube cache.

        Returns the newest ``succeeded`` run for ``cache_key`` — its scalar
        envelope, the relaxed ``final_geometry`` (so the caller can write it
        back with zero compute), and the per-step ``curve`` — or ``None`` on a
        miss. The partial index ``struct_runs_cache_idx`` makes this a single
        index probe. ``provenance = 'computed'`` is explicit here (not just
        implied by the index predicate, 0084) — an index predicate only
        changes *how* a query is planned, never *what* it returns, so a
        compute-cache probe must filter it itself or an imported
        ``provenance='external'`` row (a different method
        fingerprint) could silently serve as a false hit for a computed
        relax request."""
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, fidelity, model, converged, n_steps, energy, "
                    "max_force, max_disp, final_geometry, structure_sha, forces "
                    "FROM struct_runs "
                    "WHERE cache_key = %s AND status = 'succeeded' "
                    "AND provenance = 'computed' "
                    "ORDER BY id DESC LIMIT 1",
                    (cache_key,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            run_id = int(row["id"])
            curve = [
                float(c[0])
                for c in conn.execute(
                    "SELECT max_force FROM struct_frames "
                    "WHERE run_id = %s AND max_force IS NOT NULL ORDER BY step",
                    (run_id,),
                ).fetchall()
            ]
        out = cast(dict[str, Any], row)
        out["curve"] = curve
        return cast(StructCachedRunRow, out)

    def structure_run_forces(
        self,
        ref_id: int,
        *,
        run_id: int | None = None,
        on_version: int | None = None,
    ) -> StructForcesPayload | None:
        """The stored per-atom force payload for one run (gripe 161576) —
        ``run_id`` pins a specific run (returned regardless of design
        version — an explicit pin always answers with *that* run's forces);
        omitted, the *latest* run carrying a non-null ``forces`` column wins
        (any fidelity, converged or not — a non-converged run's forces are
        still an informative strain signal), optionally restricted to
        ``on_version`` (FIX 2: a superseded design version's forces must
        never surface as if they were current — the caller passes the design's
        *current* version here for the no-``run_id`` "latest" lookup;
        ``on_version`` is ignored when ``run_id`` is given).

        Returns ``{"run_id", "fidelity", "vectors", "labels", "approx",
        "source"}`` (``vectors``/``labels`` may themselves be ``None`` when a
        *pinned* run recorded none), or ``None`` when ``run_id`` doesn't name
        a run on this design, or (with no ``run_id``) no matching run has
        ever recorded forces."""
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if run_id is not None:
                    cur.execute(
                        "SELECT id, fidelity, forces FROM struct_runs "
                        "WHERE id = %s AND ref_id = %s",
                        (run_id, ref_id),
                    )
                elif on_version is not None:
                    cur.execute(
                        "SELECT id, fidelity, forces FROM struct_runs "
                        "WHERE ref_id = %s AND forces IS NOT NULL AND on_version = %s "
                        "ORDER BY id DESC LIMIT 1",
                        (ref_id, on_version),
                    )
                else:
                    cur.execute(
                        "SELECT id, fidelity, forces FROM struct_runs "
                        "WHERE ref_id = %s AND forces IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1",
                        (ref_id,),
                    )
                row = cast(StructForcesRow, cur.fetchone())
        if row is None:
            return None
        blob = row["forces"] or {}
        return {
            "run_id": int(row["id"]),
            "fidelity": str(row["fidelity"]),
            "vectors": blob.get("vectors"),
            "labels": blob.get("labels"),
            "approx": bool(blob.get("approx", False)),
            "source": blob.get("source"),
        }

    # -- delete ----------------------------------------------------------
    def structure_delete(self, ref_id: int) -> int:
        """Soft-delete a design: ref deleted, atoms/bonds retired, card dropped."""
        ver = self.structure_version(ref_id) + 1
        with self.tx() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() "
                "WHERE ref_id = %s AND kind = 'structure' AND retired_at IS NULL",
                (ref_id,),
            )
            n = conn.execute(
                "UPDATE struct_atoms SET retired_version = %s "
                "WHERE ref_id = %s AND retired_version IS NULL",
                (ver, ref_id),
            ).rowcount
            conn.execute(
                "UPDATE struct_bonds SET retired_version = %s "
                "WHERE ref_id = %s AND retired_version IS NULL",
                (ver, ref_id),
            )
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
                (ref_id,),
            )
        return int(n)
