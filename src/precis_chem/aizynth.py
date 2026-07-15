"""AiZynthFinder container plumbing (ADR 0056 slice 1b).

Two pure, gate-testable pieces — the parser + the argv builder — so the
container's *shape* is validated without a cluster or a built image (the
live run is exercised through the ``RUNNER``/``STAGER`` hooks in
``jobs.py``, the ``struct_relax`` pattern).

**Parser.** AiZynthFinder's ``aizynthcli`` writes ``trees.json`` — a
ranked list of routes, each a nested ``ReactionTree`` dict that alternates
molecule and reaction nodes (``ReactionTree.to_dict``):

    mol{type:'mol', smiles, in_stock, children:[rxn...]}
      └ rxn{type:'reaction', smiles, metadata:{template, classification,
             policy_probability}, children:[mol...]}   ← the precursors

:func:`parse_aizynth_trees` walks the top-ranked route into our flat
:class:`~precis_chem.ir.RouteGraph` (one :class:`RouteStep` per reaction
node). Slice 2 (LinChemIn) will replace this bespoke walk with a shared
normalizer, but the IR contract is the same.

**Argv.** :func:`build_aizynth_argv` is the ``podman run`` command line the
dispatch ssh's to the route node — the target SMILES in, ``trees.json`` out
under the bind-mounted ``/work/out``. The config (policy + stock model
paths) is baked into the wrapper image (weights mounted from the NAS), so
the argv only carries the target.
"""

from __future__ import annotations

import json
from typing import Any

from precis_chem.ir import RouteGraph, RouteStep, normalize_smiles

#: Bind-mount points inside the container + the AiZynth output filename.
CONTAINER_IN = "/work/in"
CONTAINER_OUT = "/work/out"
TREES_FILE = "trees.json"


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_routes(data: Any) -> list[dict[str, Any]]:
    """Coerce parsed ``trees.json`` to a ranked list of root molecule dicts.

    Single-SMILES ``trees.json`` is a bare list; be defensive about the
    wrapper shapes other aizynthcli output modes use (``{trees: [...]}`` /
    ``{top_ranked_routes: [...]}`` / ``{data: [{trees: [...]}]}``).
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("trees", "top_ranked_routes", "routes"):
            val = data.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        inner = data.get("data")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return _extract_routes(inner[0])
    return []


def parse_aizynth_trees(
    content: str | bytes | Any,
    *,
    target: str | None = None,
    engine_version: str = "aizynth",
) -> RouteGraph:
    """Parse AiZynth ``trees.json`` into a :class:`RouteGraph` (top-ranked
    route). ``target`` overrides the root SMILES for the graph label."""
    if isinstance(content, (str, bytes)):
        data = json.loads(content)
    else:
        data = content
    routes = _extract_routes(data)
    tgt = normalize_smiles(target) if target else ""

    if not routes:
        return RouteGraph(
            target=tgt,
            engine="aizynth",
            engine_version=engine_version,
            steps=[],
            solved=False,
            provenance={"engine": "aizynth", "n_routes": 0},
        )

    root = routes[0]
    if not tgt:
        tgt = normalize_smiles(str(root.get("smiles", "")))

    steps: list[RouteStep] = []
    solved = True

    def _walk(mol: dict[str, Any]) -> None:
        for rxn in mol.get("children") or []:
            if not isinstance(rxn, dict):
                continue
            precursors = [
                c
                for c in (rxn.get("children") or [])
                if isinstance(c, dict) and c.get("type") == "mol"
            ]
            md = rxn.get("metadata") or {}
            tmpl = md.get("template")
            steps.append(
                RouteStep(
                    id=len(steps) + 1,
                    product=normalize_smiles(str(mol.get("smiles", ""))),
                    reactants=[
                        normalize_smiles(str(p.get("smiles", ""))) for p in precursors
                    ],
                    template_id=str(tmpl) if tmpl is not None else None,
                    reaction_smarts=rxn.get("smiles"),
                    conditions=md.get("classification") or None,
                    confidence=_as_float(md.get("policy_probability")),
                    in_stock=bool(precursors)
                    and all(p.get("in_stock", False) for p in precursors),
                )
            )
            for p in precursors:
                _walk(p)

    def _check_leaves(mol: dict[str, Any]) -> None:
        nonlocal solved
        kids = mol.get("children") or []
        if not kids:
            if not mol.get("in_stock", False):
                solved = False
            return
        for rxn in kids:
            if not isinstance(rxn, dict):
                continue
            for p in rxn.get("children") or []:
                if isinstance(p, dict) and p.get("type") == "mol":
                    _check_leaves(p)

    _walk(root)
    _check_leaves(root)

    return RouteGraph(
        target=tgt,
        engine="aizynth",
        engine_version=engine_version,
        steps=steps,
        solved=solved,
        score=_as_float(root.get("scores", {}).get("state score"))
        if isinstance(root.get("scores"), dict)
        else None,
        provenance={"engine": "aizynth", "n_routes": len(routes)},
    )


#: Where the wrapper image expects the policy/stock model files (config.yml +
#: the referenced ONNX/pickle models) — mounted read-only from the NAS, not
#: baked into the image (ADR 0056 §5: image = code, weights = mounted data).
CONTAINER_MODELS = "/models"


def build_aizynth_argv(
    *,
    ref_id: int,
    in_dir: str,
    out_dir: str,
    smiles: str,
    image: str,
    container_cmd: str = "podman",
    models_dir: str | None = None,
) -> list[str]:
    """The ``podman run`` argv for one AiZynth plan (pure, testable).

    Deterministic ``--name precis-route-<ref_id>`` so a sweeper can kill it by
    name. The wrapper image's ``precis-aizynth-run`` entrypoint runs
    ``aizynthcli`` and drops ``trees.json`` into ``/work/out``; the target
    SMILES is the only per-job argument. When ``models_dir`` is given (the NAS
    path, a node-deploy concern), it is bind-mounted read-only at
    :data:`CONTAINER_MODELS` so the config's model paths resolve.
    """
    argv = [
        container_cmd,
        "run",
        "--rm",
        "--name",
        f"precis-route-{ref_id}",
        "-v",
        f"{in_dir}:{CONTAINER_IN}:ro",
        "-v",
        f"{out_dir}:{CONTAINER_OUT}",
    ]
    if models_dir:
        argv += ["-v", f"{models_dir}:{CONTAINER_MODELS}:ro"]
    argv += [image, "precis-aizynth-run", smiles]
    return argv
