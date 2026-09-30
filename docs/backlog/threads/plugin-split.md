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

1. **gr457894** — scripts/deploy restarts on sha change, not on
   installed-file-list change, so a pure rename leaves long-lived processes
   holding deleted modules. Every remaining step in this thread is a module
   move and no gate catches this class (a fresh test process always imports
   the new tree). Observed once, on a session MCP (gr458061's half); the
   prod worker suspected the same day was never implicated, and pathway does
   not even route through that host.
2. **gr458360** — the pytest template DB carries core migrations only, so
   every plugin-table test (se, and every future extracted plugin) is
   order-dependent: `Migrator.discover_sources` is never called, only the
   bare-Path legacy form. Already caused one red gate 2026-09-30 by
   ordering luck. Every module this thread extracts adds another plugin
   whose tests inherit this gap — fix before, not after, milestone 2
   below.
3. **gr454796** — `test_ml_calculator_cache_is_keyed_on_model_and_dispersion`
   hard-fails instead of skipping when the optional `dft-ml` extra is
   absent; the same gap a plugin-boundary test needs to not have, since
   the whole point of the split is code that runs without an extra
   installed.
4. **backlog/plugin-split-runtime-shell.md** — steps 1, 3 and 5 (step 2
   landed 2026-09-29). Step 1, the import-boundary test, now lands green
   because step 2 removed the last core→plugin import, so it goes first and
   becomes the gate the 10-16 moves are verified against.
5. **backlog/cli-lazy-subcommand-loading.md** — hard prerequisite for the
   split (installing precis-util + precis-catpath dies importing
   precis.cli.taproot) and independently closes the outage class that killed
   every node's embedder. Below 4 only because 4 is behaviour-neutral and
   this touches 58 modules.

## Horizon

Milestones 2–4 and 6 are specced inside plugin-split-runtime-shell.md's
post-10-16 ordering and share its pointer (6 pairs it with the fold-in's
own item); they become items as each comes into reach.

1. **backlog/plugin-split-runtime-shell.md + backlog/package-split.md** —
   the 2026-10-16 gate: the four open calls answered (se placement,
   "private until paper" meaning, package-split's fate, orphan-table
   ownership) and package-split ruled superseded or merged. Waits on Reto;
   nothing below starts until it closes.
2. **backlog/plugin-split-runtime-shell.md**, hexfold out first — waits on
   1, Do-next 4 (the import-boundary gate) and Do-next 1 (gr457894); proves
   the entry-point mechanics at zero API risk.
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

- (none beyond Horizon's stated waits)

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
