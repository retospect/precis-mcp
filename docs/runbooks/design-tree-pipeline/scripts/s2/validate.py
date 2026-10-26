"""Check S2-corrections.yaml: loads, every ref exists in the repo manifests, no duplicate refs."""

import collections
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(DRAFTS + "/S2-corrections.yaml", encoding="utf-8") as fh:
    d = yaml.safe_load(fh)
known = set()


def walk(x, f, node):
    if isinstance(x, dict):
        if "role" in x and "id" in x:
            node = x["id"]
        for r in x.get("rules") or []:
            if isinstance(r, dict) and "id" in r:
                known.add(f"{f}#{node}#{r['id']}")
        for k, v in x.items():
            if k != "rules":
                walk(v, f, node)
    elif isinstance(x, list):
        for v in x:
            walk(v, f, node)


for f in ("join.yaml", "rotate.yaml", "close.yaml", "processes.yaml"):
    with open(f"{DRAFTS}/s2/repo/{f}", encoding="utf-8") as fh:
        walk(yaml.safe_load(fh), f, None)
refs = [r["ref"] for r in d["rules"]] + d["kept_refs"]
print("unknown", [r for r in refs if r not in known])
print("dups", [k for k, v in collections.Counter(refs).items() if v > 1])
print("actions", collections.Counter(r["action"] for r in d["rules"]))
print(
    "verified",
    collections.Counter(r.get("verified") for r in d["rules"] if "verified" in r),
)
print("cap modes", collections.Counter(c["mode"] for c in d["capabilities"]))
print(
    "cap promote by mode",
    collections.Counter(c["mode"] for c in d["capabilities"] if c["promote"]),
)
