"""Count Stage 2 verdicts across run 3 verified batches (T2 replaces T's anodising and heat-treatment)."""

import collections
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

V = collections.Counter()
per = {}
for b in list("LMNOPQRSTUV") + ["T2"]:
    c = collections.Counter()
    for n in yaml.safe_load(open(f"{DRAFTS}/run3/verified/{b}.yaml", encoding="utf-8")):  # noqa: SIM115
        if not isinstance(n, dict) or "claims" not in n:
            continue
        for cl in n["claims"]:
            c[str(cl.get("verdict"))] += 1
            c["_remapped"] += bool(cl.get("remapped"))
            c["_n"] += 1
    per[b] = c
    V.update(c)
keys = [
    "confirmed",
    "confirmed-alt",
    "excerpt-only",
    "contradicted",
    "unsupported",
    "unreachable",
    "dropped",
    "_remapped",
]
print("| batch | claims | " + " | ".join(k.strip("_") for k in keys) + " |")
print("|---|---:|" + "---:|" * len(keys))
for b, c in per.items():
    print(f"| {b} | {c['_n']} | " + " | ".join(str(c[k]) for k in keys) + " |")
print(
    f"| **total** | **{V['_n']}** | " + " | ".join(f"**{V[k]}**" for k in keys) + " |"
)
n = V["_n"]
ok = V["confirmed"] + V["confirmed-alt"]
bad = V["contradicted"] + V["unsupported"]
print(
    f"confirmed incl alt {ok}/{n} = {100 * ok / n:.1f}%; wrong or unsourced {bad} = {100 * bad / n:.1f}%; "
    f"unreachable {100 * V['unreachable'] / n:.1f}%"
)
