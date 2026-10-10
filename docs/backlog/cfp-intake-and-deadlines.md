---
status: idea
title: cfp kind — mint from a URL over MCP, deadline/opens-on fields, a consumer for WATCH:* tags
pillar: memory-graph
---

# cfp intake and deadlines

Moved from gr462088 in the 2026-10-10 gripe triage. Items 3–6 of that
gripe shipped in d6c4b47be. What is left:

1. **MCP intake.** `put(kind='cfp', url=…)` does not exist. A conference call
   cannot be minted from MCP, and the ingest path treats the URL as a paper.
2. **Dates as fields.** The deadline and opens-on dates are not structured
   fields, so nothing can sort or alert on them.
3. **WATCH:\* has no consumer.** cfp refs accept `WATCH:*` tags, but no
   scheduled job reads them, so the tag does nothing.

## Acceptance

- `put(kind='cfp', url=…)` mints a cfp ref, not a paper.
- `search(kind='cfp')` can sort by deadline.
- A `WATCH:` tagged cfp triggers a scheduled re-check, or the tag is
  refused.
