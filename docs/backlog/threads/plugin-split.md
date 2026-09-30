# plugin split

**Status:** ends when `pip install precis-util <one-model>` boots a serve
exposing that model's kinds and skills with no precis-mcp installed, and
catpath is the reference model in its own repo. Today the split is
prep-only until 2026-10-16; step 2 has landed and its prod dogfood exposed
a deploy-mechanics defect every remaining step will re-trigger. Unbreak
prod, make module moves deploy-safe, then continue the behaviour-neutral
prep. Pathway presentation work is the same thread by dependency.
**Last reviewed:** 2026-09-30
**Worktree:** `plugin-split`

## Do next

1. **td457903** — pathway compare and the U-lever return ImportError on prod
   now; the fix is one restart of the stale castor worker, on Reto's queue.
   A broken live surface outranks everything here and is the cheapest item.
2. **gr457894** — the durable half of 1: scripts/deploy restarts on sha
   change, not on installed-file-list change, so a pure rename leaves
   long-lived workers holding deleted modules. Every remaining step is a
   module move and no gate catches this class (a fresh test process always
   imports the new tree).
3. **backlog/plugin-split-runtime-shell.md** — steps 1, 3 and 5 (step 2
   landed 2026-09-29). Step 1, the import-boundary test, now lands green
   because step 2 removed the last core→plugin import, so it goes first and
   becomes the gate the 10-16 moves are verified against.
4. **backlog/cli-lazy-subcommand-loading.md** — hard prerequisite for the
   split (installing precis-util + precis-catpath dies importing
   precis.cli.taproot) and independently closes the outage class that killed
   every node's embedder. Below 3 only because 3 is behaviour-neutral and
   this touches 58 modules.

## Horizon

Milestones 2–4 and 6 are specced inside plugin-split-runtime-shell.md's
post-10-16 ordering and share its pointer; they become items as each comes
into reach.

1. **backlog/plugin-split-runtime-shell.md + backlog/package-split.md** —
   the 2026-10-16 gate: the four open calls answered (se placement,
   "private until paper" meaning, package-split's fate, orphan-table
   ownership) and package-split ruled superseded or merged. Waits on Reto;
   nothing below starts until it closes.
2. **backlog/plugin-split-runtime-shell.md**, hexfold out first — waits on
   1, Do-next 3 (the import-boundary gate) and gr457894; proves the
   entry-point mechanics at zero API risk.
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
6. **backlog/pathway-presentation-shared-module.md** — catpath as the
   reference model, private until paper; waits on 5, which answers where
   shared presentation logic lives. Needs a catpath version bump + wheel
   redeploy.

## Parked

- (none beyond Horizon's stated waits)

## No action needed

- **autocatpath<0.23 ceiling** — ruled out: the parity gate is a live check
  against the installed engine.
- **Per-plugin Postgres schemas** — ruled out 2026-09-27: pgbouncer
  transaction pooling makes dynamic search_path a race.
- **Extracting precis_se as a peer plugin** — ruled: geometry application
  layer, not a domain model.
