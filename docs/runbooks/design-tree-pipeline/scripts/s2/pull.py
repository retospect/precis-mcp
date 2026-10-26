"""Copy the S1 repo manifests (at the landing sha) into <drafts>/s2/repo."""

import subprocess
import sys
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

SHA = sys.argv[1]
FILES = [
    "join.yaml",
    "rotate.yaml",
    "close.yaml",
    "processes.yaml",
    "capabilities.json",
]
for f in FILES:
    out = subprocess.run(
        ["git", "show", f"{SHA}:src/precis/data/design-tree/{f}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=REPO,
    ).stdout
    with open(f"{DRAFTS}/s2/repo/{f}", "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f, len(out))
