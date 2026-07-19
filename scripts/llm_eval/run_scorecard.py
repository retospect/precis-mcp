"""Parallel golden-task scorecard — one thread per candidate, over its tier's axes.

DB-free by design: drives the real router seam (:func:`router.dispatch`) with
the wired scorers (:mod:`llm_eval.scorers`) and buckets to the catalog's 1..5
ordinal, but never touches the Store — so it runs ``--no-record`` with zero prod
writes. Emits a per-tier scorecard (ordinal + raw mean + n per axis), the
cost-per-task, and flags the cheapest OSS model that matches-or-beats the tier's
claude incumbent on every wired axis.

Env (OpenRouter): PRECIS_LLM_BACKEND=openai, PRECIS_LLM_BASE_URL, PRECIS_LLM_API_KEY.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dispatch import last_json_object, robust_dispatch

from precis.llm_eval.scorers import SCORERS, bucket_to_ordinal
from precis.llm_eval.tasks import load_gold_set
from precis.utils.llm.router import Tier

GOLD = "scripts/llm_eval/gold_set/corpus_v1.json"

# (display id, OpenRouter slug, tier, is_incumbent)
ROSTER = [
    # super tier
    ("claude-opus-4-8", "anthropic/claude-opus-4.8", Tier.CLOUD_SUPER, True),
    ("z-ai/glm-5.2", "z-ai/glm-5.2", Tier.CLOUD_SUPER, False),
    ("moonshotai/kimi-k3", "moonshotai/kimi-k3", Tier.CLOUD_SUPER, False),
    ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro", Tier.CLOUD_SUPER, False),
    # mid tier
    ("claude-sonnet-5", "anthropic/claude-sonnet-5", Tier.CLOUD_MID, True),
    ("moonshotai/kimi-k2.7-code", "moonshotai/kimi-k2.7-code", Tier.CLOUD_MID, False),
    ("z-ai/glm-4.7", "z-ai/glm-4.7", Tier.CLOUD_MID, False),
    # small tier
    ("claude-haiku-4-5", "anthropic/claude-haiku-4.5", Tier.CLOUD_SMALL, True),
    ("openai/gpt-oss-120b", "openai/gpt-oss-120b", Tier.CLOUD_SMALL, False),
    ("z-ai/glm-4.7-flash", "z-ai/glm-4.7-flash", Tier.CLOUD_SMALL, False),
    (
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
        Tier.CLOUD_SMALL,
        False,
    ),
]


def run_model(display: str, slug: str, tier: Tier, tasks: list) -> dict:
    """Run one candidate over all gold tasks; return per-axis rollups + cost."""
    by_axis: dict[str, list] = {}
    cost_total = 0.0
    cost_known = 0
    for t in tasks:
        score = 0.0
        err = None
        try:
            # robust_dispatch retries an empty reply with a bounded reasoning
            # budget, so a reasoning model isn't scored 0 for starving its own
            # content (the confound the slab probe surfaced).
            res, note = robust_dispatch(
                tier=tier,
                prompt=t.prompt,
                model=slug,
                tools_needed=t.tools_needed,
                max_tokens=1200,
                source="llm_eval",
            )
        except Exception as exc:  # transport blew up
            res, note = None, f"raise: {exc}"
        if res is None or res.error or note == "empty":
            err = note if res is None else (res.error or "empty")
        else:
            c = res.cost_usd
            if c is not None:
                cost_total += float(c)
                cost_known += 1
            # fence-/nesting-tolerant data fallback (res.data is one-level regex)
            data = res.data if res.data is not None else last_json_object(res.text)
            score = SCORERS[t.scorer](res.text or "", data, t.expect)
        by_axis.setdefault(t.axis, []).append(
            {"task_id": t.task_id, "score": score, "error": err}
        )
    axes = {}
    for axis, rows in by_axis.items():
        mean = sum(r["score"] for r in rows) / len(rows)
        axes[axis] = {
            "n": len(rows),
            "mean": mean,
            "ordinal": bucket_to_ordinal(mean),
            "errors": [r["task_id"] for r in rows if r["error"]],
            "per_task": rows,
        }
    n_tasks = len(tasks)
    return {
        "display": display,
        "slug": slug,
        "tier": tier.value,
        "axes": axes,
        "cost_total": cost_total,
        "cost_known": cost_known,
        "cost_per_task": (cost_total / cost_known) if cost_known else None,
        "n_tasks": n_tasks,
    }


def main() -> None:
    tasks = load_gold_set(GOLD)
    print(f"gold set: {len(tasks)} tasks; roster: {len(ROSTER)} models (parallel)\n")
    results: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=len(ROSTER)) as ex:
        futs = {ex.submit(run_model, d, s, t, tasks): d for (d, s, t, _inc) in ROSTER}
        for fut in cf.as_completed(futs):
            r = fut.result()
            results[r["display"]] = r
            errs = sum(len(a["errors"]) for a in r["axes"].values())
            cpt = r["cost_per_task"]
            cpt_s = "—" if cpt is None else f"${cpt * 1000:.3f}/1k"
            print(f"  done: {r['display']:<30} cost/task={cpt_s}  errors={errs}")

    out = Path(
        "/private/tmp/claude-501/-Users-reto-work-projects-code-precis-mcp/"
        "16354f43-b0a8-449f-976b-9c6a780dfb08/scratchpad/scorecard.json"
    )
    out.write_text(
        json.dumps({d: results[d] for d, *_ in ROSTER if d in results}, indent=2)
    )
    print(f"\nraw results -> {out}")


if __name__ == "__main__":
    main()
