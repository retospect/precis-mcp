"""Write S2 input slices: A = FDM rules cited to Stratasys / industrial-fdm; B = join.yaml split check."""

import json
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(DRAFTS + "/s2/candidates.json", encoding="utf-8") as fh:
    C = json.load(fh)

KEEP = [
    "file",
    "node",
    "rule",
    "equipment",
    "text",
    "value",
    "unit",
    "source_url",
    "confidence",
    "machine_specific",
]


def slim(o):
    return {k: o[k] for k in KEEP if o.get(k) not in (None, "")}


A = [
    slim(o)
    for o in C
    if o["equipment"] == "industrial-fdm"
    or (o["stratasys"] and "fdm" in o["source_url"].lower())
]
B = [slim(o) for o in C if o["file"] == "join.yaml" and o not in A and slim(o) not in A]
for name, rows in (("A", A), ("B", B)):
    with open(f"{DRAFTS}/s2/in-{name}.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(rows, fh, sort_keys=False, allow_unicode=True, width=140)
    print(name, len(rows))
