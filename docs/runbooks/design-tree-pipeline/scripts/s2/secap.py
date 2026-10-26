"""Summarise se_capabilities.json at the S1 sha: modes and the fields each row carries."""

import json
import subprocess
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

raw = subprocess.run(
    ["git", "show", "03589268e:src/precis/data/se_capabilities.json"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=True,
    cwd=REPO,
).stdout
with open(DRAFTS + "/s2/repo/se_capabilities.json", "w", encoding="utf-8") as fh:
    fh.write(raw)
d = json.loads(raw)
print([k for k in d if k != "rows"])
for r in d["rows"]:
    f = r["fields"]
    filled = [
        k
        for k, v in f.items()
        if isinstance(v, dict)
        and (v.get("house") is not None or v.get("physical") is not None)
    ]
    print(
        r["mode"],
        "|",
        r["title"],
        "| filled:",
        ",".join(filled),
        "| null:",
        ",".join(k for k in f if k not in filled),
    )
