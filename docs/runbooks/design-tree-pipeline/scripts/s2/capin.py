"""Number CAP records (idx) and write the verifier input for the ones carrying a figure."""

from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(DRAFTS + "/s2/out/CAP.yaml", encoding="utf-8") as fh:
    cap = yaml.safe_load(fh)
todo = []
for i, c in enumerate(cap):
    c["idx"] = i
    if c.get("min") is None and c.get("max") is None:
        continue
    todo.append(
        {
            k: c.get(k)
            for k in (
                "idx",
                "mode",
                "field",
                "min",
                "max",
                "unit",
                "machine",
                "source_url",
                "quote",
            )
        }
    )
with open(DRAFTS + "/s2/out/CAP.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(cap, fh, sort_keys=False, allow_unicode=True, width=140)
with open(DRAFTS + "/s2/verify-cap-in.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(todo, fh, sort_keys=False, allow_unicode=True, width=140)
print(len(cap), "to verify", len(todo))
