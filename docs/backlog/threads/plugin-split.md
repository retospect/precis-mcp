# plugin split

**Status:** ends when `pip install precis-util <one-model>` boots a serve
exposing that model's kinds and skills with no precis-mcp installed, and
catpath is the reference model in its own repo — specced in
`backlog/plugin-split-runtime-shell.md`; it is the packaging half of
`docs/mission.md`'s "machine-usable tools" for agents, since an installed
model adds kinds to a constant 7-verb surface rather than tools to an
agent's budget. Module moves are held to 2026-10-16 (Reto's 30-day rule);
until then only behaviour-neutral prep lands. Steps 1–5 of the backlog
item have landed. Reto's 2026-10-01 rulings are in it:
package-split superseded, private-until-paper = invisible. The goal is a
minimal releasable model package that is also fully useful inside precis,
and deploys that don't take hours for a simple change. Pathway
presentation work is the same thread by dependency.
**Last reviewed:** 2026-09-30 (pillar review same day added gr458360 and
gr454796 to Do next, and the 11-gripe god-module cluster to Horizon)
**Worktree:** `plugin-split` (any fresh tree works; nothing is unlanded)

## Resume here (2026-10-02)

- **Step 4 is done** (2026-10-02): CLI subcommands load lazily from
  `precis.cli.registry`; a plugin adds commands via the `precis.cli`
  entry-point group. Behaviour-neutral at the CLI surface — help output
  is byte-identical to the eager tree (`tests/test_cli_lazy.py`).
- **Step 3 is done** (deploy 38; gr459123 closed 2026-10-02 after the
  session MCP's respawn resolved `precis-pathway-help` from the plugin).
  A long-lived container holding old install metadata needs a reinstall or
  respawn after any entry-point change; the session MCP's in-place serve
  respawn was enough this time.
- **Entry-point changes need no rebuild round any more.** The ship gate
  runs `uv run` without `--no-sync` and re-syncs `/opt/venv` itself;
  `scripts/test` passes `--heal` to `scripts/lib/check-entry-points.py`,
  which reinstalls into its throwaway container (~3 s). Only long-lived
  containers (the session MCP) still need a reinstall step.
- **Dogfood on prod, not locally.** The session MCP serves the deployed
  sha only (`origin/prod`); a qland is invisible until deploy. The local
  `scripts/prod-precis tools get --kind skill --id precis-status` fallback
  runs this tree on the Mac, where `pathway` is absent because autocatpath
  is not installed — that is not a prod finding. `precis-status` now names
  gate-failed kinds ("Kinds unavailable: …") on both paths.
- **Possibly redundant test setup, unchecked:** `test_se_catalog_binding`,
  `test_se_fasten_seatclamp` and `test_se_realized_by` replay *core*
  migrations (0093, 0156, 0163) by hand. The 50 plugin seeders were removed
  2026-10-02; these were left because they are core, not plugin, SQL.

## Do next

1. **Step 7: deploy channel for member wheels** (backlog item step 7),
   now unblocked by the step-8 ruling below. Step 6 closed as prep on
   2026-10-02: the `db` marker already splits store-free from store-backed
   tests, and the per-package shares are in the item. The import boundary
   still carries one grandfathered breach, **gr459054**
   (`quest/roadmap_tick.py` importing `precis_se.handler`); the fix is the
   quest thread's, and a staleness assertion drops the exemption when they
   land it.
2. **Step 8, ruled 2026-10-02 (Reto): publish to PyPI** from
   precis-util's first release. Claim the member names before the split
   mints them and revive `publish.yml` (it lapsed at v8.4.4; pyproject is
   at 8.35.x). Step 7 (the deploy channel for member wheels) follows from
   this ruling.

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
5. **backlog/plugin-split-runtime-shell.md** — `pip install precis-util
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
