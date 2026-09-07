---
status: ready
prio: normal
title: Todo surface naming — tier enum + view= convergence (approved 2026-09-06)
---

# Todo surface naming — approved items

Todo-infra review (2026-09-06); Reto approved these two, declined the rest
(see foot). Independent; neither blocks the other.

- **Tier as enum, not facet booleans.** `meta.rotation_root=true` /
  `meta.worker_mintable=false` make agents memorize "worker_mintable=false
  means tactical" (a negated permission consequence standing in for a
  concept). Surface a single `meta.tier: strategic|tactical|subtask` on
  put/tag and in the skills; the two booleans remain as storage (derive on
  write). Skills should lead with tier names.
- **Converge list surfaces on `view=`.** `get(kind='todo', id='/open')`
  overloads `id=` as a view selector, and "open" means three things
  (STATUS value; a view that includes doing+blocked; the OPEN tag
  namespace). Move the flat views to `view=` on get/search, keep `/paths`
  as deprecated aliases, and rename the open+doing+blocked view `active`.

test: per item; the `/path` alias window needs a both-forms-accepted test.

Declined 2026-09-06 (don't re-propose without new evidence): `won't-do` →
`wont-do` rename; plan-`wip` → `doing` convergence; `view='parked'` union;
paused-as-open-tag.
