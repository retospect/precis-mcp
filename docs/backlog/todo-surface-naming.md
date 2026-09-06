---
status: draft
prio: normal
title: Todo surface naming — tier enum, wont-do, wip/doing, view= convergence
---

# Todo surface naming — agent-facing clarity items

Todo-infra review (2026-09-06). Each item is independent; none blocks the
others. The prio-decoy finding from the same review shipped separately
(verb-level `prio=` + `PRIO:` alias sync — see git log).

- **Tier as enum, not facet booleans.** `meta.rotation_root=true` /
  `meta.worker_mintable=false` make agents memorize "worker_mintable=false
  means tactical" (a negated permission consequence standing in for a
  concept). Surface a single `meta.tier: strategic|tactical|subtask` on
  put/tag and in the skills; the two booleans can remain as storage
  (derive on write) or migrate. Skills should lead with tier names.
- **`won't-do` → `wont-do`.** Apostrophe in a closed-vocab value is a
  quoting hazard — our own doable SQL escapes it (`won''t-do`), and every
  agent tag call risks the same. Keep the old value as a read/write alias
  through a deprecation window.
- **plan `wip` vs todo `doing`.** Same concept, two names across kinds
  that share one ecosystem (`plan`/`make` use `wip`; todo uses `doing`).
  Pick `doing` everywhere; accept the other as input alias.
- **Converge list surfaces on `view=`.** `get(kind='todo', id='/open')`
  overloads `id=` as a view selector, and "open" means three things
  (STATUS value; a view that includes doing+blocked; the OPEN tag
  namespace). Move the flat views to `view=` on get/search, keep `/paths`
  as deprecated aliases, and rename the open+doing+blocked view `active`.
- **`view='parked'` union.** A resuming agent wanting "everything stalled
  under this root" needs 4 calls (waiting / blocked / ask-user /
  attention). One union view with a reason column per row.
- **STATUS:paused loses `doing`.** Pause rides the STATUS axis
  (closed-prefix replace), so pausing a doing leaf and unpausing lands it
  on `open`. Pause is a subtree *modifier*, not a lifecycle state — an
  open `paused` tag would preserve the underlying status. (Query-time
  ancestor-walk already reads tags, so the SQL change is contained.)

test: per item; the alias windows each need a both-forms-accepted test.
