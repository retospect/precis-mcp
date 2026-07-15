"""In-container route normalizer — AiZynth trees.json → precis-canonical route.json.

Runs **inside** the precis-aizynth image (ADR 0056 slice 2), where ``linchemin``
+ ``rdkit`` are installed. After ``aizynthcli`` writes ``trees.json``, the shim
calls this module to:

  1. translate the AiZynth route (``az_retro``) into a LinChemIn **SynGraph**,
  2. extract engine-agnostic steps (product ⇐ reactants, target-first) — the
     same extractor slice-3 ASKCOS reuses (askcos format → the same SynGraph),
  3. compute route-level **descriptors** (``routes_descriptors``) — the scoring
     substrate,
  4. enrich each step with AiZynth-only metadata (policy probability, buyable
     leaves, reaction class, template) pulled from the raw ``trees.json``,

and emit ``route.json``. It is **best-effort**: any failure leaves ``trees.json``
in place (the precis side falls back to the bespoke ``parse_aizynth_trees``), so
a normalizer hiccup never fails an otherwise-good plan.

    python -m az_to_route  [trees.json]  [route.json]  [engine_version]

Defaults: ``/work/out/trees.json`` → ``/work/out/route.json``, version from
``$PRECIS_AIZYNTH_VERSION``. Keep the emitted schema in lockstep with the
precis-side reader ``precis_chem.normalize.parse_syngraph``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SCHEMA_VERSION = 1


def _canon(smiles: str) -> str:
    """rdkit-canonical SMILES (the container has rdkit); identity on failure."""
    try:
        from rdkit import Chem

        m = Chem.MolFromSmiles(smiles)
        if m is not None:
            return Chem.MolToSmiles(m)
    except Exception:
        pass
    return smiles


def syngraph_steps(sg: Any) -> list[dict[str, Any]]:
    """SynGraph → ordered steps (target-first). Engine-agnostic — reused by any
    engine whose native format LinChemIn can translate to a SynGraph."""
    by_product: dict[str, dict[str, Any]] = {}
    for node in sg.graph:
        if type(node).__name__ != "ChemicalEquation":
            continue
        products = [m.smiles for m in node.get_products()]
        product = products[0] if products else ""
        by_product[product] = {
            "product": product,
            "reactants": [m.smiles for m in node.get_reactants()],
            "reaction_smiles": node.smiles,
            "template_id": node.template if node.template else None,
        }
    # BFS from the target(s): each root is a step; a reactant that is itself the
    # product of another step is the next. Deterministic, target-first.
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue = [m.smiles for m in sg.get_roots()]
    while queue:
        p = queue.pop(0)
        if p in seen or p not in by_product:
            continue
        seen.add(p)
        step = by_product[p]
        ordered.append(step)
        queue.extend(step["reactants"])
    for p, step in sorted(by_product.items()):  # defensive: any unreached CE
        if p not in seen:
            ordered.append(step)
    for i, step in enumerate(ordered, 1):
        step["id"] = i
    return ordered


def aizynth_enrichment(trees: list[dict[str, Any]]) -> tuple[dict[str, dict], set[str]]:
    """Walk the raw AiZynth tree → ``{canonical(product): {confidence,
    classification, template_id}}`` + the set of buyable (in_stock) SMILES."""
    meta: dict[str, dict] = {}
    buyable: set[str] = set()

    def walk(mol: dict[str, Any]) -> None:
        if mol.get("in_stock"):
            buyable.add(_canon(str(mol.get("smiles", ""))))
        for rxn in mol.get("children") or []:
            if not isinstance(rxn, dict):
                continue
            md = rxn.get("metadata") or {}
            meta[_canon(str(mol.get("smiles", "")))] = {
                "confidence": md.get("policy_probability"),
                "classification": md.get("classification"),
                "template_id": md.get("template_code") or md.get("template"),
            }
            for p in rxn.get("children") or []:
                if isinstance(p, dict) and p.get("type") == "mol":
                    walk(p)

    for root in trees:
        if isinstance(root, dict):
            walk(root)
    return meta, buyable


def _coerce_trees(data: Any) -> list[dict[str, Any]]:
    """trees.json is a bare list for a single SMILES; tolerate wrapper shapes."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("trees", "top_ranked_routes", "routes"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def build_route(trees_content: str, *, engine_version: str) -> dict[str, Any]:
    from linchemin.interfaces.facade import facade

    trees = _coerce_trees(json.loads(trees_content))
    if not trees:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine": "aizynth",
            "engine_version": engine_version,
            "target": "",
            "solved": False,
            "steps": [],
            "metrics": {},
            "score": None,
            "provenance": {
                "engine": "aizynth",
                "normalizer": "linchemin",
                "n_routes": 0,
            },
        }

    syngraphs, _meta = facade(
        "translate", trees, input_format="az_retro", output_format="syngraph"
    )
    top = syngraphs[0]
    steps = syngraph_steps(top)

    try:
        df, _m = facade("routes_descriptors", [top])
        metrics = {
            k: (float(v) if hasattr(v, "__float__") else v)
            for k, v in df.iloc[0].to_dict().items()
            if k != "route_id"
        }
    except Exception as exc:  # descriptors are optional enrichment
        print(f"az_to_route: descriptors failed: {exc}", file=sys.stderr)
        metrics = {}

    enrich, buyable = aizynth_enrichment(trees)
    for step in steps:
        e = enrich.get(_canon(step["product"]), {})
        step["confidence"] = e.get("confidence")
        step["conditions"] = e.get("classification")
        if not step.get("template_id"):
            step["template_id"] = e.get("template_id")
        step["in_stock"] = bool(step["reactants"]) and all(
            _canon(r) in buyable for r in step["reactants"]
        )

    leaves = [m.smiles for m in top.get_leaves()]
    solved = all(_canon(leaf) in buyable for leaf in leaves) if leaves else False
    target = top.get_roots()[0].smiles if top.get_roots() else ""

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "aizynth",
        "engine_version": engine_version,
        "target": target,
        "solved": solved,
        "steps": steps,
        "metrics": metrics,
        "score": metrics.get("cdscore"),
        "provenance": {
            "engine": "aizynth",
            "normalizer": "linchemin",
            "n_routes": len(syngraphs),
        },
    }


def main(argv: list[str]) -> int:
    trees_path = argv[1] if len(argv) > 1 else "/work/out/trees.json"
    route_path = argv[2] if len(argv) > 2 else "/work/out/route.json"
    version = (
        argv[3]
        if len(argv) > 3
        else os.environ.get("PRECIS_AIZYNTH_VERSION", "aizynth")
    )
    try:
        with open(trees_path) as fh:
            content = fh.read()
        route = build_route(content, engine_version=version)
        with open(route_path, "w") as fh:
            json.dump(route, fh)
        print(
            f"az_to_route: wrote {route_path} "
            f"({len(route['steps'])} step(s), solved={route['solved']})",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:  # best-effort: leave trees.json for the fallback
        print(f"az_to_route: normalization skipped ({exc})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
