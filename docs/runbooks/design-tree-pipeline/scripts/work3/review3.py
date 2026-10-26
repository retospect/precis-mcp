"""Write <drafts>/REVIEW-3.md from processes.yaml and work3/build3.json.

Usage: uv run --with pyyaml python3 <drafts>/work3/review3.py
"""

import collections
import json
import re
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

doc = yaml.safe_load(open(DRAFTS + "/processes.yaml", encoding="utf-8"))  # noqa: SIM115
b = json.load(open(DRAFTS + "/work3/build3.json", encoding="utf-8"))  # noqa: SIM115
root = doc["process"]

rows = []  # (method, process, node, coverage, rules)


def short(nid):
    return nid.removeprefix("process/")


for mname, m in root["children"].items():
    for pname, p in m["children"].items():
        if p.get("children"):
            for tname, t in p["children"].items():
                rows.append((mname, pname, t))
        else:
            rows.append((mname, pname, p))

cf = collections.Counter()
eq = collections.Counter()
ms = 0
nrules = 0
red = []
for _, _, n in rows:
    for r in n["rules"]:
        nrules += 1
        cf[r["confidence"]] += 1
        eq[r["equipment"]] += 1
        ms += bool(r.get("machine_specific"))
        numeric = r.get("value") is not None and re.search(r"\d", str(r["value"]))
        if numeric and (
            r.get("corrected") or r["confidence"] in ("shop-rule", "unverified")
        ):
            red.append((n["id"], r))

out = []
w = out.append
w("# Run 3 (S6 processes): review for Reto\n")
w(
    f"{len(rows)} nodes, {nrules} rules. Manifest: `<drafts>/processes.yaml` (SPEC 2.2 field names: roles goal / "
    "method / process / leaf). Deep processes (desktop-fdm, cnc-mill, lathe, injection-mould) hold rule topics as leaves; "
    "the other 22 processes are one shallow node each.\n"
)
w("Confidence: " + ", ".join(f"{k} {v}" for k, v in cf.most_common()) + ".")
w(
    f" {sum(1 for _, _, n in rows for r in n['rules'] if r.get('corrected'))} rules corrected. "
    f"{ms} rules are `machine_specific` (number depends on the machine, tool or material; S1 moves them to "
    "`se_capabilities.json` rows via `table_ref`).\n"
)
w("Equipment scope: " + ", ".join(f"{k} {v}" for k, v in eq.most_common()) + ".\n")

w("## (a) Rules per node\n")
w(
    "| node | cov | rules | shop-rule | unverified | corrected | excerpt-only | machine-specific |"
)
w("|---|---|---:|---:|---:|---:|---:|---:|")
for _, _, n in rows:
    rr = n["rules"]
    c = collections.Counter(r["confidence"] for r in rr)
    w(
        f"| {short(n['id'])} | {n['coverage']} | {len(rr)} | {c['shop-rule']} | {c['unverified']} | "
        f"{sum(1 for r in rr if r.get('corrected'))} | {c['excerpt-only']} | {sum(1 for r in rr if r.get('machine_specific'))} |"
    )
w("")

w(f"## (b) Red list ({len(red)} rules)\n")
w(
    "Corrected, shop-rule and unverified rules that give a number. Do not enforce in DRC until upgraded. A corrected rule "
    "is listed because its value changed; its confidence shows the level of the new source.\n"
)
cur = None
for nid, r in red:
    if nid != cur:
        w(f"\n**{short(nid)}**\n")
        cur = nid
    tag = (
        ("corrected, now " + r["confidence"]) if r.get("corrected") else r["confidence"]
    )
    note = r.get("note") or ""
    w(
        f"- {r['id']}: {r['text'][:140]} = {r['value']} {r.get('unit') or ''} - {tag}{': ' + note[:160] if note else ''}"
    )
w("")

w("## (c) Conflicts kept as paired rules\n")
for _, _, n in rows:
    for cfl in n.get("conflicts") or []:
        topic = cfl.get("topic") if isinstance(cfl, dict) else str(cfl)
        reading = cfl.get("reading", "") if isinstance(cfl, dict) else ""
        w(f"- {short(n['id'])}: {topic}. {str(reading)[:300]}")
w("")

w(f"## (d) Off-node claims removed ({len(b['off_node'])})\n")
w(
    "Claims an answer added for another process (FDM asides in milling answers, moulding rib rules in EDM, generic "
    "machining tables in coating answers). Each process's own node covers them.\n"
)
by = collections.Counter((short(x[0]), x[2]) for x in b["off_node"])
for (nid, e), k in sorted(by.items()):
    w(f"- {nid}: {k} x {e}")
w("")

w(f"## (e) Drops ({len(b['dropped'])}) with reasons\n")
for nid, cid, why in b["dropped"]:
    w(f"- {short(nid)} {cid}: {why}")
w("")

open(DRAFTS + "/REVIEW-3.md", "w", encoding="utf-8").write("\n".join(out) + "\n")  # noqa: SIM115
print(
    "nodes",
    len(rows),
    "rules",
    nrules,
    "red",
    len(red),
    "off-node",
    len(b["off_node"]),
    "dropped",
    len(b["dropped"]),
)
