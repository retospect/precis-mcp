# plugin split

**Status:** ends when `pip install precis-util <one-model>` boots a serve
exposing that model's kinds and skills with no precis-mcp installed, and
catpath is the reference model in its own repo — specced in
`backlog/plugin-split-runtime-shell.md`; it is the packaging half of
`docs/mission.md`'s "machine-usable tools" for agents, since an installed
model adds kinds to a constant 7-verb surface rather than tools to an
agent's budget. Today the split is prep-only until 2026-10-16; step 2 has
landed, and the dogfood that looked like it broke prod was a stale local
MCP (gr458061) — but the deploy-mechanics defect it surfaced is real and
every remaining step re-triggers it. Make module moves deploy-safe, then
continue the behaviour-neutral prep. Pathway presentation work is the same
thread by dependency.
**Last reviewed:** 2026-09-30 (pillar review same day added gr458360 and
gr454796 to Do next, and the 11-gripe god-module cluster to Horizon)
**Worktree:** `plugin-split`

## Do next

1. **gr458360** — the pytest template DB carries core migrations only, so
   every plugin-table test (se, and every future extracted plugin) is
   order-dependent: `Migrator.discover_sources` is never called, only the
   bare-Path legacy form. Already caused one red gate 2026-09-30 by
   ordering luck. Every module this thread extracts adds another plugin
   whose tests inherit this gap — fix before, not after, milestone 2
   below.
2. **gr454796** — `test_ml_calculator_cache_is_keyed_on_model_and_dispersion`
   hard-fails instead of skipping when the optional `dft-ml` extra is
   absent; the same gap a plugin-boundary test needs to not have, since
   the whole point of the split is code that runs without an extra
   installed.
3. **backlog/plugin-split-runtime-shell.md** — steps 3 and 5 (step 2 landed
   2026-09-29, step 1 landed 2026-10-01 as
   `tests/test_plugin_import_boundary.py`). The boundary is now a gate
   rather than a convention, which is what the 10-16 moves get verified
   against. It carries one grandfathered breach, **gr459054** —
   `quest/roadmap_tick.py` importing `precis_se.handler`, found by
   dogfooding the same day, the same violation step 2 removed reintroduced
   four days later in a different file. The fix is the quest thread's;
   a staleness assertion drops the exemption automatically when they land
   it. Step 3 (declare `precis.skills` from one in-tree plugin) was built
   and proved working on 2026-10-01, then reverted unlanded — **gr459123**:
   an entry point is read from installed dist metadata, not the checkout, so
   both long-lived containers still report an empty group and landing it
   would red the pathway skill assertion in every in-flight worktree's gate
   until each image rebuilds. Prod is not at risk (deploy does a real
   `uv pip install --upgrade`). It needs only an announced dev-image
   rebuild round — not gr457894, per the correction below. It is the
   dev-side form of the same class: an installed dist serving metadata its
   pyproject no longer matches.
4. **backlog/cli-lazy-subcommand-loading.md** — hard prerequisite for the
   split (installing precis-util + precis-catpath dies importing
   precis.cli.taproot) and independently closes the outage class that killed
   every node's embedder. Last only because 3 is behaviour-neutral and this
   touches 58 modules.

**gr457894 left Do next on 2026-10-01, and this is the correction that
matters most in this file.** It sat at Do-next 1 for five rounds on the claim
that `scripts/deploy` restarts on a sha change but not on an installed-file-list
change, so a module move would leave long-lived processes holding deleted
modules — making the restart story a precondition for every extraction here.
Reading `deploy/redeploy-precis.yml` before building it shows the premise is
false: step 0b sets `precis_deploy_changed` from the installed-vs-pinned sha
(so a rename fires it), step 0b2 classifies bounce scope fail-safe toward
`full` for `src/precis/`, `src/precis_pathway/`, unknown paths and any diff
failure, and `full` drains and bounces the **worker** units, not only web.
This round supplied a matching data point: a `src/precis_pathway/`-only
commit deployed at 00:30Z, and the 04:12Z dispatched run executed the new
code. Fix option 1 is withdrawn in the gripe; what survives is dev-side
staleness, owned by gr458061 (session MCP containers) and gr459123 (gate
container metadata). Nothing in this thread waits on it.

## Horizon

Milestones 2–4 and 6 are specced inside plugin-split-runtime-shell.md's
post-10-16 ordering and share its pointer (6 pairs it with the fold-in's
own item); they become items as each comes into reach.

1. **backlog/plugin-split-runtime-shell.md** — the 2026-10-16 gate, now
   mostly closed. Reto ruled 2026-10-01: package-split is **superseded**
   (deleted; its prep lifted as the item's steps 6–8) and "private until
   paper" means **invisible** — the catpath precedent, no separate
   database. Two calls stay in the item, neither blocking: se placement is
   low-stakes (it moves with geom either way) and orphan-table ownership
   needs an owner rather than a decision. Reto restated the goal the same
   day: a minimal package releasable publicly *and* fully useful inside
   precis, plus a hope that modularity shrinks the deploy jam — which the
   item records as only partly supported (59% of commits touch core).
2. **backlog/plugin-split-runtime-shell.md**, hexfold out first — waits on
   the 10-16 snooze only; the import-boundary gate it also wanted landed 2026-10-01, and
   the restart precondition it used to carry (gr457894) turned out not to
   exist. Proves the entry-point mechanics at zero API risk.
3. **backlog/plugin-split-runtime-shell.md**, precis_surface out — waits on
   2; folds the marching-cubes tables into geom so the boundary is
   exercised, not declared.
4. **backlog/plugin-split-runtime-shell.md**, the precis-geom wheel
   (cad/structure/design/blocktree + se) — waits on 3; the load-bearing
   layer (948 KB, se at 138 geometry imports).
5. **backlog/plugin-split-runtime-shell.md +
   backlog/cli-lazy-subcommand-loading.md** — `pip install precis-util
   <one-model>` boots a serve exposing that model's kinds and skills with no
   precis-mcp installed. Waits on 4; the acceptance criterion the whole
   split is judged against.
6. **backlog/pathway-presentation-shared-module.md +
   backlog/plugin-split-runtime-shell.md** — catpath as the reference
   model, private until paper; waits on 5, which answers where shared
   presentation logic lives. Needs a catpath version bump + wheel
   redeploy.
7. **The 11-gripe god-module cluster** (**gr343710**, **gr343711**,
   **gr343712**, **gr343713**, **gr343714**, **gr343715**, **gr343716**,
   **gr343717**, **gr343719**, **gr343720**, **gr343752**) — the
   decomposition debt the split retires; not individually actionable, and
   not worth ranking piece by piece when the split's module-by-module
   extraction addresses the whole cluster by construction.

## Parked

- **gr458944** — pathway titles. Dogfooding this thread's surface on prod
  2026-09-30 found 697 of 701 pathway refs titled "(computing)", including
  every `status: ready` one: the dispatched-job path seeds that placeholder
  and completion never replaced it. The write-path fix is **verified on
  prod** (ref 459170, completed 04:12Z 2026-10-01, titled `NO → NH3 on Pd`).
  The backfill ran 2026-10-01: 529 `ready` rows retitled from their stored
  results, 170 failed/superseded rows had `(computing)` swapped for their
  status, and one genuinely computing row was left alone. It is additive
  and reversible, since the old title is `pathway <slug> (computing)` and
  the slug is still on each ref. Only the failed-path retitle in
  `quest/loop.py` is left, and it is the quest thread's: core cannot reuse
  `pathway_title` (plugin-owned) but does not need to, since the failed case
  is a suffix swap. Deployed is not verified: closing the gap took three
  dogfood rounds, because only an arriving input could exercise the path.

## No action needed

- **td457903** — withdrawn: the pathway ImportError was the reporting
  session's own stale dev MCP container (gr458061's shape), not prod. Do not
  re-file it as a prod restart. Dogfood redone 2026-09-30 on a fresh
  engine-capable process: `compare` and the U-lever both correct on prod
  records, so step 2 is confirmed behaviour-neutral and nothing here is
  broken.
- **autocatpath<0.23 ceiling** — ruled out: the parity gate is a live check
  against the installed engine.
- **Per-plugin Postgres schemas** — ruled out 2026-09-27: pgbouncer
  transaction pooling makes dynamic search_path a race.
- **Extracting precis_se as a peer plugin** — ruled: geometry application
  layer, not a domain model.
