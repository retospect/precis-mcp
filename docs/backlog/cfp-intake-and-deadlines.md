---
status: idea
title: cfp kind — mint from a URL over MCP, deadline/opens-on fields (WATCH re-check shipped)
pillar: memory-graph
---

# cfp intake and deadlines

Moved from gr462088 in the 2026-10-10 gripe triage. Items 3–6 of that
gripe shipped in d6c4b47be. What is left:

1. **MCP intake.** `put(kind='cfp', url=…)` does not exist:
   `src/precis/handlers/cfp.py::CfpHandler` sets `supports_put=False`, so a
   call can only enter as a PDF (`precis add --as cfp`, `inbox/cfp/`). The
   ingest path treats a URL as a paper.
2. **Dates as fields.** The deadline and opens-on dates are not structured
   fields, so nothing can sort or alert on them.

Owner `src/precis/handlers/cfp.py`. The `WATCH:*` re-check shipped
(`workers/cfp_watch.py`); it reads `meta.source_url`, which item 1's intake
must set on the cfp.

RULED (Reto 2026-10-12, design-fold): build item 2 first, then item 1.

- **Slice 1 — typed dates.** `meta.deadline` and `meta.opens_on` (ISO
  dates, UTC), set with `edit(kind='cfp')` and validated like
  `meta.source_url`; a bad date rejects with BadInput. `search(kind='cfp')`
  sorts by deadline. Not a `due:<date>` tag: tags are free text and cannot
  sort by date. Until slice 1 lands, a `due:` tag is the stopgap.
- **Slice 2 — URL intake.** `put(kind='cfp', url=…)` mints a cfp (never a
  paper) and sets `meta.source_url`, so `cfp_watch` picks it up.

Who: agent-code.

## Acceptance

- `put(kind='cfp', url=…)` mints a cfp ref, not a paper.
- `search(kind='cfp')` can sort by deadline.
