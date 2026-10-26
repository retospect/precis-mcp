"""Dump full cached Perplexity answers (ref body chunks) to raw/<id>.md, read-only.

Usage: python3 dump_pplx.py <repo_dir> <id> [<id> ...]
"""

import os
import pathlib
import subprocess
import sys
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

repo = pathlib.Path(sys.argv[1])
out = pathlib.Path(__file__).parent / "raw"
out.mkdir(exist_ok=True)
env = dict(os.environ, PRECIS_PROD_PSQL_OPTS="-At")
for rid in sys.argv[2:]:
    sql = (
        "SELECT string_agg(text, E'\\n\\n' ORDER BY ord) FROM chunks "
        f"WHERE ref_id = {int(rid)} AND retired_at IS NULL"
    )
    res = subprocess.run(
        [str(repo / "scripts/prod-psql"), "--ro", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    (out / f"{rid}.md").write_text(res.stdout, encoding="utf-8")
    print(rid, len(res.stdout))
