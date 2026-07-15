"""Generic engine→route.json normalizer (ADR 0056 slice 3).

A **standalone** LinChemIn container: given a planner's native output (any
``input_format`` LinChemIn can translate — ``askcosv2``, ``ibm_retro``, …), it
emits the precis-canonical ``route.json`` that ``precis_chem.normalize.
parse_syngraph`` reads. This is the *service*-engine analogue of the aizynth
image's bundled ``az_to_route.py``: a container engine bundles its own
normalizer, a service engine (ASKCOS) has no image to bundle in, so precis runs
this generic one on the returned paths.

The step extraction (SynGraph → target-first steps) is **engine-agnostic** —
identical to the aizynth shim's — so adding an engine is a new ``input_format``,
never new normalizer code. Per-engine metadata enrichment (buyable leaves,
policy scores) is engine-specific and lives with the caller; this generic path
emits structure + LinChemIn route descriptors only.

    python to_route.py --input-format <fmt> <raw.json> <route.json> \
        [engine] [engine_version]

Best-effort like the aizynth shim: on any failure it still writes a minimal
route.json (or exits 0) so the caller degrades rather than crashes.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SCHEMA_VERSION = 1


def syngraph_steps(sg: Any) -> list[dict[str, Any]]:
    """SynGraph → ordered steps (target-first). Engine-agnostic (see module
    docstring) — the same walk the aizynth shim uses."""
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
            # Engine-specific fields left null on the generic path; a caller
            # with native metadata may enrich them.
            "confidence": None,
            "conditions": None,
            "in_stock": False,
        }
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
    for p, step in sorted(by_product.items()):
        if p not in seen:
            ordered.append(step)
    for i, step in enumerate(ordered, 1):
        step["id"] = i
    return ordered


def _coerce_paths(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("paths", "trees", "routes", "output"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def build_route(
    raw_content: str, *, input_format: str, engine: str, engine_version: str
) -> dict[str, Any]:
    from linchemin.interfaces.facade import facade

    paths = _coerce_paths(json.loads(raw_content))
    if not paths:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine": engine,
            "engine_version": engine_version,
            "target": "",
            "solved": False,
            "steps": [],
            "metrics": {},
            "score": None,
            "provenance": {
                "engine": engine,
                "normalizer": "linchemin",
                "input_format": input_format,
                "n_routes": 0,
            },
        }

    syngraphs, _meta = facade(
        "translate", paths, input_format=input_format, output_format="syngraph"
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
    except Exception as exc:
        print(f"to_route: descriptors failed: {exc}", file=sys.stderr)
        metrics = {}

    target = top.get_roots()[0].smiles if top.get_roots() else ""
    # A planner that returns paths returns *solved* paths (all leaves buyable),
    # so a present route is solved unless the caller enriches otherwise.
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "target": target,
        "solved": bool(steps),
        "steps": steps,
        "metrics": metrics,
        "score": metrics.get("cdscore"),
        "provenance": {
            "engine": engine,
            "normalizer": "linchemin",
            "input_format": input_format,
            "n_routes": len(syngraphs),
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Normalize planner output → route.json")
    ap.add_argument("--input-format", required=True)
    ap.add_argument("raw", nargs="?", default="/work/out/raw.json")
    ap.add_argument("route", nargs="?", default="/work/out/route.json")
    ap.add_argument("engine", nargs="?", default="unknown")
    ap.add_argument("engine_version", nargs="?", default="unknown")
    args = ap.parse_args(argv[1:])
    try:
        with open(args.raw) as fh:
            content = fh.read()
        route = build_route(
            content,
            input_format=args.input_format,
            engine=args.engine,
            engine_version=args.engine_version,
        )
        with open(args.route, "w") as fh:
            json.dump(route, fh)
        print(
            f"to_route: wrote {args.route} ({len(route['steps'])} step(s))",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(f"to_route: normalization failed ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
