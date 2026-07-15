"""RouteHandler — the retrosynthesis ``route`` kind (ADR 0056).

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
  the job (ADR 0044).
- ``get``    — list routes, or render one route graph (``id=slug``).
- ``delete`` — soft-retire a route.

Ships **dark** behind ``PRECIS_CHEM_ENABLED`` (``KindSpec.requires_env``):
the kind is hidden from the catalogue and the dispatcher until the flag is
set. See ``docs/design/chem-tools-integration.md`` + ADR 0056.
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_chem.engine import DEFAULT_MAX_STEPS, resolve_engine
from precis_chem.ir import RouteGraph, cache_key, normalize_smiles
from precis_chem.persist import apply_route_result

#: Env naming the compute node a ``retrosynth`` job pins to. Unset ⇒ the
#: handler runs the (in-process) engine inline — the slice-0 fallback that
#: keeps the round-trip testable without a cluster (catpath's EMT analogue).
ROUTE_NODE_ENV = "PRECIS_CHEM_ROUTE_NODE"


class RouteHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="route",
        title="Route",
        description=(
            "A retrosynthesis route-graph (precis-chem plugin, ADR 0056). "
            "put(id='<slug>', target='<SMILES>', engine='stub'|'aizynth', "
            "requested_by=<todo>) plans a synthetic route — a content-addressed "
            "cache hit if already planned, else an in-process solve or a minted "
            "retrosynth compute job. get lists routes or renders one graph "
            "(id=slug); delete soft-retires. The LLM traverses the graph, never "
            "runs a planner in the request path. See chem-tools-integration.md."
        ),
        supports_get=True,
        supports_put=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        role="artifact",
        corpus_role="none",
        can_own_jobs=True,
        # Dark-ship: the kind is hidden until the flag is set.
        requires_env=("PRECIS_CHEM_ENABLED",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("route: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        engine: str | None = None,
        title: str | None = None,
        requested_by: int | str | None = None,
        max_steps: int | None = None,
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
        key = cache_key(
            target=tgt,
            engine=eng.name,
            engine_version=eng.version,
            max_steps=steps,
        )

        existing = self.store.get_ref(kind="route", id=slug)
        # Content-addressed cache hit: same slug already carries a solved route
        # under this exact key ⇒ zero recompute (ADR 0007 / 0056 §6).
        if existing is not None:
            meta = existing.meta or {}
            if meta.get("cache_key") == key and meta.get("route"):
                graph = RouteGraph.from_json(meta["route"])
                return Response(
                    body=f"# route '{slug}' — cache hit (no recompute)\n\n"
                    + graph.render()
                )

        # Ensure the ref exists (create on first put; re-plan updates meta).
        if existing is None:
            ref = self.store.insert_ref(
                kind="route",
                slug=slug,
                title=(title or slug).strip() or slug,
                meta={
                    "target": tgt,
                    "engine": eng.name,
                    "engine_version": eng.version,
                    "cache_key": key,
                    "status": "planning",
                    "max_steps": steps,
                },
            )
        else:
            ref = existing
            self.store.stamp_ref_meta(
                ref.id,
                {
                    "target": tgt,
                    "engine": eng.name,
                    "engine_version": eng.version,
                    "cache_key": key,
                    "status": "planning",
                    "max_steps": steps,
                },
            )

        params = {
            "route_ref_id": ref.id,
            "target": tgt,
            "engine": eng.name,
            "engine_version": eng.version,
            "cache_key": key,
            "max_steps": steps,
        }

        node = os.environ.get(ROUTE_NODE_ENV)
        if node:
            # Compute lane: mint a derived job on the route node (ADR 0044).
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
        apply_route_result(self.store, ref.id, graph, cache_key=key)
        state = "solved" if graph.solved else "unsolved"
        return Response(
            body=f"# route '{slug}' — {state} ({graph.engine}, in-process)\n\n"
            + graph.render()
        )

    def _dispatch(
        self,
        ref: Any,
        params: dict[str, Any],
        node: str,
        requested_by: int | str | None,
    ) -> Response:
        """Mint a ``retrosynth`` job pinned to the route node (ADR 0044).

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
            from precis.handlers import _todo_guards as todo_guards

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
        return Response(
            body=(
                f"# route '{ref.slug}' dispatched to {node}{note}\n\n"
                f"{job_resp.body}\n\n"
                f"The plan lands on the route on completion. "
                f"Poll: get(kind='route', id='{ref.slug}')."
            )
        )

    def _wire_requester(self, requester_id: int, job_resp_body: str) -> None:
        """Link the requesting todo to the job + arm its wait (ADR 0044).

        ``requester --requested--> job`` (the edge ``derived_job_succeeded`` +
        the failure-bubble follow), then inject that evaluator as the todo's
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
    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
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
        if v and v not in ("route", "graph", "tree"):
            raise BadInput(
                f"unknown route view {view!r}",
                next="view='metrics' (route descriptors) | omit for the route graph",
            )
        return Response(body=graph.render())

    # ── delete ────────────────────────────────────────────────────────
    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='route') requires id= (the route slug)")
        ref = self.store.get_ref(kind="route", id=str(id).strip())
        if ref is None:
            raise NotFound(f"route {id!r} not found")
        self.store.soft_delete_ref(ref.id)
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
