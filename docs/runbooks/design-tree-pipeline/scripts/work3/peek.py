"""Print contradicted-dropped claims and claims carrying verifier suggestions (suggested_text, correct_value, ...)."""

import json
import pathlib
import sys
from pathlib import Path

import yaml

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

RUN = pathlib.Path(DRAFTS + "/run3/verified")
d = json.load(open(DRAFTS + "/work3/build3.json", encoding="utf-8"))  # noqa: SIM115
print("== contradicted, no override")
for c in d["contradicted"]:
    print(
        c[0].split("/")[-1],
        c[1],
        "|",
        c[2],
        "|",
        c[3],
        "|",
        str(c[4])[:160],
        "|",
        str(c[5])[:300],
    )
print("== suggestions")
keys = ("suggested_text", "correct_value", "source_url_corrected", "remapped_to")
for b in sys.argv[1:]:
    for n in yaml.safe_load((RUN / f"{b}.yaml").read_text(encoding="utf-8")):
        if not isinstance(n, dict) or "claims" not in n:
            continue
        for c in n["claims"]:
            hit = {k: c[k] for k in keys if c.get(k)}
            if hit:
                print(
                    b,
                    n["id"].split("/")[-1],
                    c["id"],
                    c.get("verdict"),
                    "|",
                    c["text"][:120],
                    "|",
                    c.get("value"),
                    "|",
                    hit,
                )
