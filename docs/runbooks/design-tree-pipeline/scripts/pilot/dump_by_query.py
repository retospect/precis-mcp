"""Find the cached perplexity-reasoning ref whose title is the query in q/<slug>.txt,
dump its full body (read-only) to raw/<slug>.md, print '<slug> <ref_id> <bytes>'.

Usage: python3 dump_by_query.py <repo_dir> <run_dir> <slug>
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

repo, run, slug = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
query = (run / "q" / f"{slug}.txt").read_text(encoding="utf-8").strip()
if "$qq$" in query:
    sys.exit("query contains the dollar-quote tag")
env = dict(os.environ, PRECIS_PROD_PSQL_OPTS="-At")


def psql(sql):
    res = subprocess.run(
        [str(repo / "scripts/prod-psql"), "--ro", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return [ln for ln in res.stdout.splitlines() if ln not in ("BEGIN", "COMMIT")]


rows = psql(
    "SELECT ref_id FROM refs WHERE kind = 'perplexity-reasoning' AND "
    f"left(title, 120) = left($qq${query}$qq$, 120) ORDER BY ref_id DESC LIMIT 1"
)
if not rows or not rows[0].strip():
    sys.exit(f"{slug}: no cached ref found for this query; did the get() call succeed?")
rid = int(rows[0])
body = psql(
    "SELECT string_agg(text, E'\\n\\n' ORDER BY ord) FROM chunks "
    f"WHERE ref_id = {rid} AND retired_at IS NULL"
)
text = "\n".join(body)
out = run / "raw"
out.mkdir(exist_ok=True)
(out / f"{slug}.md").write_text(
    f"<!-- perplexity-reasoning:{rid} -->\n{text}\n", encoding="utf-8"
)
print(slug, rid, len(text))
