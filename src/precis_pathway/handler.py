"""The ``pathway`` kind — a precis-mcp plugin handler.

Slice 0 (dark, ``PRECIS_AUTOCATPATH_ENABLED``): a `pathway` ref owns a autocatpath
reaction-network run. ``put`` takes the config YAML as the body, runs the
autocatpath pipeline **in-process on EMT** (cheap, qualitative), and persists:

* the ``methods.md`` paragraph as the embedded/citable body chunk
  (``chunk_kind='pathway_body'``),
* the reaction graph + pooled-uncertainty ``results.json`` + the provenance
  config snapshot in ``meta``.

Regen is content-addressed: re-``put``ting an unchanged config is a no-op
cache hit. Fan-out across ``(model, seed)`` and heavy backends move to the
precis compute lane in later slices (see
``docs/backlog/autocatpath-integration.md`` in precis-mcp). Native `structure`
refs per intermediate (the ``pathway-node`` link) are slice 1 — deferred
here because link relations are a closed `Relation` Literal in precis core
and need a core edit to extend.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store

from .persist import BODY_KIND, pathway_title, persist_result

#: When set, `put` routes the compute to a `autocatpath_explore` job pinned to this
#: node instead of running autocatpath in-process (slice 1). The gateway sets it to
#: the GPU node's PRECIS_NODE (e.g. 'spark'); unset → in-process EMT (slice 0).
_ROUTE_NODE_ENV = "PRECIS_AUTOCATPATH_ROUTE_NODE"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _nf(x: Any) -> float:
    """None (a persisted non-finite barrier, stored as JSON null) → nan, so the
    number formatters below still render 'nan' instead of raising on None."""
    return float("nan") if x is None else float(x)


class PathwayHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pathway",
        title="Reaction pathway (autocatpath)",
        description=(
            "A catalyst reaction-network exploration (autocatpath): give it a "
            "surface, substrate, and target as a YAML config body; it relaxes "
            "every intermediate and finds NEB barriers, reporting energies "
            "with pooled uncertainty (low-confidence flagged, not faked). "
            "put(kind='pathway', id='<name>', text='<config yaml>') runs it; "
            "get(kind='pathway', id='<name>', view='network'|'profile'|"
            "'methods'|'config'). Slice 0 runs EMT in-process (qualitative). "
            "See precis-pathway-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        is_numeric=False,
        role="artifact",
        corpus_role="none",
        # Own the derived autocatpath_explore compute job (compute lane),
        # so `put` can route the run to the pinned node instead of running it
        # in-process. Requires precis KindSpec.can_own_jobs (>= 8.22).
        can_own_jobs=True,
        # 'preview' builds the reaction network cheaply (no ML compute) so the
        # LLM can read + argue with the intermediates/steps before spending
        # relax/NEB. Re-put without it to run.
        modes=("preview",),
        views=(
            "analysis",
            "compare",
            "intermediates",
            "steps",
            "warnings",
            "profile",
            "mermaid",
            "network",
            "methods",
            "config",
        ),
    )

    def __init__(self, *, hub: Hub) -> None:
        # Gated dark: the kind only appears when explicitly enabled, so
        # the slice merges without exposing an in-process compute path by
        # default. (Mirrors PRECIS_SANDBOX_ENABLED / PRECIS_CLASSIFY_ENABLED.)
        if os.environ.get("PRECIS_AUTOCATPATH_ENABLED", "") not in (
            "1",
            "true",
            "True",
        ):
            raise InitError(
                "pathway kind is off; set PRECIS_AUTOCATPATH_ENABLED=1 to enable"
            )
        # autocatpath (ase/rdkit/networkx) is a hard dep of autocatpath[precis], but
        # guard so a broken env drops the kind cleanly instead of crashing boot.
        try:
            from . import runner  # noqa: F401
        except Exception as e:  # pragma: no cover - env-dependent
            raise InitError(f"autocatpath pipeline unavailable: {e}") from e
        _ = hub

    # -- put -------------------------------------------------------------
    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        from . import runner

        if not text or not text.strip():
            raise BadInput(
                "pathway needs the config YAML as the body (text=)",
                next=(
                    "put(kind='pathway', id='no_to_no3_pd', "
                    'text=\'substrate: "NO"\\ntarget: "NO3"\\n...\')'
                ),
            )

        route_node = os.environ.get(_ROUTE_NODE_ENV) or None
        # Routed: run the config's own backend on the pinned node. In-process:
        # force EMT (the gateway has no ML backend, keeps the put cheap).
        force = None if route_node else "emt"

        # Parse once (chem-safe) to derive the slug + the effective content key,
        # so an unchanged config short-circuits before the expensive run.
        try:
            from autocatpath.config import _load_yaml

            raw = _load_yaml(text)
            effective = runner.effective_config(raw, force_backend=force)
            key = runner.content_key(effective)
            slug = self._slugify(id or effective.get("name") or "pathway")
        except BadInput:
            raise
        except Exception as e:
            raise BadInput(f"could not parse pathway config: {e}") from e

        store = self.hub.live_store
        existing = store.get_ref(kind="pathway", id=slug)

        if mode == "preview":
            return self._preview(store, slug, raw, effective, key, existing, tags)

        if (
            existing is not None
            and existing.meta.get("content_key") == key
            and existing.meta.get("status") == "ready"
        ):
            return Response(
                body=(
                    f"pathway '{slug}' unchanged (cache hit {key[:12]}); "
                    "nothing to recompute."
                )
            )

        if route_node:
            return self._dispatch_job(
                store, slug, raw, effective, key, route_node, force, tags, existing
            )

        # In-process on EMT (slice 0). Synchronous — keep demo configs small.
        artifact = runner.run_pathway_from_yaml(text, force_backend="emt")
        if existing is None:
            with store.tx() as conn:
                ref = store.insert_ref(
                    kind="pathway",
                    slug=slug,
                    title=pathway_title(artifact),
                    meta={"status": "computing"},
                    conn=conn,
                )
            ref_id, verb = ref.id, "created"
        else:
            ref_id, verb = existing.id, "regenerated"
        # persist_result stamps the full meta, (re)writes the methods body, and
        # ingests each intermediate as a linked `structure` ref (slice 1b).
        persist_result(
            store,
            ref_id,
            artifact,
            pathway_slug=slug,
            extra_meta={"backend_forced": "emt", "slice": 0},
        )
        if tags:
            with store.tx() as conn:
                for t in tags:
                    self._add_tag(store, ref_id, t, conn)

        return Response(body=self._put_summary(slug, verb, artifact))

    def _dispatch_job(
        self,
        store: Store,
        slug: str,
        raw_config: dict[str, Any],
        effective: dict[str, Any],
        key: str,
        node: str,
        force: str | None,
        tags: list[str] | None,
        existing: Any,
    ) -> Response:
        """Route the compute: ensure the pathway ref exists (status=computing),
        then mint a `autocatpath_explore` job pinned to `node` (compute lane).
        The node's ssh_node worker claims it and runs autocatpath there,
        writing the result back onto this ref."""
        seed_meta = {
            "content_key": key,
            "config": effective,
            "status": "computing",
            "route_node": node,
            "slice": 1,
        }
        placeholder = (
            f"# {slug}\n\nautocatpath compute dispatched to **{node}** "
            f"(cache_key {key[:12]}). Results will replace this on completion."
        )
        with store.tx() as conn:
            if existing is None:
                ref = store.insert_ref(
                    kind="pathway",
                    slug=slug,
                    title=f"pathway {slug} (computing)",
                    meta=seed_meta,
                    conn=conn,
                )
                store.blocks.insert_blocks(
                    ref.id,
                    [
                        BlockInsert(
                            pos=0, text=placeholder, meta={"chunk_kind": BODY_KIND}
                        )
                    ],
                    conn=conn,
                )
                ref_id = ref.id
            else:
                ref_id = existing.id
                store.stamp_ref_meta(ref_id, seed_meta, conn=conn)
            for t in tags or []:
                self._add_tag(store, ref_id, t, conn)

        from precis.handlers.job import JobHandler

        job = JobHandler(hub=self.hub).put(
            job_type="autocatpath_explore",
            executor="ssh_node",
            parent_id=ref_id,
            idem_key=f"autocatpath_explore:{key}",
            params={
                "pathway_ref_id": ref_id,
                "pathway_slug": slug,
                "config": raw_config,
                "force_backend": force,  # None → run the config's own backend
                "content_key": key,
                "target_node": node,
            },
        )
        return Response(
            body=(
                f"dispatched autocatpath compute for '{slug}' to {node} "
                f"(cache_key {key[:12]}). {job.body}\n"
                f"Track: get(kind='pathway', id='{slug}') — status 'computing' "
                "until the job writes back."
            )
        )

    # -- get -------------------------------------------------------------
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput(
                "pathway get needs an id (the pathway slug)",
                next="get(kind='pathway', id='no_to_no3_pd')",
            )
        store = self.hub.live_store
        ref = store.get_ref(kind="pathway", id=str(id))
        if ref is None:
            raise BadInput(f"no pathway '{id}'")

        meta = ref.meta or {}
        if meta.get("status") == "computing":
            node = meta.get("route_node", "?")
            return Response(
                body=f"pathway '{id}' is computing on {node} "
                f"(cache_key {str(meta.get('content_key', ''))[:12]}). Check back shortly."
            )
        v = (view or "").lower()
        if v == "config":
            return Response(body=meta.get("config_snapshot_yaml", "(no config)"))
        if v == "methods":
            blocks = store.blocks.list_blocks_for_ref(ref.id)
            body = "\n\n".join(b.text for b in blocks if b.text)
            return Response(body=body or "(no methods)")
        if v == "mermaid":
            return Response(body=self._mermaid(meta))
        if v == "compare":
            return Response(body=self._compare(store, ref, meta))

        computed = bool(meta.get("graph"))
        if v in ("analysis", "steps", "warnings") and not computed:
            return Response(
                body=f"pathway '{id}' not computed yet (status "
                f"{meta.get('status', '?')}). Run it (put without mode='preview') "
                "first, then read this view."
            )
        if v == "analysis":
            from .toon_views import analysis_text

            return Response(body=analysis_text(meta))
        if v == "steps":
            from .toon_views import steps_toon

            return Response(body=steps_toon(meta))
        if v == "warnings":
            from .toon_views import warnings_toon

            return Response(body=warnings_toon(meta))
        if v == "intermediates":
            if computed:
                from .toon_views import intermediates_toon

                return Response(body=intermediates_toon(meta))
            return Response(body=self._intermediates(meta))  # preview topology
        if v == "network":
            return Response(body=self._render_network(ref.title, meta))
        if v == "profile":
            return Response(body=self._render_profile(ref.title, meta))
        # default (no view): analysis if computed, else the preview/profile text
        if computed:
            from .toon_views import analysis_text

            return Response(body=analysis_text(meta))
        return Response(body=self._render_profile(ref.title, meta))

    # -- compare (cross-candidate) ---------------------------------------
    def _compare(self, store: Store, ref: Any, meta: dict[str, Any]) -> str:
        """Compare this pathway against every computed sibling sharing the same
        substrate→target (same reaction), as one interleaved TOON table."""
        from . import analysis
        from .toon_views import compare_toon

        r = meta.get("results", {})
        substrate, target = r.get("substrate"), r.get("target")
        if not substrate or not target:
            return "cannot compare: this pathway has no computed results yet."
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT slug_id.id_value, refs.meta FROM refs "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = refs.ref_id AND slug_id.id_kind = 'cite_key' "
                "WHERE refs.kind = 'pathway' AND refs.deleted_at IS NULL "
                "  AND refs.meta->'results'->>'substrate' = %s "
                "  AND refs.meta->'results'->>'target' = %s "
                "  AND refs.meta->>'status' = 'ready'",
                (substrate, target),
            ).fetchall()
        candidates = []
        for slug, m in rows:
            g = (m or {}).get("graph")
            if not g:
                continue
            c_root, c_target = analysis.roots(g, m.get("results", {}))
            candidates.append(
                {
                    "slug": slug or "?",
                    "lever": (m.get("config") or {})
                    .get("slab", {})
                    .get("element", "?"),
                    "graph": g,
                    "root": c_root,
                    "target": c_target,
                }
            )
        if len(candidates) < 2:
            return (
                f"only 1 computed pathway for {substrate}→{target}; nothing to "
                "compare yet. Run variants (different surface/dopant) to build a "
                "leaderboard."
            )
        return compare_toon(candidates)

    # -- preview (cheap, no compute) -------------------------------------
    def _preview(
        self,
        store: Store,
        slug: str,
        raw_config: dict[str, Any],
        effective: dict[str, Any],
        key: str,
        existing: Any,
        tags: list[str] | None,
    ) -> Response:
        """Build the reaction network cheaply (rule-based, no ML) and store it
        as a `status:preview` pathway so the LLM can read + argue with the
        intermediates/steps before spending any relax/NEB compute."""
        from . import runner
        from .text_views import topology_to_mermaid, topology_to_text

        topo = runner.network_topology(raw_config)
        body = topology_to_text(topo)
        seed_meta = {
            "content_key": key,
            "config": effective,
            "topology": topo,
            "status": "preview",
            "slice": "1b",
        }
        title = f"{topo['substrate']} → {topo['target']} on {topo['element']} (preview)"
        with store.tx() as conn:
            if existing is None:
                ref = store.insert_ref(
                    kind="pathway", slug=slug, title=title, meta=seed_meta, conn=conn
                )
                store.blocks.insert_blocks(
                    ref.id,
                    [BlockInsert(pos=0, text=body, meta={"chunk_kind": BODY_KIND})],
                    conn=conn,
                )
                ref_id = ref.id
            else:
                ref_id = existing.id
                store.stamp_ref_meta(ref_id, seed_meta, conn=conn)
                store.blocks.replace_body_chunk(
                    ref_id, body, chunk_kind=BODY_KIND, conn=conn
                )
            for t in tags or []:
                self._add_tag(store, ref_id, t, conn)
        return Response(
            body=(
                f"previewed '{slug}': {len(topo['states'])} intermediates, "
                f"{len(topo['steps'])} steps — NO compute spent. Argue with it, "
                f"edit, then re-put without mode='preview' to run.\n\n{body}\n\n"
                f"```mermaid\n{topology_to_mermaid(topo)}\n```"
            )
        )

    def _mermaid(self, meta: dict[str, Any]) -> str:
        from . import runner
        from .text_views import graph_to_mermaid, topology_to_mermaid

        if meta.get("graph"):
            return "```mermaid\n" + graph_to_mermaid(meta["graph"]) + "\n```"
        topo = meta.get("topology") or (
            runner.network_topology(meta["config"]) if meta.get("config") else None
        )
        if topo:
            return "```mermaid\n" + topology_to_mermaid(topo) + "\n```"
        return "(no network to render)"

    def _intermediates(self, meta: dict[str, Any]) -> str:
        from . import runner
        from .text_views import topology_to_text

        topo = meta.get("topology") or (
            runner.network_topology(meta["config"]) if meta.get("config") else None
        )
        return topology_to_text(topo) if topo else "(no network)"

    # -- delete ----------------------------------------------------------
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None:
            raise BadInput("pathway delete needs an id")
        store = self.hub.live_store
        ref = store.get_ref(kind="pathway", id=str(id))
        if ref is None:
            raise BadInput(f"no pathway '{id}'")
        store.soft_delete_ref(ref.id)
        return Response(body=f"deleted pathway '{id}'")

    # -- tag -------------------------------------------------------------
    def tag(
        self,
        *,
        id: str | int | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput("pathway tag needs an id")
        store = self.hub.live_store
        ref = store.get_ref(kind="pathway", id=str(id))
        if ref is None:
            raise BadInput(f"no pathway '{id}'")
        with store.tx() as conn:
            for t in add or []:
                self._add_tag(store, ref.id, t, conn)
            for t in remove or []:
                self._remove_tag(store, ref.id, t, conn)
        return Response(body=f"tagged pathway '{id}'")

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _slugify(name: Any) -> str:
        s = _SLUG_RE.sub("-", str(name).strip().lower()).strip("-")
        return s or "pathway"

    @staticmethod
    def _put_summary(slug: str, verb: str, artifact: dict[str, Any]) -> str:
        r = artifact["results_json"]
        n_nodes = len(r["nodes"])
        n_edges = len(r["edges"])
        low = sum(1 for e in r["edges"] if e["barrier"].get("low_confidence"))
        warns = len(artifact["warnings"])
        return (
            f"{verb} pathway '{slug}': {n_nodes} states, {n_edges} steps "
            f"({r['n_samples']} samples, backend {r['backend']}). "
            f"{low} low-confidence barrier(s), {warns} warning(s). "
            f"get(kind='pathway', id='{slug}', view='profile')"
        )

    @staticmethod
    def _render_profile(title: str, meta: dict[str, Any]) -> str:
        r = meta.get("results", {})
        lines = [f"# {title}", ""]
        ref_name = r.get("pathway", [None])[0]
        lines.append(f"Energy reference: {r.get('energy_reference', '?')}")
        lines.append("")
        lines.append("## States (relative energy, eV)")
        nodes = r.get("nodes", {})
        for name in r.get("pathway", []):
            est = nodes.get(name, {})
            rel = _nf(est.get("mean"))
            std = _nf(est.get("std")) if est.get("std") is not None else 0.0
            flag = "  [LOW CONFIDENCE]" if est.get("low_confidence") else ""
            here = "  ←root" if name == ref_name else ""
            lines.append(f"  {name:<16} {rel:+.3f} ± {std:.3f}{flag}{here}")
        warns = meta.get("warnings") or []
        if warns:
            lines += ["", "## Warnings", *(f"  - {w}" for w in warns)]
        return "\n".join(lines)

    @staticmethod
    def _render_network(title: str, meta: dict[str, Any]) -> str:
        r = meta.get("results", {})
        lines = [f"# {title} — reaction network", ""]
        for e in r.get("edges", []):
            b = e.get("barrier", {})
            d = e.get("delta_e", {})
            flag = "  [LOW CONFIDENCE]" if b.get("low_confidence") else ""
            lines.append(
                f"  {e['reactant']} → {e['product']}: "
                f"Ea={_nf(b.get('mean')):.3f}±{_nf(b.get('std')):.3f}  "
                f"ΔE={_nf(d.get('mean')):+.3f}±{_nf(d.get('std')):.3f}{flag}"
            )
        return "\n".join(lines)

    def _add_tag(self, store: Store, ref_id: int, raw: str, conn: Any) -> None:
        from precis.store import Tag

        store.add_tag(
            ref_id, Tag.parse_strict(raw, kind="pathway"), set_by="agent", conn=conn
        )

    def _remove_tag(self, store: Store, ref_id: int, raw: str, conn: Any) -> None:
        from precis.store import Tag

        store.remove_tag(ref_id, Tag.parse_strict(raw, kind="pathway"), conn=conn)
