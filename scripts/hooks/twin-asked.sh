#!/usr/bin/env bash
# UserPromptSubmit hook (gr548277): when the prompt is a "what is X?" ask,
# print a one-line reminder that the term belongs in the card forge's
# twin:asked queue. Advisory only — never blocks, always exit 0.
#
# The regex table mirrors precis.reading.asked.extract_asked_term (the asa
# capture uses that copy; this script stays dependency-free for hook
# contexts) — keep the two in step.
#
# Wiring (settings.json — not in this repo's tree): a UserPromptSubmit
# entry running `bash "$CLAUDE_PROJECT_DIR/scripts/hooks/twin-asked.sh"`.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

TWIN_ASKED_PAYLOAD="$payload" python3 - <<'PYEOF' || exit 0
import json
import os
import re
import sys

payload = os.environ.get("TWIN_ASKED_PAYLOAD", "")
try:
    prompt = str(json.loads(payload).get("prompt") or "")
except Exception:
    sys.exit(0)
prompt = prompt.strip()
if not prompt:
    sys.exit(0)

WHAT_RE = re.compile(
    r"^(?:what|what's|whats)\s+(?:is|are|does|do)\s+"
    r"(?:an?\s+|the\s+)?(?P<term>[^?]{2,60}?)(?:\s+mean)?\s*\??$",
    re.IGNORECASE,
)
REMIND_RE = re.compile(
    r"^remind\s+me\s+what\s+(?:an?\s+|the\s+)?(?P<term>[^?]{2,60}?)\s+is\s*\??$",
    re.IGNORECASE,
)
BARE_RE = re.compile(r"^(?P<term>\S{2,40})\?$", re.IGNORECASE)
EXCLUDE_RE = re.compile(r"^status\s+of\b", re.IGNORECASE)

term = None
for pat in (WHAT_RE, REMIND_RE, BARE_RE):
    m = pat.match(prompt)
    if m:
        t = " ".join(m.group("term").split())
        if t and not EXCLUDE_RE.match(t):
            term = t
            break

if term is None:
    sys.exit(0)
print(
    "twin: Reto asked what '%s' is — after answering, "
    "put(kind='concept', text='%s: <one-line definition>', tags=['twin:asked'])"
    % (term, term)
)
PYEOF
exit 0
