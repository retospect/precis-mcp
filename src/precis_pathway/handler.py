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

import json
import logging
import math
import os
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
from precis.handlers._mode_help import require_mode
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.text import slugify

if TYPE_CHECKING:
    from precis.store import Store

    from .environment import Environment
    from .types import NetworkTopology, PathwayArtifact

from .persist import BODY_KIND, pathway_title, persist_result, quest_minted_slug

#: When set, `put` routes the compute to a `autocatpath_explore` job pinned to this
#: node instead of running autocatpath in-process (slice 1). The gateway sets it to
#: the GPU node's PRECIS_NODE (e.g. 'spark'); unset → in-process EMT (slice 0).
_ROUTE_NODE_ENV = "PRECIS_AUTOCATPATH_ROUTE_NODE"

#: mode='verify' seed-count bounds (gr543165): default 3 independent
#: seeds up front, minimum 2 — the ruling's "≥2 at dispatch (default
#: 3), not one seed plus rerun-on-fail".
_VERIFY_DEFAULT_SEEDS = 3
_VERIFY_MIN_SEEDS = 2

log = logging.getLogger(__name__)


#: Views that accept the CHE potential lever ``args={'U': <V vs RHE>}``
#: (docs/backlog/pathway-conditions-effects-report.md Phase 1 item 1;
#: 'staircase' re-tilts the affine export — SPSO slice 3).
_POTENTIAL_VIEWS = ("analysis", "profile", "compare", "staircase")

#: The ladder rungs ``args={'tier'}`` accepts — the same vocabulary
#: ``precis.quest.compute._TIERS`` declares, duplicated here (with
#: :data:`_TIER_CASE`) so a search filter never imports the orchestrator.
_SEARCH_TIERS = ("screening", "neb", "verify")

#: SQL mirror of :func:`precis.quest.compute._pathway_tier` behind the
#: ``args={'tier'}`` search filter (gr592660): the stamped ``meta.tier``
#: wins; an unstamped run derives from ``results`` (``screening: true`` →
#: 'screening', ``template='coadsorbed'`` with a falsy ``neb_schedule`` →
#: 'verify'), else 'neb'. The pathway refs alias is ``p``. The falsy list
#: is every JSON spelling Python's ``not`` reads as false — ``null``
#: included: ``#>`` on an explicit ``"neb_schedule": null`` yields the
#: jsonb literal 'null', not SQL NULL (missing key is the COALESCE arm).
_TIER_CASE = """\
CASE WHEN p.meta->>'tier' IN ('screening', 'neb', 'verify')
          THEN p.meta->>'tier'
     WHEN p.meta #> '{results,screening}' = 'true'::jsonb
          THEN 'screening'
     WHEN p.meta #>> '{results,template}' = 'coadsorbed'
          AND COALESCE(p.meta #> '{results,neb_schedule}', 'null'::jsonb)
              IN ('null'::jsonb, 'false'::jsonb, '0'::jsonb,
                  '""'::jsonb, '[]'::jsonb, '{}'::jsonb)
          THEN 'verify'
     ELSE 'neb' END"""


def _like_escape(text: str) -> str:
    """A literal LIKE pattern for ``text`` — the default ``\\`` escape."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


def _gate_arg(args: dict[str, Any]) -> str | None:
    """``args['gate']`` — the ``view='compare'`` row filter: 'pass' or
    'blocked' (gr592278). Absent → ``None``. Pops the key so
    :func:`_point_args` keeps rejecting every other unknown arg."""
    if "gate" not in args:
        return None
    raw = args.pop("gate")
    val = str(raw).strip().lower()
    if val not in ("pass", "blocked"):
        raise BadInput(
            f"args={{'gate': {raw!r}}}: gate is 'pass' or 'blocked'",
            next="get(kind='pathway', id=<slug>, view='compare', "
            "args={'gate': 'pass'})",
        )
    return val


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


def _handle_line(ref: Any) -> str:
    """The one-line header every pathway read leads with (gr553326): the
    ``pw<id>`` universal handle plus the ``/refs/pathway/<id>`` web reader
    path — the two routes a bare slug never reached."""
    handle = handle_registry.try_format("pathway", int(ref.id)) or f"pw{ref.id}"
    return f"{handle} · /refs/pathway/{ref.id}"


#: The kinetics v2 blocks ``put`` accepts as ``args=`` (D1), overlaid onto
#: the YAML body's own keys of the same name.
_ENV_ARGS = ("environment", "objective")

_ENV_KEY_RE = re.compile(r"\b((?:environment|objective)(?:\.\w+)*(?:\[[^\]]+\])?)")

_ENV_EXAMPLE = (
    "environment: {T: 298.15, gases: {inputs: {NO: 0.1, H2: 1.0}, "
    "outputs: far}, sticking: {NO: {s0: 0.5, source: pa498804}}}"
)


def _env_overlay(raw: Any, args: dict[str, Any] | None) -> Any:
    """The YAML config with ``args['environment'|'objective']`` laid over
    it. Stating a block in both places with different content is refused:
    one config, one environment."""
    if not args:
        return raw
    out = dict(raw) if isinstance(raw, dict) else {}
    for key in _ENV_ARGS:
        if key not in args:
            continue
        if key in out and out[key] != args[key]:
            raise BadInput(
                f"pathway {key} given in both the YAML body and args with "
                "different content",
                next=f"drop `{key}:` from the YAML body or from args=",
            )
        out[key] = args[key]
    return out


def _is_quest_pathway(meta: dict[str, Any], slug: str | None) -> bool:
    """A quest's barrier lane minted this pathway — the mint-stamped
    ``meta.candidate_ref``, or the ``q<N>cand-<digest>`` slug head on
    refs that predate the stamp (:func:`persist.quest_minted_slug`)."""
    return meta.get("candidate_ref") is not None or quest_minted_slug(slug)


def _env_key(message: str) -> str | None:
    """The config key an engine refusal names (``environment.gases.outputs``
    …), or ``None`` when the error is not about the D1 blocks."""
    m = _ENV_KEY_RE.search(message)
    return m.group(1) if m else None


def _env_next(key: str) -> str:
    return (
        f"fix `{key}` in the config YAML (or args={{'environment': {{...}}}}); "
        f"shape: {_ENV_EXAMPLE} — see get(kind='skill', id='precis-pathway-help')"
    )


class PathwayHandler(Handler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with :func:`_point_args`.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "get": ArgsSpec(
            {"U", "pH", "point", "environment", "axis", "class", "page_size", "offset"}
        ),
        # kinetics v2 D1 blocks, overlaid onto the YAML body (_env_overlay);
        # 'seeds' is mode='verify' only (the multi-seed dispatch count/list).
        "put": ArgsSpec({"environment", "objective", "manifest", "sweep", "seeds"}),
        # Search filters on stored meta (gr592660) — the declaration is
        # also what lets dispatch reject a bogus args= key naming these.
        "search": ArgsSpec({"candidate", "engine", "status", "tier"}),
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
            "handle, filtered by args={'status'|'tier'|'engine'|'candidate'}. "
            "Slice 0 runs EMT in-process (qualitative). "
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
        # 'revalidate' (kinetics v2 D10) mints autocatpath_revalidate for a
        # stored result's validation manifest — no config body needed.
        # 'recompute_kinetics' mints autocatpath_kinetics for a stored
        # pathway, optionally under an overriding environment/objective/sweep.
        # 'verify' (gr543165): multi-seed-at-dispatch — one call that
        # reruns the pathway's candidate NEB on N fresh seeds.
        # 'measure_modes' mints autocatpath_modes: the normal modes a stored
        # vib-priced pathway lacks, from its seed jobs' geometries.
        modes=(
            "preview",
            "revalidate",
            "recompute_kinetics",
            "verify",
            "measure_modes",
        ),
        views=(
            "analysis",
            "compare",
            "intermediates",
            "steps",
            "kinetics",
            "kinetics-sweep",
            "kinetics-summary",
            "optimum",
            "objective",
            "tpd",
            "manifest",
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

    def seed_geometry(self, pathway_ref_id: int) -> tuple[dict[str, str], str]:
        """Expose stored seed evidence through the kind boundary for exports."""
        from .ingest import seed_geometry

        return seed_geometry(self.hub.live_store, pathway_ref_id)

    # -- put -------------------------------------------------------------
    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if mode == "verify":  # engine-free: the job checks the host
            return self._verify(id, args)
        if mode == "revalidate":  # engine-free: the job checks the host
            return self._revalidate(id, args)
        if mode == "recompute_kinetics":  # the engine is optional on dispatch hosts
            return self._recompute_kinetics(id, args)
        if mode == "measure_modes":  # engine-free: the job checks the host
            return self._measure_modes(id)
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

            raw = _env_overlay(_load_yaml(text), args)
            effective = runner.effective_config(raw, force_backend=force)
            key = runner.content_key(effective)
            slug = slugify(
                str(id or effective.get("name") or "pathway"), default="pathway"
            )
        except BadInput:
            raise
        except Exception as e:
            env_key = _env_key(str(e))
            if env_key is not None:
                raise BadInput(
                    f"pathway environment/objective refused: {e}",
                    next=_env_next(env_key),
                ) from e
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
        # an args= environment/objective overlay lives only in `raw`, not `text`
        overlaid = any(k in (args or {}) for k in _ENV_ARGS)
        artifact: PathwayArtifact = (
            runner.run_pathway(raw, force_backend="emt")
            if overlaid
            else runner.run_pathway_from_yaml(text, force_backend="emt")
        )
        if existing is None:
            with store.tx() as conn:
                ref = store.insert_ref(
                    kind="pathway",
                    slug=slug,
                    title=pathway_title(artifact) or f"pathway {slug}",
                    meta={"status": "computing", "created_by": "put:pathway:emt"},
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
                    meta={**seed_meta, "created_by": "put:pathway:dispatch"},
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

    def _verify(self, ident: str | int | None, args: dict[str, Any] | None) -> Response:
        """``mode='verify'`` (gr543165 — multi-seed at dispatch): dispatch
        N independent seeds for the pathway's NEB in one call, through
        :func:`precis.quest.compute.request_rerun`'s rerun mint (a fresh
        nonce-salted tree, the prior result kept linked for comparison).

        ``args={'seeds': 3}`` is a COUNT — default 3, minimum 2 — of
        fresh seed numbers minted past every seed the pathway already
        registered (a seed an earlier run used counts once for
        independence, so verify always adds NEW numbers); a list passes
        verbatim through the rerun's own seed validation. Refuses with
        ``next=`` when the pathway has no NEB inputs (no
        ``meta.candidate_ref`` — it wasn't minted by a quest's barrier
        lane, so there is no candidate whose NEB to re-run); the
        rerun's own bounds (quest lane off, no reaction config, an eval
        already in flight) surface their refusals verbatim."""
        store = self.hub.live_store
        if ident is None:
            raise BadInput(
                "mode='verify' needs the pathway handle (id=)",
                next="put(kind='pathway', id='pw123', mode='verify', "
                "args={'seeds': 3})",
            )
        ref = _lookup(store, str(ident))
        if ref is None:
            raise BadInput(
                f"no pathway '{ident}'", next="get(kind='pathway') lists all"
            )
        meta = ref.meta if isinstance(ref.meta, dict) else {}
        cand = meta.get("candidate_ref")
        if isinstance(cand, str) and cand.isdigit():
            cand = int(cand)
        if not isinstance(cand, int) or isinstance(cand, bool):
            raise BadInput(
                f"pathway '{ident}' has no NEB inputs — it was not minted "
                "by a quest's barrier lane (no candidate_ref)",
                next=f"get(kind='pathway', id={str(ident)!r}, view='trust') "
                "to read its gate; verify reruns a candidate's NEB",
            )
        from precis.quest.compute import _validate_rerun_seeds, request_rerun
        from precis.utils import trust_gate

        raw = (args or {}).get("seeds", _VERIFY_DEFAULT_SEEDS)
        if isinstance(raw, list | tuple):
            seed_list = _validate_rerun_seeds(raw)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            if raw < _VERIFY_MIN_SEEDS:
                raise BadInput(
                    f"mode='verify' needs at least {_VERIFY_MIN_SEEDS} seeds "
                    f"(got {raw}) — a barrier is credited on ≥2 independent "
                    "seeds",
                    next=f"args={{'seeds': {_VERIFY_DEFAULT_SEEDS}}}",
                )
            results = meta.get("results")
            results = results if isinstance(results, dict) else {}
            known = (
                trust_gate.run_seeds(
                    [r for r in results.get("trust") or [] if isinstance(r, dict)],
                    declared=results.get("seeds"),
                )
                or set()
            )
            for key in ("requested_seeds", "executed_seeds"):
                known |= {
                    int(s)
                    for s in meta.get(key) or []
                    if isinstance(s, int) and not isinstance(s, bool)
                }
            hi = max(known, default=-1)
            seed_list = list(range(hi + 1, hi + 1 + raw))
        else:
            raise BadInput(
                f"args={{'seeds': {raw!r}}}: seeds is a count "
                f"(default {_VERIFY_DEFAULT_SEEDS}, minimum "
                f"{_VERIFY_MIN_SEEDS}) or an explicit list of seed ints",
                next=f"args={{'seeds': {_VERIFY_DEFAULT_SEEDS}}}",
            )
        ack = request_rerun(store, cand, hub=self.hub, seeds=seed_list)
        pw_handle = handle_registry.try_format("pathway", int(ref.id)) or f"pw{ref.id}"
        return Response(
            body=(
                f"verify {pw_handle}: dispatched {len(seed_list)} seed(s) "
                f"{seed_list} for the pathway's NEB — {ack}"
            )
        )

    def _revalidate(
        self, ident: str | int | None, args: dict[str, Any] | None
    ) -> Response:
        """``mode='revalidate'``: mint ``autocatpath_revalidate`` for the
        pathway's stored validation manifest (kinetics v2 D10). The job, not
        this call, checks the lock hash on the host that runs it."""
        store = self.hub.live_store
        if ident is None:
            raise BadInput(
                "mode='revalidate' needs the pathway handle (id=)",
                next="put(kind='pathway', id='pw123', mode='revalidate', "
                "args={'manifest': '<manifest_id>'})",
            )
        ref = _lookup(store, str(ident))
        if ref is None:
            raise BadInput(
                f"no pathway '{ident}'", next="get(kind='pathway') lists all"
            )
        stored = (ref.meta or {}).get("manifest_id")
        if not stored:
            raise BadInput(
                f"pathway '{ident}' carries no validation manifest (aggregated "
                "before autocatpath 0.38)",
                next=f"re-run it on the current engine, then get(kind='pathway', "
                f"id={str(ident)!r}, view='manifest')",
            )
        want = str((args or {}).get("manifest") or stored)
        if want != stored:
            raise BadInput(
                f"manifest {want[:12]} is not this pathway's ({str(stored)[:12]})",
                next=f"put(kind='pathway', id={str(ident)!r}, mode='revalidate', "
                f"args={{'manifest': {stored!r}}})",
            )
        from precis.handlers.job import JobHandler

        job = JobHandler(hub=self.hub).put(
            job_type="autocatpath_revalidate",
            executor="ssh_node",
            parent_id=ref.id,
            idem_key=f"autocatpath_revalidate:{ref.id}:{stored}",
            requires={},
            params={
                "pathway_ref_id": ref.id,
                "manifest_id": stored,
            },
        )
        return Response(
            body=(
                f"re-validation of manifest {str(stored)[:12]} queued. {job.body}\n"
                f"Verdict: get(kind='pathway', id={str(ident)!r}, view='manifest')"
            )
        )

    def _measure_modes(self, ident: str | int | None) -> Response:
        """``mode='measure_modes'``: mint ``autocatpath_modes`` for a stored
        vib-priced pathway whose record has no modes, which the sweep's re-T
        rule needs to vary T."""
        from precis_pathway import modes_job

        store = self.hub.live_store
        if ident is None:
            raise BadInput(
                "mode='measure_modes' needs the pathway handle (id=)",
                next="put(kind='pathway', id='pw123', mode='measure_modes')",
            )
        ref = _lookup(store, str(ident))
        if ref is None:
            raise BadInput(
                f"no pathway '{ident}'", next="get(kind='pathway') lists all"
            )
        thermo = ((ref.meta or {}).get("results") or {}).get("thermo") or {}
        if thermo.get("mode") != "vib":
            raise BadInput(
                f"pathway '{ident}' is not vib-priced (thermo.mode="
                f"{thermo.get('mode')!r}); it has no measured modes to add",
                next=f"get(kind='pathway', id={str(ident)!r}, view='methods')",
            )
        lacking = (
            set(thermo.get("state_corrections") or {})
            - set(thermo.get("vib_fallback") or [])
            - set(thermo.get("state_modes") or {})
        )
        if not lacking:
            return Response(body=f"pathway '{ident}' already has modes for every state")
        if not modes_job.seed_jobs(store, ref.id):
            raise BadInput(
                f"pathway '{ident}' has no succeeded seed job with stored geometry",
                next="re-run the pathway: new seeds store their modes",
            )
        from precis.handlers.job import JobHandler

        job = JobHandler(hub=self.hub).put(
            job_type=modes_job.NAME,
            executor="ssh_node",
            parent_id=ref.id,
            idem_key=f"{modes_job.NAME}:{ref.id}",
            requires={"gpu": 1},
            params={"pathway_ref_id": ref.id, "pathway_slug": ref.slug or str(ident)},
        )
        return Response(
            body=(
                f"mode measurement queued for '{ident}' ({len(lacking)} states "
                f"lack modes). {job.body}\n"
                f"Then: put(kind='pathway', id={str(ident)!r}, "
                "mode='recompute_kinetics')"
            )
        )

    def _recompute_kinetics(
        self, ident: str | int | None, args: dict[str, Any] | None
    ) -> Response:
        """``mode='recompute_kinetics'``: mint ``autocatpath_kinetics`` for a
        stored pathway so its ``kinetics_v2`` (and sweep) are re-solved on the
        current engine under the stored conditions — an explicit
        ``environment`` / ``objective`` / ``sweep`` arg overrides only its own
        key; a key left unset falls back to the stored value (the last
        solve's record, then the config's blocks, then the legacy
        conditions). A pathway with no usable stored environment is refused
        here, not after the solve burns a run (gr550976). The environment
        is validated locally and, when installed, by the compute engine;
        objective mappings and an explicit ``sweep`` are shape-checked at the door
        (:func:`kinetics_job.sweep_shape_error` — the refusal names the
        accepted keys). Hosts without the engine defer its checks to the job.
        A quest-minted pathway with no sweep arg and no
        config-declared ``kinetics.sweep`` takes
        :data:`kinetics_job.QUEST_KINETICS_SWEEP` instead of the stored
        record echo (gr543723); a non-quest pathway keeps the
        stored/engine box."""
        store = self.hub.live_store
        example = (
            "put(kind='pathway', id='pw123', mode='recompute_kinetics', "
            f"args={{'environment': {{...}}}}) — {_ENV_EXAMPLE}"
        )
        if ident is None:
            raise BadInput(
                "mode='recompute_kinetics' needs the pathway handle (id=)",
                next=example,
            )
        ref = _lookup(store, str(ident))
        if ref is None:
            raise BadInput(
                f"no pathway '{ident}'", next="get(kind='pathway') lists all"
            )
        meta = dict(ref.meta or {})
        config = meta.get("config")
        results = meta.get("results") or {}
        if not isinstance(config, dict) or not config:
            raise BadInput(
                f"pathway '{ident}' stores no config to recompute from",
                next=f"get(kind='pathway', id={str(ident)!r}, view='config')",
            )
        if not results.get("nodes") or not results.get("edges"):
            raise BadInput(
                f"pathway '{ident}' has no aggregated results yet "
                f"(status {meta.get('status')!r})",
                next="wait for the aggregate, or re-run the pathway first",
            )
        given = dict(args or {})
        env = given.get("environment")
        obj = given.get("objective")
        sweep = given.get("sweep")
        for name, val in (("environment", env), ("objective", obj), ("sweep", sweep)):
            if val is not None and not isinstance(val, dict):
                raise BadInput(
                    f"args['{name}'] must be a mapping, got {type(val).__name__}",
                    next=example,
                )
        from .aggregate_job import kinetics_idem_key
        from .kinetics_job import (
            QUEST_KINETICS_SWEEP,
            SWEEP_KEYS_HINT,
            overlay_recompute_config,
            stored_environment,
            stored_objective,
            stored_sweep,
            sweep_shape_error,
        )

        if sweep is not None:
            reason = sweep_shape_error(sweep)
            if reason is not None:
                raise BadInput(
                    f"args['sweep'] refused: {reason}",
                    next=SWEEP_KEYS_HINT,
                )

        # an explicit arg overrides only its own key; a key it leaves unset
        # falls back to the pathway's stored value — the last solve's
        # kinetics record first, then the config's own blocks, then the
        # legacy conditions the config already prices (gr550976).
        env_stored, env_src = stored_environment(meta, config, results)
        obj_stored, obj_src = stored_objective(meta, config, results)
        sweep_stored, sweep_src = stored_sweep(meta, config, results)
        if env is None:
            env = env_stored
        # Config-backed environments need no overlay, but still pass the
        # same door checks as explicit and resolved-record environments.
        if env is None and env_src == "config":
            env = config["environment"]
        if obj is None:
            obj = obj_stored
        if obj is None and obj_src == "config":
            obj = config["objective"]
        sweep_default = False
        if sweep is None:
            # a caller-declared box always wins (args above, or the
            # config's own kinetics.sweep stored_sweep reports as
            # "config"); else a quest-minted pathway takes the widened
            # precis-side default, not the stale record echo (gr543723)
            if sweep_src != "config" and _is_quest_pathway(meta, ref.slug):
                sweep = QUEST_KINETICS_SWEEP
                sweep_default = True
            else:
                sweep = sweep_stored
        if env is not None:
            from .environment import Environment as PathwayEnvironment

            who = "args" if given.get("environment") is not None else "stored"
            try:
                point = PathwayEnvironment.from_dict(env)
            except BadInput as e:
                env_key = _env_key(str(e))
                raise BadInput(
                    f"pathway {who} environment refused: {e}",
                    next=_env_next(env_key or "environment"),
                ) from e
            if "ion_conc_M" in point.electrochemical:
                raise BadInput(
                    f"pathway {who} environment refused: "
                    "recompute_kinetics refuses electrochemical.ion_conc_M: "
                    "the engine cannot price this read-only condition; "
                    "a recompute must not silently drop it",
                    next=(
                        "Use get(kind='pathway', view='kinetics', "
                        "args={'environment': ...}) to look up this condition; "
                        "request a recompute without ion_conc_M only if a solve "
                        "without that condition is intended."
                    ),
                )
            # Validate without replacing the original block: normalization
            # must not change the stored conditions sent to the worker.
            try:
                from autocatpath.environment import Environment
            except ImportError:
                pass
            else:
                try:
                    Environment.from_dict(env)
                except Exception as e:
                    env_key = _env_key(str(e))
                    raise BadInput(
                        f"pathway {who} environment refused: {e}",
                        next=_env_next(env_key or "environment"),
                    ) from e
        if obj is not None:
            try:
                from autocatpath.environment import Objective
            except ImportError:
                pass
            else:
                try:
                    Objective.from_dict(obj)
                except Exception as e:
                    env_key = _env_key(str(e))
                    who = "args" if given.get("objective") is not None else "stored"
                    raise BadInput(
                        f"pathway {who} objective refused: {e}",
                        next=_env_next(env_key or "objective"),
                    ) from e
        if env is None and env_src is None:
            raise BadInput(
                f"pathway '{ident}' stores no environment to re-solve under "
                "(no environment block, no kinetics_v2 record env and no "
                "conditions.pressures — the (T, p_total) sweep needs a gas "
                "table to scale)",
                next=example,
            )
        new_config = overlay_recompute_config(
            config, environment=env, objective=obj, sweep=sweep
        )
        from precis.handlers.job import JobHandler

        job = JobHandler(hub=self.hub).put(
            job_type="autocatpath_kinetics",
            executor="ssh_node",
            parent_id=ref.id,
            idem_key=kinetics_idem_key(ref.id, new_config, results),
            requires={},
            params={
                "pathway_ref_id": ref.id,
                "pathway_slug": ref.slug or str(ident),
                "config": new_config,
                "content_key": meta.get("content_key"),
            },
        )
        overlaid = [
            k
            for k, v in (
                ("environment", given.get("environment")),
                ("objective", given.get("objective")),
                ("sweep", given.get("sweep")),
            )
            if v is not None
        ]
        reused = [
            f"{k} ({src})"
            for k, v, src in (
                ("environment", given.get("environment"), env_src),
                ("objective", given.get("objective"), obj_src),
                ("sweep", given.get("sweep"), None if sweep_default else sweep_src),
            )
            if v is None and src is not None
        ]
        parts = []
        if overlaid:
            parts.append(f"overriding {', '.join(overlaid)}")
        if reused:
            parts.append(f"reusing stored {', '.join(reused)}")
        if sweep_default:
            parts.append(
                "quest default sweep box (T 350-900 K, p_total 0.1-100 bar, 8x8)"
            )
        detail = f" — {'; '.join(parts)}" if parts else ""
        return Response(
            body=(
                f"kinetics recompute queued for '{ident}'{detail}. {job.body}\n"
                f"Result: get(kind='pathway', id={str(ident)!r}, view='kinetics') "
                "and view='kinetics-sweep'"
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
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Find pathways by reaction label / slug words, or by a linked
        structure / quest handle (gr489581 — search used to be
        Unsupported; agents detoured through structure fisheyes).

        A structure handle pivots on the pathway's ``meta.candidate_ref``
        stamp — the same rows ``view='pathways'`` lists — plus link rows
        either direction (gr592660: links alone found nothing, the stamp
        is the live mark). A quest handle resolves the population
        ``view='kinetics-summary'`` counts: direct ``serves`` links plus
        pathways whose candidate serves the quest. ``args=`` filters the
        matches on stored meta — ``status`` / ``tier`` / ``engine``
        (a substring of ``meta.autocatpath_version``) / ``candidate``
        (an ``st<N>`` handle, ref id or structure slug)."""
        store = self.hub.live_store
        text = (q or "").strip()
        extra = args or {}
        status_f = extra.get("status")
        tier_f = extra.get("tier")
        engine_f = extra.get("engine")
        candidate_f = extra.get("candidate")
        if not text and not any(
            v is not None for v in (status_f, tier_f, engine_f, candidate_f)
        ):
            raise BadInput(
                "pathway search needs q= (a reaction label, slug words, "
                "or a structure/quest handle) or an args= filter",
                next="search(kind='pathway', q='NO→NH3') · "
                "search(kind='pathway', q='se123') · "
                "search(kind='pathway', args={'status': 'ready'}) · "
                "get(kind='pathway') lists all",
            )
        tier: str | None = None
        if tier_f is not None:
            tier = str(tier_f).strip().lower()
            if tier not in _SEARCH_TIERS:
                raise BadInput(
                    f"unknown pathway tier {tier_f!r}",
                    options=list(_SEARCH_TIERS),
                    next="search(kind='pathway', args={'tier': 'screening'})",
                )
        cand_filter: str | None = None
        if candidate_f is not None:
            cand_txt = str(candidate_f).strip()
            pc = handle_registry.parse(cand_txt)
            if pc is not None and not pc[1] and pc[0] == "structure":
                cand_filter = str(pc[2])
            elif cand_txt.isdigit():
                cand_filter = cand_txt
            else:
                c = store.get_ref(kind="structure", id=cand_txt)
                if c is None:
                    raise BadInput(
                        f"args['candidate'] {candidate_f!r} names no live "
                        "structure (an st<N> handle, ref id or slug)",
                        next="search(kind='pathway', args={'candidate': 'st123'})",
                    )
                cand_filter = str(c.id)

        # A handle (st123 / qu7) names a neighbour to pivot from: inbound
        # links for any kind; a structure adds its candidate_ref stamp
        # and outgoing links, a quest the kinetics-summary scope.
        parsed = handle_registry.parse(text)
        via_handle: dict[int, str] = {}
        cand_ref = ""
        quest_id = -1
        if parsed is not None and not parsed[1] and parsed[0] != "pathway":
            tgt = store.get_ref(kind=parsed[0], id=parsed[2])
            if tgt is not None:
                for ln in store.links_for(tgt.id, direction="in"):
                    via_handle[ln.src_ref_id] = f"linked from {text}"
                if parsed[0] == "structure":
                    cand_ref = str(tgt.id)
                    for ln in store.links_for(tgt.id, direction="out"):
                        via_handle.setdefault(ln.dst_ref_id, f"linked to {text}")
                elif parsed[0] == "quest":
                    quest_id = int(tgt.id)

        # ``_QUEST_SCOPE`` verbatim — one definition with
        # view='kinetics-summary' (gr592660). It binds the ``p`` alias and
        # %(quest_id)s, which this query provides.
        from precis.utils.pathway_kinetics_summary import _QUEST_SCOPE

        quest_cond = _QUEST_SCOPE.strip().removeprefix("AND ")

        esc = _like_escape(text)
        clauses = ""
        params: dict[str, Any] = {
            "pat": f"%{esc}%",
            "via": sorted(via_handle) or [-1],
            "cand_ref": cand_ref,
            "quest_id": quest_id,
            "lim": page_size,
        }
        if status_f is not None:
            clauses += " AND p.meta->>'status' ILIKE %(status_f)s"
            params["status_f"] = str(status_f)
        if tier is not None:
            clauses += f" AND ({_TIER_CASE}) = %(tier)s"
            params["tier"] = tier
        if engine_f is not None:
            clauses += " AND p.meta->>'autocatpath_version' ILIKE %(engine_f)s"
            params["engine_f"] = f"%{_like_escape(str(engine_f))}%"
        if cand_filter is not None:
            clauses += " AND p.meta->>'candidate_ref' = %(cand_filter)s"
            params["cand_filter"] = cand_filter

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT p.ref_id, slug_id.id_value, p.title, "
                "       p.meta->>'status', "
                "       CASE WHEN slug_id.id_value ILIKE %(pat)s "
                "            OR p.title ILIKE %(pat)s THEN 'slug/title' "
                "            ELSE 'body' END AS matched_on, "
                "       (%(cand_ref)s <> '' "
                "        AND p.meta->>'candidate_ref' = %(cand_ref)s)"
                "           AS is_candidate, "
                f"       (%(quest_id)s > 0 AND {quest_cond}) AS via_quest "
                "FROM refs p "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = p.ref_id AND slug_id.id_kind = 'cite_key' "
                "WHERE p.kind = 'pathway' AND p.retired_at IS NULL AND ("
                "  slug_id.id_value ILIKE %(pat)s OR p.title ILIKE %(pat)s "
                "  OR EXISTS (SELECT 1 FROM chunks c "
                "             WHERE c.ref_id = p.ref_id "
                "             AND c.retired_at IS NULL AND c.text ILIKE %(pat)s)"
                "  OR p.ref_id = ANY(%(via)s)"
                "  OR (%(cand_ref)s <> '' "
                "      AND p.meta->>'candidate_ref' = %(cand_ref)s)"
                f"  OR (%(quest_id)s > 0 AND {quest_cond})"
                f"){clauses} ORDER BY p.updated_at DESC LIMIT %(lim)s",
                params,
            ).fetchall()
        label = text or "the args= filters"
        if not rows:
            return Response(
                body=f"no pathways match {label!r} — get(kind='pathway') "
                "lists all; a structure or quest handle finds linked pathways"
            )
        lines = [f"# {len(rows)} pathway(s) matching {label!r}"]
        for ref_id, slug, title, status, matched_on, is_cand, via_quest in rows:
            note = (
                f"candidate of {text}"
                if is_cand
                else via_handle.get(ref_id)
                or (f"via {text}" if via_quest else matched_on)
            )
            # gr553326: the pw<id> handle beside the slug — the slug alone
            # never reached the ref id the web reader keys on.
            handle = handle_registry.try_format("pathway", ref_id) or f"pw{ref_id}"
            lines.append(
                f"  {handle}  {slug or ref_id}  [{status or '?'}]  {title}  ({note})"
            )
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
        if view is not None and view.lower() == "kinetics-summary":
            from precis.utils.pathway_kinetics_summary import kinetics_summary

            if id is not None:
                raise BadInput(
                    "kinetics-summary is a population view; omit the pathway id",
                    next="get(kind='pathway', view='kinetics-summary') or "
                    "get(kind='quest', id='qu…', view='kinetics-summary')",
                )
            return Response(body=kinetics_summary(store, args=args))
        if id is None:
            # A bare get lists. Search is unsupported on this kind, so until
            # 2026-10-01 an agent that did not already hold a slug had no way
            # to find any of ~700 pathways — and this branch's error pointed
            # at an example slug that never existed.
            return Response(body=self._list(store))
        point_args = {k: val for k, val in (args or {}).items() if k != "environment"}
        gate = _gate_arg(point_args)
        if gate is not None and (view or "").lower() != "compare":
            raise BadInput(
                f"args={{'gate': {gate!r}}} applies to view='compare' only",
                next=f"get(kind='pathway', id={str(id)!r}, view='compare', "
                f"args={{'gate': {gate!r}}})",
            )
        U, pH = _point_args(point_args)
        # A trailing ``~<source>→<target>`` step selector (Simulation step
        # deep-links) — dispatch's universal-handle/slug routing reattaches
        # any ``~selector`` suffix to the resolved id untouched (it only
        # recognises chunk/view selectors of its own, e.g. ``~ord``), so it
        # reaches us as one string. ``slugify`` never produces ``~``, so
        # splitting on the first one is unambiguous.
        ident = str(id)
        slug, sep, step_label = ident.partition("~")
        ref = _lookup(store, slug)
        if ref is None:
            raise BadInput(f"no pathway '{id}'")
        if args is not None and "environment" in args:
            if sep or point_args:
                raise BadInput(
                    "environment supplies the whole point; do not combine it with "
                    "U/pH/point or a step selector",
                    next=f"get(kind='pathway', id={slug!r}, view='kinetics', "
                    "args={'environment': {'T': 298.15}})",
                )
            resp = self._environment_view(
                store, ref, id=id, view=view, raw=args["environment"]
            )
        else:
            resp = (
                self._get_step(store, ref, step_label)
                if sep
                else self._get_view(
                    store, ref, id=id, view=view, args=args, U=U, pH=pH, gate=gate
                )
            )
        # gr553326: every read carries the pw<id> handle + the web reader
        # path — a bare slug reached neither. It leads prose views; config
        # and mermaid are verbatim payloads (a YAML snapshot, a fenced
        # diagram) where a leading line breaks parse/paste, so there it
        # rides as a payload-native comment instead — a trailing ``#`` for
        # YAML, a ``%%`` inside the fence for mermaid.
        if resp.body:
            handle = _handle_line(ref)
            v = (view or "").lower()
            if v == "config":
                body = f"{resp.body}\n# {handle}"
            elif v == "mermaid" and resp.body.endswith("\n```"):
                body = resp.body.removesuffix("\n```") + f"\n%% {handle}\n```"
            elif v == "mermaid":
                body = f"{resp.body}\n%% {handle}"
            else:
                body = f"{handle}\n{resp.body}"
            resp = replace(resp, body=body)
        return resp

    def _environment_view(
        self,
        store: Store,
        ref: Any,
        *,
        id: str | int,
        view: str | None,
        raw: Any,
    ) -> Response:
        """Read-only environment selection; never borrow an off-point solve.

        CHE uses named pH terms when pH is supplied. T sets their kT;
        reservoir and salt changes otherwise select stored kinetics.
        Bracketed results expose their corners, without claiming an interpolated
        rate; computation remains behind the explicit recompute door.
        """
        from .environment import Environment, lookup_environment, ph_analysis_potential

        if view not in ("analysis", "staircase", "kinetics"):
            raise BadInput(
                "environment applies to analysis, staircase and kinetics views",
                options=["analysis", "staircase", "kinetics"],
                next=f"get(kind='pathway', id={id!r}, view='kinetics', "
                "args={'environment': {'T': 298.15}})",
            )
        env = Environment.from_dict(raw)
        meta = ref.meta or {}
        lookup = lookup_environment(meta, env)
        header = (
            f"environment_basis: {env.basis}\n"
            f"environment: {json.dumps(env.to_dict(), sort_keys=True)}\n"
            f"stored result: {lookup['status']}\n"
        )
        if lookup.get("reason"):
            header += f"{lookup['reason']}\n"
        if view == "kinetics":
            if lookup["status"] == "none":
                return Response(
                    body=header
                    + "No stored kinetics at this point. No compute started."
                )
            if lookup["status"] == "bracketed":
                return Response(
                    body=header
                    + "Adjacent stored sweep corners (not interpolated):\n"
                    + json.dumps(lookup["corners"], sort_keys=True)
                )
            if lookup["source"] in ("kinetics_sweep", "kinetics_sweep_previous"):
                return Response(
                    body=header + json.dumps(lookup["record"], sort_keys=True)
                )
            from .toon_views import kinetics_text

            results = dict(meta.get("results") or {})
            selected = lookup["source"]
            if selected == "kinetics_v2_previous":
                selected = "kinetics_v2"
                results[selected] = lookup["record"]
            other = "kinetics" if selected == "kinetics_v2" else "kinetics_v2"
            other_lookup = lookup_environment(
                {**meta, "results": {other: results.get(other)}}, env
            )
            if other_lookup["status"] != "exact":
                results.pop(other, None)
            return Response(body=header + kinetics_text({**meta, "results": results}))
        ec = env.electrochemical
        u, ph = env.u_rhe, ec.get("pH")
        if ph is not None and u is None and view == "analysis":
            u = ph_analysis_potential(meta)
        args: dict[str, Any] = {}
        if view == "staircase" and ph is not None:
            args["pH"] = ph
        response = self._get_view(
            store,
            ref,
            id=id,
            view=view,
            args=args,
            U=u,
            pH=ph if view == "staircase" else None,
            environment=env,
        )
        model_note = (
            "Thermodynamics uses stored T/gas energies with the CHE U lever; "
            "pH and ion concentration do not reprice these energies.\n"
        )
        if ph is not None:
            model_note = (
                "Thermodynamics uses stored energies plus named CHE pH terms; "
                "T sets protonation kT. Gas pressures and ion concentration "
                "do not reprice these energies.\n"
            )
        return replace(
            response,
            body=header + model_note + (response.body or ""),
        )

    def _get_view(
        self,
        store: Store,
        ref: Any,
        *,
        id: str | int,
        view: str | None,
        args: dict[str, Any] | None,
        U: float | None,
        pH: float | None,
        gate: str | None = None,
        environment: Environment | None = None,
    ) -> Response:
        """The per-view dispatch behind :meth:`get` — resolved ref in,
        the view's body out. ``get`` wraps it in the ``pw<id> ·
        /refs/pathway/<id>`` header line."""
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
        if v == "optimum":
            from .optimum import optimum_text

            return Response(body=optimum_text(store, ref))
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
            # gr553328: a pathway with a kinetics environment gets the
            # profile-vs-kinetics conditions paragraph appended — the
            # stored methods chunk can't state it (it predates the solve).
            from precis.utils import pathway_conditions

            extra = pathway_conditions.methods_conditions(meta, body=body)
            if extra:
                body = f"{body}\n\n{extra}" if body else extra
            return Response(body=body or "(no methods)")
        if v == "mermaid":
            return Response(body=self._mermaid(meta))
        if v == "manifest":
            from .manifest import manifest_text

            return Response(body=manifest_text(meta))
        if v == "objective":
            from .objective import objective_text

            return Response(body=objective_text(store, ref))
        if v == "compare":
            return Response(body=self._compare(store, ref, meta, U=U, gate=gate))
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
                    environment=environment,
                )
            )

        computed = bool(meta.get("graph"))
        if (
            v
            in (
                "analysis",
                "steps",
                "kinetics",
                "kinetics-sweep",
                "tpd",
                "trust",
                "warnings",
            )
            and not computed
        ):
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

            return Response(body=analysis_text(meta, U=U, environment=environment))
        if v == "steps":
            from .toon_views import steps_toon

            return Response(body=steps_toon(meta))
        if v == "kinetics":
            from .toon_views import kinetics_text

            return Response(body=kinetics_text(meta))
        if v == "kinetics-sweep":
            from .toon_views import kinetics_sweep_text

            return Response(body=kinetics_sweep_text(meta))
        if v == "tpd":
            from .toon_views import tpd_text

            return Response(body=tpd_text(meta))
        if v == "trust":
            from .toon_views import trust_toon

            # The gate's precis-side rows (absurd magnitude, twin
            # disagreement) live on the candidate structure, not the
            # pathway — read ``meta.candidate_ref`` through.
            cand_meta: dict[str, Any] | None = None
            cand_handle: str | None = None
            cand = meta.get("candidate_ref")
            if isinstance(cand, str) and cand.isdigit():
                cand = int(cand)
            if isinstance(cand, int) and not isinstance(cand, bool):
                cref = store.get_ref(kind="structure", id=cand)
                if cref is not None:
                    cand_meta = dict(cref.meta or {})
                    cand_handle = (
                        handle_registry.try_format("structure", int(cref.id))
                        or f"structure:{cref.id}"
                    )
            pw_handle = (
                handle_registry.try_format("pathway", int(ref.id))
                or f"pathway:{ref.id}"
            )
            # Seed independence (gr517804 R6 + round-2 R1): the gate
            # pools the distinct same-engine seeds this pathway's READY
            # siblings ran AND their endpoint_identity records, so the
            # endpoint_agreement row reflects the union. A lookup failure
            # logs at WARNING and renders the seed gate
            # "not-run (lookup failed)" — never a silent PASS (review item
            # 10); the view is read-only either way.
            pooled_seeds: set[int] | None = None
            pooled_identity: list[dict[str, Any]] = []
            seed_lookup_failed = False
            try:
                from precis.utils import trust_gate

                pooled_seeds, pooled_identity = trust_gate.sibling_evidence(
                    store, meta=meta, exclude_ref_id=int(ref.id)
                )
            except Exception:
                seed_lookup_failed = True
                log.warning(
                    "pathway %s trust view: sibling seed lookup failed",
                    ref.id,
                    exc_info=True,
                )
            return Response(
                body=trust_toon(
                    meta,
                    pw_handle=pw_handle,
                    candidate=cand_meta,
                    candidate_handle=cand_handle,
                    pooled_seeds=pooled_seeds,
                    pooled_identity=pooled_identity,
                    seed_lookup_failed=seed_lookup_failed,
                )
            )
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
    def _get_step(self, store: Store, ref: Any, step_label: str) -> Response:
        """Resolve a ``~<source>→<target>`` step selector against the
        computed graph's edges (``meta['graph']['links']``) — see
        ``analysis._path_steps``'s ``f"{source}→{target}"`` label, unique per
        network. The ref is already resolved (``get`` looks it up once for
        the header line)."""
        slug = ref.slug or str(ref.id)
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
            # gr592665: the `cand` join resolves each row's candidate
            # structure (meta.candidate_ref) to its handle + title — the slug
            # alone never said which dopant/site the row priced.
            rows = conn.execute(
                "SELECT refs.ref_id, slug_id.id_value, refs.title, "
                "refs.meta->>'status', "
                "refs.updated_at, refs.meta->>'tier', refs.meta->'span', "
                "refs.meta->'low_confidence', cand.ref_id, cand.title FROM refs "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = refs.ref_id AND slug_id.id_kind = 'cite_key' "
                "LEFT JOIN refs cand ON cand.kind = 'structure' "
                "  AND cand.ref_id::text = refs.meta->>'candidate_ref' "
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
        titles = {title for _, _, title, *_ in rows}
        shared = titles.pop() if len(titles) == 1 else None
        lines = [
            f"{total} pathways ("
            + ", ".join(f"{n} {status}" for status, n in counts)
            + f"); the {len(rows)} most recently updated"
            + (f", all '{shared}'" if shared else "")
            + ":",
            "",
            "pw | slug | "
            + ("" if shared else "title | ")
            + "candidate | status | tier | span eV | updated",
        ]
        for (
            ref_id,
            slug,
            title,
            status,
            updated,
            tier,
            span,
            low,
            cand_id,
            cand_title,
        ) in rows:
            span_txt = f"{span:.2f}" if isinstance(span, (int, float)) else "-"
            if low is True:
                span_txt += " (low confidence)"
            # gr553326: the pw<id> handle next to the slug — the web reader
            # keys on the ref id (/refs/pathway/<id>).
            handle = handle_registry.try_format("pathway", ref_id) or f"pw{ref_id}"
            cand_cell = "-"
            if cand_id is not None:
                ch = (
                    handle_registry.try_format("structure", int(cand_id))
                    or f"st{cand_id}"
                )
                cand_cell = f"{ch} {cand_title}" if cand_title else ch
            lines.append(
                f"{handle} | {slug or '?'} | "
                + ("" if shared else f"{title} | ")
                + f"{cand_cell} | {status or '?'} | {tier or '-'} | {span_txt} | "
                f"{updated:%Y-%m-%d %H:%MZ}"
            )
        # Point the hint at a ready pathway: view='analysis' on one still
        # computing only says "check back shortly".
        first = next(
            (row[1] for row in rows if row[1] and row[3] == "ready"),
            next((row[1] for row in rows if row[1]), None),
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
        self,
        store: Store,
        ref: Any,
        meta: dict[str, Any],
        *,
        U: float | None = None,
        gate: str | None = None,
    ) -> str:
        """Compare this pathway against every computed sibling sharing the same
        substrate→target (same reaction), as one interleaved TOON table. With
        ``U`` (V vs RHE) every CHE-stamped candidate is re-levered first and
        the table ranks by energetic span at that potential.

        Every row carries a ``gate`` column — the trust-gate verdict replayed
        per row, exactly the gate view='trust' renders (gr592278: a tight
        rate-limiting spread used to read as ``conf=ok`` on gate-BLOCKED
        candidates) — and an ``anchor`` cell, the named credit anchor
        view='trust' headlines (td625117 review: the footnote pointing at
        view='trust' supplied no anchor and cost a lookup per row).
        ``gate='pass'|'blocked'`` filters rows by that verdict; ``n/a``
        rows (pre-trust-schema) never survive a filter."""
        from precis.utils import reaction_graph as analysis
        from precis.utils import trust_gate

        from .toon_views import compare_toon, gate_verdict, row_anchor

        r = meta.get("results", {})
        substrate, target = r.get("substrate"), r.get("target")
        if not substrate or not target:
            return "cannot compare: this pathway has no computed results yet."
        with store.pool.connection() as conn:
            # Same `cand` join as _list (gr592665): the candidate structure's
            # handle + title rides each compare row; its meta feeds the
            # gate's precis-side guard rows (absurd magnitude, twin).
            rows = conn.execute(
                "SELECT refs.ref_id, slug_id.id_value, refs.meta, "
                "cand.ref_id, cand.title, cand.meta FROM refs "
                "LEFT JOIN ref_identifiers slug_id "
                "  ON slug_id.ref_id = refs.ref_id AND slug_id.id_kind = 'cite_key' "
                "LEFT JOIN refs cand ON cand.kind = 'structure' "
                "  AND cand.ref_id::text = refs.meta->>'candidate_ref' "
                "WHERE refs.kind = 'pathway' AND refs.retired_at IS NULL "
                "  AND refs.meta->'results'->>'substrate' = %s "
                "  AND refs.meta->'results'->>'target' = %s "
                "  AND refs.meta->>'status' = 'ready'",
                (substrate, target),
            ).fetchall()
        candidates = []
        row_metas: dict[int, dict[str, Any]] = {}
        cand_meta_by_ref: dict[int, dict[str, Any] | None] = {}
        for ref_id, slug, m, cand_id, cand_title, cand_meta in rows:
            g = (m or {}).get("graph")
            if not g:
                continue
            rid = int(ref_id)
            row_metas[rid] = dict(m or {})
            cand_meta_by_ref[rid] = (
                dict(cand_meta) if isinstance(cand_meta, dict) else None
            )
            c_root, c_target = analysis.roots(g, m.get("results", {}))
            cand_cell = "-"
            if cand_id is not None:
                ch = (
                    handle_registry.try_format("structure", int(cand_id))
                    or f"st{cand_id}"
                )
                cand_cell = f"{ch} {cand_title}" if cand_title else ch
            candidates.append(
                {
                    "ref_id": ref_id,
                    "slug": slug or "?",
                    "candidate": cand_cell,
                    "lever": (m.get("config") or {})
                    .get("slab", {})
                    .get("element", "?"),
                    "graph": g,
                    "root": c_root,
                    "target": c_target,
                }
            )
        # The seed-independence gate pools each pathway's READY same-key
        # siblings — one batched scan for every row, not one per row
        # (sibling_evidence_batch, gr592278). A lookup failure blocks every
        # row (review item 10), never a silent pass.
        seed_lookup_failed = False
        try:
            evidence = trust_gate.sibling_evidence_batch(store, metas=row_metas)
        except Exception:
            seed_lookup_failed = True
            evidence = {}
            log.warning(
                "pathway %s compare: sibling seed lookup failed",
                ref.id,
                exc_info=True,
            )
        for c in candidates:
            rid = int(c["ref_id"])
            pooled, identity = evidence.get(rid, (set(), []))
            verdict, why = gate_verdict(
                row_metas[rid],
                candidate_meta=cand_meta_by_ref.get(rid),
                pooled_seeds=pooled,
                pooled_identity=identity,
                seed_lookup_failed=seed_lookup_failed,
            )
            c["gate"] = f"{verdict} ({why})" if why else verdict
            # td625117 review: the row's credit anchor rides the table —
            # the old footnote pointed at view='trust' for it, one lookup
            # per row, and supplied nothing.
            c["anchor"] = row_anchor(
                row_metas[rid],
                candidate_meta=cand_meta_by_ref.get(rid),
                pooled_seeds=pooled,
                pooled_identity=identity,
                seed_lookup_failed=seed_lookup_failed,
            )
        if gate is not None:
            surviving = [c for c in candidates if str(c["gate"]).startswith(gate)]
            if not surviving:
                counts: dict[str, int] = {}
                for c in candidates:
                    v = str(c["gate"]).split(" ", 1)[0]
                    counts[v] = counts.get(v, 0) + 1
                tally = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
                return (
                    f"no pathways with gate='{gate}' for {substrate}→{target} "
                    f"({len(candidates)} compared: {tally}). Drop the filter "
                    "for every row's verdict, or read view='trust' on one row."
                )
            candidates = surviving
        if len(candidates) < 2 and gate is None:
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
        environment: Environment | None = None,
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

        from .environment import Environment, stored_potential
        from .staircase_routes import render_staircase, routes_for_pathway
        from .staircase_states import pathway_coads, point_states

        raw_results = meta.get("results")
        results = raw_results if isinstance(raw_results, dict) else {}
        if U is not None:
            u = U
        elif pH is not None or (
            environment is not None and "pH" in environment.electrochemical
        ):
            u = stored_potential(meta)
        else:
            # Reads without requested pH retain the legacy operating point.
            u = sq.pathway_operating_u(meta.get("config"), results)
        cfg = meta.get("config") or {}
        if environment is None and pH is not None:
            # Legacy "optimal" is resolved above before validating the point.
            point_cfg = dict(cfg)
            for section, key in (
                ("conditions", "potential_U"),
                ("electrochemistry", "U_vs_RHE"),
            ):
                point_cfg[section] = dict(cfg.get(section) or {})
                point_cfg[section].pop(key, None)
            block = Environment.from_config(point_cfg).to_dict()
            block["T"] = (results.get("che_affine") or {}).get("T") or block["T"]
            block["electrochemical"] = {"U_vs_RHE": u, "pH": pH}
            environment = Environment.from_dict(block)
        if environment is not None and environment.u_rhe is not None:
            u = environment.u_rhe
        sr = routes_for_pathway(store, ref, environment=environment)
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
            # gr491459: what the sweep left out (detached anchors, unpooled
            # models) rides the resting line — the anchor count alone
            # reads as a clean result.
            dropped: list[str] = []
            for m in maps:
                for note in m.get("dropped") or []:
                    if note not in dropped:
                        dropped.append(str(note))
                if resting is None:
                    region = sq.resting_region(m, u)
                    if region is not None:
                        resting = str(region.get("label") or "?")
            structures.append(
                {
                    "handle": handle,
                    "resting": resting,
                    "assumed": sq.assumed_label(coads),
                    "mismatch": sq.mismatch(region, coads, u=u, label=resting),
                    "dropped": dropped,
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
                    kind="pathway",
                    slug=slug,
                    title=title,
                    meta={**seed_meta, "created_by": "put:pathway:preview"},
                    conn=conn,
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
        # gr553328: the profile prices gases at its own table; a recorded
        # kinetics environment gets the one-line conditions note beside it.
        from precis.utils import pathway_conditions

        cond_note = pathway_conditions.conditions_note(meta)
        if cond_note:
            lines.append(cond_note)
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
