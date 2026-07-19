#!/usr/bin/env python
"""Run the full candidate×axis LLM eval matrix through the real router seam.

DRY by construction: every candidate is exercised via
:func:`precis.utils.llm.router.dispatch` — the same switchable ``openai_compat``
lane production uses when ``PRECIS_LLM_BACKEND=openai`` — so this eval also
validates the switch itself. We do not open a second HTTP client; we wrap
``dispatch`` only to tally cost/tokens (which the harness's per-task score drops).

Candidates (OSS + the claude incumbents) all resolve as OpenRouter slugs, so one
base url + one key covers the whole matrix. Scoring reuses the shipped
:mod:`precis.llm_eval` harness + scorers; each model runs every axis, giving a
full capability profile regardless of its "home" tier.

Run (from the repo root)::

    export PRECIS_LLM_API_KEY=$(cat ~/.secrets/pw/openrouter_api_key)
    uv run python scripts/llm_eval/run_matrix.py

Writes ``scorecard.md`` + ``results.json`` to --out (default: this dir).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# The eval drives the OpenRouter-hosted OSS/claude lane; set the switch *before*
# importing the router so LlmConfig.from_env() (read at dispatch time) sees it.
os.environ.setdefault("PRECIS_LLM_BACKEND", "openai")
os.environ.setdefault("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
os.environ.setdefault("PRECIS_SUMMARIZE_MAX_TOKENS", "4096")  # reasoning headroom
if not os.environ.get("PRECIS_LLM_API_KEY"):
    _keyfile = Path.home() / ".secrets" / "pw" / "openrouter_api_key"
    if _keyfile.exists():
        os.environ["PRECIS_LLM_API_KEY"] = _keyfile.read_text().strip()

from precis.llm_eval.harness import run_eval
from precis.llm_eval.tasks import load_gold_set
from precis.utils.llm.router import Tier
from precis.utils.llm.router import dispatch as _real_dispatch

# tier label -> candidate OpenRouter slugs; the first of each tier is the
# claude INCUMBENT it would replace. All resolve on OpenRouter (verified).
MATRIX: dict[str, list[str]] = {
    "cloud-super": [
        "anthropic/claude-opus-4.8",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-pro",
    ],
    "cloud-mid": [
        "anthropic/claude-sonnet-5",
        "moonshotai/kimi-k2.7-code",
        "z-ai/glm-4.7",
        "qwen/qwen3.7-max",
        "minimax/minimax-m3",
    ],
    "cloud-small": [
        "anthropic/claude-haiku-4.5",
        "openai/gpt-oss-120b",
        "deepseek/deepseek-v4-flash",
        "z-ai/glm-4.7-flash",
        "qwen/qwen3.6-flash",
        "openai/gpt-oss-20b",
    ],
}

AXES = [
    "code",
    "reasoning-convergence",
    "tool-structured",
    "long-context-recall",
    "summarize-extract",
]


def _counting_dispatch(model: str, tally: dict[str, dict]):
    """Wrap the real router dispatch to sum cost/tokens/errors per model."""

    def _d(req):
        res = _real_dispatch(req)
        t = tally[model]
        t["calls"] += 1
        if getattr(res, "cost_usd", None):
            t["cost"] += res.cost_usd
        if getattr(res, "total_tokens", None):
            t["tokens"] += res.total_tokens
        if getattr(res, "error", None):
            t["errors"] += 1
        return res

    return _d


def _eval_model(model: str, tasks, tally: dict[str, dict]) -> tuple[str, dict]:
    """Run one model over the whole gold set; return {axis: (ordinal, mean, n)}."""
    report = run_eval(
        None,  # store unused: record=False
        model=model,
        tier=Tier.CLOUD_MID,  # forces the openai_compat (tool-less cloud) lane
        tasks=tasks,
        dispatch_fn=_counting_dispatch(model, tally),
        record=False,
    )
    axes = {
        r.axis: {"ordinal": r.ordinal, "mean": round(r.mean_score, 3), "n": r.n}
        for r in report.results
    }
    return model, axes


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).parent
    ap.add_argument("--gold", default=str(here / "gold_set" / "full.json"))
    ap.add_argument("--out", default=str(here))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--only", nargs="*", help="restrict to these model slugs")
    args = ap.parse_args()

    if not os.environ.get("PRECIS_LLM_API_KEY"):
        print(
            "ERROR: PRECIS_LLM_API_KEY not set and no ~/.secrets key found",
            file=sys.stderr,
        )
        return 2

    tasks = load_gold_set(args.gold)
    models = [(tier, m) for tier, ms in MATRIX.items() for m in ms]
    if args.only:
        models = [(t, m) for t, m in models if m in args.only]
    incumbents = {ms[0] for ms in MATRIX.values()}

    print(
        f"eval: {len(models)} models × {len(tasks)} tasks "
        f"({len({t.axis for t in tasks})} axes) via {os.environ['PRECIS_LLM_BASE_URL']}"
    )
    tally: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "cost": 0.0, "tokens": 0, "errors": 0}
    )
    t0 = time.time()
    results: dict[str, dict] = {}
    tier_of: dict[str, str] = {m: tier for tier, m in models}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_eval_model, m, tasks, tally): m for _, m in models}
        for fut in as_completed(futs):
            model, axes = fut.result()
            results[model] = axes
            errs = tally[model]["errors"]
            print(f"  done {model:32} errors={errs}/{tally[model]['calls']}")

    payload = {
        "gold": args.gold,
        "n_tasks": len(tasks),
        "elapsed_s": round(time.time() - t0, 1),
        "results": results,
        "spend": {m: tally[m] for m in results},
        "tier_of": tier_of,
        "incumbents": sorted(incumbents),
    }
    out = Path(args.out)
    (out / "results.json").write_text(json.dumps(payload, indent=2))
    scorecard = render(payload)
    (out / "scorecard.md").write_text(scorecard)
    print("\n" + scorecard)
    print(f"\nwrote {out / 'scorecard.md'} and {out / 'results.json'}")
    return 0


def render(p: dict) -> str:
    """A per-tier scorecard: ordinal per axis, mean, run-cost, incumbent delta."""
    results, tier_of, incs = p["results"], p["tier_of"], set(p["incumbents"])
    spend = p["spend"]
    short = {
        "code": "code",
        "reasoning-convergence": "reason",
        "tool-structured": "tool",
        "long-context-recall": "recall",
        "summarize-extract": "summ",
    }
    lines = [
        "# LLM eval scorecard — OSS vs claude (through the router switch)",
        "",
        f"{len(results)} models × {p['n_tasks']} tasks, {p['elapsed_s']}s. "
        "Ordinal 1–5 per axis (higher better); mean across axes. "
        "`$` = this run's OpenRouter spend (tiny prompts — a relative signal, "
        "not prod cost). ★ = incumbent for its tier.",
        "",
    ]
    header = "| model | " + " | ".join(short[a] for a in AXES) + " | mean | $ | errs |"
    sep = "|" + "---|" * (len(AXES) + 4)
    for tier in ("cloud-super", "cloud-mid", "cloud-small"):
        tier_models = [m for m in results if tier_of.get(m) == tier]
        if not tier_models:
            continue
        # incumbent mean, for the delta flag
        inc = next((m for m in tier_models if m in incs), None)
        inc_ord = (
            {a: results[inc].get(a, {}).get("ordinal", 0) for a in AXES} if inc else {}
        )

        def mean_ord(m):
            vs = [results[m].get(a, {}).get("ordinal", 0) for a in AXES]
            return sum(vs) / len(vs) if vs else 0.0

        tier_models.sort(key=lambda m: (m not in incs, -mean_ord(m)))
        lines += ["", f"## {tier}", "", header, sep]
        for m in tier_models:
            cells = []
            for a in AXES:
                o = results[m].get(a, {}).get("ordinal", 0)
                cells.append(str(o))
            mo = mean_ord(m)
            star = " ★" if m in incs else ""
            beats = ""
            if inc and m not in incs:
                n_ge = sum(
                    1
                    for a in AXES
                    if results[m].get(a, {}).get("ordinal", 0) >= inc_ord.get(a, 0)
                )
                beats = f" ({n_ge}/{len(AXES)}≥★)"
            cost = spend.get(m, {}).get("cost", 0.0)
            errs = spend.get(m, {}).get("errors", 0)
            name = m.split("/")[-1]
            lines.append(
                f"| {name}{star}{beats} | "
                + " | ".join(cells)
                + f" | {mo:.1f} | ${cost:.4f} | {errs} |"
            )
    lines += [
        "",
        "**Read it:** in each tier the OSS rows flagged `(k/5≥★)` match-or-beat the "
        "claude incumbent on k of 5 axes. A safe default swap needs 5/5 on the axes "
        "that tier actually runs (super/mid: code+reason+tool; small: tool+recall+summ).",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
