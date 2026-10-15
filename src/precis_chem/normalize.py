"""Engine-agnostic route normalizer — the precis side of slice 2.

The heavy lifting (LinChemIn ``facade`` translate + ``routes_descriptors``) runs
**inside the engine container**, where ``linchemin``/``rdkit`` are installed
(``docker/aizynth/az_to_route.py``). That shim emits a precis-canonical
``route.json`` beside the engine's native output. This module reads that clean
JSON into a :class:`~precis_chem.ir.RouteGraph` — **no chemistry deps**, so it
loads on the always-on request path and the parse is shared by every engine
(AiZynth today, ASKCOS in slice 3: the same ``route.json`` shape).

``route.json`` schema (v1):

    {"schema_version": 1, "engine": "aizynth", "engine_version": "4.3.2",
     "target": "<SMILES>", "solved": true,
     "steps": [{"id": 1, "product": "<SMILES>", "reactants": ["<SMILES>", …],
                "reaction_smiles": "<r>>p>", "template_id": "…",
                "confidence": 0.81, "conditions": "…", "in_stock": false}, …],
     "metrics": {"nr_steps": 2, "cdscore": 0.33, …},
     "score": 0.33, "provenance": {"normalizer": "linchemin", …},
     "routes": [{…same step/metrics/score/solved shape per route…}, …]}

The step order is authoritative (the shim emits target-first). ``metrics`` is the
LinChemIn ``routes_descriptors`` row — the substrate for our own route scoring.
STEER slice 3 adds the optional ``routes`` list: every route the engine kept,
each normalised with its own descriptors — ``routes[0]`` duplicates the
top-level fields (old readers see only the top route); the rest land on
``RouteGraph.alternatives``.
"""

from __future__ import annotations

import json
from typing import Any

from precis.utils.coerce import as_float
from precis_chem.ir import IR_VERSION, RouteGraph, RouteStep, normalize_smiles

#: The filename the container shim writes beside the engine's native output.
ROUTE_FILE = "route.json"


def _step_from_json(d: dict[str, Any], *, fallback_id: int) -> RouteStep:
    return RouteStep(
        id=int(d.get("id", fallback_id)),
        product=normalize_smiles(str(d.get("product", ""))),
        reactants=[normalize_smiles(str(r)) for r in d.get("reactants", [])],
        template_id=(
            str(d["template_id"]) if d.get("template_id") is not None else None
        ),
        # route.json carries the full reaction SMILES under ``reaction_smiles``;
        # the IR field (named ``reaction_smarts``) holds that reaction string
        # (same as slice-1b's parse_aizynth_trees). Accept either key.
        reaction_smarts=d.get("reaction_smiles") or d.get("reaction_smarts"),
        conditions=(str(d["conditions"]) if d.get("conditions") is not None else None),
        confidence=as_float(d.get("confidence")),
        in_stock=bool(d.get("in_stock", False)),
    )


def parse_syngraph(
    content: str | bytes | dict[str, Any],
    *,
    target: str | None = None,
    engine_version: str | None = None,
    max_alternatives: int = 9,
) -> RouteGraph:
    """Parse a container-emitted ``route.json`` into a :class:`RouteGraph`.

    Engine-agnostic: any engine whose container runs the LinChemIn shim emits
    this shape. ``target`` / ``engine_version`` override the JSON's values when
    the caller knows better (the job passes the requested SMILES + image digest).
    Tolerant of missing optional keys; raises only on unparseable JSON.

    A ``routes`` list (slice 3) carries the kept lower-ranked routes — each
    normalised with its own descriptors — onto ``alternatives``, capped at
    ``max_alternatives``.
    """
    if isinstance(content, (str, bytes)):
        data = json.loads(content)
    else:
        data = content
    if not isinstance(data, dict):
        raise ValueError(f"route.json must be a JSON object, got {type(data).__name__}")

    tgt = (
        normalize_smiles(target)
        if target
        else normalize_smiles(str(data.get("target", "")))
    )
    ev = engine_version or str(data.get("engine_version", "?"))
    engine = str(data.get("engine", "?"))

    prov_raw = data.get("provenance")
    provenance: dict[str, Any] = dict(prov_raw) if isinstance(prov_raw, dict) else {}
    provenance["route_schema"] = int(data.get("schema_version", IR_VERSION))

    alternatives = [
        _route_doc_to_graph(alt, target=tgt, engine=engine, engine_version=ev)
        for alt in (data.get("routes") or [])[1 : 1 + max(0, max_alternatives)]
        if isinstance(alt, dict)
    ]

    return RouteGraph(
        target=tgt,
        engine=engine,
        engine_version=ev,
        steps=_steps(data),
        solved=bool(data.get("solved", False)),
        score=as_float(data.get("score")),
        metrics=dict(data.get("metrics") or {}),
        provenance=provenance,
        alternatives=alternatives,
    )


def _steps(d: dict[str, Any]) -> list[RouteStep]:
    return [
        _step_from_json(s, fallback_id=i)
        for i, s in enumerate(d.get("steps", []) or [], 1)
        if isinstance(s, dict)
    ]


def _route_doc_to_graph(
    d: dict[str, Any], *, target: str, engine: str, engine_version: str
) -> RouteGraph:
    """One ``routes[]`` entry → a RouteGraph (no alternatives of its own —
    the list is flat)."""
    return RouteGraph(
        target=target,
        engine=engine,
        engine_version=engine_version,
        steps=_steps(d),
        solved=bool(d.get("solved", False)),
        score=as_float(d.get("score")),
        metrics=dict(d.get("metrics") or {}),
    )
