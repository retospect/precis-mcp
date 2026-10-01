---
status: ready
pillar: platform
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

## Reviewed 2026-10-01: 19 keep, 2 optional, 8 salvage, 14 drop

All 43 were bundled off the node and test-merged against current main
(`git merge-tree`, a real conflict answer): 33 merge clean, 10 conflict.
Five read-only reviewers then checked each gripe against current main and
read each diff. **No branch's tests were run** — "has a test" means the diff
carries one. Every KEEP still goes through the gate.

- **KEEP (19)** — defect still live on main, fix sound:
  `gripe_182230` (chase-coverage ledger; carries a migration numbered 0171
  that main already used — renumber, then `/go` only), `gripe_228652`
  (glyph_health at extraction — the ingest thread's rank-2 item; land first
  there), `gripe_294498` (FIFO gate-slot tickets; also covers gr343941's
  remaining half), `gripe_450122`, `gripe_450123` (narrow its `except` to
  embedder causes first), `gripe_450524`, `gripe_451270`, `gripe_451274`,
  `gripe_452084` (defect 4 only, the unbounded `kind_provider` table),
  `gripe_452204`, `gripe_453828` (restrict the "installed but not declared"
  check to the project's own dist), `gripe_453862` (take as the base over
  453859 — both rewrite the same claim SQL), `gripe_454753` (unblocks the
  nanobuds export), `gripe_454792`, `gripe_454796`, `gripe_454865`,
  `gripe_456213` (same-batch residual; file a gripe id for it),
  `gripe_456240`, `gripe_456641`.
- **Optional (2)** — `gripe_180306` (hub reconcile sweep: a feature, one LLM
  call per candidate pair; the reported pair is already merged),
  `gripe_451269` (one docs paragraph).
- **SALVAGE (8)** — lift parts, redo by hand: `gripe_248866` (unverified
  whether a child process inherits the heartbeat's macOS TCC grant, which
  would make the probe a false green; interpreter list duplicated from
  deploy), `gripe_451276` (router still treats a pin's second pad as a
  foreign obstacle), `gripe_451356` (substring package match false-flags;
  where mating direction comes from is undecided), `gripe_452203` (dedup
  still misses the gripe's own example), `gripe_453859` (lift the re-arm
  guard and `--needs-manual`; share one pass definition with 453862),
  `gripe_453860` (keep journal + census, drop the auto-heal — it fans out
  ~914 refs on its first pass), `gripe_454480` (an already-fixed run
  reopens the gripe, so it may loop), `gripe_458084` (counter, prev/next
  links and banner stay on the loaded revision after an in-place step).
- **DROP (14)** — fixed independently on main: `gripe_450132`,
  `gripe_453913`, `gripe_454488`, `gripe_454563`, `gripe_454650`,
  `gripe_456236`, `gripe_457053`, `gripe_457326`, `gripe_457361`,
  `gripe_458087`; superseded: `gripe_228699` (by 228652), `gripe_458061`
  (by the 457361 fix); not a fix: `gripe_452384` (patches a sibling, the
  gripe is fixed), `gripe_311857` (widens the piped-exit guard, which
  pre-empts Reto's open choice in `piped-exit-guard-tuning.md`).

## Still there, re-measured 2026-10-01

Reto asked whether these are still around. They are — all 43, unchanged in
count, on the same node. What changed is the decay, and the re-measure moved
every number in this item:

- **Behind `main`: 64 to 282 commits**, not the 36–254 recorded on 09-30. Main
  moved ~28 commits overnight, so the freshest four (`gripe_456641`,
  `gripe_458061`, `gripe_458084`, `gripe_458087`) are now 64 behind rather than
  36. Nothing was rebased; the branches stood still.
- **The oldest are older than this item said.** `gripe_450122` and
  `gripe_450132` are 282 behind, authored 2026-09-25/26.
- **Scratch is growing, not static: 59 clone directories, 17G** (was 55 / 16G),
  plus 161M of `diagnose_clones/`. **15 of the 59 clones have no matching
  branch at all.** The growth is not idle churn — the four new clones are the
  four jobs the publish pre-flight skipped after 09-30 16:04Z, which is its own
  bug, filed as **gr458899**: the pre-flight runs *after* the clone, so every
  skip leaves ~300MB behind forever.
- Nothing has written a branch since 2026-09-30 11:51Z. The lane is inert as
  ruled; only the disk keeps moving.

The decay argument in this item is therefore stronger than when it was
written, not weaker: a further day cost the freshest branches 28 commits of
divergence each.

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
