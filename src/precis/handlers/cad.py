"""CadHandler — the parametric solid-model kind (ADR 0041).

A ``cad`` design is a slug-addressed ref whose body is a flat node set
(one ``cad_node`` chunk per element). The agent authors it as a small
text design language (:mod:`precis.cad.scene`), reads its node tree, and
*probes* it analytically — never a mesh. The verbs map straight onto the
seven-verb surface (no new verbs):

- ``put``    — create / replace a design (``id=`` slug, ``text=`` source).
- ``get``    — list designs (no id), a design's node tree (``id=slug``), a
  single node (``id='ca<chunk_id>'``), or a probe / analysis
  (``view='ray'|'point'|'arc'|'section'|'clearance'|'connectivity'|'dof'|
  'volume'`` with ``args=``). ``connectivity`` answers what-touches-what,
  path-between-parts, and the one-connected-solid truism.
- ``search`` — over design names + descriptions.
- ``delete`` — soft-retire a whole design.

See ``precis-cad-help``.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from precis.cad.bulk import volume as cad_volume
from precis.cad.export import (
    ExportError,
    export_mesh,
    export_step,
    manifold_available,
    step_available,
    to_openscad,
)
from precis.cad.probe import (
    probe_arc,
    probe_point,
    probe_ray,
    probe_section_z,
)
from precis.cad.relate import clearance as cad_clearance
from precis.cad.relate import connectivity as cad_connectivity
from precis.cad.relate import translational_dof
from precis.cad.scene import SceneError, build_design, parse_source
from precis.cad.vec import Vec3, vec3
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import render_agent_table
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)

_PROBE_VIEWS = (
    "ray",
    "point",
    "arc",
    "section",
    "clearance",
    "connectivity",
    "dof",
    "volume",
)
_EXPORT_VIEWS = ("scad", "stl", "3mf", "step")
_VIEWS = (*_PROBE_VIEWS, *_EXPORT_VIEWS)


def _vec(args: dict[str, Any], key: str) -> Vec3:
    raw = args.get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise BadInput(
            f"args.{key} must be a 3-number list [x,y,z]",
            next=f"get(kind='cad', id='<slug>', view='ray', args={{'{key}': [0,0,0]}})",
        )
    try:
        return vec3(float(raw[0]), float(raw[1]), float(raw[2]))
    except (TypeError, ValueError):
        raise BadInput(f"args.{key} must be three numbers") from None


class CadHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="cad",
        title="CAD",
        description=(
            "Parametric solid-model design (ADR 0041). put creates/replaces a "
            "design from a text source (one node per line: '<name> <add|cut|"
            "intersect> <config> [@x,y,z] [rot:..] [polar:nNrR|linear:..]', "
            "config e.g. cyl:r3h12 box:w40d20h10); get lists designs, shows a "
            "design's node tree (id=slug), one node (id='ca<id>'), or probes "
            "analytically (view='ray|point|arc|section|clearance|connectivity|"
            "dof|volume', args={...}; connectivity: what touches what, path "
            "a→b, is-it-one-solid); search over names; delete soft-retires. "
            "Postgres-"
            "canonical, no meshing in the design loop. See precis-cad-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        supports_search_hits=True,
        supports_delete=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        role="artifact",
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("cad: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── link: placement only (ADR 0045) ─────────────────────────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Folder placement via the reserved virtual ``rel='parent'``.

        CAD designs have no stored-link surface (yet) — the only
        accepted relation is ``parent``, a ``refs.parent_id`` write
        into a ``kind='folder'`` container (ADR 0045).
        """
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="cad", id=str(id).strip())
            return place_ref(self.store, kind="cad", ref=ref, target=target, mode=mode)
        raise BadInput(
            "cad link supports only rel='parent' (folder placement)",
            next=(
                "link(kind='cad', id='<slug>', target='folder:N', "
                "rel='parent') places; mode='remove' unfiles"
            ),
        )

    # ── put ──────────────────────────────────────────────────────────
    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='cad') requires id= (the design slug)",
                next="put(kind='cad', id='flange', text='plate add cyl:r25h8')",
            )
        slug = str(id).strip()
        if text is None or not str(text).strip():
            raise BadInput(
                "put(kind='cad') requires text= (the design source)",
                next=(
                    "put(kind='cad', id='flange', text='''\\n"
                    "plate    add cyl:r25h8\\n"
                    "hub_bore cut cyl:r8h10 @0,0,-1\\n''')"
                ),
            )
        try:
            spec = parse_source(str(text))
        except SceneError as exc:
            raise BadInput(f"cad source error: {exc}") from exc
        if not spec.nodes:
            raise BadInput("cad design has no nodes")
        # Build eagerly so a bad config / geometry surfaces on put.
        try:
            design = build_design(spec)
        except Exception as exc:  # kernel build error
            raise BadInput(f"cad build error: {exc}") from exc

        ttl = (title or slug).strip() or slug
        ref, created, n = self.store.cad_save(
            slug=slug,
            title=ttl,
            spec=spec,
            card_text=self._card_text(ttl, spec, design),
        )
        _spec2, handles = self.store.cad_load(ref.id)
        verb = "created" if created else "updated"
        head = (
            f"# {slug} — {verb}: {len(spec.components)} part(s), {n} node(s)"
            f"{self._interference_note(design, spec)}"
            f"{self._connectivity_note(design, spec)}"
        )
        return Response(body=head + "\n" + self._tree_table(spec, handles))

    # ── derive ───────────────────────────────────────────────────────
    def derive(  # type: ignore[override]
        self,
        *,
        id: str | int,
        to: str,
        text: str,
        title: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Branch a **new** design ``to`` from ``id`` with ``text`` as its source,
        linked ``derived-from`` the parent (the web editor's "Apply" step).

        CAD is authored as whole text (not incremental ops, unlike structure), so
        a derivative is just a fresh design under a new slug plus the lineage
        link. The parent is untouched (delete it separately if you want)."""
        parent = resolve_live_slug_ref(self.store, kind="cad", id=str(id).strip())
        to_slug = str(to).strip()
        if not to_slug:
            raise BadInput("derive requires to= (the new design slug)")
        if self.store.get_ref(kind="cad", id=to_slug) is not None:
            raise BadInput(
                f"design {to_slug!r} already exists",
                next="pick a fresh slug for the derived design",
            )
        if text is None or not str(text).strip():
            raise BadInput("derive requires text= (the new design source)")
        try:
            spec = parse_source(str(text))
        except SceneError as exc:
            raise BadInput(f"cad source error: {exc}") from exc
        if not spec.nodes:
            raise BadInput("derived cad design has no nodes")
        try:
            design = build_design(spec)
        except Exception as exc:  # kernel build error
            raise BadInput(f"cad build error: {exc}") from exc

        ttl = (title or to_slug).strip() or to_slug
        ref, _created, n = self.store.cad_save(
            slug=to_slug,
            title=ttl,
            spec=spec,
            card_text=self._card_text(ttl, spec, design),
        )
        # lineage: the derived design points back to its parent
        self.store.add_link(
            src_ref_id=ref.id, dst_ref_id=parent.id, relation="derived-from"
        )
        _spec2, handles = self.store.cad_load(ref.id)
        head = (
            f"# {to_slug} — derived from {parent.slug}: "
            f"{len(spec.components)} part(s), {n} node(s)"
            f"{self._interference_note(design, spec)}"
            f"{self._connectivity_note(design, spec)}"
        )
        return Response(body=head + "\n" + self._tree_table(spec, handles))

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
        s = str(id).strip()
        parsed = handle_registry.parse(s)
        if parsed is not None and parsed[0] == "cad" and parsed[1]:
            return self._render_node(int(parsed[2]))

        ref = resolve_live_slug_ref(self.store, kind="cad", id=s)
        spec, handles = self.store.cad_load(ref.id)
        if view is None:
            head = (
                f"# {ref.slug} — {len(spec.components)} part(s), "
                f"{len(spec.nodes)} node(s)"
            )
            return Response(body=head + "\n" + self._tree_table(spec, handles))
        if view == "scad":
            return Response(body=to_openscad(spec, name=str(ref.slug or s)))
        if view in ("stl", "3mf", "step"):
            return self._render_export(spec, str(ref.slug or s), view, args or {})
        if view not in _PROBE_VIEWS:
            raise BadInput(
                f"unknown cad view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the node tree",
            )
        design = build_design(spec)
        return self._render_probe(view, design, spec, args or {})

    # ── export ───────────────────────────────────────────────────────
    def _render_export(
        self, spec: Any, slug: str, fmt: str, args: dict[str, Any]
    ) -> Response:
        """Write the design to a file: ``stl``/``3mf`` (mesh, via manifold3d)
        or ``step`` (exact B-rep, via OpenCASCADE). Path defaults to a temp
        file named after the design; override with ``args={'path': '...'}``.
        Each backend is an optional extra — a missing one is reported as
        Unsupported with the install hint, not a crash."""
        if fmt in ("stl", "3mf") and not manifold_available():
            raise Unsupported(
                f"{fmt.upper()} export needs the manifold3d backend",
                next="install it:  pip install 'precis-mcp[cad-export]'",
            )
        if fmt == "step" and not step_available():
            raise Unsupported(
                "STEP export needs the OpenCASCADE backend",
                next="install it:  pip install 'precis-mcp[cad-step]'",
            )
        raw = args.get("path")
        out = (
            Path(str(raw)).expanduser()
            if raw
            else Path(tempfile.gettempdir()) / f"{slug}.{fmt}"
        )
        try:
            path = (
                export_step(spec, out)
                if fmt == "step"
                else export_mesh(spec, out, fmt=fmt)
            )
        except ExportError as exc:
            raise BadInput(str(exc)) from exc
        size = path.stat().st_size
        kernel = "OpenCASCADE B-rep" if fmt == "step" else "manifold3d mesh"
        return Response(
            body=(
                f"# exported {slug} → {fmt.upper()} ({kernel})\n"
                f"{path}  ({size:,} bytes)\n\n"
                "Next: open it in a slicer / CAD app, or pass "
                "args={'path': '/abs/out."
                f"{fmt}'}} to choose the location."
            )
        )

    # ── delete ───────────────────────────────────────────────────────
    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='cad') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="cad", id=str(id).strip())
        n = self.store.cad_delete(ref.id)
        return Response(body=f"retired cad design {ref.slug} ({n} chunk(s))")

    # ── search ───────────────────────────────────────────────────────
    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        """Find designs by intent. Searches each design's one summary card
        (title + components + node names + description/use); ``mode=`` is the
        usual lexical / semantic / hybrid axis."""
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='cad') requires q=",
                next="search(kind='cad', q='6-bolt flange')",
            )
        q = str(q)
        triples = self._card_search(q, query_vec=None, mode=mode, page_size=page_size)
        if not triples:
            return Response(
                body=f"no cad designs match {q!r}\n\n"
                "Next: widen with mode='semantic', or add a `desc:`/`use:` line "
                "to a design so it's findable by purpose."
            )
        rows = []
        for _block, ref, _score in triples:
            handle = handle_registry.try_format("cad", ref.id, chunk=False) or "—"
            rows.append({"handle": handle, "design": ref.slug, "title": ref.title})
        return Response(
            body=f"# {len(triples)} cad design(s) for {q!r}\n"
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
        """The shared leg behind search() and search_hits(): a fused search
        over the per-design ``card_combined`` chunks (ADR 0041 Amendment 1)."""
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
            kind="cad",
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
        """Design-level hits for the cross-kind merge (kind='*'). One hit per
        design (the summary card); the handle is the design ref ``cd<id>``,
        not a node."""
        triples = self._card_search(
            q, query_vec=query_vec, mode=mode, page_size=page_size
        )
        self.store.bump_salience([b.id for b, _r, _s in triples])
        out: list[SearchHit] = []
        for block, ref, score in triples:
            text = (getattr(block, "text", "") or "").strip()
            preview = text if len(text) <= 200 else text[:199].rstrip() + "\u2026"
            out.append(
                SearchHit(
                    score=float(score),
                    kind="cad",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"cad:{ref.slug or ref.id}",
                    uhandle=handle_registry.try_format("cad", ref.id, chunk=False),
                )
            )
        return out

    # ── rendering helpers ────────────────────────────────────────────
    def _render_list(self) -> Response:
        designs = self.store.list_refs(kind="cad", order_by="id_desc", limit=50)
        if not designs:
            return Response(
                body="no cad designs yet\n\nNext: put(kind='cad', id='flange', "
                "text='plate add cyl:r25h8')"
            )
        rows = [{"design": r.slug, "title": r.title} for r in designs]
        return Response(
            body=f"# {len(designs)} cad design(s)\n"
            + render_agent_table(rows, schema=["design", "title"])
        )

    def _pose(self, node: Any) -> str:
        if node.pattern is not None:
            p = node.pattern
            if p["kind"] == "polar":
                return f"polar n{int(p['n'])} r{p['r']:g} z"
            return f"linear n{int(p['n'])} d({p['dx']:g},{p['dy']:g},{p['dz']:g})"
        x, y, z = node.loc
        pose = f"@{x:g},{y:g},{z:g}"
        if node.rot != (0.0, 0.0, 0.0):
            pose += f" rot{node.rot}"
        return pose

    def _tree_table(self, spec: Any, handles: dict[str, int]) -> str:
        rows = []
        for node in spec.nodes:
            cid = handles.get(node.name)
            handle = (
                handle_registry.format_handle("cad", cid, chunk=True) if cid else "—"
            )
            rows.append(
                {
                    "handle": handle,
                    "name": node.name,
                    "part": node.component,
                    "op": node.op,
                    "config": node.config,
                    "pose": self._pose(node),
                }
            )
        return render_agent_table(
            rows, schema=["handle", "name", "part", "op", "config", "pose"]
        )

    def _render_node(self, chunk_id: int) -> Response:
        rec = self.store.cad_node(chunk_id)
        if rec is None:
            raise NotFound(f"cad node ca{chunk_id} not found")
        _ref_id, name, meta = rec
        handle = handle_registry.format_handle("cad", chunk_id, chunk=True)
        payload = {"handle": handle, "name": name, **meta}
        return Response(body=render_agent_table([payload]))

    def _card_text(self, title: str, spec: Any, design: Any) -> str:
        """The one embeddable summary per design — built from the author's
        own names (hub_bore, bolts, ...) so search lands on intent."""
        comps = ", ".join(spec.components)
        names = ", ".join(n.name for n in spec.nodes)
        shapes = ", ".join(sorted({n.config.split(":")[0] for n in spec.nodes}))
        dims = ""
        try:
            from precis.cad.bulk import _expr_aabb

            lo, hi = _expr_aabb(design, design.whole())
            dims = (
                f" Bbox {hi[0] - lo[0]:.3g}x{hi[1] - lo[1]:.3g}x{hi[2] - lo[2]:.3g} mm."
            )
        except Exception:  # pragma: no cover - bbox is best-effort
            pass
        intent = ""
        desc = (spec.meta.get("description") or "").strip()
        use = (spec.meta.get("use") or "").strip()
        if desc:
            intent += f" {desc}"
        if use:
            intent += f" Used for: {use}"
        return (
            f"{title} (CAD design).{intent} Parts: {comps}. "
            f"Features: {names}. Shapes: {shapes}.{dims}"
        )

    def _interference_note(self, design: Any, spec: Any) -> str:
        comps = list(dict.fromkeys(spec.components))
        warns = []
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                try:
                    res = cad_clearance(design, comps[i], comps[j])
                except Exception:  # pragma: no cover - defensive
                    continue
                if res.interfering:
                    warns.append(
                        f"⚠ {comps[i]} ↔ {comps[j]} interfere ({res.gap:g} mm)"
                    )
        return ("  " + "; ".join(warns)) if warns else ""

    def _connectivity_note(self, design: Any, spec: Any) -> str:
        """The "a part is one connected solid" truism, surfaced at author time:
        warn when the design is >1 disconnected body (floating parts)."""
        if len(dict.fromkeys(spec.components)) < 2:
            return ""
        try:
            conn = cad_connectivity(design)
        except Exception:  # pragma: no cover - defensive
            return ""
        if conn.connected:
            return ""
        iso = conn.isolated()
        if iso:
            return f"  ⚠ floating (touches nothing): {', '.join(iso)}"
        bodies = " | ".join("+".join(g) for g in conn.groups)
        return f"  ⚠ {len(conn.groups)} disconnected bodies: {bodies}"

    def _render_probe(
        self, view: str, design: Any, spec: Any, args: dict[str, Any]
    ) -> Response:
        comp = args.get("component")
        if view == "point":
            pt = probe_point(design, _vec(args, "p"), component=comp)
            rows = [
                {
                    "name": h.label,
                    "state": h.relation,
                    "measure": "" if h.measure is None else f"{h.measure:g}",
                }
                for h in pt.hits
            ]
            head = f"probe point {tuple(pt.point)} — {pt.state}"
            return Response(
                body=head
                + "\n"
                + render_agent_table(rows, schema=["name", "state", "measure"])
            )
        if view == "ray":
            ray = probe_ray(design, _vec(args, "o"), _vec(args, "d"), component=comp)
            rows = [
                {
                    "t_in": f"{s.t_in:g}",
                    "t_out": f"{s.t_out:g}",
                    "len": f"{s.length:g}",
                    "state": s.state,
                    "feature": s.feature or "",
                }
                for s in ray.segments
            ]
            return Response(
                body="probe ray\n"
                + render_agent_table(
                    rows, schema=["t_in", "t_out", "len", "state", "feature"]
                )
            )
        if view == "arc":
            axis = (
                _vec(args, "axis")
                if isinstance(args.get("axis"), (list, tuple))
                else vec3(0, 0, 1)
            )
            arc = probe_arc(
                design, _vec(args, "c"), axis, float(args.get("r", 0.0)), component=comp
            )
            rows = [
                {
                    "theta_in": f"{s.theta_in:g}",
                    "theta_out": f"{s.theta_out:g}",
                    "span": f"{s.span:g}",
                    "state": s.state,
                    "feature": s.feature or "",
                }
                for s in arc.segments
            ]
            return Response(
                body=f"probe arc r={arc.radius:g}\n"
                + render_agent_table(
                    rows, schema=["theta_in", "theta_out", "span", "state", "feature"]
                )
            )
        if view == "section":
            z = float(args.get("z", 0.0))
            sec = probe_section_z(design, z, component=comp)
            rows = [
                {
                    "loop": lp.role,
                    "name": lp.label,
                    "shape": lp.shape,
                    "geom": ",".join(f"{k}={v:g}" for k, v in lp.geom.items()),
                }
                for lp in sec.loops
            ]
            return Response(
                body=f"section z={z:g}\n"
                + render_agent_table(rows, schema=["loop", "name", "shape", "geom"])
            )
        if view == "clearance":
            a, b = self._two_components(args, spec)
            cl = cad_clearance(design, a, b)
            tag = "interfere" if cl.interfering else "clear"
            return Response(body=f"clearance {a} ↔ {b}: {cl.gap:g} mm ({tag})")
        if view == "connectivity":
            return self._render_connectivity(design, spec, args)
        if view == "dof":
            mv = str(args.get("moving") or "")
            fx = str(args.get("fixed") or "")
            if mv not in spec.components or fx not in spec.components:
                raise BadInput(
                    "view='dof' needs args.moving and args.fixed (component names)",
                    next=f"components: {spec.components}",
                )
            dof = translational_dof(design, mv, fx)
            rows = [
                {"axis": k, "travel_mm": ("inf" if v == float("inf") else f"{v:g}")}
                for k, v in dof.travel.items()
            ]
            return Response(
                body=f"translational DOF {mv} vs {fx}\n"
                + render_agent_table(rows, schema=["axis", "travel_mm"])
            )
        # volume
        vol = cad_volume(design, component=comp)
        return Response(
            body=(
                f"volume{f' [{comp}]' if comp else ''}: {vol.volume:g} mm³ "
                f"(sampled, ±{vol.rel_err * 100:.1f}%); "
                f"centroid {tuple(round(float(x), 3) for x in vol.centroid)}"
            )
        )

    def _render_connectivity(
        self, design: Any, spec: Any, args: dict[str, Any]
    ) -> Response:
        """The assembly contact graph — "what's connected to X", "is there a
        path A→B", and the whole-assembly "one connected solid?" verdict.

        ``args``: ``{'of': part}`` → that part's neighbours; ``{'a': p, 'b':
        q}`` → the contact chain p…q (or "different bodies"); nothing → the
        full report (bodies + contacts). ``tol`` overrides the contact mm."""
        tol = float(args.get("tol", 1e-2))
        conn = cad_connectivity(design, tol=tol)

        a, b = args.get("a"), args.get("b")
        if a is not None and b is not None:
            a, b = str(a), str(b)
            for name in (a, b):
                if name not in spec.components:
                    raise BadInput(
                        "connectivity path needs args.a and args.b (component names)",
                        next=f"components: {spec.components}",
                    )
            chain = conn.path(a, b)
            if chain is None:
                return Response(
                    body=f"no connected path {a} → {b} — they are in separate bodies"
                )
            hops = len(chain) - 1
            return Response(
                body=f"path {a} → {b}:  " + " → ".join(chain) + f"  ({hops} contact(s))"
            )

        of = args.get("of")
        if of is not None:
            of = str(of)
            if of not in spec.components:
                raise BadInput(
                    "connectivity args.of must be a component name",
                    next=f"components: {spec.components}",
                )
            nbrs = conn.neighbors(of)
            body = f"{of} touches: " + (
                ", ".join(nbrs) if nbrs else "nothing — floating body ⚠"
            )
            return Response(body=body)

        verdict = (
            "one connected solid ✓"
            if conn.connected
            else f"{len(conn.groups)} separate bodies ⚠"
        )
        lines = [f"# connectivity — {len(conn.components)} component(s): {verdict}"]
        for i, g in enumerate(conn.groups, 1):
            lines.append(f"  body {i}: {', '.join(g)}")
        iso = conn.isolated()
        if iso:
            lines.append(f"  ⚠ floating (touch nothing): {', '.join(iso)}")
        rows = [
            {
                "a": c.a,
                "b": c.b,
                "gap_mm": f"{c.gap:g}",
                "state": "interfere" if c.interfering else "touch",
            }
            for c in conn.contacts
        ]
        table = (
            render_agent_table(rows, schema=["a", "b", "gap_mm", "state"])
            if rows
            else "(no contacts)"
        )
        return Response(body="\n".join(lines) + "\n" + table)

    def _two_components(self, args: dict[str, Any], spec: Any) -> tuple[str, str]:
        a = str(args.get("a") or "")
        b = str(args.get("b") or "")
        if a not in spec.components or b not in spec.components:
            raise BadInput(
                "view='clearance' needs args.a and args.b (component names)",
                next=f"components: {spec.components}",
            )
        return a, b
