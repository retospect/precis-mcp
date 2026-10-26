"""Print compact rows from a drafter output, optionally filtered by action."""

import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(f"{DRAFTS}/s2/out/{sys.argv[1]}.yaml", encoding="utf-8") as fh:
    out = yaml.safe_load(fh)
act = sys.argv[2] if len(sys.argv) > 2 else None
for r in out:
    if act and r.get("action") != act:
        continue
    url = (r.get("source_url") or "").split("/")[2:3]
    print(
        r["ref"].split("#", 1)[1],
        "|",
        r.get("action"),
        "|",
        r.get("equipment"),
        "|",
        url,
        "|",
        (r.get("note") or "")[:150],
    )
