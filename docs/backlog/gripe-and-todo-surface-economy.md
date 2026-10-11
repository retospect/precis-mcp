---
status: idea
title: Gripe and todo threads — one-call comment+link, fast /gripes page
pillar: platform
---

# Gripe and todo threads — link in one call, fast web page

Bundled in the 2026-10-10 gripe triage. Head+tail comment render (gr472988)
and the todo comment append path (gr472987) shipped in b9f44519f. What is
left is one MCP-call ergonomics bug and the slow web page.

## 1. Comment and link in one put (gr472989)

`put(kind='gripe', id=N, text=…, link=…, rel=…)` rejects the call, so
closing a duplicate takes three calls. `src/precis/handlers/gripe.py` raises
on `link=`/`rel=` together with `text=`, with a comment saying why. Apply the
comment, then the link. The quest logbook append rejects the same
combination; fix both.

## 2. Web /gripes page (bugs, fix alongside)

- gr477858: `/gripes` loads very slowly. Not profiled yet. Profile the route
  in `src/precis_web/routes/gripes.py` first. Likely causes: it renders every
  comment thread, and it has no pagination. Paginate threads, or render head
  and tail only.
- gr477992: the page header reads "144 open, 4 in review" while the nav
  chip reads 221. The header leaves out `triaged`. List every live status
  and a total.

## Acceptance

- A duplicate-close is one `put` and one `tag`.
- `/gripes` loads in under 1 s on prod, and its header totals match the
  nav chip.

Not in scope: count, group-by and graph-shape queries. Those are gaps 2–3
in [mcp-surface-economy.md](mcp-surface-economy.md) (gr472990).

Who: agent-code, S.
