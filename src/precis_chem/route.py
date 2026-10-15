"""RouteHandler — the retrosynthesis ``route`` kind.

A ``route`` is a slug-addressed authored artifact (like ``structure`` /
``cad`` / ``pcb``): a target molecule whose synthetic route-graph
(``meta.route``) is planned by a swappable engine on the compute lane and
read back by the LLM as a graph — never a synchronous planner call. Maps
onto the seven verbs:

- ``put``    — create a route to ``target=<SMILES>`` (``id=`` slug,
  ``engine=stub|aizynth``). Returns a content-addressed **cache hit** if the
  same target+engine was already planned; else runs the engine
  (in-process ``stub``) or mints a ``retrosynth`` compute job pinned to
  ``PRECIS_CHEM_ROUTE_NODE``. ``requested_by=<todo>`` blocks that todo on
  the job. ``constraints=['ewod-oil']`` declares an execution-platform
  constraint (``precis_chem.constraints``): recorded on the route, part of
  the content address, and screened advisorily over each planned step.
  ``strategy='<text>'`` records a STEER strategy (``precis_chem.steer``):
  *not* part of the content address (the search is unchanged; only the
  ranking layer depends on it), reranked inside the job when the plan
  lands — a ``put`` never makes an LLM call.
- ``get``    — list routes, or render one route graph (``id=slug``). When a
  recorded strategy has a cached rerank the page leads with the strategy
  pick and shows the engine's own top route beside it.
- ``delete`` — soft-retire a route.

Ships live: no per-plugin dark switch. An operator who needs it off uses
``PRECIS_KINDS_DISABLED``, the one general control.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_chem import steer
from precis_chem.classify import classify_route
from precis_chem.constraints import resolve_constraints, screen_route
from precis_chem.engine import DEFAULT_MAX_STEPS, resolve_engine
from precis_chem.ir import RouteGraph, cache_key, normalize_smiles
from precis_chem.persist import apply_route_result

#: Env naming the compute node a ``retrosynth`` job pins to. Unset ⇒ the
#: handler runs the (in-process) engine inline — the slice-0 fallback that
#: keeps the round-trip testable without a cluster (autocatpath's EMT analogue).
ROUTE_NODE_ENV = "PRECIS_CHEM_ROUTE_NODE"


class RouteHandler(Handler):
    #: SMILES / retrosynthesis route data: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="route",
        plugin_api=1,  # precis.protocol.PLUGIN_API this model is built for
        title="Route",
        description=(
            "A retrosynthesis route-graph (precis-chem plugin). "
            "put(id='<slug>', target='<SMILES>', engine='stub'|'aizynth', "
            "requested_by=<todo>, constraints=['ewod-oil'], "
            "strategy='<text>') plans a synthetic "
            "route — a content-addressed cache hit if already planned, else an "
            "in-process solve or a minted retrosynth compute job; a declared "
            "platform constraint (e.g. ewod-oil — EWOD digital microfluidics "
            "in oil) is recorded, cache-keyed, and advisorily screened per "
            "step; a declared strategy is recorded (not cache-keyed) and "
            "reranked inside the job. get lists routes or renders one graph "
            "(id=slug) — a scored strategy's pick leads the page; "
            "view='steer', q='<strategy>' reranks the kept candidate routes "
            "by an LLM's fit to a natural-language strategy; delete "
            "soft-retires. The LLM traverses the graph, never runs a planner "
            "in the request path."
        ),
        supports_get=True,
        supports_put=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        placement="artifact",
        corpus_role="none",
        can_own_jobs=True,
    )

    #: STEER scorer; None = the model router. Tests inject a fake.
    steer_ask: steer.Ask | None = None

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("route: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        engine: str | None = None,
        title: str | None = None,
        requested_by: int | str | None = None,
        max_steps: int | None = None,
        constraints: list[str] | str | None = None,
        strategy: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='route') requires id= (the route slug)",
                next="put(kind='route', id='aspirin', target='CC(=O)Oc1ccccc1C(=O)O')",
            )
        slug = str(id).strip()
        if target is None or not str(target).strip():
            raise BadInput(
                "put(kind='route') requires target= (the product SMILES)",
                next="put(kind='route', id='aspirin', target='CC(=O)Oc1ccccc1C(=O)O')",
            )
        tgt = normalize_smiles(target)
        # Resolve the engine now so an unknown name fails fast + we get its
        # version for the content address.
        try:
            eng = resolve_engine(engine)
        except ValueError as exc:
            raise BadInput(str(exc), next="engine='stub' | 'aizynth'") from exc
        steps = int(max_steps) if max_steps is not None else DEFAULT_MAX_STEPS
        try:
            declared = [c.name for c in resolve_constraints(constraints)]
        except ValueError as exc:
            raise BadInput(str(exc), next="constraints=['ewod-oil']") from exc
        strat: str | None = None
        if strategy is not None:
            try:
                strat = steer.normalise_strategy(strategy)
            except ValueError as exc:
                raise BadInput(
                    str(exc),
                    next="strategy='avoid protecting groups' "
                    f"(≤{steer.MAX_STRATEGY_CHARS} chars)",
                ) from exc
        key = cache_key(
            target=tgt,
            engine=eng.name,
            engine_version=eng.version,
            max_steps=steps,
            constraints=declared,
        )

        existing = self.store.get_ref(kind="route", id=slug)
        # Every strategy ever put against this slug, ordered + deduped — the
        # rerank reads the ref (not job params) so a put that idem-collapses
        # onto an in-flight job still gets its ranking.
        existing_meta = (existing.meta if existing is not None else None) or {}
        steer_strategies = [
            str(s) for s in (existing_meta.get("steer_strategies") or [])
        ]
        if strat is not None and strat not in steer_strategies:
            steer_strategies.append(strat)

        params = {
            "route_ref_id": 0,  # filled once the ref exists
            "target": tgt,
            "engine": eng.name,
            "engine_version": eng.version,
            "cache_key": key,
            "max_steps": steps,
            "constraints": declared,
        }
        if strat is not None:
            params["strategy"] = strat

        # Content-addressed cache hit: same slug already carries a solved route
        # under this exact key ⇒ zero recompute. The strategy is *not* part of
        # the address — a cache hit with an unranked strategy mints a
        # steer-only job (the dispatch sees the landed plan and skips the
        # engine), so two puts differing only in strategy share one run.
        if existing is not None:
            meta = existing.meta or {}
            if meta.get("cache_key") == key and meta.get("route"):
                graph = RouteGraph.from_json(meta["route"])
                head = f"# route '{slug}' — cache hit (no recompute)\n\n"
                if strat is None:
                    return Response(body=head + graph.render())
                self.store.stamp_ref_meta(
                    existing.id, {"steer_strategies": steer_strategies}
                )
                if steer.steer_key(strat, graph) in (meta.get("steer") or {}):
                    return Response(
                        body=head + f"strategy '{strat}' already ranked — "
                        "the route page leads with its pick.\n\n" + graph.render()
                    )
                node = os.environ.get(ROUTE_NODE_ENV)
                if node:
                    params["route_ref_id"] = existing.id
                    return self._dispatch(existing, params, node, requested_by)
                return Response(
                    body=head
                    + f"strategy '{strat}' recorded — no compute lane for the "
                    "rerank here; view='steer' scores it on demand.\n\n"
                    + graph.render()
                )

        # Ensure the ref exists (create on first put; re-plan updates meta).
        stamp = {
            "target": tgt,
            "engine": eng.name,
            "engine_version": eng.version,
            "cache_key": key,
            "status": "planning",
            "max_steps": steps,
            "constraints": declared,
        }
        if steer_strategies:
            stamp["steer_strategies"] = steer_strategies
        if existing is None:
            ref = self.store.insert_ref(
                kind="route",
                slug=slug,
                title=(title or slug).strip() or slug,
                meta=stamp,
            )
        else:
            ref = existing
            self.store.stamp_ref_meta(ref.id, stamp)
        params["route_ref_id"] = ref.id

        node = os.environ.get(ROUTE_NODE_ENV)
        if node:
            # Compute lane: mint a derived job on the route node.
            return self._dispatch(ref, params, node, requested_by)

        # Slice-0 inline fallback (no route node configured): run the
        # in-process engine now. A container engine raises here — tell the
        # caller to configure a node.
        try:
            graph = eng.plan(tgt, max_steps=steps)
        except NotImplementedError as exc:
            raise BadInput(
                f"engine '{eng.name}' needs a compute node: {exc}",
                next=f"set {ROUTE_NODE_ENV}=<node> (and build the wrapper image), "
                "or use engine='stub'",
            ) from exc
        graph = classify_route(screen_route(graph, declared))
        apply_route_result(self.store, ref.id, graph, cache_key=key)
        state = "solved" if graph.solved else "unsolved"
        body = f"# route '{slug}' — {state} ({graph.engine}, in-process)\n\n"
        if strat is not None:
            # No compute lane ⇒ no job to carry the rerank; the request path
            # stays LLM-free either way (steer is a job-time/view-time call).
            body += (
                f"strategy '{strat}' recorded — no compute lane for the "
                "rerank here; view='steer' scores it on demand.\n\n"
            )
        return Response(body=body + graph.render())

    def _dispatch(
        self,
        ref: Any,
        params: dict[str, Any],
        node: str,
        requested_by: int | str | None,
    ) -> Response:
        """Mint a ``retrosynth`` job pinned to the route node.

        The job is a *derived* compute step: it parents on the **route**, not
        a todo — the artifact owns it (cache-fillable, idempotent). When a
        caller names ``requested_by`` it also wants to block on the result; we
        then write a ``requested`` link + inject a ``derived_job_succeeded``
        auto_check so that todo closes on success / bubbles on failure.
        Mirrors ``StructureHandler._dispatch_relax``.
        """
        from precis.handlers.job import JobHandler

        requester_id = _as_int_or_none(requested_by)
        if requester_id is not None:
            from precis.todo import guards as todo_guards

            todo_guards.check_parent_exists(self.store, requester_id)

        job_params = dict(params)
        job_params["target_node"] = node

        hub = self.hub if self.hub is not None else Hub(store=self.store)
        job_resp = JobHandler(hub=hub).put(
            job_type="retrosynth",
            executor="ssh_node",
            parent_id=ref.id,  # the artifact owns the job (compute lane)
            params=job_params,
            # Collapse re-submits of the same plan onto one in-flight job.
            idem_key=params["cache_key"],
        )
        note = ""
        if requester_id is not None:
            self._wire_requester(requester_id, job_resp.body)
            note = f" (todo #{requester_id} will block on it)"
        # A cache-hit put (plan already landed) mints a steer-only job.
        planned = (ref.meta or {}).get("route") and (ref.meta or {}).get(
            "cache_key"
        ) == params["cache_key"]
        lands = (
            "The strategy rerank lands on the route on completion (the plan "
            "itself is a cache hit — no engine re-run)."
            if planned
            else "The plan lands on the route on completion."
        )
        strategy_note = ""
        if params.get("strategy"):
            strategy_note = (
                f"strategy '{params['strategy']}' recorded — the rerank runs "
                "in the job, not here.\n\n"
            )
        return Response(
            body=(
                f"# route '{ref.slug}' dispatched to {node}{note}\n\n"
                f"{strategy_note}"
                f"{job_resp.body}\n\n"
                f"{lands} "
                f"Poll: get(kind='route', id='{ref.slug}')."
            )
        )

    def _wire_requester(self, requester_id: int, job_resp_body: str) -> None:
        """Link the requesting todo to the job + arm its wait.

        ``requester --requested--> job`` (the edge ``derived_job_succeeded`` +
        the failure bubble follow), then inject that evaluator as the todo's
        ``auto_check`` when it has none. Idempotent. Copied from
        ``StructureHandler._wire_requester``.
        """
        m = re.search(r"id=(\d+)", job_resp_body)
        if m is None:
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

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        refresh: bool = False,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        ref = self.store.get_ref(kind="route", id=str(id).strip())
        if ref is None:
            raise NotFound(f"route {id!r} not found")
        meta = ref.meta or {}
        blob = meta.get("route")
        if not blob:
            status = meta.get("status") or "planning"
            target = meta.get("target") or "?"
            return Response(
                body=f"# route '{ref.slug}' — {status}\n\n"
                f"target: {target}\nengine: {meta.get('engine', '?')}\n\n"
                "(no route yet — the compute job hasn't landed; poll again)"
            )
        graph = RouteGraph.from_json(blob)
        v = (view or "").strip().lower()
        if v in ("metrics", "descriptors", "score"):
            # Route-level descriptors — the scoring substrate (slice 2). Only the
            # LinChemIn-normalized path populates them; a stub/legacy route says so.
            return Response(body=graph.metrics_render())
        if v == "steer":
            return self._render_steer(ref, graph, q=q, refresh=bool(refresh))
        if v and v not in ("route", "graph", "tree"):
            raise BadInput(
                f"unknown route view {view!r}",
                next=(
                    "view='metrics' (route descriptors) | view='steer', "
                    "q='<strategy>' (LLM rerank) | omit for the route graph"
                ),
            )
        return Response(body=self._page_render(meta, graph))

    def _render_steer(
        self, ref: Any, graph: RouteGraph, *, q: str | None, refresh: bool
    ) -> Response:
        """Rerank the kept routes against the strategy in ``q=``. Scores are
        cached on ``meta.steer`` keyed by strategy + routes, so a repeat
        costs nothing; ``refresh=True`` re-asks."""
        try:
            strategy = steer.normalise_strategy(q)
        except ValueError as exc:
            raise BadInput(
                str(exc),
                next=f"get(kind='route', id={ref.slug!r}, view='steer', "
                "q='avoid protecting groups')",
            ) from exc
        key = steer.steer_key(strategy, graph)
        cache = dict((ref.meta or {}).get("steer") or {})
        hit = cache.get(key)
        if hit and not refresh:
            scores = [steer.SteerScore.from_json(s) for s in hit.get("scores") or []]
            return Response(body=steer.render(strategy, graph, scores, cached=True))
        scores = steer.steer(strategy, graph, ask=self.steer_ask)
        if any(s.score is not None for s in scores):
            cache[key] = {
                "strategy": strategy,
                "prompt": steer.PROMPT_VERSION,
                "scores": [s.to_json() for s in scores],
                "at": datetime.now(UTC).isoformat(),
            }
            self.store.stamp_ref_meta(ref.id, {"steer": cache})
        return Response(body=steer.render(strategy, graph, scores, cached=False))

    def _page_render(self, meta: dict[str, Any], graph: RouteGraph) -> str:
        """The route page: when a recorded strategy has a cached rerank, lead
        with its pick and show the engine's own top route beside it (the
        newest recorded strategy with a ranking wins)."""
        strategies = [str(s) for s in meta.get("steer_strategies") or []]
        cache = meta.get("steer") or {}
        for s in reversed(strategies):
            hit = cache.get(steer.steer_key(s, graph))
            if hit:
                scores = [
                    steer.SteerScore.from_json(x) for x in hit.get("scores") or []
                ]
                return steer.render_page(s, graph, scores)
        body = graph.render()
        if strategies:
            body += (
                f"\n\nstrategy '{strategies[-1]}' recorded — the rerank runs "
                "in the plan job; not landed yet. "
                "view='steer', q='<strategy>' scores on demand."
            )
        return body

    # ── delete ────────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='route') requires id= (the route slug)")
        ref = self.store.get_ref(kind="route", id=str(id).strip())
        if ref is None:
            raise NotFound(f"route {id!r} not found")
        self.store.retire_ref(ref.id)
        return Response(body=f"retired route '{ref.slug}'")

    # ── helpers ────────────────────────────────────────────────────────
    def _render_list(self) -> Response:
        routes = self.store.list_refs(kind="route", order_by="id_desc", limit=50)
        if not routes:
            return Response(
                body="no routes yet\n\nNext: put(kind='route', id='aspirin', "
                "target='CC(=O)Oc1ccccc1C(=O)O')"
            )
        lines = [f"# {len(routes)} route(s)"]
        for r in routes:
            meta = r.meta or {}
            status = meta.get("status") or "?"
            target = meta.get("target") or "?"
            lines.append(f"- {r.slug}  [{status}]  {target}")
        return Response(body="\n".join(lines))


def _as_int_or_none(v: Any) -> int | None:
    """Coerce a requester id to int, tolerating a ``todo:<n>`` / string id."""
    if v is None:
        return None
    raw = str(v).strip()
    raw = raw.split(":", 1)[1] if raw.startswith("todo:") else raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
