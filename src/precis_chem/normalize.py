"""Engine-agnostic route normalizer — the precis side of slice 2 (ADR 0056 §10).

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
     "score": 0.33, "provenance": {"normalizer": "linchemin", …}}

The step order is authoritative (the shim emits target-first). ``metrics`` is the
LinChemIn ``routes_descriptors`` row — the substrate for our own route scoring.
"""

from __future__ import annotations

import json
from typing import Any

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
        confidence=_as_float(d.get("confidence")),
        in_stock=bool(d.get("in_stock", False)),
    )


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_syngraph(
    content: str | bytes | dict[str, Any],
    *,
    target: str | None = None,
    engine_version: str | None = None,
) -> RouteGraph:
    """Parse a container-emitted ``route.json`` into a :class:`RouteGraph`.

    Engine-agnostic: any engine whose container runs the LinChemIn shim emits
    this shape. ``target`` / ``engine_version`` override the JSON's values when
    the caller knows better (the job passes the requested SMILES + image digest).
    Tolerant of missing optional keys; raises only on unparseable JSON.
    """
    if isinstance(content, (str, bytes)):
        data = json.loads(content)
    else:
        data = content
    if not isinstance(data, dict):
        raise ValueError(f"route.json must be a JSON object, got {type(data).__name__}")

    steps = [
        _step_from_json(s, fallback_id=i)
        for i, s in enumerate(data.get("steps", []) or [], 1)
        if isinstance(s, dict)
    ]
    tgt = (
        normalize_smiles(target)
        if target
        else normalize_smiles(str(data.get("target", "")))
    )
    ev = engine_version or str(data.get("engine_version", "?"))

    prov_raw = data.get("provenance")
    provenance: dict[str, Any] = dict(prov_raw) if isinstance(prov_raw, dict) else {}
    provenance["route_schema"] = int(data.get("schema_version", IR_VERSION))

    return RouteGraph(
        target=tgt,
        engine=str(data.get("engine", "?")),
        engine_version=ev,
        steps=steps,
        solved=bool(data.get("solved", False)),
        score=_as_float(data.get("score")),
        metrics=dict(data.get("metrics") or {}),
        provenance=provenance,
    )
