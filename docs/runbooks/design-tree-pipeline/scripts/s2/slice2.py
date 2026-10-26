"""Split in-A.yaml into agent slices A1/A2 plus the mechanical set M (powder-bed, industrial-fdm node, thermoforming)."""

from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(DRAFTS + "/s2/in-A.yaml", encoding="utf-8") as fh:
    A = yaml.safe_load(fh)

A2_NODES = (
    "warping-and-first-layer",
    "dimensional-accuracy",
    "material-selection",
    "strength-and-infill",
)
MECH = (
    "process/additive/powder-bed",
    "process/additive/industrial-fdm",
    "process/forming/thermoforming",
)
s = {"A1": [], "A2": [], "M": []}
for r in A:
    r["ref"] = f"{r['file']}#{r['node']}#{r['rule']}"
    if r["node"] in MECH:
        s["M"].append(r)
    elif r["node"].endswith(A2_NODES):
        s["A2"].append(r)
    else:
        s["A1"].append(r)
for k, rows in s.items():
    with open(f"{DRAFTS}/s2/in-{k}.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(rows, fh, sort_keys=False, allow_unicode=True, width=140)
    print(k, len(rows))

with open(DRAFTS + "/s2/in-B.yaml", encoding="utf-8") as fh:
    B = yaml.safe_load(fh)
for r in B:
    r["ref"] = f"{r['file']}#{r['node']}#{r['rule']}"
with open(DRAFTS + "/s2/in-B.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(B, fh, sort_keys=False, allow_unicode=True, width=140)
print("B", len(B))
