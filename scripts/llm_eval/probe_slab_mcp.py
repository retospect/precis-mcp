"""Probe the slab-building MCP with a few agents — find HOW they fail, split
tool-affordance failures from model failures (the co-improve loop).

DB-free: replays each model's proposed put-payload JSON through the REAL put
pipeline (``_build_cell`` / ``_ops_establish_cell`` / ``Scene`` / ``apply_ops``
from ``handlers.structure`` + ``structure.ops``) so we test the shipped tool, not
a reconstruction — but never touches Postgres.

Two tasks isolate the confound:

* **A (no hint)** — the model gets only the structure skill doc + the goal. The
  skill's op table OMITS the ``slab`` op, so this measures whether the model can
  build the surface at all from the documented surface (expected: most
  hand-enumerate → a TOOL-DOC failure shared across models).
* **B (slab op provided)** — the compact ``slab`` op is handed over (as the
  production reaction-context prompt does); the model only edits composition
  (dope one top-layer Pd -> Cu). This measures actual design capability.

Emits a per-model trajectory: parsed? built? natoms / fixed / composition vs
expected, which op it used, and a fault label (tool vs model).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np

from precis.handlers.structure import _build_cell, _ops_establish_cell
from precis.structure.cell import Cell
from precis.structure.ops import OpError, apply_ops
from precis.structure.scene import FIX_ALL, Scene
from precis.utils.llm.router import LlmRequest, Tier, dispatch

SKILL = (
    resources.files("precis.data")
    .joinpath("skills/precis-structure-help.md")
    .read_text()
)

LIT_HINT = (
    "Literature hint (real corpus): mesoporous Cu-doped CeO2/Pd catalysts create "
    "interface Cu-O-M sites that markedly improve NO conversion; a single Cu "
    "substituent in the top metal layer is a common design knob for tuning the "
    "NO->NH3 barrier on fcc(111) surfaces."
)

TASK_A = (
    "You are designing a catalyst surface for the NO -> NH3 reaction. "
    f"{LIT_HINT}\n\n"
    "Build a Pd fcc(111) slab, size 3x3x4 (36 atoms), ~10 Angstrom vacuum, with "
    "the bottom 2 layers frozen. Return ONLY the put(kind='structure') payload "
    "JSON: an object with optional 'cell' and a list 'ops'. No prose, no code "
    "fences — the JSON object must be the last thing in your reply."
)
TASK_B = (
    "You are tuning a Pd fcc(111) catalyst surface for the NO -> NH3 reaction. "
    f"{LIT_HINT}\n\n"
    "The base slab is built by this exact op (use it verbatim as the first op):\n"
    '  {"op": "slab", "element": "Pd", "size": [3, 3, 4], "vacuum": 10.0, '
    '"fix_layers": 2}\n'
    "Your design knob: substitute exactly ONE top-layer Pd atom with a Cu "
    "dopant. Return ONLY the put(kind='structure') payload JSON (object with "
    "'ops'). No prose, no code fences — JSON object last."
)

# (display, OpenRouter slug, tier)
MODELS = [
    ("opus-4.8 (super inc.)", "anthropic/claude-opus-4.8", Tier.CLOUD_SUPER),
    ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", Tier.CLOUD_SUPER),
    ("glm-5.2", "z-ai/glm-5.2", Tier.CLOUD_SUPER),
    ("kimi-k3", "moonshotai/kimi-k3", Tier.CLOUD_SUPER),
    ("haiku-4.5 (small inc.)", "anthropic/claude-haiku-4.5", Tier.CLOUD_SMALL),
    ("gpt-oss-120b", "openai/gpt-oss-120b", Tier.CLOUD_SMALL),
]

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _last_json(text: str) -> dict | None:
    """Best-effort: parse the last {...} block in the reply."""
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    # try progressively shorter suffixes from the last '{'
    starts = [mm.start() for mm in re.finditer(r"\{", text)]
    for s in reversed(starts):
        try:
            return json.loads(text[s : m.end()])
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _build_scene(payload: dict) -> Scene:
    """Replay the REAL put path: cell from payload/op, then apply_ops."""
    ops = payload.get("ops") or []
    if "cell" in payload:
        scene = Scene(cell=_build_cell(payload["cell"]))
    elif _ops_establish_cell(ops):
        scene = Scene(cell=Cell(np.eye(3), (True, True, True)))
    else:
        raise OpError("payload needs a 'cell' or a cell-establishing op (slab)")
    apply_ops(scene, ops)
    return scene


def _min_dist(scene: Scene) -> float:
    """Crude overlap check: smallest pairwise Cartesian distance (Angstrom)."""
    atoms = list(scene.atoms.values())
    if len(atoms) < 2:
        return 99.9
    carts = [scene.cell.frac_to_cart(a.frac) for a in atoms]
    best = 99.9
    for i in range(len(carts)):
        for j in range(i + 1, len(carts)):
            best = min(best, float(np.linalg.norm(carts[i] - carts[j])))
    return best


def _analyze(payload: dict | None, task: str) -> dict[str, Any]:
    if payload is None:
        return {"parsed": False, "fault": "no-json"}
    ops = payload.get("ops") or []
    op_names = [o.get("op") for o in ops if isinstance(o, dict)]
    used_slab = "slab" in op_names
    n_add_atom = op_names.count("add_atom")
    try:
        scene = _build_scene(payload)
    except (OpError, KeyError, ValueError, TypeError) as exc:
        # a build failure: was it a documented-but-wrong key / unknown op?
        return {
            "parsed": True,
            "built": False,
            "op_names": op_names,
            "used_slab": used_slab,
            "error": str(exc),
        }
    els = [a.element for a in scene.atoms.values()]
    n = len(els)
    n_pd = els.count("Pd")
    n_cu = els.count("Cu")
    n_fixed = sum(1 for a in scene.atoms.values() if a.fixed == FIX_ALL)
    return {
        "parsed": True,
        "built": True,
        "op_names": op_names,
        "used_slab": used_slab,
        "n_add_atom": n_add_atom,
        "natoms": n,
        "n_Pd": n_pd,
        "n_Cu": n_cu,
        "n_fixed": n_fixed,
        "min_dist": round(_min_dist(scene), 2),
    }


def run_one(display: str, slug: str, tier: Tier, task_id: str, task: str) -> dict:
    prompt = f"{SKILL}\n\n---\n\n# TASK\n{task}"
    try:
        res = dispatch(
            LlmRequest(
                tier=tier, prompt=prompt, model=slug, max_tokens=2000, source="llm_eval"
            )
        )
        err = getattr(res, "error", None)
        text = getattr(res, "text", "") or ""
    except Exception as exc:  # transport blew up
        err, text = str(exc), ""
    payload = _last_json(text) if not err else None
    a = _analyze(payload, task_id)
    a.update({"model": display, "task": task_id, "transport_error": err})
    return a


def main() -> None:
    jobs = [(d, s, t, "A", TASK_A) for (d, s, t) in MODELS]
    jobs += [(d, s, t, "B", TASK_B) for (d, s, t) in MODELS]
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(run_one, *j) for j in jobs]
        for fut in cf.as_completed(futs):
            results.append(fut.result())
    out = Path(
        "/private/tmp/claude-501/-Users-reto-work-projects-code-precis-mcp/"
        "16354f43-b0a8-449f-976b-9c6a780dfb08/scratchpad/slab_probe.json"
    )
    out.write_text(json.dumps(results, indent=2))

    # --- report, grouped by task ---
    def key(r: dict) -> tuple:
        order = [d for d, *_ in MODELS]
        return (r["task"], order.index(r["model"]))

    for task_id, expect in (
        ("A", "36 atoms, 18 fixed, slab op"),
        ("B", "36 atoms, 35 Pd + 1 Cu, 18 fixed"),
    ):
        print(f"\n=== TASK {task_id} (expect: {expect}) ===")
        for r in sorted([x for x in results if x["task"] == task_id], key=key):
            if r.get("transport_error"):
                print(f"  {r['model']:<24} TRANSPORT-ERR {r['transport_error'][:40]}")
            elif not r.get("parsed"):
                print(f"  {r['model']:<24} NO-JSON in reply")
            elif not r.get("built"):
                print(
                    f"  {r['model']:<24} BUILD-FAIL slab={r['used_slab']} "
                    f"ops={r['op_names']}  err={r['error'][:60]}"
                )
            else:
                print(
                    f"  {r['model']:<24} built n={r['natoms']} "
                    f"Pd={r['n_Pd']} Cu={r['n_Cu']} fixed={r['n_fixed']} "
                    f"slab_op={r['used_slab']} add_atom={r.get('n_add_atom')} "
                    f"mindist={r['min_dist']}"
                )
    print(f"\nraw -> {out}")


if __name__ == "__main__":
    main()
