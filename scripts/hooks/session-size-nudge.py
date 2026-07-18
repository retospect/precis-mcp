#!/usr/bin/env python3
"""PostToolUse hook: propose /compact as the session grows large.

Hooks don't get a live token count, so this uses the transcript file SIZE as a
proxy for context fullness. At rising tiers it injects a one-line nudge telling
the agent to persist residuals + propose ``/compact`` to the user — a deliberate
compact (with residuals saved) beats a silent auto-summarize that can drop
in-flight work. Fires **once per tier** (a single state file in TMPDIR records
the highest tier already fired), so it's a fast silent stat on every other call.

The transcript grows monotonically (even across compactions), so post-compact
the next nudge simply comes at the next tier — no cross-hook state needed.

Tune with ``PRECIS_SESSION_NUDGE_MB`` (first tier, default 6). Tiers = base,
2·base, 3·base, 5·base MB. Set it very high to effectively disable.

Wired in .claude/settings.json (PostToolUse, catch-all matcher). Never blocks.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tpath = payload.get("transcript_path") or ""
    sid = payload.get("session_id") or "nosid"
    if not tpath or not os.path.isfile(tpath):
        return 0
    try:
        size = os.path.getsize(tpath)
    except OSError:
        return 0

    base = int(os.environ.get("PRECIS_SESSION_NUDGE_MB", "6")) * 1_000_000
    tiers = [base, 2 * base, 3 * base, 5 * base]
    crossed = sum(1 for t in tiers if size >= t)  # 0..4
    if crossed == 0:
        return 0

    state = os.path.join(tempfile.gettempdir(), f"precis-sizenudge-{sid}")
    try:
        with open(state) as f:
            fired = int(f.read().strip() or "0")
    except (OSError, ValueError):
        fired = 0
    if crossed <= fired:
        return 0
    try:
        with open(state, "w") as f:
            f.write(str(crossed))
    except OSError:
        pass

    mb = size // 1_000_000
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[session-size] Transcript ~{mb}MB — the context is getting large. "
                        "Persist any residuals (OPEN-ITEMS / gripe / memory), then PROPOSE "
                        "`/compact` to the user for a clean-context continuation — a deliberate "
                        "compact beats a silent auto-summarize. (The PreCompact hook reminds on "
                        "persist; raise PRECIS_SESSION_NUDGE_MB to nudge later.)"
                    ),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
