# `_quest_status` reads Tag.namespace where it means Tag.prefix — always "unknown"

`workers/job_types/quest_tick.py::_quest_status` filters
`t.namespace == "STATUS"`, but `Tag.namespace` is the tag-KIND
discriminator (`"closed"`/`"flag"`/`"open"`); the closed prefix lives in
`Tag.prefix`. A closed `STATUS:x` tag comes back as
`Tag(namespace="closed", prefix="STATUS", value="x")`, so the comprehension
never matches and every quest reads "unknown".

Found 2026-08-28 by making the identical mistake in an op script (the
`mcp_verb_kwarg_silent_drop` shape: the defensive `getattr(..., None)`
turned a wrong attribute into silent wrong data instead of an
AttributeError).

Blast radius: cosmetic today — `_quest_status` only annotates the RC2
self-rest report, so those reports have always said `status=unknown`. The
routing decision itself uses `active_quest_ids` (SQL, correct). But it's a
copy-paste trap: audit other `tags_for(...)` consumers for the same
`namespace ==` misread before someone builds a real branch on one.

Fix: `t.prefix == "STATUS"` (+ a test asserting a closed STATUS tag is
actually read back). One-liner; ride any next ship from a quest-touching
worktree.

Related nit, same audit: `quest/allocator.py::run_allocator_pass` defaults
`compute=True` and bypasses `_quest_compute_enabled` — CLI-only (zero
worker callers), but a hand-run `precis quest` allocator pass on a
`compute_lane=off` quest would dispatch compute. Thread the switch there
too.
