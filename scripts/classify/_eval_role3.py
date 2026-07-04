"""eval-role3 — 3-way collapse eval (own / background / furniture).

Gold is DERIVED from the 11-way `role` label (and its accept-alternatives,
so the coin-tosses grade fairly):
  own        <- method, result, interpretation, limitation, future-work
  background <- related-work, motivation
  furniture  <- boilerplate, data, n-a

Reports strict + accept-aware accuracy + confusion. Also reports the
own-vs-background split precision/recall, since that is the distinction
citation-grounding actually depends on.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _eval
from _llm import classify_one

MAP = {
    "method": "own",
    "result": "own",
    "interpretation": "own",
    "limitation": "own",
    "future-work": "own",
    "related-work": "background",
    "motivation": "background",
    "boilerplate": "furniture",
    "data": "furniture",
    "n-a": "furniture",
    "unknown": "background",
}


def collapse(role: str) -> str:
    return MAP.get(role, "background")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default=None)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    axis = _eval.load_axis("role3")
    _, recs = _eval.load_gold("chunks")
    if args.limit:
        recs = recs[: args.limit]

    strict = accept = err = 0
    confusion: dict[str, Counter] = defaultdict(Counter)
    n = 0
    print(
        f"classifying {len(recs)} chunks (role3, model={args.model or 'local'})...",
        file=sys.stderr,
    )
    for rec in recs:
        primary = collapse(rec["labels"]["role"])
        acc = {primary} | {collapse(a) for a in rec["labels"].get("role_accept", [])}
        pred = classify_one(_eval.build_prompt(rec, axis), args.model)
        val = (pred or {}).get("value")
        if val not in ("own", "background", "furniture"):
            err += 1
            continue
        n += 1
        confusion[primary][val] += 1
        if val == primary:
            strict += 1
        if val in acc:
            accept += 1

    print(f"\n=== role3 (own/background/furniture) (n={n}, llm-errors={err}) ===")
    print(f"  strict accuracy:       {strict}/{n} = {strict / n:.0%}")
    print(f"  accept-aware accuracy: {accept}/{n} = {accept / n:.0%}")
    gate = "PASS" if accept / n >= 0.85 else "BELOW 85%"
    print(f"  gate (>=85% accept-aware): {gate}")
    print("  confusion (gold -> predicted):")
    for g in sorted(confusion):
        cells = ", ".join(f"{k}:{v}" for k, v in confusion[g].most_common())
        print(f"    {g:12s} -> {cells}")

    # the citation-critical own-vs-background separation (ignore furniture)
    own_gold = sum(confusion["own"].values())
    own_as_own = confusion["own"]["own"]
    bg_gold = sum(confusion["background"].values())
    bg_as_own = confusion["background"]["own"]
    if own_as_own + bg_as_own:
        prec = own_as_own / (own_as_own + bg_as_own)
        print(
            f"\n  own-claim precision: {prec:.0%} "
            f"(of chunks called 'own', how many really are this paper's "
            f"own — the citation-safety number)"
        )
    if own_gold:
        print(
            f"  own-claim recall:    {own_as_own / own_gold:.0%} "
            f"({own_as_own}/{own_gold})"
        )
    if bg_gold:
        print(f"  background leaking into own: {bg_as_own}/{bg_gold}")


if __name__ == "__main__":
    main()
