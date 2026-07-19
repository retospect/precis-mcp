"""Structure round-trip eval — the monthly co-improvement harness.

Design: ``docs/design/structure-roundtrip-eval.md``. For each generated
ground-truth structure S and each model, run the cycle

    S --describe(model)--> prose --build(model)--> S'

and score how faithfully S' reproduces S with the representation-invariant
comparator (:mod:`precis.structure.invariants`) — never coordinates. Tracks
BOTH fidelity (did the structure survive the language round trip?) and cost
(how much did it take?), so a monthly run shows whether a better model or a
sharper skill doc moved the numbers.

DB-free: the build half is ``apply_ops`` in memory; nothing touches Postgres.

Run (needs ASE, the [dft] extra, for the slab generator):

    PRECIS_LLM_BACKEND=openai \
    PRECIS_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    PRECIS_LLM_API_KEY=$(cat ~/.secrets/pw/openrouter_api_key) \
    uv run --extra dft python scripts/llm_eval/roundtrip.py

Appends a dated row to ``scripts/llm_eval/ROUNDTRIP_RESULTS.md``.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime
import json
import sys
from importlib import resources
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dispatch import last_json_object, robust_dispatch

from precis.structure.export import to_extxyz
from precis.structure.invariants import compare, fingerprint
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis.utils.llm.router import Tier

SKILL = (
    resources.files("precis.data")
    .joinpath("skills/precis-structure-help.md")
    .read_text()
)

# (display, OpenRouter slug, tier) — the tier incumbents + the scorecard's top OSS.
ROSTER = [
    ("opus-4.8", "anthropic/claude-opus-4.8", Tier.CLOUD_SUPER),
    ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", Tier.CLOUD_SUPER),
    ("sonnet-5", "anthropic/claude-sonnet-5", Tier.CLOUD_MID),
    ("haiku-4.5", "anthropic/claude-haiku-4.5", Tier.CLOUD_SMALL),
    ("gpt-oss-120b", "openai/gpt-oss-120b", Tier.CLOUD_SMALL),
]

DESCRIBE_PROMPT = (
    "You are given an atomic structure in extended-XYZ format (Cartesian Å; a "
    "trailing flag marks frozen atoms). Describe it in PLAIN PROSE, precisely "
    "enough that a chemist could rebuild it exactly: the surface/lattice type, "
    "the element(s), the size (atoms per layer × number of layers), the vacuum, "
    "which layers are frozen, and any dopant or adsorbate — its element and which "
    "layer or site it sits in. RULES: natural language only. NO coordinates, NO "
    "lists of numbers, NO JSON, NO code. 2–6 sentences.\n\n{dump}"
)
BUILD_PROMPT = (
    "# TASK\nBuild the structure described below as a put(kind='structure') "
    "payload: a JSON object with an optional 'cell' and a list 'ops'. Prefer the "
    "compact `slab` op for a metal surface. Return ONLY the JSON object — no "
    "prose, no code fences — as the last thing in your reply.\n\n"
    "Description:\n{prose}"
)


def _slab_ops(element: str, size: tuple[int, int, int], fix: int) -> list[dict]:
    return [
        {
            "op": "slab",
            "element": element,
            "size": list(size),
            "vacuum": 10.0,
            "fix_layers": fix,
        }
    ]


def _top_label(scene: Scene) -> str:
    """The label of the highest-z atom (a top-layer site for doping)."""
    return max(
        scene.atoms, key=lambda la: scene.cell.frac_to_cart(scene.atoms[la].frac)[2]
    )


def generate() -> list[tuple[str, Scene]]:
    """The v1 parametric gold set: pure fcc(111) slabs + top-layer dopants.

    Known-good by construction (built via the real `slab` op path); each scene
    IS its own answer key.
    """
    out: list[tuple[str, Scene]] = []
    for el in ("Pd", "Ni", "Cu", "Ag"):
        s = Scene(cell=_empty_cell())
        apply_ops(s, _slab_ops(el, (3, 3, 3), 1))
        out.append((f"{el}(111) 3x3x3", s))
    # top-layer dopants on a Pd base (exercises per-layer composition)
    for dopant in ("Cu", "Ni"):
        s = Scene(cell=_empty_cell())
        apply_ops(s, _slab_ops("Pd", (3, 3, 4), 2))
        apply_ops(s, [{"op": "set_element", "atom": _top_label(s), "element": dopant}])
        out.append((f"Pd(111) 3x3x4 +{dopant} top-dopant", s))
    return out


def _empty_cell():
    import numpy as np

    from precis.structure.cell import Cell

    return Cell(np.eye(3), (True, True, True))


#: Trials per (model, structure). Models are stochastic, so a single trip is
#: noisy (a model swung 0.17/0.83/0.33/0.67 across passes) — average K trials for
#: a stable monthly number. K≈3–5; raise for a tighter figure at linear cost.
TRIALS = 3

#: A trip at or above this fidelity counts as a "clean" round trip (reliability).
_CLEAN = 0.9


def round_trip(
    display: str, slug: str, tier: Tier, name: str, source: Scene, trial: int = 0
) -> dict:
    """One S → describe → build → S' cycle for one model; scored + costed."""
    dump = to_extxyz(source, constraints=True)
    desc, dnote = robust_dispatch(
        tier=tier,
        prompt=DESCRIBE_PROMPT.format(dump=dump),
        model=slug,
        max_tokens=800,
        source="roundtrip.describe",
    )
    prose = desc.text or ""
    build_prompt = f"{SKILL}\n\n---\n\n{BUILD_PROMPT.format(prose=prose)}"
    built, bnote = robust_dispatch(
        tier=tier,
        prompt=build_prompt,
        model=slug,
        max_tokens=2000,
        source="roundtrip.build",
    )
    cost = (desc.cost_usd or 0.0) + (built.cost_usd or 0.0)
    row = {
        "model": display,
        "task": name,
        "trial": trial,
        "describe_note": dnote,
        "build_note": bnote,
        "cost_usd": round(cost, 5),
        "prose_chars": len(prose),
    }
    payload = last_json_object(built.text or "")
    if payload is None:
        return {
            **row,
            "score": 0.0,
            "fault": "no-json",
            "valid": False,
            "reply": (built.text or "")[-400:],
        }
    try:
        rebuilt = Scene(cell=_empty_cell())
        apply_ops(rebuilt, payload.get("ops") or [])
        if "cell" in payload and not rebuilt.atoms:
            raise ValueError("empty build")
        res = compare(fingerprint(source), fingerprint(rebuilt))
    except Exception as exc:  # a build failure is a real (model) fault, scored 0
        # keep the offending payload so a monthly fault is debuggable (tool-fix
        # vs model-fault) without a re-run.
        return {
            **row,
            "score": 0.0,
            "fault": f"build:{str(exc)[:80]}",
            "valid": False,
            "payload": json.dumps(payload)[:500],
        }
    return {**row, "score": res["score"], "parts": res["parts"], "valid": res["valid"]}


def main() -> None:
    gold = generate()
    n_trips = len(gold) * TRIALS
    print(
        f"gold: {len(gold)} structures × {TRIALS} trials × {len(ROSTER)} models "
        f"({n_trips} trips/model, same-model round trip)\n"
    )
    jobs = [
        (d, s, t, name, scene, k)
        for (d, s, t) in ROSTER
        for (name, scene) in gold
        for k in range(TRIALS)
    ]
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=min(12, len(jobs))) as ex:
        futs = [ex.submit(round_trip, *j) for j in jobs]
        for fut in cf.as_completed(futs):
            results.append(fut.result())

    # per-model rollup, averaged over trials (mean fidelity + reliability + cost)
    by_model: dict[str, list[dict]] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    print(f"{'model':<16} {'fidelity':>9} {'clean%':>7} {'fault%':>7} {'$/trip':>9}")
    lines = []
    for display, _s, _t in ROSTER:
        rows = by_model.get(display, [])
        if not rows:
            continue
        n = len(rows)
        mean = sum(r["score"] for r in rows) / n
        clean = sum(1 for r in rows if r["score"] >= _CLEAN) / n
        fault = sum(1 for r in rows if r.get("fault")) / n
        cpt = sum(r["cost_usd"] for r in rows) / n
        print(
            f"{display:<16} {mean:>9.3f} {clean * 100:>6.0f}% {fault * 100:>6.0f}% "
            f"{cpt * 1000:>8.3f}m"
        )
        lines.append((display, mean, clean, fault, cpt))

    scratch = Path(
        "/private/tmp/claude-501/-Users-reto-work-projects-code-precis-mcp/"
        "16354f43-b0a8-449f-976b-9c6a780dfb08/scratchpad/roundtrip.json"
    )
    scratch.write_text(json.dumps(results, indent=2))

    # append a dated trend row to the tracked results log
    log = Path(__file__).resolve().parent / "ROUNDTRIP_RESULTS.md"
    today = datetime.date.today().isoformat()
    block = [
        f"\n## {today}  ({len(gold)} structures × {TRIALS} trials, same-model round trip)\n"
    ]
    block.append(
        "| model | fidelity | clean% | fault% | $/trip |\n|---|---|---|---|---|"
    )
    for display, mean, clean, fault, cpt in lines:
        block.append(
            f"| {display} | {mean:.3f} | {clean * 100:.0f}% | {fault * 100:.0f}% | "
            f"${cpt * 1000:.3f}m |"
        )
    with log.open("a") as fh:
        fh.write("\n".join(block) + "\n")
    print(f"\nappended trend row -> {log}\nraw -> {scratch}")


if __name__ == "__main__":
    main()
