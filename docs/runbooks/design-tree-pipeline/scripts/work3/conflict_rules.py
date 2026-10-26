"""For each node with conflicts, print the conflict topics and the node's rules (id, value, text) to map by hand."""

from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

doc = yaml.safe_load(open(DRAFTS + "/processes.yaml", encoding="utf-8"))  # noqa: SIM115


def walk(n):
    if n.get("conflicts"):
        print(f"\n## {n['id']}")
        for i, c in enumerate(n["conflicts"]):
            print(f"  C{i}: {c['topic']}")
        for r in n["rules"]:
            print(
                f"    {r['id']} [{r['confidence']}] {str(r.get('value'))[:30]} | {r['text'][:110]}"
            )
    for c in (n.get("children") or {}).values():
        walk(c)


walk(doc["process"])
