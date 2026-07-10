"""StructureHandler — the atomistic cell + bond-graph kind (ADR 0043).

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

The relaxer/DFT and file export (CIF/POSCAR/XYZ) are rented backends added in
later increments. See ``precis-structure-help``.
"""

from __future__ import annotations

import json
import os
import re
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
from precis.structure.relax import RelaxResult
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

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
_VIEWS = (*_PROBE_VIEWS, *_NAV_VIEWS, "runs", "markers", *_EXPORT_VIEWS)


@dataclass
class _NeedsDispatch:
    """An energy-rung relax that missed the §23.16 cache and has no local
    backend — it must run on the GPU node as a ``struct_relax`` job (§23.12).
    Carries everything the dispatch needs: the content address + the staged
    input geometry + the canonical / POSCAR-row orderings for the write-back.

    ``requester_id`` is the optional todo that *asked for* this relax and
    wants to block on it (ADR 0044 compute lane). The job parents on the
    structure regardless; when a requester is named, the dispatch also
    writes a ``requested`` link + a ``derived_job_succeeded`` auto_check so
    the todo closes on completion / bubbles on failure."""

    fidelity: str
    model: str
    steps: int
    cache_key: str
    structure_sha: str
    order: list[str]
    poscar: str
    poscar_labels: list[str]
    requester_id: int | None


def _poscar_row_labels(scene: Scene) -> list[str]:
    """Atom labels in the row order ``export.to_poscar`` emits (element-grouped),
    so a relaxed POSCAR's rows map back to labels → canonical rank."""
    order, groups = export._grouped(scene)
    return [a.label for el in order for a in groups[el]]


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
        return Cell(np.array(spec["lattice"], dtype=float), pbc)  # type: ignore[arg-type]
    try:
        return Cell.from_lengths_angles(
            float(spec["a"]),
            float(spec["b"]),
            float(spec["c"]),
            float(spec.get("alpha", 90.0)),
            float(spec.get("beta", 90.0)),
            float(spec.get("gamma", 90.0)),
            pbc,  # type: ignore[arg-type]
        )
    except KeyError as exc:
        raise BadInput(
            f"cell needs 'lattice' or a/b/c (missing {exc})",
            next="cell={'a':8.4,'b':8.4,'c':24,'pbc':[true,true,false]}",
        ) from None


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


class StructureHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="structure",
        title="Structure",
        description=(
            "Atomistic cell + bond-graph design (ADR 0043). put creates/replaces "
            "from JSON {cell:{a,b,c,pbc}|{lattice,pbc}, ops:[...]}; edit applies "
            "more ops (set_cell/add_atom/set_element/vacancy/displace/add_bond/"
            "remove_bond/constrain, plus eye/measure/unmark/remove_measure "
            "markers); get lists designs, shows a TOC (id=slug), or probes "
            "(view='atom|neighborhood|bonds|find|validate|markers', args={...}); "
            "link relates designs (rel='derived-from'); delete soft-retires. "
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
    def put(  # type: ignore[override]
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
        payload = _payload(text, args)
        if "cell" not in payload:
            raise BadInput("put(kind='structure') payload needs a 'cell'")
        scene = Scene(cell=_build_cell(payload["cell"]))
        res = self._run_ops(scene, payload.get("ops", []))
        if isinstance(res, _NeedsDispatch):
            relax_result: RelaxResult | None = None
            dispatch: _NeedsDispatch | None = res
        else:
            relax_result, dispatch = res, None
        relax_summary = self._relax_summary(relax_result)
        existing = self.store.get_ref(kind="structure", id=slug)
        version = (int(existing.meta.get("version", 0)) + 1) if existing else 1
        desc = str(payload.get("description") or "").strip()
        ttl = (title or slug).strip() or slug
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
    def edit(  # type: ignore[override]
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
            relax_result: RelaxResult | None = None
            dispatch: _NeedsDispatch | None = res
        else:
            relax_result, dispatch = res, None
        relax_summary = self._relax_summary(relax_result)
        version = self.store.structure_version(ref.id) + 1
        desc = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
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
        ``derived-from`` the parent (ADR 0043 bundle — the instruction-box Apply).

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
    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
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
        if view in _EXPORT_VIEWS:
            return self._render_export(view, scene, str(ref.slug or id))
        if view in _NAV_VIEWS:
            return self._render_nav(view, scene, args or {})
        if view not in _PROBE_VIEWS:
            raise BadInput(
                f"unknown structure view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the TOC",
            )
        return self._render_probe(view, scene, args or {})

    def _render_export(self, view: str, scene: Scene, slug: str) -> Response:
        """Emit the geometry as a file format. POSCAR/extXYZ are pure; CIF
        needs ASE (the optional ``[dft]`` extra) — a missing one is Unsupported
        with an install hint, not a crash (ADR 0043 §13)."""
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

    def _render_runs(self, ref: Any) -> Response:
        """The design's compute history — the fidelity ladder over time (§9)."""
        runs = self.store.structure_runs(ref.id)
        if not runs:
            return Response(
                body=f"# {ref.slug}: no compute runs yet\n\n"
                "Next: edit(kind='structure', id='"
                + str(ref.slug)
                + "', ops=[{'op':'relax','fidelity':'clean'}])"
            )
        rows = [
            {
                "run": f"r{r['id']}",
                "fidelity": r["fidelity"],
                "status": r["status"],
                "conv": "yes" if r["converged"] else "no",
                "steps": str(r["n_steps"]),
                "energy": "—" if r["energy"] is None else f"{r['energy']:.4f}",
                "max_force": "—" if r["max_force"] is None else f"{r['max_force']:.4f}",
                "v": str(r["on_version"]),
            }
            for r in runs
        ]
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

    # ── link ─────────────────────────────────────────────────────────
    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove a link from this design to another ref — e.g. a derived
        design → its parent (``rel='derived-from'``, target ``structure:<slug>``).

        The reserved virtual ``rel='parent'`` is folder placement
        (ADR 0045) — a ``refs.parent_id`` write, never a stored link.
        Derivation (``derived-from``) and placement are orthogonal axes.
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
        n_added, n_removed = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
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
    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
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

        # Cache-first for the expensive energy rungs (ADR §23.16). The rung-0
        # ``clean`` repair is instant + pure + energy-free, so it is never
        # cached — it just runs. The key is over the *input* geometry (this
        # scene, after graph ops, before relax mutates it), so capture the
        # content address + canonical order now.
        cached_rung = fidelity not in ("clean", "0")
        cache_key = structure_sha = None
        order: list[str] = []
        if cached_rung:
            params = {"steps": steps}
            cache_key = relax_cache.run_cache_key(
                scene, fidelity=fidelity, model=model, params=params
            )
            structure_sha = relax_cache.structure_sha(scene)
            order = relax_cache.canonical_order(scene)
            hit = self.store.structure_find_cached_run(cache_key)
            if hit is not None:
                geom = hit.get("final_geometry")
                if geom:
                    relax_cache.apply_geometry(scene, geom)
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
                )

        try:
            res = run_relax(scene, fidelity=fidelity, steps=steps, model=model)
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
            return _NeedsDispatch(
                fidelity=fidelity,
                model=model,
                steps=steps,
                cache_key=cache_key,
                structure_sha=structure_sha or "",
                order=order,
                poscar=export.to_poscar(scene),
                poscar_labels=_poscar_row_labels(scene),
                # Optional requesting todo (compute lane, ADR 0044). Accept
                # the clear ``requested_by`` key; tolerate the legacy
                # ``parent_id`` spelling from before the lane split.
                requester_id=_as_int_or_none(
                    ro.get("requested_by", ro.get("parent_id"))
                ),
            )

        # Cache miss: stamp the content address + relaxed geometry so the next
        # identical request — on this design or any other sharing the input —
        # is a zero-compute hit.
        if cached_rung and res.converged:
            res.cache_key = cache_key
            res.structure_sha = structure_sha
            res.final_geometry = relax_cache.serialize_geometry(scene, order)
        return res

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
            params={"cached": True} if res.from_cache else None,
        )

    def _dispatch_relax(self, ref: Any, version: int, nd: _NeedsDispatch) -> Response:
        """Mint a ``struct_relax`` job for an energy rung with no local backend
        (ADR 0043 §23.12, ADR 0044). The relaxed geometry lands in the §23.16
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
        from precis.handlers.job import JobHandler

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
            "cache_key": nd.cache_key,
            "structure_sha": nd.structure_sha,
            "order": nd.order,
            "poscar_labels": nd.poscar_labels,
            "poscar": nd.poscar,
            # Pin to the GPU node so its worker claims the job (§23 #3) — the
            # stager + container then share one host's NFS view.
            "target_node": os.environ.get("PRECIS_DFT_NODE", "spark"),
        }
        job_resp = JobHandler(hub=hub).put(
            job_type="struct_relax",
            executor="ssh_node",
            # The build subject owns the job (compute lane, ADR 0044).
            parent_id=ref.id,
            params=params,
            # Collapse re-submits of the *same* relax onto one in-flight job.
            idem_key=f"struct_relax:{nd.cache_key}",
        )
        note = ""
        if nd.requester_id is not None:
            self._wire_requester(nd.requester_id, job_resp.body)
            note = f" (todo #{nd.requester_id} will block on it)"
        return Response(
            body=(
                f"# relax[{nd.fidelity}] dispatched to the GPU node{note}\n\n"
                f"{job_resp.body}\n\n"
                f"The run lands in the cache on completion. "
                f"Poll: get(kind='structure', id='{slug}', view='runs')."
            )
        )

    def _wire_requester(self, requester_id: int, job_resp_body: str) -> None:
        """Link the requesting todo to the just-minted job and arm its wait.

        Writes ``requester --requested--> job`` (the edge the
        ``derived_job_succeeded`` evaluator + the failure-bubble follow), then
        injects that evaluator as the todo's ``auto_check`` when it has none —
        mirroring how ``dispatch`` arms ``child_job_succeeded`` for the intent
        lane. A todo that already carries a deliberate auto_check is left
        alone. Idempotent on both writes."""
        m = re.search(r"id=(\d+)", job_resp_body)
        if m is None:  # defensive — put always reports the id
            return
        job_id = int(m.group(1))
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
        rows = []
        for label, atom in scene.atoms.items():
            rows.append(
                {
                    "atom": f"{handle}#{label}",
                    "element": atom.element,
                    "frac": ",".join(f"{x:.3f}" for x in atom.frac),
                    "coord": probe.coordination(scene, label),
                    "fixed": "yes" if atom.fixed else "no",
                }
            )
        body = (
            head
            + "\n"
            + render_agent_table(
                rows, schema=["atom", "element", "frac", "coord", "fixed"]
            )
        )
        return Response(body=body)

    def _render_probe(self, view: str, scene: Scene, args: dict[str, Any]) -> Response:
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
    def search(  # type: ignore[override]
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
        return self.store.search_blocks(
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
        self.store.bump_salience([b.id for b, _r, _s in triples])
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
