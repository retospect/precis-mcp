"""Retrosynthesis engine port + the built-in engines (ADR 0056 §4).

The engine is the swappable leaf behind the ``route`` kind. A
:class:`RetrosynthEngine` maps a target SMILES to a normalized
:class:`~precis_chem.ir.RouteGraph`; adding AiZynthFinder / ASKCOS is a
new adapter, never a change to the kind or the verb surface.

Three **transports** (ADR 0056 §4) — how the engine actually runs. The
``retrosynth`` job dispatches on this, not on the engine name:

* **inprocess** — runs in the worker/handler process. The deterministic
  :class:`StubEngine` is the slice-0 fallback (catpath's in-process EMT
  analogue): no chemistry deps, no cluster, so the whole compute-lane
  round-trip + the content-addressed cache are gate-testable.
* **container** — a one-shot container the ``retrosynth`` job runs on a
  compute node (the ``struct_relax`` pattern): the shim runs the planner,
  normalizes to ``route.json`` in-image, and the dispatch reads it back.
  :class:`AiZynthEngine` (slice 1b/2). rdkit/aizynth/linchemin live **inside
  the image**, never here, so this module imports clean without ``[chem]``.
* **service** — a long-running HTTP planner the ``retrosynth`` job POSTs to
  (its native output is normalized by a standalone LinChemIn container, since
  there is no per-engine image to bundle the normalizer in).
  :class:`AskcosEngine` (slice 3): ASKCOS v2 is a multi-service platform with
  a Tree-Builder REST API, not a CLI — so it is a service, not a container.

Every engine carries an :attr:`input_format` — the LinChemIn translate format
of its native output (``az_retro`` / ``askcosv2`` / …), the one string that
lets the *same* normalizer serve every engine ("swap the engine, keep the IR").
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from precis_chem.ir import RouteGraph, RouteStep, normalize_smiles

#: Default retrosynthesis search depth (max disconnections along a branch).
DEFAULT_MAX_STEPS = 6

#: The engine transports the ``retrosynth`` dispatch knows how to run.
TRANSPORT_INPROCESS = "inprocess"
TRANSPORT_CONTAINER = "container"
TRANSPORT_SERVICE = "service"


@runtime_checkable
class RetrosynthEngine(Protocol):
    """A planner that produces a normalized route to a target molecule."""

    name: str
    version: str
    #: How the engine runs (see module docstring): ``inprocess`` / ``container``
    #: / ``service``. The ``retrosynth`` dispatch branches on this.
    transport: str
    #: LinChemIn translate format of the engine's native output (``az_retro`` /
    #: ``askcosv2`` / …); ``""`` for an engine that emits the IR directly (stub).
    input_format: str
    #: Back-compat alias — ``True`` iff :attr:`transport` is ``container``.
    is_container: bool

    def plan(self, target: str, *, max_steps: int = DEFAULT_MAX_STEPS) -> RouteGraph:
        """Return a route to ``target`` (in-process engines only)."""
        ...


class StubEngine:
    """A deterministic, chemistry-free toy planner — the slice-0 fallback.

    It does **no real chemistry**: it splits the target string into two
    synthetic "precursors" by a fixed rule so the output is reproducible
    across runs (tests + the ``PRECIS_CHEM_ROUTE_NODE``-unset inline path).
    The provenance is stamped ``stub`` so a stub route is never mistaken for
    a planned one. Its only job is to exercise the substrate: mint → plan →
    write-back → cache hit.
    """

    name = "stub"
    version = "stub-v1"
    transport = TRANSPORT_INPROCESS
    input_format = ""  # emits the IR directly — no normalizer
    is_container = False

    def plan(self, target: str, *, max_steps: int = DEFAULT_MAX_STEPS) -> RouteGraph:
        tgt = normalize_smiles(target)
        # A single deterministic disconnection into two buyable "precursors".
        # Purely lexical: reproducible, obviously synthetic.
        left = f"{tgt}>>A" if tgt else "A"
        right = f"{tgt}>>B" if tgt else "B"
        step = RouteStep(
            id=1,
            product=tgt,
            reactants=[left, right],
            template_id="stub-disconnect",
            conditions="(stub — no real chemistry)",
            confidence=0.5,
            in_stock=True,
        )
        return RouteGraph(
            target=tgt,
            engine=self.name,
            engine_version=self.version,
            steps=[step],
            solved=True,
            score=0.5,
            provenance={"engine": "stub", "note": "deterministic placeholder"},
        )


class AiZynthEngine:
    """AiZynthFinder adapter — container engine (ADR 0056 slice 1b/2).

    AiZynth runs in a wrapper container built on the compute node
    (``docker/aizynth``); it is **not** run in-process (containerizing keeps
    the heavy rdkit/aizynth env + model files off the always-on workers). So
    :meth:`plan` raises — the ``retrosynth`` job routes container engines to
    ``precis_chem.jobs._run_container``, which builds the ``podman run`` argv
    (:meth:`run_argv`), then reads the shim-normalized ``route.json`` (slice 2),
    falling back to the native :attr:`native_output` (``trees.json``) via
    :attr:`native_parser`. The remaining live-run wiring is a node-deploy
    concern (per-node ``podman build`` + ``PRECIS_CHEM_ROUTE_NODE`` + the NAS
    models mount).
    """

    name = "aizynth"
    version = "aizynth-container"
    transport = TRANSPORT_CONTAINER
    input_format = "az_retro"  # LinChemIn translate format of trees.json
    is_container = True
    #: Wrapper image tag (built per-node from ``docker/aizynth``). The job
    #: dispatch reads this; pin a digest at deploy for reproducible provenance.
    image = "precis-aizynth:latest"
    #: The engine's native output filename (fallback when ``route.json`` absent).
    native_output = "trees.json"

    def run_argv(
        self,
        *,
        ref_id: int,
        in_dir: str,
        out_dir: str,
        smiles: str,
        container_cmd: str,
        models_dir: str | None,
    ) -> list[str]:
        """The ``podman/docker run`` argv for one plan (delegates to the
        aizynth argv builder; lazy import keeps this module chem-dep-free)."""
        from precis_chem.aizynth import build_aizynth_argv

        return build_aizynth_argv(
            ref_id=ref_id,
            in_dir=in_dir,
            out_dir=out_dir,
            smiles=smiles,
            image=self.image,
            container_cmd=container_cmd,
            models_dir=models_dir,
        )

    def native_parser(
        self, content: str, *, target: str, engine_version: str
    ) -> RouteGraph:
        """Parse the native ``trees.json`` (the slice-1b fallback path)."""
        from precis_chem.aizynth import parse_aizynth_trees

        return parse_aizynth_trees(
            content, target=target, engine_version=engine_version
        )

    def plan(self, target: str, *, max_steps: int = DEFAULT_MAX_STEPS) -> RouteGraph:
        raise NotImplementedError(
            "aizynth is a container engine: the retrosynth job runs it via "
            "`podman run` on the route node (precis_chem.jobs._run_container), "
            "not in-process. Set PRECIS_CHEM_ROUTE_NODE + build the wrapper "
            "image (docker/aizynth) to enable it."
        )


#: Env naming the base URL of a running ASKCOS v2 deployment (its Tree-Builder
#: REST API). Operator-configured trusted infra (like the DB DSN / LLM base
#: URL), so the service call is exempt from the agent-URL SSRF guard.
ASKCOS_ENDPOINT_ENV = "PRECIS_ASKCOS_URL"


class AskcosEngine:
    """ASKCOS v2 adapter — **service** engine (ADR 0056 slice 3).

    ASKCOS v2 is a multi-service platform with a Tree-Builder REST API (not a
    CLI), so it is a *service*: the ``retrosynth`` job POSTs the target to the
    running deployment (:data:`ASKCOS_ENDPOINT_ENV`), extracts the returned
    ``paths`` (``askcosv2`` format), and normalizes them to ``route.json`` with
    the standalone LinChemIn normalizer container — then the *same*
    :func:`precis_chem.normalize.parse_syngraph` reads it. Proves "two engines,
    one IR": the precis-side reader is untouched from AiZynth; only the
    transport + ``input_format`` differ.

    Live-run remaining (cluster): stand up an ASKCOS v2 deployment, set
    ``PRECIS_ASKCOS_URL``, build the normalizer image (``docker/normalizer``),
    and **verify the request/response schema against that instance's
    ``/docs``** (encoded in :mod:`precis_chem.askcos`, flagged there).
    """

    name = "askcos"
    version = "askcos-v2"
    transport = TRANSPORT_SERVICE
    input_format = "askcosv2"  # LinChemIn translate format of ASKCOS paths
    is_container = False
    #: The standalone LinChemIn normalizer image (built from docker/normalizer).
    image = "precis-normalizer:latest"

    @property
    def endpoint(self) -> str | None:
        """The configured ASKCOS base URL, or ``None`` when unset."""
        return os.environ.get(ASKCOS_ENDPOINT_ENV) or None

    def plan(self, target: str, *, max_steps: int = DEFAULT_MAX_STEPS) -> RouteGraph:
        raise NotImplementedError(
            "askcos is a service engine: the retrosynth job POSTs to the ASKCOS "
            "Tree-Builder API (precis_chem.jobs._run_service), not in-process. "
            f"Set {ASKCOS_ENDPOINT_ENV} to a running ASKCOS v2 deployment."
        )


#: Registry of built-in engines by name. Plugins/extras extend this later; a
#: future OSS-backend switch (ADR 0046 analogue) would resolve here.
_ENGINES: dict[str, type] = {
    StubEngine.name: StubEngine,
    AiZynthEngine.name: AiZynthEngine,
    AskcosEngine.name: AskcosEngine,
}

#: The default engine when a caller names none. ``stub`` keeps the merge
#: inert + gate-testable; flip to ``aizynth`` once slice 1b's container lands.
DEFAULT_ENGINE = StubEngine.name


def resolve_engine(name: str | None) -> RetrosynthEngine:
    """Resolve an engine by name (default :data:`DEFAULT_ENGINE`)."""
    key = (name or DEFAULT_ENGINE).strip().lower()
    cls = _ENGINES.get(key)
    if cls is None:
        raise ValueError(
            f"unknown retrosynthesis engine {name!r}; known: {sorted(_ENGINES)}"
        )
    return cls()  # type: ignore[return-value]
