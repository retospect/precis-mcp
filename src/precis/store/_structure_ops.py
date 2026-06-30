"""Store ops for the ``structure`` kind (ADR 0043 §4/§12).

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
from typing import Any

import numpy as np
from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene

_LABEL_RE = re.compile(r"^a([A-Z][a-z]?)(\d+)$")


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

    # -- write -----------------------------------------------------------
    def _write_struct_card(
        self, conn: Connection, *, ref_id: int, card_text: str
    ) -> None:
        conn.execute(
            "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref_id,),
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s, 'agent', -1, 'card_combined', %s, %s)",
            (ref_id, card_text, Jsonb({})),
        )

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
            self._write_struct_card(conn, ref_id=ref.id, card_text=card_text)
        return ref, created

    # -- read ------------------------------------------------------------
    def structure_load(self, ref_id: int) -> tuple[Scene, dict[str, int]]:
        """Reconstruct the in-memory Scene + a ``{label: atom_id}`` map."""
        ref = self.get_ref(kind="structure", id=ref_id)
        meta = dict(ref.meta) if (ref is not None and ref.meta) else {}
        lattice = np.array(meta.get("lattice", np.eye(3) * 10.0), dtype=float)
        pbc = tuple(meta.get("pbc", (True, True, True)))
        scene = Scene(cell=Cell(lattice, pbc), label_hi=dict(meta.get("label_hi", {})))  # type: ignore[arg-type]
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
                    image=tuple(int(x) for x in (image or [0, 0, 0])),  # type: ignore[arg-type]
                )
            )
        return scene, handles

    def structure_version(self, ref_id: int) -> int:
        """Current design version (0 if absent)."""
        ref = self.get_ref(kind="structure", id=ref_id)
        if ref is None or not ref.meta:
            return 0
        return int(ref.meta.get("version", 0))

    def structure_list(self, *, limit: int = 50) -> list[Any]:
        """Live structure design refs, most-recent first."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'structure' "
                "AND deleted_at IS NULL ORDER BY ref_id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        out = []
        for (rid,) in rows:
            ref = self.get_ref(kind="structure", id=int(rid))
            if ref is not None:
                out.append(ref)
        return out

    # -- compute runs (ADR 0043 §9/§12) ----------------------------------
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
    ) -> int:
        """Record one compute pass + its per-step convergence curve. The curve
        is stored as ``struct_frames`` (energy/force per step); geometry frames
        are MD/NEB-only (§6.9). ``cache_key`` / ``structure_sha`` /
        ``final_geometry`` populate the §23.16 run-cube cache (NULL for the
        uncached ``clean`` rung). Returns the new ``struct_runs.id``."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO struct_runs "
                "(ref_id, fidelity, status, model, on_version, converged, "
                " n_steps, energy, max_force, max_disp, params, "
                " cache_key, structure_sha, final_geometry) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
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

    def structure_runs(self, ref_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        """A design's compute history, most-recent first (the fidelity ladder)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, fidelity, status, model, on_version, converged, "
                "n_steps, energy, max_force, max_disp, created_at "
                "FROM struct_runs WHERE ref_id = %s ORDER BY id DESC LIMIT %s",
                (ref_id, limit),
            ).fetchall()
        cols = [
            "id",
            "fidelity",
            "status",
            "model",
            "on_version",
            "converged",
            "n_steps",
            "energy",
            "max_force",
            "max_disp",
            "created_at",
        ]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def structure_find_cached_run(self, cache_key: str) -> dict[str, Any] | None:
        """Look a relax request up in the run-cube cache (ADR §23.16).

        Returns the newest ``succeeded`` run for ``cache_key`` — its scalar
        envelope, the relaxed ``final_geometry`` (so the caller can write it
        back with zero compute), and the per-step ``curve`` — or ``None`` on a
        miss. The partial index ``struct_runs_cache_idx`` makes this a single
        index probe."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT id, fidelity, model, converged, n_steps, energy, "
                "max_force, max_disp, final_geometry, structure_sha "
                "FROM struct_runs "
                "WHERE cache_key = %s AND status = 'succeeded' "
                "ORDER BY id DESC LIMIT 1",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            run_id = int(row[0])
            curve = [
                float(c[0])
                for c in conn.execute(
                    "SELECT max_force FROM struct_frames "
                    "WHERE run_id = %s AND max_force IS NOT NULL ORDER BY step",
                    (run_id,),
                ).fetchall()
            ]
        cols = [
            "id",
            "fidelity",
            "model",
            "converged",
            "n_steps",
            "energy",
            "max_force",
            "max_disp",
            "final_geometry",
            "structure_sha",
        ]
        out = dict(zip(cols, row, strict=True))
        out["curve"] = curve
        return out

    # -- delete ----------------------------------------------------------
    def structure_delete(self, ref_id: int) -> int:
        """Soft-delete a design: ref deleted, atoms/bonds retired, card dropped."""
        ver = self.structure_version(ref_id) + 1
        with self.tx() as conn:
            conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE ref_id = %s AND kind = 'structure' AND deleted_at IS NULL",
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
