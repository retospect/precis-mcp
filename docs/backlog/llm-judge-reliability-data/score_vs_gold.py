"""Score the four judge replicates against the operator gold set.

Reports accuracy per replicate and for the opus modal verdict, over the
whole gold set and over the operator-blessed subset only. Also breaks
out where the judges erred: missed defects (judge said benign, gold says
repair) vs false alarms (judge flagged, gold says benign) vs wrong-lane.
Stdlib only; run with `uv run score_vs_gold.py`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REPS = {
    "A (opus)": "replicate_a_opus",
    "B (opus)": "replicate_b_opus",
    "C (opus)": "replicate_c_opus",
    "D (sonnet)": "replicate_d_sonnet",
}
BENIGN = {"NONE", "SCOPE_DRIFT"}


def load(name: str) -> dict[int, dict]:
    rows = {}
    with (HERE / f"{name}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                rows[int(row["link_id"])] = row
    return rows


def main() -> None:
    gold = {}
    with (HERE / "gold.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                gold[int(row["link_id"])] = row

    reps = {k: load(v) for k, v in REPS.items()}
    ids = sorted(gold)
    blessed = [i for i in ids if gold[i]["blessed"]]

    def modal(i: int) -> str:
        c = Counter(reps[k][i]["disposition"] for k in reps if "opus" in k)
        top, n = c.most_common(1)[0]
        return top if n > 1 else "SPLIT"

    print(
        f"# Judges vs operator gold — {len(ids)} edges ({len(blessed)} operator-blessed)\n"
    )
    print("| judge | accuracy (all 30) | accuracy (blessed 19) |")
    print("|---|---|---|")
    rows = [(k, lambda i, k=k: reps[k][i]["disposition"]) for k in REPS]
    rows.append(("opus modal", modal))
    for label, fn in rows:
        a = sum(fn(i) == gold[i]["gold"] for i in ids) / len(ids)
        b = sum(fn(i) == gold[i]["gold"] for i in blessed) / len(blessed)
        print(
            f"| {label} | {a:.0%} ({sum(fn(i) == gold[i]['gold'] for i in ids)}/{len(ids)}) "
            f"| {b:.0%} ({sum(fn(i) == gold[i]['gold'] for i in blessed)}/{len(blessed)}) |"
        )

    print("\n## Error shape (opus modal vs gold)\n")
    missed, false_alarm, wrong_lane = [], [], []
    for i in ids:
        m, g = modal(i), gold[i]["gold"]
        if m == g:
            continue
        if m in BENIGN and g not in BENIGN:
            missed.append((i, m, g))
        elif m not in BENIGN and g in BENIGN:
            false_alarm.append((i, m, g))
        else:
            wrong_lane.append((i, m, g))
    for name, bucket in (
        ("missed defect (judge benign, gold repair)", missed),
        ("false alarm (judge repair, gold benign)", false_alarm),
        ("wrong lane (both flagged, different repair)", wrong_lane),
    ):
        print(f"- **{name}**: {len(bucket)}")
        for i, m, g in bucket:
            note = " [policy delta]" if "policy_delta" in gold[i] else ""
            print(f"  - fi{gold[i]['hub']} (link {i}): judge={m} gold={g}{note}")

    print("\n## Per-judge error shape\n")
    print("| judge | missed | false alarm | wrong lane |")
    print("|---|---|---|---|")
    for k in REPS:
        mi = fa = wl = 0
        for i in ids:
            d, g = reps[k][i]["disposition"], gold[i]["gold"]
            if d == g:
                continue
            if d in BENIGN and g not in BENIGN:
                mi += 1
            elif d not in BENIGN and g in BENIGN:
                fa += 1
            else:
                wl += 1
        print(f"| {k} | {mi} | {fa} | {wl} |")

    print("\n## Modality audit (from the gold set)\n")
    mod = Counter()
    for i in ids:
        m = gold[i].get("modality", "")
        if m.startswith("missing"):
            mod["missing"] += 1
        elif "WRONG" in m:
            mod["present but wrong"] += 1
        elif m.startswith("n/a"):
            mod["n/a (wrong source)"] += 1
        else:
            mod["present and accurate"] += 1
    for k, v in mod.most_common():
        print(f"- {k}: {v}")


if __name__ == "__main__":
    main()
