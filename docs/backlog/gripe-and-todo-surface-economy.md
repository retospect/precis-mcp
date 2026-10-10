---
status: idea
title: Gripe and todo threads — head+tail render, append path, one-call comment+link, fast /gripes page
pillar: platform
---

# Gripe and todo threads — render less, append cheaply

Bundled in the 2026-10-10 gripe triage. One deliverable: long comment
threads on `gripe` and `todo` refs cost too much to read and write, over
MCP and on the web. The sections share the render path for comment chunks,
so build them together.

## 1. Head+tail render by default (gr472988)

`get(kind='gripe')` renders every comment, up to the newest 20. That was 42%
of precis result bytes in 24 h (token review). Default to the body plus the
last 2–3 comments and a withheld count. Keep `view='comments'` for the
full thread.

## 2. Todo comment append (gr472987)

`todo` has no append path. Bodies grow to ~24 KB through `edit` and every
`get` bills them again. Add the gripe-style `put(kind='todo', id=N,
text=…)` comment chunk, and give todos the same head+tail render as §1.

## 3. Comment and link in one put (gr472989)

`put(kind='gripe', id=N, text=…, link=…, rel=…)` rejects the call, so
closing a duplicate takes three calls. Apply the comment, then the link.
The quest logbook append rejects the same combination; fix both.

## 4. Web /gripes page (bugs, fix alongside)

- gr477858: `/gripes` loads very slowly. Not profiled yet. The likely cause
  is the same as §1: it renders every comment thread, with no pagination.
- gr477992: the page header reads "144 open, 4 in review" while the nav
  chip reads 221. The header leaves out `triaged`. List every live status
  and a total.

## Acceptance

- A `get` on a gripe with 30 comments returns the body, the last 3
  comments and a withheld count.
- A todo takes a comment through `put(id=…)` without rewriting its body.
- A duplicate-close is one `put` and one `tag`.
- `/gripes` loads in under 1 s on prod, and its header totals match the
  nav chip.

Not in scope: count, group-by and graph-shape queries. Those are gaps 2–3
in [mcp-surface-economy.md](mcp-surface-economy.md) (gr472990).
