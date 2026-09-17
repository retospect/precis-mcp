---
status: draft
title: ci gate shape follow-ups — gate on one Linux leg, and a docs-only lane that still runs the doc-reading tests
prio: normal
---

# ci gate shape follow-ups

After the 6-way shard (ed4c9ee9, 2026-09-16) the `scripts/ship --remote`
gate is ~12 min wall and 17 jobs. The account's standard-runner cap is 20
(GitHub Free; larger runners need an org on Team and are billed even on
public repos, so they are not an option). Two further cuts, each a gate
**policy** change Reto decides, not a mechanical one.

## 1. Gate on one Linux leg

Move Python 3.12 and the macOS/Windows legs from `push`/`pull_request` to
the existing `schedule` trigger in `check.yml`; keep lint + 6 shards of
3.13 as the ship gate. 17 jobs → 11, so two concurrent ships fan out fully
under the cap instead of the second one queueing.

Trade-off: the Windows leg was red on main for days this week until
soft-wobbling-rocket fixed the path seams (af78f144). Monthly-only platform
legs would find such breaks weeks later and off the ship that caused them.
Mitigation if taken: nightly, not monthly, for the demoted legs, and
`scripts/ship` prints the last scheduled run's verdict as a warning.

## 2. Docs-only lane

27% of commits over the last 30 days touch only `docs/`, `scripts/`, or
`.claude/`. A plain `paths-ignore` skip is **unsafe here**: 172 test
modules reference `docs/` and several read it as data
(`test_doc_pointers.py`, `test_deploy_tree_no_secrets.py`,
`test_checklist_sync.py`, backlog front-matter checks), so a docs-only
diff can still go red. The correct shape is a **docs lane**, not a skip:

- `dorny/paths-filter` (or `git diff --name-only` against the merge base)
  classifies the push; a docs-only push runs lint + one unsharded job of
  the doc-reading tests (`-k` list or a `docs` marker) and skips the shard
  matrix.
- `scripts/ship` already accepts `skipped` job conclusions in its
  failing-job filter, so a skipped matrix reads as green; the run-level
  conclusion is `success` when every non-skipped job succeeds. No ship
  change needed beyond documenting the lane.
- `src/precis/data/skills/**` and `scripts/**` are NOT docs for this
  purpose (skills are served at runtime and tested; scripts are executed by
  tests). Only `docs/**`, `*.md` at the root, and `.claude/**` qualify.

## Acceptance criteria

1: a ship on a code change runs 11 jobs; a scheduled run still exercises all
17. 2: a docs-only ship completes in under 5 min and still fails when a
backlog file breaks front-matter or a doc pointer dangles.

## Open questions / decisions log

- Take 1 at all, given the Windows precedent? Nightly cadence acceptable?
- For 2: marker vs `-k` list for the docs tests. A marker is durable; the
  list rots.
