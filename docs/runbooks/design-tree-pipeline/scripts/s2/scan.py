"""List S2 candidate rules: FDM/moulding-scoped or Stratasys-cited, from the repo manifests."""

import collections
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

D = DRAFTS + "/s2/repo/"
FILES = ["join.yaml", "rotate.yaml", "close.yaml", "processes.yaml"]
EQ = {"desktop-fdm", "industrial-fdm", "injection-mould", "sla"}
TXT = re.compile(
    r"\b(fdm|3d.?print|printed|print|layer|nozzle|perimeter|infill|bridg|overhang|mould|mold|sla|resin)",
    re.I,
)

out = []
stats = collections.Counter()


def walk(x, f, node):
    if isinstance(x, dict):
        if "role" in x and "id" in x:
            node = x["id"]
        for r in x.get("rules") or []:
            if not isinstance(r, dict) or "text" not in r:
                continue
            url = r.get("source_url") or ""
            eq = r.get("equipment")
            strat = "stratasys" in url.lower()
            hit = strat or eq in EQ or (TXT.search(r["text"]) and eq in (None, "any"))
            if hit:
                stats[(f, eq, strat)] += 1
                out.append(
                    {
                        "file": f,
                        "node": node,
                        "rule": r.get("id"),
                        "equipment": eq,
                        "stratasys": strat,
                        "text": r["text"],
                        "value": r.get("value"),
                        "unit": r.get("unit"),
                        "source_url": url,
                        "confidence": r.get("confidence"),
                        "machine_specific": r.get("machine_specific"),
                    }
                )
        for k, v in x.items():
            if k != "rules":
                walk(v, f, node)
    elif isinstance(x, list):
        for v in x:
            walk(v, f, node)


for f in FILES:
    with open(D + f, encoding="utf-8") as fh:
        walk(yaml.safe_load(fh), f, None)

with open(DRAFTS + "/s2/candidates.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1)
for k, v in sorted(stats.items(), key=str):
    print(k, v)
print("total", len(out))
ids = collections.Counter((o["file"], o["node"], o["rule"]) for o in out)
print("dup ids", [k for k, v in ids.items() if v > 1][:5], file=sys.stderr)
