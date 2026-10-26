"""Load drafter outputs, check refs against inputs, and list records needing verification."""

import collections
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

S = DRAFTS + "/s2/"
todo = []
for k in sys.argv[1:]:
    with open(f"{S}in-{k}.yaml", encoding="utf-8") as fh:
        refs = [r["ref"] for r in yaml.safe_load(fh)]
    with open(f"{S}out/{k}.yaml", encoding="utf-8") as fh:
        out = yaml.safe_load(fh)
    got = [r.get("ref") for r in out]
    missing = set(refs) - set(got)
    extra = set(got) - set(refs)
    acts = collections.Counter(r.get("action") for r in out)
    print(
        k,
        len(out),
        dict(acts),
        "missing",
        sorted(missing)[:5],
        "extra",
        sorted(extra)[:5],
    )
    for r in out:
        recs = r.get("split_into") or [r]
        for i, s in enumerate(recs):
            if s.get("source_url") and s.get("quote") and r.get("action") != "keep":
                todo.append(
                    {
                        "id": f"{r['ref']}" + (f"/{i}" if r.get("split_into") else ""),
                        "source_url": s["source_url"],
                        "quote": s["quote"],
                        "claim": {
                            x: s.get(x)
                            for x in (
                                "text",
                                "value",
                                "unit",
                                "min",
                                "max",
                                "equipment",
                            )
                            if s.get(x) is not None
                        },
                    }
                )
print("to verify", len(todo))
with open(f"{S}verify-in.yaml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(todo, fh, sort_keys=False, allow_unicode=True, width=140)
print(collections.Counter(t["source_url"].split("/")[2] for t in todo).most_common(12))
