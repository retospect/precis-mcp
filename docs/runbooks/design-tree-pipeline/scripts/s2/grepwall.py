"""Strip tags from the saved Prusa page and print text around wall/perimeter mentions."""

import html
import re
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # docs/runbooks/design-tree-pipeline; was the design-tree drafts dir
DRAFTS = str(ROOT)
REPO = str(ROOT.parents[2])

with open(
    DRAFTS + "/s2/dl/prusa-modeling.html", encoding="utf-8", errors="replace"
) as fh:
    t = fh.read()
t = html.unescape(
    re.sub(
        r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S)
    )
)
t = re.sub(r"\s+", " ", t)
print(len(t))
for m in re.finditer(r"(?i)(perimeter|printable|0\.45|1\.35|0\.9 ?mm)", t):
    print("...", t[max(0, m.start() - 160) : m.end() + 160])
    print()
