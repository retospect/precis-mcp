"""eval-junk — binary junk/substantive detector eval (cascade Tier 1).

Gold is DERIVED from the role label: junk if role in {boilerplate, data,
n-a}, else substantive. Reports the metrics that actually matter for a
cascade filter:
  * discard precision — of chunks the model calls junk, how many really are
    (a false discard silently loses real content, so this must be high),
  * junk recall — of real junk, how much is caught (misses just cost a bit
    more downstream, harmless),
  * false-discard rate — substantive chunks wrongly dropped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _eval
from _llm import classify_one

JUNK_ROLES = {"boilerplate", "data", "n-a"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default=None)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    axis = _eval.load_axis("junk")
    _, recs = _eval.load_gold("chunks")
    if args.limit:
        recs = recs[: args.limit]

    tp = fp = tn = fn = err = 0
    misclass = []  # (gold, pred, slug, ref:ord)
    print(
        f"classifying {len(recs)} chunks (junk/substantive, "
        f"model={args.model or 'local'})...",
        file=sys.stderr,
    )
    for rec in recs:
        gold = "junk" if rec["labels"]["role"] in JUNK_ROLES else "substantive"
        pred = classify_one(_eval.build_prompt(rec, axis), args.model)
        val = (pred or {}).get("value")
        if val not in ("junk", "substantive"):
            err += 1
            continue
        if gold == "junk" and val == "junk":
            tp += 1
        elif gold == "substantive" and val == "junk":
            fp += 1
            misclass.append(
                (
                    "substantive",
                    "junk",
                    rec.get("slug"),
                    f"{rec['ref_id']}:{rec['ord']}",
                    rec["labels"]["role"],
                )
            )
        elif gold == "substantive" and val == "substantive":
            tn += 1
        else:  # gold junk, pred substantive
            fn += 1
            misclass.append(
                (
                    "junk",
                    "substantive",
                    rec.get("slug"),
                    f"{rec['ref_id']}:{rec['ord']}",
                    rec["labels"]["role"],
                )
            )

    n = tp + fp + tn + fn
    gold_junk = tp + fn
    print(f"\n=== junk detector (n={n}, llm-errors={err}) ===")
    print(f"  gold: junk={gold_junk}, substantive={tn + fp}")
    print(f"  accuracy:                 {(tp + tn) / n:.1%}")
    print(
        f"  DISCARD PRECISION:        {tp / (tp + fp):.1%}  "
        f"({tp}/{tp + fp} flagged-junk are truly junk)"
    )
    print(
        f"  junk recall:              {tp / gold_junk:.1%}  "
        f"({tp}/{gold_junk} of real junk caught)"
    )
    print(
        f"  false-discard rate:       {fp / (tn + fp):.1%}  "
        f"({fp}/{tn + fp} substantive chunks wrongly dropped)"
    )
    print(
        f"  junk slipping downstream: {fn}/{gold_junk}  (harmless, "
        f"just reaches the expensive stage)"
    )
    if misclass:
        print("\n  misclassifications:")
        for g, pr, slug, addr, role in misclass:
            print(f"    {addr:14s} {slug:16s} gold={g}({role}) -> pred={pr}")


if __name__ == "__main__":
    main()
