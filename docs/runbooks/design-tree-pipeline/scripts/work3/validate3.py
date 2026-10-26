"""Check processes.yaml: unique ids, roles, required fields, every override key matched a claim."""

import collections
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

sys.path.insert(0, DRAFTS + "/work3")
from overrides3 import NODE_OV, OV  # noqa: E402

doc = yaml.safe_load(open(DRAFTS + "/processes.yaml", encoding="utf-8"))  # noqa: SIM115
ids = collections.Counter()
roles = collections.Counter()
rule_ids = set()
problems = []


def walk(n, parent):
    ids[n["id"]] += 1
    roles[n["role"]] += 1
    if parent and not n["id"].startswith(parent + "/"):
        problems.append(f"id {n['id']} not under {parent}")
    for r in n.get("rules") or []:
        rule_ids.add((n["id"], r["id"]))
        for f in ("text", "equipment", "confidence"):
            if not r.get(f):
                problems.append(f"{n['id']} {r['id']} missing {f}")
        if r["confidence"] in ("standard", "handbook", "secondary") and not r.get(
            "source_url"
        ):
            problems.append(f"{n['id']} {r['id']} {r['confidence']} without source_url")
    if n["role"] in ("leaf", "process") and "rules" in n and not n.get("summary"):
        problems.append(f"{n['id']} no summary")
    for c in (n.get("children") or {}).values():
        walk(c, n["id"])


walk(doc["process"], None)
dup = [k for k, v in ids.items() if v > 1]
seen_claims = set()
for b in "LMNOPQRSTUV":
    pass
for (nid, cid), o in OV.items():
    if (nid, cid) not in rule_ids and not o.get("drop"):
        problems.append(f"override not applied: {nid} {cid}")
for nid in NODE_OV:
    if nid not in ids:
        problems.append(f"node override for missing node {nid}")
print("nodes", sum(ids.values()), dict(roles), "dup", dup)
print("\n".join(problems) or "no problems")
