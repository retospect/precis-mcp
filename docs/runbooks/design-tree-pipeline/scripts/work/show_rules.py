"""Print compact rules for given node ids from the design-tree manifests.

Usage: python3 show_rules.py <node-id-substring> [...]
"""

import pathlib
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

root = pathlib.Path(DRAFTS)
nodes = []


def walk(obj):
    if isinstance(obj, dict):
        if "id" in obj and ("rules" in obj or "checks" in obj):
            nodes.append(obj)
        for v in obj.values():
            walk(v)
    elif isinstance(obj, list):
        for v in obj:
            walk(v)


for name in ("join.yaml", "rotate.yaml", "close.yaml", "principles.yaml"):
    walk(yaml.safe_load((root / name).read_text(encoding="utf-8")))

for want in sys.argv[1:]:
    for n in nodes:
        if want in n["id"]:
            print(f"## {n['id']} ({n.get('coverage')})")
            for r in n.get("rules") or []:
                rid = r.get("id", "?")
                print(
                    f"  {rid} [{r.get('confidence')}/{r.get('equipment')}] {r.get('text')} = {r.get('value')} {r.get('unit') or ''}"
                )
            for i in n.get("issues") or []:
                print(f"  ! {i.get('issue')} -> {i.get('fix')}")
