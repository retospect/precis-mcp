"""List leaf nodes from the main-branch design-tree manifests (id, file, short description, existing implies)."""

import subprocess
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

out = []


def walk(x, f):
    if isinstance(x, dict):
        if x.get("role") == "leaf":
            desc = (
                x.get("summary")
                or x.get("title")
                or x.get("desc")
                or x.get("name")
                or ""
            )
            out.append(
                {
                    "id": x["id"],
                    "file": f,
                    "desc": str(desc)[:160],
                    "keys": sorted(k for k in x if k not in ("rules",)),
                    "implies": x.get("implies"),
                }
            )
        for v in x.values():
            walk(v, f)
    elif isinstance(x, list):
        for v in x:
            walk(v, f)


for f in ("join.yaml", "rotate.yaml", "close.yaml", "processes.yaml"):
    raw = subprocess.run(
        ["git", "show", f"main:src/precis/data/design-tree/{f}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=REPO,
    ).stdout
    walk(yaml.safe_load(raw), f)
print(len(out), "with implies", sum(1 for o in out if o["implies"]))
print("keys sample", out[0]["keys"])
with open(DRAFTS + "/s2/leaves.txt", "w", encoding="utf-8") as fh:
    for o in out:
        if o["file"] != "processes.yaml":
            fh.write(f"{o['id']} | {o['desc'][:120]}\n")
