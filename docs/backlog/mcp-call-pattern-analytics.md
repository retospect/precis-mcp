---
status: idea
title: Call-pattern analytics over the tool_calls ledger (gr484070 item 5)
pillar: platform
---

# call-pattern analytics over the tool ledger

The broad ask deferred from gr484070: mine *all* `tool_calls` rows —
not just rejected-before-work errors — for usage patterns that drive
tool/skill design. Which kinds/args get misused, which skills get
searched right after a failure (post-error help-seeking), which verbs
get retried to success, which arg shapes correlate with abandons.
Capture is already done: the ledger records every dispatch with
session_id/seq, masked arg_sample/error_head, result_bytes and client.
What's missing is the analytics pass — detectors over the full window
(`scripts/mine-sessions/detect.py` is the offline prototype) and a
surface for the findings (a recurring digest, or gripes like the
call_errors half that shipped).
