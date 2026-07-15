"""Retrosynthesis engine port + the built-in engines (ADR 0056 §4).

The engine is the swappable leaf behind the ``route`` kind. A
:class:`RetrosynthEngine` maps a target SMILES to a normalized
:class:`~precis_chem.ir.RouteGraph`; adding AiZynthFinder / ASKCOS is a
new adapter, never a change to the kind or the verb surface.

Two styles (ADR 0056 §4):

* **in-process** (``is_container = False``) — runs in the worker/handler
  process. The deterministic :class:`StubEngine` is the slice-0 fallback
  (catpath's in-process EMT analogue): it needs no chemistry deps and no
  cluster, so the whole compute-lane round-trip + the content-addressed
  cache are testable in the gate.
* **container** (``is_container = True``) — the portable-CPU default for
  real planners. :class:`AiZynthEngine` is the slice-1b placeholder: its
  ``plan`` doesn't run in-process; the ``retrosynth`` job builds a
  ``podman run`` argv on the compute node (the ``struct_relax`` pattern)
  and the engine's ``run_argv`` describes that container. rdkit/aizynth
  are lazy-imported **inside the container shim**, never here — so this
  module stays import-clean without the ``[chem]`` extra.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from precis_chem.ir import RouteGraph, RouteStep, normalize_smiles

#: Default retrosynthesis search depth (max disconnections along a branch).
DEFAULT_MAX_STEPS = 6


@runtime_checkable
class RetrosynthEngine(Protocol):
    """A planner that produces a normalized route to a target molecule."""

    name: str
    version: str
    #: True → the engine runs as a container job on a compute node, not
    #: in-process (see :meth:`plan` — it raises for container engines).
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
    """AiZynthFinder adapter — slice-1b container placeholder (ADR 0056 §8).

    AiZynth runs in a ``FROM upstream@digest`` container built on the compute
    node; it is **not** run in-process (containerizing keeps the heavy
    rdkit/aizynth env + model files off the always-on workers). :meth:`plan`
    therefore raises: the ``retrosynth`` job's dispatch builds the podman
    argv from :attr:`image` and parses the container's ``result.json`` into a
    :class:`RouteGraph`. Wiring that argv + the wrapper Dockerfile + the
    node build is slice 1b (filed) — this class pins the seam.
    """

    name = "aizynth"
    version = "aizynth-container"
    is_container = True
    #: Wrapper image tag (built per-node, ``FROM upstream@digest``). The job
    #: dispatch reads this; real digest pinning is slice 1b.
    image = "precis-aizynth:latest"

    def plan(self, target: str, *, max_steps: int = DEFAULT_MAX_STEPS) -> RouteGraph:
        raise NotImplementedError(
            "aizynth is a container engine: the retrosynth job runs it via "
            "`podman run` on the route node (slice 1b), not in-process. Set "
            "PRECIS_CHEM_ROUTE_NODE + build the wrapper image to enable it."
        )


#: Registry of built-in engines by name. Plugins/extras extend this later; a
#: future OSS-backend switch (ADR 0046 analogue) would resolve here.
_ENGINES: dict[str, type] = {
    StubEngine.name: StubEngine,
    AiZynthEngine.name: AiZynthEngine,
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
