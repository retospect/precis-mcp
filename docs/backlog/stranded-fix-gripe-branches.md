---
status: ready
prio: high
---

# 43 fix_gripe branches are stranded on the agent-lane worker — merge or delete

Every fix the auto-fix lane ever produced is sitting on one node's disk. The
lane pushed into the worker's own checkout instead of the shared remote and
reported success (gr458326, fixed 2026-09-30); nothing forwarded the branches
on, and nothing is watching them. Reto's call on the lane itself is **leave it
not doing anything** — so this item is only about the 43 branches it already
made.

They are perishable. Every one was authored between 2026-09-25 and 2026-09-30
and now sits 36–254 commits behind `main`. They rot in place: the further main
moves, the more of each diff is either conflict or already-redundant.

## Where they are

The agent-lane worker's fix-repo checkout (`PRECIS_FIX_REPO_DIR`, deploy-owned)
holds all 43 as plain local branches with no upstream. One branch per gripe, no
duplicates. The scratch root (`PRECIS_FIX_WORK_DIR/clones/`) separately holds
**55 per-gripe clones totalling 16G**, plus 161M of `diagnose_clones/` — that
is a second, unrelated problem worth a look while someone is on the box.

## What the triage found (2026-09-30, read-only)

Freshest four, 36 commits behind, authored 2026-09-30 — the lowest-risk
candidates and the ones whose gripes are most likely still live:

| branch | subject | diffstat |
|---|---|---|
| `gripe_456641` | flag measure_environment's far-rim runaway | 3 files, +93 −2 |
| `gripe_458061` | reveal mounted-checkout drift | 2 files, +194 −14 |
| `gripe_458084` | scrub revisions through live-scene seam | 2 files, +75 −10 |
| `gripe_458087` | index check_via_pad_keepout with per-layer STRtree | 1 file, +115 −72 |

Largest diffs, all 153–158 behind, so the highest value *and* the highest
decay: `gripe_453860` (journal + heal fetched-but-bodiless papers, 10 files,
+734), `gripe_454753` (shared export preflight on every draft path, 8 files,
+620), `gripe_228652` (record glyph_health at extraction, 6 files, +901),
`gripe_451356` (edge-mating connectors + DRC, 5 files, +432).

Stalest two, 254 behind, authored 2026-09-25/26 — likeliest to delete
unexamined: `gripe_450122` (docs-only citation split) and `gripe_450132`
(reject id-bearing todo put).

The full per-branch table (tip sha, author date, commits-behind, diffstat,
subject) is reproducible on the node in one pass; it is not copied here because
tracked files carry no git shas.

## Two things the triage did NOT establish

- **Whether any branch still applies.** The triage reported "all branches apply
  cleanly, no merge conflicts anticipated" on the basis that each merge-base is
  an ancestor of `main`. That does not follow — a merge-base is an ancestor of
  main by construction, for every branch, always. It is evidence of nothing.
  Conflict risk is unmeasured, and at 150+ commits behind it is the dominant
  question for the large diffs.
- **Whether the work is already superseded.** Several of these gripes were
  fixed by hand afterwards (gr456236 and gr457361 are known cases; there are
  likely more among the 39 that were reset from `in_review` to `open`). A
  branch whose gripe was independently fixed is pure deletion.

## The ask

A merge-or-delete pass, cheapest-first:

1. For each branch, check whether its gripe is already closed or independently
   fixed — that is a DB question and disposes of some fraction outright.
2. For survivors, `git merge-tree` against current main for a real conflict
   answer (not the ancestry check above).
3. Merge what is clean and still wanted, through the normal gate; delete the
   rest. Nothing here should bypass review just because an agent wrote it.

Do it before the diffs decay further. Separately, reclaim the 16G of scratch
clones — the lane has no cleanup path for them.
