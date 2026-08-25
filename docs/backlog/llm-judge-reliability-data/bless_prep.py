"""Build the 30-edge blessing sheet skeleton: verdicts + notes per edge,
disagreements first, then unanimous fill stratified across labels.
Prints one block per selected edge for the operator sheet."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REPS = {
    "A": "replicate_a_opus",
    "B": "replicate_b_opus",
    "C": "replicate_c_opus",
    "D": "replicate_d_sonnet",
}


def load(name: str) -> dict[int, dict]:
    rows = {}
    with (HERE / f"{name}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                rows[int(row["link_id"])] = row
    return rows


roster = []
with (HERE / "roster.psv").open(encoding="utf-8") as fh:
    header = fh.readline().strip().split("|")
    for line in fh:
        roster.append(dict(zip(header, line.strip().split("|"))))

reps = {k: load(v) for k, v in REPS.items()}

edges = []
for r in roster:
    lid = int(r["link_id"])
    vs = {k: reps[k][lid] for k in reps}
    disp = [vs[k]["disposition"] for k in "ABCD"]
    pv = [vs[k]["passage_verdict"] for k in "ABCD"]
    disagree = len(set(disp)) > 1 or len(set(pv)) > 1
    edges.append((r, vs, disagree))

sel = [e for e in edges if e[2]]
# fill with unanimous, spread across dispositions, exemplars first
by_label: dict[str, list] = {}
for e in edges:
    if not e[2]:
        by_label.setdefault(e[1]["A"]["disposition"], []).append(e)
for lab in sorted(by_label, key=lambda x: len(by_label[x])):
    for e in sorted(by_label[lab], key=lambda e: e[0]["stratum"]):
        if len(sel) >= 30:
            break
        sel.append(e)

print(f"selected {len(sel)} ({sum(1 for e in sel if e[2])} with disagreement)\n")
for r, vs, disagree in sel:
    lid = r["link_id"]
    tag = "DISAGREE" if disagree else "unanimous"
    print(
        f"=== link {lid} hub fi{r['hub']} src pa{r['src']} chunk pc{r['chunk']} [{r['stratum']}/{tag}]"
    )
    for k in "ABCD":
        v = vs[k]
        print(
            f"  {k}: {v['passage_verdict']}/{v['paper_verdict']}/{v['disposition']} — {v.get('note', '')}"
        )
    disp = Counter(vs[k]["disposition"] for k in "ABC")
    print(f"  modal(opus): {disp.most_common(1)[0][0]}")
    print()
