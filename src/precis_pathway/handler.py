"""The ``pathway`` kind — a precis-mcp plugin handler.

Slice 0: a `pathway` ref owns a autocatpath
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

import math
import os
from typing import TYPE_CHECKING, Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
from precis.handlers._mode_help import require_mode
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.text import slugify

if TYPE_CHECKING:
    from precis.store import Store

    from .types import NetworkTopology, PathwayArtifact

from .persist import BODY_KIND, pathway_title, persist_result

#: When set, `put` routes the compute to a `autocatpath_explore` job pinned to this
#: node instead of running autocatpath in-process (slice 1). The gateway sets it to
#: the GPU node's PRECIS_NODE (e.g. 'spark'); unset → in-process EMT (slice 0).
_ROUTE_NODE_ENV = "PRECIS_AUTOCATPATH_ROUTE_NODE"


#: Views that accept the CHE potential lever ``args={'U': <V vs RHE>}``
#: (docs/backlog/pathway-conditions-effects-report.md Phase 1 item 1;
#: 'staircase' re-tilts the affine export — SPSO slice 3).
_POTENTIAL_VIEWS = ("analysis", "profile", "compare", "staircase")


def _finite(raw: Any, name: str, unit: str) -> float:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int | float)
        or not math.isfinite(raw)
    ):
        raise BadInput(
            f"args={{'{name}': {raw!r}}}: {name} must be a finite number ({unit})"
        )
    return float(raw)


def _point_args(args: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """``(U, pH)`` from ``args`` — ``U`` (V vs RHE), ``pH``, or ``point``
    as ``'U,pH'`` / ``[U, pH]`` (the picked point of ``view='staircase'``,
    gr489471). Absent → ``None``. Any other key is rejected by name so a
    typo (``u``, ``potential``) never reads as "no lever"."""
    if not isinstance(args, dict) or not args:
        return None, None
    unknown = sorted(str(k) for k in args if k not in ("U", "pH", "point"))
    if unknown:
        raise BadInput(
            f"pathway get args={unknown!r} not recognised",
            options=["U", "pH", "point"],
            next="args={'U': -0.3}  (potential, V vs RHE; views analysis|profile|compare)",
        )
    u = _finite(args["U"], "U", "V vs RHE") if "U" in args else None
    ph = _finite(args["pH"], "pH", "pH units") if "pH" in args else None
    if "point" in args:
        raw = args["point"]
        parts = raw.split(",") if isinstance(raw, str) else raw
        try:
            pu, pp = (float(x) for x in parts)
        except (TypeError, ValueError):
            raise BadInput(
                f"args={{'point': {raw!r}}}: point is 'U,pH' (V vs RHE, pH)"
            ) from None
        if u is not None or ph is not None:
            raise BadInput("args: give point= or U=/pH=, not both")
        u, ph = _finite(pu, "U", "V vs RHE"), _finite(pp, "pH", "pH units")
    return u, ph


def _potential_arg(args: dict[str, Any] | None) -> float | None:
    """``args['U']`` as a finite float (V vs RHE), or None when absent."""
    return _point_args(args)[0]


def _nf(x: Any) -> float:
    """None (a persisted non-finite barrier, stored as JSON null) → nan, so the
    number formatters below still render 'nan' instead of raising on None."""
    return float("nan") if x is None else float(x)


def _lookup(store: Any, ident: str) -> Any:
    """The live pathway ``ident`` names: its slug first, then — for an
    all-digit string — its bare ref id (gr489163: ``id='246367'`` missed
    while ``pw246367`` and numeric quest ids resolve). ``None`` if neither."""
    ref = store.get_ref(kind="pathway", id=ident)
    if ref is None and ident.strip().isdigit():
        ref = store.get_ref(kind="pathway", id=int(ident))
    return ref


class PathwayHandler(Handler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with :func:`_point_args`.
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "get": frozenset({"U", "pH", "point"}),
    }

    spec: ClassVar[KindSpec] = KindSpec(
        kind="pathway",
        plugin_api=1,  # precis.protocol.PLUGIN_API this model is built for
        title="Reaction pathway (autocatpath)",
        description=(
            "A catalyst reaction-network exploration (autocatpath): give it a "
            "surface, substrate, and target as a YAML config body; it relaxes "
            "every intermediate and finds NEB barriers, reporting energies "
            "with pooled uncertainty (low-confidence flagged, not faked). "
            "put(kind='pathway', id='<name>', text='<config yaml>') runs it; "
            "get(kind='pathway') with no id lists existing pathways; "
            "get(kind='pathway', id='<name>', view='network'|'profile'|"
            "'methods'|'config'). search(kind='pathway', q='NO→NH3') finds "
            "pathways by slug/title/body text or a linked structure/quest "
            "handle. Slice 0 runs EMT in-process (qualitative). "
            "See precis-pathway-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_search=True,
        is_numeric=False,
        placement="artifact",
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
            "kinetics",
            "trust",
            "warnings",
            "profile",
            "mermaid",
            "network",
            "methods",
            "config",
            "staircase",
        ),
    )

    def __init__(self, *, hub: Hub) -> None:
        # autocatpath (ase/rdkit/networkx) is a hard dep of autocatpath[precis], but
        # guard so a broken env drops the kind cleanly instead of crashing boot.
        # Probe the base `autocatpath` package directly rather than `from .
        # import runner`: `runner` pulls in `autocatpath.pipeline`, which
        # module-level-imports `render`/`viz` for matplotlib figure export —
        # forcing a ~2.5s font-cache regen on every fresh container boot just
        # to answer "is autocatpath installed" (gr345269). ase/rdkit/networkx
        # are unconditional deps of `autocatpath` itself, so a clean bare
        # import already proves the whole catalyst extra is present; the
        # real `runner` import still happens lazily on first `put()`.
        try:
            import autocatpath  # noqa: F401
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

        # A typo'd/unknown mode (e.g. 'previw') must never fall through to
        # the real run/dispatch path below and mint a GPU compute job —
        # gr343054. `None` (the "run it" default) is never checked here.
        if mode is not None:
            require_mode(spec=self.spec, verb="put", mode=mode)

        # The env renders as the full autocatpath capability list since the
        # multi-node fan-out (comma-separated). This surface mints ONE
        # explore job, so it pins to the first entry — the raw string would
        # match no worker's PRECIS_NODE and wedge the job unclaimed forever.
        env_route = os.environ.get(_ROUTE_NODE_ENV) or ""
        route_nodes = [n.strip() for n in env_route.split(",") if n.strip()]
        route_node = route_nodes[0] if route_nodes else None
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
            slug = slugify(
                str(id or effective.get("name") or "pathway"), default="pathway"
            )
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
        artifact: PathwayArtifact = runner.run_pathway_from_yaml(
            text, force_backend="emt"
        )
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
                store.chunks.insert_chunks(
                    ref.id,
                    [
                        ChunkInsert(
                            ord=0, text=placeholder, meta={"chunk_kind": BODY_KIND}
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

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(self.spec.views)

    # -- search ------------------------------------------------------------
    def search(
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        """Find pathways by reaction label / slug words, or by a linked
        structure / quest handle (gr489581 — search used to be
        Unsupported; agents detoured through structure fisheyes)."""
        store = self.hub.live_store
        if not q or not q.strip():
            raise BadInput(
                "pathway search needs q= (a reaction label, slug words, "
                "or a structure/quest handle)",
                next="search(kind='pathway', q='NO→NH3') · "
                "search(kind='pathway', q='se123') · get(kind='pathway') "
                "lists all",
            )
        text = q.strip()

        # A handle (se123 / qu7 / pw12) names a neighbour to pivot from:
        # every inbound 'related-to'/'serves' link from a pathway.
        parsed = handle_registry.parse(text)
        via_handle: dict[int, str] = {}
        if parsed is not None and not parsed[1]:
            tgt = store.get_ref(kind=parsed[0], id=parsed[2])
            if tgt is not None and parsed[0] != "pathway":
                for ln in store.links_for(tgt.id, direction="in"):
                    via_handle[ln.src_ref_id] = f"linked from {text}"

        esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, slug_id.id_value, r.title, "
                "       r.meta->>'status', "
                "       CASE WHEN slug_id.id_value ILIKE %(p)s "
                "            OR r.title ILIKE %(p)s THEN 'slug/title' "
                "            ELSE 'body' END AS matched_on "
                "FROM refs r "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = r.ref_id AND slug_id.id_kind = 'cite_key' "
                "WHERE r.kind = 'pathway' AND r.retired_at IS NULL AND ("
                "  slug_id.id_value ILIKE %(p)s OR r.title ILIKE %(p)s "
                "  OR EXISTS (SELECT 1 FROM chunks c "
                "             WHERE c.ref_id = r.ref_id "
                "             AND c.retired_at IS NULL AND c.text ILIKE %(p)s)"
                "  OR r.ref_id = ANY(%(via)s)"
                ") ORDER BY r.updated_at DESC LIMIT %(lim)s",
                {"p": f"%{esc}%", "via": sorted(via_handle) or [-1], "lim": page_size},
            ).fetchall()
        if not rows:
            return Response(
                body=f"no pathways match {text!r} — get(kind='pathway') "
                "lists all; a structure or quest handle finds linked pathways"
            )
        lines = [f"# {len(rows)} pathway(s) matching {text!r}"]
        for ref_id, slug, title, status, matched_on in rows:
            via = via_handle.get(ref_id)
            note = via or matched_on
            lines.append(f"  {slug or ref_id}  [{status or '?'}]  {title}  ({note})")
        return Response(body="\n".join(lines))

    # -- get -------------------------------------------------------------
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        store = self.hub.live_store
        if id is None:
            # A bare get lists. Search is unsupported on this kind, so until
            # 2026-10-01 an agent that did not already hold a slug had no way
            # to find any of ~700 pathways — and this branch's error pointed
            # at an example slug that never existed.
            return Response(body=self._list(store))
        U, pH = _point_args(args)
        # A trailing ``~<source>→<target>`` step selector (Simulation step
        # deep-links) — dispatch's universal-handle/slug routing reattaches
        # any ``~selector`` suffix to the resolved id untouched (it only
        # recognises chunk/view selectors of its own, e.g. ``~ord``), so it
        # reaches us as one string. ``slugify`` never produces ``~``, so
        # splitting on the first one is unambiguous.
        ident = str(id)
        slug, sep, step_label = ident.partition("~")
        if sep:
            return self._get_step(store, slug, step_label)
        ref = _lookup(store, slug)
        if ref is None:
            raise BadInput(f"no pathway '{id}'")

        # An unknown view= (typo or stale doc) must never silently fall
        # through to the default analysis/profile output — gr343054.
        v = (view or "").lower()
        if view is not None and v not in self.accepted_views():
            accepted = self.accepted_views()
            raise BadInput(
                f"pathway view {view!r} not recognised",
                options=accepted,
                next=(
                    f"get(kind='pathway', id={id!r}, view={accepted[0]!r}) — "
                    f"accepted views: {accepted}"
                ),
            )

        if U is not None and view is not None and v not in _POTENTIAL_VIEWS:
            raise BadInput(
                f"args={{'U': …}} does not apply to view {view!r}",
                options=list(_POTENTIAL_VIEWS),
                next=f"get(kind='pathway', id={id!r}, view='analysis', args={{'U': {U}}})",
            )
        if pH is not None and v != "staircase":
            raise BadInput(
                "args pH/point picks a point on view='staircase' only",
                next=f"get(kind='pathway', id={id!r}, view='staircase', "
                f"args={{'U': {U}, 'pH': {pH}}})",
            )

        meta = ref.meta or {}
        if meta.get("status") == "computing":
            node = meta.get("route_node", "?")
            return Response(
                body=f"pathway '{id}' is computing on {node} "
                f"(cache_key {str(meta.get('content_key', ''))[:12]}). Check back shortly."
            )
        if v == "config":
            return Response(body=meta.get("config_snapshot_yaml", "(no config)"))
        if v == "methods":
            blocks = store.chunks.list_chunks_for_ref(ref.id)
            body = "\n\n".join(b.text for b in blocks if b.text)
            return Response(body=body or "(no methods)")
        if v == "mermaid":
            return Response(body=self._mermaid(meta))
        if v == "compare":
            return Response(body=self._compare(store, ref, meta, U=U))
        if v == "staircase":
            return Response(
                # U alone is the plain re-tilt (the structure view always
                # forwards its quest's U); a pH or point picks a point
                body=self._staircase(
                    store,
                    ref,
                    meta,
                    U=U,
                    pH=pH,
                    picked=isinstance(args, dict) and ("pH" in args or "point" in args),
                )
            )

        computed = bool(meta.get("graph"))
        if v in ("analysis", "steps", "kinetics", "trust", "warnings") and not computed:
            return Response(
                body=f"pathway '{id}' not computed yet (status "
                f"{meta.get('status', '?')}). Run it (put without mode='preview') "
                "first, then read this view."
            )
        if U is not None:
            # analysis / profile / the default view at a stated potential:
            # the lever needs a computed, CHE-stamped graph — never a silent
            # zero shift on a pre-CHE run.
            from precis.utils import reaction_graph as analysis

            if not computed:
                raise BadInput(
                    f"pathway '{id}' not computed yet — args={{'U': …}} needs a "
                    "computed graph"
                )
            if not analysis.has_potential_lever(meta.get("graph") or {}):
                raise BadInput(
                    f"pathway '{id}' carries no n_H on its graph (pre-CHE engine "
                    "run) — the potential lever is unavailable; re-run it on the "
                    "current engine",
                    next=f"get(kind='pathway', id={id!r}, view='analysis')",
                )
        if v == "analysis" or (v == "" and U is not None):
            from .toon_views import analysis_text

            return Response(body=analysis_text(meta, U=U))
        if v == "steps":
            from .toon_views import steps_toon

            return Response(body=steps_toon(meta))
        if v == "kinetics":
            from .toon_views import kinetics_text

            return Response(body=kinetics_text(meta))
        if v == "trust":
            from .toon_views import trust_toon

            return Response(body=trust_toon(meta))
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
            return Response(body=self._render_profile(ref.title, meta, U=U))
        # default (no view): analysis if computed, else the preview/profile text
        if computed:
            from .toon_views import analysis_text

            return Response(body=analysis_text(meta))
        return Response(body=self._render_profile(ref.title, meta))

    # -- step selector -----------------------------------------------------
    def _get_step(self, store: Store, slug: str, step_label: str) -> Response:
        """Resolve a ``~<source>→<target>`` step selector against the
        computed graph's edges (``meta['graph']['links']``) — see
        ``analysis._path_steps``'s ``f"{source}→{target}"`` label, unique per
        network."""
        ref = _lookup(store, slug)
        if ref is None:
            raise BadInput(f"no pathway '{slug}'")
        meta = ref.meta or {}
        graph = meta.get("graph")
        if not graph:
            raise BadInput(
                f"pathway '{slug}' has no computed graph yet (status "
                f"{meta.get('status', '?')}); a step selector only resolves "
                "against a computed pathway.",
                next=f"get(kind='pathway', id='{slug}') — run it first "
                "(put without mode='preview')",
            )
        links = graph.get("links") or []
        by_label = {f"{e['source']}→{e['target']}": e for e in links}
        edge = by_label.get(step_label)
        if edge is None:
            available = sorted(by_label)
            shown = ", ".join(available[:30])
            more = f" (+{len(available) - 30} more)" if len(available) > 30 else ""
            raise BadInput(
                f"pathway '{slug}' has no step '{step_label}'.",
                next=(
                    f"available steps: {shown}{more}"
                    if available
                    else f"pathway '{slug}' has no steps"
                ),
            )
        from precis.utils.handle_registry import try_format

        from .toon_views import step_view

        pw_handle = try_format("pathway", ref.id) or slug
        return Response(body=step_view(meta, pw_handle, edge))

    # -- compare (cross-candidate) ---------------------------------------
    _LIST_LIMIT = 20

    def _list(self, store: Store) -> str:
        """The most recently updated pathways, with real slugs to pass back
        as ``id=``, plus the total per status."""
        with store.pool.connection() as conn:
            counts = conn.execute(
                "SELECT coalesce(meta->>'status', '?'), count(*) FROM refs "
                "WHERE kind = 'pathway' AND retired_at IS NULL "
                "GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            rows = conn.execute(
                "SELECT slug_id.id_value, refs.title, refs.meta->>'status', "
                "refs.updated_at, refs.meta->>'tier', refs.meta->'span', "
                "refs.meta->'low_confidence' FROM refs "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = refs.ref_id AND slug_id.id_kind = 'cite_key' "
                "WHERE refs.kind = 'pathway' AND refs.retired_at IS NULL "
                "ORDER BY refs.updated_at DESC LIMIT %s",
                (self._LIST_LIMIT,),
            ).fetchall()
        if not rows:
            return (
                "no pathways yet. Frame one without spending compute: "
                "put(kind='pathway', id='<name>', mode='preview', "
                "text='<config yaml>') — see precis-pathway-help."
            )
        total = sum(n for _, n in counts)
        # Quest candidates all carry the same reaction title, so a title
        # column would repeat one string 20 times. Name it once instead; the
        # tier and the energetic span are what tell the rows apart.
        titles = {title for _, title, *_ in rows}
        shared = titles.pop() if len(titles) == 1 else None
        lines = [
            f"{total} pathways ("
            + ", ".join(f"{n} {status}" for status, n in counts)
            + f"); the {len(rows)} most recently updated"
            + (f", all '{shared}'" if shared else "")
            + ":",
            "",
            "slug | "
            + ("" if shared else "title | ")
            + "status | tier | span eV | updated",
        ]
        for slug, title, status, updated, tier, span, low in rows:
            span_txt = f"{span:.2f}" if isinstance(span, (int, float)) else "-"
            if low is True:
                span_txt += " (low confidence)"
            lines.append(
                f"{slug or '?'} | "
                + ("" if shared else f"{title} | ")
                + f"{status or '?'} | {tier or '-'} | {span_txt} | "
                f"{updated:%Y-%m-%d %H:%MZ}"
            )
        # Point the hint at a ready pathway: view='analysis' on one still
        # computing only says "check back shortly".
        first = next(
            (row[0] for row in rows if row[0] and row[2] == "ready"),
            next((row[0] for row in rows if row[0]), None),
        )
        if first:
            lines += [
                "",
                f"next: get(kind='pathway', id='{first}', view='analysis') · "
                "view='compare' ranks it against every computed pathway for "
                "the same substrate→target.",
            ]
        return "\n".join(lines)

    def _compare(
        self, store: Store, ref: Any, meta: dict[str, Any], *, U: float | None = None
    ) -> str:
        """Compare this pathway against every computed sibling sharing the same
        substrate→target (same reaction), as one interleaved TOON table. With
        ``U`` (V vs RHE) every CHE-stamped candidate is re-levered first and
        the table ranks by energetic span at that potential."""
        from precis.utils import reaction_graph as analysis

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
                "WHERE refs.kind = 'pathway' AND refs.retired_at IS NULL "
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
        return compare_toon(candidates, U=U)

    # -- staircase (SPSO slice 3, the che_affine read) --------------------
    def _staircase(
        self,
        store: Store,
        ref: Any,
        meta: dict[str, Any],
        *,
        U: float | None,
        pH: float | None = None,
        picked: bool = False,
    ) -> str:
        """`view='staircase'` — the reaction staircase from the engine's
        affine CHE export, re-tilted at ``U`` (default: the pathway's
        recorded operating point, else 0 V). Where a linked structure has
        a surface-Pourbaix map, the map's resting termination at U is the
        staircase's stated starting surface (§7.2), with the coads
        mismatch verdict when the pathway's slab spec disagrees.

        ``picked`` (any of ``U``/``pH``/``point`` given) adds the point
        verdict (gr489471): a state per rung, the worst rung and window
        membership, from :func:`~precis_pathway.staircase_states.
        point_states` — the same object the web picker reads. Its map is
        the one of the structure the pathway ran on (``candidate_ref``)
        when linked, else the first linked structure with a map."""
        from precis.quest import pourbaix_map
        from precis.quest import staircase as sq

        from .staircase_routes import render_staircase, routes_for_pathway
        from .staircase_states import pathway_coads, point_states

        results = meta.get("results") if isinstance(meta.get("results"), dict) else {}
        u = U if U is not None else sq.pathway_operating_u(meta.get("config"), results)
        sr = routes_for_pathway(store, ref)
        cfg = meta.get("config") or {}
        ph = sr.pH
        for c in (
            (cfg.get("conditions") or {}).get("pH"),
            (cfg.get("electrochemistry") or {}).get("pH"),
        ):
            if ph is None and isinstance(c, int | float) and not isinstance(c, bool):
                ph = float(c)
        structures = []
        point_map: tuple[int, dict[str, Any], dict[str, Any]] | None = None
        cand = meta.get("candidate_ref")
        for s in sq.structures_for_pathway(store, int(ref.id)):
            smeta = dict(s.meta or {})
            coads = sq.coads_of(smeta)
            handle = (
                handle_registry.try_format("structure", int(s.id))
                or f"structure:{s.id}"
            )
            resting, region = None, None
            maps = pourbaix_map.maps_for_structure(store, s)
            # the structure the pathway ran on wins over another linked one
            if maps and (point_map is None or int(s.id) == cand):
                point_map = (int(s.id), maps[0], smeta)
            for m in maps:
                region = sq.resting_region(m, u)
                if region is not None:
                    resting = str(region.get("label") or "?")
                    break
            structures.append(
                {
                    "handle": handle,
                    "resting": resting,
                    "assumed": sq.assumed_label(coads),
                    "mismatch": sq.mismatch(region, coads, u=u, label=resting),
                }
            )
        states = None
        if picked:
            if pH is not None:
                ph = pH
            states = point_states(
                sr,
                point_map[1] if point_map else None,
                u_rhe=u,
                ph=ph if ph is not None else 0.0,
                coads=pathway_coads(store, meta, point_map[2] if point_map else None),
            )
        return render_staircase(
            sr,
            results,
            u_rhe=u,
            title=ref.title,
            ph=ph,
            structures=structures,
            states=states,
        )

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

        topo: NetworkTopology = runner.network_topology(raw_config)
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
                store.chunks.insert_chunks(
                    ref.id,
                    [ChunkInsert(ord=0, text=body, meta={"chunk_kind": BODY_KIND})],
                    conn=conn,
                )
                ref_id = ref.id
            else:
                ref_id = existing.id
                store.stamp_ref_meta(ref_id, seed_meta, conn=conn)
                store.chunks.replace_body_chunk(
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
        topo: NetworkTopology | None = meta.get("topology") or (
            runner.network_topology(meta["config"]) if meta.get("config") else None
        )
        if topo:
            return "```mermaid\n" + topology_to_mermaid(topo) + "\n```"
        return "(no network to render)"

    def _intermediates(self, meta: dict[str, Any]) -> str:
        from . import runner
        from .text_views import topology_to_text

        topo: NetworkTopology | None = meta.get("topology") or (
            runner.network_topology(meta["config"]) if meta.get("config") else None
        )
        return topology_to_text(topo) if topo else "(no network)"

    # -- delete ----------------------------------------------------------
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None:
            raise BadInput("pathway delete needs an id")
        store = self.hub.live_store
        ref = _lookup(store, str(id))
        if ref is None:
            raise BadInput(f"no pathway '{id}'")
        store.retire_ref(ref.id)
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
        ref = _lookup(store, str(id))
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
    def _put_summary(slug: str, verb: str, artifact: PathwayArtifact) -> str:
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
    def _render_profile(
        title: str, meta: dict[str, Any], *, U: float | None = None
    ) -> str:
        r = meta.get("results", {})
        lines = [f"# {title}", ""]
        ref_name = r.get("pathway", [None])[0]
        lines.append(f"Energy reference: {r.get('energy_reference', '?')}")
        # ``results.nodes`` holds the pooled estimates (mean/std) but no CHE
        # stamp; the per-state ``n_H`` lives on the graph node of the same id.
        n_h: dict[str, Any] = {}
        if U is not None:
            from .toon_views import potential_line

            n_h = {
                str(n.get("id")): n.get("n_H")
                for n in (meta.get("graph") or {}).get("nodes", [])
            }
            lines.append(potential_line(U))
        lines.append("")
        lines.append(
            "## States (relative energy, eV"
            + (f", at U = {U:+.2f} V vs RHE)" if U is not None else ")")
        )
        nodes = r.get("nodes", {})
        for name in r.get("pathway", []):
            est = nodes.get(name, {})
            rel = _nf(est.get("mean"))
            std = _nf(est.get("std")) if est.get("std") is not None else 0.0
            flag = "  [LOW CONFIDENCE]" if est.get("low_confidence") else ""
            here = "  ←root" if name == ref_name else ""
            if U is not None:
                nh = n_h.get(name)
                if nh is None:
                    flag += "  [no n_H — unshifted]"
                else:
                    rel += float(nh) * U
                    here += f"  n_H={int(nh) if float(nh).is_integer() else nh}"
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
