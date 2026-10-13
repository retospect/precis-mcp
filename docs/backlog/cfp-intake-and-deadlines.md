---
status: idea
title: cfp kind — mint from a URL over MCP (typed dates shipped)
pillar: memory-graph
---

# cfp intake and deadlines

Moved from gr462088 in the 2026-10-10 gripe triage. Items 3–6 of that
gripe shipped in d6c4b47be. What is left:

1. **MCP intake.** `put(kind='cfp', url=…)` does not exist:
   `src/precis/handlers/cfp.py::CfpHandler` sets `supports_put=False`, so a
   call can only enter as a PDF (`precis add --as cfp`, `inbox/cfp/`). The
   ingest path treats a URL as a paper.

Owner `src/precis/handlers/cfp.py`. The `WATCH:*` re-check shipped
(`workers/cfp_watch.py`); it reads `meta.source_url`, which item 1's intake
must set on the cfp. Item 2 (typed dates) shipped: `meta.deadline` /
`meta.opens_on` via `edit(kind='cfp', args=…)`, shown in `get`, and
`search(kind='cfp', args={'sort': 'deadline'|'opens_on'|'recency'})` —
the queryless browse defaults to deadline order.

RULED (Reto 2026-10-12, design-fold): build item 2 first, then item 1.

- **Slice 1 — typed dates.** SHIPPED.
- **Slice 2 — URL intake.** `put(kind='cfp', url=…)` mints a cfp (never a
  paper) and sets `meta.source_url`, so `cfp_watch` picks it up.

Who: agent-code.

## Acceptance

- `put(kind='cfp', url=…)` mints a cfp ref, not a paper.
